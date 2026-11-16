"""Prefer a single source-backed full-street annotation in Studio drafts.

Dry-run by default. Only the active DRAFT annotation is considered; validated
documents and a user's own draft are never rewritten.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import unicodedata

import fitz

from jin_runtime.studio_contract import StudioContractError, normalize_studio_annotation, training_annotation_issues
from jin_runtime.studio_store import StudioStore


# Bill-to is eligible only when its source line is under an explicit billing
# heading. Buyer often shares that printed block and must not inherit it.
ROLES = ("SUPPLIER", "SHIP_TO", "BILL_TO")
ATOMIC = ("HOUSE_NUMBER", "HOUSE_NUMBER_SUFFIX", "STREET_TYPE", "STREET_NAME")


def normalize(text: object) -> str:
    value = unicodedata.normalize("NFKD", str(text or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def value(field: dict | None) -> str:
    if not field:
        return ""
    return str(field.get("corrected_value") or field.get("extracted_value") or "").strip()


def source_match(pdf: fitz.Document, text: str, preferred: dict | None) -> dict | None:
    """Return one printed line containing the full street, near prior evidence."""
    wanted = normalize(text)
    if not wanted:
        return None
    prior = (preferred or {}).get("evidence") or {}
    prior_page = prior.get("page")
    prior_box = prior.get("bbox") or []
    if not prior_page or len(prior_box) != 4:
        return None
    options: list[tuple[float, dict]] = []
    for page_index in range(min(len(pdf), 20)):
        page = pdf[page_index]
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                line_text = " ".join(span.get("text", "") for span in spans).strip()
                if wanted != normalize(line_text):
                    continue
                bounds = line["bbox"]
                rect = [
                    round(bounds[0] / page.rect.width * 1000),
                    round(bounds[1] / page.rect.height * 1000),
                    round(bounds[2] / page.rect.width * 1000),
                    round(bounds[3] / page.rect.height * 1000),
                ]
                if page_index + 1 != int(prior_page):
                    continue
                distance = abs((rect[1] + rect[3]) / 2 - (prior_box[1] + prior_box[3]) / 2)
                if distance > 20:
                    continue
                options.append((distance, {"text": line_text, "evidence": {"page": page_index + 1, "bbox": rect}}))
    if not options:
        return None
    options.sort(key=lambda item: item[0])
    return options[0][1]


def update_fields(fields: list[dict], pdf: fitz.Document, roles: tuple[str, ...] = ROLES) -> tuple[list[dict], list[str]]:
    updated = copy.deepcopy(fields)
    changed_roles: list[str] = []
    for role in roles:
        by_label = {field["label"]: field for field in updated}
        street = by_label.get(f"{role}_STREET")
        parts = {suffix: by_label.get(f"{role}_{suffix}") for suffix in ATOMIC}
        house = value(parts["HOUSE_NUMBER"])
        suffix = value(parts["HOUSE_NUMBER_SUFFIX"])
        street_type = value(parts["STREET_TYPE"])
        street_name = value(parts["STREET_NAME"])
        current = value(street)
        if not any(parts.values()):
            continue
        if current:
            full = current
            if house and not normalize(current).startswith(normalize(house) + " "):
                prefix = " ".join(filter(None, (house, suffix)))
                full = f"{prefix} {current}"
        elif street_type and street_name:
            full = " ".join(filter(None, (house, suffix, street_type, street_name)))
        else:
            continue
        preferred = street or next((part for part in parts.values() if part and part.get("evidence")), None)
        match = source_match(pdf, full, preferred)
        if not match:
            continue
        if role == "BILL_TO":
            from scripts.studio_annotation_pdf_audit import explicit_address_block_role

            if explicit_address_block_role(pdf, match["evidence"]) != "BILL_TO":
                continue
        # A full street must be visible on one source line. Do not silently
        # discard components when OCR or layout prevents proving the merge.
        full = match["text"]
        if street:
            street["status"] = "CORRECTED"
            street["corrected_value"] = full
            street["evidence"] = match["evidence"]
            street["comment"] = "Voie complète relue dans le PDF ; composants séparés retirés. À vérifier par l'utilisateur."
        else:
            updated.append(
                {
                    "path": f"order.addresses.{role.lower()}.selected.components.street",
                    "label": f"{role}_STREET",
                    "status": "CORRECTED",
                    "extracted_value": None,
                    "corrected_value": full,
                    "evidence": match["evidence"],
                    "comment": "Voie complète relue dans le PDF ; composants séparés retirés. À vérifier par l'utilisateur.",
                }
            )
        updated = [field for field in updated if field not in [part for part in parts.values() if part]]
        changed_roles.append(role)
    return updated, changed_roles


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Save new DRAFT revisions")
    parser.add_argument("--document-id", help="Restrict the plan to one Studio document")
    parser.add_argument("--fields-json", action="store_true", help="Print the planned field annotations as JSON")
    parser.add_argument("--role", choices=ROLES, help="Only merge one business address role")
    args = parser.parse_args()
    if args.apply and os.name == "nt":
        parser.error(
            "Do not write the bind-mounted Studio SQLite database from Windows "
            "while Linux services may use it; save through the Studio API/MCP instead."
        )
    store = StudioStore()
    changes = []
    pending = []
    skipped = []
    for document in store.list_documents(limit=10000):
        if args.document_id and document["documentId"] != args.document_id:
            continue
        if document["status"] != "DRAFT":
            continue
        detail = store.get_document(document["documentId"])
        annotation = detail["latestAnnotation"]
        if not (
            str(annotation["reviewed_by"]).startswith("jin-model-")
            or annotation["reviewed_by"] in {"jin-street-campaign", "jin-pdf-audit", "jin-pdf-locator", "jin-geometry-qa"}
        ):
            continue
        with fitz.open(store.pdf_path(document["documentId"])) as pdf:
            fields, roles = update_fields(annotation["field_annotations"], pdf, (args.role,) if args.role else ROLES)
        if not roles:
            continue
        payload = {
            "status": "DRAFT",
            "reviewed_by": "jin-street-campaign",
            "field_annotations": fields,
            "note": "Voie(s) complète(s) vérifiée(s) sur le PDF, sans annotation séparée du numéro/type/nom. Les autres champs restent à relire.",
        }
        try:
            normalize_studio_annotation(
                payload,
                document_id=document["documentId"],
                document_sha256=detail["pdfSha256"],
                revision=annotation["revision"] + 1,
            )
        except StudioContractError as error:
            skipped.append({"document_id": document["documentId"], "reason": str(error)})
            continue
        before = training_annotation_issues(annotation["field_annotations"])
        after = training_annotation_issues(fields)
        conflicts = {"CONFLICTING_REGION_LABELS", "DUPLICATE_SOURCE_REGION", "OVERLAPPING_COMPOSITE_STREET"}
        if sum(issue["code"] in conflicts for issue in after) > sum(issue["code"] in conflicts for issue in before):
            skipped.append({"document_id": document["documentId"], "reason": "new source-region conflict"})
            continue
        changes.append({"document_id": document["documentId"], "filename": document["filename"], "roles": roles})
        pending.append((document["documentId"], payload))
    if args.apply:
        for document_id, payload in pending:
            store.save_annotation(document_id, payload)
    if args.fields_json:
        print(json.dumps({"pending": [{"document_id": document_id, "field_annotations": payload["field_annotations"], "note": payload["note"]} for document_id, payload in pending], "skipped": skipped}, ensure_ascii=False))
    else:
        print(json.dumps({"mode": "apply" if args.apply else "dry_run", "documents": len(changes), "street_roles": sum(len(item["roles"]) for item in changes), "skipped": skipped, "details": changes}, ensure_ascii=False))


if __name__ == "__main__":
    main()

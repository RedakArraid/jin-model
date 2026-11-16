"""Quarantine unsafe machine-confirmed Studio boxes, preserving human decisions.

Some PDFs expose native text outside the rendered page. A matching string in
that text is not sufficient evidence for a visual annotation. This script
retracts only confirmations made by the automated PDF audit/locator, leaving
the field as AMBIGUOUS in a DRAFT for manual review.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re

import fitz

from scripts.studio_annotation_pdf_audit import explicit_address_block_role, folded
from jin_runtime.studio_store import StudioStore


AUTOMATIC_COMMENTS = (
    "Valeur contrôlée dans son rectangle du PDF",
    "Occurrence unique retrouvée dans le PDF",
)
NAME_STOPWORDS = {
    "tva", "ht", "ttc", "total", "date", "port", "fax", "tel", "telephone",
    "reference", "commande", "code", "france", "eur", "euro", "siren", "siret",
}
ADDRESS_SUFFIXES = {
    "HOUSE_NUMBER", "HOUSE_NUMBER_SUFFIX", "STREET", "STREET_TYPE", "STREET_NAME",
    "POSTAL_CODE", "CITY", "COUNTRY", "COUNTRY_CODE", "INDUSTRIAL_ZONE",
    "ACTIVITY_PARK", "CS", "BP", "CEDEX",
}


def geometry_health(pdf: fitz.Document) -> dict:
    total = bad = 0
    worst_page = 0.0
    for page in pdf:
        words = page.get_text("words")
        if not words:
            continue
        off_page = sum(
            word[0] < -1 or word[1] < -1
            or word[2] > page.rect.width + 1 or word[3] > page.rect.height + 1
            for word in words
        )
        total += len(words)
        bad += off_page
        worst_page = max(worst_page, off_page / len(words))
    ratio = bad / total if total else 0.0
    return {"words": total, "off_page_words": bad, "off_page_ratio": round(ratio, 4), "worst_page_ratio": round(worst_page, 4), "unsafe": total > 20 and (ratio >= 0.08 or worst_page >= 0.15)}


def party_name_nearby(fields: list[dict], role: str, evidence: dict) -> bool:
    page = evidence.get("page")
    box = evidence.get("bbox") or []
    if len(box) != 4:
        return False
    x = (box[0] + box[2]) / 2
    y = (box[1] + box[3]) / 2
    for other in fields:
        if other.get("label") != role + "_NAME":
            continue
        other_box = (other.get("evidence") or {}).get("bbox") or []
        if len(other_box) != 4 or (other.get("evidence") or {}).get("page") != page:
            continue
        ox = (other_box[0] + other_box[2]) / 2
        oy = (other_box[1] + other_box[3]) / 2
        if abs(x - ox) <= 230 and abs(y - oy) <= 180:
            return True
    return False


def unsafe_reason(field: dict, fields: list[dict], pdf: fitz.Document, geometry: dict) -> str:
    evidence = field.get("evidence") or {}
    box = evidence.get("bbox") or []
    if geometry["unsafe"]:
        return f"géométrie PDF non fiable ({geometry['off_page_words']}/{geometry['words']} mots hors page)"
    if len(box) != 4 or min(box) < 0 or max(box) > 1000 or box[0] >= box[2] or box[1] >= box[3]:
        return "rectangle source invalide"
    if box[0] == 0 or box[1] == 0 or box[2] == 1000 or box[3] == 1000:
        return "rectangle collé au bord de page"
    label = str(field.get("label") or "")
    value = str(field.get("corrected_value") or field.get("extracted_value") or "").strip()
    if label.endswith("_NAME") and label.split("_", 1)[0] in {"BUYER", "BILL", "SHIP", "SUPPLIER"}:
        simple = re.sub(r"[^a-z]+", "", folded(value))
        if simple in NAME_STOPWORDS or len(simple) < 3 or not re.search(r"[a-z]", simple):
            return "nom de partie manifestement non plausible"
    for role in ("BUYER", "BILL_TO", "SHIP_TO", "SUPPLIER"):
        if not label.startswith(role + "_"):
            continue
        suffix = label[len(role) + 1 :]
        if suffix not in ADDRESS_SUFFIXES and suffix != "NAME":
            break
        source_role = explicit_address_block_role(pdf, evidence)
        if role in {"BILL_TO", "SHIP_TO"} and source_role != role:
            return "rôle d'adresse non soutenu par un en-tête de bloc"
        if role in {"BUYER", "SUPPLIER"}:
            if source_role in {"BILL_TO", "SHIP_TO"}:
                return "valeur située dans un autre bloc d'adresse"
            if suffix in ADDRESS_SUFFIXES and not party_name_nearby(fields, role, evidence):
                return "adresse sans partie correspondante à proximité"
        if suffix == "POSTAL_CODE" and not re.fullmatch(r"\d{5}", value):
            return "code postal non plausible"
        if suffix == "CITY" and (not re.search(r"[A-Za-zÀ-ÿ]", value) or len(value) > 60):
            return "ville non plausible"
        break
    if label == "LINE_ITEM_REFERENCE":
        if not re.search(r"\d", value) or len(value) < 4 or len(value) > 32 or value.startswith("."):
            return "référence article non plausible"
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--document-id")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.apply and os.name == "nt":
        parser.error("Apply through Linux container only")
    store = StudioStore()
    details = []
    total: Counter[str] = Counter()
    for document in store.list_documents(limit=10000):
        if document["status"] != "DRAFT" or args.document_id and args.document_id != document["documentId"]:
            continue
        before = (document.get("latestAnnotation") or {}).get("field_annotations") or []
        target_indices = [
            index for index, field in enumerate(before)
            if field.get("status") in {"CONFIRMED", "CORRECTED"}
            and str(field.get("comment") or "").startswith(AUTOMATIC_COMMENTS)
        ]
        if not target_indices:
            continue
        try:
            with fitz.open(store.pdf_path(document["documentId"])) as pdf:
                geometry = geometry_health(pdf)
                fields = [dict(field) for field in before]
                changed = []
                for index in target_indices:
                    reason = unsafe_reason(fields[index], fields, pdf, geometry)
                    if not reason:
                        continue
                    fields[index]["status"] = "AMBIGUOUS"
                    fields[index]["comment"] = f"Contrôle visuel/sémantique : {reason}. Revoir le PDF et redessiner si nécessaire."
                    changed.append({"label": fields[index]["label"], "path": fields[index]["path"], "reason": reason})
            if args.apply and changed:
                store.save_annotation(
                    document["documentId"],
                    {
                        "status": "DRAFT",
                        "reviewed_by": "jin-geometry-qa",
                        "field_annotations": fields,
                        "note": "Retrait des confirmations automatiques non fiables. Les décisions humaines et les autres champs sont conservés.",
                    },
                )
                total["saved"] += 1
            total["audited"] += 1
            total["downgraded"] += len(changed)
            if geometry["unsafe"]:
                total["unsafe_geometry_documents"] += 1
            details.append({"document_id": document["documentId"], "filename": document["filename"], "geometry": geometry, "changed": changed if args.document_id else len(changed)})
        except Exception as error:
            total["errors"] += 1
            details.append({"document_id": document["documentId"], "filename": document["filename"], "error": str(error)})
    report = {"mode": "apply" if args.apply else "dry_run", "summary": total, "details": details}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=dict), encoding="utf-8")
    print(json.dumps({"mode": report["mode"], "summary": total, "errors": [x for x in details if "error" in x], "details": details if args.document_id else []}, ensure_ascii=False, default=dict))


if __name__ == "__main__":
    main()

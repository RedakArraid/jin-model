"""Conservatively review Studio draft preannotations against their source PDFs.

This does not validate documents for training. It turns a preannotation into a
CONFIRMED draft only when its own printed rectangle supports the value. Other
preannotations become AMBIGUOUS, with a reason for the human reviewer. Existing
human decisions and validated documents are never changed.

Run from a Linux container sharing the Studio volume. In particular, do not
write the bind-mounted SQLite database from Windows while Docker uses it.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import unicodedata

import fitz

from jin_runtime.studio_contract import training_annotation_issues
from jin_runtime.studio_store import StudioStore


AUTOMATIC_REVIEWERS = {
    "jin-model-preannotation",
    "jin-model-assisted",
    "jin-street-campaign",
    "jin-pdf-audit",
    "mcp-service",
}
ORDER_ANCHORS = re.compile(r"commande|command\s*n|order|votre\s+r[ée]f|bon\s+de\s+commande", re.I)
DATE_ANCHORS = re.compile(r"date|du\s+jour|[ée]mis\s+le", re.I)
LINE_NUMBER_ANCHORS = re.compile(r"n[°o]?\s*(?:de\s+)?ligne|poste|position", re.I)
LINE_COLUMN_PATTERNS = {
    "LINE_ITEM_REFERENCE": re.compile(r"\b(?:article|reference|ref|code|materiel|material)\b"),
    "LINE_ITEM_DESCRIPTION": re.compile(r"\b(?:designation|description|libelle|produit|product)\b"),
    "LINE_ITEM_QUANTITY": re.compile(r"\b(?:qte|quantite|qty|quant|qtee)\b"),
    "LINE_ITEM_UNIT": re.compile(r"\b(?:unite|unit|u\.m|uom)\b"),
    "LINE_ITEM_UNIT_PRICE": re.compile(r"\b(?:prix|price|p\.u|pu)\b"),
    "LINE_ITEM_TOTAL": re.compile(r"\b(?:montant|total|amount)\b"),
}


def folded(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char)).casefold()


def normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", folded(value))


def value_matches(label: str, expected: object, printed: str) -> bool:
    wanted = normalized(expected)
    observed = normalized(printed)
    if not wanted or not observed:
        return False
    if label == "DOCUMENT_TYPE":
        if wanted == "purchaseorder":
            return bool(re.search(r"\b(?:bon\s+de\s+commande|purchase\s+order|commande\s+(?:client|fournisseur))\b", folded(printed)))
        return False
    if label.endswith("_DATE") or label == "ORDER_DATE":
        iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(expected or ""))
        if iso:
            year, month, day = iso.groups()
            return any(form in observed for form in (year + month + day, day + month + year))
    if label in {"LINE_ITEM_QUANTITY", "LINE_ITEM_UNIT_PRICE", "LINE_ITEM_TOTAL", "ADDITIONAL_CHARGE_AMOUNT"} or label.startswith("TOTAL_"):
        try:
            number = float(str(expected).replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            return False
        if number < 0:
            return False
        candidates = [f"{number:.3f}", f"{number:.2f}", f"{number:g}"]
        for candidate in candidates:
            if normalized(candidate) == observed:
                return True
        return False
    # Short values must be whole tokens. This prevents a quantity of 1 from
    # being accepted in 10, or a street number from matching a postal code.
    if len(wanted) < 5:
        return wanted in [normalized(token) for token in re.findall(r"[\wÀ-ÿ]+", printed)]
    return wanted == observed or (len(wanted) >= 8 and wanted in observed)


def printed_region(pdf: fitz.Document, evidence: dict | None) -> tuple[str, str]:
    if not isinstance(evidence, dict):
        return "", "missing rectangle"
    box = evidence.get("bbox")
    try:
        page_number = int(evidence.get("page"))
        if not isinstance(box, (list, tuple)) or len(box) != 4 or not 1 <= page_number <= len(pdf):
            return "", "invalid rectangle or page"
        page = pdf[page_number - 1]
        x0, y0, x1, y1 = (float(v) for v in box)
        if not (0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000):
            return "", "invalid rectangle coordinates"
        rect = fitz.Rect(
            x0 * page.rect.width / 1000 - 1,
            y0 * page.rect.height / 1000 - 1,
            x1 * page.rect.width / 1000 + 1,
            y1 * page.rect.height / 1000 + 1,
        )
        text = page.get_text("text", clip=rect).strip()
        return text, "" if text else "no extractable text in rectangle"
    except (TypeError, ValueError, IndexError):
        return "", "invalid rectangle"


def nearby_text(pdf: fitz.Document, evidence: dict, *, horizontal: float = 150, vertical: float = 40) -> str:
    page = pdf[int(evidence["page"]) - 1]
    x0, y0, x1, y1 = (float(v) for v in evidence["bbox"])
    rect = fitz.Rect(
        max(0, (x0 - horizontal) * page.rect.width / 1000),
        max(0, (y0 - vertical) * page.rect.height / 1000),
        min(page.rect.width, (x1 + 15) * page.rect.width / 1000),
        min(page.rect.height, (y1 + 15) * page.rect.height / 1000),
    )
    return page.get_text("text", clip=rect)


def explicit_address_block_role(pdf: fitz.Document, evidence: dict) -> str:
    """Find the nearest printed address-block heading above this field."""
    page = pdf[int(evidence["page"]) - 1]
    box = evidence["bbox"]
    field_center = (float(box[0]) + float(box[2])) / 2
    field_top = float(box[1])
    candidates: list[tuple[float, str]] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            line_text = folded(" ".join(span.get("text", "") for span in line.get("spans", [])))
            role = ""
            if len(line_text) > 65:
                continue
            if re.search(r"factur[ée]?\s*[aà]|adresse\s+de\s+facturation|bill\s*to", line_text):
                role = "BILL_TO"
            elif re.search(r"adresse\s+de\s+livraison|livr[ée]?\s*[aà]|ship\s*to", line_text):
                role = "SHIP_TO"
            if not role:
                continue
            line_box = line["bbox"]
            header_center = (line_box[0] + line_box[2]) / 2 / page.rect.width * 1000
            header_bottom = line_box[3] / page.rect.height * 1000
            vertical_gap = field_top - header_bottom
            horizontal_gap = abs(field_center - header_center)
            if -5 <= vertical_gap <= 135 and horizontal_gap <= 280:
                candidates.append((vertical_gap + horizontal_gap * 0.4, role))
    return min(candidates)[1] if candidates else ""


def printed_line_column(pdf: fitz.Document, evidence: dict) -> str:
    """Infer the table column from positioned printed header spans."""
    page = pdf[int(evidence["page"]) - 1]
    box = evidence["bbox"]
    field_center = (float(box[0]) + float(box[2])) / 2
    field_top = float(box[1])
    candidates: list[tuple[float, str]] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = folded(span.get("text", ""))
                box = span["bbox"]
                center = (box[0] + box[2]) / 2 / page.rect.width * 1000
                bottom = box[3] / page.rect.height * 1000
                vertical_gap = field_top - bottom
                horizontal_gap = abs(field_center - center)
                if not 0 <= vertical_gap <= 620 or horizontal_gap > 95:
                    continue
                for label, pattern in LINE_COLUMN_PATTERNS.items():
                    if pattern.search(text):
                        candidates.append((vertical_gap * 0.5 + horizontal_gap, label))
    return min(candidates)[1] if candidates else ""


def review_field(field: dict, pdf: fitz.Document) -> str:
    if field.get("status") != "UNREVIEWED":
        return field.get("status") or ""
    label = str(field.get("label") or "")
    expected = field.get("extracted_value")
    evidence = field.get("evidence")
    printed, reason = printed_region(pdf, evidence)
    if reason or not value_matches(label, expected, printed):
        field["status"] = "AMBIGUOUS"
        field["comment"] = (
            "Audit PDF : la valeur préremplie n'est pas prouvée par son cadre "
            f"({reason or 'texte différent'}). Vérifier et recadrer manuellement."
        )
        return "AMBIGUOUS"
    if label == "LINE_ITEM_REFERENCE":
        raw = str(expected or "").strip()
        if not re.search(r"\d", raw) or len(raw) > 32 or len(raw) < 4 or raw.startswith(".") or len(raw.split()) > 2:
            field["status"] = "AMBIGUOUS"
            field["comment"] = "Audit PDF : référence atypique ; contrôler qu'il s'agit bien du code article complet."
            return "AMBIGUOUS"
    if label in LINE_COLUMN_PATTERNS:
        column = printed_line_column(pdf, evidence)
        if column != label:
            field["status"] = "AMBIGUOUS"
            field["comment"] = "Audit PDF : valeur visible mais colonne de tableau non confirmée ou différente."
            return "AMBIGUOUS"
    if label.startswith(("BUYER_", "BILL_TO_", "SHIP_TO_", "SUPPLIER_")) and evidence:
        # A source-backed value can still have the wrong business role. In
        # particular, JIN has mapped 'Facturé à' blocks to BUYER addresses.
        block_role = explicit_address_block_role(pdf, evidence)
        address_part = any(
            label.endswith("_" + suffix)
            for suffix in (
                "NAME", "STREET", "STREET_TYPE", "STREET_NAME", "HOUSE_NUMBER",
                "HOUSE_NUMBER_SUFFIX", "POSTAL_CODE", "CITY", "INDUSTRIAL_ZONE",
                "ACTIVITY_PARK", "CS", "BP", "COUNTRY", "COUNTRY_CODE",
            )
        )
        if address_part and label.startswith("BUYER_") and block_role == "BILL_TO":
            field["status"] = "AMBIGUOUS"
            field["comment"] = "Audit PDF : la zone est libellée Facturation ; ne pas la confondre avec l'adresse acheteur."
            return "AMBIGUOUS"
        if address_part and label.startswith(("BUYER_", "BILL_TO_", "SUPPLIER_")) and block_role == "SHIP_TO":
            field["status"] = "AMBIGUOUS"
            field["comment"] = "Audit PDF : la zone est libellée Livraison ; vérifier son rôle avant apprentissage."
            return "AMBIGUOUS"
    if label == "LINE_ITEM_NUMBER":
        vicinity = nearby_text(pdf, evidence, horizontal=20, vertical=75)
        if not LINE_NUMBER_ANCHORS.search(vicinity):
            field["status"] = "AMBIGUOUS"
            field["comment"] = "Audit PDF : chiffre visible, mais la colonne « numéro de ligne » n'est pas établie."
            return "AMBIGUOUS"
    if label == "ORDER_NUMBER" and not ORDER_ANCHORS.search(nearby_text(pdf, evidence)):
        field["status"] = "AMBIGUOUS"
        field["comment"] = "Audit PDF : numéro visible mais son rôle de numéro de commande reste à confirmer."
        return "AMBIGUOUS"
    if label == "ORDER_DATE" and not DATE_ANCHORS.search(nearby_text(pdf, evidence)):
        field["status"] = "AMBIGUOUS"
        field["comment"] = "Audit PDF : date visible mais son rôle de date de commande reste à confirmer."
        return "AMBIGUOUS"
    if label in {"CUSTOMER_CODE", "SUPPLIER_CODE"}:
        context = folded(nearby_text(pdf, evidence))
        role = "client" if label == "CUSTOMER_CODE" else "fournisseur"
        if role not in context:
            field["status"] = "AMBIGUOUS"
            field["comment"] = f"Audit PDF : code visible, mais le libellé « {role} » n'est pas établi."
            return "AMBIGUOUS"
    if label in {"TOTAL_SUBTOTAL", "TOTAL_NET", "TOTAL_GROSS", "TOTAL_AMOUNT_DUE"}:
        context = folded(nearby_text(pdf, evidence, horizontal=160, vertical=20))
        if not re.search(r"total|sous.total|montant|net\s+a\s+payer", context):
            field["status"] = "AMBIGUOUS"
            field["comment"] = "Audit PDF : montant visible, mais le libellé du total n'est pas établi."
            return "AMBIGUOUS"
    field["status"] = "CONFIRMED"
    field["comment"] = "Valeur contrôlée dans son rectangle du PDF ; préannotation à relire par l'utilisateur."
    return "CONFIRMED"


def avoid_new_training_conflicts(before: list[dict], updated: list[dict]) -> int:
    """Downgrade only new confirmations until no new overlap blocker remains."""
    baseline = Counter(issue["code"] for issue in training_annotation_issues(before))
    downgraded = 0
    for _ in range(len(updated)):
        issues = training_annotation_issues(updated)
        seen: Counter[str] = Counter()
        new_issue = None
        for issue in issues:
            seen[issue["code"]] += 1
            if seen[issue["code"]] > baseline[issue["code"]]:
                new_issue = issue
                break
        if new_issue is None:
            break
        indexes = [new_issue["fieldIndex"]]
        if "otherFieldIndex" in new_issue:
            indexes.append(new_issue["otherFieldIndex"])
        candidates = [i for i in indexes if before[i].get("status") not in {"CONFIRMED", "CORRECTED"} and updated[i].get("status") in {"CONFIRMED", "CORRECTED"}]
        if not candidates:
            break
        for i in candidates:
            updated[i]["status"] = "AMBIGUOUS"
            updated[i]["comment"] = f"Audit PDF : conflit de zones source ({new_issue['code']}) ; choisir le bon rôle/cadre."
            downgraded += 1
    return downgraded


def is_machine_draft(document: dict, annotation: dict) -> bool:
    if document.get("status") != "DRAFT":
        return False
    reviewer = str(annotation.get("reviewed_by") or "")
    return reviewer in AUTOMATIC_REVIEWERS or reviewer.startswith("jin-model-")


def audit_document(document: dict, store: StudioStore) -> tuple[list[dict] | None, dict]:
    detail = store.get_document(document["documentId"])
    annotation = detail.get("latestAnnotation") or {}
    if not is_machine_draft(document, annotation):
        return None, {"document_id": document["documentId"], "reason": "human draft or validated"}
    before = annotation["field_annotations"]
    fields = [dict(field) for field in before]
    counts: Counter[str] = Counter()
    with fitz.open(store.pdf_path(document["documentId"])) as pdf:
        for field in fields:
            if field.get("status") == "UNREVIEWED":
                counts[review_field(field, pdf)] += 1
    counts["conflicts_downgraded"] = avoid_new_training_conflicts(before, fields)
    counts["remaining_unreviewed"] = sum(field.get("status") == "UNREVIEWED" for field in fields)
    return fields, {"document_id": document["documentId"], "filename": document["filename"], "counts": counts}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--document-id")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.apply and __import__("os").name == "nt":
        parser.error("Apply through the Linux API/container, never from Windows against the shared SQLite DB")
    store = StudioStore()
    details = []
    summary: Counter[str] = Counter()
    for document in store.list_documents(limit=10000):
        if args.document_id and document["documentId"] != args.document_id:
            continue
        try:
            fields, result = audit_document(document, store)
            if fields is None:
                summary["skipped"] += 1
            else:
                if args.document_id:
                    result["fields"] = [
                        {"label": field.get("label"), "status": field.get("status"), "value": field.get("corrected_value") or field.get("extracted_value"), "comment": field.get("comment")}
                        for field in fields
                    ]
                if args.apply and result["counts"]["CONFIRMED"] + result["counts"]["AMBIGUOUS"]:
                    store.save_annotation(
                        document["documentId"],
                        {
                            "status": "DRAFT",
                            "reviewed_by": "jin-pdf-audit",
                            "field_annotations": fields,
                            "note": "Audit automatique conservateur des rectangles PDF. Les CONFIRMED ont une correspondance source ; AMBIGUOUS exige une décision humaine. Aucune validation pour l'apprentissage.",
                        },
                    )
                    summary["saved"] += 1
                summary.update(result["counts"])
                summary["audited"] += 1
        except Exception as error:  # report remaining work, do not hide it
            result = {"document_id": document["documentId"], "filename": document["filename"], "error": str(error)}
            summary["errors"] += 1
        details.append(result)
    report = {"mode": "apply" if args.apply else "dry_run", "summary": summary, "details": details}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=dict), encoding="utf-8")
    print(json.dumps({"mode": report["mode"], "summary": summary, "errors": [x for x in details if "error" in x], "details": details if args.document_id else []}, ensure_ascii=False, default=dict))


if __name__ == "__main__":
    main()

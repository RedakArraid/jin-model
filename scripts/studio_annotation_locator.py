"""Locate unresolved Studio draft values in their PDF, without human validation.

Only machine-audited drafts are eligible. A unique printed occurrence is used
only when its business block and (for line items) table row/column agree.
Values not meeting those conditions stay AMBIGUOUS for the reviewer.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path
import re

import fitz

from scripts.studio_annotation_pdf_audit import (
    avoid_new_training_conflicts,
    folded,
    LINE_COLUMN_PATTERNS,
    normalized,
    nearby_text,
    value_matches,
)
from jin_runtime.studio_store import StudioStore


LINE_PATH = re.compile(r"order\.line_items\[(\d+)\]")


def lookup_keys(field: dict) -> set[str]:
    value = field.get("corrected_value") or field.get("extracted_value")
    raw = str(value or "").strip()
    keys = {normalized(raw)} if raw else set()
    label = field.get("label")
    if label in {"LINE_ITEM_QUANTITY", "LINE_ITEM_UNIT_PRICE", "LINE_ITEM_TOTAL", "ADDITIONAL_CHARGE_AMOUNT"} or str(label).startswith("TOTAL_"):
        try:
            number = float(raw.replace(" ", "").replace(",", "."))
            keys.update(normalized(x) for x in (f"{number:g}", f"{number:.1f}", f"{number:.2f}", f"{number:.3f}"))
        except ValueError:
            pass
    if str(label).endswith("_DATE"):
        iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if iso:
            year, month, day = iso.groups()
            keys.update((year + month + day, day + month + year))
    return {key for key in keys if len(key) >= 4 or label in {"LINE_ITEM_QUANTITY", "LINE_ITEM_UNIT_PRICE", "LINE_ITEM_TOTAL"}}


def indexed_occurrences(pdf: fitz.Document, keys: set[str]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = defaultdict(list)
    reference_keys = {key for key in keys if len(key) >= 7 and any(char.isdigit() for char in key)}
    for page_index, page in enumerate(pdf):
        lines: dict[tuple[int, int], list[tuple]] = defaultdict(list)
        for word in page.get_text("words"):
            if len(word) >= 8:
                lines[(word[5], word[6])].append(word)
        for words in lines.values():
            words.sort(key=lambda word: (word[7], word[0]))
            for start in range(len(words)):
                phrase = []
                for stop in range(start, min(start + 7, len(words))):
                    word = words[stop]
                    if phrase and word[0] - phrase[-1][2] > 24:
                        break
                    phrase.append(word)
                    text = " ".join(piece[4] for piece in phrase)
                    key = normalized(text)
                    extra_keys = {key} & keys
                    if len(phrase) == 1 and reference_keys:
                        extra_keys.update(candidate for candidate in reference_keys if len(key) > len(candidate) and len(key) - len(candidate) <= 3 and key.endswith(candidate))
                    if not extra_keys:
                        continue
                    x0 = min(piece[0] for piece in phrase)
                    y0 = min(piece[1] for piece in phrase)
                    x1 = max(piece[2] for piece in phrase)
                    y1 = max(piece[3] for piece in phrase)
                    # Some malformed PDFs expose invisible text far outside
                    # the rendered page. Clamping such coordinates would make
                    # a seemingly valid but visually false annotation box.
                    if not (0 <= x0 < x1 <= page.rect.width and 0 <= y0 < y1 <= page.rect.height):
                        continue
                    nx0 = max(0, min(999, math.floor(x0 / page.rect.width * 1000)))
                    ny0 = max(0, min(999, math.floor(y0 / page.rect.height * 1000)))
                    nx1 = max(nx0 + 1, min(1000, math.ceil(x1 / page.rect.width * 1000)))
                    ny1 = max(ny0 + 1, min(1000, math.ceil(y1 / page.rect.height * 1000)))
                    candidate = {
                        "text": text,
                        "evidence": {
                            "page": page_index + 1,
                            "bbox": [
                                nx0,
                                ny0,
                                nx1,
                                ny1,
                            ],
                        },
                    }
                    for extra_key in extra_keys:
                        index[extra_key].append(candidate)
    return index


def line_anchor(fields: list[dict], field: dict) -> dict | None:
    match = LINE_PATH.search(str(field.get("path") or ""))
    if not match:
        return None
    row = match.group(1)
    options = [
        other for other in fields
        if other is not field and LINE_PATH.search(str(other.get("path") or ""))
        and LINE_PATH.search(other["path"]).group(1) == row
        and (other.get("evidence") or {}).get("bbox")
    ]
    options.sort(key=lambda other: (other.get("label") != "LINE_ITEM_DESCRIPTION", other.get("status") == "AMBIGUOUS"))
    return options[0] if options else None


def page_context(pdf: fitz.Document) -> dict[int, dict[str, list[tuple]]]:
    """Read headings once per page instead of once per candidate value."""
    context = {}
    for page_index, page in enumerate(pdf):
        addresses = []
        columns = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                line_text = folded(" ".join(span.get("text", "") for span in line.get("spans", [])))
                bounds = line["bbox"]
                center = (bounds[0] + bounds[2]) / 2 / page.rect.width * 1000
                bottom = bounds[3] / page.rect.height * 1000
                if len(line_text) <= 65:
                    if re.search(r"factur[ée]?\s*[aà]|adresse\s+de\s+facturation|bill\s*to", line_text):
                        addresses.append((center, bottom, "BILL_TO"))
                    elif re.search(r"adresse\s+de\s+livraison|livr[ée]?\s*[aà]|ship\s*to", line_text):
                        addresses.append((center, bottom, "SHIP_TO"))
                for span in line.get("spans", []):
                    text = folded(span.get("text", ""))
                    box = span["bbox"]
                    span_center = (box[0] + box[2]) / 2 / page.rect.width * 1000
                    span_bottom = box[3] / page.rect.height * 1000
                    for label, pattern in LINE_COLUMN_PATTERNS.items():
                        if pattern.search(text):
                            columns.append((span_center, span_bottom, label))
        context[page_index + 1] = {"addresses": addresses, "columns": columns}
    return context


def block_role(context: dict, evidence: dict) -> str:
    box = evidence["bbox"]
    center = (box[0] + box[2]) / 2
    top = box[1]
    candidates = [
        (top - bottom + abs(center - header_center) * 0.4, role)
        for header_center, bottom, role in context[evidence["page"]]["addresses"]
        if -5 <= top - bottom <= 135 and abs(center - header_center) <= 280
    ]
    return min(candidates)[1] if candidates else ""


def column_role(context: dict, evidence: dict) -> str:
    box = evidence["bbox"]
    center = (box[0] + box[2]) / 2
    top = box[1]
    candidates = [
        ((top - bottom) * 0.5 + abs(center - header_center), label)
        for header_center, bottom, label in context[evidence["page"]]["columns"]
        if 0 <= top - bottom <= 620 and abs(center - header_center) <= 95
    ]
    return min(candidates)[1] if candidates else ""


def context_accepts(pdf: fitz.Document, context: dict, fields: list[dict], field: dict, candidate: dict) -> bool:
    label = str(field.get("label") or "")
    evidence = candidate["evidence"]
    if label.startswith(("BUYER_", "BILL_TO_", "SHIP_TO_", "SUPPLIER_")):
        source_role = block_role(context, evidence)
        if label.startswith("BILL_TO_") and source_role != "BILL_TO":
            return False
        if label.startswith("SHIP_TO_") and source_role != "SHIP_TO":
            return False
        if label.startswith(("BUYER_", "SUPPLIER_")) and source_role in {"BILL_TO", "SHIP_TO"}:
            return False
    if label == "ORDER_NUMBER" and not re.search(r"commande|order|votre\s+ref", folded(nearby_text(pdf, evidence))):
        return False
    if label == "ORDER_DATE" and not re.search(r"date|emis\s+le", folded(nearby_text(pdf, evidence))):
        return False
    if label in {"CUSTOMER_CODE", "SUPPLIER_CODE"}:
        anchor = "client" if label == "CUSTOMER_CODE" else "fournisseur"
        if anchor not in folded(nearby_text(pdf, evidence)):
            return False
    if label in {"LINE_ITEM_REFERENCE", "LINE_ITEM_DESCRIPTION", "LINE_ITEM_QUANTITY", "LINE_ITEM_UNIT", "LINE_ITEM_UNIT_PRICE", "LINE_ITEM_TOTAL"}:
        anchor = line_anchor(fields, field)
        if anchor is None:
            return False
        anchor_evidence = anchor["evidence"]
        if evidence["page"] != anchor_evidence["page"]:
            return False
        row_center = sum(evidence["bbox"][1::2]) / 2
        anchor_center = sum(anchor_evidence["bbox"][1::2]) / 2
        # Adjacent fee/DEEE subrows can be only 15-20 normalized units apart.
        # A narrow same-row tolerance prevents borrowing their quantity/price.
        tolerance = 16 if label == "LINE_ITEM_DESCRIPTION" else 9
        if abs(row_center - anchor_center) > tolerance:
            return False
        if column_role(context, evidence) != label:
            return False
        if label == "LINE_ITEM_REFERENCE" and not re.search(r"\d", candidate["text"]):
            return False
    if label.startswith("TOTAL_") and not re.search(r"total|sous.total|montant|net\s+a\s+payer", folded(nearby_text(pdf, evidence))):
        return False
    return True


def locate_fields(fields: list[dict], pdf: fitz.Document) -> tuple[list[dict], Counter[str]]:
    updated = [dict(field) for field in fields]
    eligible = [
        field for field in updated
        if field.get("status") == "AMBIGUOUS"
        and (
            "Préannotation non localisée" in str(field.get("comment") or "")
            or "Audit PDF" in str(field.get("comment") or "")
        )
        and field.get("label") not in {"DOCUMENT_TYPE", "LINE_ITEM_NUMBER"}
        and lookup_keys(field)
    ]
    keys = {key for field in eligible for key in lookup_keys(field)}
    index = indexed_occurrences(pdf, keys)
    context = page_context(pdf)
    result: Counter[str] = Counter()
    for field in eligible:
        keys = lookup_keys(field)
        candidates = [candidate for key in keys for candidate in index.get(key, [])]
        # A span may be reachable by several numeric normalizations.
        candidates = list({(candidate["evidence"]["page"], tuple(candidate["evidence"]["bbox"])): candidate for candidate in candidates}.values())
        candidates = [
            candidate for candidate in candidates
            if context_accepts(pdf, context, updated, field, candidate)
            and (value_matches(field["label"], field.get("extracted_value"), candidate["text"])
                 or (field["label"] == "LINE_ITEM_REFERENCE" and normalized(candidate["text"]).endswith(normalized(field.get("extracted_value")))))
        ]
        if len(candidates) != 1:
            continue
        candidate = candidates[0]
        old = normalized(field.get("extracted_value"))
        new = normalized(candidate["text"])
        if field["label"] == "LINE_ITEM_REFERENCE" and old != new:
            field["status"] = "CORRECTED"
            field["corrected_value"] = candidate["text"]
            result["corrected"] += 1
        else:
            field["status"] = "CONFIRMED"
            result["confirmed"] += 1
        field["evidence"] = candidate["evidence"]
        field["comment"] = "Occurrence unique retrouvée dans le PDF et cohérente avec la zone métier/colonne ; à relire par l'utilisateur."
    result["conflicts_downgraded"] = avoid_new_training_conflicts(fields, updated)
    return updated, result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--document-id")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.apply and os.name == "nt":
        parser.error("Apply through a Linux container, never Windows against shared Studio SQLite")
    store = StudioStore()
    details = []
    total: Counter[str] = Counter()
    for document in store.list_documents(limit=10000):
        if args.document_id and args.document_id != document["documentId"]:
            continue
        if document["status"] != "DRAFT":
            continue
        annotation = document.get("latestAnnotation") or {}
        if annotation.get("reviewed_by") not in {"jin-pdf-audit", "jin-pdf-locator"}:
            continue
        before = annotation["field_annotations"]
        try:
            with fitz.open(store.pdf_path(document["documentId"])) as pdf:
                fields, counts = locate_fields(before, pdf)
            changed = [
                {"label": after.get("label"), "path": after.get("path"), "status": after.get("status"), "value": after.get("corrected_value") or after.get("extracted_value"), "evidence": after.get("evidence")}
                for old, after in zip(before, fields)
                if old.get("status") != after.get("status")
            ]
            if args.apply and counts["confirmed"] + counts["corrected"] > counts["conflicts_downgraded"]:
                store.save_annotation(
                    document["documentId"],
                    {
                        "status": "DRAFT",
                        "reviewed_by": "jin-pdf-locator",
                        "field_annotations": fields,
                        "note": "Valeurs ambiguës localisées uniquement si occurrence unique et cohérente avec rôle/ligne/colonne. Brouillon à revoir, non validé pour l'apprentissage.",
                    },
                )
                total["saved"] += 1
            total.update(counts)
            total["audited"] += 1
            details.append({"document_id": document["documentId"], "filename": document["filename"], "counts": counts, "changed": changed if args.document_id else []})
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

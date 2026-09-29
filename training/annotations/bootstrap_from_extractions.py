#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

import fitz

FIELD_LABELS = {
    "building": "ADDRESS_BUILDING",
    "residence": "ADDRESS_RESIDENCE",
    "entry": "ADDRESS_ENTRY",
    "floor": "ADDRESS_FLOOR",
    "unit": "ADDRESS_UNIT",
    "house_number": "ADDRESS_HOUSE_NUMBER",
    "house_number_suffix": "ADDRESS_HOUSE_NUMBER_SUFFIX",
    "street_type": "ADDRESS_STREET_TYPE",
    "street_name": "ADDRESS_STREET_NAME",
    "industrial_zone": "ADDRESS_INDUSTRIAL_ZONE",
    "activity_park": "ADDRESS_ACTIVITY_PARK",
    "place_name": "ADDRESS_PLACE_NAME",
    "postal_box": "ADDRESS_POSTAL_BOX",
    "tsa": "ADDRESS_TSA",
    "cs": "ADDRESS_CS",
    "postal_routing_code": "ADDRESS_POSTAL_ROUTING_CODE",
    "postal_code": "ADDRESS_POSTAL_CODE",
    "city": "ADDRESS_CITY",
    "cedex_number": "ADDRESS_CEDEX_NUMBER",
    "insee_code": "ADDRESS_INSEE_CODE",
    "country": "ADDRESS_COUNTRY",
    "country_code": "ADDRESS_COUNTRY_CODE",
    "purchase_order_number": "ORDER_NUMBER",
    "order_number": "ORDER_NUMBER",
    "customer_reference": "CUSTOMER_REFERENCE",
    "order_date": "ORDER_DATE",
    "date": "ORDER_DATE",
    "material_number": "LINE_ITEM_REFERENCE",
    "article_number": "LINE_ITEM_REFERENCE",
    "product_code": "LINE_ITEM_REFERENCE",
    "description": "LINE_ITEM_DESCRIPTION",
    "quantity": "LINE_ITEM_QUANTITY",
    "uom": "LINE_ITEM_UNIT",
    "unit_price": "LINE_ITEM_UNIT_PRICE",
    "line_total": "LINE_ITEM_TOTAL",
    "subtotal": "TOTAL_SUBTOTAL",
    "total_net": "TOTAL_NET",
    "total_before_tax": "TOTAL_NET",
    "total_vat": "TOTAL_VAT",
    "total_tax": "TOTAL_VAT",
    "grand_total": "TOTAL_GROSS",
    "total_gross": "TOTAL_GROSS",
    "amount_due": "TOTAL_AMOUNT_DUE",
}


def norm(value: Any) -> str:
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    ).lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def collect_candidates(obj: Any, path: str = "") -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else key
            label = FIELD_LABELS.get(key)
            lower_path = child_path.lower()
            if key == "unit" and ("lines[" in lower_path or ".lines." in lower_path):
                label = "LINE_ITEM_UNIT"
            if key in {"description", "quantity", "unit_price", "line_total", "material_number", "article_number", "product_code", "uom"} and "lines" not in lower_path:
                label = None
            if key in {"subtotal", "total_net", "total_before_tax", "total_vat", "total_tax", "grand_total", "total_gross", "amount_due"} and "total" not in lower_path:
                label = None
            if label and scalar(value) and str(value).strip():
                out.append(
                    {
                        "path": child_path,
                        "key": key,
                        "label": label,
                        "value": str(value).strip(),
                    }
                )
            if isinstance(value, (dict, list)):
                out.extend(collect_candidates(value, child_path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            out.extend(collect_candidates(value, f"{path}[{index}]"))
    return out


def page_words(page: fitz.Page) -> list[dict[str, Any]]:
    words = []
    for raw in page.get_text("words", sort=True):
        x0, y0, x1, y1, text, block, line, word = raw
        token = norm(text)
        if token:
            words.append(
                {
                    "bbox": [float(x0), float(y0), float(x1), float(y1)],
                    "text": str(text),
                    "norm": token,
                    "block": int(block),
                    "line": int(line),
                    "word": int(word),
                }
            )
    return words


def candidate_tokens(value: str) -> list[str]:
    pieces = re.findall(r"[^\s,;:/()\[\]{}]+", value)
    return [token for token in (norm(piece) for piece in pieces) if token]


def find_matches(words: list[dict[str, Any]], value: str) -> list[tuple[int, int]]:
    target = candidate_tokens(value)
    if not target:
        return []
    matches = []
    width = len(target)
    for start in range(0, len(words) - width + 1):
        if [word["norm"] for word in words[start : start + width]] == target:
            matches.append((start, start + width))
    if matches:
        return matches

    # Fallback for PDF tokenization differences (e.g. 124-126 => 124, -, 126).
    target_joined = norm(value)
    for start in range(len(words)):
        joined = ""
        for end in range(start + 1, min(len(words), start + 10) + 1):
            joined += words[end - 1]["norm"]
            if joined == target_joined:
                matches.append((start, end))
                break
            if len(joined) > len(target_joined):
                break
    return matches


def union_bbox(words: list[dict[str, Any]], start: int, end: int) -> list[float]:
    selected = words[start:end]
    return [
        min(word["bbox"][0] for word in selected),
        min(word["bbox"][1] for word in selected),
        max(word["bbox"][2] for word in selected),
        max(word["bbox"][3] for word in selected),
    ]


def normalized_bbox(box: list[float], page: fitz.Page) -> list[int]:
    rect = page.rect
    width = max(float(rect.width), 1.0)
    height = max(float(rect.height), 1.0)
    return [
        max(0, min(1000, round(1000 * box[0] / width))),
        max(0, min(1000, round(1000 * box[1] / height))),
        max(0, min(1000, round(1000 * box[2] / width))),
        max(0, min(1000, round(1000 * box[3] / height))),
    ]


def extraction_filename(payload: dict[str, Any], json_path: Path) -> str:
    document = payload.get("document") if isinstance(payload.get("document"), dict) else {}
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    return str(
        payload.get("filename")
        or payload.get("file_name")
        or document.get("filename")
        or source.get("filename")
        or (json_path.stem + ".pdf")
    )


def find_pdf(pdf_root: Path, filename: str) -> Path | None:
    exact = list(pdf_root.rglob(filename))
    if exact:
        return exact[0]
    stem = Path(filename).stem
    candidates = list(pdf_root.rglob(stem + ".pdf")) + list(pdf_root.rglob(stem + ".PDF"))
    return candidates[0] if len(candidates) == 1 else None


def annotate_one(pdf_path: Path, payload: dict[str, Any], max_ambiguity: int) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = collect_candidates(payload)
    doc = fitz.open(pdf_path)
    pages = []
    matched = ambiguous = unmatched = 0
    unmatched_fields = []

    word_pages = [page_words(page) for page in doc]
    regions_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)

    for candidate in candidates:
        occurrences = []
        for page_index, words in enumerate(word_pages):
            for start, end in find_matches(words, candidate["value"]):
                occurrences.append((page_index, start, end))

        if not occurrences:
            unmatched += 1
            unmatched_fields.append({**candidate, "reason": "not_found"})
            continue
        if len(occurrences) > max_ambiguity:
            ambiguous += 1
            unmatched_fields.append(
                {**candidate, "reason": "ambiguous", "occurrences": len(occurrences)}
            )
            continue

        # Unique/low-ambiguity exact matches are safe weak labels; retain all occurrences
        # because repeated line-item values can legitimately occur in tables.
        for page_index, start, end in occurrences:
            page = doc[page_index]
            words = word_pages[page_index]
            box = union_bbox(words, start, end)
            regions_by_page[page_index].append(
                {
                    "label": candidate["label"],
                    "bbox": normalized_bbox(box, page),
                    "bbox_space": "normalized",
                    "value": candidate["value"],
                    "source_path": candidate["path"],
                    "annotation_source": "jin_structured_output_weak_label",
                    "requires_review": True,
                }
            )
        matched += 1

    for page_index in range(len(doc)):
        pages.append(
            {
                "page": page_index + 1,
                "regions": regions_by_page.get(page_index, []),
            }
        )

    doc.close()
    annotation = {
        "document_id": pdf_path.stem,
        "source_pdf": str(pdf_path),
        "annotation_status": "WEAK_LABELS_REVIEW_REQUIRED",
        "pages": pages,
    }
    report = {
        "pdf": str(pdf_path),
        "candidate_fields": len(candidates),
        "matched_fields": matched,
        "ambiguous_fields": ambiguous,
        "unmatched_fields": unmatched,
        "unmatched": unmatched_fields,
    }
    return annotation, report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap review-required region labels from corrected JIN extraction JSON and native PDF words."
    )
    parser.add_argument("--pdf-root", required=True)
    parser.add_argument("--extractions-dir", required=True)
    parser.add_argument("--output-dir", default="corpus/annotations")
    parser.add_argument("--report", default="data/annotation_bootstrap_report.json")
    parser.add_argument("--max-ambiguity", type=int, default=3)
    args = parser.parse_args()

    pdf_root = Path(args.pdf_root)
    extraction_root = Path(args.extractions_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    reports = []
    processed = missing_pdf = 0
    for json_path in sorted(extraction_root.rglob("*.json")):
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        filename = extraction_filename(payload, json_path)
        pdf_path = find_pdf(pdf_root, filename)
        if pdf_path is None:
            missing_pdf += 1
            reports.append({"json": str(json_path), "filename": filename, "error": "pdf_not_found"})
            continue

        annotation, report = annotate_one(pdf_path, payload, args.max_ambiguity)
        (output_dir / f"{pdf_path.stem}.json").write_text(
            json.dumps(annotation, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        reports.append(report)
        processed += 1

    summary = {
        "processed": processed,
        "missing_pdf": missing_pdf,
        "matched_fields": sum(r.get("matched_fields", 0) for r in reports),
        "ambiguous_fields": sum(r.get("ambiguous_fields", 0) for r in reports),
        "unmatched_fields": sum(r.get("unmatched_fields", 0) for r in reports),
        "documents": reports,
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "documents"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

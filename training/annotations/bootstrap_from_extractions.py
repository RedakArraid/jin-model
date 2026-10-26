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

try:
    from PIL import Image
    import pytesseract
    from pytesseract import Output
except ImportError:  # Native-text annotations still work without OCR extras.
    Image = None
    pytesseract = None
    Output = None

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
    "customer_order_number": "ORDER_NUMBER",
    "order_number": "ORDER_NUMBER",
    "customer_reference": "CUSTOMER_REFERENCE",
    "customer_agency_code": "CUSTOMER_CODE",
    "order_date": "ORDER_DATE",
    "date": "ORDER_DATE",
    "requested_delivery_date": "DELIVERY_DATE",
    "expected_delivery_date": "DELIVERY_DATE",
    "required_date": "DELIVERY_DATE",
    "delivery_date": "DELIVERY_DATE",
    "quote_number": "OFFER_NUMBER",
    "quote_numbers": "OFFER_NUMBER",
    "derogation_number": "DEROGATION_NUMBER",
    "derogation_numbers": "DEROGATION_NUMBER",
    "line_number": "LINE_ITEM_NUMBER",
    "material_number": "LINE_ITEM_REFERENCE",
    "article_number": "LINE_ITEM_REFERENCE",
    "product_code": "LINE_ITEM_REFERENCE",
    "description": "LINE_ITEM_DESCRIPTION",
    "quantity": "LINE_ITEM_QUANTITY",
    "uom": "LINE_ITEM_UNIT",
    "unit_price": "LINE_ITEM_UNIT_PRICE",
    "net_unit_price": "LINE_ITEM_UNIT_PRICE",
    "line_total": "LINE_ITEM_TOTAL",
    "subtotal": "TOTAL_SUBTOTAL",
    "total_net": "TOTAL_NET",
    "total_before_tax": "TOTAL_NET",
    "total_vat": "TOTAL_VAT",
    "total_tax": "TOTAL_VAT",
    "grand_total": "TOTAL_GROSS",
    "total_gross": "TOTAL_GROSS",
    "amount_due": "TOTAL_AMOUNT_DUE",
    "vat_number": "VAT_NUMBER",
    "siren": "SIREN",
    "siret": "SIRET",
}

PARTY_LABELS = {
    "buyer": "BUYER",
    "supplier": "SUPPLIER",
    "ship_to": "SHIP_TO",
    "deliver_to": "SHIP_TO",
    "delivery_address": "SHIP_TO",
    "bill_to": "BILL_TO",
    "invoice_to": "BILL_TO",
    "payer": "PAYER",
    "ship_from": "SHIP_FROM",
}

ADDRESS_LABEL_SUFFIX = {
    "building": "BUILDING",
    "residence": "RESIDENCE",
    "entry": "ENTRY",
    "floor": "FLOOR",
    "unit": "UNIT",
    "house_number": "HOUSE_NUMBER",
    "house_number_suffix": "HOUSE_NUMBER_SUFFIX",
    "street_type": "STREET_TYPE",
    "street_name": "STREET_NAME",
    "street": "STREET",
    "industrial_zone": "INDUSTRIAL_ZONE",
    "activity_park": "ACTIVITY_PARK",
    "place_name": "PLACE_NAME",
    "postal_box": "POSTAL_BOX",
    "tsa": "TSA",
    "cs": "CS",
    "postal_routing_code": "POSTAL_ROUTING_CODE",
    "postal_code": "POSTAL_CODE",
    "city": "CITY",
    "cedex_number": "CEDEX_NUMBER",
    "country": "COUNTRY",
    "country_code": "COUNTRY_CODE",
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


def _party_role(path: str, obj: Any, inherited: str | None) -> str | None:
    if isinstance(obj, dict):
        explicit = str(obj.get("role") or "").strip().lower()
        if explicit in PARTY_LABELS:
            return PARTY_LABELS[explicit]
    lowered = path.lower()
    for marker, label in PARTY_LABELS.items():
        if re.search(rf"(?:^|[.\[])" + re.escape(marker) + r"(?:[.\[]|$)", lowered):
            return label
    return inherited


def _line_context(path: str) -> bool:
    lowered = path.lower()
    return ".lines[" in lowered or ".line_items[" in lowered or lowered.startswith("lines[") or lowered.startswith("line_items[")


def _total_context(path: str) -> bool:
    lowered = path.lower()
    return ".totals." in lowered or lowered.startswith("totals.")


def _charge_context(path: str) -> bool:
    lowered = path.lower()
    return ".additional_charges[" in lowered or lowered.startswith("additional_charges[")


def _field_label(key: str, path: str, role: str | None) -> str | None:
    lowered = path.lower()
    if _line_context(path):
        if key in {"material_number", "article_number", "product_code", "sku", "ean", "gtin"}:
            return "LINE_ITEM_REFERENCE"
        if key in {"unit", "uom"}:
            return "LINE_ITEM_UNIT"
        if key in {"unit_price", "net_unit_price"}:
            return "LINE_ITEM_UNIT_PRICE"
        if key in {"line_total", "total", "net"} and any(
            marker in lowered for marker in (".amounts.", ".lines[", ".line_items[")
        ):
            return "LINE_ITEM_TOTAL"
        if key in {"requested_delivery_date", "delivery_date"}:
            return "LINE_ITEM_DELIVERY_DATE"
        return FIELD_LABELS.get(key)
    if _charge_context(path):
        if key in {"amount", "net", "total"}:
            return "ADDITIONAL_CHARGE_AMOUNT"
        if key in {"description", "label", "type", "charge_type"}:
            return "ADDITIONAL_CHARGE_LABEL"
    if _total_context(path):
        return FIELD_LABELS.get(key)
    if role:
        if key in {"name", "legal_name", "party_name", "recipient"}:
            return f"{role}_NAME"
        if key in {"code", "party_code"}:
            return f"{role}_CODE"
        if key == "vat_number":
            return f"{role}_VAT_NUMBER"
        if key in {"siren", "siret"}:
            return f"{role}_{key.upper()}"
        if key in {"phone", "fax", "email"}:
            return f"{role}_CONTACT_{key.upper()}"
        if key in ADDRESS_LABEL_SUFFIX and any(
            marker in lowered for marker in (".address.", ".components.", ".delivery_address.")
        ):
            return f"{role}_{ADDRESS_LABEL_SUFFIX[key]}"
    label = FIELD_LABELS.get(key)
    if key in {
        "description", "quantity", "unit_price", "net_unit_price", "line_total",
        "material_number", "article_number", "product_code", "uom",
    } and not _line_context(path):
        return None
    if key in {
        "subtotal", "total_net", "total_before_tax", "total_vat", "total_tax",
        "grand_total", "total_gross", "amount_due",
    } and not _total_context(path):
        return None
    return label


def _evidence_hint(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        value = next((item for item in value if isinstance(item, dict)), None)
    if not isinstance(value, dict):
        return {}
    hint: dict[str, Any] = {}
    try:
        page = int(value.get("page"))
        if page > 0:
            hint["page_hint"] = page
    except (TypeError, ValueError):
        pass
    bbox = value.get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4:
        try:
            hint["bbox_hint"] = [float(part) for part in bbox]
        except (TypeError, ValueError):
            pass
    return hint


def _append_candidate(
    out: list[dict[str, Any]],
    *,
    label: str,
    value: Any,
    path: str,
    evidence: Any = None,
    canonical_value: Any = None,
    confidence: Any = None,
) -> None:
    if not scalar(value) or not str(value).strip():
        return
    candidate: dict[str, Any] = {
        "path": path,
        "key": path.rsplit(".", 1)[-1],
        "label": label,
        "value": str(value).strip(),
    }
    if canonical_value not in (None, ""):
        candidate["canonical_value"] = str(canonical_value).strip()
    if isinstance(confidence, (int, float)):
        candidate["source_confidence"] = float(confidence)
    candidate.update(_evidence_hint(evidence))
    out.append(candidate)


def collect_candidates(
    obj: Any,
    path: str = "",
    *,
    role: str | None = None,
    label_hint: str | None = None,
    inherited_evidence: Any = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        role = _party_role(path, obj, role)
        own_evidence = obj.get("evidence") or inherited_evidence
        if label_hint and scalar(obj.get("value")):
            display_value = obj.get("raw_value") or obj.get("value")
            _append_candidate(
                out,
                label=label_hint,
                value=display_value,
                canonical_value=obj.get("value"),
                path=f"{path}.value" if path else "value",
                evidence=own_evidence,
                confidence=obj.get("confidence") or obj.get("final_confidence"),
            )
            return out
        for key, value in obj.items():
            child_path = f"{path}.{key}" if path else key
            label = _field_label(key, child_path, role)
            if label and scalar(value) and str(value).strip():
                _append_candidate(
                    out,
                    label=label,
                    value=value,
                    path=child_path,
                    evidence=own_evidence if _line_context(child_path) else None,
                )
            if isinstance(value, (dict, list)):
                child_evidence = own_evidence if _line_context(child_path) else None
                out.extend(
                    collect_candidates(
                        value,
                        child_path,
                        role=role,
                        label_hint=label,
                        inherited_evidence=child_evidence,
                    )
                )
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            out.extend(
                collect_candidates(
                    value,
                    f"{path}[{index}]",
                    role=role,
                    label_hint=label_hint,
                    inherited_evidence=inherited_evidence,
                )
            )
    elif label_hint and scalar(obj):
        _append_candidate(out, label=label_hint, value=obj, path=path, evidence=inherited_evidence)
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
    if words or Image is None or pytesseract is None:
        return words

    # Scanned orders have no native PDF words. Render only those pages and use
    # the same local OCR family as the runtime so they remain annotatable.
    scale = 2.0
    pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    data = pytesseract.image_to_data(
        image,
        lang="fra+eng+deu",
        output_type=Output.DICT,
    )
    for index, raw_text in enumerate(data.get("text") or []):
        text = str(raw_text).strip()
        token = norm(text)
        try:
            confidence = float(data["conf"][index])
        except (KeyError, TypeError, ValueError):
            confidence = -1
        if not token or confidence < 0:
            continue
        x0 = float(data["left"][index]) / scale
        y0 = float(data["top"][index]) / scale
        x1 = x0 + float(data["width"][index]) / scale
        y1 = y0 + float(data["height"][index]) / scale
        words.append(
            {
                "bbox": [x0, y0, x1, y1],
                "text": text,
                "norm": token,
                "block": int(data.get("block_num", [0])[index]),
                "line": int(data.get("line_num", [0])[index]),
                "word": int(data.get("word_num", [index])[index]),
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
    annotation_root = payload.get("normalized_output")
    if not isinstance(annotation_root, dict) or not annotation_root:
        annotation_root = payload
    candidates = collect_candidates(annotation_root)
    deduplicated = []
    seen_candidates = set()
    for candidate in candidates:
        bbox_hint = tuple(round(float(value), 3) for value in candidate.get("bbox_hint") or [])
        key = (
            candidate["label"], norm(candidate["value"]), candidate.get("page_hint"), bbox_hint,
        )
        if key in seen_candidates:
            continue
        seen_candidates.add(key)
        deduplicated.append(candidate)
    candidates = deduplicated
    doc = fitz.open(pdf_path)
    pages = []
    matched = ambiguous = unmatched = 0
    unmatched_fields = []

    word_pages = [page_words(page) for page in doc]
    regions_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)

    for candidate in candidates:
        occurrences = []
        for page_index, words in enumerate(word_pages):
            if candidate.get("page_hint") and page_index + 1 != candidate["page_hint"]:
                continue
            for start, end in find_matches(words, candidate["value"]):
                hint = candidate.get("bbox_hint")
                if hint:
                    box = union_bbox(words, start, end)
                    center_x = (box[0] + box[2]) / 2
                    center_y = (box[1] + box[3]) / 2
                    tolerance = 4.0
                    if not (
                        hint[0] - tolerance <= center_x <= hint[2] + tolerance
                        and hint[1] - tolerance <= center_y <= hint[3] + tolerance
                    ):
                        continue
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
                    **(
                        {"canonical_value": candidate["canonical_value"]}
                        if candidate.get("canonical_value") else {}
                    ),
                    "source_path": candidate["path"],
                    "annotation_source": "jin_structured_output_weak_label",
                    **(
                        {"source_confidence": candidate["source_confidence"]}
                        if candidate.get("source_confidence") is not None else {}
                    ),
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
        "annotator": "codex_assisted_jin",
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

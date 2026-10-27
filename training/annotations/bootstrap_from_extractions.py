#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import statistics
import unicodedata
from collections import defaultdict
from decimal import Decimal, InvalidOperation
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


LINE_GROUP_RE = re.compile(r"(?:^|\.)(?:lines|line_items)\[\d+\]", re.IGNORECASE)
CHARGE_GROUP_RE = re.compile(r"(?:^|\.)additional_charges\[\d+\]", re.IGNORECASE)
ADDRESS_GROUP_RE = re.compile(r"(?:^|\.)addresses\[\d+\]", re.IGNORECASE)
IGNORED_DERIVED_BRANCHES = frozenset(
    {
        "verification",
        "normalization",
        "ban_verification",
        "clean_address",
        "quality",
        "reference_data",
        "output_audit",
        "matched_components",
        "suggested_components",
        "warnings",
    }
)


def _total_context(path: str) -> bool:
    lowered = path.lower()
    return ".totals." in lowered or lowered.startswith("totals.")


def _charge_context(path: str) -> bool:
    lowered = path.lower()
    return ".additional_charges[" in lowered or lowered.startswith("additional_charges[")


def _field_label(key: str, path: str, role: str | None) -> str | None:
    lowered = path.lower()
    if _line_context(path):
        if key == "line_number":
            return "LINE_ITEM_NUMBER"
        if key in {
            "material_number", "article_number", "supplier_material_number",
            "product_code", "sku", "ean", "gtin",
        }:
            return "LINE_ITEM_REFERENCE"
        if key == "description":
            return "LINE_ITEM_DESCRIPTION"
        if key == "quantity":
            return "LINE_ITEM_QUANTITY"
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
        # Order-level aliases sometimes appear below ``references`` or
        # ``delivery`` on every product row. They must not become duplicated
        # header annotations (for example one ORDER_NUMBER per line).
        return None
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
    # An address component without a resolved business role is not usable as
    # supervised ground truth.  The old ADDRESS_* fallback mixed buyer,
    # supplier and delivery addresses into the same singleton labels.
    if key in ADDRESS_LABEL_SUFFIX:
        return None
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
    suppressed_keys: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        role = _party_role(path, obj, role)
        own_evidence = obj.get("evidence") or inherited_evidence
        local_suppressed = set(suppressed_keys)
        warnings = {
            str(item).strip().lower()
            for item in (obj.get("warnings") or [])
            if str(item).strip()
        }
        if "country_inferred_from_postal_pattern" in warnings:
            local_suppressed.update({"country", "country_code"})
        if "untyped_postal_routing_code_inferred" in warnings:
            local_suppressed.add("postal_routing_code")
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
            if str(key).lower() in IGNORED_DERIVED_BRANCHES:
                continue
            # V3 exposes unresolved candidates under ``alternatives``. They
            # are intentionally not ground truth and must never seed a box.
            if str(key).lower() == "alternatives":
                continue
            if str(key).lower() in local_suppressed:
                continue
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
                        suppressed_keys=frozenset(local_suppressed),
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
                    suppressed_keys=suppressed_keys,
                )
            )
    elif label_hint and scalar(obj):
        _append_candidate(out, label=label_hint, value=obj, path=path, evidence=inherited_evidence)
    return out


def _path_group(path: str, label: str) -> str:
    """Return the business object that owns an annotation path.

    The group deliberately contains the line index. Two quantities equal to
    ``1`` on two different rows are distinct fields, while aliases such as
    ``material_number`` and ``article_number`` on the same row can be folded
    when they carry the same value.
    """

    for pattern in (LINE_GROUP_RE, CHARGE_GROUP_RE):
        match = pattern.search(path)
        if match:
            return match.group(0).lstrip(".").lower()
    if label.startswith("ADDRESS_"):
        match = ADDRESS_GROUP_RE.search(path)
        if match:
            return match.group(0).lstrip(".").lower()
    return ""


def _reference_prior(value: Any) -> int:
    compact = re.sub(r"[\s._/-]+", "", str(value or "")).upper()
    if compact.isdigit() and len(compact) in {10, 11}:
        return 3
    if compact.isdigit() and 7 <= len(compact) <= 14:
        return 2
    if any(char.isdigit() for char in compact):
        return 1
    return 0


def _candidate_preference(candidate: dict[str, Any]) -> tuple[int, int, int, float, int]:
    path = str(candidate.get("path") or "").lower()
    role_source_priority = 0
    if re.search(r"\.addresses\.[a-z_]+\.selected\.components\.", path):
        role_source_priority = 5
    elif ".delivery_address.components." in path:
        role_source_priority = 4
    elif re.search(r"\.parties\.(?:buyer|supplier|ship_to|bill_to|payer|ship_from)\.address\.", path):
        role_source_priority = 3
    elif re.search(r"\.addresses\[\d+\]\.components\.", path):
        role_source_priority = 2
    elif ".parties." in path:
        role_source_priority = 1
    canonical_leaf = int(
        any(
            path.endswith(suffix)
            for suffix in (
                ".material_number", ".unit_price", ".net", ".name", ".value",
                ".description", ".amount",
            )
        )
    )
    return (
        _reference_prior(candidate.get("value"))
        if candidate.get("label") == "LINE_ITEM_REFERENCE" else 0,
        role_source_priority,
        int(bool(candidate.get("bbox_hint"))),
        float(candidate.get("source_confidence") or 0.0),
        canonical_leaf,
    )


def deduplicate_candidates(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove only semantic aliases, never equal fields from different rows."""

    ordered_keys: list[tuple[str, str, str]] = []
    selected: dict[tuple[str, str, str], dict[str, Any]] = {}
    for candidate in candidates:
        group = _path_group(str(candidate.get("path") or ""), str(candidate["label"]))
        semantic_value = norm(candidate.get("value"))
        if group and candidate["label"] == "LINE_ITEM_REFERENCE":
            semantic_value = "primary-reference"
        elif group and str(candidate["label"]).startswith("ADDITIONAL_CHARGE_"):
            semantic_value = "one-field-per-charge"
        elif not group:
            # Header/footer and role-specific party fields are singletons.
            # Different projections of the same business object must compete
            # for one canonical label instead of creating duplicate fields.
            semantic_value = "singleton"
        key = (
            group,
            str(candidate["label"]),
            semantic_value,
        )
        if key not in selected:
            ordered_keys.append(key)
            selected[key] = candidate
            continue
        if _candidate_preference(candidate) > _candidate_preference(selected[key]):
            selected[key] = candidate
    result = [selected[key] for key in ordered_keys]
    atomic_street_roles = {
        label.rsplit("_", 2)[0]
        for candidate in result
        if (
            (label := str(candidate.get("label") or "")).endswith(
                ("_STREET_TYPE", "_STREET_NAME")
            )
        )
    }
    return [
        candidate
        for candidate in result
        if not (
            str(candidate.get("label") or "").endswith("_STREET")
            and str(candidate.get("label") or "").removesuffix("_STREET")
            in atomic_street_roles
        )
    ]


def _line_group(path: str) -> str | None:
    match = LINE_GROUP_RE.search(path)
    return match.group(0).lstrip(".").lower() if match else None


def _line_index(path: str) -> int:
    group = _line_group(path)
    if not group:
        return 10**9
    match = re.search(r"\[(\d+)\]", group)
    return int(match.group(1)) if match else 10**9


def _occurrence_key(occurrence: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(occurrence["page_index"]),
        int(occurrence["start"]),
        int(occurrence["end"]),
    )


def _occurrence_center(occurrence: dict[str, Any]) -> tuple[float, float]:
    box = occurrence["bbox"]
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def _candidate_occurrences(
    candidate: dict[str, Any],
    word_pages: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    for page_index, words in enumerate(word_pages):
        if candidate.get("page_hint") and page_index + 1 != candidate["page_hint"]:
            continue
        for start, end in find_matches(words, candidate["value"]):
            box = union_bbox(words, start, end)
            hint = candidate.get("bbox_hint")
            if hint:
                center_x = (box[0] + box[2]) / 2
                center_y = (box[1] + box[3]) / 2
                tolerance = 4.0
                if not (
                    hint[0] - tolerance <= center_x <= hint[2] + tolerance
                    and hint[1] - tolerance <= center_y <= hint[3] + tolerance
                ):
                    continue
            occurrences.append(
                {
                    "page_index": page_index,
                    "start": start,
                    "end": end,
                    "bbox": box,
                }
            )
    return occurrences


LINE_LABEL_PRIORITY = {
    "LINE_ITEM_REFERENCE": 0,
    "LINE_ITEM_DESCRIPTION": 1,
    "LINE_ITEM_NUMBER": 2,
    "LINE_ITEM_QUANTITY": 3,
    "LINE_ITEM_UNIT": 4,
    "LINE_ITEM_UNIT_PRICE": 5,
    "LINE_ITEM_TOTAL": 6,
    "LINE_ITEM_DELIVERY_DATE": 7,
}


def _x_tiebreak(label: str, occurrence: dict[str, Any]) -> float:
    x, _ = _occurrence_center(occurrence)
    if label in {"LINE_ITEM_NUMBER", "LINE_ITEM_REFERENCE", "LINE_ITEM_UNIT_PRICE"}:
        return x
    if label in {"LINE_ITEM_QUANTITY", "LINE_ITEM_UNIT", "LINE_ITEM_TOTAL"}:
        return -x
    return x


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


def _decimal_value(value: Any) -> Decimal | None:
    text = str(value or "").strip().replace("\u00a0", "").replace(" ", "")
    if not re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", text):
        return None
    if "," in text and "." in text:
        decimal_separator = "," if text.rfind(",") > text.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        text = text.replace(thousands_separator, "").replace(decimal_separator, ".")
    else:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


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

    # Structured JSON turns printed ``1`` and ``10,00`` into ``1.0`` and
    # ``10.0``. Decimal comparison restores those exact numeric matches while
    # the later geometry pass decides which table row/column owns them.
    target_decimal = _decimal_value(value)
    if target_decimal is not None:
        for index, word in enumerate(words):
            if _decimal_value(word["text"]) == target_decimal:
                matches.append((index, index + 1))
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


DOCUMENT_TYPE_ANCHORS = {
    "purchase_order": (
        "bon de commande fournisseur",
        "bon de commande",
        "purchase order",
        "commande fournisseur",
        "bestellung",
        "commande",
    ),
    "invoice": ("facture", "invoice", "rechnung"),
    "credit_note": ("avoir", "credit note", "gutschrift"),
    "delivery_note": ("bon de livraison", "delivery note", "lieferschein"),
    "quotation": ("devis", "quotation", "quote", "angebot"),
}


def find_document_type_evidence(
    annotation_root: dict[str, Any],
    word_pages: list[list[dict[str, Any]]],
    doc: fitz.Document,
) -> dict[str, Any] | None:
    document = annotation_root.get("document")
    if not isinstance(document, dict):
        return None
    document_type = str(
        document.get("type")
        or document.get("primary_document_type")
        or document.get("detected_document_type")
        or ""
    ).strip().lower()
    anchors = DOCUMENT_TYPE_ANCHORS.get(document_type, ())
    candidates: list[tuple[int, float, int, int, str]] = []
    for anchor_index, anchor in enumerate(anchors):
        for page_index, words in enumerate(word_pages[:3]):
            for start, end in find_matches(words, anchor):
                box = union_bbox(words, start, end)
                # Prefer the most specific anchor, then the first/topmost page.
                candidates.append((anchor_index, page_index, box[1], start, anchor))
    if not candidates:
        return None
    anchor_index, page_index, _top, start, anchor = min(candidates)
    words = word_pages[page_index]
    # Token joining can make ``end`` differ from the actual match. Resolve it
    # again and pick the occurrence beginning at ``start``.
    actual = next(
        ((match_start, match_end) for match_start, match_end in find_matches(words, anchor) if match_start == start),
        None,
    )
    if actual is None:
        return None
    box = union_bbox(words, actual[0], actual[1])
    return {
        "page": page_index + 1,
        "bbox": normalized_bbox(box, doc[page_index]),
        "source_text": " ".join(word["text"] for word in words[actual[0] : actual[1]]),
        "match_method": "document_type_anchor",
    }


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
    raw_candidates = collect_candidates(annotation_root)
    candidates = deduplicate_candidates(raw_candidates)
    doc = fitz.open(pdf_path)
    pages = []
    matched = ambiguous = unmatched = 0
    unmatched_fields = []

    word_pages = [page_words(page) for page in doc]
    document_type_evidence = find_document_type_evidence(annotation_root, word_pages, doc)
    regions_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    records = [
        {
            "candidate": candidate,
            "occurrences": _candidate_occurrences(candidate, word_pages),
        }
        for candidate in candidates
    ]
    selected: dict[int, tuple[dict[str, Any], str]] = {}
    used_occurrences: set[tuple[int, int, int]] = set()

    def available(record: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            occurrence
            for occurrence in record["occurrences"]
            if _occurrence_key(occurrence) not in used_occurrences
        ]

    def choose(index: int, occurrence: dict[str, Any], method: str) -> None:
        selected[index] = (occurrence, method)
        used_occurrences.add(_occurrence_key(occurrence))

    # Explicit source geometry is the strongest signal. A row-sized hint can
    # still contain multiple numeric tokens, so select the nearest token to its
    # centre and leave the finer column decision to the line pass below.
    for index, record in enumerate(records):
        candidate = record["candidate"]
        options = available(record)
        if not options or not candidate.get("bbox_hint") or _line_group(candidate["path"]):
            continue
        hint = candidate["bbox_hint"]
        hint_x = (hint[0] + hint[2]) / 2
        hint_y = (hint[1] + hint[3]) / 2
        occurrence = min(
            options,
            key=lambda item: (
                (_occurrence_center(item)[0] - hint_x) ** 2
                + (_occurrence_center(item)[1] - hint_y) ** 2
            ),
        )
        choose(index, occurrence, "source_geometry")

    # A unique textual occurrence is safe without inventing a second region.
    ordered_indexes = sorted(
        range(len(records)),
        key=lambda index: (
            _line_index(records[index]["candidate"]["path"]),
            LINE_LABEL_PRIORITY.get(records[index]["candidate"]["label"], -1),
            index,
        ),
    )
    for index in ordered_indexes:
        if index in selected:
            continue
        candidate = records[index]["candidate"]
        if (
            _line_group(candidate["path"])
            and candidate["label"] not in {"LINE_ITEM_REFERENCE", "LINE_ITEM_DESCRIPTION"}
        ):
            continue
        options = available(records[index])
        if len(options) == 1:
            choose(index, options[0], "unique_text")

    # Build a row anchor from a unique reference or description. Then use both
    # its vertical position and the learned table column for repeated values
    # such as quantities, units and prices.
    def line_anchors() -> dict[str, dict[str, Any]]:
        anchors: dict[str, tuple[int, dict[str, Any]]] = {}
        for index, (occurrence, _method) in selected.items():
            candidate = records[index]["candidate"]
            group = _line_group(candidate["path"])
            if not group:
                continue
            priority = LINE_LABEL_PRIORITY.get(candidate["label"], 99)
            current = anchors.get(group)
            if current is None or priority < current[0]:
                anchors[group] = (priority, occurrence)
        return {group: item[1] for group, item in anchors.items()}

    def column_profiles() -> dict[str, float]:
        values: dict[str, list[float]] = defaultdict(list)
        for index, (occurrence, _method) in selected.items():
            candidate = records[index]["candidate"]
            if not _line_group(candidate["path"]):
                continue
            page = doc[occurrence["page_index"]]
            x, _ = _occurrence_center(occurrence)
            values[candidate["label"]].append(x / max(float(page.rect.width), 1.0))
        return {label: statistics.median(items) for label, items in values.items()}

    for _pass in range(2):
        anchors = line_anchors()
        profiles = column_profiles()
        progress = False
        for index in ordered_indexes:
            if index in selected:
                continue
            record = records[index]
            candidate = record["candidate"]
            group = _line_group(candidate["path"])
            anchor = anchors.get(group or "")
            if not anchor:
                continue
            anchor_x, anchor_y = _occurrence_center(anchor)
            page_index = anchor["page_index"]
            page = doc[page_index]
            page_height = max(float(page.rect.height), 1.0)
            page_width = max(float(page.rect.width), 1.0)
            row_options = []
            for occurrence in available(record):
                if occurrence["page_index"] != page_index:
                    continue
                x, y = _occurrence_center(occurrence)
                if abs(y - anchor_y) / page_height <= 0.045:
                    row_options.append(occurrence)
            if not row_options:
                continue
            profile = profiles.get(candidate["label"])
            if profile is not None:
                occurrence = min(
                    row_options,
                    key=lambda item: (
                        abs(_occurrence_center(item)[1] - anchor_y) / page_height * 8
                        + abs(_occurrence_center(item)[0] / page_width - profile)
                    ),
                )
            else:
                occurrence = min(
                    row_options,
                    key=lambda item: (
                        abs(_occurrence_center(item)[1] - anchor_y),
                        _x_tiebreak(candidate["label"], item),
                    ),
                )
            choose(index, occurrence, "line_geometry")
            progress = True
        if not progress:
            break

    # Wrapped header fields can carry their own exact source bbox even when
    # tokenization prevents a text match (dates and punctuated identifiers are
    # common examples). Keep that box as an explicitly ambiguous fallback.
    # Row-level hints are excluded because they cover an entire product line,
    # not the individual value.
    for index, record in enumerate(records):
        if index in selected:
            continue
        candidate = record["candidate"]
        hint = candidate.get("bbox_hint")
        page_number = int(candidate.get("page_hint") or 0)
        if _line_group(candidate["path"]) or not hint or not (1 <= page_number <= len(doc)):
            continue
        page = doc[page_number - 1]
        if not (
            len(hint) == 4
            and 0 <= hint[0] < hint[2] <= float(page.rect.width) + 1
            and 0 <= hint[1] < hint[3] <= float(page.rect.height) + 1
        ):
            continue
        choose(
            index,
            {
                "page_index": page_number - 1,
                "start": -(index + 1),
                "end": -(index + 1),
                "bbox": list(hint),
            },
            "source_bbox_fallback",
        )

    for index, record in enumerate(records):
        candidate = record["candidate"]
        picked = selected.get(index)
        if picked is None:
            if record["occurrences"]:
                ambiguous += 1
                unmatched_fields.append(
                    {
                        **candidate,
                        "reason": "ambiguous",
                        "occurrences": len(record["occurrences"]),
                    }
                )
            else:
                unmatched += 1
                unmatched_fields.append({**candidate, "reason": "not_found"})
            continue

        occurrence, method = picked
        page_index = occurrence["page_index"]
        regions_by_page[page_index].append(
            {
                "label": candidate["label"],
                "bbox": normalized_bbox(occurrence["bbox"], doc[page_index]),
                "bbox_space": "normalized",
                "value": candidate["value"],
                **(
                    {"canonical_value": candidate["canonical_value"]}
                    if candidate.get("canonical_value") else {}
                ),
                "source_path": candidate["path"],
                "annotation_source": "jin_structured_output_weak_label",
                "match_method": method,
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
        **(
            {"document_type_evidence": document_type_evidence}
            if document_type_evidence else {}
        ),
        "pages": pages,
    }
    report = {
        "pdf": str(pdf_path),
        "source_candidate_fields": len(raw_candidates),
        "candidate_fields": len(candidates),
        "semantic_aliases_removed": len(raw_candidates) - len(candidates),
        "matched_fields": matched,
        "ambiguous_fields": ambiguous,
        "unmatched_fields": unmatched,
        "match_methods": {
            method: sum(1 for _occurrence, selected_method in selected.values() if selected_method == method)
            for method in sorted({selected_method for _occurrence, selected_method in selected.values()})
        },
        "max_ambiguity": max_ambiguity,
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

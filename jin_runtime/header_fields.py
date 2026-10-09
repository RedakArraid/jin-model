"""Source-backed reconciliation for structured purchase-order headers."""
from __future__ import annotations

import copy
from datetime import datetime
import re
import unicodedata
from typing import Any

from jin_runtime.output_quality import _find_purchase_order


def _fold(value: Any) -> str:
    text = "".join(
        char for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    ).upper()
    return re.sub(r"[^A-Z0-9]+", " ", text).strip()


def _source(block: dict[str, Any]) -> tuple[Any, Any]:
    source = block.get("source") if isinstance(block.get("source"), dict) else {}
    return source.get("page") or block.get("page"), source.get("bbox") or block.get("bbox")


def _blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    collections = [payload.get("blocks")]
    collections.extend(
        page.get("blocks") for page in payload.get("pages") or []
        if isinstance(page, dict)
    )
    for items in collections:
        if not isinstance(items, list):
            continue
        for block in items:
            if not isinstance(block, dict) or not str(block.get("text") or "").strip():
                continue
            page, bbox = _source(block)
            key = (page, str(block.get("text")), tuple(bbox or []))
            if key not in seen:
                seen.add(key)
                found.append(block)
    return found


def _center(bbox: Any) -> tuple[float, float] | None:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    return (float(bbox[0]) + float(bbox[2])) / 2, (float(bbox[1]) + float(bbox[3])) / 2


def _reference_commande_candidate(blocks: list[dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    for label in blocks:
        folded = _fold(label.get("text"))
        if not ("REFERENCE COMMANDE" in folded and folded.startswith(("N ", "NO ", "NUMERO "))):
            continue
        label_page, label_bbox = _source(label)
        label_center = _center(label_bbox)
        if label_center is None:
            continue
        candidates = []
        for block in blocks:
            page, bbox = _source(block)
            center = _center(bbox)
            if page != label_page or center is None:
                continue
            delta_y = center[1] - label_center[1]
            if not 5 <= delta_y <= 55:
                continue
            text = " ".join(str(block.get("text") or "").split())
            match = re.fullmatch(
                r"(?:\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\s+)?"
                r"(?P<number>\d(?:[ ._-]?\d){4,})"
                r"(?:\s*/\s*(?:ENGAGEMENT|PROJET|AFFAIRE|CHANTIER)\b.*)?",
                text,
                flags=re.I,
            )
            if not match:
                continue
            number = re.sub(r"\s+", "", match.group("number"))
            candidates.append((abs(center[0] - label_center[0]), number, block))
        if candidates:
            _, number, block = min(candidates, key=lambda item: item[0])
            return number, block
    return None


def _piece_column_candidate(blocks: list[dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    for piece in blocks:
        if _fold(piece.get("text")) != "PIECE":
            continue
        page, piece_bbox = _source(piece)
        piece_center = _center(piece_bbox)
        if piece_center is None:
            continue
        same_header = []
        for block in blocks:
            block_page, bbox = _source(block)
            center = _center(bbox)
            if block_page == page and center and abs(center[1] - piece_center[1]) <= 5:
                same_header.append(_fold(block.get("text")))
        supplier_reference_layout = bool(
            any(value.startswith("FOURN") for value in same_header)
            and "NOTRE REFERENCE" in same_header
        )
        client_commercial_layout = bool(
            "CLIENT" in same_header
            and any(
                value.startswith("REFERENCE") or value.startswith("COMMERCIAL")
                for value in same_header
            )
        )
        if not (
            "DATE" in same_header
            and (supplier_reference_layout or client_commercial_layout)
        ):
            continue
        candidates = []
        for block in blocks:
            block_page, bbox = _source(block)
            center = _center(bbox)
            if block_page != page or center is None:
                continue
            delta_y = center[1] - piece_center[1]
            text = "".join(str(block.get("text") or "").split())
            if (
                5 <= delta_y <= 45
                and abs(center[0] - piece_center[0]) <= 45
                and re.fullmatch(r"[A-Z0-9._/-]{5,30}", text, flags=re.I)
                and any(char.isdigit() for char in text)
                and not re.fullmatch(r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}", text)
            ):
                candidates.append((abs(center[0] - piece_center[0]), text, block))
        if candidates:
            _, number, block = min(candidates, key=lambda item: item[0])
            return number, block
    return None


def _field(number: str, block: dict[str, Any], method: str) -> dict[str, Any]:
    page, bbox = _source(block)
    evidence = {
        key: value for key, value in {
            "page": page,
            "bbox": bbox,
            "source_text": str(block.get("text") or "").strip(),
            "extraction_method": method,
        }.items() if value not in (None, "", [])
    }
    return {
        "value": number,
        "raw_value": number,
        "normalized_value": number,
        "ocr_confidence": 0.995,
        "semantic_confidence": 0.998,
        "validation_confidence": 1.0,
        "final_confidence": 0.9965,
        "validation_status": "SOURCE_SUPPORTED",
        "warnings": [],
        "evidence": evidence,
    }


def _explicit_suffix_candidate(
    payload: dict[str, Any], current_value: Any,
) -> tuple[str, str, int, bool] | None:
    """Preserve a short suffix that belongs to an explicitly labelled PO ID."""
    current = re.sub(r"\s+", "", str(current_value or "")).upper()
    if not current or not re.fullmatch(r"[A-Z0-9._-]{4,40}", current):
        return None
    matches: list[tuple[str, str, int, bool]] = []
    for page_number, page in enumerate(payload.get("pages") or [], 1):
        if not isinstance(page, dict):
            continue
        for line in str(page.get("text") or "").splitlines():
            label = re.search(
                r"(?:COMMANDE\s+FOURNISSEUR|N\s*[°ºO]?\s*CDE|N\s*[°ºO]?\s*COMMANDE)\s*:\s*",
                line,
                flags=re.I,
            )
            if not label:
                continue
            tail = line[label.end():]
            value = re.match(
                r"(?P<base>[A-Z0-9](?:[A-Z0-9\s._-]*[A-Z0-9]))\s*/\s*"
                r"(?P<suffix>[A-Z]{1,8})(?=\s|$)",
                tail,
                flags=re.I,
            )
            if not value:
                continue
            base = re.sub(r"\s+", "", value.group("base")).upper()
            if base != current:
                continue
            composite = f"{base} / {value.group('suffix').upper()}"
            strong_supplier_title = bool(re.search(
                r"COMMANDE\s+FOURNISSEUR", label.group(0), flags=re.I
            )) and len(value.group("suffix")) <= 4
            matches.append((
                composite,
                value.group(0).strip(),
                int(page.get("page") or page_number),
                strong_supplier_title,
            ))
    if len(matches) >= 2 and len({item[0] for item in matches}) == 1:
        return matches[0][:3] + (True,)
    if len(matches) == 1 and matches[0][3]:
        return matches[0][:3] + (False,)
    return None


def _repeated_inline_order_candidate(
    blocks: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]] | None:
    """Recover an order ID repeated in a compact top-of-page title."""
    for block in blocks:
        page, bbox = _source(block)
        if page != 1 or not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or bbox[1] > 110:
            continue
        text = " ".join(str(block.get("text") or "").split())
        values = [
            re.sub(r"\s+", "", match).upper()
            for match in re.findall(
                r"\b(?:CDE|COMMANDE)\s+N(?:\s*[^A-Z0-9\s])?\s*:?[ ]*"
                r"([0-9](?:[ ]?[0-9]){5,})",
                text,
                flags=re.I,
            )
        ]
        compact_text = re.sub(r"\s+", "", text).upper()
        repeated = [
            value for value in set(values)
            if compact_text.count(value) >= 2
        ]
        if len(repeated) == 1:
            return repeated[0], block
    return None



def _normalize_explicit_date(value: str) -> str | None:
    raw = " ".join(str(value or "").split())
    for fmt in (
        "%d/%m/%Y",
        "%d/%m/%y",
        "%d.%m.%Y",
        "%d.%m.%y",
        "%d-%m-%Y",
        "%d-%m-%y",
    ):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _explicit_order_du_date(
    payload: dict[str, Any],
    current_number: Any,
) -> tuple[str, str, int] | None:
    number = " ".join(str(current_number or "").split())
    if not number:
        return None
    number_pattern = re.escape(number).replace(r"\ ", r"\s+")
    date_pattern = r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})"
    pattern = re.compile(
        rf"COMMANDE\s*N\s*[°ºO]?\s*[:#.-]?\s*{number_pattern}"
        rf"[^\n]{{0,80}}(?:\r?\n\s*)?DU\s+{date_pattern}",
        flags=re.I,
    )
    sources: list[tuple[int, str]] = []
    raw = str(payload.get("raw_text") or "")
    if raw:
        sources.append((1, raw))
    for index, page in enumerate(payload.get("pages") or [], 1):
        if isinstance(page, dict) and str(page.get("text") or "").strip():
            sources.append(
                (int(page.get("page") or index), str(page.get("text")))
            )
    for page, source in sources:
        match = pattern.search(source)
        if not match:
            continue
        raw_date = match.group(1)
        normalized = _normalize_explicit_date(raw_date)
        if normalized:
            return normalized, match.group(0).strip(), page
    return None


def _date_field(
    value: str,
    raw_value: str,
    source_text: str,
    page: int,
    method: str,
) -> dict[str, Any]:
    return {
        "value": value,
        "raw_value": raw_value,
        "normalized_value": value,
        "ocr_confidence": 0.995,
        "semantic_confidence": 0.999,
        "validation_confidence": 1.0,
        "final_confidence": 0.997,
        "validation_status": "SOURCE_SUPPORTED",
        "warnings": [],
        "evidence": {
            "page": page,
            "source_text": source_text,
            "extraction_method": method,
        },
    }


def reconcile_header_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Prefer explicit header cells over a neighboring reference or clipped OCR cell."""
    po = _find_purchase_order(payload)
    if not po:
        return payload
    header = po.get("purchase_order")
    if not isinstance(header, dict):
        header = po
    current = header.get("number") or header.get("order_number")
    current_value = current.get("value") if isinstance(current, dict) else current
    explicit_date = _explicit_order_du_date(payload, current_value)
    if explicit_date:
        normalized_date, source_text, date_page = explicit_date
        current_date = header.get("order_date")
        current_date_value = (
            current_date.get("value")
            if isinstance(current_date, dict)
            else current_date
        )
        if str(current_date_value or "") != normalized_date:
            if current_date not in (None, ""):
                header["order_date_original"] = copy.deepcopy(current_date)
                if isinstance(header["order_date_original"], dict):
                    header["order_date_original"]["disqualified_reason"] = (
                        "EXPLICIT_ORDER_DU_DATE_OVERRIDES_OTHER_DATE"
                    )
            raw_match = re.search(
                r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})",
                source_text,
            )
            raw_date = raw_match.group(1) if raw_match else normalized_date
            header["order_date"] = _date_field(
                normalized_date,
                raw_date,
                source_text,
                date_page,
                "explicit_order_du_date_v63",
            )
            payload.setdefault("reconciliation_actions", []).append({
                "action": "promote_order_date",
                "from": current_date_value,
                "to": normalized_date,
                "reason": "explicit_commande_number_du_date",
            })
    suffix_candidate = _explicit_suffix_candidate(payload, current_value)
    if suffix_candidate:
        number, source_text, page, repeated = suffix_candidate
        promoted = copy.deepcopy(current) if isinstance(current, dict) else {}
        promoted.update({
            "value": number,
            "raw_value": number,
            "normalized_value": number,
            "semantic_confidence": 0.998,
            "validation_confidence": 1.0,
            "final_confidence": max(float(promoted.get("final_confidence") or 0), 0.9965),
            "validation_status": "SOURCE_SUPPORTED",
            "warnings": [],
            "evidence": {
                **(promoted.get("evidence") or {}),
                "page": page,
                "source_text": source_text,
                "extraction_method": (
                    "repeated_explicit_order_suffix_v61"
                    if repeated else "explicit_supplier_order_suffix_v62"
                ),
            },
        })
        header["number_original"] = copy.deepcopy(current)
        if isinstance(header["number_original"], dict):
            header["number_original"]["disqualified_reason"] = (
                "EXPLICIT_SUFFIX_PRESERVED"
            )
        header["number"] = promoted
        payload.setdefault("reconciliation_actions", []).append({
            "action": "preserve_order_number_suffix",
            "from": current_value,
            "to": number,
            "reason": (
                "same_explicit_composite_identifier_repeated"
                if repeated else "explicit_supplier_order_composite_identifier"
            ),
        })
        return payload

    blocks = _blocks(payload)
    candidate = _repeated_inline_order_candidate(blocks)
    method = "repeated_inline_order_label_v62"
    reason = "same_order_identifier_repeated_in_top_title"
    if not candidate:
        candidate = _reference_commande_candidate(blocks)
        method = "explicit_reference_commande_column_v60"
        reason = "explicit_reference_commande_value"
    if not candidate:
        candidate = _piece_column_candidate(blocks)
        method = "explicit_date_piece_order_column_v60"
        reason = "explicit_piece_value_in_order_header"
    if not candidate:
        return payload

    number, block = candidate
    compact_current = re.sub(r"\s+", "", str(current_value or "")).upper()
    compact_number = re.sub(r"\s+", "", number).upper()
    if compact_current == compact_number:
        return payload
    if current_value not in (None, ""):
        header["number_original"] = copy.deepcopy(current)
        if isinstance(header["number_original"], dict):
            header["number_original"]["disqualified_reason"] = (
                "LESS_RELEVANT_OR_CLIPPED_HEADER_REFERENCE"
            )
    header["number"] = _field(number, block, method)
    payload.setdefault("reconciliation_actions", []).append({
        "action": "promote_order_number",
        "from": current_value,
        "to": number,
        "reason": reason,
    })
    return payload

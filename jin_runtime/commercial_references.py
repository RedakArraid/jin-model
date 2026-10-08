"""Source-backed quotation and derogation references.

One purchase order may cite several quotations, and each quotation may apply to
different product lines.  This module keeps document-level references separate
from line-level associations and never guesses an association that is not printed.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any


_QUOTE_LABEL_RE = re.compile(
    r"\b(?:"
    r"N\s*(?:[°º]|O)?\s*D['’ ]?OFFRE|"
    r"REF(?:ERENCE)?\s*(?:DU\s+|DE\s+)?DEVIS|"
    r"DEVIS|OFFRE(?:\s+DE\s+PRIX)?|QUOTATION|QUOTE"
    r")\b\s*(?:N\s*(?:[°º]|O|UMERO)?|NO|#)?\s*[:.=\-]?\s*",
    re.I,
)
_DEROGATION_LABEL_RE = re.compile(
    r"\b(?:N\s*(?:[°º]|O)?\s*(?:DE\s+)?DEROGATION|DEROGATION|DEROG\.?)(?:S)?\b"
    r"\s*(?:N\s*(?:[°º]|O|UMERO)?|NO|#)?\s*[:.=\-]?\s*",
    re.I,
)
_REFERENCE_RE = re.compile(r"(?=[A-Z0-9._/-]{3,50}\b)(?=[A-Z0-9._/-]*\d)[A-Z0-9][A-Z0-9._/-]{2,49}", re.I)
_NEXT_REFERENCE_RE = re.compile(r"^\s*(?:,|;|\+|\bET\b|\bAND\b)\s*(?:N\s*(?:[°º]|O)?\s*)?", re.I)
_INVALID_VALUES = {
    "DEVIS", "OFFRE", "PRIX", "NUMERO", "REFERENCE", "REF", "PAGE",
    "EUR", "EURO", "HT", "TTC", "TVA",
}


def _fold(value: Any) -> str:
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    ).upper()
    return re.sub(r"\s+", " ", text).strip()


def _valid_reference(value: str) -> bool:
    value = value.strip(" .,:;#-/")
    if value in _INVALID_VALUES or not any(char.isdigit() for char in value):
        return False
    if re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", value):
        return False
    return 3 <= len(value) <= 50


def _references_after_labels(text: str, kind: str) -> list[str]:
    """Read one or more identifiers immediately following an explicit label."""
    folded = _fold(text)
    if kind == "derogation":
        folded = re.sub(r"\bDEROG\.(?=\s)", "DEROG ", folded)
    label_re = _QUOTE_LABEL_RE if kind == "quote" else _DEROGATION_LABEL_RE
    found: list[str] = []
    for label in label_re.finditer(folded):
        tail = folded[label.end():label.end() + 140]
        first = _REFERENCE_RE.match(tail)
        if not first:
            continue
        value = first.group(0).strip(" .,:;#-/")
        if _valid_reference(value) and value not in found:
            found.append(value)
        position = first.end()
        while position < len(tail):
            separator = _NEXT_REFERENCE_RE.match(tail[position:])
            if not separator:
                break
            position += separator.end()
            candidate = _REFERENCE_RE.match(tail[position:])
            if not candidate:
                break
            value = candidate.group(0).strip(" .,:;#-/")
            if _valid_reference(value) and value not in found:
                found.append(value)
            position += candidate.end()
    return found


def _field(value: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "value": value,
        "raw_value": value,
        "normalized_value": value,
        "confidence": 0.995,
        "final_confidence": 0.995,
        "validation_status": "SOURCE_SUPPORTED",
        "evidence": evidence,
    }


def _direct_references(value: Any) -> list[str]:
    """Read identifiers from a table cell whose column header defines the type."""
    folded = _fold(value)
    found: list[str] = []
    for candidate in _REFERENCE_RE.finditer(folded):
        reference = candidate.group(0).strip(" .,:;#-/")
        if _valid_reference(reference) and reference not in found:
            found.append(reference)
    return found


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).replace("\u00a0", " ").strip()
    text = re.sub(r"[^0-9,.'()\- ]", "", text).replace(" ", "").replace("'", "")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        parsed = float(text)
    except ValueError:
        return None
    return -parsed if negative else parsed


def _material_key(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", _fold(value))


def _description_similarity(left: Any, right: Any) -> float:
    left_value = re.sub(r"[^A-Z0-9 ]", " ", _fold(left))
    right_value = re.sub(r"[^A-Z0-9 ]", " ", _fold(right))
    left_value = " ".join(left_value.split())
    right_value = " ".join(right_value.split())
    if not left_value or not right_value:
        return 0.0
    left_words = {word for word in left_value.split() if len(word) > 2}
    right_words = {word for word in right_value.split() if len(word) > 2}
    overlap = len(left_words & right_words) / max(1, len(left_words | right_words))
    return max(overlap, SequenceMatcher(None, left_value, right_value).ratio())


def _line_material_keys(line: dict[str, Any]) -> set[str]:
    fields = (
        "material_number", "article_number", "supplier_material_number",
        "customer_material_number", "raw_material_reference",
    )
    return {key for key in (_material_key(line.get(field)) for field in fields) if key}


def _header_column(headers: list[Any], *markers: str, excluded: tuple[str, ...] = ()) -> int | None:
    for index, header in enumerate(headers):
        folded = _fold(header)
        if any(marker in folded for marker in markers) and not any(marker in folded for marker in excluded):
            return index
    return None


def _line_value(line: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        parsed = _number(line.get(key))
        if parsed is not None:
            return parsed
    return None


def _same_number(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= max(0.01, abs(right) * 0.0001)


def _match_table_row(
    lines: list[dict[str, Any]],
    row: list[Any],
    columns: dict[str, int | None],
    used: set[int],
) -> int | None:
    def cell(name: str) -> Any:
        index = columns.get(name)
        return row[index] if index is not None and index < len(row) else None

    material = _material_key(cell("material"))
    quantity = _number(cell("quantity"))
    amount = _number(cell("amount"))
    description = cell("description")
    ranked: list[tuple[float, int]] = []
    for index, line in enumerate(lines):
        if index in used:
            continue
        line_materials = _line_material_keys(line)
        exact_material = bool(material and material in line_materials)
        compatible_material = bool(
            material and any(
                len(material) >= 5 and len(candidate) >= 5
                and (material.endswith(candidate) or candidate.endswith(material))
                for candidate in line_materials
            )
        )
        if material and not (exact_material or compatible_material):
            continue
        score = 8.0 if exact_material else 6.0 if compatible_material else 0.0
        if _same_number(quantity, _line_value(line, "quantity")):
            score += 3.0
        if _same_number(amount, _line_value(line, "line_total", "net_amount", "amount")):
            score += 3.0
        similarity = _description_similarity(description, line.get("description"))
        score += 2.0 * similarity
        # Without a printed material reference, require a complete numeric and
        # descriptive signature. This avoids assigning a quote by row position.
        if not material and not (
            _same_number(quantity, _line_value(line, "quantity"))
            and _same_number(amount, _line_value(line, "line_total", "net_amount", "amount"))
            and similarity >= 0.55
        ):
            continue
        ranked.append((score, index))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked[0][1]


def _apply_table_reference_columns(
    payload: dict[str, Any],
    po: dict[str, Any],
) -> dict[tuple[int, str, str], dict[str, Any]]:
    """Attach explicitly-columned references to their matching product line."""
    lines = [line for line in (po.get("lines") or []) if isinstance(line, dict)]
    evidence_by_value: dict[tuple[int, str, str], dict[str, Any]] = {}
    if not lines:
        return evidence_by_value
    for table in payload.get("tables") or []:
        if not isinstance(table, dict):
            continue
        headers = list(table.get("headers") or [])
        rows = list(table.get("data") or [])
        if not headers or not rows:
            continue
        quote_column = _header_column(headers, "DEVIS", "OFFRE", "QUOTE", "QUOTATION")
        derogation_column = _header_column(headers, "DEROG")
        if quote_column is None and derogation_column is None:
            continue
        columns = {
            "material": _header_column(
                headers, "ARTICLE", "MATERIAL", "PRODUIT", "REFERENCE", "REF",
                excluded=("DEVIS", "OFFRE", "QUOTE", "DEROG"),
            ),
            "quantity": _header_column(headers, "QTE", "QUANT", "QTY", "QT", "CDEE"),
            "amount": _header_column(headers, "MONTANT", "AMOUNT", "TOTAL"),
            "description": _header_column(headers, "DESIGN", "SIGNATION", "DESCRIPTION", "LIBELLE"),
        }
        used: set[int] = set()
        for row_index, raw_row in enumerate(rows):
            if not isinstance(raw_row, (list, tuple)):
                continue
            row = list(raw_row)
            if row_index == 0 and any(
                marker in _fold(" ".join(str(value or "") for value in row))
                for marker in ("DEVIS", "OFFRE", "QUOTE", "DEROG")
            ):
                continue
            references: dict[str, list[str]] = {}
            for kind, column in (("quote", quote_column), ("derogation", derogation_column)):
                if column is not None and column < len(row):
                    values = _direct_references(row[column])
                    if values:
                        references[kind] = values
            if not references:
                continue
            line_index = _match_table_row(lines, row, columns, used)
            if line_index is None:
                continue
            used.add(line_index)
            line = lines[line_index]
            page = (table.get("source") or {}).get("page") or table.get("page")
            for kind, values in references.items():
                singular = f"{kind}_number"
                plural = f"{kind}_numbers"
                existing = line.get(plural) or []
                if isinstance(existing, str):
                    existing = [existing]
                merged = list(dict.fromkeys([*(str(value) for value in existing if value), *values]))
                line[plural] = merged
                if not line.get(singular):
                    line[singular] = merged[0]
                column = quote_column if kind == "quote" else derogation_column
                for value in values:
                    evidence_by_value[(line_index, kind, value)] = {
                        "page": page,
                        "source_text": str(row[column]),
                        "extraction_method": f"structured_table_{kind}_column_v1",
                        "table_id": table.get("id"),
                        "table_row": row_index + 1,
                    }
    return evidence_by_value


def _purchase_order(payload: dict[str, Any]) -> dict[str, Any] | None:
    business = payload.get("business_extractions") or {}
    purchase_order = business.get("purchase_order")
    return purchase_order if isinstance(purchase_order, dict) else None


def enrich_commercial_references(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract plural quote/derogation IDs and preserve explicit line links."""
    po = _purchase_order(payload)
    if not po:
        return payload

    records: dict[tuple[str, str], dict[str, Any]] = {}

    def remember(
        kind: str,
        number: str,
        evidence: dict[str, Any],
        *,
        line_number: Any = None,
        material_number: Any = None,
    ) -> None:
        key = (kind, number)
        record = records.setdefault(key, {
            "reference_type": kind,
            "number": number,
            "scope": "document",
            "line_numbers": [],
            "material_numbers": [],
            "confidence": 0.995,
            "validation_status": "SOURCE_SUPPORTED",
            "evidence": [],
        })
        if evidence and evidence not in record["evidence"]:
            record["evidence"].append(evidence)
        if line_number not in (None, ""):
            line_value = str(line_number)
            if line_value not in record["line_numbers"]:
                record["line_numbers"].append(line_value)
            record["scope"] = "line"
        if material_number not in (None, ""):
            material_value = str(material_number)
            if material_value not in record["material_numbers"]:
                record["material_numbers"].append(material_value)

    table_evidence = _apply_table_reference_columns(payload, po)

    # Explicit line evidence is authoritative for the association. Existing
    # parser fields are retained and promoted to plural lists.
    for index, line in enumerate(po.get("lines") or [], 1):
        if not isinstance(line, dict):
            continue
        line_number = line.get("line_number") or str(index)
        material = line.get("material_number") or line.get("article_number")
        text_parts = [line.get("raw_text"), line.get("description")]
        text_parts.extend(line.get("notes") or [])
        line_text = " | ".join(str(value) for value in text_parts if value)
        page = line.get("page")
        for kind, singular, plural in (
            ("quote", "quote_number", "quote_numbers"),
            ("derogation", "derogation_number", "derogation_numbers"),
        ):
            values: list[str] = []
            existing = line.get(plural) or []
            if isinstance(existing, str):
                existing = [existing]
            values.extend(str(value) for value in existing if value)
            if line.get(singular):
                values.append(str(line[singular]))
            values.extend(_references_after_labels(line_text, kind))
            values = list(dict.fromkeys(value for value in values if _valid_reference(value)))
            if not values:
                continue
            line[plural] = values
            if not line.get(singular):
                line[singular] = values[0]
            for value in values:
                evidence = table_evidence.get((index - 1, kind, value)) or {
                    "page": page,
                    "source_text": line_text,
                    "extraction_method": f"explicit_line_{kind}_reference_v1",
                }
                evidence = {key: item for key, item in evidence.items() if item not in (None, "")}
                remember(
                    kind, value, evidence,
                    line_number=line_number,
                    material_number=material,
                )

    # Document-level occurrences remain document-scoped unless the same number
    # was explicitly observed on a product line above.
    for page_index, page in enumerate(payload.get("pages") or [], 1):
        if not isinstance(page, dict):
            continue
        page_number = page.get("page") or page_index
        for raw_line in str(page.get("text") or "").splitlines():
            source_text = " ".join(raw_line.split())
            if not source_text:
                continue
            for kind in ("quote", "derogation"):
                for value in _references_after_labels(source_text, kind):
                    remember(kind, value, {
                        "page": page_number,
                        "source_text": source_text,
                        "extraction_method": f"explicit_document_{kind}_reference_v1",
                    })

    # Preserve a valid quote already extracted by the core even when its page
    # text is unavailable to the runtime layer.
    header = po.setdefault("purchase_order", {})
    existing_quote = header.get("quote_number")
    if isinstance(existing_quote, dict):
        value = existing_quote.get("value")
        if value and _valid_reference(str(value)):
            remember("quote", str(value), existing_quote.get("evidence") or {})

    for kind, list_key, singular, plural in (
        ("quote", "quote_references", "quote_number", "quote_numbers"),
        ("derogation", "derogation_references", "derogation_number", "derogation_numbers"),
    ):
        selected = [
            record for (record_kind, _number), record in records.items()
            if record_kind == kind
        ]
        if not selected:
            continue
        selected.sort(key=lambda item: item["number"])
        po[list_key] = selected
        fields = [_field(item["number"], item["evidence"][0] if item["evidence"] else {}) for item in selected]
        header[plural] = fields
        current = header.get(singular)
        current_value = current.get("value") if isinstance(current, dict) else current
        if not current_value:
            header[singular] = fields[0]

    combined = [records[key] for key in sorted(records)]
    if combined:
        payload["commercial_references"] = combined
    return payload


__all__ = ["enrich_commercial_references"]

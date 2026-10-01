"""Extract all customer PO references from explicitly grouped orders."""
from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any

from jin_runtime.output_quality import _find_purchase_order


_GROUPED_ORDER_RE = re.compile(r"\bCOMMANDE\s+REGROUPEE\b", re.I)
_REFERENCE_LINE_RE = re.compile(
    r"COLIS\s+CONTREMARQUE\s*:\s*(?P<site>.*?)\s*\*+\s*"
    r"RAPPELEZ\s+SUR\s+FACTURE\s+LE\s+N\s*[°ºO]?\s*COMMANDE\s*:\s*"
    r"(?P<number>.*?)\s*\*+\s*$",
    re.I,
)
_PRODUCT_LINE_RE = re.compile(
    r"^(?P<description>.+?)\s+"
    r"(?P<quantity>\d+(?:[.,]\d{2}))\s+"
    r"(?P<uom>PIECE|PCE|PI)\s+"
    r"(?P<schedule>\d{2}/\d{2})"
    r"(?:\s+(?P<unit_price>\d[\d .]*,\d{2})\s+"
    r"(?P<line_total>\d[\d .]*,\d{2}))?\s*$",
    re.I,
)
_SEQUENCE_RE = re.compile(r"^SEQ\s*:\s*(?P<reference>[A-Z0-9._/-]+)\s*$", re.I)


def _fold(value: Any) -> str:
    text = "".join(
        char for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    )
    return " ".join(text.upper().split())


def _page_texts(payload: dict[str, Any]) -> list[tuple[int, str]]:
    pages = payload.get("pages") or []
    if not pages:
        po = _find_purchase_order(payload)
        pages = po.get("pages") or [] if po else []
    if not pages:
        for extraction in (payload.get("business_extractions") or {}).values():
            if isinstance(extraction, dict) and extraction.get("pages"):
                pages = extraction["pages"]
                break
    found = [
        (int(page.get("page") or index), str(page.get("text") or ""))
        for index, page in enumerate(pages, 1)
        if isinstance(page, dict) and page.get("text")
    ]
    if found:
        return found
    raw = str(payload.get("raw_text") or "")
    return [(1, raw)] if raw else []


def _field(value: str, page: int, source_text: str, site: str) -> dict[str, Any]:
    return {
        "value": value,
        "raw_value": value,
        "normalized_value": value,
        "site": site,
        "confidence": 0.999,
        "final_confidence": 0.999,
        "validation_status": "SOURCE_SUPPORTED",
        "evidence": {
            "page": page,
            "source_text": source_text,
            "extraction_method": "explicit_grouped_customer_order_reference_v1",
        },
    }


def _decimal(value: str | None) -> float | None:
    if not value:
        return None
    return float(value.replace(" ", "").replace(".", "").replace(",", "."))


def _reference_values(match: re.Match[str]) -> tuple[str, str]:
    value = " ".join(match.group("number").strip(" *:;,. ").split())
    site = " ".join(match.group("site").strip(" *:;,. ").split())
    return value, site


def _extract_grouped_lines(page_texts: list[tuple[int, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lines: list[dict[str, Any]] = []
    current_number = ""
    current_site = ""
    pending: dict[str, Any] | None = None

    for page_number, page_text in page_texts:
        for raw_line in page_text.splitlines():
            line = " ".join(raw_line.split())
            reference_match = _REFERENCE_LINE_RE.search(line)
            if reference_match:
                current_number, current_site = _reference_values(reference_match)
                pending = None
                continue
            product_match = _PRODUCT_LINE_RE.match(line)
            if product_match and current_number:
                pending = {
                    "description": product_match.group("description").strip(),
                    "quantity": _decimal(product_match.group("quantity")),
                    "uom": product_match.group("uom").upper(),
                    "delivery_schedule": product_match.group("schedule"),
                    "unit_price": _decimal(product_match.group("unit_price")),
                    "net_unit_price": _decimal(product_match.group("unit_price")),
                    "line_net_amount": _decimal(product_match.group("line_total")),
                    "line_total": _decimal(product_match.group("line_total")),
                    "currency": "EUR",
                    "customer_order_number": current_number,
                    "delivery_site": current_site,
                    "raw_text": line,
                    "page": page_number,
                    "confidence": 0.995,
                    "validation_status": "SOURCE_SUPPORTED",
                    "warnings": ([] if product_match.group("unit_price") else ["PRICE_NOT_PRINTED_IN_SOURCE"]),
                }
                continue
            sequence_match = _SEQUENCE_RE.match(line)
            if sequence_match and pending:
                pending["material_number"] = sequence_match.group("reference")
                pending["line_number"] = str(len(lines) + 1)
                pending["evidence"] = {
                    "page": page_number,
                    "source_text": f"{pending['raw_text']} | {line}",
                    "extraction_method": "grouped_order_text_line_v1",
                }
                lines.append(pending)
                pending = None

    groups: list[dict[str, Any]] = []
    ordered_numbers = list(dict.fromkeys(line["customer_order_number"] for line in lines))
    for number in ordered_numbers:
        group_lines = [line for line in lines if line["customer_order_number"] == number]
        priced = [line for line in group_lines if line.get("line_total") is not None]
        groups.append({
            "customer_order_number": number,
            "site": group_lines[0].get("delivery_site"),
            "line_count": len(group_lines),
            "priced_line_count": len(priced),
            "pricing_complete": len(priced) == len(group_lines),
            "total_net": round(sum(float(line["line_total"]) for line in priced), 2) if priced else None,
            "currency": "EUR",
        })
    return lines, groups


def enrich_grouped_order_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Expose every agency/customer number without inventing a primary one."""
    page_texts = _page_texts(payload)
    if not page_texts or not any(_GROUPED_ORDER_RE.search(_fold(text)) for _, text in page_texts):
        return payload

    references: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page_number, page_text in page_texts:
        for raw_line in page_text.splitlines():
            line = " ".join(raw_line.split())
            match = _REFERENCE_LINE_RE.search(line)
            if not match:
                continue
            value, site = _reference_values(match)
            key = re.sub(r"[^A-Z0-9]", "", _fold(value))
            if len(key) < 5 or sum(char.isdigit() for char in key) < 3 or key in seen:
                continue
            seen.add(key)
            references.append(_field(value, page_number, line, site))

    if len(references) < 2:
        return payload

    document = payload.setdefault("document", {})
    document["order_structure"] = "grouped"
    document["grouped_order_count"] = len(references)
    payload["customer_order_references"] = references
    document_references = payload.setdefault("document_references", {})
    document_references.pop("customer_order_number", None)
    document_references["customer_order_numbers"] = references

    po = _find_purchase_order(payload)
    if po:
        header = po.get("purchase_order")
        if not isinstance(header, dict):
            header = po
        current = header.get("number") or header.get("order_number")
        if current:
            header["number_original"] = copy.deepcopy(current)
        header["number"] = {
            "value": None,
            "final_confidence": 0.0,
            "validation_status": "MULTIPLE_VALUES",
            "warnings": ["Multiple explicit customer order numbers; no primary number selected."],
        }
        header["numbers"] = references
        po["order_structure"] = "grouped"
        grouped_lines, groups = _extract_grouped_lines(page_texts)
        if len(grouped_lines) >= 2:
            po["lines_original"] = copy.deepcopy(po.get("lines") or [])
            po["lines"] = grouped_lines
            po["grouped_orders"] = groups
            payload["grouped_orders"] = groups
    return payload

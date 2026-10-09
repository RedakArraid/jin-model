from __future__ import annotations

import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ADDRESS_KEYS = (
    "building", "industrial_zone", "house_number", "house_number_suffix",
    "street_type", "street_name", "postal_box", "bp", "cs", "tsa",
    "postal_code", "city", "cedex", "country",
)
PARTY_ROLES = ("buyer", "supplier", "ship_to", "bill_to")


def empty_common_document() -> dict[str, Any]:
    return {
        "document_type": None,
        "order_number": None,
        "order_date": None,
        "customer_reference": None,
        "supplier_reference": None,
        "parties": {
            role: {"name": None, "address": {key: None for key in ADDRESS_KEYS}}
            for role in PARTY_ROLES
        },
        "lines": [],
        "totals": {
            "net": None,
            "vat": None,
            "gross": None,
            "amount_due": None,
        },
        "spatial": {"fields": [], "zones": []},
    }


def field_value(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def _first_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = field_value(mapping.get(key))
        if value not in (None, ""):
            return value
    return None


def _address_from_party(party: dict[str, Any] | None) -> dict[str, Any]:
    party = party or {}
    address = party.get("address") if isinstance(party.get("address"), dict) else {}
    out = {key: address.get(key) for key in ADDRESS_KEYS}
    if out.get("bp") is None:
        out["bp"] = address.get("postal_box")
    return out


def _address_record(clean: dict[str, Any], role: str) -> dict[str, Any] | None:
    order = clean.get("order") if isinstance(clean.get("order"), dict) else {}
    addresses = order.get("addresses") or []
    records = [
        item for item in addresses
        if isinstance(item, dict) and item.get("role") == role
    ]
    if not records:
        return None
    record = records[0]
    components = (
        record.get("components")
        if isinstance(record.get("components"), dict)
        else {}
    )
    result = {key: components.get(key) for key in ADDRESS_KEYS}
    if result.get("bp") is None:
        result["bp"] = components.get("postal_box")
    return result


def _evidence_to_spatial(path: str, field: Any) -> dict[str, Any] | None:
    if not isinstance(field, dict):
        return None
    evidence = (
        field.get("evidence")
        if isinstance(field.get("evidence"), dict)
        else {}
    )
    bbox = evidence.get("bbox")
    page = evidence.get("page")
    if not (isinstance(bbox, list) and len(bbox) == 4):
        return None
    return {
        "path": path,
        "page": int(page or 1),
        "bbox": [float(x) for x in bbox],
    }


def jin_clean_to_common(clean: dict[str, Any]) -> dict[str, Any]:
    out = empty_common_document()
    document = (
        clean.get("document") if isinstance(clean.get("document"), dict) else {}
    )
    order = clean.get("order") if isinstance(clean.get("order"), dict) else {}
    parties = (
        order.get("parties") if isinstance(order.get("parties"), dict) else {}
    )

    out["document_type"] = document.get("type")
    out["order_number"] = field_value(order.get("customer_order_number"))
    out["order_date"] = field_value(order.get("order_date"))
    out["customer_reference"] = field_value(order.get("customer_reference"))
    out["supplier_reference"] = field_value(order.get("supplier_reference"))

    for role in PARTY_ROLES:
        party = parties.get(role) if isinstance(parties.get(role), dict) else {}
        out["parties"][role]["name"] = party.get("legal_name") or party.get("name")
        address = _address_record(clean, role) or _address_from_party(party)
        out["parties"][role]["address"].update(address)

    for index, line in enumerate(order.get("line_items") or []):
        if not isinstance(line, dict):
            continue
        refs = line.get("references") if isinstance(line.get("references"), dict) else {}
        pricing = line.get("pricing") if isinstance(line.get("pricing"), dict) else {}
        amounts = line.get("amounts") if isinstance(line.get("amounts"), dict) else {}
        reference = _first_value(
            refs,
            "material_number", "article_number", "supplier_material_number",
            "manufacturer_part_number", "customer_material_number", "sku", "ean", "gtin",
        )
        out["lines"].append({
            "line_index": index,
            "reference": reference,
            "description": line.get("description"),
            "quantity": field_value(line.get("quantity")),
            "unit": field_value(line.get("unit")),
            "unit_price": _first_value(
                pricing, "net_unit_price", "unit_price", "gross_unit_price", "price"
            ),
            "line_total": _first_value(
                amounts, "line_total", "net", "total", "amount"
            ),
        })

    totals = order.get("totals") if isinstance(order.get("totals"), dict) else {}
    out["totals"] = {
        "net": _first_value(totals, "total_net", "net", "net_amount", "subtotal"),
        "vat": _first_value(totals, "total_vat", "vat", "tax", "vat_amount"),
        "gross": _first_value(totals, "total_gross", "gross", "gross_amount", "total_ttc"),
        "amount_due": _first_value(totals, "amount_due", "net_to_pay", "total_due"),
    }

    spatial = []
    for path, field in (
        ("order_number", order.get("customer_order_number")),
        ("order_date", order.get("order_date")),
        ("customer_reference", order.get("customer_reference")),
        ("supplier_reference", order.get("supplier_reference")),
    ):
        item = _evidence_to_spatial(path, field)
        if item:
            spatial.append(item)
    out["spatial"]["fields"] = spatial
    return compact_common(out)


def compact_common(value: Any) -> Any:
    if isinstance(value, dict):
        output = {}
        for key, child in value.items():
            compacted = compact_common(child)
            if compacted not in (None, "", [], {}):
                output[key] = compacted
        return output
    if isinstance(value, list):
        output = []
        for child in value:
            compacted = compact_common(child)
            if compacted not in (None, "", [], {}):
                output.append(compacted)
        return output
    return value


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value))
        if not unicodedata.combining(char)
    ).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def normalize_identifier(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def normalize_number(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    raw = str(value).replace("\u00a0", " ").replace("\u202f", " ").strip()
    raw = re.sub(r"[^0-9,\.\-+]", "", raw)
    if not raw:
        return None
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def parse_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        value = json.loads(raw)
        return (value, None) if isinstance(value, dict) else (None, "JSON_NOT_OBJECT")
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    if start < 0:
        return None, "NO_JSON_OBJECT"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                fragment = raw[start:index + 1]
                try:
                    value = json.loads(fragment)
                    return (value, None) if isinstance(value, dict) else (None, "JSON_NOT_OBJECT")
                except json.JSONDecodeError:
                    return None, "INVALID_JSON"
    return None, "UNTERMINATED_JSON"


def load_ground_truth(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if "documents" in payload and isinstance(payload["documents"], dict):
        return payload["documents"]
    if isinstance(payload, dict):
        return payload
    raise ValueError("Ground truth must be a JSON object keyed by relative PDF filename.")

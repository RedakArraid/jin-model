from __future__ import annotations

import copy
import math
import re
import unicodedata
from typing import Any, Iterable


def _norm(value: Any) -> str:
    if value is None:
        return ""
    text = "".join(c for c in unicodedata.normalize("NFKD", str(value)) if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def _value(obj: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = obj.get(key)
        if value not in (None, "", False):
            return value
    return None


def _address_dict(block: dict[str, Any]) -> dict[str, Any]:
    nested = block.get("address")
    return nested if isinstance(nested, dict) else block


def _iter_address_lists(obj: Any) -> Iterable[list[dict[str, Any]]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in {"business_addresses", "addresses"} and isinstance(value, list):
                items = [x for x in value if isinstance(x, dict)]
                if items:
                    yield items
            yield from _iter_address_lists(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _iter_address_lists(value)


def _append_unique(parts: list[str], value: Any, prefix: str | None = None) -> None:
    if value in (None, "", False):
        return
    text = str(value).strip()
    if not text:
        return
    if prefix and not _norm(text).startswith(_norm(prefix) + " ") and _norm(text) != _norm(prefix):
        text = f"{prefix} {text}"
    n = _norm(text)
    if not n:
        return
    for existing in parts:
        if n == _norm(existing):
            return
    parts.append(text)


def _street_line(a: dict[str, Any]) -> str:
    house = _value(a, "house_number", "number", "numero")
    suffix = _value(a, "house_number_suffix", "street_number_suffix", "suffix")
    street_type = _value(a, "street_type", "street_kind", "voie_type")
    street_name = _value(a, "street_name", "voie")
    explicit_street = _value(a, "street")

    pieces: list[str] = []
    if house:
        pieces.append(str(house).strip())
    if suffix:
        pieces.append(str(suffix).strip())
    if street_type:
        pieces.append(str(street_type).strip())
    if street_name:
        pieces.append(str(street_name).strip())
    structured = " ".join(x for x in pieces if x).strip()

    if structured and (street_type or house):
        return structured
    return str(explicit_street or structured or "").strip()


def build_formatted_address(block: dict[str, Any]) -> str:
    """Build a stable formatted address from structured components.

    It deliberately never derives verification truth. It only formats already
    extracted fields and removes exact duplicate components.
    """
    a = _address_dict(block)
    parts: list[str] = []

    for key in ("building", "residence", "entry", "floor", "unit"):
        _append_unique(parts, a.get(key))

    _append_unique(parts, _street_line(a))

    for key in ("industrial_zone", "activity_park", "place_name", "complement", "address_complement"):
        _append_unique(parts, a.get(key))

    _append_unique(parts, _value(a, "postal_box", "bp"), "BP")
    _append_unique(parts, a.get("tsa"), "TSA")
    _append_unique(parts, a.get("cs"), "CS")
    _append_unique(parts, a.get("postal_routing_code"))

    postal_code = _value(a, "postal_code", "zip_code", "zipcode")
    city = _value(a, "city", "commune", "ville")
    cedex = a.get("cedex")
    cedex_number = _value(a, "cedex_number", "cedex_no")
    postal_parts: list[str] = []
    if postal_code:
        postal_parts.append(str(postal_code).strip())
    if city:
        postal_parts.append(str(city).strip())
    if cedex is True or cedex_number:
        postal_parts.append("CEDEX")
    if cedex_number:
        postal_parts.append(str(cedex_number).strip())
    _append_unique(parts, " ".join(postal_parts))

    _append_unique(parts, _value(a, "country", "country_name"))
    return ", ".join(parts)


def _count_phrase(text: str, phrase: Any) -> int:
    phrase_n = _norm(phrase)
    if not phrase_n:
        return 0
    text_tokens = _norm(text).split()
    phrase_tokens = phrase_n.split()
    if not phrase_tokens or len(phrase_tokens) > len(text_tokens):
        return 0
    n = len(phrase_tokens)
    return sum(1 for i in range(len(text_tokens) - n + 1) if text_tokens[i : i + n] == phrase_tokens)


def _money(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, str):
        s = value.strip().replace("\u00a0", " ").replace(" ", "")
        if not s:
            return None
        if "," in s and "." not in s:
            s = s.replace(",", ".")
        elif "," in s and "." in s and s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        try:
            v = float(re.sub(r"[^0-9+\-.]", "", s))
            return v if math.isfinite(v) else None
        except ValueError:
            return None
    return None


def _find_purchase_order(result: dict[str, Any]) -> dict[str, Any]:
    business = result.get("business_extractions")
    if isinstance(business, dict):
        for key in ("purchase_order", "purchase_order_with_terms"):
            value = business.get(key)
            if isinstance(value, dict):
                return value.get("purchase_order") if isinstance(value.get("purchase_order"), dict) else value
        for value in business.values():
            if isinstance(value, dict):
                po = value.get("purchase_order")
                if isinstance(po, dict):
                    return po
                if any(k in value for k in ("lines", "totals", "order_number", "purchase_order_number")):
                    return value
    return result.get("purchase_order") if isinstance(result.get("purchase_order"), dict) else {}


def _issue(issues: list[dict[str, Any]], code: str, severity: str, path: str, message: str, **details: Any) -> None:
    item = {"code": code, "severity": severity, "path": path, "message": message}
    if details:
        item["details"] = details
    issues.append(item)


def _audit_address(block: dict[str, Any], path: str, issues: list[dict[str, Any]], repair: bool) -> int:
    repairs = 0
    a = _address_dict(block)
    formatted = block.get("formatted_address") or a.get("formatted_address")
    city = _value(a, "city", "commune", "ville")
    postal = _value(a, "postal_code", "zip_code", "zipcode")
    country_code = str(_value(a, "country_code") or block.get("country_code") or "").upper()
    country = _norm(_value(a, "country") or block.get("country"))

    duplicate_city = bool(formatted and city and _count_phrase(str(formatted), city) > 1)
    if duplicate_city:
        _issue(
            issues,
            "ADDRESS_DUPLICATE_CITY",
            "warning",
            f"{path}.formatted_address",
            "City appears more than once in formatted_address.",
            city=city,
            formatted_address=formatted,
        )

    canonical = build_formatted_address(block)
    if repair and canonical and (duplicate_city or not formatted):
        if formatted and formatted != canonical:
            block.setdefault("formatted_address_original", formatted)
        block["formatted_address"] = canonical
        block["formatted_address_source"] = "structured_components_v2"
        repairs += 1

    if postal and (country_code == "FR" or country == "france") and not re.fullmatch(r"\d{5}", str(postal).strip()):
        _issue(
            issues,
            "FR_POSTAL_CODE_INVALID",
            "warning",
            f"{path}.address.postal_code",
            "French postal code is not exactly five digits.",
            postal_code=postal,
        )
    if postal and not city:
        _issue(
            issues,
            "ADDRESS_CITY_MISSING",
            "warning",
            f"{path}.address.city",
            "Postal code is present but city is missing.",
            postal_code=postal,
        )

    lat = _money(_value(a, "latitude", "lat"))
    lon = _money(_value(a, "longitude", "lon", "lng"))
    if lat is not None and not (-90 <= lat <= 90):
        _issue(issues, "LATITUDE_OUT_OF_RANGE", "error", f"{path}.address.latitude", "Latitude is outside [-90, 90].", latitude=lat)
    if lon is not None and not (-180 <= lon <= 180):
        _issue(issues, "LONGITUDE_OUT_OF_RANGE", "error", f"{path}.address.longitude", "Longitude is outside [-180, 180].", longitude=lon)

    verified = block.get("is_verified_real_address")
    status = str(block.get("verification_status") or a.get("verification_status") or "").upper()
    if verified is True and status not in {"VERIFIED", "CONFIRMED"}:
        _issue(
            issues,
            "VERIFICATION_STATUS_CONTRADICTION",
            "error",
            path,
            "is_verified_real_address=true without VERIFIED/CONFIRMED status.",
            verification_status=status or None,
        )
    if status in {"VERIFIED", "CONFIRMED"} and not (block.get("verification_provider") or a.get("verification_provider")):
        _issue(issues, "VERIFICATION_PROVIDER_MISSING", "warning", path, "Verified address has no verification_provider.")
    return repairs


def _audit_lines(po: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    lines = po.get("lines")
    if not isinstance(lines, list):
        return
    seen_numbers: set[str] = set()
    for idx, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        path = f"purchase_order.lines[{idx}]"
        line_no = line.get("line_number")
        if line_no not in (None, ""):
            key = str(line_no)
            if key in seen_numbers:
                _issue(issues, "DUPLICATE_LINE_NUMBER", "warning", f"{path}.line_number", "Line number is duplicated.", line_number=line_no)
            seen_numbers.add(key)

        qty = _money(line.get("quantity"))
        unit_price = _money(line.get("unit_price"))
        total = _money(line.get("line_total") if "line_total" in line else line.get("amount"))
        if qty is not None and unit_price is not None and total is not None and not any(
            k in line for k in ("discount", "discount_amount", "discount_rate")
        ):
            expected = qty * unit_price
            tolerance = max(0.03, abs(total) * 0.01)
            if abs(expected - total) > tolerance:
                _issue(
                    issues,
                    "LINE_AMOUNT_MISMATCH",
                    "warning",
                    path,
                    "quantity * unit_price differs from line total.",
                    quantity=qty,
                    unit_price=unit_price,
                    expected=round(expected, 4),
                    line_total=total,
                )


def _audit_totals(po: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    totals = po.get("totals") if isinstance(po.get("totals"), dict) else {}
    if not totals:
        return
    net = _money(_value(totals, "total_before_tax", "total_net", "net", "subtotal"))
    vat = _money(_value(totals, "total_vat", "vat", "total_tax"))
    gross = _money(_value(totals, "amount_due", "grand_total", "total_gross", "gross"))
    if net is not None and vat is not None and gross is not None:
        expected = net + vat
        tolerance = max(0.05, abs(gross) * 0.005)
        if abs(expected - gross) > tolerance:
            _issue(
                issues,
                "TOTAL_ARITHMETIC_MISMATCH",
                "warning",
                "purchase_order.totals",
                "Net + VAT differs from gross/amount_due.",
                net=net,
                vat=vat,
                expected=round(expected, 4),
                gross=gross,
            )


def audit_and_repair_output(result: dict[str, Any], repair: bool = True) -> dict[str, Any]:
    out = copy.deepcopy(result)
    issues: list[dict[str, Any]] = []
    repairs = 0

    for list_idx, addresses in enumerate(_iter_address_lists(out)):
        for idx, block in enumerate(addresses):
            repairs += _audit_address(block, f"addresses[{list_idx}][{idx}]", issues, repair)

    po = _find_purchase_order(out)
    if po:
        _audit_lines(po, issues)
        _audit_totals(po, issues)

    errors = sum(1 for x in issues if x["severity"] == "error")
    warnings = sum(1 for x in issues if x["severity"] == "warning")
    out["output_quality"] = {
        "status": "ERROR" if errors else "WARN" if warnings else "PASS",
        "issue_count": len(issues),
        "error_count": errors,
        "warning_count": warnings,
        "repair_count": repairs,
        "issues": issues,
    }
    return out

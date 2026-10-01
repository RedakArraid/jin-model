"""Source-backed extraction of customer agency/site codes.

Agency, branch and site identifiers are business routing data, not postal
address components.  This module only promotes a code when it is explicitly
labelled or when the same short token is independently present beside the
order number and inside the selected delivery block.  The latter handles
layouts such as ``N° de commande: ... / VIU`` without customer-specific
look-up tables.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any, Iterable

from jin_runtime.output_quality import _find_purchase_order


DELIVERY_ROLES = {"ship_to", "deliver_to", "consignee"}
_CODE_TOKEN_RE = re.compile(r"^[A-Z0-9](?:[A-Z0-9._/-]{0,18}[A-Z0-9])?$", re.I)
_ORDER_LABEL_RE = re.compile(
    r"\b(?:N\s*[°ºO]?\s*(?:DE\s+)?(?:COMMANDE|CDE)|PURCHASE\s+ORDER|P\.?\s*O\.?)\b",
    re.I,
)
_TRAILING_CODE_RE = re.compile(r"(?:/|\|)\s*(?P<code>[A-Z0-9][A-Z0-9._-]{1,19})\s*$", re.I)
_EXPLICIT_CODE_RES = (
    re.compile(
        r"\b(?:CODE|N\s*[°ºO]?|NUM(?:E|É)RO|ID)\s*(?:D['’ ]?\s*)?"
        r"(?:CLIENT\s+)?(?:AGENCE|SITE|ÉTABLISSEMENT|ETABLISSEMENT|SUCCURSALE|"
        r"DÉPÔT|DEPOT|MAGASIN|CENTRE|POINT\s+DE\s+VENTE)"
        r"(?:\s+(?:CLIENT|DE\s+LIVRAISON|DESTINATAIRE))?\s*[:#-]?\s*"
        r"(?P<code>[A-Z0-9][A-Z0-9._/-]{1,19})\b",
        re.I,
    ),
    re.compile(
        r"\b(?:CUSTOMER\s+)?(?:AGENCY|BRANCH|SITE|LOCATION|STORE|DEPOT)\s+"
        r"(?:CODE|ID|NO\.?|NUMBER)\s*[:#-]?\s*"
        r"(?P<code>[A-Z0-9][A-Z0-9._/-]{1,19})\b",
        re.I,
    ),
)
_STOP_CODES = {
    "ADRESSE", "AGENCE", "ATTN", "BAT", "BATIMENT", "BP", "CEDEX", "CENTRE",
    "CLIENT", "CODE", "CS", "DEPOT", "DESTINATAIRE", "FR", "FRANCE", "LIVRAISON",
    "MAGASIN", "PORT", "QUAI", "RUE", "SITE", "TSA", "ZA", "ZAC", "ZAE", "ZI",
}


def _fold(value: Any) -> str:
    text = "".join(
        char for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    ).upper()
    return re.sub(r"[^A-Z0-9]+", " ", text).strip()


def _code(value: Any) -> str | None:
    text = re.sub(r"\s+", "", str(value or "").strip(" \t,;:#()[]{}" )).upper()
    return text or None


def _valid_code(value: Any, *, explicit: bool = False) -> bool:
    code = _code(value)
    if not code or not _CODE_TOKEN_RE.fullmatch(code) or _fold(code) in _STOP_CODES:
        return False
    if len(code) < 2 or len(code) > 20:
        return False
    if re.fullmatch(r"\d{5}", code) or re.fullmatch(r"\d{8,}", code):
        return False
    return explicit or any(char.isalpha() for char in code)


def _block_evidence(block: dict[str, Any], method: str) -> dict[str, Any]:
    source = block.get("source") if isinstance(block.get("source"), dict) else {}
    return {
        key: value for key, value in {
            "page": source.get("page") or block.get("page"),
            "bbox": source.get("bbox") or block.get("bbox"),
            "source_text": block.get("text"),
            "extraction_method": method,
        }.items() if value not in (None, "", [])
    }


def _blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    def add(items: Any) -> None:
        if not isinstance(items, list):
            return
        for block in items:
            if not isinstance(block, dict) or not block.get("text"):
                continue
            source = block.get("source") if isinstance(block.get("source"), dict) else {}
            bbox = source.get("bbox") or block.get("bbox") or []
            key = (source.get("page") or block.get("page"), str(block.get("text")), tuple(bbox))
            if key not in seen:
                seen.add(key)
                found.append(block)

    add(payload.get("blocks"))
    for page in payload.get("pages") or []:
        if isinstance(page, dict):
            add(page.get("blocks"))
    return found


def _order_number(po: dict[str, Any]) -> str | None:
    header = po.get("purchase_order") if isinstance(po.get("purchase_order"), dict) else po
    for key in ("number", "order_number", "purchase_order_number"):
        field = header.get(key)
        value = field.get("value") if isinstance(field, dict) else field
        if value not in (None, ""):
            return str(value).strip()
    return None


def _delivery_blocks(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if str(value.get("role") or "").lower() in DELIVERY_ROLES:
            yield value
        for child in value.values():
            yield from _delivery_blocks(child)
    elif isinstance(value, list):
        for child in value:
            yield from _delivery_blocks(child)


def _address(block: dict[str, Any]) -> dict[str, Any]:
    address = block.get("address")
    return address if isinstance(address, dict) else block


def _delivery_tokens(po: dict[str, Any]) -> set[str]:
    values: list[Any] = []
    for block in po.get("business_addresses") or []:
        if not isinstance(block, dict) or str(block.get("role") or "").lower() not in DELIVERY_ROLES:
            continue
        address = _address(block)
        values.extend(address.get("raw_lines") or [])
        values.extend(address.get(key) for key in ("address_complement", "line1", "line2", "line3"))
    ship_to = po.get("ship_to")
    if isinstance(ship_to, dict):
        address = _address(ship_to)
        values.extend(address.get("raw_lines") or [])
        values.extend(address.get(key) for key in ("address_complement", "line1", "line2", "line3"))
    return {
        code for value in values
        if (code := _code(value)) and _valid_code(code)
    }


def _same_code(value: Any, code: str) -> bool:
    return _code(value) == code


def _candidate_evidence_for_delivery_token(
    blocks: list[dict[str, Any]], code: str,
) -> dict[str, Any] | None:
    for block in blocks:
        if _same_code(block.get("text"), code):
            return _block_evidence(block, "isolated_delivery_block_code_v1")
    return None


def _explicit_candidates(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for block in blocks:
        text = " ".join(str(block.get("text") or "").split())
        for pattern in _EXPLICIT_CODE_RES:
            for match in pattern.finditer(text):
                code = _code(match.group("code"))
                raw_code = match.group("code")
                labelled_separator = text[:match.start("code")].rstrip().endswith((":", "#", "-"))
                code_shaped = (
                    any(char.isdigit() for char in raw_code)
                    or any(char in "._/-" for char in raw_code)
                    or (raw_code == raw_code.upper() and len(raw_code) <= 5)
                )
                if code and _valid_code(code, explicit=True) and (labelled_separator or code_shaped):
                    candidates.append({
                        "value": code,
                        "confidence": 0.995,
                        "reason": "explicit_customer_agency_code_label",
                        "evidence": _block_evidence(block, "explicit_customer_agency_code_v1"),
                    })
    return candidates


def _corroborated_candidates(
    blocks: list[dict[str, Any]], po: dict[str, Any], delivery_tokens: set[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    order_number = _order_number(po)
    if not order_number:
        return candidates
    folded_number = _fold(order_number)
    for block in blocks:
        text = " ".join(str(block.get("text") or "").split())
        if not _ORDER_LABEL_RE.search(_fold(text)):
            continue
        match = _TRAILING_CODE_RE.search(text)
        if not match or folded_number not in _fold(text[:match.start()]):
            continue
        code = _code(match.group("code"))
        if not code or not _valid_code(code) or code not in delivery_tokens:
            continue
        corroboration = _candidate_evidence_for_delivery_token(blocks, code)
        candidates.append({
            "value": code,
            "confidence": 0.99,
            "reason": "order_suffix_corroborated_in_delivery_block",
            "evidence": _block_evidence(block, "corroborated_customer_agency_code_v1"),
            "corroborating_evidence": [corroboration] if corroboration else [],
        })
    return candidates


def _select(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate["value"]].append(candidate)
    if not grouped:
        return None
    ranked = sorted(
        grouped.items(),
        key=lambda item: (max(c["confidence"] for c in item[1]), len(item[1])),
        reverse=True,
    )
    best_value, best_items = ranked[0]
    best_score = max(item["confidence"] for item in best_items)
    if len(ranked) > 1:
        second_score = max(item["confidence"] for item in ranked[1][1])
        if second_score == best_score:
            return None
    primary = max(best_items, key=lambda item: item["confidence"])
    corroborating = [
        evidence
        for item in best_items
        for evidence in item.get("corroborating_evidence") or []
        if evidence
    ]
    source_corroborated = bool(corroborating) or primary.get("reason") == (
        "order_suffix_corroborated_in_delivery_block"
    )
    return {
        "value": best_value,
        "raw_value": best_value,
        "normalized_value": best_value,
        "site": "ship_to",
        "confidence": best_score,
        "final_confidence": best_score,
        "validation_status": "SOURCE_CORROBORATED" if source_corroborated else "SOURCE_SUPPORTED",
        "evidence": primary.get("evidence") or {},
        "corroborating_evidence": corroborating,
        "extraction_reason": primary.get("reason"),
    }


def enrich_customer_agency_codes(payload: dict[str, Any]) -> dict[str, Any]:
    """Expose a dedicated agency code and keep it out of clean postal labels."""
    po = _find_purchase_order(payload)
    if not po:
        return payload
    blocks = _blocks(payload)
    candidates = _explicit_candidates(blocks)
    candidates.extend(_corroborated_candidates(blocks, po, _delivery_tokens(po)))
    selected = _select(candidates)
    if candidates:
        payload["customer_agency_code_candidates"] = candidates
    if not selected:
        return payload

    payload["customer_agency_code"] = selected
    po["customer_agency_code"] = selected
    header = po.get("purchase_order")
    if isinstance(header, dict):
        header["customer_agency_code"] = selected

    code = selected["value"]
    ship_to = po.get("ship_to")
    if isinstance(ship_to, dict):
        ship_to["customer_agency_code"] = code

    canonical_delivery = [
        block for block in po.get("business_addresses") or []
        if isinstance(block, dict)
        and str(block.get("role") or "").lower() == "ship_to"
        and "superseded" not in str(block.get("role_label") or "").lower()
        and not any("superseded" in str(warning).lower() for warning in block.get("warnings") or [])
    ]
    if len(canonical_delivery) == 1:
        canonical_delivery[0]["customer_agency_code"] = code
        canonical_delivery[0]["customer_agency_code_evidence"] = selected.get("evidence")

    seen: set[int] = set()
    for block in _delivery_blocks(payload):
        if id(block) in seen:
            continue
        seen.add(id(block))
        address = _address(block)
        address_values = list(address.get("raw_lines") or [])
        address_values.extend(address.get(key) for key in ("address_complement", "line1", "line2", "line3"))
        if any(_same_code(value, code) for value in address_values):
            block["customer_agency_code"] = code
            block["customer_agency_code_evidence"] = selected.get("evidence")
            warnings = block.setdefault("warnings", [])
            marker = "customer_agency_code_separated_from_postal_address"
            if marker not in warnings:
                warnings.append(marker)
    return payload

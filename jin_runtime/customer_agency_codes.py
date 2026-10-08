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
    re.compile(
        # Bare Magasin/Site/Dépôt captions often introduce a business
        # activity, not a routing code. Their explicit ``CODE ...`` forms are
        # still handled above; this shorthand is reserved for ``Agence``.
        r"(?:^|[\n|])\s*AGENCE\s*[:#-]?\s*"
        r"(?P<code>[A-Z0-9][A-Z0-9._/-]{1,19})\b",
        re.I,
    ),
)
_STOP_CODES = {
    "ADRESSE", "AGENCE", "ATTN", "BAT", "BATIMENT", "BP", "CEDEX", "CENTRE",
    "CLIENT", "CODE", "CS", "DEPOT", "DESTINATAIRE", "FR", "FRANCE", "LIVRAISON",
    "DE", "DU", "LA", "LE", "LES", "MAGASIN", "PORT", "QUAI", "RUE",
    "SITE", "TSA", "ZA", "ZAC", "ZAE", "ZI",
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


def _valid_code(
    value: Any, *, explicit: bool = False, corroborated_numeric: bool = False,
) -> bool:
    code = _code(value)
    if not code or not _CODE_TOKEN_RE.fullmatch(code) or _fold(code) in _STOP_CODES:
        return False
    if len(code) < 2 or len(code) > 20:
        return False
    if code.isdigit():
        # A numeric agency identifier can look exactly like a French postcode.
        # Accept it only behind an explicit label or when another independent
        # source (the order-number prefix) corroborates the isolated delivery
        # token.  Long digit strings remain transaction/contact identifiers.
        return bool((explicit or corroborated_numeric) and 2 <= len(code) <= 7)
    if re.fullmatch(r"\d{8,}", code):
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
    postal_codes: set[str] = set()
    for block in po.get("business_addresses") or []:
        if not isinstance(block, dict) or str(block.get("role") or "").lower() not in DELIVERY_ROLES:
            continue
        address = _address(block)
        if postal_code := _code(address.get("postal_code")):
            postal_codes.add(postal_code)
        values.extend(address.get("raw_lines") or [])
        values.extend(address.get(key) for key in ("address_complement", "line1", "line2", "line3"))
    ship_to = po.get("ship_to")
    if isinstance(ship_to, dict):
        address = _address(ship_to)
        if postal_code := _code(address.get("postal_code")):
            postal_codes.add(postal_code)
        values.extend(address.get("raw_lines") or [])
        values.extend(address.get(key) for key in ("address_complement", "line1", "line2", "line3"))
    return {
        code for value in values
        if (code := _code(value))
        and code not in postal_codes
        and _valid_code(code, corroborated_numeric=code.isdigit())
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


def _source_page_bbox(block: dict[str, Any]) -> tuple[Any, Any]:
    source = block.get("source") if isinstance(block.get("source"), dict) else {}
    return source.get("page") or block.get("page"), source.get("bbox") or block.get("bbox")


def _delivery_postal_codes(po: dict[str, Any]) -> set[str]:
    postcodes: set[str] = set()
    for block in _delivery_blocks(po):
        address = _address(block)
        if postcode := _code(address.get("postal_code")):
            postcodes.add(postcode)
    return postcodes


def _near_delivery_heading(
    blocks: list[dict[str, Any]], code_block: dict[str, Any],
) -> dict[str, Any] | None:
    code_page, code_bbox = _source_page_bbox(code_block)
    if not isinstance(code_bbox, (list, tuple)) or len(code_bbox) != 4:
        return None
    for block in blocks:
        folded = _fold(block.get("text"))
        if not re.search(r"\b(?:ADRESSE DE LIVRAISON|A LIVRER|SHIP TO|DELIVER TO)\b", folded):
            continue
        page, bbox = _source_page_bbox(block)
        if page != code_page or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        if 0 <= float(code_bbox[1]) - float(bbox[1]) <= 180:
            return block
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

    # Vector PDFs often split ``Agence`` and its value into two text blocks.
    # Pair only an isolated role label with the nearest code-shaped block on
    # the same visual row and page.
    for label in blocks:
        if _fold(label.get("text")) != "AGENCE":
            continue
        label_page, label_bbox = _source_page_bbox(label)
        if not isinstance(label_bbox, (list, tuple)) or len(label_bbox) != 4:
            continue
        aligned: list[tuple[float, dict[str, Any], str]] = []
        label_center_y = (float(label_bbox[1]) + float(label_bbox[3])) / 2
        for block in blocks:
            if block is label:
                continue
            page, bbox = _source_page_bbox(block)
            if page != label_page or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                continue
            code = _code(block.get("text"))
            block_center_y = (float(bbox[1]) + float(bbox[3])) / 2
            gap = float(bbox[0]) - float(label_bbox[2])
            if (
                code and _valid_code(code, explicit=True)
                and 0 <= gap <= 300
                and abs(block_center_y - label_center_y) <= 12
            ):
                aligned.append((gap, block, code))
        if aligned:
            _, block, code = min(aligned, key=lambda item: item[0])
            candidates.append({
                "value": code,
                "confidence": 0.995,
                "reason": "paired_customer_agency_label_and_code",
                "evidence": _block_evidence(
                    block, "paired_customer_agency_label_code_v2"
                ),
                "corroborating_evidence": [
                    _block_evidence(label, "customer_agency_role_label_v2")
                ],
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
        if match and folded_number in _fold(text[:match.start()]):
            code = _code(match.group("code"))
            if code and _valid_code(code) and code in delivery_tokens:
                corroboration = _candidate_evidence_for_delivery_token(blocks, code)
                candidates.append({
                    "value": code,
                    "confidence": 0.99,
                    "reason": "order_suffix_corroborated_in_delivery_block",
                    "evidence": _block_evidence(block, "corroborated_customer_agency_code_v1"),
                    "corroborating_evidence": [corroboration] if corroboration else [],
                })

    # Some customer systems encode a numeric branch/site at the beginning of
    # the order identifier and print it as a separate item in the delivery zone.
    # Require all three independent layout signals: the order prefix, an exact
    # isolated block, and a nearby delivery heading. This also handles codes
    # that the address parser correctly kept out of postal ``raw_lines``.
    prefix = re.match(r"^(\d{2,7})(?=[A-Z])", folded_number)
    if prefix:
        code = prefix.group(1)
        order_blocks = [
            block for block in blocks
            if _ORDER_LABEL_RE.search(_fold(block.get("text")))
            and folded_number in _fold(block.get("text"))
        ]
        code_blocks = [block for block in blocks if _same_code(block.get("text"), code)]
        postcode_conflict = code in _delivery_postal_codes(po)
        if (
            order_blocks and not postcode_conflict
            and _valid_code(code, corroborated_numeric=True)
        ):
            for code_block in code_blocks:
                heading = _near_delivery_heading(blocks, code_block)
                if not heading:
                    continue
                candidates.append({
                    "value": code,
                    "confidence": 0.99,
                    "reason": "order_prefix_corroborated_in_delivery_zone",
                    "evidence": _block_evidence(
                        code_block, "corroborated_numeric_customer_agency_code_v1"
                    ),
                    "corroborating_evidence": [
                        _block_evidence(order_blocks[0], "customer_agency_order_prefix_v1"),
                        _block_evidence(heading, "customer_agency_delivery_zone_v1"),
                    ],
                })
                break

    # In a common two-column order layout, the five-digit delivery agency is
    # printed as an isolated token below ``Adresse de livraison``.  It may be
    # different from the five-digit order prefix: one identifies the emitting
    # entity, the other the destination agency.  Promote the destination only
    # when the order has the structured ``12345CA...`` shape, the token is a
    # right-column block near the delivery heading, and it is not the postal
    # code.  These layout constraints avoid guessing from arbitrary numbers.
    structured_numeric_order = re.match(r"^\d{5}CA[A-Z0-9]{6,}$", folded_number)
    if structured_numeric_order:
        order_blocks = [
            block for block in blocks
            if _ORDER_LABEL_RE.search(_fold(block.get("text")))
            and folded_number in _fold(block.get("text"))
        ]
        postcodes = _delivery_postal_codes(po)
        for code_block in blocks:
            code = _code(code_block.get("text"))
            if (
                not code
                or not re.fullmatch(r"\d{5}", code)
                or code in postcodes
                or not _valid_code(code, corroborated_numeric=True)
            ):
                continue
            heading = _near_delivery_heading(blocks, code_block)
            if not heading:
                continue
            _, code_bbox = _source_page_bbox(code_block)
            _, heading_bbox = _source_page_bbox(heading)
            if not (
                isinstance(code_bbox, (list, tuple)) and len(code_bbox) == 4
                and isinstance(heading_bbox, (list, tuple)) and len(heading_bbox) == 4
                and float(code_bbox[0]) >= float(heading_bbox[0]) + 100
            ):
                continue
            candidates.append({
                "value": code,
                "confidence": 0.989,
                "reason": "isolated_numeric_agency_in_delivery_zone",
                "evidence": _block_evidence(
                    code_block, "isolated_numeric_delivery_agency_code_v2"
                ),
                "corroborating_evidence": [
                    _block_evidence(order_blocks[0], "structured_customer_order_v2")
                    if order_blocks else None,
                    _block_evidence(heading, "customer_agency_delivery_zone_v2"),
                ],
            })
            break
    return candidates


def _agency_email_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Recover an explicit agency code carried by an agency mailbox.

    This remains template-independent: the local part must literally start
    with ``agence-``, ``agence_`` or ``agence.``.  A line break immediately
    after the separator is tolerated because narrow PDF columns commonly wrap
    ``agence-ppc.toulouse@...`` at that exact position.
    """
    candidates: list[dict[str, Any]] = []
    for page_number, page in enumerate(payload.get("pages") or [], 1):
        if not isinstance(page, dict):
            continue
        source = str(page.get("text") or "")
        joined = re.sub(r"(?<=[._-])\s*\n\s*", "", source)
        for match in re.finditer(
            r"\bAGENCE[._-](?P<code>[A-Z0-9]+(?:[._-][A-Z0-9]+){0,2})@",
            joined,
            flags=re.I,
        ):
            code = _code(match.group("code"))
            if not code or not _valid_code(code, explicit=True):
                continue
            candidates.append({
                "value": code,
                "confidence": 0.994,
                "reason": "explicit_agency_mailbox_code",
                "evidence": {
                    "page": int(page.get("page") or page_number),
                    "source_text": match.group(0).rstrip("@"),
                    "extraction_method": "explicit_agency_mailbox_code_v3",
                },
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
    candidates.extend(_agency_email_candidates(payload))
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

"""Conservative promotion of strong PDF-geometry evidence into business output.

The CPU field router deliberately emits suggestions instead of silently
overwriting the core extraction.  This module is the narrow reconciliation
layer between both representations.  It promotes only explicit order labels
and unambiguous delivery blocks, preserves superseded values, and records every
action for auditability.
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import re
import unicodedata
from typing import Any

from jin_runtime.output_quality import _find_purchase_order


ORDER_TYPES = {"purchase_order", "purchase_order_with_terms"}
DELIVERY_ROLES = {"ship_to", "deliver_to", "consignee"}
SITE_FIELDS = {"street", "street_name", "industrial_zone", "business_park", "lieu_dit", "building"}


def _norm(value: Any) -> str:
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    ).upper()
    return re.sub(r"[^A-Z0-9]+", " ", text).strip()


def _compact(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", _norm(value))


def _value(field: Any) -> Any:
    return field.get("value") if isinstance(field, dict) else field


def _address(block: dict[str, Any]) -> dict[str, Any]:
    nested = block.get("address")
    return nested if isinstance(nested, dict) else block


def _street(components: dict[str, Any]) -> str:
    explicit = components.get("street")
    if explicit:
        return _norm(explicit)
    return _norm(" ".join(
        str(value)
        for value in (components.get("street_type"), components.get("street_name"))
        if value
    ))


def _city_tokens(value: Any) -> set[str]:
    aliases = {"ST": "SAINT", "STE": "SAINTE"}
    return {aliases.get(token, token) for token in _norm(value).split() if token}


def _component_count(components: dict[str, Any]) -> int:
    # Count semantic dimensions, not aliases. ``street`` and ``street_name``
    # describe one fact; counting both could hide a geometry-only site line.
    return sum((
        bool(components.get("house_number")),
        bool(_street(components)),
        bool(components.get("industrial_zone")),
        bool(components.get("business_park")),
        bool(components.get("lieu_dit")),
        bool(components.get("building")),
        bool(components.get("postal_code")),
        bool(components.get("city")),
        bool(components.get("po_box")),
        bool(components.get("cs")),
        bool(components.get("tsa")),
    ))


def _complete_delivery(candidate: dict[str, Any]) -> bool:
    components = candidate.get("components") or {}
    return bool(
        components.get("postal_code")
        and components.get("city")
        and (
            any(components.get(key) for key in SITE_FIELDS)
            or candidate.get("party_name")
        )
        and float(candidate.get("confidence") or 0.0) >= 0.97
    )


def _address_equivalent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_postal = _compact(left.get("postal_code"))
    right_postal = _compact(right.get("postal_code"))
    left_city = _norm(left.get("city"))
    right_city = _norm(right.get("city"))
    if left_postal and right_postal and left_postal != right_postal:
        return False
    if (
        left_city and right_city and left_city != right_city
        and _city_tokens(left_city) != _city_tokens(right_city)
    ):
        return False
    left_street, right_street = _street(left), _street(right)
    if left_street and right_street and left_street != right_street:
        return False
    return bool((left_postal and right_postal) or (left_street and right_street))


def _candidate_matches_block(candidate: dict[str, Any], block: dict[str, Any]) -> bool:
    components = candidate.get("components") or {}
    target = _address(block)
    evidence = 0
    party = _norm(candidate.get("party_name"))
    target_party = _norm(block.get("party_name"))
    if party and target_party and (party in target_party or target_party in party):
        evidence += 2
    house = _compact(components.get("house_number"))
    target_house = _compact(target.get("house_number") or target.get("building_number"))
    if house and target_house and house == target_house:
        evidence += 1
    street = _street(components)
    target_street = _street(target)
    if street and target_street and (
        street in target_street or target_street in street
        or set(street.split()) == set(target_street.split())
    ):
        evidence += 2
    postal = _compact(components.get("postal_code"))
    target_postal = _compact(target.get("postal_code"))
    if postal and target_postal and postal == target_postal:
        evidence += 1
    return evidence >= 2


def _matches_supplier(
    candidate: dict[str, Any],
    addresses: list[dict[str, Any]],
    geometry_candidates: list[dict[str, Any]] | None = None,
) -> bool:
    components = candidate.get("components") or {}
    candidate_party = _norm(candidate.get("party_name"))
    distinct_geometry_supplier = any(
        isinstance(other, dict)
        and str(other.get("role") or "").lower() == "supplier"
        and not _address_equivalent(components, other.get("components") or {})
        and bool((other.get("components") or {}).get("postal_code"))
        for other in geometry_candidates or []
    )
    for block in addresses:
        if str(block.get("role") or "").lower() != "supplier":
            continue
        supplier = _address(block)
        if not _address_equivalent(components, supplier):
            continue
        # A distinct supplier address found by geometry proves that this core
        # supplier role came from interleaved columns. Keep the explicit
        # ship-to block instead of vetoing it.
        if distinct_geometry_supplier:
            continue
        # A core extraction can confuse the delivery establishment with the
        # supplier when both sit in the same visual header.  An explicit
        # ship-to geometry block carrying the very same party name is stronger
        # role evidence.  Keep the veto when the party names differ: this is
        # the characteristic false-positive pattern on supplier letterheads.
        supplier_party = _norm(block.get("party_name"))
        if (
            candidate_party
            and supplier_party
            and (candidate_party in supplier_party or supplier_party in candidate_party)
        ):
            continue
        candidate_house = _compact(components.get("house_number"))
        supplier_house = _compact(supplier.get("house_number") or supplier.get("building_number"))
        if not candidate_house or not supplier_house or candidate_house == supplier_house:
            return True
    return False


def _candidate_key(candidate: dict[str, Any]) -> tuple[str, ...]:
    components = candidate.get("components") or {}
    return (
        _compact(components.get("postal_code")),
        " ".join(sorted(_city_tokens(components.get("city")))),
        _compact(components.get("house_number")),
        _street(components),
        _norm(components.get("industrial_zone") or components.get("building")),
    )


def _repeated_party_name(value: Any) -> bool:
    """Return true for flattened duplicate cells such as ``ISERBA ISERBA``."""
    words = _norm(value).split()
    middle = len(words) // 2
    return bool(len(words) >= 2 and len(words) % 2 == 0 and words[:middle] == words[middle:])


def _party_name_looks_like_address(value: Any) -> bool:
    folded = _norm(value)
    return bool(
        re.match(r"^(?:ZI|ZA|ZAC|ZAE|ZONE|PARC)\b", folded)
        or re.match(
            r"^(?:PAR MAIL|MERCI DE|VOTRE ACCUSE|CONTACT|LIVRAISON|ADRESSE)\b",
            folded,
        )
        or (
            re.search(r"\b\d{1,4}(?:\s+ET\s+\d{1,4})?\b", folded)
            and re.search(
                r"\b(?:RUE|ROUTE|AV|AVENUE|BOULEVARD|BD|CHEMIN|IMPASSE|ALLEE|PLACE|QUAI)\b",
                folded,
            )
        )
    )


def _source_candidate_crosses_address_columns(candidate: dict[str, Any]) -> bool:
    """Detect a linear-text address assembled from two visual columns.

    Routing numbers following BP/CS/TSA are ignored. Two remaining French
    locality postcodes in one candidate are strong evidence that billing and
    delivery columns were interleaved by the PDF text layer.
    """
    text = " | ".join(str(value) for value in (
        candidate.get("source_text"),
        candidate.get("formatted_address_suggestion"),
    ) if value)
    folded = _norm(text)
    folded = re.sub(r"\b(?:BP|CS|TSA)\s+\d{3,6}\b", " ", folded)
    postals = set(re.findall(
        r"(?<!\d)(?:0[1-9]|[1-8]\d|9[0-8])\d{3}(?!\d)",
        folded,
    ))
    return len(postals) > 1


def _merge_multiline_delivery_candidates(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge a numbered street and its continuation road at one delivery site."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for candidate in candidates:
        components = candidate.get("components") or {}
        key = (
            _norm(candidate.get("party_name")),
            _compact(components.get("postal_code")),
            _norm(components.get("city")),
        )
        grouped.setdefault(key, []).append(candidate)

    merged: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for group in grouped.values():
        if len(group) != 2:
            continue
        numbered = [item for item in group if (item.get("components") or {}).get("house_number")]
        continuations = [item for item in group if not (item.get("components") or {}).get("house_number")]
        kilometre_lines = [
            item for item in numbered
            if _norm(item.get("department")) in {"KM", "PK"}
        ]
        primary_lines = [item for item in numbered if item not in kilometre_lines]
        if (
            len(numbered) == 2
            and len(kilometre_lines) == 1
            and len(primary_lines) == 1
            and abs(
                int(kilometre_lines[0].get("source_line") or 0)
                - int(primary_lines[0].get("source_line") or 0)
            ) <= 2
        ):
            base, continuation = primary_lines[0], kilometre_lines[0]
            combined = copy.deepcopy(base)
            combined_components = combined.setdefault("components", {})
            continuation_components = continuation.get("components") or {}
            continuation_street = " ".join(
                str(value)
                for value in (
                    continuation.get("department"),
                    continuation_components.get("house_number"),
                    continuation_components.get("house_number_suffix"),
                    continuation_components.get("street_type"),
                    continuation_components.get("street_name"),
                )
                if value
            ).strip()
            combined_components["address_complement"] = continuation_street
            primary_street = " ".join(
                str(value)
                for value in (
                    combined_components.get("house_number"),
                    combined_components.get("house_number_suffix"),
                    combined_components.get("street_type"),
                    combined_components.get("street_name"),
                )
                if value
            ).strip() or str(combined_components.get("street") or "").strip()
            locality = " ".join(
                str(value)
                for value in (
                    combined_components.get("postal_code"),
                    combined_components.get("city"),
                )
                if value
            ).strip()
            combined["formatted_address_suggestion"] = ", ".join(
                value for value in (primary_street, continuation_street, locality) if value
            )
            combined["merged_source_lines"] = [
                base.get("source_line"), continuation.get("source_line")
            ]
            merged.append(combined)
            consumed.update({id(base), id(continuation)})
            continue
        if len(numbered) != 1 or len(continuations) != 1:
            continue
        base, continuation = numbered[0], continuations[0]
        base_components = base.get("components") or {}
        continuation_components = continuation.get("components") or {}
        continuation_street = " ".join(
            str(value)
            for value in (
                continuation_components.get("street_type"),
                continuation_components.get("street_name"),
            )
            if value
        ).strip() or str(continuation_components.get("street") or "").strip()
        if not continuation_street or _street(base_components) == _street(continuation_components):
            continue
        combined = copy.deepcopy(base)
        combined_components = combined.setdefault("components", {})
        combined_components["address_complement"] = continuation_street
        primary_street = " ".join(
            str(value)
            for value in (
                combined_components.get("house_number"),
                combined_components.get("street_type"),
                combined_components.get("street_name"),
            )
            if value
        ).strip() or str(combined_components.get("street") or "").strip()
        locality = " ".join(
            str(value)
            for value in (
                combined_components.get("postal_code"),
                combined_components.get("city"),
            )
            if value
        ).strip()
        combined["formatted_address_suggestion"] = ", ".join(
            value for value in (primary_street, continuation_street, locality) if value
        )
        combined["merged_source_lines"] = [
            base.get("source_line"), continuation.get("source_line")
        ]
        merged.append(combined)
        consumed.update({id(base), id(continuation)})
    return [item for item in candidates if id(item) not in consumed] + merged


def _enrich_equivalent_candidate(
    candidate: dict[str, Any],
    all_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep the explicit role but use a richer duplicate address block.

    Some templates repeat the delivery address below the header with a
    building or lot complement.  Geometry correctly reads both copies, while
    only the header copy carries the ship-to role.  Equivalent postal/street
    content makes the merge deterministic.
    """
    def same_site_street_extension(left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_postal = _compact(left.get("postal_code"))
        right_postal = _compact(right.get("postal_code"))
        left_city = _norm(left.get("city"))
        right_city = _norm(right.get("city"))
        left_house = _compact(left.get("house_number"))
        right_house = _compact(right.get("house_number"))
        left_street = _street(left)
        right_street = _street(right)
        return bool(
            left_postal and left_postal == right_postal
            and left_city and left_city == right_city
            and left_house and left_house == right_house
            and left_street and right_street
            and (
                left_street.startswith(right_street + " ")
                or right_street.startswith(left_street + " ")
            )
        )

    related = [candidate]
    components = candidate.get("components") or {}
    for item in all_candidates:
        if item is candidate or not isinstance(item, dict):
            continue
        if str(item.get("role") or "").lower() not in {"ship_to", "unknown", "supplier"}:
            continue
        # Linear source text can interleave two columns while retaining the
        # correct delivery postal code.  Never use such a block to enrich a
        # clean geometric candidate: its street/complement carries the other
        # party's address even though the site key still matches.
        if _source_candidate_crosses_address_columns(item):
            continue
        other = item.get("components") or {}
        if _address_equivalent(components, other) or same_site_street_extension(
            components, other
        ):
            related.append(item)
    if len(related) == 1:
        return candidate

    enriched = copy.deepcopy(candidate)
    enriched_components = enriched.setdefault("components", {})
    for item in related[1:]:
        base_line = candidate.get("source_line")
        item_line = item.get("source_line")
        nearby_copy = bool(
            base_line is None
            or item_line is None
            or abs(int(base_line) - int(item_line)) <= 6
        )
        for key, value in (item.get("components") or {}).items():
            if (
                key in {
                    "building", "industrial_zone", "business_park",
                    "lieu_dit", "address_complement",
                }
                and not nearby_copy
            ):
                # Same street/postcode can occur once as the buyer address and
                # later as a shorter explicit delivery block. Do not import
                # site complements from that distant copy.
                continue
            if value and not enriched_components.get(key):
                enriched_components[key] = value
    def clean_street_extension(item: dict[str, Any]) -> bool:
        other_street = _street(item.get("components") or {})
        base_street = _street(components)
        if not other_street or not base_street or len(other_street) <= len(base_street):
            return True
        if not other_street.startswith(base_street + " "):
            return True
        suffix = other_street[len(base_street):].strip()
        return not re.search(
            r"\b\d{1,4}(?:\s*(?:BIS|TER|QUATER))?\s+"
            r"(?:RUE|ROUTE|AVENUE|AV|BOULEVARD|BD|CHEMIN|IMPASSE|ALLEE|PLACE|QUAI)\b",
            suffix,
        )

    street_sources = [item for item in related if clean_street_extension(item)]
    fullest_street = max(
        street_sources or [candidate],
        key=lambda item: len(_street(item.get("components") or {}).split()),
    ).get("components") or {}
    if len(_street(fullest_street).split()) > len(_street(enriched_components).split()):
        for key in ("street", "street_type", "street_name"):
            if fullest_street.get(key):
                enriched_components[key] = fullest_street[key]

    equivalent_cities = [
        str((item.get("components") or {}).get("city") or "").strip()
        for item in related
        if (item.get("components") or {}).get("city")
        and _city_tokens((item.get("components") or {}).get("city"))
        == _city_tokens(enriched_components.get("city"))
    ]
    if equivalent_cities:
        # Prefer a normal locality order over a visually interleaved variant
        # such as ``DE SANGONIS ST ANDRE``. Hyphenated official forms are also
        # more precise while preserving exactly the same locality tokens.
        enriched_components["city"] = max(
            equivalent_cities,
            key=lambda value: (
                not _norm(value).startswith(("DE ", "DU ", "DES ")),
                "-" in value,
                len(value),
            ),
        )

    parts = []
    for value in (enriched.get("department"), enriched_components.get("building")):
        if value and _norm(value) not in {_norm(existing) for existing in parts}:
            parts.append(str(value))
    street = " ".join(
        str(value)
        for value in (
            enriched_components.get("house_number"),
            enriched_components.get("house_number_suffix"),
            enriched_components.get("street_type"),
            enriched_components.get("street_name"),
        )
        if value
    ).strip() or str(enriched_components.get("street") or "").strip()
    if street:
        parts.append(street)
    for key in (
        "industrial_zone", "business_park", "lieu_dit", "address_complement",
        "bp", "cs", "tsa",
    ):
        value = enriched_components.get(key)
        if value and _norm(value) not in {_norm(existing) for existing in parts}:
            parts.append(str(value))
    locality = " ".join(
        str(value)
        for value in (
            enriched_components.get("postal_code"), enriched_components.get("city")
        )
        if value
    ).strip()
    if locality:
        parts.append(locality)
    enriched["formatted_address_suggestion"] = ", ".join(parts)
    enriched["role"] = "ship_to"
    enriched["party_name"] = candidate.get("party_name")
    enriched["confidence"] = max(
        float(item.get("confidence") or 0.0) for item in related
    )
    enriched["role_source_line"] = candidate.get("source_line")
    return enriched


def _field_from_anchor(candidate: dict[str, Any]) -> dict[str, Any]:
    value = str(candidate.get("value") or "").strip()
    return {
        "value": value,
        "raw_value": value,
        "normalized_value": value,
        "ocr_confidence": candidate.get("confidence"),
        "semantic_confidence": candidate.get("confidence"),
        "validation_confidence": 1.0,
        "final_confidence": candidate.get("confidence"),
        "validation_status": "SOURCE_SUPPORTED",
        "warnings": ["promoted_from_explicit_geometry_anchor"],
        "evidence": {
            "page": candidate.get("page"),
            "bbox": candidate.get("bbox"),
            "source_text": candidate.get("source_text") or value,
            "extraction_method": candidate.get("source") or "geometry_anchor",
        },
    }


def _date_field_from_anchor(candidate: dict[str, Any]) -> dict[str, Any] | None:
    raw = str(candidate.get("value") or "").strip()
    parsed = None
    for date_format in (
        "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y",
        "%d-%m-%y", "%d.%m.%Y", "%d.%m.%y",
    ):
        try:
            parsed = datetime.strptime(raw, date_format)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    field = _field_from_anchor(candidate)
    field.update({
        "value": parsed.date().isoformat(),
        "raw_value": raw,
        "normalized_value": parsed.date().isoformat(),
    })
    return field


def _plausible_order_candidate(candidate: dict[str, Any]) -> bool:
    value = str(candidate.get("value") or "").strip()
    compact = _compact(value)
    if len(compact) < 5 or sum(char.isdigit() for char in compact) < 4:
        return False
    if re.fullmatch(r"(?:\d{1,2}[/.-]){2}\d{2,4}", value):
        return False
    normalized = _norm(value)
    if re.search(r"\b(?:RUE|AVENUE|BOULEVARD|ROUTE|CHEMIN|IMPASSE|CEDEX)\b", normalized):
        return False
    if re.search(
        r"\b(?:JANVIER|FEVRIER|MARS|AVRIL|MAI|JUIN|JUILLET|AOUT|SEPTEMBRE|"
        r"OCTOBRE|NOVEMBRE|DECEMBRE)\b",
        normalized,
    ) and re.search(r"\b(?:LE|DU|AU|REPRISE|FERMETURE)\b", normalized):
        return False
    if len(re.findall(r"[A-ZÀ-Ý]{2,}", normalized)) >= 4:
        return False
    return True


def _source_labels_contact_number(payload: dict[str, Any], value: Any) -> bool:
    """Return true when the current numeric candidate follows TEL/FAX in source."""
    digits = re.sub(r"\D", "", str(value or ""))
    if not (8 <= len(digits) <= 15):
        return False
    raw_text = str(payload.get("raw_text") or "")
    if not raw_text:
        raw_text = "\n".join(
            str(page.get("text") or "")
            for page in payload.get("pages") or []
            if isinstance(page, dict)
        )
    for line in raw_text.splitlines():
        folded_line = _norm(line)
        label = re.search(
            r"\b(?:TEL(?:EPHONE)?|TELECOPIE|FAX)\b",
            folded_line,
        )
        if label and digits in re.sub(r"\D", "", folded_line[label.start():]):
            return True
    return False


def _explicit_source_delivery_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Recover a fully printed address after an explicit delivery reminder.

    This is intentionally narrower than the geometry parser.  It is used for
    noisy scans where the main OCR blocks contain a clean repeated delivery
    sentence but the lower-resolution field-router pass damages one or two
    digits.  No address is inferred: every promoted component must be printed
    immediately after an explicit delivery-address label.
    """
    raw_text = str(payload.get("raw_text") or "")
    if not raw_text.strip():
        # The public extraction payload exposes the OCR text page by page.  A
        # top-level ``raw_text`` is present in unit-level/core payloads, but is
        # deliberately omitted from some API responses.  Rebuild exactly the
        # same line stream here so source reconciliation also runs in the real
        # Docker/API path.
        pages = payload.get("pages") or []
        if not pages:
            purchase_order = _find_purchase_order(payload)
            pages = purchase_order.get("pages") or [] if purchase_order else []
        raw_text = "\n".join(
            str(page.get("text") or "")
            for page in pages
            if isinstance(page, dict) and page.get("text")
        )
    lines = [" ".join(line.split()) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]
    street_types = (
        r"RUE|ROUTE|AVENUE|AV|BOULEVARD|BD|CHEMIN|IMPASSE|ALLEE|ALL[ÉE]E|"
        r"DEPARTEMENTALE|D[ÉE]PARTEMENTALE|PLACE|QUAI|COURS|PASSAGE|VOIE|"
        r"MONTEE|MONT[ÉE]E|ROND[ -]?POINT"
    )
    postal_code = r"(?:\d{5}|\d{2}\s+\d{3})"
    pattern = re.compile(
        rf"^(?P<party>.*?[A-Za-zÀ-ÿ])\s+"
        rf"(?P<house>\d{{1,4}}(?:\s*(?:BIS|TER|QUATER)|\s+ET\s+\d{{1,4}})?)\s+"
        rf"(?P<street_type>{street_types})\s+"
        rf"(?P<street_name>.+?)\s+"
        rf"(?P<postal>(?:F(?:R)?[- ]?)?{postal_code})\s+"
        rf"(?P<city>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’ .-]{{1,50}}?)(?:\s+FRANCE)?$",
        flags=re.I,
    )
    street_line_pattern = re.compile(
        rf"^(?:(?P<house>\d{{1,4}}(?:\s*[,/-]\s*\d{{1,4}})?"
        rf"(?:\s*(?:BIS|TER|QUATER)|\s+ET\s+\d{{1,4}})?)\s*[,;]?\s+)?"
        rf"(?P<street_type>{street_types})\s+(?P<street_name>.+)$",
        flags=re.I,
    )
    locality_pattern = re.compile(
        rf"^(?:F(?:R)?[- ]?)?(?P<postal>{postal_code})\s+(?P<city>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’ .-]{{1,60}})$",
        flags=re.I,
    )

    def candidate(party: str, components: dict[str, Any], source: str, line_index: int):
        street = " ".join(str(components.get(key) or "") for key in (
            "house_number", "street_type", "street_name"
        )).strip()
        parts = [street]
        for key in ("industrial_zone", "business_park", "building", "address_complement"):
            if components.get(key):
                parts.append(str(components[key]))
        parts.append(f"{components['postal_code']} {components['city']}")
        return {
            "role": "ship_to",
            "party_name": party,
            "components": components,
            "formatted_address_suggestion": ", ".join(value for value in parts if value),
            "confidence": 0.995,
            "page": 1,
            "source_line": line_index,
            "source": "explicit_source_delivery_reminder_v1",
            "source_text": source,
            "requires_review": True,
        }

    found: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        label = _norm(line)
        if not (
            "ADRESSE DE LIVRAISON EST" in label
            or "A LIVRER A L ADRESSE CI DESSOUS" in label
            or re.search(r"\b(?:ADRESSE|HDRESSE|DRESSE) (?:DE )?LIVRAISON\b", label)
            or "LIEU DE LIVRAISON" in label
        ):
            continue
        label_party_match = re.search(
            r"\bLIEU\s+DE\s+LIVRAISON\s*:?[ ]*(?P<party>.+?)\s*$",
            line,
            flags=re.I,
        )
        label_party = (
            label_party_match.group("party").strip(" ,;:-")
            if label_party_match else ""
        )
        following = []
        for value in lines[index + 1:index + 9]:
            if re.match(
                r"^(?:TEL|TELEPHONE|FAX|EMAIL|REGLEMENT|ATTENTION|MODE D EXPEDITION|"
                r"DESIGNATION|CODE\s+REFERENCE|CODE\b|MONTANT\b)",
                _norm(value),
            ):
                break
            cleaned = re.split(r"\bHEURE\s+DE\s+RECEPTION\b", value, maxsplit=1, flags=re.I)[0]
            cleaned = re.split(
                r"\s+(?:T[^\s:]{0,5}L(?:[^\s:]*)?|FAX)\s*:",
                cleaned,
                maxsplit=1,
                flags=re.I,
            )[0]
            cleaned = re.sub(
                r"\s+PAGE(?:\s+\d+(?:\s*/\s*\d+)?)?\s*$",
                "",
                cleaned,
                flags=re.I,
            )
            cleaned = re.sub(
                r"(?:\s+\d{1,2}\s*[Hh]\s*\d{2}){1,4}\s*$",
                "",
                cleaned,
            ).strip(" ,;:-")
            if cleaned and _norm(cleaned) not in {
                "VIREMENT", "CHEQUE", "CB", "CARTE BANCAIRE",
            }:
                following.append(cleaned)
        labelled_one_line_pattern = re.compile(
            rf"^(?:(?P<industrial>Z\.?\s*[IAE]\.?|ZAC|ZAE)\s+)?"
            rf"(?P<house>\d{{1,4}}(?:\s*(?:BIS|TER|QUATER)|\s+ET\s+\d{{1,4}})?)\s+"
            rf"(?P<street_type>{street_types})\s+(?P<street_name>.+?)\s+"
            rf"(?P<postal>{postal_code})\s+"
            rf"(?P<city>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’ .-]{{1,60}}?)(?:\s+FRANCE)?$",
            flags=re.I,
        )
        if label_party:
            labelled_site = next(
                (match for value in following if (match := labelled_one_line_pattern.match(value))),
                None,
            )
            if labelled_site:
                components = {
                    "house_number": labelled_site.group("house").strip(),
                    "street_type": labelled_site.group("street_type").strip(),
                    "street_name": labelled_site.group("street_name").strip(" ,;:-"),
                    "postal_code": re.sub(r"\D", "", labelled_site.group("postal")),
                    "city": labelled_site.group("city").strip(" ,;:-"),
                }
                if labelled_site.group("industrial"):
                    components["industrial_zone"] = labelled_site.group("industrial").strip()
                source = " | ".join([line] + following)
                found.append(candidate(label_party, components, source, index + 1))
                continue
        attempts = list(following)
        if len(following) == 2:
            attempts.append(" ".join(following))
        matched = False
        for source in attempts:
            match = pattern.match(source.strip(" ,;:-"))
            if not match:
                continue
            party = match.group("party").strip(" ,;:-")
            if not party or len(party) > 100:
                continue
            postal = re.sub(r"\D", "", match.group("postal"))
            components = {
                "house_number": match.group("house").strip(),
                "street_type": match.group("street_type").strip(),
                "street_name": match.group("street_name").strip(" ,;:-"),
                "postal_code": postal,
                "city": match.group("city").strip(" ,;:-"),
            }
            found.append(candidate(party, components, source, index + 1))
            matched = True
            break
        if matched:
            continue

        # Split-column ERP scans can interleave left-side labels with the
        # delivery block.  Recover only components printed in the short window
        # following the explicit delivery heading.  Example:
        # ``Type livraison. Livre GERONDEAU`` / ``... 2123 ROUTE NATIONALE`` /
        # ``Devise EUR 45774 SARAN CEDEX FRANCE``.
        inline_street_pattern = re.compile(
            rf"(?<!\d)(?P<house>\d{{1,4}}(?:\s*(?:BIS|TER|QUATER)|\s+ET\s+\d{{1,4}})?)\s+"
            rf"(?P<street_type>{street_types})\s+(?P<street_name>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'\u2019 .-]{{1,80}})$",
            flags=re.I,
        )
        inline_locality_pattern = re.compile(
            rf"(?<!\d)(?P<postal>{postal_code})\s+"
            rf"(?P<city>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'\u2019 .-]{{1,60}}?)(?:\s+FRANCE)?$",
            flags=re.I,
        )
        inline_street = next(
            (match for value in following if (match := inline_street_pattern.search(value))),
            None,
        )
        inline_locality = next(
            (match for value in following if (match := inline_locality_pattern.search(value))),
            None,
        )
        inline_party = next(
            (
                match.group("party").strip(" ,;:-")
                for value in following
                if (match := re.search(
                    r"\bLIVRE\s+(?P<party>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 &'\u2019.-]{1,80})$",
                    value,
                    flags=re.I,
                ))
            ),
            None,
        )
        if inline_street and inline_locality and inline_party:
            components = {
                "house_number": inline_street.group("house").strip(),
                "street_type": inline_street.group("street_type").strip(),
                "street_name": inline_street.group("street_name").strip(" ,;:-"),
                "postal_code": re.sub(r"\D", "", inline_locality.group("postal")),
                "city": inline_locality.group("city").strip(" ,;:-"),
            }
            source = " | ".join(following)
            found.append(candidate(inline_party, components, source, index + 1))
            continue

        block = [
            value for value in following
            if _norm(value) not in {"FR", "FRANCE"}
        ]
        locality_index = next(
            (position for position, value in enumerate(block) if locality_pattern.match(value)),
            None,
        )
        if locality_index is None:
            continue
        locality = locality_pattern.match(block[locality_index])
        street_index = next(
            (
                position for position, value in enumerate(block[:locality_index])
                if street_line_pattern.match(value.strip(" ,;:-"))
            ),
            None,
        )
        if street_index is None or locality is None:
            continue
        street_match = street_line_pattern.match(block[street_index].strip(" ,;:-"))
        party_lines = []
        prefix_extras = []
        for value in block[:street_index]:
            folded = _norm(value)
            if re.match(r"^(?:ZI|ZA|ZAC|ZAE|ZONE|PARC|TECHNIPARC|TECHNOPARC)\b", folded):
                prefix_extras.append(value)
            else:
                party_lines.append(value)
        if (
            len(party_lines) >= 2
            and all(len(_compact(value)) <= 6 for value in party_lines[1:])
        ):
            # A short depot code such as ``CPS`` is a separate line, not part
            # of the printed company name.
            party = party_lines[0].strip(" ,;:-")
        else:
            party = " ".join(party_lines).strip(" ,;:-")
        if not street_match or not party or len(party) > 100:
            continue
        components = {
            "street_type": street_match.group("street_type").strip(),
            "street_name": street_match.group("street_name").strip(" ,;:-"),
            "postal_code": re.sub(r"\D", "", locality.group("postal")),
            "city": locality.group("city").strip(" ,;:-"),
        }
        if street_match.group("house"):
            components["house_number"] = street_match.group("house").strip(" ,;:-")
        extras = prefix_extras + block[street_index + 1:locality_index]
        for extra in extras:
            folded = _norm(extra)
            if folded == _norm(locality.group("city")):
                continue
            routing_zone = re.match(
                r"^B\.?\s*P\.?\s*(?P<bp>\d+)\s+"
                r"(?P<zone>Z\.?\s*I\.?\s+.+?)(?:\s+VIREMENT)?$",
                extra,
                flags=re.I,
            )
            if routing_zone:
                components["po_box"] = f"BP {routing_zone.group('bp')}"
                components["industrial_zone"] = routing_zone.group("zone").strip()
                continue
            if re.match(r"^(?:ZI|ZA|ZAC|ZAE|ZONE)\b", folded):
                components["industrial_zone"] = extra
            elif re.match(r"^(?:PARC|TECHNIPARC|TECHNOPARC)\b", folded):
                components["business_park"] = extra
            elif re.match(r"^(?:BAT|BATIMENT|IMMEUBLE|RESIDENCE)\b", folded):
                components["building"] = extra
            else:
                components["address_complement"] = extra
        source = " | ".join(block[:locality_index + 1])
        found.append(candidate(party, components, source, index + 1))
    return found


def _address_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    components = copy.deepcopy(candidate.get("components") or {})
    if components.get("street_name") and not components.get("street"):
        components["street"] = " ".join(
            str(value)
            for value in (components.get("street_type"), components.get("street_name"))
            if value
        )
    components.setdefault("country", "France")
    components.setdefault("country_code", "FR")
    formatted = str(candidate.get("formatted_address_suggestion") or "").strip()
    digest = hashlib.sha1(
        (formatted + "|" + str(candidate.get("page") or "")).encode("utf-8")
    ).hexdigest()[:12]
    party_name = candidate.get("party_name")
    department = candidate.get("department")
    if _party_name_looks_like_address(party_name) and department:
        party_name, department = department, None
    return {
        "address_id": f"addr_geometry_{digest}",
        "role": "ship_to",
        "role_label": "Adresse de livraison",
        "party_name": party_name,
        "department": department,
        "address": components,
        "formatted_address": formatted,
        "role_confidence": min(float(candidate.get("confidence") or 0.97), 0.98),
        "address_confidence": float(candidate.get("confidence") or 0.97),
        "confidence": float(candidate.get("confidence") or 0.97),
        "validation_status": "SOURCE_SUPPORTED_REVIEW_REQUIRED",
        "is_verified_real_address": False,
        "evidence": {
            "page": candidate.get("page"),
            "source_text": formatted,
            "extraction_method": "geometry_ship_to_reconciliation_v1",
            "zone_id": candidate.get("zone_id"),
            "source_line": candidate.get("source_line"),
        },
        "warnings": ["promoted_from_geometry_suggestion", "requires_human_review"],
    }


def _clone_as_delivery(block: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    cloned = copy.deepcopy(block)
    original_role = cloned.get("role")
    cloned["address_id"] = f"{cloned.get('address_id') or 'address'}:geometry-ship-to"
    cloned["role"] = "ship_to"
    cloned["role_label"] = "Adresse de livraison"
    cloned["role_confidence"] = min(float(hint.get("confidence") or 0.97), 0.98)
    cloned["validation_status"] = "SOURCE_SUPPORTED_REVIEW_REQUIRED"
    cloned.setdefault("warnings", []).extend([
        f"role_promoted_from_{original_role or 'unknown'}",
        "corroborated_by_ship_to_geometry",
        "requires_human_review",
    ])
    cloned["evidence"] = {
        "page": hint.get("page"),
        "source_text": hint.get("formatted_address_suggestion"),
        "extraction_method": "geometry_role_reconciliation_v1",
        "zone_id": hint.get("zone_id"),
        "source_line": hint.get("source_line"),
    }
    return cloned


def _supersede_delivery(addresses: list[dict[str, Any]], promoted: dict[str, Any]) -> None:
    for block in addresses:
        if str(block.get("role") or "").lower() not in DELIVERY_ROLES:
            continue
        block["role_original"] = block.get("role")
        block["role"] = "unknown"
        block["role_label_original"] = block.get("role_label")
        block["role_label"] = "Superseded delivery candidate"
        block.setdefault("warnings", []).append("superseded_by_geometry_delivery_candidate")
    addresses.append(promoted)


def _core_ship_is_suspicious(block: dict[str, Any]) -> bool:
    address = _address(block)
    text = " | ".join(str(value) for value in (
        address.get("raw"), block.get("formatted_address"), address.get("address_complement")
    ) if value)
    postals = set(re.findall(r"(?<!\d)(?:0[1-9]|[1-8]\d|9[0-8])\d{3}(?!\d)", text))
    folded = _norm(text)
    complement = _norm(address.get("address_complement"))
    product_complement = bool(
        "N INTERNE" in complement
        or (
            re.search(r"\b\d{8,14}\b", complement)
            and any(marker in complement for marker in (
                "POMPE", "CHAUDIERE", "BRULEUR", "ARTICLE", "PORT", "PRIX",
            ))
        )
    )
    return bool(
        len(postals) > 1
        or _repeated_party_name(block.get("party_name"))
        or product_complement
        or any(marker in folded for marker in (
            "CONDITIONS DE LIVRAISON", "CONDITIONS DE PAIEMENT", "SIEGE SOCIAL",
            "PAS DE LIVRAISON", "LIVRE FORFAIT", "ADHERENT ALGOREL",
        ))
    )


def _ensure_order_container(payload: dict[str, Any], candidate: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    po = _find_purchase_order(payload)
    if po:
        return po
    document = payload.get("document") or {}
    current_type = document.get("primary_document_type") or document.get("detected_document_type")
    if current_type in ORDER_TYPES:
        return {}
    page_classifications = document.get("page_classifications") or []
    first = next((item for item in page_classifications if item.get("page") == 1), {})
    first_signals = " ".join(str(item) for item in first.get("signals") or [])
    order_signal_count = sum(
        marker in _norm(first_signals)
        for marker in ("COMMANDE", "ITEM LIKE IDENTIFIERS", "N COMMANDE")
    )
    if (
        int(candidate.get("page") or 0) != 1
        or float(candidate.get("confidence") or 0.0) < 0.99
        or order_signal_count < 2
    ):
        return {}

    business = payload.setdefault("business_extractions", {})
    po = {
        "purchase_order": {},
        "business_addresses": [],
        "lines": [],
        "totals": {},
        "requires_human_review": True,
        "extraction_source": "runtime_geometry_reconciliation_v1",
    }
    business["purchase_order"] = po
    document["primary_document_type_original"] = current_type
    new_type = "purchase_order_with_terms" if current_type == "legal_terms" else "purchase_order"
    document["primary_document_type"] = new_type
    document["detected_document_type"] = new_type
    document["classification_reconciliation"] = {
        "source": "explicit_page_1_order_geometry",
        "requires_review": True,
    }
    actions.append({
        "action": "promote_document_type",
        "from": current_type,
        "to": new_type,
        "reason": "explicit_page_1_order_number_and_order_signals",
    })
    return po


def reconcile_weak_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Reconcile explicit geometry evidence and record every promoted value."""
    suggestions = payload.get("weak_field_suggestions") or {}
    anchored = suggestions.get("anchored_fields") or {}
    actions: list[dict[str, Any]] = []
    order_candidate = anchored.get("order_number")
    date_candidate = anchored.get("order_date")

    po = _find_purchase_order(payload)
    if not po and isinstance(order_candidate, dict) and _plausible_order_candidate(order_candidate):
        po = _ensure_order_container(payload, order_candidate, actions)

    if (
        po
        and isinstance(order_candidate, dict)
        and order_candidate.get("value")
        and _plausible_order_candidate(order_candidate)
    ):
        header = po.get("purchase_order")
        if not isinstance(header, dict):
            header = po
        current = header.get("number") or header.get("order_number")
        current_value = _value(current)
        candidate_value = order_candidate.get("value")
        explicit = str(order_candidate.get("source") or "").startswith(
            "geometry_explicit_order_"
        )
        current_evidence = current.get("evidence") if isinstance(current, dict) else {}
        if not isinstance(current_evidence, dict):
            current_evidence = {}
        current_method = str(
            current_evidence.get("extraction_method")
            or (current.get("extraction_method") if isinstance(current, dict) else "")
            or ""
        )
        strong_core_methods = {
            "explicit_attached_numero_below_order_title",
            "explicit_order_label",
            "explicit_composite_erp_order_id",
            "explicit_n_de_commande",
            "inline_transaction_header",
            "spatial_order_number",
            "native_table_anchor",
            "multiblock_order_number",
            "po_number_composite_duplicate_cleanup_v60",
            "explicit_reference_commande_column_v60",
            "explicit_date_piece_order_column_v60",
        }
        current_is_contact = _source_labels_contact_number(payload, current_value)
        current_compact = _compact(current_value)
        candidate_compact = _compact(candidate_value)
        truncated_geometry_prefix = bool(
            current_compact
            and candidate_compact
            and current_compact.startswith(candidate_compact)
            and len(candidate_compact) < len(current_compact)
        )
        override_multiblock_ocr = bool(
            explicit
            and current_method == "multiblock_order_number"
            and not truncated_geometry_prefix
            and float(order_candidate.get("confidence") or 0.0) >= 0.995
        )
        internal_value = _value(header.get("internal_number"))
        geometry_matches_header_identity = bool(
            explicit
            and current_method == "explicit_n_de_commande"
            and internal_value
            and _compact(internal_value) == candidate_compact
            and current_compact != candidate_compact
            and float(order_candidate.get("confidence") or 0.0) >= 0.995
        )
        if not current_value or (
            explicit and _compact(current_value) != _compact(candidate_value)
            and (
                current_method not in strong_core_methods
                or current_is_contact
                or override_multiblock_ocr
                or geometry_matches_header_identity
            )
        ):
            if current_value:
                header["number_original"] = copy.deepcopy(current)
                if current_is_contact and isinstance(header["number_original"], dict):
                    header["number_original"]["disqualified_reason"] = (
                        "SOURCE_LABELED_CONTACT_NUMBER"
                    )
                elif override_multiblock_ocr and isinstance(header["number_original"], dict):
                    header["number_original"]["disqualified_reason"] = (
                        "LOWER_PRECISION_MULTIBLOCK_OCR"
                    )
                elif geometry_matches_header_identity and isinstance(header["number_original"], dict):
                    header["number_original"]["disqualified_reason"] = (
                        "LATER_BODY_ORDER_LABEL_CONFLICTS_WITH_HEADER"
                    )
            header["number"] = _field_from_anchor(order_candidate)
            actions.append({
                "action": "promote_order_number",
                "from": current_value,
                "to": candidate_value,
                "reason": "explicit_geometry_order_label" if explicit else "missing_core_value",
            })

    if (
        po
        and isinstance(order_candidate, dict)
        and str(order_candidate.get("source") or "").startswith(
            "geometry_explicit_order_metadata_"
        )
        and isinstance(date_candidate, dict)
        and date_candidate.get("value")
        and float(date_candidate.get("confidence") or 0.0) >= 0.99
        and int(date_candidate.get("page") or 0) == int(order_candidate.get("page") or 0)
    ):
        header = po.get("purchase_order")
        if not isinstance(header, dict):
            header = po
        current_date = header.get("order_date")
        if not _value(current_date):
            promoted_date = _date_field_from_anchor(date_candidate)
            if promoted_date:
                header["order_date"] = promoted_date
                actions.append({
                    "action": "promote_order_date",
                    "from": None,
                    "to": promoted_date["value"],
                    "reason": "same_explicit_geometry_order_metadata_table",
                })

    if po:
        addresses = po.setdefault("business_addresses", [])
        geometry_candidates = [
            item for item in suggestions.get("address_candidates") or []
            if isinstance(item, dict)
        ]
        source_candidates = _explicit_source_delivery_candidates(payload)
        if source_candidates:
            suggestions["source_text_address_candidates"] = source_candidates
        # Explicit repeated source text is the tie-breaker for OCR-heavy PDFs:
        # it must win over a geometrically equivalent candidate whose party or
        # digits were damaged by the lower-resolution field-router pass.
        all_address_candidates = source_candidates + geometry_candidates
        # A complete block printed immediately below an explicit delivery
        # label is stronger role evidence than later geometry.  On some dense
        # templates the geometric zone continues into legal footer addresses,
        # which otherwise creates several plausible ``ship_to`` keys and makes
        # reconciliation abandon the correct explicit block as ambiguous.
        clean_source_candidates = [
            item for item in source_candidates
            if not _source_candidate_crosses_address_columns(item)
        ]
        complete_geometry_candidates = [
            item for item in geometry_candidates
            if str(item.get("role") or "").lower() == "ship_to"
            and _complete_delivery(item)
        ]
        if clean_source_candidates:
            role_candidates = clean_source_candidates
        elif complete_geometry_candidates:
            role_candidates = geometry_candidates
        else:
            role_candidates = source_candidates or all_address_candidates
        candidates = [
            item for item in role_candidates
            if isinstance(item, dict) and str(item.get("role") or "").lower() == "ship_to"
        ]
        candidates = _merge_multiline_delivery_candidates(candidates)
        complete: dict[tuple[str, ...], dict[str, Any]] = {}
        for candidate in candidates:
            if _complete_delivery(candidate) and not _matches_supplier(
                candidate, addresses, geometry_candidates
            ):
                complete.setdefault(_candidate_key(candidate), candidate)

        promoted = None
        reason = None
        if len(complete) == 1:
            candidate = next(iter(complete.values()))
            candidate = _enrich_equivalent_candidate(candidate, all_address_candidates)
            existing_ship = next((
                block for block in addresses
                if str(block.get("role") or "").lower() in DELIVERY_ROLES
            ), None)
            if existing_ship:
                existing_components = _address(existing_ship)
                candidate_components = candidate.get("components") or {}
                equivalent = _address_equivalent(existing_components, candidate_components)
                existing_house = _compact(
                    existing_components.get("house_number")
                    or existing_components.get("building_number")
                )
                candidate_house = _compact(candidate_components.get("house_number"))
                explicit_house_disagrees = bool(
                    existing_house
                    and candidate_house
                    and existing_house != candidate_house
                )
                if (
                    equivalent
                    and not _core_ship_is_suspicious(existing_ship)
                    and _component_count(existing_components) >= _component_count(candidate_components)
                    and not explicit_house_disagrees
                ):
                    candidate = None
            if candidate:
                promoted = _address_from_candidate(candidate)
                if existing_ship and _address_equivalent(
                    _address(existing_ship), candidate.get("components") or {}
                ):
                    # Geometry owns the clean address components, while the
                    # core role parser can still own a more precise recipient
                    # or site contact (for example a named installer). Do not
                    # preserve a duplicated party created by flattened cells.
                    existing_party = existing_ship.get("party_name")
                    if (
                        existing_party
                        and not _repeated_party_name(existing_party)
                        and not _party_name_looks_like_address(existing_party)
                    ):
                        promoted["party_name"] = existing_party
                    for key in ("department", "contact_name", "contact_phone", "contact_email"):
                        value = existing_ship.get(key)
                        if (
                            value
                            and not promoted.get(key)
                            and not (
                                key == "department"
                                and _norm(value) == _norm(promoted.get("party_name"))
                            )
                        ):
                            promoted[key] = value
                reason = "unique_complete_ship_to_geometry"
        elif not complete and candidates:
            matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for hint in candidates:
                for block in addresses:
                    role = str(block.get("role") or "").lower()
                    if role in {"supplier", *DELIVERY_ROLES}:
                        continue
                    if _candidate_matches_block(hint, block):
                        matches.append((hint, block))
            unique = {
                str(block.get("address_id") or id(block)): (hint, block)
                for hint, block in matches
            }
            if len(unique) == 1:
                hint, block = next(iter(unique.values()))
                promoted = _clone_as_delivery(block, hint)
                reason = "ship_to_geometry_matches_single_core_party"

        if promoted:
            previous = [
                block.get("formatted_address")
                for block in addresses
                if str(block.get("role") or "").lower() in DELIVERY_ROLES
            ]
            _supersede_delivery(addresses, promoted)
            actions.append({
                "action": "promote_delivery_address",
                "from": previous,
                "to": promoted.get("formatted_address"),
                "reason": reason,
            })

    payload["weak_field_reconciliation"] = {
        "version": "geometry-core-reconciliation-v1",
        "actions": actions,
        "requires_review": bool(actions),
    }
    return payload

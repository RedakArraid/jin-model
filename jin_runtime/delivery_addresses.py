"""Deterministic, source-preserving cleanup for delivery addresses."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from jin_runtime.output_quality import _find_purchase_order


DELIVERY_ROLES = {"ship_to", "deliver_to", "consignee"}
ROUTING_FIELDS = (("po_box", "BP"), ("postal_box", "BP"), ("tsa", "TSA"), ("cs", "CS"))
STREET_TYPE_ALIASES = {
    "R": "RUE", "AV": "AVENUE", "AVENUE": "AVENUE", "BD": "BOULEVARD", "BOULEVARD": "BOULEVARD",
    "RTE": "ROUTE", "ROUTE": "ROUTE", "CHE": "CHEMIN", "CHEM": "CHEMIN",
    "CHEMIN": "CHEMIN", "RUE": "RUE", "IMP": "IMPASSE", "IMPASSE": "IMPASSE",
    "ALL": "ALLEE", "ALLEE": "ALLEE", "QUAI": "QUAI", "TRAVERSE": "TRAVERSE",
}


def _text(value: Any) -> str | None:
    if value in (None, "", False):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip(" \t,;|")
    return text or None


def _norm(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


def _address(block: dict[str, Any]) -> dict[str, Any]:
    value = block.get("address")
    return value if isinstance(value, dict) else block


def _prefixed(value: Any, prefix: str) -> str | None:
    text = _text(value)
    if not text:
        return None
    normalized = _norm(text)
    compact_prefix = prefix.replace(" ", "")
    if normalized == prefix or normalized.startswith(prefix + " "):
        return text
    if normalized.replace(" ", "").startswith(compact_prefix):
        suffix = re.sub(r"^[A-Za-z.\s]+", "", text).strip()
        return f"{prefix} {suffix}".strip()
    return f"{prefix} {text}"


def _city_and_cedex(address: dict[str, Any]) -> tuple[str | None, bool, str | None]:
    city = _text(address.get("city") or address.get("commune") or address.get("ville"))
    if city:
        city = re.sub(r"^[^\w]+", "", city, flags=re.UNICODE).strip() or None
        city = re.sub(r"\s*-\s*", "-", city).strip() or None
    cedex = bool(address.get("cedex"))
    cedex_number = _text(address.get("cedex_number") or address.get("cedex_no"))
    if city:
        match = re.match(r"^(.*?)\s+CEDEX(?:\s+(\d{1,2}))?$", city, flags=re.I)
        if match:
            city = _text(match.group(1))
            cedex = True
            cedex_number = cedex_number or _text(match.group(2))
        # A country printed in the neighboring address column is sometimes
        # appended to the OCR city (``SARAN FRANCE CEDEX``). Preserve genuine
        # municipalities such as TREMBLAY EN FRANCE / ROISSY-EN-FRANCE.
        if city and re.search(r"\s+FRANCE$", city, flags=re.I) and not re.search(
            r"(?:\bEN\s+FRANCE|-EN-FRANCE)$", city,
            flags=re.I,
        ):
            city = _text(re.sub(r"\s+FRANCE$", "", city, flags=re.I))
    return city, cedex, cedex_number


def _source_street(address: dict[str, Any]) -> str | None:
    explicit = _text(address.get("street"))
    street_type = _text(address.get("street_type"))
    street_name = _text(address.get("street_name"))
    if street_name and street_type and not _norm(street_name).startswith(_norm(street_type) + " "):
        return f"{street_type} {street_name}"
    return street_name or explicit


def _street_signature(value: Any) -> tuple[str, str]:
    tokens = _norm(value).split()
    if not tokens:
        return "", ""
    street_type = STREET_TYPE_ALIASES.get(tokens[0], "")
    return street_type, " ".join(tokens[1:] if street_type else tokens)


def _clean_street_ocr(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    # French apostrophes are occasionally exposed as a glyph resembling ``3``
    # or ``4`` by embedded PDF fonts. Restrict the repair to D3/D4 immediately
    # followed by a letter, so genuine road designations such as "D4 Ozenay"
    # remain untouched.
    text = re.sub(r"\b[Dd][34](?=[A-Za-zÀ-ÿ])", "d'", text)
    text = re.sub(r"\bLIMIERE\b", "LUMIERE", text, flags=re.I)
    # Payment terms live in the neighboring column on several ERP layouts and
    # can be appended to the last postal token (``... BP 5151 VIREMENT``).
    # They are never part of a thoroughfare.
    text = re.sub(
        r"\s+(?:VIREMENTS?|CHEQUE|PAIEMENT|REGLEMENT)\s*$",
        "",
        text,
        flags=re.I,
    )
    # A standalone trailing zone abbreviation is not part of the street name.
    text = re.sub(r"\s*[- ]+(?:ZI|ZA|ZAC|ZAE)[- ]*$", "", text, flags=re.I)
    return _text(text)


def _preferred_street(address: dict[str, Any], matched: dict[str, Any]) -> str | None:
    source = _source_street(address)
    canonical = _text(matched.get("street"))
    if source and canonical:
        source_type, source_name = _street_signature(source)
        canonical_type, canonical_name = _street_signature(canonical)
        if (
            source_name
            and source_name == canonical_name
            and source_type
            and canonical_type
            and source_type != canonical_type
        ):
            # BAN remains the verifier, but it must not replace an explicitly
            # printed AVENUE by RUE (or the reverse) in the delivered label.
            return _clean_street_ocr(source)
    preferred = _clean_street_ocr(canonical or source)
    if preferred and _norm(preferred) in STREET_TYPE_ALIASES:
        # A lone ``RUE``/``AVENUE`` is a parsing remnant, not a deliverable
        # thoroughfare. Keep the surrounding zone/site instead.
        return None
    return preferred


def _street_line(address: dict[str, Any], matched: dict[str, Any]) -> str | None:
    house = _text(matched.get("house_number") or address.get("house_number")
                  or address.get("building_number"))
    suffix = _text(address.get("house_number_suffix"))
    street = _preferred_street(address, matched)
    if not street:
        fallback = _text(address.get("line1"))
        if fallback and re.match(r"^\d{5}\b", fallback):
            # A locality-only line (``83000 Toulon``) cannot stand in for a
            # street and must not prevent recovery from another table column.
            return None
        if fallback and any(
            _norm(fallback) == _norm(address.get(key))
            for key in (
                "industrial_zone", "business_park", "activity_park", "lieu_dit",
                "address_complement", "complement", "building", "residence",
            )
            if address.get(key)
        ):
            return None
        return None if fallback and _norm(fallback) in STREET_TYPE_ALIASES else fallback
    number = " ".join(item for item in (house, suffix) if item)
    if number and not re.match(rf"^{re.escape(number)}(?:\s|,|$)", street, flags=re.I):
        return f"{number} {street}"
    return street


def _append(lines: list[str], value: Any) -> None:
    text = _text(value)
    if not text:
        return
    normalized = _norm(text)
    if not normalized or any(normalized == _norm(existing) for existing in lines):
        return
    lines.append(text)


def _clean_address_complement(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    if re.match(r"^(?:cli|client|code\s+client)\s*[:#]", folded):
        return None
    if folded in {"virement", "cheque", "carte bancaire", "cb"}:
        return None
    if re.search(
        r"\b(?:contact|correspondant|a l'attention|en express|merci de|code d.?ouverture|"
        r"code porte|code cadenas|digicode|horaires?|reception|adherent|portable|conditions? liv|"
        r"livraison de preference|"
        r"livrer de preference|livraison le matin|pas de livraison|instruction de livraison)\b",
        folded,
    ):
        return None
    if _norm(text) in {"MISSING", "UNKNOWN", "INCONNU", "N A", "NA"}:
        return None
    text = re.sub(r"^\((.*)\)$", r"\1", text).strip()
    text = re.sub(r"\b[Rr]oind\s+[Pp]oint\b", "rond-point", text)
    return text


def _without_duplicate_routing(value: Any, routing: Iterable[tuple[str, Any]]) -> str | None:
    """Remove BP/CS/TSA already exposed as a structured routing component."""
    text = _text(value)
    if not text:
        return None
    for prefix, structured in routing:
        digits = re.sub(r"\D", "", str(structured or ""))
        if not digits:
            continue
        letters = r"B\.?\s*P\.?" if prefix == "BP" else re.escape(prefix)
        text = re.sub(
            rf"(?:\s*[-,/|]\s*)?\b{letters}\s*{re.escape(digits)}\b",
            "",
            text,
            flags=re.I,
        )
    return _text(text.strip(" -/,;|"))


def _clean_site_component(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    if re.fullmatch(r"-\s*(?:ZI|ZA|ZAC|ZAE)\s*-?", text, flags=re.I):
        return None
    text = re.split(
        r"\s*\|\s*(?:LIVRAISON|MERCI\s+DE\s+LIVRER|A\s+LIVRER|INSTRUCTION)\b",
        text,
        maxsplit=1,
        flags=re.I,
    )[0]
    # Common OCR confusion on French activity-zone abbreviations: a printed
    # ``Z.A.D.`` is sometimes read as ``2.A.D.``. Keep this deliberately
    # restricted to the beginning of a site component.
    text = re.sub(
        r"^2\s*\.\s*A\s*\.\s*D\s*\.?(?=\s|$)",
        "Z.A.D.",
        text,
        flags=re.I,
    )
    text = re.sub(r"\b[Zz][|1]\s*", "ZI ", text)
    text = re.split(r"\s*\|\s*(?:U|QTE|QUANTIT[ÉE])\b", text, maxsplit=1, flags=re.I)[0]
    text = re.sub(r"\s*\|\s*(?:FR|FRANCE)\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*[-/]+\s*$", "", text)
    return _text(text)


def _site_norm(value: Any) -> str:
    """Normalize a site fragment for safe cross-field de-duplication."""
    normalized = _norm(value)
    normalized = re.sub(r"\bROIND\s+POINT\b", "ROND POINT", normalized)
    return " ".join(STREET_TYPE_ALIASES.get(token, token) for token in normalized.split())


def _company_site_norm(value: Any) -> str:
    """Normalize harmless company/site aliases used on adjacent label lines."""
    tokens = []
    for token in _norm(value).split():
        if token == "DR":
            # ``DR`` (direction régionale) is commonly present on only one of
            # two otherwise identical PROLIANS recipient lines.
            continue
        tokens.append({"ST": "SAINT", "STE": "SAINTE"}.get(token, token))
    return " ".join(tokens)


def _deduplicate_concatenated_company_site(value: Any) -> str | None:
    """Split two OCR-joined aliases of the same site and keep the first."""
    text = _text(value)
    if not text:
        return None
    first_token = text.split()[0]
    repeated = re.search(
        rf"\s+(?={re.escape(first_token)}\b)", text, flags=re.I
    )
    if not repeated:
        return text
    first, second = text[:repeated.start()].strip(), text[repeated.end():].strip()
    if _company_site_norm(first) == _company_site_norm(second):
        return first
    return text


def _redundant_compound_department(
        department: Any, site_components: Iterable[Any]) -> bool:
    """Detect a department field made only from already structured site data.

    Some geometry reconciliation preserves a slash-joined site description as
    ``department`` while also exposing both halves as ``industrial_zone`` and
    ``address_complement``.  Requiring at least two slash-separated fragments
    prevents a real department or recipient line from being discarded.
    """
    text = _text(department)
    sites = [_site_norm(value) for value in site_components if _text(value)]
    if not text or len(sites) < 2:
        return False
    fragments = [
        _site_norm(fragment.strip(" ()"))
        for fragment in re.split(r"\s*/\s*", text)
        if _site_norm(fragment.strip(" ()"))
    ]
    return len(fragments) >= 2 and all(fragment in sites for fragment in fragments)


def _clean_delivery_party(value: Any) -> str | None:
    text = _text(value)
    if not text or len(text) > 100 or not any(char.isalpha() for char in text):
        return None
    text = re.sub(r"^(?:CONTACT\s+CODE)\s+", "", text, flags=re.I).strip()
    folded = _norm(text)
    if folded in {
        "FR", "FRANCE", "DE LIVRAISON", "ADRESSE DE LIVRAISON",
        "ADRESSE LIVRAISON", "LIEU DE LIVRAISON", "SA", "SAS", "SASU",
        "SARL", "EURL",
    } or re.fullmatch(r"[A-Z]?DRESSE DE LIVRAISON", folded):
        return None
    if re.match(r"^(?:TELEPHONE|TEL|FAX|TELECOPIE|EMAIL|MAIL)\b", folded):
        return None
    if any(marker in folded for marker in (
        " AU CAPITAL ", " RCS ", " TVA ", " CONDITIONS ", " ADRESSE DE LIVRAISON ",
        " FOURNISSEUR ", " MODE D EXPEDITION ", " VEUILLEZ NOUS AVISER ",
        " TOUTE EXPEDITION ", " FACTURE EN ", " CATALOGUE ", " PLAN DE VENTE ",
        " PROCHAINS MOIS ", " HTTP ", " WWW ", "@",
    )):
        return None
    delivery_wish_sentence = bool(
        folded.startswith("LIVRAISON")
        and "SOUHAIT" in folded
        and "SOCI" in folded
    )
    if folded.startswith(("A FACTURER", "A LIVRER")) or delivery_wish_sentence or len(text.split()) > 10:
        return None
    return text


def _looks_like_street(value: Any) -> bool:
    return bool(re.match(
        r"^\d+[A-Z]?\s+(?:RUE|R|AVENUE|AV|ROUTE|RTE|BOULEVARD|BD|CHEMIN|IMPASSE|ALLEE|QUAI)\b",
        _norm(value),
    ))


def clean_delivery_address(block: dict[str, Any]) -> dict[str, Any]:
    """Build a clean postal label without destroying extracted source values."""
    address = _address(block)
    agency_field = block.get("customer_agency_code")
    agency_code = _text(
        agency_field.get("value") if isinstance(agency_field, dict) else agency_field
    )
    verification = block.get("ban_verification") or block.get("address_verification") or {}
    verified = verification.get("status") in {"EXACT_MATCH", "CANONICAL_MATCH"}
    matched = verification.get("matched_components") if verified else {}
    matched = matched if isinstance(matched, dict) else {}

    city, cedex, cedex_number = _city_and_cedex(address)
    if verified:
        city = _text(matched.get("city")) or city
    postal_code = _text(matched.get("postal_code") if verified else None) or _text(
        address.get("postal_code") or address.get("zip_code")
    )
    country = _text(address.get("country") or address.get("country_name"))
    country_code = _text(address.get("country_code"))
    industrial_zone = _clean_site_component(address.get("industrial_zone"))
    address_complement = _clean_address_complement(address.get("address_complement") or address.get("complement"))
    if address_complement and re.match(r"^Z[|1]\s*", address_complement, flags=re.I):
        address_complement = _clean_site_component(address_complement)
    country_fragments = {_norm(value) for value in (country, country_code) if value}
    if industrial_zone and _norm(industrial_zone) in country_fragments:
        industrial_zone = None
    if address_complement and _norm(address_complement) in country_fragments:
        address_complement = None
    if not industrial_zone and address_complement and re.match(
            r"^(?:ZONE|ZAC|ZAE|ZA|ZI|PARC\s+D['’ ]?ACTIVIT[ÉE]S?)\b",
            address_complement, flags=re.I):
        industrial_zone, address_complement = _clean_site_component(address_complement), None

    raw_source = " | ".join(
        str(value)
        for value in (
            address.get("raw"),
            block.get("formatted_address"),
            (block.get("evidence") or {}).get("source_text"),
        )
        if value
    )
    postal_routing_code = _text(address.get("postal_routing_code"))
    inferred_po_box = None
    if postal_routing_code and re.search(
        rf"\bB\.?\s*P\.?\s*{re.escape(postal_routing_code)}\b",
        raw_source,
        flags=re.I,
    ):
        inferred_po_box = _prefixed(postal_routing_code, "BP")
        postal_routing_code = None

    raw_party_name = _text(block.get("party_name"))
    embedded_site = None
    if raw_party_name:
        site_match = re.search(
            r"\b(?P<site>(?:Z\.?\s*[IAE]\.?|ZAC|ZAE|ZONE)\s+.+?)\s*$",
            raw_party_name,
            flags=re.I,
        )
        if site_match:
            embedded_site = _clean_site_component(site_match.group("site"))
        # An adjacent VAT row may be fused between the recipient and its site.
        # Preserve the proven company prefix and recover the postal zone
        # separately instead of publishing the complete noisy OCR sentence.
        raw_party_name = re.split(
            r"\s+N\s*(?:[Â°º�]|O)?\s*TVA\b|\s+TVA\s+(?:EUROPE|INTRA)?\b",
            raw_party_name,
            maxsplit=1,
            flags=re.I,
        )[0].strip(" ,;|:-")
    recipient = _deduplicate_concatenated_company_site(
        _clean_delivery_party(raw_party_name)
    )
    street = _preferred_street(address, matched if verified else {})
    source_street_line = _street_line(address, matched if verified else {})
    if not industrial_zone and embedded_site:
        industrial_zone = embedded_site
    routing_match = re.search(
        r"\s+B\.?\s*P\.?\s*(?P<number>\d{2,})\s*$",
        source_street_line or "",
        flags=re.I,
    )
    if routing_match and not any(
        address.get(key) for key in ("po_box", "postal_box", "bp")
    ):
        inferred_po_box = f"BP {routing_match.group('number')}"
        source_street_line = _text(
            (source_street_line or "")[:routing_match.start()]
        )
        if street:
            street = _text(re.sub(
                r"\s+B\.?\s*P\.?\s*\d{2,}\s*$",
                "",
                street,
                flags=re.I,
            ))
    if recipient and _looks_like_street(recipient) and (
        not source_street_line or _norm(source_street_line) == _norm(recipient)
    ):
        # Some table layouts put the complete street in the recipient column.
        # Promote it to the postal street field instead of displaying it as a
        # company name (for example ``205, Av General Pruneau``).
        street = _clean_street_ocr(recipient)
        source_street_line = street
        recipient = None

    department = _clean_delivery_party(block.get("department"))
    if department and re.match(r"^2\s*\.\s*A\s*\.\s*D\b", department, flags=re.I):
        department = _clean_site_component(department)
    if recipient and department and _norm(recipient).startswith("CHEZ "):
        # A care-of line is routing information; the adjacent company remains
        # the business recipient and must be shown first in the postal label.
        recipient, department = department, recipient
    if (
        recipient
        and department
        and "/" not in department
        and not re.search(
            r"\b(?:RUE|AVENUE|AV|ROUTE|RTE|BOULEVARD|BD|CHEMIN|IMPASSE|QUAI|ALLEE)\b",
            department,
            flags=re.I,
        )
        and _company_site_norm(recipient) != _company_site_norm(department)
        and _company_site_norm(department).startswith(
            _company_site_norm(recipient) + " "
        )
    ):
        # Prefer the complete site name rather than publishing both a short
        # recipient and its longer duplicate (``REXEL CHAMPIGNY`` followed by
        # ``REXEL CHAMPIGNY SUR MARNE``).
        recipient, department = department, None

    components: dict[str, Any] = {
        "recipient": recipient,
        "department": department,
        "building": _text(address.get("building")),
        "residence": _text(address.get("residence")),
        "house_number": _text(matched.get("house_number") if verified else None)
                        or _text(address.get("house_number") or address.get("building_number")),
        "house_number_suffix": _text(address.get("house_number_suffix")),
        "street": street,
        "industrial_zone": industrial_zone,
        "business_park": _clean_site_component(address.get("business_park") or address.get("activity_park")),
        "lieu_dit": _text(address.get("lieu_dit") or address.get("place_name")),
        "address_complement": address_complement,
        "po_box": _prefixed(address.get("po_box") or address.get("postal_box") or address.get("bp"), "BP")
                  or inferred_po_box,
        "tsa": _prefixed(address.get("tsa"), "TSA"),
        "cs": _prefixed(address.get("cs"), "CS"),
        "postal_routing_code": postal_routing_code,
        "postal_code": postal_code,
        "city": city,
        "cedex": cedex or None,
        "cedex_number": cedex_number,
        "country": country,
        "country_code": country_code,
    }
    components = {key: value for key, value in components.items() if value not in (None, "", False)}
    excluded_components: list[dict[str, str]] = []
    if agency_code:
        removed = False
        for key in (
            "department", "building", "residence", "industrial_zone", "business_park",
            "lieu_dit", "address_complement", "postal_routing_code",
        ):
            if components.get(key) and _norm(components[key]) == _norm(agency_code):
                components.pop(key, None)
                removed = True
        if removed:
            excluded_components.append({
                "type": "customer_agency_code",
                "value": agency_code,
                "reason": "non_postal_business_identifier",
            })
    if components.get("industrial_zone") and components.get("street"):
        combined_site = str(components["industrial_zone"])
        split_site_street = re.match(
            r"^(?P<site>(?:ZAC|ZAE|ZONE|Z\.?\s*[IAE]\.?)\b.+?)\s*[-–]\s*"
            r"(?P<street_fragment>.+)$",
            combined_site,
            flags=re.I,
        )
        if (
            split_site_street
            and source_street_line
            and len(_site_norm(split_site_street.group("street_fragment"))) >= 5
            and _site_norm(source_street_line).startswith(
                _site_norm(split_site_street.group("street_fragment"))
            )
        ):
            # The right half is a clipped duplicate of the independently
            # structured street: ``ZI LA PALUDS - 430 AV DE LA`` plus
            # ``430 AV DE LA PALUDS``.  Keep only the actual zone on this line.
            components["industrial_zone"] = split_site_street.group("site").strip()
            combined_site = str(components["industrial_zone"])
        combined_match = re.match(
            r"^(?P<zone>ZAC|ZAE|ZONE|Z\.?\s*[IAE]\.?(?:\s*[AE]\.?)?)\s+(?P<tail>.+)$",
            combined_site,
            flags=re.I,
        )
        if (
            combined_match
            and _site_norm(combined_match.group("tail"))
            == _site_norm(components["street"])
        ):
            components["industrial_zone"] = combined_match.group("zone")
        elif combined_match and source_street_line:
            # A two-column PDF can interleave ``ZI DU GROS HETRE`` with
            # ``76 BIS RUE ALTMAYER`` as ``ZI DU 76 BIS RUE ALTMAYER`` plus
            # ``RUE DU GROS HETRE``. Reassemble the site only when the zone
            # tail ends with the complete, independently structured street.
            tail = combined_match.group("tail")
            street_suffix = re.search(
                rf"(?P<lead>.*?)\s*{re.escape(source_street_line)}\s*$",
                tail,
                flags=re.I,
            )
            complement_site = re.match(
                r"^(?:RUE|ROUTE|AVENUE|BOULEVARD|CHEMIN|IMPASSE|ALLEE)\s+"
                r"(?P<lead>DU|DES|DE\s+LA|DE\s+L['â€™]?|DE)\s+"
                r"(?P<site>.+)$",
                str(components.get("address_complement") or ""),
                flags=re.I,
            )
            if (
                street_suffix
                and complement_site
                and _site_norm(street_suffix.group("lead"))
                == _site_norm(complement_site.group("lead"))
            ):
                components["industrial_zone"] = " ".join((
                    combined_match.group("zone"),
                    street_suffix.group("lead").strip(),
                    complement_site.group("site").strip(),
                ))
                components.pop("address_complement", None)
    if components.get("address_complement"):
        if components.get("industrial_zone"):
            zone_key = _site_norm(components["industrial_zone"])
            complement_key = _site_norm(components["address_complement"])
            trailing_instruction = complement_key[len(zone_key):].strip() \
                if complement_key.startswith(zone_key) else ""
            if trailing_instruction in {
                "VIREMENT", "CHEQUE", "PAGE", "REGLEMENT", "PAIEMENT",
            }:
                components.pop("address_complement", None)
        if not components.get("address_complement"):
            cleaned_complement = None
        else:
            cleaned_complement = _without_duplicate_routing(
                components["address_complement"],
                (
                    ("BP", components.get("po_box")),
                    ("TSA", components.get("tsa")),
                    ("CS", components.get("cs")),
                ),
            )
        if cleaned_complement:
            components["address_complement"] = cleaned_complement
        else:
            components.pop("address_complement", None)
    if (
        components.get("department")
        and components.get("city")
        and _norm(components["department"]) == _norm(components["city"])
    ):
        components.pop("department", None)
    if (
        components.get("recipient")
        and components.get("department")
        and _company_site_norm(components["recipient"])
        == _company_site_norm(components["department"])
    ):
        components.pop("department", None)
    if (
        components.get("address_complement")
        and components.get("city")
        and _norm(components["address_complement"]) == _norm(components["city"])
    ):
        components.pop("address_complement", None)
    if (
        components.get("department")
        and components.get("building")
        and _norm(components["department"]) == _norm(components["building"])
    ):
        components.pop("building", None)
    if (
        components.get("department")
        and components.get("industrial_zone")
        and (
            _site_norm(components["department"])
            == _site_norm(components["industrial_zone"])
            or (
                re.match(r"^(?:ZONE|ZAC|ZAE|ZA|ZI)\b", _norm(components["department"]))
                and _site_norm(components["industrial_zone"]).startswith(
                    _site_norm(components["department"]) + " "
                )
            )
        )
    ):
        components.pop("department", None)
    if components.get("building") and any(
        _norm(components["building"]) == _norm(components.get(key))
        for key in ("industrial_zone", "business_park", "lieu_dit", "address_complement")
        if components.get(key)
    ):
        components.pop("building", None)
    if _redundant_compound_department(
        components.get("department"),
        (
            components.get("recipient"),
            source_street_line,
            components.get("building"),
            components.get("residence"),
            components.get("industrial_zone"),
            components.get("business_park"),
            components.get("lieu_dit"),
            components.get("address_complement"),
        ),
    ):
        components.pop("department", None)
    if components.get("building") and any(
        _company_site_norm(components["building"])
        and _company_site_norm(components["building"])
        in _company_site_norm(components.get(key))
        for key in ("recipient", "department")
        if components.get(key)
    ):
        components.pop("building", None)

    lines: list[str] = []
    _append(lines, components.get("recipient"))
    for key in ("department", "building", "residence"):
        value = components.get(key)
        # A parser may expose ``DYNAMIKUM - BATIMENT B5`` as the department
        # and ``BATIMENT B5`` as a structured building component.  Both are
        # useful in JSON, but printing the contained fragment twice makes the
        # postal label noisy.
        if key in {"building", "residence"} and value and any(
            _norm(value) in _norm(existing) for existing in lines
        ):
            continue
        _append(lines, value)
    _append(lines, source_street_line)
    for key in ("industrial_zone", "business_park", "lieu_dit", "address_complement"):
        _append(lines, components.get(key))
    for key in ("po_box", "tsa", "cs", "postal_routing_code"):
        _append(lines, components.get(key))
    locality = " ".join(item for item in (postal_code, city) if item)
    if cedex:
        locality = f"{locality} CEDEX".strip()
        if cedex_number:
            locality = f"{locality} {cedex_number}"
    _append(lines, locality)
    _append(lines, components.get("country"))

    # Upper-case label lines are deterministic and comply with common French
    # postal-label practice; structured components retain their readable form.
    label_lines = [line.upper() for line in lines]
    warnings: list[str] = []
    if not postal_code or not city:
        warnings.append("DELIVERY_LOCALITY_INCOMPLETE")
    if not any(components.get(key) for key in ("street", "industrial_zone", "business_park", "lieu_dit")):
        warnings.append("DELIVERY_THOROUGHFARE_OR_SITE_MISSING")
    source_formatted = _text(block.get("formatted_address"))
    one_line = ", ".join(label_lines)
    status = "VERIFIED_CANONICAL" if verified else "STRUCTURED_UNVERIFIED"
    if warnings:
        status = "INCOMPLETE"
    return {
        "status": status,
        "one_line": one_line or source_formatted,
        "lines": label_lines,
        "components": components,
        "source_formatted": source_formatted,
        "reference_status": verification.get("status"),
        "warnings": warnings,
        "excluded_components": excluded_components,
    }


def _address_lists(value: Any) -> Iterable[list[dict[str, Any]]]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"business_addresses", "addresses"} and isinstance(child, list):
                yield [item for item in child if isinstance(item, dict)]
            yield from _address_lists(child)
    elif isinstance(value, list):
        for child in value:
            yield from _address_lists(child)


def enrich_delivery_addresses(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach the clean view to delivery blocks and expose selection metadata."""
    seen: set[int] = set()
    delivery_blocks: list[dict[str, Any]] = []
    for addresses in _address_lists(payload):
        for block in addresses:
            if id(block) in seen:
                continue
            seen.add(id(block))
            if str(block.get("role") or "").lower() not in DELIVERY_ROLES:
                continue
            clean = clean_delivery_address(block)
            block["clean_address"] = clean
            block["formatted_address_clean"] = clean.get("one_line")
            delivery_blocks.append(block)

    po = _find_purchase_order(payload)
    if po:
        # The verbose core response may expose copied address lists at several
        # levels. Selection must use only the canonical purchase-order list.
        primary = [block for block in po.get("business_addresses") or []
                   if isinstance(block, dict)
                   and str(block.get("role") or "").lower() == "ship_to"]
        po["delivery_address_selection"] = {
            "status": "SELECTED" if len(primary) == 1 else "MISSING" if not primary else "AMBIGUOUS",
            "candidate_count": len(primary),
            "selected_address_id": primary[0].get("address_id") if len(primary) == 1 else None,
        }
    return payload

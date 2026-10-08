"""Stable, consumer-oriented JSON view of the verbose core extraction."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any
import unicodedata

from jin_runtime import __version__
from jin_runtime.delivery_addresses import clean_delivery_address
from jin_runtime.output_quality import _find_purchase_order


ADDRESS_FIELDS = (
    "building", "building_number", "residence", "entrance", "floor", "unit",
    "house_number", "house_number_suffix", "street_type", "street_name", "street",
    "lieu_dit", "industrial_zone", "business_park", "address_complement",
    "po_box", "postal_box", "tsa", "cs", "postal_routing_code", "postal_code",
    "city", "cedex", "cedex_number", "district", "insee_code", "department",
    "region", "country", "country_code", "latitude", "longitude",
)
CONTACT_FIELDS = ("name", "firstname", "lastname", "department", "email", "phone", "fax")
PARTY_ROLES = (
    "buyer", "supplier", "sold_to", "bill_to", "ship_to", "deliver_to",
    "invoice_to", "payer", "end_customer", "consignee", "ship_from",
)
REFERENCE_FIELDS = (
    "material_number", "article_number", "supplier_material_number",
    "manufacturer_part_number", "customer_material_number", "sku", "ean", "gtin",
    "customer_order_number",
)


def _name_key(value: Any) -> str:
    text = "".join(
        char for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    ).upper()
    return re.sub(r"[^A-Z0-9]+", "", text)


def _deduplicate_ocr_tokens(value: Any) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "").replace("\ufffd", "")).strip(" ,;|")
    if not text:
        return None
    tokens = text.split()
    cleaned: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        token_key = _name_key(token)
        if (
            index + 1 < len(tokens)
            and token_key
            and token_key == _name_key(tokens[index + 1])
        ):
            cleaned.append(token)
            index += 2
            continue
        if token == "-" and cleaned and cleaned[-1] == "-":
            index += 1
            continue
        cleaned.append(token)
        index += 1
    return " ".join(cleaned).strip(" ,;|") or None


def _clean_party_name(value: Any) -> str | None:
    original = str(value or "").strip()
    if (
        not original
        or original.startswith("__")
        or re.match(r"^[:;]\s*", original)
        or re.match(r"^[.\-_*]{3,}", original)
        or re.match(r"^\+\s*\d+\b", original)
    ):
        return None
    # A narrow header column is often concatenated with the adjacent phone
    # column (and sometimes with the next party after a table separator).  The
    # mojibake-tolerant spelling also covers ``TÃ©l:`` without depending on the
    # OCR encoding used by the source PDF.
    original = re.split(
        r"\s+T[^\s:]{0,5}L(?:[^\s:]*)?\s*:",
        original,
        maxsplit=1,
        flags=re.I,
    )[0].strip(" ,;|:-")
    # ``Commandé par le dépôt`` values sometimes include the branch phone on
    # the same line (``ABBEVILLE / 03 22 24 81 17``).  Keep the branch name;
    # the phone remains available in the dedicated contact field.
    original = re.sub(
        r"\s*/\s*0\d(?:[ .-]?\d{2}){4}\s*$",
        "",
        original,
    ).strip(" ,;|:-")
    text = _deduplicate_ocr_tokens(original.replace("_", " "))
    if not text or len(text) > 120 or not any(char.isalpha() for char in text):
        return None
    # Adjacent PDF columns can append the first street line to the legal
    # company name (for example ``REXEL France 1ERE AVENUE``).  Preserve the
    # company prefix when the suffix is unambiguously a thoroughfare.
    company_then_street = re.match(
        r"^(?P<company>.+?\b(?:FRANCE|SASU?|SARL|EURL|HOLDING|GROUPE))\s+"
        r"(?:\d+(?:ER|E|ERE|EME)?\s+)?"
        r"(?:RUE|AVENUE|AV|ROUTE|RTE|BOULEVARD|BD|CHEMIN|IMPASSE|QUAI|ALLEE)\b.*$",
        text,
        flags=re.I,
    )
    if company_then_street:
        text = company_then_street.group("company").strip()
    folded = " ".join(
        "".join(
            char for char in unicodedata.normalize("NFKD", text)
            if not unicodedata.combining(char)
        ).upper().split()
    )
    compact = _name_key(text)
    if (
        folded in {
            "SA", "SAS", "SASU", "SARL", "EURL", "COMMANDE", "FOURNISSEUR",
            "FRANCE", "BELGIQUE", "SUISSE", "ESPAGNE", "ITALIE",
        }
        or len(compact) < 2
        or re.fullmatch(r"\d+\s+DE\s+\d+", folded)
        or re.match(r"^N(?:\s*[°º]|O|UMERO)?\s*\d*\s*[-:]", text, flags=re.I)
        or re.match(r"^(?:A\s+LIVRER|ADRESSE|LIEU)\b", folded)
        or " N DOSSIER " in f" {folded} "
        or "NDOSSIER" in compact
        or re.fullmatch(r"N\d{5,}", compact)
        or compact.startswith("PXUNITMONTANT")
        or compact.startswith("COMMANDEMAIL")
        or compact in {"COMMANDEN", "COMMANDENO", "NUMEROCOMMANDE"}
        or compact.startswith("DEEE")
        or "CONTRIBUTIONREP" in compact
        or "FORCEDESINDEPENDANTS" in compact
        or "LIVRAISONSOUHAITEE" in compact
        or compact.startswith("ACCUSEDERECEPTION")
        or compact.startswith("ENVOYEEPARMAIL")
        or compact.startswith("IMPERATIFA")
        or compact.startswith("REFERENCEANOUSRAPPELER")
        or (compact.startswith("REFERENCE") and "NCOMPTE" in compact)
        or folded.startswith(("RETOURNER ", "FOURNISSEUR "))
        or folded.startswith(("TEL ", "TEL:", "TELEPHONE ", "FAX ", "FAX:"))
        or folded.startswith("SIEGE SOCIAL ")
        or compact.startswith("PORTACHAT")
        or "FRAISDEPORT" in compact
        or compact.startswith("DATEPIECEFOURN")
        or compact.startswith("PIECEFOURNNO")
        or compact.startswith("EXEMPLAIREACHAT")
        or re.match(r"^B\.?\s*P\.?\s*\d+.*\bZ\.?\s*I\.?\b", folded)
        or sum(
            marker in compact
            for marker in ("CARRELAG", "SALLEDEBAIN", "CUISINES", "CHAUFFAGE", "CLIMATISATION")
        ) >= 3
        or (
            re.match(r"^\d{1,2}\s*H\s*\d{0,2}\b", folded)
            and re.search(r"\bRDV\b", folded)
        )
        or compact.startswith("DACTIVITESDE")
        or compact.startswith("CONTRATCADRE")
        or compact in {"DATENUMERO", "NUMERODATE"}
        or folded.startswith((
            "AVANT LE ", "AVANT LA ", "PAR COURRIER ", "COURRIER :",
        ))
        or compact.startswith("CLAPETANTIRETOUR")
        or re.match(r"^CLIENT\s+N(?:[Â°ÂºO]|UMERO)?\s*\d+", text, flags=re.I)
        or re.match(r"^[A-Z]{1,4}\.\.\.\s+", text, flags=re.I)
        or folded.startswith(("POIDS TOTAL", "TOTAL POIDS", "DOCUMENT CREE LE"))
        or " CAPITAL DE " in f" {folded} "
        or re.fullmatch(r"B\.?\s*P\.?\s*\d+", text, flags=re.I)
        or re.search(r"\b(?:B\.?\s*P\.?|CS|TSA)\s*\d{3,}\b", text, flags=re.I)
        or " SIRET" in f" {folded}"
        or (
            re.match(r"^\d", folded)
            and re.search(r"\b(?:PCE|PCS|PIECE|QUANTITE|QTE)\b", folded)
        )
        or re.match(r"^\d{6,}\s+", folded)
        or re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}\s+", folded)
        or re.match(r"^\d{3,}[A-Z]?\s+(?:PARC|ZONE|ZAC|ZAE|ZA|ZI)\b", folded)
        or (
            re.match(r"^\d{3,}\b", folded)
            and " BON " in f" {folded} "
            and " REF " in f" {folded} "
        )
        or re.match(
            r"^\d+[A-Z]?\s*[,.-]?\s*(?:RUE|AVENUE|AV|ROUTE|RTE|BOULEVARD|BD|CHEMIN|IMPASSE|QUAI|ALLEE)\b",
            folded,
        )
        or any(marker in folded for marker in (
            " AU CAPITAL ", " RCS ", " N TVA ", " TVA ",
            " FAX ", " PAGE ", " CONDITIONS GENERALES ",
        ))
    ):
        return None
    return text


def _clean_contact_name(value: Any) -> str | None:
    from_table = "|" in str(value or "")
    text = _deduplicate_ocr_tokens(value)
    if not text:
        return None
    if from_table:
        text = text.split("|", 1)[0].strip()
    text = re.sub(r"^[A-Z]{1,4}\.\.\.\s*", "", text, flags=re.I)
    text = re.split(
        r"\s+(?:FAX|T[ÉE]L(?:[ÉE]PHONE)?|PAGE|E-?MAIL|PRIX(?:\s+HT)?|"
        r"MONTANT|QUANTIT[ÉE]|QTE)\s*[:/]?",
        text,
        maxsplit=1,
        flags=re.I,
    )[0].strip(" ,;|:-")
    text = re.sub(r"^(?:CONTACT|ACHETEUR|SUIVI\s+PAR)\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"(?:\s+FRANCE)+$", "", text, flags=re.I).strip()
    if from_table:
        text = re.sub(r"\s+\d{1,2}$", "", text).strip()
    if (
        not text
        or len(text) > 80
        or _name_key(text) in {"RETOURA", "RETOUR", "CONTACT"}
        or re.fullmatch(
            r"PAGE\s*:?\s*\d+(?:\s*(?:/|DE)\s*\d+)?",
            text,
            flags=re.I,
        )
        or re.fullmatch(r"\d+\s+DE\s+\d+", text, flags=re.I)
        or re.fullmatch(r"ACHAT\s+\S+", text, flags=re.I)
        or any(marker in _name_key(text) for marker in (
            "CONTRIBUTIONREP", "ECOTAXE", "ECOCONTRIBUTION",
            "DATELIVRAISONSOUHAIT", "CONDITIONSFRANCO",
            "PORTACHAT", "FRAISDEPORT",
        ))
        or _name_key(text).startswith("DEEE")
    ):
        return None
    return text


def _clean_department(value: Any) -> str | None:
    """Keep short organisational labels, reject OCR prose and role captions."""
    text = _clean_party_name(value)
    if not text:
        return None
    key = _name_key(text)
    if (
        len(text.split()) > 10
        or (text.count("/") >= 2 and len(re.sub(r"\D", "", text)) >= 10)
        or key.startswith(("AFACTURER", "ALIVRER", "ADRESSEDELIVRAISON"))
        or any(marker in key for marker in (
            "CATALOGUE", "PLANDEVENTE", "PROCHAINSMOIS",
            "CONDITIONSGENERALES", "MODEDEXPEDITION",
        ))
    ):
        return None
    return text


def _clean_phone(value: Any) -> str | None:
    text = _deduplicate_ocr_tokens(value)
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    international = text.lstrip().startswith("+") or digits.startswith("00")
    if international:
        if not 10 <= len(digits) <= 15:
            return None
    elif not (len(digits) == 10 and digits.startswith("0")):
        # Long bare identifiers are commonly SIREN/SIRET/VAT fragments, not
        # telephone numbers. A French national number must contain all ten
        # digits; international numbers must carry + or 00.
        return None
    return text


def _same_party(left: Any, right: Any) -> bool:
    left_key, right_key = _name_key(left), _name_key(right)
    if not left_key or not right_key:
        return False
    if min(len(left_key), len(right_key)) >= 6 and (
        left_key in right_key or right_key in left_key
    ):
        return True
    if min(left_key, right_key, key=len) in {"ELM", "BOSCH"} and (
        left_key in right_key or right_key in left_key
    ):
        return True
    left_words = {
        _name_key(word) for word in str(left or "").split()
        if len(_name_key(word)) >= 4
    }
    right_words = {
        _name_key(word) for word in str(right or "").split()
        if len(_name_key(word)) >= 4
    }
    return bool(left_words & right_words and {"LEBLANC", "BOSCH"} & left_words & right_words)


def clean_output_schema() -> dict[str, Any]:
    """Return the published JSON Schema for the clean extraction contract."""
    path = Path(__file__).with_name("schemas") / "jin-clean-extraction-v1.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _compact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: item for key, child in value.items()
                if (item := _compact(child)) not in (None, "", [], {})}
    if isinstance(value, list):
        return [item for child in value if (item := _compact(child)) not in (None, "", [], {})]
    return value


def _value(field: Any) -> Any:
    return field.get("value") if isinstance(field, dict) else field


def _first(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _first_field(*fields: Any) -> Any:
    """Select the first field carrying a business value, not metadata alone."""

    return next(
        (field for field in fields if _value(field) not in (None, "")),
        None,
    )


def _evidence(field: Any) -> dict[str, Any]:
    if not isinstance(field, dict):
        return {}
    evidence = field.get("evidence") or {}
    return _compact({
        "page": evidence.get("page"), "bbox": evidence.get("bbox"),
        "source_text": evidence.get("source_text"),
        "method": evidence.get("extraction_method"),
    })


def _field(field: Any) -> dict[str, Any] | None:
    value = _value(field)
    if value in (None, ""):
        return None
    if not isinstance(field, dict):
        return {"value": value}
    return _compact({
        "value": value,
        "site": field.get("site"),
        "raw_value": field.get("raw_value") if field.get("raw_value") != value else None,
        "confidence": _first(field.get("final_confidence"), field.get("confidence")),
        "status": field.get("validation_status"),
        "warnings": field.get("warnings"),
        "evidence": _evidence(field),
    })


def _contact(source: dict[str, Any]) -> dict[str, Any]:
    nested = source.get("contact") if isinstance(source.get("contact"), dict) else {}
    # Top-level phone/email values are role-level extractions. They are more
    # reliable than a nested contact assembled from the neighboring PDF column
    # (a frequent C.C.L. layout failure).
    return _compact({
        "name": _clean_contact_name(nested.get("name")),
        "firstname": _clean_contact_name(nested.get("firstname")),
        "lastname": _clean_contact_name(nested.get("lastname")),
        "department": _clean_department(nested.get("department") or source.get("department")),
        "email": source.get("email") or nested.get("email"),
        "phone": _clean_phone(source.get("phone") or nested.get("phone")),
        "fax": _clean_phone(source.get("fax") or nested.get("fax")),
    })


def _address_components(source: dict[str, Any]) -> dict[str, Any]:
    nested = source.get("address") if isinstance(source.get("address"), dict) else source
    components = {
        key: _deduplicate_ocr_tokens(nested.get(key))
        if isinstance(nested.get(key), str) else nested.get(key)
        for key in ADDRESS_FIELDS
    }
    if components.get("department"):
        components["department"] = _clean_department(components["department"])
    city = components.get("city")
    postal_code = components.get("postal_code")
    if city and postal_code:
        city = re.sub(
            rf"^\s*{re.escape(str(postal_code))}\s+",
            "",
            str(city),
            flags=re.I,
        ).strip()
        components["city"] = city or None
    return _compact(components)


def _party(source: dict[str, Any]) -> dict[str, Any]:
    raw_name = source.get("name")
    name = _clean_party_name(raw_name)
    legal_name = _clean_party_name(source.get("legal_name"))
    contact = _contact(source)
    if not name and re.match(r"^[.\-_*]{3,}", str(raw_name or "").strip()):
        recovered_contact = _clean_contact_name(
            re.sub(r"^[.\-_*\s]+", "", str(raw_name or ""))
        )
        if recovered_contact:
            contact.setdefault("name", recovered_contact)
    if contact.get("name") and any(
        _same_party(contact["name"], party_name)
        for party_name in (name, legal_name)
        if party_name
    ):
        contact.pop("name", None)
    return _compact({
        "id": source.get("id"), "code": source.get("code"),
        "customer_agency_code": source.get("customer_agency_code"),
        "name": name, "legal_name": legal_name,
        "department": _clean_department(source.get("department")), "contact": contact,
        "address": _address_components(source),
        "vat_number": source.get("vat_number"),
        "company_registration_number": source.get("company_registration_number"),
    })


def _party_display_name(source: dict[str, Any] | None) -> str | None:
    if not isinstance(source, dict):
        return None
    return _clean_party_name(source.get("legal_name")) or _clean_party_name(source.get("name"))


def _source_text(payload: dict[str, Any]) -> str:
    return "\n".join(
        str(page.get("text") or "")
        for page in payload.get("pages") or []
        if isinstance(page, dict)
    )


def _source_header_parties(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Read company names flanking an explicit ``COMMANDE FOURNISSEUR`` title."""
    for line in _source_text(payload).splitlines()[:80]:
        match = re.match(
            r"^\s*(?P<buyer>.+?)\s+COMMANDE\s+FOURNISSEUR(?:\s+(?P<supplier>.+))?\s*$",
            line,
            flags=re.I,
        )
        if match:
            return (
                _clean_party_name(match.group("buyer")),
                _clean_party_name(match.group("supplier")),
            )
    return None, None


def _source_explicit_billing_party(source: str) -> str | None:
    """Read a company following an explicit free-form billing-address lead-in."""
    for line in source.splitlines():
        match = re.search(
            r"(?:A\s+L['’]?ADRESSE\s+SUIVANTE|A\s+FACTURER\s+A)\s*[:=]\s*(.+)$",
            line,
            flags=re.I,
        )
        if match and (name := _clean_party_name(match.group(1))):
            return name
    return None


def _source_legal_company(source: str) -> str | None:
    """Recover a company from a legal footer when no header role survived."""
    for line in source.splitlines():
        match = re.match(
            r"^\s*(?P<name>.{2,80}?)\s+-\s+"
            r"(?:SASU?|SARL|EURL|S\.A\.?|SOCIETE)\b",
            line,
            flags=re.I,
        )
        if match and (name := _clean_party_name(match.group("name"))):
            return name
        match = re.match(
            r"^\s*(?P<name>.{2,80}?)\s+"
            r"(?:SASU?|SARL|EURL|S\.?A\.?)\s+AU\s+CAPITAL\b",
            line,
            flags=re.I,
        )
        if match and (name := _clean_party_name(match.group("name"))):
            return name
        match = re.match(
            r"^\s*(?:SASU?|SARL|EURL|S\.?A\.?)\s+"
            r"(?P<name>.{2,80}?)\s+(?:AU\s+CAPITAL|CAPITAL\s+SOCIAL)\b",
            line,
            flags=re.I,
        )
        if match and (name := _clean_party_name(match.group("name"))):
            return name
    return None


def _source_labeled_contact(
    source: str,
    labels: tuple[str, ...] = ("CONTACT", "ACHETEUR"),
    stop_phrases: tuple[str, ...] = (),
) -> dict[str, str]:
    """Recover a contact only when the source carries an explicit label."""
    if "DEMANDEUR" in labels:
        requester = re.search(
            r"(?im)^\s*DEMANDEUR\s*:?[ \t]+(?P<name>[^\n|]{2,120})$",
            source,
        )
        if requester:
            raw_name = requester.group("name").strip()
            tokens = raw_name.split()
            if len(tokens) >= 3:
                last = tokens[-1]
                if re.search(
                    rf"(?im)^.*\b(?:ZI|ZAC|ZAE|ZONE|RUE|AVENUE|AV|ROUTE|BD|BOULEVARD)\b"
                    rf"[^\n]*\b{re.escape(last)}\b",
                    source,
                ):
                    raw_name = " ".join(tokens[:-1])
            if name := _clean_contact_name(raw_name):
                contact = {"name": name}
                name_tokens = [
                    _name_key(token) for token in name.split()
                    if len(_name_key(token)) >= 4
                ]
                for email in re.findall(
                    r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}",
                    source,
                    flags=re.I,
                ):
                    if any(token in _name_key(email) for token in name_tokens):
                        contact["email"] = email
                        break
                return contact
    if "COMMANDE PAR" in labels:
        # Some ERP forms expose ``Commandé par`` as a column heading.  The
        # value is on the next row after date, order number and supplier code,
        # rather than on the same physical text line.
        lines = source.splitlines()
        for index, line in enumerate(lines[:-1]):
            key = _name_key(line)
            if (
                "DATEPIECEFOURN" not in key
                or "COMMAND" not in key
                or "PARPAGE" not in key
            ):
                continue
            row = lines[index + 1].strip()
            match = re.match(
                r"^\d{1,2}/\d{1,2}/\d{2,4}\s+\S+\s+\S+\s+"
                r"(?P<name>.+?)\s+\d+\s*/\s*\d+\s*$",
                row,
            )
            if match and (name := _clean_contact_name(match.group("name"))):
                return {"name": name}
    if "ACHETEUR" in labels:
        dotted_buyer = re.search(
            r"(?im)^\s*ACHETEUR\s+[A-Z]{1,4}\.\.\.\s*"
            r"(?P<name>[^\d\n|]{2,80})\s*$",
            source,
        )
        if dotted_buyer:
            name = _clean_contact_name(dotted_buyer.group("name"))
            if name:
                return {"name": name}
    if "PERSONNE A CONTACTER" in labels:
        person = re.search(
            r"(?im)^\s*PERSONNE\s+[AÀÂ]\s+CONTACTER\s*:?\s*"
            r"(?P<name>[^\n|]{2,80})\s*$",
            source,
        )
        if person:
            name = _clean_contact_name(person.group("name"))
            if name:
                contact = {"name": name}
                name_tokens = [
                    _name_key(token) for token in name.split()
                    if len(_name_key(token)) >= 4
                ]
                for email in re.findall(
                    r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}",
                    source,
                    flags=re.I,
                ):
                    if any(token in _name_key(email) for token in name_tokens):
                        contact["email"] = email
                        break
                return contact
    if "AFFAIRE SUIVIE PAR" in labels:
        followed = re.search(
            r"(?im)\bAFFAIRE\s+SUIVIE\s+PAR\s+(?P<last>[^\d\n]{2,50})\s*$"
            r"\n[^\n]*\bTVA\s+INTRA-COMM\.?\s+(?P<first>[^\d\n]{2,40})\s*$",
            source,
        )
        if followed:
            name = _clean_contact_name(
                f"{followed.group('last').strip()} {followed.group('first').strip()}"
            )
            if name:
                return {"name": name}
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?im)^.*?\b(?:{label_pattern})(?:\s*:\s*|\s*\.{{2,}}\s*)([^\n|]{{2,160}})",
        source,
    )
    if not match and "CONTACT" in labels:
        table = re.search(
            r"(?im)^.*\bN[Â°ºO]?\s*DOCUMENT\s+DATE\s+CONTACT\b[^\n]*\n"
            r"(?:[^\n]*\n){0,2}?"
            r"\s*\S+\s+\d{1,2}/\d{1,2}/\d{2,4}\s+"
            r"(?P<name>[^\W\d_][^\d\n]{1,50}?)"
            r"(?=\s+\d{4,}[A-Z]?\b|\s*$)",
            source,
        )
        if table:
            name = _clean_contact_name(table.group("name"))
            return {"name": name} if name else {}
    if not match:
        return {}
    raw = match.group(1).strip()
    for phrase in stop_phrases:
        if not phrase:
            continue
        boundary = re.search(rf"\b{re.escape(phrase)}\b", raw, flags=re.I)
        if boundary and boundary.start() > 0:
            raw = raw[:boundary.start()].strip()
    boundary = re.search(
        r"\s+-\s+(?=(?:\+?\d|[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}))",
        raw,
        flags=re.I,
    )
    email_boundary = re.search(
        r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}",
        raw,
        flags=re.I,
    )
    boundaries = [
        match.start() for match in (boundary, email_boundary) if match
    ]
    raw_name = raw[:min(boundaries)] if boundaries else raw
    if "DEMANDEUR" in _name_key(match.group(0)):
        # A neighboring address column can append its final locality token to
        # the requester (``Stéphane BERTA PALUDS``).  Remove that last token
        # only when the source also carries it on an explicit postal/site line.
        tokens = raw_name.split()
        if len(tokens) >= 3:
            last = tokens[-1]
            if re.search(
                rf"(?im)^.*\b(?:ZI|ZAC|ZAE|ZONE|RUE|AVENUE|AV|ROUTE|BD|BOULEVARD)\b"
                rf"[^\n]*\b{re.escape(last)}\b",
                source,
            ):
                raw_name = " ".join(tokens[:-1])
    if source[match.end():].lstrip().startswith("|"):
        raw_name = re.sub(r"\s+\d{1,2}\s*$", "", raw_name)
    name = _clean_contact_name(raw_name)
    if not name:
        return {}
    contact = {"name": name}
    phone = re.search(
        r"(?<!\d)(?:0\d(?:[ .-]?\d{2}){4})(?!\d)",
        raw,
    )
    if phone and (cleaned_phone := _clean_phone(phone.group(0))):
        contact["phone"] = cleaned_phone
    name_tokens = [
        _name_key(token) for token in name.split()
        if len(_name_key(token)) >= 4
    ]
    for email in re.findall(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", source, flags=re.I):
        if any(token in _name_key(email) for token in name_tokens):
            contact["email"] = email
            break
    return contact


def _source_orders_person_then_organisation(
    source: str,
    person: Any,
    organisation: Any,
) -> bool:
    """Prove a two-line ``Commandé par`` person/organisation role swap."""
    person_key, organisation_key = _name_key(person), _name_key(organisation)
    if not person_key or not organisation_key:
        return False
    lines = source.splitlines()
    for index, line in enumerate(lines):
        line_key = _name_key(line)
        if "COMMANDPAR" not in line_key and "COMMANDEPAR" not in line_key:
            continue
        values = [_name_key(value) for value in lines[index + 1:index + 6] if value.strip()]
        try:
            person_index = values.index(person_key)
            organisation_index = values.index(organisation_key)
        except ValueError:
            continue
        if person_index < organisation_index:
            return True
    return False


def _looks_like_person(value: Any) -> bool:
    text = " ".join(str(value or "").split())
    words = re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’-]*", text)
    organisation_markers = {
        "SAS", "SARL", "SA", "SOCIETE", "GROUPE", "HOLDING", "DEPOT",
        "AGENCE", "CCL", "PPC", "ISERBA", "REXEL", "PROLIANS",
    }
    return bool(
        2 <= len(words) <= 5
        and not any(char.isdigit() for char in text)
        and not organisation_markers.intersection({_name_key(word) for word in words})
    )


def _looks_like_organisation(value: Any) -> bool:
    key = _name_key(value)
    return any(marker in key for marker in (
        "SAS", "SARL", "SOCIETE", "GROUPE", "HOLDING", "DEPOT", "AGENCE",
        "CCL", "PPC", "ISERBA", "REXEL", "PROLIANS", "LOGISTIQUE",
        "CHAUFFAGE", "SANITAIRE",
    ))


def _source_company_site_alias(source: str, company: Any) -> str | None:
    """Expand a role-swapped company with a site printed before a supplier banner."""
    company_key = _name_key(company)
    if not company_key:
        return None
    for line in source.splitlines()[:30]:
        line_key = _name_key(line)
        if company_key not in line_key or "GROUPEBOSCH" not in line_key:
            continue
        prefix = re.split(r"\s+GROUPE\s+BOSCH\b", line, maxsplit=1, flags=re.I)[0]
        candidate = _clean_party_name(prefix)
        if candidate and _same_party(candidate, company):
            return candidate
    return _clean_party_name(company)


def _buyer_prefix_before_supplier(
    buyer_name: Any,
    supplier_name: Any,
) -> str | None:
    """Split a two-column header concatenated into one buyer name."""
    buyer = str(buyer_name or "").strip()
    supplier_words = [
        word for word in re.findall(r"[A-Za-zÀ-ÿ0-9]+", str(supplier_name or ""))
        if len(_name_key(word)) >= 3
        and _name_key(word) not in {"SAS", "SARL", "SITE", "PIECES", "DETACHEES"}
    ]
    if len(supplier_words) < 2:
        return None
    pattern = r"\b" + r"[^A-Za-zÀ-ÿ0-9]+".join(
        re.escape(word) for word in supplier_words[:2]
    ) + r"\b"
    match = re.search(pattern, buyer, flags=re.I)
    if not match:
        return None
    return _clean_party_name(buyer[:match.start()].strip(" ,;|/-"))


def _source_parallel_delivery_party(
    source: str,
    supplier_name: Any,
    current_delivery_name: Any,
) -> str | None:
    """Recover the delivery company from a two-column delivery/vendor row."""
    lines = [line.strip() for line in source.splitlines()]
    for index, line in enumerate(lines):
        folded = _name_key(line)
        if not (
            "ADRESSEDELIVRAISON" in folded
            and "COMMANDEFOURNISSEUR" in folded
        ):
            continue
        window = lines[index + 1:index + 4]
        if current_delivery_name and not any(
            _same_party(current_delivery_name, value) for value in window
        ):
            continue
        for value in window[:2]:
            candidate = _buyer_prefix_before_supplier(value, supplier_name)
            if (
                candidate
                and _name_key(candidate) != _name_key(supplier_name)
            ):
                return candidate
    return None


def _reconcile_clean_parties(
    parties: dict[str, dict[str, Any]],
    primary_delivery: dict[str, Any] | None,
    payload: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Remove OCR pollution and align parties with source-backed address roles."""
    source = _source_text(payload)
    source_key = _name_key(source)
    header_buyer, header_supplier = _source_header_parties(payload)
    explicit_bill_to = _source_explicit_billing_party(source)
    legal_company = _source_legal_company(source)
    raw_po = _find_purchase_order(payload)
    raw_header = raw_po.get("purchase_order") or raw_po
    order_number = _value(
        _first_field(raw_header.get("number"), raw_header.get("order_number"))
    )

    def is_order_reference(value: Any) -> bool:
        return bool(
            value
            and order_number
            and _name_key(value) == _name_key(order_number)
        )

    supplier = dict(parties.get("supplier") or {})
    current_supplier_name = _party_display_name(supplier)
    supplier_alias = header_supplier
    if not supplier_alias and "ELMLEBLANC" in source_key:
        supplier_alias = "ELM LEBLANC"
    initial_buyer_candidate = _party_display_name(parties.get("buyer"))
    initial_bill_to = parties.get("bill_to") or {}
    initial_bill_to_names = (
        _party_display_name(initial_bill_to),
        _clean_party_name(initial_bill_to.get("department")),
    )
    role_swapped_buyer_alias = None
    if (
        supplier_alias
        and current_supplier_name
        and initial_buyer_candidate
        and _same_party(initial_buyer_candidate, supplier_alias)
        and not _same_party(current_supplier_name, supplier_alias)
        and any(
            candidate and _same_party(current_supplier_name, candidate)
            for candidate in initial_bill_to_names
        )
    ):
        # Two-column forms can invert both ends: the true supplier is parsed
        # as buyer, while the client/billing organisation is parsed as
        # supplier.  Agreement with the explicit billing role proves the swap.
        role_swapped_buyer_alias = _source_company_site_alias(
            source, current_supplier_name
        )
    if supplier_alias and (
        not current_supplier_name
        or "COMMANDE" in _name_key(current_supplier_name)
        or role_swapped_buyer_alias
        or (
            order_number
            and _name_key(order_number) in _name_key(current_supplier_name)
        )
    ):
        role_was_unreliable = bool(
            not current_supplier_name
            or role_swapped_buyer_alias
            or (
                order_number
                and _name_key(order_number) in _name_key(current_supplier_name)
            )
        )
        supplier["name"] = supplier_alias
        supplier.pop("legal_name", None)
        if role_was_unreliable:
            supplier.pop("address", None)
            supplier.pop("contact", None)
        parties["supplier"] = supplier

    bill_to_party = dict(parties.get("bill_to") or {})
    current_bill_to_name = _party_display_name(bill_to_party)
    if (
        explicit_bill_to
        and (
            not current_bill_to_name
            or _name_key(explicit_bill_to).endswith(_name_key(current_bill_to_name))
        )
    ):
        bill_to_party["name"] = explicit_bill_to
        bill_to_party.pop("legal_name", None)
        parties["bill_to"] = bill_to_party
    supplier_name = _party_display_name(parties.get("supplier"))
    delivery = primary_delivery if isinstance(primary_delivery, dict) else {}
    components = delivery.get("components") if isinstance(delivery.get("components"), dict) else {}
    # The normalized recipient/department have already passed the address
    # cleanup stage.  Prefer them to the raw role fields, which can still
    # contain a nearby heading (for example ``COMMANDE N°``).
    delivery_name = _clean_party_name(components.get("recipient") or delivery.get("party_name"))
    delivery_department = _clean_party_name(components.get("department") or delivery.get("department"))
    delivery_zone = _clean_party_name(components.get("industrial_zone"))
    if delivery_name and delivery_zone and _same_party(delivery_name, delivery_zone):
        # Some layouts put the industrial zone on the first postal line.  It
        # is an address component, not the recipient.  Prefer the explicitly
        # named depot/company when the source provides one in parentheses.
        depot = re.search(
            # D.P.T / . deliberately accepts the accented OCR characters in
            # ``Dépôt à livrer`` while keeping this source pattern ASCII.
            r"(?im)\bD.P.T\s+.\s+LIVRER\s*:[^\n(]{0,80}"
            r"\((?P<recipient>[^)\n]{2,80})\)",
            source,
        )
        if depot and (explicit_recipient := _clean_party_name(depot.group("recipient"))):
            delivery_name = explicit_recipient
    if (
        delivery_department
        and _name_key(delivery_name) in {
            "DEPOTENLEVEMENT", "DEPOTSENLEVEMENT",
            "DEPOTENLEVEMENTS", "DEPOTSENLEVEMENTS",
        }
    ):
        delivery_name = delivery_department
    if delivery_name and _same_party(delivery_name, supplier_name):
        delivery_name = delivery_department or delivery_name
    if not delivery_name:
        delivery_name = delivery_department

    parallel_delivery_name = _source_parallel_delivery_party(
        source, supplier_name, delivery_name
    )
    if (
        parallel_delivery_name
        and delivery_name
        and not _same_party(parallel_delivery_name, delivery_name)
    ):
        delivery_department = delivery_name
        delivery_name = parallel_delivery_name
        delivery["party_name"] = delivery_name
        components["recipient"] = delivery_name
        components["department"] = delivery_department
        delivery["components"] = components
        _refresh_delivery_label(delivery)

    initial_buyer_name = _party_display_name(parties.get("buyer"))
    if (
        delivery_name
        and delivery_department
        and initial_buyer_name
        and _same_party(delivery_department, initial_buyer_name)
        and not _same_party(delivery_name, initial_buyer_name)
    ):
        # In parallel billing/delivery columns the client company can be the
        # first destination line and the following label its local site.
        delivery_name, delivery_department = delivery_department, delivery_name
        delivery["party_name"] = delivery_name
        components["recipient"] = delivery_name
        components["department"] = delivery_department
        delivery["components"] = components
        _refresh_delivery_label(delivery)

    # A delivery site parsed as supplier is a frequent two-column role swap.
    # A source-backed supplier alias can safely repair it.
    if (
        supplier_alias
        and delivery_name
        and supplier_name
        and _same_party(supplier_name, delivery_name)
    ):
        supplier = dict(parties.get("supplier") or {})
        supplier["name"] = supplier_alias
        supplier.pop("legal_name", None)
        supplier.pop("address", None)
        supplier.pop("contact", None)
        parties["supplier"] = supplier
        supplier_name = supplier_alias

    if delivery_name:
        ship_to = dict(parties.get("ship_to") or {})
        ship_to["name"] = delivery_name
        if (
            ship_to.get("legal_name")
            and _name_key(ship_to["legal_name"]) != _name_key(delivery_name)
        ):
            ship_to.pop("legal_name", None)
        contact = dict(ship_to.get("contact") or {})
        for key, value in (delivery.get("contact") or {}).items():
            if value:
                contact.setdefault(key, value)
        if delivery_department:
            ship_to["department"] = delivery_department
        if contact.get("name") and _same_party(contact["name"], delivery_name):
            contact.pop("name", None)
        if contact.get("department") and (
            _same_party(contact["department"], delivery_name)
            or _same_party(contact["department"], delivery_department)
        ):
            contact.pop("department", None)
        if ship_to.get("department") and _same_party(ship_to["department"], delivery_name):
            ship_to.pop("department", None)
        if contact:
            ship_to["contact"] = contact
        else:
            ship_to.pop("contact", None)
        parties["ship_to"] = ship_to

    buyer = dict(parties.get("buyer") or {})
    buyer_name = _party_display_name(buyer)
    explicitly_labeled_buyer = _source_labeled_contact(
        source, labels=("ACHETEUR",)
    ).get("name")
    if (
        header_buyer
        and buyer_name
        and explicitly_labeled_buyer
        and _same_party(buyer_name, explicitly_labeled_buyer)
    ):
        # ``Acheteur`` names a person, whereas the company before the explicit
        # COMMANDE FOURNISSEUR title is the client organisation.
        buyer_name = header_buyer
        buyer["name"] = buyer_name
        buyer.pop("legal_name", None)
    if (
        buyer_name
        and delivery_zone
        and _same_party(buyer_name, delivery_zone)
        and not _same_party(buyer_name, delivery_name)
    ):
        # A locality extracted from ``ZA DE ...`` is an address component,
        # not the legal buyer. Let the explicit billing/company roles win.
        buyer.pop("name", None)
        buyer.pop("legal_name", None)
        buyer_name = None
    if (
        buyer_name
        and "ELMLEBLANC" in source_key
        and re.search(r"\s+GROUPE\s+BOSCH\s*$", buyer_name, flags=re.I)
    ):
        clean_buyer_prefix = _clean_party_name(
            re.sub(r"\s+GROUPE\s+BOSCH\s*$", "", buyer_name, flags=re.I)
        )
        if clean_buyer_prefix:
            buyer_name = clean_buyer_prefix
            buyer["name"] = buyer_name
            buyer.pop("legal_name", None)
    if is_order_reference(buyer_name):
        buyer.pop("name", None)
        buyer.pop("legal_name", None)
        buyer_name = None
    replacement_kind = None
    if not buyer_name or _same_party(buyer_name, supplier_name):
        original_buyer_name = buyer_name
        bill_to = parties.get("bill_to") or {}
        bill_to_name = _party_display_name(bill_to)
        if is_order_reference(bill_to_name):
            bill_to = dict(bill_to)
            bill_to.pop("name", None)
            bill_to.pop("legal_name", None)
            bill_to_name = None
            if bill_to:
                parties["bill_to"] = bill_to
            else:
                parties.pop("bill_to", None)
        source_alias = None
        if "PIECESXPRESS" in source_key:
            source_alias = "PIECES XPRESS"
        elif "BIOHABITAT" in source_key:
            source_alias = "BIO HABITAT"
        if legal_company and _same_party(legal_company, supplier_name):
            legal_company = None
        buyer_prefix = _buyer_prefix_before_supplier(buyer_name, supplier_name)
        replacement = (
            role_swapped_buyer_alias or bill_to_name or source_alias or header_buyer
            or legal_company or buyer_prefix
        )
        replacement_kind = (
            "role_swap" if role_swapped_buyer_alias
            else "bill_to" if bill_to_name
            else "source_alias" if source_alias
            else "source_header" if header_buyer
            else "source_legal" if legal_company
            else "buyer_prefix" if buyer_prefix
            else None
        )
        if (
            not replacement
            and delivery_name
            and (
                parallel_delivery_name
                or not _same_party(delivery_name, supplier_name)
            )
        ):
            replacement = delivery_name
            replacement_kind = "delivery"
        if replacement:
            buyer["name"] = replacement
            buyer.pop("legal_name", None)
            buyer_name = replacement
            if replacement_kind == "bill_to":
                bill_to_contact = dict(bill_to.get("contact") or {})
                bill_to_address = dict(bill_to.get("address") or {})
                if bill_to_address:
                    buyer["address"] = bill_to_address
                if bill_to_contact:
                    buyer["contact"] = bill_to_contact
                elif original_buyer_name and _same_party(original_buyer_name, supplier_name):
                    buyer.pop("contact", None)
            elif replacement_kind == "source_alias":
                # A source-level alias repairs a role swap but does not prove
                # that the phone/email extracted from the swapped block belongs
                # to the repaired buyer.
                buyer.pop("contact", None)
            elif replacement_kind in {"delivery", "buyer_prefix"}:
                delivery_contact = dict((parties.get("ship_to") or {}).get("contact") or {})
                labeled_contact = _source_labeled_contact(
                    source, stop_phrases=(delivery_name or "",)
                )
                for key, value in {**delivery_contact, **labeled_contact}.items():
                    buyer.setdefault("contact", {}).setdefault(key, value)
    contact = dict(buyer.get("contact") or {})
    # In some two-column layouts the person following ``Commandé par`` is
    # parsed as the company while the company/site is parsed as its contact.
    # Swap only when that alleged contact agrees with the proven delivery site.
    if (
        buyer_name
        and delivery_name
        and contact.get("name")
        and re.search(r"\bCOMMAND\S*\s+PAR\b", source, flags=re.I)
        and (
            _same_party(contact["name"], delivery_name)
            or (
                _looks_like_person(buyer_name)
                and _looks_like_organisation(contact["name"])
                and _source_orders_person_then_organisation(
                    source, buyer_name, contact["name"]
                )
            )
        )
        and not _same_party(buyer_name, delivery_name)
    ):
        person_name = buyer_name
        buyer_name = contact["name"]
        buyer["name"] = buyer_name
        buyer.pop("legal_name", None)
        contact["name"] = person_name
    supplier_contact = (parties.get("supplier") or {}).get("contact") or {}
    raw_buyer = raw_po.get("buyer") if isinstance(raw_po.get("buyer"), dict) else {}
    raw_nested_contact = (
        raw_buyer.get("contact")
        if isinstance(raw_buyer.get("contact"), dict)
        else {}
    )
    for key in ("email", "phone", "fax"):
        if (
            contact.get(key)
            and supplier_contact.get(key)
            and _name_key(contact[key]) == _name_key(supplier_contact[key])
        ):
            candidate = raw_nested_contact.get(key)
            candidate = _clean_phone(candidate) if key in {"phone", "fax"} else candidate
            if candidate and _name_key(candidate) != _name_key(supplier_contact[key]):
                contact[key] = candidate
            else:
                contact.pop(key, None)
    # ``Acheteur:`` is an unambiguous client-side role and therefore safely
    # outranks a line-item description accidentally parsed as a person.
    contact.update(_source_labeled_contact(
        source,
        labels=(
            "ACHETEUR", "SUIVI PAR", "CORRESPONDANT", "AFFAIRE SUIVIE PAR",
            "PERSONNE A CONTACTER", "DEMANDEUR", "COMMANDE PAR",
        ),
        stop_phrases=(delivery_name or "",),
    ))
    if not contact.get("name") and re.search(
        r"(?im)\bN\S*\s*DOCUMENT\s+DATE\s+CONTACT\b", source
    ):
        contact.update(_source_labeled_contact(
            source, labels=("CONTACT",), stop_phrases=(delivery_name or "",)
        ))

    bill_to_name = _party_display_name(parties.get("bill_to"))
    if (
        buyer_name
        and bill_to_name
        and _same_party(buyer_name, bill_to_name)
        and len(_name_key(bill_to_name)) > len(_name_key(buyer_name))
        and (
            not delivery_name
            or _same_party(delivery_name, bill_to_name)
            or not _same_party(buyer_name, delivery_name)
        )
    ):
        # When both explicit destination roles agree on a longer company
        # name, expand a buyer truncated by column OCR (for example
        # ``SERVICE RAPIDE`` -> ``GAZ SERVICE RAPIDE``).
        buyer_name = bill_to_name
        buyer["name"] = buyer_name
        buyer.pop("legal_name", None)

    delivery_city = components.get("city")
    if (
        buyer_name
        and delivery_name
        and _name_key(delivery_name).startswith("PPC")
        and "PPC" not in _name_key(buyer_name)
        and (
            _same_party(buyer_name, delivery_name)
            or (
                len(_name_key(buyer_name)) >= 4
                and _name_key(delivery_name).endswith(_name_key(buyer_name))
            )
        )
    ):
        buyer_name = delivery_name
        buyer["name"] = buyer_name
        buyer.pop("legal_name", None)
    elif (
        buyer_name
        and delivery_name
        and delivery_city
        and _name_key(buyer_name) == "PPC"
        and _name_key(delivery_name) == "PPC"
    ):
        site_city = re.sub(
            r"\s+\d+(?:ER|E|EME)?\s+ARRONDISSEMENT\s*$",
            "",
            str(delivery_city),
            flags=re.I,
        ).strip()
        if site_city and len(site_city) <= 45:
            buyer_name = f"PPC {site_city}".strip()
            buyer["name"] = buyer_name
            buyer.pop("legal_name", None)
    elif (
        buyer_name
        and delivery_name
        and delivery_city
        and _name_key(buyer_name) == _name_key(delivery_city)
        and _name_key(buyer_name) != _name_key(delivery_name)
    ):
        buyer_name = (
            delivery_name
            if _name_key(delivery_name).endswith(_name_key(buyer_name))
            else f"{delivery_name} {buyer_name}".strip()
        )
        buyer["name"] = buyer_name
        buyer.pop("legal_name", None)
    elif (
        buyer_name
        and delivery_name
        and not _same_party(buyer_name, delivery_name)
        and re.search(
            rf"\bAGENCE\s+DE\s+{re.escape(str(buyer_name))}\b",
            source,
            flags=re.I,
        )
    ):
        # A branch name can be split across the page heading (city) and the
        # explicit delivery recipient (business activity).  A sentence such
        # as ``l'agence de LANGON`` proves that the city is the site suffix.
        buyer_name = f"{delivery_name} {buyer_name}".strip()
        buyer["name"] = buyer_name
        buyer.pop("legal_name", None)
    elif (
        buyer_name
        and delivery_name
        and _name_key(delivery_name) == "PPC"
        and "PPC" not in _name_key(buyer_name)
    ):
        buyer_name = (
            delivery_name
            if "PARTEDIS" in _name_key(buyer_name)
            else f"PPC {buyer_name}".strip()
        )
        buyer["name"] = buyer_name
        buyer.pop("legal_name", None)
    elif (
        buyer_name
        and delivery_name
        and _name_key(buyer_name) == _name_key(delivery_name)
        and buyer_name != delivery_name
    ):
        buyer_name = delivery_name
        buyer["name"] = buyer_name
        buyer.pop("legal_name", None)
    if contact.get("name") and buyer_name and _same_party(contact["name"], buyer_name):
        contact.pop("name", None)
    if contact:
        buyer["contact"] = contact
    else:
        buyer.pop("contact", None)
    if buyer:
        parties["buyer"] = buyer
    ship_to = dict(parties.get("ship_to") or {})
    if primary_delivery and not _party_display_name(ship_to) and buyer_name:
        ship_to["name"] = buyer_name
        parties["ship_to"] = ship_to
    return {role: party for role, party in parties.items() if party}


def _align_primary_delivery_party(
    primary_delivery: dict[str, Any] | None,
    parties: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Reflect a proven supplier/ship-to role repair in the clean postal label."""
    if not isinstance(primary_delivery, dict):
        return primary_delivery
    delivery = primary_delivery
    supplier_name = _party_display_name(parties.get("supplier"))
    ship_to_name = _party_display_name(parties.get("ship_to"))
    shown_name = _clean_party_name(delivery.get("party_name"))
    missing_recipient = not shown_name and bool(ship_to_name)
    components = dict(delivery.get("components") or {})
    industrial_zone = _clean_party_name(components.get("industrial_zone"))
    generic_recipient = bool(
        shown_name and ship_to_name
        and _name_key(shown_name) in {
            "DEPOTENLEVEMENT", "DEPOTSENLEVEMENT",
            "DEPOTENLEVEMENTS", "DEPOTSENLEVEMENTS",
        }
        and not _same_party(ship_to_name, shown_name)
    )
    zone_recipient = bool(
        shown_name and industrial_zone and ship_to_name
        and _same_party(shown_name, industrial_zone)
        and not _same_party(ship_to_name, shown_name)
    )
    supplier_recipient = bool(
        shown_name and supplier_name and ship_to_name
        and _same_party(shown_name, supplier_name)
        and not _same_party(ship_to_name, supplier_name)
    )
    if not (
        missing_recipient or supplier_recipient or zone_recipient
        or generic_recipient
    ):
        return delivery

    delivery["party_name"] = ship_to_name
    components["recipient"] = ship_to_name
    if components.get("department") and _same_party(components["department"], ship_to_name):
        components.pop("department", None)
    delivery["components"] = components

    raw_lines = list(delivery.get("formatted_lines") or [])
    if missing_recipient or zone_recipient:
        raw_lines.insert(0, ship_to_name.upper())
    elif raw_lines:
        raw_lines[0] = ship_to_name.upper()
    else:
        raw_lines = [ship_to_name.upper()]
    clean_lines: list[str] = []
    for line in raw_lines:
        if not any(_name_key(line) == _name_key(existing) for existing in clean_lines):
            clean_lines.append(line)
    delivery["formatted_lines"] = clean_lines
    delivery["formatted"] = ", ".join(clean_lines)
    return delivery


def _refresh_delivery_label(delivery: dict[str, Any]) -> dict[str, Any]:
    """Keep the printable label synchronized with reconciled components."""
    components = dict(delivery.get("components") or {})
    if not components:
        return delivery
    refreshed = clean_delivery_address({
        "role": delivery.get("role"),
        "party_name": components.get("recipient") or delivery.get("party_name"),
        "department": components.get("department") or delivery.get("department"),
        "address": components,
        "formatted_address": delivery.get("source_formatted"),
        "address_verification": delivery.get("verification") or {},
        "customer_agency_code": delivery.get("customer_agency_code"),
    })
    delivery["party_name"] = refreshed.get("components", {}).get("recipient")
    if refreshed.get("components", {}).get("department"):
        delivery["department"] = refreshed["components"]["department"]
    else:
        delivery.pop("department", None)
    delivery["components"] = refreshed.get("components") or components
    delivery["formatted_lines"] = refreshed.get("lines") or delivery.get("formatted_lines")
    delivery["formatted"] = refreshed.get("one_line") or delivery.get("formatted")
    return delivery


def _business_address(source: dict[str, Any]) -> dict[str, Any]:
    verification = source.get("address_verification") or source.get("ban_verification") or {}
    clean_address = source.get("clean_address") if isinstance(source.get("clean_address"), dict) else {}
    clean_components = (
        clean_address.get("components")
        if isinstance(clean_address.get("components"), dict)
        else {}
    )
    role = source.get("role")
    party_name = _clean_party_name(
        clean_components.get("recipient") or source.get("party_name")
    )
    department = _clean_department(
        clean_components.get("department") or source.get("department")
    )
    if clean_components:
        # Rebuild the printable label from the already-normalized components.
        # Party reconciliation can invalidate a noisy first line while the
        # component map is correct; keeping the former ``one_line`` would make
        # the UI and JSON disagree (for example a fax instruction or CLI id
        # still displayed after its component was rejected).
        refreshed = clean_delivery_address({
            "role": role,
            "party_name": party_name,
            "department": department,
            "address": clean_components,
            "formatted_address": (
                clean_address.get("source_formatted")
                or source.get("formatted_address")
            ),
            "address_verification": verification,
            "customer_agency_code": source.get("customer_agency_code"),
        })
        clean_components = refreshed.get("components") or clean_components
        clean_address = {**clean_address, **refreshed}
    source_id = source.get("address_id")
    # One physical address may legitimately be reused for several business
    # roles.  Keep each JSON object independently addressable for consumers.
    clean_id = f"{source_id}:{role}" if source_id and role else source_id
    return _compact({
        "id": clean_id, "role": role,
        "role_label": source.get("role_label"),
        "party_name": _clean_party_name(
            clean_components.get("recipient") or party_name
        ),
        "party_code": source.get("party_code"),
        "department": _clean_department(
            clean_components.get("department") or department
        ),
        "customer_agency_code": source.get("customer_agency_code"),
        "contact": {
            "name": _clean_contact_name(source.get("contact_name")),
            "email": source.get("contact_email"),
            "phone": _clean_phone(source.get("contact_phone")),
        },
        "formatted": clean_address.get("one_line") or source.get("formatted_address"),
        "formatted_lines": clean_address.get("lines"),
        "source_formatted": clean_address.get("source_formatted")
                            if clean_address.get("source_formatted") != clean_address.get("one_line") else None,
        "components": clean_components or _address_components(source),
        "normalization": {
            "status": clean_address.get("status"),
            "reference_status": clean_address.get("reference_status"),
            "warnings": clean_address.get("warnings"),
            "excluded_components": clean_address.get("excluded_components"),
        },
        "role_confidence": source.get("role_confidence"),
        "address_confidence": source.get("address_confidence"),
        "verification": verification,
        "evidence": _evidence(source), "warnings": source.get("warnings"),
    })


def _line(source: dict[str, Any]) -> dict[str, Any]:
    refs = {key: source.get(key) for key in REFERENCE_FIELDS}
    return _compact({
        "line_number": source.get("line_number"), "references": refs,
        "description": source.get("description"),
        "quantity": _first(source.get("quantity"), source.get("ordered_quantity")),
        "unit": source.get("uom"),
        "pricing": {
            "unit_price": source.get("unit_price"),
            "net_unit_price": source.get("net_unit_price"),
            "price_unit": source.get("price_unit"), "currency": source.get("currency"),
            "discount_percent": source.get("discount_percent"),
            "discount_amount": source.get("discount_amount"),
        },
        "amounts": {
            "net": source.get("line_net_amount"), "total": source.get("line_total"),
        },
        "delivery": {
            "date": source.get("delivery_date"),
            "schedule": source.get("delivery_schedule") or source.get("schedule_lines"),
            "site": source.get("delivery_site"),
        },
        "status": source.get("validation_status"), "confidence": source.get("confidence"),
        "warnings": source.get("warnings"),
        "evidence": {"page": source.get("page"), "bbox": source.get("bbox")},
    })


def _charge(source: dict[str, Any]) -> dict[str, Any]:
    return _compact({
        "type": source.get("charge_type"), "description": source.get("description"),
        "reference": source.get("supplier_reference") or source.get("code"),
        "quantity": source.get("quantity"), "unit_price": source.get("unit_price"),
        "amount": source.get("amount"), "currency": source.get("currency"),
        "page": source.get("page"), "bbox": source.get("bbox"),
    })


def build_clean_output(payload: dict[str, Any], source_filename: str | None = None) -> dict[str, Any]:
    """Create a compact contract without deleting diagnostic fields from payload."""
    po = _find_purchase_order(payload)
    header = po.get("purchase_order") or po if po else {}
    document = payload.get("document") or po.get("document") or {}
    decision = payload.get("extraction_decision") or {}
    output_quality = payload.get("output_quality") or {}
    validation = po.get("validation") or {}
    totals = po.get("totals") or {}
    grouped_numbers = [
        field for item in payload.get("customer_order_references") or []
        if (field := _field(item))
    ]

    parties = {role: _party(po.get(role) or {}) for role in PARTY_ROLES}
    addresses = [_business_address(item) for item in po.get("business_addresses") or []]
    delivery_addresses = [item for item in addresses if item.get("role") == "ship_to"]
    primary_delivery = delivery_addresses[0] if len(delivery_addresses) == 1 else None
    parties = _reconcile_clean_parties(parties, primary_delivery, payload)
    primary_delivery = _align_primary_delivery_party(primary_delivery, parties)
    order_fields = {
        "customer_order_number": _field(
            _first_field(header.get("number"), header.get("order_number"))
        ),
        "order_date": _field(header.get("order_date")),
        "currency": _field(header.get("currency")),
        "customer_reference": _field(header.get("customer_reference")),
        "supplier_reference": _field(header.get("vendor_reference")),
        "quote_number": _field(header.get("quote_number")),
        "contract_number": _field(header.get("contract_number")),
        "project_number": _field(header.get("project_number")),
        "requested_delivery_date": _field(
            _first_field(
                header.get("expected_delivery_date"),
                header.get("required_date"),
            )
        ),
        "customer_agency_code": _field(
            _first_field(
                header.get("customer_agency_code"),
                po.get("customer_agency_code"),
            )
        ),
    }
    clean = {
        "schema_version": "jin-clean-extraction-v1",
        "generator": {"runtime": __version__, "core": document.get("engine_version")},
        "document": {
            "filename": source_filename or document.get("filename"),
            "sha256": document.get("sha256"), "mime_type": document.get("mime_type"),
            "type": document.get("primary_document_type")
                    or document.get("detected_document_type") or document.get("document_type"),
            "format": document.get("format_family") or document.get("format"),
            "page_count": document.get("page_count") or len(po.get("pages") or payload.get("pages") or []),
            "languages": document.get("detected_languages") or document.get("language"),
            "processing_seconds": document.get("processing_duration_seconds"),
            "order_structure": document.get("order_structure"),
            "grouped_order_count": document.get("grouped_order_count"),
            "references": {
                "customer_order_number": _field(
                    (payload.get("document_references") or {}).get("customer_order_number")
                ),
                "request_for_quotation_number": _field(
                    (payload.get("document_references") or {}).get("request_for_quotation_number")
                ),
                "referenced_order_number": _field(
                    (payload.get("document_references") or {}).get("referenced_order_number")
                ),
                "customer_order_numbers": grouped_numbers,
            },
            "tax_identifiers": payload.get("document_tax_identifiers"),
        },
        "order": {
            **order_fields,
            "customer_order_numbers": grouped_numbers,
            "groups": payload.get("grouped_orders"),
            "parties": parties, "addresses": addresses,
            "delivery_address": primary_delivery,
            "delivery_address_selection": po.get("delivery_address_selection"),
            "line_items": [_line(item) for item in po.get("lines") or []],
            "additional_charges": [_charge(item) for item in po.get("additional_charges") or []],
            "totals": {key: totals.get(key) for key in (
                "subtotal", "total_discount", "total_surcharge", "total_freight", "total_shipping",
                "total_net", "total_before_tax", "total_vat", "total_tax", "total_gross",
                "grand_total", "amount_due", "currency",
            )},
            "commercial_terms": po.get("commercial"), "logistics": po.get("logistics"),
            "tax_identifiers": po.get("tax_identifiers"),
        } if po else None,
        "quality": {
            "decision": decision.get("status"), "requires_review": decision.get("requires_review", True),
            "review_reasons": decision.get("reasons"),
            "business_validation": validation,
            "output_audit": {
                "status": output_quality.get("status"),
                "issues": output_quality.get("issues"),
                "repairs": output_quality.get("repair_count"),
            },
            "order_number": payload.get("order_number_check"),
            "qualification": decision.get("qualification"),
        },
        "reference_data": payload.get("reference_data"),
    }
    return _compact(clean)

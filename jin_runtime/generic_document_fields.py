"""Source-backed identifiers that are useful for every document type.

The core business extractor intentionally focuses on purchase orders.  Some
adjacent documents, especially order confirmations, still carry a customer
order reference and VAT identifiers that consumers need.  This module exposes
only explicitly printed, narrowly matched values and keeps their evidence.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any


_FR_VAT_RE = re.compile(
    r"(?<![A-Z0-9])F\s*R\s*([A-Z0-9](?:[ .-]*[A-Z0-9]))\s*"
    r"((?:[0-9OIL][ .-]*){8}[0-9OIL])(?![A-Z0-9])",
    re.I,
)
_FRENCH_REGISTRATION_RE = re.compile(
    r"\b(?P<type>SIRET|SIREN)\s*(?:N\s*(?:[°ºO]|UMERO)?\s*)?[:#.-]?\s*"
    r"(?P<number>(?:\d[ .-]*){13}\d|(?:\d[ .-]*){8}\d)(?!\d)",
    re.I,
)
_CUSTOMER_ORDER_RE = re.compile(
    r"(?:REF(?:ERENCE)?\.?\s*(?:DE\s+)?COMMANDE\s+CLIENT|"
    r"VOTRE\s+(?:N(?:O|UMERO)?\.?\s*)?COMMANDE|"
    r"CUSTOMER\s+(?:PURCHASE\s+)?ORDER(?:\s+(?:NO|NUMBER))?)"
    r"\s*[:#-]?\s*(?P<value>[A-Z0-9][A-Z0-9./_-]{3,39})\b",
    re.I,
)
_RFQ_TITLE_RE = re.compile(
    r"\b(?:DEMANDE\s+DE(?:\s+PAGE)?\s+PRIX|REQUEST\s+FOR\s+(?:QUOTATION|QUOTE)|RFQ)\b",
    re.I,
)
_RFQ_ALPHANUMERIC_REFERENCE_RE = re.compile(
    r"\b(?=[A-Z0-9._/-]{3,31}\b)(?=[A-Z0-9._/-]*[A-Z])(?=[A-Z0-9._/-]*\d)"
    r"[A-Z][A-Z0-9._/-]{2,30}\b",
    re.I,
)
_RFQ_NUMERIC_REFERENCE_RE = re.compile(
    r"\bN\s*(?:[\u00b0\u00ba]|O|UMERO)?\s*[:#.-]*\s*(?P<value>\d{3,30})\b",
    re.I,
)
_REFERENCED_ORDER_RE = re.compile(
    r"\bBON\s+DE\s+COMMANDE\s+N\s*[\u00b0\u00baO]?\s*[:#.-]?\s*"
    r"(?P<value>[A-Z0-9][A-Z0-9._/-]{3,39})\b",
    re.I,
)
_CANCELLATION_REFERENCED_ORDER_RE = re.compile(
    r"\bCOMMANDE\s+N\s*[\u00b0\u00baO]?\s*[:#.-]?\s*"
    r"(?P<value>[A-Z0-9][A-Z0-9._/-]{3,39})\b",
    re.I,
)


def _fold(value: Any) -> str:
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    ).upper()
    return re.sub(r"\s+", " ", text).strip()


def _valid_french_vat(value: str) -> bool:
    if not re.fullmatch(r"FR\d{11}", value):
        return False
    key = int(value[2:4])
    siren = int(value[4:])
    return key == (12 + 3 * (siren % 97)) % 97


def _valid_luhn(value: str) -> bool:
    if not value.isdigit():
        return False
    total = 0
    parity = len(value) % 2
    for index, char in enumerate(value):
        digit = int(char)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _vat_from_siren(siren: str) -> str:
    key = (12 + 3 * (int(siren) % 97)) % 97
    return f"FR{key:02d}{siren}"


def _page_texts(payload: dict[str, Any]) -> list[tuple[int, str]]:
    pages = payload.get("pages") or []
    found = {
        int(page.get("page") or index): str(page.get("text") or "")
        for index, page in enumerate(pages, 1)
        if isinstance(page, dict) and page.get("text")
    }
    recognized = (
        (payload.get("weak_field_suggestions") or {}).get("recognized_pages") or []
    )

    def label_score(text: str) -> int:
        folded = _fold(text)
        return sum(
            bool(re.search(rf"\b{marker}\b", folded))
            for marker in (
                "TVA", "VAT", "SIRET", "SIREN", "COMMANDE", "ORDER",
                "FACTURE", "INVOICE", "ADRESSE", "ADDRESS", "TOTAL",
                "DATE", "MONTANT", "QUANTITE", "FOURNISSEUR", "ACHETEUR",
            )
        )

    for page in recognized:
        if (
            isinstance(page, dict)
            and page.get("text_source") == "tesseract_ocr"
            and page.get("text")
        ):
            # When the native text layer is overwhelmingly rotated, its line
            # stream is not searchable as human-readable text. The weak field
            # router already OCRs the correctly rendered page; reuse exactly
            # that source-backed text for document-wide IDs such as VAT.
            page_number = int(page.get("page") or 1)
            router_text = str(page["text"])
            native_text = found.get(page_number, "")
            # Router OCR is a recovery source, not an unconditional authority.
            # Keep an already coherent core page: a second OCR pass can turn a
            # correct legal identifier into a different, coincidentally valid
            # VAT number. Use the router only when it materially restores
            # semantic labels (typical of a rotated/reversed native layer).
            if not native_text or label_score(router_text) >= label_score(native_text) + 2:
                found[page_number] = router_text
    if found:
        return sorted(found.items())
    raw_text = str(payload.get("raw_text") or "")
    return [(1, raw_text)] if raw_text else []


def _party_role(context: str) -> str:
    folded = _fold(context)
    if any(term in folded for term in ("CLIENT", "ACHETEUR", "BUYER", "DONNEUR D ORDRE")):
        return "buyer"
    if any(term in folded for term in ("FOURNISSEUR", "SUPPLIER", "VENDOR")):
        return "supplier"
    return "unknown"


def _request_for_quotation_reference(line: str) -> str | None:
    """Return a reference printed on an explicit RFQ title line.

    OCR/layout extraction may interleave ``Page`` inside ``Demande de prix``;
    page counters such as 1/1 must never become the document reference.
    """
    folded = _fold(line)
    title = _RFQ_TITLE_RE.search(folded)
    if not title:
        return None
    tail = folded[title.end():]
    for match in _RFQ_ALPHANUMERIC_REFERENCE_RE.finditer(tail):
        value = match.group(0).strip(" .,:;#-/")
        if value not in {"PAGE", "PRIX", "NUMERO", "LIVRER"}:
            return value
    labelled_numeric = _RFQ_NUMERIC_REFERENCE_RE.search(tail)
    return labelled_numeric.group("value") if labelled_numeric else None


def enrich_generic_document_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Add exact VAT IDs and explicit customer-order references to a payload."""
    tax_by_value: dict[str, dict[str, Any]] = {}
    registrations_by_siren: dict[str, dict[str, Any]] = {}
    customer_order: dict[str, Any] | None = None
    request_for_quotation: dict[str, Any] | None = None
    referenced_order: dict[str, Any] | None = None
    document = payload.get("document") or {}
    document_type = (
        document.get("primary_document_type")
        or document.get("detected_document_type")
        or document.get("document_type")
    )

    for page_number, page_text in _page_texts(payload):
        lines = page_text.splitlines()
        for line_index, raw_line in enumerate(lines):
            line = " ".join(raw_line.split())
            if not line:
                continue
            context = " ".join(lines[max(0, line_index - 1):line_index + 2])
            folded = _fold(line)
            labelled = any(
                marker in folded
                for marker in ("TVA", "VAT", "INTRACOM", "INTRA COMMUNAUTAIRE", "TAX NUMBER")
            )
            for match in _FRENCH_REGISTRATION_RE.finditer(line.upper()):
                registration_type = match.group("type").upper()
                number = re.sub(r"\D", "", match.group("number"))
                expected_length = 14 if registration_type == "SIRET" else 9
                if len(number) != expected_length or not _valid_luhn(number):
                    continue
                siren = number[:9]
                if not _valid_luhn(siren):
                    continue
                role = _party_role(context)
                candidate = {
                    "registration_type": registration_type.lower(),
                    "registration_number": number,
                    "siren": siren,
                    "siret": number if registration_type == "SIRET" else None,
                    "role": role,
                    "confidence": 0.99,
                    "validation_status": "CHECKSUM_VALID",
                    "evidence": {
                        "page": page_number,
                        "source_text": line,
                        "extraction_method": "explicit_french_registration_checksum_v1",
                    },
                }
                existing_registration = registrations_by_siren.get(siren)
                if (
                    existing_registration is None
                    or (candidate.get("siret") and not existing_registration.get("siret"))
                    or (existing_registration.get("role") == "unknown" and role != "unknown")
                ):
                    registrations_by_siren[siren] = candidate
            for match in _FR_VAT_RE.finditer(line.upper()):
                compact = re.sub(r"[^A-Z0-9]", "", match.group(1) + match.group(2))
                corrected = compact.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))
                value = "FR" + corrected
                valid = _valid_french_vat(value)
                if not labelled and not valid:
                    continue
                role = _party_role(context)
                candidate = {
                    "type": "vat",
                    "vat_number": value,
                    "country_code": "FR",
                    "role": role,
                    "confidence": 0.995 if valid and labelled else (0.97 if valid else 0.86),
                    "validation_status": "CHECKSUM_VALID" if valid else "LABEL_SUPPORTED",
                    "evidence": {
                        "page": page_number,
                        "source_text": line,
                        "extraction_method": "generic_vat_regex_checksum_v1",
                    },
                    "warnings": (["ocr_characters_normalized"] if compact != corrected else [])
                    + ([] if valid else ["french_vat_checksum_not_verified"]),
                }
                existing = tax_by_value.get(value)
                if existing is None or (existing.get("role") == "unknown" and role != "unknown"):
                    tax_by_value[value] = candidate

            if customer_order is None:
                match = _CUSTOMER_ORDER_RE.search(line)
                if match:
                    value = match.group("value").strip(" .,:;#-")
                    if any(char.isdigit() for char in value):
                        customer_order = {
                            "value": value,
                            "raw_value": value,
                            "normalized_value": value,
                            "confidence": 0.995,
                            "final_confidence": 0.995,
                            "validation_status": "SOURCE_SUPPORTED",
                            "evidence": {
                                "page": page_number,
                                "source_text": line,
                                "extraction_method": "explicit_customer_order_reference_v1",
                            },
                        }
            if request_for_quotation is None:
                value = _request_for_quotation_reference(line)
                if value:
                    request_for_quotation = {
                        "value": value,
                        "raw_value": value,
                        "normalized_value": value,
                        "confidence": 0.995,
                        "final_confidence": 0.995,
                        "validation_status": "SOURCE_SUPPORTED",
                        "evidence": {
                            "page": page_number,
                            "source_text": line,
                            "extraction_method": "explicit_request_for_quotation_reference_v1",
                        },
                    }
            if referenced_order is None and document_type not in {
                "purchase_order", "purchase_order_with_terms", "purchase_order_bundle"
            }:
                match = _REFERENCED_ORDER_RE.search(line)
                if match is None and document_type == "order_cancellation":
                    match = _CANCELLATION_REFERENCED_ORDER_RE.search(line)
                if match:
                    value = match.group("value").strip(" .,:;#-")
                    if any(char.isdigit() for char in value):
                        referenced_order = {
                            "value": value,
                            "raw_value": value,
                            "normalized_value": value,
                            "confidence": 0.995,
                            "final_confidence": 0.995,
                            "validation_status": "SOURCE_SUPPORTED",
                            "evidence": {
                                "page": page_number,
                                "source_text": line,
                                "extraction_method": "explicit_referenced_order_number_v1",
                            },
                        }

    # A valid, explicitly labelled SIREN/SIRET can deterministically corroborate
    # or derive the French VAT number. Derived values remain clearly identified as
    # such; they are never presented as text printed verbatim in the document.
    for siren, registration in registrations_by_siren.items():
        value = _vat_from_siren(siren)
        existing = tax_by_value.get(value)
        support = registration["evidence"]
        if existing:
            existing["siren"] = siren
            if registration.get("siret"):
                existing["siret"] = registration["siret"]
            existing["registration_number"] = registration["registration_number"]
            existing["confidence"] = max(float(existing.get("confidence") or 0), 0.998)
            existing["validation_status"] = "CHECKSUM_AND_REGISTRATION_MATCH"
            existing["supporting_evidence"] = support
            if existing.get("role") == "unknown" and registration.get("role") != "unknown":
                existing["role"] = registration["role"]
            continue
        tax_by_value[value] = {
            "type": "vat",
            "vat_number": value,
            "country_code": "FR",
            "role": registration["role"],
            "siren": siren,
            "siret": registration.get("siret"),
            "registration_number": registration["registration_number"],
            "confidence": 0.94 if registration.get("siret") else 0.92,
            "validation_status": "DERIVED_FROM_VALID_SIRET" if registration.get("siret") else "DERIVED_FROM_VALID_SIREN",
            "evidence": {
                **support,
                "extraction_method": "vat_derived_from_valid_french_registration_v1",
            },
            "warnings": ["vat_number_derived_from_registration_identifier_not_printed"],
        }

    if tax_by_value:
        payload["document_tax_identifiers"] = list(tax_by_value.values())
        business = payload.get("business_extractions") or {}
        purchase_order = business.get("purchase_order")
        if isinstance(purchase_order, dict):
            core_identifiers = {
                item.get("vat_number"): item
                for item in purchase_order.get("tax_identifiers") or []
                if isinstance(item, dict) and item.get("vat_number")
            }
            core_identifiers.update(tax_by_value)
            purchase_order["tax_identifiers"] = list(core_identifiers.values())
            for item in tax_by_value.values():
                role = item.get("role")
                if role not in {"buyer", "supplier"}:
                    continue
                party = purchase_order.get(role)
                if not isinstance(party, dict):
                    continue
                if not party.get("vat_number"):
                    party["vat_number"] = item["vat_number"]
                registration = item.get("siret") or item.get("siren")
                if registration and not party.get("company_registration_number"):
                    party["company_registration_number"] = registration
    document_references = payload.setdefault("document_references", {})
    if customer_order:
        document_references["customer_order_number"] = customer_order
    if request_for_quotation:
        document_references["request_for_quotation_number"] = request_for_quotation
    if referenced_order:
        document_references["referenced_order_number"] = referenced_order
    if not document_references:
        payload.pop("document_references", None)
    return payload

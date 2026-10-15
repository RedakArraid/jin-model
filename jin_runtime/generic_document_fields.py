"""Source-backed identifiers that are useful for every document type.

The core business extractor intentionally focuses on purchase orders.  Some
adjacent documents, especially order confirmations, still carry a customer
order reference and VAT identifiers that consumers need.  This module exposes
only narrowly matched values with traceable evidence.  A French VAT number may
also be derived deterministically from a checksum-valid SIREN/SIRET.
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


def _valid_french_registration(value: str) -> bool:
    """Accept only checksum-valid SIREN/SIRET values suitable for VAT derivation."""
    if not re.fullmatch(r"\d{9}|\d{14}", value):
        return False
    return _valid_luhn(value) and _valid_luhn(value[:9])


def _vat_from_siren(siren: str) -> str:
    key = (12 + 3 * (int(siren) % 97)) % 97
    return f"FR{key:02d}{siren}"


def _tax_identifier_sort_key(identifier: dict[str, Any]) -> tuple[int, float, int]:
    value = str(identifier.get("vat_number") or "")
    status = str(identifier.get("validation_status") or "")
    valid = _valid_french_vat(value)
    trusted = valid and (
        status in {"CHECKSUM_VALID", "CHECKSUM_AND_REGISTRATION_MATCH"}
        or status.startswith("DERIVED_FROM_VALID_")
    )
    confidence = float(identifier.get("confidence") or 0.0)
    resolved_role = int(identifier.get("role") in {"buyer", "supplier"})
    return (2 if trusted else 1 if valid else 0, confidence, resolved_role)


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
    buyer = any(
        term in folded
        for term in ("CLIENT", "ACHETEUR", "BUYER", "DONNEUR D ORDRE")
    )
    supplier = any(
        term in folded
        for term in ("FOURNISSEUR", "SUPPLIER", "VENDOR")
    )
    if buyer and not supplier:
        return "buyer"
    if supplier and not buyer:
        return "supplier"
    return "unknown"


def _contextual_party_role(lines: list[str], line_index: int) -> tuple[str, float, str, str]:
    """Resolve only labels printed on or before an identifier.

    A heading starting the next block must not own the preceding SIRET.  This
    matters for layouts where the customer's legal identifier is immediately
    followed by ``INFOS FOURNISSEUR``.
    """
    line = " ".join(lines[line_index].split())
    direct = _party_role(line)
    context_lines = lines[max(0, line_index - 5):line_index + 1]
    context = " ".join(" ".join(item.split()) for item in context_lines if item.strip())
    if direct != "unknown":
        return direct, 0.995, "explicit_role_on_identifier_line_v1", context
    preceding = _party_role(" ".join(context_lines[:-1]))
    if preceding != "unknown":
        return preceding, 0.94, "preceding_role_heading_v1", context
    return "unknown", 0.0, "unresolved", context


_PARTY_NAME_STOPWORDS = {
    "SAS", "SASU", "SARL", "SA", "EURL", "GMBH", "LTD", "LLC",
    "SOCIETE", "COMPANY", "FRANCE", "CEDEX", "SERVICE", "AGENCE",
}


def _party_name_match_score(name: Any, context: str) -> float:
    name_folded = re.sub(r"[^A-Z0-9]+", " ", _fold(name)).strip()
    context_folded = re.sub(r"[^A-Z0-9]+", " ", _fold(context)).strip()
    if len(name_folded) < 4 or not context_folded:
        return 0.0
    if re.search(rf"(?<![A-Z0-9]){re.escape(name_folded)}(?![A-Z0-9])", context_folded):
        return 0.99
    tokens = {
        token for token in name_folded.split()
        if len(token) >= 4 and token not in _PARTY_NAME_STOPWORDS
    }
    if not tokens:
        return 0.0
    context_tokens = set(context_folded.split())
    overlap = tokens.intersection(context_tokens)
    if len(tokens) == 1 and overlap:
        return 0.975
    if len(overlap) >= 2 and len(overlap) / len(tokens) >= 0.6:
        return 0.97
    return 0.0


def _reconcile_registration_role(
    registration: dict[str, Any],
    purchase_order: dict[str, Any],
    is_purchase_order: bool,
) -> None:
    assignment = registration.get("role_assignment") or {}
    role = str(registration.get("role") or "unknown")
    candidates: list[tuple[float, int, str, str, str]] = []
    if role in {"buyer", "supplier"}:
        method = str(assignment.get("method") or "explicit_role_context_v1")
        score = float(registration.get("role_confidence") or 0.0)
        if role == "supplier" and method == "structured_party_registration_v1":
            # The core party parser can duplicate the customer's legal entity
            # into supplier on dense purchase-order headers. A bare structured
            # placement is therefore weaker than the customer-issuer prior;
            # an explicit supplier heading remains authoritative below.
            score = min(score, 0.89)
        candidates.append((
            score,
            1,
            role,
            str(registration.get("party_role") or role),
            method,
        ))

    context = str(
        (registration.get("evidence") or {}).get("role_context")
        or (registration.get("evidence") or {}).get("source_text")
        or ""
    )
    for party_role in ("buyer", "bill_to", "ship_to", "supplier"):
        party = purchase_order.get(party_role)
        if not isinstance(party, dict):
            continue
        business_role = "supplier" if party_role == "supplier" else "buyer"
        names = (party.get("legal_name"), party.get("name"))
        score = max((_party_name_match_score(name, context) for name in names), default=0.0)
        if score:
            if business_role == "supplier":
                # An unlabelled legal footer normally belongs to the customer
                # issuing the purchase order. Supplier identity alone is kept
                # as supporting evidence but must not overrule that prior.
                score = min(score, 0.89)
            priority = 3 if business_role == "buyer" else 2
            candidates.append((score, priority, business_role, party_role, "party_name_context_match_v1"))

    if is_purchase_order:
        # A purchase order is normally issued by the customer. An unqualified
        # SIREN/SIRET in its header or legal footer therefore belongs to the
        # buyer unless explicit or identity-backed supplier evidence is stronger.
        candidates.append((0.90, 0, "buyer", "buyer", "purchase_order_issuer_default_v1"))

    if not candidates:
        registration["role"] = "unknown"
        registration["role_confidence"] = 0.0
        registration["role_assignment"] = {"method": "unresolved"}
        return

    score, _, resolved_role, party_role, method = max(candidates)
    registration["role"] = resolved_role
    registration["party_role"] = party_role
    registration["role_confidence"] = score
    registration["role_assignment"] = {
        "method": method,
        "party_role": party_role,
        "inferred": method == "purchase_order_issuer_default_v1",
        "qualification": (
            "Unqualified French registration on a purchase order is attributed "
            "to the customer issuer unless stronger supplier evidence exists."
            if method == "purchase_order_issuer_default_v1"
            else "Role supported by explicit context or party identity agreement."
        ),
    }


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
    business = payload.get("business_extractions") or {}
    purchase_order = business.get("purchase_order")
    if not isinstance(purchase_order, dict):
        purchase_order = business.get("purchase_order_with_terms")
    if not isinstance(purchase_order, dict):
        purchase_order = {}
    is_purchase_order = document_type in {
        "purchase_order", "purchase_order_with_terms", "purchase_order_bundle",
    } or bool(purchase_order)

    page_texts = _page_texts(payload)
    for page_number, page_text in page_texts:
        lines = page_text.splitlines()
        for line_index, raw_line in enumerate(lines):
            line = " ".join(raw_line.split())
            if not line:
                continue
            folded = _fold(line)
            labelled = any(
                marker in folded
                for marker in ("TVA", "VAT", "INTRACOM", "INTRA COMMUNAUTAIRE", "TAX NUMBER")
            )
            for match in _FRENCH_REGISTRATION_RE.finditer(line.upper()):
                registration_type = match.group("type").upper()
                number = re.sub(r"\D", "", match.group("number"))
                expected_length = 14 if registration_type == "SIRET" else 9
                if len(number) != expected_length or not _valid_french_registration(number):
                    continue
                siren = number[:9]
                role, role_confidence, role_method, role_context = _contextual_party_role(
                    lines, line_index
                )
                candidate = {
                    "registration_type": registration_type.lower(),
                    "registration_number": number,
                    "siren": siren,
                    "siret": number if registration_type == "SIRET" else None,
                    "role": role,
                    "role_confidence": role_confidence,
                    "role_assignment": {"method": role_method},
                    "confidence": 0.99,
                    "validation_status": "CHECKSUM_VALID",
                    "evidence": {
                        "page": page_number,
                        "source_text": line,
                        "role_context": role_context,
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
                role, role_confidence, role_method, role_context = _contextual_party_role(
                    lines, line_index
                )
                candidate = {
                    "type": "vat",
                    "vat_number": value,
                    "country_code": "FR",
                    "role": role,
                    "role_confidence": role_confidence,
                    "role_assignment": {"method": role_method},
                    "confidence": 0.995 if valid and labelled else (0.97 if valid else 0.86),
                    "validation_status": "CHECKSUM_VALID" if valid else "LABEL_SUPPORTED",
                    "evidence": {
                        "page": page_number,
                        "source_text": line,
                        "role_context": role_context,
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

    # The core extractor can already have resolved an explicitly labelled
    # company registration even when the generic text stream split the label
    # and number across visual rows.  Accept that structured value only from a
    # resolved buyer/supplier role and only after both the SIREN and the full
    # SIRET (when present) pass their checksums.
    if isinstance(purchase_order, dict):
        for role in ("buyer", "supplier"):
            party = purchase_order.get(role)
            if not isinstance(party, dict):
                continue
            raw_registration = party.get("company_registration_number")
            number = re.sub(r"\D", "", str(raw_registration or ""))
            if not _valid_french_registration(number):
                continue
            siren = number[:9]
            registration_type = "SIRET" if len(number) == 14 else "SIREN"
            candidate = {
                "registration_type": registration_type.lower(),
                "registration_number": number,
                "siren": siren,
                "siret": number if registration_type == "SIRET" else None,
                "role": role,
                "party_role": role,
                "role_confidence": 0.995 if role == "buyer" else 0.89,
                "role_assignment": {"method": "structured_party_registration_v1"},
                "confidence": 0.98,
                "validation_status": "CHECKSUM_VALID",
                "evidence": {
                    "source_text": str(raw_registration),
                    "extraction_method": "structured_party_registration_checksum_v2",
                },
            }
            existing_registration = registrations_by_siren.get(siren)
            if existing_registration is None:
                registrations_by_siren[siren] = candidate
            else:
                if candidate.get("siret") and not existing_registration.get("siret"):
                    existing_registration["siret"] = candidate["siret"]
                    existing_registration["registration_number"] = candidate["registration_number"]
                    existing_registration["registration_type"] = "siret"
                if (
                    existing_registration.get("role") == "unknown"
                    or float(candidate["role_confidence"])
                    > float(existing_registration.get("role_confidence") or 0)
                ):
                    existing_registration["role"] = role
                    existing_registration["party_role"] = role
                    existing_registration["role_confidence"] = candidate["role_confidence"]
                    existing_registration["role_assignment"] = candidate["role_assignment"]

    for registration in registrations_by_siren.values():
        _reconcile_registration_role(registration, purchase_order, is_purchase_order)

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
            if (
                existing.get("role") == "unknown"
                or float(registration.get("role_confidence") or 0)
                > float(existing.get("role_confidence") or 0)
            ):
                existing["role"] = registration["role"]
                existing["party_role"] = registration.get("party_role")
                existing["role_confidence"] = registration.get("role_confidence")
                existing["role_assignment"] = registration.get("role_assignment")
            continue
        derivation_confidence = min(0.995, float(registration.get("confidence") or 0.0))
        tax_by_value[value] = {
            "type": "vat",
            "vat_number": value,
            "country_code": "FR",
            "role": registration["role"],
            "party_role": registration.get("party_role"),
            "role_confidence": registration.get("role_confidence"),
            "role_assignment": registration.get("role_assignment"),
            "siren": siren,
            "siret": registration.get("siret"),
            "registration_number": registration["registration_number"],
            # The formula is deterministic.  Its confidence is therefore the
            # confidence of the checksum-valid source registration itself,
            # rather than an arbitrary penalty for not printing the VAT value.
            "confidence": derivation_confidence,
            "validation_status": "DERIVED_FROM_VALID_SIRET" if registration.get("siret") else "DERIVED_FROM_VALID_SIREN",
            "derived": True,
            "calculation": {
                "algorithm": "french_vat_key_from_siren_v1",
                "formula": "(12 + 3 * (SIREN mod 97)) mod 97",
            },
            "evidence": {
                **support,
                "extraction_method": "vat_derived_from_valid_french_registration_v1",
            },
            "warnings": [
                "vat_number_derived_from_registration_identifier_not_printed",
                *(
                    ["vat_role_inferred_as_customer_purchase_order_issuer"]
                    if (registration.get("role_assignment") or {}).get("inferred")
                    else []
                ),
            ],
        }

    if tax_by_value:
        ordered_identifiers = sorted(
            tax_by_value.values(),
            key=_tax_identifier_sort_key,
            reverse=True,
        )
        payload["document_tax_identifiers"] = ordered_identifiers
        if isinstance(purchase_order, dict):
            core_identifiers = {
                item.get("vat_number"): item
                for item in purchase_order.get("tax_identifiers") or []
                if isinstance(item, dict) and item.get("vat_number")
            }
            core_identifiers.update(tax_by_value)
            purchase_order["tax_identifiers"] = sorted(
                core_identifiers.values(),
                key=_tax_identifier_sort_key,
                reverse=True,
            )
            for item in ordered_identifiers:
                role = item.get("role")
                if role not in {"buyer", "supplier"}:
                    continue
                party_role = item.get("party_role") or role
                party = purchase_order.get(party_role)
                if not isinstance(party, dict):
                    continue
                current_vat = re.sub(
                    r"[^A-Z0-9]", "", str(party.get("vat_number") or "").upper()
                )
                if not _valid_french_vat(current_vat):
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

"""Aggregate extraction checks without treating a confidence score as proof."""
from __future__ import annotations

from typing import Any
import re

from jin_runtime.output_quality import _find_purchase_order, _money
from jin_runtime.order_numbers import check_order_number


def _value(field: Any) -> Any:
    return field.get("value") if isinstance(field, dict) else field


def apply_extraction_gate(payload: dict[str, Any]) -> dict[str, Any]:
    reasons: list[dict[str, str]] = []

    def require(condition: bool, code: str, path: str) -> None:
        if not condition:
            reasons.append({"code": code, "path": path})

    quality = payload.setdefault("quality", {})
    document = payload.get("document") or {}
    doc_type = document.get("primary_document_type") or document.get("detected_document_type")
    po = _find_purchase_order(payload)
    is_order = doc_type in {"purchase_order", "purchase_order_with_terms"} or bool(po)
    if quality.get("requires_human_review"):
        reasons.append({"code": "CORE_REVIEW_REQUIRED", "path": "quality"})
    for warning in payload.get("runtime_warnings") or []:
        reasons.append({"code": "ENRICHMENT_UNAVAILABLE", "path": str(warning.get("component"))})
    for issue in (payload.get("output_quality") or {}).get("issues", []):
        if issue.get("severity") in {"error", "warning"}:
            # Duplicated formatting was repaired; it need not block the document.
            if issue.get("code") != "ADDRESS_DUPLICATE_CITY":
                reasons.append({"code": str(issue.get("code")), "path": str(issue.get("path"))})
    suggestions = payload.get("weak_field_suggestions") or {}
    for conflict in suggestions.get("field_conflicts") or []:
        reasons.append({"code": "FIELD_CANDIDATES_DISAGREE", "path": str(conflict.get("field"))})

    if is_order:
        grouped_references = [
            item for item in payload.get("customer_order_references") or []
            if isinstance(item, dict) and item.get("value")
        ]
        if len(grouped_references) >= 2:
            number_check = {
                "value": None,
                "values": [item["value"] for item in grouped_references],
                "status": "MULTIPLE_SOURCE_SUPPORTED",
                "issues": [],
                "corroborating_candidates": grouped_references,
                "qualification": "Explicit customer order references from a grouped order; no primary value inferred.",
            }
        else:
            number_check = check_order_number(po, suggestions)
        payload["order_number_check"] = number_check
        for code in number_check["issues"]:
            reasons.append({"code": code, "path": "purchase_order.number"})
        require(bool(po), "ORDER_EXTRACTION_MISSING", "business_extractions.purchase_order")
        header = po.get("purchase_order") or po
        require(
            bool(_value(header.get("number") or header.get("order_number")))
            or len(grouped_references) >= 2,
            "ORDER_NUMBER_MISSING",
            "purchase_order.number",
        )
        require(bool(_value(header.get("order_date"))), "ORDER_DATE_MISSING", "purchase_order.order_date")
        require(_money((po.get("totals") or {}).get("total_net")) is not None,
                "ORDER_TOTAL_MISSING", "totals.total_net")
        for role in ("buyer", "supplier"):
            party = po.get(role) or {}
            name = party.get("name") or party.get("legal_name")
            require(bool(name), "PARTY_NAME_MISSING", role)
            if name:
                normalized = re.sub(r"[^A-Z0-9]", "", str(name).upper())
                require(normalized not in {"SAS", "SASU", "SARL", "SA", "EURL", "GMBH", "LTD", "LLC"},
                        "PARTY_NAME_UNINFORMATIVE", role)
        contact_collision = (
            ((po.get("validation") or {}).get("checks") or {})
            .get("purchase_order_contact_collision")
        )
        if isinstance(contact_collision, dict) and isinstance(
            contact_collision.get("valid"), bool
        ):
            require(
                contact_collision["valid"],
                "PARTY_CONTACT_ROLE_CONFLICT",
                "buyer/supplier.contact",
            )
        else:
            # Compatibility for older core payloads without the dedicated
            # collision validator. Prefer direct role fields; nested contacts
            # and address contacts may contain shared supplier/service numbers.
            for contact_field in ("email", "phone"):
                values = []
                for role in ("buyer", "supplier"):
                    party = po.get(role) or {}
                    direct = party.get(contact_field)
                    if direct not in (None, ""):
                        candidates = [direct]
                    else:
                        # Older payloads often expose the only role contact in
                        # the nested object/address.  Use those solely as a
                        # fallback so they cannot overrule an explicit direct
                        # role value.
                        candidates = [(party.get("contact") or {}).get(contact_field)]
                        candidates.extend(
                            address.get(f"contact_{contact_field}")
                            for address in po.get("business_addresses") or []
                            if address.get("role") == role
                        )
                    normalized = {
                        re.sub(r"\D", "", str(value))
                        if contact_field == "phone"
                        else str(value or "").strip().casefold()
                        for value in candidates
                        if value is not None
                    } - {""}
                    values.append(normalized)
                require(
                    not values[0].intersection(values[1]),
                    "PARTY_CONTACT_ROLE_CONFLICT",
                    f"buyer/supplier.{contact_field}",
                )
        lines = po.get("lines") or []
        require(bool(lines), "ORDER_LINES_MISSING", "lines")
        for index, line in enumerate(lines):
            path = f"lines[{index}]"
            reference = next((line.get(key) for key in (
                "material_number", "supplier_material_number", "manufacturer_part_number",
                "customer_material_number", "sku", "ean", "gtin",
            ) if line.get(key)), None)
            require(bool(reference), "PRODUCT_REFERENCE_MISSING", path)
            require(_money(line.get("quantity")) is not None, "QUANTITY_MISSING", path)
            require(any(_money(line.get(key)) is not None for key in ("net_unit_price", "unit_price")), "UNIT_PRICE_MISSING", path)
            require(_money(line.get("line_total")) is not None, "LINE_TOTAL_MISSING", path)
        validation = po.get("validation") or {}
        require(validation.get("status") == "PASS", "BUSINESS_VALIDATION_INCOMPLETE", "validation")
        if po.get("requires_human_review"):
            reasons.append({"code": "BUSINESS_REVIEW_REQUIRED", "path": "purchase_order"})
        addresses = po.get("business_addresses") or []
        roles = {str(address.get("role") or "").lower() for address in addresses}
        # These roles are required for unattended order handling, not evidence
        # that every source document necessarily contains both addresses.
        for required_role in ("ship_to", "bill_to"):
            require(required_role in roles, "ADDRESS_ROLE_MISSING", f"business_addresses.{required_role}")
        delivery_addresses = [address for address in addresses
                              if str(address.get("role") or "").lower() == "ship_to"]
        if len(delivery_addresses) > 1:
            reasons.append({"code": "DELIVERY_ADDRESS_AMBIGUOUS", "path": "business_addresses.ship_to"})
        elif len(delivery_addresses) == 1:
            clean_delivery = delivery_addresses[0].get("clean_address") or {}
            if clean_delivery.get("status") == "INCOMPLETE":
                reasons.append({"code": "DELIVERY_ADDRESS_INCOMPLETE", "path": "business_addresses.ship_to"})
        for index, address in enumerate(addresses):
            path = f"business_addresses[{index}]"
            role = str(address.get("role") or "").lower()
            require(bool(role) and role not in {"unknown", "unassigned", "other"}, "ADDRESS_ROLE_UNRESOLVED", path)
            confidence = address.get("role_confidence")
            if isinstance(confidence, (int, float)):
                require(confidence >= .80, "ADDRESS_ROLE_UNCERTAIN", path)
            components = address.get("address") or {}
            require(bool(components.get("postal_code") and components.get("city")), "ADDRESS_LOCALITY_INCOMPLETE", path)
            reference = address.get("ban_verification") or {}
            reference_status = reference.get("status")
            if reference_status in {"DEPARTMENT_NOT_INDEXED"}:
                reasons.append({"code": "ADDRESS_REFERENCE_UNAVAILABLE", "path": path})
            elif reference_status in {"NOT_FOUND"}:
                reasons.append({"code": "ADDRESS_NOT_FOUND_IN_REFERENCE", "path": path})
            elif reference_status in {"CLOSE_STREET_MATCH"}:
                reasons.append({"code": "ADDRESS_REFERENCE_SUGGESTION", "path": path})
            elif reference_status in {"STREET_MATCH_NUMBER_NOT_FOUND"}:
                reasons.append({"code": "ADDRESS_NUMBER_NOT_FOUND_IN_REFERENCE", "path": path})
    else:
        # A successful HTTP response or a clean arithmetic audit is not proof of
        # a supported, complete purchase-order extraction.
        reasons.append({"code": "NOT_A_STRUCTURED_PURCHASE_ORDER", "path": "document"})

    unique = list({(reason["code"], reason["path"]): reason for reason in reasons}.values())
    review = bool(unique)
    quality["requires_human_review"] = review
    if po and review:
        po["requires_human_review"] = True
    payload["extraction_decision"] = {
        "status": "REVIEW_REQUIRED" if review else "CHECKS_PASSED",
        "requires_review": review,
        "reasons": unique,
        "policy_version": "cpu-quality-gate-v1",
        "qualification": "Rule-based checks, not a guarantee of factual accuracy; validate against reviewed documents before unattended use.",
    }
    return payload

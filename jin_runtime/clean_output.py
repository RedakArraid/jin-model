"""Stable, consumer-oriented JSON view of the verbose core extraction."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jin_runtime import __version__
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
    return _compact({key: nested.get(key) or source.get(key) for key in CONTACT_FIELDS})


def _address_components(source: dict[str, Any]) -> dict[str, Any]:
    nested = source.get("address") if isinstance(source.get("address"), dict) else source
    return _compact({key: nested.get(key) for key in ADDRESS_FIELDS})


def _party(source: dict[str, Any]) -> dict[str, Any]:
    return _compact({
        "id": source.get("id"), "code": source.get("code"),
        "customer_agency_code": source.get("customer_agency_code"),
        "name": source.get("name"), "legal_name": source.get("legal_name"),
        "department": source.get("department"), "contact": _contact(source),
        "address": _address_components(source),
        "vat_number": source.get("vat_number"),
        "company_registration_number": source.get("company_registration_number"),
    })


def _business_address(source: dict[str, Any]) -> dict[str, Any]:
    verification = source.get("address_verification") or source.get("ban_verification") or {}
    clean_address = source.get("clean_address") if isinstance(source.get("clean_address"), dict) else {}
    role = source.get("role")
    source_id = source.get("address_id")
    # One physical address may legitimately be reused for several business
    # roles.  Keep each JSON object independently addressable for consumers.
    clean_id = f"{source_id}:{role}" if source_id and role else source_id
    return _compact({
        "id": clean_id, "role": role,
        "role_label": source.get("role_label"), "party_name": source.get("party_name"),
        "party_code": source.get("party_code"), "department": source.get("department"),
        "customer_agency_code": source.get("customer_agency_code"),
        "contact": {
            "name": source.get("contact_name"), "email": source.get("contact_email"),
            "phone": source.get("contact_phone"),
        },
        "formatted": clean_address.get("one_line") or source.get("formatted_address"),
        "formatted_lines": clean_address.get("lines"),
        "source_formatted": clean_address.get("source_formatted")
                            if clean_address.get("source_formatted") != clean_address.get("one_line") else None,
        "components": clean_address.get("components") or _address_components(source),
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

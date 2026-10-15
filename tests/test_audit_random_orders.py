from scripts.audit_random_orders import _automatic_checks, _extract_model


def test_extract_model_reads_public_delivery_value_contract():
    payload = {
        "document": {"primary_document_type": "purchase_order"},
        "normalized_output": {
            "order": {
                "delivery_address": {
                    "normalized_value": (
                        "REXEL-CENTRE LOGISTIQUE MEUNG/LOIRE\n"
                        "1ERE AVENUE\n"
                        "ZAC SYNERGIE VAL DE LOIRE\n"
                        "45130 MEUNG-SUR-LOIRE\n"
                        "FRANCE"
                    ),
                    "value": (
                        "1ERE AVENUE, ZAC SYNERGIE VAL DE LOIRE, "
                        "45130 MEUNG-SUR-LOIRE, France"
                    ),
                    "components": {"street": "1ERE AVENUE"},
                }
            }
        },
    }

    model = _extract_model(payload)

    assert model["delivery_address"].startswith("REXEL-CENTRE")
    assert model["delivery_source_value"].startswith("1ERE AVENUE")
    assert model["delivery_lines"] == [
        "REXEL-CENTRE LOGISTIQUE MEUNG/LOIRE",
        "1ERE AVENUE",
        "ZAC SYNERGIE VAL DE LOIRE",
        "45130 MEUNG-SUR-LOIRE",
        "FRANCE",
    ]


def test_extract_model_keeps_legacy_delivery_fields_compatible():
    payload = {
        "document": {"primary_document_type": "purchase_order"},
        "normalized_output": {
            "order": {
                "delivery_address": {
                    "formatted": "PIECES EXPRESS, 1 RUE PHILIPPE LEBON",
                    "formatted_lines": ["PIECES EXPRESS", "1 RUE PHILIPPE LEBON"],
                }
            }
        },
    }

    model = _extract_model(payload)

    assert model["delivery_address"] == "PIECES EXPRESS, 1 RUE PHILIPPE LEBON"
    assert model["delivery_source_value"] is None
    assert model["delivery_lines"] == ["PIECES EXPRESS", "1 RUE PHILIPPE LEBON"]


def test_extract_model_keeps_article_reference_confidence_for_batch_audits():
    payload = {
        "document": {"primary_document_type": "purchase_order"},
        "normalized_output": {
            "order": {
                "line_items": [
                    {
                        "line_number": "1",
                        "references": {"supplier_material_number": "8719999999"},
                        "description": "Produit",
                        "quantity": 1,
                        "confidence": 0.98,
                        "reference_confidence": 1.0,
                        "reference_status": "SUPPORTED_DOMINANT_NUMERIC",
                    },
                    {
                        "line_number": "2",
                        "references": {"article_number": "CARTER DU BLOC"},
                        "description": "CARTER DU BLOC",
                        "quantity": 1,
                        "confidence": 0.35,
                        "reference_confidence": 0.0,
                        "reference_status": "TEXT_CAPTURE",
                        "warnings": ["material_reference_text_capture"],
                    },
                ]
            }
        },
    }

    model = _extract_model(payload)

    assert model["line_count"] == 2
    assert model["line_reference_status_counts"] == {
        "SUPPORTED_DOMINANT_NUMERIC": 1,
        "TEXT_CAPTURE": 1,
    }
    assert model["line_reference_mean_confidence"] == 0.5
    assert model["line_reference_severe_count"] == 1
    assert model["lines"][1]["reference"] == "CARTER DU BLOC"


def test_extract_model_keeps_order_and_line_requested_delivery_dates():
    payload = {
        "document": {"primary_document_type": "purchase_order"},
        "normalized_output": {"order": {
            "requested_delivery_date": {
                "value": "2026-03-03",
                "confidence": 0.96,
                "evidence": {"source_text": "03/03/2026"},
            },
            "line_items": [
                {
                    "line_number": "1",
                    "references": {"article_number": "7716701550"},
                    "delivery": {"date": "2026-03-03"},
                },
                {
                    "line_number": "2",
                    "references": {"article_number": "7716705075"},
                    "delivery": {"date": "2026-03-04"},
                },
            ],
        }},
    }

    model = _extract_model(payload)

    assert model["requested_delivery_date"] == "2026-03-03"
    assert model["requested_delivery_date_score"] == 0.96
    assert model["line_requested_delivery_dates"] == ["2026-03-03", "2026-03-04"]
    assert model["lines"][0]["requested_delivery_date"] == "2026-03-03"


def test_checksum_valid_derived_vat_outranks_invalid_ocr_and_passes_audit():
    payload = {
        "document": {"primary_document_type": "purchase_order"},
        "business_extractions": {
            "purchase_order": {
                "tax_identifiers": [{
                    "type": "vat",
                    "vat_number": "FR0K352747018",
                    "confidence": 0.86,
                    "validation_status": "LABEL_SUPPORTED",
                    "evidence": {
                        "source_text": "TVA FR 0K 352 747 018",
                        "extraction_method": "generic_vat_regex_checksum_v1",
                    },
                }],
            },
        },
        "document_tax_identifiers": [{
            "type": "vat",
            "vat_number": "FR69352747018",
            "confidence": 0.98,
            "validation_status": "DERIVED_FROM_VALID_SIRET",
            "derived": True,
            "siren": "352747018",
            "siret": "35274701800013",
            "registration_number": "35274701800013",
            "role": "buyer",
            "party_role": "bill_to",
            "role_confidence": 0.99,
            "role_assignment": {"method": "party_name_context_match_v1"},
            "evidence": {
                "source_text": "SIRET 352 747 018 00013",
                "extraction_method": "vat_derived_from_valid_french_registration_v1",
            },
        }],
    }

    model = _extract_model(payload)

    assert model["vat_number"] == "FR69352747018"
    assert model["vat_validation_status"] == "DERIVED_FROM_VALID_SIRET"
    assert model["vat_derived"] is True
    assert model["vat_role"] == "buyer"
    assert model["vat_party_role"] == "bill_to"
    assert model["vat_role_confidence"] == 0.99
    assert model["vat_role_assignment"]["method"] == "party_name_context_match_v1"
    checks = _automatic_checks(model, "TVA FR 0K 352 747 018", ["FR0K352747018"])
    assert checks["vat"] == "BON_CALCULEE_SIRET"

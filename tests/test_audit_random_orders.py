from scripts.audit_random_orders import _extract_model


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

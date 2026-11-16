"""Single-spaced consumer values must remain distinct from verbatim evidence."""
import copy

import pytest

from jin_runtime.clean_output import _field, _normalize_extracted_spacing, build_clean_output


@pytest.mark.parametrize("raw,expected", [
    ("dse  g idn  47", "dse g idn 47"),
    ("  dse\t g\u00a0\u00a0idn\u202f 47  ", "dse g idn 47"),
    ("ST C CSP 33443", "ST C CSP 33443"),
    ("5889382/ 8742", "5889382/ 8742"),
    ("5889382/   8742", "5889382/ 8742"),
    ("5889382 / 8742", "5889382 / 8742"),
    ("5889382/8742", "5889382/8742"),
    ("joint\n  torique", "joint\n torique"),
    (0, 0), (False, False),
])
def test_field_normalization_retains_raw_and_source_values(raw, expected):
    evidence = {"source_text": raw, "page": 1, "bbox": [10, 20, 100, 40]}
    field = {"value": raw, "raw_value": raw, "evidence": evidence, "final_confidence": 0.98}
    original = copy.deepcopy(field)
    result = _field(field)
    assert result["value"] == expected
    assert result["evidence"] == evidence
    assert result.get("raw_value", expected) == raw
    assert result["confidence"] == 0.98
    assert field == original
    assert _normalize_extracted_spacing(result) == result


def test_clean_output_normalizes_all_business_values_without_changing_audit_text():
    raw = "dse  g idn  47"
    source_text = "COMMANDE  dse  g idn  47\n  DEUXIEME LIGNE"
    evidence = {"source_text": source_text, "page": 1, "bbox": [10, 20, 100, 40]}
    payload = {
        "document": {"primary_document_type": "purchase_order", "filename": "document  original.pdf"},
        "document_references": {"referenced_order_number": {"value": raw, "evidence": evidence}},
        "order_number_check": {"value": raw, "source_evidence": evidence, "issues": []},
        "business_extractions": {"purchase_order": {
            "purchase_order": {"number": {"value": raw, "evidence": evidence}},
            "buyer": {"name": "SOCIETE  TEST"},
            "lines": [{
                "line_number": 1, "material_number": "AA  00123", "quantity": 0,
                "description": "JOINT\u00a0\u00a0 TORIQUE", "evidence": evidence,
            }],
            "logistics": {"delivery_location": " DEPOT  EST ", "source_text": source_text},
            "additional_charges": [{"type": "shipping", "description": "Frais  de port", "amount": 0}],
        }},
    }
    original = copy.deepcopy(payload)
    out = build_clean_output(payload)
    number = out["order"]["customer_order_number"]
    assert number["value"] == "dse g idn 47"
    assert number["raw_value"] == raw
    assert number["evidence"]["source_text"] == source_text
    assert out["document"]["filename"] == "document  original.pdf"
    assert out["document"]["references"]["referenced_order_number"]["value"] == "dse g idn 47"
    assert out["quality"]["order_number"]["value"] == "dse g idn 47"
    assert out["quality"]["order_number"]["source_evidence"] == evidence
    line = out["order"]["line_items"][0]
    assert line["references"]["material_number"] == "AA 00123"
    assert line["description"] == "JOINT TORIQUE"
    assert line["quantity"] == 0
    assert out["order"]["logistics"]["delivery_location"] == "DEPOT EST"
    assert out["order"]["logistics"]["source_text"] == source_text
    assert out["order"]["additional_charges"][0]["description"] == "Frais de port"
    assert payload == original


def test_nested_address_values_keep_raw_geometry_and_formatted_source():
    value = {"selected": {
        "value": "12  RUE  ISAAC NEWTON",
        "formatted_lines": [" DEPOT  EST ", "12  RUE ISAAC NEWTON"],
        "components": {"street": "RUE  ISAAC NEWTON"},
        "raw_value": "12  RUE  ISAAC NEWTON",
        "source_formatted": "DEPOT  EST\n12  RUE ISAAC NEWTON",
        "evidence": {"source_text": "12  RUE  ISAAC NEWTON", "bbox": [1, 2, 30, 40]},
    }}
    result = _normalize_extracted_spacing(value)["selected"]
    assert result["value"] == "12 RUE ISAAC NEWTON"
    assert result["formatted_lines"] == ["DEPOT EST", "12 RUE ISAAC NEWTON"]
    assert result["components"]["street"] == "RUE ISAAC NEWTON"
    for key in ("raw_value", "source_formatted", "evidence"):
        assert result[key] == value["selected"][key]

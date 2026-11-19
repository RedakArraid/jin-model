import copy

import fitz
import pytest

from jin_runtime.geometry_fields import extract_geometry_suggestions
from jin_runtime.weak_field_router import _extract_first_page_lines
from jin_runtime.weak_field_reconciliation import _candidate_matches_block, reconcile_weak_fields


def test_comma_locality_keeps_complete_core_delivery_and_its_evidence():
    with fitz.open() as pdf:
        page = pdf.new_page(width=792, height=612)
        for y, text in [(15, "Adresse de livraison"), (30, "CLIENT DEPOT"),
                        (42, "10 RUE ALPHA"), (54, "Strasbourg, 67100"), (66, "FRANCE"),
                        (125, "FOURNISSEUR"), (138, "126 RUE BETA"), (151, "93700 Drancy")]:
            page.insert_text((520, y), text, fontsize=8)
        lines, _ = _extract_first_page_lines(pdf.tobytes(), "fra+eng+deu")
    suggestions = extract_geometry_suggestions(lines)
    ships = [item for item in suggestions["address_candidates"] if item["role"] == "ship_to"]
    assert any(item["components"].get("postal_code") == "67100"
               and item["components"].get("city") == "Strasbourg" for item in ships)
    delivery = {"role": "ship_to", "party_name": "CLIENT DEPOT", "address_id": "delivery",
                "address": {"house_number": "10", "street": "RUE ALPHA", "postal_code": "67100",
                            "city": "Strasbourg", "country": "France"},
                "evidence": {"source_text": "10 RUE ALPHA, Strasbourg, 67100"}}
    supplier = {"role": "supplier", "party_name": "FOURNISSEUR", "address_id": "vendor",
                "address": {"house_number": "126", "street": "RUE BETA", "postal_code": "93700", "city": "Drancy"}}
    original = copy.deepcopy(delivery)
    payload = {"business_extractions": {"purchase_order": {"business_addresses": [delivery, supplier]}},
               "weak_field_suggestions": suggestions}
    reconcile_weak_fields(payload)
    assert payload["business_extractions"]["purchase_order"]["business_addresses"][0] == original
    assert not payload["weak_field_reconciliation"]["actions"]


@pytest.mark.parametrize("conflict", [
    {"postal_code": "75001"}, {"house_number": "126"}, {"city": "Paris"}, {"street": "RUE BETA"},
])
def test_company_agreement_cannot_override_conflicting_address(conflict):
    address = {"house_number": "10", "street": "RUE ALPHA", "postal_code": "67100", "city": "Strasbourg"}
    candidate = {"party_name": "CLIENT", "components": {**address, **conflict}}
    assert not _candidate_matches_block(candidate, {"party_name": "CLIENT SAS", "address": address})


def test_partial_geometry_still_matches_compatible_street():
    assert _candidate_matches_block(
        {"party_name": "CLIENT", "components": {"street": "RUE ALPHA", "house_number": "10"}},
        {"party_name": "CLIENT SAS", "address": {"street": "RUE ALPHA", "house_number": "10", "postal_code": "67100"}},
    )

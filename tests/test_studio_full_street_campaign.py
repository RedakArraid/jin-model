import fitz

from scripts.studio_full_street_campaign import update_fields


def _pdf() -> fitz.Document:
    pdf = fitz.open()
    page = pdf.new_page(width=600, height=800)
    page.insert_text((60, 100), "12 RUE ISAAC NEWTON")
    page.insert_text((60, 300), "34 RUE DES ECOLES")
    return pdf


def test_merges_only_locally_sourced_street_and_removes_atomic_components():
    with _pdf() as pdf:
        fields = [
            {"path": "order.addresses.ship_to.selected.components.house_number", "label": "SHIP_TO_HOUSE_NUMBER", "status": "UNREVIEWED", "extracted_value": "12", "evidence": {"page": 1, "bbox": [100, 110, 130, 130]}},
            {"path": "order.addresses.ship_to.selected.components.street", "label": "SHIP_TO_STREET", "status": "UNREVIEWED", "extracted_value": "RUE ISAAC NEWTON", "evidence": {"page": 1, "bbox": [130, 110, 400, 130]}},
        ]
        merged, roles = update_fields(fields, pdf)
    assert roles == ["SHIP_TO"]
    assert [(field["label"], field.get("corrected_value")) for field in merged] == [
        ("SHIP_TO_STREET", "12 RUE ISAAC NEWTON")
    ]


def test_does_not_merge_street_from_a_different_source_line():
    with _pdf() as pdf:
        fields = [
            {"path": "order.addresses.ship_to.selected.components.house_number", "label": "SHIP_TO_HOUSE_NUMBER", "status": "UNREVIEWED", "extracted_value": "34", "evidence": {"page": 1, "bbox": [100, 110, 130, 130]}},
            {"path": "order.addresses.ship_to.selected.components.street", "label": "SHIP_TO_STREET", "status": "UNREVIEWED", "extracted_value": "RUE DES ECOLES", "evidence": {"page": 1, "bbox": [130, 110, 400, 130]}},
        ]
        merged, roles = update_fields(fields, pdf)
    assert roles == []
    assert merged == fields

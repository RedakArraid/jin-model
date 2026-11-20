from jin_runtime.clean_output import build_clean_output, _source_labeled_contact


def test_buyer_contact_does_not_swallow_supplier_phone_from_neighboring_column():
    person = "Suivi par : Gregory EXEMPLE - Responsable Commercial"
    supplier_details = "08-20-00-30-00 commandes@fournisseur.fr / 08-20-00-40-00"
    payload = {
        "document": {"primary_document_type": "purchase_order"},
        "pages": [{"page": 1, "text": person + " " + supplier_details}],
        "blocks": [
            {"text": person, "source": {"page": 1, "bbox": [40, 200, 230, 215]}, "metadata": {"compartmentalized": True}},
            {"text": supplier_details, "source": {"page": 1, "bbox": [320, 200, 550, 215]}, "metadata": {"compartmentalized": True}},
        ],
        "business_extractions": {"purchase_order": {
            "buyer": {"name": "CLIENT EXEMPLE", "phone": "01-48-92-08-95", "contact": {"phone": "01-48-92-08-95"}},
            "supplier": {"name": "FOURNISSEUR EXEMPLE"},
        }},
    }
    clean = build_clean_output(payload)
    contact = clean["order"]["parties"]["buyer"]["contact"]
    assert contact["name"] == "Gregory EXEMPLE - Responsable Commercial"
    assert contact["phone"] == "01-48-92-08-95"
    assert "email" not in contact


def test_inline_phone_stays_contact_data_not_part_of_person_name():
    contact = _source_labeled_contact("Contact : Gregory EXEMPLE 01-48-92-08-95")
    assert contact["name"] == "Gregory EXEMPLE"
    assert contact["phone"] == "01-48-92-08-95"

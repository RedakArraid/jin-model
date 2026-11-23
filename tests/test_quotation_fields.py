"""Supplier offers have their own contract, never a disguised purchase order."""
from __future__ import annotations

import pymupdf

from jin_runtime.clean_output import build_clean_output, clean_output_schema
from jin_runtime.extraction_gate import apply_extraction_gate
from jin_runtime.quotation_fields import extract_quotation


def _write(page, x, y, text):
    page.insert_text((x, y), text, fontsize=9)


def _offer_pdf(*, incomplete=False, two_page_line=False, locality_only=False):
    doc = pymupdf.open()
    first = doc.new_page(width=595, height=842)
    _write(first, 55, 45, "elm.leblanc SAS")
    _write(first, 55, 60, "124, 126 rue de Stalingrad")
    _write(first, 55, 75, "F-93711 Drancy Cedex")
    _write(first, 245, 100, "Offre de prix")
    _write(first, 55, 165, "Offre redigee par : Camille Test")
    _write(first, 55, 150, "Contact commercial : Camille Vente")
    _write(first, 55, 180, "Tel. : 06 12 34 56 78")
    _write(first, 55, 195, "E-mail : camille@example.test")
    _write(first, 325, 165, "Societe : CLIENT TEST")
    _write(first, 325, 180, "A l'attention de : Lea Client")
    _write(first, 325, 195, "Adresse :" if locality_only else "Adresse : 24 rue Exemple")
    _write(first, 325, 210, "84000 AVIGNON")
    _write(first, 55, 250, "Affaire / projet : Projet Test")
    _write(first, 55, 265, "Offre n° : OF-2026-42")
    _write(first, 55, 280, "Date de l'offre : 19/10/2026")
    _write(first, 55, 295, "Offre valable jusqu'au : 31/12/2026")
    _write(first, 55, 440, "Informations specifiques a votre projet :")
    _write(first, 55, 460, "INSTALLATEUR : INSTALLATEUR TEST")

    second = doc.new_page(width=595, height=842)
    _write(second, 170, 100, "Equipements")
    _write(second, 55, 115, "Code article")
    _write(second, 180, 115, "Designation")
    if two_page_line:
        _write(second, 55, 690, "7716704737")
        _write(second, 115, 690, "Produit test sur deux pages")
        page = doc.new_page(width=595, height=842)
        _write(page, 55, 48, "elm.leblanc SAS")
        _write(page, 55, 60, "124, 126 rue de Stalingrad")
        _write(page, 55, 70, "7716704737")
        _write(page, 115, 70, "suite du produit")
        _write(page, 55, 82, "7716704738")
        _write(page, 115, 82, "Autre produit")
        _write(page, 340, 82, "100,00")
        _write(page, 395, 82, "2")
        _write(page, 425, 82, "1,50")
        _write(page, 460, 82, "200,00")
        _write(page, 340, 94, "50,00")
        _write(page, 395, 94, "1")
        _write(page, 425, 94, "0,50")
        _write(page, 460, 94, "50,00")
        summary_page = page
        summary_y = 145
        subtotal = "250,00"
        eco = "3,50"
        final = "253,50"
    else:
        _write(second, 55, 170, "7716704737")
        _write(second, 115, 170, "Produit test")
        if not incomplete:
            _write(second, 340, 170, "100,00")
            _write(second, 395, 170, "2")
            _write(second, 425, 170, "1,50")
            _write(second, 460, 170, "200,00")
        summary_page = second
        summary_y = 220
        subtotal = "200,00"
        eco = "3,00"
        final = "203,00"
    _write(summary_page, 390, summary_y, "Sous-total HT")
    _write(summary_page, 460, summary_y, subtotal)
    _write(summary_page, 170, summary_y + 40, "Prix Total net hors taxes et hors eco-participation")
    _write(summary_page, 450, summary_y + 40, subtotal)
    _write(summary_page, 170, summary_y + 55, "Total eco-participation net hors taxes")
    _write(summary_page, 450, summary_y + 55, eco)
    _write(summary_page, 170, summary_y + 70, "PRIX TOTAL NET (hors taxes)")
    _write(summary_page, 450, summary_y + 70, final)
    _write(summary_page, 55, summary_y + 105, "Date de demarrage du Marche (ZFNG) : 19/10/2026")
    _write(summary_page, 55, summary_y + 120, "Date de fin du Marche : 31/12/2026")

    terms = doc.new_page(width=595, height=842)
    _write(terms, 55, 90, "Informations additionnelles")
    _write(terms, 55, 120, "Documentation")
    _write(terms, 55, 135, "Documentation du produit.")
    _write(terms, 55, 160, "Livraisons")
    _write(terms, 55, 175, "Livraison standard.")
    _write(terms, 55, 190, "Conditions")
    _write(terms, 55, 205, "Conditions generales.")
    _write(terms, 55, 400, "elm.leblanc S.A.S")
    data = doc.tobytes()
    doc.close()
    return data


def _extract(data):
    payload = {"document": {"primary_document_type": "quotation"},
               "quality": {"requires_human_review": True, "warnings": [
                   "Transactional page(s) detected as quotation but no structured business extraction was produced."]}}
    return apply_extraction_gate(extract_quotation(data, payload))


def test_offer_is_structured_and_does_not_create_an_order():
    payload = _extract(_offer_pdf())
    offer = payload["quotation_extraction"]
    assert offer["number"]["value"] == "OF-2026-42"
    assert offer["date"]["value"] == "2026-10-19"
    assert offer["valid_until"]["value"] == "2026-12-31"
    assert offer["seller"]["name"] == "elm.leblanc SAS"
    assert offer["seller"]["commercial_contact"]["value"] == "Camille Vente"
    assert offer["recipient"]["name"]["value"] == "CLIENT TEST"
    assert offer["recipient"]["address"]["value"] == "24 rue Exemple, 84000 AVIGNON"
    assert offer["project_details"]["installer"]["value"] == "INSTALLATEUR TEST"
    assert offer["line_items"][0]["references"]["article_number"] == "7716704737"
    assert offer["line_items"][0]["pricing"]["eco_participation_unit"] == 1.5
    assert offer["totals"]["total_net"] == 203
    assert offer["period"]["code"] == "ZFNG"
    assert offer["terms"]["delivery"] == "Livraison standard."
    assert offer["terms"]["conditions"] == "Conditions generales."
    assert offer["validation"]["status"] == "PASS"
    assert payload["extraction_decision"]["status"] == "CHECKS_PASSED"
    clean = build_clean_output(payload)
    assert clean["quotation"]["number"]["value"] == "OF-2026-42"
    assert "order" not in clean
    assert "quotation" in clean_output_schema()["properties"]


def test_incomplete_offer_stays_review_required():
    payload = _extract(_offer_pdf(incomplete=True))
    assert payload["quotation_extraction"]["validation"]["status"] == "WARNING"
    assert "QUOTATION_VALIDATION_INCOMPLETE" in {
        item["code"] for item in payload["extraction_decision"]["reasons"]
    }


def test_locality_only_recipient_address_is_preserved_but_marked_incomplete():
    payload = _extract(_offer_pdf(locality_only=True))
    address = payload["quotation_extraction"]["recipient"]["address"]
    assert address["value"] == "84000 AVIGNON"
    assert address["evidence"]["page"] == 1
    reasons = {item["code"] for item in payload["extraction_decision"]["reasons"]}
    assert "QUOTATION_RECIPIENT_ADDRESS_INCOMPLETE" in reasons
    assert "QUOTATION_RECIPIENT_ADDRESS_MISSING" not in reasons


def test_continuation_page_and_overlapping_price_row_are_not_lost_or_duplicated():
    offer = _extract(_offer_pdf(two_page_line=True))["quotation_extraction"]
    assert [item["references"]["article_number"] for item in offer["line_items"]] == [
        "7716704737", "7716704738"
    ]
    assert [item["amounts"]["net"] for item in offer["line_items"]] == [200, 50]
    assert "Stalingrad" not in offer["line_items"][0]["description"]
    assert offer["validation"]["status"] == "PASS"


def test_order_with_offer_reference_is_not_treated_as_quotation():
    payload = {"document": {"primary_document_type": "purchase_order"}}
    assert "quotation_extraction" not in extract_quotation(_offer_pdf(), payload)

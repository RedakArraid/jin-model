from types import SimpleNamespace

import fitz
import pytest

from po_ocr.models import PageResult, PurchaseOrderHeader, WordToken, Evidence, ExtractedField, Totals
from po_ocr.pdf_native import _needs_hybrid_ocr, _supplement_native_words, extract_native_page
from po_ocr.extract import all_rows, extract_totals, compact_transaction_header_candidate, verify_compact_header_number
from uda.business import (
    _enhance_order_header_from_page_layout,
    _enhance_strict_order_number_v47,
    _extract_geometry_table_items_v46,
    _quantity_first_table_items,
    _strict_text_totals_v48,
)


def _page(rows, number=1):
    words = [WordToken(text=text, page=number, bbox=(x0, y, x1, y+10), confidence=1.0, source="native_pdf")
             for y, cells in rows for text, x0, x1 in cells]
    return PageResult(page=number, width=600, height=842, source_type="native_pdf",
                      text="\n".join(" ".join(t for t, _, _ in cells) for _, cells in rows),
                      words=words, confidence=1.0)


def test_native_ocr_gate_includes_inline_images_but_skips_native_tables_and_logos():
    page = SimpleNamespace(rect=fitz.Rect(0, 0, 600, 842),
                           get_image_info=lambda: [{"bbox": (0, 0, 600, 842)}])
    words = _page([(80, [("Commande", 10, 80), ("PO1234", 100, 160)])]).words
    assert _needs_hybrid_ocr(page, words, {})
    assert not _needs_hybrid_ocr(page, words, {"hybrid_native_ocr_enabled": False})
    headers = _page([(100, [("Article", 10, 50), ("Désignation", 70, 130),
                           ("Quantité", 300, 340), ("Prix", 400, 440)])]).words
    assert not _needs_hybrid_ocr(page, words+headers, {})
    page.get_image_info = lambda: [{"bbox": (10, 10, 150, 50)}]
    assert not _needs_hybrid_ocr(page, words, {})


def test_hybrid_ocr_preserves_native_numbers_and_word_coordinates(monkeypatch):
    from po_ocr import ocr
    document = fitz.open()
    page = document.new_page(width=600, height=842)
    page.insert_text((100, 110), "123456.78")
    _, original, _ = extract_native_page(page, 3, {"hybrid_native_ocr_enabled": False})
    scale = 144/72

    def fake_ocr(image, number, cfg, psm):
        assert psm == 11 and number == 3
        x0, y0, x1, y1 = original[0].bbox
        assert image.getpixel((int((x0+x1)*scale/2), int((y0+y1)*scale/2))) == (255, 255, 255)
        return SimpleNamespace(words=[
            WordToken(text="128456.78", page=3, bbox=tuple(x*scale for x in original[0].bbox), source="ocr", confidence=.99),
            WordToken(text="QUANTITE", page=3, bbox=(600, 400, 720, 425), source="ocr", confidence=.93),
        ])

    monkeypatch.setattr(ocr, "_tesseract_single", fake_ocr)
    words = _supplement_native_words(page, 3, original, {"hybrid_native_ocr_dpi": 144})
    assert words[0] == original[0]
    assert [word.text for word in words] == ["123456.78", "QUANTITE"]
    assert words[1].bbox == (300, 200, 360, 212.5)
    assert words[1].source == "ocr"
    document.close()


def test_geometry_keeps_net_price_when_an_extra_list_price_column_is_present():
    page = _page([
        (200, [("ARTICLE", 10, 60), ("DESIGNATION", 150, 220), ("UNITE", 275, 300),
               ("QUANTITE", 315, 355), ("PRIX", 465, 490), ("NET", 493, 510), ("MONTANT", 535, 580)]),
        (235, [("SKU12345", 10, 65), ("VALVE", 80, 125), ("PIECE", 276, 300),
               ("2,000", 330, 355), ("100,00", 385, 415), ("51,00", 470, 510), ("102,00", 540, 580)]),
        (251, [("RENFORCEE", 80, 145)]),
        (266, [("Fabricant", 80, 128), (":", 133, 136), ("SKU12345", 140, 200)]),
        (281, [("Remises", 80, 125), (":", 129, 132), ("49.00%", 140, 180)]),
        (295, [("Client", 80, 110), ("456123", 120, 150), ("Bon", 160, 180), ("998877", 190, 230)]),
        (325, [("Montant", 400, 450), ("HT", 460, 480), (":", 482, 485), ("102,00", 540, 580)]),
        (350, [("Adresse", 80, 125), ("livraison", 140, 200)]),
    ])
    lines, _ = _extract_geometry_table_items_v46([page], {})
    assert len(lines) == 1
    line = lines[0]
    assert (line.quantity, line.unit_price, line.line_total) == (2, 51, 102)
    assert line.description == "VALVE RENFORCEE"
    assert line.uom == "PIECE"


def test_geometry_recovers_missing_quantity_label_from_repeated_aligned_unit_cells():
    header = [("Article", 10, 45), ("Designation", 125, 185), ("PU", 470, 482),
              ("Net", 485, 500), ("Montant", 545, 580)]
    page = _page([
        (180, header),
        (220, [("REF12345", 15, 75), ("1,000", 310, 340), ("|PCE", 350, 372),
               ("100,00", 385, 418), ("50,00", 435, 463), ("50,00", 487, 518), ("50,00", 548, 579), ("6", 590, 595)]),
        (235, [("Produit", 80, 120), ("alpha", 130, 160)]),
        (260, [("REF67890", 15, 75), ("2,000", 310, 340), ("|PCE", 350, 372),
               ("100,00", 385, 418), ("50,00", 435, 463), ("50,00", 487, 518), ("100,00", 543, 579), ("6", 590, 595)]),
        (275, [("Produit", 80, 120), ("beta", 130, 160)]),
        (300, [("Frais", 80, 110), ("de", 115, 128), ("livraison", 135, 175), ("8,00", 550, 580)]),
    ])
    lines, charges = _extract_geometry_table_items_v46([page], {})
    assert len(lines) == 2
    assert [line.quantity for line in lines] == [1, 2]
    assert [line.unit_price for line in lines] == [50, 50]
    assert [line.line_total for line in lines] == [50, 100]
    assert [line.description for line in lines] == ["Produit alpha", "Produit beta"]
    assert len(charges) == 1 and charges[0].amount == 8


def test_geometry_keeps_multipage_lines_and_rejects_inconsistent_printed_amount():
    header = [("Reference", 10, 65), ("Designation", 90, 160), ("Quantite", 325, 365),
              ("PU", 445, 475), ("Montant", 530, 580)]
    def sample(number, amount):
        return _page([(180, header), (220, [("REF12345", 10, 65), ("Valve", 90, 130),
                       ("2,00", 335, 365), ("10,00", 450, 480), (amount, 550, 580)])], number)
    lines, _ = _extract_geometry_table_items_v46([sample(1, "20,00"), sample(2, "20,00"), sample(3, "300,00")], {})
    assert [line.page for line in lines] == [1, 2]


def test_abbreviated_order_column_is_not_confused_with_supplier_or_date():
    page = _page([
        (80, [("BON", 10, 35), ("DE", 40, 55), ("COMMANDE", 60, 130)]),
        (110, [("FOURNISSEUR", 10, 80), ("DATE", 100, 130), ("N°", 175, 185), ("CDE", 190, 212)]),
        (130, [("987654", 25, 65), ("15/03/2026", 90, 145), ("225577", 180, 220)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value == "225577"


def test_segmented_order_number_preserves_prefix_and_printed_hyphen():
    page = _page([
        (42, [("BON", 160, 190), ("DE", 195, 215), ("COMMANDE", 220, 300),
              ("D'ACHAT", 305, 365)]),
        (72, [("N°", 160, 177), ("02", 184, 200), ("-", 205, 210),
              ("9260205710", 216, 305)]),
        (150, [("CCL", 45, 70), ("COLOMIERS", 75, 140), ("www.ccl.fr", 480, 550)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    field = po.purchase_order.number
    assert field.value == "02 - 9260205710"
    assert field.raw_value == "02 - 9260205710"
    assert field.evidence.source_text == "02 - 9260205710"
    assert field.evidence.extraction_method == "explicit_segmented_order_number"
    assert field.warnings == []


def test_ccl_scan_reconstructs_only_missing_segment_separator_and_keeps_raw_evidence():
    page = _page([
        (42, [("BON", 160, 190), ("DE", 195, 215), ("COMMANDE", 220, 300),
              ("D'ACHAT", 305, 365)]),
        (72, [("N°", 160, 177), ("04", 184, 200),
              ("9260202817", 216, 305)]),
        (150, [("CCL", 45, 70), ("MOISSAC", 75, 140), ("www.ccl.fr", 480, 550)]),
    ])
    for word in page.words:
        word.source = "ocr"
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    field = po.purchase_order.number
    assert field.value == "04 - 9260202817"
    assert field.raw_value == "04 9260202817"
    assert field.evidence.source_text == "04 9260202817"
    assert field.evidence.extraction_method == "explicit_segmented_order_number_reconstructed_separator"
    assert len(field.warnings) == 1


def test_unseparated_numeric_groups_are_not_reformatted_without_corroborated_layout():
    page = _page([
        (42, [("BON", 160, 190), ("DE", 195, 215), ("COMMANDE", 220, 300)]),
        (72, [("N°", 160, 177), ("04", 184, 200),
              ("9260202817", 216, 305)]),
        (150, [("AUTRE", 45, 85), ("SOCIETE", 90, 150)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value != "04 - 9260202817"


def test_spaced_number_below_numero_header_keeps_all_digit_groups():
    page = _page([
        (50, [("COMMANDE", 10, 80)]),
        (80, [("DATE", 10, 45), ("NUMERO", 150, 205)]),
        (100, [("28/11/2025", 10, 80), ("20", 150, 165), ("736", 170, 195)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value == "20 736"


def test_recipient_and_supplier_headings_recover_bosc_order_number():
    page = _page([
        (80, [("Adresse", 10, 55), ("destinataire", 60, 130),
              ("Commande", 300, 365), ("Fournisseur", 370, 445)]),
        (105, [("Exincourt", 10, 70), ("sanitaire", 75, 125),
               ("N°", 300, 315), ("CHA213287", 320, 390), ("BOSCH/LEBLANC", 400, 500)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value == "CHA213287"


def test_supplier_order_heading_recovers_spaced_cf_identifier():
    page = _page([
        (30, [("COMMANDE", 300, 380), ("FOURNISSEUR", 390, 490)]),
        (120, [("Tél.", 20, 42), ("0820003000", 46, 120)]),
        (160, [("CF", 20, 38), ("001968062", 42, 115),
               ("9704726", 125, 180), ("93711", 400, 440)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value == "CF001968062"


def test_supplier_order_heading_recovers_composite_cf_identifier():
    page = _page([
        (30, [("COMMANDE", 300, 380), ("FOURNISSEUR", 390, 490)]),
        (75, [("N°", 20, 34), ("CF", 40, 58), ("15", 65, 78),
              ("4", 83, 89), ("000572686", 96, 169),
              ("DATE", 185, 220), ("10/03/26", 230, 290)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value == "CF000572686"
    assert po.purchase_order.number.evidence.extraction_method == "explicit_composite_erp_order_id"


def test_supplier_order_heading_never_joins_product_code_and_article_reference():
    page = _page([
        (30, [("COMMANDE", 300, 380), ("FOURNISSEUR", 390, 490)]),
        (80, [("N°", 20, 32), ("Document", 40, 100), ("Date", 210, 245)]),
        (100, [("CF352124", 40, 105), ("20/03/2026", 210, 275)]),
        (150, [("Code", 20, 50), ("art", 54, 70), ("Référence", 90, 150),
               ("Désignation", 180, 250), ("Qte", 350, 375), ("Montant", 500, 555)]),
        (180, [("H44606", 20, 65), ("7736504817", 90, 165),
               ("CHAUFFE-BAINS", 180, 270), ("1.000", 350, 380), ("315.00", 500, 545)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value == "CF352124"


def test_composite_cf_identifier_survives_a_missing_scanned_title():
    page = _page([
        (75, [("N°", 20, 34), ("CF", 40, 58), ("15", 65, 78),
              ("4", 83, 89), ("000568624", 96, 169),
              ("DATE", 185, 220), ("19/12/25", 230, 290)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value == "CF000568624"
    assert po.purchase_order.number.evidence.extraction_method == "explicit_composite_erp_order_id"


def test_product_tableau_de_commande_does_not_beat_piece_column():
    page = _page([
        (60, [("Commande", 10, 75), ("Date", 80, 110), ("de", 115, 128),
              ("livraison", 135, 190), ("souhaitee", 195, 250)]),
        (90, [("Date", 10, 40), ("Piece", 100, 130), ("Client", 170, 205),
              ("Reference", 220, 280), ("Commercial", 300, 365)]),
        (110, [("02/04/2026", 10, 75), ("1784007", 100, 150),
               ("FELM2140", 170, 225), ("BERTE", 300, 340)]),
        (300, [("8716774182", 10, 80), ("TABLEAU", 90, 140), ("DE", 145, 160),
               ("COMMANDE", 165, 230), ("1", 240, 245), ("1", 250, 255),
               ("2EL8716774182", 260, 350), ("1", 360, 365)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value == "1784007"


def test_attached_numero_below_order_title_beats_acknowledgement_number():
    page = _page([
        (40, [("COMMANDE", 10, 80)]),
        (60, [("N°5516418", 10, 75), ("/1877", 80, 115)]),
        (100, [("Suite", 10, 40), ("dernier", 45, 85), ("accuse", 90, 125),
               ("No", 130, 145), ("18923185", 150, 210)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value == "5516418 /1877"


def test_bon_de_commande_explicit_number_beats_product_identifier():
    page = _page([
        (40, [("BON", 10, 35), ("DE", 40, 55), ("COMMANDE", 60, 125),
              ("n°BC051994", 130, 210)]),
        (180, [("N°", 10, 25), ("Client", 30, 70), ("Acheteur", 80, 125)]),
        (220, [("LC11-4PVHYB", 10, 95)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value == "BC051994"


def test_customer_order_note_cannot_anchor_document_number_in_product_description():
    page = _page([
        (250, [("pour", 10, 30), ("la", 35, 42), ("commande", 50, 105), ("client", 115, 145), ("n°", 150, 162), ("9799456", 170, 210)]),
        (267, [("BAS", 10, 30), ("NOX", 35, 55), ("C4", 65, 82), ("9/11L", 90, 120)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_order_header_from_page_layout(po, [page], {})
    assert po.purchase_order.number.value is None


def test_tax_registration_in_raster_footer_is_not_a_tax_amount():
    page = _page([(700, [("TVA", 10, 40), ("intracommunautaire", 50, 140), ("FR", 145, 160),
                         ("38", 170, 185), ("790", 190, 210), ("553", 215, 235), ("242", 240, 260)])])
    totals = extract_totals(all_rows([page]))
    assert totals.total_vat is None
    assert totals.total_tax is None


def test_page_marker_does_not_cancel_explicit_order_number():
    page = _page([(30, [("COMMANDE", 10, 75), ("N°", 80, 95), ("880012345", 100, 160),
                       ("Page", 480, 510), ("1", 515, 520), ("sur", 525, 542), ("1", 550, 555)])])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value == "880012345"


def test_english_order_heading_cannot_promote_following_spreadsheet_quantity():
    page = _page([
        (20, [("Purchase", 10, 55), ("Order", 60, 90), ("PO-XLSX-2026-009", 100, 230)]),
        (40, [("Product", 10, 50), ("10", 65, 85), ("2,00", 150, 180)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value == "PO-XLSX-2026-009"


def test_quantity_first_table_distinguishes_products_shipping_and_signature():
    page = _page([
        (180, [("Qté", 50, 65), ("Unité", 70, 95), ("Désignation", 250, 315),
               ("Référence", 470, 515), ("Prix", 535, 550), ("Unité", 560, 580)]),
        (220, [("3", 12, 18), ("PCE", 22, 40), ("VALVE", 125, 165), ("REF12345", 470, 515), ("12,50€", 535, 570)]),
        (235, [("Prix", 125, 145), ("négocié", 150, 190), ("le", 200, 210), ("19/03/26", 215, 270)]),
        (260, [("1", 12, 18), ("PCE", 22, 40), ("FRAIS", 125, 152), ("DE", 155, 168), ("PORT", 172, 200),
               ("PORT", 470, 515), ("20,00€", 535, 570)]),
        (320, [("Responsable", 125, 190), ("Service", 200, 245), ("Visa", 470, 500)]),
    ])
    lines, charges = _quantity_first_table_items([page])
    assert len(lines) == 1 and len(charges) == 1
    assert (lines[0].quantity, lines[0].unit_price, lines[0].line_total) == (3, 12.5, 37.5)
    assert lines[0].warnings and "calculated" in lines[0].warnings[0]
    assert charges[0].charge_type == "shipping" and charges[0].amount == 20


def test_geometry_keeps_alphabetic_shipping_code_in_document_amount():
    page = _page([
        (180, [("Article", 10, 50), ("Designation", 120, 190), ("Quantite", 350, 390),
               ("Prix", 430, 452), ("net", 455, 480), ("Montant", 535, 580)]),
        (220, [("SKU12345", 10, 65), ("VALVE", 120, 160), ("1", 368, 375),
               ("183,17", 450, 480), ("183,17", 550, 580)]),
        (250, [("PORTSTD", 10, 65), ("FRAIS", 120, 148), ("DE", 150, 163), ("PORT", 165, 190),
               ("STANDARD", 195, 240), ("1", 368, 375), ("20,00", 450, 480), ("20,00", 550, 580)]),
    ])
    lines, charges = _extract_geometry_table_items_v46([page], {})
    assert len(lines) == 1 and len(charges) == 1
    assert charges[0].code == "PORTSTD"
    assert charges[0].charge_type == "shipping"
    assert charges[0].parent_material_number is None
    assert lines[0].line_total + charges[0].amount == pytest.approx(203.17)


def _compact_header_page(prefix="CF", title="COMMANDE FOURNISSEUR", label=None):
    row = [(prefix, 70, 88), ("001234567", 100, 175), ("26/03/26", 230, 290), ("1", 320, 325)]
    if label:
        row.insert(0, (label, 10, 60))
    page = _page([(20, [(title, 300, 570)]), (170, row)])
    for word in page.words:
        word.source = "ocr"
    return page


def test_compact_scan_number_requires_agreeing_crop_readings(monkeypatch):
    from PIL import Image
    from po_ocr import ocr
    page = _compact_header_page(prefix="CR")
    calls = []

    def read_crop(image, number, cfg, psm):
        calls.append(psm)
        return SimpleNamespace(words=[WordToken(text="CF", page=number, bbox=(0,0,20,10), source="ocr"),
                                      WordToken(text="001234567", page=number, bbox=(25,0,90,10), source="ocr")],
                               confidence=.96)
    monkeypatch.setattr(ocr, "_tesseract_single", read_crop)
    verify_compact_header_number(page, Image.new("RGB", (600,842), "white"), {})
    assert calls == [7, 13]
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    field = po.purchase_order.number
    assert field.value == "CF001234567"
    assert field.raw_value == "CR 001234567"
    assert field.evidence.bbox == (70,170,175,180)
    assert field.evidence.source_text == "CR 001234567"
    assert field.evidence.extraction_method == "compact_order_header_verified_ocr"
    assert field.final_confidence <= .96
    assert po.purchase_order.customer_reference.value is None
    assert po.purchase_order.quote_number.value is None


def test_disagreeing_or_missing_crop_confirmation_never_promotes_scan_number(monkeypatch):
    from PIL import Image
    from po_ocr import ocr
    page = _compact_header_page()
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value is None
    def disagree(image, number, cfg, psm):
        value = "CF001234567" if psm == 7 else "CR001234567"
        return SimpleNamespace(words=[WordToken(text=value,page=number,bbox=(0,0,100,10),source="ocr")],confidence=.99)
    monkeypatch.setattr(ocr, "_tesseract_single", disagree)
    verify_compact_header_number(page, Image.new("RGB", (600,842), "white"), {})
    assert page.ocr_diagnostics["compact_header_number"]["confirmed"] is False
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value is None


@pytest.mark.parametrize("title,label,prefix", [
    ("DEVIS", None, "CF"),
    ("Voir bon de commande", None, "CF"),
    ("COMMANDE FOURNISSEUR", "Client", "CF"),
    ("COMMANDE FOURNISSEUR", "Compte", "CF"),
    ("COMMANDE FOURNISSEUR", "Devis", "CF"),
    ("COMMANDE FOURNISSEUR", None, "Ref"),
    ("COMMANDE FOURNISSEUR", None, "Tel"),
])
def test_compact_header_rejects_customer_account_quote_and_contact_numbers(title,label,prefix):
    assert compact_transaction_header_candidate(_compact_header_page(prefix,title,label)) is None


def test_preserved_order_candidate_keeps_evidence_and_never_inflates_confidence():
    page = _page([(30, [("COMMANDE",10,80), ("FOURNISSEUR",90,180)])])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    original = ExtractedField(value="PO-2026-0123",raw_value="PO-2026-0123",normalized_value="PO-2026-0123",
        final_confidence=.71,ocr_confidence=.72,semantic_confidence=.70,
        evidence=Evidence(page=1,bbox=(10,50,100,65),source_text="N° commande PO-2026-0123",extraction_method="original_reader"))
    po.purchase_order.number = original
    po.purchase_order.customer_reference.value = "OTHER-55555"
    before = original.model_dump()
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number is original
    assert po.purchase_order.number.model_dump() == before
    assert po.purchase_order.customer_reference.value == "OTHER-55555"


def test_piece_header_means_document_number_only_in_compact_transaction_table():
    page = _page([
        (180, [("Commande",10,75)]),
        (200, [("Date",20,45), ("Pièce",100,130), ("Fourn.",170,200), ("Notre",270,300), ("référence",310,360)]),
        (220, [("15/01/2026",10,65), ("314159",95,135), ("F509",170,200)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po,[page],{})
    assert po.purchase_order.number.value == "314159"
    assert po.purchase_order.number.evidence.bbox == (95,220,135,230)
    assert po.purchase_order.customer_reference.value is None
    page.words.append(WordToken(text="Qte",page=1,bbox=(390,200,420,210),source="native_pdf"))
    other = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(other,[page],{})
    assert other.purchase_order.number.value is None


def test_rexel_cde_number_outranks_supplier_heading_compact_candidate():
    page = _page([
        (40, [("BON",10,35), ("DE",40,55), ("COMMANDE",60,120),
              ("REXEL",125,165), ("-",170,175), ("CDE",180,205),
              ("n°",210,220), ("026432682",225,285)]),
        (80, [("COMMANDE",10,80), ("FOURNISSEUR",85,160)]),
        (100, [("DU",10,30), ("05001100",35,90)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value == "026432682"
    assert po.purchase_order.number.evidence.extraction_method == "explicit_order_label"


def test_spaced_mainframe_title_anchors_number_on_following_row():
    page = _page([
        (40, [("C",10,20), ("O",25,35), ("M",40,50), ("M",55,65),
              ("A",70,80), ("N",85,95), ("D",100,110), ("E",115,125)]),
        (60, [("N.",10,20), (":",24,28), ("009-2460-220126",35,135)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    _enhance_strict_order_number_v47(po, [page], {})
    assert po.purchase_order.number.value == "009-2460-220126"
    assert po.purchase_order.number.evidence.extraction_method == "explicit_attached_numero_below_order_title"


def test_abbreviated_price_amount_headers_keep_auxiliary_cells_and_summary_out_of_products():
    page = _page([
        (200, [("Référence",10,70), ("Désignation",100,180), ("UA",315,325), ("UP",330,340),
               ("Réf.",355,375), ("Interne",378,408), ("Qte",440,460), ("Px",490,502), ("Base",504,525), ("Mnt",550,565), ("Net",568,588)]),
        (350, [("REF12345",10,70), ("VALVE",100,140), ("1",315,325), ("1",330,340), ("REF12345",355,405),
               ("1,00",440,460), ("65,325",490,525), ("65,33",550,585)]),
        (370, [("Eco-Participation",100,190), ("1,00",440,460), ("0,14",550,585)]),
        (400, [("Total",100,135), ("H.T",140,165), ("Marchandise",170,240), ("65,33",550,585)]),
        (420, [("Total",100,135), ("H.T",140,165), ("Eco-Participation",170,260), ("0,14",550,585)]),
    ])
    lines, charges = _extract_geometry_table_items_v46([page],{})
    assert len(lines) == 1 and len(charges) == 1
    assert lines[0].description == "VALVE"
    assert lines[0].unit_price == 65.325 and lines[0].line_total == 65.33
    assert charges[0].amount == .14


def test_printed_net_ht_footer_overrides_merchandise_subtotal_without_adding_charges():
    page = _page([
        (700, [("POIDS",100,140), ("TOTAL",145,180), ("NET",230,250), ("H.T.",253,273),
               ("TVA",285,305), ("TAUX",320,345), ("MONTANT",360,410), ("TVA",413,438),
               ("NET",480,500), ("A",505,513), ("PAYER",520,555)]),
        (720, [("0,00",100,125), ("Kg",130,145), ("778,03",230,273), ("1",290,298), ("20,0",320,345),
               ("155,61",385,425), ("933,64",510,550)]),
    ])
    po = SimpleNamespace(totals=Totals(total_net=777.89,subtotal=777.89,grand_total=2026))
    _strict_text_totals_v48(po,[page],{})
    assert po.totals.total_net == 778.03
    assert po.totals.subtotal == 777.89
    assert po.totals.total_vat == 155.61
    assert po.totals.grand_total == 933.64

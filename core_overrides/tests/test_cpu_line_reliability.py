from types import SimpleNamespace

import fitz
import pytest

from po_ocr.models import (
    AdditionalCharge,
    Address,
    DocumentMeta,
    PageResult,
    PurchaseOrderHeader,
    PurchaseOrderResult,
    WordToken,
    Evidence,
    ExtractedField,
    LineItem,
    Party,
    Totals,
)
from po_ocr.config import load_config
from po_ocr.pdf_native import _needs_hybrid_ocr, _supplement_native_words, extract_native_page
from po_ocr.extract import (
    all_rows, extract_lines, extract_lines_from_pages, extract_parties,
    extract_multiline_lines_from_pages, extract_totals,
    compact_transaction_header_candidate, verify_compact_header_number,
    _find_table_header,
)
from uda.business import (
    _clean_po_number_v48,
    _dual_reference_stacked_native_table_items_v67,
    _dual_reference_receipt_date_items_v5169,
    _enhance_requested_delivery_date_v5169,
    _enhance_strict_totals_v47,
    _enhance_totals_from_native_tables,
    _enhance_order_header_from_page_layout,
    _enhance_inline_transaction_header_v46,
    _enhance_strict_order_number_v47,
    _extract_geometry_table_items_v46,
    _line_hypothesis_total_fit_v66,
    _material_anchored_split_table_items_v61,
    _material_anchored_numeric_row_items_v61,
    _merge_explicit_page_charges_v63,
    _native_table_line_items_and_charges,
    _normalize_labeled_party_code_v5166,
    _party_from_labeled_zone_v48,
    _partial_line_hypothesis_rejection_v5175,
    _repair_dimension_ocr_v5166,
    _repair_native_long_decimal_line_prices_v5170,
    _repair_order_date_delivery_collision_v5170,
    _repair_order_number_evidence_bbox_v5172,
    _repair_purchase_order_party_roles_v5170,
    _repair_supplier_purchase_order_addressee_v5173,
    _rexellike_product_fee_table,
    _separate_promotional_continuation_notes_v5171,
    _repair_scan_currency_suffix_artifacts_v64,
    _repair_legal_footer_validation_v65,
    _reconcile_requested_delivery_dates_v5169,
    _separate_additional_charge_lines_v62,
    _sanitize_legal_footer_lines_v48,
    _sanitize_double_counted_shipping_v47,
    _sanitize_internal_reference_v47,
    _sanitize_non_product_lines_v61,
    _sanitize_tax_identifier_totals_v5174,
    _price_then_quantity_table_items,
    _quantity_first_table_items,
    _summary_table_totals_v48,
    _semantic_text_table_items_v68,
    _two_row_erp_table_items,
    _strict_text_totals_v48,
    _downgrade_unreconciled_derived_lines_v5170,
    enhance_purchase_order_with_native_structure,
)


def _page(rows, number=1):
    words = [WordToken(text=text, page=number, bbox=(x0, y, x1, y+10), confidence=1.0, source="native_pdf")
             for y, cells in rows for text, x0, x1 in cells]
    return PageResult(page=number, width=600, height=842, source_type="native_pdf",
                      text="\n".join(" ".join(t for t, _, _ in cells) for _, cells in rows),
                      words=words, confidence=1.0)


def test_dual_reference_receipt_date_table_recovers_lines_dates_and_descriptions():
    page = _page([
        (300, [("N°", 4, 14), ("Référence", 25, 72), ("fournisseur", 76, 125),
               ("N°", 183, 193), ("Quantité", 291, 330), ("Code", 334, 355),
               ("Date", 370, 391), ("P.", 427, 438), ("Unit", 440, 460),
               ("P.", 477, 488), ("Unit", 490, 510), ("Montant", 526, 565)]),
        (311, [("ligne", 4, 24), ("unité", 334, 357), ("réception", 370, 410),
               ("brut", 442, 463), ("Net", 494, 510)]),
        (350, [("30002244", 25, 66), ("504265200", 187, 233), ("4", 326, 332),
               ("PC", 339, 351), ("16/02/26", 368, 407), ("191,00", 427, 458),
               ("95,9775", 486, 522), ("383,91", 535, 566)]),
        (360, [("1", 4, 9)]),
        (370, [("CDE", 25, 46), ("A", 49, 56), ("DISTANCE", 59, 98), ("BFU", 101, 120)]),
        (390, [("7716701550", 25, 76), ("504201498", 187, 233), ("1", 326, 332),
               ("PC", 339, 351), ("03/03/26", 368, 407), ("1@346,00", 421, 463),
               ("1@346", 484, 516), ("1@346,00", 525, 568)]),
        (400, [("2", 4, 9)]),
        (410, [("Egalis", 25, 55), ("ballon", 58, 88), ("Icondens", 91, 130),
               ("GVD", 133, 153), ("IC", 156, 167), ("24", 170, 182)]),
        (430, [("TOTAL", 420, 455), ("HT", 459, 473), ("1729,91", 525, 568)]),
    ])

    lines = _dual_reference_receipt_date_items_v5169(
        [page], load_config(None), "EUR"
    )

    assert len(lines) == 2
    assert lines[0].line_number == "1"
    assert lines[0].supplier_material_number == "30002244"
    assert lines[0].customer_material_number == "504265200"
    assert lines[0].description == "CDE A DISTANCE BFU"
    assert lines[0].requested_delivery_date == "2026-02-16"
    assert lines[0].gross_unit_price == 191.0
    assert lines[0].net_unit_price == 95.9775
    assert lines[0].line_total == 383.91
    assert lines[1].line_number == "2"
    assert lines[1].description == "Egalis ballon Icondens GVD IC 24"
    assert lines[1].requested_delivery_date == "2026-03-03"
    assert lines[1].net_unit_price == 1346.0
    assert lines[1].line_total == 1346.0


def test_uniform_line_delivery_date_is_promoted_but_mixed_dates_are_not():
    po = PurchaseOrderResult(document=DocumentMeta(
        id="x", filename="x.pdf", extension="pdf", file_size=1, sha256="x",
        page_count=1, is_pdf=True, processing_timestamp="2026-01-01T00:00:00Z",
        model_version="test",
    ))
    po.lines = [
        LineItem(material_number="7716701550", requested_delivery_date="2026-03-03", confidence=.97),
        LineItem(material_number="7716705075", requested_delivery_date="2026-03-03", confidence=.96),
    ]

    _reconcile_requested_delivery_dates_v5169(po)

    assert po.purchase_order.expected_delivery_date.value == "2026-03-03"
    assert po.logistics.requested_delivery_date == "2026-03-03"
    assert po.purchase_order.expected_delivery_date.evidence.extraction_method == (
        "uniform_line_requested_delivery_date_v5169"
    )

    mixed = PurchaseOrderResult(document=po.document)
    mixed.lines = [
        LineItem(material_number="A12345", requested_delivery_date="2026-03-03"),
        LineItem(material_number="B12345", requested_delivery_date="2026-03-04"),
    ]
    _reconcile_requested_delivery_dates_v5169(mixed)
    assert mixed.purchase_order.expected_delivery_date.value is None
    assert mixed.logistics.requested_delivery_date is None


def test_explicit_requested_delivery_date_uses_date_after_label_not_order_date():
    po = PurchaseOrderResult(document=DocumentMeta(
        id="x", filename="x.pdf", extension="pdf", file_size=1, sha256="x",
        page_count=1, is_pdf=True, processing_timestamp="2026-01-01T00:00:00Z",
        model_version="test",
    ))
    page = _page([
        (100, [("Date", 20, 40), ("commande", 43, 85), ("07/04/2026", 90, 145),
               ("Commande", 180, 225), ("Date", 230, 250), ("de", 253, 265),
               ("livraison", 268, 315), ("souhaitée", 318, 365),
               (":", 368, 371), ("13/04/2026", 375, 430)]),
    ])

    _enhance_requested_delivery_date_v5169(po, [page], load_config(None))

    assert po.purchase_order.expected_delivery_date.value == "2026-04-13"
    assert po.logistics.requested_delivery_date == "2026-04-13"
    assert (
        po.purchase_order.expected_delivery_date.evidence.extraction_method
        == "explicit_requested_delivery_date_v5169"
    )


def test_requested_delivery_date_accepts_standalone_delai_with_spaced_date():
    po = PurchaseOrderResult(document=DocumentMeta(
        id="x", filename="x.pdf", extension="pdf", file_size=1, sha256="x",
        page_count=1, is_pdf=True, processing_timestamp="2026-01-01T00:00:00Z",
        model_version="test",
    ))
    page = _page([
        (100, [("DELAI", 20, 55), ("26", 65, 78), ("10", 82, 95),
               ("26", 99, 112), ("IN", 130, 145), ("2", 150, 156)]),
    ])

    _enhance_requested_delivery_date_v5169(po, [page], load_config(None))

    assert po.purchase_order.expected_delivery_date.value == "2026-10-26"
    assert po.logistics.requested_delivery_date == "2026-10-26"


def test_requested_delivery_date_ignores_delivery_policy_prose_without_date():
    po = PurchaseOrderResult(document=DocumentMeta(
        id="x", filename="x.pdf", extension="pdf", file_size=1, sha256="x",
        page_count=1, is_pdf=True, processing_timestamp="2026-01-01T00:00:00Z",
        model_version="test",
    ))
    page = _page([
        (100, [("La", 20, 30), ("date", 33, 53), ("de", 56, 68),
               ("livraison", 71, 118), ("indiquée", 121, 160),
               ("correspond", 163, 215), ("au", 218, 230),
               ("site", 233, 255), ("sous", 258, 280), ("48h", 283, 300)]),
    ])

    _enhance_requested_delivery_date_v5169(po, [page], load_config(None))

    assert po.purchase_order.expected_delivery_date.value is None
    assert po.logistics.requested_delivery_date is None


def test_long_native_decimal_price_repairs_inflated_price_and_derived_amount():
    line = LineItem(
        material_number="7716705092",
        description="CHAUD.ACLEA BASNOX CGLM24-7XN",
        quantity=8,
        unit_price=6200000.0,
        net_unit_price=6200000.0,
        line_total=49600000.0,
        line_net_amount=49600000.0,
        raw_text="7716705092 CHAUD.ACLEA BASNOX CGLM24-7XN 8,000 PI 620,0000",
        warnings=["Line total calculated from printed quantity and unit price; no printed line amount."],
    )

    repaired = _repair_native_long_decimal_line_prices_v5170(
        [line], load_config(None)
    )

    assert repaired == 1
    assert line.unit_price == 620.0
    assert line.net_unit_price == 620.0
    assert line.line_total == 4960.0
    assert line.line_net_amount == 4960.0
    assert "unit_price_native_long_decimal_repaired_v5170" in line.warnings


def test_long_native_decimal_price_does_not_replace_a_distinct_printed_amount():
    line = LineItem(
        material_number="7716705092",
        quantity=8,
        unit_price=6200000.0,
        net_unit_price=6200000.0,
        line_total=4950.0,
        line_net_amount=4950.0,
        raw_text="7716705092 PRODUIT 8,000 PI 620,0000 4.950,00",
    )

    repaired = _repair_native_long_decimal_line_prices_v5170(
        [line], load_config(None)
    )

    assert repaired == 1
    assert line.unit_price == 620.0
    assert line.line_total == 4950.0
    assert line.line_net_amount == 4950.0


def test_explicit_header_date_repairs_delivery_date_collision():
    po = PurchaseOrderResult(document=DocumentMeta(
        id="x", filename="x.pdf", extension="pdf", file_size=1, sha256="x",
        page_count=1, is_pdf=True, processing_timestamp="2026-01-01T00:00:00Z",
        model_version="test",
    ))
    po.purchase_order.order_date = ExtractedField(value="2024-02-13")
    po.purchase_order.expected_delivery_date = ExtractedField(value="2024-02-13")
    po.logistics.requested_delivery_date = "2024-02-13"
    page = _page([
        (40, [("BON", 180, 210), ("DE", 214, 230), ("COMMANDE", 234, 300),
              ("D'ACHAT", 304, 355)]),
        (70, [("du", 210, 225), ("09/02/2024", 230, 290),
              ("à", 295, 303), ("11:24:34", 308, 355)]),
        (120, [("Date", 20, 45), ("de", 50, 60), ("livraison", 65, 115),
               ("13/02/2024", 120, 180)]),
    ])

    _, repaired = _repair_order_date_delivery_collision_v5170(
        po, [page], load_config(None)
    )

    assert repaired == 1
    assert po.purchase_order.order_date.value == "2024-02-09"
    assert po.purchase_order.expected_delivery_date.value == "2024-02-13"
    assert po.purchase_order.order_date.evidence.extraction_method == (
        "explicit_order_header_date_v5170"
    )


def test_purchase_order_addressee_is_supplier_and_explicit_bill_to_is_buyer():
    po = PurchaseOrderResult(document=DocumentMeta(
        id="x", filename="x.pdf", extension="pdf", file_size=1, sha256="x",
        page_count=1, is_pdf=True, processing_timestamp="2026-01-01T00:00:00Z",
        model_version="test",
    ))
    po.buyer = Party(
        name="LEBLANC E.L.M.", legal_name="LEBLANC E.L.M.",
        address=Address(postal_code="93711", city="DRANCY CEDEX"),
        vat_number="FR07716320619",
    )
    po.purchase_order.number = ExtractedField(value="04 - 9240202813")
    po.supplier = Party(name="N° 04 - 9240202813 NORMALE")
    po.bill_to = Party(
        name="COMPTOIR COMMERCIAL DU LANGUEDOC",
        legal_name="COMPTOIR COMMERCIAL DU LANGUEDOC",
        address=Address(street="RUE VOLTAIRE", postal_code="82000", city="MONTAUBAN"),
    )
    page = _page([
        (35, [("BON", 180, 210), ("DE", 214, 230), ("COMMANDE", 234, 300),
              ("D'ACHAT", 304, 355)]),
        (75, [("LEBLANC", 220, 270), ("E.L.M.", 275, 315)]),
        (130, [("A", 20, 28), ("Facturer", 32, 80), ("à", 84, 90)]),
        (145, [("COMPTOIR", 20, 80), ("COMMERCIAL", 85, 150),
               ("DU", 155, 170), ("LANGUEDOC", 175, 240)]),
    ])

    _, repaired = _repair_purchase_order_party_roles_v5170(
        po, [page], load_config(None)
    )

    assert repaired == 1
    assert po.buyer.name == "COMPTOIR COMMERCIAL DU LANGUEDOC"
    assert po.buyer.vat_number == "FR07716320619"
    assert po.supplier.name == "LEBLANC E.L.M."
    assert po.supplier.address.postal_code == "93711"
    assert po.supplier.vat_number is None


def test_supplier_order_service_block_repairs_vendor_and_generic_account_buyer():
    po = PurchaseOrderResult(document=DocumentMeta(
        id="x", filename="x.pdf", extension="pdf", file_size=1, sha256="x",
        page_count=1, is_pdf=True, processing_timestamp="2026-01-01T00:00:00Z",
        model_version="test",
    ))
    po.buyer = Party(
        name="COMPTE 18030", legal_name="COMPTE 18030",
        address=Address(postal_code="93711", city="DRANCY CEDEX"),
    )
    po.supplier = Party(name="y", legal_name="y")
    po.ship_to = Party(name="CATRYBAYART PERENCHIES")
    page = _page([
        (35, [("COMMANDE", 180, 250), ("FOURNISSEUR", 255, 350)]),
        (55, [("contact@catrybayart.com", 20, 155)]),
        (75, [("76", 20, 32), ("rue", 36, 54), ("d'Emmerin", 58, 115),
              ("-", 118, 122)]),
        (90, [("59000", 20, 50), ("LILLE", 55, 90),
              ("Commandez", 105, 165), ("en", 170, 185), ("ligne", 190, 220)]),
        (180, [("ELM", 200, 225), ("LEBLANC", 230, 280),
               ("SERVICE", 285, 330), ("ACHATS", 335, 375)]),
        (200, [("COMPTE", 220, 270), ("CLIENT", 275, 315), ("18030", 320, 355)]),
        (220, [("93711", 220, 255), ("DRANCY", 260, 310), ("CEDEX", 315, 350)]),
    ])

    _, repaired = _repair_supplier_purchase_order_addressee_v5173(
        po, [page], load_config(None)
    )

    assert repaired == 2
    assert po.supplier.name == "ELM LEBLANC"
    assert po.supplier.address.postal_code == "93711"
    assert po.supplier.address.city == "DRANCY CEDEX"
    assert po.buyer.name == "CATRYBAYART"
    assert po.buyer.address.street == "76 rue d'Emmerin"
    assert po.buyer.address.postal_code == "59000"
    assert po.buyer.address.city == "LILLE"


def test_unreconciled_derived_amount_is_not_left_high_confidence():
    po = PurchaseOrderResult(document=DocumentMeta(
        id="x", filename="x.pdf", extension="pdf", file_size=1, sha256="x",
        page_count=1, is_pdf=True, processing_timestamp="2026-01-01T00:00:00Z",
        model_version="test",
    ))
    po.totals.total_before_tax = 4960.0
    po.lines = [LineItem(
        material_number="7716705092", quantity=8, unit_price=6200000.0,
        line_total=49600000.0, confidence=0.97, validation_status="VALID",
        warnings=["Line total calculated from printed quantity and unit price; no printed line amount."],
    )]

    _, downgraded = _downgrade_unreconciled_derived_lines_v5170(
        po, load_config(None)
    )

    assert downgraded == 1
    assert po.lines[0].confidence == 0.45
    assert po.lines[0].validation_status == "REVIEW_REQUIRED"


def test_internal_order_number_cannot_duplicate_a_line_article_reference():
    po = PurchaseOrderResult(document=DocumentMeta(
        id="x", filename="x.pdf", extension="pdf", file_size=1, sha256="x",
        page_count=1, is_pdf=True, processing_timestamp="2026-01-01T00:00:00Z",
        model_version="test",
    ))
    po.purchase_order.internal_number = ExtractedField(
        value="7716705092", normalized_value="7716705092", raw_value="7716705092",
        final_confidence=0.98,
    )
    po.lines = [LineItem(material_number="7716705092")]

    _sanitize_internal_reference_v47(po)

    assert po.purchase_order.internal_number.value is None
    assert po.purchase_order.internal_number.final_confidence == 0.0


def test_standalone_delivery_label_keeps_centered_address_block_full_width():
    page = _page([
        (414, [("(1)", 45, 58), ("a", 62, 70), ("livrer", 74, 107),
               ("le", 111, 121), ("26/02/2024", 125, 184)]),
        (430, [("A", 20, 28), ("livrer", 32, 65), ("a", 69, 77),
               ("l'adresse", 81, 126), ("ci-dessous", 130, 187)]),
        (450, [("PIECES", 248, 292), ("DETACHEES", 298, 365)]),
        (466, [("Z.I.", 240, 260), ("-", 264, 270), ("Rue", 274, 295),
               ("Denis", 300, 330), ("Papin", 335, 368)]),
        (482, [("24102", 260, 292), ("BERGERAC", 298, 360)]),
        (498, [("FRANCE", 275, 324)]),
        (530, [("Reglement", 20, 80), ("TOTAL", 430, 475), ("337.12", 500, 545)]),
    ])

    party = _party_from_labeled_zone_v48(
        [page],
        [r"adresse\s+de\s+livraison", r"a\s+livrer\s+a", r"livre\s+a"],
        {},
        prefer_role="ship_to",
    )

    assert party is not None
    assert party.name == "PIECES DETACHEES"
    assert party.address.line1 == "Z.I. - Rue Denis Papin"
    assert party.address.postal_code == "24102"
    assert party.address.city == "BERGERAC"


def test_delivery_label_with_opposite_supplier_content_stays_in_its_column():
    page = _page([
        # The label is deliberately indented relative to the address.  House
        # number and postal code must still be collected from the whole half.
        (200, [("Adresse", 70, 108), ("de", 112, 122),
               ("livraison", 126, 172), ("imperative", 176, 230),
               ("ELM", 340, 365), ("LEBLANC", 370, 420),
               ("BOSCH", 425, 465)]),
        (220, [("SIDV", 20, 48), ("BAYONNE", 52, 105),
               ("124,126", 340, 380), ("rue", 385, 404),
               ("de", 408, 418), ("Stalingrad", 422, 480)]),
        (236, [("24", 20, 32), ("Chemin", 36, 75), ("de", 79, 89),
               ("Sabalce", 93, 136), ("93711", 340, 372),
               ("DRANCY", 377, 420), ("CEDEX", 425, 462)]),
        (252, [("64100", 20, 52), ("BAYONNE", 57, 110),
               ("Tel", 340, 360), ("08", 365, 378), ("20", 382, 395)]),
    ])

    party = _party_from_labeled_zone_v48(
        [page],
        [r"adresse\s+de\s+livraison", r"a\s+livrer\s+a"],
        {},
        prefer_role="ship_to",
    )

    assert party is not None
    assert party.name == "SIDV BAYONNE"
    assert party.address.line1 == "24 Chemin de Sabalce"
    assert party.address.postal_code == "64100"
    assert party.address.city == "BAYONNE"
    assert "STALINGRAD" not in " ".join(party.address.raw_lines).upper()


def test_duplicate_pdf_layer_cleanup_preserves_composite_po_suffix():
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader(number=ExtractedField(
        value="5416718 5416718 / 1877 1877",
        evidence=Evidence(
            page=1, bbox=(20, 125, 450, 146),
            source_text="5416718 5416718 / 1877 1877",
        ),
    )))

    _clean_po_number_v48(po, {"layout": {"po_number_tail_cleanup": True}})

    assert po.purchase_order.number.value == "5416718 / 1877"
    assert po.purchase_order.number.evidence.extraction_method == (
        "po_number_composite_duplicate_cleanup_v60"
    )


def test_duplicate_pdf_layer_cleanup_still_collapses_simple_identifier():
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader(number=ExtractedField(
        value="5416718 5416718",
    )))
    _clean_po_number_v48(po, {"layout": {"po_number_tail_cleanup": True}})
    assert po.purchase_order.number.value == "5416718"


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


def test_native_pdf_exact_overlay_words_are_deduplicated_by_position():
    document = fitz.open()
    page = document.new_page(width=600, height=842)
    page.insert_text((100, 110), "BERGERAC")
    page.insert_text((100, 110), "BERGERAC")
    page.insert_text((100, 140), "BERGERAC")

    _, words, _ = extract_native_page(
        page, 1, {"hybrid_native_ocr_enabled": False}
    )

    assert [word.text for word in words] == ["BERGERAC", "BERGERAC"]
    assert words[0].bbox != words[1].bbox
    document.close()


def test_split_internal_and_supplier_headers_keep_reference_ownership():
    page = _page([
        (262, [("CODE", 25, 51), ("DESIGNATION", 180, 243),
               ("REF", 387, 405), ("QUANTITE", 445, 491),
               ("PRIX", 500, 521), ("MONTANT", 533, 578)]),
        (273, [("INTERNE", 18, 58), ("FOURNISSEUR", 367, 433)]),
        (304, [("1663503", 20, 55), ("COUVERCLE", 59, 103),
               ("DN120", 105, 127), ("MANCHON(1)", 129, 174),
               ("1.00p.", 465, 490), ("68.46p.", 505, 535),
               ("68.46", 552, 575)]),
        (315, [("1674662", 20, 55), ("KIT", 59, 68),
               ("RESISTANCE", 70, 104), ("ELECTRIQUE", 106, 139),
               ("LONGUEUR", 231, 261), ("7735502657", 357, 402),
               ("1.00p.", 465, 490), ("233.66p.", 500, 535),
               ("233.66", 547, 575)]),
        (327, [("320MM(1)", 59, 95)]),
        (359, [("Montant", 59, 86), ("de", 88, 96), ("port", 98, 112),
               ("35.00", 512, 535), ("35.00", 552, 575)]),
        (382, [("(1)", 59, 68), ("a", 70, 74), ("livrer", 75, 93),
               ("le", 94, 101), ("26/02/2024", 102, 138)]),
    ])

    candidates = [
        extract_lines(all_rows([page])),
        extract_lines_from_pages([page]),
        extract_multiline_lines_from_pages([page]),
    ]
    for lines in candidates:
        assert len(lines) >= 2
        first, second = lines[:2]
        assert first.customer_material_number == "1663503"
        assert first.supplier_material_number is None
        assert first.material_number == "1663503"
        assert (first.quantity, first.uom, first.unit_price, first.line_total) == (
            1, "PCE", 68.46, 68.46,
        )
        assert second.customer_material_number == "1674662"
        assert second.supplier_material_number == "7735502657"
        assert second.material_number == "7735502657"
        assert (second.quantity, second.uom, second.unit_price, second.line_total) == (
            1, "PCE", 233.66, 233.66,
        )
        assert "320MM(1)" in second.description


def test_complete_header_does_not_swallow_product_and_supplier_shipping_rows():
    page = _page([
        (394, [("Reférence", 31, 75), ("-", 77, 80),
               ("Désignation", 83, 134), ("Quantite", 422, 459),
               ("Unite", 476, 499), ("Prix", 507, 524),
               ("Unitaire", 527, 560)]),
        (415, [("87167712610", 31, 86), ("-", 89, 92),
               ("PRESSOS.", 94, 140), ("1", 451, 456),
               ("PI", 479, 488), ("75.4800", 532, 564)]),
        (437, [("PORT", 31, 56), ("FOURNISSEUR", 59, 124),
               ("1", 451, 456), ("PI", 479, 488),
               ("20.0000", 532, 564)]),
        (479, [("Montant", 377, 420), ("HT", 423, 438),
               ("Net", 441, 458), ("95.48", 522, 550)]),
    ])
    rows = all_rows([page], y_factor=0.55)

    header = _find_table_header(rows, 3)
    assert header is not None
    assert header[:2] == (0, 1)

    lines = extract_lines_from_pages([page])
    assert len(lines) == 2
    product, shipping = lines
    assert product.material_number == "87167712610"
    assert product.description == "PRESSOS."
    assert (product.quantity, product.uom, product.unit_price, product.line_total) == (
        1, "PCE", 75.48, 75.48,
    )
    assert "calculated" in product.warnings[0]
    assert shipping.material_number is None
    assert shipping.description == "PORT FOURNISSEUR"
    assert (shipping.quantity, shipping.uom, shipping.unit_price, shipping.line_total) == (
        1, "PCE", 20, 20,
    )

    # Multiline parsing can retain the following charge row in the product's
    # raw evidence. The structured product identity must still win.
    product.raw_text += " | PORT FOURNISSEUR 1 PI 20.0000"

    po = SimpleNamespace(
        lines=lines,
        additional_charges=[],
        totals=Totals(total_net=95.48, total_before_tax=95.48, currency="EUR"),
    )
    po, reclassified = _separate_additional_charge_lines_v62(po, {})
    assert len(po.lines) == 1
    assert len(po.additional_charges) == 1
    assert po.additional_charges[0].charge_type == "shipping"
    assert po.additional_charges[0].amount == 20
    assert reclassified == {"shipping": 1}


def test_supplier_role_word_inside_shipping_row_is_not_a_party_label():
    page = _page([
        (100, [("PORT", 31, 56), ("FOURNISSEUR", 59, 124),
               ("1", 451, 456), ("PI", 479, 488),
               ("20.0000", 532, 564)]),
    ])

    parties = extract_parties(all_rows([page]))

    assert parties["supplier"].name is None

    po = SimpleNamespace(lines=[], additional_charges=[], totals=Totals(currency="EUR"))
    po, recovered = _merge_explicit_page_charges_v63(po, [page], {})
    assert recovered == {"shipping": 1}
    assert len(po.additional_charges) == 1
    charge = po.additional_charges[0]
    assert charge.description == "PORT FOURNISSEUR"
    assert (charge.quantity, charge.uom, charge.unit_price, charge.amount) == (
        1, "PCE", 20, 20,
    )


def test_promotion_note_is_not_description_and_legal_footer_stops_table():
    page = _page([
        (394, [("Reférence", 31, 75), ("-", 77, 80),
               ("Désignation", 83, 134), ("Quantite", 422, 459),
               ("Unite", 476, 499), ("Prix", 507, 524),
               ("Unitaire", 527, 560)]),
        (415, [("7733701938", 31, 86), ("-", 89, 92),
               ("CL5000M", 94, 140), ("125/5", 143, 170),
               ("E", 173, 180), ("PENT", 183, 210),
               ("6", 451, 456), ("PI", 479, 488),
               ("1499.7273", 520, 564)]),
        (430, [("Promo", 94, 125), ("PA", 128, 142),
               ("n°", 145, 157), ("26085", 160, 188)]),
        (455, [("Retrouvez", 20, 65), ("l'ensemble", 68, 115),
               ("de", 118, 128), ("nos", 131, 148),
               ("activités", 151, 195)]),
        (470, [("SIREN", 20, 55), ("716320619", 58, 110)]),
    ])

    lines = extract_lines_from_pages([page])

    assert len(lines) == 1
    assert lines[0].material_number == "7733701938"
    assert lines[0].description == "CL5000M 125/5 E PENT"
    assert lines[0].notes == ["Promo PA n° 26085"]
    assert "Retrouvez" not in (lines[0].raw_text or "")


def test_postprocessor_separates_promotion_continuation_from_description():
    line = LineItem(
        material_number="7733701938",
        description="CL5000M 125/5 E PENT Promo PA n° 26085",
        quantity=6,
        unit_price=1499.7273,
        line_total=8998.3638,
    )

    moved = _separate_promotional_continuation_notes_v5171([line])

    assert moved == 1
    assert line.description == "CL5000M 125/5 E PENT"
    assert line.notes == ["Promo PA n° 26085"]


def test_postprocessor_removes_html_quote_note_from_product_description():
    line = LineItem(
        material_number="EL7724001291",
        description="THERMOSTAT OR 100 <p>&nbsp;devis CCH-060126-0709</p>",
        quantity=6,
        unit_price=30,
        line_total=180,
    )

    moved = _separate_promotional_continuation_notes_v5171([line])

    assert moved == 1
    assert line.description == "THERMOSTAT OR 100"
    assert line.notes == ["devis CCH-060126-0709"]


def test_stacked_customer_and_supplier_references_use_net_price_without_losing_rows():
    page = _page([
        (180, [("Article", 10, 50), ("Designation", 120, 210),
               ("Quantite", 315, 360), ("U.F", 375, 395),
               ("P.U", 420, 442), ("Prix", 480, 505), ("net", 515, 538)]),
        (200, [("N/REF", 70, 105), (":", 108, 111),
               ("A", 120, 128), ("L'ATTENTION", 132, 198)]),
        (220, [("8718585355", 70, 125)]),
        (232, [("SONDE", 70, 105), ("EXTERIEURE", 110, 170),
               ("1,000", 320, 352), ("U", 378, 386),
               ("98,66", 420, 450), ("50,32", 510, 540),
               ("29/02/24", 548, 590), ("8", 594, 598)]),
        (248, [("C10921", 10, 50), ("|87167432990", 70, 132)]),
        (260, [("VASE", 70, 100), ("EXPANSION", 105, 165), ("5L", 170, 185),
               ("3,000", 320, 352), ("U", 378, 386),
               ("114,48", 415, 452), ("58,38", 510, 540),
               ("29/02/24", 548, 590), ("8", 594, 598)]),
        (276, [("C10937", 10, 50), ("|87167571380", 70, 132)]),
        (288, [("TETE", 70, 98), ("DE", 102, 116), ("PURGEUR", 120, 170),
               ("1,000", 320, 352), ("U", 378, 386),
               ("27,79", 420, 450), ("14,17", 510, 540),
               ("29/02/24", 548, 590), ("8", 594, 598)]),
        # The leading ``1`` is an OCR rendering of the same vertical rule as ``|``.
        (304, [("C19855", 10, 50), ("187181070500", 70, 137)]),
        (316, [("ELECTRODE", 70, 130), ("ALLUMAGE", 135, 190),
               ("2,000", 320, 352), ("U", 378, 386),
               ("21,63", 420, 450), ("11,03", 510, 540),
               ("29/02/24", 548, 590), ("8", 594, 598)]),
        (340, [("io", 70, 82), ("ATTENTION", 90, 150)]),
        (360, [("MT", 300, 315), ("H.T.", 320, 345), ("743,84", 510, 550)]),
    ])

    lines = extract_multiline_lines_from_pages([page])

    assert len(lines) == 4
    assert lines[0].material_number == "8718585355"
    assert lines[0].supplier_material_number == "8718585355"
    assert lines[0].customer_material_number is None
    assert (lines[0].gross_unit_price, lines[0].unit_price,
            lines[0].net_unit_price, lines[0].line_total) == (
                98.66, 50.32, 50.32, 50.32,
            )
    assert lines[1].customer_material_number == "C10921"
    assert lines[1].supplier_material_number == "87167432990"
    assert lines[1].material_number == "87167432990"
    assert (lines[1].quantity, lines[1].unit_price, lines[1].line_total) == (
        3, 58.38, 175.14,
    )
    assert lines[2].material_number == "87167571380"
    assert lines[3].customer_material_number == "C19855"
    assert lines[3].supplier_material_number == "87181070500"
    assert lines[3].material_number == "87181070500"
    assert lines[3].description == "ELECTRODE ALLUMAGE"
    assert "ATTENTION" not in lines[3].raw_text


def test_legal_footer_filter_does_not_match_ape_inside_soupape():
    product = LineItem(
        line_number="1", material_number="87167621440",
        description="SOUPAPE DE SECURITE 3BARS 15X21 FEM",
        quantity=3, unit_price=17.26, line_total=51.78,
        raw_text="SOUPAPE DE SECURITE 3BARS 15X21 FEM",
    )
    footer = LineItem(
        line_number="2", material_number="01565121900014",
        description="SIRET 015 651 219 00014 APE 4674 B",
        quantity=1, unit_price=1, line_total=1,
    )
    po = SimpleNamespace(
        lines=[product, footer], notes={},
        validation=SimpleNamespace(
            checks={"legal_footer_line_items": {"count": 1, "valid": False}},
            warnings=["Legal/footer text was structured as purchase-order line item(s): 1"],
            errors=[], status="WARNING",
        ),
    )

    po = _sanitize_legal_footer_lines_v48(po, {})
    po = _repair_legal_footer_validation_v65(po, {})

    assert [line.material_number for line in po.lines] == ["87167621440"]
    assert po.lines[0].description.startswith("SOUPAPE")
    assert po.validation.checks["legal_footer_line_items"]["valid"]
    assert po.validation.status == "PASS"


def test_quantity_column_prefers_unit_bound_number_over_product_dimension():
    page = _page([
        (100, [("Article", 20, 58), ("Designation", 150, 215),
               ("Quantite", 340, 385), ("Prix", 445, 470),
               ("Montant", 525, 575)]),
        (120, [("C26952", 20, 58), ("CHAUDIERE", 75, 125),
               ("18KW", 315, 340), ("U", 345, 352), ("4,000", 360, 388),
               ("U", 392, 399), ("560,00", 445, 480),
               ("2240,00", 525, 575)]),
    ])

    lines = extract_lines(all_rows([page]))

    assert len(lines) == 1
    assert lines[0].quantity == 4.0


def test_total_hors_taxe_is_net_and_legal_vat_identifier_is_not_amount():
    page = _page([
        (700, [("TOTAL", 390, 430), ("HORS", 435, 465),
               ("TAXE", 470, 500), ("EUR", 505, 530), ("337.12", 550, 580)]),
        (810, [("N.TVA", 350, 390), (":", 393, 396),
               ("FR57467200515", 400, 480)]),
    ])

    totals = extract_totals(all_rows([page]))

    assert totals.total_net == 337.12
    assert totals.total_vat is None
    assert totals.total_tax is None
    assert totals.grand_total is None


def test_labelled_shipping_charge_is_recovered_outside_line_hypotheses():
    page = _page([
        (359, [("Montant", 59, 86), ("de", 88, 96), ("port", 98, 112),
               ("35.00", 512, 535), ("35.00", 552, 575)]),
        (715, [("TOTAL", 400, 430), ("HORS", 435, 465),
               ("TAXE", 470, 500), ("337.12", 550, 580)]),
    ])
    po = SimpleNamespace(lines=[], additional_charges=[], totals=Totals(currency="EUR"))

    po, recovered = _merge_explicit_page_charges_v63(po, [page], {})

    assert recovered == {"shipping": 1}
    assert len(po.additional_charges) == 1
    assert po.additional_charges[0].charge_type == "shipping"
    assert po.additional_charges[0].amount == 35.0
    assert po.additional_charges[0].description == "Montant de port"
    assert po.totals.total_shipping == 35.0


def test_structured_net_total_is_not_overwritten_by_product_subtotal_fallback():
    page = _page([(
        700,
        [("TOTAL", 390, 430), ("HT", 435, 455), ("2.572,24", 520, 570)],
    )])
    po = SimpleNamespace(
        lines=[LineItem(quantity=1, unit_price=2572.24, line_total=2572.24)],
        additional_charges=[],
        totals=Totals(
            subtotal=2572.24,
            total_product_lines=2572.24,
            total_net=2616.55,
            total_before_tax=2616.55,
            currency="EUR",
        ),
    )

    _enhance_strict_totals_v47(po, [page], {})

    assert po.totals.subtotal == 2572.24
    assert po.totals.total_net == 2616.55
    assert po.totals.total_before_tax == 2616.55


def test_native_summary_freight_is_merged_once_as_a_shipping_charge():
    table = SimpleNamespace(
        data=[
            ["Total Brut HT Lignes produit", "+ Total taxes lignes",
             "+ Frais de port", "TOTAL NET HT"],
            ["2.572,24", "9,31", "35,00", "2.616,55"],
        ],
        source=SimpleNamespace(page=2),
        confidence=0.98,
    )
    po = SimpleNamespace(
        additional_charges=[],
        totals=Totals(currency="EUR"),
    )

    _enhance_totals_from_native_tables(po, [table])
    _enhance_totals_from_native_tables(po, [table])

    assert po.totals.subtotal == 2572.24
    assert po.totals.total_tax == 9.31
    assert po.totals.total_freight == 35
    assert po.totals.total_net == 2616.55
    assert len(po.additional_charges) == 1
    charge = po.additional_charges[0]
    assert charge.charge_type == "shipping"
    assert charge.amount == 35
    assert charge.evidence.extraction_method == "explicit_native_summary_charge_v5175"


def test_charge_recovery_ignores_summary_grid_and_exact_source_duplicate():
    page = _page([
        (359, [("Eco", 59, 82), ("Contribution", 86, 145), ("unit.", 150, 176),
               ("HT", 180, 195), (":", 198, 201), ("1,75", 552, 575)]),
        (700, [("Montant", 20, 65), ("HT", 70, 85), ("Ecotaxe", 100, 145),
               ("HT", 150, 165), ("Port", 180, 205), ("HT", 210, 225),
               ("%", 240, 247), ("TVA", 250, 272), ("Total", 400, 430),
               ("TTC", 435, 455), ("1", 505, 512), ("372,27", 540, 580)]),
        (720, [("TOTAL", 20, 55), ("ECO-PARTICIPATION", 60, 175),
               ("(HT)", 180, 205), ("9,44", 550, 580)]),
    ])
    existing = AdditionalCharge(
        charge_type="environmental_fee",
        description="Eco Contribution unit. HT",
        amount=3.5,
        page=1,
        evidence=Evidence(page=1, source_text="Eco Contribution unit. HT : 1,75"),
    )
    po = SimpleNamespace(lines=[], additional_charges=[existing], totals=Totals(currency="EUR"))
    po, recovered = _merge_explicit_page_charges_v63(po, [page], {})
    assert recovered == {}
    assert po.additional_charges == [existing]


def test_libelle_family_and_prix_total_header_extracts_complete_product_line():
    page = _page([
        (180, [("Code", 20, 48), ("article", 50, 91), ("Libelle", 110, 166),
               ("Famille", 275, 329), ("Quantite", 385, 435),
               ("PU", 465, 482), ("H.T.", 484, 505),
               ("Prix", 525, 550), ("total", 552, 585)]),
        (220, [("87167717410", 20, 88), ("Flexible", 110, 151),
               ("de", 155, 167), ("manometre", 171, 228),
               ("Pieces", 278, 316), ("detachees", 321, 371),
               ("1.00", 397, 429), ("8.55EUR", 466, 505),
               ("8.55EUR", 548, 590)]),
        (250, [("TOTAL", 20, 58), ("8.55EUR", 548, 590)]),
    ])

    lines = extract_lines_from_pages([page])

    assert len(lines) == 1
    assert lines[0].material_number == "87167717410"
    assert lines[0].description == "Flexible de manometre"
    assert lines[0].product_family == "Pieces detachees"
    assert (lines[0].quantity, lines[0].unit_price, lines[0].line_total) == (1, 8.55, 8.55)


def test_geometry_separates_family_from_libelle_for_prix_total_layout():
    page = _page([
        (180, [("Code", 20, 48), ("article", 50, 91), ("Libelle", 110, 166),
               ("Famille", 275, 329), ("Quantite", 385, 435),
               ("PU", 465, 482), ("H.T.", 484, 505),
               ("Prix", 525, 550), ("total", 552, 585)]),
        (220, [("87167717410", 20, 88), ("Flexible", 110, 151),
               ("de", 155, 167), ("manometre", 171, 228),
               ("Pieces", 278, 316), ("detachees", 321, 371),
               ("1.00", 397, 429), ("8.55", 466, 500),
               ("8.551", 548, 590)]),
        (250, [("TOTAL", 20, 58), ("8.551", 548, 590)]),
        (270, [("FRAIS", 20, 55), ("DE", 58, 72), ("PORT", 75, 105),
               ("0.00", 548, 590)]),
    ])

    lines, charges = _extract_geometry_table_items_v46([page], {})

    assert len(charges) == 1
    assert charges[0].charge_type == "shipping"
    assert charges[0].description == "FRAIS DE PORT"
    assert charges[0].amount == 0.0
    assert len(lines) == 1
    assert lines[0].description == "Flexible de manometre"
    assert lines[0].product_family == "Pieces detachees"
    assert (lines[0].quantity, lines[0].unit_price, lines[0].line_total) == (1, 8.55, 8.551)


def test_repeated_aligned_euro_as_one_artifact_is_repaired_by_arithmetic():
    page = _page([
        (180, [("Prix", 525, 550), ("total", 552, 585)]),
        (220, [("87167717410", 20, 88), ("1.00", 397, 429),
               ("8.55", 466, 500), ("8.551", 548, 590)]),
        (250, [("TOTAL", 20, 58), ("8.551", 548, 590)]),
        (270, [("FRAIS", 20, 55), ("DE", 58, 72), ("PORT", 75, 105),
               ("0.001", 548, 590)]),
        (290, [("TOTAL", 20, 58), ("H.T.", 61, 83), ("8.551", 548, 590)]),
    ])
    page.source_type = "scan_ocr"
    line = LineItem(
        material_number="87167717410", description="Flexible de manometre",
        quantity=1, unit_price=8.55, line_total=8.551, line_net_amount=8.551,
        page=1,
    )
    charge = AdditionalCharge(
        charge_type="shipping", description="FRAIS DE PORT 0.001",
        amount=0.001, page=1,
    )
    po = SimpleNamespace(
        lines=[line], additional_charges=[charge],
        totals=Totals(
            total_shipping=0.001, total_net=8.551, total_before_tax=8.551,
            total_gross=8.551, total_after_tax=8.551, grand_total=8.551,
            amount_due=8.551, currency="EUR",
        ),
    )

    po, repairs = _repair_scan_currency_suffix_artifacts_v64(po, [page], {})

    assert po.lines[0].line_total == 8.55
    assert po.lines[0].line_net_amount == 8.55
    assert po.additional_charges[0].amount == 0.0
    assert po.totals.total_shipping == 0.0
    assert po.totals.total_net == 8.55
    assert po.totals.grand_total == 8.55
    assert repairs["line_total"] == 1
    assert repairs["additional_charge"] == 1
    assert repairs["document_total"] >= 3


def test_single_three_decimal_scan_value_is_not_rewritten_without_page_signature():
    page = _page([
        (180, [("Prix", 525, 550), ("total", 552, 585)]),
        (220, [("TOTAL", 20, 58), ("8.551", 548, 590)]),
    ])
    page.source_type = "scan_ocr"
    line = LineItem(quantity=1, unit_price=8.55, line_total=8.551, page=1)
    po = SimpleNamespace(lines=[line], additional_charges=[], totals=Totals(total_net=8.551))

    po, repairs = _repair_scan_currency_suffix_artifacts_v64(po, [page], {})

    assert po.lines[0].line_total == 8.551
    assert po.totals.total_net == 8.551
    assert repairs == {}


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


def test_geometry_recognizes_purchase_price_pa_header_and_free_type_error_total():
    page = _page([
        (180, [("Code", 30, 53), ("art", 55, 69), ("Référence", 79, 122),
               ("Désignation", 239, 291), ("Qte", 411, 428), ("U", 441, 450),
               ("P.A", 463, 480), ("HT", 484, 499), ("Montant", 505, 543), ("HT", 545, 560)]),
        (220, [("H51094", 29, 61), ("7716780589", 68, 119), ("CACHE", 150, 185),
               ("TUBES", 190, 225), ("3.000", 411, 434), ("U", 439, 446),
               ("30.00", 475, 498), ("90.00", 523, 546), ("€", 548, 553)]),
        (250, [("H37381", 29, 61), ("8733501984", 68, 119), ("CLIMATISEUR", 150, 220),
               ("1.000", 411, 434), ("U", 439, 446), ("0.00", 480, 498),
               ("#Type!", 515, 545), ("€", 548, 553)]),
    ])
    lines, charges = _extract_geometry_table_items_v46([page], {})
    assert charges == []
    assert len(lines) == 2
    assert lines[0].material_number == "7716780589"
    assert lines[0].article_number == "7716780589"
    assert lines[0].customer_material_number == "H51094"
    assert lines[0].supplier_material_number == "7716780589"
    assert (lines[0].quantity, lines[0].unit_price, lines[0].line_total) == (3, 30, 90)
    assert lines[1].material_number == "8733501984"
    assert lines[1].article_number == "8733501984"
    assert lines[1].customer_material_number == "H37381"
    assert lines[1].supplier_material_number == "8733501984"
    assert (lines[1].quantity, lines[1].unit_price, lines[1].line_total) == (1, 0, 0)


def test_hypothesis_total_fit_rewards_complete_table_and_penalizes_sparse_subset():
    complete = [
        LineItem(material_number="7716704752", quantity=9, unit_price=760, line_total=6840),
        LineItem(material_number="7738111040", quantity=6, unit_price=95.47, line_total=572.82),
        LineItem(material_number="7738112343", quantity=1, unit_price=41.21, line_total=41.21),
    ]
    target = 7454.03

    complete_fit = _line_hypothesis_total_fit_v66(complete, [], target, {})
    sparse_fit = _line_hypothesis_total_fit_v66(complete[1:], [], target, {})

    assert complete_fit["valid"] is True
    assert complete_fit["adjustment"] > 0
    assert sparse_fit["valid"] is False
    assert sparse_fit["adjustment"] < 0
    assert sparse_fit["document_sum"] == 614.03


def test_partial_invalid_hypothesis_cannot_replace_supported_complete_table():
    current_result = (
        0.45,
        {"supported_lines": 6},
        {"valid": False},
        1.0,
    )
    sparse_result = (
        1.48,
        {"supported_lines": 3},
        {"valid": False},
        0.3333,
    )

    rejection = _partial_line_hypothesis_rejection_v5175(
        "native_table", sparse_result, 3, current_result, 9, {}
    )

    assert rejection["reason"] == "partial_coverage_vs_current_supported_lines"
    assert rejection["candidate_supported_lines"] == 3
    assert rejection["current_supported_lines"] == 6


def test_partial_hypothesis_is_allowed_when_it_matches_printed_total():
    current_result = (0.45, {"supported_lines": 6}, {"valid": False}, 1.0)
    reconciled_result = (1.48, {"supported_lines": 3}, {"valid": True}, 0.3333)

    assert _partial_line_hypothesis_rejection_v5175(
        "native_table", reconciled_result, 3, current_result, 9, {}
    ) is None


def test_product_fee_table_ignores_interleaved_supplier_offer_annotations():
    table = SimpleNamespace(
        data=[
            [
                "Lig.", "Produit", "Qte", "Condit.", "Prix Unitaire",
                "Montant ligne HT", "Date de livraison",
            ],
            [
                "4\n5\n6\n7\n8\n9",
                "\n".join([
                    "7733703987 00481 G10-4 - HOMECOM EASY WI-FI",
                    "Communication au Fournisseur :",
                    "OFFRE DU 011026 PAR MAIL 7733703987",
                    "7709003525 MELANGEUR ONDEA C LC10/LC11",
                    "7736995063 AZ 417 - ALLONGE 750 MM 60/100",
                    "7736507169 ONDEA HYDROSMART LC15-5 HFP N",
                    "D3EES14080 DEEE APPAREIL DE CHAUFFAGE ELECTRIQUE > 5KG",
                    "7736507485 CADRE ECARTEUR ONDEA HYDROSMART BAS NOX2",
                    "7738111017 THERMOSTAT CR10H",
                    "D3EES95010 DEEE EQUIPEMENT DE CONTROLE ET DE SURVEILLANCE",
                ]),
                "2\n1\n1\n1\n1\n1\n1\n1",
                "PIECE\nPIECE\nPIECE\nPIECE\nPIECE\nPIECE",
                "40,00000\n28,64250\n46,23000\n683,40000\n1,75000\n48,74250\n53,96000\n0,14000",
                "80,00000\n28,64250\n46,23000\n683,40000\n1,75000\n48,74250\n53,96000\n0,14000",
                "12-10-2026\n12-10-2026\n12-10-2026\n12-10-2026\n12-10-2026\n12-10-2026",
            ],
        ],
        source=SimpleNamespace(page=2),
        confidence=0.98,
    )

    lines, charges = _rexellike_product_fee_table(table)

    assert [line.line_number for line in lines] == ["4", "5", "6", "7", "8", "9"]
    assert [line.material_number for line in lines] == [
        "7733703987", "7709003525", "7736995063",
        "7736507169", "7736507485", "7738111017",
    ]
    assert [line.line_total for line in lines] == pytest.approx([
        80, 28.6425, 46.23, 683.4, 48.7425, 53.96,
    ])
    assert [charge.amount for charge in charges] == pytest.approx([1.75, 0.14])
    assert lines[0].raw_text.startswith("4 | 7733703987")


@pytest.mark.parametrize("source_code", [".7733703987", ".7098850"])
@pytest.mark.parametrize("offer_note", [
    "Offre FGO-180926-1132",
    "Offre FGO-180926-1132 frais de port offerts",
])
def test_product_table_strips_customer_dot_prefix_and_keeps_offer_as_note(source_code, offer_note):
    table = SimpleNamespace(
        data=[
            [
                "Lig.", "Produit", "Qté", "Condit.", "Prix Unitaire",
                "Montant ligne HT", "Date de livraison",
            ],
            [
                "1",
                f"{source_code} 00645 MODULE WIFI CLE USB HOMECOM EASY\n"
                f"Communication au Fournisseur :\n{offer_note}",
                "4", "PIECE", "0,10000", "0,40000", "28-10-2026",
            ],
        ],
        source=SimpleNamespace(page=1),
        confidence=0.98,
    )

    lines, charges = _rexellike_product_fee_table(table)

    assert len(lines) == 1
    assert not charges
    line = lines[0]
    assert line.material_number == line.article_number == source_code[1:]
    assert line.description == "00645 MODULE WIFI CLE USB HOMECOM EASY"
    assert line.quantity == 4
    assert line.unit_price == pytest.approx(0.1)
    assert line.line_total == pytest.approx(0.4)
    assert line.requested_delivery_date == "2026-10-28"
    assert line.quote_number == "FGO-180926-1132"
    assert line.quote_numbers == ["FGO-180926-1132"]
    assert line.notes == [offer_note]
    assert f"{source_code} 00645" in line.raw_text


def test_detached_quote_label_is_not_a_product_even_with_borrowed_amount():
    line = LineItem(
        line_number="1", material_number="Offre", article_number="Offre",
        description="FGO-180926-1132", quantity=4, unit_price=0.1,
        line_total=0.4, raw_text="1 | Offre FGO-180926-1132 | 4 | 0.1 | 0.4",
    )

    kept, reasons = _sanitize_non_product_lines_v61([line], load_config())

    assert kept == []
    assert reasons == {"quote_note_as_product": 1}


def test_complete_dual_reference_table_beats_sparse_material_anchor_hypothesis():
    page = _page([
        (180, [("Code", 30, 53), ("art", 55, 69), ("Reference", 79, 122),
               ("Designation", 239, 291), ("Qte", 411, 428), ("U", 441, 450),
               ("P.A", 463, 480), ("HT", 484, 499), ("Montant", 505, 543), ("HT", 545, 560)]),
        (220, [("H51087", 29, 61), ("7716704752", 68, 119), ("#3p", 122, 143),
               ("-", 145, 150), ("CHAUDIERE", 152, 220),
               ("9.000", 411, 434), ("U", 439, 446), ("760.00", 470, 498),
               ("6840.00", 515, 546)]),
        (250, [("H55337", 29, 61), ("7738111040", 68, 119), ("BOS", 122, 145),
               ("REGULATEUR", 150, 220),
               ("6.000", 411, 434), ("U", 439, 446), ("95.47", 475, 498),
               ("572.82", 520, 546)]),
        (280, [("H38621", 29, 61), ("7738112343", 68, 119), ("SONDE", 150, 190),
               ("1.000", 411, 434), ("U", 439, 446), ("41.21", 475, 498),
               ("41.21", 523, 546)]),
        (310, [("H40786", 29, 61), ("7738112714", 68, 119), ("ADAPTATEUR", 150, 220),
               ("6.000", 411, 434), ("U", 439, 446), ("12.69", 475, 498),
               ("76.14", 523, 546)]),
    ])
    target = 7530.17
    po = PurchaseOrderResult(
        document=DocumentMeta(
            id="dual-reference-test",
            filename="dual-reference.pdf",
            extension=".pdf",
            file_size=1,
            sha256="0" * 64,
            page_count=1,
            is_pdf=True,
            is_native_pdf=True,
            has_text_layer=True,
            processing_timestamp="2026-10-14T00:00:00Z",
        ),
        pages=[page],
        totals=Totals(
            subtotal=target,
            total_net=target,
            total_before_tax=target,
            currency="EUR",
        ),
    )

    result = enhance_purchase_order_with_native_structure(po, [], [page], {})

    assert result.validation.checks["line_parser_hypothesis"]["selected"] == "geometry"
    assert len(result.lines) == 4
    assert [line.material_number for line in result.lines] == [
        "7716704752", "7738111040", "7738112343", "7738112714",
    ]
    assert [line.customer_material_number for line in result.lines] == [
        "H51087", "H55337", "H38621", "H40786",
    ]
    assert result.lines[0].description.startswith("#3p - CHAUDIERE")
    assert result.lines[1].description.startswith("BOS REGULATEUR")
    assert sum(line.line_total for line in result.lines) == pytest.approx(target)


def test_geometry_types_customer_code_right_of_supplier_reference():
    page = _page([
        (180, [("Reference", 25, 70), ("Adressage", 115, 160),
               ("Designation", 170, 225), ("Quantite", 385, 425),
               ("PU", 435, 447), ("net", 450, 463),
               ("Montant", 502, 536), ("HT", 539, 551)]),
        (220, [("7733701564", 22, 72), ("Z43-08", 116, 145),
               ("UNITE", 169, 195), ("INTERIEURE", 198, 251),
               ("2.00", 400, 422), ("104.00", 474, 501),
               ("208.00", 526, 553)]),
    ])

    lines, charges = _extract_geometry_table_items_v46([page], {})

    assert charges == []
    assert len(lines) == 1
    assert lines[0].material_number == "7733701564"
    assert lines[0].supplier_material_number == "7733701564"
    assert lines[0].customer_material_number == "Z43-08"
    assert (lines[0].quantity, lines[0].unit_price, lines[0].line_total) == (2, 104, 208)


def test_material_pattern_reassembles_split_product_and_environmental_fee_rows():
    page = _page([
        (267, [("CODE", 25, 51), ("REF", 387, 405)]),
        (272, [("DESIGNATION", 180, 243), ("QUANTITE", 445, 491),
               ("PRIX", 500, 521), ("MONTANT", 533, 578)]),
        (278, [("INTERNE", 18, 58), ("FOURNISSEUR", 367, 433)]),
        (309, [("1506621", 20, 55), ("MODULE", 59, 89), ("WIFI", 91, 106),
               ("HOMECOM", 145, 183), ("EASY", 185, 204), ("2.00p.", 465, 490),
               ("p.", 528, 535)]),
        (320, [("7736606771", 59, 98)]),
        (331, [("ECO", 59, 74), ("PARTICIPATION", 76, 131),
               ("2.00p.", 465, 490), ("0.14p.", 510, 535), ("0.28", 558, 575)]),
    ])
    lines, charges = _material_anchored_split_table_items_v61([page], {})
    assert len(lines) == 1
    line = lines[0]
    assert line.material_number == "7736606771"
    assert line.customer_material_number == "1506621"
    assert line.supplier_material_number == "7736606771"
    assert line.description == "MODULE WIFI HOMECOM EASY"
    assert (line.quantity, line.uom) == (2, "PCE")
    assert line.unit_price is None and line.line_total is None
    assert len(charges) == 1
    assert charges[0].charge_type == "environmental_fee"
    assert (charges[0].quantity, charges[0].unit_price, charges[0].amount) == (2, 0.14, 0.28)
    assert charges[0].supplier_reference == "7736606771"


def test_non_product_filter_removes_date_footer_and_empty_parser_fragments():
    lines = [
        LineItem(material_number="23/12/2025", description="Poids : kg TOTAL HORS",
                 quantity=1183.29, unit_price=1183.29, line_total=1400175.2241),
        LineItem(material_number="7716780266", description=""),
        LineItem(material_number="8719999999", description="Produit partiellement lu"),
        LineItem(material_number="REF-OK", description="Produit complet", quantity=2, unit_price=10),
    ]
    cleaned, rejected = _sanitize_non_product_lines_v61(lines, {})
    assert [line.material_number for line in cleaned] == ["8719999999", "REF-OK"]
    assert rejected == {"date_footer_as_product": 1, "non_commercial_fragment": 1}


def test_material_anchor_recovers_degraded_numeric_row_and_wrapped_description():
    page = _page([
        (180, [("Article", 10, 45), ("Désignation", 120, 190),
               ("unit.", 390, 420), ("Net", 455, 480), ("Montant", 525, 570)]),
        (220, [("7716780266", 20, 85), ("1,000", 310, 340), ("PIEC", 350, 380),
               ("144,00", 405, 440), ("49,75", 450, 480),
               ("72,36", 500, 530), ("72,36", 550, 580), ("6", 590, 595)]),
        (235, [("THERMOSTAT", 20, 90), ("D'AMBIANCE", 95, 160),
               ("FILAIRE", 165, 215), ("TRL", 220, 245)]),
    ])
    lines = _material_anchored_numeric_row_items_v61([page], {})
    assert len(lines) == 1
    line = lines[0]
    assert line.material_number == "7716780266"
    assert line.supplier_material_number == "7716780266"
    assert line.description == "THERMOSTAT D'AMBIANCE FILAIRE TRL"
    assert (line.quantity, line.uom, line.unit_price, line.line_total) == (1, "PCE", 72.36, 72.36)


def test_material_anchor_keeps_long_table_context_and_reconciles_one_hidden_amount():
    rows = [
        (180, [("Article", 10, 45), ("Désignation", 120, 190),
               ("unit.", 390, 420), ("Net", 455, 480), ("Montant", 525, 570)]),
    ]
    values = [
        ("7733701997", 5, "242,00", "98,01", "490,05"),
        ("7733701568", 2, "463,00", "187,52", "375,03"),
        ("7733701809", 2, "655,00", "265,28", "530,55"),
        ("7733701935", 2, "2398,00", "971,19", "1942,38"),
        ("7733701936", 2, "2736,00", "1108,08", "2216,16"),
        ("7733701937", 2, "3261,00", None, None),
        ("7733701995", 2, "1118,00", "503,10", "1006,20"),
        ("7733703059", 1, "410,67", "410,67", "410,67"),
    ]
    for index, (reference, quantity, list_price, net_price, amount) in enumerate(values):
        y = 220 + index * 30
        cells = [
            (reference, 20, 85), (f"{quantity},000", 310, 340),
            ("PIEC", 350, 380), (list_price, 405, 440),
            ("55,00", 450, 480),
        ]
        if net_price and amount:
            cells.extend([(net_price, 500, 530), (amount, 550, 580)])
        cells.append(("6", 590, 595))
        rows.append((y, cells))
        rows.append((y + 12, [
            ("PAC", 20, 45), ("CLIMATE", 50, 95),
            (f"MODELE{index + 1}", 100, 155), ("+10,00", 450, 480),
        ]))
    page = _page(rows)

    lines = _material_anchored_numeric_row_items_v61(
        [page], {}, target_total=9612.45
    )

    assert len(lines) == 8
    assert [line.material_number for line in lines] == [value[0] for value in values]
    assert round(sum(line.line_total for line in lines), 2) == 9612.45
    recovered = lines[5]
    assert (recovered.unit_price, recovered.line_total) == (1320.71, 2641.41)
    assert "net_price_recovered_from_printed_discount_chain" in recovered.warnings
    assert "line_amount_recovered_from_reconciled_document_residual" in recovered.warnings


def test_port_franco_does_not_turn_delivery_postal_code_into_shipping_cost():
    po = PurchaseOrderResult(document=DocumentMeta(
        id="x", filename="x.pdf", extension="pdf", file_size=1, sha256="x",
        page_count=1, is_pdf=True, processing_timestamp="2026-01-01T00:00:00Z",
        model_version="test",
    ))
    po.commercial.freight_terms = "Port franco"
    po.commercial.freight_amount = 59840.0
    po.commercial.shipping_cost = 59840.0
    po.totals.total_shipping = 59840.0
    po.totals.total_freight = 59840.0
    page = _page([(370, [
        ("Port", 20, 45), ("franco", 50, 85),
        ("59840", 300, 335), ("PERENCHIES", 340, 410),
    ])])

    _enhance_inline_transaction_header_v46(po, [page], {})
    _sanitize_double_counted_shipping_v47(po, {})

    assert po.logistics.shipping_method is None
    assert po.commercial.freight_terms == "Port franco"
    assert po.commercial.freight_amount is None
    assert po.commercial.shipping_cost is None
    assert po.totals.total_shipping is None
    assert po.totals.total_freight is None


def test_price_then_quantity_layout_calculates_missing_line_amount():
    page = _page([
        (435, [("Ref", 20, 33), ("commande", 35, 78), ("N°", 131, 141), ("offre", 142, 161),
               ("de", 163, 173), ("prix", 175, 190), ("Ref", 232, 245), ("fournisseur", 247, 291),
               ("Code", 354, 374), ("i", 375, 378), ("COPITOLE", 380, 418),
               ("Désignation", 443, 489), ("Tarif", 606, 624), ("unitaire", 626, 656),
               ("HT", 658, 669), ("Quantité", 689, 723)]),
        (456, [("06-253460", 31, 72), ("(vide)", 142, 164), ("87168354790", 299, 351),
               ("T36272", 365, 393), ("Kit", 454, 464), ("vase", 466, 483),
               ("d'expansion", 485, 530), ("93,03", 664, 686), ("1", 723, 728)]),
    ])
    lines = _price_then_quantity_table_items([page])
    assert len(lines) == 1
    assert lines[0].material_number == "87168354790"
    assert lines[0].supplier_material_number == "87168354790"
    assert lines[0].customer_material_number == "T36272"
    assert lines[0].description == "Kit vase d'expansion"
    assert (lines[0].quantity, lines[0].unit_price, lines[0].line_total) == (1, 93.03, 93.03)


def test_standalone_header_date_is_used_as_order_date():
    page = _page([(370, [("Date", 20, 45), ("11/08/2026", 55, 112)])])
    page.width=842
    page.height=595
    po = SimpleNamespace(
        purchase_order=PurchaseOrderHeader(),
        supplier=SimpleNamespace(code=None),
        buyer=SimpleNamespace(name=None,legal_name=None,contact=SimpleNamespace(name=None,phone=None)),
    )
    _enhance_inline_transaction_header_v46(po,[page],{})
    assert po.purchase_order.order_date.value == "2026-08-11"
    assert po.purchase_order.order_date.evidence.extraction_method == "standalone_header_date"


def test_two_row_erp_layout_aligns_description_quantity_and_commercial_row():
    page = _page([
        (340, [("DESIGNATION",20,110),("QUANTITES",300,350),("DELAI",390,425),("MONTANT",520,575)]),
        (355, [("CODE",20,55),("REFERENCE",90,145),("FOURNISSEUR",150,225),("Sem/Ann",390,430),("P.U",470,490),("NET",515,535),("H.T.",540,565)]),
        (380, [("ADAPTATEUR",20,90),("RF",95,110),("OXYLIS",115,160),("2,00",315,340),("14/26",390,425)]),
        (395, [("5560438",20,60),("7716780588",90,145),("/",155,158),("DEROG",165,205),("VBE-030226-0929",210,295),("PIECE",360,400),("20,00",470,495),("40,00",525,550)]),
        (418, [("CHAUDIERE",20,85),("MURALE",90,145),("GAZ",150,175),("5,00",315,340),("14/26",390,425)]),
        (433, [("4913906",20,60),("7716705092",90,145),("7716705092",150,205),("/",210,213),("DEROG",220,260),("VBE-030226-0929",265,350),("PIECE",360,400),("615,00",465,500),("3.075,00",515,560)]),
        (470, [("HORAIRES",20,80),("DE",85,100),("RECEPTION",105,175)]),
    ])
    lines=_two_row_erp_table_items([page])
    assert len(lines)==2
    assert (lines[0].material_number,lines[0].supplier_material_number)==("7716780588","7716780588")
    assert lines[0].customer_material_number == "5560438"
    assert (lines[0].quantity,lines[0].unit_price,lines[0].line_total)==(2,20,40)
    assert lines[0].delivery_week=="14/26"
    assert (lines[1].material_number,lines[1].supplier_material_number)==("7716705092","7716705092")
    assert lines[1].customer_material_number == "4913906"
    assert (lines[1].quantity,lines[1].unit_price,lines[1].line_total)==(5,615,3075)


def test_semantic_text_table_recovers_quantity_first_product_and_shipping():
    page = _page([
        (180, [("oté", 20, 42), ("Désignation", 70, 150), ("Vos", 300, 320),
               ("Références", 325, 390), ("Prix", 500, 525), ("Unit.", 530, 560)]),
        (220, [("1,000", 20, 55), ("PCE", 60, 90), ("CONSOLE", 110, 170),
               ("CL5000IU", 175, 235), ("CN35E", 240, 280), ("7733701995", 320, 390),
               ("497,48/PCE", 500, 570)]),
        (250, [("1,000", 20, 55), ("PCE", 60, 90), ("TRANSPORT", 110, 190),
               ("55,00/PCE", 500, 570)]),
    ])
    lines, charges = _semantic_text_table_items_v68([page], {})
    assert len(lines) == 1 and len(charges) == 1
    assert lines[0].material_number == "7733701995"
    assert lines[0].description == "CONSOLE CL5000IU CN35E"
    assert (lines[0].quantity, lines[0].unit_price, lines[0].line_total) == (1, 497.48, 497.48)
    assert charges[0].charge_type == "shipping" and charges[0].amount == 55


def test_semantic_text_table_recovers_dual_reference_rows_and_unit_eco_fees():
    page = _page([
        (180, [("Numéro", 10, 55)]),
        (195, [("d'article", 10, 60), ("Réf.fournisseur", 70, 160), ("Description", 170, 250),
               ("Unité", 300, 335), ("Quantité", 345, 395), ("Prix", 410, 435),
               ("unit.net", 440, 485), ("Remise", 490, 530), ("Montant", 535, 585)]),
        (230, [("832404", 10, 50), ("7733701933", 70, 135), ("UNITE", 170, 205),
               ("EXTERIEURE", 210, 280), ("UN", 300, 318), ("1,00", 350, 380),
               ("810,13", 415, 450), ("0,00", 490, 520), ("810,13", 540, 580)]),
        (245, [("Eco", 170, 190), ("Contribution", 195, 255), ("unit.", 260, 285),
               ("HT", 290, 305), (":", 308, 311), ("7,69", 540, 570)]),
        (270, [("779847", 10, 50), ("7733701566", 70, 135), ("UNITE", 170, 205),
               ("INTERIEURE", 210, 275), ("UN", 300, 318), ("2,00", 350, 380),
               ("161,12", 415, 450), ("0,00", 490, 520), ("322,24", 540, 580)]),
        (285, [("Eco", 170, 190), ("Contribution", 195, 255), ("unit.", 260, 285),
               ("HT", 290, 305), (":", 308, 311), ("1,75", 540, 570)]),
    ])
    lines, charges = _semantic_text_table_items_v68([page], {})
    assert [line.material_number for line in lines] == ["7733701933", "7733701566"]
    assert [line.customer_material_number for line in lines] == ["832404", "779847"]
    assert [charge.amount for charge in charges] == [7.69, 3.5]
    assert sum(line.line_total for line in lines) + sum(charge.amount for charge in charges) == 1143.56


def test_semantic_text_table_recovers_noisy_two_row_erp_and_reference_roles():
    page = _page([
        (180, [("DESIGNATION", 20, 110), ("QUANTITES", 300, 350),
               ("DELAT", 390, 425), ("MONTANT", 520, 575)]),
        (195, [("CODE", 20, 55), ("REFERENCE", 90, 145), ("FOURNISSEUR", 150, 225),
               ("Sem/Ann", 390, 430), ("P.U", 470, 490), ("NET", 515, 535)]),
        (220, [("PLATINE", 20, 80), ("ELECTRONIQUE", 85, 170), ("ELM;REF", 175, 220),
               ("8738712225", 225, 285), ("4,00", 315, 345), ("131/26", 390, 430)]),
        (235, [("4004868", 20, 65), ("87387712225", 90, 165), ("PIECE", 360, 400),
               ("262,20", 470, 505), ("1.048,80", 525, 575)]),
        (245, [("****33816", 20, 75)]),
        (260, [("MODULE", 20, 75), ("WI-FI", 80, 115), ("6,00", 315, 345),
               ("31/26", 390, 430)]),
        (275, [("1325892", 20, 65), ("PIECE", 360, 400), ("33,75", 470, 505),
               ("202,50", 525, 575)]),
        (290, [("7736606771", 90, 155)]),
    ])
    lines, charges = _semantic_text_table_items_v68([page], {})
    assert not charges and len(lines) == 2
    assert (lines[0].material_number, lines[0].customer_material_number) == ("8738712225", "4004868")
    assert lines[0].description == "PLATINE ELECTRONIQUE ELM;REF"
    assert lines[0].delivery_week == "31/26"
    assert (lines[1].material_number, lines[1].customer_material_number) == ("7736606771", "1325892")


def test_semantic_text_table_recovers_nbr_uac_products_and_port():
    page = _page([
        (180, [("CODE", 10, 45), ("NBR", 50, 75), ("en", 80, 95),
               ("DESIGNATION", 110, 200), ("NBR", 250, 275), ("EN", 280, 295),
               ("UAC", 300, 330), ("PRIX", 410, 440), ("ACH", 445, 475),
               ("MONTANT", 520, 580), ("HT", 585, 598)]),
        (220, [("63043454", 10, 65), ("1,00", 75, 95), ("JOINT", 110, 150),
               ("D'ÉTANCHÉITÉ", 155, 235), ("120X152X10MM", 240, 325),
               ("1,00", 335, 365), ("PCE", 370, 395), ("9,60", 450, 480),
               ("9,60", 540, 570)]),
        (245, [("8735100865", 10, 75), ("1", 80, 87), ("ANODE", 110, 155),
               ("M8X1280", 160, 220), ("D=33", 225, 255), ("1,00", 335, 365),
               ("PCE", 370, 395), ("83,40", 445, 480), ("83,40", 535, 575)]),
        (270, [("1", 10, 17), ("FRAIS", 110, 150), ("DE", 155, 170), ("PORT", 175, 210),
               ("1,00", 335, 365), ("PCE", 370, 395), ("20,00", 445, 480),
               ("20,00", 535, 575)]),
    ])
    lines, charges = _semantic_text_table_items_v68([page], {})
    assert [line.material_number for line in lines] == ["63043454", "8735100865"]
    assert [line.line_total for line in lines] == [9.6, 83.4]
    assert len(charges) == 1 and charges[0].charge_type == "shipping" and charges[0].amount == 20


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


def test_geometry_recovers_multipage_quantity_unit_price_table_without_amount_column():
    header = [
        ("Reference", 30, 78), ("-", 81, 85),
        ("Designation", 89, 145), ("Quantite", 422, 459),
        ("Unite", 476, 499), ("Prix", 507, 524),
        ("Unitaire", 527, 561),
    ]
    page_one = _page([
        (180, header),
        (220, [
            ("7736507169", 31, 81), ("-", 84, 87),
            ("CHAUFFE", 89, 132), ("BAINS", 135, 163),
            ("NOX", 313, 333), ("2", 335, 340), ("LC15", 343, 365),
            ("1", 451, 457), ("PI", 479, 488), ("683.4000", 527, 564),
        ]),
        (235, [("Promo", 45, 75), ("PA", 78, 91), ("26085", 106, 134)]),
        (250, [
            ("7733704055", 31, 81), ("-", 84, 87),
            ("REMPLACE", 229, 280), ("7733701870", 282, 332),
            ("1", 451, 457), ("PI", 479, 488), ("747.4500", 527, 564),
        ]),
    ], number=1)
    page_two = _page([
        (74, header),
        (101, [
            ("7716704756", 31, 81), ("-", 84, 87),
            ("CHAUD.", 89, 124), ("ICONDENS", 210, 257), ("24", 315, 325),
            ("1", 451, 457), ("PI", 479, 488), ("950.0000", 527, 564),
        ]),
        (117, [("ODP", 45, 68), ("NCA-030726-1029", 70, 153),
               ("du", 156, 168), ("03/07/26", 170, 209)]),
        (138, [
            ("7716704756", 31, 81), ("-", 84, 87),
            ("CHAUD.", 89, 124), ("ICONDENS", 210, 257), ("24", 315, 325),
            ("1", 451, 457), ("PI", 479, 488), ("950.0000", 527, 564),
        ]),
        (175, [
            ("7733701571", 31, 81), ("-", 84, 87),
            ("UNIT", 89, 110), ("EXT.", 113, 133), ("70", 172, 182),
            ("18", 446, 457), ("PI", 479, 488), ("517.3578", 527, 564),
        ]),
        (195, [("FRANCO", 31, 70)]),
        (210, [("NB:", 31, 47), ("Sans", 49, 70), ("retour", 72, 96)]),
    ], number=2)

    lines, charges = _extract_geometry_table_items_v46(
        [page_one, page_two], load_config(None)
    )

    assert not charges
    assert len(lines) == 5
    assert [line.page for line in lines] == [1, 1, 2, 2, 2]
    assert [line.material_number for line in lines] == [
        "7736507169", "7733704055", "7716704756", "7716704756", "7733701571",
    ]
    assert [line.quantity for line in lines] == [1, 1, 1, 1, 18]
    assert [line.unit_price for line in lines] == [683.4, 747.45, 950, 950, 517.3578]
    assert [round(line.line_total, 4) for line in lines] == [
        683.4, 747.45, 950, 950, 9312.4404,
    ]
    assert "2 LC15" in lines[0].description
    assert "7733701870" in lines[1].description
    assert all(not line.description.startswith("-") for line in lines)
    assert all("Promo" not in line.description for line in lines)
    assert all("ODP" not in line.description for line in lines)
    assert all("FRANCO" not in line.description for line in lines)


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
    assert po.purchase_order.number.value == "5516418 / 1877"


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


def test_final_normalizer_moves_shipping_and_tax_rows_out_of_products():
    product = LineItem(
        line_number="1", material_number="87167716300", description="CARTER",
        quantity=1, unit_price=100, line_total=100, currency="EUR", page=1,
    )
    shipping = LineItem(
        line_number="2", material_number="PORTSTD", description="FRAIS DE PORT STANDARD",
        quantity=1, unit_price=20, line_total=20, currency="EUR", page=1,
        raw_text="PORTSTD | FRAIS DE PORT STANDARD | 1 | 20,00 | 20,00",
    )
    tax = LineItem(
        line_number="3", material_number="TGAP", description="TAXE TGAP",
        quantity=1, unit_price=2.5, line_total=2.5, currency="EUR", page=1,
    )
    po = SimpleNamespace(
        lines=[product, shipping, tax], additional_charges=[],
        totals=Totals(total_net=122.5, currency="EUR"),
    )

    po, counts = _separate_additional_charge_lines_v62(po, {})

    assert [line.material_number for line in po.lines] == ["87167716300"]
    assert [charge.charge_type for charge in po.additional_charges] == ["shipping", "tax"]
    assert [charge.amount for charge in po.additional_charges] == [20, 2.5]
    assert po.totals.total_shipping == 20
    assert counts == {"shipping": 1, "tax": 1}


def test_final_normalizer_moves_bare_port_code_out_of_products():
    product = LineItem(
        line_number="1", material_number="EL87144314800", description="CLE DE CODAGE 1480",
        quantity=1, unit_price=6.7, line_total=6.7, currency="EUR", page=1,
    )
    shipping = LineItem(
        line_number="2", material_number="PORT", article_number="PORT",
        quantity=1, unit_price=20, line_total=20, currency="EUR", page=1,
        raw_text="PORT | 1,0 | 20,00 | 20,00 | 1",
    )
    po = SimpleNamespace(
        lines=[product, shipping], additional_charges=[],
        totals=Totals(total_net=26.7, currency="EUR"),
    )

    po, counts = _separate_additional_charge_lines_v62(po, {})

    assert [line.material_number for line in po.lines] == ["EL87144314800"]
    assert len(po.additional_charges) == 1
    assert po.additional_charges[0].charge_type == "shipping"
    assert po.additional_charges[0].code == "PORT"
    assert po.additional_charges[0].amount == 20
    assert po.totals.total_shipping == 20
    assert counts == {"shipping": 1}


def _native_table(headers, row, page=1):
    return SimpleNamespace(
        data=[headers, row],
        source=SimpleNamespace(page=page),
        confidence=0.98,
    )


def test_native_table_preserves_two_identical_physical_product_rows():
    table = SimpleNamespace(
        data=[
            ["Article", "Désignation", "Qté cdée", "Prix net", "Montant HT"],
            ["EL8716747368", "KIT UPMO 15-60 + CONNECTEUR", "1", "183,17", "183,17"],
            ["EL8716747368", "KIT UPMO 15-60 + CONNECTEUR", "1", "183,17", "183,17"],
            ["EL87167712590", "JOINT", "1", "32,13", "32,13"],
        ],
        source=SimpleNamespace(page=1),
        confidence=0.98,
    )

    lines, charges = _native_table_line_items_and_charges([table])

    assert charges == []
    assert [line.material_number for line in lines] == [
        "EL8716747368", "EL8716747368", "EL87167712590",
    ]
    assert [line.line_number for line in lines] == ["1", "2", "3"]


def test_dual_reference_stacked_table_uses_supplier_reference_and_extracts_port_charge():
    table = _native_table(
        ["Reference", "Ref Four", "Nombre", "Designation", "Quantite",
         "P.P. HT", "Rem", "Prix HT", "Montant HT"],
        [
            "36 451\n19 424\n346",
            "8718585342\n8718584806\nPORTA",
            "1PCE\n1PCE\n1PCE",
            "Commande par le depot :\nAgence Commerciale / 03\n81 63 00 33\n"
            "Pour stock\nVEILLEUSE AE BUDERUS\n(IONISATION) SUR G124\n"
            "G224 G234 G334 G434\nINJECTEUR DE VEILLEUSE\n"
            "PORT/ACHATS -\nREFACTURE\nFOURNISSEUR",
            "1,000PCE\n1,000PCE\n1,000PCE",
            "170,00\n25,00\n20,00",
            "46,00\n46,00",
            "91,80\n13,50\n20,00",
            "91,80\n13,50\n20,00",
        ],
    )

    lines = _dual_reference_stacked_native_table_items_v67(table)

    assert len(lines) == 3
    assert [line.customer_material_number for line in lines] == ["36 451", "19 424", "346"]
    assert [line.material_number for line in lines] == ["8718585342", "8718584806", "PORTA"]
    assert [line.supplier_material_number for line in lines] == ["8718585342", "8718584806", "PORTA"]
    assert lines[0].description == "VEILLEUSE AE BUDERUS (IONISATION) SUR G124 G224 G234 G334 G434"
    assert lines[1].description == "INJECTEUR DE VEILLEUSE"
    assert lines[2].description == "PORT/ACHATS - REFACTURE FOURNISSEUR"

    native_lines, native_charges = _native_table_line_items_and_charges([table])
    assert [line.material_number for line in native_lines] == ["8718585342", "8718584806"]
    assert [line.customer_material_number for line in native_lines] == ["36 451", "19 424"]
    assert len(native_charges) == 1
    assert native_charges[0].charge_type == "shipping"
    assert native_charges[0].code == "PORTA"

    po = SimpleNamespace(
        lines=lines,
        additional_charges=[],
        totals=Totals(total_net=125.3, currency="EUR"),
    )
    po, counts = _separate_additional_charge_lines_v62(po, {})

    assert [line.material_number for line in po.lines] == ["8718585342", "8718584806"]
    assert counts == {"shipping": 1}
    assert po.additional_charges[0].code == "PORTA"
    assert po.additional_charges[0].amount == 20


def test_dual_reference_stacked_table_aligns_missing_supplier_cell_after_port_charge():
    table = _native_table(
        ["Reference", "Ref Four", "Nombre", "Designation", "Quantite",
         "P.P. HT", "Rem", "Prix HT", "Montant HT"],
        [
            "346\n217 773",
            "87168542240",
            "1PCE\n1PCE",
            "Commande par le depot :\nTrevenans / 03 84 27 28 28\nPour stock\n"
            "PORT/ACHATS -\nREFACTURE\nFOURNISSEUR\n"
            "Pour CHAPUIS ENT / Cmde\n496 678 / Ref: BOSCH\nCABLE HT",
            "1,000PCE\n1,000PCE",
            "20,00\n64,00",
            "40,00",
            "20,00\n38,40",
            "20,00\n38,40",
        ],
    )

    lines = _dual_reference_stacked_native_table_items_v67(table)

    assert len(lines) == 2
    assert lines[0].material_number == "346"
    assert lines[0].supplier_material_number is None
    assert lines[0].description == "PORT/ACHATS - REFACTURE FOURNISSEUR"
    assert lines[1].customer_material_number == "217 773"
    assert lines[1].supplier_material_number == "87168542240"
    assert lines[1].material_number == "87168542240"
    assert lines[1].description == "CABLE HT"

    native_lines, native_charges = _native_table_line_items_and_charges([table])
    assert len(native_lines) == 1
    assert native_lines[0].material_number == "87168542240"
    assert native_lines[0].customer_material_number == "217 773"
    assert native_lines[0].description == "CABLE HT"
    assert len(native_charges) == 1 and native_charges[0].amount == 20

    po = SimpleNamespace(
        lines=lines,
        additional_charges=[],
        totals=Totals(total_net=58.4, currency="EUR"),
    )
    po, counts = _separate_additional_charge_lines_v62(po, {})
    assert [line.material_number for line in po.lines] == ["87168542240"]
    assert counts == {"shipping": 1}
    assert po.additional_charges[0].amount == 20


def test_compact_deee_reference_is_environmental_fee_attached_to_previous_product():
    product = LineItem(
        line_number="3", material_number="8719999999", description="PRODUIT",
        quantity=1, unit_price=646.19, line_total=646.19, currency="EUR", page=1,
    )
    environmental_fee = LineItem(
        line_number="4", material_number="DEEE7.69", article_number="DEEE7.69",
        description="DEEE7.69", quantity=1, unit_price=7.69, line_total=7.69,
        currency="EUR", page=1, raw_text="DEEE7.69 | 1.00p. | 7.69p. | 7.69",
    )
    po = SimpleNamespace(
        lines=[product, environmental_fee],
        additional_charges=[],
        totals=Totals(total_net=653.88, currency="EUR"),
    )

    po, counts = _separate_additional_charge_lines_v62(po, {})

    assert [line.material_number for line in po.lines] == ["8719999999"]
    assert counts == {"environmental_fee": 1}
    charge = po.additional_charges[0]
    assert charge.code == "DEEE7.69"
    assert charge.amount == 7.69
    assert charge.parent_line_number == "3"
    assert charge.parent_material_number == "8719999999"


def test_charge_separation_preserves_source_backed_non_one_line_numbers():
    lines = [
        LineItem(
            line_number="2", material_number="7733703987", description="PRODUIT A",
            quantity=1, unit_price=10, line_total=10, currency="EUR", page=1,
            raw_text="2 | 7733703987 | PRODUIT A | 1 | 10,00 | 10,00",
        ),
        LineItem(
            line_number="3", material_number="7709003525", description="PRODUIT B",
            quantity=2, unit_price=12, line_total=24, currency="EUR", page=1,
            raw_text="3 | 7709003525 | PRODUIT B | 2 | 12,00 | 24,00",
        ),
    ]
    po = SimpleNamespace(
        lines=lines,
        additional_charges=[],
        totals=Totals(total_net=34, currency="EUR"),
    )

    po, counts = _separate_additional_charge_lines_v62(po, {})

    assert counts == {}
    assert [line.line_number for line in po.lines] == ["2", "3"]


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
    assert calls == [7, 11]
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


def test_compact_header_accepts_parenthesized_title_with_trailing_glyph():
    page = _page([
        (20, [("(COMMANDE FOURNISSEUR à", 300, 570)]),
        (170, [("N", 10, 20), ("CE", 70, 88), ("001234567", 100, 175),
               ("DATE", 185, 220), ("26/03/26", 230, 290), ("1", 320, 325)]),
    ])
    for word in page.words:
        word.source = "ocr"
    candidate = compact_transaction_header_candidate(page)
    assert candidate is not None
    assert candidate["value"] == "CE001234567"


@pytest.mark.parametrize("raw, expected", [
    ("ELOOL", "EL001"),
    ("ABC00I", "ABC001"),
    ("EL001", "EL001"),
    ("BOSCH", "BOSCH"),
    ("DEPOT", "DEPOT"),
])
def test_labeled_party_code_repairs_only_trailing_numeric_mask(raw, expected):
    assert _normalize_labeled_party_code_v5166(raw) == expected


def test_numeric_dimension_repair_is_narrow_and_traceable():
    lines = [
        LineItem(description="SOUPAPE 3BARS 15XZ1 FEM"),
        LineItem(description="MODELE XZ1 STANDARD"),
    ]
    po = SimpleNamespace(lines=lines)
    po, count = _repair_dimension_ocr_v5166(po, {})
    assert count == 1
    assert po.lines[0].description == "SOUPAPE 3BARS 15X21 FEM"
    assert "numeric_dimension_ocr_z_repaired_to_2" in po.lines[0].warnings
    assert po.lines[1].description == "MODELE XZ1 STANDARD"


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


def test_native_table_order_number_recovers_exact_word_bbox_without_changing_value():
    page = _page([
        (100, [("N°", 10, 25), ("Commande", 30, 85), ("Date", 170, 200)]),
        (120, [("CM-", 70, 90), ("00405095", 90, 155), ("06/07/2026", 170, 230)]),
    ])
    po = SimpleNamespace(purchase_order=PurchaseOrderHeader())
    po.purchase_order.number = ExtractedField(
        value="CM-00405095",
        raw_value="CM-00405095",
        normalized_value="CM-00405095",
        final_confidence=.99,
        evidence=Evidence(
            page=1,
            source_text="CM-00405095",
            extraction_method="native_table_anchor",
        ),
    )
    assert _repair_order_number_evidence_bbox_v5172(po, [page]) == 1
    assert po.purchase_order.number.value == "CM-00405095"
    assert po.purchase_order.number.evidence.bbox == (70, 120, 155, 130)
    assert _repair_order_number_evidence_bbox_v5172(po, [page]) == 0


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


def test_summary_table_reads_label_value_cells_without_confusing_vat_rate():
    table = SimpleNamespace(
        data=[[
            "TVA (%)\n20",
            "Valeur HT\n6 957.34",
            "Valeur TVA\n1 391.47",
            "TOTAL TTC\n8 348.81 €",
        ]],
        source=SimpleNamespace(page=2),
        confidence=0.98,
    )
    po = SimpleNamespace(totals=Totals())
    _summary_table_totals_v48(po, [table], {})
    assert po.totals.total_net == 6957.34
    assert po.totals.total_vat == 1391.47
    assert po.totals.total_gross == 8348.81
    assert po.totals.grand_total == 8348.81


def test_summary_table_does_not_treat_vat_registration_as_tax_amount():
    table = SimpleNamespace(
        data=[[
            "NÂ° TVA intracommunautaire\nTotal lignes HT",
            "FR54793797283\n1 253,68",
        ]],
        source=SimpleNamespace(page=1),
        confidence=0.99,
    )
    po = SimpleNamespace(totals=Totals())

    _summary_table_totals_v48(po, [table], {})

    assert po.totals.total_vat is None
    assert po.totals.total_tax is None
    assert po.totals.total_net == 1253.68


def test_tax_registration_amount_is_removed_after_all_total_recovery_passes():
    po = SimpleNamespace(
        totals=Totals(
            total_net=1253.68,
            total_vat=54793797283.0,
            total_tax=54793797283.0,
        ),
        tax_identifiers=[{"vat_number": "FR54793797283"}],
        buyer=SimpleNamespace(vat_number="FR54793797283"),
        supplier=SimpleNamespace(vat_number=None),
        bill_to=SimpleNamespace(vat_number=None),
        ship_to=SimpleNamespace(vat_number=None),
        taxes=[SimpleNamespace(tax_amount=54793797283.0)],
    )

    po, repaired = _sanitize_tax_identifier_totals_v5174(po, {})

    assert repaired == 3
    assert po.totals.total_vat is None
    assert po.totals.total_tax is None
    assert po.taxes[0].tax_amount is None
    assert po.totals.total_net == 1253.68


def test_mixed_total_header_does_not_overwrite_net_with_ttc_and_keeps_explicit_eur():
    page = _page([
        (700, [("Escompte",20,70),("Total",80,110),("HT",115,130),(":",135,138),("12",470,482),("990,00",485,525)]),
        (720, [("Total",20,50),("TVA",55,75),(":",80,83),("2",470,478),("598,00",482,520)]),
        (740, [("Montant",20,65),("HT",70,85),("Ecotaxe",100,150),("HT",155,170),("Port",185,210),("HT",215,230),("Total",430,460),("TTC",465,485),(":",490,493),("15",500,512),("588,00",515,555)]),
        (760, [("EUR",20,45),("12",80,92),("990,00",95,135),("0,00",160,185),("0,00",210,235),("20,00",270,300),("2",350,358),("598,00",362,402),("15",500,512),("588,00",515,555)]),
        (780, [("NET",400,425),("A",430,438),("PAYER",442,480),(":",485,488),("15",500,512),("588,00",515,555)]),
    ])
    po=SimpleNamespace(purchase_order=PurchaseOrderHeader(),totals=Totals())
    _strict_text_totals_v48(po,[page],{})
    assert po.totals.total_net==12990
    assert po.totals.total_vat==2598
    assert po.totals.total_gross==15588
    assert po.totals.grand_total==15588
    assert po.totals.currency=="EUR"
    assert po.purchase_order.currency.value=="EUR"

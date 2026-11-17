"""Scanned order forms: source punctuation, captions, roles and footer boundaries."""
from unittest.mock import patch

import pytest
from PIL import Image

from po_ocr.address_intelligence import parse_address
from po_ocr.address_roles import (
    _explicit_role_anchors, recover_explicit_role_blocks,
    recover_ordering_form_addressee, build_business_addresses,
)
from po_ocr.config import load_config
from po_ocr.extract import all_rows, extract_totals, extract_header
from po_ocr.models import WordToken
from po_ocr.ocr import (
    OCRCandidate, _is_garbage, words_to_text, recover_underlined_role_labels,
    _recover_low_confidence_phones,
)
from uda.business import (
    _enhance_inline_transaction_header_v46, _extract_geometry_table_items_v46,
    _sanitize_summary_totals_v46,
    _extract_vat_identifiers_v52,
)
from test_order_table_header_cells import page, order


def scaled(source, scale):
    source.width *= scale
    source.height *= scale
    source.source_type = "scan_ocr"
    for word in source.words:
        word.source = "ocr"
        word.bbox = tuple(value * scale for value in word.bbox)
    return source


@pytest.mark.parametrize("token", ["-", "/", ".", ":", "%", "€", "#", "°", "04", "9261109999"])
def test_source_separators_are_not_garbage(token):
    assert not _is_garbage(token, {})


@pytest.mark.parametrize("token", ["|||||", "____", "~!@#{}", ""])
def test_symbol_noise_remains_filtered(token):
    assert _is_garbage(token, {})


@pytest.mark.parametrize("scale", [1, 4])
def test_large_logo_does_not_bridge_order_title_and_number(scale):
    source = scaled(page([
        (40, [("BON", 160, 185), ("DE", 190, 205), ("COMMANDE", 210, 300)]),
        (66, [("N°", 160, 177), ("04", 183, 200), ("-", 206, 211), ("9261109999", 217, 310)]),
    ]), scale)
    source.words.insert(0, WordToken(text="LOGO", bbox=(25*scale, 20*scale, 120*scale, 65*scale),
                                     page=1, source="ocr", confidence=.90))
    rows = words_to_text(source.words).splitlines()
    assert "N° 04 - 9261109999" in rows
    assert not any("LOGO" in row and "9261109999" in row for row in rows)


@pytest.mark.parametrize("scale", [1, 4])
def test_underlined_caption_allows_ocr_overlap_but_not_reversed_words(scale):
    source = scaled(page([(200, [("A", 30, 38), ("Livrer", 31, 78), ("à", 82, 86)])]), scale)
    roles = _explicit_role_anchors([{"words": source.words}], scale)
    assert any(role == "ship_to" for role, _ in roles)
    source.words[1].bbox = (0, 200*scale, 10*scale, 210*scale)
    assert not _explicit_role_anchors([{"words": source.words}], scale)


def test_underlined_word_box_can_cover_following_caption_token():
    source = scaled(page([(125, [("Commandé", 30, 123), ("par", 101, 120)])]), 4)
    assert any(role == "buyer" for role, _ in _explicit_role_anchors([{"words": source.words}], 4))


def form(scale=1, title="BON DE COMMANDE", explicit_supplier=False):
    rows = [
        (40, [(title, 160, 380)]),
        (125, [("Commandé", 30, 93), ("par", 99, 120)]),
        (145, [("JEAN EXEMPLE", 30, 110)]),
        (163, [("CLIENT AGENCE", 30, 120)]),
        (110, [("FOURNISSEUR INDUSTRIE", 300, 460)]),
        (130, [("124-126 RUE DES ATELIERS", 300, 475)]),
        (150, [("93711 DRANCY CEDEX", 300, 440)]),
        (200, [("A", 30, 38), ("Livrer", 41, 78), ("à", 82, 86),
               ("A", 300, 308), ("Facturer", 312, 355), ("à", 360, 365)]),
        (220, [("CLIENT DEPOT", 30, 125), ("CLIENT LEGAL", 300, 440)]),
        (238, [("300 IMPASSE DES ATELIERS", 30, 220), ("Archive ScanDocs Center / 0456", 300, 520)]),
        (256, [("82200 MOISSAC", 30, 125), ("CS 80010", 300, 360)]),
        (274, [("81700 BLAN", 300, 390)]),
        (302, [("Fournisseur n°: 387840", 300, 450)]),
        (360, [("Référence", 30, 100), ("Désignation", 115, 200),
               ("Quantité", 400, 445), ("Prix", 500, 540)]),
    ]
    if explicit_supplier:
        rows.append((90, [("Fournisseur", 300, 365)]))
    return scaled(page(rows), scale)


@pytest.mark.parametrize("scale", [1, 4])
def test_order_form_keeps_three_postal_roles_and_ordering_contact_separate(scale):
    source = form(scale)
    result = order()
    result.ship_to.department = "UNRELATED OLD INSTRUCTIONS"
    result.ship_to.contact.department = "WRONG NEIGHBOURING PARTY"
    result = recover_explicit_role_blocks(result, [source])
    assert result.buyer.name == "CLIENT AGENCE"
    assert result.buyer.contact.name == "JEAN EXEMPLE"
    assert not result.buyer.address.raw_lines
    assert result.ship_to.name == "CLIENT DEPOT"
    assert result.ship_to.department is None
    assert result.ship_to.contact.department is None
    address, _, _ = parse_address(result.ship_to.address)
    assert (address.house_number, address.postal_code, address.city) == ("300", "82200", "MOISSAC")
    assert result.bill_to.name == "CLIENT LEGAL"
    assert "ScanDocs" in result.bill_to.department
    result = recover_ordering_form_addressee(result, [source])
    assert result.supplier.name == "FOURNISSEUR INDUSTRIE"
    assert result.supplier.code == "387840"
    assert parse_address(result.supplier.address)[0].postal_code == "93711"
    once = result.model_dump()
    recover_explicit_role_blocks(result, [source])
    recover_ordering_form_addressee(result, [source])
    assert result.model_dump() == once
    addresses = build_business_addresses(result, [source])
    supplier = next(address for address in addresses if address.role == "supplier")
    assert supplier.address.postal_code == "93711"
    assert "RUE DES ATELIERS" in supplier.formatted_address.upper()


@pytest.mark.parametrize("title,explicit", [("FACTURE", False), ("BON DE COMMANDE", True)])
def test_unlabelled_addressee_inference_needs_order_context_and_no_explicit_vendor(title, explicit):
    result = order()
    result.supplier.name = "KEEP EXPLICIT VENDOR"
    recover_ordering_form_addressee(result, [form(title=title, explicit_supplier=explicit)])
    assert result.supplier.name == "KEEP EXPLICIT VENDOR"


def test_unlabelled_vendor_requires_full_postal_block():
    source = form()
    source.words = [word for word in source.words if "RUE DES" not in word.text]
    result = order()
    recover_ordering_form_addressee(result, [source])
    assert not result.supplier.name


def test_label_recovery_adds_only_caption_not_unreviewed_values():
    source = scaled(page([(40, [("BON DE COMMANDE", 100, 300)]),
                          (300, [("Date livraison", 30, 100)])]), 1)
    reread = scaled(page([(200, [("A", 30, 38), ("Livrer", 41, 78), ("à", 82, 86)]),
                          (220, [("UNRELATED NEW VALUE", 30, 200)])]), 1)
    candidate = OCRCandidate(text=reread.text, words=reread.words, confidence=.95, psm=3)
    with patch("po_ocr.ocr._tesseract_single", return_value=candidate) as read:
        recover_underlined_role_labels(source, Image.new("L", (595, 842), 255), {})
        recover_underlined_role_labels(source, Image.new("L", (595, 842), 255), {})
    assert read.call_count == 1
    assert "Livrer" in source.text
    assert "UNRELATED" not in source.text
    assert source.ocr_diagnostics["role_label_recovery"]["added"][0]["role"] == "ship_to"


def test_label_recovery_timeout_preserves_original_words():
    source = scaled(page([(40, [("COMMANDE livraison", 100, 300)])]), 1)
    before = source.model_copy(deep=True)
    with patch("po_ocr.ocr._tesseract_single", side_effect=RuntimeError("timeout")):
        recover_underlined_role_labels(source, Image.new("L", (595, 842), 255), {})
    assert source.words == before.words
    assert source.ocr_diagnostics["role_label_recovery"]["error"] == "RuntimeError"


def product_table(summary="Montant HT Net 259.08", reference="Notre Réf: 1732047"):
    return page([
        (300, [("Référence", 20, 75), ("-", 77, 80), ("Désignation", 100, 170),
               ("Quantité", 400, 445), ("Unité", 460, 485), ("Prix Unitaire", 500, 570)]),
        (322, [("8716860999", 20, 75), ("COFFRET SECURITE", 100, 230),
               ("1", 420, 430), ("PI", 462, 474), ("259.0800", 510, 560)]),
        (335, [(reference, 20, 140)]),
        (400, [(summary, 180, 560)]),
        (430, [("Conditions de vente et mentions légales", 20, 350)]),
    ])


@pytest.mark.parametrize("summary", ["Montant HT Net 259.08", "Montant HT: 259.08", "Montant TTC 310.90"])
def test_full_summary_row_stops_description_before_column_clipping(summary):
    items, charges = _extract_geometry_table_items_v46([product_table(summary)], load_config(None))
    assert not charges
    assert len(items) == 1
    assert items[0].description == "COFFRET SECURITE"
    assert items[0].customer_material_number == "1732047"
    assert (items[0].quantity, items[0].unit_price) == (1, 259.08)


@pytest.mark.parametrize("reference", ["Votre Réf: 1732047", "Notre Réf: TEXTE", "BP N° 842849"])
def test_customer_article_needs_explicit_own_reference_with_digits(reference):
    items, _ = _extract_geometry_table_items_v46([product_table(reference=reference)], load_config(None))
    assert items[0].customer_material_number is None


def test_reference_table_heading_is_not_an_internal_order_reference():
    result = _enhance_inline_transaction_header_v46(order(), [product_table()], load_config(None))
    assert result.purchase_order.internal_number.value is None


def test_dotted_vat_amount_and_printed_gross_total():
    source = page([(700, [("Montant T.V.A. 51.82", 350, 540)]),
                   (720, [("Montant TTC 310.90", 350, 540)])])
    totals = extract_totals(all_rows([source]))
    assert totals.total_vat == 51.82
    assert totals.grand_total == 310.90
    result = order()
    _sanitize_summary_totals_v46(result, [source], load_config(None))
    assert result.totals.grand_total == 310.90


@pytest.mark.parametrize("text", ["N° T.V.A.: FR07716320619", "TVA intracommunautaire FR07716320619", "N° TVA 716320619"])
def test_vat_identifiers_never_become_tax_amounts(text):
    assert extract_totals(all_rows([page([(700, [(text, 20, 300)])])])).total_vat is None


@pytest.mark.parametrize("old_conf,new_conf,label,number,offset,expected", [
    (.43, .96, "Tél", "0563294190", 0, True),
    (.93, .96, "Tél", "0563294190", 0, False),
    (.43, .70, "Tél", "0563294190", 0, False),
    (.43, .96, "Code", "0563294190", 0, False),
    (.43, .96, "Tél", "0563294190", 150, False),
    (.43, .96, "Tél", "0563294180", 0, False),
])
def test_phone_reread_requires_local_label_confidence_and_one_digit(old_conf, new_conf, label, number, offset, expected):
    old = WordToken(text="0963294190", page=1, bbox=(80, 160, 150, 180), source="ocr", confidence=old_conf)
    fresh = WordToken(text=number, page=1, bbox=(80, 160+offset, 150, 170+offset), source="ocr", confidence=new_conf)
    caption = WordToken(text=label, page=1, bbox=(55, 160+offset, 70, 170+offset), source="ocr", confidence=.95)
    diagnostics = _recover_low_confidence_phones([old], [caption, fresh])
    assert bool(diagnostics) is expected
    assert old.text == (number if expected else "0963294190")
    if expected:
        assert diagnostics[0]["source_text"] == "0963294190"


def test_delivery_date_does_not_capture_supplier_code_in_neighbouring_column():
    source = page([(300, [("Date de livraison :19/11/2026", 30, 220), ("Fournisseur n°: 387840", 300, 440)])])
    fields, _, _ = extract_header(all_rows([source]))
    assert fields["expected_delivery_date"].raw_value == "19/11/2026"
    assert fields["expected_delivery_date"].value == "2026-11-19"


def test_existing_role_bound_vat_normalizes_only_with_checksum_supported_source():
    result = order()
    result.bill_to.vat_number = "FRO7716320619"
    result.supplier.vat_number = "FRO7999999999"
    source = page([(700, [("TVA intracommunautaire FR07716320619", 20, 350)])])
    _extract_vat_identifiers_v52(result, [source], load_config(None))
    assert result.bill_to.vat_number == "FR07716320619"
    assert result.supplier.vat_number == "FRO7999999999"


def test_billing_metadata_is_not_an_address_component():
    source = form()
    source.words.extend(page([(286, [("N° Intracom. FR07716320619", 300, 480)]),
                              (298, [("VE 030 J FDM LE 20", 300, 440)])]).words)
    result = recover_explicit_role_blocks(order(), [source])
    assert result.bill_to.address.raw_lines == ["CS 80010", "81700 BLAN"]

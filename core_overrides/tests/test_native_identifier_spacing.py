"""Preserve source spacing across native PDF text objects, not ERP templates."""
from datetime import datetime, timezone

import fitz
import pytest

from po_ocr.config import load_config
from po_ocr.extract import all_rows
from po_ocr.models import DocumentMeta, PageResult, PurchaseOrderResult, WordToken
from po_ocr.pdf_native import _stitch_touching_native_identifiers, extract_native_page
from uda.business import _enhance_inline_transaction_header_v46, _normalize_inline_po_number


def _word(text, x0, x1, *, y=100, source="native_pdf", page=1):
    return WordToken(text=text, page=page, bbox=(x0, y, x1, y + 18),
                     confidence=0.97, source=source)


@pytest.mark.parametrize("left_gap,right_gap,expected", [
    (0, 5, ["5889382/", "8742"]),
    (5, 0, ["5889382", "/8742"]),
    (0, 0, ["5889382/8742"]),
    (5, 5, ["5889382", "/", "8742"]),
    (0.2, 0.2, ["5889382", "/", "8742"]),
    (-2, 5, ["5889382", "/", "8742"]),
])
def test_only_touching_separator_boundaries_are_joined(left_gap, right_gap, expected):
    slash_x = 255 + left_gap
    words = [_word("5889382", 185, 255), _word("/", slash_x, slash_x + 5),
             _word("8742", slash_x + 5 + right_gap, slash_x + 45 + right_gap)]
    original = [word.model_dump() for word in words]
    result = _stitch_touching_native_identifiers(words)
    assert [word.text for word in result] == expected
    assert [word.model_dump() for word in words] == original
    assert _stitch_touching_native_identifiers(result) == result
    assert all(word.confidence == 0.97 for word in result)


@pytest.mark.parametrize("separator", ["/", "-", "_", "."])
def test_split_alphanumeric_identifiers_keep_their_separator(separator):
    words = [_word("PO", 10, 30), _word(separator, 30, 35), _word("001234", 35, 95)]
    result = _stitch_touching_native_identifiers(words)
    assert [word.text for word in result] == [f"PO{separator}001234"]
    assert result[0].bbox == (10, 100, 95, 118)


@pytest.mark.parametrize("source", ["ocr", "azure", "structured_file"])
def test_ocr_geometry_does_not_authorize_native_spacing_reconstruction(source):
    words = [_word("5889382", 185, 255, source=source), _word("/", 255, 260),
             _word("8742", 265, 305)]
    assert _stitch_touching_native_identifiers(words) == words


def test_word_groups_columns_baselines_and_pages_are_not_concatenated():
    for words in [
        [_word("ST", 10, 30), _word("C", 30, 40), _word("CSP", 40, 70), _word("33443", 70, 120)],
        [_word("193", 10, 40), _word("444", 40, 70)],
        [_word("5889382", 185, 255), _word("/", 355, 360)],
        [_word("5889382", 185, 255), _word("/", 255, 260, y=105)],
        [_word("5889382", 185, 255), _word("/", 255, 260, page=2)],
        [_word("TEXTE", 10, 60), _word("-", 60, 65), _word("SUITE", 65, 115)],
    ]:
        assert _stitch_touching_native_identifiers(words) == words


def _pdf_header(number, suffix, left_gap, right_gap):
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((20, 140), "COMMANDE", fontname="hebo", fontsize=20)
    # Deliberately write the slash first in a separate regular-font PDF object.
    # The two bold layers reproduce the invisible overprinting in ERP exports.
    number_end = 185 + fitz.get_text_length(number, fontname="hebo", fontsize=18)
    slash_x = number_end + left_gap
    slash_end = slash_x + fitz.get_text_length("/", fontname="helv", fontsize=18)
    page.insert_text((slash_x, 140), "/", fontname="helv", fontsize=18)
    for _ in range(2):
        page.insert_text((185, 140), number, fontname="hebo", fontsize=18)
        page.insert_text((slash_end + right_gap, 140), suffix, fontname="hebo", fontsize=18)
    page.insert_text((335, 140), "du 13.11.2026", fontsize=18)
    return document


@pytest.mark.parametrize("number,suffix", [("5889382", "8742"), ("5888260", "1877")])
@pytest.mark.parametrize("left_gap,right_gap", [(0, 5), (5, 0), (0, 0), (5, 5)])
def test_native_pdf_to_order_header_preserves_each_side_of_slash(number, suffix, left_gap, right_gap):
    expected = number + (" " if left_gap else "") + "/" + (" " if right_gap else "") + suffix
    with _pdf_header(number, suffix, left_gap, right_gap) as document:
        text, words, confidence = extract_native_page(document[0], 1, {"hybrid_native_ocr_enabled": False})
        assert expected in text
        page = PageResult(page=1, width=595, height=842, source_type="native_pdf",
                          text=text, words=words, confidence=confidence)
        assert expected in all_rows([page])[0].text
        meta = DocumentMeta(id="spacing", filename="spacing.pdf", extension="pdf", file_size=1,
                            sha256="spacing", page_count=1, is_pdf=True, is_native_pdf=True,
                            has_text_layer=True, model_version="test",
                            processing_timestamp=datetime.now(timezone.utc).isoformat())
        po = _enhance_inline_transaction_header_v46(PurchaseOrderResult(document=meta), [page], load_config(None))
        field = po.purchase_order.number
        assert field.value == field.raw_value == field.normalized_value == expected
        assert field.evidence.source_text == expected
        assert po.purchase_order.order_date.value == "2026-11-13"


@pytest.mark.parametrize("raw,expected", [
    ("26 365", "26 365"), ("26   365", "26 365"),
    ("26\u00a0365", "26 365"), ("00\u202f365", "00 365"),
    ("26365", "26365"), ("dse  g idn  147", "dse g idn 147"),
])
@pytest.mark.parametrize("legacy_setting", [False, True])
def test_inline_identifiers_never_compact_visible_spaces(raw, expected, legacy_setting):
    config = {"layout": {"inline_order_number_normalize_digit_groups": legacy_setting}}
    assert _normalize_inline_po_number(raw, config) == (expected, None)


@pytest.mark.parametrize("number", [
    "26 365 / CPM", "13 205 / NICE", "207 841 / Nice", "000 123 / Agence-7",
    "13 205/NICE", "207 841/ Nice", "207 841 /Nice",
])
@pytest.mark.parametrize("adjacent_address", [False, True])
def test_native_supplier_order_header_retains_full_identifier(number, adjacent_address):
    with fitz.open() as document:
        page = document.new_page(width=595, height=842)
        if adjacent_address:
            page.insert_text((20, 160), "Adresse de livraison", fontsize=10)
        page.insert_text((180, 160), "COMMANDE FOURNISSEUR : " + number, fontsize=10)
        text, words, confidence = extract_native_page(page, 1, {"hybrid_native_ocr_enabled": False})
        assert number in text
        parsed = PageResult(page=1, width=595, height=842, source_type="native_pdf",
                            text=text, words=words, confidence=confidence)
        meta = DocumentMeta(id="groups", filename="groups.pdf", extension="pdf", file_size=1,
                            sha256="groups", page_count=1, is_pdf=True, is_native_pdf=True,
                            has_text_layer=True, model_version="test",
                            processing_timestamp=datetime.now(timezone.utc).isoformat())
        po = _enhance_inline_transaction_header_v46(PurchaseOrderResult(document=meta), [parsed], load_config(None))
        field = po.purchase_order.number
        assert field.value == field.raw_value == field.normalized_value == number
        assert field.evidence.source_text == number
        assert po.purchase_order.project_name.value is None


@pytest.mark.parametrize("raw,expected", [
    ("13  205  /  NICE", "13 205 / NICE"),
    ("207\u00a0841 / Nice", "207 841 / Nice"),
    ("000 123 / Agence-7", "000 123 / Agence-7"),
    ("000 123 / Saint-Étienne", "000 123 / Saint-Étienne"),
    ("207 841 / Nice Adresse de livraison", "207 841 / Nice"),
    ("207 841 / Nice au 19/11/2026", "207 841 / Nice"),
])
def test_slash_suffix_is_not_reinterpreted_as_a_project(raw, expected):
    assert _normalize_inline_po_number(raw) == (expected, None)

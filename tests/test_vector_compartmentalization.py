from __future__ import annotations

from po_ocr.address_intelligence import parse_address
from po_ocr.layout_guides import split_items_by_compartments
from po_ocr.models import Address, WordToken


def _word(text: str, x0: float, x1: float) -> WordToken:
    return WordToken(text=text, page=1, bbox=(x0, 10, x1, 20), source="native_pdf")


def test_vector_separator_splits_a_visual_row_before_gap_fallback() -> None:
    words = [_word("Adresse", 10, 50), _word("livraison", 55, 100), _word("Client", 125, 160)]
    groups = split_items_by_compartments(
        words,
        [{"orientation": "vertical", "x0": 112, "y0": 0, "x1": 112, "y1": 50}],
        [],
        gap_threshold=80,
    )
    assert [group["text"] for group in groups] == ["Adresse livraison", "Client"]
    assert groups[1]["boundary_source"] == "pdf_vector"


def test_address_parser_excludes_contact_and_delivery_instructions() -> None:
    address, _, _ = parse_address(Address(raw_lines=[
        "212 AVENUE MAGELLAN",
        "ZONE TEC 2 | EN EXPRESS - MERCI",
        "CORRESPONDANT : HINNEBO CINDY",
        "30320 MARGUERITTES",
    ]))
    assert address.street == "AVENUE MAGELLAN"
    assert address.postal_code == "30320"
    assert address.city == "MARGUERITTES"
    assert address.address_complement == "ZONE TEC 2"


def test_address_parser_excludes_decorative_adherent_line() -> None:
    address, _, _ = parse_address(Address(raw_lines=[
        "1 RUE PHILIPPE LEBON",
        "************************************************ ADHERENT",
        "14120 MONDEVILLE",
    ]))
    assert address.address_complement is None


def test_address_parser_preserves_an_ordinal_before_the_street_type() -> None:
    address, _, _ = parse_address(Address(raw_lines=[
        "1ERE AVENUE",
        "ZAC SYNERGIE VAL DE LOIRE",
        "45130 MEUNG-SUR-LOIRE",
        "FRANCE",
    ]))
    assert address.street_type == "AVENUE"
    assert address.street_name == "1ERE"
    assert address.street == "1ERE AVENUE"

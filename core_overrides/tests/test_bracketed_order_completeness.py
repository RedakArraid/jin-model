"""Repeated fees and spaced references must not silently shorten an order."""
from types import SimpleNamespace

import pytest

from po_ocr.models import Address, AdditionalCharge, Evidence, PageResult, Party, Totals, WordToken
from po_ocr.address_intelligence import parse_address
from po_ocr.validate import validate
from test_order_table_header_cells import order
from uda.business import (
    _explicit_reference_layout_rows_v5202,
    _explicit_reference_vertical_items_v5202,
    _merge_explicit_page_charges_v63,
    _enhance_strict_totals_v47,
    _repair_buyer_from_matching_registration_footer_v5202,
)


def page(rows, number=1):
    words = []
    for y, text in rows:
        x = 10
        for token in text.split():
            end = x + len(token) * 4
            words.append(WordToken(text=token, bbox=(x, y, end, y + 10), page=number,
                                   confidence=1.0, source="native_pdf"))
            x = end + 4
    return PageResult(page=number, width=600, height=842, words=words,
                      text="\n".join(text for _, text in rows), confidence=1.0,
                      source_type="native_pdf")


HEADER = "Produits Qte PU HT %TVA PU TTC Mtt HT"


def test_spaced_bracket_references_and_repeated_fees_reconcile_across_pages():
    first = page([
        (100, HEADER),
        (120, "[ELM 7736504817] Chauffe eau 4,00 365,00 20,00 438,00 1 460,00"),
        (140, "[ECO PART] Eco Participation 3,00 1,75 20,00 2,10 5,25"),
        (160, "[ELM7736506273] Chauffe eau 1,00 400,00 20,00 480,00 400,00"),
        (180, "[ECO PART] Eco Participation 1,00 7,00 20,00 8,40 7,00"),
        (200, "[ELM 7724001291 ] Thermostat 2,00 57,00 20,00 68,40 114,00"),
        (220, "[ECO PART] Eco Participation 1,00 7,00 20,00 8,40 7,00"),
    ])
    second = page([
        (100, HEADER),
        (120, "[7738112497] Ventouse 3,00 57,28 20,00 68,74 171,84"),
        (140, "[ECO PART] Eco Participation 1,00 7,00 20,00 8,40 7,00"),
        (180, "Total EUR HT 2 172,09"),
        (200, "Total TVA 434,42"),
        (220, "Total EUR TTC 2 606,51"),
    ], number=2)
    pages = [first, second]
    lines = _explicit_reference_layout_rows_v5202(pages, {})
    assert [line.material_number for line in lines] == ["7736504817", "7736506273", "7724001291", "7738112497"]
    assert all(line.bbox for line in lines)
    assert [line.page for line in lines] == [1, 1, 1, 2]
    po = SimpleNamespace(lines=lines, additional_charges=[], totals=Totals())
    po, counts = _merge_explicit_page_charges_v63(po, pages, {})
    assert counts == {"environmental_fee": 4}
    assert [c.amount for c in po.additional_charges] == [5.25, 7, 7, 7]
    assert [c.quantity for c in po.additional_charges] == [3, 1, 1, 1]
    assert [c.parent_line_number for c in po.additional_charges] == ["1", "2", "3", "4"]
    po, counts = _merge_explicit_page_charges_v63(po, pages, {})
    assert counts == {} and len(po.additional_charges) == 4
    po = _enhance_strict_totals_v47(po, pages, {})
    assert po.totals.total_net == 2172.09
    assert sum(line.line_total for line in lines) + sum(c.amount for c in po.additional_charges) == pytest.approx(po.totals.total_net)


def test_existing_charge_without_box_consumes_only_one_source_occurrence():
    fee = "[ECO PART] Eco Participation 1,00 7,00 20,00 8,40 7,00"
    source = page([(100, HEADER), (120, fee), (150, fee)])
    po = SimpleNamespace(lines=[], totals=Totals(), additional_charges=[AdditionalCharge(
        charge_type="environmental_fee", page=1, amount=7,
        evidence=Evidence(page=1, source_text=fee),
    )])
    _, counts = _merge_explicit_page_charges_v63(po, [source], {})
    assert counts == {"environmental_fee": 1}
    assert len(po.additional_charges) == 2


def test_vertical_table_accepts_spaced_prefix_without_consuming_following_fee():
    source = page([])
    source.text = HEADER + "\n[ELM 7736504817 ] Chauffe eau\n4,00\n365,00\n20,00\n438,00\n1 460,00\n[ECO PART] Eco Participation\n4,00\n1,75\n7,00"
    lines = _explicit_reference_vertical_items_v5202([source], {})
    assert [(line.material_number, line.quantity, line.line_total) for line in lines] == [("7736504817", 4, 1460)]


@pytest.mark.parametrize("registration,expected", [("34073666900037", 1), ("12345678900011", 0)])
def test_comma_delimited_legal_footer_matches_registration_not_supplier(registration, expected):
    source = page([
        (750, "HYDRO-DIFF', 18 RUE BASCOUT, 94600 CHOISY LE ROI - Tel : 01-48-92-08-95 - contact@hydrodiff.fr 1/2"),
        (770, "SARL au capital de 45 735 euros - SIRET 340 736 669 00037, RCS CRETEIL"),
    ])
    po = SimpleNamespace(buyer=Party(name="Commande 206", company_registration_number=registration),
                         supplier=Party(name="ELM LEBLANC / BOSCH"))
    _, count = _repair_buyer_from_matching_registration_footer_v5202(po, [source])
    assert count == expected
    if expected:
        assert po.buyer.name == "HYDRO-DIFF'"
        assert po.buyer.address.line1 == "18 RUE BASCOUT"
        assert po.buyer.address.city == "CHOISY LE ROI"
        assert po.buyer.email == "contact@hydrodiff.fr"
        assert po.buyer.phone == "01-48-92-08-95"


def test_comma_separated_house_numbers_remain_part_of_the_street():
    result = parse_address(Address(line1="124, 126 rue de Stalingrad", postal_code="93711", city="DRANCY CEDEX"))
    address, _, _ = result
    assert address.house_number == "124, 126"
    assert address.street == "RUE de Stalingrad"
    assert not address.address_complement


def test_printed_net_total_cannot_be_hidden_by_an_extracted_gross_total():
    result = order()
    result.pages = [page([(100, HEADER), (200, "Total EUR HT 100,00"), (220, "Total EUR TTC 120,00")])]
    result.totals.grand_total = 120
    validate(result, {})
    check = result.validation.checks["printed_total_presence"]
    assert check["printed_label_present"] is True
    assert check["valid"] is False
    result.totals.total_net = 100
    validate(result, {})
    assert result.validation.checks["printed_total_presence"]["valid"] is True

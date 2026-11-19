"""Source ownership in stacked scanned tables, mastheads and totals."""
import pytest

from po_ocr.extract import (
    _line_from_cells, all_rows, extract_header, extract_parties,
    extract_multiline_lines_from_pages, abbreviated_net_total,
)
from po_ocr.address_intelligence import parse_address
from po_ocr.models import Address
from uda.business import (
    _strict_text_totals_v48, _enhance_parties_zone_v2,
    _separate_promotional_continuation_notes_v5171,
    _enhance_inline_transaction_header_v46, _sanitize_party_addresses_v48,
)
from test_order_table_header_cells import order, page


def stacked_page(scale=1):
    source = page([
        (180, [("Article", 5, 38), ("Designation", 130, 170),
               ("Quantite", 315, 355), ("P.U", 395, 420),
               ("Prix", 465, 490), ("net", 495, 510)]),
        (200, [("C01014", 5, 33), ("|7733703987", 40, 86)]),
        # G10-4 is close to the article column but is product description.
        (212, [("MODULE", 40, 65), ("G10-4", 70, 88),
               ("HOMECOM", 100, 131), ("EASY", 136, 155),
               ("(WI-FI)", 160, 193), ("20,000", 320, 348), ("U", 357, 364),
               ("40,00", 395, 420), ("18,00", 465, 490), ("23/11/26", 520, 565)]),
        (240, [("C26953", 5, 33), ("|7716704752", 40, 86)]),
        (252, [("CHMUR", 40, 65), ("OXYLIS", 70, 93), ("ICONDENS", 100, 135),
               ("MIXTE", 220, 245), ("24KW", 250, 270),
               ("5,000", 320, 348), ("U", 357, 364),
               ("775,00", 395, 420), ("775,00", 465, 490), ("23/11/26", 520, 565)]),
        (265, [("MARCHE", 40, 65), ("B2C", 70, 85), ("RBGA-10170897", 95, 155)]),
        (290, [("ATTENTION", 40, 100)]),
        (310, [("MI H.T.", 180, 220), ("4235 ,00", 230, 285)]),
    ])
    source.width *= scale
    source.height *= scale
    for word in source.words:
        word.bbox = tuple(value * scale for value in word.bbox)
    return source


@pytest.mark.parametrize("scale", [1, 4])
def test_stacked_anchor_not_replaced_by_model_name_on_numeric_row(scale):
    lines = extract_multiline_lines_from_pages([stacked_page(scale)])
    assert len(lines) == 2
    assert [line.customer_material_number for line in lines] == ["C01014", "C26953"]
    assert [line.material_number for line in lines] == ["7733703987", "7716704752"]
    assert lines[0].description == "MODULE G10-4 HOMECOM EASY (WI-FI)"
    assert lines[1].description.startswith("CHMUR OXYLIS ICONDENS MIXTE 24KW")
    assert [line.quantity for line in lines] == [20, 5]
    assert [line.unit_price for line in lines] == [18, 775]
    assert [line.line_total for line in lines] == [360, 3875]
    assert [line.requested_delivery_date for line in lines] == ["2026-11-23"] * 2
    assert _separate_promotional_continuation_notes_v5171(lines) == 1
    assert lines[1].description == "CHMUR OXYLIS ICONDENS MIXTE 24KW"
    assert lines[1].notes == ["MARCHE B2C RBGA-10170897"]


@pytest.mark.parametrize("quantity", ["24KW 5,000 U", "24KW U 5,000 U", "5,000 U", "24KW 5,000 UN"])
def test_quantity_unit_does_not_erase_description_spill(quantity):
    row = all_rows([page([(200, [("dummy", 1, 20)])])])[0]
    result = _line_from_cells({"material": "7716704752", "description": "CHAUDIERE",
                              "quantity": quantity, "unit_price": "775,00"}, row, "EUR")
    assert result.quantity == 5
    assert result.description == ("CHAUDIERE 24KW" if "24KW" in quantity else "CHAUDIERE")


@pytest.mark.parametrize("label", ["MI H.T.", "MT H.T.", "MNT HT"])
@pytest.mark.parametrize("amount", ["4426 ,46", "4 426, 46", "4426,46"])
def test_abbreviated_ocr_net_total_with_split_decimal(label, amount):
    result = order()
    _strict_text_totals_v48(result, [page([(740, [(label, 180, 230), (amount, 240, 290)])])], {})
    assert result.totals.total_net == result.totals.subtotal == 4426.46


@pytest.mark.parametrize("text", ["MI TVA 885,29", "MI H.T.\n4426,46", "MI HT 12345", "AMI HT 42,00"])
def test_ocr_total_tolerance_does_not_accept_unlabelled_amounts(text):
    assert abbreviated_net_total(text) is None


@pytest.mark.parametrize("text", ["Devise......... E", "Devise EUR", "DEVISER LE MONTAGE"])
def test_currency_label_does_not_generate_quote_number(text):
    source = page([(120, [(text, 10, 200)])])
    fields, _, _ = extract_header(all_rows([source]), "EUR")
    assert "quote_number" not in fields
    result = _enhance_inline_transaction_header_v46(order(), [source], {})
    assert not result.purchase_order.quote_number.value


@pytest.mark.parametrize("label", ["DEVIS", "QUOTE", "QUOTATION", "ANGEBOT"])
def test_explicit_quote_label_still_works(label):
    fields, _, _ = extract_header(all_rows([page([(120, [(label + ' DV-123', 10, 200)])])]), "EUR")
    assert fields["quote_number"].value.upper() == "DV-123"


@pytest.mark.parametrize("label", ["NET PAYER", "NET A PAYER", "MONTANT A PAYER", "TOTAL PAYER"])
def test_payment_summary_is_not_a_payer_party(label):
    parties = extract_parties(all_rows([page([(740, [(label, 400, 550)]), (755, [("5311,75", 450, 500)])])]))
    assert parties["payer"].name is None


def test_explicit_payer_party_remains_supported():
    parties = extract_parties(all_rows([page([(120, [("Payeur : SOCIETE SAS", 20, 200)])])]))
    assert parties["payer"].name == "SOCIETE SAS"


@pytest.mark.parametrize("routing,field,expected", [
    ("B.P.", "po_box", "BP 77605"), ("BP", "po_box", "BP 77605"),
    ("CS", "cs", "CS 77605"), ("TSA", "tsa", "TSA 77605"),
])
def test_postal_routing_label_is_not_stripped_as_contact_name(routing, field, expected):
    result = order()
    result.buyer.address = Address(line1="28 RUE ALPHA", line2=f"{routing} 77605 - 21076 DIJON CEDEX")
    _sanitize_party_addresses_v48(result, {})
    address, _, _ = parse_address(result.buyer.address)
    assert getattr(address, field) == expected
    assert (address.postal_code, address.city) == ("21076", "DIJON")


@pytest.mark.parametrize("offset", [0, 190])
def test_head_office_address_does_not_absorb_neighboring_agencies(offset):
    # Also place the head office in the second column: ownership comes from
    # the label, not a fixed x coordinate or the customer's identity.
    groups = [("SIÈGE SOCIAL", "28 RUE ALPHA", "BP 12345 - 21076 DIJON CEDEX"),
              ("AGENCE EST", "3 RUE BETA", "21300 CHENOVE"),
              ("AGENCE SUD", "Z.A. GAMMA", "52000 CHAUMONT")]
    if offset:
        groups[0], groups[1] = groups[1], groups[0]
    source = page([(15, [("EXEMPLE SAS", 10, 110)]), *[
        (50 + index * 18, [(group[index], 10 + col * 190, 175 + col * 190)
                           for col, group in enumerate(groups)]) for index in range(3)
    ]])
    # The label is shorter than the address, giving a real inter-column gap.
    for word in source.words:
        if word.bbox[1] == 50:
            word.bbox = (word.bbox[0], 50, word.bbox[0]+100, 60)
    result = _enhance_parties_zone_v2(order(), [source], {})
    address, _, _ = parse_address(result.buyer.address)
    assert address.house_number == "28"
    assert address.postal_code == "21076"
    assert address.city == "DIJON"
    assert "BETA" not in str(address.model_dump())
    assert "GAMMA" not in str(address.model_dump())

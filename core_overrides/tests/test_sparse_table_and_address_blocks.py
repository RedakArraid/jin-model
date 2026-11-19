"""Protect source ownership in compact order headers and sparse table cells."""
import pytest
import fitz
from types import SimpleNamespace

from po_ocr.address_intelligence import parse_address
from po_ocr.address_roles import recover_explicit_role_blocks
from po_ocr.config import load_config
from po_ocr.models import Address
from po_ocr.models import PageResult
from po_ocr.pdf_native import extract_native_page
from uda.business import (
    _enhance_order_header_from_page_layout, _enhance_strict_order_number_v47,
    _enhance_parties_zone_v2, _standard_native_table_items,
)
from uda.models import SourceRef
from uda.readers import _table, read_pdf_or_image
from test_order_table_header_cells import order, page


@pytest.mark.parametrize("number", ["CDEA-123456", "ABCD-000123", "CF-28-1234567"])
def test_compact_order_title_retains_prefix_and_excludes_neighbor_vendor(number):
    source = page([
        (112, [("Commande", 29, 100), ("N°", 104, 118), (number, 122, 230),
               ("FOURNISSEUR", 520, 580)]),
        (147, [("N° preneur d'ordre", 26, 92), ("ABC", 106, 123), ("93700 Drancy", 520, 579)]),
        (160, [("Date commande", 26, 85), ("18/11/2026", 106, 146)]),
    ])
    source.width, source.height = 792, 612
    result = _enhance_order_header_from_page_layout(order(), [source], load_config(None))
    result = _enhance_strict_order_number_v47(result, [source], load_config(None))
    field = result.purchase_order.number
    assert field.value == number
    assert field.evidence.source_text == number
    assert field.evidence.bbox == (122, 112, 230, 122)


@pytest.mark.parametrize("positions", [(0,), (1,), (2,), (0, 2), (0, 1, 2), ()])
@pytest.mark.parametrize("geometry", [False, True])
def test_optional_offer_values_never_broadcast_or_shift(positions, geometry):
    codes = ["7736000001", "7736000002", "7736000003"]
    rows = [(200 + i * 15, [(code, 10, 70), ("ARTICLE", 80, 140),
                            ("1", 200, 210), ("10,00", 240, 270), ("10,00", 300, 330)]
                           + ([(f"OFF-{i+1}", 400, 450)] if i in positions else []))
            for i, code in enumerate(codes)]
    source = page(rows)
    data = [["Code article", "Libellé", "Qté", "Prix unitaire net", "Montant HT", "N° Offre"],
            ["\n".join(codes), "ARTICLE\nARTICLE\nARTICLE", "1\n1\n1",
             "10,00\n10,00\n10,00", "10,00\n10,00\n10,00",
             "\n".join(f"OFF-{i+1}" for i in positions)]]
    table = _table("synthetic", data, SourceRef(page=1))
    if geometry:
        for cell in table.cells:
            if cell.row == 2:
                cell.source.bbox = ((0, 195, 75, 250) if cell.column == 1
                                    else (390, 195, 480, 250) if cell.column == 6 else None)
    lines = _standard_native_table_items(table, pages=[source])
    assert len(lines) == 3
    ambiguous = bool(positions) and len(positions) < 3 and not geometry
    assert [line.quote_number for line in lines] == [
        f"OFF-{i+1}" if i in positions and not ambiguous else None for i in range(3)]
    assert all(("quote_stack_alignment_unresolved" in line.warnings) == ambiguous for line in lines)


@pytest.mark.parametrize("locality,postal,city", [
    ("Strasbourg, 67100", "67100", "Strasbourg"),
    ("Saint-Étienne, 42000", "42000", "Saint-Étienne"),
    ("BP, 67100", None, None), ("Rue Exemple, 67100", None, None),
])
def test_comma_locality_not_street_or_routing(locality, postal, city):
    parsed, _, _ = parse_address(Address(raw_lines=[locality]))
    assert (parsed.postal_code, parsed.city) == (postal, city)
    if postal:
        assert not parsed.address_complement


def address_header():
    source = page([
        (15, [("Adresse", 520, 557), ("de", 560, 570), ("livraison", 573, 625)]),
        (26, [("CLIENT SAS", 27, 215), ("DEPOT", 520, 570)]),
        (40, [("10 RUE ALPHA", 51, 198), ("25 RUE BETA", 520, 625)]),
        (53, [("67100 Strasbourg", 77, 172), ("Metz, 57000", 520, 590)]),
        (66, [("FRANCE", 520, 550)]),
        (112, [("Commande N° CDEA-123456", 29, 206)]),
        (125, [("FOURNISSEUR", 520, 585)]),
        (138, [("126 RUE GAMMA", 520, 635)]),
        (151, [("93700 Drancy", 520, 579)]),
    ])
    source.width, source.height = 792, 612
    return source


def test_issuer_address_does_not_absorb_neighboring_labelled_delivery():
    result = _enhance_parties_zone_v2(order(), [address_header()], load_config(None))
    parsed, _, _ = parse_address(result.buyer.address)
    assert result.buyer.name == "CLIENT SAS"
    assert (parsed.house_number, parsed.street, parsed.postal_code, parsed.city) == (
        "10", "RUE ALPHA", "67100", "Strasbourg")
    assert "BETA" not in str(parsed.model_dump())


def test_delivery_ends_before_distant_unlabelled_supplier():
    result = recover_explicit_role_blocks(order(), [address_header()])
    parsed, _, _ = parse_address(result.ship_to.address)
    assert result.ship_to.name == "DEPOT"
    assert (parsed.house_number, parsed.street, parsed.postal_code, parsed.city) == (
        "25", "RUE BETA", "57000", "Metz")
    assert "GAMMA" not in str(parsed.model_dump())


def test_pdf_reader_preserves_cells_for_sparse_stacked_offer(tmp_path):
    path = tmp_path / "synthetic.pdf"
    xs = [20, 100, 240, 280, 370, 460, 560]
    with fitz.open() as pdf:
        sheet = pdf.new_page(width=600, height=600)
        for x in xs:
            sheet.draw_line((x, 100), (x, 200))
        for y in (100, 125, 200):
            sheet.draw_line((20, y), (560, y))
        headers = ["Code article", "Libelle", "Qte", "Prix unitaire net", "Montant HT", "Offre"]
        for x, value in zip(xs, headers):
            sheet.insert_text((x + 3, 115), value, fontsize=7)
        for index, y in enumerate((140, 155, 170)):
            values = [f"773600000{index+1}", "ARTICLE", "1", "10,00", "10,00", "OFF-123" if index == 2 else ""]
            for x, value in zip(xs, values):
                sheet.insert_text((x + 3, y), value, fontsize=7)
        text, words, confidence = extract_native_page(sheet, 1)
        source = PageResult(page=1, width=600, height=600, text=text, words=words,
                            confidence=confidence, source_type="native_pdf")
        pdf.save(path)
    engine = SimpleNamespace(config={"layout": {}}, _local_pages=lambda *_: ([source], []))
    result = read_pdf_or_image(path, engine)
    assert len(result.tables) == 1
    table = result.tables[0]
    assert table.source.bbox == (20, 100, 560, 200)
    assert all(cell.source.bbox for cell in table.cells)
    lines = _standard_native_table_items(table, pages=result.page_results)
    assert len(lines) == 3
    assert [line.quote_number for line in lines] == [None, None, "OFF-123"]

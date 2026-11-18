"""Orderable references must come from their owned table cells, not the first code."""
import pytest

from po_ocr.config import load_config
from po_ocr.extract import (
    _reference_cell, _column_headers_from_rows, all_rows,
    extract_lines_from_pages, extract_multiline_lines_from_pages,
)
from uda.business import _extract_geometry_table_items_v46
from test_order_table_header_cells import page, order


def owned_table(*, reference_label="Votre", lines=2, rules=True, scale=1):
    rows = [
        (225, [("Conditions", 25, 60), ("de", 65, 72), ("livraison", 75, 110), ("DDP", 140, 160)]),
        (245, [("Conditions de paiement", 25, 110), ("45 jours", 140, 170)]),
        (270, [("N°", 28, 38), ("d'article", 56, 88), ("Couleur", 108, 138),
               ("Description", 193, 238), (reference_label, 301, 321),
               ("référence", 324, 359), ("Quantité", 392, 424),
               ("Prix", 450, 464), ("Livraison", 515, 550)]),
        (300, [("1", 31, 36), ("123456", 45, 72), ("CHAUFFE", 149, 190),
               ("EAU", 194, 213), ("GPL", 216, 233),
               ("7", 285, 289), ("736", 292, 305), ("507", 308, 321), ("999", 324, 337),
               ("5,00", 403, 418), ("UN", 428, 439), ("167,75", 456, 480),
               ("EUR", 488, 502), ("11/01/2027", 513, 553)]),
        (313, [("BAS-NOX", 149, 189)]),
    ]
    if lines > 1:
        rows.extend([
            (335, [("2", 31, 36), ("654321", 45, 72), ("CHAUFFE", 149, 190),
                   ("EAU", 194, 213), ("VENTOUSE", 235, 278),
                   ("7", 285, 289), ("736", 292, 305), ("505", 308, 321), ("099", 324, 337),
                   ("14,00", 398, 418), ("UN", 428, 439), ("291,05", 456, 480),
                   ("EUR", 488, 502), ("11/01/2027", 513, 553)]),
            (348, [("BAS-NOX", 149, 189)]),
        ])
    rows.extend([(380, [("Montant", 370, 402), ("HT", 405, 420), ("4913,45", 470, 500)]),
                 (410, [("Suivi par", 30, 80), ("CONTACT", 380, 420)])])
    result = page(rows)
    if rules:
        result.vector_lines = [dict(orientation="vertical", x0=x, x1=x, y0=266, y1=285)
                               for x in (23, 43, 101, 147, 283, 377, 441, 504, 562)]
    if scale != 1:
        result.width *= scale
        result.height *= scale
        for word in result.words:
            word.bbox = tuple(v * scale for v in word.bbox)
        for line in result.vector_lines:
            for key in ("x0", "x1", "y0", "y1"):
                line[key] *= scale
    return result


@pytest.mark.parametrize("parser", ["spatial", "multiline", "geometry"])
@pytest.mark.parametrize("lines,scale", [(1, 1), (2, 1), (2, 4)])
def test_supplier_reference_after_description_has_priority(parser, lines, scale):
    source = owned_table(lines=lines, scale=scale)
    if parser == "geometry":
        extracted, charges = _extract_geometry_table_items_v46([source], load_config(None))
        assert not charges
    else:
        fn = extract_lines_from_pages if parser == "spatial" else extract_multiline_lines_from_pages
        extracted = fn([source])
    assert len(extracted) == lines
    assert [(x.material_number, x.customer_material_number, x.supplier_material_number)
            for x in extracted] == [
        ("7736507999", "123456", "7736507999"),
        ("7736505099", "654321", "7736505099"),
    ][:lines]
    assert [(x.quantity, x.unit_price, x.line_total) for x in extracted] == [
        (5, 167.75, 838.75), (14, 291.05, 4074.7),
    ][:lines]
    assert extracted[0].description == "CHAUFFE EAU GPL BAS-NOX"
    assert all(x.requested_delivery_date == "2027-01-11" for x in extracted)
    assert all(x.uom == "UN" for x in extracted)
    assert all("Suivi" not in x.description and "736" not in x.description for x in extracted)
    assert "7 736 507 999" in extracted[0].raw_text


@pytest.mark.parametrize("label", ["Votre", "Your"])
def test_relative_supplier_header_and_unqualified_customer_column(label):
    columns = _column_headers_from_rows(all_rows([owned_table(reference_label=label)])[2:3])
    assert columns["supplier_material"] > columns["description"]
    assert columns["customer_material"] < columns["description"]


@pytest.mark.parametrize("value,expected", [
    ("7 736 507 999", "7736507999"),
    ("77 33 701 099", "7733701099"),
    ("8 700 205 099 0", "87002050990"),
    ("0 123 456 789", "0123456789"),
    ("7736507999", "7736507999"),
    ("AB12345", "AB12345"),
    ("7736507999 7736505099", "7736507999"),
    ("PIECES DETACHEES", None),
    ("11/01/2027", None),
    ("7 736 507 999 GPL", None),
    ("123456 654321", "123456"),
])
def test_reference_cell_groups_only_numeric_typography(value, expected):
    assert _reference_cell(value)[0] == expected


def test_supplier_reference_in_one_column_without_customer_code():
    source = page([(200, [("Your", 20, 40), ("reference", 44, 82),
                          ("Description", 150, 210), ("Quantity", 350, 390), ("Price", 450, 480)])])
    columns = _column_headers_from_rows(all_rows([source]))
    assert "supplier_material" in columns
    assert "customer_material" not in columns


def test_owned_reference_columns_without_vector_rules():
    extracted, charges = _extract_geometry_table_items_v46([owned_table(rules=False)], load_config(None))
    assert not charges
    assert [(x.supplier_material_number, x.customer_material_number) for x in extracted] == [
        ("7736507999", "123456"), ("7736505099", "654321")]


def test_owned_columns_keep_footer_instruction_guard():
    source = owned_table(lines=1)
    note = page([(330, [("FRANCO LIVRAISON EN MATINEE", 149, 280)])])
    source.words.extend(note.words)
    lines, _ = _extract_geometry_table_items_v46([source], load_config(None))
    assert len(lines) == 1
    assert "FRANCO" not in lines[0].description


def test_unrelated_possessive_does_not_assign_a_reference_role():
    source = page([(200, [("Article", 20, 55), ("Description", 150, 210),
                          ("Votre", 215, 240), ("Quantité", 350, 390), ("Prix", 450, 480)])])
    columns = _column_headers_from_rows(all_rows([source]))
    assert "supplier_material" not in columns


def test_unlabelled_neighbor_is_not_a_delivery_department():
    from po_ocr.address_roles import recover_explicit_role_blocks
    source = page([
        (140, [("Adresse", 28, 60), ("de", 64, 73), ("livraison", 77, 111),
               ("VENDEUR", 278, 320), ("SAS", 325, 342)]),
        (160, [("CLIENT SITE", 28, 100)]),
        (180, [("7 avenue de Test", 28, 135), ("124 rue Centrale", 278, 370)]),
        (195, [("11200 Lezignan Corbieres", 28, 155), ("93711 Drancy CEDEX", 278, 385)]),
        (210, [("FRANCE", 28, 70), ("FRANCE", 278, 320)]),
        (240, [("Conditions de paiement", 28, 150)]),
    ])
    result = recover_explicit_role_blocks(order(), [source])
    assert result.ship_to.name == "CLIENT SITE"
    assert not result.ship_to.department
    assert not result.ship_to.contact.department
    assert "VENDEUR" not in str(result.ship_to.address)

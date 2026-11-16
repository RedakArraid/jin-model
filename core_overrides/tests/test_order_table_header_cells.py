"""Generic regressions for dotted table headers, wrapped kits and party blocks."""
import pytest
import fitz
from po_ocr.pdf_native import extract_native_page
from po_ocr.validate import validate

from po_ocr.config import load_config
from po_ocr.models import Address, DocumentMeta, LineItem, PageResult, PurchaseOrderResult, WordToken
from po_ocr.address_intelligence import parse_address
from po_ocr.address_roles import formatted_address, recover_registration_party_blocks, recover_explicit_role_blocks
from uda.business import (
    _extract_geometry_table_items_v46, _enhance_requested_delivery_date_v5169,
    _enhance_strict_order_number_v47,
)
from uda.material_references import recover_material_references_from_descriptions


def page(rows, number=1):
    words = [WordToken(text=text, bbox=(left, y, right, y+10), page=number,
                       confidence=1, source="native_pdf")
             for y, cells in rows for text, left, right in cells]
    return PageResult(page=number, width=595, height=842, source_type="native_pdf",
                      words=words, confidence=1,
                      text="\n".join(" ".join(text for text, _, _ in cells) for _, cells in rows))


def order():
    return PurchaseOrderResult(document=DocumentMeta(
        id="test", filename="synthetic.pdf", extension="pdf", file_size=1, sha256="test",
        page_count=2, is_pdf=True, processing_timestamp="2026-01-01T00:00:00Z", model_version="test",
    ))


def header(unit_price="P.U."):
    return (260, [("Reference", 16, 70), ("Designation", 92, 152),
                  ("Quantite", 355, 390), (unit_price, 420, 440), ("HT", 443, 458),
                  ("Montant", 495, 525), ("HT", 528, 540)])


@pytest.mark.parametrize("unit_price", ["P.U.", "P.U", "PU"])
def test_dotted_price_header_keeps_bottom_rows_and_wrapped_next_page(unit_price):
    pages = [page([
        header(unit_price),
        (720, [("CLI00001", 16, 70), ("CLIMATE 3000i REF ENSEMBLE 7 733 701 735", 92, 328),
               ("30,000", 360, 390), ("291,700", 420, 455), ("8751,00", 495, 540)]),
        (772, [("CLI00002", 16, 70), ("CLIMATE 6000i CODE 77 33 701 844 COMPOSE DE", 92, 328),
               ("2,000", 360, 390), ("441,500", 420, 455), ("883,00", 495, 540)]),
        (790, [("UNITE INTERIEUR", 92, 220)]),
    ]), page([
        header(unit_price),
        (280, [("77 33 701 808 + GROUPE EXTERIEUR 77 33 701 809", 92, 328)]),
        (308, [("CLI00003", 16, 70), ("UNITE CLIMATE 6000iW CODE 77 33 701 810", 92, 328),
               ("10,000", 360, 390), ("224,200", 420, 455), ("2242,00", 495, 540)]),
        (350, [("Total HT", 400, 460), ("11876,00", 495, 540)]),
        (800, [("Notre droit de revendication", 92, 328)]),
    ], 2)]
    lines, charges = _extract_geometry_table_items_v46(pages, load_config(None))
    recover_material_references_from_descriptions(lines, load_config(None))
    assert not charges
    assert [(line.material_number, line.customer_material_number, line.quantity, line.line_total)
            for line in lines] == [
        ("7733701735", "CLI00001", 30, 8751),
        ("7733701844", "CLI00002", 2, 883),
        ("7733701810", "CLI00003", 10, 2242),
    ]
    assert "UNITE INTERIEUR 77 33 701 808" in lines[1].description
    assert "77 33 701 809" in lines[1].description
    assert "revendication" not in lines[-1].description
    assert [line.page for line in lines] == [1, 1, 2]
    assert sum(line.line_total for line in lines) == 11876


@pytest.mark.parametrize("description,expected", [
    ("KIT REF ENSEMBLE 7 733 701 735 REF 7733701564 + 7733701565", "7733701735"),
    ("KIT CODE 77 33 701 844 COMPOSE DE 77 33 701 808 + 77 33 701 809", "7733701844"),
    ("ARTICLE CODE 77 33 701 932 PUISSANCE 4.1 KW", "7733701932"),
    ("ARTICLE REF 7733701564 REF 7733701565", None),
    ("ARTICLE REMPLACE CODE 7733701564", None),
    ("ARTICLE 7733701564", None),
    ("ARTICLE CODE 77 33 701 932 5", None),
    ("ARTICLE CODE 7733701932ABC", None),
])
def test_labeled_reference_not_components_dimensions_or_ambiguous_refs(description, expected):
    line = LineItem(material_number="CLI00001", supplier_material_number="CLI00001",
                    description=description, quantity=2, unit_price=10, line_total=20)
    recover_material_references_from_descriptions([line], load_config(None))
    assert line.supplier_material_number == (expected or "CLI00001")
    assert line.customer_material_number == ("CLI00001" if expected else None)


def test_label_does_not_overwrite_a_supported_reference():
    line = LineItem(material_number="7733701564", description="ARTICLE CODE 7733701844",
                    quantity=2, unit_price=10)
    recover_material_references_from_descriptions([line], load_config(None))
    assert line.material_number == "7733701564"


def test_generated_native_pdf_with_dotted_headers_and_three_decimal_cells():
    with fitz.open() as doc:
        pages = []
        for i, y in enumerate((780, 305)):
            pdf_page = doc.new_page(width=595, height=842)
            for x, text in [(16, "Reference"), (92, "Designation"), (355, "Quantite"),
                            (420, "P.U. HT"), (495, "Montant HT")]:
                pdf_page.insert_text((x, 260), text, fontsize=8)
            for x, text in [(16, f"CLI0000{i+1}"), (92, "SERIE 6000i CODE 77 33 701 844"),
                            (360, "30,000"), (420, "441,500"), (495, "13 245,00")]:
                pdf_page.insert_text((x, y), text, fontsize=8)
            text, words, confidence = extract_native_page(pdf_page, i+1)
            pages.append(PageResult(page=i+1, width=595, height=842, text=text,
                                    words=words, confidence=confidence, source_type="native_pdf"))
        lines, _ = _extract_geometry_table_items_v46(pages, load_config(None))
        assert [(line.quantity, line.unit_price, line.line_total) for line in lines] == [
            (30, 441.5, 13245), (30, 441.5, 13245),
        ]


@pytest.mark.parametrize("price_label", ["P.U. HT", "P.U HT", "P. U. H.T.", "PU HT"])
def test_missing_price_is_flagged_when_dotted_header_is_present(price_label):
    result = order()
    result.pages = [page([(260, [(price_label, 400, 450)])])]
    result.lines = [LineItem(material_number="7733701844", quantity=2, line_total=883)]
    validate(result, {"po_number_contact_collision_check": False})
    check = result.validation.checks["line_field_completeness"]
    assert check["price_header"] is True
    assert check["valid"] is False


def test_explicit_order_cell_uses_its_column_not_issuer_date_or_vendor():
    source = page([
        (18, [("SOCIETE CLIENT", 15, 180), ("Commande", 261, 306), ("N°", 308, 318),
              ("Date", 392, 410), ("Fournisseur", 488, 536)]),
        (34, [("ZI SAINT JOSEPH", 15, 120), ("CO016642", 270, 310),
              ("08/02/2027", 380, 422), ("FO000012", 493, 532)]),
    ])
    result = _enhance_strict_order_number_v47(order(), [source], load_config(None))
    assert result.purchase_order.number.value == "CO016642"
    assert result.purchase_order.number.evidence.bbox == (270, 34, 310, 44)


def test_delivery_not_before_date_is_source_backed_and_qualified():
    result = _enhance_requested_delivery_date_v5169(order(), [page([
        (300, [("FACTURATION ET LIVRAISON A PARTIR DU 08/02/2027", 90, 335)]),
    ])], load_config(None))
    assert result.purchase_order.expected_delivery_date.value == "2027-02-08"
    assert "delivery_not_before_date" in result.purchase_order.expected_delivery_date.warnings


def test_zone_excludes_postcode_city_and_contact_instruction():
    result, _, _ = parse_address(Address(raw_lines=["ZA LA CARRETIERE - 04130 VOLX - TEL"]))
    assert result.industrial_zone == "ZA LA CARRETIERE"
    assert result.postal_code == "04130"
    assert result.city == "VOLX"
    assert formatted_address(result) == "ZA LA CARRETIERE, 04130 VOLX, France"


def test_registration_label_blocks_isolate_names_addresses_and_contacts():
    source = page([
        (18, [("S.A.S. SOCIETE CLIENT", 15, 195), ("Commande N°", 261, 318)]),
        (34, [("ZI SAINT JOSEPH", 15, 120), ("CO016642", 270, 310)]),
        (50, [("240 RUE DES ENTREPRENEURS", 15, 195)]),
        (67, [("04100 MANOSQUE", 15, 120)]),
        (83, [("Tel : 0492727667", 15, 140)]),
        (116, [("SIRET : 48313573700011", 15, 165)]),
        (133, [("N/Id", 15, 31), ("CEE : FR49483135737", 33, 170)]),
        (139, [("SOCIETE FOURNISSEUR", 269, 400)]),
        (172, [("124-125 rue de Stalingrad", 269, 420)]),
        (188, [("93711 Drancy", 269, 350)]),
        (205, [("V/Id", 269, 290), ("CEE : FR50340530864", 293, 410)]),
    ])
    result = order()
    result.supplier.phone = "0492727667"
    result.buyer.contact.name = "Notre droit de revendication"
    recover_registration_party_blocks(result, [source])
    assert result.buyer.name == "S.A.S. SOCIETE CLIENT"
    assert result.buyer.address.line1 == "ZI SAINT JOSEPH"
    assert result.buyer.contact.name is None
    assert result.buyer.phone == "0492727667"
    assert result.supplier.name == "SOCIETE FOURNISSEUR"
    assert result.supplier.address.line1 == "124-125 rue de Stalingrad"
    assert result.supplier.phone is None


def test_inline_delivery_label_keeps_wrapped_company_and_site():
    source = page([
        (290, [("FACTURATION ET", 90, 150), ("LIVRAISON", 152, 202), ("A", 205, 213),
               ("PARTIR DU 08/02/2027", 216, 335)]),
        (300, [("LIVRAISON", 90, 145), ("A", 148, 155), ("L'ADRESSE", 158, 215),
               (":", 217, 220), ("CHAUFFAGE SANITAIRE", 225, 350)]),
        (310, [("MANOSQUIN - ZA LA CARRETIERE - 04130 VOLX - TEL", 90, 350)]),
        (328, [("1 HEURE AVANT SVP", 90, 210)]),
        (350, [("REMISE 3000 I = 57.25%", 90, 235)]),
        (410, [("Designation Quantite Montant", 90, 350)]),
    ])
    result = order()
    result.ship_to.name = "MANOSQUIN - ZA LA CARRETIERE - 04130 VOLX - TEL"
    result.ship_to.address = Address(line1="04130 VOLX - TEL", postal_code="04130", city="VOLX - TEL")
    result = recover_explicit_role_blocks(result, [source])
    assert result.ship_to.name == "CHAUFFAGE SANITAIRE MANOSQUIN"
    address, _, _ = parse_address(result.ship_to.address)
    assert address.postal_code == "04130"
    assert address.industrial_zone == "ZA LA CARRETIERE"
    assert all("HEURE" not in value and "REMISE" not in value for value in address.raw_lines)
    recover_explicit_role_blocks(result, [source])
    assert result.ship_to.name == "CHAUFFAGE SANITAIRE MANOSQUIN"

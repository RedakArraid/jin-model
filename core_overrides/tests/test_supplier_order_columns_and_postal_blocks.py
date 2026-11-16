"""Supplier-reference columns and source-bounded postal form recovery."""
import pytest

from po_ocr.address_intelligence import parse_address
from po_ocr.address_roles import (
    _block_contacts, recover_named_supplier_form, recover_accounting_postal_notice,
    build_business_addresses,
    recover_explicit_role_blocks,
)
from po_ocr.config import load_config
from po_ocr.models import Address
from uda.business import _extract_geometry_table_items_v46
from test_order_table_header_cells import page, order


def table(freight=True, border=True, label="Réf.fournisseur"):
    rows = [
        (354, [("Numéro", 25, 52)]),
        (363, [("d'article", 25, 52), (label, 65, 119), ("Description", 221, 261),
               ("Unité", 349, 367), ("Quantité", 372, 403), ("Prix", 411, 425),
               ("unit.net", 427, 453), ("Remise", 468, 495), ("Montant", 505, 533), ("Dépôt", 552, 574)]),
        (402, [("239301", 25, 52), ("7736507599", 65, 109), ("CHAUFFE BAIN BOSCH WR8", 136, 244),
               ("UN", 351, 362), ("1,00", 389, 405), ("515,56", 429, 453),
               ("0", 473, 488), ("515,56", 519, 544), ("AUG", 555, 572)]),
    ]
    if freight:
        rows.append((416, [("239301", 25, 52), ("FDP", 65, 81), ("FRAIS DE PORT", 136, 210),
                           ("UN", 351, 362), ("1,00", 389, 405), ("35,00", 429, 453),
                           ("0", 473, 488), ("35,00", 519, 544), ("AUG", 555, 572)]))
    rows.extend([
        (650, [("toute facture non envoyée à cette adresse aura pour conséquence", 30, 330)]),
        (700, [("Total HT", 420, 470), ("550,56", 510, 550)]),
    ])
    source = page(rows)
    if border:
        source.vector_lines = [
            {"orientation": "vertical", "x0": x, "x1": x, "y0": 350, "y1": 470}
            for x in (16, 60, 126, 344, 369, 408, 460, 500, 548, 580)
        ] + [{"orientation": "horizontal", "x0": 16, "x1": 580, "y0": 470, "y1": 470}]
    return source


@pytest.mark.parametrize("label", ["Réf.fournisseur", "Référence.fournisseur", "Ref.supplier"])
def test_explicit_supplier_reference_is_not_the_customer_article(label):
    lines, charges = _extract_geometry_table_items_v46([table(label=label)], load_config(None))
    assert len(lines) == 1
    line = lines[0]
    assert (line.material_number, line.supplier_material_number, line.customer_material_number) == (
        "7736507599", "7736507599", "239301")
    assert line.description == "CHAUFFE BAIN BOSCH WR8"
    assert (line.quantity, line.unit_price, line.line_total) == (1, 515.56, 515.56)
    assert len(charges) == 1
    assert (charges[0].code, charges[0].charge_type, charges[0].amount) == ("FDP", "shipping", 35)
    assert charges[0].description == "FRAIS DE PORT"
    assert charges[0].parent_material_number is None


def test_table_closing_border_prevents_accounting_text_becoming_description_without_freight():
    lines, _ = _extract_geometry_table_items_v46([table(freight=False)], load_config(None))
    assert lines[0].description == "CHAUFFE BAIN BOSCH WR8"


def test_shipping_row_ends_description_even_without_vector_borders():
    lines, _ = _extract_geometry_table_items_v46([table(border=False)], load_config(None))
    assert lines[0].description == "CHAUFFE BAIN BOSCH WR8"


def test_internal_divider_ending_at_merged_cell_does_not_truncate_following_products():
    source = table(freight=False)
    source.vector_lines.append({"orientation": "vertical", "x0": 130, "x1": 130, "y0": 350, "y1": 420})
    source.vector_lines.append({"orientation": "horizontal", "x0": 16, "x1": 580, "y0": 420, "y1": 420})
    source.words.extend(page([(445, [
        ("239302", 25, 52), ("7736507600", 65, 109), ("CHAUFFE BAIN WR10", 136, 244),
        ("UN", 351, 362), ("1,00", 389, 405), ("515,56", 429, 453),
        ("0", 473, 488), ("515,56", 519, 544), ("AUG", 555, 572),
    ])]).words)
    lines, _ = _extract_geometry_table_items_v46([source], load_config(None))
    assert [line.material_number for line in lines] == ["7736507599", "7736507600"]
    assert lines[-1].description == "CHAUFFE BAIN WR10"


@pytest.mark.parametrize("lines", [
    ["ZI DES JARDINS - 431 AV DE LA", "GARE", "13400 AUBAGNE"],
    ["ZI DES JARDINS - 431 AV DE LA GARE", "13400 AUBAGNE"],
])
def test_named_zone_and_wrapped_numbered_street_are_separate_source_preserved(lines):
    parsed, _, _ = parse_address(Address(raw_lines=lines))
    assert parsed.industrial_zone == "ZI DES JARDINS"
    assert parsed.house_number == "431"
    assert parsed.street == "AVENUE DE LA GARE"
    assert parsed.postal_code == "13400"
    assert parsed.city == "AUBAGNE"
    assert not parsed.address_complement
    assert parsed.raw_lines == lines
    twice, _, _ = parse_address(parsed)
    assert twice.street == parsed.street
    assert twice.industrial_zone == parsed.industrial_zone
    assert not twice.address_complement


def test_complete_street_does_not_absorb_an_unrelated_complement_or_country():
    parsed, _, _ = parse_address(Address(raw_lines=["431 AV DE LA GARE", "BATIMENT CENTRAL", "13400 AUBAGNE"]))
    assert parsed.street == "AVENUE DE LA GARE"
    broken, _, _ = parse_address(Address(raw_lines=["431 AV DE LA", "FRANCE"]))
    assert "FRANCE" not in broken.street


def test_building_word_in_company_name_does_not_pollute_postal_components():
    source = page([
        (240, [("Adresse", 310, 350), ("de", 354, 365), ("Livraison", 369, 430)]),
        (255, [("SOCIETE DU BATIMENT", 310, 445)]),
        (270, [("ZI DES JARDINS - 431 AV DE LA", 310, 490)]),
        (283, [("GARE", 310, 340)]),
        (296, [("13400 AUBAGNE", 310, 400)]),
    ])
    result = recover_explicit_role_blocks(order(), [source])
    assert result.ship_to.name == "SOCIETE DU BATIMENT"
    parsed, _, _ = parse_address(result.ship_to.address)
    assert not parsed.building
    assert parsed.street == "AVENUE DE LA GARE"


@pytest.mark.parametrize("text,phone,fax", [
    ("Téléphone / Fax 0820003000 / 0143117317", "0820003000", "0143117317"),
    ("Téléphone / Fax 0426783636", "0426783636", None),
    ("Fax : 0143117317", None, "0143117317"),
])
def test_shared_telephone_fax_label_keeps_phone_first(text, phone, fax):
    contacts, postal = _block_contacts([text])
    assert contacts.get("phone") == phone
    assert contacts.get("fax") == fax
    assert not postal


def supplier_form(repeated=True, title="Commande Fournisseur N° TEST00001"):
    rows = [
        (25, [("ZI DES JARDINS - 431 AV DE LA GARE", 240, 440)]),
        (40, [("13400 AUBAGNE", 240, 325)]),
        (55, [("Téléphone / Fax 0426783636", 240, 380)]),
        (100, [(title, 170, 440)]),
        (170, [("Nom", 25, 45), ("FOURNISSEUR SAS", 120, 240)]),
        (185, [("Téléphone / Fax", 25, 100), ("0820003000 / 0143117317", 120, 240)]),
        (240, [("Adresse de Livraison", 310, 430)]),
        (255, [("CLIENT", 310, 365)]),
        (270, [("Téléphone 0781829152", 310, 430)]),
    ]
    if repeated:
        rows.extend([(135, [("FOURNISSEUR SAS", 310, 430)]),
                     (150, [("124-126 RUE DE STALINGRAD", 310, 455)]),
                     (165, [("93711 DRANCY CEDEX", 310, 425)])])
    return page(sorted(rows))


def test_supplier_form_uses_own_address_and_phone_not_delivery():
    result = order()
    result.supplier.code = "13400"
    result.supplier.phone = "0781829152"
    result.buyer.fax = result.buyer.contact.fax = "0426783636"
    recover_named_supplier_form(result, [supplier_form()])
    assert result.supplier.name == "FOURNISSEUR SAS"
    assert result.supplier.code is None
    address, _, _ = parse_address(result.supplier.address)
    assert (address.house_number, address.postal_code, address.city, address.cedex) == ("124-126", "93711", "DRANCY", True)
    assert result.supplier.contact.phone == "0820003000"
    assert result.supplier.contact.fax == "0143117317"
    assert result.buyer.contact.phone == "0426783636"
    assert result.buyer.contact.fax is None
    addresses = build_business_addresses(result, [supplier_form()], {"addresses": {"verification": {"enabled": False}}})
    assert any(item.role == "supplier" for item in addresses)


def test_supplier_account_explicitly_labelled_is_not_removed_for_looking_postal():
    source = supplier_form()
    code_row = page([(200, [("Code fournisseur 13400", 20, 170)])])
    source.words.extend(code_row.words)
    result = order()
    result.supplier.code = "13400"
    recover_named_supplier_form(result, [source])
    assert result.supplier.name == "FOURNISSEUR SAS"
    assert result.supplier.code == "13400"


@pytest.mark.parametrize("source", [supplier_form(repeated=False), supplier_form(title="Confirmation de vente")])
def test_unbound_name_form_does_not_change_supplier(source):
    result = order()
    result.supplier.name = "KNOWN SUPPLIER"
    recover_named_supplier_form(result, [source])
    assert result.supplier.name == "KNOWN SUPPLIER"


@pytest.mark.parametrize("invoice_instruction", [True, False])
def test_accounting_relocation_requires_invoice_address_instruction(invoice_instruction):
    rows = [(650, [("ATTENTION : notre SERVICE COMPTABILITE a déménagé : CLIENT – 5 ROUTE DE MONTS - 37300 JOUE LES TOURS", 20, 575)])]
    if invoice_instruction:
        rows.append((663, [("toute facture non envoyée à cette adresse aura pour conséquence un retard", 20, 540)]))
    result = order()
    recover_accounting_postal_notice(result, [page(rows)])
    if not invoice_instruction:
        assert not result.bill_to.name
        return
    assert result.bill_to.name == "CLIENT"
    parsed, _, _ = parse_address(result.bill_to.address)
    assert (parsed.house_number, parsed.street, parsed.postal_code, parsed.city) == (
        "5", "ROUTE DE MONTS", "37300", "JOUE LES TOURS")
    addresses = build_business_addresses(result, [page(rows)], {"addresses": {"verification": {"enabled": False}}})
    assert any(item.role == "bill_to" for item in addresses)

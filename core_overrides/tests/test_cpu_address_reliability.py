"""Explicit postal blocks must retain their own roles and contact coordinates."""

from types import SimpleNamespace

from po_ocr.address_intelligence import parse_address
from po_ocr.address_roles import build_business_addresses, formatted_address, recover_explicit_role_blocks
from po_ocr.models import Address, Contact, PageResult, Party, WordToken


def _page(rows):
    words = []
    for y, fragments in rows:
        for x, text in fragments:
            for token in text.split():
                width = max(3.0, len(token) * 4.2)
                words.append(WordToken(text=token, page=1, bbox=(x, y, x + width, y + 8), confidence=1.0, source="native_pdf"))
                x += width + 3
    return PageResult(page=1, width=595, height=841, source_type="native_pdf", words=words,
                      text="\n".join(" ".join(text for _, text in fragments) for _, fragments in rows), confidence=1.0)


def _parties(**overrides):
    return SimpleNamespace(**{role: overrides.get(role, Party()) for role in ("buyer", "supplier", "ship_to", "bill_to")})


def test_vertical_role_labels_keep_delivery_and_billing_blocks_separate():
    page = _page([
        (50, [(80, "DEPOT LIVRAISON"), (350, "FOURNISSEUR LOCAL")]),
        (65, [(25, "LIEU DE"), (80, "Service logistique"), (290, "DESTINATAIRE"), (350, "124 RUE DU FOURNISSEUR")]),
        (77, [(20, "LIVRAISON"), (80, "Zone des Ateliers"), (350, "93700 DRANCY")]),
        (90, [(80, "83140 SIX FOURS LES PLAGES"), (350, "Tel: 01 40 00 00 00")]),
        (102, [(80, "Alex 06 12 34 56 78")]),
        (113, [(80, "Reliquat ACCEPTE")]),
        (133, [(350, "En 1 exemplaire a")]),
        (145, [(299, "FACTURE"), (350, "ACHETEUR CENTRAL")]),
        (157, [(312, "A"), (350, "Depot de: SUD")]),
        (169, [(298, "EXPEDIER"), (350, "2,Rue des Lilas - BP 1173")]),
        (181, [(350, "06003 Nice CEDEX 1")]),
        (193, [(350, "Ou par mail a : factures@example.fr")]),
        (236, [(30, "Designation Quantite Prix unitaire")]),
    ])
    wrong_party = Party(name="FOURNISSEUR LOCAL", address=Address(line1="124 RUE DU FOURNISSEUR", line2="93700 DRANCY", postal_code="93700", city="DRANCY"),
                        email="supplier@example.fr", phone="01 40 00 00 00", contact=Contact(email="supplier@example.fr", phone="01 40 00 00 00"))
    parties = _parties(bill_to=wrong_party)
    addresses = build_business_addresses(parties, [page], {"addresses": {"verification": {"enabled": False}}})
    by_role = {address.role: address for address in addresses}
    assert by_role["ship_to"].address.postal_code == "83140"
    assert by_role["ship_to"].address.city == "SIX FOURS LES PLAGES"
    assert by_role["ship_to"].party_name == "DEPOT LIVRAISON"
    assert by_role["ship_to"].contact_name == "Alex"
    assert by_role["ship_to"].contact_phone == "06 12 34 56 78"
    assert by_role["bill_to"].party_name == "ACHETEUR CENTRAL"
    assert by_role["bill_to"].address.postal_code == "06003"
    assert by_role["bill_to"].address.house_number == "2"
    assert by_role["bill_to"].address.po_box == "BP 1173"
    assert by_role["bill_to"].contact_email == "factures@example.fr"
    assert by_role["bill_to"].contact_phone is None
    assert "FOURNISSEUR" not in by_role["bill_to"].formatted_address


def test_named_delivery_contact_is_kept_outside_postal_address():
    page = _page([
        (330, [(50, "Facture a :"), (294, "Adresse de livraison :")]),
        (344, [(40, "ACHETEUR"), (286, "DEPOT CLIENT")]),
        (356, [(40, "1 RUE DU SIEGE"), (286, "435 RUE DES ATELIERS")]),
        (390, [(40, "01704 BEYNOST CEDEX"), (286, "34070"), (370, "MONTPELLIER GAROSUD")]),
        (405, [(40, "France"), (286, "France"), (550, "11028")]),
        (423, [(286, "Mederic 06 37 67 07 16")]),
        (467, [(30, "Designation Quantite Prix unitaire")]),
    ])
    parties = recover_explicit_role_blocks(_parties(), [page])
    address, _, _ = parse_address(parties.ship_to.address)
    assert address.postal_code == "34070"
    assert address.city == "MONTPELLIER GAROSUD"
    assert parties.ship_to.contact.name == "Mederic"
    assert parties.ship_to.contact.phone == "06 37 67 07 16"
    assert parties.bill_to.contact.phone is None
    assert not any("Mederic" in line or "11028" in line for line in address.raw_lines)


def test_adresse_destinataire_recovers_the_delivery_column():
    page = _page([
        (50, [(20, "Adresse destinataire"), (340, "Commande Fournisseur")]),
        (65, [(20, "Exincourt sanitaire chauffage"), (340, "E.L.M. Leblanc SAS")]),
        (80, [(20, "53, rue d'Egouttes"), (340, "124, 126 Rue de Stalingrad")]),
        (95, [(20, "B.P 27"), (340, "93711 DRANCY CEDEX")]),
        (110, [(20, "25400 EXINCOURT")]),
        (145, [(20, "Designation Quantite Prix unitaire")]),
    ])
    parties = recover_explicit_role_blocks(_parties(), [page])
    address, _, _ = parse_address(parties.ship_to.address)
    assert parties.ship_to.name == "Exincourt sanitaire chauffage"
    assert address.house_number == "53"
    assert address.street_name == "d'Egouttes"
    assert address.po_box == "BP 27"
    assert address.postal_code == "25400"
    assert address.city == "EXINCOURT"


def test_unlabelled_recipient_is_not_assumed_to_be_supplier_or_delivery():
    page = _page([(20, [(350, "DESTINATAIRE")]), (34, [(350, "CLIENT")]),
                  (48, [(350, "1 RUE DES ARBRES")]), (62, [(350, "75001 PARIS")])])
    parties = recover_explicit_role_blocks(_parties(), [page])
    assert parties.supplier.name is None
    assert parties.ship_to.name is None


def test_shortened_street_type_is_not_duplicated_as_address_complement():
    address, _, _ = parse_address(Address(line1="15 Av Du General Pruneau", line2="83000 TOULON"))
    assert address.street_type == "AVENUE"
    assert address.address_complement is None
    assert formatted_address(address).count("Pruneau") == 1


def test_house_number_comma_without_space_keeps_street_and_postal_routing():
    address, _, _ = parse_address(Address(line1="2,Rue des Lilas - BP 1173", line2="06003 Nice CEDEX 1"))
    assert address.house_number == "2"
    assert address.street_type == "RUE"
    assert address.street_name == "des Lilas"
    assert address.po_box == "BP 1173"
    assert address.postal_code == "06003"
    assert address.city == "Nice"
    assert address.cedex_number == "1"


def test_role_label_before_numbered_street_is_not_part_of_street_components():
    source = Address(line1="DESTINATAIRE 126 RUE DE STALINGRAD", line2="93700 DRANCY")
    address, _, _ = parse_address(source)
    assert address.house_number == "126"
    assert address.street_type == "RUE"
    assert address.street_name == "DE STALINGRAD"
    assert address.raw_lines[0] == "DESTINATAIRE 126 RUE DE STALINGRAD"
    assert source.line1 == "DESTINATAIRE 126 RUE DE STALINGRAD"
    assert address.address_complement is None
    party = Party(name="SOCIETE FOURNISSEUR", address=source)
    parties = _parties(supplier=party)
    result = build_business_addresses(parties, [], {"addresses": {"verification": {"enabled": False}}})
    assert [item.role for item in result] == ["supplier"]
    assert parties.ship_to.name is None


def test_recipient_label_followed_by_customer_code_is_not_a_numbered_street():
    address, _, _ = parse_address(Address(line1="DESTINATAIRE 9034 SOCIETE EXEMPLE", line2="93700 DRANCY"))
    assert address.house_number is None
    assert address.street_type is None
    assert address.street_name is None
    assert address.raw_lines[0] == "DESTINATAIRE 9034 SOCIETE EXEMPLE"

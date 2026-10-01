"""CPU routing regression tests: titles, item tables and legal references."""

import pytest

from uda.engine import UniversalDocumentAI
from uda.semantic import aggregate_page_classifications, classify_page, classify_pages, detect_document_type


ORDER_TEXT = """COMMANDE N° 660092101
du 19/03/26
Lieu de livraison : notre depot
Qte / Unite Designation Reference Prix / Unite
3 PCE ECHANGEUR 87167723990 63,75 EUR / PCE
2 PCE TURBINE 87387208000 37,23 EUR / PCE
2 PCE MEMBRANE 87155058250 7,14 EUR / PCE
1 PCE FRAIS DE PORT 20,00 EUR / PCE
Cette commande se refere a nos conditions generales d'achat
"""

QUOTE_HEADER = """ENTREPRISE EXEMPLE
124 rue des Ateliers
Offre de prix
Contact commercial : Alice Exemple
Offre n° : HLA-300326-1338
Date de l'offre : 30/03/2026
Offre valable jusqu'au : 30/04/2026
"""

PRODUCT_TABLE = """Selection produits, pieces et services
Code article Designation Prix unitaire Qte Prix total HT
7733701988 Unite interieure 105,00 EUR 12 1 260,00 EUR
7733701989 Unite exterieure 215,00 EUR 12 2 580,00 EUR
Sous-total HT 3 840,00 EUR
"""

QUOTE_TERMS = """Informations additionnelles
Nos conditions generales de vente s'appliquent a ce devis.
Livraisons
Ce devis n'est pas un document d'engagement de livraison.
Merci de mentionner les conditions specifiques sur votre bon de commande.
La commande du client devra etre livree a l'adresse mentionnee sur ce devis.
En cas de changement d'adresse de livraison, contacter nos equipes.
Responsabilite, confidentialite et droit applicable.
"""


def _pages(*texts):
    return classify_pages([{"page": i + 1, "text": text} for i, text in enumerate(texts)], None)


def test_order_title_and_lines_outweigh_reference_to_purchase_terms():
    page = classify_page(ORDER_TEXT, page_number=1)
    assert page["type"] == "purchase_order"
    assert page["explicit_document_title"] == "purchase_order"
    assert "explicit-title:purchase_order" in page["signals"]


def test_ocr_title_joined_to_letterhead_outweighs_dense_legal_footer():
    text = """SAS CAPITAL DE 8 500 000 COMMANDE FOURNISSEUR
Siege social et coordonnees entreprise
001964998 26/03/26
Article Designation ante Pur unit. Y PU Net Montant HT
7736506316 1,000 PIEC 37,00 49,75 18,59 18,59
NOS MARCHANDISES SONT VENDUES AVEC RESERVE DE PROPRIETE
SUIVANT NOS CONDITIONS GENERALES DE VENTE
Responsabilite fournisseur acquereur, droit applicable et litiges.
1 Conditions de vente 2 Conditions de paiement 3 Livraison 4 Confidentialite
"""
    page = classify_page(text)
    assert page["type"] == "purchase_order"
    assert page["explicit_document_title"] == "purchase_order"


def test_standalone_terms_mentioning_orders_stay_legal():
    page = classify_page("""Conditions generales d'achat
Bon de commande : il doit mentionner les modalites de livraison.
1 Champ d'application : le fournisseur accepte le bon de commande.
2 Responsabilite : la commande ne peut etre annulee.
3 Confidentialite : donnees de l'acquereur.
4 Droit applicable et litiges.
""")
    assert page["type"] == "legal_terms"
    assert page["explicit_document_title"] is None


def test_acknowledgement_reminder_is_not_a_new_purchase_order():
    page = classify_page("""Relance retour(s) sans AR
Nous n'avons pas reçu votre accusé de réception pour la commande 5423707.
Merci de nous retourner votre confirmation de commande.
""")
    assert page["type"] == "generic_document"
    assert page["explicit_document_title"] == "generic_document"


def test_order_reminder_without_acknowledgement_is_not_a_new_purchase_order():
    page = classify_page("""Relance commande(s) sans AR
Relance du 11/02/2026
Merci de nous envoyer un accusé de réception pour les bons ci-dessous.
N° commande Date commande Date prévue Notre référence
5423707 10/02/2026 11/02/2026
""")
    assert page["type"] == "generic_document"
    assert page["explicit_document_title"] == "generic_document"


def test_notification_reminding_an_existing_order_is_not_a_new_order():
    page = classify_page("""NOTIFICATION DE RELANCE D'UNE COMMANDE
ATTENTION RELANCE
Commande N° 945890 du 19/01/2026 AR 18733908
A CE JOUR NOUS SOMMES TOUJOURS EN ATTENTE DE CETTE COMMANDE.
""")
    assert page["type"] == "generic_document"
    assert page["explicit_document_title"] == "generic_document"


def test_remainder_abandonment_notice_is_not_a_new_purchase_order():
    page = classify_page("""BOSCH P.D. DOMESTIQUES ABANDON DE RELIQUAT
Commande P-detachee : 5316064 / 8742
Reference : 1025786 / 16935788
Le reliquat ci-dessus est abandonne.
""")
    assert page["type"] == "generic_document"
    assert page["explicit_document_title"] == "generic_document"


def test_grouped_order_is_still_a_purchase_order():
    page = classify_page("""B.P. 61178 COMMANDE REGROUPEE 124 RUE DE STALINGRAD
DESIGNATION QUANTITES SEMAINE PRIX UNIT. MONTANT NET H.T.
COLIS CONTREMARQUE : PROLIANS BA MONTPELLIER
RAPPELEZ SUR FACTURE LE N° COMMANDE : ST 1 1A1 42564
""")
    assert page["type"] == "purchase_order"
    assert page["explicit_document_title"] == "purchase_order"


def test_order_number_title_joined_to_letterhead_is_a_purchase_order():
    page = classify_page("""ANCONETTI Page 1 Espace Sanitaire COMMANDE N° 674072553
15 Av Du General Pruneau du 24/02/26
LIEU DE LIVRAISON ANCONETTI CENTRALE
Quantite Designation Vos References Prix Unit.
""")
    assert page["type"] == "purchase_order"
    assert page["explicit_document_title"] == "purchase_order"


def test_visually_spaced_mainframe_order_title_is_a_purchase_order():
    page = classify_page("""C O M M A N D E __________________
N. : 009-2460-220126 22.01.2026
MESSIEURS NOUS VOUS PASSONS COMMANDE DES PRODUITS INDIQUES CI-DESSOUS
LIEU DE LIVRAISON : RICHARDSON AG. DE IRIGNY
DESIGNATION QUANTITE UC PRIX HT MT. EURO
""")
    assert page["type"] == "purchase_order"
    assert page["explicit_document_title"] == "purchase_order"


def test_order_cancellation_is_not_a_new_purchase_order():
    text = """REF A RAPPELER: Annulation 010940321D7PAAME
X DEMANDE D'ANNULATION D'UNE COMMANDE XX
Veuillez annuler la commande pour les lignes suivantes
Commande N° 940321 du 06/01/2026
"""
    page = classify_page(text)
    assert page["type"] == "order_cancellation"
    assert page["explicit_document_title"] == "order_cancellation"
    assert detect_document_type(text, "pdf") == "order_cancellation"


@pytest.mark.parametrize("text", [
    "Demande de prix DEM01105 Numéro\nCode élément Description Qté Montant HT TVA",
    "Date : 26/01/2026 DEMANDE DE Page PRIX 1/1 N° DP4799 A livrer le 26/01/2026\n"
    "Réf. Désignation Qte Unité PUHT Délai\nDemande de prix envoyé par MAIL.",
])
def test_request_for_price_is_not_a_purchase_order(text):
    page = classify_page(text)
    assert page["type"] == "request_for_quotation"
    assert page["explicit_document_title"] == "request_for_quotation"
    assert detect_document_type(text, "pdf") == "request_for_quotation"


def test_approval_form_that_mentions_an_attached_order_is_not_the_order():
    first = """C-SC2: Confidential
RB General Approval Form
Brief Description: Validation remplacement sous garantie
Commande en gratuit en PJ (bon de commande N° 02-9260205710).
Attachment: COMMANDE_9260205710_1_230226_104317
Approver(s): Validation I2R
"""
    history = """Change History
Description: bon de commande N° 02-9260205710 en piece jointe
Approver(s): Dufier Jacky
Status Closed
"""
    pages = _pages(first, history, history)
    assert pages[0]["type"] == "approval_form"
    assert all(page["type"] != "purchase_order" for page in pages)
    result = aggregate_page_classifications(pages)
    assert result["primary_document_type"] == "approval_form"
    assert detect_document_type(first, "pdf") == "approval_form"


def test_online_terms_link_does_not_turn_transactional_cpo_into_legal_terms():
    text = """FOURNISSEUR BOSCH
Conditions generales d'achats : www.proxi-totalenergies.fr
adresse de livraison ref cde : 06-252422
CPO 26 RUE DE MESMERRIEN 29200 BREST
Date 26/01/2026
Ref commande N offre Ref fournisseur Designation Tarif unitaire HT Quantite
06-252422 (vide) 87168101420 Soupape chauffage Geminox 14,05 4
"""
    page = classify_page(text)
    assert page["type"] == "purchase_order"
    assert page["explicit_document_title"] == "purchase_order"


@pytest.mark.parametrize("heading, expected", [
    ("Offre de prix", "quotation"),
    ("DEVIS N° D2026-12", "quotation"),
    ("FACTURE N° F2026-12", "invoice"),
    ("INVOICE 2026-12", "invoice"),
    ("BON DE LIVRAISON 2026-12", "delivery_note"),
])
def test_other_transaction_titles_are_not_orders_because_of_table_or_reference(heading, expected):
    text = heading + "\nCommande n° 660092101\n" + PRODUCT_TABLE + "\n" + QUOTE_TERMS
    page = classify_page(text)
    assert page["type"] == expected
    assert page["explicit_document_title"] == expected
    assert detect_document_type(text, "text") == expected


def test_quote_table_inherits_quote_title_without_manufacturing_order():
    pages = _pages(QUOTE_HEADER, PRODUCT_TABLE, QUOTE_TERMS)
    assert [page["type"] for page in pages[:2]] == ["quotation", "quotation"]
    assert pages[2]["type"] != "purchase_order"
    assert "document-title-context:quotation" in pages[1]["signals"]
    result = aggregate_page_classifications(pages)
    assert result["primary_document_type"] == "quotation"


def test_separately_titled_order_is_preserved_in_quote_order_bundle():
    pages = _pages(QUOTE_HEADER, PRODUCT_TABLE, ORDER_TEXT)
    assert pages[2]["type"] == "purchase_order"
    assert aggregate_page_classifications(pages)["primary_document_type"] == "purchase_order"


def test_order_with_separate_conditions_keeps_original_page_roles():
    pages = _pages(ORDER_TEXT, "Conditions generales d'achat\nResponsabilite et litiges fournisseur acquereur")
    result = aggregate_page_classifications(pages)
    assert [page["type"] for page in pages] == ["purchase_order", "legal_terms"]
    assert result["detected_document_type"] == "purchase_order_with_terms"


def test_quote_pdf_does_not_emit_a_purchase_order_and_requires_review(tmp_path):
    import fitz

    path = tmp_path / "document.pdf"
    with fitz.open() as document:
        for text in (QUOTE_HEADER, PRODUCT_TABLE, QUOTE_TERMS):
            page = document.new_page()
            page.insert_text((35, 50), text, fontsize=10)
        document.save(path)
    engine = UniversalDocumentAI()
    engine.config["addresses"]["verification"]["enabled"] = False
    engine.po_engine.config["addresses"]["verification"]["enabled"] = False
    engine.config["semantic_model"]["enabled"] = False
    engine.config["multimodal"]["enabled"] = False
    result = engine.extract(path)
    assert result.document.primary_document_type == "quotation"
    assert "purchase_order" not in result.business_extractions
    assert result.quality.requires_human_review is True

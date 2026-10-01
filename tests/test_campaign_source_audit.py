import csv
from pathlib import Path
import tempfile
import unittest

from scripts.finalize_full_audit_campaign import _field_statuses, _write_csv


class CampaignSourceAuditTests(unittest.TestCase):
    def test_empty_review_csv_overwrites_stale_rows_and_keeps_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.csv"
            _write_csv(path, [{"fichier": "ancien.pdf", "verdict": "REVUE"}])
            _write_csv(path, [], ["fichier", "verdict"])
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter=";"))
            self.assertEqual(rows, [])
            self.assertEqual(
                path.read_text(encoding="utf-8-sig").strip(),
                "fichier;verdict",
            )

    def test_approval_form_referencing_an_order_is_coherent_as_non_order(self):
        statuses, reasons = _field_statuses({
            "model": {"is_order": False, "order_number": None},
            "source_text": (
                "RB General Approval Form\n"
                "Commande en gratuit en PJ (bon de commande N° 02-9260205710)."
            ),
            "automatic_checks": {"type": "BON"},
        })
        self.assertEqual(statuses["controle_type_source"], "COHERENT_SOURCE")
        self.assertEqual(statuses["controle_numero_source"], "SANS_OBJET")
        self.assertNotIn("TYPE_A_VERIFIER", reasons)

    def test_order_cancellation_is_coherent_as_non_order(self):
        statuses, reasons = _field_statuses({
            "model": {"is_order": False, "order_number": None},
            "source_text": (
                "DEMANDE D'ANNULATION D'UNE COMMANDE\n"
                "Commande N° 940321 du 06/01/2026"
            ),
            "automatic_checks": {"type": "BON"},
        })
        self.assertEqual(statuses["controle_type_source"], "COHERENT_SOURCE")
        self.assertEqual(statuses["controle_numero_source"], "SANS_OBJET")
        self.assertNotIn("TYPE_A_VERIFIER", reasons)

    def test_reminder_for_missing_acknowledgements_is_a_non_order(self):
        statuses, reasons = _field_statuses({
            "model": {"is_order": False, "order_number": None},
            "source_text": (
                "Relance retour(s) sans AR\n"
                "Merci de nous envoyer un accusÃ© de rÃ©ception pour les bons ci-dessous.\n"
                "NÂ° commande Date commande Date prÃ©vue\n"
                "5423707 10/02/2026 11/02/2026"
            ),
            "automatic_checks": {"type": "A_VERIFIER"},
        })
        self.assertEqual(statuses["controle_type_source"], "COHERENT_SOURCE")
        self.assertEqual(statuses["controle_numero_source"], "SANS_OBJET")
        self.assertNotIn("TYPE_A_VERIFIER", reasons)

    def test_disagreeing_order_number_candidates_require_role_review(self):
        statuses, reasons = _field_statuses({
            "model": {
                "is_order": True,
                "order_number": "H7736504817",
                "review_reasons": [
                    {"code": "ORDER_NUMBER_CANDIDATES_DISAGREE", "path": "purchase_order.number"}
                ],
            },
            "source_text": (
                "COMMANDE FOURNISSEUR\nN° Document Date\nCF352124 20/03/2026\n"
                "H44606 7736504817 CHAUFFE-BAINS"
            ),
            "automatic_checks": {"type": "BON"},
        })
        self.assertEqual(statuses["controle_numero_source"], "ROLE_A_VERIFIER")
        self.assertIn("NUMERO_COMMANDE_ROLE_A_VERIFIER", reasons)

    def test_replaced_different_core_number_requires_role_review(self):
        statuses, reasons = _field_statuses({
            "model": {
                "is_order": True,
                "order_number": "124 126",
                "review_reasons": [{
                    "code": "ORDER_NUMBER_REPLACED_DIFFERENT_CORE_VALUE",
                    "path": "purchase_order.number",
                }],
            },
            "source_text": (
                "COMMANDE\nNÂ°5405637 /1877\n"
                "ELM LEBLANC 124 126 AV STALINGRAD"
            ),
            "automatic_checks": {"type": "BON"},
        })
        self.assertEqual(statuses["controle_numero_source"], "ROLE_A_VERIFIER")
        self.assertIn("NUMERO_COMMANDE_ROLE_A_VERIFIER", reasons)

    def test_request_for_price_is_source_supported_as_non_order(self):
        for title in (
            "Demande de prix DEM01105 Numéro",
            "Date : 26/01/2026 DEMANDE DE Page PRIX 1/1 N° DP4799",
        ):
            with self.subTest(title=title):
                statuses, reasons = _field_statuses({
                    "model": {"is_order": False, "order_number": None},
                    "source_text": title,
                    "automatic_checks": {"type": "BON"},
                })
                self.assertEqual(statuses["controle_type_source"], "COHERENT_SOURCE")
                self.assertEqual(statuses["controle_numero_source"], "SANS_OBJET")
                self.assertNotIn("TYPE_A_VERIFIER", reasons)

    def test_jumbled_columns_between_order_and_supplier_still_support_type(self):
        statuses, reasons = _field_statuses({
            "model": {
                "is_order": True,
                "order_number": "CF000333144",
            },
            "source_text": (
                "VERNEY SA (COMMANDE au fi In FOURNISSEUR\n"
                "N° CF 000333144 DATE 26/12/25 PAGE 1 ELM LEBLANC"
            ),
            "automatic_checks": {"type": "A_VERIFIER"},
        })
        self.assertEqual(statuses["controle_type_source"], "COHERENT_SOURCE")
        self.assertNotIn("TYPE_A_VERIFIER", reasons)

    def test_visually_spaced_order_title_supports_type(self):
        statuses, reasons = _field_statuses({
            "model": {"is_order": True, "order_number": "009-2460-220126"},
            "source_text": (
                "C O M M A N D E __________________\n"
                "N. : 009-2460-220126 22.01.2026\n"
                "LIEU DE LIVRAISON : RICHARDSON AG. DE IRIGNY"
            ),
            "automatic_checks": {"type": "A_VERIFIER"},
        })
        self.assertEqual(statuses["controle_type_source"], "COHERENT_SOURCE")
        self.assertNotIn("TYPE_A_VERIFIER", reasons)

    def test_order_terms_sentence_after_spaced_title_does_not_override_order(self):
        statuses, reasons = _field_statuses({
            "model": {"is_order": True, "order_number": "009-2460-220126"},
            "source_text": (
                "C O M M A N D E __________________\n"
                "N. : 009-2460-220126 22.01.2026\n"
                "MESSIEURS NOUS VOUS PASSONS COMMANDE DES PRODUITS INDIQUES\n"
                "CONDITIONS GENERALES D'ACHAT ET CONDITIONS PARTICULIERES"
            ),
            "automatic_checks": {"type": "A_VERIFIER"},
        })
        self.assertEqual(statuses["controle_type_source"], "COHERENT_SOURCE")
        self.assertNotIn("TYPE_A_VERIFIER", reasons)

    def test_grouped_order_numbers_are_all_source_supported(self):
        statuses, reasons = _field_statuses({
            "model": {
                "is_order": True,
                "order_number": None,
                "order_numbers": ["ST 1 1A1 42564", "ST 2 1A1 67204"],
            },
            "source_text": (
                "COMMANDE REGROUPEE\n"
                "RAPPELEZ SUR FACTURE LE N° COMMANDE : ST 1 1A1 42564\n"
                "RAPPELEZ SUR FACTURE LE N° COMMANDE : ST 2 1A1 67204"
            ),
            "automatic_checks": {"type": "BON"},
        })
        self.assertEqual(statuses["controle_type_source"], "COHERENT_SOURCE")
        self.assertEqual(
            statuses["controle_numero_source"],
            "MULTIPLES_SUPPORTES_PAR_SOURCE",
        )
        self.assertNotIn("NUMERO_COMMANDE_MANQUANT", reasons)

    def test_checksum_backed_french_vat_accepts_ocr_o_zero_confusion(self):
        statuses, reasons = _field_statuses({
            "model": {
                "is_order": True,
                "order_number": "9260202817",
                "vat_number": "FR07716320619",
            },
            "source_text": (
                "BON DE COMMANDE D'ACHAT N° 9260202817\n"
                "N° Intracom. FRO7716320619\nSIREN 716320619"
            ),
            "source_vat_candidates": ["FRO7716320619"],
            "automatic_checks": {"type": "BON"},
        })
        self.assertEqual(statuses["controle_tva_source"], "SUPPORTEE_PAR_SOURCE")
        self.assertNotIn("TVA_NON_RETROUVEE_DANS_SOURCE", reasons)

    def test_rotated_page_vat_is_supported_by_router_ocr_evidence(self):
        statuses, reasons = _field_statuses({
            "model": {
                "is_order": True,
                "order_number": "1.3961",
                "vat_number": "FR71775708373",
                "vat_evidence": {
                    "source_text": "SIRET 775 708 373 00011 - TVA FR 71 775 708 373",
                    "extraction_method": "generic_vat_regex_checksum_v1",
                },
            },
            "source_text": "1693.1 oN EDNAMMOC ED NOB",
            "automatic_checks": {"type": "BON"},
        })
        self.assertEqual(statuses["controle_tva_source"], "SUPPORTEE_PAR_PREUVE")
        self.assertNotIn("TVA_NON_RETROUVEE_DANS_SOURCE", reasons)

    def test_natural_language_sentence_is_not_a_valid_order_number(self):
        statuses, reasons = _field_statuses({
            "model": {
                "is_order": True,
                "order_number": "Reprise le 05 Janvier 2026.",
            },
            "source_text": (
                "COMMANDE FOURNISSEUR\n"
                "N° CF 15 4 000572686 DATE 10/03/26\n"
                "Reprise le 05 Janvier 2026."
            ),
            "automatic_checks": {"type": "BON"},
        })
        self.assertEqual(statuses["controle_numero_source"], "VALEUR_IMPLAUSIBLE")
        self.assertIn("NUMERO_COMMANDE_IMPLAUSIBLE", reasons)

    def test_h_correction_in_delivery_label_is_accepted(self):
        statuses, reasons = _field_statuses({
            "model": {
                "is_order": True,
                "order_number": "CF000572686",
                "delivery_address": "GERONDEAU, 2123 ROUTE NATIONALE, 45774 SARAN CEDEX",
                "delivery_components": {
                    "recipient": "GERONDEAU",
                    "street": "2123 ROUTE NATIONALE",
                    "postal_code": "45774",
                    "city": "SARAN CEDEX",
                },
            },
            "source_text": (
                "COMMANDE FOURNISSEUR CF000572686\n"
                "hdresse de livraison GERONDEAU 2123 ROUTE NATIONALE 45774 SARAN CEDEX"
            ),
            "automatic_checks": {"type": "BON"},
        })
        self.assertEqual(statuses["controle_adresse_source"], "CONTENU_ET_ROLE_SUPPORTES")
        self.assertNotIn("ROLE_ADRESSE_A_VERIFIER", reasons)

    def test_adresse_livraison_without_de_supports_delivery_role(self):
        statuses, reasons = _field_statuses({
            "model": {
                "is_order": True,
                "order_number": "PR 7 J14 52107",
                "delivery_address": "PROLIANS DP ORANGE, 585 AVENUE DE VERDUN, 84100 ORANGE",
                "delivery_components": {
                    "recipient": "PROLIANS DP ORANGE",
                    "street": "AVENUE DE VERDUN",
                    "postal_code": "84100",
                    "city": "ORANGE",
                },
            },
            "source_text": (
                "COMMANDE NÂ° PR 7 J14 52107\nAdresse Livraison:\n"
                "PROLIANS DP ORANGE\n585 AVENUE DE VERDUN\n84100 ORANGE"
            ),
            "automatic_checks": {"type": "BON"},
        })
        self.assertEqual(statuses["controle_adresse_source"], "CONTENU_ET_ROLE_SUPPORTES")
        self.assertNotIn("ROLE_ADRESSE_A_VERIFIER", reasons)

    def test_acknowledgement_instruction_does_not_turn_order_into_confirmation(self):
        statuses, _ = _field_statuses({
            "model": {
                "is_order": True,
                "order_number": "PR 0 SC 61409",
                "delivery_address": "1522 route de Periers, 50180 Agneaux",
                "delivery_components": {"postal_code": "50180", "city": "Agneaux"},
            },
            "source_text": (
                "COMMANDE N° DRANCY\n"
                "PR 0 SC 61409 93700 DRANCY\n"
                "Adresse Livraison: 1522 route de Periers 50180 Agneaux\n"
                "CONFIRMATION DE COMMANDE OU A.R A COMMUNIQUER AU DEPOT\n"
                "Ainsi qu'un ACCUSE DE RECEPTION de commande."
            ),
            "automatic_checks": {"type": "A_VERIFIER"},
        })
        self.assertEqual(statuses["controle_type_source"], "COHERENT_SOURCE")

    def test_confirmation_title_remains_non_order_evidence(self):
        statuses, _ = _field_statuses({
            "model": {"is_order": False},
            "source_text": "CONFIRMATION DE COMMANDE\nRef. commande client CF350794",
            "automatic_checks": {"type": "A_VERIFIER"},
        })
        self.assertEqual(statuses["controle_type_source"], "COHERENT_SOURCE")

    def test_purchase_terms_heading_is_coherent_as_legal_non_order(self):
        statuses, reasons = _field_statuses({
            "model": {"is_order": False, "order_number": None},
            "source_text": (
                "LES CONDITIONS GENERALES D’ACHAT\n"
                "1. COMMANDE\nToute commande implique l'acceptation des presentes conditions."
            ),
            "automatic_checks": {"type": "A_VERIFIER"},
        })
        self.assertEqual(statuses["controle_type_source"], "COHERENT_SOURCE")
        self.assertEqual(statuses["controle_numero_source"], "SANS_OBJET")
        self.assertNotIn("TYPE_A_VERIFIER", reasons)

    def test_online_terms_reference_does_not_hide_structured_cpo(self):
        statuses, reasons = _field_statuses({
            "model": {"is_order": True, "order_number": "06-252422"},
            "source_text": (
                "Conditions generales d'achats : www.proxi-totalenergies.fr\n"
                "adresse de livraison ref cde : 06-252422\n"
                "Ref commande N offre Ref fournisseur Tarif unitaire HT Quantite"
            ),
            "automatic_checks": {"type": "A_VERIFIER"},
        })
        self.assertEqual(statuses["controle_type_source"], "COHERENT_SOURCE")
        self.assertEqual(statuses["controle_numero_source"], "SUPPORTE_PAR_SOURCE")
        self.assertNotIn("TYPE_A_VERIFIER", reasons)

    def test_missing_delivery_is_only_an_error_when_source_labels_one(self):
        base = {
            "model": {"is_order": True, "order_number": "CF0900546"},
            "automatic_checks": {"type": "BON"},
        }
        statuses, reasons = _field_statuses({
            **base,
            "source_text": "COMMANDE FOURNISSEUR CF0900546\n15 AVENUE DE FONTCOUVERTE",
        })
        self.assertEqual(
            statuses["controle_adresse_source"], "ABSENTE_OU_NON_EXPLICITE_SOURCE"
        )
        self.assertNotIn("ADRESSE_LIVRAISON_MANQUANTE", reasons)

        statuses, reasons = _field_statuses({
            **base,
            "source_text": "COMMANDE FOURNISSEUR CF0900546\nAdresse de livraison\nENLEVEMENT",
            "source_contexts": {"delivery": ["Adresse de livraison ENLEVEMENT"]},
        })
        self.assertEqual(statuses["controle_adresse_source"], "NON_REQUISE_ENLEVEMENT")
        self.assertNotIn("ADRESSE_LIVRAISON_MANQUANTE", reasons)

        statuses, reasons = _field_statuses({
            **base,
            "source_text": "COMMANDE FOURNISSEUR CF0900546\nAdresse de livraison\nDEPOT TEST",
            "source_contexts": {"delivery": ["Adresse de livraison DEPOT TEST"]},
        })
        self.assertEqual(
            statuses["controle_adresse_source"], "MANQUANTE_AVEC_LIBELLE_SOURCE"
        )
        self.assertIn("ADRESSE_LIVRAISON_MANQUANTE", reasons)


if __name__ == "__main__":
    unittest.main()

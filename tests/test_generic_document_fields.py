import unittest

from jin_runtime.clean_output import build_clean_output
from jin_runtime.generic_document_fields import enrich_generic_document_fields


class GenericDocumentFieldsTests(unittest.TestCase):
    def test_rotated_native_page_uses_router_ocr_for_vat(self):
        payload = {
            "document": {"primary_document_type": "purchase_order", "page_count": 1},
            "pages": [{"page": 1, "text": "373 807 577 17 RF AVT"}],
            "weak_field_suggestions": {
                "recognized_pages": [{
                    "page": 1,
                    "text_source": "tesseract_ocr",
                    "text": "SIRET 775 708 373 00011 - TVA FR 71 775 708 373",
                }],
            },
        }

        out = enrich_generic_document_fields(payload)
        identifier = out["document_tax_identifiers"][0]
        self.assertEqual(identifier["vat_number"], "FR71775708373")
        self.assertEqual(
            identifier["evidence"]["source_text"],
            "SIRET 775 708 373 00011 - TVA FR 71 775 708 373",
        )

    def test_coherent_core_page_wins_over_noisier_second_ocr_for_vat(self):
        payload = {
            "document": {"primary_document_type": "remittance_advice", "page_count": 1},
            "pages": [{
                "page": 1,
                "text": (
                    "S.A. au capital RCS Montpellier B 775 588 692 "
                    "NAF 4672Z TVA FR 25 775 588 692 "
                    "Siret 775 588 692 00258 IBAN FR76\n"
                    "LIBELLE DATE VOTRE REF. NOTRE REF. MONTANT\n"
                    "Total Virement 154.036,48 EUR"
                ),
            }],
            "weak_field_suggestions": {
                "recognized_pages": [{
                    "page": 1,
                    "text_source": "tesseract_ocr",
                    "text": (
                        "RCE Montpellier 8775 588 602 NAF 4672Z "
                        "TVA FR 25 775 688 602 Sie 775 588 692 00258\n"
                        "LIBELLE DATE VOTRE REF. NOTRE REF. MONTANT\n"
                        "Total Virement 154.036,48 EUR"
                    ),
                }],
            },
        }

        out = enrich_generic_document_fields(payload)
        self.assertEqual(
            [item["vat_number"] for item in out["document_tax_identifiers"]],
            ["FR25775588692"],
        )
        self.assertIn(
            "RCS Montpellier B 775 588 692",
            out["document_tax_identifiers"][0]["evidence"]["source_text"],
        )

    def test_approval_form_keeps_cited_order_as_reference_only(self):
        payload = {
            "document": {"primary_document_type": "approval_form", "page_count": 1},
            "pages": [{
                "page": 1,
                "text": "RB General Approval Form\nCommande en PJ (bon de commande N° 02-9260205710).",
            }],
        }
        out = enrich_generic_document_fields(payload)
        self.assertEqual(
            out["document_references"]["referenced_order_number"]["value"],
            "02-9260205710",
        )
        self.assertNotIn("customer_order_number", out["document_references"])
        clean = build_clean_output(out)
        self.assertEqual(
            clean["document"]["references"]["referenced_order_number"]["value"],
            "02-9260205710",
        )
        self.assertNotIn("order", clean)

    def test_request_for_price_exposes_its_reference_not_a_customer_order(self):
        for title, expected in (
            ("Demande de prix DEM01105 Numéro", "DEM01105"),
            ("Date : 26/01/2026 DEMANDE DE Page PRIX 1/1 N° DP4799 A livrer", "DP4799"),
        ):
            with self.subTest(title=title):
                payload = {
                    "document": {"primary_document_type": "request_for_quotation", "page_count": 1},
                    "pages": [{"page": 1, "text": title}],
                }
                out = enrich_generic_document_fields(payload)
                self.assertNotIn("customer_order_number", out["document_references"])
                self.assertEqual(
                    out["document_references"]["request_for_quotation_number"]["value"],
                    expected,
                )
                clean = build_clean_output(out)
                self.assertEqual(
                    clean["document"]["references"]["request_for_quotation_number"]["value"],
                    expected,
                )
                self.assertNotIn("order", clean)

    def test_order_cancellation_exposes_referenced_order_only(self):
        payload = {
            "document": {"primary_document_type": "order_cancellation", "page_count": 1},
            "pages": [{
                "page": 1,
                "text": (
                    "DEMANDE D'ANNULATION D'UNE COMMANDE\n"
                    "Veuillez annuler la commande pour les lignes suivantes\n"
                    "Commande N° 940321 du 06/01/2026"
                ),
            }],
        }
        out = enrich_generic_document_fields(payload)
        self.assertEqual(
            out["document_references"]["referenced_order_number"]["value"],
            "940321",
        )
        clean = build_clean_output(out)
        self.assertEqual(clean["document"]["type"], "order_cancellation")
        self.assertEqual(
            clean["document"]["references"]["referenced_order_number"]["value"],
            "940321",
        )
        self.assertNotIn("order", clean)

    def test_non_order_exposes_customer_order_reference_and_vat_ids(self):
        payload = {
            "document": {"primary_document_type": "order_confirmation", "page_count": 1},
            "pages": [{
                "page": 1,
                "text": (
                    "CONFIRMATION DE COMMANDE\n"
                    "N de TVA FR46502794241\n"
                    "Ref. commande client CF350794\n"
                    "Client facture: WENDEL, N de TVA FR46502794241\n"
                    "e.l.m. leblanc SAS - N TVA : FR 89 542 097 944"
                ),
            }],
        }

        out = enrich_generic_document_fields(payload)
        self.assertEqual(
            out["document_references"]["customer_order_number"]["value"], "CF350794"
        )
        identifiers = out["document_tax_identifiers"]
        self.assertEqual([item["vat_number"] for item in identifiers], [
            "FR46502794241", "FR89542097944",
        ])
        self.assertEqual(identifiers[0]["role"], "buyer")
        clean = build_clean_output(out)
        self.assertEqual(
            clean["document"]["references"]["customer_order_number"]["value"],
            "CF350794",
        )
        self.assertEqual(len(clean["document"]["tax_identifiers"]), 2)
        self.assertNotIn("order", clean)


if __name__ == "__main__":
    unittest.main()

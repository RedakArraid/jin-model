import unittest

from jin_runtime.clean_output import build_clean_output
from jin_runtime.generic_document_fields import enrich_generic_document_fields


class GenericDocumentFieldsTests(unittest.TestCase):
    def test_valid_siret_derives_vat_with_explicit_provenance(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"page": 1, "text": "Acheteur ACME - SIRET 775 708 373 00011"}],
            "business_extractions": {"purchase_order": {
                "purchase_order": {}, "buyer": {}, "supplier": {}, "lines": [],
            }},
        }

        out = enrich_generic_document_fields(payload)
        identifier = out["document_tax_identifiers"][0]
        self.assertEqual(identifier["vat_number"], "FR71775708373")
        self.assertEqual(identifier["siret"], "77570837300011")
        self.assertEqual(identifier["validation_status"], "DERIVED_FROM_VALID_SIRET")
        self.assertEqual(identifier["confidence"], 0.99)
        self.assertTrue(identifier["derived"])
        self.assertEqual(
            identifier["calculation"]["algorithm"],
            "french_vat_key_from_siren_v1",
        )
        self.assertIn("derived", identifier["warnings"][0])
        buyer = out["business_extractions"]["purchase_order"]["buyer"]
        self.assertEqual(buyer["vat_number"], "FR71775708373")
        self.assertEqual(buyer["company_registration_number"], "77570837300011")
        clean_buyer = build_clean_output(out)["order"]["parties"]["buyer"]
        self.assertEqual(clean_buyer["siren"], "775708373")
        self.assertEqual(clean_buyer["siret"], "77570837300011")

    def test_structured_checksum_valid_siret_derives_vat_when_text_stream_missed_it(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"page": 1, "text": "BON DE COMMANDE"}],
            "business_extractions": {"purchase_order_with_terms": {
                "buyer": {"company_registration_number": "352 747 018 00013"},
                "supplier": {},
                "lines": [],
            }},
        }

        out = enrich_generic_document_fields(payload)

        identifier = out["document_tax_identifiers"][0]
        self.assertEqual(identifier["vat_number"], "FR69352747018")
        self.assertEqual(identifier["role"], "buyer")
        self.assertEqual(identifier["confidence"], 0.98)
        self.assertEqual(identifier["validation_status"], "DERIVED_FROM_VALID_SIRET")
        self.assertEqual(
            identifier["evidence"]["extraction_method"],
            "vat_derived_from_valid_french_registration_v1",
        )
        order = out["business_extractions"]["purchase_order_with_terms"]
        self.assertEqual(order["buyer"]["vat_number"], "FR69352747018")
        self.assertEqual(order["tax_identifiers"][0]["vat_number"], "FR69352747018")

    def test_structured_registration_with_invalid_checksum_does_not_derive_vat(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"company_registration_number": "35274701800014"},
                "supplier": {},
                "lines": [],
            }},
        }

        out = enrich_generic_document_fields(payload)

        self.assertNotIn("document_tax_identifiers", out)

    def test_checksum_valid_siren_alone_derives_vat(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"page": 1, "text": "Acheteur ACME - SIREN 444 768 550"}],
        }

        out = enrich_generic_document_fields(payload)

        identifier = out["document_tax_identifiers"][0]
        self.assertEqual(identifier["vat_number"], "FR46444768550")
        self.assertEqual(identifier["siren"], "444768550")
        self.assertIsNone(identifier["siret"])
        self.assertEqual(identifier["validation_status"], "DERIVED_FROM_VALID_SIREN")

    def test_valid_derived_vat_precedes_malformed_label_supported_ocr_value(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{
                "page": 1,
                "text": (
                    "No Intra Communautaire : FR0K352747018\n"
                    "SIRET : 352 747 018 00013"
                ),
            }],
            "business_extractions": {"purchase_order": {
                "buyer": {}, "supplier": {}, "lines": [],
            }},
        }

        out = enrich_generic_document_fields(payload)

        identifiers = out["document_tax_identifiers"]
        self.assertEqual(
            [identifier["vat_number"] for identifier in identifiers],
            ["FR69352747018", "FR0K352747018"],
        )
        self.assertEqual(identifiers[0]["validation_status"], "DERIVED_FROM_VALID_SIRET")
        self.assertEqual(
            out["business_extractions"]["purchase_order"]["tax_identifiers"][0]["vat_number"],
            "FR69352747018",
        )

    def test_customer_legal_footer_matches_bill_to_and_assigns_buyer_tax_role(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{
                "page": 1,
                "text": (
                    "NET A PAYER 915,65 EUR\n"
                    "Siège Social : SAS SISCA au capital - Siret : 35274701800013\n"
                    "INFOS FOURNISSEUR\nELM LEBLANC"
                ),
            }],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "SIDV AURILLAC"},
                "bill_to": {"name": "SISCA", "legal_name": "SISCA"},
                "supplier": {"name": "ELM LEBLANC"},
                "lines": [],
            }},
        }

        out = enrich_generic_document_fields(payload)

        identifier = out["document_tax_identifiers"][0]
        self.assertEqual(identifier["role"], "buyer")
        self.assertEqual(identifier["party_role"], "bill_to")
        self.assertEqual(identifier["role_confidence"], 0.99)
        self.assertEqual(
            identifier["role_assignment"]["method"],
            "party_name_context_match_v1",
        )
        order = out["business_extractions"]["purchase_order"]
        self.assertEqual(order["bill_to"]["vat_number"], "FR69352747018")
        self.assertNotIn("vat_number", order["buyer"])

    def test_following_supplier_heading_does_not_steal_customer_siret(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{
                "page": 1,
                "text": (
                    "Email : contact-morlaix@iziconfort.fr\n"
                    "Siret : 444 768 550 00485\n"
                    "INFOS FOURNISSEUR\nCOMMANDE Bosch pièces de rechange"
                ),
            }],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "IZI by EDF"},
                "supplier": {"name": "ELM LEBLANC"},
                "lines": [],
            }},
        }

        out = enrich_generic_document_fields(payload)

        identifier = out["document_tax_identifiers"][0]
        self.assertEqual(identifier["role"], "buyer")
        self.assertEqual(identifier["party_role"], "buyer")
        self.assertEqual(identifier["role_confidence"], 0.90)
        self.assertEqual(
            identifier["role_assignment"]["method"],
            "purchase_order_issuer_default_v1",
        )
        self.assertIn(
            "vat_role_inferred_as_customer_purchase_order_issuer",
            identifier["warnings"],
        )

    def test_explicit_supplier_identity_overrides_purchase_order_buyer_default(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{
                "page": 1,
                "text": "FOURNISSEUR ELM LEBLANC\nSIREN 542 097 944",
            }],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "CLIENT TEST"},
                "supplier": {"name": "ELM LEBLANC"},
                "lines": [],
            }},
        }

        out = enrich_generic_document_fields(payload)

        identifier = out["document_tax_identifiers"][0]
        self.assertEqual(identifier["vat_number"], "FR89542097944")
        self.assertEqual(identifier["role"], "supplier")
        self.assertEqual(identifier["party_role"], "supplier")
        self.assertEqual(
            identifier["role_assignment"]["method"],
            "preceding_role_heading_v1",
        )
        self.assertEqual(
            out["business_extractions"]["purchase_order"]["supplier"]["vat_number"],
            "FR89542097944",
        )

    def test_customer_identity_beats_interleaved_supplier_words_in_legal_footer(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{
                "page": 1,
                "text": (
                    "REXEL BON France DE - COMMANDE 13 Boulevard du Fort de Vaux "
                    "N SIREN FOURNISSEUR : 309 304 616 "
                    "N TVA IC FOURNISSEUR : FR 26 309 304 616"
                ),
            }],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "REXEL FRANCE"},
                "supplier": {
                    "name": "BOSCH THERMOTECHNOLOGIE",
                    "vat_number": "FR26309304616",
                },
                "lines": [],
            }},
        }

        out = enrich_generic_document_fields(payload)

        identifier = out["document_tax_identifiers"][0]
        self.assertEqual(identifier["vat_number"], "FR26309304616")
        self.assertEqual(identifier["role"], "buyer")
        self.assertEqual(identifier["party_role"], "buyer")
        self.assertEqual(
            identifier["role_assignment"]["method"],
            "party_name_context_match_v1",
        )
        order = out["business_extractions"]["purchase_order"]
        self.assertEqual(order["buyer"]["vat_number"], "FR26309304616")
        self.assertNotIn("vat_number", order["supplier"])

    def test_unqualified_registration_on_non_order_keeps_unknown_role(self):
        payload = {
            "document": {"primary_document_type": "remittance_advice"},
            "pages": [{"page": 1, "text": "SIREN 444 768 550"}],
        }

        out = enrich_generic_document_fields(payload)

        identifier = out["document_tax_identifiers"][0]
        self.assertEqual(identifier["role"], "unknown")
        self.assertEqual(identifier["role_confidence"], 0.0)
        self.assertEqual(identifier["role_assignment"]["method"], "unresolved")

    def test_unlabelled_supplier_party_collision_still_defaults_to_customer_issuer(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{
                "page": 1,
                "text": (
                    "Merci de confirmer à achat@eau-vapeur.fr\n"
                    "Siège Social : SAS au capital - Siret : 67201002200032"
                ),
            }],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "Eau et Vapeur LYON"},
                "ship_to": {"name": "Eau et Vapeur LYON"},
                # Dense layouts have historically duplicated the customer here.
                "supplier": {"name": "Eau Et Vapeur"},
                "lines": [],
            }},
        }

        out = enrich_generic_document_fields(payload)

        identifier = out["document_tax_identifiers"][0]
        self.assertEqual(identifier["vat_number"], "FR23672010022")
        self.assertEqual(identifier["role"], "buyer")
        self.assertEqual(identifier["role_assignment"]["method"], "purchase_order_issuer_default_v1")

    def test_printed_vat_is_corroborated_by_matching_siren_without_duplication(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{
                "page": 1,
                "text": "Fournisseur ACME SIREN 775 708 373 TVA FR 71 775 708 373",
            }],
        }

        out = enrich_generic_document_fields(payload)
        identifiers = out["document_tax_identifiers"]
        self.assertEqual(len(identifiers), 1)
        self.assertEqual(identifiers[0]["validation_status"], "CHECKSUM_AND_REGISTRATION_MATCH")
        self.assertEqual(identifiers[0]["siren"], "775708373")

    def test_invalid_registration_checksum_does_not_create_vat(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"page": 1, "text": "SIREN 123 456 789"}],
        }

        out = enrich_generic_document_fields(payload)

        self.assertNotIn("document_tax_identifiers", out)

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

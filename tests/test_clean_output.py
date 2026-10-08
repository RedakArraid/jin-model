import unittest

from jin_runtime.clean_output import build_clean_output, clean_output_schema
from jin_runtime.delivery_addresses import enrich_delivery_addresses


class CleanOutputTests(unittest.TestCase):
    def test_contract_is_compact_grouped_and_keeps_zeroes(self):
        payload = {
            "document": {"primary_document_type": "purchase_order", "page_count": 1},
            "business_extractions": {"purchase_order": {
                "purchase_order": {
                    "number": {"value": "CF0012", "final_confidence": .9,
                               "evidence": {"page": 1, "bbox": [1, 2, 3, 4],
                                            "source_text": "Commande CF0012"}},
                    "customer_reference": {"value": "CLIENT-7"},
                    "customer_agency_code": {"value": "LY07", "final_confidence": .99},
                },
                "buyer": {"name": "CLIENT", "contact": {"email": "buyer@example.test"}},
                "supplier": {"name": "SUPPLIER"},
                "business_addresses": [{
                    "address_id": "a1", "role": "ship_to", "party_name": "DEPOT",
                    "address": {"house_number": "2", "street_name": "Diderot",
                                "postal_code": "06003", "city": "NICE"},
                    "ban_verification": {"status": "EXACT_MATCH", "provider": "BAN"},
                }],
                "lines": [{"line_number": 1, "material_number": "0007", "quantity": 0,
                           "net_unit_price": 0, "line_total": 0, "description": "Echantillon"}],
                "totals": {"total_net": 0, "currency": "EUR"},
            }},
            "extraction_decision": {"status": "CHECKS_PASSED", "requires_review": False},
        }
        out = build_clean_output(payload, source_filename="commande.pdf")
        self.assertEqual(out["schema_version"], "jin-clean-extraction-v1")
        self.assertEqual(out["document"]["filename"], "commande.pdf")
        self.assertEqual(out["order"]["customer_order_number"]["value"], "CF0012")
        self.assertEqual(out["order"]["customer_reference"]["value"], "CLIENT-7")
        self.assertEqual(out["order"]["customer_agency_code"]["value"], "LY07")
        self.assertEqual(out["order"]["line_items"][0]["references"]["material_number"], "0007")
        self.assertEqual(out["order"]["line_items"][0]["quantity"], 0)
        self.assertEqual(out["order"]["line_items"][0]["pricing"]["net_unit_price"], 0)
        self.assertEqual(out["order"]["totals"]["total_net"], 0)
        self.assertEqual(out["order"]["addresses"][0]["id"], "a1:ship_to")
        self.assertEqual(out["order"]["delivery_address"]["role"], "ship_to")
        self.assertNotIn("raw_text", out)

    def test_reused_physical_address_has_unique_role_specific_ids(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "business_addresses": [
                    {"address_id": "same", "role": "buyer", "formatted_address": "1 rue Test"},
                    {"address_id": "same", "role": "bill_to", "formatted_address": "1 rue Test"},
                ],
            }},
        }
        addresses = build_clean_output(payload)["order"]["addresses"]
        self.assertEqual([item["id"] for item in addresses], ["same:buyer", "same:bill_to"])
        self.assertEqual(len({item["id"] for item in addresses}), 2)

    def test_metadata_only_fields_are_omitted_and_do_not_mask_fallbacks(self):
        metadata_only = {
            "ocr_confidence": 0.0,
            "semantic_confidence": 0.0,
            "validation_confidence": 1.0,
            "final_confidence": 0.0,
            "validation_status": "NOT_CHECKED",
        }
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "purchase_order": {
                    "number": metadata_only,
                    "order_number": {"value": "PO-FALLBACK"},
                    "customer_reference": metadata_only,
                    "expected_delivery_date": metadata_only,
                    "required_date": {"value": "2026-10-08"},
                    "customer_agency_code": metadata_only,
                },
                "customer_agency_code": {"value": "AG02"},
                "lines": [],
            }},
        }

        order = build_clean_output(payload)["order"]

        self.assertEqual(order["customer_order_number"]["value"], "PO-FALLBACK")
        self.assertNotIn("customer_reference", order)
        self.assertEqual(order["requested_delivery_date"]["value"], "2026-10-08")
        self.assertEqual(order["customer_agency_code"]["value"], "AG02")

    def test_clean_delivery_label_is_used_without_losing_source_value(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {"business_addresses": [{
                "address_id": "delivery", "role": "ship_to",
                "formatted_address": "2 rue Diderot 06003 Nice CEDEX 1 Nice",
                "address": {"house_number": "2", "street": "rue Diderot",
                            "postal_code": "06003", "city": "Nice CEDEX 1"},
            }]}}
        }
        enrich_delivery_addresses(payload)
        delivery = build_clean_output(payload)["order"]["delivery_address"]
        self.assertEqual(delivery["formatted_lines"], ["2 RUE DIDEROT", "06003 NICE CEDEX 1"])
        self.assertEqual(delivery["source_formatted"], "2 rue Diderot 06003 Nice CEDEX 1 Nice")

    def test_non_order_has_no_empty_order_shell(self):
        out = build_clean_output({"document": {"primary_document_type": "quotation"},
                                  "extraction_decision": {"status": "REVIEW_REQUIRED"}})
        self.assertNotIn("order", out)
        self.assertEqual(out["document"]["type"], "quotation")

    def test_party_view_removes_order_number_and_duplicate_ocr_noise(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"page": 1, "text": "BON DE COMMANDE D'ACHAT"}],
            "business_extractions": {"purchase_order": {
                "buyer": {
                    "name": "CCL CASTRES DEPOT",
                    "legal_name": "N° 01 - 9251107284",
                    "phone": "0567466134",
                    "contact": {
                        "name": "MARTINEZ Tony",
                        "phone": "0820003000",
                        "email": "tony-martinez@ccl.fr",
                    },
                },
                "supplier": {"name": "ELM LEBLANC"},
                "ship_to": {"name": "COMPTOIR COMMERCIAL DU LANGUEDOC"},
                "business_addresses": [{
                    "address_id": "delivery",
                    "role": "ship_to",
                    "party_name": "C.C.L. MAZAMET",
                    "clean_address": {
                        "one_line": "C.C.L. MAZAMET, 1 RUE ARMAND PUECH, 81200 MAZAMET",
                        "components": {"recipient": "C.C.L. MAZAMET", "postal_code": "81200", "city": "Mazamet"},
                    },
                }],
            }},
        }
        parties = build_clean_output(payload)["order"]["parties"]
        self.assertEqual(parties["buyer"]["name"], "CCL CASTRES DEPOT")
        self.assertNotIn("legal_name", parties["buyer"])
        self.assertEqual(parties["buyer"]["contact"]["phone"], "0567466134")
        self.assertEqual(parties["ship_to"]["name"], "C.C.L. MAZAMET")

    def test_party_view_uses_role_fallbacks_instead_of_polluted_names(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"page": 1, "text": "SALICA COMMANDE"}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "SAS", "legal_name": "SAS"},
                "supplier": {"name": "ELM LEBLANC"},
                "bill_to": {"name": "SALICA", "legal_name": "SALICA"},
                "ship_to": {"name": "205, Av Général Pruneau"},
                "business_addresses": [{
                    "address_id": "delivery",
                    "role": "ship_to",
                    "party_name": "205, Av Général Pruneau",
                    "department": "ANCONETTI / Dépôt ANC. TOULON",
                    "clean_address": {
                        "one_line": "205 AV GÉNÉRAL PRUNEAU, 83000 TOULON",
                        "components": {
                            "recipient": "205, Av Général Pruneau",
                            "department": "ANCONETTI / Dépôt ANC. TOULON",
                            "postal_code": "83000",
                            "city": "Toulon",
                        },
                    },
                }],
            }},
        }
        parties = build_clean_output(payload)["order"]["parties"]
        self.assertEqual(parties["buyer"]["name"], "SALICA")
        self.assertEqual(parties["ship_to"]["name"], "ANCONETTI / Dépôt ANC. TOULON")

    def test_party_view_cleans_ppc_doubled_tokens_and_boilerplate(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {
                    "name": "SENS SENS",
                    "contact": {
                        "name": "PERSEVAL PERSEVAL Sylvain Sylvain FRANCE FRANCE",
                        "phone": "03 03 58 58 92 92 90 90 26 26",
                    },
                },
                "supplier": {"name": "ELM ELM LEBLANC LEBLANC"},
                "ship_to": {"name": "PARTEDIS PARTEDIS SAS SAS au au capital capital RCS RCS"},
                "business_addresses": [{
                    "address_id": "delivery",
                    "role": "ship_to",
                    "party_name": "PPC",
                    "clean_address": {
                        "one_line": "PPC, 7 LES BAS MUSATS, 89100 MALAY LE GRAND",
                        "components": {"recipient": "PPC", "postal_code": "89100", "city": "Malay-le-Grand"},
                    },
                }],
            }},
        }
        parties = build_clean_output(payload)["order"]["parties"]
        self.assertEqual(parties["buyer"]["name"], "PPC SENS")
        self.assertEqual(parties["buyer"]["contact"]["name"], "PERSEVAL Sylvain")
        self.assertEqual(parties["buyer"]["contact"]["phone"], "03 58 92 90 26")
        self.assertEqual(parties["ship_to"]["name"], "PPC")

    def test_party_view_rejects_prose_department_and_identifier_as_fax(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "BIO HABITAT"},
                "supplier": {
                    "name": "ELM LEBLANC",
                    "fax": "86511239915",
                },
                "ship_to": {
                    "name": "PPC",
                    "department": (
                        "de son catalogue ou de son plan de vente "
                        "dans les 6 prochains mois."
                    ),
                },
            }},
        }
        parties = build_clean_output(payload)["order"]["parties"]
        self.assertNotIn("fax", parties["supplier"].get("contact", {}))
        self.assertNotIn("department", parties["ship_to"])
        self.assertNotIn("department", parties["ship_to"].get("contact", {}))

    def test_party_view_uses_bill_to_contact_when_buyer_role_was_swapped(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {
                    "name": "ELM",
                    "contact": {"email": "bosch.elmleblanc@fr.bosch.com"},
                },
                "supplier": {"name": "ELM LEBLANC"},
                "bill_to": {
                    "name": "SALICA",
                    "contact": {"email": "srvlad@ciffreobona.fr"},
                },
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "SALICA")
        self.assertEqual(buyer["contact"]["email"], "srvlad@ciffreobona.fr")

    def test_party_view_recognises_short_elm_supplier_alias(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "E.L.M.", "contact": {"name": "Richard DINTZNER"}},
                "supplier": {"name": "E.L.M. LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery",
                    "role": "ship_to",
                    "party_name": "E.L.M.",
                    "department": "Salon Chauffage Sanitaire",
                    "clean_address": {
                        "one_line": "SALON CHAUFFAGE SANITAIRE, 68110 ILLZACH",
                        "components": {
                            "recipient": "E.L.M.",
                            "department": "Salon Chauffage Sanitaire",
                            "postal_code": "68110",
                            "city": "ILLZACH",
                        },
                    },
                }],
            }},
        }
        parties = build_clean_output(payload)["order"]["parties"]
        self.assertEqual(parties["buyer"]["name"], "Salon Chauffage Sanitaire")
        self.assertEqual(parties["buyer"]["contact"]["name"], "Richard DINTZNER")
        self.assertEqual(parties["ship_to"]["name"], "Salon Chauffage Sanitaire")

    def test_delivery_label_follows_proven_ship_to_instead_of_supplier(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "E.L.M."},
                "supplier": {"name": "E.L.M. LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery",
                    "role": "ship_to",
                    "party_name": "E.L.M.",
                    "department": "Salon Chauffage Sanitaire",
                    "clean_address": {
                        "one_line": "E.L.M., SALON CHAUFFAGE SANITAIRE, 68110 ILLZACH",
                        "lines": ["E.L.M.", "SALON CHAUFFAGE SANITAIRE", "68110 ILLZACH"],
                        "components": {
                            "recipient": "E.L.M.",
                            "department": "Salon Chauffage Sanitaire",
                            "postal_code": "68110",
                            "city": "ILLZACH",
                        },
                    },
                }],
            }},
        }
        delivery = build_clean_output(payload)["order"]["delivery_address"]
        self.assertEqual(delivery["party_name"], "Salon Chauffage Sanitaire")
        self.assertEqual(
            delivery["formatted_lines"],
            ["SALON CHAUFFAGE SANITAIRE", "68110 ILLZACH"],
        )
        self.assertNotIn("department", delivery["components"])

    def test_polluted_numeric_buyer_header_uses_source_alias(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": "PIECES XPRESS Bon de commande"}],
            "business_extractions": {"purchase_order": {
                "buyer": {
                    "name": "002587 Bon n 813255 Ref : LOG GARDIEN VESINET",
                    "contact": {"phone": "0 820 00 3000"},
                },
                "supplier": {"name": "ELM LEBLANC BOSCH"},
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "PIECES XPRESS")
        self.assertNotIn("contact", buyer)

    def test_party_address_removes_adjacent_ocr_duplicates(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "supplier": {
                    "name": "ELM LEBLANC",
                    "address": {
                        "postal_code": "93711",
                        "city": "93711 DRANCY DRANCY CEDEX CEDEX",
                    },
                },
            }},
        }
        address = build_clean_output(payload)["order"]["parties"]["supplier"]["address"]
        self.assertEqual(address["city"], "DRANCY CEDEX")

    def test_supplier_order_header_recovers_parties_and_contact(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "ANDRETY DIGNE COMMANDE FOURNISSEUR ELM LEBLANC SAS\n"
                "Acheteur : Veronique BOYER 2 | |"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {
                    "name": "04000 DIGNE N Dossier 0895939 93700 DRANCY",
                    "contact": {"name": "Veronique BOYER 2 | |"},
                },
                "supplier": {"name": "PX Unit. Montant"},
            }},
        }
        parties = build_clean_output(payload)["order"]["parties"]
        self.assertEqual(parties["buyer"]["name"], "ANDRETY DIGNE")
        self.assertEqual(parties["buyer"]["contact"]["name"], "Veronique BOYER")
        self.assertEqual(parties["supplier"]["name"], "ELM LEBLANC SAS")

    def test_dotted_person_is_contact_and_delivery_company_is_buyer(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": "GERONDEAU COMMANDE FOURNISSEUR"}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "....... FLORIAN LOBROT"},
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "GERONDEAU",
                    "clean_address": {
                        "one_line": "GERONDEAU, 45774 SARAN",
                        "components": {"recipient": "GERONDEAU", "postal_code": "45774", "city": "SARAN"},
                    },
                }],
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "GERONDEAU")
        self.assertEqual(buyer["contact"]["name"], "FLORIAN LOBROT")

    def test_order_number_party_falls_back_to_delivery_company(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "COMMANDE N 5516418 /1877 ELM LEBLANC\n"
                "Email : Carole.PONSERRE@groupeppc.fr\n"
                "Contact : PONSERRE Carole"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "N 5516418 /1877"},
                "supplier": {"name": "COMMANDE MAIL PIECE DETACHEE"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "PPC-LILLE", "contact_phone": "03 28 04 05 80",
                    "clean_address": {
                        "one_line": "PPC-LILLE, 59118 WAMBRECHIES",
                        "components": {"recipient": "PPC-LILLE", "postal_code": "59118", "city": "WAMBRECHIES"},
                    },
                }],
            }},
        }
        parties = build_clean_output(payload)["order"]["parties"]
        self.assertEqual(parties["buyer"]["name"], "PPC-LILLE")
        self.assertEqual(parties["buyer"]["contact"]["name"], "PONSERRE Carole")
        self.assertEqual(
            parties["buyer"]["contact"]["email"],
            "Carole.PONSERRE@groupeppc.fr",
        )
        self.assertEqual(parties["buyer"]["contact"]["phone"], "03 28 04 05 80")
        self.assertEqual(parties["supplier"]["name"], "ELM LEBLANC")

    def test_buyer_phone_shared_with_supplier_uses_role_nested_contact(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {
                    "name": "SIDV AUCH Nord",
                    "phone": "08 20 00 30 00",
                    "contact": {"phone": "05 62 63 36 81"},
                },
                "supplier": {"name": "ELM LEBLANC", "phone": "08 20 00 30 00"},
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["contact"]["phone"], "05 62 63 36 81")

    def test_truncated_national_phone_is_not_published(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "CLIENT", "phone": "04 90 13 44"},
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertNotIn("contact", buyer)

    def test_explicit_buyer_contact_replaces_line_item_pollution(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "Acheteur : LAMIRAL GEHANT CATHERINE - 03 81 61 68 52 - "
                "c.gehant@vfconfort.fr"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {
                    "name": "DEEE 0.14",
                    "legal_name": "VFConfort",
                    "contact": {"name": "Contribution REP"},
                },
                "supplier": {"name": "ELM LEBLANC"},
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        contact = buyer["contact"]
        self.assertNotIn("name", buyer)
        self.assertEqual(contact["name"], "LAMIRAL GEHANT CATHERINE")
        self.assertEqual(contact["phone"], "03 81 61 68 52")
        self.assertEqual(contact["email"], "c.gehant@vfconfort.fr")

    def test_two_column_buyer_supplier_name_is_split(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "Agence ALFORTVILLE CEDEO ELM LEBLANC PIECES DETACHEES\n"
                "Contact: Eric Rafidinarivo ERIC.RAFIDINARIVO@CEDEO.FR"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {
                    "name": "Agence ALFORTVILLE CEDEO ELM LEBLANC PIECES DETACHEES (21015305)",
                },
                "supplier": {"name": "N° 4121337769 / 3186 1 / 1"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "formatted_address": "19 Quai de la Revolution, 94140 Alfortville",
                    "clean_address": {
                        "one_line": "19 QUAI DE LA REVOLUTION, 94140 ALFORTVILLE",
                        "lines": ["19 QUAI DE LA REVOLUTION", "94140 ALFORTVILLE"],
                        "components": {
                            "street": "19 Quai de la Revolution",
                            "postal_code": "94140", "city": "Alfortville",
                        },
                    },
                }],
            }},
        }
        parties = build_clean_output(payload)["order"]["parties"]
        self.assertEqual(parties["supplier"]["name"], "ELM LEBLANC")
        self.assertEqual(parties["buyer"]["name"], "Agence ALFORTVILLE CEDEO")
        self.assertEqual(parties["buyer"]["contact"]["name"], "Eric Rafidinarivo")
        self.assertEqual(
            parties["buyer"]["contact"]["email"],
            "ERIC.RAFIDINARIVO@CEDEO.FR",
        )
        self.assertEqual(parties["ship_to"]["name"], "Agence ALFORTVILLE CEDEO")
        delivery = build_clean_output(payload)["order"]["delivery_address"]
        self.assertEqual(delivery["party_name"], "Agence ALFORTVILLE CEDEO")
        self.assertEqual(
            delivery["formatted_lines"],
            [
                "AGENCE ALFORTVILLE CEDEO",
                "19 QUAI DE LA REVOLUTION",
                "94140 ALFORTVILLE",
            ],
        )

    def test_coordinate_polluted_buyer_uses_clean_delivery_site(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": "PROLIANS BAURES"}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "+9 PROLIANS Z.A.D. PROLIANS de Raujolles BAURES"},
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "PROLIANS BA MILLAU",
                    "clean_address": {
                        "one_line": "PROLIANS BA MILLAU, 12100 CREISSELS",
                        "lines": ["PROLIANS BA MILLAU", "12100 CREISSELS"],
                        "components": {
                            "recipient": "PROLIANS BA MILLAU",
                            "postal_code": "12100", "city": "CREISSELS",
                        },
                    },
                }],
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "PROLIANS BA MILLAU")

    def test_delivery_recipient_wins_over_order_heading_pollution(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": "PROLIANS BAURES\nCOMMANDE N°\nAdresse Livraison"}],
            "business_extractions": {"purchase_order": {
                "number": {"value": "PR A 1A4 63160"},
                "buyer": {"name": "+9 PROLIANS Z.A.D. PROLIANS de Raujolles BAURES"},
                "supplier": {"name": "ELM LEBLANC"},
                "bill_to": {
                    "name": "PR A 1A4 63160",
                    "legal_name": "COMMANDE N°",
                },
                "ship_to": {"name": "BA MILLAU", "legal_name": "BA MILLAU"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "COMMANDE N°",
                    "clean_address": {
                        "one_line": "PROLIANS BA MILLAU, 12100 CREISSELS",
                        "lines": ["PROLIANS BA MILLAU", "12100 CREISSELS"],
                        "components": {
                            "recipient": "PROLIANS BA MILLAU",
                            "postal_code": "12100", "city": "CREISSELS",
                        },
                    },
                }],
            }},
        }
        parties = build_clean_output(payload)["order"]["parties"]
        self.assertEqual(parties["buyer"]["name"], "PROLIANS BA MILLAU")
        self.assertEqual(parties["ship_to"]["name"], "PROLIANS BA MILLAU")
        self.assertNotIn("legal_name", parties["ship_to"])
        self.assertNotIn("bill_to", parties)

    def test_country_role_noise_promotes_delivery_company_and_site_city(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": "BOURG EN BRESSE\nPPC\nA livrer à l'adresse ci-dessous"}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "BOURG EN BRESSE", "legal_name": "BOURG EN BRESSE"},
                "supplier": {"name": "BOSCH P.D. DOMESTIQUES"},
                "ship_to": {"name": "FRANCE", "legal_name": "A livrer à l'adresse ci-dessous :"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "FRANCE", "department": "PPC",
                    "clean_address": {
                        "one_line": "PPC, 26 AVENUE ARSENE D'ARSONVAL, 01000 BOURG-EN-BRESSE, FRANCE",
                        "lines": ["PPC", "26 AVENUE ARSENE D'ARSONVAL", "01000 BOURG-EN-BRESSE", "FRANCE"],
                        "components": {
                            "department": "PPC", "postal_code": "01000",
                            "city": "Bourg-en-Bresse", "country": "France",
                        },
                    },
                }],
            }},
        }
        order = build_clean_output(payload)["order"]
        self.assertEqual(order["parties"]["buyer"]["name"], "PPC BOURG EN BRESSE")
        self.assertEqual(order["parties"]["ship_to"]["name"], "PPC")
        self.assertEqual(order["delivery_address"]["party_name"], "PPC")

    def test_total_heading_buyer_falls_back_to_bill_to_company(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": "SAS ELM LEBLANC\nAdresse de facturation impérative : Eau et Vapeur"}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "POIDS TOTAL NET H.T.", "contact": {"name": "retour à"}},
                "bill_to": {
                    "name": "Eau et Vapeur",
                    "address": {"postal_code": "93200", "city": "Saint Denis"},
                    "contact": {"email": "livraison@eau-vapeur.fr"},
                },
                "supplier": {"name": "de livraison souhaitée : 18/02/2026"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "IGEO BREST",
                    "clean_address": {
                        "one_line": "IGEO BREST, 29490 GUIPAVAS",
                        "lines": ["IGEO BREST", "29490 GUIPAVAS"],
                        "components": {"recipient": "IGEO BREST", "postal_code": "29490", "city": "Guipavas"},
                    },
                }],
            }},
        }
        parties = build_clean_output(payload)["order"]["parties"]
        self.assertEqual(parties["buyer"]["name"], "Eau et Vapeur")
        self.assertEqual(parties["buyer"]["contact"]["email"], "livraison@eau-vapeur.fr")
        self.assertEqual(parties["buyer"]["address"]["postal_code"], "93200")
        self.assertEqual(parties["supplier"]["name"], "ELM LEBLANC")

    def test_commanded_by_person_and_delivery_company_are_unswapped(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": "Commandé par :\nTADDEI XAVIER\nPLATEFORME LOGISTIQUE CCL"}],
            "business_extractions": {"purchase_order": {
                "buyer": {
                    "name": "TADDEI XAVIER",
                    "contact": {"name": "PLATEFORME LOGISTIQUE CCL"},
                    "email": "xavier-taddei@ccl.fr",
                },
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "PLATEFORME LOGISTIQUE",
                    "clean_address": {
                        "one_line": "PLATEFORME LOGISTIQUE, 31770 COLOMIERS",
                        "lines": ["PLATEFORME LOGISTIQUE", "31770 COLOMIERS"],
                        "components": {"recipient": "PLATEFORME LOGISTIQUE", "postal_code": "31770", "city": "Colomiers"},
                    },
                }],
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "PLATEFORME LOGISTIQUE CCL")
        self.assertEqual(buyer["contact"]["name"], "TADDEI XAVIER")

    def test_commanded_by_two_line_site_swaps_person_and_organisation(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "Commandé par : 124-126 RUE DE STALINGRAD\n"
                "ESQUIROL PATRICK\n"
                "CCL COLOMIERS ARCHE\n"
                "A Livrer à : CCL SAINT-ALBAN"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {
                    "name": "ESQUIROL PATRICK",
                    "contact": {"name": "CCL COLOMIERS ARCHE"},
                },
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "CCL SAINT-ALBAN",
                    "clean_address": {
                        "one_line": "CCL SAINT-ALBAN, 31140 SAINT-ALBAN",
                        "lines": ["CCL SAINT-ALBAN", "31140 SAINT-ALBAN"],
                        "components": {
                            "recipient": "CCL SAINT-ALBAN",
                            "postal_code": "31140", "city": "SAINT-ALBAN",
                        },
                    },
                }],
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "CCL COLOMIERS ARCHE")
        self.assertEqual(buyer["contact"]["name"], "ESQUIROL PATRICK")

    def test_role_swapped_billing_company_and_table_contact_are_recovered(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "SAS ELM LEBLANC\n"
                "Eau et Vapeur NANCY GROUPE BOSCH\n"
                "Adresse de facturation impérative :\n"
                "EAU ET VAPEUR\nLA MANUFACTURE\n"
                "Date Pièce Fourn. Notre référence Commandé par Page\n"
                "02/04/2026 219376 F503 EL OUARDI Kadija 1 / 1"
            )}],
            "business_extractions": {"purchase_order": {
                "purchase_order": {"number": {"value": "219376"}},
                "buyer": {
                    "name": "SAS ELM LEBLANC",
                    "phone": "03.83.48.69.50",
                },
                "supplier": {"name": "EAU ET VAPEUR"},
                "bill_to": {"name": "LA MANUFACTURE", "department": "EAU ET VAPEUR"},
                "ship_to": {
                    "name": "Date Pièce Fourn. Notre",
                    "department": "02/04/2026 219376 F503",
                },
            }},
        }
        parties = build_clean_output(payload)["order"]["parties"]
        self.assertEqual(parties["buyer"]["name"], "Eau et Vapeur NANCY")
        self.assertEqual(parties["buyer"]["contact"]["name"], "EL OUARDI Kadija")
        self.assertEqual(parties["supplier"]["name"], "ELM LEBLANC")
        self.assertNotIn("ship_to", parties)

    def test_line_item_party_pollution_falls_back_to_delivery_company(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "Adresse de livraison COMMANDE FOURNISSEUR : 157 879 / AM\n"
                "LEBLANC AMIENS ELM LEBLANC SAS\n"
                "Montières Activités 126 BLD DE STALINGRAD"
            )}],
            "business_extractions": {"purchase_order": {
                "purchase_order": {"number": {"value": "157879 / AM"}},
                "buyer": {
                    "name": "87167723990 ELM100018 ECHANGEUR A PLAQUES",
                    "contact": {"name": "PORT.ACHAT FRAIS DE PORT"},
                },
                "supplier": {"name": "ELM LEBLANC SAS"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "Montières Activités",
                    "department": "LEBLANC AMIENS",
                    "clean_address": {
                        "one_line": "MONTIÈRES ACTIVITÉS, 80016 AMIENS",
                        "lines": ["MONTIÈRES ACTIVITÉS", "80016 AMIENS"],
                        "components": {
                            "recipient": "Montières Activités",
                            "department": "LEBLANC AMIENS",
                            "postal_code": "80016", "city": "AMIENS",
                        },
                    },
                }],
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "LEBLANC AMIENS")
        self.assertNotIn("name", buyer.get("contact", {}))

    def test_company_name_drops_interleaved_street_and_demandeur_is_clean(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "ELM LEBLANC\n"
                "Demandeur Stéphane BERTA PALUDS\n"
                "ZI LA PALUDS - 430 AV DE LA PALUDS"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {
                    "name": "REXEL France 1ERE AVENUE",
                    "email": "aubagne@afdb.fr",
                },
                "supplier": {"name": "ELM LEBLANC"},
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "REXEL France")
        self.assertEqual(buyer["contact"]["name"], "Stéphane BERTA")

    def test_table_contact_is_recovered_for_delivery_site_buyer(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "N° Document Date Contact Code WENDEL Plateforme\n"
                "France\n"
                "CF352950 13/04/2026 Valérie 01853P Parc d'activité MARMANDE SUD"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "01853P Parc d'activité MARMANDE SUD"},
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "WENDEL Plateforme",
                    "clean_address": {
                        "one_line": "WENDEL PLATEFORME, 47250 SAMAZAN",
                        "lines": ["WENDEL PLATEFORME", "47250 SAMAZAN"],
                        "components": {"recipient": "WENDEL Plateforme", "postal_code": "47250", "city": "Samazan"},
                    },
                }],
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "WENDEL Plateforme")
        self.assertEqual(buyer["contact"]["name"], "Valérie")

    def test_dotted_buyer_label_stops_contact_before_delivery_company(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "ELM LEBLANC\nAcheteur....... LEFEBVRE CHRISTELLE "
                "DESENFANS CAMBRAI LOGISTIQUE 83 ROUTE DE BAPAUME"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "LA FORCE DES INDEPENDANTS SOCODA"},
                "supplier": {"name": "y"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "DESENFANS",
                    "clean_address": {
                        "one_line": "DESENFANS, 83 ROUTE DE BAPAUME, 59400 CAMBRAI",
                        "lines": ["DESENFANS", "83 ROUTE DE BAPAUME", "59400 CAMBRAI"],
                        "components": {"recipient": "DESENFANS", "postal_code": "59400", "city": "Cambrai"},
                    },
                }],
            }},
        }
        parties = build_clean_output(payload)["order"]["parties"]
        self.assertEqual(parties["buyer"]["name"], "DESENFANS")
        self.assertEqual(parties["buyer"]["contact"]["name"], "LEFEBVRE CHRISTELLE")
        self.assertEqual(parties["supplier"]["name"], "ELM LEBLANC")

    def test_legal_capital_banner_is_not_a_buyer_name(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": "S.A.S. CAPITAL DE 8 500 000\nELM LEBLANC"}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "S.A.S. CAPITAL DE 8 500 000"},
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "DESENFANS",
                    "clean_address": {
                        "one_line": "DESENFANS, 59400 CAMBRAI",
                        "lines": ["DESENFANS", "59400 CAMBRAI"],
                        "components": {"recipient": "DESENFANS", "postal_code": "59400", "city": "Cambrai"},
                    },
                }],
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "DESENFANS")

    def test_price_heading_is_removed_from_explicit_buyer_contact(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": "Acheteur : MARIE-PIERRE BRIERY PRIX HT Page 1 / 1"}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "SOROFI SAS"},
                "supplier": {"name": "ELM LEBLANC"},
            }},
        }
        contact = build_clean_output(payload)["order"]["parties"]["buyer"]["contact"]
        self.assertEqual(contact["name"], "MARIE-PIERRE BRIERY")

    def test_correspondent_label_is_published_as_buyer_contact(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "Correspondant: Guillaume DIEU\n"
                "gdieu@iziconfort.fr"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "IZI confort"},
                "supplier": {"name": "ELM LEBLANC"},
            }},
        }
        contact = build_clean_output(payload)["order"]["parties"]["buyer"]["contact"]
        self.assertEqual(contact["name"], "Guillaume DIEU")
        self.assertEqual(contact["email"], "gdieu@iziconfort.fr")

    def test_page_counter_is_not_published_as_correspondent(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": "Correspondant : Page : 1"}],
            "business_extractions": {"purchase_order": {
                "buyer": {
                    "name": "GARANKA HOLDING",
                    "phone": "02 51 85 40 42",
                    "contact": {"name": "Page : 1"},
                },
                "supplier": {"name": "ELM LEBLANC"},
            }},
        }
        contact = build_clean_output(payload)["order"]["parties"]["buyer"]["contact"]
        self.assertNotIn("name", contact)
        self.assertEqual(contact["phone"], "02 51 85 40 42")

    def test_personne_a_contacter_repairs_name_and_email(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "Personne à contacter Amélie PEQUIGNOT\n"
                "amelie.pequignot@frei-sodiam.fr"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {
                    "name": "Exincourt sanitaire chauffage",
                    "contact": {"name": "er Amélie PEQUIGNOT"},
                },
                "supplier": {"name": "ELM LEBLANC"},
            }},
        }
        contact = build_clean_output(payload)["order"]["parties"]["buyer"]["contact"]
        self.assertEqual(contact["name"], "Amélie PEQUIGNOT")
        self.assertEqual(contact["email"], "amelie.pequignot@frei-sodiam.fr")

    def test_truncated_buyer_expands_from_matching_billing_and_delivery_roles(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "SERVICE RAPIDE"},
                "bill_to": {"name": "GAZ SERVICE RAPIDE"},
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "GAZ SERVICE RAPIDE",
                    "clean_address": {
                        "one_line": "GAZ SERVICE RAPIDE, 95600 EAUBONNE",
                        "lines": ["GAZ SERVICE RAPIDE", "95600 EAUBONNE"],
                        "components": {
                            "recipient": "GAZ SERVICE RAPIDE",
                            "postal_code": "95600", "city": "Eaubonne",
                        },
                    },
                }],
            }},
        }
        parties = build_clean_output(payload)["order"]["parties"]
        self.assertEqual(parties["buyer"]["name"], "GAZ SERVICE RAPIDE")

    def test_industrial_zone_is_not_used_as_delivery_recipient(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "Dépôt à livrer : MONDEVILLE (PIECES EXPRESS)\n"
                "Z.A. HENRI SPRIET\n1 Rue Philippe Lebon\n14120 MONDEVILLE"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "PIECES XPRESS"},
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "Z.A. HENRI SPRIET",
                    "clean_address": {
                        "one_line": (
                            "Z.A. HENRI SPRIET, 1 RUE PHILIPPE LEBON, "
                            "14120 MONDEVILLE"
                        ),
                        "lines": [
                            "Z.A. HENRI SPRIET", "1 RUE PHILIPPE LEBON",
                            "14120 MONDEVILLE",
                        ],
                        "components": {
                            "recipient": "Z.A. HENRI SPRIET",
                            "industrial_zone": "Z.A. HENRI SPRIET",
                            "street": "1 Rue Philippe Lebon",
                            "postal_code": "14120", "city": "Mondeville",
                        },
                    },
                }],
            }},
        }
        order = build_clean_output(payload)["order"]
        self.assertEqual(order["parties"]["ship_to"]["name"], "PIECES EXPRESS")
        self.assertEqual(order["delivery_address"]["party_name"], "PIECES EXPRESS")
        self.assertEqual(order["delivery_address"]["components"]["recipient"], "PIECES EXPRESS")
        self.assertEqual(
            order["delivery_address"]["formatted_lines"][:2],
            ["PIECES EXPRESS", "Z.A. HENRI SPRIET"],
        )

    def test_dotted_ocr_buyer_field_becomes_contact_and_header_company_wins(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "GERONDEAU COMMANDE FOURNISSEUR\n"
                "Acheteur cs... STEPHANIE"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "cs... STEPHANIE"},
                "supplier": {"name": "BOSCH THERMOTECHNOLOGIE"},
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "GERONDEAU")
        self.assertEqual(buyer["contact"]["name"], "STEPHANIE")

    def test_address_fragment_buyer_falls_back_to_matching_billing_party(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "D'ACTIVITES DE LA VALENTINE"},
                "bill_to": {"name": "SOMGAZ"},
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "SOMGAZ",
                    "clean_address": {
                        "one_line": "SOMGAZ, 13011 MARSEILLE",
                        "lines": ["SOMGAZ", "13011 MARSEILLE"],
                        "components": {
                            "recipient": "SOMGAZ", "postal_code": "13011",
                            "city": "Marseille",
                        },
                    },
                }],
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "SOMGAZ")

    def test_supplier_brand_suffix_is_removed_from_buyer(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": "Eau Et Vapeur GROUPE BOSCH\nELM LEBLANC"}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "Eau Et Vapeur GROUPE BOSCH"},
                "supplier": {"name": "ELM LEBLANC"},
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "Eau Et Vapeur")

    def test_ppc_delivery_site_expands_city_only_buyer(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "LISSES"},
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "PPC-LISSES",
                    "clean_address": {
                        "one_line": "PPC-LISSES, 91090 LISSES",
                        "lines": ["PPC-LISSES", "91090 LISSES"],
                        "components": {
                            "recipient": "PPC-LISSES", "postal_code": "91090",
                            "city": "Lisses",
                        },
                    },
                }],
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "PPC-LISSES")

    def test_generic_pickup_recipient_is_replaced_by_delivery_company(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "SANIZ (PARIS 19)"},
                "bill_to": {"name": "Référence à nous rappeler obligatoirement"},
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "DEPOTS ENLEVEMENTS",
                    "department": "AU FORUM DU BATIMENT",
                    "clean_address": {
                        "one_line": (
                            "DEPOTS ENLEVEMENTS, AU FORUM DU BATIMENT, "
                            "64 BIS RUE MEAUX, 75019 PARIS"
                        ),
                        "lines": [
                            "DEPOTS ENLEVEMENTS", "AU FORUM DU BATIMENT",
                            "64 BIS RUE MEAUX", "75019 PARIS",
                        ],
                        "components": {
                            "recipient": "DEPOTS ENLEVEMENTS",
                            "department": "AU FORUM DU BATIMENT",
                            "street": "64 BIS RUE MEAUX",
                            "postal_code": "75019", "city": "Paris",
                        },
                    },
                }],
            }},
        }
        order = build_clean_output(payload)["order"]
        self.assertNotIn("bill_to", order["parties"])
        self.assertEqual(
            order["parties"]["ship_to"]["name"], "AU FORUM DU BATIMENT"
        )
        self.assertEqual(
            order["delivery_address"]["formatted_lines"][0],
            "AU FORUM DU BATIMENT",
        )

    def test_contract_heading_buyer_falls_back_to_legal_company(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "COMMANDE D'ACHAT N 2846/4728739\n"
                "Contrat cadre n Accord 2011 - 2013\n"
                "ENGIE HOME SERVICES - SAS au capital de 1 121 232 euros"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "Contrat cadre n° Accord 2011 - 2013"},
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "OUEST ENGIE HOME SERVICES LE HAVRE",
                    "clean_address": {
                        "one_line": "OUEST ENGIE HOME SERVICES LE HAVRE, 76290 MONTIVILLIERS",
                        "lines": [
                            "OUEST ENGIE HOME SERVICES LE HAVRE",
                            "76290 MONTIVILLIERS",
                        ],
                        "components": {
                            "recipient": "OUEST ENGIE HOME SERVICES LE HAVRE",
                            "postal_code": "76290", "city": "Montivilliers",
                        },
                    },
                }],
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "ENGIE HOME SERVICES")

    def test_colon_prefixed_person_becomes_contact_not_buyer(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "ANDRETY AVIGNON COMMANDE FOURNISSEUR ELM LEBLANC SAS\n"
                "| Acheteur : Frédéric TESSIER | |"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": ": Frédéric TESSIER"},
                "supplier": {"name": "ELM LEBLANC SAS"},
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "ANDRETY AVIGNON")
        self.assertEqual(buyer["contact"]["name"], "Frédéric TESSIER")

    def test_explicit_acheteur_person_does_not_replace_header_company(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "ANDRETY AVIGNON COMMANDE FOURNISSEUR ELM LEBLANC SAS\n"
                "| Acheteur : Christophe ESTEVEZ | |"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "Christophe ESTEVEZ"},
                "supplier": {"name": "ELM LEBLANC SAS"},
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "ANDRETY AVIGNON")
        self.assertEqual(buyer["contact"]["name"], "Christophe ESTEVEZ")

    def test_truncated_buyer_expands_from_explicit_billing_company(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "SERVICE RAPIDE"},
                "bill_to": {"name": "GAZ SERVICE RAPIDE"},
                "supplier": {"name": "BOSCH THERMOTECHNOLOGIE"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "URBAN",
                    "clean_address": {
                        "one_line": "URBAN, 5 RUE DE L'HAUTIL, 78700 CONFLANS",
                        "lines": ["URBAN", "5 RUE DE L'HAUTIL", "78700 CONFLANS"],
                        "components": {
                            "recipient": "URBAN", "house_number": "5",
                            "street": "RUE DE L'HAUTIL", "postal_code": "78700",
                            "city": "CONFLANS",
                        },
                    },
                }],
            }},
        }
        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
        self.assertEqual(buyer["name"], "GAZ SERVICE RAPIDE")

    def test_buyer_company_precedes_its_local_delivery_site(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "GAZ SERVICE RAPIDE"},
                "supplier": {"name": "BOSCH THERMOTECHNOLOGIE"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "URBAN", "department": "GAZ SERVICE RAPIDE",
                    "clean_address": {
                        "one_line": "URBAN, GAZ SERVICE RAPIDE, 5 RUE DE L'HAUTIL, 78700 CONFLANS",
                        "lines": ["URBAN", "GAZ SERVICE RAPIDE", "5 RUE DE L'HAUTIL", "78700 CONFLANS"],
                        "components": {
                            "recipient": "URBAN", "department": "GAZ SERVICE RAPIDE",
                            "house_number": "5", "street": "RUE DE L'HAUTIL",
                            "postal_code": "78700", "city": "CONFLANS",
                        },
                    },
                }],
            }},
        }
        order = build_clean_output(payload)["order"]
        self.assertEqual(order["parties"]["ship_to"]["name"], "GAZ SERVICE RAPIDE")
        self.assertEqual(
            order["delivery_address"]["formatted_lines"][:2],
            ["GAZ SERVICE RAPIDE", "URBAN"],
        )

    def test_marketing_banner_and_grouped_order_heading_are_not_buyers(self):
        cases = (
            (
                "CARRELAG ALLE DE BAIN CUISINES CHAUFFAGE CLIMATISATION",
                "WENDEL LANGON SAS au capital de 40 000",
                "WENDEL Langon",
            ),
            (
                "** EXEMPLAIRE ACHAT ** N° Fournisseur: 5061 LE 27/01/26",
                "",
                "PROLIANS BA MONTPELLIER",
            ),
        )
        for polluted, source, delivery_name in cases:
            with self.subTest(polluted=polluted):
                payload = {
                    "document": {"primary_document_type": "purchase_order"},
                    "pages": [{"text": source}],
                    "business_extractions": {"purchase_order": {
                        "buyer": {"name": polluted},
                        "supplier": {"name": "ELM LEBLANC"},
                        "business_addresses": [{
                            "address_id": "delivery", "role": "ship_to",
                            "party_name": delivery_name,
                            "clean_address": {
                                "one_line": f"{delivery_name}, 34000 TEST",
                                "lines": [delivery_name, "34000 TEST"],
                                "components": {
                                    "recipient": delivery_name,
                                    "postal_code": "34000", "city": "TEST",
                                },
                            },
                        }],
                    }},
                }
                buyer = build_clean_output(payload)["order"]["parties"]["buyer"]
                self.assertEqual(buyer["name"], delivery_name)

    def test_parallel_delivery_supplier_row_recovers_delivery_company(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "Adresse de livraison COMMANDE FOURNISSEUR : 161 111 / AM\n"
                "LEBLANC AMIENS ELM LEBLANC SAS\n"
                "Montières Activités 126 BLD DE STALINGRAD"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "PORT.ACHAT FRAIS DE PORT"},
                "supplier": {"name": "ELM LEBLANC SAS"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "Montières Activités",
                    "clean_address": {
                        "one_line": "MONTIÈRES ACTIVITÉS, 7 BIS RUE ALFRED CATEL, 80016 AMIENS",
                        "lines": ["MONTIÈRES ACTIVITÉS", "7 BIS RUE ALFRED CATEL", "80016 AMIENS"],
                        "components": {
                            "recipient": "Montières Activités", "house_number": "7",
                            "house_number_suffix": "bis", "street": "RUE ALFRED CATEL",
                            "postal_code": "80016", "city": "AMIENS",
                        },
                    },
                }],
            }},
        }
        order = build_clean_output(payload)["order"]
        self.assertEqual(order["parties"]["buyer"]["name"], "LEBLANC AMIENS")
        self.assertEqual(order["parties"]["ship_to"]["name"], "LEBLANC AMIENS")
        self.assertEqual(
            order["delivery_address"]["formatted_lines"][:2],
            ["LEBLANC AMIENS", "MONTIÈRES ACTIVITÉS"],
        )

    def test_placeholder_client_number_falls_back_to_delivery_party(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "client n° 0"},
                "bill_to": {"name": "client n° 0"},
                "supplier": {"name": "BOSCH"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "CPO",
                    "clean_address": {
                        "one_line": "CPO, 26 RUE DE MESMERRIEN, 29200 BREST",
                        "lines": ["CPO", "26 RUE DE MESMERRIEN", "29200 BREST"],
                        "components": {
                            "recipient": "CPO", "street": "26 RUE DE MESMERRIEN",
                            "postal_code": "29200", "city": "Brest",
                        },
                    },
                }],
            }},
        }
        parties = build_clean_output(payload)["order"]["parties"]
        self.assertEqual(parties["buyer"]["name"], "CPO")
        self.assertNotIn("bill_to", parties)

    def test_multiline_affaire_suivie_contact_is_joined(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "SAS au CAPITAL 274.408 EUR Affaire suivie par DELEVAQUE\n"
                "FR 80 387873276 TVA INTRA-COMM. ANNE\n"
                "205 Av Du General Pruneau"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "SALICA"},
                "supplier": {"name": "ELM LEBLANC"},
            }},
        }
        contact = build_clean_output(payload)["order"]["parties"]["buyer"]["contact"]
        self.assertEqual(contact["name"], "DELEVAQUE ANNE")

    def test_billing_role_drops_routing_and_instruction_noise(self):
        for polluted in ("B.P. 1", "ACCUSE DE RECEPTION de commande.", "envoyée par mail ==>"):
            with self.subTest(polluted=polluted):
                payload = {
                    "document": {"primary_document_type": "purchase_order"},
                    "business_extractions": {"purchase_order": {
                        "buyer": {"name": "PROLIANS"},
                        "supplier": {"name": "ELM LEBLANC"},
                        "bill_to": {"name": polluted},
                    }},
                }
                parties = build_clean_output(payload)["order"]["parties"]
                self.assertNotIn("bill_to", parties)

    def test_billing_role_drops_footer_product_and_prose_noise(self):
        polluted_values = (
            "courrier : PROLIANS BURDIN MARINGUE - Comptabilité",
            "CLAPET ANTI RETOUR D15",
            "avant le 5 du mois suivant.",
        )
        for polluted in polluted_values:
            with self.subTest(polluted=polluted):
                payload = {
                    "document": {"primary_document_type": "purchase_order"},
                    "business_extractions": {"purchase_order": {
                        "buyer": {"name": "PROLIANS"},
                        "supplier": {"name": "ELM LEBLANC"},
                        "bill_to": {"name": polluted},
                    }},
                }
                parties = build_clean_output(payload)["order"]["parties"]
                self.assertNotIn("bill_to", parties)

    def test_header_instruction_and_reference_fragments_are_not_parties(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {
                    "name": "Référence : FBD29644 N° Compte : 15013322",
                    "contact": {"name": "ACHAT fgp"},
                },
                "supplier": {"name": "ELM LEBLANC"},
                "bill_to": {"name": "11H30 ou sur RDV"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "retourner par fax, ou par mail à l'adresse",
                    "department": "FGP",
                    "clean_address": {
                        "one_line": "FGP, 80 RUE BARBERIS, 06300 NICE",
                        "lines": ["FGP", "80 RUE BARBERIS", "06300 NICE"],
                        "components": {
                            "recipient": "retourner par fax, ou par mail à l'adresse",
                            "department": "FGP", "street": "80 RUE BARBERIS",
                            "postal_code": "06300", "city": "NICE",
                        },
                    },
                }],
            }},
        }

        parties = build_clean_output(payload)["order"]["parties"]

        self.assertEqual(parties["buyer"]["name"], "FGP")
        self.assertNotIn("contact", parties["buyer"])
        self.assertNotIn("bill_to", parties)
        self.assertEqual(parties["ship_to"]["name"], "FGP")
        delivery = build_clean_output(payload)["order"]["delivery_address"]
        self.assertNotIn("RETOURNER", delivery["formatted"])

    def test_party_name_drops_concatenated_phone_and_next_table_column(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "PPC TÃ©l: 04 78 61 92 73|PARTEDIS Chauffage Sanitaire"},
                "supplier": {"name": "ELM LEBLANC"},
            }},
        }

        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]

        self.assertEqual(buyer["name"], "PPC")

    def test_phone_prefixed_legal_footer_does_not_replace_delivery_buyer(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "PPC 62 bd Henry Navier COMMANDE MAIL PIECE DETACHEE\n"
                "Tél: 04 78 61 92 73|PARTEDIS Chauffage Sanitaire - "
                "SAS au capital de 17 739 996E -"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "N°5500277 /1877"},
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "PPC",
                    "clean_address": {
                        "one_line": "PPC, 62 BOULEVARD HENRI NAVIER, 95150 TAVERNY",
                        "lines": ["PPC", "62 BOULEVARD HENRI NAVIER", "95150 TAVERNY"],
                        "components": {
                            "recipient": "PPC", "house_number": "62",
                            "street": "Boulevard Henri Navier",
                            "postal_code": "95150", "city": "TAVERNY",
                        },
                    },
                }],
            }},
        }

        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]

        self.assertEqual(buyer["name"], "PPC TAVERNY")

    def test_clean_address_label_is_rebuilt_after_client_id_removal(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "REXEL TAVERNY",
                    "clean_address": {
                        "one_line": (
                            "REXEL TAVERNY, 1 RUE MARGUERITE PEREY, "
                            "CLI: 6133247, 95150 TAVERNY"
                        ),
                        "lines": [
                            "REXEL TAVERNY", "1 RUE MARGUERITE PEREY",
                            "CLI: 6133247", "95150 TAVERNY",
                        ],
                        "components": {
                            "recipient": "REXEL TAVERNY", "house_number": "1",
                            "street": "Rue Marguerite Perey",
                            "address_complement": "CLI: 6133247",
                            "postal_code": "95150", "city": "TAVERNY",
                        },
                    },
                }],
            }},
        }

        delivery = build_clean_output(payload)["order"]["delivery_address"]

        self.assertNotIn("6133247", delivery["formatted"])
        self.assertNotIn("address_complement", delivery["components"])

    def test_short_ppc_site_suffix_repairs_truncated_buyer(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "LILLE"},
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "PPC-LILLE",
                    "clean_address": {
                        "one_line": "PPC-LILLE, 59118 WAMBRECHIES",
                        "lines": ["PPC-LILLE", "59118 WAMBRECHIES"],
                        "components": {
                            "recipient": "PPC-LILLE", "postal_code": "59118",
                            "city": "WAMBRECHIES",
                        },
                    },
                }],
            }},
        }

        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]

        self.assertEqual(buyer["name"], "PPC-LILLE")

    def test_ppc_delivery_does_not_concatenate_partedis_legal_footer(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "PARTEDIS Chauffage Sanitaire"},
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "PPC",
                    "clean_address": {
                        "one_line": "PPC, 228 AVENUE JEAN MERMOZ, 63000 CLERMONT FERRAND",
                        "lines": ["PPC", "228 AVENUE JEAN MERMOZ", "63000 CLERMONT FERRAND"],
                        "components": {
                            "recipient": "PPC", "house_number": "228",
                            "street": "AVENUE JEAN MERMOZ", "postal_code": "63000",
                            "city": "CLERMONT FERRAND",
                        },
                    },
                }],
            }},
        }

        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]

        self.assertEqual(buyer["name"], "PPC")

    def test_supplier_heading_fragment_falls_back_to_billing_party(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "FOURNISSEUR Du CL d'émission REXEL-CENTRE"},
                "supplier": {"name": "ELM LEBLANC"},
                "bill_to": {"name": "REXEL France"},
            }},
        }

        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]

        self.assertEqual(buyer["name"], "REXEL France")

    def test_table_delivery_headings_are_not_a_contact_name(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "buyer": {
                    "name": "ESPINOSA",
                    "contact": {
                        "name": "Date livraison souhaitée Conditions Franco",
                    },
                },
                "supplier": {"name": "ELM LEBLANC"},
            }},
        }

        buyer = build_clean_output(payload)["order"]["parties"]["buyer"]

        self.assertNotIn("contact", buyer)

    def test_address_zone_fragment_buyer_falls_back_to_billing_company(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": "BON DE COMMANDE\nFacturé à : ISERBA"}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "DE ROGERVILLE"},
                "supplier": {"name": "ELM LEBLANC"},
                "bill_to": {"name": "ISERBA"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "ISERBA",
                    "clean_address": {
                        "one_line": "ISERBA, ZA DE ROGERVILLE, 76430 OUDALLE",
                        "lines": ["ISERBA", "ZA DE ROGERVILLE", "76430 OUDALLE"],
                        "components": {
                            "recipient": "ISERBA",
                            "industrial_zone": "ZA DE ROGERVILLE",
                            "postal_code": "76430", "city": "OUDALLE",
                        },
                    },
                }],
            }},
        }

        parties = build_clean_output(payload)["order"]["parties"]

        self.assertEqual(parties["buyer"]["name"], "ISERBA")

    def test_address_routing_fragment_buyer_uses_source_company_alias(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "Commande fournisseur\n"
                "BIO HABITAT - Rue C. Tellier-ZI de la Folie-"
                "La Chaize le Vicomte-CS 50001"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "Chaize le Vicomte-CS 50001 -"},
                "supplier": {"name": "ELM LEBLANC"},
            }},
        }

        parties = build_clean_output(payload)["order"]["parties"]

        self.assertEqual(parties["buyer"]["name"], "BIO HABITAT")

    def test_table_heading_buyer_uses_legal_footer_company(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "COMMANDE\nDATE NUMERO\n13/04/2026 21 463\n"
                "SARL SLARC ANCONETTI au capital de 100000 euros"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "DATE NUMERO"},
                "supplier": {"name": "ELM LEBLANC"},
            }},
        }

        parties = build_clean_output(payload)["order"]["parties"]

        self.assertEqual(parties["buyer"]["name"], "SLARC ANCONETTI")

    def test_explicit_billing_leadin_expands_truncated_company(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "MERCI D'EXPEDIER VOS FACTURES A L'ADRESSE SUIVANTE = "
                "DESCOURS ET CABAUD PACA"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "PROLIANS PROVENCE"},
                "supplier": {"name": "ELM LEBLANC"},
                "bill_to": {"name": "ET CABAUD PACA"},
            }},
        }
        bill_to = build_clean_output(payload)["order"]["parties"]["bill_to"]
        self.assertEqual(bill_to["name"], "DESCOURS ET CABAUD PACA")

    def test_ordering_depot_phone_is_not_part_of_buyer_name(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "Adresse de livraison COMMANDE FOURNISSEUR : 158 381 / AB\n"
                "LEBLANC ABBEVILLE ELM LEBLANC SAS\n"
                "Commandé par le dépôt :\n"
                "ABBEVILLE / 03 22 24 81 17"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "ABBEVILLE / 03 22 24 81 17"},
                "supplier": {"name": "ELM LEBLANC SAS"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "LEBLANC ABBEVILLE",
                    "clean_address": {
                        "one_line": "LEBLANC ABBEVILLE, 80100 ABBEVILLE",
                        "lines": ["LEBLANC ABBEVILLE", "80100 ABBEVILLE"],
                        "components": {
                            "recipient": "LEBLANC ABBEVILLE",
                            "postal_code": "80100", "city": "ABBEVILLE",
                        },
                    },
                }],
            }},
        }

        parties = build_clean_output(payload)["order"]["parties"]
        self.assertEqual(parties["buyer"]["name"], "LEBLANC ABBEVILLE")
        self.assertNotIn("03 22", parties["buyer"]["name"])

    def test_bio_habitat_legal_buyer_is_distinct_from_delivery_site(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"text": (
                "Commande fournisseur\n"
                "Magasin Vendéopôle BIO habitat U1\n"
                "Suivi par : SOUBRIE MARIE\n"
                "BIO HABITAT - Rue C. Tellier"
            )}],
            "business_extractions": {"purchase_order": {
                "buyer": {"name": "1 de 1"},
                "supplier": {"name": "ELM LEBLANC"},
                "business_addresses": [{
                    "address_id": "delivery", "role": "ship_to",
                    "party_name": "Vendéopôle Atlantique",
                    "clean_address": {
                        "one_line": "VENDÉOPÔLE ATLANTIQUE, 85210 SAINTE-HERMINE",
                        "lines": ["VENDÉOPÔLE ATLANTIQUE", "85210 SAINTE-HERMINE"],
                        "components": {
                            "recipient": "Vendéopôle Atlantique",
                            "postal_code": "85210", "city": "Sainte-Hermine",
                        },
                    },
                }],
            }},
        }
        parties = build_clean_output(payload)["order"]["parties"]
        self.assertEqual(parties["buyer"]["name"], "BIO HABITAT")
        self.assertEqual(parties["buyer"]["contact"]["name"], "SOUBRIE MARIE")
        self.assertEqual(parties["ship_to"]["name"], "Vendéopôle Atlantique")

    def test_published_schema_describes_the_versioned_contract(self):
        schema = clean_output_schema()
        self.assertEqual(schema["properties"]["schema_version"]["const"],
                         "jin-clean-extraction-v1")
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("customer_agency_code", schema["properties"]["order"]["properties"])
        self.assertIn("customer_agency_code", schema["$defs"]["address"]["properties"])


if __name__ == "__main__":
    unittest.main()

import unittest

from jin_runtime.delivery_addresses import clean_delivery_address, enrich_delivery_addresses


class DeliveryAddressTests(unittest.TestCase):
    def test_clean_label_separates_contact_and_removes_duplicate_cedex(self):
        block = {
            "role": "ship_to", "party_name": "CLIENT", "contact_name": "Alice",
            "formatted_address": "2 rue Diderot, 06003 Nice CEDEX 1, Nice CEDEX 1",
            "address": {
                "house_number": "2", "street_type": "rue", "street_name": "Diderot",
                "postal_code": "06003", "city": "Nice CEDEX 1", "country": "France",
            },
        }
        clean = clean_delivery_address(block)
        self.assertEqual(clean["lines"], ["CLIENT", "2 RUE DIDEROT", "06003 NICE CEDEX 1", "FRANCE"])
        self.assertNotIn("Alice", clean["one_line"])
        self.assertEqual(clean["one_line"].count("NICE"), 1)
        self.assertEqual(clean["source_formatted"], block["formatted_address"])

    def test_building_already_contained_in_department_is_not_printed_twice(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "CONFOGAZ IDF",
            "department": "DYNAMIKUM - BATIMENT B5",
            "address": {
                "building": "BATIMENT B5",
                "house_number": "3",
                "street": "RUE DU PALATINAT",
                "postal_code": "78300",
                "city": "POISSY",
                "country": "France",
            },
        })
        self.assertEqual(clean["one_line"].count("BATIMENT B5"), 1)
        self.assertNotIn("building", clean["components"])

    def test_partial_street_repeated_in_zone_is_removed(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "AU FORUM DU BATIMENT",
            "department": "ZI LA PALUDS",
            "address": {
                "building": "AU FORUM DU BATIMENT",
                "house_number": "430",
                "street": "AV DE LA PALUDS",
                "industrial_zone": "ZI LA PALUDS - 430 AV DE LA",
                "postal_code": "13400",
                "city": "AUBAGNE",
                "country": "France",
            },
        })
        self.assertEqual(clean["components"]["industrial_zone"], "ZI LA PALUDS")
        self.assertNotIn("building", clean["components"])
        self.assertEqual(clean["one_line"].count("430 AV DE LA PALUDS"), 1)
        self.assertNotIn("ZI LA PALUDS - 430 AV DE LA", clean["one_line"])

    def test_longer_recipient_replaces_redundant_short_site_name(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "REXEL CHAMPIGNY",
            "department": "REXEL CHAMPIGNY SUR MARNE",
            "address": {
                "house_number": "91",
                "street": "RUE MARCEL PAUL",
                "postal_code": "94500",
                "city": "CHAMPIGNY-SUR-MARNE",
            },
        })
        self.assertEqual(clean["components"]["recipient"], "REXEL CHAMPIGNY SUR MARNE")
        self.assertNotIn("department", clean["components"])
        self.assertEqual(clean["lines"].count("REXEL CHAMPIGNY SUR MARNE"), 1)

    def test_prolians_regional_site_alias_is_not_printed_twice(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "PROLIANS DR SAINT ETIENNE",
            "department": "PROLIANS ST ETIENNE",
            "address": {
                "house_number": "3", "street": "RUE JEAN SNELLA",
                "postal_code": "42000", "city": "SAINT-ETIENNE",
            },
        })

        self.assertNotIn("department", clean["components"])
        self.assertEqual(clean["lines"].count("PROLIANS DR SAINT ETIENNE"), 1)

        joined = clean_delivery_address({
            "role": "ship_to",
            "party_name": "PROLIANS DR SAINT ETIENNE PROLIANS ST ETIENNE",
            "address": {
                "house_number": "3", "street": "RUE JEAN SNELLA",
                "postal_code": "42000", "city": "SAINT-ETIENNE",
            },
        })
        self.assertEqual(
            joined["components"]["recipient"], "PROLIANS DR SAINT ETIENNE"
        )

    def test_legal_form_alone_is_not_recipient_and_exact_site_duplicate_is_removed(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "SAS",
            "department": "AU FORUM DU BATIMENT",
            "address": {
                "building": "AU FORUM DU BATIMENT",
                "house_number": "211-213",
                "street": "AVENUE MAURICE BERTEAUX",
                "postal_code": "78500",
                "city": "SARTROUVILLE",
            },
        })
        self.assertNotIn("recipient", clean["components"])
        self.assertNotIn("building", clean["components"])
        self.assertEqual(clean["one_line"].count("AU FORUM DU BATIMENT"), 1)

    def test_verified_ban_components_drive_clean_label_but_source_is_preserved(self):
        block = {
            "role": "ship_to", "formatted_address": "15 av du general pruneau 83000 toulon",
            "address": {"house_number": "15", "street": "av du general pruneau",
                        "postal_code": "83000", "city": "TOULON", "country": "France"},
            "ban_verification": {
                "status": "CANONICAL_MATCH", "matched_components": {
                    "house_number": "15", "street": "Avenue Général Pruneau",
                    "postal_code": "83000", "city": "Toulon",
                },
            },
        }
        clean = clean_delivery_address(block)
        self.assertEqual(clean["status"], "VERIFIED_CANONICAL")
        self.assertEqual(clean["lines"][0], "15 AVENUE GÉNÉRAL PRUNEAU")
        self.assertEqual(clean["source_formatted"], block["formatted_address"])

    def test_ban_cannot_change_an_explicit_source_street_type(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "CONFOGAZ IDF",
            "address": {
                "house_number": "9", "street": "AVENUE DES FRERES LUMIERE",
                "postal_code": "93330", "city": "NEUILLY SUR MARNE",
            },
            "ban_verification": {
                "status": "CANONICAL_MATCH",
                "matched_components": {
                    "house_number": "9", "street": "Rue des Freres Lumiere",
                    "postal_code": "93330", "city": "Neuilly-sur-Marne",
                },
            },
        })
        self.assertIn("9 AVENUE DES FRERES LUMIERE", clean["lines"])
        self.assertNotIn("9 RUE DES FRERES LUMIERE", clean["lines"])

    def test_delivery_party_instruction_and_apostrophe_ocr_are_cleaned(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "Contact Code WENDEL Plateforme",
            "address": {
                "street": "Route D4OZENAY",
                "address_complement": "Livraison de preference avant 12h",
                "postal_code": "71000", "city": "MACON",
            },
        })
        self.assertEqual(clean["components"]["recipient"], "WENDEL Plateforme")
        self.assertEqual(clean["components"]["street"], "Route d'OZENAY")
        self.assertNotIn("address_complement", clean["components"])

    def test_apostrophe_ocr_variant_three_is_cleaned(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "address": {
                "street": "Route D3OZENAY",
                "postal_code": "71000", "city": "MACON",
            },
        })
        self.assertEqual(clean["components"]["street"], "Route d'OZENAY")

    def test_country_code_is_not_emitted_as_an_industrial_zone(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "SOROFI ROANNE",
            "address": {
                "house_number": "196-210", "street": "Rue de Charlieu",
                "industrial_zone": "FR", "postal_code": "42300",
                "city": "Roanne Cedex", "country": "France", "country_code": "FR",
            },
        })
        self.assertNotIn("industrial_zone", clean["components"])
        self.assertEqual(
            clean["one_line"],
            "SOROFI ROANNE, 196-210 RUE DE CHARLIEU, 42300 ROANNE CEDEX, FRANCE",
        )

    def test_country_suffix_is_removed_from_industrial_zone(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "SOROFI MONTELIMAR",
            "address": {
                "industrial_zone": "ZA DE FORTUNEAU | FR",
                "postal_code": "26200", "city": "MONTELIMAR", "country": "France",
            },
        })
        self.assertEqual(clean["components"]["industrial_zone"], "ZA DE FORTUNEAU")
        self.assertEqual(
            clean["one_line"],
            "SOROFI MONTELIMAR, ZA DE FORTUNEAU, 26200 MONTELIMAR, FRANCE",
        )

    def test_repeated_zone_with_payment_instruction_is_not_printed(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "PROLIANS BA BEZIERS",
            "address": {
                "house_number": "24", "street": "RUE MARTIN LUTHER KING",
                "industrial_zone": "ZI LE CAPISCOL",
                "address_complement": "ZI LE CAPISCOL VIREMENT",
                "cs": "CS 63009", "postal_code": "34536", "city": "BEZIERS",
                "country": "France",
            },
        })
        self.assertNotIn("address_complement", clean["components"])
        self.assertEqual(clean["one_line"].count("ZI LE CAPISCOL"), 1)
        self.assertNotIn("VIREMENT", clean["one_line"])

    def test_vat_and_zone_fused_into_recipient_are_separated(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": (
                "PROLIANS BA PERPIGNAN N�TVA EUROPE FR25775588692 "
                "Z.I SAINT CHARLES"
            ),
            "address": {
                "street": "AVENUE DE BRUXELLES BP 5151 VIREMENT",
                "postal_code": "66031", "city": "PERPIGNAN CEDEX",
                "country": "France",
            },
        })

        self.assertEqual(clean["components"]["recipient"], "PROLIANS BA PERPIGNAN")
        self.assertEqual(clean["components"]["industrial_zone"], "Z.I SAINT CHARLES")
        self.assertEqual(clean["components"]["street"], "AVENUE DE BRUXELLES")
        self.assertEqual(clean["components"]["po_box"], "BP 5151")
        self.assertEqual(clean["one_line"], (
            "PROLIANS BA PERPIGNAN, AVENUE DE BRUXELLES, Z.I SAINT CHARLES, "
            "BP 5151, 66031 PERPIGNAN CEDEX, FRANCE"
        ))
        self.assertNotIn("TVA", clean["one_line"])
        self.assertNotIn("VIREMENT", clean["one_line"])

    def test_delivery_department_is_kept_and_bare_street_type_is_removed(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "SALICA",
            "department": "Dépôt SAL. CARROS DEPOT",
            "address": {
                "line1": "RUE", "street": "RUE",
                "industrial_zone": "Z.I. 4e Avenue - 12e Rue",
                "postal_code": "06510", "city": "CARROS", "country": "France",
            },
        })
        self.assertEqual(clean["components"]["department"], "Dépôt SAL. CARROS DEPOT")
        self.assertNotIn("street", clean["components"])
        self.assertEqual(clean["lines"][:3], [
            "SALICA", "DÉPÔT SAL. CARROS DEPOT", "Z.I. 4E AVENUE - 12E RUE",
        ])

    def test_contact_label_is_not_used_as_delivery_recipient(self):
        clean = clean_delivery_address({
            "role": "ship_to", "party_name": "TELEPHONE 04 81 51",
            "address": {
                "house_number": "120", "street": "Rue Hélène Boucher",
                "postal_code": "69330", "city": "PUSIGNAN",
            },
        })
        self.assertNotIn("recipient", clean["components"])
        self.assertEqual(clean["lines"][0], "120 RUE HÉLÈNE BOUCHER")

    def test_country_name_is_not_used_as_delivery_recipient(self):
        clean = clean_delivery_address({
            "role": "ship_to", "party_name": "FRANCE", "department": "PPC",
            "address": {
                "house_number": "28", "street": "boulevard Lenine",
                "postal_code": "76800", "city": "ST ETIENNE DU ROUVRAY",
                "country": "France",
            },
        })
        self.assertNotIn("recipient", clean["components"])
        self.assertEqual(clean["lines"][0], "PPC")
        self.assertEqual(clean["lines"][-1], "FRANCE")

    def test_delivery_label_fragment_is_not_used_as_recipient(self):
        clean = clean_delivery_address({
            "role": "ship_to", "party_name": "DE LIVRAISON",
            "address": {
                "house_number": "2123", "street": "ROUTE NATIONALE",
                "postal_code": "45774", "city": "SARAN CEDEX",
                "country": "France",
            },
        })
        self.assertNotIn("recipient", clean["components"])
        self.assertEqual(clean["lines"][0], "2123 ROUTE NATIONALE")

    def test_country_column_is_removed_from_city_but_real_city_name_is_preserved(self):
        noisy = clean_delivery_address({
            "role": "ship_to", "party_name": "GERONDEAU",
            "address": {
                "house_number": "2123", "street": "ROUTE NATIONALE",
                "postal_code": "45774", "city": "SARAN FRANCE CEDEX",
                "country": "France",
            },
        })
        self.assertEqual(noisy["components"]["city"], "SARAN")
        self.assertEqual(
            noisy["one_line"],
            "GERONDEAU, 2123 ROUTE NATIONALE, 45774 SARAN CEDEX, FRANCE",
        )

        genuine = clean_delivery_address({
            "role": "ship_to",
            "address": {
                "street": "Rue des Ateliers", "postal_code": "93290",
                "city": "TREMBLAY EN FRANCE", "country": "France",
            },
        })
        self.assertEqual(genuine["components"]["city"], "TREMBLAY EN FRANCE")

    def test_zone_ocr_drops_table_fragments(self):
        clean = clean_delivery_address({
            "role": "ship_to", "party_name": "WENDEL LANGON",
            "address": {
                "street": "Route de Bazas", "industrial_zone": "Z| DUMES | U | QTE",
                "postal_code": "33210", "city": "LANGON",
            },
        })
        self.assertEqual(clean["components"]["industrial_zone"], "ZI DUMES")
        self.assertNotIn("QTE", clean["one_line"])

    def test_zone_ocr_in_complement_is_cleaned_too(self):
        clean = clean_delivery_address({
            "role": "ship_to", "party_name": "WENDEL LANGON",
            "address": {
                "street": "Route de Bazas", "address_complement": "Z| DUMES",
                "postal_code": "33210", "city": "LANGON",
            },
        })
        self.assertEqual(clean["components"]["industrial_zone"], "ZI DUMES")
        self.assertNotIn("address_complement", clean["components"])

    def test_parenthesized_roundabout_complement_is_normalized(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "address": {
                "industrial_zone": "Parc d'activite Marmande Sud",
                "address_complement": "(Roind Point du peage autoroute)",
                "postal_code": "47250", "city": "SAMAZAN",
            },
        })
        self.assertEqual(
            clean["components"]["address_complement"],
            "rond-point du peage autoroute",
        )

    def test_leading_ocr_quote_is_removed_from_city(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "address": {
                "street": "Avenue J.L LAMBOT",
                "postal_code": "83079",
                "city": "‘TOULON",
            },
        })
        self.assertEqual(clean["components"]["city"], "TOULON")
        self.assertIn("83079 TOULON", clean["one_line"])

    def test_slash_joined_department_duplicate_is_removed_from_json_and_label(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "WENDEL Plateforme",
            "department": (
                "Parc d'activite MARMANDE SUD / "
                "(Roind Point du peage autoroute)"
            ),
            "address": {
                "industrial_zone": "Parc d'activite MARMANDE SUD",
                "address_complement": "(Roind Point du peage autoroute)",
                "postal_code": "47250",
                "city": "SAMAZAN",
            },
        })
        self.assertNotIn("department", clean["components"])
        self.assertEqual(clean["one_line"].count("PARC D'ACTIVITE MARMANDE SUD"), 1)
        self.assertEqual(clean["one_line"].count("ROND-POINT DU PEAGE AUTOROUTE"), 1)

    def test_enrichment_marks_ambiguous_multiple_ship_to_addresses(self):
        payload = {"business_extractions": {"purchase_order": {
            "business_addresses": [
                {"address_id": "a", "role": "ship_to", "address": {"postal_code": "75001", "city": "Paris"}},
                {"address_id": "b", "role": "ship_to", "address": {"postal_code": "69001", "city": "Lyon"}},
            ]
        }}}
        enrich_delivery_addresses(payload)
        po = payload["business_extractions"]["purchase_order"]
        self.assertEqual(po["delivery_address_selection"]["status"], "AMBIGUOUS")
        self.assertTrue(all("clean_address" in item for item in po["business_addresses"]))

    def test_zone_complement_is_promoted_to_delivery_site(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "address": {"address_complement": "Zone des Négadoux",
                        "postal_code": "83140", "city": "Six Fours les Plages"},
        })
        self.assertEqual(clean["status"], "STRUCTURED_UNVERIFIED")
        self.assertEqual(clean["components"]["industrial_zone"], "Zone des Négadoux")
        self.assertNotIn("address_complement", clean["components"])


    def test_placeholders_instructions_and_redundant_city_department_are_removed(self):
        placeholder = clean_delivery_address({
            "role": "ship_to",
            "party_name": "PPC",
            "address": {
                "house_number": "24",
                "street": "RUE DES RENARDIERES",
                "address_complement": "missing",
                "postal_code": "62300",
                "city": "LENS",
            },
        })
        self.assertNotIn("address_complement", placeholder["components"])
        self.assertNotIn("MISSING", placeholder["one_line"])

        instruction = clean_delivery_address({
            "role": "ship_to",
            "party_name": "ISERBA",
            "address": {
                "house_number": "8",
                "street": "AVENUE EUGENE HENAFF",
                "address_complement": "LIVRAISON LE MATIN / MR BOUCHA",
                "postal_code": "69120",
                "city": "VAULX-EN-VELIN",
            },
        })
        self.assertNotIn("address_complement", instruction["components"])

        duplicate = clean_delivery_address({
            "role": "ship_to",
            "party_name": "PROLIANS LC AGNEAUX",
            "department": "AGNEAUX",
            "address": {
                "house_number": "1522",
                "street": "ROUTE DE PERIERS",
                "postal_code": "50180",
                "city": "AGNEAUX",
            },
        })
        self.assertNotIn("department", duplicate["components"])
        self.assertEqual(duplicate["one_line"].count("AGNEAUX"), 2)

    def test_phone_and_shipping_terms_are_not_address_complements(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "PPC",
            "address": {
                "house_number": "28",
                "street": "BOULEVARD LENINE",
                "address_complement": "Portable: | Condition liv : Livre franco",
                "postal_code": "76800",
                "city": "ST ETIENNE DU ROUVRAY",
                "country": "France",
            },
        })
        self.assertNotIn("address_complement", clean["components"])
        self.assertEqual(
            clean["one_line"],
            "PPC, 28 BOULEVARD LENINE, 76800 ST ETIENNE DU ROUVRAY, FRANCE",
        )

    def test_shipping_instruction_is_not_exposed_as_delivery_department(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "RICHARDSON AG. DE IRIGNY",
            "department": (
                "MODE D'EXPEDITION : / VEUILLEZ NOUS AVISER DE TOUTE "
                "EXPEDITION A NOTRE ATTENTION, FACTURE EN 1 EXP."
            ),
            "address": {
                "house_number": "26",
                "street": "RUE DE LA MOUCHE",
                "industrial_zone": "Z.I.",
                "postal_code": "69540",
                "city": "IRIGNY",
                "country": "France",
            },
        })
        self.assertNotIn("department", clean["components"])
        self.assertNotIn("EXPEDITION", clean["one_line"])
        self.assertEqual(
            clean["lines"],
            [
                "RICHARDSON AG. DE IRIGNY",
                "26 RUE DE LA MOUCHE",
                "Z.I.",
                "69540 IRIGNY",
                "FRANCE",
            ],
        )

    def test_delivery_wish_sentence_is_not_a_department(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "ESPINOSA",
            "department": "Livraison souhaitée à la société ESPINOSA",
            "address": {
                "house_number": "43", "street": "Boulevard Berthelot",
                "postal_code": "34000", "city": "Montpellier",
            },
        })

        self.assertNotIn("department", clean["components"])
        self.assertNotIn("LIVRAISON SOUHAITEE", clean["one_line"])

    def test_city_repeated_as_complement_is_removed(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "PROLIANS DP ORANGE",
            "address": {
                "house_number": "585",
                "street": "AVENUE DE VERDUN",
                "address_complement": "ORANGE",
                "postal_code": "84100",
                "city": "ORANGE",
            },
        })
        self.assertNotIn("address_complement", clean["components"])
        self.assertEqual(clean["one_line"].count("ORANGE"), 2)

    def test_dangling_zone_markers_are_removed_from_site_and_street(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "PROLIANS",
            "address": {
                "house_number": "462",
                "street": "RUE DE L'INDUSTRIE -ZI-",
                "industrial_zone": "ZI LES ROCHETTES -",
                "postal_code": "34009",
                "city": "MONTPELLIER",
            },
        })
        self.assertEqual(clean["components"]["street"], "RUE DE L'INDUSTRIE")
        self.assertEqual(clean["components"]["industrial_zone"], "ZI LES ROCHETTES")

    def test_duplicate_routing_and_compound_department_are_removed(self):
        routing = clean_delivery_address({
            "role": "ship_to",
            "party_name": "MAG U3",
            "address": {
                "address_complement": "Site eco-industria - Rue des fermes - BP321",
                "po_box": "BP 321",
                "postal_code": "59813",
                "city": "LESQUIN",
            },
        })
        self.assertEqual(
            routing["components"]["address_complement"],
            "Site eco-industria - Rue des fermes",
        )
        self.assertEqual(routing["one_line"].count("BP 321"), 1)
        self.assertEqual(routing["one_line"].count("SITE ECO-INDUSTRIA"), 1)

        compound = clean_delivery_address({
            "role": "ship_to",
            "party_name": "PPC-LILLE",
            "department": "PPC-LILLE / 262 ALLEE DE L'ECOPARK / BAT D",
            "address": {
                "building": "BAT D",
                "house_number": "262",
                "street": "ALLEE DE L'ECOPARK",
                "postal_code": "59118",
                "city": "WAMBRECHIES",
            },
        })
        self.assertNotIn("department", compound["components"])
        self.assertEqual(compound["one_line"].count("PPC-LILLE"), 1)
        self.assertEqual(compound["one_line"].count("262 ALLEE DE L'ECOPARK"), 1)

        abbreviated_street = clean_delivery_address({
            "role": "ship_to",
            "party_name": "C.C.L BEZIERS",
            "department": "Z.A.C DE MERCORENT / 350 R ALPHONSE BEAU DE ROCHAS",
            "address": {
                "house_number": "350",
                "street": "R ALPHONSE BEAU DE ROCHAS",
                "industrial_zone": "Z.A.C DE MERCORENT",
                "postal_code": "34500",
                "city": "BEZIERS",
            },
            "ban_verification": {
                "status": "EXACT_MATCH",
                "matched_components": {
                    "house_number": "350",
                    "street": "Rue Alphonse Beau de Rochas",
                    "postal_code": "34500",
                    "city": "BÃ©ziers",
                },
            },
        })
        self.assertNotIn("department", abbreviated_street["components"])
        self.assertEqual(abbreviated_street["one_line"].count("MERCOR"), 1)

    def test_zone_promoted_from_complement_drops_trailing_country_fragment(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "SOROFI MONTELIMAR",
            "address": {
                "house_number": "7",
                "street": "CHEMIN DE FORTUNEAU",
                "address_complement": "ZA DE FORTUNEAU | FR",
                "postal_code": "26200",
                "city": "MONTELIMAR",
                "country": "France",
                "country_code": "FR",
            },
        })
        self.assertEqual(clean["components"]["industrial_zone"], "ZA DE FORTUNEAU")
        self.assertNotIn("| FR", clean["one_line"])

    def test_client_identifier_is_not_an_address_complement(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "REXEL France",
            "address": {
                "house_number": "2", "street": "RUE DE PARIS",
                "address_complement": "CLI: 6133247",
                "postal_code": "75001", "city": "PARIS",
            },
        })

        self.assertNotIn("address_complement", clean["components"])
        self.assertNotIn("6133247", clean["one_line"])

    def test_payment_method_is_not_an_address_complement(self):
        clean = clean_delivery_address({
            "role": "ship_to", "party_name": "PROLIANS LC AGNEAUX",
            "address": {
                "house_number": "1522", "street": "ROUTE DE PERIERS",
                "address_complement": "VIREMENT", "po_box": "BP 63",
                "postal_code": "50180", "city": "AGNEAUX",
            },
        })
        self.assertNotIn("VIREMENT", clean["one_line"])
        self.assertNotIn("address_complement", clean["components"])

    def test_access_code_is_not_a_postal_address_component(self):
        clean = clean_delivery_address({
            "role": "ship_to", "party_name": "ISERBA",
            "address": {
                "house_number": "2104", "street": "RUE JEAN JAURES",
                "address_complement": "code cadenas 2375",
                "postal_code": "71410", "city": "SANVIGNES LES MINES",
            },
        })
        self.assertNotIn("2375", clean["one_line"])
        self.assertNotIn("address_complement", clean["components"])

    def test_care_of_line_follows_business_recipient(self):
        clean = clean_delivery_address({
            "role": "ship_to", "party_name": "CHEZ ISERBA",
            "department": "CONFOGAZ IDF",
            "address": {
                "house_number": "17", "street": "RUE DU BOIS MOUSSAY",
                "postal_code": "93240", "city": "STAINS",
            },
        })
        self.assertEqual(clean["components"]["recipient"], "CONFOGAZ IDF")
        self.assertEqual(clean["components"]["department"], "CHEZ ISERBA")
        self.assertEqual(clean["lines"][:2], ["CONFOGAZ IDF", "CHEZ ISERBA"])

    def test_delivery_instruction_is_removed_from_zone_and_city_hyphens_are_tightened(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "DEPAN CHAUFFAGE SERVICE",
            "address": {
                "house_number": "2",
                "street": "RUE MARIE CURIE",
                "industrial_zone": "ZA LA POINTE DE L'ABBE | LIVRAISON IMPERATIVE LE MATIN",
                "postal_code": "91700",
                "city": "VILLIERS -SUR-ORGE",
            },
        })
        self.assertEqual(clean["components"]["industrial_zone"], "ZA LA POINTE DE L'ABBE")
        self.assertEqual(clean["components"]["city"], "VILLIERS-SUR-ORGE")
        self.assertNotIn("LIVRAISON IMPERATIVE", clean["one_line"])

    def test_duplicate_building_and_zone_are_not_both_exposed(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "PPC",
            "address": {
                "house_number": "62",
                "street": "BOULEVARD HENRI NAVIER",
                "building": "ZAE DU CHENE BOCQUET TAVERT PARK BAT 6/3",
                "industrial_zone": "ZAE DU CHENE BOCQUET TAVERT PARK BAT 6/3",
                "postal_code": "95150",
                "city": "TAVERNY",
            },
        })
        self.assertNotIn("building", clean["components"])
        self.assertEqual(clean["one_line"].count("ZAE DU CHENE BOCQUET"), 1)

    def test_common_lumiere_ocr_error_and_combined_zone_street_are_cleaned(self):
        lumiere = clean_delivery_address({
            "role": "ship_to",
            "party_name": "CONFOGAZ IDF",
            "address": {
                "house_number": "9",
                "street": "AVENUE DES FRERES LIMIERE",
                "postal_code": "93330",
                "city": "NEUILLY SUR MARNE",
            },
        })
        self.assertEqual(lumiere["components"]["street"], "AVENUE DES FRERES LUMIERE")

        combined = clean_delivery_address({
            "role": "ship_to",
            "party_name": "CCL BEDARIEUX",
            "department": "Z.A.E.",
            "address": {
                "street": "ROUTE DE NISSERGUES",
                "industrial_zone": "Z.A.E. ROUTE DE NISSERGUES",
                "postal_code": "34600",
                "city": "BEDARIEUX",
            },
        })
        self.assertNotIn("department", combined["components"])
        self.assertEqual(combined["components"]["industrial_zone"], "Z.A.E.")
        self.assertEqual(combined["one_line"].count("ROUTE DE NISSERGUES"), 1)

    def test_interleaved_zone_and_street_columns_are_reassembled(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "PROLIANS GW SAINT AVOLD",
            "department": "ZI DU",
            "address": {
                "house_number": "76",
                "house_number_suffix": "BIS",
                "street": "RUE ALTMAYER",
                "industrial_zone": "ZI DU 76 BIS RUE ALTMAYER",
                "address_complement": "RUE DU GROS HETRE",
                "postal_code": "57500",
                "city": "SAINT-AVOLD",
                "country": "France",
            },
        })

        self.assertEqual(
            clean["components"]["industrial_zone"], "ZI DU GROS HETRE",
        )
        self.assertNotIn("address_complement", clean["components"])
        self.assertNotIn("department", clean["components"])
        self.assertEqual(clean["one_line"].count("76 BIS RUE ALTMAYER"), 1)

    def test_address_like_recipient_is_promoted_to_street(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "205, Av General Pruneau",
            "department": "ANCONETTI / Depot ANC. TOULON",
            "address": {"postal_code": "83000", "city": "TOULON"},
        })
        self.assertNotIn("recipient", clean["components"])
        self.assertEqual(clean["components"]["street"], "205, Av General Pruneau")
        self.assertEqual(
            clean["lines"],
            [
                "ANCONETTI / DEPOT ANC. TOULON",
                "205, AV GENERAL PRUNEAU",
                "83000 TOULON",
            ],
        )

    def test_address_like_recipient_is_promoted_when_line1_repeats_it(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "205, Av General Pruneau",
            "department": "ANCONETTI / Depot ANC. TOULON",
            "address": {
                "line1": "205, Av General Pruneau",
                "postal_code": "83000",
                "city": "TOULON",
            },
        })
        self.assertNotIn("recipient", clean["components"])
        self.assertEqual(clean["components"]["street"], "205, Av General Pruneau")

    def test_locality_line_does_not_block_street_promotion(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "205, Av General Pruneau",
            "department": "ANCONETTI / Depot ANC. TOULON",
            "address": {
                "line1": "83000 Toulon",
                "postal_code": "83000",
                "city": "TOULON",
            },
        })
        self.assertNotIn("recipient", clean["components"])
        self.assertEqual(clean["components"]["street"], "205, Av General Pruneau")
        self.assertEqual(clean["lines"][0], "ANCONETTI / DEPOT ANC. TOULON")

    def test_hyphenated_bare_zone_marker_is_removed(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "PROLIANS",
            "address": {
                "street": "RUE DE L'INDUSTRIE",
                "industrial_zone": "-ZI",
                "postal_code": "34009",
                "city": "MONTPELLIER",
            },
        })
        self.assertNotIn("industrial_zone", clean["components"])
        self.assertNotIn("-ZI", clean["one_line"])

    def test_ocr_two_dot_a_dot_d_is_normalized_as_activity_zone(self):
        clean = clean_delivery_address({
            "role": "ship_to",
            "party_name": "PROLIANS BA MILLAU",
            "address": {
                "industrial_zone": "2.A.D DE RAUJOLLES",
                "street": "7 RUE ANDRE DUPONT",
                "postal_code": "12100",
                "city": "CREISSELS",
            },
        })
        self.assertEqual(clean["components"]["industrial_zone"], "Z.A.D. DE RAUJOLLES")
        self.assertIn("Z.A.D. DE RAUJOLLES", clean["one_line"])


if __name__ == "__main__":
    unittest.main()

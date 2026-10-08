import unittest

from jin_runtime.weak_field_reconciliation import reconcile_weak_fields


def base_payload():
    return {
        "document": {
            "primary_document_type": "purchase_order",
            "detected_document_type": "purchase_order",
        },
        "business_extractions": {
            "purchase_order": {
                "purchase_order": {},
                "business_addresses": [],
                "lines": [],
                "totals": {},
            }
        },
        "weak_field_suggestions": {"anchored_fields": {}, "address_candidates": []},
    }


def order_candidate(value="PO-123"):
    return {
        "value": value,
        "confidence": 0.998,
        "source": "geometry_explicit_order_label_v3",
        "source_text": value,
        "page": 1,
        "bbox": [40, 80, 120, 95],
    }


def address_block(role, party, house, street, postal, city):
    return {
        "address_id": f"{role}-{postal}",
        "role": role,
        "party_name": party,
        "address": {
            "house_number": house,
            "street": street,
            "postal_code": postal,
            "city": city,
            "country": "France",
            "country_code": "FR",
        },
        "formatted_address": f"{house} {street}, {postal} {city}",
    }


def ship_candidate(party, house, street_type, street_name, postal=None, city=None):
    components = {
        "house_number": house,
        "street_type": street_type,
        "street_name": street_name,
    }
    if postal:
        components["postal_code"] = postal
    if city:
        components["city"] = city
    return {
        "role": "ship_to",
        "party_name": party,
        "components": components,
        "formatted_address_suggestion": " ".join(
            item for item in (house, street_type, street_name, postal, city) if item
        ),
        "confidence": 0.995 if postal and city else 0.97,
        "page": 1,
        "source_line": 10,
        "zone_id": "p1_ship_to_01",
    }


class WeakFieldReconciliationTests(unittest.TestCase):
    def test_kilometre_second_street_is_merged_into_one_delivery_address(self):
        payload = base_payload()
        first = ship_candidate(
            "CCL PERPIGNAN SUD", "3760", "AV", "JULIEN PANCHOT (GPS)",
            "66000", "PERPIGNAN",
        )
        first["source_line"] = 13
        second = ship_candidate(
            "CCL PERPIGNAN SUD", "4", "ROUTE", "DE THUIR",
            "66000", "PERPIGNAN",
        )
        second["department"] = "KM"
        second["source_line"] = 14
        payload["weak_field_suggestions"]["address_candidates"] = [first, second]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["address"]["house_number"], "3760")
        self.assertEqual(
            selected[0]["address"]["address_complement"],
            "KM 4 ROUTE DE THUIR",
        )

    def test_geometry_zone_replaces_core_zone_misread_as_recipient(self):
        payload = base_payload()
        existing = address_block(
            "ship_to", "ZA DE ROGERVILLE", "1", "CHEMIN DES PLANS D'EAU",
            "76430", "OUDALLE",
        )
        existing["department"] = "ISERBA"
        existing["address"]["street_name"] = "DES PLANS D'EAU"
        payload["business_extractions"]["purchase_order"]["business_addresses"] = [existing]
        candidate = ship_candidate(
            "ISERBA", "1", "CHEMIN", "DES PLANS D'EAU", "76430", "OUDALLE",
        )
        candidate["components"]["industrial_zone"] = "ZA DE ROGERVILLE"
        payload["weak_field_suggestions"]["address_candidates"] = [candidate]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "ISERBA")
        self.assertNotEqual(selected[0].get("department"), "ISERBA")
        self.assertEqual(selected[0]["address"]["industrial_zone"], "ZA DE ROGERVILLE")

    def test_explicit_order_number_fills_or_replaces_core_value(self):
        payload = base_payload()
        payload["weak_field_suggestions"]["anchored_fields"]["order_number"] = order_candidate("674072490")
        out = reconcile_weak_fields(payload)
        header = out["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertEqual(header["number"]["value"], "674072490")

        header["number"] = {"value": "15 Av du General Pruneau"}
        out = reconcile_weak_fields(payload)
        self.assertEqual(header["number"]["value"], "674072490")
        self.assertEqual(header["number_original"]["value"], "15 Av du General Pruneau")

    def test_explicit_order_metadata_replaces_product_reference(self):
        payload = base_payload()
        header = payload["business_extractions"]["purchase_order"]["purchase_order"]
        header["number"] = {"value": "H7736504817"}
        candidate = order_candidate("CF352124")
        candidate["source"] = "geometry_explicit_order_metadata_v3"
        payload["weak_field_suggestions"]["anchored_fields"]["order_number"] = candidate

        out = reconcile_weak_fields(payload)
        header = out["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertEqual(header["number"]["value"], "CF352124")
        self.assertEqual(header["number_original"]["value"], "H7736504817")

    def test_date_from_same_explicit_order_metadata_table_fills_missing_date(self):
        payload = base_payload()
        number = order_candidate("FBC22025806")
        number["source"] = "geometry_explicit_order_metadata_v3"
        payload["weak_field_suggestions"]["anchored_fields"].update({
            "order_number": number,
            "order_date": {
                "value": "04/02/26",
                "confidence": 0.995,
                "source": "geometry_anchor_v2",
                "page": 1,
                "bbox": [338, 284, 374, 295],
            },
        })

        out = reconcile_weak_fields(payload)
        date = out["business_extractions"]["purchase_order"]["purchase_order"]["order_date"]

        self.assertEqual(date["value"], "2026-02-04")
        self.assertEqual(date["raw_value"], "04/02/26")
        self.assertTrue(any(
            action.get("action") == "promote_order_date"
            for action in out["weak_field_reconciliation"]["actions"]
        ))

    def test_date_without_same_explicit_order_table_is_not_promoted(self):
        payload = base_payload()
        payload["weak_field_suggestions"]["anchored_fields"].update({
            "order_number": order_candidate("FBC22025806"),
            "order_date": {
                "value": "04/02/26", "confidence": 0.995,
                "source": "geometry_anchor_v2", "page": 1,
            },
        })

        out = reconcile_weak_fields(payload)
        header = out["business_extractions"]["purchase_order"]["purchase_order"]

        self.assertNotIn("order_date", header)

    def test_explicit_geometry_replaces_source_labeled_phone_even_if_core_method_is_strong(self):
        payload = base_payload()
        payload["pages"] = [{
            "page": 1,
            "text": (
                "tel suivi cde 0 820 003 000\n"
                "adresse de livraison ref cde : 06-252422"
            ),
        }]
        header = payload["business_extractions"]["purchase_order"]["purchase_order"]
        header["number"] = {
            "value": "0820003000",
            "evidence": {"extraction_method": "inline_transaction_header"},
        }
        candidate = order_candidate("06-252422")
        payload["weak_field_suggestions"]["anchored_fields"]["order_number"] = candidate

        out = reconcile_weak_fields(payload)
        header = out["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertEqual(header["number"]["value"], "06-252422")
        self.assertEqual(
            header["number_original"]["disqualified_reason"],
            "SOURCE_LABELED_CONTACT_NUMBER",
        )

    def test_explicit_geometry_repairs_lower_precision_multiblock_ocr(self):
        payload = base_payload()
        header = payload["business_extractions"]["purchase_order"]["purchase_order"]
        header["number"] = {
            "value": "PR IA4 93899",
            "evidence": {"extraction_method": "multiblock_order_number"},
        }
        payload["weak_field_suggestions"]["anchored_fields"]["order_number"] = (
            order_candidate("PR N 1A4 93899")
        )

        out = reconcile_weak_fields(payload)
        header = out["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertEqual(header["number"]["value"], "PR N 1A4 93899")
        self.assertEqual(
            header["number_original"]["disqualified_reason"],
            "LOWER_PRECISION_MULTIBLOCK_OCR",
        )

    def test_header_identity_beats_later_body_n_de_commande(self):
        payload = base_payload()
        header = payload["business_extractions"]["purchase_order"]["purchase_order"]
        header["number"] = {
            "value": "25021487",
            "evidence": {"extraction_method": "explicit_n_de_commande"},
        }
        header["internal_number"] = {"value": "FBD29406"}
        payload["weak_field_suggestions"]["anchored_fields"]["order_number"] = (
            order_candidate("FBD29406")
        )

        out = reconcile_weak_fields(payload)
        header = out["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertEqual(header["number"]["value"], "FBD29406")
        self.assertEqual(
            header["number_original"]["disqualified_reason"],
            "LATER_BODY_ORDER_LABEL_CONFLICTS_WITH_HEADER",
        )

    def test_instruction_party_is_replaced_by_clean_department_party(self):
        payload = base_payload()
        candidate = ship_candidate(
            "par mail a l'adresse", "80", "RUE", "BARBERIS", "06300", "NICE",
        )
        candidate["department"] = "FGP"
        payload["weak_field_suggestions"]["address_candidates"] = [candidate]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(selected[0]["party_name"], "FGP")
        self.assertIsNone(selected[0].get("department"))

    def test_truncated_geometry_prefix_does_not_replace_full_multiblock_number(self):
        payload = base_payload()
        header = payload["business_extractions"]["purchase_order"]["purchase_order"]
        header["number"] = {
            "value": "PR 7 604 52249",
            "evidence": {"extraction_method": "multiblock_order_number"},
        }
        payload["weak_field_suggestions"]["anchored_fields"]["order_number"] = (
            order_candidate("PR 7 604")
        )

        out = reconcile_weak_fields(payload)
        header = out["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertEqual(header["number"]["value"], "PR 7 604 52249")
        self.assertNotIn("number_original", header)

    def test_explicit_geometry_never_replaces_strong_core_order_title_number(self):
        payload = base_payload()
        header = payload["business_extractions"]["purchase_order"]["purchase_order"]
        header["number"] = {
            "value": "5405637 /1877",
            "evidence": {
                "extraction_method": "explicit_attached_numero_below_order_title",
            },
        }
        payload["weak_field_suggestions"]["anchored_fields"]["order_number"] = (
            order_candidate("124 126")
        )

        out = reconcile_weak_fields(payload)
        header = out["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertEqual(header["number"]["value"], "5405637 /1877")
        self.assertNotIn("number_original", header)
        self.assertFalse(any(
            action.get("action") == "promote_order_number"
            for action in out.get("reconciliation_actions") or []
        ))

    def test_geometry_prefix_never_truncates_reconstructed_composite_number(self):
        payload = base_payload()
        header = payload["business_extractions"]["purchase_order"]["purchase_order"]
        header["number"] = {
            "value": "5416718 /1877",
            "evidence": {
                "page": 1,
                "bbox": [20, 125, 450, 146],
                "source_text": "5416718 5416718 / 1877 1877",
                "extraction_method": "po_number_composite_duplicate_cleanup_v60",
            },
        }
        payload["weak_field_suggestions"]["anchored_fields"]["order_number"] = (
            order_candidate("5416718")
        )

        out = reconcile_weak_fields(payload)

        self.assertEqual(header["number"]["value"], "5416718 /1877")
        self.assertNotIn("number_original", header)
        self.assertFalse(any(
            action.get("action") == "promote_order_number"
            for action in out.get("reconciliation_actions") or []
        ))

    def test_scheduling_sentence_is_never_promoted_as_order_number(self):
        payload = base_payload()
        payload["weak_field_suggestions"]["anchored_fields"]["order_number"] = (
            order_candidate("Reprise le 05 Janvier 2026.")
        )
        out = reconcile_weak_fields(payload)
        header = out["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertNotIn("number", header)

    def test_unique_complete_delivery_candidate_is_promoted(self):
        payload = base_payload()
        po = payload["business_extractions"]["purchase_order"]
        po["business_addresses"] = [
            address_block("ship_to", "PPC", None, "rue Ferdinand de Lesseps", "33693", "Merignac")
        ]
        candidate = ship_candidate("PPC", "2650", "avenue", "de Maurin", "34070", "Montpellier")
        payload["weak_field_suggestions"]["address_candidates"] = [candidate]

        out = reconcile_weak_fields(payload)
        addresses = out["business_extractions"]["purchase_order"]["business_addresses"]
        selected = [item for item in addresses if item["role"] == "ship_to"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["address"]["postal_code"], "34070")
        self.assertEqual(addresses[0]["role"], "unknown")

    def test_candidate_matching_supplier_is_never_promoted(self):
        payload = base_payload()
        po = payload["business_extractions"]["purchase_order"]
        po["business_addresses"] = [
            address_block("supplier", "ELM", "126", "rue de Stalingrad", "93700", "Drancy")
        ]
        payload["weak_field_suggestions"]["address_candidates"] = [
            ship_candidate("REGMATHERM", "126", "rue", "de Stalingrad", "93700", "Drancy")
        ]

        out = reconcile_weak_fields(payload)
        addresses = out["business_extractions"]["purchase_order"]["business_addresses"]
        self.assertFalse(any(item["role"] == "ship_to" for item in addresses))

    def test_explicit_ship_to_can_correct_same_party_mislabeled_as_supplier(self):
        payload = base_payload()
        po = payload["business_extractions"]["purchase_order"]
        po["business_addresses"] = [
            address_block("supplier", "ORVIF ORLY", "32", "chemin des Carrières", "94310", "Orly")
        ]
        payload["weak_field_suggestions"]["address_candidates"] = [
            ship_candidate("ORVIF ORLY", "32", "chemin", "des Carrières", "94310", "Orly")
        ]

        out = reconcile_weak_fields(payload)
        addresses = out["business_extractions"]["purchase_order"]["business_addresses"]
        selected = [item for item in addresses if item["role"] == "ship_to"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["address"]["postal_code"], "94310")

    def test_distinct_geometry_supplier_prevents_delivery_column_veto(self):
        payload = base_payload()
        po = payload["business_extractions"]["purchase_order"]
        po["business_addresses"] = [
            address_block(
                "buyer", "ELM LEBLANC / BOSCH", "42",
                "avenue Francois Mitterrand", "59494", "Petite Foret",
            ),
            address_block(
                "supplier", "ELM LEBLANC BOSCH", "42",
                "avenue Francois Mitterrand", "59494", "Petite Foret",
            ),
            address_block(
                "ship_to", "EUROPE SANITAIRE CHAUFFAGE", "42",
                "avenue Francois Mitterrand", "59494", "Petite Foret",
            ),
        ]
        payload["weak_field_suggestions"]["address_candidates"] = [
            ship_candidate(
                "EUROPE SANITAIRE CHAUFFAGE", "42", "avenue",
                "Francois Mitterrand", "59494", "Petite Foret",
            ),
            {
                **ship_candidate(
                    None, "124-126", "rue", "de Stalingrad", "93711", "Drancy",
                ),
                "role": "supplier",
            },
        ]

        out = reconcile_weak_fields(payload)
        selected = [
            item
            for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "EUROPE SANITAIRE CHAUFFAGE")
        self.assertEqual(selected[0]["address"]["postal_code"], "59494")

    def test_ship_to_is_enriched_from_richer_equivalent_duplicate(self):
        payload = base_payload()
        payload["weak_field_suggestions"]["address_candidates"] = [
            ship_candidate(None, "12", "Rue", "Louise Michel", "69320", "Feyzin"),
            {
                **ship_candidate(None, "12", "Rue", "Louise Michel", "69320", "Feyzin"),
                "role": "unknown",
                "components": {
                    "house_number": "12",
                    "street_type": "Rue",
                    "street_name": "Louise Michel",
                    "building": "THERMLOG Bâtiment F",
                    "postal_code": "69320",
                    "city": "Feyzin",
                },
                "formatted_address_suggestion": (
                    "THERMLOG Bâtiment F, 12 Rue Louise Michel, 69320 Feyzin"
                ),
            },
        ]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["address"]["building"], "THERMLOG Bâtiment F")
        self.assertIn("Bâtiment F", selected[0]["formatted_address"])

    def test_distant_buyer_copy_does_not_pollute_explicit_delivery(self):
        delivery = ship_candidate(
            "IZI confort Amiens", "7", "rue", "Ambroise Croizat",
            "80450", "CAMON",
        )
        delivery["source_line"] = 16
        buyer_copy = {
            **ship_candidate(
                None, "7", "rue", "Ambroise Croizat", "80450", "CAMON",
            ),
            "role": "unknown",
            "source_line": 1,
        }
        buyer_copy["components"].update({
            "building": "Lot 118 - ZA La Blanche Tâche",
            "industrial_zone": "ZA La Blanche Tâche",
        })
        payload = base_payload()
        payload["weak_field_suggestions"]["address_candidates"] = [
            buyer_copy, delivery,
        ]

        out = reconcile_weak_fields(payload)
        selected = next(
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        )

        self.assertNotIn("building", selected["address"])
        self.assertNotIn("industrial_zone", selected["address"])

    def test_same_site_longer_street_occurrence_completes_delivery_street(self):
        payload = base_payload()
        partial = ship_candidate(
            "SAS", "211-213", "AVENUE", "MAURICE", "78500", "SARTROUVILLE"
        )
        partial["department"] = "AU FORUM DU BATIMENT"
        partial["components"]["building"] = "AU FORUM DU BATIMENT"
        complete = {
            **ship_candidate(
                None, "211-213", "AVENUE", "MAURICE BERTEAUX",
                "78500", "SARTROUVILLE",
            ),
            "role": "unknown",
        }
        payload["weak_field_suggestions"]["address_candidates"] = [partial, complete]

        out = reconcile_weak_fields(payload)
        selected = [
            item
            for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["address"]["street_name"], "MAURICE BERTEAUX")
        self.assertIn("MAURICE BERTEAUX", selected[0]["formatted_address"])

    def test_interleaved_source_candidate_never_contaminates_clean_geometry(self):
        payload = base_payload()
        clean = ship_candidate(
            "MAGASIN BIO HABITAT U1", "257", "AVENUE", "DES ALBIZIAS",
            "85210", "SAINTE-HERMINE",
        )
        clean["department"] = "VENDEOPOLE ATLANTIQUE"
        payload["raw_text"] = (
            "Adresse de livraison ELM LEBLANC SAS\n"
            "Magasin BIO habitat U1 124-126 RUE DE STALINGRAD\n"
            "257 Avenue des Albizias 93711 FRANCE Drancy CEDEX\n"
            "85210 SAINTE-HERMINE\nFRANCE"
        )
        payload["weak_field_suggestions"]["address_candidates"] = [clean]

        out = reconcile_weak_fields(payload)
        selected = next(
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        )
        self.assertEqual(selected["address"]["street_name"], "DES ALBIZIAS")
        self.assertNotIn("93711", selected["formatted_address"])
        self.assertNotIn("DRANCY", selected["formatted_address"].upper())

    def test_second_numbered_street_is_not_appended_to_delivery_street(self):
        payload = base_payload()
        clean = ship_candidate(
            "VERNEY SA", "28", "RUE", "DE MAYENCE ZAE CAPNORD",
            "21076", "DIJON",
        )
        polluted = {
            **ship_candidate(
                None, "28", "RUE", "DE MAYENCE ZAE CAPNORD 3 RUE DAGUERRE",
                "21076", "DIJON",
            ),
            "role": "unknown",
        }
        polluted["components"]["bp"] = "BP 77605"
        payload["weak_field_suggestions"]["address_candidates"] = [clean, polluted]

        out = reconcile_weak_fields(payload)
        selected = next(
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        )
        self.assertEqual(selected["address"]["street_name"], "DE MAYENCE ZAE CAPNORD")
        self.assertEqual(selected["address"]["bp"], "BP 77605")
        self.assertNotIn("DAGUERRE", selected["formatted_address"])

    def test_reordered_abbreviated_city_uses_clean_duplicate_locality(self):
        payload = base_payload()
        delivery = ship_candidate(
            "PROLIANS BA ST ANDRE SANG", "7", "RUE", "DES CHENES VERTS",
            "34725", "DE SANGONIS ST ANDRE",
        )
        duplicate = {
            **ship_candidate(
                None, "7", "RUE", "DES CHENES VERTS",
                "34725", "SAINT-ANDRE-DE-SANGONIS",
            ),
            "role": "supplier",
        }
        payload["weak_field_suggestions"]["address_candidates"] = [delivery, duplicate]

        out = reconcile_weak_fields(payload)
        selected = next(
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        )
        self.assertEqual(selected["address"]["city"], "SAINT-ANDRE-DE-SANGONIS")

    def test_two_street_lines_at_one_site_are_merged_not_marked_ambiguous(self):
        payload = base_payload()
        payload["weak_field_suggestions"]["address_candidates"] = [
            ship_candidate("SIDV TARBES", "1", "Rue", "de la Garounère", "65000", "TARBES"),
            ship_candidate("SIDV TARBES", None, "Route", "de Pau", "65000", "TARBES"),
        ]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["address"]["house_number"], "1")
        self.assertEqual(selected[0]["address"]["address_complement"], "Route de Pau")
        self.assertIn("Route de Pau", selected[0]["formatted_address"])

    def test_explicit_delivery_reminder_recovers_clean_address_from_scan_text(self):
        payload = base_payload()
        payload["raw_text"] = (
            "COMMANDE FOURNISSEUR\n"
            "Adresse de livraison\n"
            "DESENFANS CAMBRAI LOGISTIQUE 85 ROUTE DE BAPAUME\n"
            "594D0 CAMBRAT\n"
            "NOUS VOUS RAPPELONS QUE L'ADRESSE DE LIVRAISON EST\n"
            "DESENFANS 83 ROUTE DE BAPAUME 59400 CAMBRAI\n"
            "VEUILLEZ NOUS RETOURNER L'ACCUSE RECEPTION"
        )
        payload["weak_field_suggestions"]["address_candidates"] = [
            ship_candidate("594D0CAMBRAT", "85", "ROUTE", "DE BAPAUME")
        ]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "DESENFANS")
        self.assertEqual(selected[0]["address"]["house_number"], "83")
        self.assertEqual(selected[0]["address"]["postal_code"], "59400")
        self.assertEqual(selected[0]["address"]["city"], "CAMBRAI")

    def test_address_below_delivery_label_wins_over_damaged_geometry_party(self):
        payload = base_payload()
        payload["raw_text"] = (
            "COMMANDE 5364731\n"
            "A livrer a l'adresse ci-dessous :\n"
            "PPC\n"
            "28 boulevard Lenine\n"
            "76800 ST ETIENNE DU ROUVRAY\n"
            "FRANCE\n"
            "Tel. : 02 78 26 00 00"
        )
        damaged = ship_candidate(
            "FRANCE", "28", "boulevard", "Lenine", "76800", "ST ETIENNE DU ROUVRAY"
        )
        damaged["confidence"] = 0.99
        payload["weak_field_suggestions"]["address_candidates"] = [damaged]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "PPC")
        self.assertEqual(selected[0]["address"]["postal_code"], "76800")

    def test_attached_bis_house_number_is_recovered_from_delivery_block(self):
        payload = base_payload()
        payload["raw_text"] = (
            "COMMANDE 5486309\n"
            "A livrer a l'adresse ci-dessous :\n"
            "PIECES DETACHEES\n"
            "2bis Rue Ferdinand de Lesseps\n"
            "33700 MERIGNAC\n"
            "FRANCE\n"
            "Reglement : VIR 30 jours"
        )

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "PIECES DETACHEES")
        self.assertEqual(selected[0]["address"]["house_number"].casefold(), "2bis")
        self.assertEqual(selected[0]["address"]["postal_code"], "33700")

    def test_multiline_delivery_block_keeps_industrial_zone(self):
        payload = base_payload()
        payload["raw_text"] = (
            "A livrer a l'adresse ci-dessous :\n"
            "PPC\n"
            "2 RUE DE MAYENCIN\n"
            "ZI CHAMP ROMAN\n"
            "38400 ST MARTIN D'HERES\n"
            "FRANCE\n"
            "Reglement : VIR 30 jours"
        )

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "PPC")
        self.assertEqual(selected[0]["address"]["industrial_zone"], "ZI CHAMP ROMAN")
        self.assertEqual(selected[0]["address"]["postal_code"], "38400")

    def test_delivery_block_is_recovered_from_public_page_text(self):
        payload = base_payload()
        payload["pages"] = [{
            "page": 1,
            "text": (
                "COMMANDE 5434727\n"
                "A livrer a l'adresse ci-dessous :\n"
                "PPC\n"
                "2650 AVENUE DE MAURIN\n"
                "34070 MONTPELLIER\n"
                "FRANCE\n"
                "Tel. : 04 67 54 67 78"
            ),
        }]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "PPC")
        self.assertEqual(selected[0]["address"]["house_number"], "2650")
        self.assertEqual(selected[0]["address"]["postal_code"], "34070")

    def test_delivery_block_normalizes_visually_spaced_postal_code(self):
        payload = base_payload()
        payload["pages"] = [{
            "page": 1,
            "text": (
                "COMMANDE Bosch pieces de rechange\n"
                "Adresse de livraison\n"
                "IZI confort Agen\n"
                "Avenue du Bruilhois\n"
                "47 520 LE PASSAGE D'AGEN\n"
                "Correspondant: Damien LETURGEON"
            ),
        }]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "IZI confort Agen")
        self.assertEqual(selected[0]["address"]["street"], "Avenue du Bruilhois")
        self.assertEqual(selected[0]["address"]["postal_code"], "47520")
        self.assertEqual(selected[0]["address"]["city"], "LE PASSAGE D'AGEN")

    def test_delivery_place_keeps_party_zone_and_departmental_road(self):
        payload = base_payload()
        payload["pages"] = [{
            "page": 1,
            "text": (
                "COMMANDE N° 674072553\n"
                "LIEU DE LIVRAISON\n"
                "ANCONETTI CENTRALE\n"
                "ZA Val de L'ARC HEURE DE RECEPTION\n"
                "Départementale 56 7h00 12h00\n"
                "13790 CHATEAUNEUF -LE-ROUGE"
            ),
        }]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "ANCONETTI CENTRALE")
        self.assertEqual(selected[0]["address"]["industrial_zone"], "ZA Val de L'ARC")
        self.assertEqual(selected[0]["address"]["street"], "Départementale 56")
        self.assertEqual(selected[0]["address"]["postal_code"], "13790")

    def test_inline_delivery_place_party_outranks_following_mode_label(self):
        payload = base_payload()
        payload["pages"] = [{
            "page": 1,
            "text": (
                "C O M M A N D E\n"
                "N. : 009-2460-220126\n"
                "LIEU DE LIVRAISON : RICHARDSON AG. DE IRIGNY\n"
                "Z.I. 26 RUE DE LA MOUCHE 69540 IRIGNY\n"
                "MODE D'EXPEDITION :"
            ),
        }]
        payload["weak_field_suggestions"]["address_candidates"] = [
            ship_candidate("MODE D'EXPEDITION", "26", "RUE", "DE LA MOUCHE", "69540", "IRIGNY")
        ]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "RICHARDSON AG. DE IRIGNY")
        self.assertEqual(selected[0]["address"]["postal_code"], "69540")

    def test_interleaved_erp_delivery_columns_recover_complete_address(self):
        payload = base_payload()
        payload["pages"] = [{
            "page": 1,
            "text": (
                "COMMANDE FOURNISSEUR\n"
                "Port avancé hdresse de livraison\n"
                "Type livraison. Livre GERONDEAU\n"
                "Délai souhaité 23 4 26 2123 ROUTE NATIONALE\n"
                "Société GERONDEAU Sanitaire Chauffage\n"
                "Devise EUR 45774 SARAN CEDEX FRANCE\n"
                "CODE DESIGNATION QUANTITE PU NET"
            ),
        }]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "GERONDEAU")
        self.assertEqual(selected[0]["address"]["house_number"], "2123")
        self.assertEqual(selected[0]["address"]["street"], "ROUTE NATIONALE")
        self.assertEqual(selected[0]["address"]["postal_code"], "45774")
        self.assertEqual(selected[0]["address"]["city"], "SARAN CEDEX")

    def test_delivery_label_without_de_strips_inline_phone_fax_and_page(self):
        payload = base_payload()
        payload["pages"] = [{
            "page": 1,
            "text": (
                "Adresse Livraison: Cd Rglt\n"
                "PROLIANS DP ORANGE Tél: 04 90 00 00 00\n"
                "585 AVENUE DE VERDUN Fax: 04 90 00 00 01\n"
                "VIREMENT\n"
                "ORANGE\n"
                "84100 ORANGE PAGE 1\n"
                "DESIGNATION QUANTITE PRIX"
            ),
        }]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "PROLIANS DP ORANGE")
        self.assertEqual(selected[0]["address"]["house_number"], "585")
        self.assertEqual(selected[0]["address"]["street"], "AVENUE DE VERDUN")
        self.assertEqual(selected[0]["address"]["postal_code"], "84100")
        self.assertEqual(selected[0]["address"]["city"], "ORANGE")
        self.assertNotIn("address_complement", selected[0]["address"])

    def test_delivery_block_parses_post_box_zone_and_page_without_number(self):
        payload = base_payload()
        payload["pages"] = [{
            "page": 1,
            "text": (
                "Adresse Livraison: Cd Rglt: 030 J LE 15\n"
                "PROLIANS DP TOULON Tél: 0820003000\n"
                "CPS Fax: 0143117317\n"
                "391 Avenue J.L LAMBOT\n"
                "BP 67 Z.I. EST VIREMENT\n"
                "TOULON\n"
                "83079 TOULON PAGE\n"
                "DESIGNATION QUANTITES DELAI MONTANT"
            ),
        }]
        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "PROLIANS DP TOULON")
        self.assertEqual(selected[0]["address"]["house_number"], "391")
        self.assertEqual(selected[0]["address"]["street"], "Avenue J.L LAMBOT")
        self.assertEqual(selected[0]["address"]["po_box"], "BP 67")
        self.assertEqual(selected[0]["address"]["industrial_zone"], "Z.I. EST")
        self.assertEqual(selected[0]["address"]["postal_code"], "83079")

    def test_explicit_delivery_block_ignores_footer_geometry_false_positives(self):
        payload = base_payload()
        payload["pages"] = [{
            "page": 1,
            "text": (
                "A livrer a l'adresse ci-dessous :\n"
                "PPC\n"
                "2 RUE DE MAYENCIN\n"
                "ZI CHAMP ROMAN\n"
                "38400 ST MARTIN D'HERES\n"
                "FRANCE\n"
                "Siege social : 4 rue Ferdinand de Lesseps 33693 Merignac Cedex"
            ),
        }]
        payload["weak_field_suggestions"]["address_candidates"] = [
            ship_candidate("PPC", "2", "RUE", "DE MAYENCIN", "38400", "ST MARTIN D'HERES"),
            ship_candidate("Siege social", "4", "RUE", "Ferdinand de Lesseps", "33693", "Merignac"),
        ]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "PPC")
        self.assertEqual(selected[0]["address"]["postal_code"], "38400")
        self.assertEqual(selected[0]["address"]["industrial_zone"], "ZI CHAMP ROMAN")

    def test_explicit_delivery_block_accepts_comma_after_house_number(self):
        payload = base_payload()
        payload["pages"] = [{
            "page": 1,
            "text": (
                "A livrer a l'adresse ci-dessous :\n"
                "PIECES DETACHEES\n"
                "22, Rue de la Retardais\n"
                "35000 RENNES\n"
                "FRANCE"
            ),
        }]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "PIECES DETACHEES")
        self.assertEqual(selected[0]["address"]["house_number"], "22")
        self.assertEqual(selected[0]["address"]["postal_code"], "35000")

    def test_incomplete_ship_hint_can_promote_one_matching_buyer(self):
        payload = base_payload()
        po = payload["business_extractions"]["purchase_order"]
        po["business_addresses"] = [
            address_block("buyer", "VERNEY SA", "28", "rue de Mayence", "21076", "Dijon")
        ]
        payload["weak_field_suggestions"]["address_candidates"] = [
            ship_candidate("VERNEY SA", "28", "rue", "de Mayence")
        ]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["address"]["postal_code"], "21076")
        self.assertIn("corroborated_by_ship_to_geometry", selected[0]["warnings"])

    def test_explicit_geometry_replaces_truncated_house_number(self):
        payload = base_payload()
        po = payload["business_extractions"]["purchase_order"]
        po["business_addresses"] = [
            address_block("ship_to", "PPC", "5", "avenue de Fondeyre", "31200", "Toulouse")
        ]
        payload["weak_field_suggestions"]["address_candidates"] = [
            ship_candidate("PPC", "3 et 5", "avenue", "de Fondeyre", "31200", "Toulouse")
        ]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["address"]["house_number"], "3 et 5")

    def test_geometry_wins_over_interleaved_billing_and_delivery_source_block(self):
        payload = base_payload()
        payload["pages"] = [{
            "page": 1,
            "text": (
                "Facturé à : Adresse de livraison : Merci de livrer si dispo\n"
                "ISERBA ISERBA\n"
                "2 RUE DE LA MAIRIE\n"
                "ZAC des Malettes\n"
                "303 rue du Chat Botté\n"
                "CS 10412 03400 TOULON SUR ALLIER\n"
                "01704 BEYNOST CEDEX\n"
                "France"
            ),
        }]
        polluted = address_block(
            "ship_to", "ISERBA ISERBA", "2", "RUE DE LA MAIRIE", "03400", "TOULON SUR ALLIER"
        )
        polluted["address"]["industrial_zone"] = "ZAC des Malettes"
        polluted["address"]["address_complement"] = "CS 10412 03400 TOULON SUR ALLIER"
        polluted["formatted_address"] = (
            "2 RUE DE LA MAIRIE, ZAC des Malettes, CS 10412 03400 TOULON SUR ALLIER, "
            "01704 BEYNOST CEDEX"
        )
        payload["business_extractions"]["purchase_order"]["business_addresses"] = [polluted]
        payload["weak_field_suggestions"]["address_candidates"] = [
            ship_candidate("ISERBA", "2", "RUE", "DE LA MAIRIE", "03400", "TOULON SUR ALLIER")
        ]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "ISERBA")
        self.assertEqual(selected[0]["address"]["postal_code"], "03400")
        self.assertNotIn("industrial_zone", selected[0]["address"])
        self.assertNotIn("01704", selected[0]["formatted_address"])

    def test_geometry_removes_product_lines_but_keeps_named_delivery_contact(self):
        payload = base_payload()
        polluted = address_block(
            "ship_to", "SYLVAIN JOUBERT", "6", "IMPASSE DU PALATIN", "42110", "FEURS"
        )
        polluted["department"] = "INSTALLATEUR SVP / ALLO C'EST LE PLOMBIER"
        polluted["address"]["address_complement"] = (
            "BOSCH - ELM PIECES 87168525180 POMPE | UPM3 | N° INTERNE : 4 425 951 | PORT"
        )
        polluted["formatted_address"] = (
            "6 IMPASSE DU PALATIN, BOSCH - ELM PIECES 87168525180 POMPE, 42110 FEURS"
        )
        payload["business_extractions"]["purchase_order"]["business_addresses"] = [polluted]
        payload["weak_field_suggestions"]["address_candidates"] = [
            ship_candidate("SOROFI SAS", "6", "IMPASSE", "DU PALATIN", "42110", "FEURS")
        ]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["party_name"], "SYLVAIN JOUBERT")
        self.assertEqual(selected[0]["department"], "INSTALLATEUR SVP / ALLO C'EST LE PLOMBIER")
        self.assertNotIn("address_complement", selected[0]["address"])
        self.assertNotIn("87168525180", selected[0]["formatted_address"])

    def test_page_one_order_with_following_terms_gets_order_container(self):
        payload = {
            "document": {
                "primary_document_type": "legal_terms",
                "detected_document_type": "legal_terms",
                "page_classifications": [{
                    "page": 1,
                    "type": "generic_document",
                    "signals": ["n° commande", "commande-near-top", "item-like-identifiers"],
                }],
            },
            "business_extractions": {"terms_and_conditions": {}},
            "weak_field_suggestions": {
                "anchored_fields": {"order_number": order_candidate("A2503480")},
                "address_candidates": [],
            },
        }
        out = reconcile_weak_fields(payload)
        self.assertEqual(out["document"]["primary_document_type"], "purchase_order_with_terms")
        po = out["business_extractions"]["purchase_order"]
        self.assertEqual(po["purchase_order"]["number"]["value"], "A2503480")


    def test_reordered_city_on_repeated_pages_is_one_delivery_site(self):
        payload = base_payload()
        page_one = ship_candidate(
            "PROLIANS DP TOULON", "391", "AV", "JL LAMBOT", "83130", "LA GARDE"
        )
        page_two = ship_candidate(
            "PROLIANS DP TOULON", "391", "AV", "JL LAMBOT", "83130", "GARDE LA"
        )
        page_two["page"] = 2
        payload["weak_field_suggestions"]["address_candidates"] = [page_one, page_two]

        out = reconcile_weak_fields(payload)
        selected = [
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        ]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["address"]["city"], "LA GARDE")

    def test_existing_address_like_party_does_not_replace_geometry_recipient(self):
        payload = base_payload()
        existing = address_block(
            "ship_to", "3 et 5 avenue de Fondeyre", None,
            "avenue de Fondeyre", "31200", "TOULOUSE",
        )
        existing["department"] = "PPC"
        payload["business_extractions"]["purchase_order"]["business_addresses"] = [existing]
        candidate = ship_candidate(
            "PPC", "3 et 5", "avenue", "de Fondeyre", "31200", "TOULOUSE"
        )
        payload["weak_field_suggestions"]["address_candidates"] = [candidate]

        out = reconcile_weak_fields(payload)
        selected = next(
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        )
        self.assertEqual(selected["party_name"], "PPC")
        self.assertNotEqual(selected.get("department"), "PPC")

    def test_clean_geometry_department_is_not_overwritten_by_polluted_core_value(self):
        payload = base_payload()
        existing = address_block(
            "ship_to", "BIO HABITAT MAG SL SAV", None,
            "Chemin du Parois", "85300", "CHALLANS",
        )
        existing["department"] = "EX.SPBI / 30 Chem. du Parois"
        existing["address"]["address_complement"] = "CONDITIONS DE LIVRAISON"
        payload["business_extractions"]["purchase_order"]["business_addresses"] = [existing]
        candidate = ship_candidate(
            "BIO HABITAT MAG SL SAV", "30", "Chemin", "du Parois", "85300", "CHALLANS"
        )
        candidate["department"] = "EX.SPBI"
        payload["weak_field_suggestions"]["address_candidates"] = [candidate]

        out = reconcile_weak_fields(payload)
        selected = next(
            item for item in out["business_extractions"]["purchase_order"]["business_addresses"]
            if item["role"] == "ship_to"
        )
        self.assertEqual(selected["department"], "EX.SPBI")


if __name__ == "__main__":
    unittest.main()

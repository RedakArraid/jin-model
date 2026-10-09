import unittest

import fitz

from jin_runtime.geometry_fields import extract_geometry_suggestions
from jin_runtime.weak_field_router import _extract_first_page_lines


class GeometryFieldTests(unittest.TestCase):
    @staticmethod
    def make_pdf() -> bytes:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)

        page.insert_text((40, 100), "N° de commande :")
        page.insert_text((125, 100), "CAC2410HAR00035")
        page.insert_text((40, 120), "Date :")
        page.insert_text((125, 120), "18/10/2024")

        page.insert_text((286, 150), "124 RUE DE STALINGRAD")
        page.insert_text((286, 170), "93700")
        page.insert_text((358, 173), "DRANCY")

        page.insert_text((286, 220), "Adresse de livraison :")
        page.insert_text((286, 240), "ZA DE ROGERVILLE")
        page.insert_text((286, 255), "1 CHEMIN DES PLANS D'EAU")
        page.insert_text((286, 280), "76430")
        page.insert_text((358, 283), "OUDALLE")

        page.insert_text((40, 220), "Facture a :")
        page.insert_text((40, 240), "ZAC des Malettes")
        page.insert_text((40, 255), "303 rue du Chat Botte")
        page.insert_text((40, 275), "CS 10412")
        page.insert_text((40, 295), "01704 BEYNOST CEDEX")

        page.insert_text((425, 500), "Total HT :")
        page.insert_text((502, 500), "6")
        page.insert_text((512, 500), "638,00 EUR")
        page.insert_text((500, 520), "215,00 EUR")

        data = doc.tobytes()
        doc.close()
        return data

    def test_cross_block_visual_lines_restore_fields_and_localities(self):
        lines, source = _extract_first_page_lines(self.make_pdf(), "fra+eng+deu")
        self.assertEqual(source, "native_pdf_text")

        result = extract_geometry_suggestions(lines)
        fields = result["anchored_fields"]
        self.assertEqual(fields["order_number"]["value"], "CAC2410HAR00035")
        self.assertEqual(fields["order_date"]["value"], "18/10/2024")
        self.assertEqual(fields["total_net"]["value"], "6638,00")

        by_role = {item["role"]: item for item in result["address_candidates"]}
        self.assertEqual(by_role["supplier"]["components"]["postal_code"], "93700")
        self.assertEqual(by_role["supplier"]["components"]["city"], "DRANCY")
        self.assertEqual(by_role["ship_to"]["components"]["postal_code"], "76430")
        self.assertEqual(by_role["ship_to"]["components"]["city"], "OUDALLE")
        self.assertEqual(
            by_role["ship_to"]["components"]["industrial_zone"],
            "ZA DE ROGERVILLE",
        )
        self.assertEqual(by_role["bill_to"]["components"]["postal_code"], "01704")
        self.assertEqual(by_role["bill_to"]["components"]["city"], "BEYNOST")
        self.assertTrue(by_role["bill_to"]["components"]["cedex"])
        self.assertEqual(by_role["bill_to"]["components"]["cs"], "CS 10412")

        for address in result["address_candidates"]:
            self.assertNotIn("215,00", address["formatted_address_suggestion"])

    def test_contact_name_cannot_become_street_without_known_type(self):
        lines, _ = _extract_first_page_lines(self.make_pdf(), "fra+eng+deu")
        result = extract_geometry_suggestions(lines)
        formatted = " | ".join(
            item["formatted_address_suggestion"]
            for item in result["address_candidates"]
        )
        self.assertNotIn("RODAS", formatted)

    @staticmethod
    def make_wendel_pdf() -> bytes:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((330, 150), "124 126 RUE DE STALINGRAD")
        page.insert_text((330, 170), "93711 DRANCY CEDEX")
        page.insert_text((40, 220), "N° Document")
        page.insert_text((150, 220), "Date")
        page.insert_text((40, 240), "CF328595")
        page.insert_text((150, 240), "10/07/2024")
        page.insert_text((215, 260), "A facturer à:")
        page.insert_text((365, 260), "A livrer à:")
        page.insert_text((215, 280), "WENDEL DISTRIBUTION")
        page.insert_text((365, 280), "WENDEL Villeneuve")
        page.insert_text((365, 300), "Route de Bordeaux")
        page.insert_text((215, 300), "CS 50115")
        page.insert_text((215, 320), "47203 MARMANDE Cedex")
        page.insert_text((365, 320), "47300 BIAS")
        page.insert_text((360, 720), "TOTAL H.T")
        page.insert_text((470, 720), "Valeur TVA")
        page.insert_text((550, 720), "TOTAL TTC")
        page.insert_text((390, 742), "66.81")
        page.insert_text((485, 742), "13.36")
        page.insert_text((555, 742), "80.17")
        data = doc.tobytes()
        doc.close()
        return data

    def test_wendel_table_headers_ranges_roles_and_totals(self):
        lines, _ = _extract_first_page_lines(self.make_wendel_pdf(), "fra+eng+deu")
        result = extract_geometry_suggestions(lines)
        fields = result["anchored_fields"]
        self.assertEqual(fields["order_number"]["value"], "CF328595")
        self.assertEqual(fields["order_date"]["value"], "10/07/2024")
        self.assertEqual(fields["total_net"]["value"], "66.81")
        self.assertEqual(fields["total_vat"]["value"], "13.36")
        self.assertEqual(fields["total_gross"]["value"], "80.17")
        by_role = {item["role"]: item for item in result["address_candidates"]}
        self.assertEqual(by_role["supplier"]["components"]["house_number"], "124-126")
        self.assertEqual(by_role["supplier"]["components"]["postal_code"], "93711")
        self.assertEqual(by_role["ship_to"]["components"]["postal_code"], "47300")
        self.assertEqual(by_role["bill_to"]["components"]["postal_code"], "47203")
        self.assertEqual(by_role["bill_to"]["components"]["cs"], "CS 50115")
        self.assertTrue(by_role["bill_to"]["components"]["cedex"])

    @staticmethod
    def make_sisca_pdf() -> bytes:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 80), "Adresse de livraison impérative")
        page.insert_text((40, 100), "PLF SIDV BAYONNE")
        page.insert_text((40, 120), "24 Chemin de Sabalce")
        page.insert_text((40, 140), "64100 BAYONNE")
        page.insert_text((330, 80), "ELM LEBLANC (BOSCH)")
        page.insert_text((330, 120), "124,126 rue de Stalingrad")
        page.insert_text((330, 140), "93711 Drancy Cedex")
        page.insert_text((330, 165), "Adresse de facturation impérative")
        page.insert_text((330, 185), "SISCA")
        page.insert_text((330, 205), "144 ROUTE DE TOULOUSE")
        page.insert_text((330, 225), "65600 SEMEAC")
        page.insert_text((40, 255), "Commande")
        page.insert_text((40, 275), "Date")
        page.insert_text((150, 275), "Pièce")
        page.insert_text((40, 295), "08/08/2024")
        page.insert_text((150, 295), "1662733")
        page.insert_text((210, 700), "NET H.T.")
        page.insert_text((360, 700), "MONTANT TVA")
        page.insert_text((460, 700), "MONTANT TTC")
        page.insert_text((535, 700), "NET A PAYER")
        page.insert_text((220, 725), "275,89")
        page.insert_text((370, 725), "55,18")
        page.insert_text((470, 725), "331,07")
        page.insert_text((540, 725), "331,07")
        data = doc.tobytes()
        doc.close()
        return data

    def test_sisca_piece_summary_totals_and_anchor_bands(self):
        lines, _ = _extract_first_page_lines(self.make_sisca_pdf(), "fra+eng+deu")
        result = extract_geometry_suggestions(lines)
        fields = result["anchored_fields"]
        self.assertEqual(fields["order_number"]["value"], "1662733")
        self.assertEqual(fields["order_date"]["value"], "08/08/2024")
        self.assertEqual(fields["total_net"]["value"], "275,89")
        self.assertEqual(fields["total_vat"]["value"], "55,18")
        self.assertEqual(fields["total_gross"]["value"], "331,07")
        self.assertEqual(fields["amount_due"]["value"], "331,07")
        by_role = {item["role"]: item for item in result["address_candidates"]}
        self.assertEqual(by_role["supplier"]["components"]["house_number"], "124-126")
        self.assertEqual(by_role["supplier"]["components"]["postal_code"], "93711")
        self.assertEqual(by_role["ship_to"]["components"]["city"], "BAYONNE")
        self.assertEqual(by_role["bill_to"]["components"]["city"], "SEMEAC")

    def test_offer_metadata_and_country_prefixed_postal_code(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 40), "elm.leblanc SAS")
        page.insert_text((50, 60), "124-126 rue de Stalingrad")
        page.insert_text((50, 80), "F-93711 Drancy Cedex")
        page.insert_text((50, 160), "Offre de prix")
        page.insert_text((50, 210), "Offre n° : SCA-050126-0857")
        page.insert_text((50, 230), "Date de l'offre : 05/01/2026")
        data = doc.tobytes()
        doc.close()
        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        result = extract_geometry_suggestions(lines)
        fields = result["anchored_fields"]
        self.assertEqual(fields["offer_number"]["value"], "SCA-050126-0857")
        self.assertEqual(fields["offer_date"]["value"], "05/01/2026")
        self.assertNotIn("order_date", fields)
        self.assertTrue(
            any(
                item["components"].get("postal_code") == "93711"
                for item in result["address_candidates"]
            )
        )

    def test_spaced_french_postal_code_is_reconstructed_in_delivery_block(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 80), "Adresse de livraison")
        page.insert_text((40, 100), "IZI confort Montlucon")
        page.insert_text((40, 120), "35 rue de Pasquis")
        page.insert_text((40, 140), "03 100 MONTLUCON")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        result = extract_geometry_suggestions(lines)
        ship_to = next(
            item for item in result["address_candidates"]
            if item["role"] == "ship_to"
        )
        self.assertEqual(ship_to["components"]["postal_code"], "03100")
        self.assertEqual(ship_to["components"]["city"], "MONTLUCON")

    def test_single_left_delivery_anchor_does_not_absorb_right_supplier_column(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((28, 150), "Adresse de livraison")
        page.insert_text((28, 165), "BIO habitat Beaucaire U4")
        page.insert_text((28, 174), "Route de la Brasserie")
        page.insert_text((28, 183), "30300 Beaucaire")
        page.insert_text((278, 150), "ELM LEBLANC SAS")
        page.insert_text((278, 171), "124-126 RUE DE STALINGRAD")
        page.insert_text((278, 180), "93711 Drancy CEDEX")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        ship_to = [item for item in addresses if item["role"] == "ship_to"]
        self.assertEqual(len(ship_to), 1)
        self.assertEqual(ship_to[0]["components"]["postal_code"], "30300")
        self.assertEqual(ship_to[0]["components"]["city"], "Beaucaire")
        right_column = next(
            item for item in addresses
            if item["components"].get("postal_code") == "93711"
        )
        self.assertNotEqual(right_column["role"], "ship_to")

    def test_delivery_comment_does_not_become_role_anchor_or_absorb_supplier(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((78, 74), "Adresse de livraison impérative")
        page.insert_text((314, 74), "ELM LEBLANC (BOSCH)")
        page.insert_text((34, 90), "SIDV LOURDES")
        page.insert_text((314, 100), "124,126 rue de Stalingrad")
        page.insert_text((32, 118), "18 Avenue François Abadie")
        page.insert_text((314, 122), "93711 Drancy Cedex - FRANCE")
        page.insert_text((32, 132), "65100 LOURDES")
        page.insert_text((368, 154), "Adresse de facturation impérative")
        page.insert_text((340, 170), "SISCA")
        page.insert_text((340, 194), "144 ROUTE DE TOULOUSE")
        page.insert_text((340, 208), "65600 SEMEAC - FRANCE")
        page.insert_text((111, 362), "urgent svp, à livrer au plus vite")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        ship_to = [item for item in addresses if item["role"] == "ship_to"]
        self.assertEqual(len(ship_to), 1)
        self.assertEqual(ship_to[0]["components"]["postal_code"], "65100")
        supplier = next(
            item for item in addresses
            if item["components"].get("postal_code") == "93711"
        )
        self.assertEqual(supplier["role"], "supplier")

    def test_reference_column_before_billing_heading_keeps_delivery_column_separate(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((34, 250), "Référence:")
        page.insert_text((215, 250), "A facturer à:")
        page.insert_text((364, 250), "A livrer à:")
        page.insert_text((218, 270), "WENDEL DISTRIBUTION")
        page.insert_text((369, 270), "WENDEL Marmande")
        page.insert_text((369, 282), "Av François MITTERRAND")
        page.insert_text((218, 296), "CS 50115")
        page.insert_text((369, 296), "BP 115")
        page.insert_text((218, 307), "47203 MARMANDE Cedex")
        page.insert_text((369, 307), "47200 MARMANDE")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        ship_to = next(item for item in addresses if item["role"] == "ship_to")
        self.assertEqual(ship_to["party_name"], "WENDEL Marmande")
        self.assertEqual(ship_to["components"]["postal_code"], "47200")
        self.assertEqual(ship_to["components"]["bp"], "BP 115")
        self.assertNotIn("cs", ship_to["components"])

    def test_short_po_box_zone_department_and_ocr_quote_are_preserved_cleanly(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((80, 250), "Adresse Livraison:")
        page.insert_text((24, 270), "PROLIANS DP TOULON")
        page.insert_text((24, 282), "CPS")
        page.insert_text((24, 294), "391 Avenue J.L LAMBOT")
        page.insert_text((24, 306), "BP 67 Z.I. EST")
        page.insert_text((24, 318), "‘TOULON")
        page.insert_text((24, 330), "83079 ‘TOULON")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        ship_to = next(item for item in addresses if item["role"] == "ship_to")
        self.assertEqual(ship_to["party_name"], "PROLIANS DP TOULON")
        self.assertEqual(ship_to["department"], "CPS")
        self.assertEqual(ship_to["components"]["bp"], "BP 67")
        self.assertEqual(ship_to["components"]["industrial_zone"], "Z.I. EST")
        self.assertEqual(ship_to["components"]["city"], "TOULON")

    def test_late_billing_anchor_does_not_shrink_earlier_delivery_column(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((404, 176), "Adresse de livraison")
        page.insert_text((313, 202), "ORVIF ORLY")
        page.insert_text((313, 216), "32 CHEMIN DES CARRIERES")
        page.insert_text((313, 230), "94310 ORLY")
        page.insert_text((271, 705), "MERCI D'ENVOYER LA FACTURE A LA COMPTABILITE")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        delivery = next(
            item for item in addresses
            if item["components"].get("postal_code") == "94310"
        )
        self.assertEqual(delivery["role"], "ship_to")
        self.assertEqual(delivery["party_name"], "ORVIF ORLY")

    def test_abbreviated_chem_street_is_supported(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((28, 150), "Adresse de livraison")
        page.insert_text((28, 165), "BIO HABITAT MAG SL SAV")
        page.insert_text((28, 180), "30 Chem. du Parois")
        page.insert_text((28, 195), "85300 CHALLANS")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        ship_to = next(item for item in addresses if item["role"] == "ship_to")
        self.assertEqual(ship_to["components"]["house_number"], "30")
        self.assertEqual(ship_to["components"]["postal_code"], "85300")
        self.assertEqual(ship_to["components"]["city"], "CHALLANS")

    def test_departmental_road_and_fractional_house_number_are_supported(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 40), "Facture a")
        page.insert_text((40, 80), "Adresse de livraison")
        page.insert_text((40, 100), "ANCONETTI CENTRALE")
        page.insert_text((40, 120), "Départementale 56")
        page.insert_text((40, 140), "13790 CHATEAUNEUF LE ROUGE")
        page.insert_text((320, 100), "ISERBA")
        page.insert_text((320, 120), "16 Â AVENUE DES QUATRE CANTONS")
        page.insert_text((320, 140), "76000 ROUEN")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        self.assertTrue(any(
            item["components"].get("street_type", "").lower().startswith("départementale")
            for item in addresses
        ))
        delivery = next(
            item for item in addresses
            if item["components"].get("postal_code") == "13790"
        )
        self.assertEqual(delivery["party_name"], "ANCONETTI CENTRALE")
        iserba = next(
            item for item in addresses
            if item["components"].get("postal_code") == "76000"
        )
        self.assertEqual(iserba["components"]["house_number"], "16")
        self.assertEqual(iserba["components"]["house_number_suffix"], "½")

    def test_explicit_order_label_supports_numeric_spaced_and_next_row_values(self):
        cases = (
            ("COMMANDE N° 674072490", None, "674072490"),
            ("COMMANDE FOURNISSEUR : 66 211 Adresse de livraison", None, "66 211"),
            ("COMMANDE N°", "NO 0 CHA 30183", "NO 0 CHA 30183"),
        )
        for label, following, expected in cases:
            with self.subTest(expected=expected):
                doc = fitz.open()
                page = doc.new_page(width=595, height=842)
                page.insert_text((40, 80), label)
                if following:
                    page.insert_text((40, 100), following)
                data = doc.tobytes()
                doc.close()
                lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
                result = extract_geometry_suggestions(lines)
                self.assertEqual(
                    result["anchored_fields"]["order_number"]["value"], expected
                )
                self.assertEqual(
                    result["anchored_fields"]["order_number"]["source"],
                    "geometry_explicit_order_label_v3",
                )

    def test_reference_commande_column_drops_trailing_separator(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 80), "COMMANDE FOURNISSEUR")
        page.insert_text((40, 120), "Date")
        page.insert_text((145, 120), "Reference")
        page.insert_text((230, 120), "Commande")
        page.insert_text((40, 140), "03/02/2026")
        page.insert_text((145, 140), "245")
        page.insert_text((169, 140), "969")
        page.insert_text((193, 140), "/")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        result = extract_geometry_suggestions(lines)
        order_number = result["anchored_fields"]["order_number"]
        self.assertEqual(order_number["value"], "245 969")
        self.assertEqual(order_number["source_text"], "245 969")

    def test_composite_cf_header_recovers_final_number_without_document_title(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 80), "N° CF 15 4 000572686")
        page.insert_text((190, 80), "DATE")
        page.insert_text((235, 80), "10/03/26")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        result = extract_geometry_suggestions(lines)
        number = result["anchored_fields"]["order_number"]
        self.assertEqual(number["value"], "CF000572686")
        self.assertIn("CF 15 4 000572686", number["source_text"])

    def test_cf_header_date_adjacent_to_explicit_order_number_is_recovered(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 80), "COMMANDE N CF001968062 9/04/26")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        fields = extract_geometry_suggestions(lines)["anchored_fields"]
        self.assertEqual(fields["order_number"]["value"], "CF001968062")
        self.assertEqual(fields["order_date"]["value"], "9/04/26")
        self.assertEqual(
            fields["order_date"]["source"],
            "geometry_adjacent_order_date_v3",
        )

    def test_top_location_date_is_recovered_with_strong_order_number(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 55), "TOULON LE 25/03/26")
        page.insert_text((40, 120), "COMMANDE N STTQU244876")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        fields = extract_geometry_suggestions(lines)["anchored_fields"]
        self.assertEqual(fields["order_number"]["value"], "STTQU244876")
        self.assertEqual(fields["order_date"]["value"], "25/03/26")
        self.assertEqual(
            fields["order_date"]["source"],
            "geometry_top_location_order_date_v3",
        )
    def test_delivery_site_without_street_is_kept(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 80), "Adresse de livraison")
        page.insert_text((40, 100), "IZI confort Saint-Jean-de-Luz")
        page.insert_text((40, 120), "ZA Lanzelai - BAT A - N°3 et 4")
        page.insert_text((40, 140), "64 310 ASCAIN")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        result = extract_geometry_suggestions(lines)
        ship_to = next(
            item for item in result["address_candidates"]
            if item["role"] == "ship_to"
        )
        self.assertEqual(ship_to["components"]["postal_code"], "64310")
        self.assertIn("ZA Lanzelai", ship_to["components"]["industrial_zone"])

    def test_order_title_and_date_label_do_not_override_real_number(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 60), "COMMANDE Bosch pieces de rechange")
        page.insert_text((330, 80), "93711 Drancy cedex")
        page.insert_text((40, 120), "N° Commande Date Mode emission")
        page.insert_text((40, 140), "CM-00391163")
        page.insert_text((180, 140), "17/03/2026")
        page.insert_text((40, 180), "Date commande : 17/03/2026")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        fields = extract_geometry_suggestions(lines)["anchored_fields"]
        self.assertEqual(fields["order_number"]["value"], "CM-00391163")

    def test_document_cell_under_order_heading_is_explicit_order_metadata(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 60), "Commande Fournisseur")
        page.insert_text((40, 100), "N° Document")
        page.insert_text((210, 100), "Date")
        page.insert_text((40, 120), "CF352124")
        page.insert_text((210, 120), "20/03/2026")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        fields = extract_geometry_suggestions(lines)["anchored_fields"]
        self.assertEqual(fields["order_number"]["value"], "CF352124")
        self.assertEqual(
            fields["order_number"]["source"],
            "geometry_explicit_order_metadata_v3",
        )

    def test_numero_date_reference_table_uses_numero_not_your_reference(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((75, 210), "Bon de commande")
        page.insert_text((266, 263), "Numero")
        page.insert_text((340, 263), "Date")
        page.insert_text((407, 263), "Votre Reference")
        page.insert_text((266, 284), "FBC22025806")
        page.insert_text((338, 284), "04/02/26")
        page.insert_text((401, 284), "CFA139228/STO")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        number = extract_geometry_suggestions(lines)["anchored_fields"]["order_number"]
        self.assertEqual(number["value"], "FBC22025806")
        self.assertEqual(number["source"], "geometry_explicit_order_metadata_v3")

    def test_inline_duplicate_order_number_stops_at_first_identifier(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 80), "COMMANDE 5511915 5511915 / 1877 du 24.03.2026")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        fields = extract_geometry_suggestions(lines)["anchored_fields"]
        self.assertEqual(fields["order_number"]["value"], "5511915")

    def test_standalone_order_title_prefers_attached_number_below_over_supplier_street(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((420, 40), "COMMANDE")
        page.insert_text((420, 60), "N\u00b05405637 /1877")
        page.insert_text((40, 120), "COMMANDE 5405637/1877 - 03/02/2026")
        page.insert_text((360, 120), "ELM LEBLANC")
        page.insert_text((40, 140), "COMMANDE")
        page.insert_text((360, 140), "124 126 AV STALINGRAD")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        fields = extract_geometry_suggestions(lines)["anchored_fields"]
        self.assertEqual(fields["order_number"]["value"], "5405637 /1877")
        self.assertNotEqual(fields["order_number"]["value"], "124 126")

    def test_attached_number_marker_is_stripped_and_following_date_is_rejected(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 80), "BON DE COMMANDE n°BC052737 du 28/01/26")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        fields = extract_geometry_suggestions(lines)["anchored_fields"]
        self.assertEqual(fields["order_number"]["value"], "BC052737")
        self.assertNotEqual(fields["order_number"]["value"], "28/01/26")

    def test_product_tableau_de_commande_is_not_an_order_number_label(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 80), "Reference Designation Quantite Montant")
        page.insert_text((40, 105), "8716774182 TABLEAU DE COMMANDE 1 1 2EL8716774182")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        fields = extract_geometry_suggestions(lines)["anchored_fields"]
        self.assertNotIn("order_number", fields)

    def test_product_note_for_customer_order_is_not_the_document_number(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 80), "pour la commande client n° 9758685")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        fields = extract_geometry_suggestions(lines)["anchored_fields"]
        self.assertNotIn("order_number", fields)

    def test_product_note_sur_commande_does_not_override_piece_table_number(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 60), "Commande Date de livraison souhaitee : 04/12/2025")
        page.insert_text((40, 90), "Date")
        page.insert_text((130, 90), "Piece")
        page.insert_text((205, 90), "Client Reference Commercial P")
        page.insert_text((40, 110), "03/12/2025")
        page.insert_text((130, 110), "1759185")
        page.insert_text((205, 110), "FELM2140 MAGNE Pierre 1/1")
        page.insert_text((40, 180), "vanne sur commande 17938710")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        number = extract_geometry_suggestions(lines)["anchored_fields"]["order_number"]
        self.assertEqual(number["value"], "1759185")
        self.assertEqual(number["source"], "geometry_explicit_order_metadata_v3")

    def test_single_letter_r_is_accepted_as_a_printed_street_type(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 60), "A Livrer a :")
        page.insert_text((40, 80), "C.C.L BEZIERS")
        page.insert_text((40, 100), "Z.A.C DE MERCORENT")
        page.insert_text((40, 120), "350 R ALPHONSE BEAU DE ROCHAS")
        page.insert_text((40, 140), "34500 BEZIERS")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        ship_to = next(item for item in addresses if item["role"] == "ship_to")
        self.assertEqual(ship_to["components"]["house_number"], "350")
        self.assertEqual(ship_to["components"]["street_type"], "R")
        self.assertEqual(ship_to["components"]["postal_code"], "34500")

    def test_inline_delivery_sentence_recovers_party_street_and_locality(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text(
            (40, 100),
            "Livraison souhaitée à la société ESPINOSA, 43 boulevard Berthelot 34000 Montpellier",
        )
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        ship_to = next(item for item in addresses if item["role"] == "ship_to")
        self.assertEqual(ship_to["party_name"], "ESPINOSA")
        self.assertEqual(ship_to["components"]["house_number"], "43")
        self.assertEqual(ship_to["components"]["postal_code"], "34000")
        self.assertEqual(ship_to["components"]["city"], "Montpellier")

    def test_recipient_heading_and_long_inline_street_recover_delivery(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 80), "Adresse destinataire")
        page.insert_text((40, 100), "EXINCOURT SANITAIRE CHAUFFAGE")
        page.insert_text((40, 120), "53 rue des Grandes Egouttes 25400 EXINCOURT")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        ship_to = next(item for item in addresses if item["role"] == "ship_to")
        self.assertEqual(ship_to["party_name"], "EXINCOURT SANITAIRE CHAUFFAGE")
        self.assertEqual(ship_to["components"]["postal_code"], "25400")
        self.assertEqual(ship_to["components"]["city"], "EXINCOURT")

    def test_reference_commande_column_preserves_complete_compound_value(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 80), "Date")
        page.insert_text((125, 80), "Reference Commande")
        page.insert_text((40, 100), "12/02/2026")
        page.insert_text((127, 100), "2346887 / 3389453/03")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        fields = extract_geometry_suggestions(lines)["anchored_fields"]
        self.assertEqual(fields["order_number"]["value"], "2346887 / 3389453/03")

    def test_commande_a_distance_is_not_promoted_as_an_order_number(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 60), "Commande a distance 5720720")
        page.insert_text((40, 100), "N Document")
        page.insert_text((160, 100), "Date")
        page.insert_text((40, 120), "CF347820")
        page.insert_text((160, 120), "04/12/2025")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        fields = extract_geometry_suggestions(lines)["anchored_fields"]
        self.assertEqual(fields["order_number"]["value"], "CF347820")

    def test_comma_house_number_and_routing_code_are_source_preserved(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 60), "Adresse de livraison")
        page.insert_text((40, 80), "PROLIANS BA BEZIERS")
        page.insert_text((40, 100), "24, avenue Martin Luther King")
        page.insert_text((40, 120), "CS 63009 - ZI LE CAPISCOL")
        page.insert_text((40, 140), "34536 BEZIERS")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        ship_to = next(item for item in addresses if item["role"] == "ship_to")
        self.assertEqual(ship_to["components"]["house_number"], "24")
        self.assertEqual(ship_to["components"]["postal_code"], "34536")
        self.assertEqual(ship_to["components"]["cs"], "CS 63009")

    def test_business_park_and_dual_house_number_are_kept(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 60), "Adresse de livraison")
        page.insert_text((40, 80), "PPC")
        page.insert_text((40, 100), "TECHNIPARC LA BASTIDONNE")
        page.insert_text((40, 120), "3 et 5 avenue de Fondeyre")
        page.insert_text((40, 140), "31200 TOULOUSE")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        ship_to = next(item for item in addresses if item["role"] == "ship_to")
        self.assertEqual(ship_to["components"]["house_number"], "3 et 5")
        self.assertEqual(
            ship_to["components"]["business_park"], "TECHNIPARC LA BASTIDONNE"
        )

    def test_fuzzy_delivery_label_and_attached_city_form_a_site_address(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((300, 80), "Adi 550 de livraison")
        page.insert_text((300, 100), "CATRYBAYART ATTIN LA PAIX FAITE")
        page.insert_text((300, 120), "62170ATTIN")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        ship_to = next(item for item in addresses if item["role"] == "ship_to")
        self.assertEqual(ship_to["components"]["postal_code"], "62170")
        self.assertEqual(ship_to["components"]["city"], "ATTIN")

    def test_same_line_delivery_department_is_kept_before_street(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 60), "Adresse de livraison")
        page.insert_text((40, 80), "VERNEY SA")
        page.insert_text((40, 100), "Depot Dijon")
        page.insert_text((115, 100), "28 RUE DE MAYENCE ZAE CAPNORD")
        page.insert_text((40, 120), "BP 77605 21076 DIJON CEDEX")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        ship_to = next(item for item in addresses if item["role"] == "ship_to")
        self.assertEqual(ship_to["party_name"], "VERNEY SA")
        self.assertEqual(ship_to["department"], "Depot Dijon")

    def test_inline_zone_is_separated_from_street_name(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 60), "Adresse de livraison")
        page.insert_text((40, 80), "VERNEY SA")
        page.insert_text((40, 100), "Depot Dijon 28 RUE DE MAYENCE ZAE CAPNORD BP 77605")
        page.insert_text((40, 120), "21076 DIJON CEDEX")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        ship_to = next(item for item in addresses if item["role"] == "ship_to")
        self.assertEqual(ship_to["components"]["street_name"], "DE MAYENCE")
        self.assertEqual(ship_to["components"]["industrial_zone"], "ZAE CAPNORD")
        self.assertEqual(ship_to["components"]["bp"], "BP 77605")

    def test_direct_cf_number_in_legacy_header_is_the_order_number(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 80), "N")
        page.insert_text((70, 80), "CF")
        page.insert_text((120, 80), "000334206")
        page.insert_text((220, 80), "DATE")
        page.insert_text((280, 80), "03/02/26")
        page.insert_text((390, 80), "PAGE 1")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        number = extract_geometry_suggestions(lines)["anchored_fields"]["order_number"]
        self.assertEqual(number["value"], "CF000334206")
        self.assertEqual(number["source"], "geometry_explicit_order_label_v3")

    def test_four_plus_attached_digit_postcode_recovers_zone_only_delivery(self):
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((296, 80), "Adresse de livraison")
        page.insert_text((296, 100), "DESENFANS / ST JOSSE")
        page.insert_text((450, 100), "ZA DE LA JUDOCIENNE")
        page.insert_text((296, 120), "6217")
        page.insert_text((322, 120), "0SAINT-JOSSE")
        page.insert_text((450, 120), "FRANCE")
        data = doc.tobytes()
        doc.close()

        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        addresses = extract_geometry_suggestions(lines)["address_candidates"]
        ship_to = next(item for item in addresses if item["role"] == "ship_to")
        self.assertEqual(ship_to["party_name"], "DESENFANS / ST JOSSE")
        self.assertEqual(ship_to["components"]["industrial_zone"], "ZA DE LA JUDOCIENNE")
        self.assertEqual(ship_to["components"]["postal_code"], "62170")
        self.assertEqual(ship_to["components"]["city"], "SAINT-JOSSE")


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

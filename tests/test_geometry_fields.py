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


if __name__ == "__main__":
    unittest.main()

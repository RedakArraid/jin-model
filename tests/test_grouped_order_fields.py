import unittest

from jin_runtime.clean_output import build_clean_output
from jin_runtime.grouped_order_fields import enrich_grouped_order_fields


SOURCE = """B.P. 61178 COMMANDE REGROUPEE 124 RUE DE STALINGRAD
COLIS CONTREMARQUE : PROLIANS BA MONTPELLIER ** RAPPELEZ SUR FACTURE LE N° COMMANDE : ST 1 1A1 42564 **
CLIMATE CL6001I W26E UNITES INTER 2,6KW R32 15,00 PIECE 06/26 144,58 2.168,70
SEQ: 94137
COLIS CONTREMARQUE : PROLIANS BA BEZIERS ** RAPPELEZ SUR FACTURE LE N° COMMANDE : ST 2 1A1 67204 **
CLIMATE CL6001I 26E UNITES EXTER 2,6KW R32 10,00 PIECE 06/26 237,18 2.371,80
SEQ: 94145
"""


class GroupedOrderFieldTests(unittest.TestCase):
    def test_all_references_are_kept_without_inventing_a_primary_number(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "business_extractions": {"purchase_order": {
                "purchase_order": {"number": {"value": "ST 1 1A1 42564"}},
                "pages": [{"page": 1, "text": SOURCE}],
            }},
        }
        enrich_grouped_order_fields(payload)

        references = payload["customer_order_references"]
        self.assertEqual(
            [item["value"] for item in references],
            ["ST 1 1A1 42564", "ST 2 1A1 67204"],
        )
        self.assertEqual(
            [item["site"] for item in references],
            ["PROLIANS BA MONTPELLIER", "PROLIANS BA BEZIERS"],
        )
        header = payload["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertIsNone(header["number"]["value"])
        self.assertEqual(header["number_original"]["value"], "ST 1 1A1 42564")
        self.assertEqual(payload["document"]["grouped_order_count"], 2)
        lines = payload["business_extractions"]["purchase_order"]["purchase_order"]["lines"]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["material_number"], "94137")
        self.assertEqual(lines[0]["quantity"], 15.0)
        self.assertEqual(lines[0]["unit_price"], 144.58)
        self.assertEqual(lines[0]["line_total"], 2168.70)
        self.assertEqual(lines[0]["customer_order_number"], "ST 1 1A1 42564")

        clean = build_clean_output(payload)
        self.assertNotIn("customer_order_number", clean["order"])
        self.assertEqual(
            [item["value"] for item in clean["order"]["customer_order_numbers"]],
            ["ST 1 1A1 42564", "ST 2 1A1 67204"],
        )
        self.assertEqual(clean["document"]["order_structure"], "grouped")
        self.assertEqual(clean["order"]["line_items"][0]["quantity"], 15.0)
        self.assertEqual(
            clean["order"]["line_items"][0]["references"],
            {"material_number": "94137", "customer_order_number": "ST 1 1A1 42564"},
        )
        self.assertEqual(clean["order"]["groups"][0]["total_net"], 2168.70)


if __name__ == "__main__":
    unittest.main()

import unittest

from jin_runtime.clean_output import build_clean_output
from jin_runtime.commercial_references import enrich_commercial_references


class CommercialReferenceTests(unittest.TestCase):
    def test_multiple_quotes_are_linked_only_to_their_explicit_lines(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{
                "page": 1,
                "text": (
                    "Commande C-100\n"
                    "Ligne 10 suivant devis DV-100\n"
                    "Ligne 20 suivant devis DV-200\n"
                    "Dérogation n° DER-77"
                ),
            }],
            "business_extractions": {"purchase_order": {
                "purchase_order": {},
                "lines": [
                    {
                        "line_number": "10", "material_number": "MAT-1",
                        "description": "Produit un", "quantity": 1,
                        "raw_text": "MAT-1 | Produit un | suivant devis DV-100",
                    },
                    {
                        "line_number": "20", "material_number": "MAT-2",
                        "description": "Produit deux", "quantity": 2,
                        "raw_text": "MAT-2 | Produit deux | suivant devis DV-200 | dérogation DER-77",
                    },
                ],
            }},
        }

        out = enrich_commercial_references(payload)
        po = out["business_extractions"]["purchase_order"]
        self.assertEqual(po["lines"][0]["quote_numbers"], ["DV-100"])
        self.assertEqual(po["lines"][1]["quote_numbers"], ["DV-200"])
        self.assertEqual(po["lines"][1]["derogation_numbers"], ["DER-77"])
        quote_links = {item["number"]: item["line_numbers"] for item in po["quote_references"]}
        self.assertEqual(quote_links, {"DV-100": ["10"], "DV-200": ["20"]})
        self.assertEqual(po["derogation_references"][0]["line_numbers"], ["20"])

        clean = build_clean_output(out)
        self.assertEqual(
            clean["order"]["line_items"][1]["references"]["quote_numbers"],
            ["DV-200"],
        )
        self.assertEqual(len(clean["order"]["quote_references"]), 2)

    def test_document_quote_is_not_assigned_to_lines_without_source_link(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"page": 1, "text": "Référence devis GLOBAL-42"}],
            "business_extractions": {"purchase_order": {
                "purchase_order": {},
                "lines": [{"line_number": "1", "material_number": "MAT-1", "description": "Produit"}],
            }},
        }

        po = enrich_commercial_references(payload)["business_extractions"]["purchase_order"]

        self.assertEqual(po["quote_references"][0]["scope"], "document")
        self.assertEqual(po["quote_references"][0]["line_numbers"], [])
        self.assertNotIn("quote_numbers", po["lines"][0])

    def test_several_numbers_after_one_explicit_label_are_preserved(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"page": 1, "text": "Devis DV-100 et DV-200"}],
            "business_extractions": {"purchase_order": {"purchase_order": {}, "lines": []}},
        }

        po = enrich_commercial_references(payload)["business_extractions"]["purchase_order"]

        self.assertEqual(
            [item["number"] for item in po["quote_references"]],
            ["DV-100", "DV-200"],
        )

    def test_abbreviated_derogation_label_is_supported(self):
        payload = {
            "pages": [{"page": 1, "text": "Dérog. DER-88"}],
            "business_extractions": {"purchase_order": {"purchase_order": {}, "lines": []}},
        }

        po = enrich_commercial_references(payload)["business_extractions"]["purchase_order"]

        self.assertEqual(po["derogation_references"][0]["number"], "DER-88")

    def test_quote_column_is_matched_by_material_quantity_and_amount(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"page": 1, "text": "Bon de commande"}],
            "tables": [{
                "id": "table-lines",
                "source": {"page": 1},
                "headers": [
                    "Article", "Désignation", "Qté cdée", "Montant HT",
                    "N° d'offre / Réf devis", "N° dérogation",
                ],
                "data": [
                    ["Article", "Désignation", "Qté cdée", "Montant HT", "Réf devis", "N° dérogation"],
                    ["EL7716780266", "THERMOSTAT", "10", "250,00", "ZAEJ11499-3", "DER-10"],
                    ["EL7716780266", "THERMOSTAT", "15", "375,00", "ZAEJ22000-4", "DER-20"],
                ],
            }],
            "business_extractions": {"purchase_order": {
                "purchase_order": {},
                "lines": [
                    {
                        "line_number": "1", "material_number": "EL7716780266",
                        "description": "THERMOSTAT", "quantity": 10, "line_total": 250,
                    },
                    {
                        "line_number": "2", "material_number": "EL7716780266",
                        "description": "THERMOSTAT", "quantity": 15, "line_total": 375,
                    },
                ],
            }},
        }

        po = enrich_commercial_references(payload)["business_extractions"]["purchase_order"]

        self.assertEqual(po["lines"][0]["quote_numbers"], ["ZAEJ11499-3"])
        self.assertEqual(po["lines"][1]["quote_numbers"], ["ZAEJ22000-4"])
        self.assertEqual(po["lines"][0]["derogation_numbers"], ["DER-10"])
        self.assertEqual(po["lines"][1]["derogation_numbers"], ["DER-20"])
        evidence = po["quote_references"][0]["evidence"][0]
        self.assertEqual(evidence["extraction_method"], "structured_table_quote_column_v1")


if __name__ == "__main__":
    unittest.main()

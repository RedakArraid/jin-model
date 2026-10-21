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

    def test_slash_separated_references_after_one_offer_label_are_preserved(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{"page": 1, "text": "OFFRE DE PRIX 82H328 / SGA-2024-013"}],
            "business_extractions": {"purchase_order": {"purchase_order": {}, "lines": []}},
        }

        po = enrich_commercial_references(payload)["business_extractions"]["purchase_order"]

        self.assertEqual(
            [item["number"] for item in po["quote_references"]],
            ["82H328", "SGA-2024-013"],
        )

    def test_abbreviated_derogation_label_is_supported(self):
        payload = {
            "pages": [{"page": 1, "text": "Dérog. DER-88"}],
            "business_extractions": {"purchase_order": {"purchase_order": {}, "lines": []}},
        }

        po = enrich_commercial_references(payload)["business_extractions"]["purchase_order"]

        self.assertEqual(po["derogation_references"][0]["number"], "DER-88")

    def test_derogation_after_material_collision_uses_distinct_id_and_links_line(self):
        payload = {
            "document": {"primary_document_type": "purchase_order"},
            "pages": [{
                "page": 1,
                "text": (
                    "756059 7733701567\n"
                    "DÉROGATION 7733701567 RBGA-10357941"
                ),
            }],
            "blocks": [{
                "id": "derogation-block",
                "text": "DÉROGATION 7733701567 RBGA-10357941",
                "source": {"page": 1, "bbox": [17, 394, 158, 413]},
            }],
            "tables": [{
                "id": "line-table",
                "source": {"page": 1},
                "headers": ["DESIGNATION CODE REFERENCE FOURNISSEUR", "QUANTITES"],
                "data": [[
                    (
                        "UNITE EXT MONOSPLIT CLIMATE 3000I 3,5KW R32\n"
                        "756059 7733701567\n"
                        "7733701567\n"
                        "DÉROGATION RBGA-10357941"
                    ),
                    "1,00",
                ]],
            }],
            "business_extractions": {"purchase_order": {
                "purchase_order": {},
                "lines": [{
                    "line_number": "1",
                    "material_number": "7733701567",
                    "supplier_material_number": "7733701567",
                    "customer_material_number": "756059",
                    "description": "UNITE EXT MONOSPLIT CLIMATE 3000I 3,5KW R32",
                    "quantity": 1,
                    "page": 1,
                }],
            }},
        }

        out = enrich_commercial_references(payload)
        po = out["business_extractions"]["purchase_order"]
        line = po["lines"][0]
        reference = po["derogation_references"][0]

        self.assertEqual(line["derogation_numbers"], ["RBGA-10357941"])
        self.assertEqual(reference["number"], "RBGA-10357941")
        self.assertEqual(reference["scope"], "line")
        self.assertEqual(reference["line_numbers"], ["1"])
        self.assertEqual(reference["material_numbers"], ["7733701567"])
        self.assertEqual(reference["evidence"][0]["bbox"], [17, 394, 158, 413])
        self.assertNotIn(
            "7733701567",
            [item["number"] for item in po["derogation_references"]],
        )
        self.assertNotIn("quote_references", po)

        clean = build_clean_output(out)
        self.assertEqual(
            clean["order"]["line_items"][0]["references"]["derogation_numbers"],
            ["RBGA-10357941"],
        )

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

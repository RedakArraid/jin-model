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

    def test_published_schema_describes_the_versioned_contract(self):
        schema = clean_output_schema()
        self.assertEqual(schema["properties"]["schema_version"]["const"],
                         "jin-clean-extraction-v1")
        self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()

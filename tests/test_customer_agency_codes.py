import unittest

from jin_runtime.clean_output import build_clean_output
from jin_runtime.customer_agency_codes import enrich_customer_agency_codes
from jin_runtime.delivery_addresses import enrich_delivery_addresses


def iserba_payload(*, delivery_code: str | None = "VIU") -> dict:
    raw_lines = ["IMPASSE HUSSON", "91170 VIRY-CHATILLON", "France"]
    address = {
        "raw": "IMPASSE HUSSON | 91170 VIRY-CHATILLON | France",
        "raw_lines": raw_lines,
        "street": "IMPASSE HUSSON",
        "postal_code": "91170",
        "city": "VIRY-CHATILLON",
        "country": "France",
    }
    if delivery_code:
        raw_lines.append(delivery_code)
        address["raw"] += f" | {delivery_code}"
        address["address_complement"] = delivery_code
    return {
        "document": {"primary_document_type": "purchase_order"},
        "blocks": [
            {
                "text": "N° de commande : CAC2407CHA00058 / VIU",
                "source": {"page": 1, "bbox": [34, 217, 246, 228]},
            },
            {"text": "VIU", "source": {"page": 1, "bbox": [538, 393, 552, 404]}},
        ],
        "business_extractions": {"purchase_order": {
            "purchase_order": {"number": {"value": "CAC2407CHA00058"}},
            "supplier": {"name": "BOSCH THERMOTECHNOLOGIE"},
            "ship_to": {"name": "UNICIA", "address": dict(address)},
            "business_addresses": [{
                "address_id": "delivery", "role": "ship_to", "party_name": "UNICIA",
                "address": address,
                "formatted_address": "IMPASSE HUSSON, VIU, 91170 VIRY-CHATILLON, France",
            }],
        }},
    }


class CustomerAgencyCodeTests(unittest.TestCase):
    def test_order_suffix_is_promoted_only_when_corroborated_in_delivery_block(self):
        payload = iserba_payload()
        enrich_customer_agency_codes(payload)
        enrich_delivery_addresses(payload)

        po = payload["business_extractions"]["purchase_order"]
        field = po["purchase_order"]["customer_agency_code"]
        self.assertEqual(field["value"], "VIU")
        self.assertEqual(field["validation_status"], "SOURCE_CORROBORATED")
        self.assertEqual(po["ship_to"]["customer_agency_code"], "VIU")

        delivery = po["business_addresses"][0]
        self.assertEqual(delivery["customer_agency_code"], "VIU")
        self.assertNotIn("VIU", delivery["clean_address"]["lines"])
        self.assertEqual(
            delivery["clean_address"]["excluded_components"],
            [{
                "type": "customer_agency_code", "value": "VIU",
                "reason": "non_postal_business_identifier",
            }],
        )
        # Raw/source values remain available for auditing.
        self.assertEqual(delivery["address"]["address_complement"], "VIU")

        clean = build_clean_output(payload)
        self.assertEqual(clean["order"]["customer_agency_code"]["value"], "VIU")
        self.assertEqual(clean["order"]["parties"]["ship_to"]["customer_agency_code"], "VIU")
        self.assertEqual(clean["order"]["delivery_address"]["customer_agency_code"], "VIU")
        self.assertNotIn("VIU", clean["order"]["delivery_address"]["formatted_lines"])

    def test_unconfirmed_order_suffix_is_not_guessed(self):
        payload = iserba_payload(delivery_code=None)
        enrich_customer_agency_codes(payload)
        po = payload["business_extractions"]["purchase_order"]
        self.assertNotIn("customer_agency_code", po)
        self.assertNotIn("customer_agency_code", po["purchase_order"])

    def test_explicit_agency_code_label_is_supported_without_template_knowledge(self):
        payload = iserba_payload(delivery_code="LY07")
        payload["blocks"] = [{
            "text": "Code agence client : LY07",
            "source": {"page": 1, "bbox": [40, 120, 180, 132]},
        }]
        enrich_customer_agency_codes(payload)
        enrich_delivery_addresses(payload)
        po = payload["business_extractions"]["purchase_order"]
        self.assertEqual(po["customer_agency_code"]["value"], "LY07")
        self.assertEqual(po["customer_agency_code"]["validation_status"], "SOURCE_SUPPORTED")
        self.assertNotIn("LY07", po["business_addresses"][0]["clean_address"]["lines"])

    def test_agency_name_without_code_shape_is_not_promoted(self):
        payload = iserba_payload(delivery_code=None)
        payload["blocks"] = [{"text": "Code agence Lyon"}]
        enrich_customer_agency_codes(payload)
        self.assertNotIn(
            "customer_agency_code",
            payload["business_extractions"]["purchase_order"],
        )

    def test_delivery_site_code_label_variant_is_supported(self):
        payload = iserba_payload(delivery_code="AB12")
        payload["blocks"] = [{"text": "Code site de livraison : AB12"}]
        enrich_customer_agency_codes(payload)
        self.assertEqual(
            payload["business_extractions"]["purchase_order"]["customer_agency_code"]["value"],
            "AB12",
        )

    def test_code_is_attached_to_clean_geometry_delivery_even_if_pollution_was_superseded(self):
        payload = iserba_payload()
        po = payload["business_extractions"]["purchase_order"]
        po["business_addresses"][0]["role"] = "unknown"
        po["business_addresses"][0]["role_label"] = "Superseded delivery candidate"
        po["business_addresses"].append({
            "address_id": "geometry", "role": "ship_to", "party_name": "ISERBA",
            "address": {
                "house_number": "1", "street": "CHEMIN DES PLANS D'EAU",
                "postal_code": "76430", "city": "OUDALLE", "country": "France",
            },
        })

        enrich_customer_agency_codes(payload)
        enrich_delivery_addresses(payload)
        clean = build_clean_output(payload)

        self.assertEqual(clean["order"]["customer_agency_code"]["value"], "VIU")
        self.assertEqual(clean["order"]["delivery_address"]["customer_agency_code"], "VIU")
        self.assertNotIn("VIU", clean["order"]["delivery_address"]["formatted_lines"])


if __name__ == "__main__":
    unittest.main()

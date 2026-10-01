import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jin_runtime.extraction_gate import apply_extraction_gate
from jin_runtime.offline import configure_core_offline
from jin_runtime.output_quality import audit_and_repair_output


def complete_order():
    return {
        "document": {"primary_document_type": "purchase_order"},
        "quality": {"requires_human_review": False},
        "business_extractions": {"purchase_order": {
            "purchase_order": {"number": {"value": "PO-123", "evidence": {
                "page": 1, "bbox": [20, 20, 100, 40], "source_text": "Commande PO-123"}},
                "order_date": {"value": "2026-03-25"}},
            "supplier": {"name": "Supplier"}, "buyer": {"name": "Customer"},
            "lines": [{"material_number": "REF-12", "quantity": 2, "unit_price": 10, "line_total": 20}],
            "totals": {"total_net": 20, "total_vat": 4, "grand_total": 24},
            "business_addresses": [
                {"role": role, "role_confidence": .95,
                 "address": {"postal_code": "75001", "city": "PARIS"}}
                for role in ("ship_to", "bill_to")
            ],
            "validation": {"status": "PASS"}, "requires_human_review": False,
        }},
    }


class ExtractionGateTests(unittest.TestCase):
    def test_complete_coherent_order_passes_checks_without_claiming_truth(self):
        out = apply_extraction_gate(audit_and_repair_output(complete_order()))
        self.assertEqual(out["extraction_decision"]["status"], "CHECKS_PASSED")
        self.assertIn("not a guarantee", out["extraction_decision"]["qualification"])

    def test_financial_mismatch_overrides_high_core_confidence(self):
        payload = complete_order()
        payload["quality"]["overall_confidence"] = .99
        payload["business_extractions"]["purchase_order"]["lines"][0]["line_total"] = 99
        out = apply_extraction_gate(audit_and_repair_output(payload))
        self.assertTrue(out["quality"]["requires_human_review"])
        self.assertTrue(out["business_extractions"]["purchase_order"]["requires_human_review"])

    def test_missing_product_fields_block_review_free_output(self):
        for missing in ("material_number", "quantity", "unit_price"):
            with self.subTest(missing=missing):
                payload = complete_order()
                del payload["business_extractions"]["purchase_order"]["lines"][0][missing]
                self.assertTrue(apply_extraction_gate(payload)["extraction_decision"]["requires_review"])

    def test_ambiguous_address_role_blocks_acceptance(self):
        payload = complete_order()
        payload["business_extractions"]["purchase_order"]["business_addresses"] = [{
            "role": "unknown", "role_confidence": .2,
            "address": {"postal_code": "75001", "city": "PARIS"},
        }]
        out = apply_extraction_gate(payload)
        self.assertIn("ADDRESS_ROLE_UNRESOLVED", {r["code"] for r in out["extraction_decision"]["reasons"]})

    def test_missing_delivery_or_billing_address_requires_review(self):
        for roles in ([], ["ship_to"], ["bill_to"]):
            with self.subTest(roles=roles):
                payload = complete_order()
                po = payload["business_extractions"]["purchase_order"]
                po["business_addresses"] = [a for a in po["business_addresses"] if a["role"] in roles]
                reasons = apply_extraction_gate(payload)["extraction_decision"]["reasons"]
                self.assertIn("ADDRESS_ROLE_MISSING", {r["code"] for r in reasons})

    def test_multiple_delivery_addresses_are_never_silently_selected(self):
        payload = complete_order()
        addresses = payload["business_extractions"]["purchase_order"]["business_addresses"]
        addresses.append(copy.deepcopy(addresses[0]))
        reasons = apply_extraction_gate(payload)["extraction_decision"]["reasons"]
        self.assertIn("DELIVERY_ADDRESS_AMBIGUOUS", {item["code"] for item in reasons})

    def test_conflicting_pages_and_failed_enrichment_require_review(self):
        payload = complete_order()
        payload["weak_field_suggestions"] = {"field_conflicts": [{"field": "order_number"}]}
        payload["runtime_warnings"] = [{"component": "field_router", "detail": "OCR failed"}]
        codes = {r["code"] for r in apply_extraction_gate(payload)["extraction_decision"]["reasons"]}
        self.assertIn("FIELD_CANDIDATES_DISAGREE", codes)
        self.assertIn("ENRICHMENT_UNAVAILABLE", codes)

    def test_legal_form_alone_and_shared_buyer_supplier_contact_need_review(self):
        payload = complete_order()
        po = payload["business_extractions"]["purchase_order"]
        po["buyer"].update(name="S.A.S.", contact={"email": "example@example.test"})
        po["supplier"].update(email="Example@example.test")
        codes = {r["code"] for r in apply_extraction_gate(payload)["extraction_decision"]["reasons"]}
        self.assertIn("PARTY_NAME_UNINFORMATIVE", codes)
        self.assertIn("PARTY_CONTACT_ROLE_CONFLICT", codes)
        self.assertEqual(po["buyer"]["contact"]["email"], "example@example.test")

    def test_non_order_never_masquerades_as_validated_order(self):
        out = apply_extraction_gate({"document": {"primary_document_type": "quotation"}})
        self.assertTrue(out["extraction_decision"]["requires_review"])

    def test_alternate_contact_does_not_hide_shared_buyer_supplier_phone(self):
        payload = complete_order()
        po = payload["business_extractions"]["purchase_order"]
        po["buyer"].update(phone="0140000000", contact={"phone": "0160000000"})
        po["supplier"].update(phone="01 40 00 00 00")
        reasons = apply_extraction_gate(payload)["extraction_decision"]["reasons"]
        self.assertIn("PARTY_CONTACT_ROLE_CONFLICT", {r["code"] for r in reasons})

    def test_exact_ban_match_does_not_add_reference_review_reason(self):
        payload = complete_order()
        addresses = payload["business_extractions"]["purchase_order"]["business_addresses"]
        for index, address in enumerate(addresses):
            address["ban_verification"] = {
                "status": "EXACT_MATCH" if index == 0 else "CANONICAL_MATCH", "verified": True,
            }
        codes = {r["code"] for r in apply_extraction_gate(payload)["extraction_decision"]["reasons"]}
        self.assertFalse(any(code.startswith("ADDRESS_REFERENCE_") or code == "ADDRESS_NOT_FOUND_IN_REFERENCE"
                             for code in codes))

    def test_nonconclusive_ban_absence_requires_review_without_declaring_invalid(self):
        payload = complete_order()
        addresses = payload["business_extractions"]["purchase_order"]["business_addresses"]
        addresses[0]["ban_verification"] = {
            "status": "NOT_FOUND", "verified": False, "conclusive": False,
        }
        out = apply_extraction_gate(payload)
        reasons = out["extraction_decision"]["reasons"]
        self.assertIn("ADDRESS_NOT_FOUND_IN_REFERENCE", {item["code"] for item in reasons})
        self.assertFalse(addresses[0]["ban_verification"]["conclusive"])

    def test_offline_disables_geocoding_and_remote_models_on_both_engines(self):
        config = {"addresses": {"verification": {"enabled": True}},
                  "semantic_model": {"enabled": True}, "multimodal": {"enabled": True},
                  "engine": {"default_backend": "azure"},
                  "azure_document_intelligence": {"enabled": True}}
        engine = SimpleNamespace(config=copy.deepcopy(config), po_engine=SimpleNamespace(config=copy.deepcopy(config)))
        with patch.dict("os.environ", {"JIN_OFFLINE": "1"}):
            configure_core_offline(engine)
        for owner in (engine, engine.po_engine):
            self.assertFalse(owner.config["addresses"]["verification"]["enabled"])
            self.assertFalse(owner.config["semantic_model"]["enabled"])
            self.assertFalse(owner.config["multimodal"]["enabled"])
            self.assertFalse(owner.config["azure_document_intelligence"]["enabled"])
            self.assertEqual(owner.config["engine"]["default_backend"], "local")


if __name__ == "__main__":
    unittest.main()

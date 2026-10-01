import copy
import unittest

from jin_runtime.order_numbers import check_order_number, normalize_identifier


def order(value="CF001964998", source="Commande client N° CF 001964998"):
    return {"purchase_order": {"number": {"value": value, "final_confidence": .91,
            "evidence": {"page": 1, "bbox": [20, 20, 100, 40], "source_text": source}}}}


class OrderNumberReliabilityTests(unittest.TestCase):
    def test_native_or_ocr_source_with_spaces_is_traceable_without_weak_candidate(self):
        result = check_order_number(order(), {})
        self.assertEqual(result["status"], "SOURCE_SUPPORTED")
        self.assertIn("do not guarantee", result["qualification"])

    def test_normalization_preserves_distinct_customer_identifiers(self):
        self.assertEqual(normalize_identifier(" cf 0012 "), "CF0012")
        for left, right in (("CF0012", "CF12"), ("P0-12", "PO-12"), ("AB-12", "AB/12")):
            self.assertNotEqual(normalize_identifier(left), normalize_identifier(right))

    def test_source_substring_is_not_sufficient_evidence(self):
        for source in ("Commande N° XCF001964998", "Commande N° CF0019649989", "Article 87167723990"):
            self.assertIn("ORDER_NUMBER_EVIDENCE_MISSING", check_order_number(order(source=source), {})["issues"])

    def test_high_score_does_not_excuse_missing_source_location(self):
        payload = order()
        payload["purchase_order"]["number"]["final_confidence"] = .999
        del payload["purchase_order"]["number"]["evidence"]["bbox"]
        self.assertIn("ORDER_NUMBER_EVIDENCE_MISSING", check_order_number(payload, {})["issues"])

    def test_identifier_separators_do_not_allow_truncated_matches(self):
        for text in ("Commande PO-123/2", "Commande PO-123-A", "Commande PO-123.2", "Commande X/PO-123", "Commande X__PO-123"):
            with self.subTest(text=text):
                self.assertIn("ORDER_NUMBER_EVIDENCE_MISSING", check_order_number(order("PO-123", text), {})["issues"])
        for text in ("Commande PO-123.", "Commande (PO-123)", "Commande PO-123, merci."):
            with self.subTest(text=text):
                self.assertFalse(check_order_number(order("PO-123", text), {})["issues"])

    def test_disagreement_with_other_page_requires_review_and_never_overwrites(self):
        payload = order()
        before = copy.deepcopy(payload)
        result = check_order_number(payload, {"anchored_field_candidates": {"order_number": [
            {"value": "CF001964998", "page": 1}, {"value": "CR001964998", "page": 2},
        ]}})
        self.assertIn("ORDER_NUMBER_CANDIDATES_DISAGREE", result["issues"])
        self.assertEqual(payload, before)

    def test_date_and_scheduling_prose_are_not_order_number_candidates(self):
        result = check_order_number(order("CF000575234"), {
            "anchored_field_candidates": {"order_number": [
                {"value": "CF000575234", "page": 1},
                {"value": "21/04/26", "page": 1},
                {"value": "Reprise le 05 Janvier 2026.", "page": 2},
            ]},
        })
        self.assertNotIn("ORDER_NUMBER_CANDIDATES_DISAGREE", result["issues"])
        self.assertEqual(
            [candidate["value"] for candidate in result["corroborating_candidates"]],
            ["CF000575234"],
        )

    def test_customer_reference_and_quote_are_not_po_candidates(self):
        payload = order()
        payload["purchase_order"].update(customer_reference={"value": "CLIENT-123"}, quote_number={"value": "DEV-42"})
        result = check_order_number(payload, {"anchored_fields": {"offer_number": {"value": "DEV-42"}}})
        self.assertFalse(result["issues"])

    def test_date_like_order_and_invalid_source_box_require_review(self):
        payload = order(value="10/03/2026", source="Document 10/03/2026")
        payload["purchase_order"]["number"]["evidence"]["bbox"] = [10, 10, 5, 20]
        self.assertEqual(set(check_order_number(payload, {})["issues"]),
                         {"ORDER_NUMBER_FORMAT_SUSPICIOUS", "ORDER_NUMBER_EVIDENCE_MISSING"})

    def test_core_warning_and_missing_number_are_not_hidden(self):
        payload = order()
        payload["purchase_order"]["number"]["warnings"] = ["OCR candidates disagree"]
        self.assertIn("ORDER_NUMBER_CORE_WARNING", check_order_number(payload, {})["issues"])
        self.assertEqual(check_order_number({}, {})["issues"], ["ORDER_NUMBER_MISSING"])

    def test_replacement_of_a_different_plausible_core_number_requires_review(self):
        payload = order("124 126", "COMMANDE 124 126 AV STALINGRAD")
        payload["purchase_order"]["number_original"] = {"value": "5405637 /1877"}
        result = check_order_number(payload, {})
        self.assertIn("ORDER_NUMBER_REPLACED_DIFFERENT_CORE_VALUE", result["issues"])

    def test_explicit_geometry_can_replace_a_known_weak_core_candidate(self):
        payload = order("A2601121", "N\u00b0 Commande A2601121")
        payload["purchase_order"]["number"]["warnings"] = [
            "promoted_from_explicit_geometry_anchor"
        ]
        payload["purchase_order"]["number_original"] = {
            "value": "15017119",
            "evidence": {"extraction_method": "date_piece_client_order_column"},
        }
        result = check_order_number(payload, {})
        self.assertNotIn("ORDER_NUMBER_REPLACED_DIFFERENT_CORE_VALUE", result["issues"])
        self.assertNotIn("ORDER_NUMBER_CORE_WARNING", result["issues"])

    def test_source_labeled_contact_original_does_not_create_false_disagreement(self):
        payload = order("06-252422", "Ref cde : 06-252422")
        payload["purchase_order"]["number_original"] = {
            "value": "0820003000",
            "disqualified_reason": "SOURCE_LABELED_CONTACT_NUMBER",
            "evidence": {"extraction_method": "inline_transaction_header"},
        }
        result = check_order_number(payload, {})
        self.assertNotIn("ORDER_NUMBER_REPLACED_DIFFERENT_CORE_VALUE", result["issues"])

    def test_same_page_explicit_geometry_prefix_corroborates_full_number(self):
        payload = order("PR 7 604 52249", "PR 7 604 52249")
        result = check_order_number(payload, {
            "anchored_fields": {
                "order_number": {
                    "value": "PR 7 604",
                    "page": 1,
                    "bbox": [36, 245, 147, 269],
                    "source": "geometry_explicit_order_label_v3",
                }
            }
        })
        self.assertNotIn("ORDER_NUMBER_CANDIDATES_DISAGREE", result["issues"])
        self.assertTrue(result["corroborating_candidates"][0]["agrees"])

    def test_inline_po_header_outranks_later_customer_order_reference(self):
        payload = order("250240", "250240")
        payload["purchase_order"]["number"]["evidence"].update({
            "page": 1,
            "bbox": [39, 185, 554, 206],
            "extraction_method": "inline_transaction_header",
        })
        result = check_order_number(payload, {
            "anchored_fields": {
                "order_number": {
                    "value": "4700554769",
                    "page": 1,
                    "bbox": [466, 562, 551, 574],
                    "source": "geometry_explicit_order_label_v3",
                }
            }
        })
        self.assertNotIn("ORDER_NUMBER_CANDIDATES_DISAGREE", result["issues"])
        self.assertEqual(
            result["corroborating_candidates"][0]["ignored_reason"],
            "LOWER_BODY_REFERENCE_BELOW_INLINE_PO_HEADER",
        )


if __name__ == "__main__":
    unittest.main()

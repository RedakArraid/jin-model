import unittest

from jin_runtime.studio_contract import FIELD_LABELS as STUDIO_FIELD_LABELS
from scripts.import_annotation_campaign_to_studio import draft_fields
from scripts.select_varied_annotation_sample import Candidate, choose_varied
from training.annotations.bootstrap_from_extractions import (
    ADDRESS_LABEL_SUFFIX,
    FIELD_LABELS as BOOTSTRAP_FIELD_LABELS,
    PARTY_LABELS,
    collect_candidates,
)


class AnnotationBootstrapTests(unittest.TestCase):
    def test_varied_selector_covers_families_before_repeating_them(self):
        candidates = [
            Candidate("a/1.pdf", "a", 1, "native_text", "1", "01"),
            Candidate("a/2.pdf", "a", 2, "native_text", "2-3", "02"),
            Candidate("b/1.pdf", "b", 1, "scan_or_low_text", "1", "03"),
            Candidate("c/1.pdf", "c", 4, "native_text", "4+", "04"),
        ]

        selected = choose_varied(candidates, 3)

        self.assertEqual({item.family for item in selected}, {"a", "b", "c"})
        self.assertEqual(len({item.stratum for item in selected}), 3)

    def test_every_bootstrap_label_is_accepted_by_studio(self):
        emitted = set(BOOTSTRAP_FIELD_LABELS.values())
        emitted.update(
            f"{role}_{suffix}"
            for role in PARTY_LABELS.values()
            for suffix in ADDRESS_LABEL_SUFFIX.values()
        )
        emitted.update(
            f"{role}_{suffix}"
            for role in PARTY_LABELS.values()
            for suffix in (
                "NAME", "CODE", "VAT_NUMBER", "SIREN", "SIRET", "CONTACT_PHONE",
                "CONTACT_FAX", "CONTACT_EMAIL",
            )
        )
        emitted.update(
            {
                "LINE_ITEM_DELIVERY_DATE",
                "ADDITIONAL_CHARGE_LABEL",
                "ADDITIONAL_CHARGE_AMOUNT",
            }
        )

        self.assertEqual(emitted - STUDIO_FIELD_LABELS, set())

    def test_wrapped_header_fields_use_printed_value_and_evidence(self):
        payload = {
            "order": {
                "customer_order_number": {
                    "value": "PO-123",
                    "confidence": 0.99,
                    "evidence": {"page": 2, "bbox": [10, 20, 80, 30]},
                },
                "order_date": {
                    "value": "2026-10-26",
                    "raw_value": "26/10/2026",
                    "evidence": {"page": 2},
                },
            }
        }

        candidates = collect_candidates(payload)

        number = next(item for item in candidates if item["label"] == "ORDER_NUMBER")
        date = next(item for item in candidates if item["label"] == "ORDER_DATE")
        self.assertEqual(number["value"], "PO-123")
        self.assertEqual(number["page_hint"], 2)
        self.assertEqual(number["bbox_hint"], [10.0, 20.0, 80.0, 30.0])
        self.assertEqual(date["value"], "26/10/2026")
        self.assertEqual(date["canonical_value"], "2026-10-26")

    def test_line_fields_inherit_row_geometry(self):
        payload = {
            "order": {
                "line_items": [
                    {
                        "line_number": "1",
                        "references": {"material_number": "7738112972"},
                        "description": "POMPE",
                        "quantity": 1,
                        "pricing": {"unit_price": 68.5, "net_unit_price": 68.5},
                        "amounts": {"net": 68.5, "total": 68.5},
                        "evidence": {"page": 1, "bbox": [5, 100, 590, 120]},
                    }
                ]
            }
        }

        candidates = collect_candidates(payload)

        labels = {item["label"] for item in candidates}
        self.assertTrue(
            {
                "LINE_ITEM_NUMBER",
                "LINE_ITEM_REFERENCE",
                "LINE_ITEM_DESCRIPTION",
                "LINE_ITEM_QUANTITY",
                "LINE_ITEM_UNIT_PRICE",
                "LINE_ITEM_TOTAL",
            }.issubset(labels)
        )
        reference = next(item for item in candidates if item["label"] == "LINE_ITEM_REFERENCE")
        self.assertEqual(reference["page_hint"], 1)
        self.assertEqual(reference["bbox_hint"], [5.0, 100.0, 590.0, 120.0])

    def test_party_and_address_labels_keep_business_role(self):
        payload = {
            "order": {
                "parties": {
                    "buyer": {
                        "name": "CLIENT SA",
                        "vat_number": "FR00123456789",
                        "siren": "775708373",
                        "siret": "77570837300011",
                    }
                },
                "delivery_address": {
                    "role": "ship_to",
                    "party_name": "CHANTIER A",
                    "components": {
                        "street_name": "ISAAC NEWTON",
                        "postal_code": "75001",
                        "city": "PARIS",
                    },
                },
            }
        }

        labels = {item["label"] for item in collect_candidates(payload)}

        self.assertIn("BUYER_NAME", labels)
        self.assertIn("BUYER_VAT_NUMBER", labels)
        self.assertIn("BUYER_SIREN", labels)
        self.assertIn("BUYER_SIRET", labels)
        self.assertIn("SHIP_TO_NAME", labels)
        self.assertIn("SHIP_TO_STREET_NAME", labels)
        self.assertIn("SHIP_TO_POSTAL_CODE", labels)
        self.assertIn("SHIP_TO_CITY", labels)

    def test_studio_draft_keeps_regions_and_unresolved_fields_reviewable(self):
        annotation = {
            "document_labels": {"document_type": "purchase_order"},
            "pages": [
                {
                    "page": 1,
                    "regions": [
                        {
                            "label": "ORDER_NUMBER",
                            "value": "PO-123",
                            "source_path": "order.customer_order_number",
                            "bbox": [10, 20, 80, 30],
                        }
                    ],
                }
            ],
            "generation_report": {
                "unresolved": [
                    {
                        "label": "DELIVERY_DATE",
                        "value": "2026-11-01",
                        "path": "order.requested_delivery_date",
                        "reason": "not_found",
                    }
                ]
            },
        }

        fields = draft_fields(annotation)

        self.assertEqual(fields[0]["label"], "DOCUMENT_TYPE")
        order = next(item for item in fields if item["label"] == "ORDER_NUMBER")
        delivery = next(item for item in fields if item["label"] == "DELIVERY_DATE")
        self.assertEqual(order["status"], "UNREVIEWED")
        self.assertEqual(order["evidence"]["bbox"], [10, 20, 80, 30])
        self.assertEqual(delivery["status"], "AMBIGUOUS")
        self.assertNotIn("evidence", delivery)

    def test_studio_draft_turns_degenerate_region_into_ambiguous_field(self):
        annotation = {
            "document_labels": {"document_type": "purchase_order"},
            "pages": [
                {
                    "page": 1,
                    "regions": [
                        {
                            "label": "LINE_ITEM_QUANTITY",
                            "value": "1",
                            "source_path": "order.line_items[0].quantity",
                            "bbox": [100, 100, 100, 110],
                        }
                    ],
                }
            ],
        }

        quantity = next(
            item for item in draft_fields(annotation)
            if item["label"] == "LINE_ITEM_QUANTITY"
        )

        self.assertEqual(quantity["status"], "AMBIGUOUS")
        self.assertNotIn("evidence", quantity)


if __name__ == "__main__":
    unittest.main()

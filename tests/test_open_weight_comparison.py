import unittest

from benchmarks.open_weight_comparison.adapters import available_model_names
from benchmarks.open_weight_comparison.common import (
    jin_clean_to_common,
    parse_json_object,
)
from benchmarks.open_weight_comparison.metrics import (
    box_iou,
    evaluate_extraction,
)
from benchmarks.open_weight_comparison.prompts import extraction_prompt


class OpenWeightComparisonTests(unittest.TestCase):
    def test_registry_contains_expected_models(self):
        names = set(available_model_names())
        self.assertTrue({
            "jin",
            "embeddinggemma2_zone",
            "granite_docling_258m",
            "glm_ocr",
            "paddleocr_vl_1_6",
            "qwen3_vl_2b",
        }.issubset(names))

    def test_prompt_forbids_invented_values(self):
        prompt = extraction_prompt().lower()
        self.assertIn("do not", prompt)
        self.assertIn("invented", prompt)
        self.assertIn("order_number", prompt)
        self.assertIn("ship_to", prompt)

    def test_json_parser_accepts_fenced_json(self):
        payload, error = parse_json_object('```json\n{"order_number":"A1"}\n```')
        self.assertIsNone(error)
        self.assertEqual(payload["order_number"], "A1")

    def test_metrics_count_missing_and_hallucination(self):
        truth = {
            "order_number": "PO-001",
            "order_date": "2026-01-02",
            "totals": {"net": "100,00"},
        }
        prediction = {
            "order_number": "PO-001",
            "totals": {"net": "100.00", "gross": "120.00"},
        }
        metrics = evaluate_extraction(prediction, truth)
        self.assertEqual(metrics["exact_match_count"], 2)
        self.assertEqual(metrics["missing_field_count"], 1)
        self.assertGreater(metrics["hallucinated_field_count"], 0)

    def test_zone_metrics_match_by_geometry(self):
        truth = {
            "spatial": {
                "zones": [
                    {"zone_type": "SHIP_TO", "page": 1, "bbox": [100, 100, 400, 300]}
                ]
            }
        }
        prediction = {
            "spatial": {
                "zones": [
                    {"zone_type": "SHIP_TO", "page": 1, "bbox": [110, 110, 390, 290]}
                ]
            }
        }
        metrics = evaluate_extraction(prediction, truth)
        self.assertEqual(metrics["zone_type_accuracy"], 1.0)
        self.assertGreater(metrics["mean_zone_bbox_iou"], 0.7)

    def test_box_iou_identity(self):
        self.assertEqual(box_iou([0, 0, 10, 10], [0, 0, 10, 10]), 1.0)

    def test_jin_clean_normalization(self):
        clean = {
            "document": {"type": "purchase_order"},
            "order": {
                "customer_order_number": {"value": "PO-01"},
                "order_date": {"value": "2026-01-02"},
                "parties": {
                    "supplier": {"name": "Supplier", "address": {"postal_code": "93711", "city": "DRANCY"}}
                },
                "line_items": [
                    {
                        "references": {"material_number": {"value": "MAT1"}},
                        "description": "Item",
                        "quantity": 2,
                        "pricing": {"unit_price": {"value": "10.00"}},
                        "amounts": {"line_total": {"value": "20.00"}},
                    }
                ],
                "totals": {"total_net": {"value": "20.00"}},
            },
        }
        out = jin_clean_to_common(clean)
        self.assertEqual(out["order_number"], "PO-01")
        self.assertEqual(out["parties"]["supplier"]["address"]["city"], "DRANCY")
        self.assertEqual(out["lines"][0]["reference"], "MAT1")
        self.assertEqual(out["totals"]["net"], "20.00")


if __name__ == "__main__":
    unittest.main()

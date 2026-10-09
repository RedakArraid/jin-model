import unittest

from benchmarks.open_weight_comparison.adapters import available_model_names
from benchmarks.open_weight_comparison.common import (
    ground_truth_is_reviewed,
    ground_truth_review_status,
    jin_clean_to_common,
    parse_json_object,
)
from benchmarks.open_weight_comparison.decision import (
    build_field_comparison_rows,
    build_head_to_head_rows,
    head_to_head_summary,
    render_decision_report,
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
            "qwen3_vl_4b",
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

    def test_ground_truth_review_metadata_is_not_scored(self):
        truth = {
            "_review": {
                "status": "reviewed",
                "reviewer": "human",
                "reviewed_at": "2026-10-09",
            },
            "order_number": "PO-001",
        }
        metrics = evaluate_extraction({"order_number": "PO-001"}, truth)
        self.assertEqual(metrics["truth_field_count"], 1)
        self.assertEqual(metrics["exact_match_count"], 1)
        self.assertTrue(ground_truth_is_reviewed(truth))
        self.assertEqual(ground_truth_review_status(truth), "reviewed")
        self.assertFalse(
            ground_truth_is_reviewed({"_review": {"status": "needs_review"}})
        )

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

    def test_head_to_head_reports_jin_and_candidate_field_wins(self):
        truth = {
            "order_number": "PO-001",
            "order_date": "2026-01-02",
            "totals": {"net": "100.00"},
        }
        jin_prediction = {
            "order_number": "WRONG",
            "order_date": "2026-01-02",
            "totals": {"net": "100.00"},
        }
        qwen_prediction = {
            "order_number": "PO-001",
            "order_date": "WRONG",
            "totals": {"net": "100.00"},
        }
        report = {
            "baseline": {"model": "jin", "available": True},
            "models": {
                "jin": {
                    "track": "direct_ie",
                    "summary": {
                        "completed": 1,
                        "field_exact_match": 2 / 3,
                        "field_token_f1": 2 / 3,
                        "missing_field_rate": 0.0,
                        "hallucination_rate": 0.0,
                        "line_item_reference_recall": None,
                    },
                    "documents": [{
                        "filename": "doc.pdf",
                        "metrics": evaluate_extraction(jin_prediction, truth),
                        "resources": {"latency_seconds": 1.0, "peak_rss_mb": 100.0},
                    }],
                    "comparison_vs_jin": {"is_baseline": True},
                },
                "qwen3_vl_2b": {
                    "track": "direct_ie",
                    "summary": {
                        "completed": 1,
                        "field_exact_match": 2 / 3,
                        "field_token_f1": 2 / 3,
                        "missing_field_rate": 0.0,
                        "hallucination_rate": 0.0,
                        "line_item_reference_recall": None,
                    },
                    "documents": [{
                        "filename": "doc.pdf",
                        "metrics": evaluate_extraction(qwen_prediction, truth),
                        "resources": {"latency_seconds": 2.0, "peak_rss_mb": 200.0},
                    }],
                    "comparison_vs_jin": {
                        "comparable": True,
                        "baseline_model": "jin",
                        "delta": {"field_exact_match": 0.0, "field_token_f1": 0.0},
                    },
                },
            },
        }
        fields = build_field_comparison_rows(report)
        outcomes = {row["path"]: row["outcome"] for row in fields}
        self.assertEqual(outcomes["order_number"], "CANDIDATE_WIN")
        self.assertEqual(outcomes["order_date"], "JIN_WIN")
        head = build_head_to_head_rows(report, fields)
        self.assertEqual(len(head), 1)
        self.assertEqual(head[0]["comparison_status"], "COMPARABLE")
        self.assertEqual(head[0]["field_candidate_wins"], 1)
        self.assertEqual(head[0]["field_jin_wins"], 1)
        self.assertEqual(head[0]["latency_ratio_vs_jin"], 2.0)
        summary = head_to_head_summary(head)
        self.assertEqual(summary["qwen3_vl_2b"]["documents_compared"], 1)
        markdown = render_decision_report(report, head, fields)
        self.assertIn("qwen3_vl_2b vs JIN", markdown)
        self.assertIn("order_number", markdown)

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

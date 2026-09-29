import json
import tempfile
import unittest
from pathlib import Path

from jin_runtime.learning import (
    LearningConfig,
    StatisticalAddressLearner,
    canonical_components,
    label_tokens_from_components,
)


class LearningTests(unittest.TestCase):
    def test_component_aliases(self):
        got = canonical_components({"address": {"numero": "30", "commune": "Tours", "code_insee": "37214"}})
        self.assertEqual(got["house_number"], "30")
        self.assertEqual(got["city"], "Tours")
        self.assertEqual(got["insee_code"], "37214")

    def test_token_labelling(self):
        text = "BATIMENT QUOFI 30 RUE DES GRANDS MORTIERS 37705 ST PIERRE DES CORPS"
        tokens, labels = label_tokens_from_components(
            text,
            {
                "address": {
                    "building": "BATIMENT QUOFI",
                    "house_number": "30",
                    "street_type": "RUE",
                    "street_name": "DES GRANDS MORTIERS",
                    "postal_code": "37705",
                    "city": "ST PIERRE DES CORPS",
                }
            },
        )
        self.assertEqual(labels[tokens.index("30")], "house_number")
        self.assertEqual(labels[tokens.index("37705")], "postal_code")
        self.assertIn("building", labels)
        self.assertIn("street_name", labels)
        self.assertIn("city", labels)

    def test_training_and_enrichment(self):
        config = LearningConfig(
            role_override_threshold=0.70,
            component_fill_threshold=0.55,
            min_role_examples=4,
            min_token_examples=20,
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            feedback = tmp / "feedback.jsonl"
            events = []
            examples = [
                ("LIVRAISON 30 RUE DES GRANDS MORTIERS 37705 ST PIERRE DES CORPS", "ship_to", "30", "37705", "ST PIERRE DES CORPS"),
                ("ADRESSE DE LIVRAISON 8 AVENUE DE LYON 69003 LYON", "ship_to", "8", "69003", "LYON"),
                ("FACTURATION 12 BOULEVARD VOLTAIRE 75011 PARIS", "bill_to", "12", "75011", "PARIS"),
                ("ADRESSE DE FACTURATION 42 RUE NATIONALE 59000 LILLE", "bill_to", "42", "59000", "LILLE"),
                ("LIVRAISON 14 ROUTE DE GRENOBLE 38100 GRENOBLE", "ship_to", "14", "38100", "GRENOBLE"),
                ("FACTURATION 5 AVENUE FOCH 67000 STRASBOURG", "bill_to", "5", "67000", "STRASBOURG"),
            ]
            for idx, (text, role, number, postal, city) in enumerate(examples):
                events.append(
                    {
                        "document_id": f"doc-{idx}",
                        "extraction": {"business_addresses": [{"formatted_address": text, "role": "unknown", "role_confidence": 0.2}]},
                        "corrections": {
                            "business_addresses": [
                                {
                                    "index": 0,
                                    "formatted_address": text,
                                    "role": role,
                                    "address": {
                                        "house_number": number,
                                        "postal_code": postal,
                                        "city": city,
                                    },
                                }
                            ]
                        },
                    }
                )
            feedback.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")

            learner = StatisticalAddressLearner(feedback, tmp / "model", config)
            status = learner.train()
            self.assertTrue(status["trained"])
            self.assertTrue(status["role_model_trained"])
            self.assertTrue(status["component_model_trained"])

            role = learner.predict_role("LIVRAISON SERVICE CLIENT 22 RUE TEST 75001 PARIS")
            self.assertIsNotNone(role)
            self.assertEqual(role["role"], "ship_to")

            result = learner.enrich(
                {
                    "business_addresses": [
                        {
                            "formatted_address": "LIVRAISON 30 RUE DES GRANDS MORTIERS 37705 ST PIERRE DES CORPS",
                            "role": "unknown",
                            "role_confidence": 0.1,
                            "address": {},
                        }
                    ]
                }
            )
            block = result["business_addresses"][0]
            self.assertEqual(block["role"], "ship_to")
            self.assertIn("statistical_learning", block)
            self.assertIn("statistical_memory", result["learning"])


if __name__ == "__main__":
    unittest.main()

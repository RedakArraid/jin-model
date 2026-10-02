from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jin_runtime.feedback_auth import (
    feedback_authorization_error,
    feedback_ingestion_enabled,
    feedback_retrain_on_write,
)
from jin_runtime.feedback_contract import (
    FeedbackValidationError,
    normalize_human_feedback,
)
from jin_runtime.learning import StatisticalAddressLearner


def _feedback(*, annotation_id: str = "annotation-0001", city: str = "Paris"):
    text = "LIVRAISON 10 RUE DE LA PAIX 75001 PARIS"
    return {
        "schema_version": 1,
        "annotation_id": annotation_id,
        "document_sha256": hashlib.sha256(b"pdf-one").hexdigest(),
        "validation": {
            "status": "human_validated",
            "reviewed_by": "quality-owner",
            "reviewed_at": "2026-10-02T08:00:00+02:00",
            "source": "geniecommande",
        },
        "extraction": {
            "business_addresses": [
                {
                    "formatted_address": text,
                    "role": "unknown",
                    "role_confidence": 0.2,
                }
            ]
        },
        "corrections": {
            "business_addresses": [
                {
                    "index": 0,
                    "formatted_address": text,
                    "role": "ship_to",
                    "address": {
                        "house_number": "10",
                        "postal_code": "75001",
                        "city": city,
                    },
                }
            ]
        },
    }


class FeedbackGuardTests(unittest.TestCase):
    def test_requires_explicit_human_validation_and_document_hash(self):
        payload = _feedback()
        del payload["validation"]
        with self.assertRaisesRegex(FeedbackValidationError, "validation is required"):
            normalize_human_feedback(payload)

        payload = _feedback()
        payload["validation"]["status"] = "model_predicted"
        with self.assertRaisesRegex(FeedbackValidationError, "human_validated"):
            normalize_human_feedback(payload)

        payload = _feedback()
        payload["document_sha256"] = "not-a-hash"
        with self.assertRaisesRegex(FeedbackValidationError, "SHA-256"):
            normalize_human_feedback(payload)

        payload = _feedback()
        payload["corrections"]["business_addresses"][0]["role"] = "delivery label"
        learner = StatisticalAddressLearner()
        with self.assertRaisesRegex(FeedbackValidationError, "not canonical"):
            learner.record_feedback(payload, retrain=False)

        payload = _feedback()
        payload["corrections"]["business_addresses"][0]["index"] = -1
        with self.assertRaisesRegex(FeedbackValidationError, "out of range"):
            learner.record_feedback(payload, retrain=False)

    def test_feedback_is_normalized_auditable_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feedback_path = root / "feedback.jsonl"
            learner = StatisticalAddressLearner(
                feedback_path=feedback_path,
                model_dir=root / "models",
            )

            first = learner.record_feedback(_feedback(), retrain=False)
            duplicate = learner.record_feedback(_feedback(), retrain=False)

            self.assertTrue(first["feedback_recorded"])
            self.assertEqual(first["feedback_records_on_disk"], 1)
            self.assertEqual(first["pending_feedback_records"], 1)
            self.assertTrue(duplicate["feedback_duplicate"])
            rows = feedback_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 1)
            stored = json.loads(rows[0])
            self.assertEqual(
                stored["validation"],
                {
                    "status": "human_validated",
                    "reviewed_by": "quality-owner",
                    "reviewed_at": "2026-10-02T06:00:00Z",
                    "source": "geniecommande",
                },
            )
            self.assertTrue(stored["annotation_digest"].startswith("sha256:"))
            self.assertTrue(stored["recorded_at"])

            tampered = dict(stored)
            tampered["document_sha256"] = hashlib.sha256(b"another-pdf").hexdigest()
            with self.assertRaisesRegex(FeedbackValidationError, "does not match"):
                normalize_human_feedback(tampered)

            with self.assertRaisesRegex(
                FeedbackValidationError, "different content"
            ):
                learner.record_feedback(_feedback(city="Lyon"), retrain=False)

    def test_training_ignores_legacy_or_unreviewed_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            feedback_path = root / "feedback.jsonl"
            feedback_path.write_text(
                "\n".join(
                    [
                        json.dumps(_feedback()),
                        json.dumps(
                            {
                                "extraction": {"business_addresses": []},
                                "corrections": {"business_addresses": []},
                            }
                        ),
                        "not-json",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            learner = StatisticalAddressLearner(
                feedback_path=feedback_path,
                model_dir=root / "models",
            )

            status = learner.train()

            self.assertEqual(status["feedback_records"], 3)
            self.assertEqual(status["events"], 1)
            self.assertEqual(status["rejected_feedback_records"], 2)
            self.assertEqual(status["malformed_feedback_records"], 1)
            self.assertEqual(status["documents_with_labels"], 1)

    def test_endpoint_is_disabled_without_secret_and_uses_bearer_token(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(feedback_ingestion_enabled())
            self.assertEqual(feedback_authorization_error({})[0], 503)

        with patch.dict(
            os.environ,
            {"JIN_FEEDBACK_TOKEN": "long-random-feedback-secret"},
            clear=True,
        ):
            self.assertTrue(feedback_ingestion_enabled())
            self.assertFalse(feedback_retrain_on_write())
            self.assertEqual(feedback_authorization_error({})[0], 401)
            self.assertIsNone(
                feedback_authorization_error(
                    {"authorization": "Bearer long-random-feedback-secret"}
                )
            )

        with patch.dict(
            os.environ,
            {"JIN_FEEDBACK_RETRAIN_ON_WRITE": "true"},
            clear=True,
        ):
            self.assertTrue(feedback_retrain_on_write())


if __name__ == "__main__":
    unittest.main()

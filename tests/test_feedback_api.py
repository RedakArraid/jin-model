from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from jin_runtime import standalone_app
from jin_runtime.learning import StatisticalAddressLearner


def _payload() -> dict:
    address = "10 RUE DE LA PAIX 75001 PARIS"
    return {
        "schema_version": 1,
        "annotation_id": "annotation-api-0001",
        "document_sha256": hashlib.sha256(b"api-pdf").hexdigest(),
        "validation": {
            "status": "human_validated",
            "reviewed_by": "api-reviewer",
            "reviewed_at": "2026-10-02T08:00:00Z",
            "source": "unit-test",
        },
        "extraction": {
            "business_addresses": [
                {"formatted_address": address, "role": "unknown"}
            ]
        },
        "corrections": {
            "business_addresses": [
                {
                    "index": 0,
                    "formatted_address": address,
                    "role": "ship_to",
                    "address": {"postal_code": "75001", "city": "PARIS"},
                }
            ]
        },
    }


class FeedbackApiTests(unittest.TestCase):
    def test_endpoint_fails_closed_then_accepts_idempotent_reviewed_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            learner = StatisticalAddressLearner(
                feedback_path=root / "feedback.jsonl",
                model_dir=root / "models",
            )
            with patch.object(standalone_app, "learner", learner):
                with patch.dict(os.environ, {}, clear=True):
                    with TestClient(standalone_app.app) as client:
                        response = client.post("/feedback", json=_payload())
                        self.assertEqual(response.status_code, 503)

                environment = {
                    "JIN_FEEDBACK_TOKEN": "long-random-feedback-secret",
                    "JIN_FEEDBACK_RETRAIN_ON_WRITE": "0",
                }
                with patch.dict(os.environ, environment, clear=True):
                    with TestClient(standalone_app.app) as client:
                        unauthorized = client.post("/feedback", json=_payload())
                        self.assertEqual(unauthorized.status_code, 401)
                        self.assertEqual(
                            unauthorized.headers.get("www-authenticate"), "Bearer"
                        )

                        invalid = client.post(
                            "/feedback",
                            headers={"Authorization": "Bearer long-random-feedback-secret"},
                            json={},
                        )
                        self.assertEqual(invalid.status_code, 422)

                        headers = {
                            "Authorization": "Bearer long-random-feedback-secret"
                        }
                        accepted = client.post(
                            "/feedback", headers=headers, json=_payload()
                        )
                        duplicate = client.post(
                            "/feedback", headers=headers, json=_payload()
                        )

                        self.assertEqual(accepted.status_code, 200)
                        self.assertTrue(accepted.json()["accepted"])
                        self.assertFalse(accepted.json()["duplicate"])
                        self.assertTrue(
                            accepted.json()["learning"]["pending_retrain"]
                        )
                        self.assertEqual(duplicate.status_code, 200)
                        self.assertTrue(duplicate.json()["duplicate"])


if __name__ == "__main__":
    unittest.main()

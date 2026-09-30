import json
import re
import unittest
from pathlib import Path


class ModelManifestTests(unittest.TestCase):
    def test_ready_models_have_size_and_sha256(self):
        manifest = json.loads(
            Path("models/JIN_MODELS_MANIFEST.json").read_text(
                encoding="utf-8"
            )
        )
        ready = [
            model
            for model in manifest["models"]
            if model["status"] == "ready"
        ]
        self.assertEqual(len(ready), 3)
        for model in ready:
            self.assertGreater(model["bytes"], 0)
            self.assertRegex(model["sha256"], r"^[0-9a-f]{64}$")

    def test_address_model_is_explicitly_optional(self):
        manifest = json.loads(
            Path("models/JIN_MODELS_MANIFEST.json").read_text(
                encoding="utf-8"
            )
        )
        address = next(
            model
            for model in manifest["models"]
            if model["file"] == "address_models.joblib"
        )
        self.assertEqual(
            address["status"], "optional_missing_training_input"
        )
        self.assertIn("learning_feedback.jsonl", address["required_input"])


if __name__ == "__main__":
    unittest.main()

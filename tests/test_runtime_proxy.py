import importlib
import sys
import types
import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from jin_runtime import __version__


fake_uda = types.ModuleType("uda")
fake_api = types.ModuleType("uda.api")
core_app = FastAPI()


@core_app.get("/health")
def core_health():
    return {"status": "ok", "version": "core-test"}


@core_app.post("/extract")
async def core_extract(request: Request):
    await request.body()
    return {
        "business_addresses": [
            {
                "role": "ship_to",
                "role_confidence": 0.99,
                "formatted_address": "30 RUE DES GRANDS MORTIERS 37705 ST PIERRE DES CORPS",
                "address": {"house_number": "30", "postal_code": "37705"},
            }
        ]
    }


fake_api.app = core_app
sys.modules.setdefault("uda", fake_uda)
sys.modules.setdefault("uda.api", fake_api)
runtime_app = importlib.import_module("jin_runtime.app")


class RuntimeProxyTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(runtime_app.app)

    def test_health_keeps_core_and_adds_runtime_layer(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["version"], "core-test")
        self.assertEqual(body["runtime_layer"]["version"], __version__)
        self.assertIn("statistical_learning", body["runtime_layer"])
        self.assertIn("local_ban_reference", body["runtime_layer"])

    def test_extract_keeps_core_payload_and_adds_learning_metadata(self):
        response = self.client.post("/extract", content=b"dummy")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["business_addresses"][0]["address"]["postal_code"], "37705")
        self.assertEqual(body["runtime_layer_version"], __version__)
        self.assertIn("statistical_memory", body["learning"])
        self.assertIn("normalized_output", body)

    def test_clean_view_returns_only_versioned_contract(self):
        response = self.client.post("/extract?view=clean", content=b"dummy")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["schema_version"], "jin-clean-extraction-v1")
        self.assertNotIn("raw_text", body)
        self.assertNotIn("weak_field_suggestions", body)

    def test_clean_contract_schema_is_published(self):
        response = self.client.get("/schemas/jin-clean-extraction-v1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["$id"], "/schemas/jin-clean-extraction-v1")


if __name__ == "__main__":
    unittest.main()

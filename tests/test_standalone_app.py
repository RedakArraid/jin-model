import unittest

from jin_runtime import standalone_app


class StandaloneAppTests(unittest.TestCase):
    def test_health_does_not_require_uda_core(self):
        payload = standalone_app.health()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["mode"], "standalone")
        self.assertIn("cpu_pdf_router", payload["models"])
        self.assertIn("weak_field_router", payload["models"])
        self.assertIn("corpus_document_router", payload["models"])

    def test_ready_requires_pdf_and_field_models(self):
        payload = standalone_app.health()
        expected = bool(
            standalone_app.pdf_router.loaded
            and standalone_app.field_router.loaded
        )
        self.assertEqual(payload["ready"], expected)


if __name__ == "__main__":
    unittest.main()

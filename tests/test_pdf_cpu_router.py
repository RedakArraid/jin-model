import tempfile
import unittest
from pathlib import Path

import fitz

from jin_runtime.pdf_cpu_router import CpuPdfRouter, extract_pdf_features_bytes


class CpuPdfRouterTests(unittest.TestCase):
    @staticmethod
    def make_pdf() -> bytes:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "COMMANDE 12345")
        page.insert_text((72, 140), "30 RUE DES GRANDS MORTIERS")
        page.insert_text((72, 170), "37705 ST PIERRE DES CORPS")
        data = doc.tobytes()
        doc.close()
        return data

    def test_feature_extraction_from_pdf_bytes(self):
        text, visual, layout, summary = extract_pdf_features_bytes(self.make_pdf())
        self.assertIn("COMMANDE", text)
        self.assertEqual(visual.shape, (292,))
        self.assertEqual(layout.shape, (25,))
        self.assertEqual(summary["pages"], 1)
        self.assertGreater(summary["words"], 0)

    def test_non_pdf_is_rejected(self):
        with self.assertRaises(ValueError):
            extract_pdf_features_bytes(b"not-a-pdf")

    def test_missing_model_degrades_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            router = CpuPdfRouter(Path(td) / "missing.joblib")
            self.assertFalse(router.loaded)
            self.assertFalse(router.status()["loaded"])
            self.assertTrue(router.status()["cpu_only"])


if __name__ == "__main__":
    unittest.main()

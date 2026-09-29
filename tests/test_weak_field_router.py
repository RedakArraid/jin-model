import tempfile
import unittest
from pathlib import Path

import fitz

from jin_runtime.weak_field_router import WeakFieldRouter, _extract_first_page_lines


class WeakFieldRouterTests(unittest.TestCase):
    @staticmethod
    def make_pdf() -> bytes:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "30 RUE DES GRANDS MORTIERS")
        page.insert_text((72, 130), "37705 ST PIERRE DES CORPS")
        data = doc.tobytes()
        doc.close()
        return data

    def test_native_pdf_words_are_preferred(self):
        lines, source = _extract_first_page_lines(self.make_pdf(), "fra+eng+deu")
        self.assertEqual(source, "native_pdf_text")
        text = " ".join(token["text"] for line in lines for token in line)
        self.assertIn("MORTIERS", text)
        self.assertIn("37705", text)

    def test_missing_model_degrades_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            router = WeakFieldRouter(Path(td) / "missing.joblib")
            self.assertFalse(router.loaded)
            status = router.status()
            self.assertTrue(status["weak_supervision"])
            self.assertTrue(status["requires_review"])


if __name__ == "__main__":
    unittest.main()

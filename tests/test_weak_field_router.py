import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import fitz
import numpy as np

from jin_runtime.weak_field_router import (
    WeakFieldRouter,
    _extract_first_page_lines,
    _native_words_are_rotated,
)


class WeakFieldRouterTests(unittest.TestCase):
    @staticmethod
    def make_test_router(labels=None):
        """Exercise postprocessing independently of any deployed model weights."""
        labels = labels or {}
        router = WeakFieldRouter.__new__(WeakFieldRouter)
        router.model_path = Path("unused-test-model.joblib")
        router.bundle = {"version": "test"}
        router.confidence_threshold = 0.80
        router.ocr_languages = "eng"
        router.vectorizer = Mock()
        router.vectorizer.transform.side_effect = lambda contexts: contexts
        router.model = Mock()
        router.model.predict.side_effect = lambda contexts: np.asarray([
            labels.get(context.split("tok=", 1)[1].split(" ", 1)[0], "O")
            for context in contexts
        ])
        router.model.predict_proba.side_effect = lambda contexts: np.full(
            (len(contexts), 1), 0.99
        )
        return router

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

    def test_overwhelmingly_vertical_native_words_require_rendered_ocr(self):
        vertical = [
            (10.0 + index, 20.0, 18.0 + index, 70.0, f"TOKEN{index}", 0, index, 0)
            for index in range(24)
        ]
        horizontal = [
            (10.0, 20.0 + index, 70.0, 28.0 + index, f"TOKEN{index}", 0, index, 0)
            for index in range(24)
        ]
        mostly_horizontal = horizontal + vertical[:4]

        self.assertTrue(_native_words_are_rotated(vertical))
        self.assertFalse(_native_words_are_rotated(horizontal))
        self.assertFalse(_native_words_are_rotated(mostly_horizontal))

    def test_missing_model_degrades_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            router = WeakFieldRouter(Path(td) / "missing.joblib")
            self.assertFalse(router.loaded)
            status = router.status()
            self.assertTrue(status["weak_supervision"])
            self.assertTrue(status["requires_review"])

    @staticmethod
    def make_order_pages(order_numbers=("CMD123456", "CMD123456")):
        pages = []
        with fitz.open() as document:
            for index, number in enumerate(order_numbers):
                page = document.new_page(width=595, height=842)
                page.insert_text((40, 70), "N de commande")
                page.insert_text((150, 70), number)
                page.insert_text((40, 150), "Adresse de livraison")
                page.insert_text((40, 175), "30 RUE DES GRANDS MORTIERS")
                page.insert_text((40, 195), "37705 ST PIERRE DES CORPS")
                for x, label, value in (
                    (40, "Article", "ABC123"),
                    (145, "Designation", "CHAUDIERE"),
                    (355, "Qte", "2"),
                    (425, "Prix net", "100,00"),
                    (495, "Montant HT", "200,00"),
                ):
                    page.insert_text((x, 330), label)
                    page.insert_text((x, 360), value)
                if index == len(order_numbers) - 1:
                    page.insert_text((375, 650), "Total HT : 200,00 EUR")
                with fitz.open() as single_page:
                    single_page.insert_pdf(document, from_page=index, to_page=index)
                    pages.append(single_page.tobytes())
            return document.tobytes(), pages

    def test_all_pages_keep_field_boxes_and_unique_structural_ids(self):
        data, pages = self.make_order_pages()
        router = self.make_test_router({"ABC123": "LINE_ITEM_PRODUCT_CODE"})
        result = router.predict_bytes(data)
        second_page = router.predict_bytes(pages[1])
        first_page = router.predict_bytes(pages[0])

        self.assertEqual(result["pages_processed"], 2)
        self.assertNotIn("total_gross", first_page["anchored_fields"])
        self.assertNotIn("total_net", first_page["anchored_fields"])
        total = result["anchored_fields"]["total_net"]
        self.assertEqual(total["page"], 2)
        self.assertEqual(total["value"], "200,00")
        self.assertEqual(total["bbox"], second_page["anchored_fields"]["total_net"]["bbox"])
        self.assertTrue(total["cell_id"].startswith("p2_"))
        self.assertTrue(total["requires_review"])
        self.assertEqual(result["field_conflicts"], [])
        self.assertEqual(
            [item["page"] for item in result["anchored_field_candidates"]["order_number"]],
            [1, 2],
        )
        self.assertEqual({item["page"] for item in result["address_candidates"]}, {1, 2})
        self.assertEqual({item["page"] for item in result["spans"]}, {1, 2})

        zone_ids, cell_ids, row_ids, subzone_ids = [], [], [], []
        page_two_regions = [region for region in result["page_regions"] if region["page"] == 2]
        self.assertEqual(len(page_two_regions), len(second_page["page_regions"]))
        for actual, expected in zip(page_two_regions, second_page["page_regions"]):
            for key in ("structural_bbox", "content_bbox", "content_regions", "search_bbox"):
                self.assertEqual(actual[key], expected[key])
        for region in result["page_regions"]:
            prefix = f"p{region['page']}_"
            self.assertTrue(region["zone_id"].startswith(prefix))
            zone_ids.append(region["zone_id"])
            owned_cells = {cell["cell_id"] for cell in region["cells"]}
            for key, id_key, all_ids in (
                ("cells", "cell_id", cell_ids),
                ("rows", "row_id", row_ids),
                ("subzones", "subzone_id", subzone_ids),
            ):
                for record in region.get(key, []):
                    self.assertEqual(record["page"], region["page"])
                    self.assertTrue(record[id_key].startswith(prefix))
                    self.assertTrue(record["requires_review"])
                    all_ids.append(record[id_key])
                    if key == "rows":
                        self.assertTrue(set(record["cells"]).issubset(owned_cells))
        for identifiers in (zone_ids, cell_ids, row_ids, subzone_ids):
            self.assertTrue(identifiers)
            self.assertEqual(len(identifiers), len(set(identifiers)))
        for span in result["spans"]:
            if span.get("zone_id"):
                self.assertIn(span["zone_id"], zone_ids)
            if span.get("cell_id"):
                self.assertIn(span["cell_id"], cell_ids)

    def test_conflicting_page_fields_are_retained_without_silent_selection(self):
        data, _ = self.make_order_pages(("CMD123456", "CMD987654"))
        result = self.make_test_router().predict_bytes(data)
        self.assertNotIn("order_number", result["anchored_fields"])
        conflict = next(item for item in result["field_conflicts"] if item["field"] == "order_number")
        self.assertEqual([item["value"] for item in conflict["candidates"]], ["CMD123456", "CMD987654"])
        self.assertEqual([item["page"] for item in conflict["candidates"]], [1, 2])
        self.assertTrue(conflict["requires_review"])

    def test_postal_code_and_city_model_spans_accept_numeric_postal_code(self):
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((40, 100), "37705 TOURS")
            data = document.tobytes()
        router = self.make_test_router({
            "37705": "ADDRESS_POSTAL_CODE",
            "TOURS": "ADDRESS_CITY",
        })
        spans = router.predict_bytes(data)["spans"]
        self.assertEqual(
            [(span["label"], span["value"]) for span in spans],
            [("ADDRESS_POSTAL_CODE", "37705"), ("ADDRESS_CITY", "TOURS")],
        )

    def test_page_id_remapping_does_not_change_document_values(self):
        result = {
            "spans": [{"value": "p1_product", "page": 1, "zone_id": "p1_order_metadata_01"}],
            "anchored_fields": {},
            "address_candidates": [],
            "model_address_candidates": [],
            "page_regions": [],
        }
        WeakFieldRouter._restore_page_provenance(result, 2)
        self.assertEqual(result["spans"][0]["value"], "p1_product")
        self.assertEqual(result["spans"][0]["zone_id"], "p2_order_metadata_01")

    def test_image_only_second_page_uses_ocr_and_preserves_page_coordinates(self):
        with fitz.open() as document:
            page = document.new_page(width=595, height=842)
            page.insert_text((40, 70), "Commande")
            document.new_page(width=595, height=842)
            data = document.tobytes()
        ocr = {
            "text": ["Date", "18/10/2024"],
            "conf": [95, 95],
            "left": [80, 200],
            "top": [180, 180],
            "width": [75, 140],
            "height": [24, 24],
            "block_num": [1, 1],
            "line_num": [1, 1],
            "word_num": [1, 2],
        }
        with patch(
            "jin_runtime.weak_field_router.pytesseract.image_to_data",
            return_value=ocr,
        ) as recognize:
            result = self.make_test_router().predict_bytes(data)
        recognize.assert_called_once()
        self.assertEqual(result["text_source"], "mixed")
        self.assertEqual(result["page_text_sources"], [
            {"page": 1, "text_source": "native_pdf_text"},
            {"page": 2, "text_source": "tesseract_ocr"},
        ])
        date = result["anchored_fields"]["order_date"]
        self.assertEqual(date["page"], 2)
        self.assertEqual(date["value"], "18/10/2024")
        self.assertEqual(date["bbox"], [168, 107, 286, 121])


if __name__ == "__main__":
    unittest.main()

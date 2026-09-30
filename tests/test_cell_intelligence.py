import unittest

import fitz

from jin_runtime.cell_intelligence import (
    _line_item_cells,
    enrich_page_regions_with_cells,
)
from jin_runtime.spatial_metrics import box_iou, evaluate_spatial_items
from jin_runtime.weak_field_router import _extract_first_page_lines
from jin_runtime.zone_intelligence import _normalize_box, _visual_rows


class CellIntelligenceTests(unittest.TestCase):
    @staticmethod
    def make_pdf() -> bytes:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)

        # Address block.
        page.insert_text((40, 90), "1 CHEMIN DES PLANS D'EAU")
        page.insert_text((40, 112), "ZA DE ROGERVILLE")
        page.insert_text((40, 134), "76430 OUDALLE")

        # Vector header cells for line items.
        boundaries = [30, 105, 335, 410, 470, 565]
        for left, right in zip(boundaries[:-1], boundaries[1:]):
            page.draw_rect(fitz.Rect(left, 300, right, 325), width=0.5)
        page.insert_text((38, 317), "Article")
        page.insert_text((115, 317), "Designation")
        page.insert_text((345, 317), "Qte")
        page.insert_text((420, 317), "Prix net")
        page.insert_text((480, 317), "Montant HT")
        page.insert_text((38, 350), "ABC123")
        page.insert_text((115, 350), "CHAUDIERE TEST")
        page.insert_text((345, 350), "2")
        page.insert_text((420, 350), "100,00")
        page.insert_text((480, 350), "200,00")
        page.insert_text((38, 375), "DEF456")
        page.insert_text((115, 375), "BALLON TEST")
        page.insert_text((345, 375), "1")
        page.insert_text((420, 375), "300,00")
        page.insert_text((480, 375), "300,00")

        data = doc.tobytes()
        doc.close()
        return data

    def test_line_item_columns_rows_and_cells(self):
        data = self.make_pdf()
        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        rows = _visual_rows(lines)
        with fitz.open(stream=data, filetype="pdf") as doc:
            page = doc[0]
            zone = [25, 285, 570, 405]
            region = {
                "zone_id": "p1_line_items_01",
                "zone_type": "LINE_ITEMS",
                "search_bbox": _normalize_box(
                    zone, page.rect.width, page.rect.height
                ),
                "content_bbox": _normalize_box(
                    zone, page.rect.width, page.rect.height
                ),
                "content_regions": [
                    _normalize_box(zone, page.rect.width, page.rect.height)
                ],
                "structural_bbox": _normalize_box(
                    zone, page.rect.width, page.rect.height
                ),
            }
            cells, row_records, columns = _line_item_cells(
                page, rows, region
            )

        self.assertEqual(
            [column["column_type"] for column in columns],
            [
                "product_code",
                "description",
                "quantity",
                "unit_price",
                "line_total",
            ],
        )
        self.assertEqual(
            sum(row["row_type"] == "data" for row in row_records), 2
        )
        values = {
            (cell["row_index"], cell["cell_type"]): cell["text"]
            for cell in cells
            if cell["row_index"] >= 0
        }
        self.assertEqual(values[(0, "quantity")], "2")
        self.assertEqual(values[(1, "line_total")], "300,00")
        self.assertTrue(
            all(
                column["boundary_source"] == "VECTOR_HEADER_CELL"
                for column in columns
            )
        )

    def test_address_and_metadata_cells_are_attached_to_regions(self):
        data = self.make_pdf()
        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        with fitz.open(stream=data, filetype="pdf") as doc:
            page = doc[0]
            address_zone = [30, 75, 250, 150]
            order_zone = [300, 70, 520, 140]
            page_regions = [
                {
                    "zone_id": "p1_ship_to_01",
                    "zone_type": "SHIP_TO",
                    "search_bbox": _normalize_box(
                        address_zone, page.rect.width, page.rect.height
                    ),
                    "content_bbox": _normalize_box(
                        address_zone, page.rect.width, page.rect.height
                    ),
                    "content_regions": [
                        _normalize_box(
                            address_zone,
                            page.rect.width,
                            page.rect.height,
                        )
                    ],
                    "structural_bbox": _normalize_box(
                        address_zone, page.rect.width, page.rect.height
                    ),
                },
                {
                    "zone_id": "p1_order_metadata_01",
                    "zone_type": "ORDER_METADATA",
                    "search_bbox": _normalize_box(
                        order_zone, page.rect.width, page.rect.height
                    ),
                    "content_bbox": _normalize_box(
                        order_zone, page.rect.width, page.rect.height
                    ),
                    "content_regions": [
                        _normalize_box(
                            order_zone, page.rect.width, page.rect.height
                        )
                    ],
                    "structural_bbox": _normalize_box(
                        order_zone, page.rect.width, page.rect.height
                    ),
                    "linked_fields": ["order_number"],
                },
            ]

        geometry = {
            "address_candidates": [
                {
                    "zone_id": "p1_ship_to_01",
                    "components": {
                        "house_number": "1",
                        "street_type": "CHEMIN",
                        "street_name": "DES PLANS D'EAU",
                        "industrial_zone": "ZA DE ROGERVILLE",
                        "postal_code": "76430",
                        "city": "OUDALLE",
                    },
                }
            ],
            "anchored_fields": {
                "order_number": {
                    "value": "ABC-12345",
                    "bbox": [600, 90, 760, 110],
                    "confidence": 0.99,
                }
            },
        }

        enriched = enrich_page_regions_with_cells(
            data, lines, geometry, page_regions
        )
        address = enriched[0]
        types = {cell["cell_type"] for cell in address["cells"]}
        self.assertIn("postal_code", types)
        self.assertIn("city", types)
        self.assertIn("street_name", types)
        self.assertGreaterEqual(address["zone_quality"]["cell_count"], 5)

        metadata = enriched[1]
        self.assertEqual(
            metadata["cells"][0]["cell_type"], "order_number"
        )
        self.assertEqual(
            geometry["anchored_fields"]["order_number"]["cell_id"],
            metadata["cells"][0]["cell_id"],
        )

    def test_spatial_iou_metrics(self):
        self.assertAlmostEqual(
            box_iou([0, 0, 10, 10], [0, 0, 10, 10]), 1.0
        )
        result = evaluate_spatial_items(
            [{"cell_type": "city", "bbox": [0, 0, 10, 10]}],
            [{"cell_type": "city", "bbox": [1, 1, 9, 9]}],
        )
        self.assertGreater(result["mean_iou"], 0.60)
        self.assertEqual(result["matched_count"], 1)


if __name__ == "__main__":
    unittest.main()

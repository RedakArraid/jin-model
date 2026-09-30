import unittest

import fitz

from jin_runtime.weak_field_router import _extract_first_page_lines
from jin_runtime.zone_intelligence import build_page_regions


class ZoneIntelligenceTests(unittest.TestCase):
    @staticmethod
    def make_pdf() -> bytes:
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)

        # Order metadata.
        page.insert_text((40, 70), "N de commande")
        page.insert_text((145, 70), "CAC2410HAR00035")
        page.insert_text((40, 90), "Date")
        page.insert_text((145, 90), "18/10/2024")

        # Delivery frame: deliberately much larger than useful text.
        page.draw_rect(fitz.Rect(280, 150, 565, 275), width=0.8)
        page.insert_text((292, 165), "Adresse de livraison")
        page.insert_text((305, 195), "1 CHEMIN DES PLANS D'EAU")
        page.insert_text((305, 218), "ZA DE ROGERVILLE")
        page.insert_text((305, 242), "76430 OUDALLE")

        # Line item table frame.
        page.draw_rect(fitz.Rect(35, 330, 560, 570), width=0.8)
        page.insert_text((45, 350), "Article")
        page.insert_text((150, 350), "Description")
        page.insert_text((390, 350), "Qte")
        page.insert_text((450, 350), "Prix")
        page.insert_text((45, 380), "ABC123")
        page.insert_text((150, 380), "CHAUDIERE TEST")
        page.insert_text((390, 380), "2")
        page.insert_text((450, 380), "100,00")

        # Totals frame.
        page.draw_rect(fitz.Rect(360, 620, 560, 700), width=0.8)
        page.insert_text((380, 645), "Total HT")
        page.insert_text((500, 645), "200,00")

        data = doc.tobytes()
        doc.close()
        return data

    def test_content_first_zone_snaps_to_vector_border(self):
        data = self.make_pdf()
        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        geometry = {
            "address_candidates": [
                {
                    "role": "ship_to",
                    "confidence": 0.995,
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
                    "value": "CAC2410HAR00035",
                    "confidence": 0.995,
                    "bbox": [244, 75, 420, 90],
                },
                "order_date": {
                    "value": "18/10/2024",
                    "confidence": 0.995,
                    "bbox": [244, 98, 340, 112],
                },
                "total_net": {
                    "value": "200,00",
                    "confidence": 0.995,
                    "bbox": [840, 756, 930, 772],
                },
            },
        }

        regions = build_page_regions(data, lines, geometry)
        by_type = {}
        for region in regions:
            by_type.setdefault(region["zone_type"], []).append(region)

        self.assertIn("SHIP_TO", by_type)
        ship = by_type["SHIP_TO"][0]
        self.assertEqual(ship["boundary_source"], "VECTOR_BORDER")
        self.assertGreaterEqual(len(ship["content_regions"]), 3)

        structural_area = (
            ship["structural_bbox"][2] - ship["structural_bbox"][0]
        ) * (
            ship["structural_bbox"][3] - ship["structural_bbox"][1]
        )
        search_area = (
            ship["search_bbox"][2] - ship["search_bbox"][0]
        ) * (
            ship["search_bbox"][3] - ship["search_bbox"][1]
        )
        self.assertLess(search_area, structural_area * 0.70)
        self.assertEqual(
            geometry["address_candidates"][0]["zone_id"],
            ship["zone_id"],
        )

        self.assertIn("ORDER_METADATA", by_type)
        self.assertIn("TOTALS", by_type)
        self.assertIn("LINE_ITEMS", by_type)

    def test_regions_use_distinct_structural_and_content_boxes(self):
        data = self.make_pdf()
        lines, _ = _extract_first_page_lines(data, "fra+eng+deu")
        geometry = {
            "address_candidates": [
                {
                    "role": "ship_to",
                    "confidence": 0.99,
                    "components": {
                        "street_type": "CHEMIN",
                        "street_name": "DES PLANS D'EAU",
                        "postal_code": "76430",
                        "city": "OUDALLE",
                    },
                }
            ],
            "anchored_fields": {},
        }
        regions = build_page_regions(data, lines, geometry)
        ship = next(item for item in regions if item["zone_type"] == "SHIP_TO")
        self.assertNotEqual(ship["structural_bbox"], ship["content_bbox"])
        self.assertNotEqual(ship["content_bbox"], ship["search_bbox"])
        self.assertEqual(ship["page"], 1)
        self.assertTrue(ship["requires_review"])


if __name__ == "__main__":
    unittest.main()

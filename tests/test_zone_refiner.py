import unittest

from jin_runtime.zone_refiner import refine_zone_evidence


def token(text, x0, y0, x1, y1, page_width=600, page_height=800):
    return {
        "text": text,
        "n": text.upper().replace(" ", ""),
        "bbox": [x0, y0, x1, y1],
        "page_width": page_width,
        "page_height": page_height,
        "block": 0,
        "line": 0,
        "word": 0,
    }


class ZoneRefinerTests(unittest.TestCase):
    def test_exact_component_evidence_suppresses_wrong_semantic_occurrence(self):
        lines = [
            [
                token("Adresse", 300, 100, 340, 110),
                token("de", 342, 100, 352, 110),
                token("livraison", 354, 100, 410, 110),
            ],
            [
                token("3", 310, 130, 316, 140),
                token("RUE", 320, 130, 345, 140),
                token("DU", 349, 130, 365, 140),
                token("PALATINAT", 369, 130, 430, 140),
            ],
            [
                token("78300", 310, 150, 345, 160),
                token("POISSY", 350, 150, 395, 160),
            ],
            [
                token("469,30", 500, 500, 550, 510),
                token("EUR", 555, 500, 580, 510),
            ],
        ]
        geometry = {
            "address_candidates": [
                {
                    "role": "ship_to",
                    "components": {
                        "house_number": "3",
                        "street_type": "RUE",
                        "street_name": "DU PALATINAT",
                        "postal_code": "78300",
                        "city": "POISSY",
                    },
                    "formatted_address_suggestion": "3 RUE DU PALATINAT, 78300 POISSY",
                    "source_line": 1,
                    "requires_review": True,
                }
            ],
            "anchored_fields": {},
        }
        raw = [
            {"label": "ADDRESS_POSTAL_CODE", "value": "78300", "bbox": [517, 188, 575, 200]},
            {"label": "ADDRESS_CITY", "value": "POISSY", "bbox": [583, 188, 658, 200]},
            {"label": "ADDRESS_POSTAL_CODE", "value": "469,30", "bbox": [833, 625, 917, 638]},
            {"label": "ADDRESS_CITY", "value": "EUR", "bbox": [925, 625, 967, 638]},
        ]

        result = refine_zone_evidence(lines, geometry, raw)
        self.assertEqual([item["value"] for item in result["spans"]], ["78300", "POISSY"])
        self.assertEqual(
            {item["value"] for item in result["suppressed_spans"]},
            {"469,30", "EUR"},
        )

        address = result["address_candidates"][0]
        self.assertEqual(address["component_evidence"]["postal_code"]["value"], "78300")
        self.assertEqual(address["component_evidence"]["city"]["value"], "POISSY")
        self.assertIsNotNone(address["region_bbox"])
        self.assertIsNotNone(address["search_zone_bbox"])

    def test_coordinate_spaces_are_explicit_and_convert_y_axis(self):
        lines = [[token("78300", 100, 150, 140, 160), token("POISSY", 145, 150, 190, 160)]]
        geometry = {
            "address_candidates": [
                {
                    "role": "unknown",
                    "components": {"postal_code": "78300", "city": "POISSY"},
                    "source_line": 0,
                }
            ],
            "anchored_fields": {},
        }
        result = refine_zone_evidence(lines, geometry, [])
        evidence = result["address_candidates"][0]["component_evidence"]["postal_code"]
        self.assertEqual(evidence["bbox_pdf_tl"], [100, 150, 140, 160])
        self.assertEqual(evidence["bbox_pdf_bl"], [100, 640, 140, 650])
        self.assertEqual(
            result["coordinate_space"]["bbox"],
            "normalized_0_1000_top_left",
        )

    def test_offer_zone_blocks_order_date_weak_span(self):
        lines = [[token("05/01/2026", 100, 100, 160, 110)]]
        geometry = {
            "address_candidates": [],
            "anchored_fields": {
                "offer_date": {
                    "value": "05/01/2026",
                    "bbox": [167, 125, 267, 138],
                    "requires_review": True,
                }
            },
        }
        raw = [
            {
                "label": "ORDER_DATE",
                "value": "05/01/2026",
                "bbox": [167, 125, 267, 138],
            }
        ]
        result = refine_zone_evidence(lines, geometry, raw)
        self.assertEqual(result["spans"], [])
        self.assertEqual(result["suppressed_spans"][0]["suppression_reason"], "offer_not_order")


if __name__ == "__main__":
    unittest.main()

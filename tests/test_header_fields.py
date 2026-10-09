import unittest

from jin_runtime.header_fields import reconcile_header_fields
from jin_runtime.order_numbers import check_order_number


def block(text, bbox):
    return {"text": text, "source": {"page": 1, "bbox": bbox}}


def payload(number, blocks):
    return {
        "blocks": blocks,
        "business_extractions": {"purchase_order": {"purchase_order": {
            "number": {
                "value": number,
                "evidence": {
                    "page": 1, "bbox": [200, 290, 400, 305],
                    "source_text": number,
                    "extraction_method": "geometry_explicit_order_label_v3",
                },
            },
        }}},
    }


class HeaderFieldReconciliationTests(unittest.TestCase):
    def test_repeated_explicit_suffix_is_preserved_as_part_of_order_number(self):
        doc = payload("264018", [])
        doc["pages"] = [
            {"page": 1, "text": "COMMANDE FOURNISSEUR : 264 018 / EX"},
            {"page": 2, "text": "ELM LEBLANC N° Cde : 264 018 / EX"},
        ]
        reconcile_header_fields(doc)
        header = doc["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertEqual(header["number"]["value"], "264018 / EX")
        self.assertEqual(
            header["number"]["evidence"]["extraction_method"],
            "repeated_explicit_order_suffix_v61",
        )
        self.assertEqual(
            header["number_original"]["disqualified_reason"],
            "EXPLICIT_SUFFIX_PRESERVED",
        )

    def test_single_supplier_order_title_preserves_short_suffix(self):
        doc = payload("256347", [])
        doc["pages"] = [{
            "page": 1,
            "text": "Adresse de livraison COMMANDE FOURNISSEUR : 256 347 / EX",
        }]
        reconcile_header_fields(doc)
        header = doc["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertEqual(header["number"]["value"], "256347 / EX")
        self.assertEqual(
            header["number"]["evidence"]["extraction_method"],
            "explicit_supplier_order_suffix_v62",
        )

    def test_repeated_top_title_outranks_secondary_compact_identifier(self):
        doc = payload("DU05001100", [
            block(
                "BON REXEL DE - CDE COMMANDE n° 061066532 du 14-09-2026 "
                "CDE - 2026 n� 061066532 Envoyé par MAIL",
                [329, 3, 816, 60],
            ),
        ])
        reconcile_header_fields(doc)
        header = doc["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertEqual(header["number"]["value"], "061066532")
        self.assertEqual(
            header["number"]["evidence"]["extraction_method"],
            "repeated_inline_order_label_v62",
        )

    def test_explicit_order_du_date_outranks_delivery_date(self):
        doc = payload("630014201", [])
        doc["business_extractions"]["purchase_order"]["purchase_order"]["order_date"] = {
            "value": "2026-04-03",
            "evidence": {"source_text": "LIVRER LE 03/04/26"},
        }
        doc["pages"] = [{
            "page": 1,
            "text": (
                "COMMANDE N° 630014201 Page 1 sur 1\n"
                "du 20/03/26 Le : 20/03/26\n"
                "LIVRER LE 03/04/26"
            ),
        }]

        reconcile_header_fields(doc)
        header = doc["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertEqual(header["order_date"]["value"], "2026-03-20")
        self.assertEqual(
            header["order_date"]["evidence"]["extraction_method"],
            "explicit_order_du_date_v63",
        )
        self.assertEqual(
            header["order_date_original"]["disqualified_reason"],
            "EXPLICIT_ORDER_DU_DATE_OVERRIDES_OTHER_DATE",
        )
    def test_piece_column_outranks_neighboring_our_reference(self):
        doc = payload("219928", [
            block("Date", [40, 228, 62, 239]),
            block("Pièce", [94, 228, 120, 239]),
            block("Fourn.", [146, 228, 177, 239]),
            block("Notre référence", [250, 228, 324, 239]),
            block("27/03/2026", [23, 246, 68, 256]),
            block("219298", [92, 246, 122, 256]),
            block("F503", [152, 246, 170, 255]),
            block("219928", [274, 244, 301, 253]),
        ])
        reconcile_header_fields(doc)
        header = doc["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertEqual(header["number"]["value"], "219298")
        self.assertEqual(header["number_original"]["value"], "219928")
        self.assertEqual(check_order_number(doc["business_extractions"]["purchase_order"], {})["status"], "SOURCE_SUPPORTED")

    def test_piece_client_commercial_header_outranks_line_item_reference(self):
        doc = payload("1 1 2EL8738729561", [
            block("Date", [20, 228, 45, 239]),
            block("Pièce", [100, 228, 130, 239]),
            block("Client", [170, 228, 205, 239]),
            block("Référence", [250, 228, 305, 239]),
            block("Commercial", [310, 228, 370, 239]),
            block("08/04/2026", [20, 246, 75, 256]),
            block("1784809", [98, 246, 140, 256]),
            block("FELM2140", [170, 246, 220, 256]),
            block("VIGNETTE Clara", [250, 246, 330, 256]),
        ])
        reconcile_header_fields(doc)
        header = doc["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertEqual(header["number"]["value"], "1784809")
        self.assertEqual(
            header["number"]["evidence"]["extraction_method"],
            "explicit_date_piece_order_column_v60",
        )
        self.assertEqual(
            header["number_original"]["disqualified_reason"],
            "LESS_RELEVANT_OR_CLIPPED_HEADER_REFERENCE",
        )

    def test_reference_commande_drops_date_and_engagement_description(self):
        doc = payload("061 / ENGAGEMENT 3", [
            block("N° / Référence Commande", [114, 223, 240, 235]),
            block("17/03/2026 248 061 / ENGAGEMENT 3", [28, 244, 239, 256]),
        ])
        reconcile_header_fields(doc)
        header = doc["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertEqual(header["number"]["value"], "248061")
        self.assertEqual(
            header["number"]["evidence"]["extraction_method"],
            "explicit_reference_commande_column_v60",
        )
        suggestions = {"anchored_field_candidates": {"order_number": [{
            "value": "061 / ENGAGEMENT 3", "page": 1,
            "source": "geometry_explicit_order_label_v3",
        }]}}
        self.assertEqual(
            check_order_number(
                doc["business_extractions"]["purchase_order"], suggestions
            )["status"],
            "SOURCE_SUPPORTED",
        )

    def test_piece_without_full_header_context_does_not_override(self):
        doc = payload("123456", [
            block("Pièce", [94, 228, 120, 239]),
            block("654321", [92, 246, 122, 256]),
        ])
        reconcile_header_fields(doc)
        header = doc["business_extractions"]["purchase_order"]["purchase_order"]
        self.assertEqual(header["number"]["value"], "123456")
        self.assertNotIn("number_original", header)


if __name__ == "__main__":
    unittest.main()

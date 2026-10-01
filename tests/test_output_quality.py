import unittest

from jin_runtime.output_quality import audit_and_repair_output, build_formatted_address


class OutputQualityTests(unittest.TestCase):
    def test_real_core_schema_audits_lines_beside_header(self):
        payload = {"business_extractions": {"purchase_order": {
            "purchase_order": {"number": {"value": "PO-123"}},
            "lines": [{"quantity": 2, "unit_price": 10, "line_total": 99, "discount_amount": None}],
            "totals": {"total_net": 20, "total_vat": 4, "grand_total": 99},
        }}}
        out = audit_and_repair_output(payload)
        codes = {issue["code"] for issue in out["output_quality"]["issues"]}
        self.assertIn("LINE_AMOUNT_MISMATCH", codes)
        self.assertIn("TOTAL_ARITHMETIC_MISMATCH", codes)

    def test_price_per_hundred_and_net_price_are_not_false_errors(self):
        out = audit_and_repair_output({"lines": [
            {"quantity": 5, "unit_price": 200, "price_unit": 100, "line_total": 10},
            {"quantity": 2, "unit_price": 100, "net_unit_price": 80,
             "discount_percent": 20, "line_total": 160},
        ]})
        self.assertFalse(out["output_quality"]["issues"])

    def test_address_format_reflects_learned_component(self):
        out = audit_and_repair_output({"business_addresses": [{
            "formatted_address": "10 RUE EXEMPLE, 75001",
            "component_source": {"city": "statistical_history"},
            "address": {"house_number": "10", "street_type": "RUE",
                        "street_name": "EXEMPLE", "postal_code": "75001", "city": "PARIS"},
        }]})
        self.assertIn("PARIS", out["business_addresses"][0]["formatted_address"])
        self.assertNotIn("ADDRESS_CITY_MISSING", {x["code"] for x in out["output_quality"]["issues"]})

    def test_rounding_tolerance_does_not_hide_percentage_errors(self):
        out = audit_and_repair_output({"lines": [
            {"quantity": 10, "net_unit_price": 15.08, "line_total": 150.75},
            {"quantity": 1, "unit_price": 1000, "line_total": 1005},
        ], "totals": {"total_net": 1000, "total_vat": 0, "grand_total": 1001}})
        issues = out["output_quality"]["issues"]
        self.assertEqual([i["path"] for i in issues if i["code"] == "LINE_AMOUNT_MISMATCH"],
                         ["purchase_order.lines[1]"])
        self.assertIn("TOTAL_ARITHMETIC_MISMATCH", {i["code"] for i in issues})

    def test_formatted_address_removes_duplicate_city(self):
        payload = {
            "business_addresses": [
                {
                    "role": "ship_to",
                    "formatted_address": "30 RUE DES GRANDS MORTIERS, 37705 ST PIERRE DES CORPS ST PIERRE DES CORPS",
                    "address": {
                        "house_number": "30",
                        "street_type": "RUE",
                        "street_name": "DES GRANDS MORTIERS",
                        "postal_code": "37705",
                        "city": "ST PIERRE DES CORPS",
                        "country": "France",
                    },
                }
            ]
        }
        out = audit_and_repair_output(payload)
        addr = out["business_addresses"][0]
        self.assertEqual(
            addr["formatted_address"],
            "30 RUE DES GRANDS MORTIERS, 37705 ST PIERRE DES CORPS, France",
        )
        self.assertEqual(
            addr["formatted_address_original"],
            "30 RUE DES GRANDS MORTIERS, 37705 ST PIERRE DES CORPS ST PIERRE DES CORPS",
        )
        self.assertEqual(out["output_quality"]["repair_count"], 1)
        self.assertTrue(
            any(
                i["code"] == "ADDRESS_DUPLICATE_CITY"
                for i in out["output_quality"]["issues"]
            )
        )

    def test_formatter_keeps_bp_cs_cedex_once(self):
        block = {
            "address": {
                "building": "BATIMENT Q",
                "house_number": "124-126",
                "street_type": "RUE",
                "street_name": "DE STALINGRAD",
                "postal_box": "77605",
                "cs": "10412",
                "postal_code": "93711",
                "city": "DRANCY",
                "cedex": True,
                "country": "France",
            }
        }
        self.assertEqual(
            build_formatted_address(block),
            "BATIMENT Q, 124-126 RUE DE STALINGRAD, BP 77605, CS 10412, 93711 DRANCY CEDEX, France",
        )

    def test_formatter_preserves_core_address_component_names(self):
        formatted = build_formatted_address({"address": {
            "entrance": "ENTREE B", "business_park": "PARC TEST",
            "lieu_dit": "LES BOIS", "po_box": "1173",
            "postal_code": "06003", "city": "NICE", "cedex": True, "cedex_number": "1",
        }})
        self.assertEqual(formatted, "ENTREE B, PARC TEST, LES BOIS, BP 1173, 06003 NICE CEDEX 1")

    def test_verification_contradiction_is_error(self):
        payload = {
            "business_addresses": [
                {
                    "formatted_address": "1 RUE TEST, 75001 PARIS",
                    "is_verified_real_address": True,
                    "verification_status": "UNAVAILABLE",
                    "address": {
                        "house_number": "1",
                        "street_type": "RUE",
                        "street_name": "TEST",
                        "postal_code": "75001",
                        "city": "PARIS",
                    },
                }
            ]
        }
        out = audit_and_repair_output(payload)
        self.assertEqual(out["output_quality"]["status"], "ERROR")
        self.assertTrue(
            any(
                i["code"] == "VERIFICATION_STATUS_CONTRADICTION"
                for i in out["output_quality"]["issues"]
            )
        )

    def test_line_and_total_mismatches_are_reported(self):
        payload = {
            "business_extractions": {
                "purchase_order": {
                    "lines": [
                        {
                            "line_number": 1,
                            "quantity": 2,
                            "unit_price": 10,
                            "line_total": 25,
                        }
                    ],
                    "totals": {
                        "total_before_tax": 25,
                        "total_vat": 5,
                        "grand_total": 40,
                    },
                }
            }
        }
        out = audit_and_repair_output(payload)
        codes = {i["code"] for i in out["output_quality"]["issues"]}
        self.assertIn("LINE_AMOUNT_MISMATCH", codes)
        self.assertIn("TOTAL_ARITHMETIC_MISMATCH", codes)


if __name__ == "__main__":
    unittest.main()

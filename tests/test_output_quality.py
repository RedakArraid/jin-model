import unittest

from jin_runtime.output_quality import audit_and_repair_output, build_formatted_address


class OutputQualityTests(unittest.TestCase):
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

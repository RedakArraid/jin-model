import json
from pathlib import Path
import socket
import tempfile
import unittest

from scripts.benchmark_cpu import (
    compare_expected, network_disabled, run_benchmark, select_documents,
    summarize_document,
)


def sample_payload():
    return {
        "document": {"primary_document_type": "purchase_order", "page_count": 2},
        "business_extractions": {"purchase_order": {
            "purchase_order": {"number": {"value": "TEST-123"}, "order_date": {"value": "2026-09-30"}},
            "buyer": {"name": "PRIVATE_COMPANY", "contact": {"email": "PRIVATE_CONTACT"}},
            "supplier": {},
            "lines": [
                {"material_number": "PRIVATE_SKU", "quantity": 2, "net_unit_price": 0},
                {"quantity": None},
            ],
            "totals": {"total_net": 20.5},
            "business_addresses": [{
                "role": "ship_to", "formatted_address": "PRIVATE_ADDRESS",
                "address": {"city": "PRIVATE_CITY", "postal_code": "75000"},
            }],
        }},
        "weak_field_suggestions": {
            "pages_processed": 2, "text_source": "mixed", "page_text_sources": [
                {"page": 1, "text_source": "native_pdf_text"},
                {"page": 2, "text_source": "tesseract_ocr"},
            ],
        },
        "extraction_decision": {"status": "REVIEW_REQUIRED", "requires_review": True,
                                "reasons": [{"code": "QUANTITY_MISSING", "path": "lines[1]"}]},
    }


class BenchmarkCpuTests(unittest.TestCase):
    def test_selection_is_deterministic_deduplicated_and_pdf_only(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for index in range(6):
                (folder / f"{index}.pdf").write_bytes(f"%PDF-{index}".encode())
            (folder / "duplicate.PDF").write_bytes(b"%PDF-0")
            (folder / "ignored.txt").write_text("ignore")
            selected, info = select_documents(folder, limit=3)
            repeated, _ = select_documents(folder, limit=3)
            self.assertEqual(selected, repeated)
            self.assertEqual(len(selected), 3)
            self.assertEqual(len({digest for _, digest in selected}), 3)
            self.assertEqual(info["candidate_files"], 7)
            self.assertEqual(info["unique_sha256"], 6)
            self.assertEqual(info["duplicate_files"], 1)

    def test_explicit_replay_preserves_names_order_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "first.pdf").write_bytes(b"%PDF-first")
            (folder / "second.pdf").write_bytes(b"%PDF-second")
            selected, info = select_documents(folder, file_list=["second.pdf", "first.pdf", "second.pdf"])
            self.assertEqual([path.name for path, _ in selected], ["second.pdf", "first.pdf"])
            self.assertEqual(info["method"], "explicit_file_list")
            with self.assertRaises(ValueError):
                select_documents(folder, file_list=["../outside.pdf"])
            with self.assertRaises(ValueError):
                select_documents(folder, limit=0)

    def test_summary_tracks_missing_fields_but_excludes_private_values(self):
        summary = summarize_document(sample_payload())
        self.assertEqual(summary["line_count"], 2)
        self.assertEqual(summary["lines_missing_reference"], 1)
        self.assertEqual(summary["lines_missing_quantity"], 1)
        self.assertEqual(summary["lines_missing_unit_price"], 1)
        self.assertEqual(summary["address_roles"], {"ship_to": 1})
        self.assertTrue(summary["party_contact_presence"]["buyer"])
        self.assertTrue(summary["ocr_required"])
        self.assertEqual(summary["review_reasons"], ["QUANTITY_MISSING"])
        self.assertNotIn("PRIVATE_", json.dumps(summary))

    def test_regression_compares_only_supplied_fields_and_handles_decimal_notation(self):
        checks = compare_expected(sample_payload(), {"line_count": 2, "total_net": "20,50"})
        self.assertEqual({check["field"] for check in checks}, {"line_count", "total_net"})
        self.assertTrue(all(check["match"] for check in checks))
        self.assertFalse(compare_expected(sample_payload(), {"order_number": "WRONG"})[0]["match"])
        with self.assertRaises(ValueError):
            compare_expected(sample_payload(), {"private_address": "not-supported"})

    def test_address_checks_match_exact_role_not_location_under_another_role(self):
        payload = sample_payload()
        address = payload["business_extractions"]["purchase_order"]["business_addresses"][0]
        address["shared_with_roles"] = ["bill_to"]
        checks = compare_expected(payload, {"address_components": {
            "ship_to": {"postal_code": "75000"}, "bill_to": {"postal_code": "75000"},
        }})
        self.assertTrue(checks[0]["match"])
        self.assertFalse(checks[1]["match"])
        self.assertEqual(checks[1]["reason"], "ROLE_MISSING")
        self.assertEqual(checks[1]["field"], "address_components.bill_to.postal_code")

    def test_order_number_comparison_only_normalizes_presentation(self):
        payload = sample_payload()
        payload["business_extractions"]["purchase_order"]["purchase_order"]["number"] = {"value": "CF001-012"}
        for wanted, matches in (("cf 001-012", True), ("CF1-012", False),
                                ("CF001/012", False), ("CF0O1-012", False)):
            with self.subTest(wanted=wanted):
                check = compare_expected(payload, {"order_number": wanted})[0]
                self.assertEqual(check["match"], matches)
                self.assertIn("preserve_zeroes", check["comparison"])

    def test_address_text_ignores_case_whitespace_but_keeps_postal_zero(self):
        payload = sample_payload()
        address = payload["business_extractions"]["purchase_order"]["business_addresses"][0]["address"]
        address.update(postal_code="01704", city="  Saint   TEST ", cedex=True)
        checks = compare_expected(payload, {"address_components": {"ship_to": {
            "postal_code": "01704", "city": "saint test", "cedex": True,
        }}})
        self.assertTrue(all(check["match"] for check in checks))
        truncated = compare_expected(payload, {"address_components": {"ship_to": {"postal_code": "1704"}}})
        self.assertFalse(truncated[0]["match"])

    def test_conflicting_addresses_do_not_mix_components_between_candidates(self):
        payload = sample_payload()
        po = payload["business_extractions"]["purchase_order"]
        po["business_addresses"] = [
            {"role": "ship_to", "address": {"postal_code": "75000", "city": "PARIS"}},
            {"role": "ship_to", "address": {"postal_code": "69000", "city": "LYON"}},
        ]
        checks = compare_expected(payload, {"address_components": {"ship_to": {
            "postal_code": "75000", "city": "LYON",
        }}})
        self.assertTrue(all(check["ambiguous"] for check in checks))
        self.assertTrue(all(not check["match"] for check in checks))
        self.assertTrue(all(check["actual"] is None for check in checks))
        self.assertEqual(checks[0]["candidate_values"], ["75000", "69000"])

    def test_contact_checks_use_contact_identity_and_same_role_only(self):
        payload = sample_payload()
        po = payload["business_extractions"]["purchase_order"]
        po["buyer"] = {"name": "COMPANY_NOT_CONTACT", "contact": {"name": "Alice", "phone": "01 23 45 67 89"}}
        po["supplier"] = {"contact": {"email": "supplier@example.test"}}
        po["business_addresses"][0]["contact_email"] = "delivery@example.test"
        checks = compare_expected(payload, {"contacts": {
            "buyer": {"name": "ALICE", "phone": "01.23.45.67.89", "email": "supplier@example.test"},
            "ship_to": {"email": "delivery@example.test"},
        }})
        self.assertEqual([check["match"] for check in checks], [True, True, False, True])
        po["buyer"]["contact"] = {}
        missing_name = compare_expected(payload, {"contacts": {"buyer": {"name": "COMPANY_NOT_CONTACT"}}})
        self.assertFalse(missing_name[0]["match"])

    def test_conflicting_contact_sources_are_not_silently_selected(self):
        payload = sample_payload()
        po = payload["business_extractions"]["purchase_order"]
        po["buyer"] = {"contact": {"email": "one@example.test"}, "email": "two@example.test"}
        checks = compare_expected(payload, {"contacts": {"buyer": {"email": "one@example.test"}}})
        self.assertFalse(checks[0]["match"])
        self.assertEqual(checks[0]["reason"], "CONFLICTING_VALUES")

    def test_explicit_components_report_only_the_requested_private_values(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "sample.pdf").write_bytes(b"%PDF-test")
            report = run_benchmark(folder, lambda path: sample_payload(), expected={
                "sample.pdf": {"address_components": {"ship_to": {"city": "PRIVATE_CITY"}}},
            })
        serialized = json.dumps(report)
        self.assertIn("PRIVATE_CITY", serialized)
        for secret in ("PRIVATE_ADDRESS", "PRIVATE_COMPANY", "PRIVATE_CONTACT", "PRIVATE_SKU"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(report["summary"]["regression"]["matched_fields"], 1)

    def test_nested_expectations_reject_unbounded_raw_text_or_invalid_shapes(self):
        for expected in (
            {"address_components": {"ship_to": {"raw": "source text"}}},
            {"address_components": {"ship_to": ["75000"]}},
            {"contacts": {"buyer": {"email": ["one@example.test"]}}},
            {"contacts": {"buyer": {"company": "not a contact"}}},
        ):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                compare_expected(sample_payload(), expected)

    def test_report_continues_after_errors_without_recording_document_text(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "good.pdf").write_bytes(b"%PDF-good")
            (folder / "bad.pdf").write_bytes(b"%PDF-bad")

            def extract(path):
                if path.name == "bad.pdf":
                    raise ValueError("PRIVATE_DOCUMENT_TEXT")
                return sample_payload()

            report = run_benchmark(folder, extract, file_list=["good.pdf", "bad.pdf"],
                                   expected={"good.pdf": {"line_count": 2}})
        self.assertEqual(report["summary"]["processed"], 2)
        self.assertEqual(report["summary"]["errors"], 1)
        self.assertEqual(report["summary"]["regression"]["matched_fields"], 1)
        self.assertEqual(report["documents"][1]["error"], {"type": "ValueError"})
        self.assertNotIn("PRIVATE_", json.dumps(report))
        self.assertIn("not factual extraction accuracy", report["qualification"])

    def test_network_guard_denies_connections_and_restores_socket(self):
        original = socket.socket.connect
        with network_disabled():
            with socket.socket() as connection:
                with self.assertRaisesRegex(RuntimeError, "NETWORK_DISABLED"):
                    connection.connect(("127.0.0.1", 1))
        self.assertIs(socket.socket.connect, original)


if __name__ == "__main__":
    unittest.main()

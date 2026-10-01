import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from jin_runtime.ban_reference import (
    LocalBanReference, canonical_address_key, department_candidates,
    index_contains, normalize_part, write_sorted_index,
)
from scripts.build_ban_index import build_from_csv, parse_departments


class BanReferenceTests(unittest.TestCase):
    def test_normalization_handles_accents_abbreviations_and_leading_number_zeroes(self):
        left = canonical_address_key(number="002", repetition="", street="av. Général Pruneau",
                                     postal_code="83000", city="Toulon")
        right = canonical_address_key(number="2", repetition=None, street="AVENUE GENERAL PRUNEAU",
                                      postal_code="83000", city="TOULON")
        self.assertEqual(left, right)
        self.assertEqual(normalize_part("rue de l'Église"), "RUE DE L EGLISE")

    def test_department_routing_keeps_corsica_and_overseas(self):
        self.assertEqual(department_candidates("06003"), ["06"])
        self.assertEqual(department_candidates("20100"), ["2A", "2B"])
        self.assertEqual(department_candidates("97400"), ["974"])

    def test_sorted_index_exact_lookup_and_non_match(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adresses-06.idx"
            wanted = canonical_address_key(number="2", repetition="", street="RUE DIDEROT",
                                           postal_code="06003", city="NICE")
            other = canonical_address_key(number="3", repetition="", street="RUE DIDEROT",
                                          postal_code="06003", city="NICE")
            info = write_sorted_index([wanted, wanted], path)
            self.assertEqual(info["records"], 1)
            self.assertTrue(index_contains(path, wanted))
            self.assertFalse(index_contains(path, other))

    def test_reference_enriches_exact_match_without_changing_role(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = canonical_address_key(number="2", repetition="", street="RUE DIDEROT",
                                        postal_code="06003", city="NICE")
            info = write_sorted_index([key], root / "adresses-06.idx")
            (root / "manifest.json").write_text(json.dumps({
                "dataset_date": "2026-09-30", "license": "Licence Ouverte 2.0",
                "departments": {"06": info},
            }), encoding="utf-8")
            payload = {"business_addresses": [{
                "role": "bill_to", "address": {"house_number": "2", "street_type": "RUE",
                "street_name": "DIDEROT", "postal_code": "06003", "city": "NICE"},
            }]}
            out = LocalBanReference(root).enrich(payload)
            address = out["business_addresses"][0]
            self.assertEqual(address["role"], "bill_to")
            self.assertEqual(address["ban_verification"]["status"], "EXACT_MATCH")
            self.assertTrue(address["is_verified_real_address"])

    def test_not_found_is_explicitly_non_conclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_sorted_index([], root / "adresses-06.idx")
            (root / "manifest.json").write_text(json.dumps({"departments": {"06": {}}}), encoding="utf-8")
            result = LocalBanReference(root).verify({"address": {
                "house_number": "99", "street": "RUE INCONNUE",
                "postal_code": "06003", "city": "NICE",
            }})
            self.assertEqual(result["status"], "NOT_FOUND")
            self.assertFalse(result["conclusive"])

    def test_csv_builder_validates_schema_and_builds_binary_index(self):
        text = "numero;rep;nom_voie;code_postal;nom_commune\n2;;Rue Diderot;06003;Nice\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "06.idx"
            streets = Path(directory) / "streets.sqlite"
            info = build_from_csv(io.StringIO(text), path, streets)
            self.assertEqual(info["source_rows"], 1)
            self.assertEqual(info["records"], 1)
            self.assertEqual(info["street_records"], 1)
        with self.assertRaises(ValueError):
            build_from_csv(io.StringIO("wrong;columns\n1;2\n"), Path("unused.idx"))

    def test_official_empty_territory_file_builds_a_valid_empty_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = build_from_csv(io.StringIO(""), root / "adresses-984.idx",
                                    root / "streets-984.sqlite")
            self.assertEqual(result["records"], 0)
            self.assertEqual(result["street_records"], 0)
            self.assertTrue((root / "adresses-984.idx").is_file())
            self.assertTrue((root / "streets-984.sqlite").is_file())

    def test_street_match_suggests_without_claiming_exact_address(self):
        text = "numero;rep;nom_voie;code_postal;nom_commune\n2;;Rue Diderot;06003;Nice\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            info = build_from_csv(io.StringIO(text), root / "adresses-06.idx",
                                  root / "streets-06.sqlite")
            (root / "manifest.json").write_text(json.dumps({"departments": {"06": info}}), encoding="utf-8")
            reference = LocalBanReference(root)
            result = reference.verify({"address": {
                "house_number": "999", "street_type": "RUE", "street_name": "DIDEROT",
                "postal_code": "06003", "city": "NICE",
            }})
            self.assertEqual(result["status"], "STREET_MATCH_NUMBER_NOT_FOUND")
            self.assertFalse(result["verified"])

    def test_street_match_accepts_a_city_subzone_without_silently_rewriting_it(self):
        text = "numero;rep;nom_voie;code_postal;nom_commune\n2;;Rue Diderot;34070;Montpellier\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            info = build_from_csv(io.StringIO(text), root / "adresses-34.idx",
                                  root / "streets-34.sqlite")
            (root / "manifest.json").write_text(json.dumps({"departments": {"34": info}}), encoding="utf-8")
            result = LocalBanReference(root).verify({"address": {
                "house_number": "999", "street": "Rue Diderot",
                "postal_code": "34070", "city": "Montpellier Garosud",
            }})
            self.assertEqual(result["status"], "STREET_MATCH_NUMBER_NOT_FOUND")
            self.assertEqual(result["suggested_components"]["city"], "Montpellier")

    def test_unique_close_street_with_existing_number_is_a_canonical_match(self):
        text = ("numero;rep;nom_voie;code_postal;nom_commune\n"
                "15;;Avenue Général Pruneau;83000;Toulon\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            info = build_from_csv(io.StringIO(text), root / "adresses-83.idx",
                                  root / "streets-83.sqlite")
            (root / "manifest.json").write_text(json.dumps({"departments": {"83": info}}), encoding="utf-8")
            result = LocalBanReference(root).verify({"address": {
                "house_number": "15", "street": "Avenue du General Pruneau",
                "postal_code": "83000", "city": "Toulon",
            }})
            self.assertEqual(result["status"], "CANONICAL_MATCH")
            self.assertTrue(result["verified"])
            self.assertEqual(result["matched_components"]["street"], "Avenue Général Pruneau")

    def test_department_cli_rejects_unknown_codes(self):
        self.assertEqual(parse_departments("06,83,06"), ["06", "83"])
        with self.assertRaises(ValueError):
            parse_departments("../06")


if __name__ == "__main__":
    unittest.main()

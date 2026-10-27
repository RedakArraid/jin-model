import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import fitz

from jin_runtime.studio_contract import FIELD_LABELS as STUDIO_FIELD_LABELS
from scripts.import_annotation_campaign_to_studio import draft_fields
from scripts.select_varied_annotation_sample import Candidate, choose_varied
from training.annotations.bootstrap_from_extractions import (
    ADDRESS_LABEL_SUFFIX,
    FIELD_LABELS as BOOTSTRAP_FIELD_LABELS,
    PARTY_LABELS,
    annotate_one,
    collect_candidates,
    deduplicate_candidates,
)


class AnnotationBootstrapTests(unittest.TestCase):
    def test_varied_selector_covers_families_before_repeating_them(self):
        candidates = [
            Candidate("a/1.pdf", "a", 1, "native_text", "1", "01"),
            Candidate("a/2.pdf", "a", 2, "native_text", "2-3", "02"),
            Candidate("b/1.pdf", "b", 1, "scan_or_low_text", "1", "03"),
            Candidate("c/1.pdf", "c", 4, "native_text", "4+", "04"),
        ]

        selected = choose_varied(candidates, 3)

        self.assertEqual({item.family for item in selected}, {"a", "b", "c"})
        self.assertEqual(len({item.stratum for item in selected}), 3)

    def test_every_bootstrap_label_is_accepted_by_studio(self):
        emitted = set(BOOTSTRAP_FIELD_LABELS.values())
        emitted.update(
            f"{role}_{suffix}"
            for role in PARTY_LABELS.values()
            for suffix in ADDRESS_LABEL_SUFFIX.values()
        )
        emitted.update(
            f"{role}_{suffix}"
            for role in PARTY_LABELS.values()
            for suffix in (
                "NAME", "CODE", "VAT_NUMBER", "SIREN", "SIRET", "CONTACT_PHONE",
                "CONTACT_FAX", "CONTACT_EMAIL",
            )
        )
        emitted.update(
            {
                "LINE_ITEM_DELIVERY_DATE",
                "ADDITIONAL_CHARGE_LABEL",
                "ADDITIONAL_CHARGE_AMOUNT",
            }
        )

        self.assertEqual(emitted - STUDIO_FIELD_LABELS, set())

    def test_wrapped_header_fields_use_printed_value_and_evidence(self):
        payload = {
            "order": {
                "customer_order_number": {
                    "value": "PO-123",
                    "confidence": 0.99,
                    "evidence": {"page": 2, "bbox": [10, 20, 80, 30]},
                },
                "order_date": {
                    "value": "2026-10-26",
                    "raw_value": "26/10/2026",
                    "evidence": {"page": 2},
                },
            }
        }

        candidates = collect_candidates(payload)

        number = next(item for item in candidates if item["label"] == "ORDER_NUMBER")
        date = next(item for item in candidates if item["label"] == "ORDER_DATE")
        self.assertEqual(number["value"], "PO-123")
        self.assertEqual(number["page_hint"], 2)
        self.assertEqual(number["bbox_hint"], [10.0, 20.0, 80.0, 30.0])
        self.assertEqual(date["value"], "26/10/2026")
        self.assertEqual(date["canonical_value"], "2026-10-26")

    def test_line_fields_inherit_row_geometry(self):
        payload = {
            "order": {
                "line_items": [
                    {
                        "line_number": "1",
                        "references": {"material_number": "7738112972"},
                        "description": "POMPE",
                        "quantity": 1,
                        "pricing": {"unit_price": 68.5, "net_unit_price": 68.5},
                        "amounts": {"net": 68.5, "total": 68.5},
                        "evidence": {"page": 1, "bbox": [5, 100, 590, 120]},
                    }
                ]
            }
        }

        candidates = collect_candidates(payload)

        labels = {item["label"] for item in candidates}
        self.assertTrue(
            {
                "LINE_ITEM_NUMBER",
                "LINE_ITEM_REFERENCE",
                "LINE_ITEM_DESCRIPTION",
                "LINE_ITEM_QUANTITY",
                "LINE_ITEM_UNIT_PRICE",
                "LINE_ITEM_TOTAL",
            }.issubset(labels)
        )
        reference = next(item for item in candidates if item["label"] == "LINE_ITEM_REFERENCE")
        self.assertEqual(reference["page_hint"], 1)
        self.assertEqual(reference["bbox_hint"], [5.0, 100.0, 590.0, 120.0])

    def test_semantic_dedup_keeps_equal_values_on_distinct_rows(self):
        payload = {
            "order": {
                "line_items": [
                    {
                        "references": {
                            "material_number": "7738112972",
                            "article_number": "7738112972",
                        },
                        "quantity": 1,
                    },
                    {
                        "references": {
                            "material_number": "7738112973",
                            "article_number": "7738112973",
                        },
                        "quantity": 1,
                    },
                ]
            }
        }

        candidates = deduplicate_candidates(collect_candidates(payload))

        quantities = [item for item in candidates if item["label"] == "LINE_ITEM_QUANTITY"]
        references = [item for item in candidates if item["label"] == "LINE_ITEM_REFERENCE"]
        self.assertEqual(len(quantities), 2)
        self.assertEqual(len(references), 2)
        self.assertEqual(
            {item["path"] for item in quantities},
            {"order.line_items[0].quantity", "order.line_items[1].quantity"},
        )

    def test_line_nested_order_alias_is_not_a_header_annotation(self):
        payload = {
            "order": {
                "customer_order_number": {"value": "PO-123"},
                "line_items": [
                    {
                        "references": {
                            "supplier_material_number": "7738112972",
                            "customer_order_number": "ROW-ORDER",
                        }
                    }
                ],
            }
        }

        candidates = collect_candidates(payload)

        self.assertEqual(
            [item["value"] for item in candidates if item["label"] == "ORDER_NUMBER"],
            ["PO-123"],
        )
        self.assertIn(
            "7738112972",
            [item["value"] for item in candidates if item["label"] == "LINE_ITEM_REFERENCE"],
        )

    def test_primary_line_reference_prefers_numeric_article_prior(self):
        payload = {
            "order": {
                "line_items": [
                    {
                        "references": {
                            "material_number": "ELM/7738112714",
                            "supplier_material_number": "7738112714",
                        }
                    },
                    {
                        "references": {
                            "material_number": "87168294760",
                            "supplier_material_number": "L1029476",
                        }
                    },
                ]
            }
        }

        references = [
            item
            for item in deduplicate_candidates(collect_candidates(payload))
            if item["label"] == "LINE_ITEM_REFERENCE"
        ]

        self.assertEqual(
            [(item["path"], item["value"]) for item in references],
            [
                ("order.line_items[0].references.supplier_material_number", "7738112714"),
                ("order.line_items[1].references.material_number", "87168294760"),
            ],
        )

    def test_geometry_assigns_repeated_quantities_to_their_own_rows(self):
        with TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "two-lines.pdf"
            document = fitz.open()
            page = document.new_page(width=600, height=800)
            page.insert_text((40, 80), "BON DE COMMANDE")
            page.insert_text((40, 180), "1 7738112972 POMPE A 1 EA 10,00 10,00")
            page.insert_text((40, 220), "2 7738112973 POMPE B 1 EA 12,00 12,00")
            document.save(pdf_path)
            document.close()
            payload = {
                "document": {"type": "purchase_order"},
                "order": {
                    "customer_order_number": {
                        "value": "PO-NOT-PRINTED",
                        "evidence": {"page": 1, "bbox": [300, 50, 400, 70]},
                    },
                    "line_items": [
                        {
                            "line_number": "1",
                            "references": {"material_number": "7738112972"},
                            "description": "POMPE A",
                            "quantity": 1.0,
                            "unit": "EA",
                            "pricing": {"unit_price": 10.0},
                            "amounts": {"total": 10.0},
                            "evidence": {"page": 1},
                        },
                        {
                            "line_number": "2",
                            "references": {"material_number": "7738112973"},
                            "description": "POMPE B",
                            "quantity": 1.0,
                            "unit": "EA",
                            "pricing": {"unit_price": 12.0},
                            "amounts": {"total": 12.0},
                            "evidence": {"page": 1},
                        },
                    ]
                },
            }

            annotation, report = annotate_one(pdf_path, payload, max_ambiguity=3)

        regions = [region for page in annotation["pages"] for region in page["regions"]]
        quantity_regions = [
            region for region in regions if region["label"] == "LINE_ITEM_QUANTITY"
        ]
        self.assertEqual(report["unmatched_fields"], 0)
        self.assertEqual(len(quantity_regions), 2)
        self.assertEqual(len({region["source_path"] for region in regions}), len(regions))
        self.assertLess(quantity_regions[0]["bbox"][1], quantity_regions[1]["bbox"][1])
        fallback = next(region for region in regions if region["label"] == "ORDER_NUMBER")
        self.assertEqual(fallback["match_method"], "source_bbox_fallback")
        annotation["document_labels"] = {"document_type": "purchase_order"}
        order_field = next(
            item for item in draft_fields(annotation) if item["label"] == "ORDER_NUMBER"
        )
        self.assertEqual(order_field["status"], "AMBIGUOUS")
        self.assertIn("evidence", order_field)

    def test_party_and_address_labels_keep_business_role(self):
        payload = {
            "order": {
                "parties": {
                    "buyer": {
                        "name": "CLIENT SA",
                        "vat_number": "FR00123456789",
                        "siren": "775708373",
                        "siret": "77570837300011",
                    },
                },
                "addresses": {
                    "ship_to": {
                        "status": "SELECTED",
                        "selected": {
                            "components": {
                                "street_name": "ISAAC NEWTON",
                                "postal_code": "75001",
                                "city": "PARIS",
                            }
                        },
                    }
                },
            }
        }

        labels = {item["label"] for item in collect_candidates(payload)}

        self.assertIn("BUYER_NAME", labels)
        self.assertIn("BUYER_VAT_NUMBER", labels)
        self.assertIn("BUYER_SIREN", labels)
        self.assertIn("BUYER_SIRET", labels)
        self.assertIn("SHIP_TO_STREET_NAME", labels)
        self.assertIn("SHIP_TO_POSTAL_CODE", labels)
        self.assertIn("SHIP_TO_CITY", labels)

    def test_unscoped_and_derived_address_components_are_not_preannotated(self):
        payload = {
            "document": {
                "tax_identifiers": [{"country_code": "FR"}],
            },
            "order": {
                "addresses": {
                    "unknown": {
                        "status": "AMBIGUOUS",
                        "alternatives": [{
                            "components": {"house_number": "12", "city": "PARIS"},
                        "verification": {
                            "matched_components": {
                                "house_number": "12",
                                "postal_code": "75001",
                                "city": "PARIS",
                            }
                        },
                        }],
                    }
                }
            },
            "quality": {"output_audit": {"issues": [{"details": {"city": "PARIS"}}]}},
        }

        candidates = collect_candidates(payload)

        self.assertFalse(
            any(item["label"].startswith("ADDRESS_") for item in candidates)
        )
        self.assertFalse(
            any(item["path"].endswith(("country_code", "city", "postal_code")) for item in candidates)
        )

    def test_only_selected_role_address_is_annotation_ground_truth(self):
        payload = {
            "order": {
                "addresses": {
                    "ship_to": {
                        "status": "SELECTED",
                        "selected": {"components": {"postal_code": "75003"}},
                        "alternatives": [
                            {"components": {"postal_code": "75002"}},
                        ],
                    }
                },
            }
        }

        candidates = deduplicate_candidates(collect_candidates(payload))
        postal_codes = [
            item for item in candidates if item["label"] == "SHIP_TO_POSTAL_CODE"
        ]

        self.assertEqual(len(postal_codes), 1)
        self.assertEqual(postal_codes[0]["value"], "75003")
        self.assertEqual(
            postal_codes[0]["path"],
            "order.addresses.ship_to.selected.components.postal_code",
        )

    def test_atomic_street_components_replace_overlapping_composite_street(self):
        payload = {
            "order": {
                "addresses": {
                    "ship_to": {
                        "status": "SELECTED",
                        "selected": {"components": {
                            "street": "RUE ISAAC NEWTON",
                            "street_type": "RUE",
                            "street_name": "ISAAC NEWTON",
                        }},
                    },
                }
            }
        }

        labels = {
            item["label"]
            for item in deduplicate_candidates(collect_candidates(payload))
        }

        self.assertIn("SHIP_TO_STREET_TYPE", labels)
        self.assertIn("SHIP_TO_STREET_NAME", labels)
        self.assertNotIn("SHIP_TO_STREET", labels)

    def test_inferred_address_components_are_not_human_preannotations(self):
        payload = {
            "order": {
                "addresses": {
                    "ship_to": {
                        "status": "SELECTED",
                        "selected": {
                            "components": {
                                "postal_routing_code": "00013",
                                "postal_code": "14120",
                                "city": "MONDEVILLE",
                                "country": "France",
                                "country_code": "FR",
                            },
                            "warnings": [
                                "country_inferred_from_postal_pattern",
                                "untyped_postal_routing_code_inferred",
                            ],
                        }
                    }
                }
            }
        }

        labels = {item["label"] for item in collect_candidates(payload)}

        self.assertIn("SHIP_TO_POSTAL_CODE", labels)
        self.assertIn("SHIP_TO_CITY", labels)
        self.assertNotIn("SHIP_TO_COUNTRY", labels)
        self.assertNotIn("SHIP_TO_COUNTRY_CODE", labels)
        self.assertNotIn("SHIP_TO_POSTAL_ROUTING_CODE", labels)

    def test_studio_draft_keeps_regions_and_unresolved_fields_reviewable(self):
        annotation = {
            "document_labels": {"document_type": "purchase_order"},
            "pages": [
                {
                    "page": 1,
                    "regions": [
                        {
                            "label": "ORDER_NUMBER",
                            "value": "PO-123",
                            "source_path": "order.customer_order_number",
                            "bbox": [10, 20, 80, 30],
                        }
                    ],
                }
            ],
            "generation_report": {
                "unresolved": [
                    {
                        "label": "DELIVERY_DATE",
                        "value": "2026-11-01",
                        "path": "order.requested_delivery_date",
                        "reason": "not_found",
                    }
                ]
            },
        }

        fields = draft_fields(annotation)

        self.assertEqual(fields[0]["label"], "DOCUMENT_TYPE")
        order = next(item for item in fields if item["label"] == "ORDER_NUMBER")
        delivery = next(item for item in fields if item["label"] == "DELIVERY_DATE")
        self.assertEqual(order["status"], "UNREVIEWED")
        self.assertEqual(order["evidence"]["bbox"], [10, 20, 80, 30])
        self.assertEqual(delivery["status"], "AMBIGUOUS")
        self.assertNotIn("evidence", delivery)

    def test_studio_draft_suppresses_duplicate_occurrences_without_fake_paths(self):
        annotation = {
            "document_labels": {
                "document_type": "purchase_order",
                "evidence": {"page": 1, "bbox": [10, 10, 100, 30]},
            },
            "pages": [
                {
                    "page": 1,
                    "regions": [
                        {
                            "label": "LINE_ITEM_QUANTITY",
                            "value": "1",
                            "source_path": "order.line_items[0].quantity",
                            "bbox": [100, 100, 120, 120],
                            "match_method": "line_geometry",
                        },
                        {
                            "label": "LINE_ITEM_QUANTITY",
                            "value": "1",
                            "source_path": "order.line_items[0].quantity",
                            "bbox": [200, 100, 220, 120],
                            "match_method": "line_geometry",
                        },
                    ],
                }
            ],
        }

        fields = draft_fields(annotation)

        quantities = [item for item in fields if item["label"] == "LINE_ITEM_QUANTITY"]
        document_type = next(item for item in fields if item["label"] == "DOCUMENT_TYPE")
        self.assertEqual(len(quantities), 1)
        self.assertNotIn("#occurrence-", quantities[0]["path"])
        self.assertEqual(document_type["evidence"]["bbox"], [10, 10, 100, 30])

    def test_studio_draft_turns_degenerate_region_into_ambiguous_field(self):
        annotation = {
            "document_labels": {"document_type": "purchase_order"},
            "pages": [
                {
                    "page": 1,
                    "regions": [
                        {
                            "label": "LINE_ITEM_QUANTITY",
                            "value": "1",
                            "source_path": "order.line_items[0].quantity",
                            "bbox": [100, 100, 100, 110],
                        }
                    ],
                }
            ],
        }

        quantity = next(
            item for item in draft_fields(annotation)
            if item["label"] == "LINE_ITEM_QUANTITY"
        )

        self.assertEqual(quantity["status"], "AMBIGUOUS")
        self.assertNotIn("evidence", quantity)

    def test_studio_draft_compacts_sparse_machine_line_indexes(self):
        annotation = {
            "document_labels": {"document_type": "purchase_order"},
            "pages": [
                {
                    "page": 1,
                    "regions": [
                        {
                            "label": "LINE_ITEM_REFERENCE",
                            "value": "REF-1",
                            "source_path": "order.line_items[0].references.article_number",
                            "bbox": [10, 100, 80, 120],
                        },
                        {
                            "label": "LINE_ITEM_REFERENCE",
                            "value": "REF-4",
                            "source_path": "order.lines[3].references.article_number",
                            "bbox": [10, 200, 80, 220],
                        },
                        {
                            "label": "LINE_ITEM_QUANTITY",
                            "value": 2,
                            "source_path": "order.line_items[6].quantity",
                            "bbox": [300, 300, 330, 320],
                        },
                    ],
                }
            ],
        }

        fields = draft_fields(annotation)
        line_paths = [
            field["path"] for field in fields if field["label"].startswith("LINE_ITEM_")
        ]

        self.assertEqual(
            line_paths,
            [
                "order.line_items[0].references.article_number",
                "order.line_items[1].references.article_number",
                "order.line_items[2].quantity",
            ],
        )


if __name__ == "__main__":
    unittest.main()

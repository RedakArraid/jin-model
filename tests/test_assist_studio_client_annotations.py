from __future__ import annotations

from scripts.assist_studio_client_annotations import (
    _client_match,
    _merge_assisted_fields,
    _proposal_score,
)


def test_client_match_uses_filename_and_order_parties() -> None:
    row = {
        "filename": "commande.pdf",
        "extraction_json": '{"order":{"parties":{"buyer":{"name":"ISERBA GROUPE"}}}}',
    }
    assert _client_match(row, "iserba") is True
    assert _client_match(row, "rexel") is False


def test_assisted_merge_uses_validated_frame_without_copying_source_value() -> None:
    current = [
        {
            "path": "order.customer_order_number.value",
            "label": "ORDER_NUMBER",
            "status": "AMBIGUOUS",
            "extracted_value": "TARGET-42",
            "corrected_value": None,
            "evidence": {"page": 1, "bbox": [1, 1, 2, 2]},
        },
        {
            "path": "order.line_items[0].references.article_number",
            "label": "LINE_ITEM_REFERENCE",
            "status": "AMBIGUOUS",
            "extracted_value": "8738717111",
            "corrected_value": None,
            "evidence": {"page": 1, "bbox": [3, 3, 4, 4]},
        },
    ]
    applied = [
        {
            "path": "order.customer_order_number.value",
            "label": "ORDER_NUMBER",
            "compartment": "HEADER",
            "status": "UNREVIEWED",
            "extracted_value": "SOURCE-MUST-NOT-WIN",
            "corrected_value": None,
            "evidence": {"page": 1, "bbox": [10, 20, 30, 40]},
        }
    ]

    merged = _merge_assisted_fields(
        current,
        applied,
        source_filename="ISERBA_VALIDEE.pdf",
    )

    assert merged[0]["extracted_value"] == "TARGET-42"
    assert merged[0]["evidence"]["bbox"] == [10, 20, 30, 40]
    assert merged[0]["status"] == "UNREVIEWED"
    assert merged[-1]["label"] == "LINE_ITEM_REFERENCE"
    assert merged[-1]["extracted_value"] == "8738717111"


def test_proposal_score_rewards_agreement_and_alignment() -> None:
    current = {"order.order_date.value": {"extracted_value": "2026-10-28"}}
    matching = {
        "fields": [
            {
                "path": "order.order_date.value",
                "extracted_value": "28/10/2026",
            }
        ],
        "alignment": {"alignedFieldCount": 1},
        "warnings": [],
        "skippedFields": [],
    }
    weak = {
        "fields": [
            {"path": "order.order_date.value", "extracted_value": "AUTRE VALEUR"}
        ],
        "alignment": {"alignedFieldCount": 0},
        "warnings": ["géométrie différente"],
        "skippedFields": [],
    }

    matching_score, matching_metrics = _proposal_score(matching, current)
    weak_score, _ = _proposal_score(weak, current)

    assert matching_score > weak_score
    assert matching_metrics["agreementCount"] == 1

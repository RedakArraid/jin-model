from __future__ import annotations

from jin_runtime.training_evaluation import (
    benchmark_decision,
    classification_metrics,
    normalize_field_value,
    region_value_metrics,
)
from jin_runtime.training_split import assign_document_splits, split_sizes


def test_document_split_is_deterministic_disjoint_and_has_a_holdout():
    document_ids = [f"document-{index:03d}" for index in range(20)]
    first = assign_document_splits(document_ids)
    second = assign_document_splits(reversed(document_ids))

    assert first == second
    assert {split: list(first.values()).count(split) for split in ("train", "validation", "test")} == {
        "train": 14,
        "validation": 3,
        "test": 3,
    }
    assert split_sizes(2) == {"train": 1, "validation": 1, "test": 0}


def test_metrics_do_not_hide_field_errors_behind_the_outside_class():
    report = classification_metrics(
        ["O", "O", "ORDER_NUMBER", "O"],
        ["O", "ORDER_NUMBER", "ORDER_NUMBER", "O"],
        outside_label="O",
    )

    assert report["tokenAccuracy"] == 0.75
    assert report["nonOutsideAccuracy"] == 0.5
    assert report["nonOutsideF1"] < report["tokenAccuracy"]
    assert report["perLabel"]["ORDER_NUMBER"]["support"] == 2


def test_region_benchmark_compares_candidate_and_original_jin_values():
    report = region_value_metrics(
        [
            {
                "label": "ORDER_NUMBER",
                "truth": "PO-123",
                "candidate": "PO 123",
                "baselinePresent": True,
                "baseline": "PO-12B",
            },
            {
                "label": "LINE_ITEM_REFERENCE",
                "truth": "8738717111",
                "candidate": "8738717111",
                "baselinePresent": False,
                "baseline": None,
            },
        ]
    )

    assert normalize_field_value("PO-123") == "po123"
    assert report["candidateExactAccuracy"] == 1.0
    assert report["baselineExactAccuracy"] == 0.0
    assert report["baselineCoverage"] == 0.5


def test_small_holdout_never_authorizes_automatic_promotion():
    decision = benchmark_decision(
        test_documents=3,
        value_metrics={
            "candidateExactAccuracy": 1.0,
            "baselineExactAccuracy": 0.0,
            "perLabel": {},
        },
        token_metrics={"nonOutsideF1": 1.0},
    )

    assert decision["status"] == "INSUFFICIENT_HOLDOUT"
    assert decision["automaticPromotion"] is False

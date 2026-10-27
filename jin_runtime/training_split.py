"""Deterministic, document-level splits for reviewed Studio datasets.

The split is materialized in the dataset manifest.  Pages from one document
therefore never leak between training, validation and the final benchmark.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable


SPLIT_POLICY_VERSION = "jin-document-holdout-v1"


def _rank(document_id: str) -> tuple[str, str]:
    normalized = str(document_id or "").strip()
    digest = hashlib.sha256(
        f"{SPLIT_POLICY_VERSION}:{normalized}".encode("utf-8")
    ).hexdigest()
    return digest, normalized


def split_sizes(document_count: int) -> dict[str, int]:
    """Return conservative 70/15/15 document counts with useful small sets."""

    count = max(0, int(document_count))
    if count == 0:
        return {"train": 0, "validation": 0, "test": 0}
    if count == 1:
        return {"train": 1, "validation": 0, "test": 0}
    if count == 2:
        return {"train": 1, "validation": 1, "test": 0}

    test = max(1, round(count * 0.15))
    validation = max(1, round(count * 0.15))
    while count - validation - test < 1:
        if test >= validation and test > 1:
            test -= 1
        elif validation > 1:
            validation -= 1
        else:
            break
    return {
        "train": count - validation - test,
        "validation": validation,
        "test": test,
    }


def assign_document_splits(document_ids: Iterable[str]) -> dict[str, str]:
    """Assign unique document ids to stable train/validation/test partitions."""

    unique = list(
        dict.fromkeys(
            str(document_id or "").strip()
            for document_id in document_ids
            if str(document_id or "").strip()
        )
    )
    ranked = sorted(unique, key=_rank)
    sizes = split_sizes(len(ranked))
    test_end = sizes["test"]
    validation_end = test_end + sizes["validation"]
    assignments: dict[str, str] = {}
    for index, document_id in enumerate(ranked):
        if index < test_end:
            split = "test"
        elif index < validation_end:
            split = "validation"
        else:
            split = "train"
        assignments[document_id] = split
    return assignments


__all__ = [
    "SPLIT_POLICY_VERSION",
    "assign_document_splits",
    "split_sizes",
]

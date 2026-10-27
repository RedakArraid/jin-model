"""Dependency-light metrics shared by JIN's trainer and benchmark."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping, Sequence


CRITICAL_FIELD_LABELS = frozenset(
    {
        "ORDER_NUMBER",
        "ORDER_DATE",
        "DELIVERY_DATE",
        "LINE_ITEM_REFERENCE",
        "LINE_ITEM_QUANTITY",
        "LINE_ITEM_UNIT_PRICE",
        "LINE_ITEM_TOTAL",
        "TOTAL_NET",
        "TOTAL_GROSS",
    }
)


def finite_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def normalize_field_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (list, tuple, set)):
        text = " ".join(str(item) for item in value)
    else:
        text = str(value)
    text = "".join(
        character
        for character in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(character)
    ).casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def value_similarity(prediction: Any, reference: Any) -> float:
    predicted = normalize_field_value(prediction)
    expected = normalize_field_value(reference)
    if not predicted and not expected:
        return 1.0
    if not predicted or not expected:
        return 0.0
    return SequenceMatcher(None, predicted, expected).ratio()


def classification_metrics(
    predictions: Iterable[Any],
    references: Iterable[Any],
    *,
    outside_label: Any = "O",
    ignored_label: Any = -100,
) -> dict[str, Any]:
    """Calculate accuracy and entity micro/macro metrics without sklearn."""

    pairs = [
        (prediction, reference)
        for prediction, reference in zip(predictions, references)
        if reference != ignored_label
    ]
    if not pairs:
        return {
            "tokenAccuracy": 0.0,
            "nonOutsideAccuracy": 0.0,
            "nonOutsidePrecision": 0.0,
            "nonOutsideRecall": 0.0,
            "nonOutsideF1": 0.0,
            "macroF1": 0.0,
            "support": 0,
            "nonOutsideSupport": 0,
            "perLabel": {},
        }

    correct = sum(prediction == reference for prediction, reference in pairs)
    labels = sorted(
        {
            label
            for pair in pairs
            for label in pair
            if label != outside_label and label != ignored_label
        },
        key=str,
    )
    per_label: dict[str, dict[str, Any]] = {}
    total_tp = total_fp = total_fn = 0
    macro_values: list[float] = []
    non_outside_correct = 0
    non_outside_support = 0
    for prediction, reference in pairs:
        if reference != outside_label:
            non_outside_support += 1
            if prediction == reference:
                non_outside_correct += 1

    for label in labels:
        tp = sum(prediction == label and reference == label for prediction, reference in pairs)
        fp = sum(prediction == label and reference != label for prediction, reference in pairs)
        fn = sum(prediction != label and reference == label for prediction, reference in pairs)
        support = tp + fn
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_label[str(label)] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
            "truePositive": tp,
            "falsePositive": fp,
            "falseNegative": fn,
        }
        total_tp += tp
        total_fp += fp
        total_fn += fn
        if support:
            macro_values.append(f1)

    precision = total_tp / (total_tp + total_fp) if total_tp + total_fp else 0.0
    recall = total_tp / (total_tp + total_fn) if total_tp + total_fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tokenAccuracy": correct / len(pairs),
        "nonOutsideAccuracy": (
            non_outside_correct / non_outside_support if non_outside_support else 0.0
        ),
        "nonOutsidePrecision": precision,
        "nonOutsideRecall": recall,
        "nonOutsideF1": f1,
        "macroF1": sum(macro_values) / len(macro_values) if macro_values else 0.0,
        "support": len(pairs),
        "nonOutsideSupport": non_outside_support,
        "perLabel": per_label,
    }


def region_value_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate candidate and active-runtime value matches by semantic label."""

    per_label_counts: dict[str, Counter[str]] = defaultdict(Counter)
    totals: Counter[str] = Counter()
    for record in records:
        label = str(record.get("label") or "UNKNOWN")
        truth = record.get("truth")
        candidate = record.get("candidate")
        baseline_present = bool(record.get("baselinePresent"))
        baseline = record.get("baseline") if baseline_present else None
        candidate_similarity = value_similarity(candidate, truth)
        baseline_similarity = value_similarity(baseline, truth) if baseline_present else 0.0
        candidate_exact = bool(normalize_field_value(truth)) and candidate_similarity == 1.0
        baseline_exact = bool(normalize_field_value(truth)) and baseline_similarity == 1.0
        values = {
            "support": 1,
            "candidateExact": int(candidate_exact),
            "candidateSoft": int(candidate_similarity >= 0.9),
            "baselineExact": int(baseline_exact),
            "baselineSoft": int(baseline_similarity >= 0.9),
            "baselinePresent": int(baseline_present),
        }
        totals.update(values)
        per_label_counts[label].update(values)

    def summarize(counts: Mapping[str, int]) -> dict[str, Any]:
        support = int(counts.get("support", 0))
        return {
            "support": support,
            "candidateExactAccuracy": counts.get("candidateExact", 0) / support if support else 0.0,
            "candidateSoftAccuracy": counts.get("candidateSoft", 0) / support if support else 0.0,
            "baselineExactAccuracy": counts.get("baselineExact", 0) / support if support else 0.0,
            "baselineSoftAccuracy": counts.get("baselineSoft", 0) / support if support else 0.0,
            "baselineCoverage": counts.get("baselinePresent", 0) / support if support else 0.0,
        }

    return {
        **summarize(totals),
        "perLabel": {
            label: summarize(counts)
            for label, counts in sorted(per_label_counts.items())
        },
    }


def benchmark_decision(
    *,
    test_documents: int,
    value_metrics: Mapping[str, Any],
    token_metrics: Mapping[str, Any],
    minimum_holdout_documents: int = 10,
) -> dict[str, Any]:
    """Return an auditable recommendation; promotion always remains manual."""

    candidate = finite_float(value_metrics.get("candidateExactAccuracy"))
    baseline = finite_float(value_metrics.get("baselineExactAccuracy"))
    per_label = value_metrics.get("perLabel") or {}
    critical_regressions = []
    for label in sorted(CRITICAL_FIELD_LABELS):
        metrics = per_label.get(label)
        if not isinstance(metrics, Mapping) or int(metrics.get("support") or 0) < 2:
            continue
        delta = finite_float(metrics.get("candidateExactAccuracy")) - finite_float(
            metrics.get("baselineExactAccuracy")
        )
        if delta < -0.02:
            critical_regressions.append({"label": label, "delta": delta})

    reasons: list[str] = []
    if test_documents < minimum_holdout_documents:
        status = "INSUFFICIENT_HOLDOUT"
        reasons.append(
            f"Le holdout contient {test_documents} documents; {minimum_holdout_documents} sont requis pour une décision fiable."
        )
    elif critical_regressions:
        status = "REGRESSION"
        reasons.append("Au moins un champ critique régresse sur le jeu de test.")
    elif candidate > baseline and finite_float(token_metrics.get("nonOutsideF1")) >= 0.70:
        status = "CANDIDATE_BETTER"
        reasons.append("Le candidat améliore l'exactitude des valeurs sans signal critique de régression.")
    elif candidate < baseline:
        status = "REGRESSION"
        reasons.append("L'exactitude des valeurs du candidat est inférieure à celle du JIN actif.")
    else:
        status = "REVIEW_REQUIRED"
        reasons.append("Les résultats ne justifient pas encore une promotion.")
    return {
        "status": status,
        "automaticPromotion": False,
        "minimumHoldoutDocuments": minimum_holdout_documents,
        "criticalRegressions": critical_regressions,
        "reasons": reasons,
    }


__all__ = [
    "CRITICAL_FIELD_LABELS",
    "benchmark_decision",
    "classification_metrics",
    "finite_float",
    "normalize_field_value",
    "region_value_metrics",
    "value_similarity",
]

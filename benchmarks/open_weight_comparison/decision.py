from __future__ import annotations

from collections import Counter
from typing import Any

BASELINE_MODEL = "jin"

QUALITY_ORDER = (
    ("field_exact_match", True),
    ("field_token_f1", True),
    ("missing_field_rate", False),
    ("hallucination_rate", False),
    ("line_item_reference_recall", True),
)

DOCUMENT_METRICS = (
    "field_exact_match",
    "field_token_f1",
    "missing_field_rate",
    "hallucination_rate",
    "line_item_reference_recall",
    "mean_field_bbox_iou",
    "zone_type_accuracy",
    "mean_zone_bbox_iou",
)


def _documents_by_name(model_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("filename")): row
        for row in model_report.get("documents") or []
        if row.get("filename")
    }


def _number(mapping: dict[str, Any] | None, key: str) -> float | None:
    if not mapping:
        return None
    value = mapping.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _delta(candidate: dict[str, Any] | None, baseline: dict[str, Any] | None, key: str) -> float | None:
    left = _number(candidate, key)
    right = _number(baseline, key)
    if left is None or right is None:
        return None
    return left - right


def _winner(candidate: dict[str, Any], baseline: dict[str, Any]) -> str:
    for metric, higher_is_better in QUALITY_ORDER:
        left = _number(candidate, metric)
        right = _number(baseline, metric)
        if left is None or right is None:
            continue
        difference = left - right
        if abs(difference) <= 1e-12:
            continue
        candidate_better = difference > 0 if higher_is_better else difference < 0
        return "CANDIDATE" if candidate_better else "JIN"
    return "TIE"


def _field_detail_map(document_row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    metrics = document_row.get("metrics") or {}
    return {
        str(item.get("path")): item
        for item in metrics.get("details") or []
        if isinstance(item, dict) and item.get("path")
    }


def build_field_comparison_rows(
    report: dict[str, Any],
    *,
    baseline_model: str = BASELINE_MODEL,
) -> list[dict[str, Any]]:
    models = report.get("models") or {}
    baseline_report = models.get(baseline_model) or {}
    baseline_docs = _documents_by_name(baseline_report)
    rows: list[dict[str, Any]] = []

    for model_name, model_report in models.items():
        if model_name == baseline_model or model_report.get("track") != "direct_ie":
            continue
        candidate_docs = _documents_by_name(model_report)
        for filename in sorted(set(baseline_docs) | set(candidate_docs)):
            baseline_doc = baseline_docs.get(filename) or {}
            candidate_doc = candidate_docs.get(filename) or {}
            if baseline_doc.get("error") or candidate_doc.get("error"):
                continue
            baseline_details = _field_detail_map(baseline_doc)
            candidate_details = _field_detail_map(candidate_doc)
            for path in sorted(set(baseline_details) | set(candidate_details)):
                jin = baseline_details.get(path) or {}
                candidate = candidate_details.get(path) or {}
                jin_match = bool(jin.get("match"))
                candidate_match = bool(candidate.get("match"))
                if jin_match and candidate_match:
                    outcome = "BOTH_CORRECT"
                elif candidate_match:
                    outcome = "CANDIDATE_WIN"
                elif jin_match:
                    outcome = "JIN_WIN"
                else:
                    outcome = "BOTH_WRONG"
                rows.append({
                    "model": model_name,
                    "baseline_model": baseline_model,
                    "filename": filename,
                    "path": path,
                    "expected": candidate.get("expected", jin.get("expected")),
                    "jin_actual": jin.get("actual"),
                    "candidate_actual": candidate.get("actual"),
                    "jin_match": jin_match,
                    "candidate_match": candidate_match,
                    "jin_token_f1": jin.get("token_f1"),
                    "candidate_token_f1": candidate.get("token_f1"),
                    "delta_vs_jin_token_f1": (
                        float(candidate.get("token_f1") or 0.0)
                        - float(jin.get("token_f1") or 0.0)
                    ),
                    "outcome": outcome,
                })
    return rows


def build_head_to_head_rows(
    report: dict[str, Any],
    field_rows: list[dict[str, Any]] | None = None,
    *,
    baseline_model: str = BASELINE_MODEL,
) -> list[dict[str, Any]]:
    models = report.get("models") or {}
    baseline_report = models.get(baseline_model) or {}
    baseline_docs = _documents_by_name(baseline_report)
    field_rows = field_rows if field_rows is not None else build_field_comparison_rows(
        report, baseline_model=baseline_model
    )

    field_counts: dict[tuple[str, str], Counter[str]] = {}
    for row in field_rows:
        key = (str(row.get("model")), str(row.get("filename")))
        field_counts.setdefault(key, Counter())[str(row.get("outcome"))] += 1

    rows: list[dict[str, Any]] = []
    for model_name, model_report in models.items():
        if model_name == baseline_model or model_report.get("track") != "direct_ie":
            continue
        candidate_docs = _documents_by_name(model_report)
        for filename in sorted(set(baseline_docs) | set(candidate_docs)):
            baseline_doc = baseline_docs.get(filename)
            candidate_doc = candidate_docs.get(filename)
            baseline_error = (baseline_doc or {}).get("error")
            candidate_error = (candidate_doc or {}).get("error")
            if baseline_doc is None or candidate_doc is None:
                status = "MISSING_DOCUMENT_RESULT"
            elif baseline_error or candidate_error:
                status = "ERROR"
            elif not baseline_doc.get("metrics") or not candidate_doc.get("metrics"):
                status = "UNSCORED"
            else:
                status = "COMPARABLE"

            baseline_metrics = (baseline_doc or {}).get("metrics") or {}
            candidate_metrics = (candidate_doc or {}).get("metrics") or {}
            baseline_resources = (baseline_doc or {}).get("resources") or {}
            candidate_resources = (candidate_doc or {}).get("resources") or {}
            counts = field_counts.get((model_name, filename), Counter())

            row: dict[str, Any] = {
                "model": model_name,
                "baseline_model": baseline_model,
                "filename": filename,
                "comparison_status": status,
                "quality_winner": (
                    _winner(candidate_metrics, baseline_metrics)
                    if status == "COMPARABLE"
                    else "UNAVAILABLE"
                ),
                "jin_error": baseline_error,
                "candidate_error": candidate_error,
                "field_candidate_wins": counts.get("CANDIDATE_WIN", 0),
                "field_jin_wins": counts.get("JIN_WIN", 0),
                "field_both_correct": counts.get("BOTH_CORRECT", 0),
                "field_both_wrong": counts.get("BOTH_WRONG", 0),
            }
            for metric in DOCUMENT_METRICS:
                row[f"jin_{metric}"] = baseline_metrics.get(metric)
                row[f"candidate_{metric}"] = candidate_metrics.get(metric)
                row[f"delta_vs_jin_{metric}"] = _delta(
                    candidate_metrics, baseline_metrics, metric
                )

            jin_latency = _number(baseline_resources, "latency_seconds")
            candidate_latency = _number(candidate_resources, "latency_seconds")
            jin_rss = _number(baseline_resources, "peak_rss_mb")
            candidate_rss = _number(candidate_resources, "peak_rss_mb")
            row.update({
                "jin_latency_seconds": jin_latency,
                "candidate_latency_seconds": candidate_latency,
                "delta_vs_jin_latency_seconds": (
                    candidate_latency - jin_latency
                    if candidate_latency is not None and jin_latency is not None
                    else None
                ),
                "latency_ratio_vs_jin": (
                    candidate_latency / jin_latency
                    if candidate_latency is not None and jin_latency not in (None, 0.0)
                    else None
                ),
                "jin_peak_rss_mb": jin_rss,
                "candidate_peak_rss_mb": candidate_rss,
                "delta_vs_jin_peak_rss_mb": (
                    candidate_rss - jin_rss
                    if candidate_rss is not None and jin_rss is not None
                    else None
                ),
                "peak_rss_ratio_vs_jin": (
                    candidate_rss / jin_rss
                    if candidate_rss is not None and jin_rss not in (None, 0.0)
                    else None
                ),
            })
            rows.append(row)
    return rows


def _pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.2f}%"


def _num(value: Any, suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}{suffix}"


def _signed_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:+.2f} pp"


def _markdown_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    return [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *["| " + " | ".join(row) + " |" for row in rows],
    ]


def render_decision_report(
    report: dict[str, Any],
    head_rows: list[dict[str, Any]],
    field_rows: list[dict[str, Any]],
    *,
    baseline_model: str = BASELINE_MODEL,
) -> str:
    models = report.get("models") or {}
    baseline = report.get("baseline") or {}
    lines = [
        "# JIN open-weight benchmark decision report",
        "",
        "JIN is the baseline. Direct information-extraction models are compared on the same PDFs and reviewed ground truth.",
        "EmbeddingGemma2 remains a separate zone-semantics track and is not ranked against JIN field extraction.",
        "",
    ]
    synthetic_count = int(
        (report.get("selection") or {}).get("synthetic_ground_truth_documents") or 0
    )
    if synthetic_count:
        lines.extend([
            "> **Synthetic smoke data detected.** "
            f"{synthetic_count} ground-truth document(s) are marked synthetic. "
            "These results validate the benchmark plumbing only and must not be published "
            "as Bosch/JIN business accuracy.",
            "",
        ])

    if not baseline.get("available"):
        lines.extend([
            "## Baseline unavailable",
            "",
            str(baseline.get("qualification") or "JIN did not produce a usable baseline in this campaign."),
            "",
        ])
        return "\n".join(lines) + "\n"

    direct_rows: list[list[str]] = []
    for model_name, model_report in models.items():
        if model_report.get("track") != "direct_ie":
            continue
        summary = model_report.get("summary") or {}
        comparison = model_report.get("comparison_vs_jin") or {}
        delta = comparison.get("delta") or {}
        direct_rows.append([
            model_name,
            "BASELINE" if model_name == baseline_model else _signed_pct(delta.get("field_exact_match")),
            _pct(summary.get("field_exact_match")),
            _pct(summary.get("field_token_f1")),
            _pct(summary.get("missing_field_rate")),
            _pct(summary.get("hallucination_rate")),
            _pct(summary.get("line_item_reference_recall")),
            _num(summary.get("latency_seconds_mean"), " s"),
            _num(summary.get("peak_rss_mb_max"), " MB"),
        ])

    lines.extend([
        "## Direct extraction summary",
        "",
        *_markdown_table(
            [
                "Model",
                "Exact delta vs JIN",
                "Exact match",
                "Token F1",
                "Missing",
                "Hallucination",
                "Line recall",
                "Latency",
                "Peak RSS",
            ],
            direct_rows,
        ),
        "",
    ])

    for model_name, model_report in models.items():
        if model_name == baseline_model or model_report.get("track") != "direct_ie":
            continue
        relevant = [
            row for row in head_rows
            if row.get("model") == model_name and row.get("comparison_status") == "COMPARABLE"
        ]
        doc_counts = Counter(str(row.get("quality_winner")) for row in relevant)
        relevant_fields = [row for row in field_rows if row.get("model") == model_name]
        field_counts = Counter(str(row.get("outcome")) for row in relevant_fields)

        lines.extend([
            f"## {model_name} vs JIN",
            "",
            f"Comparable documents: **{len(relevant)}**. "
            f"{model_name} wins **{doc_counts.get('CANDIDATE', 0)}**, "
            f"JIN wins **{doc_counts.get('JIN', 0)}**, "
            f"ties **{doc_counts.get('TIE', 0)}**.",
            "",
            f"Field-level: {model_name} wins **{field_counts.get('CANDIDATE_WIN', 0)}**, "
            f"JIN wins **{field_counts.get('JIN_WIN', 0)}**, "
            f"both correct **{field_counts.get('BOTH_CORRECT', 0)}**, "
            f"both wrong **{field_counts.get('BOTH_WRONG', 0)}**.",
            "",
        ])

        better = sorted(
            [
                row for row in relevant
                if (
                    (row.get("delta_vs_jin_field_exact_match") or 0.0) > 0
                    or (
                        abs(row.get("delta_vs_jin_field_exact_match") or 0.0) <= 1e-12
                        and (row.get("delta_vs_jin_field_token_f1") or 0.0) > 0
                    )
                )
            ],
            key=lambda row: (
                row.get("delta_vs_jin_field_exact_match") or 0.0,
                row.get("delta_vs_jin_field_token_f1") or 0.0,
            ),
            reverse=True,
        )[:10]
        worse = sorted(
            [
                row for row in relevant
                if (
                    (row.get("delta_vs_jin_field_exact_match") or 0.0) < 0
                    or (
                        abs(row.get("delta_vs_jin_field_exact_match") or 0.0) <= 1e-12
                        and (row.get("delta_vs_jin_field_token_f1") or 0.0) < 0
                    )
                )
            ],
            key=lambda row: (
                row.get("delta_vs_jin_field_exact_match") or 0.0,
                row.get("delta_vs_jin_field_token_f1") or 0.0,
            ),
        )[:10]

        if better:
            lines.extend([
                f"### PDFs where {model_name} improves on JIN",
                "",
                *_markdown_table(
                    ["PDF", "Exact delta", "Token F1 delta", "Field wins", "JIN field wins"],
                    [
                        [
                            str(row.get("filename")),
                            _signed_pct(row.get("delta_vs_jin_field_exact_match")),
                            _signed_pct(row.get("delta_vs_jin_field_token_f1")),
                            str(row.get("field_candidate_wins") or 0),
                            str(row.get("field_jin_wins") or 0),
                        ]
                        for row in better
                    ],
                ),
                "",
            ])
        if worse:
            lines.extend([
                "### PDFs where JIN remains better",
                "",
                *_markdown_table(
                    ["PDF", "Exact delta", "Token F1 delta", "Field wins", "JIN field wins"],
                    [
                        [
                            str(row.get("filename")),
                            _signed_pct(row.get("delta_vs_jin_field_exact_match")),
                            _signed_pct(row.get("delta_vs_jin_field_token_f1")),
                            str(row.get("field_candidate_wins") or 0),
                            str(row.get("field_jin_wins") or 0),
                        ]
                        for row in worse
                    ],
                ),
                "",
            ])

        path_counts: dict[str, Counter[str]] = {}
        for row in relevant_fields:
            path_counts.setdefault(str(row.get("path")), Counter())[str(row.get("outcome"))] += 1
        ranked_paths = sorted(
            path_counts.items(),
            key=lambda item: abs(
                item[1].get("CANDIDATE_WIN", 0) - item[1].get("JIN_WIN", 0)
            ),
            reverse=True,
        )[:15]
        if ranked_paths:
            lines.extend([
                "### Most discriminating fields",
                "",
                *_markdown_table(
                    ["Field path", f"{model_name} wins", "JIN wins", "Both correct", "Both wrong"],
                    [
                        [
                            path,
                            str(counts.get("CANDIDATE_WIN", 0)),
                            str(counts.get("JIN_WIN", 0)),
                            str(counts.get("BOTH_CORRECT", 0)),
                            str(counts.get("BOTH_WRONG", 0)),
                        ]
                        for path, counts in ranked_paths
                    ],
                ),
                "",
            ])

    zone_rows: list[list[str]] = []
    for model_name, model_report in models.items():
        if model_report.get("track") != "zone_semantics":
            continue
        summary = model_report.get("summary") or {}
        zone_rows.append([
            model_name,
            _pct(summary.get("zone_type_accuracy")),
            _pct(summary.get("mean_zone_bbox_iou")),
            _num(summary.get("latency_seconds_mean"), " s"),
            _num(summary.get("peak_rss_mb_max"), " MB"),
        ])
    if zone_rows:
        lines.extend([
            "## Zone-semantics track",
            "",
            *_markdown_table(
                ["Model", "Zone type accuracy", "Mean zone IoU", "Latency", "Peak RSS"],
                zone_rows,
            ),
            "",
            "These scores measure semantic reranking/localization behavior and must not be used as a direct field-extraction ranking against JIN.",
            "",
        ])

    lines.extend([
        "## Interpretation",
        "",
        "- Use field exact match and token F1 as the primary extraction-quality indicators.",
        "- Use missing-field and hallucination rates to distinguish conservative extraction from over-generation.",
        "- Use the per-document and per-field CSV files to identify templates or business fields that justify targeted JIN improvements.",
        "- Do not reuse historical JIN validation percentages as if they were scores from this common benchmark.",
        "",
    ])
    return "\n".join(lines)


def head_to_head_summary(head_rows: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    models = sorted({str(row.get("model")) for row in head_rows if row.get("model")})
    for model in models:
        rows = [
            row for row in head_rows
            if row.get("model") == model and row.get("comparison_status") == "COMPARABLE"
        ]
        counts = Counter(str(row.get("quality_winner")) for row in rows)
        output[model] = {
            "documents_compared": len(rows),
            "candidate_document_wins": counts.get("CANDIDATE", 0),
            "jin_document_wins": counts.get("JIN", 0),
            "document_ties": counts.get("TIE", 0),
        }
    return output

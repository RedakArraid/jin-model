from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any
import re

from .common import normalize_identifier, normalize_number, normalize_text


NUMERIC_PATH_SUFFIXES = {
    "quantity", "unit_price", "line_total", "net", "vat", "gross", "amount_due"
}
IDENTIFIER_PATH_SUFFIXES = {
    "order_number", "customer_reference", "supplier_reference", "postal_code", "reference"
}

def _evaluation_scope(truth: dict[str, Any]) -> list[str]:
    scope = truth.get("_scope") if isinstance(truth, dict) else None
    if isinstance(scope, dict):
        scope = scope.get("paths")
    if not isinstance(scope, list):
        return []
    return [str(item).strip() for item in scope if str(item).strip()]


def _path_in_scope(path: str, scope: list[str]) -> bool:
    if not scope:
        return True
    for rule in scope:
        if rule == path:
            return True
        if rule.endswith(".*") and path.startswith(rule[:-1]):
            return True
        if "[*]" in rule:
            pattern = re.escape(rule).replace(r"\[\*\]", r"\[\d+\]")
            if re.fullmatch(pattern, path):
                return True
    return False


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "spatial" or str(key).startswith("_"):
                continue
            path = f"{prefix}.{key}" if prefix else key
            out.update(_flatten(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            path = f"{prefix}[{index}]"
            out.update(_flatten(child, path))
    else:
        out[prefix] = value
    return out


def _token_f1(expected: Any, actual: Any) -> float:
    left = normalize_text(expected).split()
    right = normalize_text(actual).split()
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    common = Counter(left) & Counter(right)
    overlap = sum(common.values())
    precision = overlap / len(right)
    recall = overlap / len(left)
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _same_value(path: str, expected: Any, actual: Any) -> bool:
    suffix = path.rsplit(".", 1)[-1]
    if suffix in NUMERIC_PATH_SUFFIXES:
        left = normalize_number(expected)
        right = normalize_number(actual)
        return left is not None and right is not None and left == right
    if suffix in IDENTIFIER_PATH_SUFFIXES:
        return normalize_identifier(expected) == normalize_identifier(actual)
    return normalize_text(expected) == normalize_text(actual)


def box_iou(a: list[float], b: list[float]) -> float:
    x0 = max(float(a[0]), float(b[0]))
    y0 = max(float(a[1]), float(b[1]))
    x1 = min(float(a[2]), float(b[2]))
    y1 = min(float(a[3]), float(b[3]))
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area_a = max(0.0, float(a[2]) - float(a[0])) * max(0.0, float(a[3]) - float(a[1]))
    area_b = max(0.0, float(b[2]) - float(b[0])) * max(0.0, float(b[3]) - float(b[1]))
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def _spatial_map(document: dict[str, Any]) -> dict[tuple[str, int], list[float]]:
    out = {}
    spatial = document.get("spatial") if isinstance(document.get("spatial"), dict) else {}
    for item in spatial.get("fields") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        bbox = item.get("bbox")
        page = int(item.get("page") or 1)
        if path and isinstance(bbox, list) and len(bbox) == 4:
            out[(path, page)] = [float(value) for value in bbox]
    return out




def _zone_metrics(prediction: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    pred_spatial = prediction.get("spatial") if isinstance(prediction.get("spatial"), dict) else {}
    truth_spatial = truth.get("spatial") if isinstance(truth.get("spatial"), dict) else {}
    predicted = [item for item in pred_spatial.get("zones") or [] if isinstance(item, dict) and isinstance(item.get("bbox"), list)]
    expected = [item for item in truth_spatial.get("zones") or [] if isinstance(item, dict) and isinstance(item.get("bbox"), list)]
    used = set()
    matched = []
    for target in expected:
        page = int(target.get("page") or 1)
        candidates = [
            (box_iou(item["bbox"], target["bbox"]), index, item)
            for index, item in enumerate(predicted)
            if index not in used and int(item.get("page") or 1) == page
        ]
        if not candidates:
            continue
        iou, index, item = max(candidates, key=lambda row: row[0])
        if iou <= 0:
            continue
        used.add(index)
        matched.append((target, item, iou))
    return {
        "zone_truth_count": len(expected),
        "zone_predicted_count": len(predicted),
        "zone_matched_count": len(matched),
        "zone_type_accuracy": (
            sum(
                normalize_text(target.get("zone_type")) == normalize_text(item.get("zone_type"))
                for target, item, _ in matched
            ) / len(expected)
            if expected else None
        ),
        "mean_zone_bbox_iou": (
            sum(iou for _, _, iou in matched) / len(matched)
            if matched else None
        ),
        "zone_bbox_recall_iou_0_5": (
            sum(iou >= 0.5 for _, _, iou in matched) / len(expected)
            if expected else None
        ),
        "zone_bbox_recall_iou_0_75": (
            sum(iou >= 0.75 for _, _, iou in matched) / len(expected)
            if expected else None
        ),
    }

def evaluate_extraction(prediction: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    expected = _flatten(truth)
    actual = _flatten(prediction)
    scope = _evaluation_scope(truth)
    truth_paths = [
        path
        for path, value in expected.items()
        if value not in (None, "") and _path_in_scope(path, scope)
    ]
    prediction_paths = [
        path
        for path, value in actual.items()
        if value not in (None, "") and _path_in_scope(path, scope)
    ]

    matches = 0
    f1_values = []
    missing = 0
    details = []
    for path in truth_paths:
        wanted = expected[path]
        found = actual.get(path)
        present = found not in (None, "")
        match = present and _same_value(path, wanted, found)
        matches += int(match)
        missing += int(not present)
        f1 = _token_f1(wanted, found) if present else 0.0
        f1_values.append(f1)
        details.append({
            "path": path,
            "expected": wanted,
            "actual": found,
            "match": match,
            "token_f1": f1,
        })

    hallucinations = sum(
        1
        for path in prediction_paths
        if expected.get(path) in (None, "")
    )

    expected_lines = truth.get("lines") or []
    actual_lines = prediction.get("lines") or []
    expected_refs = {
        normalize_identifier(item.get("reference"))
        for item in expected_lines
        if isinstance(item, dict) and item.get("reference")
    }
    actual_refs = {
        normalize_identifier(item.get("reference"))
        for item in actual_lines
        if isinstance(item, dict) and item.get("reference")
    }
    lines_in_scope = (not scope) or any(
        rule == "lines" or rule.startswith("lines[")
        for rule in scope
    )
    if not lines_in_scope:
        line_recall = None
    elif expected_refs:
        line_recall = len(expected_refs & actual_refs) / len(expected_refs)
    else:
        line_recall = min(len(actual_lines), len(expected_lines)) / len(expected_lines) if expected_lines else None

    pred_spatial = _spatial_map(prediction)
    truth_spatial = _spatial_map(truth)
    ious = [
        box_iou(pred_spatial[key], box)
        for key, box in truth_spatial.items()
        if key in pred_spatial
    ]

    zone_metrics = _zone_metrics(prediction, truth)
    return {
        "scope_paths": scope,
        "truth_field_count": len(truth_paths),
        "predicted_field_count": len(prediction_paths),
        "exact_match_count": matches,
        "field_exact_match": matches / len(truth_paths) if truth_paths else None,
        "field_token_f1": sum(f1_values) / len(f1_values) if f1_values else None,
        "missing_field_count": missing,
        "missing_field_rate": missing / len(truth_paths) if truth_paths else None,
        "hallucinated_field_count": hallucinations,
        "hallucination_rate": hallucinations / len(prediction_paths) if prediction_paths else 0.0,
        "line_item_reference_recall": line_recall,
        "spatial_field_count": len(truth_spatial),
        "spatial_matched_count": len(ious),
        "mean_field_bbox_iou": sum(ious) / len(ious) if ious else None,
        "field_bbox_recall_iou_0_5": sum(value >= 0.5 for value in ious) / len(truth_spatial) if truth_spatial else None,
        "field_bbox_recall_iou_0_75": sum(value >= 0.75 for value in ious) / len(truth_spatial) if truth_spatial else None,
        "details": details,
        **zone_metrics,
    }


def aggregate_model(rows: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in rows if not row.get("error")]
    scored = [row for row in completed if isinstance(row.get("metrics"), dict)]

    def mean(key: str) -> float | None:
        values = [row["metrics"].get(key) for row in scored]
        values = [float(value) for value in values if value is not None]
        return sum(values) / len(values) if values else None

    latencies = [float(row["resources"]["latency_seconds"]) for row in completed if row.get("resources")]
    peak = [float(row["resources"]["peak_rss_mb"]) for row in completed if row.get("resources")]
    return {
        "documents": len(rows),
        "completed": len(completed),
        "errors": len(rows) - len(completed),
        "json_valid_rate": sum(bool(row.get("json_valid", True)) for row in completed) / len(completed) if completed else 0.0,
        "field_exact_match": mean("field_exact_match"),
        "field_token_f1": mean("field_token_f1"),
        "missing_field_rate": mean("missing_field_rate"),
        "hallucination_rate": mean("hallucination_rate"),
        "line_item_reference_recall": mean("line_item_reference_recall"),
        "mean_field_bbox_iou": mean("mean_field_bbox_iou"),
        "field_bbox_recall_iou_0_5": mean("field_bbox_recall_iou_0_5"),
        "field_bbox_recall_iou_0_75": mean("field_bbox_recall_iou_0_75"),
        "zone_type_accuracy": mean("zone_type_accuracy"),
        "mean_zone_bbox_iou": mean("mean_zone_bbox_iou"),
        "zone_bbox_recall_iou_0_5": mean("zone_bbox_recall_iou_0_5"),
        "zone_bbox_recall_iou_0_75": mean("zone_bbox_recall_iou_0_75"),
        "latency_seconds_mean": sum(latencies) / len(latencies) if latencies else None,
        "peak_rss_mb_max": max(peak) if peak else None,
    }

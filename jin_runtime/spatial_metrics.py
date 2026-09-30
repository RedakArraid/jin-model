from __future__ import annotations

from collections import defaultdict
from typing import Any


def box_area(box) -> float:
    return max(0.0, float(box[2]) - float(box[0])) * max(
        0.0, float(box[3]) - float(box[1])
    )


def _intersection_area(a, b) -> float:
    x0 = max(float(a[0]), float(b[0]))
    y0 = max(float(a[1]), float(b[1]))
    x1 = min(float(a[2]), float(b[2]))
    y1 = min(float(a[3]), float(b[3]))
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def box_iou(a, b) -> float:
    intersection = _intersection_area(a, b)
    union = box_area(a) + box_area(b) - intersection
    return intersection / union if union > 0 else 0.0


def box_coverage(predicted, truth) -> float:
    truth_area = box_area(truth)
    return _intersection_area(predicted, truth) / truth_area if truth_area > 0 else 0.0


def contamination_ratio(predicted, truth) -> float:
    predicted_area = box_area(predicted)
    if predicted_area <= 0:
        return 0.0
    return max(
        0.0,
        (predicted_area - _intersection_area(predicted, truth)) / predicted_area,
    )


def _item_type(item: dict[str, Any]) -> str:
    return str(
        item.get("cell_type")
        or item.get("zone_type")
        or item.get("label")
        or item.get("type")
        or ""
    )


def evaluate_spatial_items(
    predicted: list[dict[str, Any]],
    truth: list[dict[str, Any]],
    thresholds=(0.50, 0.75, 0.90),
) -> dict[str, Any]:
    predicted_by_type = defaultdict(list)
    truth_by_type = defaultdict(list)
    for item in predicted:
        if item.get("bbox"):
            predicted_by_type[_item_type(item)].append(item)
    for item in truth:
        if item.get("bbox"):
            truth_by_type[_item_type(item)].append(item)

    matches = []
    unmatched_predicted = []
    unmatched_truth = []

    for item_type in sorted(set(predicted_by_type) | set(truth_by_type)):
        predicted_items = predicted_by_type[item_type]
        truth_items = truth_by_type[item_type]
        used_truth = set()

        for prediction in predicted_items:
            scored = [
                (box_iou(prediction["bbox"], target["bbox"]), index, target)
                for index, target in enumerate(truth_items)
                if index not in used_truth
            ]
            if scored:
                iou, index, target = max(scored, key=lambda item: item[0])
                if iou > 0:
                    used_truth.add(index)
                    matches.append(
                        {
                            "type": item_type,
                            "iou": iou,
                            "coverage": box_coverage(
                                prediction["bbox"], target["bbox"]
                            ),
                            "contamination": contamination_ratio(
                                prediction["bbox"], target["bbox"]
                            ),
                            "predicted": prediction,
                            "truth": target,
                        }
                    )
                    continue
            unmatched_predicted.append(prediction)

        unmatched_truth.extend(
            target
            for index, target in enumerate(truth_items)
            if index not in used_truth
        )

    ious = [match["iou"] for match in matches]
    per_type = {}
    all_types = sorted(set(predicted_by_type) | set(truth_by_type))
    for item_type in all_types:
        type_matches = [match for match in matches if match["type"] == item_type]
        values = [match["iou"] for match in type_matches]
        denominator = max(len(truth_by_type[item_type]), 1)
        metrics = {
            "truth_count": len(truth_by_type[item_type]),
            "predicted_count": len(predicted_by_type[item_type]),
            "matched_count": len(type_matches),
            "mean_iou": sum(values) / len(values) if values else 0.0,
        }
        for threshold in thresholds:
            key = f"recall_iou_{str(threshold).replace('.', '_')}"
            metrics[key] = sum(value >= threshold for value in values) / denominator
        per_type[item_type] = metrics

    return {
        "truth_count": len(truth),
        "predicted_count": len(predicted),
        "matched_count": len(matches),
        "mean_iou": sum(ious) / len(ious) if ious else 0.0,
        "mean_coverage": (
            sum(match["coverage"] for match in matches) / len(matches)
            if matches
            else 0.0
        ),
        "mean_contamination": (
            sum(match["contamination"] for match in matches) / len(matches)
            if matches
            else 0.0
        ),
        "per_type": per_type,
        "unmatched_predicted": unmatched_predicted,
        "unmatched_truth": unmatched_truth,
    }

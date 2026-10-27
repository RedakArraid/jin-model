#!/usr/bin/env python3
"""Benchmark a trained LayoutLMv3 candidate on JIN's frozen test partition."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image
import torch
from transformers import AutoModelForTokenClassification, AutoProcessor

from jin_runtime.training_evaluation import (
    benchmark_decision,
    classification_metrics,
    region_value_metrics,
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _model_label(model: Any, label_id: int) -> str:
    labels = model.config.id2label or {}
    return str(labels.get(label_id, labels.get(str(label_id), "O")))


def predict_word_labels(
    row: dict[str, Any],
    *,
    processor: Any,
    model: Any,
    device: torch.device,
    max_length: int,
) -> tuple[list[str], int]:
    words = list(row.get("words") or [])
    boxes = list(row.get("boxes") or [])
    if not words:
        return [], 0
    with Image.open(row["image_path"]) as source:
        image = source.convert("RGB")
        encoding = processor(
            images=image,
            text=words,
            boxes=boxes,
            truncation=True,
            padding="max_length",
            max_length=max_length,
            return_tensors="pt",
        )
    word_ids = encoding.word_ids(batch_index=0)
    tensor_inputs = {
        key: value.to(device)
        for key, value in encoding.items()
        if isinstance(value, torch.Tensor)
    }
    with torch.no_grad():
        token_predictions = model(**tensor_inputs).logits.argmax(dim=-1)[0].cpu().tolist()

    votes: dict[int, Counter[str]] = defaultdict(Counter)
    for token_index, word_index in enumerate(word_ids):
        if word_index is None or word_index >= len(words):
            continue
        votes[int(word_index)][_model_label(model, int(token_predictions[token_index]))] += 1
    predictions = [
        votes[index].most_common(1)[0][0] if votes.get(index) else "O"
        for index in range(len(words))
    ]
    return predictions, len(votes)


def _inside(box: list[int], region: list[int]) -> bool:
    center_x = (box[0] + box[2]) / 2
    center_y = (box[1] + box[3]) / 2
    return region[0] <= center_x <= region[2] and region[1] <= center_y <= region[3]


def region_records(
    row: dict[str, Any], predicted_labels: list[str]
) -> list[dict[str, Any]]:
    words = list(row.get("words") or [])
    boxes = list(row.get("boxes") or [])
    records = []
    for region in row.get("regions") or []:
        label = str(region.get("label") or "")
        bbox = region.get("bbox")
        if not label or not isinstance(bbox, list) or len(bbox) != 4:
            continue
        selected = [
            str(word)
            for word, box, prediction in zip(words, boxes, predicted_labels)
            if prediction == label and _inside(box, bbox)
        ]
        records.append(
            {
                "documentId": row.get("document_id"),
                "page": row.get("page"),
                "label": label,
                "sourcePath": region.get("source_path"),
                "truth": region.get("value"),
                "candidate": " ".join(selected),
                "baselinePresent": bool(region.get("baseline_present")),
                "baseline": region.get("baseline_value"),
            }
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark a JIN LayoutLMv3 candidate against frozen human truth."
    )
    parser.add_argument("--data", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-length", type=int, default=512)
    args = parser.parse_args()

    data_dir = Path(args.data)
    artifact = Path(args.artifact)
    output = Path(args.output)
    test_rows = read_jsonl(data_dir / "test.jsonl")
    if not test_rows:
        raise RuntimeError("No frozen test pages are available for the benchmark")

    processor = AutoProcessor.from_pretrained(str(artifact), apply_ocr=False)
    model = AutoModelForTokenClassification.from_pretrained(str(artifact))
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    model.eval()

    all_predictions: list[str] = []
    all_references: list[str] = []
    values: list[dict[str, Any]] = []
    represented_words = 0
    total_words = 0
    for row in test_rows:
        predicted, represented = predict_word_labels(
            row,
            processor=processor,
            model=model,
            device=device,
            max_length=args.max_length,
        )
        references = [str(label) for label in row.get("labels") or []]
        if len(predicted) != len(references):
            raise RuntimeError("Candidate predictions and frozen labels are misaligned")
        all_predictions.extend(predicted)
        all_references.extend(references)
        values.extend(region_records(row, predicted))
        represented_words += represented
        total_words += len(references)

    token_metrics = classification_metrics(
        all_predictions,
        all_references,
        outside_label="O",
        ignored_label=-100,
    )
    value_metrics = region_value_metrics(values)
    documents = sorted({str(row.get("document_id")) for row in test_rows})
    decision = benchmark_decision(
        test_documents=len(documents),
        value_metrics=value_metrics,
        token_metrics=token_metrics,
    )
    report = {
        "schemaVersion": "jin-candidate-benchmark-v1",
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "artifact": str(artifact.resolve()),
        "holdout": {
            "documents": len(documents),
            "documentIds": documents,
            "pages": len(test_rows),
            "regions": len(values),
            "words": total_words,
            "wordCoverage": represented_words / total_words if total_words else 0.0,
        },
        "activeJin": {
            "regionExactAccuracy": value_metrics["baselineExactAccuracy"],
            "regionSoftAccuracy": value_metrics["baselineSoftAccuracy"],
            "regionCoverage": value_metrics["baselineCoverage"],
        },
        "candidate": {
            "regionExactAccuracy": value_metrics["candidateExactAccuracy"],
            "regionSoftAccuracy": value_metrics["candidateSoftAccuracy"],
            "tokenMetrics": token_metrics,
        },
        "delta": {
            "regionExactAccuracy": (
                value_metrics["candidateExactAccuracy"]
                - value_metrics["baselineExactAccuracy"]
            ),
            "regionSoftAccuracy": (
                value_metrics["candidateSoftAccuracy"]
                - value_metrics["baselineSoftAccuracy"]
            ),
        },
        "perLabel": value_metrics["perLabel"],
        "decision": decision,
        "promotion": {
            "automatic": False,
            "activeRuntimeChanged": False,
            "note": "Ce benchmark ne remplace jamais automatiquement le modèle JIN actif.",
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

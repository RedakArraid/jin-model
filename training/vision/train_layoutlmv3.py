#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
import torch
from torch.utils.data import Dataset
from transformers import AutoModelForTokenClassification, AutoProcessor, Trainer, TrainingArguments

from jin_runtime.training_evaluation import classification_metrics


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


class LayoutPageDataset(Dataset):
    def __init__(self, rows: list[dict[str, Any]], processor: Any, label2id: dict[str, int], max_length: int = 512):
        self.rows = rows
        self.processor = processor
        self.label2id = label2id
        self.max_length = max_length

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, idx: int) -> dict[str, torch.Tensor]:
        row = self.rows[idx]
        image = Image.open(row["image_path"]).convert("RGB")
        labels = [self.label2id.get(label, self.label2id["O"]) for label in row["labels"]]
        encoded = self.processor(
            images=image,
            text=row["words"],
            boxes=row["boxes"],
            word_labels=labels,
            truncation=True,
            padding="max_length",
            max_length=self.max_length,
            return_tensors="pt",
        )
        return {key: value.squeeze(0) for key, value in encoded.items()}


def metrics_factory(outside_id: int):
    def metrics(eval_prediction: Any) -> dict[str, float]:
        logits, labels = eval_prediction
        predictions = np.argmax(logits, axis=-1).reshape(-1).tolist()
        references = np.asarray(labels).reshape(-1).tolist()
        report = classification_metrics(
            predictions,
            references,
            outside_label=outside_id,
            ignored_label=-100,
        )
        return {
            "token_accuracy": float(report["tokenAccuracy"]),
            "non_outside_accuracy": float(report["nonOutsideAccuracy"]),
            "non_outside_precision": float(report["nonOutsidePrecision"]),
            "non_outside_recall": float(report["nonOutsideRecall"]),
            "non_outside_f1": float(report["nonOutsideF1"]),
            "macro_f1": float(report["macroF1"]),
        }

    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Fine-tune LayoutLMv3 on JIN materialized PDF pages.")
    parser.add_argument("--data", default="data/layout")
    parser.add_argument("--output", default="artifacts/layoutlmv3-jin")
    parser.add_argument("--model", default="microsoft/layoutlmv3-base")
    parser.add_argument("--epochs", type=float, default=5.0)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--learning-rate", type=float, default=2e-5)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    data_dir = Path(args.data)
    train_rows = read_jsonl(data_dir / "train.jsonl")
    validation_rows = read_jsonl(data_dir / "validation.jsonl")
    test_rows = read_jsonl(data_dir / "test.jsonl")
    if not train_rows:
        raise RuntimeError("No training pages found. Materialize and prepare the LFS corpus first.")

    labels = json.loads((data_dir / "labels.json").read_text(encoding="utf-8"))
    if "O" not in labels:
        labels = ["O", *labels]
    label2id = {label: idx for idx, label in enumerate(labels)}
    id2label = {idx: label for label, idx in label2id.items()}

    processor = AutoProcessor.from_pretrained(args.model, apply_ocr=False)
    model = AutoModelForTokenClassification.from_pretrained(
        args.model,
        num_labels=len(labels),
        label2id=label2id,
        id2label=id2label,
    )
    train_dataset = LayoutPageDataset(train_rows, processor, label2id, args.max_length)
    validation_dataset = LayoutPageDataset(validation_rows, processor, label2id, args.max_length) if validation_rows else None
    test_dataset = LayoutPageDataset(test_rows, processor, label2id, args.max_length) if test_rows else None

    training_args = TrainingArguments(
        output_dir=args.output,
        learning_rate=args.learning_rate,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        num_train_epochs=args.epochs,
        weight_decay=0.01,
        logging_steps=20,
        eval_strategy="epoch" if validation_dataset else "no",
        save_strategy="epoch",
        load_best_model_at_end=bool(validation_dataset),
        metric_for_best_model="non_outside_f1" if validation_dataset else None,
        greater_is_better=True,
        report_to=[],
        remove_unused_columns=False,
        fp16=torch.cuda.is_available(),
        save_total_limit=2,
        seed=args.seed,
        data_seed=args.seed,
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=validation_dataset,
        compute_metrics=metrics_factory(label2id["O"]),
    )
    trainer.train()
    trainer.save_model(args.output)
    processor.save_pretrained(args.output)
    validation_metrics = (
        trainer.evaluate(validation_dataset, metric_key_prefix="validation")
        if validation_dataset
        else {}
    )
    test_metrics = (
        trainer.evaluate(test_dataset, metric_key_prefix="test")
        if test_dataset
        else {}
    )
    Path(args.output).mkdir(parents=True, exist_ok=True)
    Path(args.output, "validation_token_metrics.json").write_text(
        json.dumps(validation_metrics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    Path(args.output, "test_token_metrics.json").write_text(
        json.dumps(test_metrics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    Path(args.output, "jin_training_metadata.json").write_text(
        json.dumps(
            {
                "base_model": args.model,
                "labels": labels,
                "train_pages": len(train_rows),
                "validation_pages": len(validation_rows),
                "test_pages": len(test_rows),
                "seed": args.seed,
                "validation_metrics": validation_metrics,
                "test_metrics": test_metrics,
                "task": "layout-aware token classification for document fields and table cells",
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

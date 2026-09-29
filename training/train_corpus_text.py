#!/usr/bin/env python3
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path

import joblib
from sklearn.feature_extraction.text import HashingVectorizer
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import accuracy_score, classification_report

N_FEATURES = 2**10


def load_rows(root: Path):
    with (root / "dataset_manifest.csv").open(encoding="utf-8-sig", newline="") as f:
        manifest = {r["FileName"]: r for r in csv.DictReader(f)}
    with (root / "document_type_audit.csv").open(encoding="utf-8-sig", newline="") as f:
        audit = {r["FileName"]: r for r in csv.DictReader(f, delimiter=";")}
    with (root / "metadata_classified.csv").open(encoding="utf-8-sig", newline="") as f:
        meta = {
            Path(r["FichierLocal"]).name: r
            for r in csv.DictReader(f, delimiter=";")
            if r["Split"] in ("test", "validation")
        }

    rows = []
    for filename, manifest_row in manifest.items():
        audit_row = audit.get(filename, {})
        meta_row = meta.get(filename, {})
        text = " | ".join(
            [
                filename,
                meta_row.get("Sujet", ""),
                meta_row.get("Expediteur", ""),
                audit_row.get("TextPreview", ""),
            ]
        )
        rows.append(
            {
                "split": manifest_row["Split"],
                "family": manifest_row["Family"],
                "decision": audit_row.get("Decision", ""),
                "text": text,
            }
        )
    return rows


def vectorizer():
    return HashingVectorizer(
        analyzer="char_wb",
        ngram_range=(3, 5),
        n_features=N_FEATURES,
        alternate_sign=False,
        norm="l2",
        lowercase=True,
    )


def fit(rows, target):
    vec = vectorizer()
    train = [r for r in rows if r["split"] == "test"]
    validation = [r for r in rows if r["split"] == "validation"]

    model = SGDClassifier(
        loss="log_loss",
        alpha=1e-5,
        max_iter=4000,
        tol=1e-5,
        class_weight="balanced",
        random_state=42,
    )
    model.fit(vec.transform([r["text"] for r in train]), [r[target] for r in train])
    truth = [r[target] for r in validation]
    pred = model.predict(vec.transform([r["text"] for r in validation]))
    return model, {
        "accuracy": float(accuracy_score(truth, pred)),
        "report": classification_report(
            truth, pred, output_dict=True, zero_division=0
        ),
        "train_count": len(train),
        "validation_count": len(validation),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--corpus", required=True)
    p.add_argument("--output", default="data/learning/corpus_text_router.joblib")
    p.add_argument("--metrics", default="data/learning/corpus_text_metrics.json")
    args = p.parse_args()

    rows = load_rows(Path(args.corpus))
    family, family_metrics = fit(rows, "family")
    decision, decision_metrics = fit(rows, "decision")
    bundle = {
        "version": "jin-corpus-text-router-v1",
        "n_features": N_FEATURES,
        "family_model": family,
        "decision_model": decision,
        "metrics": {
            "family": family_metrics,
            "decision": decision_metrics,
        },
    }

    output = Path(args.output)
    metrics = Path(args.metrics)
    output.parent.mkdir(parents=True, exist_ok=True)
    metrics.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, output, compress=9)
    metrics.write_text(
        json.dumps(bundle["metrics"], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "model": str(output),
                "bytes": output.stat().st_size,
                "family_accuracy": family_metrics["accuracy"],
                "decision_accuracy": decision_metrics["accuracy"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

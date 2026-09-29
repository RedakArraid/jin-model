#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import fitz
import joblib
import numpy as np
from PIL import Image
from scipy import sparse
from sklearn.feature_extraction.text import HashingVectorizer
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import SGDClassifier
from sklearn.metrics import accuracy_score, classification_report, log_loss
from sklearn.model_selection import StratifiedKFold, cross_val_predict

LAYOUT_COLS = [
    "pages", "native_page_ratio", "words", "chars", "numeric_ratio",
    "uppercase_ratio", "mean_word_len", "mean_width", "mean_height",
    *[f"grid_{i}" for i in range(16)],
]




def temperature_scale(probabilities: np.ndarray, temperature: float) -> np.ndarray:
    clipped = np.clip(probabilities, 1e-12, 1.0)
    logits = np.log(clipped) / float(temperature)
    logits -= logits.max(axis=1, keepdims=True)
    scaled = np.exp(logits)
    return scaled / scaled.sum(axis=1, keepdims=True)


def decode_exported_name(name: str) -> str:
    return re.sub(r"#U([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), name)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_metadata(metadata_dir: Path):
    by_sha: dict[tuple[str, str], dict[str, str]] = {}
    by_name: dict[tuple[str, str], dict[str, str]] = {}
    with (metadata_dir / "dataset_manifest.csv").open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            by_sha[(row["Split"], row["Sha256"].lower())] = row
            by_name[(row["Split"], row["FileName"])] = row

    audit: dict[tuple[str, str], dict[str, str]] = {}
    with (metadata_dir / "document_type_audit.csv").open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            audit[(row["Split"], row["FileName"])] = row
    return by_sha, by_name, audit


def _profile_one(item: tuple[int, str, str]) -> tuple[int, dict[str, Any], np.ndarray]:
    idx, split, raw_path = item
    path = Path(raw_path)
    doc = fitz.open(path)
    pages = len(doc)
    total_words = total_chars = native_pages = numeric = upper = 0
    grid = [0] * 16
    widths: list[float] = []
    heights: list[float] = []
    wordlens: list[int] = []
    texts: list[str] = []

    for page_idx, page in enumerate(doc):
        rect = page.rect
        widths.append(rect.width)
        heights.append(rect.height)
        words = page.get_text("words", sort=True)
        if words:
            native_pages += 1
        total_words += len(words)
        for word in words:
            x0, y0, x1, y1, text, *_ = word
            text = str(text)
            total_chars += len(text)
            wordlens.append(len(text))
            numeric += int(any(c.isdigit() for c in text))
            upper += int(any(c.isalpha() for c in text) and text.upper() == text)
            cx = (x0 + x1) / 2 / max(rect.width, 1)
            cy = (y0 + y1) / 2 / max(rect.height, 1)
            gx = min(3, max(0, int(cx * 4)))
            gy = min(3, max(0, int(cy * 4)))
            grid[gy * 4 + gx] += 1
        if page_idx < 5:
            texts.append(page.get_text("text", sort=True))

    page0 = doc[0]
    pix = page0.get_pixmap(matrix=fitz.Matrix(0.25, 0.25), colorspace=fitz.csGRAY, alpha=False)
    doc.close()
    image = Image.frombytes("L", [pix.width, pix.height], pix.samples).resize((16, 16), Image.Resampling.BILINEAR)
    visual_array = 1 - np.asarray(image, dtype=np.float32) / 255.0
    visual = np.concatenate([
        visual_array.reshape(-1), visual_array.mean(0), visual_array.mean(1),
        np.array([visual_array.mean(), visual_array.std(), (visual_array > .15).mean(), (visual_array > .35).mean()], dtype=np.float32),
    ]).astype(np.float32)

    grid_total = sum(grid) or 1
    row: dict[str, Any] = {
        "split": split,
        "file": path.name,
        "sha256": sha256_file(path),
        "pages": pages,
        "native_page_ratio": native_pages / max(pages, 1),
        "words": total_words,
        "chars": total_chars,
        "numeric_ratio": numeric / max(total_words, 1),
        "uppercase_ratio": upper / max(total_words, 1),
        "mean_word_len": sum(wordlens) / max(len(wordlens), 1),
        "mean_width": sum(widths) / max(len(widths), 1),
        "mean_height": sum(heights) / max(len(heights), 1),
        **{f"grid_{i}": grid[i] / grid_total for i in range(16)},
        "text": "\n".join(texts)[:30000],
    }
    return idx, row, visual


def extract_profiles(corpus_root: Path, workers: int):
    files: list[tuple[int, str, str]] = []
    for split in ("test", "validation"):
        split_files = sorted((corpus_root / split).rglob("*.pdf")) + sorted((corpus_root / split).rglob("*.PDF"))
        dedup: list[Path] = []
        seen: set[Path] = set()
        for p in split_files:
            if p not in seen:
                seen.add(p); dedup.append(p)
        for p in dedup:
            files.append((len(files), split, str(p)))

    rows: list[dict[str, Any] | None] = [None] * len(files)
    visuals: list[np.ndarray | None] = [None] * len(files)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_profile_one, item) for item in files]
        for completed, future in enumerate(as_completed(futures), 1):
            idx, row, visual = future.result()
            rows[idx] = row; visuals[idx] = visual
            if completed % 200 == 0:
                print(f"profiled {completed}/{len(files)}", flush=True)
    return [x for x in rows if x is not None], np.vstack([x for x in visuals if x is not None])


def attach_labels(rows, by_sha, by_name, audit):
    missing = []
    for row in rows:
        split = row["split"]
        manifest = by_sha.get((split, str(row.get("sha256", "")).lower()))
        if manifest is None:
            manifest = by_name.get((split, decode_exported_name(row["file"])))
        if manifest is None:
            missing.append(row["file"]); continue
        row["manifest_name"] = manifest["FileName"]
        row["family"] = manifest["Family"]
        row["decision"] = audit.get((split, manifest["FileName"]), {}).get("Decision", "")
    return missing


def train(rows, visuals, output: Path, metrics_path: Path):
    layout = np.array([[float(r.get(c, 0) or 0) for c in LAYOUT_COLS] for r in rows], dtype=np.float32)
    for j, col in enumerate(LAYOUT_COLS):
        if col in {"pages", "words", "chars"}:
            layout[:, j] = np.log1p(layout[:, j])

    train_idx = np.array([i for i, r in enumerate(rows) if r["split"] == "test"], dtype=int)
    validation_idx = np.array([i for i, r in enumerate(rows) if r["split"] == "validation"], dtype=int)

    def standardize(array):
        mean = array[train_idx].mean(0); std = array[train_idx].std(0); std[std < 1e-6] = 1
        return (array - mean) / std, mean, std

    visuals, visual_mean, visual_std = standardize(visuals.astype(np.float32))
    layout, layout_mean, layout_std = standardize(layout)

    vectorizer = HashingVectorizer(
        analyzer="char_wb", ngram_range=(3, 5), n_features=2**12,
        alternate_sign=False, norm="l2", lowercase=True,
    )
    text_matrix = vectorizer.transform([r.get("text", "") or "" for r in rows])
    matrix = sparse.hstack([
        text_matrix, sparse.csr_matrix(visuals), sparse.csr_matrix(layout)
    ], format="csr")

    metrics: dict[str, Any] = {}
    models: dict[str, Any] = {}
    errors: dict[str, Any] = {}
    decision_temperature = 1.0
    for target in ("family", "decision"):
        labels = np.array([r.get(target, "") for r in rows], dtype=object)
        usable = np.array([i for i, r in enumerate(rows) if r.get(target)], dtype=int)
        train = np.intersect1d(train_idx, usable)
        validation = np.intersect1d(validation_idx, usable)
        base_model = SGDClassifier(
            loss="log_loss", alpha=1e-5, max_iter=2500, tol=1e-4,
            class_weight="balanced", random_state=42, average=False,
        )
        confidence_temperature = 1.0
        if target == "decision":
            cv = StratifiedKFold(n_splits=3, shuffle=True, random_state=42)
            oof = cross_val_predict(
                base_model, matrix[train], labels[train], cv=cv,
                method="predict_proba", n_jobs=4,
            )
            classes = np.unique(labels[train])
            candidates = (1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0, 15.0, 20.0)
            confidence_temperature = min(
                candidates,
                key=lambda value: log_loss(
                    labels[train], temperature_scale(oof, value), labels=classes
                ),
            )
        model = (
            CalibratedClassifierCV(base_model, method="sigmoid", cv=3, n_jobs=4)
            if target == "family"
            else base_model
        )
        start = time.time(); model.fit(matrix[train], labels[train])
        predictions = model.predict(matrix[validation])
        raw_probabilities = model.predict_proba(matrix[validation])
        probabilities = (
            temperature_scale(raw_probabilities, confidence_temperature)
            if target == "decision"
            else raw_probabilities
        )
        report = classification_report(labels[validation], predictions, output_dict=True, zero_division=0)
        metrics[target] = {
            "accuracy": float(accuracy_score(labels[validation], predictions)),
            "macro_f1": float(report["macro avg"]["f1-score"]),
            "weighted_f1": float(report["weighted avg"]["f1-score"]),
            "train_docs": int(len(train)), "validation_docs": int(len(validation)),
            "train_seconds": time.time() - start, "report": report,
            "log_loss": float(log_loss(labels[validation], probabilities, labels=model.classes_)),
            "confidence_temperature": float(confidence_temperature),
        }
        target_errors = []
        for pos, row_idx in enumerate(validation):
            if predictions[pos] != labels[row_idx]:
                predicted_class = str(predictions[pos])
                class_index = int(np.where(model.classes_ == predicted_class)[0][0])
                target_errors.append({
                    "file": rows[row_idx]["file"], "truth": str(labels[row_idx]),
                    "prediction": predicted_class, "confidence": float(probabilities[pos][class_index]),
                })
        errors[target] = target_errors
        models[target] = model
        if target == "decision":
            decision_temperature = float(confidence_temperature)
        print(target, metrics[target]["accuracy"], metrics[target]["macro_f1"], flush=True)

    bundle = {
        "version": "jin-pdf-fusion-router-v2-cpu",
        "text_n_features": 2**12,
        "visual_shape": [16, 16],
        "layout_cols": LAYOUT_COLS,
        "visual_mean": visual_mean, "visual_std": visual_std,
        "layout_mean": layout_mean, "layout_std": layout_std,
        "models": models, "metrics": metrics,
        "decision_temperature": decision_temperature,
        "notes": "CPU-only actual-PDF hybrid: calibrated family classifier + raw decision classifier; native text + first-page visual + PDF geometry; no filename/subject",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, output, compress=3)
    metrics_path.write_text(json.dumps({"metrics": metrics, "errors": errors}, indent=2, ensure_ascii=False), encoding="utf-8")
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the JIN CPU-only real-PDF multimodal router.")
    parser.add_argument("--corpus-root", required=True, help="Directory with test/ and validation/ real PDFs")
    parser.add_argument("--metadata-dir", required=True, help="Directory containing dataset_manifest.csv and document_type_audit.csv")
    parser.add_argument("--output", default="data/learning/jin-pdf-fusion-router-v2-cpu.joblib")
    parser.add_argument("--metrics", default="data/learning/jin-pdf-fusion-router-v2-cpu-metrics.json")
    parser.add_argument("--profile-cache")
    parser.add_argument("--visual-cache")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    by_sha, by_name, audit = load_metadata(Path(args.metadata_dir))
    if args.profile_cache and args.visual_cache and Path(args.profile_cache).exists() and Path(args.visual_cache).exists():
        rows = [json.loads(line) for line in Path(args.profile_cache).read_text(encoding="utf-8").splitlines() if line.strip()]
        visuals = np.load(args.visual_cache)
    else:
        rows, visuals = extract_profiles(Path(args.corpus_root), args.workers)
        if args.profile_cache:
            Path(args.profile_cache).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        if args.visual_cache:
            np.save(args.visual_cache, visuals)

    missing = attach_labels(rows, by_sha, by_name, audit)
    if missing:
        raise RuntimeError(f"Missing manifest labels for {len(missing)} PDFs; first: {missing[:5]}")
    if len(rows) != len(visuals):
        raise RuntimeError(f"Profile/visual alignment mismatch: {len(rows)} != {len(visuals)}")

    train_count = sum(r["split"] == "test" for r in rows)
    validation_count = sum(r["split"] == "validation" for r in rows)
    if not train_count or not validation_count:
        raise RuntimeError("Both test (training) and validation splits are required")

    metrics = train(rows, visuals, Path(args.output), Path(args.metrics))
    print(json.dumps({
        "train_docs": train_count, "validation_docs": validation_count,
        "family_accuracy": metrics["family"]["accuracy"],
        "decision_accuracy": metrics["decision"]["accuracy"],
        "output": args.output,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

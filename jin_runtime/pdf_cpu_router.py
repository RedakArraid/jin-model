from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import fitz
import joblib
import numpy as np
from PIL import Image
from scipy import sparse
from sklearn.feature_extraction.text import HashingVectorizer

LAYOUT_COLS = [
    "pages", "native_page_ratio", "words", "chars", "numeric_ratio",
    "uppercase_ratio", "mean_word_len", "mean_width", "mean_height",
    *[f"grid_{i}" for i in range(16)],
]


def _extract_from_document(doc: fitz.Document) -> tuple[str, np.ndarray, np.ndarray, dict[str, Any]]:
    if len(doc) < 1:
        raise ValueError("PDF has no pages")

    pages = len(doc)
    total_words = total_chars = native_pages = numeric = upper = 0
    grid = [0] * 16
    widths: list[float] = []
    heights: list[float] = []
    word_lengths: list[int] = []
    texts: list[str] = []

    for page_index, page in enumerate(doc):
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
            word_lengths.append(len(text))
            numeric += int(any(char.isdigit() for char in text))
            upper += int(any(char.isalpha() for char in text) and text.upper() == text)
            cx = (x0 + x1) / 2 / max(rect.width, 1)
            cy = (y0 + y1) / 2 / max(rect.height, 1)
            gx = min(3, max(0, int(cx * 4)))
            gy = min(3, max(0, int(cy * 4)))
            grid[gy * 4 + gx] += 1
        if page_index < 5:
            texts.append(page.get_text("text", sort=True))

    first_page = doc[0]
    pixmap = first_page.get_pixmap(
        matrix=fitz.Matrix(0.25, 0.25),
        colorspace=fitz.csGRAY,
        alpha=False,
    )
    image = Image.frombytes("L", [pixmap.width, pixmap.height], pixmap.samples).resize(
        (16, 16), Image.Resampling.BILINEAR
    )
    visual_image = 1 - np.asarray(image, dtype=np.float32) / 255.0
    visual = np.concatenate(
        [
            visual_image.reshape(-1),
            visual_image.mean(0),
            visual_image.mean(1),
            np.array(
                [
                    visual_image.mean(),
                    visual_image.std(),
                    (visual_image > 0.15).mean(),
                    (visual_image > 0.35).mean(),
                ],
                dtype=np.float32,
            ),
        ]
    ).astype(np.float32)

    grid_total = sum(grid) or 1
    layout = {
        "pages": pages,
        "native_page_ratio": native_pages / max(pages, 1),
        "words": total_words,
        "chars": total_chars,
        "numeric_ratio": numeric / max(total_words, 1),
        "uppercase_ratio": upper / max(total_words, 1),
        "mean_word_len": sum(word_lengths) / max(len(word_lengths), 1),
        "mean_width": sum(widths) / max(len(widths), 1),
        "mean_height": sum(heights) / max(len(heights), 1),
        **{f"grid_{index}": grid[index] / grid_total for index in range(16)},
    }
    layout_vector = np.array([float(layout[column]) for column in LAYOUT_COLS], dtype=np.float32)
    for index, column in enumerate(LAYOUT_COLS):
        if column in {"pages", "words", "chars"}:
            layout_vector[index] = np.log1p(layout_vector[index])
    return "\n".join(texts)[:30000], visual, layout_vector, layout


def extract_pdf_features(path: str | Path) -> tuple[str, np.ndarray, np.ndarray, dict[str, Any]]:
    with fitz.open(Path(path)) as document:
        return _extract_from_document(document)


def extract_pdf_features_bytes(data: bytes) -> tuple[str, np.ndarray, np.ndarray, dict[str, Any]]:
    if not data.startswith(b"%PDF-"):
        raise ValueError("Input is not a PDF binary")
    with fitz.open(stream=data, filetype="pdf") as document:
        return _extract_from_document(document)


class CpuPdfRouter:
    def __init__(self, model_path: str | Path | None = None):
        self.model_path = Path(
            model_path
            or os.getenv(
                "JIN_PDF_ROUTER_MODEL",
                "/app/data/learning/jin-pdf-fusion-router-v3-cpu.joblib",
            )
        )
        self.bundle: dict[str, Any] = {}
        self.vectorizer: HashingVectorizer | None = None
        self.reload()

    @property
    def loaded(self) -> bool:
        return bool(self.bundle and self.vectorizer is not None)

    def reload(self) -> None:
        self.bundle = {}
        self.vectorizer = None
        if not self.model_path.exists():
            return
        try:
            bundle = joblib.load(self.model_path)
            self.bundle = bundle
            self.vectorizer = HashingVectorizer(
                analyzer="char_wb",
                ngram_range=(3, 5),
                n_features=int(bundle["text_n_features"]),
                alternate_sign=False,
                norm="l2",
                lowercase=True,
            )
        except Exception:
            self.bundle = {}
            self.vectorizer = None

    def status(self) -> dict[str, Any]:
        metrics = self.bundle.get("metrics") or {}
        return {
            "loaded": self.loaded,
            "model_path": str(self.model_path),
            "version": self.bundle.get("version"),
            "family_validation_accuracy": (metrics.get("family") or {}).get("accuracy"),
            "decision_validation_accuracy": (metrics.get("decision") or {}).get("accuracy"),
            "decision_with_family_prior_validation_accuracy": (metrics.get("decision_with_family_prior") or {}).get("validation_accuracy"),
            "cpu_only": True,
        }

    def _predict_features(
        self,
        text: str,
        visual: np.ndarray,
        layout_vector: np.ndarray,
        layout_summary: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.loaded or self.vectorizer is None:
            raise RuntimeError(f"CPU PDF router model not loaded: {self.model_path}")

        visual = (visual - self.bundle["visual_mean"]) / self.bundle["visual_std"]
        layout_vector = (layout_vector - self.bundle["layout_mean"]) / self.bundle["layout_std"]
        text_matrix = self.vectorizer.transform([text])
        matrix = sparse.hstack(
            [
                text_matrix,
                sparse.csr_matrix(visual.reshape(1, -1)),
                sparse.csr_matrix(layout_vector.reshape(1, -1)),
            ],
            format="csr",
        )
        output: dict[str, Any] = {
            "model_version": self.bundle["version"],
            "layout_summary": layout_summary,
        }
        predicted_family: str | None = None
        for target, model in self.bundle["models"].items():
            raw_probabilities = model.predict_proba(matrix)[0]
            predicted = str(model.predict(matrix)[0])
            probabilities = raw_probabilities
            temperature = 1.0

            if target == "family":
                predicted_family = predicted

            if target == "decision":
                family_priors = self.bundle.get("family_decision_priors") or {}
                prior_weight = float(self.bundle.get("decision_family_prior_weight", 0.0) or 0.0)
                prior = family_priors.get(predicted_family or "") or {}
                if prior and prior_weight > 0:
                    prior_vector = np.array(
                        [float(prior.get(str(label), 1.0 / len(model.classes_))) for label in model.classes_],
                        dtype=float,
                    )
                    log_scores = np.log(np.clip(raw_probabilities, 1e-12, 1.0))
                    log_scores += prior_weight * np.log(np.clip(prior_vector, 1e-12, 1.0))
                    predicted = str(model.classes_[int(log_scores.argmax())])
                    logits = log_scores - log_scores.max()
                    probabilities = np.exp(logits)
                    probabilities /= probabilities.sum()
                    output["decision_family_prior_weight"] = prior_weight
                    output["decision_family_prior"] = {
                        str(label): float(prior_vector[index])
                        for index, label in enumerate(model.classes_)
                    }

                temperature = float(self.bundle.get("decision_temperature", 1.0) or 1.0)
                if temperature > 0 and temperature != 1.0:
                    clipped = np.clip(probabilities, 1e-12, 1.0)
                    logits = np.log(clipped) / temperature
                    logits -= logits.max()
                    probabilities = np.exp(logits)
                    probabilities /= probabilities.sum()

            class_index = int(np.where(model.classes_ == predicted)[0][0])
            output[target] = predicted
            output[f"{target}_confidence"] = float(probabilities[class_index])
            if target == "decision":
                output["decision_confidence_temperature"] = temperature
        return output

    def predict(self, path: str | Path) -> dict[str, Any]:
        return self._predict_features(*extract_pdf_features(path))

    def predict_bytes(self, data: bytes) -> dict[str, Any]:
        return self._predict_features(*extract_pdf_features_bytes(data))

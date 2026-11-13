from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import joblib
import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer


def _normalize_text(text: str) -> str:
    text = (text or "").casefold()
    text = re.sub(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", " <date> ", text)
    text = re.sub(r"\b\d+[.,]\d{2}\b", " <amount> ", text)
    text = re.sub(r"\b\d{8,}\b", " <longnum> ", text)
    return text


def structural_shape(value: str | None) -> str:
    """Compact identifier shape learned from the corpus."""
    out = []
    for ch in str(value or "").upper():
        kind = "A" if ch.isalpha() else ("9" if ch.isdigit() else (" " if ch.isspace() else ch))
        if not out or out[-1] != kind:
            out.append(kind)
    return "".join(out).strip()


class LearnedFamilyRouter:
    """Advisory sparse layout-family router; deterministic guards remain authoritative."""

    def __init__(self, artifact_path: str | Path, *, shape_profile_path: str | Path | None = None):
        self.artifact_path = Path(artifact_path)
        bundle = joblib.load(self.artifact_path)
        self.classifier = bundle["classifier"]
        self.n_features = int(bundle.get("n_features", 2**18))
        self.ngram_range = tuple(bundle.get("ngram_range", (2, 6)))
        self.vectorizer = HashingVectorizer(
            analyzer="char",
            ngram_range=self.ngram_range,
            n_features=self.n_features,
            alternate_sign=False,
            norm="l2",
        )
        self.version = str(bundle.get("version", "5.0.0"))
        self.shape_profiles = {}
        if shape_profile_path:
            path = Path(shape_profile_path)
            if path.exists():
                try:
                    self.shape_profiles = json.loads(path.read_text(encoding="utf-8")).get("profiles", {})
                except Exception:
                    self.shape_profiles = {}

    @property
    def trainable_parameters(self) -> int:
        return int(self.classifier.coef_.size + self.classifier.intercept_.size)

    def predict(self, text: str, *, top_k: int = 3) -> dict[str, Any]:
        txt = _normalize_text(text)
        if not txt.strip():
            return {"family": None, "confidence": 0.0, "topk": [], "entropy": 1.0, "ood_score": 1.0}
        matrix = self.vectorizer.transform([txt[:240]])
        probabilities = self.classifier.predict_proba(matrix)[0]
        classes = list(self.classifier.classes_)
        order = np.argsort(probabilities)[::-1][:max(1, top_k)]
        top = [
            {"family": str(classes[int(i)]), "confidence": round(float(probabilities[int(i)]), 6)}
            for i in order
        ]
        clipped = np.clip(probabilities, 1e-12, 1.0)
        entropy = float(-(clipped * np.log(clipped)).sum() / max(1e-12, np.log(len(clipped))))
        confidence = float(probabilities[int(order[0])])
        ood = max(0.0, min(1.0, 0.62 * (1.0 - confidence) + 0.38 * entropy))
        return {
            "family": top[0]["family"],
            "confidence": round(confidence, 6),
            "topk": top,
            "entropy": round(entropy, 6),
            "ood_score": round(ood, 6),
            "trainable_parameters": self.trainable_parameters,
        }

    def number_shape_prior(self, family: str | None, value: str | None) -> float:
        if not family or not value:
            return 0.0
        shape = structural_shape(value)
        for item in self.shape_profiles.get(family, []):
            if item.get("shape") == shape:
                return float(item.get("probability", 0.0))
        return 0.0


def load_router(base_dir: str | Path) -> LearnedFamilyRouter | None:
    base = Path(base_dir)
    model = base / "training" / "artifacts" / "document_family_classifier_v5.joblib"
    shapes = base / "training" / "artifacts" / "po_number_shape_profiles_v5.json"
    if not model.exists():
        return None
    try:
        return LearnedFamilyRouter(model, shape_profile_path=shapes)
    except Exception:
        return None

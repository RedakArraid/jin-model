from __future__ import annotations
import os
from pathlib import Path
from typing import Any
try:
    import joblib
    from sklearn.feature_extraction.text import HashingVectorizer
except Exception:
    joblib = HashingVectorizer = None


class CorpusDocumentRouter:
    def __init__(self, model_path: str | Path | None = None):
        self.model_path = Path(
            model_path
            or os.getenv(
                "JIN_CORPUS_ROUTER_MODEL",
                "/app/data/learning/corpus_text_router.joblib",
            )
        )
        self.bundle: dict[str, Any] = {}
        self.reload()

    @property
    def available(self) -> bool:
        return joblib is not None and HashingVectorizer is not None

    def reload(self) -> None:
        self.bundle = {}
        if self.available and self.model_path.exists():
            try:
                self.bundle = joblib.load(self.model_path) or {}
            except Exception:
                self.bundle = {}

    def _vec(self):
        n = int(self.bundle.get("n_features", 2**10))
        return HashingVectorizer(
            analyzer="char_wb",
            ngram_range=(3, 5),
            n_features=n,
            alternate_sign=False,
            norm="l2",
            lowercase=True,
        )

    def status(self) -> dict[str, Any]:
        metrics = self.bundle.get("metrics") or {}
        return {
            "available": self.available,
            "loaded": bool(self.bundle),
            "model_path": str(self.model_path),
            "version": self.bundle.get("version"),
            "family_validation_accuracy": (metrics.get("family") or {}).get("accuracy"),
            "decision_validation_accuracy": (metrics.get("decision") or {}).get("accuracy"),
        }

    def predict(
        self,
        filename: str = "",
        subject: str = "",
        sender: str = "",
        text: str = "",
    ) -> dict[str, Any] | None:
        if not self.bundle:
            return None
        sample = " | ".join(
            [filename or "", subject or "", sender or "", (text or "")[:4000]]
        )
        if not sample.strip(" |"):
            return None

        x = self._vec().transform([sample])
        out: dict[str, Any] = {"model_version": self.bundle.get("version")}
        for model_key, label_key in (
            ("family_model", "family"),
            ("decision_model", "decision"),
        ):
            model = self.bundle.get(model_key)
            if model is None:
                continue
            probs = model.predict_proba(x)[0]
            i = int(probs.argmax())
            out[label_key] = str(model.classes_[i])
            out[label_key + "_confidence"] = float(probs[i])
        return out if len(out) > 1 else None

    def predict_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        doc = payload.get("document") if isinstance(payload.get("document"), dict) else {}
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        filename = str(
            payload.get("filename")
            or payload.get("file_name")
            or doc.get("filename")
            or source.get("filename")
            or ""
        )
        subject = str(
            payload.get("subject")
            or payload.get("email_subject")
            or doc.get("subject")
            or ""
        )
        sender = str(payload.get("sender") or doc.get("sender") or "")
        text = str(
            payload.get("raw_text")
            or payload.get("document_text")
            or payload.get("text")
            or doc.get("text")
            or ""
        )
        return self.predict(filename, subject, sender, text)

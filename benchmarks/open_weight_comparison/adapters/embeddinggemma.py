from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from jin_runtime.weak_field_router import WeakFieldRouter

from ..common import compact_common
from .base import BenchmarkAdapter


ZONE_QUERIES = {
    "SUPPLIER": "supplier vendor company address block",
    "SHIP_TO": "shipping delivery destination address block",
    "BILL_TO": "billing invoice address block",
    "ORDER_METADATA": "purchase order number date customer reference metadata",
    "LINE_ITEMS": "ordered material line item table quantity price reference description",
    "TOTALS": "financial totals net VAT tax gross total amount due",
}


class EmbeddingGemma2ZoneAdapter(BenchmarkAdapter):
    name = "embeddinggemma2_zone"
    track = "zone_semantics"
    model_id = "google/embeddinggemma-2"

    def load(self) -> None:
        from transformers import pipeline

        if self.models_dir is None:
            raise RuntimeError("EmbeddingGemma2 zone benchmark needs --models-dir for the JIN field router.")
        field_model = self.models_dir / "jin-field-weak-router-v2-cpu.joblib"
        self.field_router = WeakFieldRouter(field_model)
        if not self.field_router.loaded:
            raise RuntimeError(f"JIN field router not loaded: {field_model}")
        if self.device == "cpu":
            device_index = -1
        elif self.device == "auto":
            try:
                import torch
                device_index = 0 if torch.cuda.is_available() else -1
            except Exception:
                device_index = -1
        else:
            device_index = 0
        self.embedder = pipeline(
            "feature-extraction",
            model=self.model_id,
            device=device_index,
            trust_remote_code=True,
        )
        self.query_embeddings = {
            label: self._embed(text)
            for label, text in ZONE_QUERIES.items()
        }
        cache_bytes = None
        try:
            from huggingface_hub import scan_cache_dir
            for repo in scan_cache_dir().repos:
                if repo.repo_id == self.model_id:
                    cache_bytes = int(repo.size_on_disk)
                    break
        except Exception:
            cache_bytes = None
        self.load_metadata = {
            "license": "apache-2.0",
            "mode": "hybrid_zone_text_reranker",
            "hf_cache_bytes": cache_bytes,
            "qualification": "Not a standalone extractor: EmbeddingGemma2 reranks JIN-localized zone text.",
        }

    def _embed(self, text: str) -> np.ndarray:
        value = self.embedder(text)
        if isinstance(value, dict):
            value = next(iter(value.values()))
        if isinstance(value, list) and value and isinstance(value[0], dict):
            value = next(iter(value[0].values()))
        array = np.asarray(value, dtype=np.float32)
        while array.ndim > 1:
            array = array.mean(axis=0)
        norm = float(np.linalg.norm(array)) or 1.0
        return array / norm

    def extract(self, pdf_path: Path) -> dict[str, Any]:
        result = self.field_router.predict_bytes(pdf_path.read_bytes())
        zones = []
        for region in result.get("page_regions") or []:
            texts = [
                str(cell.get("text") or "")
                for cell in region.get("cells") or []
                if str(cell.get("text") or "").strip()
            ]
            if not texts:
                continue
            embedding = self._embed(" | ".join(texts))
            scores = {
                label: float(np.dot(embedding, query))
                for label, query in self.query_embeddings.items()
            }
            predicted = max(scores, key=scores.get)
            zones.append({
                "zone_type": predicted,
                "page": int(region.get("page") or 1),
                "bbox": region.get("content_bbox") or region.get("search_bbox"),
                "source_zone_type": region.get("zone_type"),
                "zone_id": region.get("zone_id"),
                "scores": scores,
            })
        return {
            "prediction": compact_common({"spatial": {"zones": zones}}),
            "json_valid": True,
            "raw_output": {"zone_count": len(zones)},
        }

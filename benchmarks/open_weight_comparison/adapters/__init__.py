from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BenchmarkAdapter
from .transformers_vlm import MODEL_SPECS, TransformersVLMAdapter

DEFAULT_MODELS = (
    "jin",
    "granite_docling_258m",
    "glm_ocr",
    "paddleocr_vl_1_6",
    "qwen3_vl_2b",
    "embeddinggemma2_zone",
)


def create_adapter(
    name: str,
    *,
    device: str,
    models_dir: Path | None,
    max_pages: int,
    max_new_tokens: int,
) -> BenchmarkAdapter:
    kwargs: dict[str, Any] = {
        "device": device,
        "models_dir": models_dir,
        "max_pages": max_pages,
        "max_new_tokens": max_new_tokens,
    }
    if name == "jin":
        from .jin import JinAdapter
        return JinAdapter(**kwargs)
    if name == "embeddinggemma2_zone":
        from .embeddinggemma import EmbeddingGemma2ZoneAdapter
        return EmbeddingGemma2ZoneAdapter(**kwargs)
    if name in MODEL_SPECS:
        return TransformersVLMAdapter(name, **kwargs)
    raise KeyError(f"Unknown benchmark model: {name}")


def available_model_names() -> list[str]:
    return ["jin", *MODEL_SPECS.keys(), "embeddinggemma2_zone"]

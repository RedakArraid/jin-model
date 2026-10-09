from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class BenchmarkAdapter(ABC):
    name: str
    track: str = "direct_ie"
    model_id: str | None = None

    def __init__(self, *, device: str = "cpu", models_dir: Path | None = None,
                 max_pages: int = 3, max_new_tokens: int = 2048) -> None:
        self.device = device
        self.models_dir = models_dir
        self.max_pages = max_pages
        self.max_new_tokens = max_new_tokens
        self.load_metadata: dict[str, Any] = {}

    @abstractmethod
    def load(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def extract(self, pdf_path: Path) -> dict[str, Any]:
        raise NotImplementedError

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "track": self.track,
            "model_id": self.model_id,
            "device": self.device,
            **self.load_metadata,
        }

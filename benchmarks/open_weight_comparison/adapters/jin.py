from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from jin_runtime.clean_output import build_clean_output

from ..common import jin_clean_to_common
from .base import BenchmarkAdapter


class JinAdapter(BenchmarkAdapter):
    name = "jin_v5_12"
    track = "direct_ie"
    model_id = "RedakArraid/jin-model"

    def load(self) -> None:
        script = Path(__file__).resolve().parents[3] / "scripts" / "benchmark_cpu.py"
        spec = importlib.util.spec_from_file_location("jin_benchmark_cpu", script)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot import {script}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._extract, runtime = module.create_local_extractor(self.models_dir)
        model_bytes = 0
        if self.models_dir and self.models_dir.exists():
            model_bytes = sum(
                path.stat().st_size
                for path in self.models_dir.rglob("*")
                if path.is_file()
            )
        self.load_metadata = {
            "runtime": runtime,
            "model_artifact_bytes": model_bytes,
        }

    def extract(self, pdf_path: Path) -> dict[str, Any]:
        payload = self._extract(pdf_path)
        clean = build_clean_output(payload, source_filename=pdf_path.name)
        return {
            "prediction": jin_clean_to_common(clean),
            "json_valid": True,
            "raw_output": clean,
        }

"""Local-only inference policy, applied before any document is processed."""
from __future__ import annotations

import os
from typing import Any


def offline_enabled() -> bool:
    return os.getenv("JIN_OFFLINE", "1").strip().lower() not in {"0", "false", "no"}


def configure_core_offline(engine: Any) -> None:
    if engine is None or not offline_enabled():
        return
    for owner in (engine, getattr(engine, "po_engine", None)):
        config = getattr(owner, "config", None)
        if not isinstance(config, dict):
            continue
        config.setdefault("engine", {})["default_backend"] = "local"
        config.setdefault("azure_document_intelligence", {})["enabled"] = False
        config.setdefault("addresses", {}).setdefault("verification", {})["enabled"] = False
        config.setdefault("semantic_model", {})["enabled"] = False
        config.setdefault("multimodal", {})["enabled"] = False

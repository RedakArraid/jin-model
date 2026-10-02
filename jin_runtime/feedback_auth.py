"""Authentication boundary for the write-only feedback endpoint."""
from __future__ import annotations

import os
import secrets
from collections.abc import Mapping


def feedback_ingestion_enabled() -> bool:
    return bool(str(os.getenv("JIN_FEEDBACK_TOKEN") or "").strip())


def feedback_retrain_on_write() -> bool:
    return str(os.getenv("JIN_FEEDBACK_RETRAIN_ON_WRITE") or "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def feedback_authorization_error(headers: Mapping[str, str]) -> tuple[int, str] | None:
    expected = str(os.getenv("JIN_FEEDBACK_TOKEN") or "").strip()
    if not expected:
        return 503, "feedback ingestion is disabled until JIN_FEEDBACK_TOKEN is configured"
    authorization = str(headers.get("authorization") or "").strip()
    scheme, separator, supplied = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not secrets.compare_digest(supplied.strip(), expected)
    ):
        return 401, "invalid feedback credentials"
    return None


__all__ = [
    "feedback_authorization_error",
    "feedback_ingestion_enabled",
    "feedback_retrain_on_write",
]

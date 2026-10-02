"""Shared HTTP boundary for authenticated human feedback ingestion."""
from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from jin_runtime.feedback_auth import (
    feedback_authorization_error,
    feedback_retrain_on_write,
)


def feedback_response(
    request: Request,
    payload: dict[str, Any],
    learner: Any,
) -> JSONResponse:
    authorization_error = feedback_authorization_error(request.headers)
    if authorization_error is not None:
        status_code, detail = authorization_error
        return JSONResponse(
            status_code=status_code,
            content={"accepted": False, "detail": detail},
            headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
        )
    try:
        status = learner.record_feedback(
            payload,
            retrain=feedback_retrain_on_write(),
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            status_code=422,
            content={"accepted": False, "detail": str(exc)},
        )
    return JSONResponse(
        content={
            "accepted": True,
            "duplicate": bool(status.get("feedback_duplicate")),
            "annotation_id": status.get("annotation_id"),
            "learning": status,
        }
    )


__all__ = ["feedback_response"]

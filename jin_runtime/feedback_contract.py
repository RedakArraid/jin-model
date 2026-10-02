"""Validation and canonicalization of human-reviewed learning feedback."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping


FEEDBACK_SCHEMA_VERSION = 1
MAX_FEEDBACK_BYTES = 2_000_000
_ANNOTATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FeedbackValidationError(ValueError):
    """Raised when feedback is not an explicit, traceable human annotation."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _document_sha256(payload: Mapping[str, Any]) -> str:
    extraction = payload.get("extraction")
    extraction = extraction if isinstance(extraction, Mapping) else {}
    document = extraction.get("document")
    document = document if isinstance(document, Mapping) else {}
    value = (
        payload.get("document_sha256")
        or payload.get("pdf_sha256")
        or document.get("sha256")
        or extraction.get("pdf_hash")
        or extraction.get("pdfHash")
    )
    return re.sub(r"^sha256:", "", _text(value).lower())


def _reviewed_at(value: Any) -> str:
    raw = _text(value)
    if not raw:
        raise FeedbackValidationError("validation.reviewed_at is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedbackValidationError(
            "validation.reviewed_at must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FeedbackValidationError(
            "validation.reviewed_at must include a timezone"
        )
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def feedback_digest(payload: Mapping[str, Any]) -> str:
    canonical = copy.deepcopy(dict(payload))
    canonical.pop("recorded_at", None)
    canonical.pop("annotation_digest", None)
    try:
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise FeedbackValidationError("feedback must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_FEEDBACK_BYTES:
        raise FeedbackValidationError(
            f"feedback exceeds the {MAX_FEEDBACK_BYTES}-byte limit"
        )
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def normalize_human_feedback(payload: Any) -> dict[str, Any]:
    """Return an immutable-version annotation or reject unreviewed feedback."""

    if not isinstance(payload, Mapping):
        raise FeedbackValidationError("feedback must be a JSON object")
    event = copy.deepcopy(dict(payload))
    version = event.get("schema_version")
    if isinstance(version, bool) or version != FEEDBACK_SCHEMA_VERSION:
        raise FeedbackValidationError("schema_version=1 is required")

    annotation_id = _text(event.get("annotation_id"))
    if not _ANNOTATION_ID.fullmatch(annotation_id):
        raise FeedbackValidationError(
            "annotation_id must contain 8-128 safe characters"
        )
    extraction = event.get("extraction")
    if not isinstance(extraction, Mapping) or not extraction:
        raise FeedbackValidationError("extraction must be a non-empty object")
    correction_key = next(
        (key for key in ("corrections", "corrected") if key in event),
        None,
    )
    corrections = event.get(correction_key) if correction_key else None
    if not isinstance(corrections, (Mapping, list)) or not corrections:
        raise FeedbackValidationError("corrections/corrected must be non-empty")

    document_sha256 = _document_sha256(event)
    if not _SHA256.fullmatch(document_sha256):
        raise FeedbackValidationError("a valid document SHA-256 is required")

    validation = event.get("validation")
    if not isinstance(validation, Mapping):
        raise FeedbackValidationError("validation is required")
    if _text(validation.get("status")).lower() != "human_validated":
        raise FeedbackValidationError(
            "validation.status must be human_validated"
        )
    reviewed_by = _text(validation.get("reviewed_by"))
    source = _text(validation.get("source"))
    if not reviewed_by or len(reviewed_by) > 160:
        raise FeedbackValidationError("validation.reviewed_by is required")
    if not source or len(source) > 120:
        raise FeedbackValidationError("validation.source is required")

    event["schema_version"] = FEEDBACK_SCHEMA_VERSION
    event["annotation_id"] = annotation_id
    event["document_sha256"] = document_sha256
    event["validation"] = {
        "status": "human_validated",
        "reviewed_by": reviewed_by,
        "reviewed_at": _reviewed_at(validation.get("reviewed_at")),
        "source": source,
    }
    supplied_digest = _text(event.get("annotation_digest"))
    computed_digest = feedback_digest(event)
    if supplied_digest and supplied_digest != computed_digest:
        raise FeedbackValidationError("annotation_digest does not match feedback")
    event["annotation_digest"] = computed_digest
    return event


def is_human_validated_feedback(payload: Any) -> bool:
    try:
        normalize_human_feedback(payload)
    except FeedbackValidationError:
        return False
    return True


__all__ = [
    "FEEDBACK_SCHEMA_VERSION",
    "MAX_FEEDBACK_BYTES",
    "FeedbackValidationError",
    "feedback_digest",
    "is_human_validated_feedback",
    "normalize_human_feedback",
]

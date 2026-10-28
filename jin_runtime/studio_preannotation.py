"""Source-backed JIN Studio preannotations with normalized PDF regions."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Mapping

from jin_runtime.studio_contract import FIELD_LABELS
from training.annotations.bootstrap_from_extractions import annotate_one


MACHINE_PREANNOTATION_REVIEWER = "jin-model-preannotation-v3"
MACHINE_PREANNOTATION_NOTE = (
    "Préannotation géométrique V3 produite par JIN-model depuis le PDF complet. "
    "Chaque champ et chaque cadre doivent être relus."
)
_LINE_COLLECTION_PATH = re.compile(
    r"(?P<prefix>(?:^|\.))(?:lines|line_items)\[(?P<index>\d+)\]"
)


def _compact_line_item_paths(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact observable order-line indexes without altering field values."""

    indexes = sorted(
        {
            int(match.group("index"))
            for field in fields
            if (match := _LINE_COLLECTION_PATH.search(str(field.get("path") or "")))
        }
    )
    replacements = {old: new for new, old in enumerate(indexes)}
    if not replacements:
        return fields
    for field in fields:
        path = str(field.get("path") or "")
        match = _LINE_COLLECTION_PATH.search(path)
        if not match:
            continue
        new_index = replacements[int(match.group("index"))]
        field["path"] = _LINE_COLLECTION_PATH.sub(
            lambda current: f"{current.group('prefix')}line_items[{new_index}]",
            path,
            count=1,
        )
    return fields


def _canonical_path(base: Any) -> str:
    return str(base or "field").strip()[:260] or "field"


def _valid_box(box: Any) -> bool:
    return (
        isinstance(box, list)
        and len(box) == 4
        and all(isinstance(value, (int, float)) for value in box)
        and 0 <= box[0] < box[2] <= 1000
        and 0 <= box[1] < box[3] <= 1000
    )


def draft_fields(annotation: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Convert source-backed regions into the Studio annotation contract."""

    fields: list[dict[str, Any]] = []
    field_indexes: dict[str, int] = {}

    def add_field(field: dict[str, Any]) -> None:
        path = _canonical_path(field.get("path"))
        field["path"] = path
        existing_index = field_indexes.get(path)
        if existing_index is None:
            field_indexes[path] = len(fields)
            fields.append(field)
            return
        if "evidence" not in fields[existing_index] and "evidence" in field:
            fields[existing_index] = field

    document_labels = annotation.get("document_labels") or {}
    document_type = str(document_labels.get("document_type") or "unknown")
    document_field = {
        "path": "document.type",
        "label": "DOCUMENT_TYPE",
        "status": "UNREVIEWED",
        "extracted_value": document_type,
        "corrected_value": None,
        "comment": (
            "Classe préremplie par JIN ; confirmer le libellé visible dans le document."
        ),
    }
    type_evidence = document_labels.get("evidence") or {}
    if _valid_box(type_evidence.get("bbox")) and int(type_evidence.get("page") or 0) > 0:
        document_field["evidence"] = {
            "page": int(type_evidence["page"]),
            "bbox": type_evidence["bbox"],
        }
    add_field(document_field)

    for page in annotation.get("pages") or []:
        page_number = int(page.get("page") or 0)
        for region in page.get("regions") or []:
            label = str(region.get("label") or "").upper()
            if label not in FIELD_LABELS:
                continue
            box = region.get("bbox")
            valid_box = _valid_box(box)
            match_method = str(region.get("match_method") or "text")
            field = {
                "path": _canonical_path(region.get("source_path") or label),
                "label": label,
                "status": (
                    "UNREVIEWED"
                    if valid_box and match_method != "source_bbox_fallback"
                    else "AMBIGUOUS"
                ),
                "extracted_value": region.get("canonical_value", region.get("value")),
                "corrected_value": None,
                "comment": (
                    f"Préannotation JIN à confirmer ({match_method})."
                    if valid_box
                    else "Zone source dégénérée après normalisation ; redessiner la boîte."
                ),
            }
            if valid_box:
                field["evidence"] = {"page": page_number, "bbox": box}
            add_field(field)

    report = annotation.get("generation_report") or {}
    for unresolved in report.get("unresolved") or []:
        label = str(unresolved.get("label") or "").upper()
        if label not in FIELD_LABELS:
            continue
        reason = str(unresolved.get("reason") or "unresolved")
        add_field(
            {
                "path": _canonical_path(unresolved.get("path") or label),
                "label": label,
                "status": "AMBIGUOUS",
                "extracted_value": unresolved.get(
                    "canonical_value", unresolved.get("value")
                ),
                "corrected_value": None,
                "comment": (
                    "Préannotation non localisée automatiquement "
                    f"({reason}) ; tracer la bonne zone, corriger ou marquer absent."
                ),
            }
        )
    return _compact_line_item_paths(fields)


def _document_type(extraction: Mapping[str, Any]) -> str:
    normalized = extraction.get("normalized_output")
    root = normalized if isinstance(normalized, Mapping) else extraction
    document = root.get("document") if isinstance(root.get("document"), Mapping) else {}
    return str(
        document.get("type")
        or document.get("primary_document_type")
        or document.get("detected_document_type")
        or "unknown"
    )


def generate_studio_preannotation(
    pdf_path: str | Path,
    extraction: Mapping[str, Any],
    *,
    max_ambiguity: int = 3,
) -> dict[str, Any]:
    """Locate extracted values in the PDF and emit normalized Studio boxes."""

    annotation, report = annotate_one(
        Path(pdf_path),
        dict(extraction),
        max_ambiguity,
    )
    document_type_evidence = annotation.get("document_type_evidence")
    annotation["document_labels"] = {
        "document_type": _document_type(extraction),
        "requires_review": True,
        **({"evidence": document_type_evidence} if document_type_evidence else {}),
    }
    annotation["generation_report"] = {
        "candidate_fields": report["candidate_fields"],
        "source_candidate_fields": report.get("source_candidate_fields"),
        "semantic_aliases_removed": report.get("semantic_aliases_removed"),
        "matched_fields": report["matched_fields"],
        "ambiguous_fields": report["ambiguous_fields"],
        "unmatched_fields": report["unmatched_fields"],
        "match_methods": report.get("match_methods") or {},
        "unresolved": report["unmatched"],
    }
    fields = draft_fields(annotation)
    framed_count = sum(
        bool(field.get("evidence", {}).get("bbox"))
        for field in fields
        if isinstance(field.get("evidence"), Mapping)
    )
    return {
        "fields": fields,
        "fieldCount": len(fields),
        "framedFieldCount": framed_count,
        "unframedFieldCount": len(fields) - framed_count,
        "report": {
            "sourceCandidateFieldCount": report.get("source_candidate_fields", 0),
            "candidateFieldCount": report["candidate_fields"],
            "localizedFieldCount": report["matched_fields"],
            "ambiguousFieldCount": report["ambiguous_fields"],
            "unmatchedFieldCount": report["unmatched_fields"],
            "matchMethods": report.get("match_methods") or {},
        },
    }


def machine_draft_payload(preannotation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": "DRAFT",
        "reviewed_by": MACHINE_PREANNOTATION_REVIEWER,
        "note": MACHINE_PREANNOTATION_NOTE,
        "field_annotations": list(preannotation.get("fields") or []),
    }


__all__ = [
    "MACHINE_PREANNOTATION_NOTE",
    "MACHINE_PREANNOTATION_REVIEWER",
    "draft_fields",
    "generate_studio_preannotation",
    "machine_draft_payload",
]

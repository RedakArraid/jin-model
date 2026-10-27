"""Reusable, value-free annotation masks for JIN Studio."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import fitz

from jin_runtime.studio_contract import FIELD_LABELS, normalize_studio_annotation, utc_now
from jin_runtime.studio_region_text import StudioRegionError, extract_pdf_region_text


ANNOTATION_MASK_SCHEMA_VERSION = 2


def _is_line_item_field(field: Mapping[str, Any]) -> bool:
    """Return whether a field belongs to the repeatable order-lines table.

    Masks intentionally model the stable header/footer layout only.  Line-item
    rows vary from one order to another and must always be produced by JIN or
    reviewed manually on the target document.
    """

    label = _text(field.get("label")).upper()
    path = _text(field.get("path")).lower()
    return label.startswith("LINE_ITEM_") or any(
        marker in path
        for marker in (".line_items[", ".lines[", "line_items[", "lines[")
    )


class StudioMaskError(ValueError):
    """Raised when an annotation mask cannot be safely created or applied."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def inspect_pdf_layout(pdf_path: str | Path) -> dict[str, Any]:
    """Return a content-free page geometry signature for compatibility checks."""

    try:
        with fitz.open(Path(pdf_path)) as document:
            pages = []
            for index, page in enumerate(document):
                rectangle = page.rect
                width = float(rectangle.width)
                height = float(rectangle.height)
                pages.append(
                    {
                        "page": index + 1,
                        "width": round(width, 3),
                        "height": round(height, 3),
                        "rotation": int(page.rotation or 0),
                        "orientation": "landscape" if width > height else "portrait",
                        "aspectRatio": round(width / max(height, 1.0), 5),
                    }
                )
    except (OSError, RuntimeError, ValueError) as exc:
        raise StudioMaskError("Impossible d'analyser la mise en page du PDF") from exc
    if not pages:
        raise StudioMaskError("Le PDF ne contient aucune page")
    return {"pageCount": len(pages), "pages": pages}


def document_type_from_payload(document: Mapping[str, Any]) -> str:
    """Resolve the reviewed type first, then the machine extraction type."""

    annotation = document.get("latestAnnotation")
    if isinstance(annotation, Mapping):
        for field in annotation.get("field_annotations") or []:
            if not isinstance(field, Mapping) or field.get("label") != "DOCUMENT_TYPE":
                continue
            value = (
                field.get("corrected_value")
                if field.get("status") == "CORRECTED"
                else field.get("extracted_value")
            )
            if _text(value):
                return _text(value)
    extraction = document.get("extraction")
    if isinstance(extraction, Mapping):
        metadata = extraction.get("document")
        if isinstance(metadata, Mapping) and _text(metadata.get("type")):
            return _text(metadata.get("type"))
        if _text(extraction.get("document_type")):
            return _text(extraction.get("document_type"))
    return ""


def build_annotation_mask(
    *,
    mask_id: str,
    name: str,
    version: int,
    source_document_id: str,
    source_filename: str,
    created_by: str,
    document_type: str,
    field_annotations: list[dict[str, Any]],
    pdf_path: str | Path,
) -> dict[str, Any]:
    """Validate and build a mask while deliberately discarding document values."""

    clean_name = _text(name)
    if len(clean_name) < 3 or len(clean_name) > 100:
        raise StudioMaskError("Le nom du masque doit contenir entre 3 et 100 caractères")
    if not isinstance(field_annotations, list) or not field_annotations:
        raise StudioMaskError("Le masque doit contenir au moins un champ annoté")

    layout = inspect_pdf_layout(pdf_path)
    value_free_fields: list[dict[str, Any]] = []
    omitted_line_item_count = 0
    for index, field in enumerate(field_annotations):
        if not isinstance(field, Mapping):
            raise StudioMaskError(f"Le champ {index + 1} du masque est invalide")
        if _is_line_item_field(field):
            omitted_line_item_count += 1
            continue
        label = _text(field.get("label")).upper()
        path = _text(field.get("path"))
        evidence = field.get("evidence")
        if label not in FIELD_LABELS:
            raise StudioMaskError(f"Le label {label or index + 1} n'est pas pris en charge")
        if not path:
            raise StudioMaskError(f"Le champ {index + 1} n'a pas de chemin JSON")
        if not isinstance(evidence, Mapping) or not evidence.get("bbox") or not evidence.get("page"):
            continue
        try:
            page = int(evidence["page"])
        except (TypeError, ValueError) as exc:
            raise StudioMaskError(f"La page du champ {path} est invalide") from exc
        if page > layout["pageCount"]:
            raise StudioMaskError(f"La page {page} du champ {path} n'existe pas dans le PDF")
        value_free_fields.append(
            {
                "path": path,
                "label": label,
                "status": "UNREVIEWED",
                "extracted_value": "",
                "corrected_value": None,
                "evidence": {"page": page, "bbox": list(evidence["bbox"])},
            }
        )
    if not value_free_fields:
        raise StudioMaskError(
            "Aucun champ ne possède de cadre PDF ; tracez au moins une zone avant de créer le masque"
        )

    try:
        normalized = normalize_studio_annotation(
            {"status": "DRAFT", "field_annotations": value_free_fields},
            document_id=source_document_id,
            document_sha256="0" * 64,
            revision=1,
        )
    except ValueError as exc:
        raise StudioMaskError(str(exc)) from exc

    fields = [
        {
            "path": field["path"],
            "label": field["label"],
            "evidence": field["evidence"],
        }
        for field in normalized["field_annotations"]
    ]
    return {
        "schemaVersion": ANNOTATION_MASK_SCHEMA_VERSION,
        "maskId": mask_id,
        "name": clean_name,
        "version": int(version),
        "documentType": _text(document_type),
        "sourceDocumentId": source_document_id,
        "sourceFilename": source_filename,
        "pageCount": layout["pageCount"],
        "pageLayout": layout["pages"],
        "fieldCount": len(fields),
        "scope": "HEADER_AND_FOOTER",
        "omittedLineItemCount": omitted_line_item_count,
        "fields": fields,
        "createdBy": _text(created_by),
        "createdAt": utc_now(),
    }


def apply_annotation_mask(
    mask: Mapping[str, Any],
    *,
    pdf_path: str | Path,
    target_document_type: str = "",
) -> dict[str, Any]:
    """Read every stored region from a new PDF and return an unreviewed proposal."""

    target_layout = inspect_pdf_layout(pdf_path)
    warnings: list[str] = []
    source_type = _text(mask.get("documentType"))
    target_type = _text(target_document_type)
    if source_type and target_type and source_type != target_type:
        warnings.append(
            f"Type différent : masque {source_type}, document cible {target_type}."
        )
    expected_pages = int(mask.get("pageCount") or 0)
    if expected_pages and expected_pages != target_layout["pageCount"]:
        warnings.append(
            f"Nombre de pages différent : masque {expected_pages}, document cible "
            f"{target_layout['pageCount']}."
        )

    source_pages = {
        int(item.get("page") or 0): item
        for item in mask.get("pageLayout") or []
        if isinstance(item, Mapping)
    }
    for target_page in target_layout["pages"]:
        source_page = source_pages.get(target_page["page"])
        if not source_page:
            continue
        ratio_delta = abs(
            float(source_page.get("aspectRatio") or 0) - float(target_page["aspectRatio"])
        )
        if source_page.get("orientation") != target_page["orientation"] or ratio_delta > 0.04:
            warnings.append(
                f"Géométrie différente sur la page {target_page['page']} ; contrôlez les cadres."
            )

    fields: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    excluded_line_items = 0
    for item in mask.get("fields") or []:
        if not isinstance(item, Mapping):
            continue
        # Enforce the V2 scope while applying historical V1 masks as well.
        if _is_line_item_field(item):
            excluded_line_items += 1
            continue
        evidence = item.get("evidence")
        evidence = evidence if isinstance(evidence, Mapping) else {}
        page = int(evidence.get("page") or 0)
        path = _text(item.get("path"))
        if page < 1 or page > target_layout["pageCount"]:
            skipped.append({"path": path, "reason": "page_absente"})
            continue
        try:
            region = extract_pdf_region_text(
                pdf_path,
                page_number=page,
                bbox=evidence.get("bbox"),
            )
        except StudioRegionError as exc:
            skipped.append({"path": path, "reason": str(exc)})
            continue
        fields.append(
            {
                "path": path,
                "label": _text(item.get("label")).upper(),
                "status": "UNREVIEWED",
                "extracted_value": region.get("text") or "",
                "corrected_value": None,
                "evidence": {"page": page, "bbox": list(region["bbox"])},
                "comment": "",
            }
        )

    if skipped:
        warnings.append(f"{len(skipped)} champ(s) n'ont pas pu être appliqués.")
    if excluded_line_items:
        warnings.append(
            f"{excluded_line_items} champ(s) de ligne historiques ont été exclus du masque."
        )
    return {
        "mask": {
            "maskId": mask.get("maskId"),
            "name": mask.get("name"),
            "version": mask.get("version"),
            "documentType": source_type,
        },
        "compatibility": "MATCH" if not warnings else "REVIEW_REQUIRED",
        "warnings": warnings,
        "targetDocumentType": target_type,
        "targetPageCount": target_layout["pageCount"],
        "fields": fields,
        "appliedFieldCount": len(fields),
        "excludedLineItemCount": excluded_line_items,
        "skippedFields": skipped,
    }


__all__ = [
    "ANNOTATION_MASK_SCHEMA_VERSION",
    "StudioMaskError",
    "apply_annotation_mask",
    "build_annotation_mask",
    "document_type_from_payload",
    "inspect_pdf_layout",
]

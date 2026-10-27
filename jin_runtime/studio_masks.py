"""Reusable, value-free annotation masks for JIN Studio."""
from __future__ import annotations

import unicodedata
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Mapping

import fitz

from jin_runtime.studio_contract import FIELD_LABELS, normalize_studio_annotation, utc_now
from jin_runtime.studio_region_text import StudioRegionError, extract_pdf_region_text


ANNOTATION_MASK_SCHEMA_VERSION = 3
MASK_ALIGNMENT_POLICY = "SOURCE_TARGET_TEXT_TRANSLATION_V1"
_MAX_TRANSLATION = 180.0
_ANCHOR_TOLERANCE = 10.0


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


def _anchor_token(value: Any) -> str:
    """Keep stable alphabetic words and discard likely document values."""

    folded = unicodedata.normalize("NFKD", _text(value).casefold())
    token = "".join(character for character in folded if character.isalpha())
    return token if len(token) >= 3 else ""


def _pdf_anchor_words(pdf_path: str | Path) -> dict[int, list[dict[str, Any]]]:
    pages: dict[int, list[dict[str, Any]]] = {}
    with fitz.open(Path(pdf_path)) as document:
        for page_index, page in enumerate(document):
            rectangle = page.rect
            width = max(float(rectangle.width), 1.0)
            height = max(float(rectangle.height), 1.0)
            words: list[dict[str, Any]] = []
            for word in page.get_text("words", sort=True):
                if len(word) < 5:
                    continue
                token = _anchor_token(word[4])
                if not token:
                    continue
                words.append(
                    {
                        "token": token,
                        "x": round(
                            (((float(word[0]) + float(word[2])) / 2) - rectangle.x0)
                            / width
                            * 1000,
                            3,
                        ),
                        "y": round(
                            (((float(word[1]) + float(word[3])) / 2) - rectangle.y0)
                            / height
                            * 1000,
                            3,
                        ),
                    }
                )
            pages[page_index + 1] = words
    return pages


def _page_anchor_matches(
    source_pdf_path: str | Path | None,
    target_pdf_path: str | Path,
) -> dict[int, list[dict[str, float]]]:
    """Match unique static words without persisting their text in the mask."""

    if not source_pdf_path:
        return {}
    try:
        source_pages = _pdf_anchor_words(source_pdf_path)
        target_pages = _pdf_anchor_words(target_pdf_path)
    except (OSError, RuntimeError, ValueError):
        return {}

    matches_by_page: dict[int, list[dict[str, float]]] = {}
    for page_number in sorted(set(source_pages) & set(target_pages)):
        source_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
        target_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for word in source_pages[page_number]:
            source_index[word["token"]].append(word)
        for word in target_pages[page_number]:
            target_index[word["token"]].append(word)
        matches: list[dict[str, float]] = []
        for token in sorted(set(source_index) & set(target_index)):
            # Repeated words are ambiguous and must never drive a translation.
            if len(source_index[token]) != 1 or len(target_index[token]) != 1:
                continue
            source = source_index[token][0]
            target = target_index[token][0]
            matches.append(
                {
                    "x": float(source["x"]),
                    "y": float(source["y"]),
                    "dx": round(float(target["x"]) - float(source["x"]), 3),
                    "dy": round(float(target["y"]) - float(source["y"]), 3),
                }
            )
        matches_by_page[page_number] = matches
    return matches_by_page


def _translation_consensus(matches: list[dict[str, float]]) -> dict[str, Any] | None:
    if len(matches) < 2:
        return None
    center_dx = float(median(item["dx"] for item in matches))
    center_dy = float(median(item["dy"] for item in matches))
    inliers = [
        item
        for item in matches
        if abs(item["dx"] - center_dx) <= _ANCHOR_TOLERANCE
        and abs(item["dy"] - center_dy) <= _ANCHOR_TOLERANCE
    ]
    if len(inliers) < 2 or len(inliers) * 4 < len(matches) * 3:
        return None
    dx = float(median(item["dx"] for item in inliers))
    dy = float(median(item["dy"] for item in inliers))
    spread = max(
        float(median(abs(item["dx"] - dx) for item in inliers)),
        float(median(abs(item["dy"] - dy) for item in inliers)),
    )
    if abs(dx) > _MAX_TRANSLATION or abs(dy) > _MAX_TRANSLATION:
        return None
    return {
        "dx": round(dx, 3),
        "dy": round(dy, 3),
        "anchorCount": len(inliers),
        "confidence": "HIGH" if len(inliers) >= 3 and spread <= 4 else "MEDIUM",
    }


def _field_translation(
    matches: list[dict[str, float]],
    bbox: list[float],
    compartment: str,
) -> dict[str, Any] | None:
    center_x = (bbox[0] + bbox[2]) / 2
    center_y = (bbox[1] + bbox[3]) / 2
    local = [
        item
        for item in matches
        if abs(item["x"] - center_x) <= 300 and abs(item["y"] - center_y) <= 170
    ]
    candidates: list[tuple[str, list[dict[str, float]]]] = [("LOCAL_ANCHORS", local)]
    if compartment == "FOOTER":
        zone = [item for item in matches if item["y"] >= max(450, center_y - 300)]
    else:
        zone = [item for item in matches if item["y"] <= min(650, center_y + 300)]
    candidates.extend((("COMPARTMENT_ANCHORS", zone), ("PAGE_ANCHORS", matches)))
    for mode, candidate_matches in candidates:
        consensus = _translation_consensus(candidate_matches)
        if consensus:
            return {"mode": mode, **consensus}
    return None


def _translated_bbox(bbox: list[float], dx: float, dy: float) -> list[float]:
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    x0 = min(max(bbox[0] + dx, 0.0), 1000.0 - width)
    y0 = min(max(bbox[1] + dy, 0.0), 1000.0 - height)
    return [round(x0, 3), round(y0, 3), round(x0 + width, 3), round(y0 + height, 3)]


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
                **(
                    {"compartment": field["compartment"]}
                    if field.get("compartment")
                    else {}
                ),
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
            **(
                {"compartment": field["compartment"]}
                if field.get("compartment")
                else {}
            ),
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
        "alignmentPolicy": MASK_ALIGNMENT_POLICY,
        "omittedLineItemCount": omitted_line_item_count,
        "fields": fields,
        "createdBy": _text(created_by),
        "createdAt": utc_now(),
    }


def apply_annotation_mask(
    mask: Mapping[str, Any],
    *,
    pdf_path: str | Path,
    source_pdf_path: str | Path | None = None,
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

    anchor_matches = _page_anchor_matches(source_pdf_path, pdf_path)
    field_alignments: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    excluded_line_items = 0
    for item in mask.get("fields") or []:
        if not isinstance(item, Mapping):
            continue
        # Enforce the header/footer scope while applying historical masks as well.
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
            source_bbox = [float(value) for value in evidence.get("bbox") or []]
            if len(source_bbox) != 4:
                raise StudioRegionError("bbox doit contenir quatre coordonnées normalisées")
            compartment = _text(item.get("compartment")).upper() or "HEADER"
            translation = _field_translation(
                anchor_matches.get(page, []),
                source_bbox,
                compartment,
            )
            translated_bbox = (
                _translated_bbox(source_bbox, translation["dx"], translation["dy"])
                if translation
                else source_bbox
            )
            region = extract_pdf_region_text(
                pdf_path,
                page_number=page,
                bbox=translated_bbox,
            )
        except (StudioRegionError, TypeError, ValueError) as exc:
            skipped.append({"path": path, "reason": str(exc)})
            continue
        shifted = bool(
            translation
            and (abs(float(translation["dx"])) >= 0.5 or abs(float(translation["dy"])) >= 0.5)
        )
        field_alignments.append(
            {
                "path": path,
                "page": page,
                "status": "TRANSLATED" if shifted else ("ALIGNED" if translation else "UNAVAILABLE"),
                "mode": translation["mode"] if translation else "EXACT_COORDINATES",
                "dx": translation["dx"] if translation else 0.0,
                "dy": translation["dy"] if translation else 0.0,
                "anchorCount": translation["anchorCount"] if translation else 0,
                "confidence": translation["confidence"] if translation else "NONE",
            }
        )
        fields.append(
            {
                "path": path,
                "label": _text(item.get("label")).upper(),
                **(
                    {"compartment": item["compartment"]}
                    if item.get("compartment")
                    else {}
                ),
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
    translated_count = sum(item["status"] == "TRANSLATED" for item in field_alignments)
    aligned_count = sum(
        item["status"] in {"TRANSLATED", "ALIGNED"} for item in field_alignments
    )
    alignment_status = (
        "TRANSLATED" if translated_count
        else ("ALIGNED" if aligned_count else "UNAVAILABLE")
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
        "alignment": {
            "policy": MASK_ALIGNMENT_POLICY,
            "status": alignment_status,
            "alignedFieldCount": aligned_count,
            "translatedFieldCount": translated_count,
            "unchangedFieldCount": len(field_alignments) - translated_count,
            "fields": field_alignments,
        },
    }


__all__ = [
    "ANNOTATION_MASK_SCHEMA_VERSION",
    "MASK_ALIGNMENT_POLICY",
    "StudioMaskError",
    "apply_annotation_mask",
    "build_annotation_mask",
    "document_type_from_payload",
    "inspect_pdf_layout",
]

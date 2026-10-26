"""Text extraction for a normalized region selected in JIN Studio."""
from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Any, Sequence

import fitz


class StudioRegionError(ValueError):
    """Raised when a PDF region cannot be read safely."""


def normalize_region_bbox(value: Any) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        raise StudioRegionError("bbox doit contenir quatre coordonnées normalisées")
    try:
        box = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise StudioRegionError("bbox doit contenir uniquement des nombres") from exc
    if not all(0 <= item <= 1000 for item in box):
        raise StudioRegionError("bbox doit utiliser l'espace normalisé 0..1000")
    if box[2] <= box[0] or box[3] <= box[1]:
        raise StudioRegionError("bbox doit avoir une surface positive")
    return box


def _clean_text(value: Any) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(value or "").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _ocr_clip(page: fitz.Page, clip: fitz.Rect) -> str:
    """OCR only the selected clip; failure remains a normal empty result."""
    try:
        from PIL import Image
        import pytesseract

        pixmap = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=clip, alpha=False)
        image = Image.open(io.BytesIO(pixmap.tobytes("png")))
        languages = str(os.getenv("JIN_OCR_LANGS") or "fra+eng+deu").strip()
        return _clean_text(pytesseract.image_to_string(image, lang=languages, config="--psm 6"))
    except Exception:
        return ""


def extract_pdf_region_text(
    pdf_path: str | Path,
    *,
    page_number: int,
    bbox: Any,
    allow_ocr: bool = True,
) -> dict[str, Any]:
    """Extract text from a 0..1000 box without changing its coordinates."""
    normalized = normalize_region_bbox(bbox)
    try:
        requested_page = int(page_number)
    except (TypeError, ValueError) as exc:
        raise StudioRegionError("page doit être un entier") from exc
    if requested_page < 1:
        raise StudioRegionError("page doit être supérieure à zéro")

    try:
        with fitz.open(Path(pdf_path)) as document:
            if requested_page > document.page_count:
                raise StudioRegionError("page absente du PDF")
            page = document.load_page(requested_page - 1)
            page_rect = page.rect
            clip = fitz.Rect(
                page_rect.x0 + normalized[0] / 1000 * page_rect.width,
                page_rect.y0 + normalized[1] / 1000 * page_rect.height,
                page_rect.x0 + normalized[2] / 1000 * page_rect.width,
                page_rect.y0 + normalized[3] / 1000 * page_rect.height,
            ) & page_rect
            text = _clean_text(page.get_text("text", clip=clip, sort=True))
            source = "native_pdf"
            if not text and allow_ocr:
                text = _ocr_clip(page, clip)
                source = "tesseract_ocr" if text else "none"
    except StudioRegionError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise StudioRegionError("lecture de la zone PDF impossible") from exc

    return {
        "page": requested_page,
        "bbox": normalized,
        "text": text,
        "source": source,
    }


__all__ = [
    "StudioRegionError",
    "extract_pdf_region_text",
    "normalize_region_bbox",
]

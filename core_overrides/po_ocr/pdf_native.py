from __future__ import annotations

import statistics
import logging
import re
import fitz

from .models import WordToken
from .ocr import words_to_text
from .normalize import ascii_fold


_LOGGER = logging.getLogger(__name__)


def _needs_hybrid_ocr(page: fitz.Page, words: list[WordToken], cfg: dict) -> bool:
    """Recognize raster templates that contribute labels absent from the text layer.

    Inline images are included by get_image_info, unlike get_images. Ordinary
    native tables and small logos keep the inexpensive native-only path.
    """
    if not words or not cfg.get("hybrid_native_ocr_enabled", True):
        return False
    text = ascii_fold(" ".join(w.text for w in words)).lower()
    header_groups = (r"\b(?:article|reference|sku)\b", r"\b(?:designation|description)\b",
                     r"\b(?:quantite|qte|quantity|qty)\b", r"\b(?:prix|price|montant|amount)\b")
    if sum(bool(re.search(pattern, text)) for pattern in header_groups) >= 3:
        return False
    min_area = page.rect.get_area() * float(cfg.get("hybrid_native_min_image_area_ratio", 0.30))
    return any((fitz.Rect(info["bbox"]) & page.rect).get_area() >= min_area
               for info in page.get_image_info())


def _supplement_native_words(page: fitz.Page, page_number: int,
                             native_words: list[WordToken], cfg: dict) -> list[WordToken]:
    from PIL import Image, ImageDraw
    from .ocr import _tesseract_single

    dpi = max(100, min(300, int(cfg.get("hybrid_native_ocr_dpi", 220))))
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    sx, sy = pix.width / page.rect.width, pix.height / page.rect.height
    draw = ImageDraw.Draw(image)
    # Removing the trusted glyphs before OCR prevents OCR from corrupting their
    # digits and gives sparse label detection a much cleaner image.
    for word in native_words:
        rect = fitz.Rect(word.bbox) * page.rotation_matrix
        draw.rectangle((rect.x0 * sx, rect.y0 * sy, rect.x1 * sx, rect.y1 * sy), fill="white")
    candidate = _tesseract_single(image, page_number, {
        "languages": cfg.get("hybrid_native_ocr_languages", ["fra", "eng"]),
        "min_word_confidence": cfg.get("hybrid_native_min_word_confidence", 50),
        "timeout_seconds": cfg.get("hybrid_native_ocr_timeout_seconds", 15),
    }, psm=11)
    result = list(native_words)
    native_rects = [fitz.Rect(word.bbox) for word in native_words]
    for word in candidate.words:
        x0, y0, x1, y1 = word.bbox
        rect = fitz.Rect(x0 / sx, y0 / sy, x1 / sx, y1 / sy) * page.derotation_matrix
        area = max(rect.get_area(), 1e-9)
        if any((rect & original).get_area() / min(area, max(original.get_area(), 1e-9)) > 0.25
               for original in native_rects):
            continue
        result.append(word.model_copy(update={"bbox": tuple(rect), "page": page_number}))
    return result


def extract_native_page(page: fitz.Page, page_number: int, cfg: dict | None = None) -> tuple[str, list[WordToken], float]:
    raw = page.get_text("words")
    words: list[WordToken] = []
    for item in raw:
        x0, y0, x1, y1, text = item[:5]
        text = str(text).strip()
        if text:
            words.append(WordToken(text=text, page=page_number, bbox=(float(x0), float(y0), float(x1), float(y1)), confidence=1.0, source="native_pdf"))
    cfg = cfg or {}
    if _needs_hybrid_ocr(page, words, cfg):
        try:
            words = _supplement_native_words(page, page_number, words, cfg)
        except (RuntimeError, OSError) as exc:
            # A missing executable or bounded OCR timeout must not discard the
            # original native text. The business validator still sees missing fields.
            _LOGGER.warning("Hybrid native OCR failed on page %s: %s", page_number, exc)
    return words_to_text(words), words, statistics.fmean(w.confidence for w in words) if words else 0.0


def is_native_page(page: fitz.Page, cfg: dict) -> bool:
    words = page.get_text("words")
    text = page.get_text("text").strip()
    if len(words) < int(cfg.get("native_text_min_words", 12)):
        return False
    if len(text) < int(cfg.get("native_text_min_chars", 40)):
        return False
    page_area = max(1.0, page.rect.width * page.rect.height)
    word_area = sum(max(0.0, (w[2] - w[0]) * (w[3] - w[1])) for w in words)
    return (word_area / page_area) >= float(cfg.get("native_text_min_coverage", 0.0005))

from __future__ import annotations

import statistics
import logging
import re
import fitz

from .models import WordToken
from .ocr import group_words_into_rows, words_to_text
from .normalize import ascii_fold


_LOGGER = logging.getLogger(__name__)


def _deduplicate_native_words(words: list[WordToken]) -> list[WordToken]:
    """Remove duplicated glyph layers without collapsing real repeated words.

    Some ERP PDFs contain two identical text objects at the same coordinates.  They
    render as a single word, but PyMuPDF returns both objects and downstream row
    parsers see values such as ``337.12 337.12``.  Position is part of the key so a
    legitimate repetition at a different location is preserved.
    """
    unique: list[WordToken] = []
    seen: set[tuple[str, float, float, float, float]] = set()
    for word in words:
        key = (
            word.text,
            round(float(word.bbox[0]), 2),
            round(float(word.bbox[1]), 2),
            round(float(word.bbox[2]), 2),
            round(float(word.bbox[3]), 2),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(word)
    return unique


_NATIVE_AMOUNT_FRAGMENT = re.compile(r"\d[\d.,'\u00a0\u202f]*\Z")


def _stitch_overlapping_native_amounts(words: list[WordToken]) -> list[WordToken]:
    """Rebuild one amount from tightly overlapping PDF text fragments.

    Some print engines emit ``1\u202f16`` and ``162,26`` in intersecting boxes for
    the *single visible* amount ``1 162,26``. Other engines emit ``5``, ``2`` and
    ``227,28`` with the middle digit repeated by the last fragment. Only merge
    a same-baseline cluster that contains a decimal amount and an actual bbox
    overlap with a matching textual suffix/prefix. Adjacent independent numbers
    and numeric identifiers are deliberately left unchanged.
    """
    removed: set[int] = set()
    replacements: dict[int, WordToken] = {}
    for row in group_words_into_rows(words, y_factor=0.30):
        cluster: list[WordToken] = []

        def flush() -> None:
            if len(cluster) < 2 or not any("," in w.text or "." in w.text for w in cluster):
                return
            joined = ""
            saw_overlapping_digits = False
            previous = None
            for word in cluster:
                value = re.sub(r"[\s\u00a0\u202f]", "", word.text)
                if previous is None:
                    joined = value
                else:
                    bbox_overlap = previous.bbox[2] - word.bbox[0]
                    overlap = next((length for length in range(
                        min(len(joined), len(value)), 0, -1
                    ) if joined.endswith(value[:length])), 0)
                    if bbox_overlap > 0.75 and overlap:
                        joined += value[overlap:]
                        saw_overlapping_digits = True
                    else:
                        joined += value
                previous = word
            if not saw_overlapping_digits or not re.fullmatch(r"\d+[,.]\d{2,6}", joined):
                return
            first = cluster[0]
            first_index = id(first)
            replacements[first_index] = first.model_copy(update={
                "text": joined,
                "bbox": (
                    min(w.bbox[0] for w in cluster),
                    min(w.bbox[1] for w in cluster),
                    max(w.bbox[2] for w in cluster),
                    max(w.bbox[3] for w in cluster),
                ),
                "confidence": min(w.confidence for w in cluster) * 0.98,
            })
            removed.update(id(w) for w in cluster[1:])

        for word in row:
            numeric = bool(_NATIVE_AMOUNT_FRAGMENT.fullmatch(word.text))
            if not numeric:
                flush()
                cluster = []
                continue
            if cluster:
                previous = cluster[-1]
                same_baseline = abs(
                    (word.bbox[1] + word.bbox[3]) - (previous.bbox[1] + previous.bbox[3])
                ) <= min(word.bbox[3] - word.bbox[1], previous.bbox[3] - previous.bbox[1]) * 0.30
                if not same_baseline or word.bbox[0] - previous.bbox[2] > 0.75:
                    flush()
                    cluster = []
            cluster.append(word)
        flush()
    return [replacements.get(id(word), word) for word in words if id(word) not in removed]


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
    words = _deduplicate_native_words(words)
    words = _stitch_overlapping_native_amounts(words)
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

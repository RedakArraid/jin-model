from __future__ import annotations

import math
import re
import statistics
import time
from dataclasses import dataclass
from collections import Counter
from PIL import Image
import pytesseract
from pytesseract import Output

from .models import WordToken


@dataclass
class OCRCandidate:
    text: str
    words: list[WordToken]
    confidence: float
    psm: int
    scale: float = 1.0
    quality: float = 0.0
    consensus: float = 0.0


def _available_languages() -> set[str]:
    try:
        return set(pytesseract.get_languages(config=""))
    except Exception:
        return {"eng"}


def resolve_languages(requested: list[str], fallback: list[str]) -> str:
    available = _available_languages()
    selected = [lang for lang in requested if lang in available]
    if not selected:
        selected = [lang for lang in fallback if lang in available]
    if not selected:
        selected = ["eng"] if "eng" in available else sorted(available)[:1]
    return "+".join(selected)


def _custom_config(cfg: dict, psm: int) -> str:
    oem = int(cfg.get("oem", 3))
    preserve = 1 if cfg.get("preserve_interword_spaces", True) else 0
    parts = [f"--oem {oem}", f"--psm {psm}", f"-c preserve_interword_spaces={preserve}"]
    dpi = int(cfg.get("user_defined_dpi", 0) or 0)
    if dpi > 0:
        parts.append(f"-c user_defined_dpi={dpi}")
    parts.append(f"-c tessedit_do_invert={1 if cfg.get('tessedit_do_invert', True) else 0}")
    parts.append(f"-c load_system_dawg={1 if cfg.get('load_system_dawg', True) else 0}")
    parts.append(f"-c load_freq_dawg={1 if cfg.get('load_freq_dawg', True) else 0}")
    whitelist = cfg.get("char_whitelist")
    blacklist = cfg.get("char_blacklist")
    if whitelist:
        parts.append(f"-c tessedit_char_whitelist={whitelist}")
    if blacklist:
        parts.append(f"-c tessedit_char_blacklist={blacklist}")
    return " ".join(parts)


def _is_garbage(text: str, cfg: dict) -> bool:
    if not cfg.get("filter_garbage_tokens", True):
        return False
    if len(text) < int(cfg.get("min_token_length", 1)) or len(text) > int(cfg.get("max_token_length", 120)):
        return True
    # Standalone separators are evidence too (e.g. ``04 - 1234567890``).
    # The ratio test below used to discard every punctuation-only token,
    # including a confidently read hyphen or decimal/currency separator.
    if text in {"-", "/", ".", ":", "#", "%", "€", "$", "£", "¥", "°"}:
        return False
    # Keep punctuation-rich tokens such as e-mails, URLs and PO references, but reject near-random symbol strings.
    alnum = sum(ch.isalnum() for ch in text)
    non_alnum_ratio = 1.0 - (alnum / max(1, len(text)))
    if alnum == 0 and len(text) > 2:
        return True
    return non_alnum_ratio > float(cfg.get("max_non_alnum_ratio", 0.85))


def _scaled_image(image: Image.Image, scale: float) -> Image.Image:
    if abs(scale - 1.0) < 1e-6:
        return image
    return image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.Resampling.LANCZOS)


def _tesseract_single(image: Image.Image, page_number: int, cfg: dict, psm: int, scale: float = 1.0) -> OCRCandidate:
    lang = resolve_languages(cfg.get("languages", ["eng"]), cfg.get("fallback_languages", ["eng"]))
    custom = _custom_config(cfg, psm)
    timeout = int(min(float(cfg.get("timeout_seconds", 120)), float(cfg.get("single_pass_timeout_seconds", cfg.get("timeout_seconds", 120)))))
    work = _scaled_image(image, scale)
    data = pytesseract.image_to_data(work, lang=lang, config=custom, output_type=Output.DICT, timeout=timeout)
    words: list[WordToken] = []
    confs: list[float] = []
    min_conf = float(cfg.get("min_word_confidence", 20))
    for i, raw in enumerate(data.get("text", [])):
        text = (raw or "").strip()
        if not text or _is_garbage(text, cfg):
            continue
        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = 0.0
        if cfg.get("drop_negative_confidence", True) and conf < 0:
            continue
        if conf < min_conf:
            continue
        left = float(data["left"][i]) / scale
        top = float(data["top"][i]) / scale
        width = float(data["width"][i]) / scale
        height = float(data["height"][i]) / scale
        score = max(0.0, min(1.0, conf / 100.0))
        confs.append(score)
        words.append(WordToken(text=text, page=page_number, bbox=(left, top, left + width, top + height), confidence=score, source="ocr"))
    text = words_to_text(words)
    confidence = statistics.fmean(confs) if confs else 0.0
    return OCRCandidate(text=text, words=words, confidence=confidence, psm=psm, scale=scale)


def _token_set(candidate: OCRCandidate) -> set[str]:
    return {re.sub(r"\W+", "", w.text.casefold()) for w in candidate.words if re.sub(r"\W+", "", w.text.casefold())}


def _candidate_consensus(candidate: OCRCandidate, candidates: list[OCRCandidate]) -> float:
    mine = _token_set(candidate)
    if not mine or len(candidates) <= 1:
        return 0.0
    overlaps: list[float] = []
    for other in candidates:
        if other is candidate:
            continue
        theirs = _token_set(other)
        if not theirs:
            continue
        overlaps.append(len(mine & theirs) / max(1, len(mine | theirs)))
    return statistics.fmean(overlaps) if overlaps else 0.0


def _quality(candidate: OCRCandidate, cfg: dict) -> float:
    words = candidate.words
    if not words:
        return 0.0
    word_cap = max(10, int(cfg.get("quality_word_cap", 120)))
    coverage = min(len(words), word_cap) / word_cap
    chars = "".join(w.text for w in words)
    alnum = sum(ch.isalnum() for ch in chars) / max(1, len(chars))
    rows = group_words_into_rows(words)
    line_structure = min(1.0, len(rows) / max(1.0, math.sqrt(len(words))))
    numeric_tokens = sum(any(ch.isdigit() for ch in w.text) for w in words)
    numeric = min(1.0, numeric_tokens / max(1.0, len(words) * 0.30))
    normalized = [re.sub(r"\W+", "", w.text.casefold()) for w in words]
    normalized = [x for x in normalized if x]
    unique_ratio = len(set(normalized)) / max(1, len(normalized))
    repetition_penalty = 0.0
    min_unique = float(cfg.get("quality_min_unique_token_ratio", 0.20))
    if unique_ratio < min_unique:
        repetition_penalty = min(0.20, min_unique - unique_ratio)

    weights = {
        "confidence": float(cfg.get("quality_weight_confidence", 0.42)),
        "coverage": float(cfg.get("quality_weight_coverage", 0.20)),
        "alnum": float(cfg.get("quality_weight_alnum", 0.12)),
        "line": float(cfg.get("quality_weight_line_structure", 0.10)),
        "numeric": float(cfg.get("quality_weight_numeric", 0.06)),
        "consensus": float(cfg.get("quality_weight_consensus", 0.10)),
    }
    total = max(1e-9, sum(weights.values()))
    quality = (
        weights["confidence"] * candidate.confidence
        + weights["coverage"] * coverage
        + weights["alnum"] * alnum
        + weights["line"] * line_structure
        + weights["numeric"] * numeric
        + weights["consensus"] * candidate.consensus
    ) / total
    return max(0.0, min(1.0, quality - repetition_penalty))


def _score_candidates(candidates: list[OCRCandidate], cfg: dict) -> None:
    for c in candidates:
        c.consensus = _candidate_consensus(c, candidates)
    for c in candidates:
        c.quality = _quality(c, cfg)


def tesseract_page_with_diagnostics(image: Image.Image, page_number: int, cfg: dict) -> tuple[str, list[WordToken], float, dict]:
    started = time.monotonic()
    budget = float(cfg.get("page_time_budget_seconds", 90))
    primary_psm = int(cfg.get("psm", 6))
    candidates: list[OCRCandidate] = []
    errors: list[str] = []

    def within_budget() -> bool:
        return (time.monotonic() - started) < budget

    def safe_run(psm: int, scale: float = 1.0):
        if not within_budget():
            return None
        try:
            c = _tesseract_single(image, page_number, cfg, psm, scale=scale)
            candidates.append(c)
            return c
        except Exception as exc:
            errors.append(f"psm={psm},scale={scale}: {type(exc).__name__}: {exc}")
            return None

    first = safe_run(primary_psm)
    if first is None:
        for fallback_psm in [3, 11, 6, 12]:
            if fallback_psm == primary_psm:
                continue
            first = safe_run(fallback_psm)
            if first is not None:
                break
    if first is None:
        diagnostics = {"selected_psm": primary_psm, "selected_scale": 1.0, "selected_quality": 0.0,
                       "selected_consensus": 0.0, "passes": [], "errors": errors, "budget_exhausted": not within_budget()}
        return "", [], 0.0, diagnostics
    _score_candidates(candidates, cfg)

    trigger_conf = float(cfg.get("ensemble_trigger_confidence", 0.90))
    trigger_words = int(cfg.get("ensemble_trigger_min_words", 25))
    trigger_quality = float(cfg.get("ensemble_trigger_min_quality", 0.72))
    need_ensemble = (
        cfg.get("ensemble_enabled", True)
        and (first.confidence < trigger_conf or len(first.words) < trigger_words or first.quality < trigger_quality)
    )

    if need_ensemble:
        psm_candidates = [int(x) for x in cfg.get("psm_candidates", [3, 4, 6, 11, 12])]
        max_passes = max(1, int(cfg.get("max_ensemble_passes", 5)))
        for psm in [x for x in psm_candidates if x != first.psm][: max_passes - 1]:
            if not within_budget():
                break
            safe_run(psm)
        _score_candidates(candidates, cfg)

    best = max(candidates, key=lambda c: c.quality)

    # If every layout pass is weak, retry the best page segmentation at a few scales.
    if cfg.get("multiscale_enabled", True) and best.quality < float(cfg.get("multiscale_trigger_quality", 0.66)):
        scales = [float(x) for x in cfg.get("scale_candidates", [1.0, 1.35, 1.7])]
        max_scales = max(1, int(cfg.get("max_multiscale_passes", 3)))
        for scale in [x for x in scales if abs(x - 1.0) > 1e-6][:max_scales]:
            if not within_budget():
                break
            safe_run(best.psm, scale=scale)
        _score_candidates(candidates, cfg)
        best = max(candidates, key=lambda c: c.quality)

    diagnostics = {
        "selected_psm": best.psm,
        "selected_scale": round(best.scale, 3),
        "selected_quality": round(best.quality, 4),
        "selected_consensus": round(best.consensus, 4),
        "budget_seconds": budget,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "budget_exhausted": not within_budget(),
        "errors": errors,
        "passes": [
            {
                "psm": c.psm,
                "scale": round(c.scale, 3),
                "confidence": round(c.confidence, 4),
                "quality": round(c.quality, 4),
                "consensus": round(c.consensus, 4),
                "words": len(c.words),
            }
            for c in sorted(candidates, key=lambda x: x.quality, reverse=True)
        ],
    }
    return best.text, best.words, best.confidence, diagnostics


def tesseract_page(image: Image.Image, page_number: int, cfg: dict) -> tuple[str, list[WordToken], float]:
    text, words, confidence, _ = tesseract_page_with_diagnostics(image, page_number, cfg)
    return text, words, confidence


def words_to_text(words: list[WordToken]) -> str:
    if not words:
        return ""
    rows = group_words_into_rows(words)
    return "\n".join(" ".join(w.text for w in row) for row in rows)


def recover_underlined_role_labels(page, image: Image.Image, cfg: dict) -> None:
    """Re-read missing postal captions after line removal.

    Underlines can make a short ``A livrer a`` caption disappear entirely while
    the rest of a scan scores highly. A bounded header pass recovers only known
    explicit role-label words, retaining their image coordinates and OCR scores.
    Low-confidence phones can also be re-read with a nearby explicit phone
    label and a high-confidence, single-digit disagreement in the same box.
    """
    from .address_roles import _explicit_role_anchors
    from .normalize import ascii_fold
    from .preprocess import _remove_lines
    import numpy as np

    unit = page.width / 595.0
    current = _explicit_role_anchors([{"words": page.words}], unit)
    roles = {role for role, _ in current}
    low = ascii_fold(page.text).lower()
    missing_delivery = "ship_to" not in roles and "livraison" in low
    missing_billing = "bill_to" not in roles and "factur" in low
    if not (missing_delivery or missing_billing) or not re.search(r"\b(?:commande|purchase\s+order)\b", low):
        return
    header = image.crop((0, 0, image.width, int(image.height * 0.55)))
    cleaned = Image.fromarray(_remove_lines(np.array(header.convert("L")), True, False, 40, 2))
    try:
        read = _tesseract_single(cleaned, page.page, {
            **cfg, "single_pass_timeout_seconds": 15, "timeout_seconds": 15,
        }, psm=11)
    except (RuntimeError, OSError) as exc:
        page.ocr_diagnostics["role_label_recovery"] = {"error": type(exc).__name__}
        return
    anchors = _explicit_role_anchors([{"words": read.words}], unit)
    added = []
    for role, anchor in anchors:
        if role not in {"buyer", "ship_to", "bill_to"} or role in roles:
            continue
        if statistics.fmean(w.confidence for w in anchor["words"]) < 0.65:
            continue
        for word in anchor["words"]:
            if not any(
                word.text == existing.text
                and abs(word.bbox[0] - existing.bbox[0]) < 4 * unit
                and abs(word.bbox[1] - existing.bbox[1]) < 4 * unit
                for existing in page.words
            ):
                page.words.append(word)
        added.append({"role": role, "text": anchor["text"], "bbox": anchor["bbox"]})
        roles.add(role)
    recovered_contacts = _recover_low_confidence_phones(page.words, read.words)
    page.text = words_to_text(page.words)
    page.ocr_diagnostics["role_label_recovery"] = {
        "added": added, "contacts": recovered_contacts, "method": "underlines_removed_header_ocr",
    }


def _recover_low_confidence_phones(words, reread):
    """Reconcile only a labelled French phone at the same source coordinates."""
    from .normalize import ascii_fold

    corrections = []
    for fresh in reread:
        if fresh.confidence < .90 or not re.fullmatch(r"0[1-9]\d{8}", fresh.text):
            continue
        height = max(1, fresh.bbox[3] - fresh.bbox[1])
        labelled = any(
            ascii_fold(label.text).lower().strip(" .:") in {"tel", "telephone", "fax"}
            and 0 <= fresh.bbox[0] - label.bbox[2] <= height * 4
            and abs(fresh.bbox[1] - label.bbox[1]) <= height * .5
            for label in reread
        )
        if not labelled:
            continue
        candidates = [old for old in words if old.source == "ocr" and old.confidence < .6
                      and re.fullmatch(r"0[1-9]\d{8}", old.text)
                      and sum(a != b for a, b in zip(old.text, fresh.text)) == 1
                      and abs(old.bbox[0] - fresh.bbox[0]) <= height * .5
                      and abs(old.bbox[1] - fresh.bbox[1]) <= height * .5]
        if len(candidates) == 1:
            old = candidates[0]
            corrections.append({"source_text": old.text, "reread_text": fresh.text,
                                "page": fresh.page, "bbox": fresh.bbox, "confidence": fresh.confidence})
            old.text, old.bbox, old.confidence = fresh.text, fresh.bbox, fresh.confidence
    return corrections


def group_words_into_rows(words: list[WordToken], y_factor: float = 0.65) -> list[list[WordToken]]:
    sorted_words = sorted(words, key=lambda w: ((w.bbox[1] + w.bbox[3]) / 2, w.bbox[0]))
    rows: list[list[WordToken]] = []
    centers: list[float] = []
    heights: list[float] = []
    for word in sorted_words:
        cy = (word.bbox[1] + word.bbox[3]) / 2
        h = max(1.0, word.bbox[3] - word.bbox[1])
        best_idx = None
        best_dist = None
        for i, center in enumerate(centers):
            # Large logos/drop caps must not bridge otherwise distinct text
            # baselines. Keep normal mixed font sizes and small punctuation,
            # but use the smaller text height for genuinely oversized glyphs.
            row_height = heights[i]
            oversized = max(h, row_height) > 2.0 * min(h, row_height)
            has_text = any(ch.isalnum() for ch in word.text)
            row_has_text = any(any(ch.isalnum() for ch in item.text) for item in rows[i])
            reference_height = min(h, row_height) if oversized and has_text and row_has_text else max(h, row_height)
            tol = reference_height * y_factor
            dist = abs(cy - center)
            if dist <= tol and (best_dist is None or dist < best_dist):
                best_idx, best_dist = i, dist
        if best_idx is None:
            rows.append([word])
            centers.append(cy)
            heights.append(h)
        else:
            rows[best_idx].append(word)
            n = len(rows[best_idx])
            centers[best_idx] = ((centers[best_idx] * (n - 1)) + cy) / n
            heights[best_idx] = max(heights[best_idx], h)
    order = sorted(range(len(rows)), key=lambda i: centers[i])
    return [sorted(rows[i], key=lambda w: w.bbox[0]) for i in order]

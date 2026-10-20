from __future__ import annotations

"""Aggregate material-reference pattern scoring.

The runtime deliberately consumes a statistical profile, not the source master-data
rows.  A score can support an already detected commercial line, but it must never be
used on its own to create a line or to rewrite a value read from the document.
"""

from dataclasses import asdict, dataclass
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import unicodedata


DEFAULT_PROFILE_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "material_reference_profile_v1.json"
)
REFERENCE_FIELDS = (
    ("supplier_material_number", 1.00),
    ("material_number", 0.97),
    ("article_number", 0.94),
    ("product_code", 0.92),
    ("manufacturer_part_number", 0.82),
    ("sku", 0.78),
    # Customer codes are useful evidence, but the supplied master data describes
    # supplier/material families and must not overrule an explicit customer column.
    ("customer_material_number", 0.62),
)
PREFIXED_REFERENCE_RE = re.compile(
    r"(?P<prefix>[A-Z]{1,6})(?:(?:\s*[+:/-]\s*)|\s+|(?=\d))"
    r"(?P<reference>\d[A-Z0-9 ]{3,34})"
)
RESERVED_SOURCE_PREFIXES = {
    "CP", "CMD", "FAX", "FR", "PO", "SIREN", "SIRET", "TEL", "TVA", "VAT"
}
SEVERE_REFERENCE_STATUSES = {"ALPHA_ONLY", "TEXT_CAPTURE"}


@dataclass(frozen=True)
class MaterialReferenceMatch:
    value: str
    normalized: str
    signature: str
    score: float
    supported: bool
    components: dict[str, float]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ResolvedMaterialReference:
    """A scored reference and its optional, explicitly configured source prefix."""

    source_value: str
    reference_value: str
    source_prefix: str | None
    match: MaterialReferenceMatch


def normalize_reference(value: object) -> str:
    """Normalize only for comparison; the extracted source value remains untouched."""
    text = unicodedata.normalize("NFKC", str(value or "")).upper().strip()
    text = text.strip("\"'`()[]{}<>:;,|_")
    # Some suppliers print grouped notation (``8-703-406-153-0``) or append
    # a short source qualifier (``7733701936/LM``). Compact only these tightly
    # constrained forms; the line still keeps the original extracted value.
    qualified = re.fullmatch(
        r"(?P<base>\d+(?:[ .-]+\d+)*)\s*/\s*[A-Z]{1,4}", text
    )
    if qualified:
        text = qualified.group("base")
    if re.fullmatch(r"\d+(?:[ .-]+\d+)+", text):
        compact = re.sub(r"[ .-]+", "", text)
        if 7 <= len(compact) <= 13:
            text = compact
    # OCR/native PDF layers sometimes split one identifier into digit groups.  Only
    # collapse whitespace when every fragment is strictly alphanumeric.
    fragments = text.split()
    if len(fragments) > 1 and all(re.fullmatch(r"[A-Z0-9]+", part) for part in fragments):
        text = "".join(fragments)
    return text


def reference_signature(value: object) -> str:
    """Return a compact run-length signature such as ``D10`` or ``D10-A1-D2``."""
    text = normalize_reference(value)
    if not text:
        return ""

    def kind(char: str) -> str:
        if char.isdigit():
            return "D"
        if "A" <= char <= "Z":
            return "A"
        return "S"

    groups: list[str] = []
    previous = kind(text[0])
    count = 1
    for char in text[1:]:
        current = kind(char)
        if current == previous:
            count += 1
            continue
        groups.append(f"{previous}{count}")
        previous, count = current, 1
    groups.append(f"{previous}{count}")
    return "-".join(groups)


def reference_nature(value: object) -> str:
    """Return the aggregate character class used by the material profile."""
    text = normalize_reference(value)
    if text.isdigit():
        return "digits_only"
    if any(char.isalpha() for char in text):
        return "contains_letter"
    return "other"


def _support(value: int, saturation: int) -> float:
    if value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(max(2, saturation)))


def _configured_profile_path(config: dict | None = None) -> Path:
    configured = (config or {}).get("layout", {}).get("material_reference_profile_path")
    configured = configured or os.environ.get("JIN_MATERIAL_REFERENCE_PROFILE")
    return Path(configured).expanduser() if configured else DEFAULT_PROFILE_PATH


@lru_cache(maxsize=8)
def _load_profile_cached(path: str, size: int, modified_ns: int) -> dict | None:
    del size, modified_ns  # They are cache-key invalidators.
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if payload.get("schema_version") != 1 or not payload.get("row_count"):
        return None
    return payload


def load_material_reference_profile(config: dict | None = None) -> dict | None:
    path = _configured_profile_path(config)
    try:
        stat = path.stat()
    except OSError:
        return None
    return _load_profile_cached(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def score_material_reference(
    value: object,
    profile: dict | None,
    *,
    min_score: float = 0.74,
    allow_short_numeric: bool = False,
) -> MaterialReferenceMatch:
    raw = str(value or "")
    normalized = normalize_reference(raw)
    signature = reference_signature(normalized)
    empty = MaterialReferenceMatch(raw, normalized, signature, 0.0, False, {})
    if not profile or not (4 <= len(normalized) <= 30):
        return empty
    # Separators are not present in this material master.  Keeping them visible here
    # prevents dates, order numbers, prices and telephone numbers from receiving a
    # material-family bonus merely because they have a common digit length.
    if not re.fullmatch(r"[A-Z0-9]+", normalized) or not any(c.isdigit() for c in normalized):
        return empty
    # Short numeric values are intrinsically ambiguous in French orders (postal
    # codes, SIRET establishment suffixes, line numbers and quantities).  The
    # master contains a small minority of them, but their shape alone is not safe
    # enough to influence automatic parser selection.
    minimum_numeric_length = int(profile.get("safety", {}).get("minimum_numeric_length", 7))
    if (
        not allow_short_numeric
        and normalized.isdigit()
        and len(normalized) < minimum_numeric_length
    ):
        return empty

    length_key = str(len(normalized))
    nature = reference_nature(normalized)
    length_count = int(profile.get("length_counts", {}).get(length_key, 0))
    signature_count = int(profile.get("signature_counts", {}).get(signature, 0))
    nature_counts = profile.get("nature_counts") or {}
    nature_counts_by_length = profile.get("nature_counts_by_length") or {}
    # Additive compatibility for profiles generated before the nature
    # distribution was introduced: use existing signature evidence instead of
    # silently lowering every historical score.
    nature_count = int(nature_counts.get(nature, signature_count))
    length_nature_count = int(
        nature_counts_by_length.get(length_key, {}).get(nature, signature_count)
    )
    by_length = profile.get("prefix_counts_by_length", {}).get(length_key, {})
    prefix1_count = int(by_length.get("1", {}).get(normalized[:1], 0))
    prefix2_count = int(by_length.get("2", {}).get(normalized[:2], 0)) if len(normalized) >= 2 else 0
    prefix3_count = int(by_length.get("3", {}).get(normalized[:3], 0)) if len(normalized) >= 3 else 0

    components = {
        "syntax": 1.0,
        "length": _support(length_count, 200),
        "nature": _support(nature_count, 200),
        "length_nature": _support(length_nature_count, 150),
        "signature": _support(signature_count, 100),
        "prefix_1": _support(prefix1_count, 150),
        "prefix_2": _support(prefix2_count, 100),
        "prefix_3": _support(prefix3_count, 30),
    }
    score = (
        0.08 * components["syntax"]
        + 0.14 * components["length"]
        + 0.06 * components["nature"]
        + 0.12 * components["length_nature"]
        + 0.18 * components["signature"]
        + 0.05 * components["prefix_1"]
        + 0.18 * components["prefix_2"]
        + 0.19 * components["prefix_3"]
    )
    score = round(max(0.0, min(1.0, score)), 4)
    return MaterialReferenceMatch(
        value=raw,
        normalized=normalized,
        signature=signature,
        score=score,
        supported=score >= min_score,
        components={name: round(component, 4) for name, component in components.items()},
    )


def _configured_source_prefixes(config: dict | None = None) -> set[str]:
    configured = (config or {}).get("layout", {}).get(
        "material_reference_source_prefixes", ["EL"]
    )
    if isinstance(configured, str):
        configured = [configured]
    return {
        unicodedata.normalize("NFKC", str(prefix)).upper().strip()
        for prefix in configured or []
        if re.fullmatch(r"[A-Za-z]{1,6}", str(prefix).strip())
    }


def _split_prefixed_reference(value: object) -> tuple[str, str] | None:
    text = unicodedata.normalize("NFKC", str(value or "")).upper().strip()
    match = PREFIXED_REFERENCE_RE.fullmatch(text)
    if not match:
        return None
    return match.group("prefix"), match.group("reference")


def _discover_source_prefixes(
    lines, profile: dict | None, config: dict | None = None
) -> set[str]:
    """Confirm an unknown prefix from repeated, distinct commercial references."""
    layout = (config or {}).get("layout", {})
    if not layout.get("material_reference_auto_source_prefixes", True) or not profile:
        return set()
    configured = _configured_source_prefixes(config)
    blocked = RESERVED_SOURCE_PREFIXES | {
        str(value).upper().strip()
        for value in layout.get("material_reference_blocked_source_prefixes", [])
    }
    threshold = float(layout.get("material_reference_auto_prefix_min_score", 0.88))
    minimum_distinct = max(
        2, int(layout.get("material_reference_auto_prefix_min_distinct", 2))
    )
    references_by_prefix: dict[str, set[str]] = {}
    for line in lines or []:
        if not str(getattr(line, "description", "") or "").strip():
            continue
        if not any(
            getattr(line, field, None) is not None
            for field in ("quantity", "unit_price", "line_total")
        ):
            continue
        seen_on_line: set[tuple[str, str]] = set()
        for field, _role_weight in REFERENCE_FIELDS:
            parts = _split_prefixed_reference(getattr(line, field, None))
            if not parts:
                continue
            prefix, candidate = parts
            if prefix in configured or prefix in blocked:
                continue
            match = score_material_reference(
                candidate,
                profile,
                min_score=threshold,
                # Short codes need an explicitly configured prefix; repeated shape
                # evidence alone is insufficient to disambiguate them.
                allow_short_numeric=False,
            )
            key = (prefix, match.normalized)
            if not match.supported or key in seen_on_line:
                continue
            seen_on_line.add(key)
            references_by_prefix.setdefault(prefix, set()).add(match.normalized)
    return {
        prefix
        for prefix, references in references_by_prefix.items()
        if len(references) >= minimum_distinct
    }


def resolve_material_reference(
    value: object,
    profile: dict | None,
    config: dict | None = None,
    *,
    min_score: float = 0.74,
    discovered_prefixes: set[str] | None = None,
) -> ResolvedMaterialReference:
    """Resolve configured client notation such as ``EL 871...`` safely.

    Prefix removal is deliberately opt-in.  The candidate after the prefix must
    start with a digit and must itself match the aggregate material profile.  This
    prevents words such as ``ELECTRODE`` or unconfigured identifiers from being
    reinterpreted as product references.
    """
    raw = str(value or "")
    configured_prefixes = _configured_source_prefixes(config)
    allowed_prefixes = configured_prefixes | set(discovered_prefixes or ())
    prefixed = _split_prefixed_reference(raw)
    if prefixed and prefixed[0] in allowed_prefixes:
        prefix, candidate = prefixed
        layout = (config or {}).get("layout", {})
        if prefix in configured_prefixes:
            prefixed_threshold = float(layout.get(
                "material_reference_prefixed_min_score", 0.82
            ))
        else:
            prefixed_threshold = float(layout.get(
                "material_reference_auto_prefix_min_score", 0.88
            ))
        match = score_material_reference(
            candidate,
            profile,
            min_score=prefixed_threshold,
            # A configured prefix supplies the missing semantic evidence needed
            # to distinguish a short material code from a postal code/quantity.
            allow_short_numeric=prefix in configured_prefixes,
        )
        return ResolvedMaterialReference(
            source_value=raw,
            reference_value=match.normalized,
            source_prefix=prefix,
            match=match,
        )

    direct = score_material_reference(value, profile, min_score=min_score)
    return ResolvedMaterialReference(
        source_value=raw,
        reference_value=raw,
        source_prefix=None,
        match=direct,
    )


def _comparison_key(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).upper()
    return re.sub(r"[^A-Z0-9]+", "", text)


def _looks_like_text_capture(value: object, line) -> bool:
    """Detect a description/full row accidentally stored as an article reference."""
    raw = " ".join(str(value or "").split()).strip()
    if not raw:
        return False
    # A separated source prefix (``EL 871...``) and split numeric groups are
    # legitimate reference typography, not prose.
    if _split_prefixed_reference(raw):
        return False
    parts = raw.split()
    if len(parts) > 1 and all(re.fullmatch(r"[A-Z0-9]+", part, re.I) for part in parts):
        if all(part.isdigit() for part in parts):
            return False
    key = _comparison_key(raw)
    for context in (
        getattr(line, "description", None),
        getattr(line, "short_description", None),
        getattr(line, "long_description", None),
        getattr(line, "raw_text", None),
    ):
        context_key = _comparison_key(context)
        if key and len(key) >= 4 and key == context_key:
            return True
    alpha_words = [
        part for part in parts
        if re.fullmatch(r"[A-Za-zÀ-ÿ]{3,}", part)
    ]
    # A reference field containing several lexical tokens, or a digit followed
    # by a product label, is a parser spill rather than a compact identifier.
    return bool(
        len(parts) >= 2
        and alpha_words
        and (len(alpha_words) >= 2 or any(char.isdigit() for char in raw))
    )


def assess_line_material_reference(
    line,
    profile: dict | None,
    config: dict | None = None,
    *,
    discovered_prefixes: set[str] | None = None,
) -> dict:
    """Assess reference shape without looking up an exact material value.

    The assessment uses only aggregate length, character-signature and prefix
    frequencies.  It deliberately remains separate from OCR/layout confidence.
    """
    layout = (config or {}).get("layout", {})
    threshold = float(layout.get("material_reference_pattern_min_score", 0.74))
    seen: set[str] = set()
    scored: list[tuple[float, ResolvedMaterialReference, str]] = []
    text_captures: list[str] = []
    alpha_only: list[str] = []

    for field, role_weight in REFERENCE_FIELDS:
        value = getattr(line, field, None)
        normalized = normalize_reference(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if _looks_like_text_capture(value, line):
            text_captures.append(field)
            continue
        if normalized.isalpha():
            alpha_only.append(field)
            continue
        resolved = resolve_material_reference(
            value,
            profile,
            config,
            min_score=threshold,
            discovered_prefixes=discovered_prefixes,
        )
        scored.append((resolved.match.score * role_weight, resolved, field))

    if scored:
        effective_score, resolved, field = max(scored, key=lambda item: item[0])
        normalized = resolved.match.normalized
        supported = bool(
            resolved.match.supported and effective_score >= threshold
        )
        dominant_numeric = bool(
            supported and normalized.isdigit() and len(normalized) in {10, 11}
        )
        if dominant_numeric:
            status = "SUPPORTED_DOMINANT_NUMERIC"
        elif supported:
            status = "SUPPORTED_PROFILE"
        else:
            status = "ATYPICAL"
        nature = reference_nature(normalized)
        return {
            "status": status,
            "confidence": round(max(0.0, min(1.0, effective_score)), 4),
            "supported": supported,
            "dominant_numeric": dominant_numeric,
            "nature": nature,
            "length": len(normalized),
            "signature": resolved.match.signature,
            "field": field,
            "source_prefix": resolved.source_prefix,
        }

    if text_captures:
        status = "TEXT_CAPTURE"
        field = text_captures[0]
    elif alpha_only:
        status = "ALPHA_ONLY"
        field = alpha_only[0]
    else:
        status = "MISSING"
        field = None
    return {
        "status": status,
        "confidence": 0.0,
        "supported": False,
        "dominant_numeric": False,
        "nature": "text" if status in SEVERE_REFERENCE_STATUSES else "missing",
        "length": 0,
        "signature": "",
        "field": field,
        "source_prefix": None,
    }


def _leading_reference_candidates(description: object) -> list[tuple[str, str]]:
    """Return compact 10/11-digit candidates from a numeric row prefix."""
    text = " ".join(str(description or "").split()).strip()
    if not text:
        return []
    # Stop before the first lexical token. This keeps recovery in the leading
    # article/line-number columns and ignores digits embedded in product prose.
    prefix_match = re.match(r"^(?P<prefix>[0-9][0-9 .-]*)(?=\s+[A-Za-zÀ-ÿ])", text)
    if not prefix_match:
        return []
    prefix = prefix_match.group("prefix").rstrip()
    groups = list(re.finditer(r"\d+", prefix))
    candidates: list[tuple[str, str]] = []
    for start in range(len(groups)):
        digits = ""
        for end in range(start, len(groups)):
            digits += groups[end].group(0)
            if len(digits) > 11:
                break
            if len(digits) not in {10, 11}:
                continue
            source = prefix[groups[start].start():groups[end].end()].strip()
            candidates.append((source, digits))
    return candidates


def restore_leading_source_material_roles(
    lines,
    config: dict | None = None,
    *,
    explicit_supplier_header: bool = False,
) -> int:
    """Restore the unqualified reference printed before the designation.

    A spatial parser can promote a model token from the designation as the
    supplier material and demote the actual ``Reference - Designation`` value
    to ``customer_material_number``. Without an explicit supplier-reference
    header, restore the leading value only when its aggregate material shape is
    strongly supported and the promoted token is materially weaker.
    """
    if explicit_supplier_header:
        return 0
    layout = (config or {}).get("layout", {})
    if not layout.get("restore_unqualified_leading_material_reference", True):
        return 0
    profile = load_material_reference_profile(config)
    if not profile:
        return 0
    threshold = float(layout.get(
        "restore_unqualified_leading_material_min_score", 0.86
    ))
    minimum_margin = float(layout.get(
        "restore_unqualified_leading_material_min_margin", 0.12
    ))
    repaired = 0
    for line in lines or []:
        raw = " ".join(str(getattr(line, "raw_text", "") or "").split()).strip()
        customer = normalize_reference(
            getattr(line, "customer_material_number", None)
        )
        current = (
            getattr(line, "supplier_material_number", None)
            or getattr(line, "material_number", None)
            or getattr(line, "article_number", None)
        )
        current_normalized = normalize_reference(current)
        if not raw or not customer or not current_normalized:
            continue
        # A visible separator proves that the candidate comes from the leading
        # table cell rather than from an arbitrary number in product prose.
        separator = re.match(
            r"^\s*(?P<reference>\d(?:[\d .]*\d)?)\s+[-\u2013\u2014]\s+",
            raw,
        )
        if not separator:
            continue
        source_value = separator.group("reference").strip()
        leading = score_material_reference(
            source_value, profile, min_score=threshold
        )
        if not leading.supported or leading.normalized != customer:
            continue
        promoted = score_material_reference(current, profile, min_score=threshold)
        if (
            promoted.supported
            and promoted.score >= leading.score - minimum_margin
        ):
            continue
        line.material_number = source_value
        line.article_number = source_value
        line.supplier_material_number = leading.normalized
        line.customer_material_number = None
        warning = "material_reference_restored_from_unqualified_leading_source"
        if warning not in line.warnings:
            line.warnings.append(warning)
        repaired += 1
    return repaired


def recover_material_references_from_descriptions(
    lines, config: dict | None = None
) -> int:
    """Recover a leading grouped supplier reference from a commercial row.

    Recovery is narrow: the row needs quantity and price/amount, all normal
    reference fields must be empty, and a 10/11-digit candidate must strongly
    match the aggregate profile. No exact article lookup is involved.
    """
    layout = (config or {}).get("layout", {})
    if not layout.get("material_reference_description_recovery", True):
        return 0
    profile = load_material_reference_profile(config)
    if not profile:
        return 0
    threshold = float(layout.get(
        "material_reference_description_recovery_min_score", 0.90
    ))
    recovered = 0
    for line in lines or []:
        if any(
            normalize_reference(getattr(line, field, None))
            for field, _weight in REFERENCE_FIELDS
        ):
            continue
        if getattr(line, "quantity", None) is None or not any(
            getattr(line, field, None) is not None
            for field in ("unit_price", "line_total")
        ):
            continue
        scored = []
        for source, candidate in _leading_reference_candidates(
            getattr(line, "description", None) or getattr(line, "raw_text", None)
        ):
            match = score_material_reference(candidate, profile, min_score=threshold)
            if match.supported:
                scored.append(
                    (match.score, len(match.normalized), source, match.normalized)
                )
        if not scored:
            continue
        _score, _length, source, canonical = max(scored)
        line.material_number = source
        line.article_number = source
        line.supplier_material_number = canonical
        line.material_reference_source_prefix = None
        if "material_reference_recovered_from_description" not in line.warnings:
            line.warnings.append("material_reference_recovered_from_description")
        recovered += 1
    return recovered


def calibrate_line_material_confidence(lines, config: dict | None = None) -> dict:
    """Calibrate each line using reference shape as bounded corroboration."""
    layout = (config or {}).get("layout", {})
    enabled = bool(layout.get("material_reference_line_confidence", True))
    profile = load_material_reference_profile(config) if enabled else None
    diagnostics = {
        "enabled": enabled,
        "profile_loaded": profile is not None,
        "profile_id": profile.get("profile_id") if profile else None,
        "checked": 0,
        "supported": 0,
        "status_counts": {},
        "mean_reference_confidence": 0.0,
        "mean_line_confidence_before": 0.0,
        "mean_line_confidence_after": 0.0,
        "severe_lines": [],
        "valid": True,
    }
    if not enabled or not lines or not profile:
        return diagnostics

    discovered_prefixes = _discover_source_prefixes(lines, profile, config)
    blend = max(0.0, min(0.5, float(layout.get(
        "material_reference_line_confidence_weight", 0.28
    ))))
    caps = {
        "TEXT_CAPTURE": float(layout.get("material_reference_text_capture_cap", 0.35)),
        "ALPHA_ONLY": float(layout.get("material_reference_alpha_only_cap", 0.55)),
        "MISSING": float(layout.get("material_reference_missing_cap", 0.72)),
        "ATYPICAL": float(layout.get("material_reference_atypical_cap", 0.82)),
    }
    before_values: list[float] = []
    after_values: list[float] = []
    reference_values: list[float] = []
    status_counts: dict[str, int] = {}
    warning_by_status = {
        "TEXT_CAPTURE": "material_reference_text_capture",
        "ALPHA_ONLY": "material_reference_alpha_only",
        "MISSING": "material_reference_missing",
        "ATYPICAL": "material_reference_shape_atypical",
    }

    for index, line in enumerate(lines or []):
        assessment = assess_line_material_reference(
            line,
            profile,
            config,
            discovered_prefixes=discovered_prefixes,
        )
        status = str(assessment["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
        reference_confidence = float(assessment["confidence"])
        before = max(0.0, min(1.0, float(getattr(line, "confidence", 0.0) or 0.0)))

        complete = sum(
            value not in (None, "")
            for value in (
                getattr(line, "description", None),
                getattr(line, "quantity", None),
                getattr(line, "unit_price", None),
                getattr(line, "line_total", None),
            )
        ) / 4.0
        quantity = getattr(line, "quantity", None)
        unit_price = getattr(line, "unit_price", None)
        line_total = getattr(line, "line_total", None)
        if quantity is not None and unit_price is not None and line_total is not None:
            expected = float(quantity) * float(unit_price)
            tolerance = max(0.04, abs(float(line_total)) * 0.006)
            arithmetic = 1.0 if abs(expected - float(line_total)) <= tolerance else 0.0
        else:
            arithmetic = 0.55
        corroboration = (
            0.55 * reference_confidence
            + 0.25 * arithmetic
            + 0.20 * complete
        )
        calibrated = min(before, (1.0 - blend) * before + blend * corroboration)
        if status in caps:
            calibrated = min(calibrated, max(0.0, min(1.0, caps[status])))

        line.material_reference_confidence = round(reference_confidence, 4)
        line.material_reference_status = status
        line.confidence = round(max(0.0, min(1.0, calibrated)), 4)
        warning = warning_by_status.get(status)
        if warning and warning not in line.warnings:
            line.warnings.append(warning)

        diagnostics["checked"] += status != "MISSING"
        diagnostics["supported"] += bool(assessment["supported"])
        if status in SEVERE_REFERENCE_STATUSES:
            diagnostics["severe_lines"].append(
                str(getattr(line, "line_number", None) or index + 1)
            )
        before_values.append(before)
        after_values.append(line.confidence)
        reference_values.append(reference_confidence)

    diagnostics["status_counts"] = dict(sorted(status_counts.items()))
    diagnostics["mean_reference_confidence"] = round(
        sum(reference_values) / max(1, len(reference_values)), 4
    )
    diagnostics["mean_line_confidence_before"] = round(
        sum(before_values) / max(1, len(before_values)), 4
    )
    diagnostics["mean_line_confidence_after"] = round(
        sum(after_values) / max(1, len(after_values)), 4
    )
    diagnostics["valid"] = not diagnostics["severe_lines"]
    return diagnostics


def line_material_pattern_summary(lines, config: dict | None = None) -> dict:
    layout = (config or {}).get("layout", {})
    enabled = bool(layout.get("material_reference_pattern_scoring", True))
    threshold = float(layout.get("material_reference_pattern_min_score", 0.74))
    profile = load_material_reference_profile(config) if enabled else None
    summary = {
        "enabled": enabled,
        "profile_loaded": profile is not None,
        "profile_id": profile.get("profile_id") if profile else None,
        "eligible_lines": 0,
        "supported_lines": 0,
        "coverage": 0.0,
        "mean_best_score": 0.0,
        "bonus": 0.0,
        "penalty": 0.0,
        "prefixed_supported_lines": 0,
        "source_prefix_counts": {},
        "status_counts": {},
    }
    if not profile or not lines:
        return summary

    discovered_prefixes = _discover_source_prefixes(lines, profile, config)
    best_scores: list[float] = []
    supported_scores: list[float] = []
    prefix_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    assessed = 0
    for line in lines:
        description = str(getattr(line, "description", "") or "").strip()
        commercial = any(
            getattr(line, field, None) is not None
            for field in ("quantity", "unit_price", "line_total")
        )
        # Pattern knowledge is corroborating evidence, never a standalone row detector.
        if len(description) < 2 or not commercial:
            continue
        assessed += 1
        assessment = assess_line_material_reference(
            line,
            profile,
            config,
            discovered_prefixes=discovered_prefixes,
        )
        status = str(assessment["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == "MISSING":
            continue
        best = float(assessment["confidence"])
        best_scores.append(best)
        if assessment["supported"] and best >= threshold:
            supported_scores.append(best)
            if assessment["source_prefix"]:
                prefix = str(assessment["source_prefix"])
                prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1

    eligible = len(best_scores)
    supported = len(supported_scores)
    coverage = supported / eligible if eligible else 0.0
    mean_best = sum(best_scores) / eligible if eligible else 0.0
    mean_supported = sum(supported_scores) / supported if supported else 0.0
    maximum_bonus = float(layout.get("material_reference_pattern_hypothesis_weight", 0.14))
    bonus = maximum_bonus * coverage * mean_supported
    severe = sum(status_counts.get(status, 0) for status in SEVERE_REFERENCE_STATUSES)
    atypical = status_counts.get("ATYPICAL", 0)
    maximum_penalty = float(layout.get(
        "material_reference_pattern_hypothesis_penalty", 0.18
    ))
    penalty = maximum_penalty * (severe + 0.25 * atypical) / max(1, assessed)
    summary.update({
        "eligible_lines": eligible,
        "supported_lines": supported,
        "coverage": round(coverage, 4),
        "mean_best_score": round(mean_best, 4),
        "bonus": round(bonus, 4),
        "penalty": round(penalty, 4),
        "prefixed_supported_lines": sum(prefix_counts.values()),
        "source_prefix_counts": dict(sorted(prefix_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
    })
    return summary


def enrich_supplier_material_roles(lines, config: dict | None = None) -> int:
    """Type existing references and separate configured source prefixes safely."""
    layout = (config or {}).get("layout", {})
    if not layout.get("material_reference_role_enrichment", True):
        return 0
    profile = load_material_reference_profile(config)
    if not profile:
        return 0
    threshold = float(layout.get("material_reference_role_min_score", 0.86))
    discovered_prefixes = _discover_source_prefixes(lines, profile, config)
    changed = 0
    for line in lines or []:
        if not str(getattr(line, "description", "") or "").strip():
            continue
        if not any(getattr(line, field, None) is not None for field in ("quantity", "unit_price", "line_total")):
            continue
        existing_supplier = getattr(line, "supplier_material_number", None)
        if existing_supplier:
            resolved = resolve_material_reference(
                existing_supplier,
                profile,
                config,
                min_score=threshold,
                discovered_prefixes=discovered_prefixes,
            )
            if resolved.source_prefix and resolved.match.supported:
                # Preserve the complete notation read from the source before
                # canonicalising the typed supplier reference.
                if not getattr(line, "material_number", None):
                    line.material_number = str(existing_supplier)
                if str(existing_supplier) != resolved.reference_value:
                    line.supplier_material_number = resolved.reference_value
                    changed += 1
                line.material_reference_source_prefix = resolved.source_prefix
            continue

        candidates: list[tuple[float, ResolvedMaterialReference]] = []
        seen: set[str] = set()
        for field in ("material_number", "article_number", "product_code"):
            value = getattr(line, field, None)
            normalized = normalize_reference(value)
            if not value or not normalized or normalized in seen:
                continue
            seen.add(normalized)
            resolved = resolve_material_reference(
                value,
                profile,
                config,
                min_score=threshold,
                discovered_prefixes=discovered_prefixes,
            )
            if resolved.match.supported:
                candidates.append((resolved.match.score, resolved))
        if not candidates:
            continue
        # Direct values retain their source typography for backward compatibility;
        # prefixed values expose the normalized reference while the generic field
        # continues to hold the complete value printed on the document.
        resolved = max(candidates, key=lambda item: item[0])[1]
        line.supplier_material_number = resolved.reference_value
        if resolved.source_prefix:
            line.material_reference_source_prefix = resolved.source_prefix
        changed += 1
    return changed

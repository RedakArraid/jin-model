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


def normalize_reference(value: object) -> str:
    """Normalize only for comparison; the extracted source value remains untouched."""
    text = unicodedata.normalize("NFKC", str(value or "")).upper().strip()
    text = text.strip("\"'`()[]{}<>:;,|")
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
    if normalized.isdigit() and len(normalized) < minimum_numeric_length:
        return empty

    length_key = str(len(normalized))
    length_count = int(profile.get("length_counts", {}).get(length_key, 0))
    signature_count = int(profile.get("signature_counts", {}).get(signature, 0))
    by_length = profile.get("prefix_counts_by_length", {}).get(length_key, {})
    prefix1_count = int(by_length.get("1", {}).get(normalized[:1], 0))
    prefix2_count = int(by_length.get("2", {}).get(normalized[:2], 0)) if len(normalized) >= 2 else 0
    prefix3_count = int(by_length.get("3", {}).get(normalized[:3], 0)) if len(normalized) >= 3 else 0

    components = {
        "syntax": 1.0,
        "length": _support(length_count, 200),
        "signature": _support(signature_count, 100),
        "prefix_1": _support(prefix1_count, 150),
        "prefix_2": _support(prefix2_count, 100),
        "prefix_3": _support(prefix3_count, 30),
    }
    score = (
        0.10 * components["syntax"]
        + 0.17 * components["length"]
        + 0.23 * components["signature"]
        + 0.07 * components["prefix_1"]
        + 0.22 * components["prefix_2"]
        + 0.21 * components["prefix_3"]
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
    }
    if not profile or not lines:
        return summary

    best_scores: list[float] = []
    supported_scores: list[float] = []
    for line in lines:
        description = str(getattr(line, "description", "") or "").strip()
        commercial = any(
            getattr(line, field, None) is not None
            for field in ("quantity", "unit_price", "line_total")
        )
        # Pattern knowledge is corroborating evidence, never a standalone row detector.
        if len(description) < 2 or not commercial:
            continue
        seen: set[str] = set()
        matches: list[float] = []
        for field, role_weight in REFERENCE_FIELDS:
            value = getattr(line, field, None)
            normalized = normalize_reference(value)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            match = score_material_reference(value, profile, min_score=threshold)
            matches.append(match.score * role_weight)
        if not matches:
            continue
        best = max(matches)
        best_scores.append(best)
        if best >= threshold:
            supported_scores.append(best)

    eligible = len(best_scores)
    supported = len(supported_scores)
    coverage = supported / eligible if eligible else 0.0
    mean_best = sum(best_scores) / eligible if eligible else 0.0
    mean_supported = sum(supported_scores) / supported if supported else 0.0
    maximum_bonus = float(layout.get("material_reference_pattern_hypothesis_weight", 0.14))
    bonus = maximum_bonus * coverage * mean_supported
    summary.update({
        "eligible_lines": eligible,
        "supported_lines": supported,
        "coverage": round(coverage, 4),
        "mean_best_score": round(mean_best, 4),
        "bonus": round(bonus, 4),
    })
    return summary


def enrich_supplier_material_roles(lines, config: dict | None = None) -> int:
    """Type an existing reference conservatively; never change or invent its value."""
    layout = (config or {}).get("layout", {})
    if not layout.get("material_reference_role_enrichment", True):
        return 0
    profile = load_material_reference_profile(config)
    if not profile:
        return 0
    threshold = float(layout.get("material_reference_role_min_score", 0.86))
    changed = 0
    for line in lines or []:
        if getattr(line, "supplier_material_number", None):
            continue
        if not str(getattr(line, "description", "") or "").strip():
            continue
        if not any(getattr(line, field, None) is not None for field in ("quantity", "unit_price", "line_total")):
            continue
        candidates: list[tuple[float, str]] = []
        seen: set[str] = set()
        for field in ("material_number", "article_number", "product_code"):
            value = getattr(line, field, None)
            normalized = normalize_reference(value)
            if not value or not normalized or normalized in seen:
                continue
            seen.add(normalized)
            match = score_material_reference(value, profile, min_score=threshold)
            if match.supported:
                candidates.append((match.score, str(value)))
        if not candidates:
            continue
        # Copy the exact source value into the more specific role.  The generic
        # material/article fields remain intact for backward compatibility.
        line.supplier_material_number = max(candidates, key=lambda item: item[0])[1]
        changed += 1
    return changed

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
        "prefixed_supported_lines": 0,
        "source_prefix_counts": {},
    }
    if not profile or not lines:
        return summary

    discovered_prefixes = _discover_source_prefixes(lines, profile, config)
    best_scores: list[float] = []
    supported_scores: list[float] = []
    prefix_counts: dict[str, int] = {}
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
        matches: list[tuple[float, ResolvedMaterialReference]] = []
        for field, role_weight in REFERENCE_FIELDS:
            value = getattr(line, field, None)
            normalized = normalize_reference(value)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            resolved = resolve_material_reference(
                value,
                profile,
                config,
                min_score=threshold,
                discovered_prefixes=discovered_prefixes,
            )
            matches.append((resolved.match.score * role_weight, resolved))
        if not matches:
            continue
        best, best_resolution = max(matches, key=lambda item: item[0])
        best_scores.append(best)
        if best_resolution.match.supported and best >= threshold:
            supported_scores.append(best)
            if best_resolution.source_prefix:
                prefix = best_resolution.source_prefix
                prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1

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
        "prefixed_supported_lines": sum(prefix_counts.values()),
        "source_prefix_counts": dict(sorted(prefix_counts.items())),
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

"""Conservative source and cross-candidate checks for the customer's PO number.

Agreement between extractors is corroboration, never ground-truth validation.
This module deliberately never changes the extracted identifier.
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any


def normalize_identifier(value: Any) -> str:
    # Preserve leading zeroes, punctuation and O/0 or I/1 distinctions.
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).upper()


def _source_contains(value: str, text: Any) -> bool:
    if not isinstance(text, str) or not value or len(value) > 128:
        return False
    pattern = r"(?<![A-Z0-9])" + r"\s*".join(re.escape(char) for char in value) + r"(?![A-Z0-9])"
    source = unicodedata.normalize("NFKC", text).upper()
    for match in re.finditer(pattern, source):
        # PO-123 must not match a fragment of PO-123/2 or X/PO-123.
        # A sentence-ending dot or parenthesis is still legitimate punctuation.
        if re.search(r"[A-Z0-9][._/\-]+$", source[:match.start()]):
            continue
        if re.match(r"[._/\-]+[A-Z0-9]", source[match.end():]):
            continue
        return True
    return False


def _location_valid(evidence: dict[str, Any]) -> bool:
    page, bbox = evidence.get("page"), evidence.get("bbox")
    return (
        isinstance(page, int) and not isinstance(page, bool) and page > 0
        and isinstance(bbox, (list, tuple)) and len(bbox) == 4
        and all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in bbox)
        and bbox[0] >= 0 and bbox[1] >= 0 and bbox[2] > bbox[0] and bbox[3] > bbox[1]
    )


def _plausible_corroborating_candidate(value: Any) -> bool:
    """Reject dates and prose before comparing independent PO candidates."""
    text = " ".join(str(value or "").split()).strip()
    identifier = normalize_identifier(text)
    if len(identifier) < 5 or sum(char.isdigit() for char in identifier) < 2:
        return False
    if re.fullmatch(
        r"(?:\d{1,2}[/.-]){2}\d{2,4}|\d{4}[-/]\d{1,2}[-/]\d{1,2}",
        text,
    ):
        return False
    folded = "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    ).casefold()
    if re.search(
        r"\b(?:janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|"
        r"octobre|novembre|decembre)\b",
        folded,
    ) and re.search(r"\b(?:le|du|au|reprise|fermeture)\b", folded):
        return False
    if len(re.findall(r"[A-Za-zÀ-ÿ]{2,}", text)) >= 4:
        return False
    return True


def _explicit_geometry_prefix_is_compatible(
    identifier: str,
    candidate: dict[str, Any],
    evidence: dict[str, Any],
) -> bool:
    """Treat a same-page explicit spatial prefix as partial corroboration."""
    candidate_value = normalize_identifier(candidate.get("value"))
    return bool(
        str(candidate.get("source") or "").startswith("geometry_explicit_order_")
        and candidate_value
        and len(candidate_value) >= 5
        and len(candidate_value) < len(identifier)
        and identifier.startswith(candidate_value)
        and candidate.get("page") == evidence.get("page")
    )


def _inline_header_outranks_lower_body_candidate(
    candidate: dict[str, Any], evidence: dict[str, Any]
) -> bool:
    """Ignore a later body-level ``Commande`` reference below the PO header.

    Customer purchase orders sometimes quote their own customer's order in a
    project/line description.  The inline transaction header is the emitting
    customer's PO identifier; a geometry label several rows below on the same
    page is useful metadata, but is not a competing document identifier.
    """
    if str(evidence.get("extraction_method") or "") != "inline_transaction_header":
        return False
    if not str(candidate.get("source") or "").startswith("geometry_explicit_order_"):
        return False
    if candidate.get("page") != evidence.get("page"):
        return False
    candidate_bbox, evidence_bbox = candidate.get("bbox"), evidence.get("bbox")
    return bool(
        isinstance(candidate_bbox, (list, tuple)) and len(candidate_bbox) == 4
        and isinstance(evidence_bbox, (list, tuple)) and len(evidence_bbox) == 4
        and all(isinstance(value, (int, float)) for value in (*candidate_bbox, *evidence_bbox))
        and candidate_bbox[1] > evidence_bbox[3] + 40
    )


def check_order_number(po: dict[str, Any], suggestions: dict[str, Any]) -> dict[str, Any]:
    header = po.get("purchase_order") or po
    field = header.get("number") or header.get("order_number") or {}
    value = field.get("value") if isinstance(field, dict) else field
    identifier = normalize_identifier(value)
    evidence = (field.get("evidence") or {}) if isinstance(field, dict) else {}
    issues: list[str] = []
    corroboration = []
    if not identifier:
        issues.append("ORDER_NUMBER_MISSING")
    else:
        if len(identifier) > 128 or re.fullmatch(r"(?:\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}[-/]\d{2}[-/]\d{2})", identifier):
            issues.append("ORDER_NUMBER_FORMAT_SUSPICIOUS")
        if not (_location_valid(evidence) and _source_contains(identifier, evidence.get("source_text"))):
            issues.append("ORDER_NUMBER_EVIDENCE_MISSING")
        original = header.get("number_original")
        original_value = original.get("value") if isinstance(original, dict) else original
        original_evidence = original.get("evidence") if isinstance(original, dict) else {}
        if not isinstance(original_evidence, dict):
            original_evidence = {}
        original_method = str(
            original_evidence.get("extraction_method")
            or (original.get("extraction_method") if isinstance(original, dict) else "")
            or ""
        )
        strong_original_methods = {
            "explicit_attached_numero_below_order_title",
            "explicit_order_label",
            "explicit_composite_erp_order_id",
            "explicit_n_de_commande",
            "inline_transaction_header",
            "spatial_order_number",
            "native_table_anchor",
            "multiblock_order_number",
        }
        if (
            original_value
            and not (
                isinstance(original, dict)
                and original.get("disqualified_reason")
            )
            and _plausible_corroborating_candidate(original_value)
            and normalize_identifier(original_value) != identifier
            and (not original_method or original_method in strong_original_methods)
        ):
            issues.append("ORDER_NUMBER_REPLACED_DIFFERENT_CORE_VALUE")
        candidates = list((suggestions.get("anchored_field_candidates") or {}).get("order_number") or [])
        anchored = (suggestions.get("anchored_fields") or {}).get("order_number")
        if isinstance(anchored, dict):
            candidates.append(anchored)
        seen = set()
        for candidate in candidates:
            if not isinstance(candidate, dict) or not candidate.get("value"):
                continue
            if not _plausible_corroborating_candidate(candidate["value"]):
                continue
            candidate_value = normalize_identifier(candidate["value"])
            key = (candidate_value, candidate.get("page"))
            if key in seen:
                continue
            seen.add(key)
            agrees = (
                candidate_value == identifier
                or _explicit_geometry_prefix_is_compatible(identifier, candidate, evidence)
            )
            lower_body_reference = _inline_header_outranks_lower_body_candidate(
                candidate, evidence
            )
            detail = {"value": candidate["value"], "page": candidate.get("page"),
                      "bbox": candidate.get("bbox"), "agrees": agrees,
                      "source": candidate.get("source")}
            if lower_body_reference:
                detail["ignored_reason"] = "LOWER_BODY_REFERENCE_BELOW_INLINE_PO_HEADER"
            corroboration.append(detail)
            if not agrees and not lower_body_reference:
                issues.append("ORDER_NUMBER_CANDIDATES_DISAGREE")
        field_warnings = set(field.get("warnings") or []) if isinstance(field, dict) else set()
        field_warnings.discard("promoted_from_explicit_geometry_anchor")
        if isinstance(field, dict) and (
            field_warnings or field.get("validation_status") in {"WARNING", "INVALID"}
        ):
            issues.append("ORDER_NUMBER_CORE_WARNING")
    return {
        "value": value, "source_evidence": evidence,
        "status": "REVIEW_REQUIRED" if issues else "SOURCE_SUPPORTED",
        "issues": list(dict.fromkeys(issues)), "corroborating_candidates": corroboration,
        "qualification": "Source tracing and candidate agreement do not guarantee identifier accuracy; no value is inferred or replaced.",
    }

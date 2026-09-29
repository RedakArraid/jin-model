from __future__ import annotations

import re
from typing import Any

from jin_runtime.geometry_fields import _norm, visual_lines

ROLE_PHRASES = {
    "ship_to": ["ADRESSE DE LIVRAISON", "A LIVRER A"],
    "bill_to": ["ADRESSE DE FACTURATION", "A FACTURER A", "FACTURE A", "FACTUREE A"],
}

LABEL_TO_COMPONENTS = {
    "ADDRESS_HOUSE_NUMBER": ["house_number"],
    "ADDRESS_HOUSE_NUMBER_SUFFIX": ["house_number_suffix"],
    "ADDRESS_STREET_TYPE": ["street_type"],
    "ADDRESS_STREET_NAME": ["street_name"],
    "ADDRESS_POSTAL_CODE": ["postal_code"],
    "ADDRESS_CITY": ["city"],
    "ADDRESS_CEDEX": ["cedex"],
    "ADDRESS_CS": ["cs"],
    "ADDRESS_BP": ["bp", "postal_box"],
    "ADDRESS_TSA": ["tsa"],
}

LABEL_TO_ANCHORED_FIELD = {
    "ORDER_NUMBER": ["order_number"],
    "ORDER_DATE": ["order_date"],
    "TOTAL_NET": ["total_net"],
    "TOTAL_VAT": ["total_vat"],
}

COORDINATE_SYSTEM = {
    "bbox": "normalized_0_1000_top_left",
    "bbox_pdf_tl": "pymupdf_points_top_left",
    "bbox_pdf_bl": "pdf_points_bottom_left",
}


def _union_bbox(tokens: list[dict[str, Any]]) -> list[float]:
    return [
        min(token["bbox"][0] for token in tokens),
        min(token["bbox"][1] for token in tokens),
        max(token["bbox"][2] for token in tokens),
        max(token["bbox"][3] for token in tokens),
    ]


def _normalized_bbox(box: list[float], width: float, height: float) -> list[int]:
    return [
        max(0, min(1000, round(1000 * box[0] / width))),
        max(0, min(1000, round(1000 * box[1] / height))),
        max(0, min(1000, round(1000 * box[2] / width))),
        max(0, min(1000, round(1000 * box[3] / height))),
    ]


def _bottom_left_bbox(box: list[float], height: float) -> list[float]:
    return [box[0], height - box[3], box[2], height - box[1]]


def _evidence(
    value: Any,
    tokens: list[dict[str, Any]],
    width: float,
    height: float,
) -> dict[str, Any]:
    box = _union_bbox(tokens)
    return {
        "value": value,
        "tokens": [token["text"] for token in tokens],
        "bbox": _normalized_bbox(box, width, height),
        "bbox_pdf_tl": box,
        "bbox_pdf_bl": _bottom_left_bbox(box, height),
        "coordinate_space": COORDINATE_SYSTEM,
    }


def _pad_bbox(
    box: list[float],
    padding: float,
    width: float,
    height: float,
) -> list[float]:
    return [
        max(0.0, box[0] - padding),
        max(0.0, box[1] - padding),
        min(width, box[2] + padding),
        min(height, box[3] + padding),
    ]


def _overlap_ratio(box: list[float], evidence_box: list[float]) -> float:
    intersection_width = max(
        0.0,
        min(box[2], evidence_box[2]) - max(box[0], evidence_box[0]),
    )
    intersection_height = max(
        0.0,
        min(box[3], evidence_box[3]) - max(box[1], evidence_box[1]),
    )
    intersection = intersection_width * intersection_height
    area = max(1.0, (box[2] - box[0]) * (box[3] - box[1]))
    return intersection / area


def _phrase_anchor(row: dict[str, Any], phrases: list[str]) -> dict[str, Any] | None:
    text = row["text"].upper()
    for phrase in phrases:
        normalized_phrase = _norm(phrase)
        joined = "".join(_norm(token["text"]) for token in row["tokens"])
        if normalized_phrase not in joined:
            continue
        first = _norm(phrase.split()[0])
        candidates = [
            token for token in row["tokens"] if _norm(token["text"]) == first
        ]
        if candidates:
            token = candidates[0]
            return {
                "x": token["bbox"][0],
                "y": row["cy"],
                "bbox": token["bbox"],
            }
    return None


def _role_anchors(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    anchors: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for role, phrases in ROLE_PHRASES.items():
            anchor = _phrase_anchor(row, phrases)
            if anchor:
                anchors.setdefault(role, []).append(anchor)
    return anchors


def _anchor_bands(
    anchors: dict[str, list[dict[str, Any]]],
    width: float,
) -> list[tuple[float, float, str, dict[str, Any]]]:
    flattened = []
    for role, items in anchors.items():
        for anchor in items:
            flattened.append((anchor["x"], role, anchor))
    flattened.sort(key=lambda item: item[0])

    bands = []
    for index, (x, role, anchor) in enumerate(flattened):
        lower = max(0.0, x - 65.0) if index == 0 else (flattened[index - 1][0] + x) / 2
        upper = (
            min(width, x + 320.0)
            if index == len(flattened) - 1
            else (x + flattened[index + 1][0]) / 2
        )
        bands.append((lower, upper, role, anchor))
    return bands


def _role_search_zone(
    rows: list[dict[str, Any]],
    role: str,
    width: float,
    height: float,
) -> list[float] | None:
    anchors = _role_anchors(rows)
    for lower, upper, candidate_role, anchor in _anchor_bands(anchors, width):
        if candidate_role == role:
            return [
                max(0.0, lower - 30.0),
                max(0.0, anchor["y"] - 8.0),
                min(width, upper + 60.0),
                min(height, anchor["y"] + 135.0),
            ]
    return None


def _token_inside(box: list[float], token: dict[str, Any], margin: float = 10.0) -> bool:
    center_x = (token["bbox"][0] + token["bbox"][2]) / 2
    center_y = (token["bbox"][1] + token["bbox"][3]) / 2
    return (
        box[0] - margin <= center_x <= box[2] + margin
        and box[1] - margin <= center_y <= box[3] + margin
    )


def _match_tokens(tokens: list[dict[str, Any]], value: Any) -> list[list[dict[str, Any]]]:
    raw_value = str(value or "").strip()
    target = _norm(raw_value)
    if not target:
        return []

    matches: list[list[dict[str, Any]]] = []
    for start in range(len(tokens)):
        joined = ""
        for end in range(start, min(len(tokens), start + 8)):
            joined += _norm(tokens[end]["text"])
            if joined == target:
                matches.append(tokens[start : end + 1])
                break
            if len(joined) > len(target) + 3:
                break

    if re.fullmatch(r"\d{5}", raw_value):
        for token in tokens:
            source = token["text"].strip().upper()
            if re.fullmatch(r"(?:F|FR)[- ]?" + re.escape(raw_value), source):
                matches.append([token])

    range_match = re.fullmatch(r"(\d{1,4})-(\d{1,4})", raw_value)
    if range_match:
        for index in range(len(tokens) - 1):
            if (
                tokens[index]["text"].strip().rstrip(",") == range_match.group(1)
                and tokens[index + 1]["text"].strip() == range_match.group(2)
            ):
                matches.append(tokens[index : index + 2])
        for index in range(len(tokens) - 2):
            if (
                tokens[index]["text"].strip() == range_match.group(1)
                and tokens[index + 1]["text"].strip() in {"-", "/"}
                and tokens[index + 2]["text"].strip() == range_match.group(2)
            ):
                matches.append(tokens[index : index + 3])

    return matches


def _component_evidence(
    rows: list[dict[str, Any]],
    candidate: dict[str, Any],
    width: float,
    height: float,
) -> tuple[dict[str, Any], list[float] | None, list[float] | None]:
    role = str(candidate.get("role") or "unknown")
    source_line = candidate.get("source_line")
    search_zone = _role_search_zone(rows, role, width, height)

    allowed_rows = []
    for row in rows:
        if source_line is not None and abs(row["index"] - int(source_line)) > 7:
            continue
        row_tokens = row["tokens"]
        if search_zone:
            row_tokens = [
                token for token in row_tokens if _token_inside(search_zone, token)
            ]
        if row_tokens:
            allowed_rows.append({**row, "tokens": row_tokens})

    if not allowed_rows and source_line is None:
        allowed_rows = rows

    evidence: dict[str, Any] = {}
    boxes: list[List[float]] = []

    for key, value in (candidate.get("components") or {}).items():
        match_value = value
        if key == "cedex":
            if not value:
                continue
            match_value = "CEDEX"
        if match_value in (None, False, ""):
            continue

        best: tuple[float, list[dict[str, Any]]] | None = None
        for row in allowed_rows:
            for match in _match_tokens(row["tokens"], match_value):
                box = _union_bbox(match)
                score = 0.0
                if source_line is not None:
                    score += abs(row["index"] - int(source_line)) * 20.0
                if search_zone:
                    zone_center = (search_zone[0] + search_zone[2]) / 2
                    score += abs((box[0] + box[2]) / 2 - zone_center) * 0.05
                if best is None or score < best[0]:
                    best = (score, match)
        if best:
            item = _evidence(match_value, best[1], width, height)
            evidence[key] = item
            boxes.append(item["bbox_pdf_tl"])

    party_name = candidate.get("party_name")
    if party_name:
        best_party = None
        for row in allowed_rows:
            matches = _match_tokens(row["tokens"], party_name)
            if matches:
                best_party = matches[0]
                break
        if best_party:
            item = _evidence(party_name, best_party, width, height)
            evidence["party_name"] = item
            boxes.append(item["bbox_pdf_tl"])

    region_bbox = None
    if boxes:
        region_bbox = _pad_bbox(
            [
                min(box[0] for box in boxes),
                min(box[1] for box in boxes),
                max(box[2] for box in boxes),
                max(box[3] for box in boxes),
            ],
            3.0,
            width,
            height,
        )

    if search_zone is None and region_bbox is not None:
        search_zone = _pad_bbox(region_bbox, 12.0, width, height)

    return evidence, region_bbox, search_zone


def _field_evidence(
    field: dict[str, Any],
    width: float,
    height: float,
) -> dict[str, Any] | None:
    normalized = field.get("bbox")
    if not normalized:
        return None
    box = [
        normalized[0] * width / 1000,
        normalized[1] * height / 1000,
        normalized[2] * width / 1000,
        normalized[3] * height / 1000,
    ]
    return {
        **field,
        "bbox_pdf_tl": box,
        "bbox_pdf_bl": _bottom_left_bbox(box, height),
        "coordinate_space": COORDINATE_SYSTEM,
    }


def refine_zone_evidence(
    lines: list[list[dict[str, Any]]],
    geometry: dict[str, Any],
    raw_spans: list[dict[str, Any]],
) -> dict[str, Any]:
    tokens = [token for line in lines for token in line]
    if not tokens:
        return {
            "spans": raw_spans,
            "suppressed_spans": [],
            "address_candidates": geometry.get("address_candidates") or [],
            "anchored_fields": geometry.get("anchored_fields") or {},
            "semantic_zones": [],
            "zone_refiner_version": "zone-aware-evidence-v1",
            "coordinate_space": COORDINATE_SYSTEM,
        }

    width = float(tokens[0]["page_width"])
    height = float(tokens[0]["page_height"])
    rows = visual_lines(lines)

    refined_addresses = []
    component_boxes: list[tuple[str, list[float]]] = []
    semantic_zones = []

    for candidate in geometry.get("address_candidates") or []:
        evidence, region_bbox, search_zone = _component_evidence(
            rows, candidate, width, height
        )
        refined = {
            **candidate,
            "component_evidence": evidence,
            "region_bbox": _normalized_bbox(region_bbox, width, height)
            if region_bbox
            else None,
            "region_bbox_pdf_tl": region_bbox,
            "region_bbox_pdf_bl": _bottom_left_bbox(region_bbox, height)
            if region_bbox
            else None,
            "search_zone_bbox": _normalized_bbox(search_zone, width, height)
            if search_zone
            else None,
            "search_zone_bbox_pdf_tl": search_zone,
            "search_zone_bbox_pdf_bl": _bottom_left_bbox(search_zone, height)
            if search_zone
            else None,
            "coordinate_space": COORDINATE_SYSTEM,
        }
        refined_addresses.append(refined)

        for key, item in evidence.items():
            component_boxes.append((key, item["bbox_pdf_tl"]))

        if region_bbox:
            semantic_zones.append(
                {
                    "zone_type": "address",
                    "role": candidate.get("role"),
                    "bbox": _normalized_bbox(region_bbox, width, height),
                    "bbox_pdf_tl": region_bbox,
                    "bbox_pdf_bl": _bottom_left_bbox(region_bbox, height),
                    "coordinate_space": COORDINATE_SYSTEM,
                    "source": "exact_component_union",
                }
            )

    refined_fields = {}
    field_boxes: dict[str, list[float]] = {}
    for key, field in (geometry.get("anchored_fields") or {}).items():
        enriched = _field_evidence(field, width, height)
        refined_fields[key] = enriched or field
        if enriched:
            field_boxes[key] = enriched["bbox_pdf_tl"]
            semantic_zones.append(
                {
                    "zone_type": "field",
                    "field": key,
                    "bbox": enriched["bbox"],
                    "bbox_pdf_tl": enriched["bbox_pdf_tl"],
                    "bbox_pdf_bl": enriched["bbox_pdf_bl"],
                    "coordinate_space": COORDINATE_SYSTEM,
                    "source": "anchored_value",
                }
            )

    kept_spans = []
    suppressed_spans = []
    has_address_geometry = bool(refined_addresses)

    for span in raw_spans:
        label = str(span.get("label") or "")
        span_box_norm = span.get("bbox")
        if not span_box_norm:
            kept_spans.append(span)
            continue
        span_box = [
            span_box_norm[0] * width / 1000,
            span_box_norm[1] * height / 1000,
            span_box_norm[2] * width / 1000,
            span_box_norm[3] * height / 1000,
        ]

        reason = None
        component_keys = LABEL_TO_COMPONENTS.get(label)
        if component_keys:
            compatible = [
                box for key, box in component_boxes if key in component_keys
            ]
            if compatible:
                if max(_overlap_ratio(span_box, box) for box in compatible) < 0.35:
                    reason = "component_evidence_mismatch"
            elif has_address_geometry:
                reason = "component_not_supported_by_geometry"
        else:
            field_keys = LABEL_TO_ANCHORED_FIELD.get(label)
            if field_keys:
                compatible_fields = [
                    field_boxes[key] for key in field_keys if key in field_boxes
                ]
                if compatible_fields and max(
                    _overlap_ratio(span_box, box) for box in compatible_fields
                ) < 0.35:
                    reason = "anchored_field_zone_mismatch"
                elif not compatible_fields and any(
                    key in field_boxes for key in ("offer_number", "offer_date")
                ) and label in {"ORDER_NUMBER", "ORDER_DATE"}:
                    reason = "offer_not_order"

        enriched_span = {
            **span,
            "bbox_pdf_tl": span_box,
            "bbox_pdf_bl": _bottom_left_bbox(span_box, height),
            "coordinate_space": COORDINATE_SYSTEM,
        }
        if reason:
            suppressed_spans.append({**enriched_span, "suppression_reason": reason})
        else:
            kept_spans.append(enriched_span)

    return {
        "spans": kept_spans,
        "suppressed_spans": suppressed_spans,
        "address_candidates": refined_addresses,
        "anchored_fields": refined_fields,
        "semantic_zones": semantic_zones,
        "zone_refiner_version": "zone-aware-evidence-v1",
        "coordinate_space": COORDINATE_SYSTEM,
    }

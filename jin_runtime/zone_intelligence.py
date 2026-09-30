from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any

import fitz


ADDRESS_COMPONENT_KEYS = (
    "building",
    "industrial_zone",
    "house_number",
    "house_number_suffix",
    "street_type",
    "street_name",
    "bp",
    "postal_box",
    "cs",
    "tsa",
    "postal_code",
    "city",
)

LINE_ITEM_WORDS = {
    "ARTICLE",
    "REFERENCE",
    "REF",
    "DESCRIPTION",
    "DESIGNATION",
    "LIBELLE",
    "COMMANDE",
    "QUANTITE",
    "QTE",
    "UNITE",
    "PRIX",
    "PU",
    "MONTANT",
    "TOTAL",
}


def _norm(value: Any) -> str:
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    ).upper()
    return re.sub(r"[^A-Z0-9]+", "", text)


def _area(box: list[float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _union(boxes: list[list[float]]) -> list[float] | None:
    if not boxes:
        return None
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def _center(box: list[float]) -> tuple[float, float]:
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def _contains(container: list[float], box: list[float], margin: float = 1.5) -> bool:
    return (
        container[0] - margin <= box[0]
        and container[1] - margin <= box[1]
        and container[2] + margin >= box[2]
        and container[3] + margin >= box[3]
    )


def _expand(
    box: list[float],
    pad_x: float,
    pad_y: float,
    page_width: float,
    page_height: float,
) -> list[float]:
    return [
        max(0.0, box[0] - pad_x),
        max(0.0, box[1] - pad_y),
        min(page_width, box[2] + pad_x),
        min(page_height, box[3] + pad_y),
    ]


def _normalize_box(
    box: list[float],
    page_width: float,
    page_height: float,
) -> list[int]:
    return [
        max(0, min(1000, round(1000 * box[0] / max(page_width, 1.0)))),
        max(0, min(1000, round(1000 * box[1] / max(page_height, 1.0)))),
        max(0, min(1000, round(1000 * box[2] / max(page_width, 1.0)))),
        max(0, min(1000, round(1000 * box[3] / max(page_height, 1.0)))),
    ]


def _visual_rows(block_lines: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    words = [token for line in block_lines for token in line]
    if not words:
        return []

    heights = sorted(
        max(0.1, token["bbox"][3] - token["bbox"][1]) for token in words
    )
    median_height = heights[len(heights) // 2]
    tolerance = max(2.5, min(5.0, median_height * 0.48))
    rows: list[dict[str, Any]] = []

    for token in sorted(
        words,
        key=lambda item: (
            (item["bbox"][1] + item["bbox"][3]) / 2.0,
            item["bbox"][0],
        ),
    ):
        center_y = (token["bbox"][1] + token["bbox"][3]) / 2.0
        best_index = None
        best_distance = None
        for index, row in enumerate(rows):
            distance = abs(center_y - row["center_y"])
            if distance <= tolerance and (
                best_distance is None or distance < best_distance
            ):
                best_index = index
                best_distance = distance
        if best_index is None:
            rows.append({"center_y": center_y, "tokens": [token]})
        else:
            row = rows[best_index]
            row["tokens"].append(token)
            row["center_y"] = sum(
                (item["bbox"][1] + item["bbox"][3]) / 2.0
                for item in row["tokens"]
            ) / len(row["tokens"])

    output = []
    for index, row in enumerate(sorted(rows, key=lambda item: item["center_y"])):
        tokens = sorted(row["tokens"], key=lambda item: item["bbox"][0])
        output.append(
            {
                "index": index,
                "center_y": row["center_y"],
                "tokens": tokens,
                "text": " ".join(token["text"] for token in tokens),
            }
        )
    return output


def _value_matches(
    rows: list[dict[str, Any]],
    value: str,
    max_tokens: int = 10,
) -> list[dict[str, Any]]:
    target = _norm(value)
    if not target:
        return []

    matches = []
    for row in rows:
        tokens = row["tokens"]
        for start in range(len(tokens)):
            accumulated = ""
            for end in range(start, min(len(tokens), start + max_tokens)):
                accumulated += _norm(tokens[end]["text"])
                if accumulated == target:
                    selected = tokens[start : end + 1]
                    box = _union([token["bbox"] for token in selected])
                    if box:
                        matches.append(
                            {
                                "row": row["index"],
                                "bbox": box,
                                "tokens": selected,
                            }
                        )
                    break
                if len(accumulated) > len(target) + 6:
                    break
    return matches


def _vector_rectangles(page: fitz.Page) -> list[list[float]]:
    rectangles: list[list[float]] = []
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if not rect:
            continue
        if (
            rect.width <= 40
            or rect.height <= 25
            or rect.width >= page.rect.width * 0.98
            or rect.height >= page.rect.height * 0.90
        ):
            continue
        box = [float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)]
        if not any(
            sum(abs(box[index] - existing[index]) for index in range(4)) < 3
            for existing in rectangles
        ):
            rectangles.append(box)
    return rectangles


def _row_bands(
    matches: list[dict[str, Any]],
    page_width: float,
    page_height: float,
) -> list[list[float]]:
    grouped: dict[int, list[list[float]]] = defaultdict(list)
    for match in matches:
        grouped[int(match["row"])].append(match["bbox"])
    return [
        _expand(_union(boxes), 2.0, 2.0, page_width, page_height)
        for _, boxes in sorted(grouped.items())
        if _union(boxes)
    ]


def _smallest_content_frame(
    page: fitz.Page,
    rectangles: list[list[float]],
    content_bbox: list[float],
    content_regions: list[list[float]],
) -> tuple[list[float], str]:
    candidates = []
    for rect in rectangles:
        contained_centers = sum(
            1
            for region in content_regions
            if (
                rect[0] - 2 <= _center(region)[0] <= rect[2] + 2
                and rect[1] - 2 <= _center(region)[1] <= rect[3] + 2
            )
        )
        fraction = contained_centers / max(len(content_regions), 1)
        if _contains(rect, content_bbox, 3.0) or fraction >= 0.80:
            if (
                _area(rect) <= max(_area(content_bbox) * 14.0, 2500.0)
                and rect[2] - rect[0] <= page.rect.width * 0.85
            ):
                candidates.append((_area(rect), rect))

    if candidates:
        return min(candidates, key=lambda item: item[0])[1], "VECTOR_BORDER"

    return (
        _expand(
            content_bbox,
            8.0,
            12.0,
            page.rect.width,
            page.rect.height,
        ),
        "CONTENT_DENSITY",
    )


def _address_region(
    page: fitz.Page,
    rows: list[dict[str, Any]],
    rectangles: list[list[float]],
    candidate: dict[str, Any],
    ordinal: int,
) -> dict[str, Any] | None:
    components = candidate.get("components") or {}
    source_line = candidate.get("source_line")

    anchor_matches = []
    for key in ("street_name", "street_type", "postal_code", "city"):
        value = components.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        matches = _value_matches(rows, value)
        if source_line is not None:
            matches.sort(key=lambda item: abs(item["row"] - int(source_line)))
        if matches:
            anchor_matches.append(matches[0])

    anchor_x = (
        sum(_center(match["bbox"])[0] for match in anchor_matches)
        / len(anchor_matches)
        if anchor_matches
        else None
    )
    anchor_y = (
        sum(_center(match["bbox"])[1] for match in anchor_matches)
        / len(anchor_matches)
        if anchor_matches
        else None
    )

    matched_components = []
    for key in ADDRESS_COMPONENT_KEYS:
        value = components.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        matches = _value_matches(rows, value)
        if not matches:
            continue

        def score(match: dict[str, Any]) -> float:
            value_score = 0.0
            if source_line is not None:
                value_score += abs(match["row"] - int(source_line)) * 30.0
            if anchor_x is not None:
                value_score += abs(_center(match["bbox"])[0] - anchor_x)
            if anchor_y is not None:
                value_score += abs(_center(match["bbox"])[1] - anchor_y) * 1.5
            return value_score

        selected = min(matches, key=score)
        if source_line is not None and abs(selected["row"] - int(source_line)) > 6:
            continue
        if source_line is None and anchor_y is not None:
            if abs(_center(selected["bbox"])[1] - anchor_y) > 120:
                continue
        matched_components.append({"component": key, **selected})

    if not matched_components:
        return None

    content_regions = _row_bands(
        matched_components,
        page.rect.width,
        page.rect.height,
    )
    content_bbox = _union(content_regions)
    if not content_bbox:
        return None

    structural_bbox, boundary_source = _smallest_content_frame(
        page,
        rectangles,
        content_bbox,
        content_regions,
    )

    search_bbox = _expand(
        content_bbox,
        6.0,
        6.0,
        page.rect.width,
        page.rect.height,
    )
    search_bbox = [
        max(search_bbox[0], structural_bbox[0]),
        max(search_bbox[1], structural_bbox[1]),
        min(search_bbox[2], structural_bbox[2]),
        min(search_bbox[3], structural_bbox[3]),
    ]

    role = str(candidate.get("role") or "unknown").upper()
    return {
        "zone_id": f"p1_{role.lower()}_{ordinal:02d}",
        "page": 1,
        "zone_type": role,
        "zone_confidence": float(candidate.get("confidence") or 0.0),
        "boundary_source": boundary_source,
        "structural_bbox": _normalize_box(
            structural_bbox, page.rect.width, page.rect.height
        ),
        "content_bbox": _normalize_box(
            content_bbox, page.rect.width, page.rect.height
        ),
        "content_regions": [
            _normalize_box(region, page.rect.width, page.rect.height)
            for region in content_regions
        ],
        "search_bbox": _normalize_box(
            search_bbox, page.rect.width, page.rect.height
        ),
        "linked_components": [
            item["component"] for item in matched_components
        ],
        "requires_review": True,
    }


def _absolute_normalized_box(
    page: fitz.Page,
    box: list[int] | list[float],
) -> list[float]:
    return [
        float(box[0]) * page.rect.width / 1000.0,
        float(box[1]) * page.rect.height / 1000.0,
        float(box[2]) * page.rect.width / 1000.0,
        float(box[3]) * page.rect.height / 1000.0,
    ]


def _group_field_region(
    page: fitz.Page,
    rectangles: list[list[float]],
    fields: dict[str, Any],
    names: tuple[str, ...],
    zone_type: str,
) -> dict[str, Any] | None:
    selected = []
    for name in names:
        field = fields.get(name)
        if not isinstance(field, dict) or not field.get("bbox"):
            continue
        selected.append((name, _absolute_normalized_box(page, field["bbox"])))
    if not selected:
        return None

    content_regions = [box for _, box in selected]
    content_bbox = _union(content_regions)
    if not content_bbox:
        return None

    structural_bbox, boundary_source = _smallest_content_frame(
        page,
        rectangles,
        content_bbox,
        content_regions,
    )
    search_bbox = _expand(
        content_bbox,
        8.0,
        8.0,
        page.rect.width,
        page.rect.height,
    )
    search_bbox = [
        max(search_bbox[0], structural_bbox[0]),
        max(search_bbox[1], structural_bbox[1]),
        min(search_bbox[2], structural_bbox[2]),
        min(search_bbox[3], structural_bbox[3]),
    ]

    return {
        "zone_id": f"p1_{zone_type.lower()}_01",
        "page": 1,
        "zone_type": zone_type,
        "zone_confidence": min(
            float(fields[name].get("confidence") or 0.0)
            for name, _ in selected
        ),
        "boundary_source": boundary_source,
        "structural_bbox": _normalize_box(
            structural_bbox, page.rect.width, page.rect.height
        ),
        "content_bbox": _normalize_box(
            content_bbox, page.rect.width, page.rect.height
        ),
        "content_regions": [
            _normalize_box(region, page.rect.width, page.rect.height)
            for region in content_regions
        ],
        "search_bbox": _normalize_box(
            search_bbox, page.rect.width, page.rect.height
        ),
        "linked_fields": [name for name, _ in selected],
        "requires_review": True,
    }


def _line_items_region(
    page: fitz.Page,
    rows: list[dict[str, Any]],
    rectangles: list[list[float]],
    totals_region: dict[str, Any] | None,
) -> dict[str, Any] | None:
    header = None
    for row in rows:
        words = {_norm(token["text"]) for token in row["tokens"]}
        score = len(words & LINE_ITEM_WORDS)
        if score >= 2:
            header = row
            break
    if not header:
        return None

    header_bbox = _union([token["bbox"] for token in header["tokens"]])
    if not header_bbox:
        return None

    bottom = page.rect.height * 0.82
    if totals_region and totals_region.get("structural_bbox"):
        normalized = totals_region["structural_bbox"]
        bottom = min(bottom, normalized[1] * page.rect.height / 1000.0 - 4)

    content_boxes = []
    previous_bottom = None
    for row in rows[header["index"] :]:
        if row["center_y"] > bottom:
            break
        box = _union([token["bbox"] for token in row["tokens"]])
        if not box:
            continue
        if previous_bottom is not None and box[1] - previous_bottom > 40 and content_boxes:
            break
        content_boxes.append(box)
        previous_bottom = box[3]

    if not content_boxes:
        return None

    content_bbox = _union(content_boxes)
    if not content_bbox:
        return None

    candidates = []
    for rect in rectangles:
        if (
            rect[0] <= header_bbox[0] + 10
            and rect[2] >= header_bbox[2] - 10
            and rect[1] <= header_bbox[1] + 15
            and rect[3] >= content_bbox[3] - 5
            and rect[2] - rect[0] >= page.rect.width * 0.45
            and rect[3] - rect[1] >= 60
        ):
            candidates.append((_area(rect), rect))

    if candidates:
        structural_bbox = min(candidates, key=lambda item: item[0])[1]
        boundary_source = "VECTOR_TABLE"
    else:
        structural_bbox = _expand(
            content_bbox,
            5.0,
            5.0,
            page.rect.width,
            page.rect.height,
        )
        boundary_source = "TEXT_DENSITY"

    search_bbox = _expand(
        content_bbox,
        3.0,
        3.0,
        page.rect.width,
        page.rect.height,
    )

    return {
        "zone_id": "p1_line_items_01",
        "page": 1,
        "zone_type": "LINE_ITEMS",
        "zone_confidence": 0.96,
        "boundary_source": boundary_source,
        "structural_bbox": _normalize_box(
            structural_bbox, page.rect.width, page.rect.height
        ),
        "content_bbox": _normalize_box(
            content_bbox, page.rect.width, page.rect.height
        ),
        "content_regions": [
            _normalize_box(box, page.rect.width, page.rect.height)
            for box in content_boxes
        ],
        "search_bbox": _normalize_box(
            search_bbox, page.rect.width, page.rect.height
        ),
        "requires_review": True,
    }


def build_page_regions(
    pdf_data: bytes,
    block_lines: list[list[dict[str, Any]]],
    geometry: dict[str, Any],
) -> list[dict[str, Any]]:
    if not pdf_data.startswith(b"%PDF-"):
        return []

    with fitz.open(stream=pdf_data, filetype="pdf") as document:
        if not len(document):
            return []
        page = document[0]
        rows = _visual_rows(block_lines)
        rectangles = _vector_rectangles(page)

        regions: list[dict[str, Any]] = []
        address_zone_by_role: dict[str, list[str]] = defaultdict(list)

        for ordinal, candidate in enumerate(
            geometry.get("address_candidates") or [],
            1,
        ):
            region = _address_region(
                page,
                rows,
                rectangles,
                candidate,
                ordinal,
            )
            if region:
                regions.append(region)
                address_zone_by_role[region["zone_type"]].append(
                    region["zone_id"]
                )
                candidate["zone_id"] = region["zone_id"]

        fields = geometry.get("anchored_fields") or {}
        order_region = _group_field_region(
            page,
            rectangles,
            fields,
            (
                "order_number",
                "order_date",
                "offer_number",
                "offer_date",
                "customer_reference",
            ),
            "ORDER_METADATA",
        )
        if order_region:
            regions.append(order_region)
            for field_name in order_region.get("linked_fields", []):
                if isinstance(fields.get(field_name), dict):
                    fields[field_name]["zone_id"] = order_region["zone_id"]

        totals_region = _group_field_region(
            page,
            rectangles,
            fields,
            (
                "total_net",
                "total_vat",
                "total_gross",
                "amount_due",
            ),
            "TOTALS",
        )
        if totals_region:
            regions.append(totals_region)
            for field_name in totals_region.get("linked_fields", []):
                if isinstance(fields.get(field_name), dict):
                    fields[field_name]["zone_id"] = totals_region["zone_id"]

        line_items_region = _line_items_region(
            page,
            rows,
            rectangles,
            totals_region,
        )
        if line_items_region:
            regions.append(line_items_region)

        regions.sort(
            key=lambda item: (
                item.get("page", 1),
                item["content_bbox"][1],
                item["content_bbox"][0],
            )
        )
        return regions

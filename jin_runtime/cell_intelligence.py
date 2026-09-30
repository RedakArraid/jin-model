from __future__ import annotations

import re
from typing import Any

import fitz

from jin_runtime.zone_intelligence import (
    _absolute_normalized_box,
    _area,
    _center,
    _norm,
    _normalize_box,
    _visual_rows,
)

HEADER_ALIASES = {
    "line_number": (("LIGNE",),),
    "product_code": (("ARTICLE",), ("CODE",), ("CODE", "ART"), ("CODE", "ARTICLE")),
    "reference": (("REFERENCE",), ("REF",)),
    "internal_reference": (("ABSOLU",), ("REFINTERNE",), ("REFERENCEINTERNE",), ("REF", "INTERNE")),
    "description": (("DESIGNATION",), ("DESCRIPTION",), ("LIBELLE",)),
    "quantity": (("QTE",), ("QUANTITE",), ("COMMANDE",), ("QTE", "CDEE")),
    "unit": (("U",), ("UNITE",), ("UOM",)),
    "unit_price": (("PRIX", "NET"), ("PA", "HT"), ("PRIX", "UNITAIRE"), ("PX", "UNIT"), ("PX", "UNITAIRE"), ("PU",), ("PX", "BASE"), ("PRIX",)),
    "line_total": (("MONTANT", "HT"), ("MNT", "NET"), ("TOTAL", "HT"), ("MONTANT",)),
    "delivery_delay": (("DELAI",),),
    "seller": (("VENDU", "PAR"),),
}

ADDRESS_COMPONENTS = (
    "building", "industrial_zone", "house_number", "house_number_suffix",
    "street_type", "street_name", "bp", "postal_box", "cs", "tsa",
    "postal_code", "city",
)

SPAN_CELL_TYPES = {
    "LINE_ITEM_REFERENCE": {"product_code", "reference", "internal_reference"},
    "LINE_ITEM_DESCRIPTION": {"description"},
    "LINE_ITEM_QUANTITY": {"quantity"},
    "LINE_ITEM_UNIT": {"unit"},
    "LINE_ITEM_UNIT_PRICE": {"unit_price"},
    "LINE_ITEM_TOTAL": {"line_total"},
    "ORDER_NUMBER": {"order_number"},
    "ORDER_DATE": {"order_date"},
    "TOTAL_NET": {"total_net"},
    "TOTAL_VAT": {"total_vat"},
    "TOTAL_GROSS": {"total_gross"},
    "TOTAL_AMOUNT_DUE": {"amount_due"},
}


def _union(boxes: list[list[float]]) -> list[float] | None:
    if not boxes:
        return None
    return [
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes),
    ]


def _inside(box: list[float], container: list[float], margin: float = 1.5) -> bool:
    x, y = _center(box)
    return (
        container[0] - margin <= x <= container[2] + margin
        and container[1] - margin <= y <= container[3] + margin
    )


def _sequence(tokens: list[dict[str, Any]], alias: tuple[str, ...]) -> tuple[int, int] | None:
    norms = [_norm(token["text"]) for token in tokens]
    joined = "".join(alias)
    for index, value in enumerate(norms):
        if value == joined:
            return index, index + 1
    for start in range(len(tokens)):
        position = start
        for wanted in alias:
            while position < len(tokens) and not norms[position]:
                position += 1
            if position >= len(tokens) or norms[position] != wanted:
                break
            position += 1
        else:
            return start, position
    return None


def _headers_in_row(row: dict[str, Any], zone: list[float]):
    tokens = [token for token in row["tokens"] if _inside(token["bbox"], zone, 2)]
    found = []
    for cell_type, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            match = _sequence(tokens, alias)
            if not match:
                continue
            selected = tokens[match[0] : match[1]]
            box = _union([token["bbox"] for token in selected])
            found.append({
                "cell_type": cell_type,
                "bbox": box,
                "x": _center(box)[0],
                "text": " ".join(token["text"] for token in selected),
            })
            break
    unique = {}
    for item in found:
        unique.setdefault(item["cell_type"], item)
    return list(unique.values())


def _header_candidates(rows: list[dict[str, Any]], zone: list[float]):
    row_headers = [
        (row, _headers_in_row(row, zone))
        for row in rows
    ]
    best = None
    for index, (row, found) in enumerate(row_headers):
        windows = [(row, found)]
        if index + 1 < len(row_headers):
            next_row, next_found = row_headers[index + 1]
            if next_row["center_y"] - row["center_y"] <= 24:
                merged = {}
                for item in found + next_found:
                    merged.setdefault(item["cell_type"], item)
                windows.append((next_row, list(merged.values())))
        for header_row, candidates in windows:
            if len(candidates) >= 2 and (
                best is None or len(candidates) > best[0]
            ):
                best = len(candidates), header_row, candidates
    return best


def _vector_separators(page: fitz.Page, zone: list[float]) -> list[float]:
    output = []
    zone_height = max(1.0, zone[3] - zone[1])
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            if not item or item[0] != "l":
                continue
            start, end = item[1], item[2]
            if abs(start.x - end.x) > 1.2:
                continue
            y0, y1 = sorted((start.y, end.y))
            overlap = max(0.0, min(y1, zone[3]) - max(y0, zone[1]))
            if overlap >= zone_height * 0.45 and zone[0] - 3 <= start.x <= zone[2] + 3:
                output.append(float(start.x))
    unique = []
    for value in sorted(output):
        if not unique or abs(value - unique[-1]) > 3:
            unique.append(value)
    return unique


def _header_rect(page: fitz.Page, header_box: list[float], zone: list[float]):
    candidates = []
    header_width = max(1.0, header_box[2] - header_box[0])
    zone_width = max(1.0, zone[2] - zone[0])
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if not rect or rect.width < 5 or rect.height < 5:
            continue
        box = [float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)]
        if not _inside(header_box, box, 2):
            continue
        if box[2] - box[0] > zone_width * 0.50:
            continue
        if box[2] - box[0] > max(header_width * 6.0, 90.0):
            continue
        candidates.append((_area(box), box))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _line_item_cells(page: fitz.Page, rows, region):
    zone = _absolute_normalized_box(page, region["search_bbox"])
    header = _header_candidates(rows, zone)
    if not header:
        return [], [], []

    _, header_row, columns = header
    columns = sorted(columns, key=lambda item: item["x"])
    centers = [item["x"] for item in columns]
    midpoint_bounds = [zone[0]] + [
        (centers[index] + centers[index + 1]) / 2
        for index in range(len(centers) - 1)
    ] + [zone[2]]
    separators = _vector_separators(page, zone)

    for index, column in enumerate(columns):
        rect = _header_rect(page, column["bbox"], zone)
        if rect:
            column["left"], column["right"] = rect[0], rect[2]
            column["boundary_source"] = "VECTOR_HEADER_CELL"
        else:
            column["left"], column["right"] = midpoint_bounds[index], midpoint_bounds[index + 1]
            column["boundary_source"] = "HEADER_ALIGNMENT"
            left = [x for x in separators if abs(x - column["left"]) < 28]
            right = [x for x in separators if abs(x - column["right"]) < 28]
            if left:
                column["left"] = min(left, key=lambda x: abs(x - column["left"]))
            if right:
                column["right"] = min(right, key=lambda x: abs(x - column["right"]))
            if left or right:
                column["boundary_source"] = "VECTOR_GRID"

    column_subzones = []
    for index, column in enumerate(columns):
        column_subzones.append({
            "subzone_id": f"{region['zone_id']}_col_{column['cell_type']}",
            "subzone_type": "COLUMN",
            "column_index": index,
            "column_type": column["cell_type"],
            "header_text": column["text"],
            "bbox": _normalize_box(
                [column["left"], zone[1], column["right"], zone[3]],
                page.rect.width, page.rect.height,
            ),
            "boundary_source": column["boundary_source"],
            "confidence": 0.99 if column["boundary_source"] == "VECTOR_HEADER_CELL" else 0.96 if column["boundary_source"] == "VECTOR_GRID" else 0.92,
            "requires_review": True,
        })

    header_cells = [
        {
            "cell_id": f"{region['zone_id']}_header_{column['cell_type']}",
            "row_index": -1,
            "column_index": index,
            "cell_type": column["cell_type"],
            "text": column["text"],
            "bbox": _normalize_box(column["bbox"], page.rect.width, page.rect.height),
            "confidence": 0.99,
            "boundary_source": "HEADER",
            "requires_review": True,
        }
        for index, column in enumerate(columns)
    ]

    cells = []
    row_records = []
    row_number = 0
    for row in rows:
        if row["index"] <= header_row["index"] or row["center_y"] < zone[1] - 2:
            continue
        if row["center_y"] > zone[3] + 2:
            break
        tokens = [token for token in row["tokens"] if _inside(token["bbox"], zone, 2)]
        if not tokens:
            continue
        normalized_text = _norm(" ".join(token["text"] for token in tokens))
        if any(key in normalized_text for key in ("TOTALHT", "NETHT", "MONTANTTTC", "NETAPAYER")):
            break

        assigned = []
        for column_index, column in enumerate(columns):
            selected = [
                token for token in tokens
                if column["left"] - 1 <= _center(token["bbox"])[0] <= column["right"] + 1
            ]
            if not selected:
                continue
            box = _union([token["bbox"] for token in selected])
            text = " ".join(token["text"] for token in selected).strip()
            if not text:
                continue
            cell = {
                "cell_id": f"{region['zone_id']}_r{row_number:03d}_{column['cell_type']}",
                "row_index": row_number,
                "column_index": column_index,
                "cell_type": column["cell_type"],
                "text": text,
                "bbox": _normalize_box(box, page.rect.width, page.rect.height),
                "token_count": len(selected),
                "confidence": 0.97 if column["boundary_source"].startswith("VECTOR") else 0.94,
                "boundary_source": column["boundary_source"],
                "requires_review": True,
            }
            cells.append(cell)
            assigned.append(cell)

        if assigned:
            types = {cell["cell_type"] for cell in assigned}
            numeric = bool(types & {"quantity", "unit_price", "line_total"})
            identity = bool(types & {"product_code", "reference", "internal_reference", "description"})
            row_type = "data" if numeric and identity else "continuation" if identity else "note"
            row_box = _union([
                _absolute_normalized_box(page, cell["bbox"])
                for cell in assigned
            ])
            row_records.append({
                "row_id": f"{region['zone_id']}_r{row_number:03d}",
                "row_index": row_number,
                "row_type": row_type,
                "bbox": _normalize_box(row_box, page.rect.width, page.rect.height),
                "cells": [cell["cell_id"] for cell in assigned],
            })
            row_number += 1

    return header_cells + cells, row_records, column_subzones


def _find_value(rows, value: str, zone: list[float]):
    target = _norm(value)
    if not target:
        return None
    matches = []
    for row in rows:
        tokens = [token for token in row["tokens"] if _inside(token["bbox"], zone, 2)]
        for start in range(len(tokens)):
            current = ""
            for end in range(start, min(len(tokens), start + 10)):
                current += _norm(tokens[end]["text"])
                if current == target:
                    selected = tokens[start : end + 1]
                    matches.append((_union([token["bbox"] for token in selected]), selected))
                    break
                if len(current) > len(target) + 6:
                    break
    return min(matches, key=lambda item: _area(item[0])) if matches else None


def _address_cells(page: fitz.Page, rows, region, candidate):
    zone = _absolute_normalized_box(page, region["search_bbox"])
    cells = []
    for component in ADDRESS_COMPONENTS:
        value = (candidate.get("components") or {}).get(component)
        if isinstance(value, bool) or not isinstance(value, (str, int, float)) or not str(value).strip():
            continue
        match = _find_value(rows, str(value), zone)
        if not match:
            continue
        box, tokens = match
        cells.append({
            "cell_id": f"{region['zone_id']}_{component}",
            "cell_type": component,
            "text": " ".join(token["text"] for token in tokens),
            "bbox": _normalize_box(box, page.rect.width, page.rect.height),
            "token_count": len(tokens),
            "confidence": 0.98,
            "boundary_source": "VALUE_MATCH",
            "requires_review": True,
        })
    return cells


def _field_cells(region, fields):
    cells = []
    for name in region.get("linked_fields", []):
        field = fields.get(name)
        if not isinstance(field, dict) or not field.get("bbox"):
            continue
        cell = {
            "cell_id": f"{region['zone_id']}_{name}",
            "cell_type": name,
            "text": str(field.get("value", "")),
            "bbox": list(field["bbox"]),
            "confidence": float(field.get("confidence") or 0.0),
            "boundary_source": "ANCHORED_FIELD",
            "requires_review": True,
        }
        cells.append(cell)
        field["cell_id"] = cell["cell_id"]
    return cells


def _quality(region, cells):
    content_area = sum(_area(box) for box in region.get("content_regions", []))
    if not content_area:
        content_area = _area(region.get("content_bbox") or [0, 0, 0, 0])
    cell_area = sum(_area(cell["bbox"]) for cell in cells)
    return {
        "search_to_content_ratio": round(_area(region.get("search_bbox") or [0, 0, 0, 0]) / content_area, 3) if content_area else None,
        "structural_to_content_ratio": round(_area(region.get("structural_bbox") or [0, 0, 0, 0]) / content_area, 3) if content_area else None,
        "cell_area_to_content_ratio": round(cell_area / content_area, 3) if content_area else None,
        "cell_count": len(cells),
        "nonempty_cell_count": sum(bool(str(cell.get("text") or "").strip()) for cell in cells),
    }


def enrich_page_regions_with_cells(pdf_data, block_lines, geometry, page_regions):
    if not pdf_data.startswith(b"%PDF-"):
        return page_regions
    with fitz.open(stream=pdf_data, filetype="pdf") as document:
        if not len(document):
            return page_regions
        page = document[0]
        rows = _visual_rows(block_lines)
        address_by_zone = {
            candidate.get("zone_id"): candidate
            for candidate in geometry.get("address_candidates") or []
            if candidate.get("zone_id")
        }
        fields = geometry.get("anchored_fields") or {}
        for region in page_regions:
            cells = []
            subzones = []
            zone_type = region.get("zone_type")
            if zone_type == "LINE_ITEMS":
                cells, row_subzones, subzones = _line_item_cells(page, rows, region)
                region["rows"] = row_subzones
            elif zone_type in {"SUPPLIER", "SHIP_TO", "BILL_TO", "UNKNOWN"}:
                candidate = address_by_zone.get(region.get("zone_id"))
                if candidate:
                    cells = _address_cells(page, rows, region, candidate)
            elif zone_type in {"ORDER_METADATA", "TOTALS"}:
                cells = _field_cells(region, fields)
            region["cells"] = cells
            if subzones:
                region["subzones"] = subzones
            region["zone_quality"] = _quality(region, cells)
    return page_regions


def assign_span_to_cell(span, page_regions):
    label = str(span.get("label") or "")
    wanted = SPAN_CELL_TYPES.get(label)
    address_type = label[len("ADDRESS_") :].lower() if label.startswith("ADDRESS_") else None
    center_x = (span["bbox"][0] + span["bbox"][2]) / 2
    center_y = (span["bbox"][1] + span["bbox"][3]) / 2
    candidates = []
    for region in page_regions:
        for cell in region.get("cells") or []:
            if not (
                cell["bbox"][0] <= center_x <= cell["bbox"][2]
                and cell["bbox"][1] <= center_y <= cell["bbox"][3]
            ):
                continue
            cell_type = cell.get("cell_type")
            if wanted and cell_type in wanted:
                candidates.append(cell)
            elif address_type and cell_type == address_type:
                candidates.append(cell)
    if candidates:
        cell = min(candidates, key=lambda item: _area(item["bbox"]))
        span["cell_id"] = cell["cell_id"]
        span["cell_type"] = cell["cell_type"]
    return span

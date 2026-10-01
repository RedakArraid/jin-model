from __future__ import annotations

from typing import Any, Iterable


BBox = tuple[float, float, float, float]


def _bbox(item: Any) -> BBox:
    value = getattr(item, "bbox", None)
    if value is None and isinstance(item, dict):
        value = item.get("bbox")
    if value is None or len(value) < 4:
        raise ValueError("Layout item has no usable bbox")
    return tuple(float(value[index]) for index in range(4))  # type: ignore[return-value]


def _point_xy(point: Any) -> tuple[float, float]:
    if hasattr(point, "x") and hasattr(point, "y"):
        return float(point.x), float(point.y)
    return float(point[0]), float(point[1])


def _rect_bbox(rect: Any) -> BBox:
    if all(hasattr(rect, key) for key in ("x0", "y0", "x1", "y1")):
        return float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)
    return tuple(float(rect[index]) for index in range(4))  # type: ignore[return-value]


def _merge_collinear(lines: list[dict[str, Any]], tolerance: float = 1.4) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for line in sorted(
        lines,
        key=lambda value: (
            value["orientation"],
            value["x0"] if value["orientation"] == "vertical" else value["y0"],
            value["y0"] if value["orientation"] == "vertical" else value["x0"],
        ),
    ):
        orientation = line["orientation"]
        fixed = line["x0"] if orientation == "vertical" else line["y0"]
        start = line["y0"] if orientation == "vertical" else line["x0"]
        end = line["y1"] if orientation == "vertical" else line["x1"]
        match = None
        for current in reversed(merged):
            if current["orientation"] != orientation:
                continue
            current_fixed = current["x0"] if orientation == "vertical" else current["y0"]
            current_start = current["y0"] if orientation == "vertical" else current["x0"]
            current_end = current["y1"] if orientation == "vertical" else current["x1"]
            if abs(fixed - current_fixed) <= tolerance and start <= current_end + tolerance and end >= current_start - tolerance:
                match = current
                break
        if match is None:
            merged.append(dict(line))
            continue
        if orientation == "vertical":
            match["x0"] = match["x1"] = round((match["x0"] + fixed) / 2, 3)
            match["y0"] = min(match["y0"], start)
            match["y1"] = max(match["y1"], end)
        else:
            match["y0"] = match["y1"] = round((match["y0"] + fixed) / 2, 3)
            match["x0"] = min(match["x0"], start)
            match["x1"] = max(match["x1"], end)
    return merged


def extract_vector_layout(page: Any, config: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[BBox]]:
    """Return trustworthy vector separators and rectangles from a native PDF page.

    PyMuPDF exposes actual drawing instructions. Keeping these guides separate
    from text means consumers can use them without changing the source words.
    """
    cfg = config or {}
    page_width = float(page.rect.width)
    page_height = float(page.rect.height)
    min_h = float(cfg.get("vector_min_horizontal_length", max(24.0, page_width * 0.035)))
    min_v = float(cfg.get("vector_min_vertical_length", max(18.0, page_height * 0.025)))
    min_rect_width = float(cfg.get("vector_min_rectangle_width", 24.0))
    min_rect_height = float(cfg.get("vector_min_rectangle_height", 12.0))
    lines: list[dict[str, Any]] = []
    rectangles: list[BBox] = []

    try:
        drawings = page.get_drawings()
    except Exception:
        return [], []
    for drawing in drawings:
        for item in drawing.get("items", []):
            kind = item[0] if item else None
            if kind == "l" and len(item) >= 3:
                x0, y0 = _point_xy(item[1])
                x1, y1 = _point_xy(item[2])
                if abs(x1 - x0) <= 1.5 and abs(y1 - y0) >= min_v:
                    y0, y1 = sorted((y0, y1))
                    x = (x0 + x1) / 2
                    lines.append({"orientation": "vertical", "x0": x, "y0": y0, "x1": x, "y1": y1, "source": "pdf_vector"})
                elif abs(y1 - y0) <= 1.5 and abs(x1 - x0) >= min_h:
                    x0, x1 = sorted((x0, x1))
                    y = (y0 + y1) / 2
                    lines.append({"orientation": "horizontal", "x0": x0, "y0": y, "x1": x1, "y1": y, "source": "pdf_vector"})
            elif kind == "re" and len(item) >= 2:
                x0, y0, x1, y1 = _rect_bbox(item[1])
                x0, x1 = sorted((x0, x1))
                y0, y1 = sorted((y0, y1))
                if x1 - x0 >= min_rect_width and y1 - y0 >= min_rect_height:
                    rectangles.append((x0, y0, x1, y1))
                # Rectangle edges are useful even when the renderer represents
                # a table border as one `re` instruction instead of four lines.
                if x1 - x0 >= min_h:
                    lines.extend([
                        {"orientation": "horizontal", "x0": x0, "y0": y0, "x1": x1, "y1": y0, "source": "pdf_rectangle"},
                        {"orientation": "horizontal", "x0": x0, "y0": y1, "x1": x1, "y1": y1, "source": "pdf_rectangle"},
                    ])
                if y1 - y0 >= min_v:
                    lines.extend([
                        {"orientation": "vertical", "x0": x0, "y0": y0, "x1": x0, "y1": y1, "source": "pdf_rectangle"},
                        {"orientation": "vertical", "x0": x1, "y0": y0, "x1": x1, "y1": y1, "source": "pdf_rectangle"},
                    ])

    lines = _merge_collinear(lines)
    unique_rectangles: list[BBox] = []
    for rect in sorted(rectangles, key=lambda value: ((value[2] - value[0]) * (value[3] - value[1]), value)):
        if not any(max(abs(a - b) for a, b in zip(rect, known)) <= 1.2 for known in unique_rectangles):
            unique_rectangles.append(rect)
    return lines, unique_rectangles


def _smallest_containing_rectangle(bbox: BBox, rectangles: Iterable[BBox]) -> BBox | None:
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    matches = [rect for rect in rectangles if rect[0] - 1 <= cx <= rect[2] + 1 and rect[1] - 1 <= cy <= rect[3] + 1]
    if not matches:
        return None
    return min(matches, key=lambda rect: (rect[2] - rect[0]) * (rect[3] - rect[1]))


def split_items_by_compartments(
    items: Iterable[Any],
    vector_lines: Iterable[dict[str, Any]] | None = None,
    vector_rectangles: Iterable[BBox] | None = None,
    *,
    gap_threshold: float = 48.0,
) -> list[dict[str, Any]]:
    """Split a visual text row at real PDF borders, then at clearly wide gaps."""
    ordered = sorted(list(items), key=lambda item: _bbox(item)[0])
    if not ordered:
        return []
    verticals = [line for line in (vector_lines or []) if line.get("orientation") == "vertical"]
    rectangles = list(vector_rectangles or [])
    groups: list[list[Any]] = [[ordered[0]]]
    sources: list[str] = []
    for item in ordered[1:]:
        previous = groups[-1][-1]
        pb = _bbox(previous)
        ib = _bbox(item)
        gap = ib[0] - pb[2]
        row_top = min(pb[1], ib[1])
        row_bottom = max(pb[3], ib[3])
        separator = any(
            pb[2] - 1.0 <= float(line["x0"]) <= ib[0] + 1.0
            and float(line["y0"]) <= row_top + 1.5
            and float(line["y1"]) >= row_bottom - 1.5
            for line in verticals
        )
        previous_rect = _smallest_containing_rectangle(pb, rectangles)
        item_rect = _smallest_containing_rectangle(ib, rectangles)
        separate_rectangles = previous_rect is not None and item_rect is not None and previous_rect != item_rect
        if separator or separate_rectangles or gap > gap_threshold:
            groups.append([item])
            sources.append("pdf_vector" if separator or separate_rectangles else "wide_gap")
        else:
            groups[-1].append(item)

    output: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        boxes = [_bbox(item) for item in group]
        output.append({
            "items": group,
            "bbox": (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)),
            "text": " ".join(str(getattr(item, "text", item.get("text", "") if isinstance(item, dict) else "")) for item in group).strip(),
            "boundary_source": sources[index - 1] if index > 0 else None,
        })
    return output

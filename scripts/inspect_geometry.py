#!/usr/bin/env python3
"""Inspect first-page geometry suggestions without running the full API."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jin_runtime.geometry_fields import (
    _anchor_bands_v3,
    _locality_v3,
    _role_anchors_v3,
    _row_segments,
    _street_v3,
    extract_geometry_suggestions,
    visual_lines,
)
from jin_runtime.weak_field_router import _extract_first_page_lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", nargs="+", type=Path)
    parser.add_argument("--lines", action="store_true", help="Include reconstructed visual lines.")
    parser.add_argument("--ocr-languages", default="fra+eng")
    args = parser.parse_args()

    reports = []
    for path in args.pdf:
        lines, source = _extract_first_page_lines(path.read_bytes(), args.ocr_languages)
        report = {
            "file": str(path),
            "text_source": source,
            **extract_geometry_suggestions(lines),
        }
        if args.lines:
            geometry_rows = visual_lines(lines)
            role_anchors = _role_anchors_v3(geometry_rows)
            page_width = (
                float(geometry_rows[0]["tokens"][0]["page_width"])
                if geometry_rows
                else 0.0
            )
            report["role_anchors"] = role_anchors
            report["anchor_bands"] = [
                {
                    "low": low,
                    "high": high,
                    "role": role,
                    "anchor": anchor,
                }
                for low, high, role, anchor in _anchor_bands_v3(role_anchors, page_width)
            ]
            report["source_lines"] = [
                {
                    "index": index,
                    "text": " ".join(str(token.get("text") or "") for token in line).strip(),
                    "bbox": [
                        min(float(token["bbox"][0]) for token in line),
                        min(float(token["bbox"][1]) for token in line),
                        max(float(token["bbox"][2]) for token in line),
                        max(float(token["bbox"][3]) for token in line),
                    ],
                }
                for index, line in enumerate(lines)
                if line
            ]
            report["visual_lines"] = [
                {
                    "index": row["index"],
                    "text": row["text"],
                    "tokens": [
                        {
                            "text": token.get("text"),
                            "bbox": token.get("bbox"),
                        }
                        for token in row["tokens"]
                    ],
                }
                for row in geometry_rows
            ]
            report["street_debug"] = []
            for row in geometry_rows:
                for segment in _row_segments(row):
                    street = _street_v3(segment)
                    if not street:
                        continue
                    localities = []
                    for candidate_row in geometry_rows[row["index"] + 1 : row["index"] + 7]:
                        locality = _locality_v3(
                            candidate_row,
                            street["bbox"][0],
                            geometry_rows[candidate_row["index"] + 1 : candidate_row["index"] + 3],
                        )
                        if locality:
                            localities.append({
                                "row": candidate_row["index"],
                                "postal_code": locality.get("postal_value"),
                                "city": " ".join(token["text"] for token in locality["city"]),
                            })
                    report["street_debug"].append({
                        "row": row["index"],
                        "text": segment["text"],
                        "x": street["bbox"][0],
                        "localities": localities,
                    })
        reports.append(report)

    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

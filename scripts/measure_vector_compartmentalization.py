#!/usr/bin/env python3
"""Measure the effect of PDF vector guides on visual-row block lengths."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics

import fitz

from po_ocr.config import load_config
from po_ocr.extract import all_rows
from po_ocr.layout_guides import extract_vector_layout, split_items_by_compartments
from po_ocr.models import PageResult
from po_ocr.pdf_native import extract_native_page, is_native_page


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("sample_files", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    sample = json.loads(args.sample_files.read_text(encoding="utf-8"))
    config = load_config(None)
    pdf_config = {**config["pdf"], "hybrid_native_ocr_enabled": False}
    before_widths: list[float] = []
    after_widths: list[float] = []
    files_with_vectors: set[str] = set()
    files_split: set[str] = set()
    native_pages = visual_rows = vector_boundaries = wide_before = wide_after = 0

    for relative in sample:
        with fitz.open(args.folder / relative) as document:
            for index, pdf_page in enumerate(document):
                if not is_native_page(pdf_page, pdf_config):
                    continue
                native_pages += 1
                text, words, confidence = extract_native_page(pdf_page, index + 1, pdf_config)
                guides, rectangles = extract_vector_layout(pdf_page, config["layout"])
                if guides or rectangles:
                    files_with_vectors.add(relative)
                page = PageResult(
                    page=index + 1, width=pdf_page.rect.width, height=pdf_page.rect.height,
                    source_type="native_pdf", text=text, words=words, confidence=confidence,
                    vector_lines=guides, vector_rectangles=rectangles,
                )
                for row in all_rows([page], y_factor=float(config["layout"]["line_y_tolerance_factor"])):
                    visual_rows += 1
                    before = (row.bbox[2] - row.bbox[0]) / pdf_page.rect.width
                    groups = split_items_by_compartments(row.words, guides, rectangles)
                    after = [(group["bbox"][2] - group["bbox"][0]) / pdf_page.rect.width for group in groups]
                    before_widths.append(before); after_widths.extend(after)
                    wide_before += int(before >= 0.55)
                    wide_after += sum(width >= 0.55 for width in after)
                    applied = sum(group.get("boundary_source") == "pdf_vector" for group in groups)
                    vector_boundaries += applied
                    if applied:
                        files_split.add(relative)

    metrics = {
        "sample_files": len(sample), "native_pages": native_pages,
        "files_with_vector_layout": len(files_with_vectors),
        "files_split_by_vector_guides": len(files_split), "visual_rows": visual_rows,
        "vector_boundaries_applied": vector_boundaries,
        "wide_rows_before": wide_before, "wide_blocks_after": wide_after,
        "wide_block_reduction_percent": round((wide_before - wide_after) / max(wide_before, 1) * 100, 1),
        "mean_width_ratio_before": round(statistics.mean(before_widths), 4),
        "mean_width_ratio_after": round(statistics.mean(after_widths), 4),
        "wide_threshold_page_ratio": 0.55,
        "scope": "native PDF pages only; raster scans have no reliable PDF vector guides",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

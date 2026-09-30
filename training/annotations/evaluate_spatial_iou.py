#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from jin_runtime.spatial_metrics import evaluate_spatial_items


def collect_cells(payload: dict) -> list[dict]:
    cells = []
    for region in payload.get("page_regions") or []:
        cells.extend(region.get("cells") or [])
    return cells


def collect_truth(payload: dict) -> list[dict]:
    if isinstance(payload.get("cells"), list):
        return payload["cells"]
    cells = []
    for page in payload.get("pages") or []:
        cells.extend(page.get("cells") or page.get("regions") or [])
    return cells


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate JIN cell/sub-zone boxes against reviewed spatial annotations."
    )
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--truth", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()

    prediction_payload = json.loads(
        Path(args.predictions).read_text(encoding="utf-8")
    )
    truth_payload = json.loads(Path(args.truth).read_text(encoding="utf-8"))

    metrics = evaluate_spatial_items(
        collect_cells(prediction_payload),
        collect_truth(truth_payload),
    )
    text = json.dumps(metrics, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

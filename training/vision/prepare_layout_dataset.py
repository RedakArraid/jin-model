#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from pdf2image import convert_from_path
from PIL import Image
import pytesseract
from pytesseract import Output


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    items = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"Manifest row {line_no} must be a JSON object")
            items.append(value)
    return items


def assert_materialized_pdf(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(path)
    head = path.read_bytes()[:160]
    if head.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise RuntimeError(f"Git LFS pointer not materialized: {path}")
    if not head.startswith(b"%PDF-"):
        raise RuntimeError(f"Not a PDF binary: {path}")


def normalize_bbox(bbox: list[float], width: int, height: int, space: str = "pixel") -> list[int]:
    x0, y0, x1, y1 = bbox
    if space in {"normalized", "layoutlm", "1000"}:
        values = [x0, y0, x1, y1]
    else:
        values = [1000 * x0 / width, 1000 * y0 / height, 1000 * x1 / width, 1000 * y1 / height]
    return [max(0, min(1000, int(round(v)))) for v in values]


def load_regions(annotation_path: Path | None, page_number: int, width: int, height: int) -> list[dict[str, Any]]:
    if annotation_path is None or not annotation_path.exists():
        return []
    data = json.loads(annotation_path.read_text(encoding="utf-8"))
    pages = data.get("pages", []) if isinstance(data, dict) else []
    for page in pages:
        if int(page.get("page", 0)) != page_number:
            continue
        regions = []
        for region in page.get("regions", []):
            if not isinstance(region, dict) or "label" not in region or "bbox" not in region:
                continue
            regions.append(
                {
                    "label": str(region["label"]),
                    "bbox": normalize_bbox(
                        [float(v) for v in region["bbox"]],
                        width,
                        height,
                        str(region.get("bbox_space", "pixel")).lower(),
                    ),
                }
            )
        return regions
    return []


def label_for_box(box: list[int], regions: list[dict[str, Any]]) -> str:
    cx = (box[0] + box[2]) / 2
    cy = (box[1] + box[3]) / 2
    candidates = []
    for region in regions:
        x0, y0, x1, y1 = region["bbox"]
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            area = max(1, (x1 - x0) * (y1 - y0))
            candidates.append((area, region["label"]))
    return min(candidates)[1] if candidates else "O"


def ocr_page(image: Image.Image, lang: str, regions: list[dict[str, Any]]) -> tuple[list[str], list[list[int]], list[str]]:
    data = pytesseract.image_to_data(image, lang=lang, output_type=Output.DICT)
    words: list[str] = []
    boxes: list[list[int]] = []
    labels: list[str] = []
    width, height = image.size
    for idx, raw in enumerate(data["text"]):
        word = str(raw).strip()
        try:
            conf = float(data["conf"][idx])
        except (TypeError, ValueError):
            conf = -1
        if not word or conf < 0:
            continue
        left = int(data["left"][idx])
        top = int(data["top"][idx])
        w = int(data["width"][idx])
        h = int(data["height"][idx])
        box = normalize_bbox([left, top, left + w, top + h], width, height, "pixel")
        words.append(word)
        boxes.append(box)
        labels.append(label_for_box(box, regions))
    return words, boxes, labels


def split_name(document_id: str) -> str:
    bucket = int(hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:8], 16) % 10
    return "train" if bucket < 8 else "validation"


def main() -> int:
    parser = argparse.ArgumentParser(description="Render materialized PDF corpus and build LayoutLMv3 page JSONL.")
    parser.add_argument("--manifest", default="corpus/manifest.jsonl")
    parser.add_argument("--output", default="data/layout")
    parser.add_argument("--dpi", type=int, default=180)
    parser.add_argument("--lang", default="fra+eng+deu")
    parser.add_argument("--limit", type=int, default=0, help="Optional document limit for smoke tests")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    output = Path(args.output)
    images_dir = output / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    rows = read_jsonl(manifest_path)
    if args.limit > 0:
        rows = rows[: args.limit]

    splits: dict[str, list[dict[str, Any]]] = {"train": [], "validation": []}
    labels = {"O"}
    for row in rows:
        document_id = str(row.get("document_id") or "").strip()
        if not document_id:
            raise ValueError("Every manifest row needs document_id")
        pdf_path = Path(str(row.get("pdf") or ""))
        if not pdf_path.is_absolute():
            pdf_path = (manifest_path.parent.parent / pdf_path).resolve() if str(pdf_path).startswith("corpus/") else (manifest_path.parent / pdf_path).resolve()
        assert_materialized_pdf(pdf_path)

        ann_raw = row.get("annotation")
        annotation_path = None
        if ann_raw:
            annotation_path = Path(str(ann_raw))
            if not annotation_path.is_absolute():
                annotation_path = (manifest_path.parent.parent / annotation_path).resolve() if str(annotation_path).startswith("corpus/") else (manifest_path.parent / annotation_path).resolve()

        pages = convert_from_path(str(pdf_path), dpi=args.dpi)
        for page_idx, image in enumerate(pages, 1):
            image = image.convert("RGB")
            regions = load_regions(annotation_path, page_idx, *image.size)
            words, boxes, page_labels = ocr_page(image, args.lang, regions)
            labels.update(page_labels)
            image_path = images_dir / f"{document_id}_p{page_idx:03d}.png"
            image.save(image_path)
            record = {
                "document_id": document_id,
                "page": page_idx,
                "image_path": str(image_path.resolve()),
                "words": words,
                "boxes": boxes,
                "labels": page_labels,
                "source_pdf": str(pdf_path.resolve()),
            }
            splits[split_name(document_id)].append(record)

    output.mkdir(parents=True, exist_ok=True)
    for split, records in splits.items():
        with (output / f"{split}.jsonl").open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    (output / "labels.json").write_text(json.dumps(sorted(labels), indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"documents": len(rows), "train_pages": len(splits["train"]), "validation_pages": len(splits["validation"]), "labels": sorted(labels)}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

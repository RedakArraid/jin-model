from __future__ import annotations

from pathlib import Path

import fitz
from PIL import Image


def render_pages(path: Path, *, max_pages: int = 3, dpi: int = 130) -> list[Image.Image]:
    images: list[Image.Image] = []
    scale = dpi / 72.0
    with fitz.open(path) as document:
        for page_index in range(min(len(document), max_pages)):
            page = document[page_index]
            pix = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                colorspace=fitz.csRGB,
                alpha=False,
            )
            images.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
    return images


def contact_sheet(
    path: Path,
    *,
    max_pages: int = 3,
    dpi: int = 130,
    max_width: int = 1800,
    max_height: int = 5000,
) -> Image.Image:
    pages = render_pages(path, max_pages=max_pages, dpi=dpi)
    if not pages:
        raise ValueError(f"PDF has no pages: {path}")
    scaled: list[Image.Image] = []
    for page in pages:
        ratio = min(1.0, max_width / max(page.width, 1))
        if ratio < 1.0:
            page = page.resize(
                (round(page.width * ratio), round(page.height * ratio)),
                Image.Resampling.LANCZOS,
            )
        scaled.append(page)
    width = max(page.width for page in scaled)
    height = sum(page.height for page in scaled)
    if height > max_height:
        ratio = max_height / height
        scaled = [
            page.resize(
                (
                    max(1, round(page.width * ratio)),
                    max(1, round(page.height * ratio)),
                ),
                Image.Resampling.LANCZOS,
            )
            for page in scaled
        ]
        width = max(page.width for page in scaled)
        height = sum(page.height for page in scaled)
    sheet = Image.new("RGB", (width, height), "white")
    y = 0
    for page in scaled:
        sheet.paste(page, (0, y))
        y += page.height
    return sheet


def crop_normalized(image: Image.Image, bbox: list[float]) -> Image.Image:
    x0, y0, x1, y1 = bbox
    return image.crop((
        max(0, round(x0 * image.width / 1000)),
        max(0, round(y0 * image.height / 1000)),
        min(image.width, round(x1 * image.width / 1000)),
        min(image.height, round(y1 * image.height / 1000)),
    ))

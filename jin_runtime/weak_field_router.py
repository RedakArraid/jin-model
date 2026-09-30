from __future__ import annotations

import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

import fitz
import joblib
import numpy as np
import pytesseract
from PIL import Image
from sklearn.feature_extraction.text import HashingVectorizer
from scipy.special import expit

from jin_runtime.cell_intelligence import assign_span_to_cell, enrich_page_regions_with_cells
from jin_runtime.geometry_fields import STREET_TYPES, extract_geometry_suggestions
from jin_runtime.zone_intelligence import build_page_regions


def _norm(value: Any) -> str:
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    ).upper()
    return re.sub(r"[^A-Z0-9]+", "", text)


def _shape(text: str) -> str:
    out: list[str] = []
    last = None
    for char in text:
        marker = "d" if char.isdigit() else "X" if char.isupper() else "x" if char.islower() else char
        if marker != last:
            out.append(marker)
            last = marker
    return "".join(out)[:16]


def _extract_first_page_lines(
    data: bytes,
    ocr_languages: str,
) -> tuple[list[list[dict[str, Any]]], str]:
    if not data.startswith(b"%PDF-"):
        raise ValueError("Input is not a PDF binary")

    with fitz.open(stream=data, filetype="pdf") as document:
        if not len(document):
            raise ValueError("PDF has no pages")
        page = document[0]
        rect = page.rect
        groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        native = page.get_text("words", sort=True)
        source = "native_pdf_text"

        if native:
            for raw in native:
                x0, y0, x1, y1, text, block, line, word = raw
                groups[(int(block), int(line))].append(
                    {
                        "text": str(text),
                        "norm": _norm(text),
                        "bbox": [float(x0), float(y0), float(x1), float(y1)],
                        "page_width": float(rect.width),
                        "page_height": float(rect.height),
                        "block": int(block),
                        "line": int(line),
                        "word": int(word),
                    }
                )
        else:
            source = "tesseract_ocr"
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(2.0, 2.0),
                colorspace=fitz.csGRAY,
                alpha=False,
            )
            image = Image.frombytes(
                "L", [pixmap.width, pixmap.height], pixmap.samples
            )
            data_ocr = pytesseract.image_to_data(
                image,
                lang=ocr_languages,
                output_type=pytesseract.Output.DICT,
                config="--psm 11",
            )
            scale_x = float(rect.width) / max(pixmap.width, 1)
            scale_y = float(rect.height) / max(pixmap.height, 1)
            texts = data_ocr.get("text", [])
            for index, raw_text in enumerate(texts):
                text = str(raw_text).strip()
                try:
                    confidence = float(data_ocr["conf"][index])
                except (TypeError, ValueError, KeyError):
                    confidence = -1.0
                if not text or confidence < 25:
                    continue
                left = float(data_ocr["left"][index]) * scale_x
                top = float(data_ocr["top"][index]) * scale_y
                width = float(data_ocr["width"][index]) * scale_x
                height = float(data_ocr["height"][index]) * scale_y
                block = int(data_ocr.get("block_num", [0] * len(texts))[index] or 0)
                line = int(data_ocr.get("line_num", [0] * len(texts))[index] or 0)
                word = int(data_ocr.get("word_num", list(range(len(texts))))[index] or index)
                groups[(block, line)].append(
                    {
                        "text": text,
                        "norm": _norm(text),
                        "bbox": [left, top, left + width, top + height],
                        "page_width": float(rect.width),
                        "page_height": float(rect.height),
                        "block": block,
                        "line": line,
                        "word": word,
                    }
                )

    lines: list[list[dict[str, Any]]] = []
    for _, tokens in sorted(
        groups.items(),
        key=lambda item: (
            min(token["bbox"][1] for token in item[1]),
            min(token["bbox"][0] for token in item[1]),
        ),
    ):
        tokens.sort(key=lambda token: token["bbox"][0])
        lines.append(tokens)
    return lines, source


def _context(tokens: list[dict[str, Any]], index: int) -> str:
    token = tokens[index]
    x0, y0, x1, y1 = token["bbox"]
    center_x = (x0 + x1) / 2 / max(token["page_width"], 1.0)
    center_y = (y0 + y1) / 2 / max(token["page_height"], 1.0)

    def at(position: int) -> str:
        if position < 0:
            return "<BOS>"
        if position >= len(tokens):
            return "<EOS>"
        return tokens[position]["norm"]

    return " ".join(
        [
            f"p2={at(index - 2)}",
            f"p1={at(index - 1)}",
            f"tok={token['norm']}",
            f"n1={at(index + 1)}",
            f"n2={at(index + 2)}",
            f"shape={_shape(token['text'])}",
            f"xb={min(9, int(center_x * 10))}",
            f"yb={min(9, int(center_y * 10))}",
            f"len={min(20, len(token['text']))}",
            "page=0",
        ]
    )


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


class _CompactSparseLinearModel:
    def __init__(self, bundle: dict[str, Any]) -> None:
        self.classes_ = np.asarray(bundle["classes"], dtype=object)
        self.intercept_ = np.asarray(bundle["intercept"], dtype=np.float32)
        self.weights = bundle["weights"]

    def decision_function(self, matrix):
        scores = np.tile(self.intercept_, (matrix.shape[0], 1)).astype(
            np.float32
        )
        for class_index, weights in enumerate(self.weights):
            indices = np.asarray(weights["indices"], dtype=np.int32)
            values = np.asarray(weights["values"], dtype=np.float32)
            scores[:, class_index] += np.asarray(
                matrix[:, indices].dot(values)
            ).reshape(-1)
        return scores

    def predict(self, matrix):
        scores = self.decision_function(matrix)
        return self.classes_[scores.argmax(axis=1)]

    def predict_proba(self, matrix):
        scores = self.decision_function(matrix)
        probabilities = expit(scores)
        totals = probabilities.sum(axis=1, keepdims=True)
        return np.divide(
            probabilities,
            totals,
            out=np.full_like(
                probabilities, 1.0 / max(len(self.classes_), 1)
            ),
            where=totals != 0,
        )


class WeakFieldRouter:
    """Weak-supervised first-page field suggester.

    It never replaces core JIN fields. All output is explicitly review-required.
    Native PDF words are preferred; Tesseract is a CPU fallback for image-only PDFs.
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        confidence_threshold: float | None = None,
    ) -> None:
        self.model_path = Path(
            model_path
            or os.getenv(
                "JIN_FIELD_ROUTER_MODEL",
                "/app/data/learning/jin-field-weak-router-v2-cpu.joblib",
            )
        )
        self.confidence_threshold = float(
            confidence_threshold
            if confidence_threshold is not None
            else os.getenv("JIN_FIELD_ROUTER_THRESHOLD", "0.80")
        )
        self.ocr_languages = os.getenv("JIN_OCR_LANGS", "fra+eng+deu")
        self.bundle: dict[str, Any] = {}
        self.vectorizer: HashingVectorizer | None = None
        self.model: Any | None = None
        self.reload()

    @property
    def loaded(self) -> bool:
        return bool(self.bundle and self.vectorizer is not None)

    def reload(self) -> None:
        self.bundle = {}
        self.vectorizer = None
        self.model = None
        if not self.model_path.exists():
            return
        try:
            bundle = joblib.load(self.model_path)
            self.bundle = bundle
            self.model = (
                _CompactSparseLinearModel(bundle)
                if bundle.get("format") == "sparse-linear-v1"
                else bundle.get("model")
            )
            if self.model is None:
                raise ValueError("weak field model missing predictor")
            self.vectorizer = HashingVectorizer(
                analyzer="char_wb",
                ngram_range=(2, 5),
                n_features=int(bundle["n_features"]),
                alternate_sign=False,
                norm="l2",
                lowercase=True,
            )
        except Exception:
            self.bundle = {}
            self.vectorizer = None

    def status(self) -> dict[str, Any]:
        metrics = self.bundle.get("metrics") or {}
        return {
            "loaded": self.loaded,
            "model_path": str(self.model_path),
            "version": self.bundle.get("version"),
            "weak_supervision": True,
            "requires_review": True,
            "confidence_threshold": self.confidence_threshold,
            "zone_intelligence_version": "spatial-zone-v1",
            "cell_intelligence_version": "cell-subzone-v1",
            "validation_token_accuracy_against_weak_labels": metrics.get(
                "validation_token_accuracy"
            ),
            "validation_macro_f1_against_weak_labels": metrics.get(
                "validation_macro_f1_all"
            ),
            "qualification": self.bundle.get(
                "qualification",
                "Agreement against deterministic weak labels only; not ground-truth field accuracy.",
            ),
        }

    @staticmethod
    def _nearest(
        items: list[dict[str, Any]],
        x: int,
        side: str | None = None,
        max_distance: int = 180,
    ) -> dict[str, Any] | None:
        candidates = []
        for item in items:
            if side == "left" and item["bbox"][2] > x:
                continue
            if side == "right" and item["bbox"][0] < x:
                continue
            center = (item["bbox"][0] + item["bbox"][2]) / 2
            distance = abs(center - x)
            if distance <= max_distance:
                candidates.append((distance, item))
        return min(candidates, key=lambda pair: pair[0])[1] if candidates else None

    def _address_candidates(
        self,
        spans: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_line: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for span in spans:
            by_line[int(span["line_index"])].append(span)

        addresses = []
        max_line = max(by_line.keys(), default=0)
        for line_index, items in sorted(by_line.items()):
            for street_type in [
                item for item in items if item["label"] == "ADDRESS_STREET_TYPE"
            ]:
                house = self._nearest(
                    [
                        item
                        for item in items
                        if item["label"] == "ADDRESS_HOUSE_NUMBER"
                    ],
                    street_type["bbox"][0],
                    "left",
                    120,
                )
                suffix = self._nearest(
                    [
                        item
                        for item in items
                        if item["label"] == "ADDRESS_HOUSE_NUMBER_SUFFIX"
                    ],
                    street_type["bbox"][0],
                    "left",
                    120,
                )
                street_name = self._nearest(
                    [
                        item
                        for item in items
                        if item["label"] == "ADDRESS_STREET_NAME"
                    ],
                    street_type["bbox"][2],
                    "right",
                    260,
                )
                if not street_name:
                    continue

                anchor = house["bbox"][0] if house else street_type["bbox"][0]
                postal_code = city = cedex = None
                extras: list[tuple[str, dict[str, Any]]] = []
                for next_line in range(line_index, min(line_index + 5, max_line + 1)):
                    line_items = by_line.get(next_line, [])
                    postal_candidates = [
                        item
                        for item in line_items
                        if item["label"] == "ADDRESS_POSTAL_CODE"
                        and abs(item["bbox"][0] - anchor) <= 160
                    ]
                    if postal_candidates and postal_code is None:
                        postal_code = min(
                            postal_candidates,
                            key=lambda item: abs(item["bbox"][0] - anchor),
                        )
                        city = self._nearest(
                            [
                                item
                                for item in line_items
                                if item["label"] == "ADDRESS_CITY"
                            ],
                            postal_code["bbox"][2],
                            "right",
                            220,
                        )
                        cedex = self._nearest(
                            [
                                item
                                for item in line_items
                                if item["label"] == "ADDRESS_CEDEX"
                            ],
                            (city or postal_code)["bbox"][2],
                            "right",
                            160,
                        )

                    for label, key in (
                        ("ADDRESS_BP", "postal_box"),
                        ("ADDRESS_CS", "cs"),
                        ("ADDRESS_TSA", "tsa"),
                    ):
                        candidates = [
                            item
                            for item in line_items
                            if item["label"] == label
                            and abs(item["bbox"][0] - anchor) <= 220
                        ]
                        if candidates:
                            extras.append(
                                (
                                    key,
                                    min(
                                        candidates,
                                        key=lambda item: abs(
                                            item["bbox"][0] - anchor
                                        ),
                                    ),
                                )
                            )

                components = {
                    "house_number": house["value"] if house else None,
                    "house_number_suffix": suffix["value"] if suffix else None,
                    "street_type": street_type["value"],
                    "street_name": street_name["value"],
                    "postal_code": postal_code["value"] if postal_code else None,
                    "city": city["value"] if city else None,
                    "cedex": cedex["value"] if cedex else None,
                }
                for key, item in extras:
                    components.setdefault(key, item["value"])
                components = {
                    key: value for key, value in components.items() if value
                }

                parts = [
                    " ".join(
                        value
                        for value in (
                            components.get("house_number"),
                            components.get("house_number_suffix"),
                            components.get("street_type"),
                            components.get("street_name"),
                        )
                        if value
                    )
                ]
                for key in ("postal_box", "cs", "tsa"):
                    if components.get(key):
                        parts.append(components[key])
                locality = " ".join(
                    value
                    for value in (
                        components.get("postal_code"),
                        components.get("city"),
                        components.get("cedex"),
                    )
                    if value
                )
                if locality:
                    parts.append(locality)

                used = [
                    item
                    for item in (
                        house,
                        suffix,
                        street_type,
                        street_name,
                        postal_code,
                        city,
                        cedex,
                    )
                    if item
                ] + [item for _, item in extras]
                addresses.append(
                    {
                        "components": components,
                        "formatted_address_suggestion": ", ".join(parts),
                        "confidence": sum(item["confidence"] for item in used)
                        / max(len(used), 1),
                        "requires_review": True,
                        "source_line": line_index,
                    }
                )
        return addresses

    def predict_bytes(self, data: bytes) -> dict[str, Any]:
        if not self.loaded or self.vectorizer is None:
            raise RuntimeError(
                f"Weak field router model not loaded: {self.model_path}"
            )

        lines, text_source = _extract_first_page_lines(
            data, self.ocr_languages
        )
        geometry = extract_geometry_suggestions(lines)
        spans: list[dict[str, Any]] = []

        for line_index, tokens in enumerate(lines):
            if not tokens:
                continue
            matrix = self.vectorizer.transform(
                [_context(tokens, index) for index in range(len(tokens))]
            )
            predictions = self.model.predict(matrix)
            probabilities = self.model.predict_proba(matrix)
            max_probability = probabilities.max(axis=1)

            raw_spans: list[dict[str, Any]] = []
            index = 0
            while index < len(tokens):
                label = str(predictions[index])
                confidence = float(max_probability[index])
                if label == "O" or confidence < self.confidence_threshold:
                    index += 1
                    continue

                end = index + 1
                confidences = [confidence]
                while (
                    end < len(tokens)
                    and str(predictions[end]) == label
                    and float(max_probability[end]) >= self.confidence_threshold
                ):
                    gap = tokens[end]["bbox"][0] - tokens[end - 1]["bbox"][2]
                    if gap > 0.04 * tokens[end]["page_width"]:
                        break
                    confidences.append(float(max_probability[end]))
                    end += 1

                selected = tokens[index:end]
                raw_spans.append(
                    {
                        "label": label,
                        "value": " ".join(token["text"] for token in selected),
                        "confidence": sum(confidences) / len(confidences),
                        "box": _union_bbox(selected),
                        "page_width": selected[0]["page_width"],
                        "page_height": selected[0]["page_height"],
                    }
                )
                index = end

            street_types = [
                item
                for item in raw_spans
                if item["label"] == "ADDRESS_STREET_TYPE"
                and _norm(item["value"]) in STREET_TYPES
            ]
            postal_codes = [
                item
                for item in raw_spans
                if item["label"] == "ADDRESS_POSTAL_CODE"
                and re.fullmatch(r"\\d{5}", item["value"].strip())
                and 1000 <= int(item["value"].strip()) <= 98999
            ]

            for item in raw_spans:
                label = item["label"]
                keep = True
                if label == "ADDRESS_STREET_TYPE":
                    keep = _norm(item["value"]) in STREET_TYPES
                elif label == "ADDRESS_POSTAL_CODE":
                    raw_postal = item["value"].strip()
                    keep = bool(
                        re.fullmatch(r"\\d{5}", raw_postal)
                        and 1000 <= int(raw_postal) <= 98999
                    )
                elif label in {
                    "ADDRESS_STREET_NAME",
                    "ADDRESS_HOUSE_NUMBER",
                    "ADDRESS_HOUSE_NUMBER_SUFFIX",
                }:
                    keep = any(
                        -0.10 * item["page_width"]
                        <= item["box"][0] - street["box"][2]
                        <= 0.08 * item["page_width"]
                        or -0.08 * item["page_width"]
                        <= street["box"][0] - item["box"][2]
                        <= 0.08 * item["page_width"]
                        for street in street_types
                    )
                elif label in {"ADDRESS_CITY", "ADDRESS_CEDEX"}:
                    keep = any(
                        -0.03 * item["page_width"]
                        <= item["box"][0] - postal["box"][2]
                        <= 0.18 * item["page_width"]
                        for postal in postal_codes
                    )
                if not keep:
                    continue

                spans.append(
                    {
                        "label": label,
                        "value": item["value"],
                        "confidence": item["confidence"],
                        "page": 1,
                        "line_index": line_index,
                        "bbox": _normalized_bbox(
                            item["box"],
                            item["page_width"],
                            item["page_height"],
                        ),
                    }
                )

        model_addresses = self._address_candidates(spans)
        geometry_addresses = geometry.get("address_candidates") or []
        geometry_fields = geometry.get("anchored_fields") or {}

        zone_geometry = {
            "address_candidates": geometry_addresses or model_addresses,
            "anchored_fields": geometry_fields,
        }
        page_regions = build_page_regions(data, lines, zone_geometry)
        page_regions = enrich_page_regions_with_cells(
            data, lines, zone_geometry, page_regions
        )

        address_zone_types = {"SUPPLIER", "SHIP_TO", "BILL_TO", "UNKNOWN"}
        constrained_spans = []
        for span in spans:
            label = str(span.get("label") or "")
            wanted_types = None
            if label.startswith("ADDRESS_"):
                wanted_types = address_zone_types
            elif label.startswith("TOTAL_"):
                wanted_types = {"TOTALS"}
            elif label.startswith("ORDER_"):
                wanted_types = {"ORDER_METADATA"}
            elif label.startswith("LINE_ITEM_"):
                wanted_types = {"LINE_ITEMS"}

            if wanted_types:
                center_x = (span["bbox"][0] + span["bbox"][2]) / 2
                center_y = (span["bbox"][1] + span["bbox"][3]) / 2
                candidates = [
                    region
                    for region in page_regions
                    if region.get("zone_type") in wanted_types
                    and region["search_bbox"][0] <= center_x <= region["search_bbox"][2]
                    and region["search_bbox"][1] <= center_y <= region["search_bbox"][3]
                ]
                if candidates:
                    region = min(
                        candidates,
                        key=lambda item: (
                            item["search_bbox"][2] - item["search_bbox"][0]
                        )
                        * (
                            item["search_bbox"][3] - item["search_bbox"][1]
                        ),
                    )
                    span["zone_id"] = region["zone_id"]
                    span["zone_type"] = region["zone_type"]
                    constrained_spans.append(span)
                elif not any(
                    region.get("zone_type") in wanted_types
                    for region in page_regions
                ):
                    constrained_spans.append(span)
            else:
                constrained_spans.append(span)
        spans = constrained_spans

        if geometry_addresses:
            trusted_postals = {
                str(item.get("components", {}).get("postal_code") or "")
                for item in geometry_addresses
                if item.get("components", {}).get("postal_code")
            }
            trusted_cities = {
                _norm(item.get("components", {}).get("city") or "")
                for item in geometry_addresses
                if item.get("components", {}).get("city")
            }
            spans = [
                span
                for span in spans
                if not (
                    span["label"] == "ADDRESS_POSTAL_CODE"
                    and span["value"] not in trusted_postals
                )
                and not (
                    span["label"] == "ADDRESS_CITY"
                    and _norm(span["value"]) not in trusted_cities
                )
            ]

        spans = [
            assign_span_to_cell(span, page_regions)
            for span in spans
        ]

        return {
            "model_version": self.bundle["version"],
            "weak_supervision": True,
            "requires_review": True,
            "text_source": text_source,
            "spans": spans,
            "anchored_fields": geometry_fields,
            "address_candidates": geometry_addresses or model_addresses,
            "model_address_candidates": model_addresses,
            "geometry_version": geometry.get("geometry_version"),
            "zone_intelligence_version": "spatial-zone-v1",
            "cell_intelligence_version": "cell-subzone-v1",
            "page_regions": page_regions,
        }

#!/usr/bin/env python3
"""Audit requested-delivery dates from a JIN review bundle.

The source scan is deliberately evidence-oriented: it only mines dates next to
explicit delivery/receipt labels, or from product-shaped rows below a labelled
line-item header.  The resulting candidates are compared with order-level and
line-level dates retained by ``audit_random_orders.py``.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import unicodedata
from typing import Any


DATE_RE = re.compile(
    r"(?<!\d)(?:[0-3]?\d[/.-](?:0?\d|1[0-2])[/.-](?:\d{2}|\d{4})|"
    r"(?:19|20)\d{2}[/.-](?:0?\d|1[0-2])[/.-][0-3]?\d)(?!\d)"
)
DELIVERY_LABEL_RE = re.compile(
    r"(?:date\s+(?:de\s+)?livraison(?:\s+(?:souhaitee|demandee|requise|estimee|prevue))?|"
    r"livraison\s+(?:souhaitee|demandee|requise|prevue)(?:\s+le)?|date\s+souhaitee|"
    r"livraison\s+par\s+(?:vos|nos)\s+soins\s+le|"
    r"date\s+(?:de\s+)?reception|reception\s+souhaitee|"
    r"a\s+livrer\s+le|livrer\s+(?:pour|le)|"
    r"requested\s+delivery\s+date|required\s+date|delivery\s+date|"
    r"lieferdatum|liefertermin)",
    re.I,
)
TABLE_TERMS = re.compile(
    r"\b(?:reference|article|code)\b.*\b(?:quantite|qte|qty)\b|"
    r"\b(?:quantite|qte|qty)\b.*\b(?:prix|montant|net|amount)\b",
    re.I,
)
PRODUCT_ROW_RE = re.compile(
    r"(?:\b[A-Z0-9._/-]*\d[A-Z0-9._/-]{4,}\b.*){1,2}"
    r"\b\d+(?:[,.]\d+)?\s*(?:PC|PCE|PCS|PIECE|PIECES|EA|UN|U|KG|BOX|L|M)\b",
    re.I,
)


def _fold(value: Any) -> str:
    text = "".join(
        char for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    )
    return " ".join(text.casefold().split())


def _normalize_date(value: str) -> str | None:
    raw = value.strip()
    for pattern in (
        "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
        "%d/%m/%y", "%d-%m-%y", "%d.%m.%y",
        "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
    ):
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def source_delivery_date_candidates(text: str) -> dict[str, Any]:
    lines = [" ".join(line.split()) for line in str(text or "").splitlines()]
    candidates: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    page = 1
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = re.fullmatch(r"---\s*PAGE\s+(\d+)\s*---", line, re.I)
        if marker:
            page = int(marker.group(1))
            index += 1
            continue
        folded = _fold(line)
        label = DELIVERY_LABEL_RE.search(folded)
        header_window = " ".join(_fold(value) for value in lines[index:index + 3])
        split_line_header = bool(
            TABLE_TERMS.search(folded)
            and any(token in header_window for token in ("reception", "livraison", "delivery"))
        )
        if not label and not split_line_header:
            index += 1
            continue
        line_scope = split_line_header or bool(TABLE_TERMS.search(header_window))
        signals.append({
            "page": page,
            "scope": "line" if line_scope else "order",
            "label": line,
        })
        limit = min(len(lines), index + (36 if line_scope else 4))
        order_candidate_found = False
        for probe in range(index, limit):
            evidence = lines[probe]
            evidence_folded = _fold(evidence)
            if probe > index and re.search(
                r"\b(?:total\s+(?:ht|net|ttc)|conditions\s+generales|siege\s+social)\b",
                evidence_folded,
            ):
                break
            if line_scope and probe > index + 2 and not PRODUCT_ROW_RE.search(evidence):
                continue
            # At order level, only dates following the delivery label are valid.
            # Some compact headers print the order date earlier on the same row.
            scan_text = evidence_folded
            if probe == index and label:
                scan_text = evidence_folded[label.end():]
            for match in DATE_RE.finditer(scan_text):
                normalized = _normalize_date(match.group(0))
                if not normalized:
                    continue
                candidates.append({
                    "value": normalized,
                    "raw_value": match.group(0),
                    "page": page,
                    "scope": "line" if line_scope else "order",
                    "label": line,
                    "source_text": evidence,
                })
                if not line_scope:
                    order_candidate_found = True
                    break
            if order_candidate_found:
                break
        index += 1

    unique = {}
    for candidate in candidates:
        key = (
            candidate["value"], candidate["page"], candidate["scope"],
            candidate["source_text"],
        )
        unique[key] = candidate
    return {"signals": signals, "candidates": list(unique.values())}


def _model_dates(item: dict[str, Any]) -> tuple[set[str], set[str]]:
    model = item.get("model") or {}
    order_dates = {
        str(model["requested_delivery_date"])
    } if model.get("requested_delivery_date") else set()
    line_dates = {
        str(value) for value in model.get("line_requested_delivery_dates") or []
        if value
    }
    if not line_dates:
        line_dates = {
            str(line.get("requested_delivery_date"))
            for line in model.get("lines") or []
            if line.get("requested_delivery_date")
        }
    return order_dates, line_dates


def audit_item(item: dict[str, Any]) -> dict[str, Any]:
    source = source_delivery_date_candidates(item.get("source_text") or "")
    source_order = {
        candidate["value"] for candidate in source["candidates"]
        if candidate["scope"] == "order"
    }
    source_lines = {
        candidate["value"] for candidate in source["candidates"]
        if candidate["scope"] == "line"
    }
    model_order, model_lines = _model_dates(item)
    source_all = source_order | source_lines
    model_all = model_order | model_lines
    if source_all and source_all == model_all:
        status = "SOURCE_MATCH"
    elif source_all and source_all & model_all:
        status = "PARTIAL_MATCH"
    elif source_all and not model_all:
        status = "MODEL_MISSING"
    elif source_all:
        status = "DATE_MISMATCH"
    elif source["signals"] and not model_all:
        status = "LABEL_WITHOUT_DATE_REVIEW"
    elif model_all:
        status = "MODEL_ONLY_REVIEW"
    else:
        status = "NO_DELIVERY_DATE_SIGNAL"
    return {
        "filename": item.get("filename"),
        "sha256": item.get("sha256"),
        "status": status,
        "source_order_dates": sorted(source_order),
        "source_line_dates": sorted(source_lines),
        "model_order_dates": sorted(model_order),
        "model_line_dates": sorted(model_lines),
        "signals": source["signals"],
        "candidates": source["candidates"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    bundle = json.loads(args.bundle.read_text(encoding="utf-8-sig"))
    items = bundle.get("items") if isinstance(bundle, dict) else None
    if not isinstance(items, list):
        raise SystemExit("Review bundle must contain an items list")
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    rows = [audit_item(item) for item in items]
    review = [row for row in rows if row["status"] not in {
        "SOURCE_MATCH", "NO_DELIVERY_DATE_SIGNAL",
    }]
    summary = {
        "documents": len(rows),
        "documents_with_source_signal": sum(bool(row["signals"]) for row in rows),
        "documents_with_source_dates": sum(
            bool(row["source_order_dates"] or row["source_line_dates"]) for row in rows
        ),
        "documents_with_model_dates": sum(
            bool(row["model_order_dates"] or row["model_line_dates"]) for row in rows
        ),
        "status_counts": dict(sorted(Counter(row["status"] for row in rows).items())),
        "review_count": len(review),
    }
    (output / "delivery_date_audit.json").write_text(
        json.dumps({"summary": summary, "items": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "delivery_date_review.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    with (output / "delivery_date_audit.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = [
            "filename", "sha256", "status", "source_order_dates", "source_line_dates",
            "model_order_dates", "model_line_dates", "signal_count", "candidate_count",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({
                key: " | ".join(row[key]) if isinstance(row.get(key), list) else row.get(key)
                for key in fields
                if key not in {"signal_count", "candidate_count"}
            } | {
                "signal_count": len(row["signals"]),
                "candidate_count": len(row["candidates"]),
            })
    (output / "delivery_date_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

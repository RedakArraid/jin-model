#!/usr/bin/env python3
"""Prepare source-checked, review-required Studio drafts for a PDF batch.

The extraction is only a proposal. This command never validates an annotation,
starts training, or replaces an existing Studio document with the same PDF SHA.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

import fitz

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jin_runtime.studio_preannotation import generate_studio_preannotation
from jin_runtime.studio_contract import normalize_studio_annotation
from jin_runtime.studio_store import StudioStore


LINE_PATH = re.compile(r"^order\.line_items\[(\d+)\]\.")
CHARGE_PATH = re.compile(r"^order\.additional_charges\[(\d+)\]\.")
PRICE_LABELS = {"LINE_ITEM_UNIT_PRICE", "LINE_ITEM_TOTAL"}
NUMERIC_LABELS = PRICE_LABELS | {"LINE_ITEM_QUANTITY"}


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _printed_numbers(text: str) -> list[Decimal]:
    values = []
    for token in re.findall(r"(?<!\d)(?:\d{1,3}(?:[ .]\d{3})+|\d+)(?:[,.]\d+)?", text):
        compact = token.replace(" ", "")
        if "," in compact and "." in compact:
            compact = compact.replace(".", "") if compact.rfind(",") > compact.rfind(".") else compact.replace(",", "")
        value = _decimal(compact)
        if value is not None:
            values.append(value)
    return values


def _numeric_words(page: fitz.Page, row: list[float], value: Any) -> list[fitz.Rect]:
    """Find the exact numeric substring, not an entire quantity/unit token."""
    target = _decimal(value)
    if target is None:
        return []
    matches: list[fitz.Rect] = []
    for word in page.get_text("words"):
        box = fitz.Rect(word[:4])
        centre_y = (box.y0 + box.y1) / 2
        if not row[1] - 1 <= centre_y <= row[3] + 1:
            continue
        token = str(word[4])
        found = re.match(r"^[+\-]?\d+(?:[,.]\d+)?", token)
        if not found or _decimal(found.group()) != target:
            continue
        prefix = found.group()
        # PyMuPDF search_for returns glyph-level boxes for a numeric substring.
        # This avoids framing the unit suffix in tokens such as "1.00p.".
        for fragment in page.search_for(prefix):
            if fragment.intersects(box) and abs(fragment.y0 - box.y0) < 3:
                if fragment not in matches:
                    matches.append(fragment)
                break
    return sorted(matches, key=lambda box: box.x0)


def _normalized(box: fitz.Rect, page: fitz.Page) -> list[int]:
    result = [
        round(1000 * box.x0 / page.rect.width),
        round(1000 * box.y0 / page.rect.height),
        round(1000 * box.x1 / page.rect.width),
        round(1000 * box.y1 / page.rect.height),
    ]
    return [max(0, min(1000, value)) for value in result]


def _line_field_boxes(pdf: fitz.Document, fields: list[dict[str, Any]], output: dict[str, Any]) -> int:
    """Recheck numeric boxes against the printed row and column positions."""
    order = output.get("order") or {}
    lines = order.get("line_items") or []
    added = 0
    for field in fields:
        if field.get("label") not in NUMERIC_LABELS:
            continue
        match = LINE_PATH.match(str(field.get("path") or ""))
        if not match or int(match.group(1)) >= len(lines):
            continue
        line = lines[int(match.group(1))]
        evidence = line.get("evidence") or {}
        page_number = int(evidence.get("page") or 0)
        row = evidence.get("bbox")
        if not (1 <= page_number <= len(pdf) and isinstance(row, list) and len(row) == 4):
            continue
        page = pdf[page_number - 1]
        old_evidence = field.pop("evidence", None)
        candidates = _numeric_words(page, row, field.get("extracted_value"))
        if not candidates:
            if old_evidence:
                field["comment"] = "Ancien cadre rejeté : valeur non retrouvée dans la ligne imprimée."
            continue
        label = field["label"]
        price = _decimal((line.get("pricing") or {}).get("unit_price"))
        total = _decimal((line.get("amounts") or {}).get("net"))
        box = None
        if label == "LINE_ITEM_QUANTITY":
            # A quantity must be to the left of the printed unit price/total.
            rightmost_price = min(
                (candidate.x0 for candidate in _numeric_words(page, row, price)),
                default=page.rect.width,
            )
            before_price = [candidate for candidate in candidates if candidate.x1 < rightmost_price]
            if len(before_price) == 1:
                box = before_price[0]
        elif label == "LINE_ITEM_UNIT_PRICE":
            if price == total:
                # Same number printed in two columns: the first is unit price.
                if len(candidates) == 2 and candidates[1].x0 - candidates[0].x1 > 5:
                    box = candidates[0]
            elif len(candidates) == 1:
                box = candidates[0]
        elif label == "LINE_ITEM_TOTAL":
            if price == total:
                if len(candidates) == 2 and candidates[1].x0 - candidates[0].x1 > 5:
                    box = candidates[1]
            elif len(candidates) == 1:
                box = candidates[0]
        if box is None:
            if old_evidence:
                field["comment"] = "Ancien cadre rejeté : colonne prix/montant ambiguë dans la ligne imprimée."
            continue
        normalized = _normalized(box, page)
        if normalized[0] >= normalized[2] or normalized[1] >= normalized[3]:
            continue
        field["evidence"] = {"page": page_number, "bbox": normalized}
        field["comment"] = "Cadre proposé dans la ligne imprimée ; à confirmer sur le PDF."
        added += 1
    return added


def _audit_critical_boxes(pdf: fitz.Document, fields: list[dict[str, Any]]) -> tuple[int, int]:
    """Drop visibly wrong boxes, preserving unreadable font maps for review."""
    removed = unreadable = 0
    critical = NUMERIC_LABELS | {"LINE_ITEM_REFERENCE", "ORDER_NUMBER"}
    for field in fields:
        evidence = field.get("evidence") or {}
        if field.get("label") not in critical or not evidence:
            continue
        page_number = int(evidence.get("page") or 0)
        if not 1 <= page_number <= len(pdf):
            continue
        page = pdf[page_number - 1]
        box = evidence["bbox"]
        rect = fitz.Rect(
            box[0] * page.rect.width / 1000,
            box[1] * page.rect.height / 1000,
            box[2] * page.rect.width / 1000,
            box[3] * page.rect.height / 1000,
        )
        words = [
            str(word[4]) for word in page.get_text("words")
            if fitz.Rect(word[:4]).intersects(rect)
        ]
        if not words:
            continue  # OCR-only page: the geometric proposal still needs review.
        text = " ".join(words)
        if sum("\ue000" <= char <= "\uf8ff" for char in text) > len(text) / 4:
            unreadable += 1
            field["comment"] = "Police PDF sans texte lisible : contrôler ce cadre visuellement."
            continue
        value = str(field.get("extracted_value") or "")
        if field["label"] in NUMERIC_LABELS:
            target = _decimal(value)
            supported = target is not None and target in _printed_numbers(text)
        else:
            compact = re.sub(r"[^\w]", "", text, flags=re.UNICODE).casefold()
            target = re.sub(r"[^\w]", "", value, flags=re.UNICODE).casefold()
            supported = bool(target) and target in compact
        if not supported:
            field.pop("evidence", None)
            field["comment"] = f"Cadre écarté : texte visible « {text[:90]} » incompatible avec la valeur proposée."
            removed += 1
    return removed, unreadable


def _make_draft(pdf_path: Path, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prepared = generate_studio_preannotation(pdf_path, payload)
    fields = prepared["fields"]
    output = payload.get("normalized_output") or payload
    with fitz.open(pdf_path) as pdf:
        numeric_added = _line_field_boxes(pdf, fields, output)
        critical_removed, unreadable_font_boxes = _audit_critical_boxes(pdf, fields)
        page_count = len(pdf)
    # A generated line ordinal, inferred country, or unprinted unit has no
    # source region to teach the model. Never turn these into a gold label.
    excluded = {"LINE_ITEM_NUMBER", "SHIP_TO_COUNTRY", "SHIP_TO_COUNTRY_CODE"}
    fields = [
        field for field in fields
        if field.get("evidence") or field.get("label") not in excluded
    ]
    # The extractor can expose both unit_price and net_unit_price as separate
    # paths. Studio intentionally permits only one semantic label per line.
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for field in fields:
        path = str(field.get("path") or "")
        group = LINE_PATH.match(path) or CHARGE_PATH.match(path)
        key = (field["label"], group.group(1) if group else "singleton")
        previous = deduped.get(key)
        if previous is None or (not previous.get("evidence") and field.get("evidence")):
            deduped[key] = field
    fields = list(deduped.values())
    for field in fields:
        field["status"] = "UNREVIEWED"
        evidence = field.get("evidence") or {}
        if evidence and not 1 <= int(evidence.get("page") or 0) <= page_count:
            raise ValueError(f"Invalid page for {field['path']}: {evidence}")
        if not evidence and not str(field.get("comment") or "").startswith("Cadre écarté"):
            field["comment"] = "Valeur proposée par JIN, cadre non fiabilisé : retrouver dans le PDF ou marquer absent."
    report = {
        "field_count": len(fields),
        "framed": sum(bool(field.get("evidence")) for field in fields),
        "unframed": sum(not field.get("evidence") for field in fields),
        "numeric_boxes_added": numeric_added,
        "critical_boxes_removed": critical_removed,
        "unreadable_font_boxes": unreadable_font_boxes,
        "labels": dict(Counter(field["label"] for field in fields)),
    }
    return fields, report


def _manifest_names(path: Path) -> dict[str, str]:
    names = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if item.get("local_pdf") and item.get("file_name"):
                names[str(item["local_pdf"])] = Path(str(item["file_name"])).name
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_dir", type=Path)
    parser.add_argument("--studio-dir", type=Path, default=Path("data/studio"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--refresh-machine-drafts", action="store_true",
        help="Refresh only this campaign's own latest machine DRAFT; never touch human work.",
    )
    args = parser.parse_args(argv)
    batch = args.batch_dir.resolve()
    names = _manifest_names(batch / "manifest.jsonl")
    filenames = json.loads((batch / "sample_files.json").read_text(encoding="utf-8"))
    if len(filenames) != len(set(filenames)):
        raise ValueError("Batch has duplicate file names")
    os.environ["JIN_STUDIO_DIR"] = str(args.studio_dir.resolve())
    store = StudioStore()
    existing = {item["pdfSha256"]: item for item in store.list_documents(limit=1000)}
    prepared = []
    seen_digests = set()
    for index, filename in enumerate(filenames, 1):
        pdf_path = batch / "pdf" / filename
        pdf_bytes = pdf_path.read_bytes()
        digest = hashlib.sha256(pdf_bytes).hexdigest()
        payload = json.loads((batch / "regression_after_fixes" / f"{index:03d}.json").read_text(encoding="utf-8"))
        extracted_sha = str(((payload.get("normalized_output") or payload).get("document") or {}).get("sha256") or "").lower()
        if digest != extracted_sha:
            raise ValueError(f"PDF/extraction SHA mismatch: {filename}")
        if digest in seen_digests:
            raise ValueError(f"Duplicate PDF SHA in selected batch: {filename}")
        seen_digests.add(digest)
        fields, report = _make_draft(pdf_path, payload)
        if not fields:
            raise ValueError(f"No annotation fields: {filename}")
        normalize_studio_annotation(
            {
                "status": "DRAFT",
                "reviewed_by": "jin-model-preannotation-rejected-batch",
                "field_annotations": fields,
            },
            document_id=f"jdoc-{digest[:32]}",
            document_sha256=digest,
            revision=1,
        )
        item = {
            "index": index,
            "filename": filename,
            "original_filename": names.get(filename, filename),
            "sha256": digest,
            "pdf_path": pdf_path,
            "pdf_bytes": pdf_bytes,
            "extraction": payload.get("normalized_output") or payload,
            "fields": fields,
            "report": report,
            "existing": existing.get(digest),
        }
        prepared.append(item)
        print(f"[{index:02d}/{len(filenames)}] {filename}: {report['framed']}/{report['field_count']} cadres", flush=True)
    report_dir = batch / "studio_preannotations"
    report_dir.mkdir(exist_ok=True)
    results = []
    for item in prepared:
        current = item["existing"]
        latest = (current or {}).get("latestAnnotation") or {}
        safe_refresh = bool(
            current
            and args.refresh_machine_drafts
            and current.get("status") == "DRAFT"
            and (current.get("source") or {}).get("source") == "production_rejected_review"
            and latest.get("reviewed_by") == "jin-model-preannotation-rejected-batch"
            and all(field.get("status") == "UNREVIEWED" for field in latest.get("field_annotations") or [])
        )
        if current is None and not args.dry_run:
            current = store.create_document(
                filename=f"REJET_PROD_{item['index']:02d}_{item['original_filename']}",
                pdf_bytes=item["pdf_bytes"],
                extraction=item["extraction"],
                source={
                    "source": "production_rejected_review",
                    "campaignId": batch.name,
                    "preannotationStatus": "REVIEW_REQUIRED",
                    "sourceFilename": item["original_filename"],
                },
            )
            store.save_annotation(current["documentId"], {
                "status": "DRAFT",
                "reviewed_by": "jin-model-preannotation-rejected-batch",
                "note": "Préannotation automatique à relire intégralement sur le PDF. Aucune valeur n'est validée pour l'apprentissage.",
                "field_annotations": item["fields"],
            })
            action = "CREATED"
        elif safe_refresh and not args.dry_run:
            store.save_annotation(current["documentId"], {
                "status": "DRAFT",
                "reviewed_by": "jin-model-preannotation-rejected-batch",
                "note": "Préannotation automatique à relire intégralement sur le PDF. Aucune valeur n'est validée pour l'apprentissage.",
                "field_annotations": item["fields"],
            })
            action = "REFRESHED"
        else:
            action = "WOULD_REFRESH" if safe_refresh else ("REUSED" if current else "WOULD_CREATE")
        result = {key: item[key] for key in ("index", "filename", "original_filename", "sha256", "report")}
        result.update({"action": action, "document_id": current["documentId"] if current else None})
        results.append(result)
        if action == "CREATED":
            print(f"  Studio {current['documentId']} créé en DRAFT", flush=True)
    summary = {
        "dry_run": args.dry_run,
        "count": len(results),
        "actions": dict(Counter(item["action"] for item in results)),
        "fields": sum(item["report"]["field_count"] for item in results),
        "framed": sum(item["report"]["framed"] for item in results),
        "numeric_boxes_added": sum(item["report"]["numeric_boxes_added"] for item in results),
        "documents": results,
    }
    report_path = report_dir / ("dry_run.json" if args.dry_run else "studio_import.json")
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if key != "documents"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

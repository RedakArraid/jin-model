#!/usr/bin/env python3
"""Build a source-backed annotation campaign for human review.

PDF binaries stay in their original folder. The campaign contains the exact
sample, full JIN responses, region annotations, a training manifest and a CSV
review queue. All generated labels remain explicitly review-required until a
human approves or corrects them.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any

import httpx

from training.annotations.bootstrap_from_extractions import annotate_one


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_sample(path: Path) -> list[str]:
    values = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(values, list) or not values:
        raise SystemExit(f"Sample must be a non-empty JSON list: {path}")
    return [str(value).replace("\\", "/") for value in values]


def _document_type(payload: dict[str, Any]) -> str:
    normalized = payload.get("normalized_output") or {}
    document = normalized.get("document") or payload.get("document") or {}
    return str(
        document.get("type")
        or document.get("primary_document_type")
        or document.get("detected_document_type")
        or "unknown"
    )


def _review_readme(source_root: Path, count: int) -> str:
    return f"""JIN annotation review campaign

Source folder: {source_root}
Documents: {count}

Every annotation starts as WEAK_LABELS_REVIEW_REQUIRED. It must not enter the
ground-truth training split until review_status is changed from PENDING and all
incorrect/missing regions are corrected.

Review order:
1. Open review_queue.csv.
2. Open the source PDF named by source_relative_path.
3. Compare it with annotations/<index>-<sha>.json.
4. Correct label, value, page or bbox when needed.
5. Set annotation_status to HUMAN_REVIEWED and requires_review to false only
   after checking the whole document, including absent fields and extra pages.
6. Record the outcome and notes in review_queue.csv.

PDFs are not copied into this campaign and are not committed to Git.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--sample-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--api", default="http://localhost:8080/api/extract")
    parser.add_argument("--max-ambiguity", type=int, default=3)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    source_root = args.source_root.resolve()
    output = args.output_dir.resolve()
    extraction_dir = output / "extractions"
    annotation_dir = output / "annotations"
    extraction_dir.mkdir(parents=True, exist_ok=True)
    annotation_dir.mkdir(parents=True, exist_ok=True)

    sample = _read_sample(args.sample_file)
    records = []
    errors = []
    with httpx.Client(timeout=300) as client:
        for index, relative in enumerate(sample, 1):
            pdf_path = (source_root / Path(*relative.split("/"))).resolve()
            try:
                pdf_path.relative_to(source_root)
            except ValueError as exc:
                raise SystemExit(f"Sample path escapes source root: {relative}") from exc
            if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
                raise SystemExit(f"Sample PDF not found: {relative}")

            digest = _sha256(pdf_path)
            stem = f"{index:03d}-{digest[:12]}"
            extraction_path = extraction_dir / f"{stem}.json"
            annotation_path = annotation_dir / f"{stem}.json"
            started = time.perf_counter()
            try:
                if extraction_path.is_file() and not args.force:
                    payload = json.loads(extraction_path.read_text(encoding="utf-8"))
                    extraction_mode = "REUSED"
                else:
                    with pdf_path.open("rb") as handle:
                        response = client.post(
                            args.api,
                            files={"file": (pdf_path.name, handle, "application/pdf")},
                        )
                    response.raise_for_status()
                    payload = response.json()
                    extraction_path.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    extraction_mode = "EXTRACTED"

                annotation, report = annotate_one(pdf_path, payload, args.max_ambiguity)
                annotation.update(
                    {
                        "document_id": stem,
                        "source_relative_path": relative,
                        "source_sha256": digest,
                        "document_labels": {
                            "document_type": _document_type(payload),
                            "requires_review": True,
                        },
                        "review": {
                            "status": "PENDING",
                            "reviewer": None,
                            "reviewed_at": None,
                            "notes": "",
                        },
                        "generation_report": {
                            "candidate_fields": report["candidate_fields"],
                            "matched_fields": report["matched_fields"],
                            "ambiguous_fields": report["ambiguous_fields"],
                            "unmatched_fields": report["unmatched_fields"],
                            "unresolved": report["unmatched"],
                        },
                    }
                )
                annotation_path.write_text(
                    json.dumps(annotation, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                region_count = sum(len(page.get("regions") or []) for page in annotation["pages"])
                record = {
                    "index": index,
                    "document_id": stem,
                    "customer_family": relative.split("/", 1)[0],
                    "source_relative_path": relative,
                    "sha256": digest,
                    "document_type": _document_type(payload),
                    "annotation_status": annotation["annotation_status"],
                    "region_count": region_count,
                    "candidate_fields": report["candidate_fields"],
                    "matched_fields": report["matched_fields"],
                    "ambiguous_fields": report["ambiguous_fields"],
                    "unmatched_fields": report["unmatched_fields"],
                    "review_status": "PENDING",
                    "reviewer_notes": "",
                    "extraction_mode": extraction_mode,
                    "seconds": round(time.perf_counter() - started, 3),
                    "extraction": extraction_path.relative_to(output).as_posix(),
                    "annotation": annotation_path.relative_to(output).as_posix(),
                }
                records.append(record)
                print(
                    f"[{index}/{len(sample)}] {pdf_path.name}: {extraction_mode}, "
                    f"{region_count} regions, {report['unmatched_fields']} unmatched",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception as exc:  # Keep the rest of the review batch usable.
                errors.append(
                    {
                        "index": index,
                        "source_relative_path": relative,
                        "sha256": digest,
                        "error": type(exc).__name__,
                        "detail": str(exc),
                    }
                )
                print(
                    f"[{index}/{len(sample)}] {pdf_path.name}: ERROR {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

    queue_path = output / "review_queue.csv"
    if records:
        with queue_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)

    manifest_path = output / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(
                    {
                        "document_id": record["document_id"],
                        "pdf": str((source_root / Path(*record["source_relative_path"].split("/"))).resolve()),
                        "annotation": str((output / record["annotation"]).resolve()),
                        "source": "esker_varied_review_campaign",
                        "split_group": record["customer_family"],
                        "review_status": record["review_status"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    summary = {
        "version": "jin-annotation-review-campaign-v1",
        "source_root": str(source_root),
        "requested": len(sample),
        "completed": len(records),
        "errors": errors,
        "regions": sum(record["region_count"] for record in records),
        "matched_fields": sum(record["matched_fields"] for record in records),
        "ambiguous_fields": sum(record["ambiguous_fields"] for record in records),
        "unmatched_fields": sum(record["unmatched_fields"] for record in records),
        "review_status": "PENDING_HUMAN_REVIEW",
    }
    (output / "campaign_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "README.txt").write_text(
        _review_readme(source_root, len(records)), encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())

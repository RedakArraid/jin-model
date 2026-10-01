#!/usr/bin/env python3
"""Prepare and resume a complete folder audit in deterministic batches."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_list(path: Path) -> list[str]:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise SystemExit(f"Expected a JSON list: {path}")
    return [str(value).replace("\\", "/") for value in values]


def _completed(batch_dir: Path, planned: list[str]) -> bool:
    bundle_path = batch_dir / "review_bundle.json"
    if not bundle_path.is_file():
        return False
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    items = bundle.get("items") or []
    return (
        len(items) == len(planned)
        and [item.get("filename") for item in items] == planned
        and not any(item.get("error") for item in items)
    )


def _prepare(
    folder: Path,
    output: Path,
    exclusions: list[Path],
    batch_size: int,
    seed: int,
) -> dict[str, Any]:
    candidates = sorted(
        path for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() == ".pdf"
    )
    by_relative = {path.relative_to(folder).as_posix(): path for path in candidates}
    excluded_hashes = set()
    excluded_files = []
    for sample in exclusions:
        for relative in _read_list(sample):
            path = by_relative.get(relative)
            if path is None:
                raise SystemExit(f"Excluded PDF no longer exists: {relative}")
            excluded_hashes.add(_sha256(path))
            excluded_files.append(relative)

    unique: dict[str, Path] = {}
    duplicates = []
    for path in candidates:
        digest = _sha256(path)
        if digest in unique:
            duplicates.append({
                "duplicate": path.relative_to(folder).as_posix(),
                "kept": unique[digest].relative_to(folder).as_posix(),
                "sha256": digest,
            })
            continue
        unique[digest] = path

    remaining = [
        (digest, path) for digest, path in unique.items()
        if digest not in excluded_hashes
    ]
    random.Random(seed).shuffle(remaining)
    batches = []
    for offset in range(0, len(remaining), batch_size):
        batch_number = len(batches) + 1
        records = remaining[offset : offset + batch_size]
        relative_files = [path.relative_to(folder).as_posix() for _, path in records]
        batch_dir = output / "batches" / f"batch-{batch_number:03d}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        planned_path = batch_dir / "sample_files.planned.json"
        planned_path.write_text(
            json.dumps(relative_files, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        batches.append({
            "batch": batch_number,
            "count": len(records),
            "sample_file": planned_path.relative_to(output).as_posix(),
            "output_dir": batch_dir.relative_to(output).as_posix(),
            "first_file": relative_files[0],
            "last_file": relative_files[-1],
            "sha256": [digest for digest, _ in records],
        })

    manifest = {
        "version": "jin-full-folder-audit-v1",
        "source_folder": str(folder),
        "seed": seed,
        "batch_size": batch_size,
        "source_pdf_files": len(candidates),
        "unique_pdf_contents": len(unique),
        "duplicate_contents": duplicates,
        "previously_audited_files": len(excluded_files),
        "previously_audited_unique_contents": len(excluded_hashes),
        "remaining_unique_contents": len(remaining),
        "batch_count": len(batches),
        "batches": batches,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "campaign_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--exclude-sample", action="append", type=Path, default=[])
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261001)
    parser.add_argument("--api", default="http://localhost:8080/api/extract")
    parser.add_argument("--start-batch", type=int, default=1)
    parser.add_argument("--end-batch", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--force-reextract", action="store_true",
        help="Re-run every selected PDF even when its batch already completed.",
    )
    parser.add_argument(
        "--reuse-manifest",
        action="store_true",
        help="Reuse the existing campaign manifest and planned batch files.",
    )
    args = parser.parse_args()
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")

    folder = args.folder.resolve()
    output = args.output_dir.resolve()
    manifest_path = output / "campaign_manifest.json"
    if args.reuse_manifest:
        if not manifest_path.is_file():
            raise SystemExit(f"Campaign manifest not found: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if Path(manifest["source_folder"]).resolve() != folder:
            raise SystemExit("Existing manifest source folder does not match")
        if int(manifest["batch_size"]) != args.batch_size or int(manifest["seed"]) != args.seed:
            raise SystemExit("Existing manifest batch size or seed does not match")
    else:
        manifest = _prepare(
            folder,
            output,
            [path.resolve() for path in args.exclude_sample],
            args.batch_size,
            args.seed,
        )
    print(json.dumps({
        key: manifest[key]
        for key in (
            "source_pdf_files", "unique_pdf_contents", "previously_audited_unique_contents",
            "remaining_unique_contents", "batch_count",
        )
    }, ensure_ascii=False), flush=True)
    if args.prepare_only:
        return 0

    end_batch = args.end_batch or int(manifest["batch_count"])
    selected_batches = [
        item for item in manifest["batches"]
        if args.start_batch <= int(item["batch"]) <= end_batch
    ]
    for position, batch in enumerate(selected_batches, 1):
        batch_number = int(batch["batch"])
        batch_dir = output / batch["output_dir"]
        planned_path = output / batch["sample_file"]
        planned = _read_list(planned_path)
        if not args.force_reextract and _completed(batch_dir, planned):
            print(
                f"[batch {batch_number:03d}] already complete ({len(planned)} PDFs)",
                flush=True,
            )
            continue
        print(
            f"[campaign {position}/{len(selected_batches)}] batch {batch_number:03d} "
            f"({len(planned)} PDFs)",
            flush=True,
        )
        command = [
            sys.executable,
            str(Path(__file__).with_name("audit_random_orders.py")),
            str(folder),
            "--count", str(len(planned)),
            "--seed", str(args.seed + batch_number),
            "--api", args.api,
            "--sample-file", str(planned_path),
            "--output-dir", str(batch_dir),
        ]
        if args.force_reextract:
            command.append("--force-reextract")
        completed = subprocess.run(command, check=False)
        if completed.returncode:
            raise SystemExit(
                f"Batch {batch_number:03d} failed with exit code {completed.returncode}"
            )

    print(json.dumps({
        "status": "extraction_complete",
        "batches": len(selected_batches),
        "first_batch": args.start_batch,
        "last_batch": end_batch,
    }), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

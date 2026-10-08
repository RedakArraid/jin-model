#!/usr/bin/env python3
from __future__ import annotations

"""Build a privacy-safe aggregate profile from a material master CSV."""

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core_overrides"))
from uda.material_references import normalize_reference, reference_signature  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--delimiter", default=";")
    parser.add_argument("--reference-column", default="MATNR")
    parser.add_argument("--description-column", default="MAKTX")
    parser.add_argument("--minimum-prefix-support", type=int, default=2)
    args = parser.parse_args()

    raw_bytes = args.source.read_bytes()
    lengths: Counter[str] = Counter()
    signatures: Counter[str] = Counter()
    prefixes: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: {"1": Counter(), "2": Counter(), "3": Counter()}
    )
    rows = 0
    invalid = 0
    duplicate_references = 0
    description_rows = 0
    seen: set[str] = set()
    with args.source.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter=args.delimiter)
        if not reader.fieldnames or args.reference_column not in reader.fieldnames:
            raise SystemExit(
                f"Missing {args.reference_column!r} column; found {reader.fieldnames!r}"
            )
        for row in reader:
            reference = normalize_reference(row.get(args.reference_column))
            if not reference:
                invalid += 1
                continue
            if reference in seen:
                duplicate_references += 1
                continue
            seen.add(reference)
            rows += 1
            description_rows += bool(str(row.get(args.description_column) or "").strip())
            length_key = str(len(reference))
            lengths[length_key] += 1
            signatures[reference_signature(reference)] += 1
            for size in (1, 2, 3):
                if len(reference) >= size:
                    prefixes[length_key][str(size)][reference[:size]] += 1

    def retained(counter: Counter[str]) -> dict[str, int]:
        return {
            key: count
            for key, count in sorted(counter.items())
            if count >= args.minimum_prefix_support
        }

    payload = {
        "schema_version": 1,
        "profile_id": "jin-material-reference-shapes-10564-v1",
        "algorithm": "aggregate-length-signature-prefix-v1",
        "source_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "row_count": rows,
        "unique_reference_count": len(seen),
        "duplicate_reference_count": duplicate_references,
        "invalid_reference_count": invalid,
        "description_row_count": description_rows,
        "minimum_prefix_support": args.minimum_prefix_support,
        "safety": {
            "minimum_numeric_length": 7,
            "short_numeric_shapes_are_diagnostic_only": True,
        },
        "length_counts": dict(sorted(lengths.items(), key=lambda item: int(item[0]))),
        "signature_counts": dict(sorted(signatures.items())),
        "prefix_counts_by_length": {
            length: {
                size: retained(counter)
                for size, counter in size_counters.items()
            }
            for length, size_counters in sorted(prefixes.items(), key=lambda item: int(item[0]))
        },
        "privacy": {
            "source_rows_included": False,
            "descriptions_included": False,
            "exact_references_included": False,
            "aggregate_prefix_minimum_support": args.minimum_prefix_support,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(args.output.resolve()),
        "rows": rows,
        "unique_references": len(seen),
        "signatures": len(signatures),
        "source_sha256": payload["source_sha256"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

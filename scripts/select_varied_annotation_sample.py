#!/usr/bin/env python3
"""Select a deterministic, varied PDF sample for an annotation campaign."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import sys
from typing import Iterable

import fitz


@dataclass(frozen=True)
class Candidate:
    relative_path: str
    family: str
    page_count: int
    text_mode: str
    page_bucket: str
    rank: str

    @property
    def stratum(self) -> str:
        return f"{self.text_mode}:{self.page_bucket}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _page_bucket(page_count: int) -> str:
    if page_count <= 1:
        return "1"
    if page_count <= 3:
        return "2-3"
    return "4+"


def inspect_pdf(root: Path, path: Path, seed: str) -> Candidate:
    relative = path.relative_to(root).as_posix()
    with fitz.open(path) as document:
        page_count = len(document)
        native_characters = sum(
            len(document[index].get_text("text").strip())
            for index in range(min(page_count, 3))
        )
    family = relative.split("/", 1)[0] if "/" in relative else "_root"
    rank = hashlib.sha256(f"{seed}\0{relative}".encode("utf-8")).hexdigest()
    return Candidate(
        relative_path=relative,
        family=family,
        page_count=page_count,
        text_mode="scan_or_low_text" if native_characters < 80 else "native_text",
        page_bucket=_page_bucket(page_count),
        rank=rank,
    )


def choose_varied(
    candidates: Iterable[Candidate],
    count: int,
    *,
    root: Path | None = None,
) -> list[Candidate]:
    """Greedily balance family and layout stratum with deterministic ties."""
    remaining = list(candidates)
    selected: list[Candidate] = []
    family_counts: Counter[str] = Counter()
    stratum_counts: Counter[str] = Counter()
    digests: set[str] = set()

    while remaining and len(selected) < count:
        remaining.sort(
            key=lambda item: (
                family_counts[item.family],
                stratum_counts[item.stratum],
                item.rank,
            )
        )
        candidate = remaining.pop(0)
        if root is not None:
            digest = _sha256(root / Path(*candidate.relative_path.split("/")))
            if digest in digests:
                continue
            digests.add(digest)
        selected.append(candidate)
        family_counts[candidate.family] += 1
        stratum_counts[candidate.stratum] += 1
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--seed", default="jin-annotation-v1")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    if args.count < 1:
        parser.error("--count must be greater than zero")

    root = args.source_root.resolve()
    paths = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() == ".pdf"
    )
    candidates: list[Candidate] = []
    errors: list[dict[str, str]] = []
    for position, path in enumerate(paths, 1):
        try:
            candidates.append(inspect_pdf(root, path, args.seed))
        except Exception as exc:
            errors.append(
                {
                    "relative_path": path.relative_to(root).as_posix(),
                    "error": type(exc).__name__,
                    "detail": str(exc),
                }
            )
        if position % 250 == 0:
            print(f"Inspected {position}/{len(paths)} PDFs", file=sys.stderr, flush=True)

    selected = choose_varied(candidates, args.count, root=root)
    if len(selected) < args.count:
        raise SystemExit(
            f"Only {len(selected)} unique valid PDFs available; requested {args.count}."
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps([item.relative_path for item in selected], ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    summary_path = args.summary or args.output.with_name("selection_summary.json")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "source_root": str(root),
        "seed": args.seed,
        "pdfs_discovered": len(paths),
        "pdfs_valid": len(candidates),
        "selected": len(selected),
        "families": dict(sorted(Counter(item.family for item in selected).items())),
        "strata": dict(sorted(Counter(item.stratum for item in selected).items())),
        "errors": errors,
        "documents": [asdict(item) for item in selected],
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "selected": len(selected),
                "families": len(summary["families"]),
                "strata": summary["strata"],
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

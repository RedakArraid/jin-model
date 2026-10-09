from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from .common import empty_common_document, ground_truth_review_status


def _selected_pdfs(
    root: Path,
    filenames: list[str] | None,
    limit: int | None,
) -> list[Path]:
    if filenames:
        paths = [root / name for name in filenames]
    else:
        paths = sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() == ".pdf"
        )
    paths = [path for path in paths if path.exists()]
    return paths[:limit] if limit else paths


def _draft_document() -> dict[str, Any]:
    document = empty_common_document()
    return {
        "_review": {
            "status": "needs_review",
            "reviewer": None,
            "reviewed_at": None,
            "notes": None,
        },
        **document,
    }


def _load_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": "jin-open-weight-ground-truth-v1",
            "documents": {},
        }
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("Ground truth file must contain one JSON object.")
    documents = payload.get("documents")
    if not isinstance(documents, dict):
        raise ValueError("Ground truth file must contain a documents object.")
    payload.setdefault("schema_version", "jin-open-weight-ground-truth-v1")
    return payload


def _non_empty_leaf_count(value: Any) -> int:
    if isinstance(value, dict):
        return sum(
            _non_empty_leaf_count(child)
            for key, child in value.items()
            if not str(key).startswith("_")
        )
    if isinstance(value, list):
        return sum(_non_empty_leaf_count(child) for child in value)
    return int(value not in (None, ""))


def _write_review_csv(path: Path, documents: dict[str, Any]) -> None:
    rows = []
    for filename, document in sorted(documents.items()):
        review = document.get("_review") if isinstance(document, dict) else {}
        review = review if isinstance(review, dict) else {}
        rows.append({
            "filename": filename,
            "status": ground_truth_review_status(document),
            "reviewer": review.get("reviewer"),
            "reviewed_at": review.get("reviewed_at"),
            "non_empty_leaf_fields": _non_empty_leaf_count(document),
            "notes": review.get("notes"),
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "filename",
                "status",
                "reviewer",
                "reviewed_at",
                "non_empty_leaf_fields",
                "notes",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    pdf_root = args.pdf_dir.resolve()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    filenames = None
    if args.file_list:
        filenames = json.loads(args.file_list.read_text(encoding="utf-8-sig"))
        if not isinstance(filenames, list):
            raise ValueError("--file-list must contain a JSON array of relative PDF paths.")

    pdfs = _selected_pdfs(pdf_root, filenames, args.limit)
    payload = _load_payload(output)
    documents = payload["documents"]

    created = 0
    preserved = 0
    for pdf_path in pdfs:
        relative = pdf_path.relative_to(pdf_root).as_posix()
        if relative in documents:
            preserved += 1
            continue
        documents[relative] = _draft_document()
        created += 1

    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    review_csv = args.review_csv
    if review_csv is None:
        review_csv = output.with_name(output.stem + "_review.csv")
    _write_review_csv(review_csv.resolve(), documents)

    return {
        "pdf_root": str(pdf_root),
        "output": str(output),
        "review_csv": str(review_csv.resolve()),
        "selected_documents": len(pdfs),
        "created_documents": created,
        "preserved_documents": preserved,
        "total_documents": len(documents),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a human-review ground-truth scaffold for the open-weight benchmark. "
            "Machine predictions are never copied into truth automatically."
        )
    )
    parser.add_argument("--pdf-dir", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/open_weight_comparison/ground_truth.json"),
    )
    parser.add_argument("--file-list", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--review-csv", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = prepare(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

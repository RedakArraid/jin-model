from __future__ import annotations

import argparse
import csv
import gc
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import DEFAULT_MODELS, available_model_names, create_adapter
from .common import (
    ground_truth_is_reviewed,
    ground_truth_review_status,
    load_ground_truth,
)
from .decision import (
    build_field_comparison_rows,
    build_head_to_head_rows,
    head_to_head_summary,
    render_decision_report,
)
from .metrics import aggregate_model, evaluate_extraction
from .resources import measured


BASELINE_MODEL = "jin"
JIN_DELTA_METRICS = (
    "field_exact_match",
    "field_token_f1",
    "missing_field_rate",
    "hallucination_rate",
    "line_item_reference_recall",
    "latency_seconds_mean",
    "peak_rss_mb_max",
)


def _json_default(value: Any):
    if hasattr(value, "item"):
        return value.item()
    return str(value)


def _selected_pdfs(
    root: Path,
    truth: dict[str, Any],
    filenames: list[str] | None,
    limit: int | None,
) -> list[Path]:
    if filenames:
        paths = [root / name for name in filenames]
    elif truth:
        paths = [
            root / name
            for name, document in truth.items()
            if ground_truth_is_reviewed(document)
        ]
    else:
        paths = sorted(
            path for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() == ".pdf"
        )
    paths = [path for path in paths if path.exists()]
    return paths[:limit] if limit else paths


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                keys.append(key)
                seen.add(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def _document_csv_row(row: dict[str, Any]) -> dict[str, Any]:
    metrics = row.get("metrics") or {}
    resources = row.get("resources") or {}
    return {
        "model": row.get("model"),
        "track": row.get("track"),
        "filename": row.get("filename"),
        "error": row.get("error"),
        "json_valid": row.get("json_valid"),
        "ground_truth_review_status": row.get("ground_truth_review_status"),
        "field_exact_match": metrics.get("field_exact_match"),
        "field_token_f1": metrics.get("field_token_f1"),
        "missing_field_rate": metrics.get("missing_field_rate"),
        "hallucination_rate": metrics.get("hallucination_rate"),
        "line_item_reference_recall": metrics.get("line_item_reference_recall"),
        "mean_field_bbox_iou": metrics.get("mean_field_bbox_iou"),
        "zone_type_accuracy": metrics.get("zone_type_accuracy"),
        "mean_zone_bbox_iou": metrics.get("mean_zone_bbox_iou"),
        "latency_seconds": resources.get("latency_seconds"),
        "peak_rss_mb": resources.get("peak_rss_mb"),
    }


def _summary_csv_row(
    name: str,
    track: str,
    summary: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "model": name,
        "track": track,
        "model_id": metadata.get("model_id"),
        "device": metadata.get("device"),
        **summary,
    }


def _attach_jin_comparison(
    report: dict[str, Any],
    summary_rows: list[dict[str, Any]],
) -> None:
    """Attach explicit deltas against JIN for direct-IE models."""
    jin_report = report.get("models", {}).get(BASELINE_MODEL) or {}
    jin_summary = jin_report.get("summary") or {}
    if not jin_summary or int(jin_summary.get("completed") or 0) == 0:
        report["baseline"] = {
            "model": BASELINE_MODEL,
            "available": False,
            "qualification": (
                "JIN was not executed successfully in this campaign, "
                "so no delta-vs-JIN metrics are reported."
            ),
        }
        return

    report["baseline"] = {
        "model": BASELINE_MODEL,
        "available": True,
        "track": jin_report.get("track"),
        "summary": jin_summary,
    }

    for model_name, model_report in report.get("models", {}).items():
        if model_name == BASELINE_MODEL:
            model_report["comparison_vs_jin"] = {"is_baseline": True}
            continue
        if model_report.get("track") != "direct_ie":
            model_report["comparison_vs_jin"] = {
                "comparable": False,
                "reason": (
                    "Different benchmark track; zone-semantics scores must not "
                    "be compared with JIN direct-IE field scores."
                ),
            }
            continue
        summary = model_report.get("summary") or {}
        deltas: dict[str, float] = {}
        for metric in JIN_DELTA_METRICS:
            candidate = summary.get(metric)
            baseline = jin_summary.get(metric)
            if candidate is None or baseline is None:
                continue
            deltas[metric] = float(candidate) - float(baseline)
        model_report["comparison_vs_jin"] = {
            "comparable": True,
            "baseline_model": BASELINE_MODEL,
            "delta": deltas,
        }

    for row in summary_rows:
        row["baseline_model"] = BASELINE_MODEL
        if row.get("model") == BASELINE_MODEL:
            row["is_jin_baseline"] = True
            continue
        row["is_jin_baseline"] = False
        if row.get("track") != "direct_ie":
            row["comparison_vs_jin"] = "NOT_COMPARABLE_DIFFERENT_TRACK"
            continue
        row["comparison_vs_jin"] = "DIRECT_IE"
        for metric in JIN_DELTA_METRICS:
            candidate = row.get(metric)
            baseline = jin_summary.get(metric)
            if candidate is None or baseline is None:
                continue
            row[f"delta_vs_jin_{metric}"] = float(candidate) - float(baseline)


def _new_report(
    args: argparse.Namespace,
    pdf_root: Path,
    truth: dict[str, Any],
    pdfs: list[Path],
    *,
    model_process_isolation: bool,
) -> dict[str, Any]:
    return {
        "schema_version": "jin-open-weight-ie-benchmark-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "device": args.device,
            "model_process_isolation": model_process_isolation,
        },
        "baseline_model": BASELINE_MODEL,
        "qualification": (
            "JIN is the benchmark baseline. Accuracy metrics are reported only "
            "where reviewed ground truth is supplied. EmbeddingGemma2 is a "
            "separate zone-semantics track, not a standalone generative extractor."
        ),
        "selection": {
            "pdf_root": str(pdf_root),
            "documents": [path.relative_to(pdf_root).as_posix() for path in pdfs],
            "ground_truth_documents": len(truth),
            "reviewed_ground_truth_documents": sum(
                1 for document in truth.values() if ground_truth_is_reviewed(document)
            ),
            "unreviewed_ground_truth_documents": sum(
                1 for document in truth.values() if not ground_truth_is_reviewed(document)
            ),
            "synthetic_ground_truth_documents": sum(
                1
                for document in truth.values()
                if isinstance(document, dict)
                and isinstance(document.get("_review"), dict)
                and bool(document["_review"].get("synthetic"))
            ),
        },
        "models": {},
    }


def _finalize_report(report: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    document_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []

    for model_name, model_report in report.get("models", {}).items():
        for row in model_report.get("documents") or []:
            document_rows.append(_document_csv_row(row))
        summary_rows.append(
            _summary_csv_row(
                model_name,
                model_report.get("track") or "",
                model_report.get("summary") or {},
                model_report.get("metadata") or {},
            )
        )

    _attach_jin_comparison(report, summary_rows)
    field_comparison_rows = build_field_comparison_rows(
        report,
        baseline_model=BASELINE_MODEL,
    )
    head_to_head_rows = build_head_to_head_rows(
        report,
        field_comparison_rows,
        baseline_model=BASELINE_MODEL,
    )
    report["head_to_head"] = head_to_head_summary(head_to_head_rows)

    (output_dir / "comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    _write_csv(output_dir / "documents.csv", document_rows)
    _write_csv(output_dir / "summary.csv", summary_rows)
    _write_csv(output_dir / "head_to_head.csv", head_to_head_rows)
    _write_csv(output_dir / "field_comparison.csv", field_comparison_rows)
    (output_dir / "decision_report.md").write_text(
        render_decision_report(
            report,
            head_to_head_rows,
            field_comparison_rows,
            baseline_model=BASELINE_MODEL,
        ),
        encoding="utf-8",
    )
    return report


def _worker_command(
    args: argparse.Namespace,
    model_name: str,
    output_dir: Path,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "benchmarks.open_weight_comparison.runner",
        "--pdf-dir",
        str(args.pdf_dir),
        "--output-dir",
        str(output_dir),
        "--models",
        model_name,
        "--device",
        args.device,
        "--max-pages",
        str(args.max_pages),
        "--max-new-tokens",
        str(args.max_new_tokens),
    ]
    if args.models_dir is not None:
        command.extend(["--models-dir", str(args.models_dir)])
    if args.ground_truth is not None:
        command.extend(["--ground-truth", str(args.ground_truth)])
    if args.file_list is not None:
        command.extend(["--file-list", str(args.file_list)])
    if args.limit is not None:
        command.extend(["--limit", str(args.limit)])
    return command


def run_isolated(args: argparse.Namespace) -> dict[str, Any]:
    """Run each CPU model in its own process so peak RSS is not cumulative."""
    pdf_root = args.pdf_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    truth = load_ground_truth(args.ground_truth)
    filenames = None
    if args.file_list:
        filenames = json.loads(args.file_list.read_text(encoding="utf-8-sig"))
    pdfs = _selected_pdfs(pdf_root, truth, filenames, args.limit)
    model_names = [item.strip() for item in args.models.split(",") if item.strip()]

    report = _new_report(
        args,
        pdf_root,
        truth,
        pdfs,
        model_process_isolation=True,
    )
    raw_root = output_dir / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="jin-open-weight-") as temp_root:
        for model_name in model_names:
            worker_dir = Path(temp_root) / model_name
            print(
                f"[{model_name}] isolated worker process...",
                file=sys.stderr,
                flush=True,
            )
            completed = subprocess.run(
                _worker_command(args, model_name, worker_dir),
                check=False,
            )
            comparison_path = worker_dir / "comparison.json"

            if comparison_path.exists():
                worker_report = json.loads(
                    comparison_path.read_text(encoding="utf-8")
                )
                model_report = (worker_report.get("models") or {}).get(model_name)
            else:
                model_report = None

            if model_report is None:
                model_report = {
                    "track": "unknown",
                    "documents": [],
                    "summary": {
                        "documents": len(pdfs),
                        "completed": 0,
                        "errors": len(pdfs),
                    },
                    "load_error": {
                        "type": "WorkerProcessError",
                        "message": (
                            f"Isolated worker exited with return code "
                            f"{completed.returncode} without a usable model report."
                        ),
                    },
                }

            model_report["worker_returncode"] = int(completed.returncode)
            report["models"][model_name] = model_report

            source_raw = worker_dir / "raw" / model_name
            if source_raw.exists():
                shutil.copytree(
                    source_raw,
                    raw_root / model_name,
                    dirs_exist_ok=True,
                )

    return _finalize_report(report, output_dir)


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Run one or more models in the current process."""
    pdf_root = args.pdf_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    truth = load_ground_truth(args.ground_truth)
    filenames = None
    if args.file_list:
        filenames = json.loads(args.file_list.read_text(encoding="utf-8-sig"))
    pdfs = _selected_pdfs(pdf_root, truth, filenames, args.limit)
    model_names = [item.strip() for item in args.models.split(",") if item.strip()]

    report = _new_report(
        args,
        pdf_root,
        truth,
        pdfs,
        model_process_isolation=False,
    )

    for model_name in model_names:
        adapter = create_adapter(
            model_name,
            device=args.device,
            models_dir=args.models_dir,
            max_pages=args.max_pages,
            max_new_tokens=args.max_new_tokens,
        )
        model_report: dict[str, Any] = {
            "track": adapter.track,
            "documents": [],
        }
        print(f"[{model_name}] loading...", file=sys.stderr, flush=True)

        try:
            with measured() as load_resources:
                adapter.load()
            model_report["load_resources"] = load_resources
            model_report["metadata"] = adapter.metadata()
        except Exception as exc:
            model_report["load_error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            model_report["summary"] = {
                "documents": len(pdfs),
                "completed": 0,
                "errors": len(pdfs),
            }
            report["models"][model_name] = model_report
            continue

        raw_dir = output_dir / "raw" / model_name
        raw_dir.mkdir(parents=True, exist_ok=True)

        for pdf_path in pdfs:
            relative = pdf_path.relative_to(pdf_root).as_posix()
            print(f"[{model_name}] {relative}", file=sys.stderr, flush=True)
            row: dict[str, Any] = {
                "model": model_name,
                "track": adapter.track,
                "filename": relative,
            }
            try:
                with measured() as resources:
                    result = adapter.extract(pdf_path)
                row["resources"] = resources
                row["json_valid"] = bool(result.get("json_valid", True))
                prediction = result.get("prediction") or {}
                row["prediction"] = prediction
                if relative in truth:
                    truth_document = truth[relative]
                    row["ground_truth_review_status"] = ground_truth_review_status(
                        truth_document
                    )
                    if ground_truth_is_reviewed(truth_document):
                        row["metrics"] = evaluate_extraction(
                            prediction,
                            truth_document,
                        )
                raw_path = raw_dir / (pdf_path.stem + ".json")
                raw_path.write_text(
                    json.dumps(
                        result,
                        ensure_ascii=False,
                        indent=2,
                        default=_json_default,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            except Exception as exc:
                row["error"] = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            model_report["documents"].append(row)

        model_report["summary"] = aggregate_model(model_report["documents"])
        report["models"][model_name] = model_report

        del adapter
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    return _finalize_report(report, output_dir)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare JIN with open-weight document information-extraction models."
        )
    )
    parser.add_argument("--pdf-dir", type=Path)
    parser.add_argument("--ground-truth", type=Path)
    parser.add_argument("--file-list", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help="Comma-separated model adapter names.",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=Path("data/learning"),
    )
    parser.add_argument(
        "--device",
        default="cpu",
        choices=("cpu", "cuda", "auto"),
    )
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--max-new-tokens", type=int, default=2048)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--list-models", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_models:
        print("\n".join(available_model_names()))
        return 0
    if args.pdf_dir is None or args.output_dir is None:
        parser.error(
            "--pdf-dir and --output-dir are required unless --list-models is used"
        )

    model_names = [item.strip() for item in args.models.split(",") if item.strip()]
    if args.device == "cpu" and len(model_names) > 1:
        report = run_isolated(args)
    else:
        report = run(args)

    completed = sum(
        model.get("summary", {}).get("completed", 0)
        for model in report["models"].values()
    )
    if BASELINE_MODEL in model_names and not (report.get("baseline") or {}).get("available"):
        return 3
    return 0 if completed else 2


if __name__ == "__main__":
    raise SystemExit(main())

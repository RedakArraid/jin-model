from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from .common import ground_truth_is_reviewed, load_ground_truth


FULL_JIN_CORE_CANDIDATES = (
    Path("model/uda/engine.py"),
    Path("uda/engine.py"),
)

JIN_MODEL_FILES = (
    "corpus_text_router.joblib",
    "jin-pdf-fusion-router-v3-cpu.joblib",
    "jin-field-weak-router-v2-cpu.joblib",
)

MODEL_RAM_GUIDANCE_GB = {
    "jin": 4.0,
    "qwen3_vl_2b": 12.0,
    "qwen3_vl_4b": 20.0,
    "embeddinggemma2_zone": 8.0,
    "granite_docling_258m": 6.0,
    "glm_ocr": 12.0,
    "paddleocr_vl_1_6": 12.0,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _memory_total_gb() -> float | None:
    if platform.system().lower() != "linux":
        return None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                return kb / 1024 / 1024
    except Exception:
        return None
    return None


def _hf_cache_has(model_id: str) -> bool | None:
    try:
        from huggingface_hub import scan_cache_dir
    except Exception:
        return None
    try:
        return any(repo.repo_id == model_id for repo in scan_cache_dir().repos)
    except Exception:
        return None




def _package_version(distribution: str) -> str | None:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


def _version_tuple(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    parts = []
    for raw in value.split("."):
        digits = "".join(char for char in raw if char.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _dependency_checks(model_names: list[str]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    needs_transformers = any(
        name in {
            "qwen3_vl_2b",
            "qwen3_vl_4b",
            "embeddinggemma2_zone",
            "granite_docling_258m",
            "glm_ocr",
            "paddleocr_vl_1_6",
        }
        for name in model_names
    )
    if needs_transformers:
        transformers_version = _package_version("transformers")
        ok = (
            transformers_version is not None
            and _version_tuple(transformers_version) >= (5, 19)
            and _version_tuple(transformers_version) < (6,)
        )
        checks.append({
            "name": "dependency:transformers",
            "ok": ok,
            "severity": "error",
            "version": transformers_version,
            "required": ">=5.19,<6",
            "detail": "ready" if ok else "install benchmarks/open_weight_comparison/requirements.txt",
        })

        torch_version = _package_version("torch")
        checks.append({
            "name": "dependency:torch",
            "ok": torch_version is not None,
            "severity": "error",
            "version": torch_version,
            "required": ">=2.5",
            "detail": "ready" if torch_version else "torch missing",
        })

    if "embeddinggemma2_zone" in model_names:
        st_version = _package_version("sentence-transformers")
        checks.append({
            "name": "dependency:sentence-transformers",
            "ok": st_version is not None,
            "severity": "error",
            "version": st_version,
            "required": ">=5.1,<6",
            "detail": "ready" if st_version else "sentence-transformers missing",
        })

    if model_names:
        fitz_available = importlib.util.find_spec("fitz") is not None
        checks.append({
            "name": "dependency:pymupdf",
            "ok": fitz_available,
            "severity": "error",
            "detail": "ready" if fitz_available else "pymupdf/fitz missing",
        })
    return checks




def _discover_private_inputs(root: Path) -> dict[str, list[str]]:
    parent = root.parent
    pdf_candidates: list[str] = []
    for candidate in [
        root / "corpus" / "validation",
        parent / "corpus" / "validation",
        *sorted(parent.glob("ARCHIVES_CDES_ESKER_PDF_*")),
    ]:
        try:
            if candidate.is_dir() and any(
                path.is_file() and path.suffix.lower() == ".pdf"
                for path in candidate.rglob("*")
            ):
                value = str(candidate.resolve())
                if value not in pdf_candidates:
                    pdf_candidates.append(value)
        except Exception:
            continue

    bundle_candidates = []
    for candidate in [
        root / "JIN_MODELS_AVAILABLE.zip",
        parent / "JIN_MODELS_AVAILABLE.zip",
    ]:
        if candidate.is_file():
            bundle_candidates.append(str(candidate.resolve()))

    core_candidates = []
    for candidate in [
        root / "model" / "uda" / "engine.py",
        parent / "model" / "uda" / "engine.py",
    ]:
        if candidate.is_file():
            core_candidates.append(str(candidate.resolve()))

    model_dir_candidates = []
    for candidate in [
        root / "data" / "learning",
        parent / "data" / "learning",
    ]:
        if candidate.is_dir():
            model_dir_candidates.append(str(candidate.resolve()))

    return {
        "pdf_dirs": pdf_candidates[:20],
        "jin_model_bundles": bundle_candidates,
        "jin_core_engines": core_candidates,
        "jin_model_dirs": model_dir_candidates,
    }


def _validate_jin_manifest(root: Path, models_dir: Path) -> list[dict[str, Any]]:
    manifest_path = root / "models" / "JIN_MODELS_MANIFEST.json"
    checks: list[dict[str, Any]] = []
    if not manifest_path.is_file():
        return [{
            "name": "jin_model_manifest",
            "ok": False,
            "severity": "error",
            "detail": f"Missing {manifest_path}",
        }]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_name = {
        item.get("file"): item
        for item in manifest.get("models") or []
        if item.get("file")
    }
    for filename in JIN_MODEL_FILES:
        path = models_dir / filename
        item = by_name.get(filename) or {}
        check = {
            "name": f"jin_model:{filename}",
            "path": str(path),
            "ok": path.is_file(),
            "severity": "error",
        }
        if not path.is_file():
            check["detail"] = "missing"
            checks.append(check)
            continue
        actual_size = path.stat().st_size
        expected_size = item.get("bytes")
        expected_sha = item.get("sha256")
        check["bytes"] = actual_size
        if expected_size is not None and actual_size != int(expected_size):
            check["ok"] = False
            check["detail"] = f"size {actual_size} != {expected_size}"
        elif expected_sha:
            actual_sha = _sha256(path)
            check["sha256"] = actual_sha
            if actual_sha != expected_sha:
                check["ok"] = False
                check["detail"] = f"sha256 {actual_sha} != {expected_sha}"
            else:
                check["detail"] = "verified"
        else:
            check["detail"] = "present"
        checks.append(check)
    return checks


def _ground_truth_checks(
    pdf_root: Path,
    ground_truth_path: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if ground_truth_path is None:
        checks.append({
            "name": "ground_truth",
            "ok": False,
            "severity": "error",
            "detail": "No --ground-truth supplied.",
        })
        return checks, {}

    if not ground_truth_path.is_file():
        checks.append({
            "name": "ground_truth",
            "ok": False,
            "severity": "error",
            "path": str(ground_truth_path),
            "detail": "file missing",
        })
        return checks, {}

    truth = load_ground_truth(ground_truth_path)
    reviewed = {
        name: document
        for name, document in truth.items()
        if ground_truth_is_reviewed(document)
    }
    missing_pdfs = sorted(
        name for name in truth if not (pdf_root / name).is_file()
    )
    checks.append({
        "name": "ground_truth",
        "ok": bool(reviewed) and not missing_pdfs,
        "severity": "error",
        "path": str(ground_truth_path),
        "documents": len(truth),
        "reviewed_documents": len(reviewed),
        "unreviewed_documents": len(truth) - len(reviewed),
        "missing_pdf_count": len(missing_pdfs),
        "missing_pdfs_sample": missing_pdfs[:20],
        "detail": (
            "ready"
            if reviewed and not missing_pdfs
            else "needs reviewed entries and matching PDFs"
        ),
    })
    return checks, {
        "documents": len(truth),
        "reviewed_documents": len(reviewed),
        "unreviewed_documents": len(truth) - len(reviewed),
    }


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    root = args.repo_root.resolve()
    pdf_root = args.pdf_dir.resolve()
    models_dir = args.models_dir.resolve()
    model_names = [
        item.strip()
        for item in args.models.split(",")
        if item.strip()
    ]

    checks: list[dict[str, Any]] = []
    checks.extend(_dependency_checks(model_names))
    pdfs = sorted(
        path for path in pdf_root.rglob("*")
        if path.is_file() and path.suffix.lower() == ".pdf"
    ) if pdf_root.is_dir() else []
    checks.append({
        "name": "pdf_corpus",
        "ok": bool(pdfs),
        "severity": "error",
        "path": str(pdf_root),
        "pdf_count": len(pdfs),
        "detail": "ready" if pdfs else "no PDF files found",
    })

    truth_checks, truth_summary = _ground_truth_checks(
        pdf_root,
        args.ground_truth.resolve() if args.ground_truth else None,
    )
    checks.extend(truth_checks)

    if "jin" in model_names:
        core_path = next(
            (root / candidate for candidate in FULL_JIN_CORE_CANDIDATES
             if (root / candidate).is_file()),
            None,
        )
        checks.append({
            "name": "jin_core_engine",
            "ok": core_path is not None,
            "severity": "error",
            "path": str(core_path) if core_path else None,
            "detail": (
                "ready"
                if core_path
                else "full JIN core missing; run prepare-model.sh with the engine bundle"
            ),
        })
        checks.extend(_validate_jin_manifest(root, models_dir))

    hf_models = {
        "qwen3_vl_2b": "Qwen/Qwen3-VL-2B-Instruct",
        "qwen3_vl_4b": "Qwen/Qwen3-VL-4B-Instruct",
        "embeddinggemma2_zone": "google/embeddinggemma-2",
        "granite_docling_258m": "ibm-granite/granite-docling-258M",
        "glm_ocr": "zai-org/GLM-OCR",
        "paddleocr_vl_1_6": "PaddlePaddle/PaddleOCR-VL-1.6",
    }
    offline = str(os.getenv("HF_HUB_OFFLINE") or "").strip().casefold() in {
        "1", "true", "yes", "on"
    }
    for model_name in model_names:
        model_id = hf_models.get(model_name)
        if not model_id:
            continue
        cached = _hf_cache_has(model_id)
        ok = cached is True or (cached is not True and not offline)
        checks.append({
            "name": f"hf_model:{model_name}",
            "ok": ok,
            "severity": "error" if offline else "warning",
            "model_id": model_id,
            "cached": cached,
            "hf_offline": offline,
            "detail": (
                "cached"
                if cached is True
                else "not cached; will download at benchmark time"
                if not offline
                else "not cached while HF_HUB_OFFLINE is enabled"
            ),
        })

    ram_gb = _memory_total_gb()
    guidance = max(
        (MODEL_RAM_GUIDANCE_GB.get(name, 0.0) for name in model_names),
        default=0.0,
    )
    checks.append({
        "name": "system_ram",
        "ok": ram_gb is None or ram_gb >= guidance,
        "severity": "warning",
        "detected_gb": ram_gb,
        "recommended_gb": guidance,
        "detail": (
            "unknown"
            if ram_gb is None
            else "sufficient"
            if ram_gb >= guidance
            else "below conservative guidance; expect swapping/OOM risk"
        ),
    })

    disk = shutil.disk_usage(root)
    free_gb = disk.free / (1024 ** 3)
    checks.append({
        "name": "disk_free",
        "ok": free_gb >= 15.0,
        "severity": "warning",
        "free_gb": free_gb,
        "recommended_free_gb": 15.0,
        "detail": "sufficient" if free_gb >= 15.0 else "low free disk for model downloads/artifacts",
    })

    blocking = [
        check for check in checks
        if check.get("severity") == "error" and not check.get("ok")
    ]
    warnings = [
        check for check in checks
        if check.get("severity") == "warning" and not check.get("ok")
    ]
    return {
        "schema_version": "jin-open-weight-preflight-v1",
        "ready": not blocking,
        "repo_root": str(root),
        "models": model_names,
        "pdf_root": str(pdf_root),
        "models_dir": str(models_dir),
        "ground_truth": str(args.ground_truth.resolve()) if args.ground_truth else None,
        "ground_truth_summary": truth_summary,
        "checks": checks,
        "blocking_count": len(blocking),
        "warning_count": len(warnings),
        "blocking": blocking,
        "warnings": warnings,
        "discovery_hints": _discover_private_inputs(root),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate all prerequisites for the JIN open-weight benchmark."
    )
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--pdf-dir", type=Path, required=True)
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--models-dir", type=Path, default=Path("data/learning"))
    parser.add_argument(
        "--models",
        default="jin,qwen3_vl_2b,qwen3_vl_4b,embeddinggemma2_zone",
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = preflight(args)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ready"] else 4


if __name__ == "__main__":
    raise SystemExit(main())

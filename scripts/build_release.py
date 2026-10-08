#!/usr/bin/env python3
"""Build a portable, privacy-safe JIN CPU release ZIP."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ARTIFACTS = (
    "alias_lexicon.json",
    "document_family_classifier_v5.joblib",
    "document_family_classifier_v5_metrics.json",
    "image_state_centroids.json",
    "image_state_rf.joblib",
    "ocr_policy.json",
    "po_number_ranker_v5.joblib",
    "po_number_ranker_v5_metrics.json",
    "po_number_shape_profiles_v5.json",
)
CORE_ROOT_FILES = (
    "README.md",
    "pyproject.toml",
    "requirements.txt",
)
MODEL_FILES = (
    "corpus_text_router.joblib",
    "jin-pdf-fusion-router-v3-cpu.joblib",
    "jin-field-weak-router-v2-cpu.joblib",
)
FORBIDDEN_PARTS = {
    ".git", ".pytest_cache", "__pycache__", "feedback", "evaluation",
    "reference", "dist",
}
FORBIDDEN_SUFFIXES = {".pdf", ".pyc", ".pyo", ".zip"}
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def _run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _version() -> str:
    text = (ROOT / "jin_runtime" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)', text, flags=re.M)
    if not match:
        raise RuntimeError("Unable to read jin_runtime.__version__")
    return match.group(1)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe(relative: Path) -> bool:
    parts = set(relative.parts)
    return (
        not parts.intersection(FORBIDDEN_PARTS)
        and relative.suffix.lower() not in FORBIDDEN_SUFFIXES
        and relative.name != ".env"
        and not relative.name.startswith("jin-model-v")
    )


def _git_files() -> set[Path]:
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, check=True, capture_output=True,
    ).stdout
    return {
        Path(value.decode("utf-8"))
        for value in output.split(b"\0")
        if value and _safe(Path(value.decode("utf-8")))
    }


def _add_tree(files: set[Path], relative_root: Path) -> None:
    absolute = ROOT / relative_root
    if not absolute.is_dir():
        raise FileNotFoundError(absolute)
    for path in absolute.rglob("*"):
        if path.is_file():
            relative = path.relative_to(ROOT)
            if _safe(relative):
                files.add(relative)


def _release_files() -> list[Path]:
    files = _git_files()
    for directory in (Path("model/config"), Path("model/po_ocr"), Path("model/uda")):
        _add_tree(files, directory)
    for name in CORE_ROOT_FILES:
        files.add(Path("model") / name)
    for name in RUNTIME_ARTIFACTS:
        files.add(Path("model/training/artifacts") / name)
    for name in MODEL_FILES:
        files.add(Path("data/learning") / name)
    missing = [str(path) for path in files if not (ROOT / path).is_file()]
    if missing:
        raise FileNotFoundError("Missing release files: " + ", ".join(sorted(missing)))
    return sorted(files, key=lambda value: value.as_posix())


def _zip_write(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data, compresslevel=9)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()

    version = _version()
    commit = _run_git("rev-parse", "HEAD")
    commit_date = _run_git("show", "-s", "--format=%cI", "HEAD")
    files = _release_files()
    prefix = f"jin-model-v{version}-cpu"
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{prefix}.zip"

    manifest_files = []
    payloads: list[tuple[str, bytes]] = []
    for relative in files:
        data = (ROOT / relative).read_bytes()
        archive_name = str(PurePosixPath(prefix) / PurePosixPath(relative.as_posix()))
        payloads.append((archive_name, data))
        manifest_files.append({
            "path": relative.as_posix(),
            "bytes": len(data),
            "sha256": _sha256_bytes(data),
        })

    manifest = {
        "name": "JIN portable CPU release",
        "version": version,
        "git_commit": commit,
        "generated_at": datetime.fromisoformat(commit_date).astimezone(timezone.utc).isoformat(),
        "privacy": {
            "customer_pdfs_included": False,
            "campaign_results_included": False,
            "feedback_included": False,
            "environment_secrets_included": False,
            "ban_reference_included": False,
        },
        "files": manifest_files,
    }
    manifest_data = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    with zipfile.ZipFile(output, "w") as archive:
        for archive_name, data in payloads:
            _zip_write(archive, archive_name, data)
        _zip_write(
            archive,
            str(PurePosixPath(prefix) / "RELEASE-MANIFEST.json"),
            manifest_data,
        )

    with zipfile.ZipFile(output) as archive:
        corrupt = archive.testzip()
        names = archive.namelist()
    if corrupt:
        raise RuntimeError(f"Corrupt ZIP entry: {corrupt}")
    forbidden = [
        name for name in names
        if any(part in FORBIDDEN_PARTS for part in PurePosixPath(name).parts)
        or PurePosixPath(name).suffix.lower() in {".pdf", ".pyc", ".pyo"}
        or PurePosixPath(name).name == ".env"
    ]
    if forbidden:
        raise RuntimeError("Forbidden release entries: " + ", ".join(forbidden[:10]))

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_suffix(output.suffix + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(json.dumps({
        "version": version,
        "commit": commit,
        "files": len(names),
        "bytes": output.stat().st_size,
        "sha256": digest,
        "zip": str(output),
        "checksum": str(checksum),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

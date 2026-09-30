#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def prepare_source(source: Path) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    if source.is_dir():
        return source, None
    if source.is_file() and source.suffix.lower() == ".zip":
        temporary = tempfile.TemporaryDirectory(prefix="jin-models-")
        with zipfile.ZipFile(source) as archive:
            archive.extractall(temporary.name)
        return Path(temporary.name), temporary
    raise SystemExit(f"Unsupported model source: {source}")


def find_file(root: Path, filename: str) -> Path | None:
    direct = root / filename
    if direct.exists():
        return direct
    matches = list(root.rglob(filename))
    return matches[0] if len(matches) == 1 else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install JIN trained model artifacts with SHA-256 verification."
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Directory or ZIP containing the trained .joblib files.",
    )
    parser.add_argument(
        "--target",
        default="data/learning",
        help="Runtime model directory.",
    )
    parser.add_argument(
        "--manifest",
        default="models/JIN_MODELS_MANIFEST.json",
    )
    args = parser.parse_args()

    source = Path(args.source)
    target = Path(args.target)
    manifest = load_manifest(Path(args.manifest))
    source_root, temporary = prepare_source(source)
    target.mkdir(parents=True, exist_ok=True)

    installed = []
    missing = []
    try:
        for model in manifest.get("models", []):
            status = str(model.get("status") or "")
            filename = str(model.get("file") or "")
            if not filename:
                continue
            if status not in {"ready", "required"}:
                continue

            src = find_file(source_root, filename)
            if src is None:
                missing.append(filename)
                continue

            expected_size = int(model["bytes"])
            expected_sha = str(model["sha256"])
            actual_size = src.stat().st_size
            actual_sha = sha256(src)
            if actual_size != expected_size:
                raise SystemExit(
                    f"{filename}: invalid size {actual_size}, expected {expected_size}"
                )
            if actual_sha != expected_sha:
                raise SystemExit(
                    f"{filename}: invalid SHA-256 {actual_sha}, expected {expected_sha}"
                )

            dst = target / filename
            shutil.copy2(src, dst)
            installed.append(filename)
            print(f"installed {filename} -> {dst}")

        if missing:
            raise SystemExit(
                "Missing required model artifacts: " + ", ".join(missing)
            )
    finally:
        if temporary is not None:
            temporary.cleanup()

    print(f"OK: installed {len(installed)} verified model artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

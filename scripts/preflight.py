#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def check_tesseract() -> tuple[bool, str]:
    binary = shutil.which("tesseract")
    if not binary:
        return False, "tesseract executable not found"
    try:
        result = subprocess.run(
            [binary, "--list-langs"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        return False, f"tesseract check failed: {exc}"
    languages = set(result.stdout.split())
    missing = {"fra", "eng", "deu"} - languages
    if missing:
        return False, "missing OCR languages: " + ", ".join(sorted(missing))
    return True, f"tesseract OK ({', '.join(sorted({'fra','eng','deu'}))})"


def main() -> int:
    parser = argparse.ArgumentParser(description="JIN runtime preflight.")
    parser.add_argument("--full", action="store_true", help="Also require the packaged uda core engine.")
    parser.add_argument("--target", default="data/learning")
    args = parser.parse_args()

    failures = []

    models_ok = verify_models_main_for_target(args.target)
    if not models_ok:
        failures.append("runtime models")

    ocr_ok, ocr_message = check_tesseract()
    print(("OK" if ocr_ok else "ERROR"), ocr_message)
    if not ocr_ok:
        failures.append("tesseract")

    if args.full:
        core_ok = Path("model/uda").is_dir() and Path("model/requirements.txt").is_file()
        print(
            "OK core engine model/uda"
            if core_ok
            else "ERROR core engine missing: run ./prepare-model.sh <engine.zip>"
        )
        if not core_ok:
            failures.append("uda core")

    if failures:
        print("Preflight failed:", ", ".join(failures))
        return 2

    print("Preflight OK.")
    return 0


def verify_models_main_for_target(target: str) -> bool:
    manifest = json.loads(
        Path("models/JIN_MODELS_MANIFEST.json").read_text(encoding="utf-8")
    )
    import hashlib

    ok = True
    for model in manifest.get("models", []):
        if model.get("status") not in {"ready", "required"}:
            continue
        path = Path(target) / model["file"]
        if not path.exists():
            print("ERROR missing model", path)
            ok = False
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.stat().st_size != int(model["bytes"]) or digest != model["sha256"]:
            print("ERROR invalid model", path)
            ok = False
        else:
            print("OK model", path.name)
    return ok


if __name__ == "__main__":
    raise SystemExit(main())

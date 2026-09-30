#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify installed JIN model artifacts.")
    parser.add_argument("--target", default="data/learning")
    parser.add_argument("--manifest", default="models/JIN_MODELS_MANIFEST.json")
    args = parser.parse_args()

    target = Path(args.target)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    failures = []

    for model in manifest.get("models", []):
        if model.get("status") not in {"ready", "required"}:
            continue
        filename = model["file"]
        path = target / filename
        if not path.exists():
            failures.append(f"{filename}: missing")
            continue
        actual_size = path.stat().st_size
        actual_sha = sha256(path)
        if actual_size != int(model["bytes"]):
            failures.append(
                f"{filename}: size {actual_size} != {model['bytes']}"
            )
        elif actual_sha != model["sha256"]:
            failures.append(
                f"{filename}: sha256 {actual_sha} != {model['sha256']}"
            )
        else:
            print(f"OK {filename} {actual_size} bytes {actual_sha}")

    if failures:
        for failure in failures:
            print("ERROR", failure)
        return 2

    print("All required runtime model artifacts are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

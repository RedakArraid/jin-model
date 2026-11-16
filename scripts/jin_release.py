#!/usr/bin/env python3
"""Export/load a complete JIN Docker image without Git, SCP or a registry.

Transport the resulting directory using approved private storage. It contains
code and dependencies, NOT Studio data; transfer a verified jin_backup bundle
separately. An image ID and the archive SHA-256 pin the exact runtime bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

SCHEMA = "jin-runtime-release/v1"


def run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_image(image: str) -> dict:
    return json.loads(run("docker", "image", "inspect", image))[0]


def export_image(image: str, output: Path) -> Path:
    info = inspect_image(image)
    labels = info.get("Config", {}).get("Labels") or {}
    if labels.get("com.jin.runtime.core-included") != "true":
        raise ValueError("Use the complete production Dockerfile, not the standalone image")
    revision = labels.get("org.opencontainers.image.revision", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Build with JIN_BUILD_REVISION=<full Git commit>")
    image_id = info["Id"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Invalid Docker image identifier")
    # Verify the installed core, without mounting user data or using network.
    run("docker", "run", "--rm", "--network", "none", "--entrypoint", "python", image_id,
        "-c", "from uda.learned import load_router, structural_shape; import jin_runtime; assert structural_shape('A12') == 'A9'")
    output.mkdir(parents=True, exist_ok=False)
    reference = f"jin-model-api:release-{image_id.split(':')[1]}"
    run("docker", "tag", image_id, reference)
    archive = output / "runtime-image.tar"
    run("docker", "image", "save", "--output", str(archive), reference)
    manifest = {
        "schema": SCHEMA, "git_revision": revision, "image_id": image_id,
        "reference": reference, "os": info["Os"], "architecture": info["Architecture"],
        "registry_digests": info.get("RepoDigests", []),
        "archive": archive.name, "bytes": archive.stat().st_size, "sha256": checksum(archive),
        "contains_studio_data": False,
    }
    (output / "release.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    # Docker accepts its local immutable image ID. No dependency on latest or
    # on a mutable tag, and no registry pull is necessary after `load`.
    (output / "image.env").write_text(f"JIN_RUNTIME_IMAGE={image_id}\nJIN_BUILD_REVISION={revision}\n", encoding="utf-8")
    return output


def verify_release(directory: Path) -> dict:
    manifest = json.loads((directory / "release.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA or manifest.get("archive") != "runtime-image.tar":
        raise ValueError("Unsupported or unsafe release manifest")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(manifest.get("image_id"))):
        raise ValueError("Invalid release image ID")
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("git_revision"))):
        raise ValueError("Invalid release Git revision")
    archive = directory / "runtime-image.tar"
    if archive.is_symlink() or archive.stat().st_size != manifest["bytes"] or checksum(archive) != manifest["sha256"]:
        raise ValueError("Runtime archive checksum/size mismatch")
    return manifest


def load_release(directory: Path) -> dict:
    manifest = verify_release(directory)
    # Refuse a platform mismatch before importing any image.
    server = json.loads(run("docker", "version", "--format", "{{json .Server}}"))
    if (server["Os"], server["Arch"]) != (manifest["os"], manifest["architecture"]):
        raise ValueError("Release platform does not match the Docker daemon")
    run("docker", "image", "load", "--input", str(directory / "runtime-image.tar"))
    loaded = inspect_image(manifest["image_id"])
    labels = loaded.get("Config", {}).get("Labels") or {}
    if loaded["Id"] != manifest["image_id"] or labels.get("org.opencontainers.image.revision") != manifest["git_revision"]:
        raise ValueError("Loaded image does not match the release")
    # Regenerate, rather than trust a possibly altered deployment env file.
    (directory / "image.env").write_text(
        f"JIN_RUNTIME_IMAGE={manifest['image_id']}\nJIN_BUILD_REVISION={manifest['git_revision']}\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--image", required=True)
    export.add_argument("--output", type=Path, required=True)
    for name in ("verify", "load"):
        commands.add_parser(name).add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "export":
        print(export_image(args.image, args.output))
    else:
        result = load_release(args.directory) if args.command == "load" else verify_release(args.directory)
        print(json.dumps({"image_id": result["image_id"], "git_revision": result["git_revision"]}))


if __name__ == "__main__":
    main()

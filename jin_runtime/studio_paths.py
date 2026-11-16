"""Portable Studio-owned paths; never persist a machine's storage prefix."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

NAMESPACES = {"documents", "datasets", "training"}
PATH_OPTIONS = {"--manifest", "--data", "--output", "--artifact"}


def relative_studio_path(value: str) -> str:
    normalized = str(value).replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if ".." in parts:
        raise ValueError("Studio path cannot traverse outside storage")
    absolute = normalized.startswith("/") or PureWindowsPath(normalized).is_absolute()
    if absolute:
        # Legacy Windows/Linux exports carry an obsolete root. Only known
        # Studio namespaces may be relocated, never arbitrary absolute files.
        positions = [i for i, part in enumerate(parts) if part in NAMESPACES]
        if not positions:
            raise ValueError("Absolute path is not managed by JIN Studio")
        parts = parts[positions[-1]:]
    if len(parts) < 2 or parts[0] not in NAMESPACES or any(":" in p for p in parts):
        raise ValueError("Path is not managed by JIN Studio")
    return PurePosixPath(*parts).as_posix()


def resolve_studio_path(root: Path, value: str) -> Path:
    candidate = (root / relative_studio_path(value)).resolve()
    if root.resolve() not in candidate.parents:
        raise ValueError("Studio path escapes storage through a symbolic link")
    return candidate


def training_paths(payload: dict[str, Any], root: Path, *, absolute: bool) -> dict[str, Any]:
    """Translate only executable path fields; leave source/audit text intact."""
    result = deepcopy(payload)

    def convert(value: str) -> str:
        return str(resolve_studio_path(root, value)) if absolute else relative_studio_path(value)

    for key in ("manifestPath", "outputDirectory", "logPath", "benchmarkPath", "artifactDirectory"):
        if result.get(key):
            result[key] = convert(result[key])
    if isinstance(result.get("result"), dict):
        result["result"] = training_paths(result["result"], root, absolute=absolute)
    for step in result.get("steps", []):
        command = step.get("command", [])
        for i in range(2, len(command)):
            option, separator, value = str(command[i]).partition("=")
            if option not in PATH_OPTIONS:
                continue
            if separator:
                command[i] = option + "=" + convert(value)
            elif i + 1 < len(command):
                command[i + 1] = convert(command[i + 1])
    if "steps" in result:
        result["command"] = " && ".join(" ".join(s["command"]) for s in result["steps"])
    return result

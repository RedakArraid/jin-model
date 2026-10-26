"""Shared machine-token handling for the Studio HTTP API and MCP server."""
from __future__ import annotations

import os
from pathlib import Path
import secrets


_ENV_NAMES = (
    "JIN_STUDIO_SERVICE_TOKEN",
    "JIN_STUDIO_TOKEN",
    "JIN_FEEDBACK_TOKEN",
)


def _shared_storage_enabled() -> bool:
    return str(os.getenv("JIN_STUDIO_SHARED_STORAGE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _managed_file_mode() -> int:
    # API and MCP can run as different users while sharing only JIN_STUDIO_DIR.
    # That scoped directory remains the security boundary for the secret.
    return 0o666 if _shared_storage_enabled() else 0o600


def _environment_token() -> str:
    for name in _ENV_NAMES:
        token = str(os.getenv(name) or "").strip()
        if token:
            return token
    return ""


def service_token_file() -> Path | None:
    configured = str(os.getenv("JIN_STUDIO_SERVICE_TOKEN_FILE") or "").strip()
    return Path(configured).resolve() if configured else None


def _studio_override_marker(path: Path) -> Path:
    return path.with_name(f"{path.name}.studio-managed")


def _mark_studio_override(path: Path) -> None:
    marker = _studio_override_marker(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    target = marker.with_name(f".{marker.name}.{secrets.token_hex(8)}.tmp")
    try:
        target.write_text("studio-managed\n", encoding="utf-8")
        try:
            target.chmod(_managed_file_mode())
        except OSError:
            pass
        os.replace(target, marker)
    finally:
        target.unlink(missing_ok=True)


def _read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    if len(token) < 32:
        raise RuntimeError(f"JIN Studio service token file is invalid: {path}")
    return token


def _write_new_token(path: Path, token: str, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(target, flags, _managed_file_mode())
    try:
        os.write(descriptor, (token + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)
    try:
        target.chmod(_managed_file_mode())
    except OSError:
        pass
    try:
        if replace:
            os.replace(target, path)
        else:
            os.link(target, path)
    finally:
        target.unlink(missing_ok=True)


def configured_service_token(*, create: bool = True) -> str:
    """Return the active token, preferring a Studio-managed shared-file override."""
    path = service_token_file()
    environment = _environment_token()
    if environment and (
        path is None or not _studio_override_marker(path).is_file()
    ):
        return environment
    if path is not None:
        existing = _read_token(path)
        if existing and (
            not environment or _studio_override_marker(path).is_file()
        ):
            return existing

    if environment:
        return environment
    if path is None or not create:
        return ""

    generated = secrets.token_urlsafe(48)
    try:
        _write_new_token(path, generated, replace=False)
        return generated
    except FileExistsError:  # API and MCP may start concurrently.
        return _read_token(path)


def rotate_service_token() -> str:
    """Rotate the Studio-managed token, overriding legacy environment tokens."""
    path = service_token_file()
    if path is None:
        raise RuntimeError("JIN_STUDIO_SERVICE_TOKEN_FILE is not configured")
    generated = secrets.token_urlsafe(48)
    _write_new_token(path, generated, replace=True)
    if _environment_token():
        _mark_studio_override(path)
    return generated


def service_token_status() -> dict[str, object]:
    """Describe machine-token management without ever returning the secret."""
    mcp_shared = not bool(str(os.getenv("JIN_STUDIO_MCP_TOKEN") or "").strip())
    path = service_token_file()
    environment = _environment_token()
    if environment and (
        path is None or not _studio_override_marker(path).is_file()
    ):
        return {
            "configured": True,
            "source": "environment",
            "rotatable": path is not None,
            "mcpShared": mcp_shared,
        }
    file_token = _read_token(path) if path is not None else ""
    if file_token and (
        not environment or _studio_override_marker(path).is_file()
    ):
        return {
            "configured": True,
            "source": "file",
            "rotatable": True,
            "mcpShared": mcp_shared,
        }
    if path is None:
        return {
            "configured": False,
            "source": "missing",
            "rotatable": False,
            "mcpShared": mcp_shared,
        }
    token = configured_service_token()
    return {
        "configured": bool(token),
        "source": "file",
        "rotatable": True,
        "mcpShared": mcp_shared,
    }


__all__ = [
    "configured_service_token",
    "rotate_service_token",
    "service_token_status",
    "service_token_file",
]

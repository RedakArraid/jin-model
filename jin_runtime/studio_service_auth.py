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


def _environment_token() -> str:
    for name in _ENV_NAMES:
        token = str(os.getenv(name) or "").strip()
        if token:
            return token
    return ""


def service_token_file() -> Path | None:
    configured = str(os.getenv("JIN_STUDIO_SERVICE_TOKEN_FILE") or "").strip()
    return Path(configured).resolve() if configured else None


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
    descriptor = os.open(target, flags, 0o600)
    try:
        os.write(descriptor, (token + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)
    try:
        target.chmod(0o600)
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
    """Return the configured token, atomically creating its shared file if needed."""
    environment = _environment_token()
    if environment:
        return environment
    path = service_token_file()
    if path is None:
        return ""
    existing = _read_token(path)
    if existing or not create:
        return existing

    generated = secrets.token_urlsafe(48)
    try:
        _write_new_token(path, generated, replace=False)
        return generated
    except FileExistsError:  # API and MCP may start concurrently.
        return _read_token(path)


def rotate_service_token() -> str:
    """Rotate the file-backed token; environment-managed secrets are immutable here."""
    if _environment_token():
        raise RuntimeError(
            "The service token comes from an environment variable; rotate it in the secret manager."
        )
    path = service_token_file()
    if path is None:
        raise RuntimeError("JIN_STUDIO_SERVICE_TOKEN_FILE is not configured")
    generated = secrets.token_urlsafe(48)
    _write_new_token(path, generated, replace=True)
    return generated


__all__ = [
    "configured_service_token",
    "rotate_service_token",
    "service_token_file",
]

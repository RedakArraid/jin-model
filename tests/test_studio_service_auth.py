from __future__ import annotations

from pathlib import Path

import pytest

from jin_runtime.studio_service_auth import (
    configured_service_token,
    rotate_service_token,
    service_token_file,
)


TOKEN_ENVIRONMENTS = (
    "JIN_STUDIO_SERVICE_TOKEN",
    "JIN_STUDIO_TOKEN",
    "JIN_FEEDBACK_TOKEN",
)


def _use_file_backed_token(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    for name in TOKEN_ENVIRONMENTS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JIN_STUDIO_SERVICE_TOKEN_FILE", str(path))


def test_file_backed_service_token_is_created_once_and_shared(tmp_path, monkeypatch):
    token_path = tmp_path / "studio" / "service-token"
    _use_file_backed_token(monkeypatch, token_path)

    first = configured_service_token()
    second = configured_service_token()

    assert len(first) >= 32
    assert second == first
    assert token_path.read_text(encoding="utf-8").strip() == first
    assert service_token_file() == token_path.resolve()


def test_file_backed_service_token_rotation_revokes_previous_value(tmp_path, monkeypatch):
    token_path = tmp_path / "service-token"
    _use_file_backed_token(monkeypatch, token_path)
    previous = configured_service_token()

    rotated = rotate_service_token()

    assert rotated != previous
    assert configured_service_token() == rotated
    assert token_path.read_text(encoding="utf-8").strip() == rotated


def test_environment_service_token_takes_precedence_and_is_not_rotated_here(
    tmp_path, monkeypatch
):
    token_path = tmp_path / "service-token"
    _use_file_backed_token(monkeypatch, token_path)
    monkeypatch.setenv("JIN_STUDIO_SERVICE_TOKEN", "managed-outside-studio")

    assert configured_service_token() == "managed-outside-studio"
    assert not token_path.exists()
    with pytest.raises(RuntimeError, match="secret manager"):
        rotate_service_token()


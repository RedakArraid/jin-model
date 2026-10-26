from __future__ import annotations

from pathlib import Path

import pytest

from jin_runtime.studio_service_auth import (
    configured_service_token,
    rotate_service_token,
    service_token_file,
    service_token_status,
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
    assert service_token_status() == {
        "configured": True,
        "source": "file",
        "rotatable": True,
        "mcpShared": True,
    }


def test_file_backed_service_token_rotation_revokes_previous_value(tmp_path, monkeypatch):
    token_path = tmp_path / "service-token"
    _use_file_backed_token(monkeypatch, token_path)
    previous = configured_service_token()

    rotated = rotate_service_token()

    assert rotated != previous
    assert configured_service_token() == rotated
    assert token_path.read_text(encoding="utf-8").strip() == rotated


def test_environment_service_token_can_be_replaced_inside_studio(
    tmp_path, monkeypatch
):
    token_path = tmp_path / "service-token"
    _use_file_backed_token(monkeypatch, token_path)
    monkeypatch.setenv("JIN_STUDIO_SERVICE_TOKEN", "managed-outside-studio")

    assert configured_service_token() == "managed-outside-studio"
    assert not token_path.exists()
    assert service_token_status() == {
        "configured": True,
        "source": "environment",
        "rotatable": True,
        "mcpShared": True,
    }
    rotated = rotate_service_token()

    assert rotated != "managed-outside-studio"
    assert token_path.read_text(encoding="utf-8").strip() == rotated
    assert configured_service_token() == rotated
    assert service_token_status() == {
        "configured": True,
        "source": "file",
        "rotatable": True,
        "mcpShared": True,
    }


def test_stale_file_does_not_override_environment_until_studio_rotation(
    tmp_path, monkeypatch
):
    token_path = tmp_path / "service-token"
    _use_file_backed_token(monkeypatch, token_path)
    token_path.write_text("stale-file-token-that-is-long-enough-to-be-valid\n", encoding="utf-8")
    monkeypatch.setenv("JIN_STUDIO_SERVICE_TOKEN", "current-environment-token")

    assert configured_service_token() == "current-environment-token"
    assert service_token_status()["source"] == "environment"

    rotated = rotate_service_token()

    assert configured_service_token() == rotated
    assert rotated != "stale-file-token-that-is-long-enough-to-be-valid"
    assert service_token_status()["source"] == "file"


def test_environment_service_token_needs_shared_storage_for_studio_rotation(
    monkeypatch,
):
    for name in TOKEN_ENVIRONMENTS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("JIN_STUDIO_SERVICE_TOKEN_FILE", raising=False)
    monkeypatch.setenv("JIN_STUDIO_SERVICE_TOKEN", "managed-without-shared-file")

    assert configured_service_token() == "managed-without-shared-file"
    assert service_token_status() == {
        "configured": True,
        "source": "environment",
        "rotatable": False,
        "mcpShared": True,
    }
    with pytest.raises(RuntimeError, match="SERVICE_TOKEN_FILE"):
        rotate_service_token()

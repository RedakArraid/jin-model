from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from scripts.jin_backup import (
    BACKUP_SCHEMA,
    RESTORE_CONFIRMATION,
    BackupError,
    create_backup,
    create_bundle,
    restore_backup,
    unpack_bundle,
    verify_backup,
)


def _source_tree(root: Path) -> dict[str, Path]:
    studio = root / "data" / "studio"
    learning = root / "data" / "learning"
    feedback = root / "feedback"
    reference = root / "data" / "reference"
    runtime_core = root / "model"
    (studio / "documents" / "jdoc-1").mkdir(parents=True)
    (studio / "datasets").mkdir()
    learning.mkdir(parents=True)
    feedback.mkdir()
    (reference / "ban").mkdir(parents=True)
    runtime_core.mkdir()
    (studio / "documents" / "jdoc-1" / "order.pdf").write_bytes(
        b"%PDF-1.4\nJIN backup test\n%%EOF\n"
    )
    (studio / "datasets" / "reviewed.jsonl").write_text(
        '{"document_id":"jdoc-1"}\n', encoding="utf-8"
    )
    (studio / "service-token").write_text("must-not-leave-host", encoding="utf-8")
    (studio / "service-token.studio-managed").write_text("managed", encoding="utf-8")
    (learning / "router.joblib").write_bytes(b"learned-model")
    (feedback / "learning_feedback.jsonl").write_text(
        '{"status":"HUMAN_VALIDATED"}\n', encoding="utf-8"
    )
    (reference / "ban" / "manifest.json").write_text("{}\n", encoding="utf-8")
    (runtime_core / "engine.py").write_text("ENGINE_VERSION = 'test'\n", encoding="utf-8")

    database = sqlite3.connect(studio / "studio.sqlite3")
    database.executescript(
        """
        CREATE TABLE studio_documents (document_id TEXT PRIMARY KEY);
        CREATE TABLE studio_annotations (annotation_id TEXT PRIMARY KEY);
        CREATE TABLE studio_users (user_id TEXT PRIMARY KEY);
        CREATE TABLE studio_sessions (
            session_hash TEXT PRIMARY KEY,
            revoked_at TEXT
        );
        CREATE TABLE studio_audit_events (event_id TEXT PRIMARY KEY);
        INSERT INTO studio_documents VALUES ('jdoc-1');
        INSERT INTO studio_users VALUES ('user-1');
        INSERT INTO studio_sessions VALUES ('session-hash', NULL);
        """
    )
    database.commit()
    database.close()
    return {
        "project_root": root,
        "output_root": root / "backups",
        "studio_root": studio,
        "learning_root": learning,
        "feedback_root": feedback,
        "reference_root": reference,
        "runtime_core_root": runtime_core,
    }


def test_create_verify_and_restore_complete_backup(tmp_path: Path):
    paths = _source_tree(tmp_path / "source")
    backup = create_backup(**paths)
    manifest = verify_backup(backup)

    assert manifest["schema"] == BACKUP_SCHEMA
    assert manifest["include_reference"] is True
    assert {item["kind"] for item in manifest["artifacts"]} == {
        "studio_database",
        "studio_files",
        "learned_models",
        "feedback",
        "runtime_core",
        "reference_data",
        "runtime_settings",
    }
    studio_database = next(
        item for item in manifest["artifacts"] if item["kind"] == "studio_database"
    )
    assert studio_database["details"]["documents"] == 1
    assert studio_database["details"]["users"] == 1

    target = tmp_path / "restored"
    result = restore_backup(
        backup,
        target_root=target,
        confirmation=RESTORE_CONFIRMATION,
    )
    assert result["revoked_sessions"] == 1
    assert (target / "data/studio/documents/jdoc-1/order.pdf").is_file()
    assert (target / "data/studio/datasets/reviewed.jsonl").is_file()
    assert not (target / "data/studio/service-token").exists()
    assert not (target / "data/studio/service-token.studio-managed").exists()
    assert (target / "data/learning/router.joblib").read_bytes() == b"learned-model"
    assert (target / "feedback/learning_feedback.jsonl").is_file()
    assert (target / "model/engine.py").is_file()
    assert (target / "data/reference/ban/manifest.json").is_file()
    assert (target / "config/runtime-settings.env").is_file()
    with sqlite3.connect(target / "data/studio/studio.sqlite3") as database:
        assert database.execute(
            "SELECT revoked_at IS NOT NULL FROM studio_sessions"
        ).fetchone()[0] == 1


def test_reference_can_be_explicitly_excluded(tmp_path: Path):
    paths = _source_tree(tmp_path / "source")
    backup = create_backup(**paths, include_reference=False)
    manifest = verify_backup(backup)
    assert manifest["include_reference"] is False
    assert "reference_data" not in {item["kind"] for item in manifest["artifacts"]}

    target = tmp_path / "restored"
    restore_backup(
        backup,
        target_root=target,
        confirmation=RESTORE_CONFIRMATION,
    )
    assert not (target / "data/reference").exists()


def test_restore_requires_confirmation_and_empty_targets(tmp_path: Path):
    paths = _source_tree(tmp_path / "source")
    backup = create_backup(**paths)
    target = tmp_path / "restored"

    with pytest.raises(BackupError, match="requires --confirm"):
        restore_backup(backup, target_root=target, confirmation="NO")

    occupied = target / "data" / "studio"
    occupied.mkdir(parents=True)
    (occupied / "keep.txt").write_text("existing", encoding="utf-8")
    with pytest.raises(BackupError, match="refusing to overwrite"):
        restore_backup(
            backup,
            target_root=target,
            confirmation=RESTORE_CONFIRMATION,
        )
    assert (occupied / "keep.txt").read_text(encoding="utf-8") == "existing"

    with pytest.raises(BackupError, match="does not contain reference data"):
        restore_backup(
            create_backup(**paths, include_reference=False),
            target_root=tmp_path / "reference-restore",
            confirmation=RESTORE_CONFIRMATION,
            restore_reference=True,
        )


def test_verification_detects_corruption(tmp_path: Path):
    paths = _source_tree(tmp_path / "source")
    backup = create_backup(**paths)
    (backup / "learning.tar.gz").write_bytes(b"corrupted")
    with pytest.raises(BackupError, match="mismatch"):
        verify_backup(backup)


def test_bundle_round_trip_is_verified(tmp_path: Path):
    paths = _source_tree(tmp_path / "source")
    backup = create_backup(**paths)
    bundle = create_bundle(backup)
    unpacked = unpack_bundle(bundle, tmp_path / "downloaded")
    assert unpacked.name == backup.name
    assert verify_backup(unpacked)["schema"] == BACKUP_SCHEMA


def test_bundle_rejects_path_traversal(tmp_path: Path):
    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        payload = b"escape"
        member = tarfile.TarInfo("../escape.txt")
        member.size = len(payload)
        handle.addfile(member, io.BytesIO(payload))
    with pytest.raises(BackupError, match="Unsafe archive member"):
        unpack_bundle(archive, tmp_path / "target")


def test_manifest_never_contains_service_token_value(tmp_path: Path):
    paths = _source_tree(tmp_path / "source")
    backup = create_backup(**paths)
    manifest_text = (backup / "manifest.json").read_text(encoding="utf-8")
    assert "must-not-leave-host" not in manifest_text
    manifest = json.loads(manifest_text)
    assert manifest["security"]["service_tokens_included"] is False


def test_settings_are_whitelisted_and_restored_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("JIN_FIELD_ROUTER_THRESHOLD", "0.95")
    monkeypatch.setenv("JIN_OCR_LANGS", "fra+deu")
    monkeypatch.setenv("JIN_STUDIO_SERVICE_TOKEN", "private-test-token")
    monkeypatch.setenv("HTTPS_PROXY", "https://user:password@example.invalid")
    paths = _source_tree(tmp_path / "source")
    (paths["runtime_core_root"] / ".env").write_text("SECRET=do-not-export")
    (paths["learning_root"] / ".env.production").write_text("SECRET=do-not-export")
    backup = create_backup(**paths)
    text = (backup / "runtime-settings.env").read_text()
    assert "JIN_FIELD_ROUTER_THRESHOLD=0.95" in text
    assert "JIN_OCR_LANGS=fra+deu" in text
    assert "private-test-token" not in text and "password" not in text
    for archive in ("runtime-core.tar.gz", "learning.tar.gz"):
        with tarfile.open(backup / archive) as contents:
            assert not any(".env" in name for name in contents.getnames())


def test_missing_reference_and_recursive_backup_targets_are_rejected(tmp_path):
    paths = _source_tree(tmp_path / "source")
    with pytest.raises(BackupError, match="Reference directory is missing"):
        create_backup(**{**paths, "reference_root": tmp_path / "missing"})
    with pytest.raises(BackupError, match="inside a source"):
        create_backup(**{**paths, "output_root": paths["studio_root"] / "backups"})

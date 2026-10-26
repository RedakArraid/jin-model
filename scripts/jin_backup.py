#!/usr/bin/env python3
"""Create, verify and restore JIN persistent data backups.

The backup is deliberately independent from the Docker volumes.  It contains
the irreplaceable Studio documents and database, human feedback and learned
models.  The large BAN reference is reproducible and is therefore opt-in.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


BACKUP_SCHEMA = "jin-model-backup/v1"
RESTORE_CONFIRMATION = "RESTORE_EMPTY_TARGET"
DEFAULT_EXCLUDED_STUDIO_FILES = frozenset(
    {
        "studio.sqlite3",
        "studio.sqlite3-wal",
        "studio.sqlite3-shm",
        "service-token",
        "service-token.studio-managed",
    }
)


class BackupError(RuntimeError):
    """Raised when a backup cannot be trusted or safely restored."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chmod_private(path: Path, *, directory: bool = False) -> None:
    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError:
        # Windows ACLs and some mounted filesystems do not expose POSIX modes.
        pass


def _git_revision(project_root: Path) -> str | None:
    configured = str(os.getenv("JIN_BACKUP_GIT_REVISION") or "").strip()
    if configured:
        return configured
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _safe_archive_name(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    member = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or member.is_absolute()
        or ".." in member.parts
    ):
        raise BackupError(f"Unsafe archive member: {value!r}")
    return member


def _validate_tar(archive: Path) -> None:
    try:
        with tarfile.open(archive, "r:gz") as handle:
            for member in handle.getmembers():
                _safe_archive_name(member.name)
                if member.issym() or member.islnk() or member.isdev():
                    raise BackupError(
                        f"Unsupported archive member type: {member.name!r}"
                    )
    except (tarfile.TarError, OSError) as exc:
        raise BackupError(f"Unreadable archive {archive}: {exc}") from exc


def _iter_regular_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BackupError(f"Symbolic links are not accepted in backups: {path}")
        if path.is_file():
            yield path


def _archive_directory(
    source: Path,
    destination: Path,
    *,
    excluded_names: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    if not source.is_dir():
        return None
    file_count = 0
    source_bytes = 0
    with tarfile.open(destination, "w:gz") as archive:
        for path in _iter_regular_files(source):
            relative = path.relative_to(source)
            if relative.name in excluded_names:
                continue
            archive.add(path, arcname=relative.as_posix(), recursive=False)
            file_count += 1
            source_bytes += path.stat().st_size
    _chmod_private(destination)
    return {
        "file_count": file_count,
        "source_bytes": source_bytes,
    }


def _sqlite_snapshot(source: Path, destination: Path) -> dict[str, Any] | None:
    if not source.is_file():
        return None
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as original:
            with closing(sqlite3.connect(destination)) as snapshot:
                original.backup(snapshot)
                result = snapshot.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise BackupError(f"SQLite snapshot integrity failure: {result}")
    except sqlite3.Error as exc:
        raise BackupError(f"Unable to snapshot Studio SQLite database: {exc}") from exc
    _chmod_private(destination)
    return _studio_database_counts(destination)


def _studio_database_counts(path: Path) -> dict[str, int]:
    requested = {
        "documents": "studio_documents",
        "annotations": "studio_annotations",
        "datasets": "studio_datasets",
        "training_runs": "studio_training_runs",
        "users": "studio_users",
        "audit_events": "studio_audit_events",
    }
    counts: dict[str, int] = {}
    try:
        with closing(
            sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        ) as db:
            tables = {
                str(row[0])
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for label, table in requested.items():
                if table in tables:
                    counts[label] = int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error as exc:
        raise BackupError(f"Unable to inspect Studio SQLite database: {exc}") from exc
    return counts


def _artifact(path: Path, *, kind: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": path.name,
        "kind": kind,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }
    if details:
        payload["details"] = details
    return payload


def create_backup(
    *,
    project_root: Path,
    output_root: Path,
    studio_root: Path,
    learning_root: Path,
    feedback_root: Path,
    reference_root: Path,
    runtime_core_root: Path,
    include_reference: bool = False,
) -> Path:
    project_root = project_root.resolve()
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    _chmod_private(output_root, directory=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_name = f"jin-model-{timestamp}"
    staging = output_root / f".{backup_name}.partial-{os.getpid()}"
    final = output_root / backup_name
    if staging.exists() or final.exists():
        raise BackupError(f"Backup destination already exists: {final}")
    staging.mkdir()
    _chmod_private(staging, directory=True)

    artifacts: list[dict[str, Any]] = []
    try:
        database_path = studio_root.resolve() / "studio.sqlite3"
        database_snapshot = staging / "studio.sqlite3"
        database_details = _sqlite_snapshot(database_path, database_snapshot)
        if database_details is not None:
            artifacts.append(
                _artifact(database_snapshot, kind="studio_database", details=database_details)
            )

        components = [
            (
                studio_root.resolve(),
                staging / "studio-files.tar.gz",
                "studio_files",
                DEFAULT_EXCLUDED_STUDIO_FILES,
            ),
            (
                feedback_root.resolve(),
                staging / "feedback.tar.gz",
                "feedback",
                frozenset(),
            ),
            (
                learning_root.resolve(),
                staging / "learning.tar.gz",
                "learned_models",
                frozenset(),
            ),
            (
                runtime_core_root.resolve(),
                staging / "runtime-core.tar.gz",
                "runtime_core",
                frozenset(),
            ),
        ]
        if include_reference:
            components.append(
                (
                    reference_root.resolve(),
                    staging / "reference.tar.gz",
                    "reference_data",
                    frozenset(),
                )
            )
        for source, archive, kind, exclusions in components:
            details = _archive_directory(source, archive, excluded_names=exclusions)
            if details is not None:
                artifacts.append(_artifact(archive, kind=kind, details=details))

        manifest = {
            "schema": BACKUP_SCHEMA,
            "created_at": _utc_now(),
            "hostname": socket.gethostname(),
            "git_revision": _git_revision(project_root),
            "include_reference": include_reference,
            "security": {
                "contains_personal_documents": True,
                "contains_password_hashes": database_details is not None,
                "service_tokens_included": False,
                "active_sessions_revoked_on_restore": True,
            },
            "excluded": [
                "environment files and application secrets",
                "Studio service token files",
                *(
                    []
                    if include_reference
                    else ["BAN/reference data (reproducible; use --include-reference to include it)"]
                ),
            ],
            "artifacts": artifacts,
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _chmod_private(manifest_path)
        verify_backup(staging)
        staging.rename(final)
        return final
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _load_manifest(backup_dir: Path) -> dict[str, Any]:
    path = backup_dir / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"Invalid or missing manifest: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != BACKUP_SCHEMA:
        raise BackupError(f"Unsupported backup schema in {path}")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise BackupError("Backup manifest has no artifacts")
    return payload


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    backup_dir = backup_dir.resolve()
    manifest = _load_manifest(backup_dir)
    seen: set[str] = set()
    for artifact in manifest["artifacts"]:
        if not isinstance(artifact, dict):
            raise BackupError("Malformed artifact entry")
        name = str(artifact.get("name") or "")
        safe_name = _safe_archive_name(name)
        if len(safe_name.parts) != 1 or name in seen:
            raise BackupError(f"Invalid or duplicate artifact name: {name!r}")
        seen.add(name)
        path = backup_dir / name
        if not path.is_file():
            raise BackupError(f"Missing backup artifact: {path}")
        if path.stat().st_size != int(artifact.get("bytes", -1)):
            raise BackupError(f"Size mismatch for {name}")
        if _sha256(path) != artifact.get("sha256"):
            raise BackupError(f"SHA-256 mismatch for {name}")
        kind = artifact.get("kind")
        if name.endswith(".tar.gz"):
            _validate_tar(path)
        elif kind == "studio_database":
            try:
                with closing(
                    sqlite3.connect(
                        f"file:{path.resolve().as_posix()}?mode=ro", uri=True
                    )
                ) as database:
                    result = database.execute("PRAGMA quick_check").fetchone()
            except sqlite3.Error as exc:
                raise BackupError(f"Invalid Studio database snapshot: {exc}") from exc
            if not result or result[0] != "ok":
                raise BackupError(f"Studio database integrity failure: {result}")
    return manifest


def _safe_extract(archive: Path, destination: Path) -> None:
    _validate_tar(archive)
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle.getmembers():
            relative = _safe_archive_name(member.name)
            target = (destination / Path(*relative.parts)).resolve()
            if target != root and root not in target.parents:
                raise BackupError(f"Archive escapes restore directory: {member.name}")
        handle.extractall(destination, filter="data")


def _directory_empty(path: Path) -> bool:
    return not path.exists() or (path.is_dir() and next(path.iterdir(), None) is None)


def _revoke_restored_sessions(database_path: Path) -> int:
    with closing(sqlite3.connect(database_path)) as database:
        table_exists = database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_sessions'"
        ).fetchone()
        if not table_exists:
            return 0
        timestamp = _utc_now()
        cursor = database.execute(
            "UPDATE studio_sessions SET revoked_at=? WHERE revoked_at IS NULL", (timestamp,)
        )
        database.commit()
        return max(0, int(cursor.rowcount))


def restore_backup(
    backup_dir: Path,
    *,
    target_root: Path,
    confirmation: str,
    restore_reference: bool = False,
) -> dict[str, Any]:
    if confirmation != RESTORE_CONFIRMATION:
        raise BackupError(
            f"Restore requires --confirm {RESTORE_CONFIRMATION}; no data was changed"
        )
    backup_dir = backup_dir.resolve()
    target_root = target_root.resolve()
    manifest = verify_backup(backup_dir)
    artifacts = {item["kind"]: backup_dir / item["name"] for item in manifest["artifacts"]}
    targets: dict[str, Path] = {}
    if "studio_database" in artifacts or "studio_files" in artifacts:
        targets["studio"] = target_root / "data" / "studio"
    if "learned_models" in artifacts:
        targets["learning"] = target_root / "data" / "learning"
    if "feedback" in artifacts:
        targets["feedback"] = target_root / "feedback"
    if "runtime_core" in artifacts:
        targets["runtime_core"] = target_root / "model"
    if restore_reference and "reference_data" not in artifacts:
        raise BackupError("This backup does not contain reference data")
    if restore_reference:
        targets["reference"] = target_root / "data" / "reference"
    non_empty = [str(path) for path in targets.values() if not _directory_empty(path)]
    if non_empty:
        raise BackupError(
            "Restore targets must be absent or empty; refusing to overwrite: "
            + ", ".join(non_empty)
        )

    target_root.mkdir(parents=True, exist_ok=True)
    stage = target_root / f".jin-restore-{uuid.uuid4().hex}"
    stage.mkdir()
    revoked_sessions = 0
    try:
        studio_stage = stage / "data" / "studio"
        if "studio_files" in artifacts:
            _safe_extract(artifacts["studio_files"], studio_stage)
        if "studio_database" in artifacts:
            studio_stage.mkdir(parents=True, exist_ok=True)
            restored_database = studio_stage / "studio.sqlite3"
            shutil.copy2(artifacts["studio_database"], restored_database)
            revoked_sessions = _revoke_restored_sessions(restored_database)
        if "learned_models" in artifacts:
            _safe_extract(artifacts["learned_models"], stage / "data" / "learning")
        if "feedback" in artifacts:
            _safe_extract(artifacts["feedback"], stage / "feedback")
        if "runtime_core" in artifacts:
            _safe_extract(artifacts["runtime_core"], stage / "model")
        if restore_reference and "reference_data" in artifacts:
            _safe_extract(artifacts["reference_data"], stage / "data" / "reference")

        for key, destination in targets.items():
            source = {
                "studio": stage / "data" / "studio",
                "learning": stage / "data" / "learning",
                "feedback": stage / "feedback",
                "runtime_core": stage / "model",
                "reference": stage / "data" / "reference",
            }[key]
            if not source.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                destination.rmdir()
            shutil.move(str(source), str(destination))
        return {
            "target_root": str(target_root),
            "revoked_sessions": revoked_sessions,
            "reference_restored": "reference" in targets,
        }
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def create_bundle(backup_dir: Path, output: Path | None = None) -> Path:
    backup_dir = backup_dir.resolve()
    verify_backup(backup_dir)
    output = (output or backup_dir.with_suffix(".tar.gz")).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.partial-{os.getpid()}")
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            for path in sorted(backup_dir.rglob("*")):
                if path.is_symlink():
                    raise BackupError(f"Symbolic link rejected while bundling: {path}")
                if path.is_file():
                    relative = Path(backup_dir.name) / path.relative_to(backup_dir)
                    archive.add(path, arcname=relative.as_posix(), recursive=False)
        _chmod_private(temporary)
        _validate_tar(temporary)
        temporary.replace(output)
        return output
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def unpack_bundle(bundle: Path, destination_root: Path) -> Path:
    bundle = bundle.resolve()
    destination_root = destination_root.resolve()
    _validate_tar(bundle)
    with tarfile.open(bundle, "r:gz") as archive:
        top_levels = {
            _safe_archive_name(member.name).parts[0]
            for member in archive.getmembers()
            if member.name
        }
    if len(top_levels) != 1:
        raise BackupError("A backup bundle must contain exactly one backup directory")
    destination = destination_root / next(iter(top_levels))
    if destination.exists():
        raise BackupError(f"Bundle destination already exists: {destination}")
    destination_root.mkdir(parents=True, exist_ok=True)
    _safe_extract(bundle, destination_root)
    verify_backup(destination)
    return destination


def _azure_value(explicit: str | None, environment_name: str) -> str:
    value = str(explicit or os.getenv(environment_name) or "").strip()
    if not value:
        raise BackupError(f"Missing {environment_name}")
    return value


def azure_upload(
    backup_dir: Path,
    *,
    account: str | None,
    container: str | None,
    prefix: str | None,
) -> str:
    account_name = _azure_value(account, "JIN_BACKUP_AZURE_ACCOUNT")
    container_name = _azure_value(container, "JIN_BACKUP_AZURE_CONTAINER")
    blob_prefix = str(prefix or os.getenv("JIN_BACKUP_AZURE_PREFIX") or "production").strip("/")
    bundle = create_bundle(backup_dir)
    blob_name = f"{blob_prefix}/{bundle.name}" if blob_prefix else bundle.name
    bundle_sha256 = _sha256(bundle)
    command = [
        "az",
        "storage",
        "blob",
        "upload",
        "--auth-mode",
        "login",
        "--account-name",
        account_name,
        "--container-name",
        container_name,
        "--name",
        blob_name,
        "--file",
        str(bundle),
        "--overwrite",
        "false",
        "--metadata",
        f"sha256={bundle_sha256}",
        "--only-show-errors",
    ]
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as exc:
        raise BackupError("Azure CLI ('az') is not installed") from exc
    except subprocess.CalledProcessError as exc:
        raise BackupError(f"Azure upload failed with exit code {exc.returncode}") from exc
    try:
        remote_size = subprocess.run(
            [
                "az",
                "storage",
                "blob",
                "show",
                "--auth-mode",
                "login",
                "--account-name",
                account_name,
                "--container-name",
                container_name,
                "--name",
                blob_name,
                "--query",
                "properties.contentLength",
                "--output",
                "tsv",
                "--only-show-errors",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise BackupError("Azure upload completed but remote verification failed") from exc
    try:
        remote_size_bytes = int(remote_size)
    except ValueError as exc:
        raise BackupError(f"Unexpected Azure blob size response: {remote_size!r}") from exc
    if remote_size_bytes != bundle.stat().st_size:
        raise BackupError(
            f"Azure blob size mismatch for {blob_name}: {remote_size} != {bundle.stat().st_size}"
        )
    return blob_name


def azure_download(
    blob_name: str,
    *,
    destination_root: Path,
    account: str | None,
    container: str | None,
) -> Path:
    account_name = _azure_value(account, "JIN_BACKUP_AZURE_ACCOUNT")
    container_name = _azure_value(container, "JIN_BACKUP_AZURE_CONTAINER")
    destination_root = destination_root.resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    bundle_name = Path(blob_name).name
    if not bundle_name.endswith(".tar.gz"):
        raise BackupError(f"Unexpected Azure backup blob name: {blob_name!r}")
    bundle = destination_root / bundle_name
    if bundle.exists():
        raise BackupError(f"Download destination already exists: {bundle}")
    temporary = bundle.with_name(f".{bundle.name}.partial-{os.getpid()}")
    try:
        subprocess.run(
            [
                "az",
                "storage",
                "blob",
                "download",
                "--auth-mode",
                "login",
                "--account-name",
                account_name,
                "--container-name",
                container_name,
                "--name",
                blob_name,
                "--file",
                str(temporary),
                "--only-show-errors",
            ],
            check=True,
        )
        metadata = subprocess.run(
            [
                "az",
                "storage",
                "blob",
                "show",
                "--auth-mode",
                "login",
                "--account-name",
                account_name,
                "--container-name",
                container_name,
                "--name",
                blob_name,
                "--query",
                "metadata.sha256",
                "--output",
                "tsv",
                "--only-show-errors",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except FileNotFoundError as exc:
        raise BackupError("Azure CLI ('az') is not installed") from exc
    except subprocess.CalledProcessError as exc:
        raise BackupError(f"Azure download failed with exit code {exc.returncode}") from exc
    try:
        if metadata and metadata.lower() not in {"none", "null"}:
            downloaded_sha256 = _sha256(temporary)
            if downloaded_sha256 != metadata:
                raise BackupError(
                    f"Downloaded bundle SHA-256 mismatch: {downloaded_sha256} != {metadata}"
                )
        temporary.replace(bundle)
        return unpack_bundle(bundle, destination_root)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _resolved_paths(args: argparse.Namespace) -> dict[str, Path]:
    project = Path(args.project_root).resolve()
    return {
        "project_root": project,
        "output_root": Path(args.output_root).resolve(),
        "studio_root": Path(args.studio_root or project / "data" / "studio").resolve(),
        "learning_root": Path(args.learning_root or project / "data" / "learning").resolve(),
        "feedback_root": Path(args.feedback_root or project / "feedback").resolve(),
        "reference_root": Path(args.reference_root or project / "data" / "reference").resolve(),
        "runtime_core_root": Path(args.runtime_core_root or project / "model").resolve(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create and verify a local backup")
    create.add_argument("--project-root", default=".")
    create.add_argument("--output-root", default=os.getenv("JIN_BACKUP_ROOT") or "backups")
    create.add_argument("--studio-root")
    create.add_argument("--learning-root")
    create.add_argument("--feedback-root")
    create.add_argument("--reference-root")
    create.add_argument("--runtime-core-root")
    create.add_argument("--include-reference", action="store_true")

    verify = subparsers.add_parser("verify", help="verify hashes, archives and SQLite")
    verify.add_argument("backup_dir")

    restore = subparsers.add_parser("restore", help="restore only into empty managed targets")
    restore.add_argument("backup_dir")
    restore.add_argument("--target-root", required=True)
    restore.add_argument("--confirm", required=True)
    restore.add_argument("--restore-reference", action="store_true")

    bundle = subparsers.add_parser("bundle", help="create one transport archive")
    bundle.add_argument("backup_dir")
    bundle.add_argument("--output")

    unpack = subparsers.add_parser("unpack", help="safely unpack and verify a transport archive")
    unpack.add_argument("bundle")
    unpack.add_argument("--destination-root", required=True)

    upload = subparsers.add_parser(
        "azure-upload", help="verify, bundle and upload with Azure managed identity"
    )
    upload.add_argument("backup_dir")
    upload.add_argument("--account")
    upload.add_argument("--container")
    upload.add_argument("--prefix")

    download = subparsers.add_parser(
        "azure-download", help="download, verify and unpack an Azure backup bundle"
    )
    download.add_argument("blob_name")
    download.add_argument("--destination-root", required=True)
    download.add_argument("--account")
    download.add_argument("--container")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            backup = create_backup(include_reference=args.include_reference, **_resolved_paths(args))
            print(backup)
        elif args.command == "verify":
            manifest = verify_backup(Path(args.backup_dir))
            print(
                json.dumps(
                    {
                        "valid": True,
                        "created_at": manifest["created_at"],
                        "artifacts": len(manifest["artifacts"]),
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "restore":
            result = restore_backup(
                Path(args.backup_dir),
                target_root=Path(args.target_root),
                confirmation=args.confirm,
                restore_reference=args.restore_reference,
            )
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "bundle":
            output = Path(args.output) if args.output else None
            print(create_bundle(Path(args.backup_dir), output))
        elif args.command == "unpack":
            print(unpack_bundle(Path(args.bundle), Path(args.destination_root)))
        elif args.command == "azure-upload":
            print(
                azure_upload(
                    Path(args.backup_dir),
                    account=args.account,
                    container=args.container,
                    prefix=args.prefix,
                )
            )
        elif args.command == "azure-download":
            print(
                azure_download(
                    args.blob_name,
                    destination_root=Path(args.destination_root),
                    account=args.account,
                    container=args.container,
                )
            )
        else:  # pragma: no cover - argparse guarantees this cannot happen.
            raise BackupError(f"Unsupported command: {args.command}")
    except BackupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

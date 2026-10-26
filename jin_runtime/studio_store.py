"""SQLite-backed storage for JIN Studio documents, annotations and datasets."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from jin_runtime.studio_contract import normalize_studio_annotation, utc_now
from jin_runtime.studio_security import (
    DUMMY_PASSWORD_HASH,
    StudioPrincipal,
    StudioSecurityError,
    hash_password,
    normalize_roles,
    normalize_username,
    permissions_for_roles,
    random_token,
    session_duration_hours,
    token_digest,
    verify_password,
)


MAX_PDF_BYTES = 100 * 1024 * 1024
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
ALLOWED_BASE_MODELS = frozenset({"microsoft/layoutlmv3-base"})
SQLITE_CONNECT_ATTEMPTS = 5
_RETRYABLE_SQLITE_MESSAGES = (
    "unable to open database file",
    "database is locked",
    "database table is locked",
    "disk i/o error",
)


def minimum_training_documents() -> int:
    try:
        configured = int(os.getenv("JIN_STUDIO_MIN_TRAINING_DOCUMENTS") or "20")
    except ValueError:
        configured = 20
    return max(2, min(10000, configured))


class StudioNotFoundError(LookupError):
    pass


class StudioConflictError(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _filename(value: str) -> str:
    name = Path(str(value or "document.pdf")).name
    cleaned = _SAFE_FILENAME.sub("_", name).strip("._") or "document.pdf"
    return cleaned[:180]


class StudioStore:
    def __init__(self, root: str | Path | None = None) -> None:
        configured = root or os.getenv("JIN_STUDIO_DIR") or "data/studio"
        self.root = Path(configured).resolve()
        self.documents_dir = self.root / "documents"
        self.datasets_dir = self.root / "datasets"
        self.root.mkdir(parents=True, exist_ok=True)
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        self.datasets_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "studio.sqlite3"
        self.shared_storage = str(
            os.getenv("JIN_STUDIO_SHARED_STORAGE") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._lock = threading.RLock()
        self._apply_shared_permissions()
        self._initialize()
        self._apply_shared_permissions()

    def _apply_shared_permissions(self) -> None:
        """Allow API and trainer user namespaces to share only Studio storage."""

        if not self.shared_storage:
            return
        for directory in (self.root, self.documents_dir, self.datasets_dir):
            try:
                directory.chmod(0o777)
            except OSError:
                pass
        for path in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            if not path.exists():
                continue
            try:
                path.chmod(0o666)
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection:
        for attempt in range(SQLITE_CONNECT_ATTEMPTS):
            connection: sqlite3.Connection | None = None
            try:
                self._apply_shared_permissions()
                connection = sqlite3.connect(self.db_path, timeout=30)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
                self._apply_shared_permissions()
                return connection
            except sqlite3.OperationalError as exc:
                if connection is not None:
                    connection.close()
                retryable = any(
                    message in str(exc).lower()
                    for message in _RETRYABLE_SQLITE_MESSAGES
                )
                if not retryable or attempt + 1 >= SQLITE_CONNECT_ATTEMPTS:
                    raise
                # Docker Desktop bind mounts can briefly reject SQLite WAL/SHM
                # files while a sibling API, MCP or trainer container restarts.
                time.sleep(0.1 * (attempt + 1))

        raise RuntimeError("unreachable SQLite connection retry state")

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS studio_documents (
                    document_id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    pdf_sha256 TEXT NOT NULL,
                    pdf_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    extraction_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_studio_documents_status
                    ON studio_documents(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_studio_documents_sha
                    ON studio_documents(pdf_sha256);

                CREATE TABLE IF NOT EXISTS studio_annotations (
                    annotation_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES studio_documents(document_id),
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(document_id, revision)
                );
                CREATE INDEX IF NOT EXISTS idx_studio_annotations_document
                    ON studio_annotations(document_id, revision DESC);

                CREATE TABLE IF NOT EXISTS studio_datasets (
                    dataset_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(name, version)
                );

                CREATE TABLE IF NOT EXISTS studio_training_runs (
                    run_id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL REFERENCES studio_datasets(dataset_id),
                    status TEXT NOT NULL,
                    model_family TEXT NOT NULL,
                    base_model TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS studio_users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    roles_json TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    last_login_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_studio_users_active
                    ON studio_users(active, username);

                CREATE TABLE IF NOT EXISTS studio_sessions (
                    session_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES studio_users(user_id),
                    csrf_token TEXT NOT NULL,
                    client_ip TEXT NOT NULL,
                    user_agent TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_studio_sessions_user
                    ON studio_sessions(user_id, expires_at DESC);

                CREATE TABLE IF NOT EXISTS studio_audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_user_id TEXT,
                    actor_username TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    client_ip TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_studio_audit_created
                    ON studio_audit_events(created_at DESC);
                """
            )

    @staticmethod
    def _user(row: sqlite3.Row) -> dict[str, Any]:
        roles = normalize_roles(_loads(row["roles_json"], []))
        return {
            "userId": row["user_id"],
            "username": row["username"],
            "displayName": row["display_name"],
            "roles": list(roles),
            "permissions": sorted(permissions_for_roles(roles)),
            "active": bool(row["active"]),
            "lockedUntil": row["locked_until"],
            "lastLoginAt": row["last_login_at"],
            "createdBy": row["created_by"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _principal(
        row: sqlite3.Row,
        *,
        session_hash: str = "",
        csrf_token: str = "",
        expires_at: str = "",
    ) -> StudioPrincipal:
        roles = normalize_roles(_loads(row["roles_json"], []))
        return StudioPrincipal(
            user_id=row["user_id"],
            username=row["username"],
            display_name=row["display_name"],
            roles=roles,
            permissions=permissions_for_roles(roles),
            auth_type="session",
            session_hash=session_hash,
            csrf_token=csrf_token,
            expires_at=expires_at,
        )

    @staticmethod
    def _active_admin_count(connection: sqlite3.Connection) -> int:
        rows = connection.execute(
            "SELECT roles_json FROM studio_users WHERE active=1"
        ).fetchall()
        return sum(
            1 for row in rows if "ADMIN" in _loads(row["roles_json"], [])
        )

    def user_count(self) -> int:
        with self._connect() as connection:
            return int(
                connection.execute("SELECT COUNT(*) count FROM studio_users").fetchone()[
                    "count"
                ]
            )

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        roles: list[str] | tuple[str, ...],
        created_by: str,
    ) -> dict[str, Any]:
        clean_username = normalize_username(username)
        clean_name = str(display_name or "").strip()
        if len(clean_name) < 2 or len(clean_name) > 120:
            raise StudioSecurityError(
                "Le nom affiché doit contenir entre 2 et 120 caractères."
            )
        clean_roles = normalize_roles(roles)
        encoded_password = hash_password(password)
        user_id = f"jusr-{uuid.uuid4().hex}"
        now = utc_now()
        try:
            with self._lock, self._connect() as connection:
                connection.execute(
                    """INSERT INTO studio_users
                       (user_id, username, display_name, password_hash, roles_json,
                        active, failed_attempts, created_by, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, ?)""",
                    (
                        user_id,
                        clean_username,
                        clean_name,
                        encoded_password,
                        _json(list(clean_roles)),
                        str(created_by or "system")[:120],
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StudioConflictError("Ce nom d'utilisateur existe déjà.") from exc
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM studio_users WHERE user_id=?", (user_id,)
            ).fetchone()
        if row is None:
            raise StudioNotFoundError(user_id)
        return self._user(row)

    def list_users(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM studio_users ORDER BY active DESC, username"
            ).fetchall()
        return [self._user(row) for row in rows]

    def update_user(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        roles: list[str] | tuple[str, ...] | None = None,
        active: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM studio_users WHERE user_id=?", (user_id,)
            ).fetchone()
            if row is None:
                raise StudioNotFoundError(user_id)
            current_roles = normalize_roles(_loads(row["roles_json"], []))
            clean_roles = normalize_roles(roles) if roles is not None else current_roles
            clean_name = (
                str(display_name or "").strip()
                if display_name is not None
                else row["display_name"]
            )
            if len(clean_name) < 2 or len(clean_name) > 120:
                raise StudioSecurityError(
                    "Le nom affiché doit contenir entre 2 et 120 caractères."
                )
            next_active = bool(active) if active is not None else bool(row["active"])
            removes_admin = "ADMIN" in current_roles and (
                "ADMIN" not in clean_roles or not next_active
            )
            if removes_admin and self._active_admin_count(connection) <= 1:
                raise StudioConflictError(
                    "Le dernier administrateur actif ne peut pas être désactivé."
                )
            now = utc_now()
            connection.execute(
                """UPDATE studio_users
                   SET display_name=?, roles_json=?, active=?, updated_at=?
                   WHERE user_id=?""",
                (clean_name, _json(list(clean_roles)), int(next_active), now, user_id),
            )
            if not next_active:
                connection.execute(
                    """UPDATE studio_sessions SET revoked_at=?
                       WHERE user_id=? AND revoked_at IS NULL""",
                    (now, user_id),
                )
        return self.get_user(user_id)

    def set_user_password(self, user_id: str, password: str) -> None:
        encoded = hash_password(password)
        now = utc_now()
        with self._lock, self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM studio_users WHERE user_id=?", (user_id,)
            ).fetchone()
            if exists is None:
                raise StudioNotFoundError(user_id)
            connection.execute(
                """UPDATE studio_users
                   SET password_hash=?, failed_attempts=0, locked_until=NULL, updated_at=?
                   WHERE user_id=?""",
                (encoded, now, user_id),
            )
            connection.execute(
                """UPDATE studio_sessions SET revoked_at=?
                   WHERE user_id=? AND revoked_at IS NULL""",
                (now, user_id),
            )

    def verify_user_password(self, user_id: str, password: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT password_hash, active FROM studio_users WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return bool(row and row["active"] and verify_password(password, row["password_hash"]))

    def authenticate_user(self, username: str, password: str) -> dict[str, Any] | None:
        supplied_username = str(username or "").strip().lower()
        now = utc_now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM studio_users WHERE username=? COLLATE NOCASE",
                (supplied_username,),
            ).fetchone()
            encoded = row["password_hash"] if row is not None else DUMMY_PASSWORD_HASH
            valid_password = verify_password(password, encoded)
            if row is None:
                return None
            locked_until = str(row["locked_until"] or "")
            if not row["active"] or (locked_until and locked_until > now):
                return None
            if not valid_password:
                attempts = int(row["failed_attempts"] or 0) + 1
                next_locked = None
                if attempts >= 5:
                    next_locked = (
                        datetime.now(timezone.utc) + timedelta(minutes=15)
                    ).isoformat().replace("+00:00", "Z")
                    attempts = 0
                connection.execute(
                    """UPDATE studio_users
                       SET failed_attempts=?, locked_until=?, updated_at=?
                       WHERE user_id=?""",
                    (attempts, next_locked, now, row["user_id"]),
                )
                return None
            connection.execute(
                """UPDATE studio_users
                   SET failed_attempts=0, locked_until=NULL, last_login_at=?, updated_at=?
                   WHERE user_id=?""",
                (now, now, row["user_id"]),
            )
            refreshed = connection.execute(
                "SELECT * FROM studio_users WHERE user_id=?", (row["user_id"],)
            ).fetchone()
        return self._user(refreshed)

    def create_session(
        self,
        user_id: str,
        *,
        client_ip: str = "",
        user_agent: str = "",
    ) -> tuple[str, StudioPrincipal]:
        token = random_token()
        csrf = random_token()
        digest = token_digest(token)
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat().replace("+00:00", "Z")
        expires = (now_dt + timedelta(hours=session_duration_hours())).isoformat().replace(
            "+00:00", "Z"
        )
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM studio_users WHERE user_id=? AND active=1", (user_id,)
            ).fetchone()
            if row is None:
                raise StudioNotFoundError(user_id)
            connection.execute(
                """INSERT INTO studio_sessions
                   (session_hash, user_id, csrf_token, client_ip, user_agent,
                    created_at, last_seen_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    digest,
                    user_id,
                    csrf,
                    str(client_ip or "")[:128],
                    str(user_agent or "")[:500],
                    now,
                    now,
                    expires,
                ),
            )
        return token, self._principal(
            row,
            session_hash=digest,
            csrf_token=csrf,
            expires_at=expires,
        )

    def resolve_session(self, token: str) -> StudioPrincipal | None:
        digest = token_digest(token)
        now = utc_now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT u.*, s.csrf_token, s.expires_at
                   FROM studio_sessions s
                   JOIN studio_users u ON u.user_id=s.user_id
                   WHERE s.session_hash=? AND s.revoked_at IS NULL
                     AND s.expires_at>? AND u.active=1""",
                (digest, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE studio_sessions SET last_seen_at=? WHERE session_hash=?",
                (now, digest),
            )
        return self._principal(
            row,
            session_hash=digest,
            csrf_token=row["csrf_token"],
            expires_at=row["expires_at"],
        )

    def revoke_session(self, session_hash: str) -> None:
        if not session_hash:
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE studio_sessions SET revoked_at=?
                   WHERE session_hash=? AND revoked_at IS NULL""",
                (utc_now(), session_hash),
            )

    def record_audit(
        self,
        *,
        actor_user_id: str = "",
        actor_username: str,
        action: str,
        target_type: str,
        target_id: str = "",
        details: Mapping[str, Any] | None = None,
        client_ip: str = "",
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO studio_audit_events
                   (actor_user_id, actor_username, action, target_type, target_id,
                    details_json, client_ip, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    actor_user_id or None,
                    str(actor_username or "unknown")[:120],
                    str(action or "unknown")[:120],
                    str(target_type or "unknown")[:80],
                    str(target_id or "")[:180],
                    _json(dict(details or {})),
                    str(client_ip or "")[:128],
                    utc_now(),
                ),
            )

    def list_audit_events(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM studio_audit_events
                   ORDER BY event_id DESC LIMIT ?""",
                (max(1, min(1000, int(limit))),),
            ).fetchall()
        return [
            {
                "eventId": row["event_id"],
                "actorUserId": row["actor_user_id"],
                "actorUsername": row["actor_username"],
                "action": row["action"],
                "targetType": row["target_type"],
                "targetId": row["target_id"],
                "details": _loads(row["details_json"], {}),
                "clientIp": row["client_ip"],
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            documents = connection.execute(
                "SELECT status, COUNT(*) count FROM studio_documents GROUP BY status"
            ).fetchall()
            annotation_count = connection.execute(
                "SELECT COUNT(*) count FROM studio_annotations"
            ).fetchone()["count"]
            datasets = connection.execute(
                "SELECT COUNT(*) count FROM studio_datasets"
            ).fetchone()["count"]
            runs = connection.execute(
                "SELECT COUNT(*) count FROM studio_training_runs"
            ).fetchone()["count"]
        return {
            "documents": {row["status"]: row["count"] for row in documents},
            "annotations": annotation_count,
            "datasets": datasets,
            "trainingRuns": runs,
            "storage": str(self.root),
        }

    def create_document(
        self,
        *,
        filename: str,
        pdf_bytes: bytes,
        extraction: Mapping[str, Any] | None = None,
        source: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not pdf_bytes.startswith(b"%PDF-"):
            raise ValueError("JIN Studio accepts PDF documents only")
        if len(pdf_bytes) > MAX_PDF_BYTES:
            raise ValueError("PDF exceeds the 100 MB limit")
        document_id = f"jdoc-{uuid.uuid4().hex}"
        digest = hashlib.sha256(pdf_bytes).hexdigest()
        document_dir = self.documents_dir / document_id
        document_dir.mkdir(parents=True, exist_ok=False)
        pdf_path = document_dir / "source.pdf"
        pdf_path.write_bytes(pdf_bytes)
        now = utc_now()
        safe_name = _filename(filename)
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO studio_documents
                   (document_id, filename, pdf_sha256, pdf_path, status,
                    source_json, extraction_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?)""",
                (
                    document_id,
                    safe_name,
                    digest,
                    str(pdf_path),
                    _json(dict(source or {})),
                    _json(dict(extraction or {})),
                    now,
                    now,
                ),
            )
        return self.get_document(document_id)

    @staticmethod
    def _document(row: sqlite3.Row, annotation: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "documentId": row["document_id"],
            "filename": row["filename"],
            "pdfSha256": row["pdf_sha256"],
            "status": row["status"],
            "source": _loads(row["source_json"], {}),
            "extraction": _loads(row["extraction_json"], {}),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "latestAnnotation": annotation,
        }

    def list_documents(self, *, status: str = "", limit: int = 200) -> list[dict[str, Any]]:
        query = "SELECT * FROM studio_documents"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status.upper())
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(1000, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
            return [
                self._document(row, self._latest_annotation(connection, row["document_id"]))
                for row in rows
            ]

    def _row(self, connection: sqlite3.Connection, document_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM studio_documents WHERE document_id=?", (document_id,)
        ).fetchone()
        if row is None:
            raise StudioNotFoundError(document_id)
        return row

    def _latest_annotation(
        self, connection: sqlite3.Connection, document_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """SELECT payload_json FROM studio_annotations
               WHERE document_id=? ORDER BY revision DESC LIMIT 1""",
            (document_id,),
        ).fetchone()
        return _loads(row["payload_json"], {}) if row else None

    def get_document(self, document_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._row(connection, document_id)
            return self._document(row, self._latest_annotation(connection, document_id))

    def pdf_path(self, document_id: str) -> Path:
        with self._connect() as connection:
            row = self._row(connection, document_id)
        path = Path(row["pdf_path"]).resolve()
        if self.documents_dir not in path.parents or not path.exists():
            raise StudioNotFoundError(document_id)
        return path

    def save_annotation(self, document_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = self._row(connection, document_id)
            latest = connection.execute(
                "SELECT MAX(revision) revision FROM studio_annotations WHERE document_id=?",
                (document_id,),
            ).fetchone()
            revision = int(latest["revision"] or 0) + 1
            annotation = normalize_studio_annotation(
                payload,
                document_id=document_id,
                document_sha256=row["pdf_sha256"],
                revision=revision,
            )
            now = utc_now()
            connection.execute(
                """INSERT INTO studio_annotations
                   (annotation_id, document_id, revision, status, reviewer,
                    digest, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    annotation["annotation_id"],
                    document_id,
                    revision,
                    annotation["status"],
                    annotation["reviewed_by"],
                    annotation["annotation_digest"],
                    _json(annotation),
                    now,
                ),
            )
            connection.execute(
                "UPDATE studio_documents SET status=?, updated_at=? WHERE document_id=?",
                (annotation["status"], now, document_id),
            )
        return annotation

    def list_annotations(self, document_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            self._row(connection, document_id)
            rows = connection.execute(
                """SELECT payload_json FROM studio_annotations
                   WHERE document_id=? ORDER BY revision DESC""",
                (document_id,),
            ).fetchall()
        return [_loads(row["payload_json"], {}) for row in rows]

    def create_dataset(self, *, name: str, document_ids: list[str]) -> dict[str, Any]:
        clean_name = str(name or "").strip()
        if len(clean_name) < 3 or len(clean_name) > 100:
            raise ValueError("Dataset name must contain 3 to 100 characters")
        unique_ids = list(dict.fromkeys(str(value).strip() for value in document_ids if str(value).strip()))
        if not unique_ids:
            raise ValueError("At least one document is required")
        if len(unique_ids) > 1000:
            raise ValueError("A dataset cannot contain more than 1000 documents")
        with self._lock, self._connect() as connection:
            placeholders = ",".join("?" for _ in unique_ids)
            rows = connection.execute(
                f"SELECT * FROM studio_documents WHERE document_id IN ({placeholders})",
                unique_ids,
            ).fetchall()
            by_id = {row["document_id"]: row for row in rows}
            missing = [item for item in unique_ids if item not in by_id]
            if missing:
                raise StudioNotFoundError(",".join(missing))
            annotations: list[dict[str, Any]] = []
            for document_id in unique_ids:
                annotation = self._latest_annotation(connection, document_id)
                if not annotation or annotation.get("status") != "HUMAN_VALIDATED":
                    raise StudioConflictError(
                        f"Document {document_id} has no human-validated annotation"
                    )
                annotations.append(annotation)
            version_row = connection.execute(
                "SELECT MAX(version) version FROM studio_datasets WHERE name=?",
                (clean_name,),
            ).fetchone()
            version = int(version_row["version"] or 0) + 1
            dataset_id = f"jds-{uuid.uuid4().hex}"
            dataset_dir = self.datasets_dir / dataset_id
            annotations_dir = dataset_dir / "annotations"
            annotations_dir.mkdir(parents=True, exist_ok=False)
            manifest_path = dataset_dir / "manifest.jsonl"
            manifest_rows = []
            region_count = 0
            labels: set[str] = set()
            for document_id, annotation in zip(unique_ids, annotations):
                document = by_id[document_id]
                pages: dict[int, list[dict[str, Any]]] = {}
                for field in annotation["field_annotations"]:
                    evidence = field.get("evidence") or {}
                    bbox = evidence.get("bbox")
                    page = evidence.get("page")
                    if not bbox or not page or field["status"] in {"ABSENT", "AMBIGUOUS"}:
                        continue
                    value = (
                        field.get("corrected_value")
                        if field["status"] == "CORRECTED"
                        else field.get("extracted_value")
                    )
                    region = {
                        "label": field["label"],
                        "bbox": bbox,
                        "bbox_space": "normalized",
                        "value": value,
                        "source_path": field["path"],
                        "annotation_source": "jin_studio_human_validated",
                        "requires_review": False,
                    }
                    pages.setdefault(int(page), []).append(region)
                    labels.add(field["label"])
                    region_count += 1
                annotation_path = annotations_dir / f"{document_id}.json"
                spatial = {
                    "document_id": document_id,
                    "document_sha256": document["pdf_sha256"],
                    "annotation_status": "HUMAN_VALIDATED",
                    "annotation_digest": annotation["annotation_digest"],
                    "pages": [
                        {"page": page, "regions": pages[page]}
                        for page in sorted(pages)
                    ],
                }
                annotation_path.write_text(_json(spatial), encoding="utf-8")
                manifest_rows.append(
                    {
                        "document_id": document_id,
                        "pdf": document["pdf_path"],
                        "annotation": str(annotation_path),
                        "pdf_sha256": document["pdf_sha256"],
                        "annotation_digest": annotation["annotation_digest"],
                    }
                )
            manifest_path.write_text(
                "".join(_json(item) + "\n" for item in manifest_rows), encoding="utf-8"
            )
            minimum_documents = minimum_training_documents()
            payload = {
                "datasetId": dataset_id,
                "name": clean_name,
                "version": version,
                "status": "READY" if region_count and len(unique_ids) >= minimum_documents else "DRAFT",
                "documentCount": len(unique_ids),
                "regionCount": region_count,
                "labels": sorted(labels),
                "documentIds": unique_ids,
                "manifestPath": str(manifest_path),
                "minimumTrainingDocuments": minimum_documents,
                "warnings": [
                    message
                    for condition, message in (
                        (
                            len(unique_ids) < minimum_documents,
                            f"At least {minimum_documents} reviewed documents are required before training.",
                        ),
                        (region_count == 0, "No reviewed spatial region is available."),
                    )
                    if condition
                ],
                "createdAt": utc_now(),
            }
            connection.execute(
                """INSERT INTO studio_datasets
                   (dataset_id, name, version, status, manifest_path, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    dataset_id,
                    clean_name,
                    version,
                    payload["status"],
                    str(manifest_path),
                    _json(payload),
                    payload["createdAt"],
                ),
            )
        return payload

    def list_datasets(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM studio_datasets ORDER BY created_at DESC"
            ).fetchall()
        return [_loads(row["payload_json"], {}) for row in rows]

    def prepare_training_run(
        self,
        *,
        dataset_id: str,
        model_family: str = "layoutlmv3",
        base_model: str = "microsoft/layoutlmv3-base",
    ) -> dict[str, Any]:
        if model_family != "layoutlmv3":
            raise ValueError("Only the reviewed LayoutLMv3 pipeline is currently supported")
        if base_model not in ALLOWED_BASE_MODELS:
            raise ValueError("Unsupported LayoutLMv3 base model")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM studio_datasets WHERE dataset_id=?",
                (dataset_id,),
            ).fetchone()
            if row is None:
                raise StudioNotFoundError(dataset_id)
            dataset = _loads(row["payload_json"], {})
            if dataset.get("status") != "READY":
                raise StudioConflictError(
                    "Dataset is not training-ready; resolve its warnings first"
                )
            run_id = f"jrun-{uuid.uuid4().hex}"
            output_root = self.root / "training" / run_id
            layout_dir = output_root / "layout"
            artifact_dir = output_root / "artifact"
            steps = [
                {
                    "name": "prepare_layout_dataset",
                    "command": [
                        "python",
                        "training/vision/prepare_layout_dataset.py",
                        "--manifest",
                        dataset["manifestPath"],
                        "--output",
                        str(layout_dir),
                    ],
                },
                {
                    "name": "fine_tune_layoutlmv3",
                    "command": [
                        "python",
                        "training/vision/train_layoutlmv3.py",
                        "--data",
                        str(layout_dir),
                        "--output",
                        str(artifact_dir),
                        "--model",
                        base_model,
                    ],
                },
            ]
            now = utc_now()
            payload = {
                "runId": run_id,
                "datasetId": dataset_id,
                "status": "QUEUED",
                "modelFamily": model_family,
                "baseModel": base_model,
                "steps": steps,
                "command": " && ".join(
                    " ".join(step["command"]) for step in steps
                ),
                "outputDirectory": str(output_root),
                "logPath": str(output_root / "training.log"),
                "createdAt": now,
                "updatedAt": now,
                "note": "Queued for the isolated JIN Studio training worker.",
            }
            connection.execute(
                """INSERT INTO studio_training_runs
                   (run_id, dataset_id, status, model_family, base_model,
                    payload_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    dataset_id,
                    payload["status"],
                    model_family,
                    base_model,
                    _json(payload),
                    now,
                    now,
                ),
            )
        return payload

    def list_training_runs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM studio_training_runs ORDER BY created_at DESC"
            ).fetchall()
        return [_loads(row["payload_json"], {}) for row in rows]

    def claim_training_run(self, worker_id: str) -> dict[str, Any] | None:
        normalized_worker = str(worker_id or "").strip()[:160]
        if not normalized_worker:
            raise ValueError("worker_id is required")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT run_id, payload_json FROM studio_training_runs
                   WHERE status='QUEUED' ORDER BY created_at ASC LIMIT 1"""
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            payload = _loads(row["payload_json"], {})
            now = utc_now()
            payload.update(
                status="RUNNING",
                workerId=normalized_worker,
                startedAt=now,
                updatedAt=now,
            )
            connection.execute(
                """UPDATE studio_training_runs
                   SET status='RUNNING', payload_json=?, updated_at=?
                   WHERE run_id=? AND status='QUEUED'""",
                (_json(payload), now, row["run_id"]),
            )
            connection.commit()
            return payload

    def heartbeat_training_run(
        self,
        run_id: str,
        *,
        current_step: str,
        completed_steps: list[str],
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM studio_training_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise StudioNotFoundError(run_id)
            payload = _loads(row["payload_json"], {})
            if payload.get("status") != "RUNNING":
                raise StudioConflictError("Training run is not running")
            now = utc_now()
            payload.update(
                currentStep=str(current_step or "")[:160],
                completedSteps=list(dict.fromkeys(completed_steps)),
                updatedAt=now,
            )
            connection.execute(
                """UPDATE studio_training_runs
                   SET payload_json=?, updated_at=? WHERE run_id=?""",
                (_json(payload), now, run_id),
            )
        return payload

    def recover_stale_training_runs(self, stale_seconds: int = 900) -> int:
        """Return abandoned runs to the queue after a worker stopped heartbeating."""

        seconds = max(60, min(86400, int(stale_seconds)))
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=seconds)
        ).isoformat().replace("+00:00", "Z")
        recovered = 0
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT run_id, payload_json FROM studio_training_runs
                   WHERE status='RUNNING' AND updated_at < ?""",
                (cutoff,),
            ).fetchall()
            for row in rows:
                payload = _loads(row["payload_json"], {})
                now = utc_now()
                payload.update(
                    status="QUEUED",
                    updatedAt=now,
                    note="Recovered after the previous training worker stopped responding.",
                    recoveryCount=int(payload.get("recoveryCount") or 0) + 1,
                )
                payload.pop("workerId", None)
                payload.pop("currentStep", None)
                connection.execute(
                    """UPDATE studio_training_runs
                       SET status='QUEUED', payload_json=?, updated_at=?
                       WHERE run_id=? AND status='RUNNING'""",
                    (_json(payload), now, row["run_id"]),
                )
                recovered += 1
            connection.commit()
        return recovered

    def finish_training_run(
        self,
        run_id: str,
        *,
        success: bool,
        error: str = "",
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM studio_training_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise StudioNotFoundError(run_id)
            payload = _loads(row["payload_json"], {})
            if payload.get("status") != "RUNNING":
                raise StudioConflictError("Training run is not running")
            now = utc_now()
            payload.update(
                status="SUCCEEDED" if success else "FAILED",
                finishedAt=now,
                updatedAt=now,
            )
            payload.pop("currentStep", None)
            if error:
                payload["error"] = str(error)[:4000]
            connection.execute(
                """UPDATE studio_training_runs
                   SET status=?, payload_json=?, updated_at=? WHERE run_id=?""",
                (payload["status"], _json(payload), now, run_id),
            )
        return payload


__all__ = [
    "ALLOWED_BASE_MODELS",
    "MAX_PDF_BYTES",
    "StudioConflictError",
    "StudioNotFoundError",
    "StudioStore",
    "minimum_training_documents",
]

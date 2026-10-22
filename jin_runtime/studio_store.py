"""SQLite-backed storage for JIN Studio documents, annotations and datasets."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from jin_runtime.studio_contract import normalize_studio_annotation, utc_now


MAX_PDF_BYTES = 100 * 1024 * 1024
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
ALLOWED_BASE_MODELS = frozenset({"microsoft/layoutlmv3-base"})


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
        self._apply_shared_permissions()
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        self._apply_shared_permissions()
        return connection

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
                """
            )

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

"""Portable, resumable Studio document transfers; no arbitrary paths or URLs.

The manifest preserves extraction and annotation history. PDF bytes travel in
bounded chunks. A document becomes visible only when its complete import commits.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid
from typing import Any

from jin_runtime.studio_contract import (
    STUDIO_ANNOTATION_SCHEMA_VERSION,
    annotation_digest,
    normalize_studio_annotation,
    utc_now,
)
from jin_runtime.studio_store import (
    MAX_PDF_BYTES,
    StudioConflictError,
    StudioNotFoundError,
    StudioStore,
)

SCHEMA = "jin-studio-document-transfer/v1"
CHUNK_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_ACTIVE_TRANSFERS = 16
MAX_STAGED_BYTES = 512 * 1024 * 1024
SESSION_SECONDS = 24 * 3600


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def bundle_digest(bundle: dict[str, Any]) -> str:
    return hashlib.sha256(_json({k: v for k, v in bundle.items() if k != "bundle_sha256"}).encode()).hexdigest()


def _validation_allowed(bundle: dict[str, Any], trusted: bool) -> None:
    if any(item["annotation"]["status"] == "HUMAN_VALIDATED" for item in bundle["annotations"]):
        enabled = os.getenv("JIN_STUDIO_MCP_ALLOW_VALIDATION", "0").lower() in {"1", "true", "yes", "on"}
        if not (enabled and trusted):
            raise ValueError(
                "Validated history requires JIN_STUDIO_MCP_ALLOW_VALIDATION=1 on the destination "
                "and confirm_trusted_source=true; no validation status is silently changed"
            )


def validate_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """Verify integrity and bindings; never rewrite reviewed values or timestamps."""
    encoded = _json(bundle).encode()
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("Transfer manifest exceeds 8 MiB")
    # Take an independent JSON snapshot (no mutable references to caller data).
    bundle = json.loads(encoded)
    if bundle.get("schema") != SCHEMA or bundle.get("bundle_sha256") != bundle_digest(bundle):
        raise ValueError("Invalid transfer schema or manifest SHA-256")
    document = bundle.get("document")
    if not isinstance(document, dict):
        raise ValueError("Missing document metadata")
    if not re.fullmatch(r"jdoc-[0-9a-f]{32}", str(document.get("documentId", ""))):
        raise ValueError("Invalid document ID")
    if not re.fullmatch(r"[0-9a-f]{64}", str(document.get("pdfSha256", ""))):
        raise ValueError("Invalid PDF SHA-256")
    filename = document.get("filename")
    if not isinstance(filename, str) or not filename or len(filename) > 255 or any(c in filename for c in "/\\\x00"):
        raise ValueError("Invalid document filename")
    for key in ("createdAt", "updatedAt"):
        if not isinstance(document.get(key), str) or not document[key]:
            raise ValueError(f"Missing document {key}")
    for key in ("source", "extraction"):
        if not isinstance(document.get(key), dict):
            raise ValueError(f"Invalid document {key}")
    size = bundle.get("pdf_size")
    if type(size) is not int or not 5 <= size <= MAX_PDF_BYTES:
        raise ValueError("Invalid PDF size (maximum 100 MiB)")
    history = bundle.get("annotations")
    if not isinstance(history, list) or len(history) > 1000:
        raise ValueError("Invalid annotation history (maximum 1000 revisions)")
    previous_revision = 0
    for entry in history:
        if not isinstance(entry, dict) or not isinstance(entry.get("annotation"), dict):
            raise ValueError("Invalid annotation history entry")
        annotation = entry["annotation"]
        revision = annotation.get("revision")
        if type(revision) is not int or revision <= previous_revision:
            raise ValueError("Annotation revisions must be positive and strictly increasing")
        previous_revision = revision
        if (
            annotation.get("document_id") != document["documentId"]
            or annotation.get("document_sha256") != document["pdfSha256"]
            or annotation.get("annotation_id") != f"ann-{document['documentId']}-r{revision}"
            or annotation.get("status") not in {"DRAFT", "HUMAN_VALIDATED"}
            or annotation.get("annotation_digest") != annotation_digest(annotation)
            or not isinstance(entry.get("created_at"), str)
            or not entry["created_at"]
        ):
            raise ValueError("Annotation identity, history or digest mismatch")
    expected_status = history[-1]["annotation"]["status"] if history else "DRAFT"
    if document.get("status") != expected_status:
        raise ValueError("Document status disagrees with its latest annotation")
    if history:
        latest = history[-1]["annotation"]
        if latest.get("schema_version") != STUDIO_ANNOTATION_SCHEMA_VERSION:
            raise ValueError("Upgrade the latest annotation to the current Studio schema before transfer")
        normalize_studio_annotation(
            latest, document_id=document["documentId"],
            document_sha256=document["pdfSha256"], revision=latest["revision"],
        )
    return bundle


class StudioDocumentTransfer:
    def __init__(self, store: StudioStore):
        self.store = store
        self.staging = store.root / "transfers"
        self.staging.mkdir(exist_ok=True)
        if self.staging.is_symlink() or self.staging.resolve().parent != store.root:
            raise ValueError("Unsafe transfer staging directory")

    def _part(self, transfer_id: str) -> Path:
        if not re.fullmatch(r"jtransfer-[0-9a-f]{32}", transfer_id):
            raise ValueError("Invalid transfer ID")
        path = self.staging / f"{transfer_id}.part"
        if path.is_symlink() or path.resolve().parent != self.staging.resolve():
            raise ValueError("Unsafe transfer path")
        return path

    @staticmethod
    def _audit(connection, action: str, target_id: str, details: dict[str, Any]) -> None:
        connection.execute(
            """INSERT INTO studio_audit_events
            (actor_user_id, actor_username, action, target_type, target_id, details_json, client_ip, created_at)
            VALUES (?, 'mcp-service', ?, 'document_transfer', ?, ?, 'mcp', ?)""",
            ("service:jin-studio-mcp", action, target_id, _json(details), utc_now()),
        )

    def export(self, document_id: str) -> dict[str, Any]:
        with self.store._connect() as connection:
            connection.execute("BEGIN")
            row = self.store._row(connection, document_id)
            document = self.store._document(row)
            document.pop("latestAnnotation")
            pdf = self.store._resolve_document_pdf_path(row)
            history = connection.execute(
                "SELECT payload_json, created_at FROM studio_annotations WHERE document_id=? ORDER BY revision",
                (document_id,),
            ).fetchall()
        with pdf.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != document["pdfSha256"]:
            raise ValueError("Stored PDF SHA-256 mismatch; export refused")
        bundle = {
            "schema": SCHEMA, "document": document, "pdf_size": pdf.stat().st_size,
            "annotations": [{"annotation": json.loads(item["payload_json"]), "created_at": item["created_at"]} for item in history],
        }
        bundle["bundle_sha256"] = bundle_digest(bundle)
        return validate_bundle(bundle)

    def read_chunk(self, document_id: str, offset: int = 0, max_bytes: int = CHUNK_BYTES) -> dict[str, Any]:
        if type(offset) is not int or offset < 0 or not 1 <= max_bytes <= CHUNK_BYTES:
            raise ValueError("Invalid offset or chunk size (maximum 256 KiB)")
        document = self.store.get_document(document_id)
        with self.store.pdf_path(document_id).open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if offset > size:
                raise ValueError("Offset exceeds PDF size")
            handle.seek(offset)
            chunk = handle.read(max_bytes)
        return {
            "data_base64": base64.b64encode(chunk).decode("ascii"),
            "sha256": hashlib.sha256(chunk).hexdigest(), "pdf_sha256": document["pdfSha256"],
            "offset": offset, "next_offset": offset + len(chunk), "pdf_size": size,
            "eof": offset + len(chunk) == size,
        }

    def _row(self, connection, transfer_id: str, *, active: bool = False):
        self._part(transfer_id)
        row = connection.execute("SELECT * FROM studio_document_transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
        if row is None:
            raise StudioNotFoundError(transfer_id)
        if active and (row["state"] != "ACTIVE" or row["expires_at"] <= time.time()):
            raise ValueError("Transfer is not active or has expired; start a new import")
        return row

    @staticmethod
    def _status(row) -> dict[str, Any]:
        bundle = json.loads(row["bundle_json"])
        state = row["state"]
        if state == "ACTIVE" and row["expires_at"] <= time.time():
            state = "EXPIRED"
        return {
            "transfer_id": row["transfer_id"], "state": state,
            "document_id": bundle["document"]["documentId"],
            "bundle_sha256": row["bundle_sha256"], "pdf_size": bundle["pdf_size"],
            "received_bytes": row["received_bytes"], "chunk_bytes": CHUNK_BYTES,
            "expires_at": row["expires_at"],
        }

    def status(self, transfer_id: str) -> dict[str, Any]:
        with self.store._connect() as connection:
            return self._status(self._row(connection, transfer_id))

    def begin(self, bundle: dict[str, Any], *, confirm_trusted_source: bool = False) -> dict[str, Any]:
        bundle = validate_bundle(bundle)
        _validation_allowed(bundle, confirm_trusted_source)
        document_id = bundle["document"]["documentId"]
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Clean only this service's expired partial files, never documents.
            expired = connection.execute("SELECT transfer_id FROM studio_document_transfers WHERE state='ACTIVE' AND expires_at<=?", (time.time(),)).fetchall()
            for item in expired:
                self._part(item["transfer_id"]).unlink(missing_ok=True)
                connection.execute("UPDATE studio_document_transfers SET state='EXPIRED' WHERE transfer_id=?", (item["transfer_id"],))
            previous = connection.execute(
                "SELECT * FROM studio_document_transfers WHERE bundle_sha256=? AND state IN ('ACTIVE', 'COMPLETE') ORDER BY expires_at DESC LIMIT 1",
                (bundle["bundle_sha256"],),
            ).fetchone()
            if previous:
                return self._status(previous)
            if connection.execute("SELECT 1 FROM studio_documents WHERE document_id=?", (document_id,)).fetchone():
                raise StudioConflictError("Document already exists; imports never overwrite local work")
            active = connection.execute("SELECT bundle_json FROM studio_document_transfers WHERE state='ACTIVE'").fetchall()
            reserved = sum(json.loads(item["bundle_json"])["pdf_size"] for item in active)
            if len(active) >= MAX_ACTIVE_TRANSFERS or reserved + bundle["pdf_size"] > MAX_STAGED_BYTES:
                raise ValueError("Transfer staging quota exceeded; finish or cancel active imports")
            transfer_id = f"jtransfer-{uuid.uuid4().hex}"
            # The part is created by the first upload, avoiding begin/rollback orphans.
            connection.execute(
                "INSERT INTO studio_document_transfers (transfer_id,bundle_sha256,bundle_json,state,trusted_validation,expires_at) VALUES (?,?,?,'ACTIVE',?,?)",
                (transfer_id, bundle["bundle_sha256"], _json(bundle), int(confirm_trusted_source), time.time() + SESSION_SECONDS),
            )
            self._audit(connection, "document_transfer.begin", transfer_id, {"document_id": document_id, "bundle_sha256": bundle["bundle_sha256"]})
            return self._status(self._row(connection, transfer_id))

    def upload(self, transfer_id: str, offset: int, data_base64: str, sha256: str) -> dict[str, Any]:
        if type(offset) is not int or offset < 0 or len(data_base64) > 4 * ((CHUNK_BYTES + 2) // 3):
            raise ValueError("Invalid chunk offset or size")
        try:
            chunk = base64.b64decode(data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Invalid chunk base64") from exc
        if not chunk or len(chunk) > CHUNK_BYTES or hashlib.sha256(chunk).hexdigest() != sha256:
            raise ValueError("Invalid chunk size or SHA-256")
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, transfer_id, active=True)
            received = row["received_bytes"]
            size = json.loads(row["bundle_json"])["pdf_size"]
            if offset > received or offset + len(chunk) > size:
                raise ValueError("Chunk is out of order or exceeds declared PDF size")
            path = self._part(transfer_id)
            if received and (not path.exists() or path.stat().st_size < received):
                raise ValueError("Partial PDF is damaged; cancel and restart import")
            with path.open("r+b" if path.exists() else "x+b") as handle:
                if offset < received:
                    handle.seek(offset)
                    if offset + len(chunk) > received or handle.read(len(chunk)) != chunk:
                        raise StudioConflictError("Retry differs from already received bytes")
                    return self._status(row)
                handle.truncate(received)  # discard uncommitted tail after a crash
                handle.seek(received)
                handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            connection.execute("UPDATE studio_document_transfers SET received_bytes=? WHERE transfer_id=?", (received + len(chunk), transfer_id))
            return self._status(self._row(connection, transfer_id))

    def finish(self, transfer_id: str, *, confirm_import: bool = False) -> dict[str, Any]:
        if not confirm_import:
            raise ValueError("confirm_import=true is required to publish the imported document")
        created_dir = None
        try:
            with self.store._lock, self.store._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._row(connection, transfer_id)
                if row["state"] == "COMPLETE":
                    return self._status(row)
                self._row(connection, transfer_id, active=True)
                bundle = validate_bundle(json.loads(row["bundle_json"]))
                _validation_allowed(bundle, bool(row["trusted_validation"]))
                document = bundle["document"]
                document_id = document["documentId"]
                if row["received_bytes"] != bundle["pdf_size"]:
                    raise ValueError("PDF transfer is incomplete")
                if connection.execute("SELECT 1 FROM studio_documents WHERE document_id=?", (document_id,)).fetchone():
                    raise StudioConflictError("Document already exists; imports never overwrite local work")
                part = self._part(transfer_id)
                with part.open("rb") as handle:
                    if os.fstat(handle.fileno()).st_size != bundle["pdf_size"] or handle.read(5) != b"%PDF-":
                        raise ValueError("PDF size or header mismatch")
                    handle.seek(0)
                    if hashlib.file_digest(handle, "sha256").hexdigest() != document["pdfSha256"]:
                        raise ValueError("PDF SHA-256 mismatch")
                directory = self.store.documents_dir / document_id
                if directory.resolve().parent != self.store.documents_dir.resolve():
                    raise ValueError("Unsafe document path")
                directory.mkdir(exist_ok=False)  # never replace even unregistered files
                created_dir = directory
                pdf_path = directory / "source.pdf"
                with part.open("rb") as source, pdf_path.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
                connection.execute(
                    """INSERT INTO studio_documents (document_id,filename,pdf_sha256,pdf_path,status,source_json,extraction_json,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (document_id, document["filename"], document["pdfSha256"], pdf_path.relative_to(self.store.root).as_posix(),
                     document["status"], _json(document["source"]), _json(document["extraction"]), document["createdAt"], document["updatedAt"]),
                )
                for entry in bundle["annotations"]:
                    annotation = entry["annotation"]
                    connection.execute(
                        """INSERT INTO studio_annotations (annotation_id,document_id,revision,status,reviewer,digest,payload_json,created_at)
                        VALUES (?,?,?,?,?,?,?,?)""",
                        (annotation["annotation_id"], document_id, annotation["revision"], annotation["status"], annotation.get("reviewed_by", ""),
                         annotation["annotation_digest"], _json(annotation), entry["created_at"]),
                    )
                connection.execute("UPDATE studio_document_transfers SET state='COMPLETE' WHERE transfer_id=?", (transfer_id,))
                self._audit(connection, "document_transfer.complete", transfer_id, {
                    "document_id": document_id, "bundle_sha256": bundle["bundle_sha256"],
                    "annotation_revisions": len(bundle["annotations"]), "trusted_validation": bool(row["trusted_validation"]),
                })
                result = self._status(self._row(connection, transfer_id))
            created_dir = None  # committed: never delete the imported document
        except BaseException:
            if created_dir is not None:
                (created_dir / "source.pdf").unlink(missing_ok=True)
                created_dir.rmdir()
            raise
        # Cleanup is retryable, independent of the atomic database publication.
        try:
            self._part(transfer_id).unlink(missing_ok=True)
        except OSError:
            pass
        return result

    def cancel(self, transfer_id: str) -> dict[str, Any]:
        with self.store._lock, self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, transfer_id)
            if row["state"] == "COMPLETE":
                raise ValueError("A completed import cannot be cancelled or deleted")
            self._part(transfer_id).unlink(missing_ok=True)
            connection.execute("UPDATE studio_document_transfers SET state='CANCELLED' WHERE transfer_id=?", (transfer_id,))
            self._audit(connection, "document_transfer.cancel", transfer_id, {})
            return self._status(self._row(connection, transfer_id))

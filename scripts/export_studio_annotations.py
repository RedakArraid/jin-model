"""Export a verified Studio snapshot before transfer or manual training preparation.

Standard library only. All revisions stay unchanged; drafts are NOT training labels.
No training, validation or destination writes are performed by this script.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

try:
    from scripts.transfer_studio_documents import MCPClient, document_ids
except ModuleNotFoundError:
    from transfer_studio_documents import MCPClient, document_ids


SCHEMA = "jin-studio-annotations-export/v1"
DOCUMENT_ID = re.compile(r"jdoc-[0-9a-f]{32}")


def encode(value) -> bytes:
    # Keep Python's numeric types: changing 1.0 into 1 breaks signed manifests.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_new(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return digest(data)


def check_bundle(bundle: dict, document_id: str) -> None:
    if not DOCUMENT_ID.fullmatch(document_id):
        raise ValueError("Invalid Studio document ID")
    if bundle.get("schema") != "jin-studio-document-transfer/v1":
        raise ValueError("Unsupported document bundle schema")
    if bundle.get("bundle_sha256") != digest(encode({k: v for k, v in bundle.items() if k != "bundle_sha256"})):
        raise ValueError("Document bundle SHA-256 mismatch")
    if bundle["document"]["documentId"] != document_id:
        raise ValueError("Document bundle identity mismatch")
    previous = 0
    for entry in bundle["annotations"]:
        annotation = entry["annotation"]
        revision = annotation["revision"]
        if type(revision) is not int or revision <= previous:
            raise ValueError("Invalid annotation revision order")
        if (annotation["document_id"] != document_id
                or annotation["document_sha256"] != bundle["document"]["pdfSha256"]):
            raise ValueError("Annotation document binding mismatch")
        previous = revision


def checked_chunk(chunk: dict, bundle: dict, offset: int) -> bytes:
    data = base64.b64decode(chunk["data_base64"], validate=True)
    if (not data or chunk["pdf_sha256"] != bundle["document"]["pdfSha256"]
            or chunk["pdf_size"] != bundle["pdf_size"] or chunk["offset"] != offset
            or chunk["next_offset"] != offset + len(data)
            or offset + len(data) > bundle["pdf_size"]
            or digest(data) != chunk["sha256"]):
        raise ValueError("Source PDF chunk is inconsistent with its manifest")
    return data


def export_annotations(source, output: Path, ids: list[str], *, progress=None) -> dict:
    """Create a NEW directory. Incomplete exports have no export_summary.json."""
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Select a non-empty, unique document list")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    total_revisions = 0
    states = Counter()
    with (output / "manifest.jsonl").open("xb") as manifest:
        for index, document_id in enumerate(ids, 1):
            bundle = source.call("export_document_bundle", document_id=document_id)
            check_bundle(bundle, document_id)
            document = bundle["document"]
            pdf_file = f"pdfs/{document_id}.pdf"
            document_file = f"documents/{document_id}.json"
            pdf_path = output / pdf_file
            pdf_path.parent.mkdir(exist_ok=True, mode=0o700)
            with pdf_path.open("xb") as pdf:
                offset = 0
                while offset < bundle["pdf_size"]:
                    chunk = source.call("read_document_pdf_chunk", document_id=document_id, offset=offset, max_bytes=262144)
                    data = checked_chunk(chunk, bundle, offset)
                    pdf.write(data)
                    offset += len(data)
                pdf.flush()
                os.fsync(pdf.fileno())
            if file_digest(pdf_path) != document["pdfSha256"]:
                raise ValueError("Exported PDF SHA-256 mismatch")
            document_sha256 = write_new(output / document_file, encode(bundle))
            revisions = []
            for entry in bundle["annotations"]:
                annotation = entry["annotation"]
                annotation_file = f"annotations/{document_id}/r{annotation['revision']:06d}.json"
                annotation_sha256 = write_new(output / annotation_file, encode(annotation))
                revisions.append({
                    "revision": annotation["revision"], "status": annotation["status"],
                    "created_at": entry["created_at"], "annotation_file": annotation_file,
                    "annotation_sha256": annotation_sha256,
                    "annotation_digest": annotation["annotation_digest"],
                })
            latest = bundle["annotations"][-1]["annotation"] if revisions else {}
            fields = latest.get("field_annotations", [])
            row = {
                "schema": SCHEMA, "document_id": document_id, "filename": document["filename"],
                "status": document["status"], "pdf_file": pdf_file,
                "pdf_sha256": document["pdfSha256"], "pdf_bytes": bundle["pdf_size"],
                "document_file": document_file, "document_sha256": document_sha256,
                "bundle_sha256": bundle["bundle_sha256"], "annotation_revisions": revisions,
                "latest_annotation_file": revisions[-1]["annotation_file"] if revisions else None,
                "field_count": len(fields), "field_status_counts": dict(Counter(f["status"] for f in fields)),
            }
            manifest.write(encode(row) + b"\n")
            total_revisions += len(revisions)
            states[document["status"]] += 1
            if progress:
                progress({"state": "EXPORTED", "document_id": document_id, "index": index, "total": len(ids)})
        manifest.flush()
        os.fsync(manifest.fileno())
    summary = {
        "schema": SCHEMA, "state": "COMPLETE", "created_at": datetime.now(timezone.utc).isoformat(),
        "documents": len(ids), "annotation_revisions": total_revisions, "document_status_counts": dict(states),
        "manifest_sha256": file_digest(output / "manifest.jsonl"),
        "training_started": False,
    }
    # Commit the completion marker only after ALL files have been read back.
    verify_export(output, summary=summary)
    write_new(output / "export_summary.json", encode(summary))
    return summary


def safe_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("Unsafe export path")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe export path")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError("Missing or unsafe export file")
    return resolved


def verify_export(output: Path, *, summary: dict | None = None) -> dict[str, dict]:
    """Verify a complete snapshot before allowing any destination writes."""
    output = Path(output)
    if summary is None:
        summary = json.loads(safe_file(output, "export_summary.json").read_text(encoding="utf-8"))
    if summary.get("schema") != SCHEMA or summary.get("state") != "COMPLETE":
        raise ValueError("Export is not complete")
    manifest = safe_file(output, "manifest.jsonl")
    if file_digest(manifest) != summary["manifest_sha256"]:
        raise ValueError("Export manifest SHA-256 mismatch")
    rows = {}
    states = Counter()
    revisions_count = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        document_id = row["document_id"]
        if row.get("schema") != SCHEMA or document_id in rows:
            raise ValueError("Invalid or duplicate manifest document")
        bundle_path = safe_file(output, row["document_file"])
        if file_digest(bundle_path) != row["document_sha256"]:
            raise ValueError("Document file SHA-256 mismatch")
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        check_bundle(bundle, document_id)
        document = bundle["document"]
        if (bundle["bundle_sha256"] != row["bundle_sha256"] or bundle["pdf_size"] != row["pdf_bytes"]
                or document["pdfSha256"] != row["pdf_sha256"] or document["status"] != row["status"]
                or document["filename"] != row["filename"]):
            raise ValueError("Document metadata mismatch")
        pdf = safe_file(output, row["pdf_file"])
        if pdf.stat().st_size != row["pdf_bytes"] or file_digest(pdf) != row["pdf_sha256"]:
            raise ValueError("PDF SHA-256 or size mismatch")
        history = row["annotation_revisions"]
        if len(history) != len(bundle["annotations"]):
            raise ValueError("Annotation history length mismatch")
        for revision, entry in zip(history, bundle["annotations"]):
            annotation_path = safe_file(output, revision["annotation_file"])
            annotation = entry["annotation"]
            if (file_digest(annotation_path) != revision["annotation_sha256"]
                    or annotation_path.read_bytes() != encode(annotation)
                    or revision["revision"] != annotation["revision"]
                    or revision["status"] != annotation["status"]
                    or revision["created_at"] != entry["created_at"]
                    or revision["annotation_digest"] != annotation["annotation_digest"]):
                raise ValueError("Annotation JSON or metadata mismatch")
        latest_file = history[-1]["annotation_file"] if history else None
        fields = bundle["annotations"][-1]["annotation"].get("field_annotations", []) if history else []
        if (row["latest_annotation_file"] != latest_file or row["field_count"] != len(fields)
                or row["field_status_counts"] != dict(Counter(f["status"] for f in fields))):
            raise ValueError("Latest annotation summary mismatch")
        revisions_count += len(history)
        states[row["status"]] += 1
        rows[document_id] = row
    if (not rows or len(rows) != summary["documents"] or revisions_count != summary["annotation_revisions"]
            or dict(states) != summary["document_status_counts"]):
        raise ValueError("Export counts mismatch")
    return rows


class ExportSource:
    """Read-only MCP-shaped source backed by a completely verified export."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.rows = verify_export(self.directory)

    def call(self, name: str, **args) -> dict:
        if name == "list_documents":
            offset, limit = args.get("offset", 0), args.get("limit", 100)
            ids = list(self.rows)[offset:offset + limit]
            return {"items": [{"documentId": item} for item in ids], "has_more": offset + len(ids) < len(self.rows), "next_offset": offset + len(ids)}
        row = self.rows[args["document_id"]]
        if name == "export_document_bundle":
            path = safe_file(self.directory, row["document_file"])
            if file_digest(path) != row["document_sha256"]:
                raise ValueError("Export changed after verification")
            return json.loads(path.read_text(encoding="utf-8"))
        if name == "read_document_pdf_chunk":
            offset, size = args.get("offset", 0), args.get("max_bytes", 262144)
            if type(offset) is not int or not 0 <= offset <= row["pdf_bytes"] or not 1 <= size <= 262144:
                raise ValueError("Invalid PDF chunk range")
            with safe_file(self.directory, row["pdf_file"]).open("rb") as pdf:
                pdf.seek(offset)
                data = pdf.read(size)
            return {"data_base64": base64.b64encode(data).decode("ascii"), "sha256": digest(data),
                    "pdf_sha256": row["pdf_sha256"], "pdf_size": row["pdf_bytes"], "offset": offset,
                    "next_offset": offset + len(data), "eof": offset + len(data) == row["pdf_bytes"]}
        raise ValueError("Export source is read-only")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--source-url", required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--allow-http", action="store_true")
    selection = export.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--document-id", action="append")
    verify = commands.add_parser("verify")
    verify.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            rows = verify_export(args.directory)
            print(json.dumps({"state": "VERIFIED", "documents": len(rows)}))
        else:
            source = MCPClient(args.source_url, os.getenv("JIN_SOURCE_MCP_TOKEN", ""), allow_http=args.allow_http)
            summary = export_annotations(source, args.output, document_ids(source, args.document_id),
                                         progress=lambda row: print(json.dumps(row), flush=True))
            print(json.dumps(summary))
        return 0
    except Exception as exc:
        print(f"Studio export failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Re-extract every safe Studio draft and atomically add fresh V3 preannotations."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

import httpx
from jsonschema import Draft202012Validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jin_runtime.clean_output import clean_output_schema
from jin_runtime.studio_contract import normalize_studio_annotation, utc_now
from jin_runtime.studio_preannotation import (
    generate_studio_preannotation,
    machine_draft_payload,
)
from jin_runtime.studio_store import StudioStore


CONFIRMATION = "REPROCESS-STUDIO-DOCUMENTS"
REVIEWER = "jin-model-preannotation-v3"
NOTE = (
    "Préannotation V3 régénérée par JIN-model depuis le PDF complet. "
    "Chaque champ doit être relu."
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _safe_root(value: Path) -> Path:
    root = value.resolve()
    database = (root / "studio.sqlite3").resolve()
    if root == root.anchor or len(root.parts) < 3 or root not in database.parents:
        raise ValueError(f"Refusing broad Studio root: {root}")
    if not database.is_file():
        raise FileNotFoundError(database)
    return root


def _backup_database(database: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(database) as source, sqlite3.connect(destination) as target:
        source.backup(target)


def _replaceability_reason(row: sqlite3.Row) -> str:
    """Return why a document must not be superseded, or an empty string."""

    if row["status"] != "DRAFT":
        return f"status={row['status']}"
    if not row["annotation_json"]:
        return ""
    annotation = json.loads(row["annotation_json"])
    reviewer = str(annotation.get("reviewed_by") or "")
    if reviewer not in {
        "codex-preannotation",
        "codex-preannotation-v3",
        REVIEWER,
    }:
        return f"reviewer={reviewer or 'unknown'}"
    for field in annotation.get("field_annotations") or []:
        if str(field.get("status") or "UNREVIEWED") not in {
            "UNREVIEWED",
            "AMBIGUOUS",
        }:
            return "human field review detected"
        if field.get("corrected_value") not in (None, ""):
            return "human correction detected"
    return ""


def _partition_replaceable(
    rows: list[sqlite3.Row],
) -> tuple[list[sqlite3.Row], list[dict[str, str]]]:
    selected: list[sqlite3.Row] = []
    skipped: list[dict[str, str]] = []
    for row in rows:
        reason = _replaceability_reason(row)
        if reason:
            skipped.append(
                {
                    "document_id": row["document_id"],
                    "filename": row["filename"],
                    "reason": reason,
                }
            )
        else:
            selected.append(row)
    return selected, skipped


def _load_documents(database: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT documents.*,
                   (SELECT annotations.payload_json
                    FROM studio_annotations annotations
                    WHERE annotations.document_id=documents.document_id
                    ORDER BY annotations.revision DESC LIMIT 1) annotation_json
            FROM studio_documents documents
            ORDER BY documents.filename COLLATE NOCASE, documents.document_id
            """
        ).fetchall()


def _preannotation_payload(pdf_path: Path, extraction: dict[str, Any], ambiguity: int) -> dict[str, Any]:
    result = generate_studio_preannotation(
        pdf_path,
        extraction,
        max_ambiguity=ambiguity,
    )
    payload = machine_draft_payload(result)
    payload["reviewed_by"] = REVIEWER
    payload["note"] = NOTE
    return payload


def _commit(
    database: Path,
    rows: list[sqlite3.Row],
    prepared: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[int, int]:
    annotation_count = 0
    field_count = 0
    with sqlite3.connect(database, timeout=60) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            document_id = row["document_id"]
            extraction, payload = prepared[document_id]
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
                "UPDATE studio_documents SET extraction_json=?, status='DRAFT', updated_at=? "
                "WHERE document_id=?",
                (_json(extraction), now, document_id),
            )
            annotation_count += 1
            field_count += len(annotation.get("field_annotations") or [])
        connection.commit()
    return annotation_count, field_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studio-dir", type=Path, default=Path("data/studio"))
    parser.add_argument("--endpoint", default="http://localhost:8080/api/extract?view=clean")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--max-ambiguity", type=int, default=8)
    parser.add_argument(
        "--reuse-extractions",
        type=Path,
        help=(
            "Reuse every available JSON from a previously staged "
            "new-extractions directory and extract only missing documents."
        ),
    )
    parser.add_argument(
        "--strict-all",
        action="store_true",
        help="Fail if any Studio document is already human-reviewed.",
    )
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    if args.confirm != CONFIRMATION:
        parser.error(f"--confirm must equal {CONFIRMATION}")

    root = _safe_root(args.studio_dir)
    database = root / "studio.sqlite3"
    all_rows = _load_documents(database)
    rows, skipped = _partition_replaceable(all_rows)
    if args.strict_all and skipped:
        preview = "; ".join(
            f"{item['filename']}: {item['reason']}" for item in skipped[:5]
        )
        raise ValueError(
            "Refusing to supersede reviewed Studio work. "
            f"Unsafe documents: {preview}"
        )
    if not rows:
        print("Aucun brouillon machine non relu à retraiter.", flush=True)
        return 0
    print(
        f"Retraitement de {len(rows)} brouillon(s) machine ; "
        f"{len(skipped)} document(s) humain(s) préservé(s).",
        flush=True,
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = root / "backups" / f"before-model-reprocess-{timestamp}"
    _backup_database(database, backup_root / "studio.sqlite3")
    staged_root = backup_root / "new-extractions"
    staged_root.mkdir(parents=True, exist_ok=False)
    staged_annotations = backup_root / "new-preannotations"
    staged_annotations.mkdir(parents=True, exist_ok=False)

    reused_root: Path | None = None
    if args.reuse_extractions:
        reused_root = args.reuse_extractions.resolve()
        backups_root = (root / "backups").resolve()
        if backups_root not in reused_root.parents or reused_root.name != "new-extractions":
            raise ValueError(
                "--reuse-extractions must name a new-extractions directory below Studio backups"
            )

    store = StudioStore(root)
    validator = Draft202012Validator(clean_output_schema("v3"))
    prepared: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    failures: list[dict[str, str]] = []
    with httpx.Client(timeout=max(10.0, args.timeout)) as session:
        for position, row in enumerate(rows, 1):
            document_id = row["document_id"]
            pdf_path = store.pdf_path(document_id)
            try:
                reusable_extraction = (
                    reused_root / f"{document_id}.json" if reused_root else None
                )
                if reusable_extraction is not None and reusable_extraction.is_file():
                    extraction = json.loads(
                        reusable_extraction.read_text(encoding="utf-8")
                    )
                else:
                    with pdf_path.open("rb") as handle:
                        response = session.post(
                            args.endpoint,
                            files={"file": (row["filename"], handle, "application/pdf")},
                        )
                    response.raise_for_status()
                    extraction = response.json()
                validator.validate(extraction)
                if extraction.get("schema_version") != "jin-clean-extraction-v3":
                    raise ValueError("JIN did not return the V3 clean contract")
                payload = _preannotation_payload(
                    pdf_path,
                    extraction,
                    max(1, args.max_ambiguity),
                )
                # Validate every proposal before the transaction starts. This
                # prevents one malformed document from rolling back at commit.
                normalize_studio_annotation(
                    payload,
                    document_id=document_id,
                    document_sha256=row["pdf_sha256"],
                    revision=1,
                )
                prepared[document_id] = (extraction, payload)
                (staged_root / f"{document_id}.json").write_text(
                    json.dumps(extraction, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                (staged_annotations / f"{document_id}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                print(
                    f"[{position}/{len(rows)}] {row['filename']}: "
                    f"{len(payload['field_annotations'])} champs",
                    flush=True,
                )
            except Exception as exc:
                failures.append(
                    {
                        "document_id": document_id,
                        "filename": row["filename"],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                print(f"[{position}/{len(rows)}] ÉCHEC {row['filename']}: {exc}", flush=True)

    report: dict[str, Any] = {
        "operation": "jin-model-studio-reprocess-v3",
        "created_at": utc_now(),
        "endpoint": args.endpoint,
        "reused_extractions": str(reused_root) if reused_root else None,
        "studio_document_count": len(all_rows),
        "document_count": len(rows),
        "skipped_reviewed_count": len(skipped),
        "skipped_reviewed": skipped,
        "prepared_count": len(prepared),
        "failures": failures,
        "database_updated": False,
    }
    if failures or len(prepared) != len(rows):
        (backup_root / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print("Lot incomplet : aucune donnée Studio n'a été modifiée.", flush=True)
        return 1

    annotations, fields = _commit(database, rows, prepared)
    report.update(
        {
            "database_updated": True,
            "annotations_created": annotations,
            "field_count": fields,
            "reviewer": REVIEWER,
            "note": NOTE,
        }
    )
    (backup_root / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Back up Studio, migrate clean extractions to V3 and rebuild annotations."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jsonschema import Draft202012Validator

from jin_runtime.clean_output import (
    canonicalize_clean_output_v3,
    clean_output_schema,
    upgrade_clean_output_v2_to_v3,
)
from jin_runtime.studio_contract import (
    STUDIO_ANNOTATION_SCHEMA_VERSION,
    annotation_digest,
    utc_now,
)
from jin_runtime.studio_store import StudioStore
from scripts.import_annotation_campaign_to_studio import draft_fields
from training.annotations.bootstrap_from_extractions import annotate_one


CONFIRMATION = "RESET-STUDIO-ANNOTATIONS"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _safe_studio_root(value: Path) -> Path:
    root = value.resolve()
    if root == root.anchor or len(root.parts) < 3:
        raise ValueError(f"Refusing broad Studio root: {root}")
    database = (root / "studio.sqlite3").resolve()
    if root not in database.parents or not database.is_file():
        raise FileNotFoundError(database)
    return root


def _backup_database(database: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(database) as source, sqlite3.connect(destination) as target:
        source.backup(target)


def _archive_dataset_files(root: Path, backup_root: Path) -> None:
    datasets = root / "datasets"
    if not datasets.exists() or not any(datasets.iterdir()):
        return
    archived = backup_root / "datasets"
    shutil.move(str(datasets), str(archived))
    datasets.mkdir(parents=True, exist_ok=True)


def _reset_and_migrate(root: Path, backup_root: Path) -> dict[str, int]:
    database = root / "studio.sqlite3"
    validator = Draft202012Validator(clean_output_schema("v3"))
    counts: dict[str, int] = {}
    with sqlite3.connect(database, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for table in (
            "studio_documents",
            "studio_annotations",
            "studio_annotation_masks",
            "studio_datasets",
            "studio_training_runs",
        ):
            counts[table] = int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )

        documents = connection.execute(
            "SELECT document_id, extraction_json FROM studio_documents"
        ).fetchall()
        migrated_count = 0
        for document in documents:
            extraction = json.loads(document["extraction_json"] or "{}")
            if extraction.get("schema_version") == "jin-clean-extraction-v2":
                extraction = upgrade_clean_output_v2_to_v3(extraction)
                migrated_count += 1
            elif extraction.get("schema_version") == "jin-clean-extraction-v3":
                extraction = canonicalize_clean_output_v3(extraction)
            if extraction.get("schema_version") != "jin-clean-extraction-v3":
                raise ValueError(
                    f"{document['document_id']}: unsupported extraction contract "
                    f"{extraction.get('schema_version')!r}"
                )
            validator.validate(extraction)
            connection.execute(
                "UPDATE studio_documents SET extraction_json=?, status='DRAFT', updated_at=? "
                "WHERE document_id=?",
                (_json(extraction), utc_now(), document["document_id"]),
            )

        connection.execute("DELETE FROM studio_training_runs")
        connection.execute("DELETE FROM studio_datasets")
        connection.execute("DELETE FROM studio_annotation_masks")
        connection.execute("DELETE FROM studio_annotations")
        connection.commit()
    _archive_dataset_files(root, backup_root)
    counts["migrated_extractions"] = migrated_count
    return counts


def _rebuild_preannotations(root: Path, max_ambiguity: int) -> dict[str, Any]:
    store = StudioStore(root)
    documents = store.list_documents(limit=1000)
    failures: list[dict[str, str]] = []
    rebuilt = 0
    fields = 0
    skipped = 0
    for position, summary in enumerate(documents, 1):
        document_id = summary["documentId"]
        document = store.get_document(document_id)
        if document.get("latestAnnotation"):
            skipped += 1
            continue
        try:
            annotation, report = annotate_one(
                store.pdf_path(document_id),
                document["extraction"],
                max_ambiguity=max_ambiguity,
            )
            document_type = str(
                (document["extraction"].get("document") or {}).get("type")
                or "unknown"
            )
            annotation["document_labels"] = {
                "document_type": document_type,
                "requires_review": True,
                **(
                    {"evidence": annotation["document_type_evidence"]}
                    if annotation.get("document_type_evidence") else {}
                ),
            }
            annotation["generation_report"] = {
                "candidate_fields": report["candidate_fields"],
                "source_candidate_fields": report.get("source_candidate_fields"),
                "semantic_aliases_removed": report.get("semantic_aliases_removed"),
                "matched_fields": report["matched_fields"],
                "ambiguous_fields": report["ambiguous_fields"],
                "unmatched_fields": report["unmatched_fields"],
                "match_methods": report.get("match_methods") or {},
                "unresolved": report["unmatched"],
            }
            annotation_payload = {
                "status": "DRAFT",
                "reviewed_by": "codex-preannotation-v3",
                "note": (
                    "Préannotation régénérée depuis le contrat canonique "
                    "jin-clean-extraction-v3. Chaque champ doit être relu."
                ),
                "field_annotations": draft_fields(annotation),
            }
            saved = store.save_annotation(document_id, annotation_payload)
            rebuilt += 1
            fields += len(saved.get("field_annotations") or [])
            print(
                f"[{position}/{len(documents)}] {document['filename']}: "
                f"{len(saved.get('field_annotations') or [])} champs",
                flush=True,
            )
        except Exception as exc:  # Continue so the report identifies every PDF.
            failures.append({
                "document_id": document_id,
                "filename": document["filename"],
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(
                f"[{position}/{len(documents)}] {document['filename']}: ÉCHEC {exc}",
                flush=True,
            )
    return {
        "documents": len(documents),
        "rebuilt": rebuilt,
        "skipped": skipped,
        "field_count": fields,
        "failures": failures,
    }


def _upgrade_generated_annotation_versions(root: Path) -> int:
    """Version the freshly generated canonical annotations consistently."""
    upgraded = 0
    with sqlite3.connect(root / "studio.sqlite3", timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT annotation_id, payload_json FROM studio_annotations"
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            if payload.get("schema_version") == STUDIO_ANNOTATION_SCHEMA_VERSION:
                continue
            if payload.get("reviewed_by") != "codex-preannotation-v3":
                raise ValueError(
                    f"Refusing to rewrite non-V3 annotation {row['annotation_id']}"
                )
            payload["schema_version"] = STUDIO_ANNOTATION_SCHEMA_VERSION
            payload["annotation_digest"] = annotation_digest(payload)
            connection.execute(
                "UPDATE studio_annotations SET digest=?, payload_json=? "
                "WHERE annotation_id=?",
                (
                    payload["annotation_digest"],
                    _json(payload),
                    row["annotation_id"],
                ),
            )
            upgraded += 1
        connection.commit()
    return upgraded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studio-dir", type=Path, default=Path("data/studio"))
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--max-ambiguity", type=int, default=8)
    parser.add_argument(
        "--resume-only",
        action="store_true",
        help="Only preannotate documents that still have no annotation.",
    )
    args = parser.parse_args(argv)
    if args.confirm != CONFIRMATION:
        parser.error(f"--confirm must equal {CONFIRMATION}")

    root = _safe_studio_root(args.studio_dir)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.resume_only:
        backup_root = root / "backups" / f"v3-resume-{timestamp}"
        backup_root.mkdir(parents=True, exist_ok=False)
        backup_database: Path | None = None
        before: dict[str, Any] = {"resume_only": True}
    else:
        backup_root = root / "backups" / f"before-v3-reset-{timestamp}"
        backup_database = backup_root / "studio.sqlite3"
        _backup_database(root / "studio.sqlite3", backup_database)
        before = _reset_and_migrate(root, backup_root)
    rebuilt = _rebuild_preannotations(root, max(1, args.max_ambiguity))
    rebuilt["upgraded_annotation_versions"] = _upgrade_generated_annotation_versions(root)
    report = {
        "operation": "studio-v3-clean-reset",
        "created_at": utc_now(),
        "studio_root": str(root),
        "backup_database": str(backup_database) if backup_database else None,
        "before": before,
        "rebuild": rebuilt,
    }
    report_path = backup_root / "reset-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False))
    return 1 if rebuilt["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Assist machine Studio drafts from validated examples of the same client."""
from __future__ import annotations

import argparse
import copy
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jin_runtime.studio_contract import normalize_studio_annotation, utc_now
from jin_runtime.studio_masks import (
    apply_annotation_mask,
    build_annotation_mask,
    document_type_from_payload,
)
from jin_runtime.studio_store import StudioStore


CONFIRMATION = "ASSIST-STUDIO-CLIENT-ANNOTATIONS"
REVIEWER = "jin-model-assisted-from-validated-v1"
MACHINE_REVIEWERS = frozenset(
    {
        "codex-preannotation",
        "codex-preannotation-v3",
        "jin-model-preannotation-v3",
        REVIEWER,
    }
)
_LINE_ITEM_PATH = re.compile(r"(?:^|\.)line_items\[\d+\](?:\.|$)")
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _safe_root(value: Path) -> Path:
    root = value.resolve()
    database = (root / "studio.sqlite3").resolve()
    if root == Path(root.anchor) or len(root.parts) < 3 or root not in database.parents:
        raise ValueError(f"Refusing broad Studio root: {root}")
    if not database.is_file():
        raise FileNotFoundError(database)
    return root


def _backup_database(database: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(database) as source, sqlite3.connect(destination) as target:
        source.backup(target)


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


def _client_match(row: Mapping[str, Any], keyword: str) -> bool:
    extraction = json.loads(row["extraction_json"] or "{}")
    parties = ((extraction.get("order") or {}).get("parties") or {})
    candidates = [str(row["filename"] or "")]
    for role in ("buyer", "bill_to", "ship_to", "supplier"):
        party = parties.get(role) or {}
        candidates.extend(
            [str(party.get("name") or ""), str(party.get("legal_name") or "")]
        )
    needle = keyword.strip().casefold()
    return bool(needle) and any(needle in value.casefold() for value in candidates)


def _document_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "documentId": row["document_id"],
        "filename": row["filename"],
        "status": row["status"],
        "extraction": json.loads(row["extraction_json"] or "{}"),
        "latestAnnotation": json.loads(row["annotation_json"] or "null"),
    }


def _machine_replaceable(document: Mapping[str, Any]) -> tuple[bool, str]:
    if document.get("status") != "DRAFT":
        return False, f"status={document.get('status')}"
    annotation = document.get("latestAnnotation")
    if not isinstance(annotation, Mapping):
        return False, "annotation absente"
    reviewer = str(annotation.get("reviewed_by") or "").strip()
    if reviewer not in MACHINE_REVIEWERS:
        return False, f"reviewer={reviewer or 'unknown'}"
    for field in annotation.get("field_annotations") or []:
        if str(field.get("status") or "UNREVIEWED") not in {
            "UNREVIEWED",
            "AMBIGUOUS",
        }:
            return False, "champ relu par un humain"
        if field.get("corrected_value") not in (None, ""):
            return False, "correction humaine présente"
    return True, ""


def _normalized_text(value: Any) -> str:
    return _NON_ALNUM.sub("", str(value or "").upper())


def _values_agree(left: Any, right: Any) -> bool:
    first = _normalized_text(left)
    second = _normalized_text(right)
    if not first or not second:
        return False
    if first.isdigit() and second.isdigit() and len(first) == len(second) == 8:
        if first[:2] in {"19", "20"}:
            first_date = first
        else:
            first_date = f"{first[4:]}{first[2:4]}{first[:2]}"
        if second[:2] in {"19", "20"}:
            second_date = second
        else:
            second_date = f"{second[4:]}{second[2:4]}{second[:2]}"
        if first_date == second_date:
            return True
    if first == second:
        return True
    return min(len(first), len(second)) >= 4 and (
        first in second or second in first
    )


def _proposal_score(
    result: Mapping[str, Any],
    current_fields: Mapping[str, Mapping[str, Any]],
) -> tuple[float, dict[str, Any]]:
    fields = [item for item in result.get("fields") or [] if isinstance(item, Mapping)]
    nonempty = sum(bool(_normalized_text(item.get("extracted_value"))) for item in fields)
    comparable = 0
    agreements = 0
    for item in fields:
        current = current_fields.get(str(item.get("path") or ""))
        if not current:
            continue
        if _normalized_text(item.get("extracted_value")) and _normalized_text(
            current.get("extracted_value")
        ):
            comparable += 1
            agreements += int(
                _values_agree(
                    item.get("extracted_value"), current.get("extracted_value")
                )
            )
    field_count = max(1, len(fields))
    alignment = result.get("alignment") or {}
    aligned = int(alignment.get("alignedFieldCount") or 0)
    skipped = len(result.get("skippedFields") or [])
    warnings = len(result.get("warnings") or [])
    coverage_ratio = nonempty / field_count
    alignment_ratio = aligned / field_count
    agreement_ratio = agreements / max(1, comparable)
    score = (
        4.0 * agreement_ratio
        + 3.0 * coverage_ratio
        + 2.0 * alignment_ratio
        + min(field_count, 60) / 100.0
        - skipped * 0.15
        - warnings * 0.10
    )
    return round(score, 6), {
        "score": round(score, 6),
        "fieldCount": len(fields),
        "nonemptyFieldCount": nonempty,
        "alignedFieldCount": aligned,
        "comparableFieldCount": comparable,
        "agreementCount": agreements,
        "warningCount": warnings,
        "skippedFieldCount": skipped,
    }


def _unreviewed(field: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(field))
    result["status"] = "UNREVIEWED"
    result["corrected_value"] = None
    return result


def _merge_assisted_fields(
    current_fields: list[dict[str, Any]],
    mask_fields: list[dict[str, Any]],
    *,
    source_filename: str,
) -> list[dict[str, Any]]:
    """Prefer validated geometry while retaining target values and line items."""

    current_by_path = {
        str(item.get("path") or ""): item
        for item in current_fields
        if str(item.get("path") or "")
    }
    applied_by_path = {
        str(item.get("path") or ""): item
        for item in mask_fields
        if str(item.get("path") or "")
    }
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    line_items: list[dict[str, Any]] = []

    for current in current_fields:
        path = str(current.get("path") or "")
        if not path or path in seen:
            continue
        if _LINE_ITEM_PATH.search(path):
            line_items.append(_unreviewed(current))
            seen.add(path)
            continue
        applied = applied_by_path.get(path)
        if applied is None:
            merged.append(_unreviewed(current))
            seen.add(path)
            continue
        assisted = _unreviewed(current)
        assisted["evidence"] = copy.deepcopy(applied.get("evidence") or {})
        assisted["compartment"] = applied.get(
            "compartment", current.get("compartment", "HEADER")
        )
        if not _normalized_text(assisted.get("extracted_value")):
            assisted["extracted_value"] = applied.get("extracted_value") or ""
        assisted["comment"] = (
            f"Cadre proposé depuis l'annotation ISERBA validée « {source_filename} » ; "
            "valeur relue dans ce PDF et à confirmer."
        )
        merged.append(assisted)
        seen.add(path)

    for path, applied in applied_by_path.items():
        if path in seen:
            continue
        assisted = _unreviewed(applied)
        assisted["comment"] = (
            f"Champ proposé depuis l'annotation ISERBA validée « {source_filename} » ; "
            "valeur relue dans ce PDF et à confirmer."
        )
        merged.append(assisted)
        seen.add(path)

    return [*merged, *line_items]


def _commit(
    database: Path,
    prepared: Mapping[str, Mapping[str, Any]],
) -> tuple[int, int]:
    annotation_count = 0
    field_count = 0
    with sqlite3.connect(database, timeout=60) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for document_id, payload in prepared.items():
            row = connection.execute(
                "SELECT pdf_sha256 FROM studio_documents WHERE document_id=?",
                (document_id,),
            ).fetchone()
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
                "UPDATE studio_documents SET status='DRAFT', updated_at=? WHERE document_id=?",
                (now, document_id),
            )
            annotation_count += 1
            field_count += len(annotation.get("field_annotations") or [])
        connection.commit()
    return annotation_count, field_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studio-dir", type=Path, default=Path("data/studio"))
    parser.add_argument("--client", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    if args.confirm != CONFIRMATION:
        parser.error(f"--confirm must equal {CONFIRMATION}")

    root = _safe_root(args.studio_dir)
    database = root / "studio.sqlite3"
    store = StudioStore(root)
    client_rows = [
        row for row in _load_documents(database) if _client_match(row, args.client)
    ]
    documents = [_document_payload(row) for row in client_rows]
    sources = [
        item
        for item in documents
        if item["status"] == "HUMAN_VALIDATED"
        and isinstance(item.get("latestAnnotation"), Mapping)
    ]
    if not sources:
        raise ValueError(f"No validated Studio annotations found for {args.client}")

    source_types = Counter(document_type_from_payload(item) for item in sources)
    document_type = source_types.most_common(1)[0][0]
    if not document_type:
        raise ValueError("Validated examples do not expose a document type")

    masks: list[dict[str, Any]] = []
    for position, source in enumerate(sources, 1):
        mask = build_annotation_mask(
            mask_id=f"validated-source-{position}",
            name=f"{args.client} validé {position}",
            version=1,
            source_document_id=source["documentId"],
            source_filename=source["filename"],
            created_by=REVIEWER,
            document_type=document_type,
            field_annotations=source["latestAnnotation"]["field_annotations"],
            pdf_path=store.pdf_path(source["documentId"]),
        )
        masks.append(
            {
                "mask": mask,
                "source": source,
                "pdfPath": store.pdf_path(source["documentId"]),
            }
        )

    targets: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for document in documents:
        if document["status"] == "HUMAN_VALIDATED":
            continue
        replaceable, reason = _machine_replaceable(document)
        target_type = document_type_from_payload(document)
        if not replaceable:
            skipped.append(
                {
                    "documentId": document["documentId"],
                    "filename": document["filename"],
                    "reason": reason,
                }
            )
        elif target_type != document_type:
            skipped.append(
                {
                    "documentId": document["documentId"],
                    "filename": document["filename"],
                    "reason": f"type={target_type or 'unknown'}",
                }
            )
        else:
            targets.append(document)

    prepared: dict[str, dict[str, Any]] = {}
    selections: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for position, target in enumerate(targets, 1):
        try:
            current_fields = target["latestAnnotation"].get("field_annotations") or []
            current_by_path = {
                str(item.get("path") or ""): item
                for item in current_fields
                if str(item.get("path") or "")
            }
            candidates: list[tuple[float, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
            for source_mask in masks:
                applied = apply_annotation_mask(
                    source_mask["mask"],
                    pdf_path=store.pdf_path(target["documentId"]),
                    source_pdf_path=source_mask["pdfPath"],
                    target_document_type=document_type,
                )
                score, metrics = _proposal_score(applied, current_by_path)
                candidates.append((score, metrics, applied, source_mask))
            candidates.sort(key=lambda item: item[0], reverse=True)
            score, metrics, applied, selected = candidates[0]
            fields = _merge_assisted_fields(
                current_fields,
                applied.get("fields") or [],
                source_filename=selected["source"]["filename"],
            )
            payload = {
                "status": "DRAFT",
                "reviewed_by": REVIEWER,
                "note": (
                    f"Préannotation assistée par {len(sources)} annotations {args.client} "
                    f"validées. Gabarit retenu : {selected['source']['filename']}. "
                    "Toutes les valeurs et tous les cadres doivent être relus."
                ),
                "field_annotations": fields,
            }
            normalize_studio_annotation(
                payload,
                document_id=target["documentId"],
                document_sha256="0" * 64,
                revision=1,
            )
            prepared[target["documentId"]] = payload
            selections.append(
                {
                    "documentId": target["documentId"],
                    "filename": target["filename"],
                    "sourceDocumentId": selected["source"]["documentId"],
                    "sourceFilename": selected["source"]["filename"],
                    **metrics,
                    "resultFieldCount": len(fields),
                }
            )
            print(
                f"[{position}/{len(targets)}] {target['filename']}: "
                f"{selected['source']['filename']} (score {score:.3f}, {len(fields)} champs)",
                flush=True,
            )
        except Exception as exc:
            failures.append(
                {
                    "documentId": target["documentId"],
                    "filename": target["filename"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            print(f"ÉCHEC {target['filename']}: {exc}", flush=True)

    if failures or len(prepared) != len(targets):
        raise RuntimeError(
            f"Incomplete assistance: {len(prepared)}/{len(targets)} prepared; "
            f"{len(failures)} failure(s). Studio was not modified."
        )

    report: dict[str, Any] = {
        "operation": "studio-client-assisted-annotation-v1",
        "createdAt": utc_now(),
        "client": args.client,
        "documentType": document_type,
        "validatedSourceCount": len(sources),
        "targetCount": len(targets),
        "skippedCount": len(skipped),
        "skipped": skipped,
        "selections": selections,
        "databaseUpdated": False,
    }
    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return 0

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = root / "backups" / f"before-client-assistance-{timestamp}"
    _backup_database(database, backup_root / "studio.sqlite3")
    annotations, fields = _commit(database, prepared)
    report.update(
        {
            "databaseUpdated": True,
            "annotationsCreated": annotations,
            "fieldCount": fields,
            "reviewer": REVIEWER,
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

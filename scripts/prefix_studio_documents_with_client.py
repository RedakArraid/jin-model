#!/usr/bin/env python3
"""Prefix existing JIN Studio filenames with their customer name.

The immutable document identifier and internal ``source.pdf`` path deliberately
stay unchanged.  Only the user-facing filename is migrated, so annotations,
masks, datasets and PDF links keep referring to the same document.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import unicodedata
from typing import Any, Mapping


TECHNICAL_SOURCE_DIRECTORIES = frozenset({"00_IDENTITE_A_VERIFIER"})
PREFIX_ALIASES = {
    "REXEL_FRANCE": "REXEL",
}
MAX_FILENAME_LENGTH = 180


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def normalize_client_prefix(value: Any) -> str:
    """Return an uppercase, filename-safe customer prefix."""

    folded = unicodedata.normalize("NFKD", str(value or "").strip())
    ascii_value = "".join(
        character for character in folded if not unicodedata.combining(character)
    )
    prefix = re.sub(r"[^A-Za-z0-9]+", "_", ascii_value).strip("_").upper()
    return PREFIX_ALIASES.get(prefix, prefix)


def _source_directory(source: Mapping[str, Any]) -> str:
    relative = str(source.get("sourceRelativePath") or "").replace("\\", "/").strip("/")
    if not relative:
        return ""
    parts = PurePosixPath(relative).parts
    directory = normalize_client_prefix(parts[0]) if len(parts) > 1 else ""
    return "" if directory in TECHNICAL_SOURCE_DIRECTORIES else directory


def _party_name(value: Any) -> str:
    if not isinstance(value, Mapping):
        return str(value or "").strip()
    return str(value.get("legal_name") or value.get("name") or "").strip()


def _buyer_prefix(extraction: Mapping[str, Any]) -> str:
    candidates: list[Any] = []
    order = extraction.get("order")
    if isinstance(order, Mapping):
        parties = order.get("parties")
        if isinstance(parties, Mapping):
            candidates.append(parties.get("buyer"))
    purchase_order = extraction.get("purchase_order")
    if isinstance(purchase_order, Mapping):
        parties = purchase_order.get("parties")
        if isinstance(parties, Mapping):
            candidates.append(parties.get("buyer"))
    parties = extraction.get("parties")
    if isinstance(parties, Mapping):
        candidates.append(parties.get("buyer"))
    for candidate in candidates:
        prefix = normalize_client_prefix(_party_name(candidate))
        if prefix:
            return prefix
    return ""


def resolve_client_prefix(
    source: Mapping[str, Any],
    extraction: Mapping[str, Any],
) -> tuple[str, str]:
    """Prefer the curated campaign directory over machine extraction."""

    directory = _source_directory(source)
    if directory:
        return directory, "sourceRelativePath"
    buyer = _buyer_prefix(extraction)
    if buyer:
        return buyer, "extraction.order.parties.buyer"
    return "", "unresolved"


def prefixed_filename(filename: str, prefix: str) -> str:
    """Build a stable PDF filename without exceeding Studio's 180-char limit."""

    original = Path(str(filename or "document.pdf")).name
    if original.upper().startswith(f"{prefix.upper()}_"):
        return original
    suffix = Path(original).suffix if Path(original).suffix.lower() == ".pdf" else ".pdf"
    stem = original[: -len(Path(original).suffix)] if Path(original).suffix else original
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "document"
    available = max(1, MAX_FILENAME_LENGTH - len(prefix) - 1 - len(suffix))
    return f"{prefix}_{stem[:available]}{suffix}"


def build_rename_plan(database: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    changes: list[dict[str, Any]] = []
    unresolved: list[dict[str, str]] = []
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT document_id, filename, source_json, extraction_json "
            "FROM studio_documents ORDER BY created_at, document_id"
        ).fetchall()
    for row in rows:
        source = _json_object(row["source_json"])
        extraction = _json_object(row["extraction_json"])
        prefix, prefix_source = resolve_client_prefix(source, extraction)
        if not prefix:
            unresolved.append(
                {"documentId": row["document_id"], "filename": row["filename"]}
            )
            continue
        new_filename = prefixed_filename(row["filename"], prefix)
        if new_filename == row["filename"]:
            continue
        updated_source = dict(source)
        updated_source.setdefault("originalStudioFilename", row["filename"])
        updated_source["clientPrefix"] = prefix
        updated_source["clientPrefixSource"] = prefix_source
        changes.append(
            {
                "documentId": row["document_id"],
                "oldFilename": row["filename"],
                "newFilename": new_filename,
                "clientPrefix": prefix,
                "clientPrefixSource": prefix_source,
                "sourceJson": json.dumps(
                    updated_source,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                ),
            }
        )
    return changes, unresolved


def backup_database(database: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(database) as source, sqlite3.connect(destination) as target:
        source.backup(target)


def apply_rename_plan(database: Path, changes: list[dict[str, Any]]) -> None:
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for change in changes:
            cursor = connection.execute(
                "UPDATE studio_documents SET filename=?, source_json=? "
                "WHERE document_id=? AND filename=?",
                (
                    change["newFilename"],
                    change["sourceJson"],
                    change["documentId"],
                    change["oldFilename"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Concurrent filename change for {change['documentId']}; migration aborted"
                )
        connection.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studio-root", type=Path, default=Path("data/studio"))
    parser.add_argument("--apply", action="store_true", help="apply after creating a backup")
    args = parser.parse_args()

    root = args.studio_root.resolve()
    database = (root / "studio.sqlite3").resolve()
    if root not in database.parents or not database.is_file():
        raise FileNotFoundError(database)
    changes, unresolved = build_rename_plan(database)
    report: dict[str, Any] = {
        "schema": "jin-studio-client-prefix-migration/v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "database": str(database),
        "mode": "apply" if args.apply else "preview",
        "renameCount": len(changes),
        "unresolvedCount": len(unresolved),
        "changes": [
            {key: value for key, value in change.items() if key != "sourceJson"}
            for change in changes
        ],
        "unresolved": unresolved,
    }
    if args.apply and changes:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_root = root / "backups" / f"before-client-prefix-{timestamp}"
        backup_database(database, backup_root / "studio.sqlite3")
        apply_rename_plan(database, changes)
        report["backup"] = str(backup_root / "studio.sqlite3")
        (backup_root / "rename-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not unresolved else 2


if __name__ == "__main__":
    raise SystemExit(main())

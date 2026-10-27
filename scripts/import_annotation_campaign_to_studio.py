#!/usr/bin/env python3
"""Import a generated review campaign into JIN Studio as draft annotations."""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

# Allow both ``python -m scripts...`` and direct script execution from the repo.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jin_runtime.studio_contract import FIELD_LABELS
from jin_runtime.studio_store import StudioStore


_LINE_COLLECTION_PATH = re.compile(
    r"(?P<prefix>(?:^|\.))(?:lines|line_items)\[(?P<index>\d+)\]"
)


def _compact_line_item_paths(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove gaps created when an extracted row has no localizable field.

    The annotation contract represents only rows that carry at least one
    annotation. Their JSON indexes must therefore describe that observable
    sequence, not retain holes from discarded machine candidates.
    """

    indexes = sorted(
        {
            int(match.group("index"))
            for field in fields
            if (match := _LINE_COLLECTION_PATH.search(str(field.get("path") or "")))
        }
    )
    replacements = {old: new for new, old in enumerate(indexes)}
    if not replacements:
        return fields
    for field in fields:
        path = str(field.get("path") or "")
        match = _LINE_COLLECTION_PATH.search(path)
        if not match:
            continue
        new_index = replacements[int(match.group("index"))]
        field["path"] = _LINE_COLLECTION_PATH.sub(
            lambda current: f"{current.group('prefix')}line_items[{new_index}]",
            path,
            count=1,
        )
    return fields


def _configure_console() -> None:
    """Keep progress output robust for decomposed accents and non-ASCII names."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="backslashreplace")


def _canonical_path(base: Any) -> str:
    return str(base or "field").strip()[:260] or "field"


def _valid_box(box: Any) -> bool:
    return (
        isinstance(box, list)
        and len(box) == 4
        and all(isinstance(value, (int, float)) for value in box)
        and 0 <= box[0] < box[2] <= 1000
        and 0 <= box[1] < box[3] <= 1000
    )


def draft_fields(annotation: dict[str, Any]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    field_indexes: dict[str, int] = {}

    def add_field(field: dict[str, Any]) -> None:
        """Keep one canonical JSON path and prefer the candidate with a box."""

        path = _canonical_path(field.get("path"))
        field["path"] = path
        existing_index = field_indexes.get(path)
        if existing_index is None:
            field_indexes[path] = len(fields)
            fields.append(field)
            return
        if "evidence" not in fields[existing_index] and "evidence" in field:
            fields[existing_index] = field

    document_labels = annotation.get("document_labels") or {}
    document_type = str(document_labels.get("document_type") or "unknown")
    document_field = {
        "path": "document.type",
        "label": "DOCUMENT_TYPE",
        "status": "UNREVIEWED",
        "extracted_value": document_type,
        "corrected_value": None,
        "comment": "Classe préremplie par JIN ; confirmer le libellé visible dans le document.",
    }
    type_evidence = document_labels.get("evidence") or {}
    if _valid_box(type_evidence.get("bbox")) and int(type_evidence.get("page") or 0) > 0:
        document_field["evidence"] = {
            "page": int(type_evidence["page"]),
            "bbox": type_evidence["bbox"],
        }
    add_field(document_field)

    for page in annotation.get("pages") or []:
        page_number = int(page.get("page") or 0)
        for region in page.get("regions") or []:
            label = str(region.get("label") or "").upper()
            if label not in FIELD_LABELS:
                continue
            box = region.get("bbox")
            valid_box = _valid_box(box)
            match_method = str(region.get("match_method") or "text")
            field = {
                "path": _canonical_path(region.get("source_path") or label),
                "label": label,
                "status": (
                    "UNREVIEWED"
                    if valid_box and match_method != "source_bbox_fallback"
                    else "AMBIGUOUS"
                ),
                "extracted_value": region.get("canonical_value", region.get("value")),
                "corrected_value": None,
                "comment": (
                    f"Préannotation Codex/JIN à confirmer ({match_method})."
                    if valid_box
                    else "Zone source dégénérée après normalisation ; redessiner la boîte."
                ),
            }
            if valid_box:
                field["evidence"] = {"page": page_number, "bbox": box}
            add_field(field)

    report = annotation.get("generation_report") or {}
    for unresolved in report.get("unresolved") or []:
        label = str(unresolved.get("label") or "").upper()
        if label not in FIELD_LABELS:
            continue
        reason = str(unresolved.get("reason") or "unresolved")
        add_field(
            {
                "path": _canonical_path(unresolved.get("path") or label),
                "label": label,
                "status": "AMBIGUOUS",
                "extracted_value": unresolved.get("canonical_value", unresolved.get("value")),
                "corrected_value": None,
                "comment": (
                    "Préannotation non localisée automatiquement "
                    f"({reason}) ; tracer la bonne zone, corriger ou marquer absent."
                ),
            }
        )
    return _compact_line_item_paths(fields)


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_dir", type=Path)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--studio-dir", type=Path, default=Path("data/studio"))
    parser.add_argument("--campaign-id")
    parser.add_argument(
        "--refresh-machine-drafts",
        action="store_true",
        help=(
            "Create a new revision only when the latest annotation is still a "
            "codex-preannotation DRAFT. Human revisions are never replaced."
        ),
    )
    args = parser.parse_args(argv)

    campaign = args.campaign_dir.resolve()
    source_root = args.source_root.resolve()
    campaign_id = args.campaign_id or campaign.name
    os.environ["JIN_STUDIO_DIR"] = str(args.studio_dir.resolve())
    store = StudioStore()
    existing = {
        (str(item.get("source", {}).get("campaignId") or ""), item["pdfSha256"]): item
        for item in store.list_documents(limit=1000)
    }

    with (campaign / "review_queue.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        records = list(csv.DictReader(handle))

    imported = []
    for position, record in enumerate(records, 1):
        digest = str(record["sha256"]).lower()
        relative = str(record["source_relative_path"]).replace("\\", "/")
        pdf_path = (source_root / Path(*relative.split("/"))).resolve()
        pdf_path.relative_to(source_root)
        annotation = json.loads(
            (campaign / record["annotation"]).read_text(encoding="utf-8")
        )
        extraction = json.loads(
            (campaign / record["extraction"]).read_text(encoding="utf-8")
        )
        current = existing.get((campaign_id, digest))
        if current is None:
            current = store.create_document(
                filename=pdf_path.name,
                pdf_bytes=pdf_path.read_bytes(),
                extraction=extraction.get("normalized_output") or extraction,
                source={
                    "source": "codex_assisted_annotation_campaign",
                    "campaignId": campaign_id,
                    "sourceRelativePath": relative,
                    "preannotationStatus": "REVIEW_REQUIRED",
                },
            )
        latest = current.get("latestAnnotation") or {}
        refreshable = (
            args.refresh_machine_drafts
            and latest.get("status") == "DRAFT"
            and latest.get("reviewed_by") == "codex-preannotation"
        )
        if not latest or refreshable:
            annotation_payload = {
                "status": "DRAFT",
                "reviewed_by": "codex-preannotation",
                "note": (
                    "Préannotation géométrique v2 produite par Codex/JIN. Chaque "
                    "champ doit être confirmé, corrigé, marqué absent ou ambigu "
                    "par un humain."
                ),
                "field_annotations": draft_fields(annotation),
            }
            saved = store.save_annotation(current["documentId"], annotation_payload)
            action = "REFRESHED" if refreshable else "IMPORTED"
        else:
            saved = latest
            action = "REUSED"
        imported.append(
            {
                "index": int(record["index"]),
                "document_id": current["documentId"],
                "sha256": digest,
                "source_relative_path": relative,
                "draft_annotation_id": saved.get("annotation_id"),
                "field_count": len((saved or {}).get("field_annotations") or []),
                "action": action,
            }
        )
        print(
            f"[{position}/{len(records)}] {pdf_path.name}: {action} "
            f"({imported[-1]['field_count']} fields)",
            flush=True,
        )

    output = campaign / "studio_import.json"
    output.write_text(
        json.dumps(
            {
                "campaign_id": campaign_id,
                "studio_dir": str(args.studio_dir.resolve()),
                "count": len(imported),
                "documents": imported,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"imported": len(imported), "studio": store.status()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

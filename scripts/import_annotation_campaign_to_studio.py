#!/usr/bin/env python3
"""Import a generated review campaign into JIN Studio as draft annotations."""
from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import sys
from typing import Any

# Allow both ``python -m scripts...`` and direct script execution from the repo.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from jin_runtime.studio_contract import FIELD_LABELS
from jin_runtime.studio_store import StudioStore


def _configure_console() -> None:
    """Keep progress output robust for decomposed accents and non-ASCII names."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="backslashreplace")


def _unique_path(base: str, used: set[str]) -> str:
    clean = str(base or "field").strip()[:260] or "field"
    candidate = clean
    counter = 2
    while candidate in used:
        candidate = f"{clean[:250]}#occurrence-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def draft_fields(annotation: dict[str, Any]) -> list[dict[str, Any]]:
    used: set[str] = set()
    fields: list[dict[str, Any]] = []
    document_type = str(
        (annotation.get("document_labels") or {}).get("document_type") or "unknown"
    )
    fields.append(
        {
            "path": _unique_path("document.type", used),
            "label": "DOCUMENT_TYPE",
            "status": "UNREVIEWED",
            "extracted_value": document_type,
            "corrected_value": None,
            "comment": "Classe préremplie par JIN ; confirmer même pour un document négatif.",
        }
    )

    for page in annotation.get("pages") or []:
        page_number = int(page.get("page") or 0)
        for region in page.get("regions") or []:
            label = str(region.get("label") or "").upper()
            if label not in FIELD_LABELS:
                continue
            box = region.get("bbox")
            valid_box = (
                isinstance(box, list)
                and len(box) == 4
                and all(isinstance(value, (int, float)) for value in box)
                and 0 <= box[0] < box[2] <= 1000
                and 0 <= box[1] < box[3] <= 1000
            )
            field = {
                "path": _unique_path(str(region.get("source_path") or label), used),
                "label": label,
                "status": "UNREVIEWED" if valid_box else "AMBIGUOUS",
                "extracted_value": region.get("canonical_value", region.get("value")),
                "corrected_value": None,
                "comment": (
                    "Préannotation Codex/JIN à confirmer."
                    if valid_box
                    else "Zone source dégénérée après normalisation ; redessiner la boîte."
                ),
            }
            if valid_box:
                field["evidence"] = {"page": page_number, "bbox": box}
            fields.append(field)

    report = annotation.get("generation_report") or {}
    for unresolved in report.get("unresolved") or []:
        label = str(unresolved.get("label") or "").upper()
        if label not in FIELD_LABELS:
            continue
        reason = str(unresolved.get("reason") or "unresolved")
        fields.append(
            {
                "path": _unique_path(str(unresolved.get("path") or label), used),
                "label": label,
                "status": "AMBIGUOUS",
                "extracted_value": unresolved.get("canonical_value", unresolved.get("value")),
                "corrected_value": None,
                "comment": (
                    "Préannotation non localisée automatiquement "
                    f"({reason}); tracer la bonne zone, corriger ou marquer absent."
                ),
            }
        )
    return fields


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign_dir", type=Path)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--studio-dir", type=Path, default=Path("data/studio"))
    parser.add_argument("--campaign-id")
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
        if not current.get("latestAnnotation"):
            annotation_payload = {
                "status": "DRAFT",
                "reviewed_by": "codex-preannotation",
                "note": (
                    "Préannotation produite par Codex/JIN. Chaque champ doit être "
                    "confirmé, corrigé, marqué absent ou ambigu par un humain."
                ),
                "field_annotations": draft_fields(annotation),
            }
            saved = store.save_annotation(current["documentId"], annotation_payload)
            action = "IMPORTED"
        else:
            saved = current.get("latestAnnotation") or {}
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

#!/usr/bin/env python3
"""Re-extract selected items of one campaign batch and merge them in place."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import time

import httpx

from audit_random_orders import (
    DELIVERY_LABEL_RE,
    ORDER_LABEL_RE,
    VAT_LABEL_RE,
    _automatic_checks,
    _contexts,
    _csv_row,
    _extract_model,
    _sha256,
    _source_text,
    _vat_source_candidates,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--batch", type=int, required=True)
    parser.add_argument("--indices", required=True, help="Comma-separated 1-based item indices")
    parser.add_argument("--api", default="http://localhost:8080/api/extract")
    args = parser.parse_args()

    folder = args.folder.resolve()
    campaign = args.campaign_dir.resolve()
    indices = sorted({int(value) for value in args.indices.split(",") if value.strip()})
    manifest = json.loads((campaign / "campaign_manifest.json").read_text(encoding="utf-8"))
    descriptor = next(
        (item for item in manifest.get("batches") or [] if int(item["batch"]) == args.batch),
        None,
    )
    if not descriptor:
        raise SystemExit(f"Unknown batch: {args.batch}")
    batch_dir = campaign / descriptor["output_dir"]
    bundle_path = batch_dir / "review_bundle.json"
    bundle_doc = json.loads(bundle_path.read_text(encoding="utf-8"))
    items = bundle_doc.get("items") or []
    if not indices or any(index < 1 or index > len(items) for index in indices):
        raise SystemExit(f"Indices must be between 1 and {len(items)}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = batch_dir / f"review_bundle.pre-replay-{stamp}.json"
    shutil.copy2(bundle_path, backup)
    replayed = []
    with httpx.Client(timeout=300) as client:
        for index in indices:
            old = items[index - 1]
            relative = str(old["filename"])
            path = folder / Path(*relative.replace("\\", "/").split("/"))
            digest = _sha256(path)
            started = time.perf_counter()
            try:
                with path.open("rb") as handle:
                    response = client.post(
                        args.api, files={"file": (path.name, handle, "application/pdf")}
                    )
                response.raise_for_status()
                payload = response.json()
                text = _source_text(payload)
                model = _extract_model(payload)
                source_vat = _vat_source_candidates(text)
                fresh = {
                    "index": index,
                    "filename": relative,
                    "sha256": digest,
                    "pages": (payload.get("document") or {}).get("page_count"),
                    "seconds": round(time.perf_counter() - started, 4),
                    "model": model,
                    "automatic_checks": _automatic_checks(model, text, source_vat),
                    "source_vat_candidates": source_vat,
                    "source_contexts": {
                        "order": _contexts(text, [ORDER_LABEL_RE]),
                        "number": _contexts(
                            text, [re.compile(re.escape(str(model.get("order_number"))), re.I)]
                        ) if model.get("order_number") else [],
                        "delivery": _contexts(text, [DELIVERY_LABEL_RE]),
                        "vat": _contexts(text, [VAT_LABEL_RE]),
                    },
                    "source_text": text,
                }
            except Exception as exc:
                raise SystemExit(f"Replay failed for {relative}: {type(exc).__name__}: {exc}") from exc
            items[index - 1] = fresh
            replayed.append({
                "index": index, "filename": relative,
                "old_model": old.get("model"), "new_model": fresh.get("model"),
            })
            print(f"[{index}/{len(items)}] {relative}: OK ({fresh['seconds']:.2f}s)", flush=True)

    bundle_path.write_text(
        json.dumps(bundle_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    rows = [_csv_row(item) for item in items if not item.get("error")]
    if rows:
        with (batch_dir / "audit_draft.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    log_path = batch_dir / "replay_log.json"
    history = json.loads(log_path.read_text(encoding="utf-8")) if log_path.is_file() else []
    history.append({
        "timestamp": stamp,
        "api": args.api,
        "backup": backup.name,
        "items": replayed,
    })
    log_path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create the source-support audit and review queue for one campaign batch."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from finalize_full_audit_campaign import _load_bundle, _row, _write_csv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--batch", type=int, required=True)
    args = parser.parse_args()

    campaign = args.campaign_dir.resolve()
    manifest = json.loads((campaign / "campaign_manifest.json").read_text(encoding="utf-8"))
    descriptor = next(
        (item for item in manifest.get("batches") or [] if int(item["batch"]) == args.batch),
        None,
    )
    if not descriptor:
        raise SystemExit(f"Unknown batch: {args.batch}")
    batch_dir = campaign / descriptor["output_dir"]
    planned = json.loads((campaign / descriptor["sample_file"]).read_text(encoding="utf-8"))
    bundle_path = batch_dir / "review_bundle.json"
    if not bundle_path.is_file():
        raise SystemExit(f"Missing bundle: {bundle_path}")
    items = _load_bundle(bundle_path)
    if len(items) != len(planned) or [item.get("filename") for item in items] != planned:
        raise SystemExit(f"Batch {args.batch:03d} is incomplete or reordered")

    rows = [
        _row(item, f"batch-{args.batch:03d}", args.batch, index)
        for index, item in enumerate(items, 1)
    ]
    review_rows = [row for row in rows if row["verdict_source"] != "SUPPORTE_PAR_SOURCE"]
    fields = list(rows[0]) if rows else []
    _write_csv(batch_dir / "audit_source.csv", rows, fields)
    _write_csv(batch_dir / "review_queue.csv", review_rows, fields)

    summary = {
        "batch": args.batch,
        "documents": len(rows),
        "errors": sum(bool(row["erreur"]) for row in rows),
        "orders": sum(row["est_commande_modele"] == "OUI" for row in rows),
        "source_verdicts": dict(Counter(row["verdict_source"] for row in rows)),
        "review_reasons": dict(Counter(
            reason
            for row in rows
            for reason in str(row["motifs_controle_source"] or "").split("|")
            if reason
        )),
        "number_controls": dict(Counter(row["controle_numero_source"] for row in rows)),
        "address_controls": dict(Counter(row["controle_adresse_source"] for row in rows)),
        "vat_controls": dict(Counter(row["controle_tva_source"] for row in rows)),
        "review_queue": len(review_rows),
    }
    (batch_dir / "audit_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return int(bool(summary["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())

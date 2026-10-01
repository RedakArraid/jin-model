#!/usr/bin/env python3
"""Merge the model run bundle with source-based manual reviews into the final CSV."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
from typing import Any


def _scalar(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("value")
    return value


def _fmt_score(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return f"{float(value):.6f}".rstrip("0").rstrip(".")
    return ""


def _codes(model: dict[str, Any]) -> str:
    return "|".join(sorted({
        str(reason.get("code"))
        for reason in model.get("review_reasons") or []
        if reason.get("code")
    }))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("reviews", type=Path)
    parser.add_argument("output_csv", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument(
        "--verdict-overrides",
        type=Path,
        help="Optional sparse JSON list of reviewed fields to override by item index.",
    )
    args = parser.parse_args()

    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    reviews = json.loads(args.reviews.read_text(encoding="utf-8"))
    overrides = {}
    if args.verdict_overrides:
        override_items = json.loads(args.verdict_overrides.read_text(encoding="utf-8"))
        overrides = {int(item["index"]): item for item in override_items}
    items = bundle.get("items") or []

    if len(items) != len(reviews):
        raise SystemExit(f"Bundle/review size mismatch: {len(items)} != {len(reviews)}")

    review_by_index = {int(review["index"]): review for review in reviews}
    if len(review_by_index) != len(reviews):
        raise SystemExit("Duplicate review index")

    rows: list[dict[str, Any]] = []
    for item in items:
        index = int(item["index"])
        if index not in review_by_index:
            raise SystemExit(f"Missing review for item {index}")
        review = dict(review_by_index[index])
        review.update({
            key: value
            for key, value in overrides.get(index, {}).items()
            if key != "index"
        })
        if review["filename"] != item["filename"]:
            raise SystemExit(
                f"Filename mismatch at item {index}: {review['filename']} != {item['filename']}"
            )
        model = item.get("model") or {}
        rows.append({
            "index": index,
            "fichier": item["filename"],
            "sha256": item.get("sha256", ""),
            "pages": item.get("pages", ""),
            "duree_traitement_s": item.get("seconds", ""),
            "type_document_modele": model.get("document_type", ""),
            "est_commande_modele": "OUI" if model.get("is_order") else "NON",
            "score_type_commande": _fmt_score(model.get("classification_score")),
            "score_global_modele": _fmt_score(model.get("overall_score")),
            "type_document_relu": review["reference_document_type"],
            "est_commande_relu": review["reference_is_order"],
            "verdict_type_commande": review["classification_verdict"],
            "numero_commande_client_modele": _scalar(model.get("order_number")) or "",
            "score_numero_commande": _fmt_score(model.get("order_number_score")),
            "numero_commande_client_relu": review["reference_order_number"],
            "verdict_numero_commande": review["order_number_verdict"],
            "adresse_livraison_modele": model.get("delivery_address") or "",
            "score_role_livraison": _fmt_score(model.get("delivery_role_score")),
            "score_adresse_livraison": _fmt_score(model.get("delivery_address_score")),
            "statut_verification_BAN": model.get("delivery_ban_status") or "",
            "adresse_BAN_verifiee": model.get("delivery_ban_verified")
            if model.get("delivery_ban_verified") is not None else "",
            "adresse_livraison_relue": review["reference_delivery_address"],
            "verdict_adresse_livraison": review["delivery_address_verdict"],
            "numero_tva_modele": _scalar(model.get("vat_number")) or "",
            "score_numero_tva": _fmt_score(model.get("vat_score")),
            "numero_tva_relu": review["reference_vat_number"],
            "verdict_numero_tva": review["vat_verdict"],
            "decision_modele": model.get("decision") or "",
            "codes_revue_modele": _codes(model),
            "verdict_global_audit": review["global_verdict"],
            "commentaire_audit_source": review["comment"],
        })

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        # Semicolon is the native Excel separator on the target French Windows
        # workstation; UTF-8 BOM preserves accents when the file is opened directly.
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "sample": {
            "count": len(rows),
            "seed": bundle.get("seed"),
            "selection": "tirage pseudo-aléatoire reproductible après déduplication SHA-256",
        },
        "verdict_counts": {
            "classification": dict(Counter(row["verdict_type_commande"] for row in rows)),
            "order_number": dict(Counter(row["verdict_numero_commande"] for row in rows)),
            "delivery_address": dict(Counter(row["verdict_adresse_livraison"] for row in rows)),
            "vat_number": dict(Counter(row["verdict_numero_tva"] for row in rows)),
            "global": dict(Counter(row["verdict_global_audit"] for row in rows)),
        },
        "strict_correct": {
            "classification": sum(row["verdict_type_commande"] == "CORRECT" for row in rows),
            "order_number": sum(row["verdict_numero_commande"] in {"CORRECT", "CORRECT_ABSENT"} for row in rows),
            "delivery_address": sum(row["verdict_adresse_livraison"] in {"CORRECT", "CORRECT_ABSENT"} for row in rows),
            "vat_number": sum(row["verdict_numero_tva"] in {"CORRECT", "CORRECT_ABSENT"} for row in rows),
        },
        "source_presence": {
            "vat_number_present": sum(bool(row["numero_tva_relu"]) for row in rows),
            "vat_number_extracted_when_present": sum(
                bool(row["numero_tva_relu"]) and bool(row["numero_tva_modele"])
                for row in rows
            ),
        },
        "notes": [
            "Les verdicts ont été établis par relecture indépendante du texte natif/OCR de chacun des 50 PDF.",
            "PARTIEL signifie que la bonne information est présente mais polluée, concaténée ou incomplète.",
            "INCERTAIN est utilisé quand le PDF ne qualifie pas explicitement le rôle livraison.",
            "Une confiance élevée du modèle ne remplace pas le verdict de relecture.",
        ],
    }
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"CSV: {args.output_csv.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

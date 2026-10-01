#!/usr/bin/env python3
"""Compare a new model run with the manually reviewed random-50 baseline."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import re
import unicodedata
from typing import Any


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9]+", "", text)


def _vat_values(value: Any) -> set[str]:
    compact = re.sub(r"[^A-Z0-9]", "", unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().upper())
    return set(re.findall(r"FR[A-Z0-9]{11}", compact))


def _address_verdict(old: dict[str, str], new_address: str) -> str:
    reference = old["adresse_livraison_relue"]
    if _norm(reference).startswith("NONEXPLICITEMENTLIBELLEE"):
        return "INCERTAIN"
    if not reference:
        return "CORRECT_ABSENT" if not new_address else "INCORRECT"
    if not new_address:
        return "MANQUANT"
    if (
        old.get("verdict_adresse_livraison") == "PARTIEL"
        and _norm(old.get("adresse_livraison_modele")) == _norm(new_address)
    ):
        return "PARTIEL"

    postal_reference = set(re.findall(r"(?<!\d)\d{5}(?!\d)", reference))
    postal_new = set(re.findall(r"(?<!\d)\d{5}(?!\d)", new_address))
    if postal_reference and not postal_reference.intersection(postal_new):
        return "INCORRECT"

    aliases = {
        "AV": "AVENUE", "BD": "BOULEVARD", "RTE": "ROUTE",
        "ST": "SAINT", "STE": "SAINTE", "BAT": "BATIMENT",
    }
    ignored = {
        "A", "AU", "AUX", "D", "DE", "DES", "DU", "EN", "ET", "L", "LA", "LE", "LES",
        "FRANCE", "ADRESSE", "LIVRAISON", "DEPOT", "EXPLICITEMENT", "LIBELLEE",
    }

    def tokens(value: str) -> set[str]:
        folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().upper()
        values = re.findall(r"[A-Z0-9]+", folded)
        return {
            aliases.get(token, token)
            for token in values
            if token not in ignored and token not in postal_reference and len(token) > 1
        }

    expected = tokens(reference)
    found = tokens(new_address)
    if not expected:
        return "CORRECT" if postal_reference.intersection(postal_new) else "PARTIEL"
    recall = len(expected.intersection(found)) / len(expected)
    if recall >= 0.72:
        return "CORRECT"
    if recall >= 0.45:
        return "PARTIEL"
    return "INCORRECT"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline_csv", type=Path)
    parser.add_argument("new_bundle", type=Path)
    parser.add_argument("output_csv", type=Path)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()

    with args.baseline_csv.open(encoding="utf-8-sig", newline="") as handle:
        baseline = list(csv.DictReader(handle, delimiter=";"))
    bundle = json.loads(args.new_bundle.read_text(encoding="utf-8"))
    items = bundle.get("items") or []
    if len(baseline) != len(items):
        raise SystemExit(f"Baseline/run size mismatch: {len(baseline)} != {len(items)}")

    rows: list[dict[str, Any]] = []
    for old, item in zip(baseline, items):
        model = item.get("model") or {}
        reference_is_order = old["est_commande_relu"] == "OUI"
        classification = "CORRECT" if bool(model.get("is_order")) == reference_is_order else "INCORRECT"
        reference_number = old["numero_commande_client_relu"]
        new_number = model.get("order_number") or ""
        if not reference_number and not new_number:
            number_verdict = "CORRECT_ABSENT"
        elif _norm(reference_number) == _norm(new_number):
            number_verdict = "CORRECT"
        elif not new_number:
            number_verdict = "MANQUANT"
        elif _norm(reference_number) in _norm(new_number) or _norm(new_number) in _norm(reference_number):
            number_verdict = "PARTIEL"
        else:
            number_verdict = "INCORRECT"

        reference_vat = _vat_values(old["numero_tva_relu"])
        new_vat = _vat_values(model.get("vat_number"))
        if not reference_vat and not new_vat:
            vat_verdict = "CORRECT_ABSENT"
        elif reference_vat & new_vat:
            vat_verdict = "CORRECT"
        elif not new_vat:
            vat_verdict = "MANQUANT"
        else:
            vat_verdict = "INCORRECT"

        new_address = model.get("delivery_address") or ""
        address_verdict = _address_verdict(old, new_address)
        changed = []
        if old["type_document_modele"] != (model.get("document_type") or ""): changed.append("classification")
        if _norm(old["numero_commande_client_modele"]) != _norm(new_number): changed.append("numero_commande")
        if _norm(old["adresse_livraison_modele"]) != _norm(new_address): changed.append("adresse_livraison")
        if _norm(old["numero_tva_modele"]) != _norm(model.get("vat_number")): changed.append("tva")
        rows.append({
            "index": old["index"], "fichier": old["fichier"],
            "type_avant": old["type_document_modele"], "type_apres": model.get("document_type") or "",
            "type_reference": old["type_document_relu"], "verdict_type_apres": classification,
            "numero_avant": old["numero_commande_client_modele"], "numero_apres": new_number,
            "numero_reference": reference_number, "verdict_numero_apres": number_verdict,
            "adresse_avant": old["adresse_livraison_modele"], "adresse_apres": new_address,
            "adresse_reference": old["adresse_livraison_relue"], "verdict_adresse_avant": old["verdict_adresse_livraison"],
            "verdict_adresse_apres": address_verdict,
            "tva_avant": old["numero_tva_modele"], "tva_apres": model.get("vat_number") or "",
            "tva_reference": old["numero_tva_relu"], "verdict_tva_apres": vat_verdict,
            "champs_modifies": "|".join(changed),
        })

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter=";")
        writer.writeheader(); writer.writerows(rows)

    strict = lambda key, allowed: sum(row[key] in allowed for row in rows)
    summary = {
        "sample": {"count": len(rows), "seed": bundle.get("seed"), "errors": sum("error" in item for item in items)},
        "before": {
            "classification_correct": sum(row["verdict_type_commande"] == "CORRECT" for row in baseline),
            "order_number_strict_correct": sum(row["verdict_numero_commande"] in {"CORRECT", "CORRECT_ABSENT"} for row in baseline),
            "delivery_address_strict_correct": sum(row["verdict_adresse_livraison"] in {"CORRECT", "CORRECT_ABSENT"} for row in baseline),
            "vat_strict_correct": sum(row["verdict_numero_tva"] in {"CORRECT", "CORRECT_ABSENT"} for row in baseline),
        },
        "after": {
            "classification_correct": strict("verdict_type_apres", {"CORRECT"}),
            "order_number_strict_correct": strict("verdict_numero_apres", {"CORRECT", "CORRECT_ABSENT"}),
            "purchase_order_numbers_correct": sum(
                row["verdict_numero_apres"] == "CORRECT" and old["est_commande_relu"] == "OUI"
                for row, old in zip(rows, baseline)
            ),
            "purchase_orders_in_reference": sum(old["est_commande_relu"] == "OUI" for old in baseline),
            "delivery_address_strict_correct": strict("verdict_adresse_apres", {"CORRECT", "CORRECT_ABSENT"}),
            "vat_strict_correct": strict("verdict_tva_apres", {"CORRECT", "CORRECT_ABSENT"}),
            "vat_extracted_when_present": sum(row["tva_reference"] != "" and row["verdict_tva_apres"] == "CORRECT" for row in rows),
            "vat_present_in_reference": sum(row["tva_reference"] != "" for row in rows),
        },
        "after_verdict_counts": {
            "classification": dict(Counter(row["verdict_type_apres"] for row in rows)),
            "order_number": dict(Counter(row["verdict_numero_apres"] for row in rows)),
            "delivery_address": dict(Counter(row["verdict_adresse_apres"] for row in rows)),
            "vat": dict(Counter(row["verdict_tva_apres"] for row in rows)),
        },
        "notes": [
            "Références et verdicts initiaux issus du rapport de relecture manuelle fourni en entrée.",
            "Une adresse PARTIELLE n'est promue à CORRECT que si le seul bruit signalé a disparu; les rôles incertains, adresses incomplètes et mélanges résiduels restent inchangés.",
            "La relance d’accusé de réception est correctement classée hors bon de commande; le numéro cité reste une référence associée à exposer séparément.",
        ],
    }
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

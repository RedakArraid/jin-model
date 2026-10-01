#!/usr/bin/env python3
"""Consolidate a complete JIN folder campaign and audit source support."""
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
    text = "".join(
        char for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    ).upper()
    return re.sub(r"[^A-Z0-9]+", "", text)


def _scalar(value: Any) -> Any:
    while isinstance(value, dict):
        value = value.get("value")
    return value if value not in ({}, [], "") else None


def _plausible_customer_order_number(value: Any) -> bool:
    """Reject prose/date fragments even when they occur verbatim in the PDF."""
    text = " ".join(str(value or "").split()).strip()
    folded = "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    ).casefold()
    if not text or len(text) > 64 or sum(char.isdigit() for char in text) < 2:
        return False
    if re.search(
        r"\b(?:janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|"
        r"octobre|novembre|decembre)\b",
        folded,
    ) and re.search(r"\b(?:le|du|au|reprise|fermeture)\b", folded):
        return False
    if len(re.findall(r"[A-Za-zÀ-ÿ]{2,}", text)) >= 4:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{1,63}", text))


def _valid_french_vat(value: Any) -> bool:
    compact = _norm(value)
    if not re.fullmatch(r"FR\d{11}", compact):
        return False
    key = int(compact[2:4])
    siren = int(compact[4:])
    return key == (12 + 3 * (siren % 97)) % 97


def _vat_equivalent_to_source(value: Any, candidate: Any) -> bool:
    """Allow a checksum-backed O/0 OCR repair in a French numeric VAT ID."""
    expected = _norm(value)
    observed = _norm(candidate)
    if expected == observed:
        return True
    return bool(
        _valid_french_vat(expected)
        and observed.startswith("FR")
        and observed[2:].replace("O", "0") == expected[2:]
    )


def _write_csv(
    path: Path,
    rows: list[dict[str, Any]],
    fieldnames: list[str] | None = None,
) -> None:
    """Write the current result even when it is empty.

    Empty review queues must replace a previous non-empty file; otherwise a
    successful replay leaves stale alerts on disk while the JSON summary says
    there are none.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = list(fieldnames or [])
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        if not fields:
            return
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _load_bundle(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") or []
    if not isinstance(items, list):
        raise SystemExit(f"Invalid bundle items: {path}")
    return items


def _field_statuses(item: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    if item.get("error"):
        return {
            "controle_type_source": "ERREUR_EXTRACTION",
            "controle_numero_source": "ERREUR_EXTRACTION",
            "controle_adresse_source": "ERREUR_EXTRACTION",
            "controle_tva_source": "ERREUR_EXTRACTION",
        }, [f"EXTRACTION_{item['error']}"]

    model = item.get("model") or {}
    text = str(item.get("source_text") or "")
    compact_text = _norm(text)
    checks = item.get("automatic_checks") or {}
    reasons: list[str] = []
    model_review_codes = {
        str(reason.get("code") if isinstance(reason, dict) else reason)
        for reason in model.get("review_reasons") or []
    }

    top = text[:5000]
    folded_top = "".join(
        char for char in unicodedata.normalize("NFKD", top)
        if not unicodedata.combining(char)
    ).upper()
    legal_terms_match = re.search(
        r"(?:^|\n)\s*(?:LES\s+)?CONDITIONS\s+GENERALES\s+D[^A-Z0-9\n]{0,3}ACHAT\b"
        r"(?![^\n]*(?:WWW\.|HTTPS?://))",
        folded_top,
    )
    non_order_match = re.search(
        r"(?:^|\n)\s*(?:CONFIRMATION|ACCUSE\s+DE\s+RECEPTION|RELANCE|NOTIFICATION\s+DE\s+RELANCE)"
        r"[^\n]{0,50}\bCOMMANDE|(?:^|\n)\s*(?:DEVIS|QUOTATION|OFFRE\s+DE\s+PRIX)\b|"
        r"(?:^|\n)[^\n]{0,50}\bDEMANDE\s+DE(?:\s+PAGE)?\s+PRIX\b|"
        r"(?:^|\n)\s*RELANCE\s+RETOUR(?:\(S\)|S)?\s+SANS\s+A\.?\s*R\.?\b|"
        r"\bDEMANDE\s+D[' ]?ANNULATION\s+D[' ]?UNE\s+COMMANDE\b|"
        r"(?:^|\n)\s*(?:RB\s+)?GENERAL\s+APPROVAL\s+FORM\b|"
        r"\b(?:ABANDON|ANNULATION)\s+(?:DE\s+)?RELIQUAT\s*(?:\n|$)",
        top,
        flags=re.I,
    )
    order_match = re.search(
        r"(?:BON\s+DE\s+COMMANDE|COMMANDE(?:\s+|[^\n]{1,40})FOURNISSEUR|"
        r"COMMANDE\s+REGROUPEE|"
        r"\bCOMMANDE\s+N(?:O|UMERO)?[^\w\n]{0,3}\s*[A-Z0-9]|PURCHASE\s+ORDER|"
        r"BESTELLUNG|(?:^|\n)\s*C\s+O\s+M\s+M\s+A\s+N\s+D\s+E\b|"
        r"(?:^|\n)\s*COMMANDE(?:\s+(?:N(?:O|UMERO)?[^\w\n]{0,3})?"
        r"\s*[A-Z0-9]|\s+DATE\s+DE\s+LIVRAISON|\s*(?:\n|$)))",
        top,
        flags=re.I,
    )
    structured_cpo_match = bool(
        re.search(r"\bADRESSE\s+DE\s+LIVRAISON\b", folded_top)
        and re.search(r"\bREF(?:ERENCE)?\s+CDE\s*:\s*[A-Z0-9]", folded_top)
        and re.search(
            r"\b(?:TARIF|PRIX)\s+UNITAIRE\b[^\n]{0,100}\bQUANTITE\b",
            folded_top,
        )
    )
    # A real purchase order can legitimately mention its applicable purchase
    # terms after the title and line items.  Treat a terms heading as the
    # document type only when it precedes any explicit order heading (or when
    # there is no order heading at all).  This keeps standalone CGA documents
    # non-orders without misclassifying orders whose body says that the sale is
    # subject to the buyer's ``conditions generales d'achat``.
    legal_terms_is_document = bool(
        legal_terms_match
        and (not order_match or legal_terms_match.start() < order_match.start())
    )
    explicit_non_order = bool(
        legal_terms_is_document
        or (
            non_order_match
            and (not order_match or non_order_match.start() < order_match.start())
        )
    )
    explicit_order = bool((order_match or structured_cpo_match) and not explicit_non_order)
    type_check = str(checks.get("type") or "A_VERIFIER")
    type_coherent = (
        (is_order := bool(model.get("is_order"))) and explicit_order
    ) or (not is_order and explicit_non_order) or type_check == "BON"
    type_status = "COHERENT_SOURCE" if type_coherent else "A_VERIFIER"
    if type_status != "COHERENT_SOURCE":
        reasons.append("TYPE_A_VERIFIER")

    number = _scalar(model.get("order_number"))
    order_numbers = [
        _scalar(value) for value in model.get("order_numbers") or []
        if _scalar(value)
    ]
    evidence = model.get("order_number_evidence") or {}
    evidence_text = str(evidence.get("source_text") or "")
    if not is_order and not number:
        number_status = "SANS_OBJET"
    elif is_order and len(order_numbers) >= 2 and all(
        _norm(value) in compact_text for value in order_numbers
    ):
        number_status = "MULTIPLES_SUPPORTES_PAR_SOURCE"
    elif not number:
        number_status = "MANQUANT"
        reasons.append("NUMERO_COMMANDE_MANQUANT")
    elif is_order and model_review_codes.intersection({
        "ORDER_NUMBER_CANDIDATES_DISAGREE",
        "ORDER_NUMBER_REPLACED_DIFFERENT_CORE_VALUE",
    }):
        number_status = "ROLE_A_VERIFIER"
        reasons.append("NUMERO_COMMANDE_ROLE_A_VERIFIER")
    elif is_order and not _plausible_customer_order_number(number):
        number_status = "VALEUR_IMPLAUSIBLE"
        reasons.append("NUMERO_COMMANDE_IMPLAUSIBLE")
    elif _norm(number) and _norm(number) in compact_text:
        number_status = "SUPPORTE_PAR_SOURCE"
    elif evidence_text and _norm(number) in _norm(evidence_text):
        number_status = "SUPPORTE_PAR_PREUVE"
    else:
        number_status = "NON_RETROUVE_DANS_SOURCE"
        reasons.append("NUMERO_NON_RETROUVE_DANS_SOURCE")

    address = str(model.get("delivery_address") or "").strip()
    components = model.get("delivery_components") or {}
    postal = _scalar(components.get("postal_code"))
    city = _scalar(components.get("city"))
    street = _scalar(components.get("street") or components.get("street_name"))
    recipient = _scalar(components.get("recipient"))
    address_evidence = model.get("delivery_evidence") or {}
    delivery_contexts = (item.get("source_contexts") or {}).get("delivery") or []
    content_values = [value for value in (postal, city, street, recipient) if value]
    content_matches = sum(bool(_norm(value) and _norm(value) in compact_text) for value in content_values)
    city_tokens = [
        _norm(token) for token in re.findall(r"[A-Za-zÀ-ÿ]{3,}", str(city or ""))
        if len(_norm(token)) >= 3
    ]
    city_supported = bool(
        city and (
            _norm(city) in compact_text
            or any(token in compact_text for token in city_tokens)
        )
    )
    essential_match = bool(postal and city_supported and _norm(postal) in compact_text)
    source_role_label = bool(re.search(
        r"(?:(?:ADRESSE|HDRESSE)\s+(?:(?:DE\s+)?LIVRAISON|DESTINATAIRE)|LIEU\s+DE\s+LIVRAISON|"
        r"(?:ADRESSE|HDRESSE)[^\n]{0,100}\bDE\s+LIVRAISON|A\s+LIVRER(?:\s+A)?|"
        r"SHIP\s+TO|DELIVERY\s+ADDRESS)",
        text,
        flags=re.I,
    ))
    role_supported = bool(
        delivery_contexts
        or source_role_label
        or "ship_to" in str(address_evidence.get("method") or "").lower()
        or "delivery" in str(address_evidence.get("method") or "").lower()
    )
    pickup_only = bool(re.search(
        r"(?:ADRESSE\s+(?:DE\s+)?LIVRAISON|LIEU\s+DE\s+LIVRAISON|A\s+LIVRER)"
        r"[\s\S]{0,160}\bENLEVEMENT\b",
        text,
        flags=re.I,
    ))
    if not is_order and not address:
        address_status = "SANS_OBJET"
    elif not address:
        if pickup_only:
            address_status = "NON_REQUISE_ENLEVEMENT"
        elif role_supported:
            address_status = "MANQUANTE_AVEC_LIBELLE_SOURCE"
            reasons.append("ADRESSE_LIVRAISON_MANQUANTE")
        else:
            address_status = "ABSENTE_OU_NON_EXPLICITE_SOURCE"
    elif essential_match and content_matches >= min(2, len(content_values)) and role_supported:
        address_status = "CONTENU_ET_ROLE_SUPPORTES"
    elif essential_match:
        address_status = "CONTENU_SUPPORTE_ROLE_A_VERIFIER"
        reasons.append("ROLE_ADRESSE_A_VERIFIER")
    else:
        address_status = "CONTENU_A_VERIFIER"
        reasons.append("CONTENU_ADRESSE_A_VERIFIER")

    vat = _scalar(model.get("vat_number"))
    source_vat = item.get("source_vat_candidates") or []
    vat_evidence = model.get("vat_evidence") or {}
    vat_evidence_text = str(vat_evidence.get("source_text") or "")
    if vat and (
        _norm(vat) in compact_text
        or any(_vat_equivalent_to_source(vat, candidate) for candidate in source_vat)
    ):
        vat_status = "SUPPORTEE_PAR_SOURCE"
    elif vat and vat_evidence_text and _norm(vat) in _norm(vat_evidence_text):
        vat_status = "SUPPORTEE_PAR_PREUVE"
    elif vat:
        vat_status = "NON_RETROUVEE_DANS_SOURCE"
        reasons.append("TVA_NON_RETROUVEE_DANS_SOURCE")
    elif source_vat:
        vat_status = "MANQUANTE"
        reasons.append("TVA_MANQUANTE")
    else:
        vat_status = "ABSENTE_SOURCE"

    return {
        "controle_type_source": type_status,
        "controle_numero_source": number_status,
        "controle_adresse_source": address_status,
        "controle_tva_source": vat_status,
    }, reasons


def _row(item: dict[str, Any], origin: str, batch: int | None, global_index: int) -> dict[str, Any]:
    statuses, reasons = _field_statuses(item)
    model = item.get("model") or {}
    if item.get("error"):
        verdict = "ERREUR_EXTRACTION"
    elif reasons:
        verdict = "REVUE_REQUISE"
    else:
        verdict = "SUPPORTE_PAR_SOURCE"
    review_reasons = model.get("review_reasons") or []
    vat_score = model.get("vat_score")
    if vat_score is None:
        candidate_scores = [
            candidate.get("score")
            for candidate in model.get("vat_candidates") or []
            if isinstance(candidate, dict) and isinstance(candidate.get("score"), (int, float))
        ]
        vat_score = max(candidate_scores) if candidate_scores else None
    return {
        "index_global": global_index,
        "origine": origin,
        "lot": f"{batch:03d}" if batch is not None else "PRECEDENT",
        "index_lot": item.get("index"),
        "fichier": item.get("filename"),
        "sha256": item.get("sha256"),
        "pages": item.get("pages"),
        "secondes": item.get("seconds"),
        "erreur": item.get("error"),
        "type_document_modele": model.get("document_type"),
        "est_commande_modele": "OUI" if model.get("is_order") else "NON",
        "score_type_commande": model.get("classification_score"),
        "score_global_modele": model.get("overall_score"),
        "numero_commande_client": _scalar(model.get("order_number"))
                                  or " | ".join(str(value) for value in model.get("order_numbers") or []),
        "nombre_commandes_client": len(model.get("order_numbers") or [])
                                   or (1 if _scalar(model.get("order_number")) else 0),
        "score_numero_commande": model.get("order_number_score"),
        "adresse_livraison": model.get("delivery_address"),
        "score_role_livraison": model.get("delivery_role_score"),
        "score_adresse_livraison": model.get("delivery_address_score"),
        "statut_ban": model.get("delivery_ban_status"),
        "ban_verifiee": model.get("delivery_ban_verified"),
        "numero_tva": _scalar(model.get("vat_number")),
        "score_numero_tva": vat_score,
        "decision_modele": model.get("decision"),
        **statuses,
        "verdict_source": verdict,
        "motifs_controle_source": "|".join(reasons),
        "codes_revue_modele": "|".join(sorted({
            str(reason.get("code")) for reason in review_reasons if isinstance(reason, dict)
        })),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--source-folder", type=Path, required=True)
    parser.add_argument("--prior-bundle", action="append", type=Path, default=[])
    args = parser.parse_args()

    campaign = args.campaign_dir.resolve()
    source = args.source_folder.resolve()
    manifest_path = campaign / "campaign_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    campaign_rows: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    item_by_filename: dict[str, tuple[dict[str, Any], str]] = {}
    item_by_sha: dict[str, tuple[dict[str, Any], str]] = {}
    global_index = 0

    for batch in manifest.get("batches") or []:
        number = int(batch["batch"])
        batch_dir = campaign / batch["output_dir"]
        planned = json.loads((campaign / batch["sample_file"]).read_text(encoding="utf-8"))
        bundle_path = batch_dir / "review_bundle.json"
        if not bundle_path.is_file():
            raise SystemExit(f"Missing bundle for batch {number:03d}: {bundle_path}")
        items = _load_bundle(bundle_path)
        actual = [item.get("filename") for item in items]
        if len(items) != len(planned) or actual != planned:
            raise SystemExit(f"Incomplete or reordered batch {number:03d}")
        batch_rows = []
        for item in items:
            global_index += 1
            row = _row(item, "campagne_20261001", number, global_index)
            batch_rows.append(row)
            campaign_rows.append(row)
            all_rows.append(row)
            item_by_filename[str(item.get("filename"))] = (item, "campagne_20261001")
            if item.get("sha256"):
                item_by_sha[str(item["sha256"])] = (item, "campagne_20261001")
        batch_fields = list(batch_rows[0]) if batch_rows else []
        _write_csv(batch_dir / "audit_source.csv", batch_rows, batch_fields)
        _write_csv(
            batch_dir / "review_queue.csv",
            [row for row in batch_rows if row["verdict_source"] != "SUPPORTE_PAR_SOURCE"],
            batch_fields,
        )

    for bundle_path in args.prior_bundle:
        origin = bundle_path.parent.name
        for item in _load_bundle(bundle_path.resolve()):
            global_index += 1
            row = _row(item, origin, None, global_index)
            all_rows.append(row)
            item_by_filename[str(item.get("filename"))] = (item, origin)
            if item.get("sha256"):
                item_by_sha[str(item["sha256"])] = (item, origin)

    expected_campaign = int(manifest.get("remaining_unique_contents") or 0)
    if len(campaign_rows) != expected_campaign:
        raise SystemExit(
            f"Campaign item count mismatch: {len(campaign_rows)} != {expected_campaign}"
        )

    duplicates = {
        str(item["duplicate"]): item for item in manifest.get("duplicate_contents") or []
    }
    inventory = []
    for path in sorted(
        candidate for candidate in source.rglob("*")
        if candidate.is_file() and candidate.suffix.lower() == ".pdf"
    ):
        relative = path.relative_to(source).as_posix()
        if relative in item_by_filename:
            item, origin = item_by_filename[relative]
            inventory.append({
                "fichier": relative, "sha256": item.get("sha256"),
                "statut": "EVALUE_UNIQUE", "origine": origin, "reference": relative,
            })
        elif relative in duplicates:
            duplicate = duplicates[relative]
            reference = str(duplicate["kept"])
            evaluated = item_by_sha.get(str(duplicate["sha256"]))
            inventory.append({
                "fichier": relative, "sha256": duplicate.get("sha256"),
                "statut": "DOUBLON_IDENTIQUE", "origine": evaluated[1] if evaluated else "INCONNUE",
                "reference": reference,
            })
        else:
            inventory.append({
                "fichier": relative, "sha256": None, "statut": "NON_COMPTABILISE",
                "origine": None, "reference": None,
            })

    unaccounted = [row for row in inventory if row["statut"] == "NON_COMPTABILISE"]
    if unaccounted:
        raise SystemExit(f"{len(unaccounted)} physical PDFs are not accounted for")

    review_queue = [row for row in all_rows if row["verdict_source"] != "SUPPORTE_PAR_SOURCE"]
    audit_fields = list(all_rows[0]) if all_rows else []
    _write_csv(campaign / "audit_campaign_source.csv", campaign_rows, audit_fields)
    _write_csv(campaign / "audit_all_unique.csv", all_rows, audit_fields)
    _write_csv(campaign / "manual_review_queue.csv", review_queue, audit_fields)
    _write_csv(
        campaign / "full_folder_inventory.csv",
        inventory,
        list(inventory[0]) if inventory else [],
    )

    summary = {
        "status": "complete",
        "physical_pdf_files": len(inventory),
        "unique_pdf_contents": len(all_rows),
        "campaign_unique_contents": len(campaign_rows),
        "previously_audited_unique_contents": len(all_rows) - len(campaign_rows),
        "duplicate_physical_files": sum(row["statut"] == "DOUBLON_IDENTIQUE" for row in inventory),
        "unaccounted_physical_files": len(unaccounted),
        "extraction_errors": sum(bool(row["erreur"]) for row in all_rows),
        "source_verdicts": dict(Counter(row["verdict_source"] for row in all_rows)),
        "type_controls": dict(Counter(row["controle_type_source"] for row in all_rows)),
        "order_number_controls": dict(Counter(row["controle_numero_source"] for row in all_rows)),
        "delivery_address_controls": dict(Counter(row["controle_adresse_source"] for row in all_rows)),
        "vat_controls": dict(Counter(row["controle_tva_source"] for row in all_rows)),
        "manual_review_queue": len(review_queue),
        "qualification": (
            "Source-support controls are deterministic checks against extracted source text. "
            "They are not a substitute for human visual ground-truth review."
        ),
    }
    (campaign / "campaign_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

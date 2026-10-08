#!/usr/bin/env python3
"""Run JIN on a reproducible random PDF sample and prepare a source-based audit bundle."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import random
import re
import sys
import time
import unicodedata
from typing import Any

import httpx


ORDER_TYPES = {"purchase_order", "purchase_order_with_terms"}
VAT_KEY_NAMES = {"vat_number", "vat_id", "tva_number", "tax_id", "intracom_vat"}
VAT_LABEL_RE = re.compile(
    r"(?:TVA|VAT|INTRA\s*COMMUNAUTAIRE|INTRACOM|UST[. -]?ID|MWST)", re.I
)
VAT_PATTERNS = (
    re.compile(r"\bFR\s?[A-Z0-9]{2}(?:[ .-]?\d){9}\b", re.I),
    re.compile(r"\bDE(?:[ .-]?\d){9}\b", re.I),
    re.compile(r"\bIT(?:[ .-]?\d){11}\b", re.I),
    re.compile(r"\bES\s?[A-Z0-9](?:[ .-]?\d){7}[A-Z0-9]\b", re.I),
    re.compile(r"\bBE(?:[ .-]?\d){10}\b", re.I),
    re.compile(r"\bNL(?:[ .-]?\d){9}B\d{2}\b", re.I),
    re.compile(r"\b(?:LU|ATU|PT|PL|GB)[A-Z0-9 .-]{8,14}\b", re.I),
)
ORDER_LABEL_RE = re.compile(
    r"(?:BON\s+DE\s+COMMANDE|COMMANDE\s+(?:CLIENT|FOURNISSEUR|REGROUPEE)|"
    r"COMMANDE\s*N(?:[?]|O|UMERO)?\s*[A-Z0-9]|PURCHASE\s+ORDER|"
    r"BESTELLUNG|BESTELLNR|N[°O]\s*(?:DE\s*)?COMMANDE|ORDER\s+(?:NO|NUMBER))",
    re.I,
)
DELIVERY_LABEL_RE = re.compile(
    r"(?:(?:ADRESSE|HDRESSE)\s+(?:DE\s+LIVRAISON|DESTINATAIRE)|LIEU\s+DE\s+LIVRAISON|"
    r"A\s+LIVRER(?:\s+A)?|SHIP\s+TO|"
    r"DELIVERY\s+ADDRESS|WARENEMPF[AÄ]NGER)", re.I,
)
DATE_LABEL_RE = re.compile(
    r"(?:DATE\s+(?:DE\s+)?COMMANDE|ORDER\s+DATE|DATE\s+D['’]EMISSION|"
    r"(?:FAIT|EMIS)\s+LE)", re.I,
)
AGENCY_LABEL_RE = re.compile(
    r"(?:CODE\s+(?:AGENCE|SITE|ETABLISSEMENT)|AGENCE\s*(?:CLIENT)?|"
    r"CUSTOMER\s+(?:BRANCH|SITE)\s+CODE)", re.I,
)
CONTACT_LABEL_RE = re.compile(
    r"(?:CONTACT|INTERLOCUTEUR|DEMANDEUR|ACHETEUR|TELEPHONE|COURRIEL|E-?MAIL)", re.I,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _value(field: Any) -> Any:
    # Extracted fields are dictionaries even when no value was found.  Returning
    # that empty metadata dictionary would leak a Python/JSON object into the CSV
    # instead of an empty cell.
    if isinstance(field, dict):
        value = field.get("value")
        if isinstance(value, dict):
            return _value(value)
        return value
    return field


def _confidence(field: Any) -> float | None:
    if not isinstance(field, dict):
        return None
    for key in ("final_confidence", "confidence", "validation_score"):
        value = field.get(key)
        if isinstance(value, (int, float)):
            return round(float(value), 6)
    return None


def _norm(value: Any) -> str:
    text = "".join(char for char in unicodedata.normalize("NFKD", str(value or ""))
                   if not unicodedata.combining(char)).upper()
    return re.sub(r"[^A-Z0-9]+", "", text)


def _po(payload: dict[str, Any]) -> dict[str, Any]:
    business = payload.get("business_extractions") or {}
    return business.get("purchase_order") or business.get("purchase_order_with_terms") or {}


def _source_text(payload: dict[str, Any]) -> str:
    pages = payload.get("pages") or _po(payload).get("pages") or []
    chunks = []
    for page in pages:
        text = page.get("text") if isinstance(page, dict) else None
        if text:
            chunks.append(f"\n--- PAGE {page.get('page', len(chunks) + 1)} ---\n{text}")
    return "\n".join(chunks).strip()


def _contexts(text: str, patterns: list[re.Pattern[str]], radius: int = 180, limit: int = 5) -> list[str]:
    collapsed = re.sub(r"[\t ]+", " ", text)
    found = []
    for pattern in patterns:
        for match in pattern.finditer(collapsed):
            excerpt = collapsed[max(0, match.start() - radius): min(len(collapsed), match.end() + radius)]
            excerpt = re.sub(r"\n{3,}", "\n\n", excerpt).strip()
            if excerpt not in found:
                found.append(excerpt)
            if len(found) >= limit:
                return found
    return found


def _vat_source_candidates(text: str) -> list[str]:
    values = []
    for line in text.splitlines():
        if not VAT_LABEL_RE.search(line):
            continue
        for pattern in VAT_PATTERNS:
            values.extend(match.group(0).strip() for match in pattern.finditer(line))
        # Some PDFs put the value on the immediately adjacent visual fragment;
        # retain the labelled line for manual review even when the regex misses.
    return list(dict.fromkeys(values))


def _model_vat_candidates(value: Any, path: str = "") -> list[dict[str, Any]]:
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key.lower() in VAT_KEY_NAMES and _value(child) not in (None, ""):
                found.append({
                    "path": child_path,
                    "value": _value(child),
                    "score": _confidence(child) if isinstance(child, dict) else _confidence(value),
                    "evidence": (
                        child.get("evidence")
                        if isinstance(child, dict)
                        else value.get("evidence")
                    ),
                })
            found.extend(_model_vat_candidates(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_model_vat_candidates(child, f"{path}[{index}]"))
    unique = {}
    for item in found:
        unique[(_norm(item["value"]), item["path"])] = item
    return list(unique.values())


def _classification_score(payload: dict[str, Any]) -> float | None:
    quality = payload.get("quality") or {}
    value = quality.get("classification_confidence")
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    classifications = (payload.get("document") or {}).get("page_classifications") or []
    scores = [item.get("confidence") for item in classifications
              if isinstance(item.get("confidence"), (int, float))]
    return round(sum(scores) / len(scores), 6) if scores else None


def _extract_model(payload: dict[str, Any]) -> dict[str, Any]:
    document = payload.get("document") or {}
    po = _po(payload)
    header = po.get("purchase_order") or po
    number = header.get("number") or header.get("order_number") or {}
    clean = payload.get("normalized_output") or {}
    clean_order = clean.get("order") or payload.get("order") or {}
    delivery = clean_order.get("delivery_address") or {}
    verification = delivery.get("verification") or {}
    order_date = clean_order.get("order_date") or header.get("order_date") or header.get("date") or {}
    agency = (
        clean_order.get("customer_agency_code")
        or header.get("customer_agency_code")
        or payload.get("customer_agency_code")
        or {}
    )
    parties = clean_order.get("parties") or {}
    buyer = parties.get("buyer") or {}
    buyer_contact = buyer.get("contact") or {}
    bill_to = parties.get("bill_to") or {}
    bill_to_contact = bill_to.get("contact") or {}
    ship_to = parties.get("ship_to") or {}
    ship_to_contact = ship_to.get("contact") or {}
    vat = _model_vat_candidates(po)
    if not vat:
        vat = _model_vat_candidates(payload.get("document_tax_identifiers") or [])
    vat_by_value: dict[str, dict[str, Any]] = {}
    for candidate in vat:
        key = _norm(candidate.get("value"))
        previous = vat_by_value.get(key)
        if previous is None or float(candidate.get("score") or -1) > float(previous.get("score") or -1):
            vat_by_value[key] = candidate
    vat = list(vat_by_value.values())
    generic_number = (payload.get("document_references") or {}).get("customer_order_number") or {}
    if not _value(number) and _value(generic_number):
        number = generic_number
    grouped_fields = payload.get("customer_order_references") or header.get("numbers") or []
    grouped_numbers = [
        _value(field) for field in grouped_fields
        if _value(field) not in (None, "")
    ]
    return {
        "document_type": document.get("primary_document_type") or document.get("detected_document_type"),
        "is_order": (document.get("primary_document_type") or document.get("detected_document_type")) in ORDER_TYPES,
        "classification_score": _classification_score(payload),
        "overall_score": (payload.get("quality") or {}).get("overall_confidence"),
        "order_number": _value(number),
        "order_numbers": grouped_numbers,
        "order_number_score": _confidence(number),
        "order_number_evidence": number.get("evidence") if isinstance(number, dict) else None,
        "order_date": _value(order_date),
        "order_date_raw": order_date.get("raw_value") if isinstance(order_date, dict) else None,
        "order_date_score": _confidence(order_date),
        "order_date_evidence": order_date.get("evidence") if isinstance(order_date, dict) else None,
        "customer_agency_code": _value(agency),
        "customer_agency_site": agency.get("site") if isinstance(agency, dict) else None,
        "customer_agency_score": _confidence(agency),
        "customer_agency_status": (
            agency.get("status") or agency.get("validation_status")
            if isinstance(agency, dict) else None
        ),
        "customer_agency_evidence": agency.get("evidence") if isinstance(agency, dict) else None,
        "buyer_name": buyer.get("name") or buyer.get("legal_name"),
        "buyer_code": buyer.get("code") or buyer.get("customer_code"),
        "buyer_vat_number": buyer.get("vat_number"),
        "buyer_contact_name": buyer_contact.get("name"),
        "buyer_contact_email": buyer_contact.get("email"),
        "buyer_contact_phone": buyer_contact.get("phone"),
        "bill_to_name": bill_to.get("name") or bill_to.get("legal_name"),
        "bill_to_contact_name": bill_to_contact.get("name"),
        "bill_to_contact_email": bill_to_contact.get("email"),
        "bill_to_contact_phone": bill_to_contact.get("phone"),
        "ship_to_name": ship_to.get("name") or ship_to.get("legal_name"),
        "ship_to_contact_name": ship_to_contact.get("name"),
        "ship_to_contact_email": ship_to_contact.get("email"),
        "ship_to_contact_phone": ship_to_contact.get("phone"),
        "delivery_address": delivery.get("formatted"),
        "delivery_lines": delivery.get("formatted_lines") or [],
        "delivery_components": delivery.get("components") or {},
        "delivery_role_score": delivery.get("role_confidence"),
        "delivery_address_score": delivery.get("address_confidence"),
        "delivery_ban_status": verification.get("status"),
        "delivery_ban_verified": verification.get("verified"),
        "delivery_evidence": delivery.get("evidence") or {},
        "vat_candidates": vat,
        "vat_number": vat[0]["value"] if vat else None,
        "vat_score": vat[0].get("score") if vat else None,
        "vat_evidence": vat[0].get("evidence") if vat else None,
        "decision": (payload.get("extraction_decision") or {}).get("status"),
        "review_reasons": (payload.get("extraction_decision") or {}).get("reasons") or [],
    }


def _automatic_checks(model: dict[str, Any], text: str, source_vat: list[str]) -> dict[str, str]:
    top = text[:5000]
    non_order_title = bool(re.search(
        r"(?:CONFIRMATION|ACCUSE\s+DE\s+RECEPTION|RELANCE|NOTIFICATION\s+DE\s+RELANCE)"
        r"[^\n]{0,50}\bCOMMANDE|(?:^|\n)\s*(?:DEVIS|QUOTATION|OFFRE\s+DE\s+PRIX)\b|"
        r"(?:^|\n)[^\n]{0,50}\bDEMANDE\s+DE(?:\s+PAGE)?\s+PRIX\b|"
        r"(?:^|\n)\s*RELANCE\s+RETOUR(?:\(S\)|S)?\s+SANS\s+A\.?\s*R\.?\b|"
        r"(?:^|\n)\s*(?:RB\s+)?GENERAL\s+APPROVAL\s+FORM\b",
        top,
        flags=re.I,
    ))
    order_signal = bool(
        (ORDER_LABEL_RE.search(top) or re.search(r"(?:^|\n)\s*COMMANDE\s*(?:\n|$)", top, re.I))
        and not non_order_title
    )
    type_check = "BON" if model["is_order"] == order_signal else "A_VERIFIER"

    number = model.get("order_number")
    order_numbers = model.get("order_numbers") or []
    evidence = model.get("order_number_evidence") or {}
    number_in_source = bool(number and _norm(number) in _norm(text))
    evidence_method = str(evidence.get("extraction_method") or "")
    explicit = any(token in evidence_method for token in ("explicit", "spatial_order", "labeled"))
    number_check = "BON" if number_in_source and explicit else "A_VERIFIER"
    if order_numbers and all(_norm(value) in _norm(text) for value in order_numbers):
        number_check = "BON_MULTIPLE"
    elif model["is_order"] and not number:
        number_check = "FAUX_MANQUANT"

    date_evidence = model.get("order_date_evidence") or {}
    date_source = date_evidence.get("source_text") or model.get("order_date_raw")
    if model.get("order_date") and date_source and _norm(date_source) in _norm(text):
        date_check = "BON_PREUVE"
    elif model.get("order_date"):
        date_check = "A_VERIFIER"
    elif model["is_order"]:
        date_check = "ABSENT"
    else:
        date_check = "NON_APPLICABLE"

    agency = model.get("customer_agency_code")
    agency_evidence = model.get("customer_agency_evidence") or {}
    agency_source = agency_evidence.get("source_text")
    if agency and _norm(agency) in _norm(text):
        agency_check = "BON_SOURCE"
    elif agency and agency_source and _norm(agency_source) in _norm(text):
        agency_check = "BON_PREUVE"
    elif agency:
        agency_check = "A_VERIFIER"
    else:
        # Agency codes are optional. Their absence is not an extraction error
        # unless a later audit proves that an agency marker exists in source.
        agency_check = "ABSENT"

    buyer_name = model.get("buyer_name")
    if buyer_name and _norm(buyer_name) in _norm(text):
        buyer_check = "BON_SOURCE"
    elif buyer_name:
        buyer_check = "A_VERIFIER"
    else:
        buyer_check = "ABSENT"

    buyer_contacts = [
        model.get("buyer_contact_name"),
        model.get("buyer_contact_email"),
        model.get("buyer_contact_phone"),
    ]
    present_contacts = [value for value in buyer_contacts if value]
    if present_contacts and all(_norm(value) in _norm(text) for value in present_contacts):
        buyer_contact_check = "BON_SOURCE"
    elif present_contacts:
        buyer_contact_check = "A_VERIFIER"
    else:
        buyer_contact_check = "ABSENT"

    ship_to_name = model.get("ship_to_name")
    if ship_to_name and _norm(ship_to_name) in _norm(text):
        ship_to_check = "BON_SOURCE"
    elif ship_to_name:
        ship_to_check = "A_VERIFIER"
    else:
        ship_to_check = "ABSENT"

    components = model.get("delivery_components") or {}
    essential = [components.get("postal_code"), components.get("city")]
    address_check = "A_VERIFIER"
    if model.get("delivery_address") and all(value and _norm(value) in _norm(text) for value in essential):
        address_check = "BON_CONTENU_A_CONFIRMER_ROLE"
    elif model["is_order"] and not model.get("delivery_address"):
        address_check = (
            "FAUX_MANQUANT"
            if DELIVERY_LABEL_RE.search(text)
            else "ABSENTE_SOURCE_EXPLICITE"
        )

    model_vat = model.get("vat_number")
    vat_evidence = model.get("vat_evidence") or {}
    if model_vat and any(_norm(model_vat) == _norm(value) for value in source_vat):
        vat_check = "BON"
    elif (
        model_vat
        and _norm(model_vat)
        and _norm(model_vat) in _norm(vat_evidence.get("source_text"))
    ):
        vat_check = "BON_PREUVE"
    elif model_vat:
        vat_check = "A_VERIFIER"
    elif source_vat:
        vat_check = "FAUX_MANQUANT"
    else:
        vat_check = "BON_ABSENT_SOURCE"
    return {
        "type": type_check,
        "order_number": number_check,
        "order_date": date_check,
        "customer_agency_code": agency_check,
        "buyer": buyer_check,
        "buyer_contact": buyer_contact_check,
        "ship_to": ship_to_check,
        "delivery_address": address_check,
        "vat": vat_check,
    }


def _csv_row(item: dict[str, Any]) -> dict[str, Any]:
    model, checks = item["model"], item["automatic_checks"]
    reasons = model.get("review_reasons") or []
    return {
        "fichier": item["filename"], "sha256": item["sha256"], "pages": item["pages"],
        "type_document_modele": model.get("document_type"),
        "est_commande_modele": "OUI" if model.get("is_order") else "NON",
        "score_type_commande": model.get("classification_score"),
        "score_global_modele": model.get("overall_score"),
        "numero_commande_client": model.get("order_number") or " | ".join(model.get("order_numbers") or []),
        "nombre_commandes_client": len(model.get("order_numbers") or []) or (1 if model.get("order_number") else 0),
        "score_numero_commande": model.get("order_number_score"),
        "date_commande": model.get("order_date"),
        "date_commande_source": model.get("order_date_raw"),
        "score_date_commande": model.get("order_date_score"),
        "code_agence_client": model.get("customer_agency_code"),
        "site_code_agence": model.get("customer_agency_site"),
        "score_code_agence": model.get("customer_agency_score"),
        "statut_code_agence": model.get("customer_agency_status"),
        "client_acheteur": model.get("buyer_name"),
        "code_client": model.get("buyer_code"),
        "tva_client": model.get("buyer_vat_number"),
        "contact_client": model.get("buyer_contact_name"),
        "email_contact_client": model.get("buyer_contact_email"),
        "telephone_contact_client": model.get("buyer_contact_phone"),
        "client_facture": model.get("bill_to_name"),
        "contact_facturation": model.get("bill_to_contact_name"),
        "email_contact_facturation": model.get("bill_to_contact_email"),
        "telephone_contact_facturation": model.get("bill_to_contact_phone"),
        "destinataire_livraison": model.get("ship_to_name"),
        "contact_livraison": model.get("ship_to_contact_name"),
        "email_contact_livraison": model.get("ship_to_contact_email"),
        "telephone_contact_livraison": model.get("ship_to_contact_phone"),
        "adresse_livraison": model.get("delivery_address"),
        "score_role_livraison": model.get("delivery_role_score"),
        "score_adresse_livraison": model.get("delivery_address_score"),
        "statut_ban": model.get("delivery_ban_status"),
        "ban_verifiee": model.get("delivery_ban_verified"),
        "numero_tva": model.get("vat_number"), "score_numero_tva": model.get("vat_score"),
        "decision_modele": model.get("decision"),
        "verification_type": checks["type"],
        "verification_numero": checks["order_number"],
        "verification_date_commande": checks["order_date"],
        "verification_code_agence": checks["customer_agency_code"],
        "verification_client": checks["buyer"],
        "verification_contact_client": checks["buyer_contact"],
        "verification_destinataire": checks["ship_to"],
        "verification_adresse_livraison": checks["delivery_address"],
        "verification_tva": checks["vat"],
        "verdict_global": "A_VERIFIER_MANUELLEMENT",
        "commentaire_audit": "",
        "codes_revue_modele": "|".join(sorted({str(reason.get('code')) for reason in reasons})),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20260930)
    parser.add_argument("--api", default="http://localhost:8080/api/extract")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--sample-file", type=Path,
        help="Replay the exact relative filenames from a JSON list instead of drawing a new sample.",
    )
    parser.add_argument(
        "--inventory-file", type=Path,
        help=(
            "Optional semicolon-delimited inventory with SHA256 and Path columns. "
            "Uses the verified hashes instead of rereading every PDF before a draw."
        ),
    )
    parser.add_argument(
        "--exclude-sample", action="append", type=Path, default=[],
        help="JSON file containing relative filenames to exclude; may be repeated.",
    )
    parser.add_argument(
        "--exclude-sha-file", action="append", type=Path, default=[],
        help="JSON list of SHA-256 contents to exclude; may be repeated.",
    )
    parser.add_argument(
        "--exclude-bundle", action="append", type=Path, default=[],
        help="Review bundle whose item SHA-256 values must be excluded; may be repeated.",
    )
    parser.add_argument(
        "--force-reextract", action="store_true",
        help="Ignore successful items from an existing bundle and call the API again.",
    )
    parser.add_argument(
        "--reextract-indices",
        help=(
            "Comma-separated one-based sample indices to call again while reusing "
            "the other successful items from the existing bundle."
        ),
    )
    args = parser.parse_args(argv)
    folder = args.folder.resolve()
    excluded: set[str] = set()
    excluded_hashes: set[str] = set()
    for sample_path in args.exclude_sample:
        values = json.loads(sample_path.read_text(encoding="utf-8"))
        if not isinstance(values, list):
            raise SystemExit(f"Excluded sample must be a JSON list: {sample_path}")
        excluded.update(str(value).replace("\\", "/") for value in values)
    for sha_path in args.exclude_sha_file:
        values = json.loads(sha_path.read_text(encoding="utf-8-sig"))
        if not isinstance(values, list):
            raise SystemExit(f"Excluded SHA file must be a JSON list: {sha_path}")
        for value in values:
            digest = str(value).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise SystemExit(f"Invalid SHA-256 in {sha_path}: {value}")
            excluded_hashes.add(digest)
    for bundle_path in args.exclude_bundle:
        previous = json.loads(bundle_path.read_text(encoding="utf-8-sig"))
        items = previous.get("items") if isinstance(previous, dict) else None
        if not isinstance(items, list):
            raise SystemExit(f"Excluded bundle must contain an items list: {bundle_path}")
        for item in items:
            digest = str((item or {}).get("sha256") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise SystemExit(f"Invalid or missing SHA-256 in {bundle_path}: {item}")
            excluded_hashes.add(digest)
    if args.sample_file:
        values = json.loads(args.sample_file.read_text(encoding="utf-8"))
        if not isinstance(values, list) or not values:
            raise SystemExit(f"Replay sample must be a non-empty JSON list: {args.sample_file}")
        selected = []
        seen_hashes = set()
        for value in values:
            relative = Path(*str(value).replace("\\", "/").split("/"))
            path = (folder / relative).resolve()
            try:
                path.relative_to(folder)
            except ValueError as exc:
                raise SystemExit(f"Replay path escapes source folder: {value}") from exc
            if not path.is_file() or path.suffix.lower() != ".pdf":
                raise SystemExit(f"Replay PDF not found: {value}")
            digest = _sha256(path)
            if digest in seen_hashes:
                raise SystemExit(f"Duplicate replay PDF content: {value}")
            seen_hashes.add(digest)
            selected.append((digest, path))
        args.count = len(selected)
    elif args.inventory_file:
        unique: dict[str, Path] = {}
        with args.inventory_file.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=";")
            if not reader.fieldnames or not {"SHA256", "Path"}.issubset(reader.fieldnames):
                raise SystemExit(
                    f"Inventory must contain SHA256 and Path columns: {args.inventory_file}"
                )
            for row in reader:
                digest = str(row.get("SHA256") or "").strip().lower()
                if not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise SystemExit(
                        f"Invalid SHA-256 in inventory {args.inventory_file}: {digest}"
                    )
                path = Path(str(row.get("Path") or "")).resolve()
                try:
                    relative = path.relative_to(folder).as_posix()
                except ValueError as exc:
                    raise SystemExit(
                        f"Inventory path escapes source folder: {path}"
                    ) from exc
                if relative in excluded or digest in excluded_hashes:
                    continue
                if not path.is_file() or path.suffix.lower() != ".pdf":
                    raise SystemExit(f"Inventory PDF not found: {path}")
                unique.setdefault(digest, path)
        selected = list(unique.items())
        random.Random(args.seed).shuffle(selected)
        selected = selected[:args.count]
    else:
        candidates = sorted(path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf")
        unique: dict[str, Path] = {}
        for path in candidates:
            if path.relative_to(folder).as_posix() in excluded:
                continue
            digest = _sha256(path)
            if digest.lower() in excluded_hashes:
                continue
            unique.setdefault(digest, path)
        selected = list(unique.items())
        random.Random(args.seed).shuffle(selected)
        selected = selected[:args.count]
    if len(selected) < args.count:
        raise SystemExit(f"Only {len(selected)} unique PDFs are available")

    reextract_indices: set[int] = set()
    if args.reextract_indices:
        try:
            reextract_indices = {
                int(value.strip())
                for value in args.reextract_indices.split(",")
                if value.strip()
            }
        except ValueError as exc:
            raise SystemExit("--reextract-indices must contain integers") from exc
        invalid = sorted(index for index in reextract_indices if not 1 <= index <= args.count)
        if invalid:
            raise SystemExit(
                "--reextract-indices outside sample range: "
                + ", ".join(map(str, invalid))
            )

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    reusable: dict[int, dict[str, Any]] = {}
    previous_bundle_path = output / "review_bundle.json"
    if previous_bundle_path.is_file() and not args.force_reextract:
        try:
            previous_bundle = json.loads(previous_bundle_path.read_text(encoding="utf-8"))
            previous_items = previous_bundle.get("items") or []
            planned_names = [path.relative_to(folder).as_posix() for _, path in selected]
            previous_names = [str(item.get("filename") or "") for item in previous_items]
            if (
                (bool(args.sample_file) or int(previous_bundle.get("seed")) == args.seed)
                and int(previous_bundle.get("count")) == args.count
                and previous_names == planned_names
            ):
                reusable = {
                    int(item["index"]): item
                    for item in previous_items
                    if not item.get("error") and int(item["index"]) not in reextract_indices
                }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            reusable = {}
    bundle = []
    with httpx.Client(timeout=240) as client:
        for index, (digest, path) in enumerate(selected, 1):
            previous = reusable.get(index)
            if (
                previous
                and previous.get("sha256") == digest
                and previous.get("filename") == path.relative_to(folder).as_posix()
            ):
                bundle.append(previous)
                print(
                    f"[{index}/{args.count}] {path.name}: REUSED ({previous.get('seconds', 0):.2f}s)",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            started = time.perf_counter()
            try:
                with path.open("rb") as handle:
                    response = client.post(args.api, files={"file": (path.name, handle, "application/pdf")})
                response.raise_for_status()
                payload = response.json()
                text = _source_text(payload)
                model = _extract_model(payload)
                source_vat = _vat_source_candidates(text)
                item = {
                    "index": index, "filename": path.relative_to(folder).as_posix(), "sha256": digest,
                    "pages": (payload.get("document") or {}).get("page_count"),
                    "seconds": round(time.perf_counter() - started, 4), "model": model,
                    "automatic_checks": _automatic_checks(model, text, source_vat),
                    "source_vat_candidates": source_vat,
                    "source_contexts": {
                        "order": _contexts(text, [ORDER_LABEL_RE]),
                        "number": _contexts(text, [re.compile(re.escape(str(model.get('order_number'))), re.I)])
                                  if model.get("order_number") else [],
                        "delivery": _contexts(text, [DELIVERY_LABEL_RE]),
                        "date": _contexts(text, [DATE_LABEL_RE]),
                        "agency": _contexts(text, [AGENCY_LABEL_RE]),
                        "contact": _contexts(text, [CONTACT_LABEL_RE]),
                        "vat": _contexts(text, [VAT_LABEL_RE]),
                    },
                    "source_text": text,
                }
            except Exception as exc:
                item = {"index": index, "filename": path.relative_to(folder).as_posix(),
                        "sha256": digest, "error": type(exc).__name__,
                        "seconds": round(time.perf_counter() - started, 4)}
            bundle.append(item)
            print(f"[{index}/{args.count}] {path.name}: {item.get('error', 'OK')} ({item['seconds']:.2f}s)",
                  file=sys.stderr, flush=True)

    (output / "review_bundle.json").write_text(
        json.dumps({"seed": args.seed, "count": args.count, "items": bundle}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "sample_files.json").write_text(
        json.dumps([item["filename"] for item in bundle], ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    rows = [_csv_row(item) for item in bundle if "error" not in item]
    if rows:
        with (output / "audit_draft.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    errors = [item for item in bundle if "error" in item]
    print(json.dumps({"selected": len(bundle), "succeeded": len(rows), "errors": len(errors),
                      "seed": args.seed, "excluded_sha256": len(excluded_hashes),
                      "output": str(output)}, ensure_ascii=False))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())

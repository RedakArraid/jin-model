"""Canonical, human-reviewed annotation contract used by JIN Studio."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping


STUDIO_ANNOTATION_SCHEMA_VERSION = 2
ANNOTATION_STATUSES = frozenset(
    {"UNREVIEWED", "CONFIRMED", "CORRECTED", "ABSENT", "AMBIGUOUS"}
)
DOCUMENT_STATUSES = frozenset({"DRAFT", "HUMAN_VALIDATED"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


FIELD_CATALOG: tuple[dict[str, str], ...] = (
    {"label": "DOCUMENT_TYPE", "name": "Type de document"},
    {"label": "ORDER_NUMBER", "name": "Numéro de commande"},
    {"label": "ORDER_DATE", "name": "Date de commande"},
    {"label": "CUSTOMER_REFERENCE", "name": "Référence client"},
    {"label": "CUSTOMER_CODE", "name": "Code client"},
    {"label": "DELIVERY_DATE", "name": "Date de livraison souhaitée"},
    {"label": "ADDRESS_BUILDING", "name": "Bâtiment"},
    {"label": "ADDRESS_RESIDENCE", "name": "Résidence"},
    {"label": "ADDRESS_ENTRY", "name": "Entrée"},
    {"label": "ADDRESS_FLOOR", "name": "Étage"},
    {"label": "ADDRESS_UNIT", "name": "Appartement / unité"},
    {"label": "ADDRESS_HOUSE_NUMBER", "name": "Numéro de rue"},
    {"label": "ADDRESS_HOUSE_NUMBER_SUFFIX", "name": "Suffixe du numéro"},
    {"label": "ADDRESS_STREET_TYPE", "name": "Type de voie"},
    {"label": "ADDRESS_STREET_NAME", "name": "Nom de voie"},
    {"label": "ADDRESS_INDUSTRIAL_ZONE", "name": "Zone industrielle"},
    {"label": "ADDRESS_ACTIVITY_PARK", "name": "Zone d'activité"},
    {"label": "ADDRESS_PLACE_NAME", "name": "Lieu-dit"},
    {"label": "ADDRESS_POSTAL_BOX", "name": "Boîte postale"},
    {"label": "ADDRESS_TSA", "name": "TSA"},
    {"label": "ADDRESS_POSTAL_ROUTING_CODE", "name": "Code d'acheminement"},
    {"label": "ADDRESS_CS", "name": "CS"},
    {"label": "ADDRESS_POSTAL_CODE", "name": "Code postal"},
    {"label": "ADDRESS_CITY", "name": "Ville"},
    {"label": "ADDRESS_CEDEX_NUMBER", "name": "CEDEX"},
    {"label": "ADDRESS_INSEE_CODE", "name": "Code INSEE"},
    {"label": "ADDRESS_COUNTRY", "name": "Pays (libellé)"},
    {"label": "ADDRESS_COUNTRY_CODE", "name": "Pays"},
    {"label": "LINE_ITEM_NUMBER", "name": "Numéro de ligne"},
    {"label": "LINE_ITEM_REFERENCE", "name": "Référence article"},
    {"label": "LINE_ITEM_DESCRIPTION", "name": "Description article"},
    {"label": "LINE_ITEM_QUANTITY", "name": "Quantité"},
    {"label": "LINE_ITEM_UNIT", "name": "Unité"},
    {"label": "LINE_ITEM_UNIT_PRICE", "name": "Prix unitaire"},
    {"label": "LINE_ITEM_TOTAL", "name": "Montant de ligne"},
    {"label": "LINE_ITEM_DELIVERY_DATE", "name": "Date de livraison de ligne"},
    {"label": "ADDITIONAL_CHARGE_LABEL", "name": "Libellé de frais"},
    {"label": "ADDITIONAL_CHARGE_AMOUNT", "name": "Montant de frais"},
    {"label": "TOTAL_SUBTOTAL", "name": "Sous-total produits"},
    {"label": "TOTAL_NET", "name": "Total HT"},
    {"label": "TOTAL_VAT", "name": "TVA"},
    {"label": "TOTAL_GROSS", "name": "Total TTC"},
    {"label": "TOTAL_AMOUNT_DUE", "name": "Montant à payer"},
    {"label": "VAT_NUMBER", "name": "Numéro de TVA"},
    {"label": "SIREN", "name": "Numéro SIREN"},
    {"label": "SIRET", "name": "Numéro SIRET"},
    {"label": "OFFER_NUMBER", "name": "Numéro d'offre"},
    {"label": "DEROGATION_NUMBER", "name": "Numéro de dérogation"},
    *tuple(
        {"label": f"{role}_{suffix}", "name": f"{name} — {component}"}
        for role, name in (
            ("BUYER", "Acheteur"),
            ("SUPPLIER", "Fournisseur"),
            ("SHIP_TO", "Livraison"),
            ("BILL_TO", "Facturation"),
            ("PAYER", "Payeur"),
            ("SHIP_FROM", "Expéditeur"),
        )
        for suffix, component in (
            ("NAME", "nom"),
            ("CODE", "code"),
            ("VAT_NUMBER", "TVA"),
            ("SIREN", "SIREN"),
            ("SIRET", "SIRET"),
            ("CONTACT_PHONE", "téléphone"),
            ("CONTACT_FAX", "fax"),
            ("CONTACT_EMAIL", "e-mail"),
            ("BUILDING", "bâtiment"),
            ("RESIDENCE", "résidence"),
            ("ENTRY", "entrée"),
            ("FLOOR", "étage"),
            ("UNIT", "appartement / unité"),
            ("HOUSE_NUMBER", "numéro de voie"),
            ("HOUSE_NUMBER_SUFFIX", "suffixe du numéro"),
            ("STREET_TYPE", "type de voie"),
            ("STREET_NAME", "nom de voie"),
            ("STREET", "voie complète"),
            ("INDUSTRIAL_ZONE", "zone industrielle"),
            ("ACTIVITY_PARK", "zone d'activité"),
            ("PLACE_NAME", "lieu-dit"),
            ("POSTAL_BOX", "boîte postale"),
            ("TSA", "TSA"),
            ("CS", "CS"),
            ("POSTAL_ROUTING_CODE", "code d'acheminement"),
            ("POSTAL_CODE", "code postal"),
            ("CITY", "ville"),
            ("CEDEX_NUMBER", "CEDEX"),
            ("COUNTRY", "pays"),
            ("COUNTRY_CODE", "code pays"),
        )
    ),
)
FIELD_LABELS = frozenset(item["label"] for item in FIELD_CATALOG)


class StudioContractError(ValueError):
    """Raised when an annotation cannot be trusted as human ground truth."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _json_value(value: Any, *, field: str) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise StudioContractError(f"{field} must be valid finite JSON") from exc
    return copy.deepcopy(value)


def _bbox(value: Any) -> list[float] | None:
    if value in (None, "", []):
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise StudioContractError("evidence.bbox must contain four coordinates")
    try:
        box = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise StudioContractError("evidence.bbox coordinates must be numbers") from exc
    if not all(0 <= item <= 1000 for item in box):
        raise StudioContractError("evidence.bbox must use normalized 0..1000 coordinates")
    if box[2] <= box[0] or box[3] <= box[1]:
        raise StudioContractError("evidence.bbox must have a positive area")
    return box


def _normalize_field(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise StudioContractError(f"field_annotations[{index}] must be an object")
    path = _text(item.get("path"))
    label = _text(item.get("label")).upper()
    status = _text(item.get("status") or "UNREVIEWED").upper()
    if not path or len(path) > 300:
        raise StudioContractError(f"field_annotations[{index}].path is required")
    if label not in FIELD_LABELS:
        raise StudioContractError(f"field_annotations[{index}].label is not supported")
    if status not in ANNOTATION_STATUSES:
        raise StudioContractError(f"field_annotations[{index}].status is invalid")

    extracted = _json_value(item.get("extracted_value"), field="extracted_value")
    corrected = _json_value(item.get("corrected_value"), field="corrected_value")
    if status == "CONFIRMED" and extracted in (None, ""):
        raise StudioContractError(
            f"field_annotations[{index}].extracted_value is required when confirmed"
        )
    if status == "CORRECTED" and corrected in (None, ""):
        raise StudioContractError(
            f"field_annotations[{index}].corrected_value is required when corrected"
        )

    evidence_raw = item.get("evidence")
    evidence_raw = evidence_raw if isinstance(evidence_raw, Mapping) else {}
    box = _bbox(evidence_raw.get("bbox"))
    page_raw = evidence_raw.get("page")
    page = None
    if page_raw not in (None, ""):
        try:
            page = int(page_raw)
        except (TypeError, ValueError) as exc:
            raise StudioContractError("evidence.page must be an integer") from exc
        if page < 1:
            raise StudioContractError("evidence.page must be greater than zero")
    if box is not None and page is None:
        raise StudioContractError("evidence.page is required with evidence.bbox")

    normalized: dict[str, Any] = {
        "path": path,
        "label": label,
        "status": status,
        "extracted_value": extracted,
        "corrected_value": corrected,
    }
    if page is not None or box is not None:
        normalized["evidence"] = {"page": page, "bbox": box}
    comment = _text(item.get("comment"))
    if comment:
        normalized["comment"] = comment[:1000]
    return normalized


def annotation_digest(payload: Mapping[str, Any]) -> str:
    canonical = copy.deepcopy(dict(payload))
    canonical.pop("annotation_digest", None)
    canonical.pop("saved_at", None)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def normalize_studio_annotation(
    payload: Any,
    *,
    document_id: str,
    document_sha256: str,
    revision: int,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise StudioContractError("annotation must be a JSON object")
    status = _text(payload.get("status") or "DRAFT").upper()
    if status not in DOCUMENT_STATUSES:
        raise StudioContractError("status must be DRAFT or HUMAN_VALIDATED")
    reviewer = _text(payload.get("reviewed_by"))
    fields_raw = payload.get("field_annotations")
    if not isinstance(fields_raw, list) or not fields_raw:
        raise StudioContractError("field_annotations must contain at least one field")
    fields = [_normalize_field(item, index) for index, item in enumerate(fields_raw)]
    paths = [item["path"] for item in fields]
    if len(paths) != len(set(paths)):
        raise StudioContractError("field annotation paths must be unique")
    if status == "HUMAN_VALIDATED":
        if not reviewer:
            raise StudioContractError("reviewed_by is required for human validation")
        if any(item["status"] == "UNREVIEWED" for item in fields):
            raise StudioContractError("all fields must be reviewed before validation")

    normalized_id = _text(document_id)
    normalized_sha = _text(document_sha256).lower()
    if not _SAFE_ID.fullmatch(normalized_id):
        raise StudioContractError("document_id is invalid")
    if not _SHA256.fullmatch(normalized_sha):
        raise StudioContractError("document_sha256 is invalid")
    normalized: dict[str, Any] = {
        "schema_version": STUDIO_ANNOTATION_SCHEMA_VERSION,
        "annotation_id": f"ann-{normalized_id}-r{revision}",
        "document_id": normalized_id,
        "document_sha256": normalized_sha,
        "revision": int(revision),
        "status": status,
        "reviewed_by": reviewer,
        "reviewed_at": utc_now() if status == "HUMAN_VALIDATED" else None,
        "field_annotations": fields,
    }
    note = _text(payload.get("note"))
    if note:
        normalized["note"] = note[:4000]
    normalized["annotation_digest"] = annotation_digest(normalized)
    return normalized


__all__ = [
    "ANNOTATION_STATUSES",
    "DOCUMENT_STATUSES",
    "FIELD_CATALOG",
    "FIELD_LABELS",
    "STUDIO_ANNOTATION_SCHEMA_VERSION",
    "StudioContractError",
    "annotation_digest",
    "normalize_studio_annotation",
    "utc_now",
]

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


_DEFAULT_FIELD_PATHS = {
    "DOCUMENT_TYPE": "document.type",
    "ORDER_NUMBER": "order.customer_order_number.value",
    "ORDER_DATE": "order.order_date.value",
    "CUSTOMER_REFERENCE": "order.customer_reference.value",
    "CUSTOMER_CODE": "order.customer_agency_code.value",
    "DELIVERY_DATE": "order.requested_delivery_date.value",
    "LINE_ITEM_NUMBER": "order.line_items[0].line_number",
    "LINE_ITEM_REFERENCE": "order.line_items[0].references.article_number",
    "LINE_ITEM_DESCRIPTION": "order.line_items[0].description",
    "LINE_ITEM_QUANTITY": "order.line_items[0].quantity",
    "LINE_ITEM_UNIT": "order.line_items[0].unit",
    "LINE_ITEM_UNIT_PRICE": "order.line_items[0].pricing.net_unit_price",
    "LINE_ITEM_TOTAL": "order.line_items[0].amounts.total",
    "LINE_ITEM_DELIVERY_DATE": "order.line_items[0].delivery.date",
    "ADDITIONAL_CHARGE_LABEL": "order.additional_charges[0].description",
    "ADDITIONAL_CHARGE_AMOUNT": "order.additional_charges[0].amount",
    "TOTAL_SUBTOTAL": "order.totals.subtotal",
    "TOTAL_NET": "order.totals.total_net",
    "TOTAL_VAT": "order.totals.total_vat",
    "TOTAL_GROSS": "order.totals.total_gross",
    "TOTAL_AMOUNT_DUE": "order.totals.amount_due",
    "VAT_NUMBER": "document.tax_identifiers[0].vat_number",
    "SIREN": "document.tax_identifiers[0].siren",
    "SIRET": "document.tax_identifiers[0].siret",
    "OFFER_NUMBER": "order.quote_number.value",
    "DEROGATION_NUMBER": "order.derogation_number.value",
}


def _canonical_field_path(label: str) -> str:
    if label in _DEFAULT_FIELD_PATHS:
        return _DEFAULT_FIELD_PATHS[label]
    if label.startswith("ADDRESS_"):
        return f"order.addresses[0].components.{label.removeprefix('ADDRESS_').lower()}"
    for role in ("BUYER", "SUPPLIER", "SHIP_TO", "BILL_TO", "PAYER", "SHIP_FROM"):
        prefix = f"{role}_"
        if not label.startswith(prefix):
            continue
        component = label.removeprefix(prefix)
        party = role.lower()
        if component.startswith("CONTACT_"):
            return f"order.parties.{party}.contact.{component.removeprefix('CONTACT_').lower()}"
        if component in {"NAME", "CODE", "VAT_NUMBER", "SIREN", "SIRET"}:
            return f"order.parties.{party}.{component.lower()}"
        return f"order.parties.{party}.address.{component.lower()}"
    return f"manual.{label.lower()}"


def _field(
    label: str,
    name: str,
    help_text: str,
    example_text: str,
) -> dict[str, str]:
    """Build a catalog entry with mandatory annotation guidance."""
    return {
        "label": label,
        "name": name,
        "help": help_text,
        "example": example_text,
        "defaultPath": _canonical_field_path(label),
    }


_ADDRESS_COMPONENTS: tuple[tuple[str, str, str], ...] = (
    ("BUILDING", "bâtiment", "le nom ou numéro de bâtiment, sans la rue"),
    ("RESIDENCE", "résidence", "le nom de résidence, sans le destinataire"),
    ("ENTRY", "entrée", "l'identifiant d'entrée, par exemple « Entrée B »"),
    ("FLOOR", "étage", "l'étage imprimé, par exemple « 3e étage »"),
    ("UNIT", "appartement / unité", "le numéro d'appartement, de lot ou d'unité"),
    ("HOUSE_NUMBER", "numéro de voie", "le numéro placé devant la voie, sans son suffixe"),
    ("HOUSE_NUMBER_SUFFIX", "suffixe du numéro", "le suffixe du numéro, par exemple BIS, TER ou A"),
    ("STREET_TYPE", "type de voie", "le type de voie seul, par exemple RUE, AVENUE ou BD"),
    ("STREET_NAME", "nom de voie", "le nom de la voie sans numéro ni type de voie"),
    ("STREET", "voie complète", "la ligne de voie complète, sans code postal ni ville"),
    ("INDUSTRIAL_ZONE", "zone industrielle", "le libellé complet de zone industrielle, par exemple « Z.I. des Paluds »"),
    ("ACTIVITY_PARK", "zone d'activité", "le libellé complet de zone d'activité, par exemple « Z.A. Henri Spriet »"),
    ("PLACE_NAME", "lieu-dit", "le nom du lieu-dit, sans code postal ni ville"),
    ("POSTAL_BOX", "boîte postale", "la boîte postale complète, par exemple « BP 123 »"),
    ("TSA", "TSA", "le libellé TSA et son numéro"),
    ("CS", "CS", "le libellé CS et son numéro"),
    ("POSTAL_ROUTING_CODE", "code d'acheminement", "le code d'acheminement distinct du code postal"),
    ("POSTAL_CODE", "code postal", "le code postal seul, généralement cinq chiffres en France"),
    ("CITY", "ville", "le nom de ville seul, CEDEX exclu"),
    ("CEDEX_NUMBER", "CEDEX", "la mention CEDEX et son éventuel numéro"),
    ("INSEE_CODE", "code INSEE", "le code INSEE explicitement imprimé"),
    ("COUNTRY", "pays", "le nom de pays imprimé en toutes lettres"),
    ("COUNTRY_CODE", "code pays", "le code pays explicitement imprimé, sans le déduire du pays"),
)

_ADDRESS_EXAMPLES: dict[str, str] = {
    "BUILDING": "Dans « Bâtiment C », encadrez « Bâtiment C ».",
    "RESIDENCE": "Dans « Résidence Les Tilleuls », encadrez « Résidence Les Tilleuls ».",
    "ENTRY": "Dans « Entrée B », encadrez « Entrée B ».",
    "FLOOR": "Dans « 3e étage », encadrez « 3e étage ».",
    "UNIT": "Dans « Appartement 24 », encadrez « 24 ».",
    "HOUSE_NUMBER": "Dans « 12 BIS RUE ISAAC NEWTON », encadrez « 12 ».",
    "HOUSE_NUMBER_SUFFIX": "Dans « 12 BIS RUE ISAAC NEWTON », encadrez « BIS ».",
    "STREET_TYPE": "Dans « 12 RUE ISAAC NEWTON », encadrez « RUE ».",
    "STREET_NAME": "Dans « 12 RUE ISAAC NEWTON », encadrez « ISAAC NEWTON ».",
    "STREET": "Dans « 12 BIS RUE ISAAC NEWTON », encadrez toute cette ligne de voie.",
    "INDUSTRIAL_ZONE": "Dans « Z.I. DES PALUDS », encadrez « Z.I. DES PALUDS ».",
    "ACTIVITY_PARK": "Dans « Z.A. HENRI SPRIET », encadrez « Z.A. HENRI SPRIET ».",
    "PLACE_NAME": "Dans « Lieu-dit La Grande Plaine », encadrez « La Grande Plaine ».",
    "POSTAL_BOX": "Dans « BP 123 », encadrez « BP 123 ».",
    "TSA": "Dans « TSA 70001 », encadrez « TSA 70001 ».",
    "CS": "Dans « CS 40215 », encadrez « CS 40215 ».",
    "POSTAL_ROUTING_CODE": "Dans « 00013 14120 MONDEVILLE », encadrez « 00013 ».",
    "POSTAL_CODE": "Dans « 24102 BERGERAC CEDEX », encadrez « 24102 ».",
    "CITY": "Dans « 24102 BERGERAC CEDEX », encadrez « BERGERAC ».",
    "CEDEX_NUMBER": "Dans « 24102 BERGERAC CEDEX 2 », encadrez « CEDEX 2 ».",
    "INSEE_CODE": "Dans « Code INSEE : 33063 », encadrez « 33063 ».",
    "COUNTRY": "Dans « FRANCE », encadrez « FRANCE ».",
    "COUNTRY_CODE": "Dans « Pays : FR », encadrez « FR ».",
}

_GENERIC_ADDRESS_FIELDS = tuple(
    _field(
        f"ADDRESS_{suffix}",
        component[0].upper() + component[1:],
        f"Encadrez uniquement {instruction}. Ne sélectionnez pas toute l'adresse.",
        _ADDRESS_EXAMPLES[suffix],
    )
    for suffix, component, instruction in _ADDRESS_COMPONENTS
    if suffix != "STREET"
)

_ROLE_IDENTITIES: tuple[tuple[str, str, str], ...] = (
    ("BUYER", "Acheteur", "l'acheteur / client qui passe la commande"),
    ("SUPPLIER", "Fournisseur", "le fournisseur qui reçoit la commande"),
    ("SHIP_TO", "Livraison", "le destinataire de la livraison"),
    ("BILL_TO", "Facturation", "le destinataire de la facture"),
    ("PAYER", "Payeur", "le payeur"),
    ("SHIP_FROM", "Expéditeur", "l'expéditeur"),
)

_ROLE_CORE_COMPONENTS: tuple[tuple[str, str, str], ...] = (
    ("NAME", "nom", "le nom commercial ou la raison sociale, sans l'adresse ni le libellé du rôle"),
    ("CODE", "code", "le code interne explicitement associé à cette partie, sans numéro de commande ni référence article"),
    ("VAT_NUMBER", "TVA", "le numéro de TVA imprimé, préfixe pays compris"),
    ("SIREN", "SIREN", "le SIREN imprimé de neuf chiffres, sans le calculer"),
    ("SIRET", "SIRET", "le SIRET imprimé de quatorze chiffres, sans le calculer"),
    ("CONTACT_PHONE", "téléphone", "le numéro de téléphone imprimé, sans son libellé"),
    ("CONTACT_FAX", "fax", "le numéro de fax imprimé, sans son libellé"),
    ("CONTACT_EMAIL", "e-mail", "l'adresse e-mail complète, sans le texte voisin"),
)

_ROLE_CORE_EXAMPLES: dict[str, str] = {
    "NAME": "Dans « Raison sociale : SOCIÉTÉ ALPHA », encadrez « SOCIÉTÉ ALPHA ».",
    "CODE": "Dans « Code tiers : C004281 », encadrez « C004281 ».",
    "VAT_NUMBER": "Dans « TVA : FR40 123 456 789 », encadrez « FR40 123 456 789 ».",
    "SIREN": "Dans « SIREN : 123 456 789 », encadrez « 123 456 789 ».",
    "SIRET": "Dans « SIRET : 123 456 789 00012 », encadrez « 123 456 789 00012 ».",
    "CONTACT_PHONE": "Dans « Tél. : 01 23 45 67 89 », encadrez « 01 23 45 67 89 ».",
    "CONTACT_FAX": "Dans « Fax : 01 23 45 67 80 », encadrez « 01 23 45 67 80 ».",
    "CONTACT_EMAIL": "Dans « E-mail : achats@exemple.fr », encadrez « achats@exemple.fr ».",
}

FIELD_CATALOG: tuple[dict[str, str], ...] = (
    _field(
        "DOCUMENT_TYPE",
        "Type de document",
        "Encadrez le titre qui prouve explicitement le type, par exemple « BON DE COMMANDE » ou « COMMANDE ». Ne sélectionnez ni toute la page, ni un logo, ni une adresse ; choisissez ensuite la valeur normalisée dans la liste.",
        "Dans « BON DE COMMANDE FOURNISSEUR », encadrez ce titre complet puis choisissez « purchase_order ».",
    ),
    _field("ORDER_NUMBER", "Numéro de commande", "Encadrez uniquement la valeur qui identifie la commande du client, sans le libellé « commande », une date ou un code client.", "Dans « N° commande : BC-2026-00418 », encadrez « BC-2026-00418 »."),
    _field("ORDER_DATE", "Date de commande", "Encadrez uniquement la date d'émission de la commande, pas une date de livraison, d'impression ou de validité.", "Dans « Date de commande : 24/10/2026 », encadrez « 24/10/2026 »."),
    _field("CUSTOMER_REFERENCE", "Référence client", "Encadrez uniquement la référence de dossier ou de chantier donnée par le client, pas le numéro principal de commande.", "Dans « Réf. chantier : CAC2410TOU00034 », encadrez « CAC2410TOU00034 »."),
    _field("CUSTOMER_CODE", "Code client", "Encadrez uniquement le code interne du client qui passe la commande, pas un numéro de commande, de fournisseur ou d'article.", "Dans « Code client : 17532053 », encadrez « 17532053 »."),
    _field("DELIVERY_DATE", "Date de livraison souhaitée", "Encadrez uniquement la date de livraison demandée au niveau de la commande ; ignorez les dates propres à une ligne article.", "Dans « Livraison souhaitée : 05/11/2026 », encadrez « 05/11/2026 »."),
    *_GENERIC_ADDRESS_FIELDS,
    _field("LINE_ITEM_NUMBER", "Numéro de ligne", "Encadrez uniquement le numéro ordinal de la ligne d'article.", "Dans « 10 | 8738717111 | BLOC GAZ », encadrez seulement « 10 »."),
    _field("LINE_ITEM_REFERENCE", "Référence article", "Encadrez uniquement le code article de cette ligne, jamais la description complète ni toute la ligne.", "Dans « 8738717111 BLOC GAZ 2 PCE », encadrez seulement « 8738717111 »."),
    _field("LINE_ITEM_DESCRIPTION", "Description article", "Encadrez uniquement la désignation de l'article ; excluez référence, quantité, prix et montant.", "Dans « 8738717111 BLOC GAZ 2 PCE », encadrez seulement « BLOC GAZ »."),
    _field("LINE_ITEM_QUANTITY", "Quantité", "Encadrez uniquement la quantité commandée de cette ligne, sans l'unité.", "Dans « Qté : 2 PCE », encadrez seulement « 2 »."),
    _field("LINE_ITEM_UNIT", "Unité", "Encadrez uniquement l'unité de vente de cette ligne, par exemple PCE, U, M ou LOT.", "Dans « Qté : 2 PCE », encadrez seulement « PCE »."),
    _field("LINE_ITEM_UNIT_PRICE", "Prix unitaire", "Encadrez uniquement le prix unitaire de cette ligne, hors quantité et montant total.", "Dans « PU 15,63 € | Total 31,26 € », encadrez « 15,63 »."),
    _field("LINE_ITEM_TOTAL", "Montant de ligne", "Encadrez uniquement le montant total de cette ligne, pas le prix unitaire ni le total du document.", "Dans « PU 15,63 € | Total 31,26 € », encadrez « 31,26 »."),
    _field("LINE_ITEM_DELIVERY_DATE", "Date de livraison de ligne", "Encadrez uniquement la date de livraison associée à cette ligne article.", "Dans la ligne article « Livraison 05/11/2026 », encadrez « 05/11/2026 »."),
    _field("ADDITIONAL_CHARGE_LABEL", "Libellé de frais", "Encadrez uniquement le libellé d'un frais hors produit, par exemple PORT, ÉCO-CONTRIBUTION ou EMBALLAGE.", "Dans « PORT 18,00 € », encadrez seulement « PORT »."),
    _field("ADDITIONAL_CHARGE_AMOUNT", "Montant de frais", "Encadrez uniquement le montant correspondant au frais hors produit.", "Dans « PORT 18,00 € », encadrez seulement « 18,00 »."),
    _field("TOTAL_SUBTOTAL", "Sous-total produits", "Encadrez uniquement le montant du sous-total des produits avant taxes et frais.", "Dans « Sous-total produits : 659,06 € », encadrez « 659,06 »."),
    _field("TOTAL_NET", "Total HT", "Encadrez uniquement le montant total hors taxes explicitement imprimé.", "Dans « Total HT : 791,15 € », encadrez « 791,15 »."),
    _field("TOTAL_VAT", "TVA", "Encadrez uniquement le montant de TVA, pas un numéro de TVA intracommunautaire.", "Dans « TVA 20 % : 158,23 € », encadrez « 158,23 »."),
    _field("TOTAL_GROSS", "Total TTC", "Encadrez uniquement le montant total toutes taxes comprises.", "Dans « Total TTC : 949,38 € », encadrez « 949,38 »."),
    _field("TOTAL_AMOUNT_DUE", "Montant à payer", "Encadrez uniquement le montant final à payer explicitement imprimé.", "Dans « Net à payer : 949,38 € », encadrez « 949,38 »."),
    _field("VAT_NUMBER", "Numéro de TVA", "Encadrez uniquement le numéro de TVA intracommunautaire imprimé, préfixe pays compris ; n'encadrez pas un montant de taxe.", "Dans « TVA intracommunautaire : FR40 123 456 789 », encadrez « FR40 123 456 789 »."),
    _field("SIREN", "Numéro SIREN", "Encadrez uniquement le SIREN imprimé de neuf chiffres ; ne l'inventez pas à partir d'un autre identifiant.", "Dans « SIREN : 123 456 789 », encadrez « 123 456 789 »."),
    _field("SIRET", "Numéro SIRET", "Encadrez uniquement le SIRET imprimé de quatorze chiffres ; ne l'inventez pas à partir d'un autre identifiant.", "Dans « SIRET : 123 456 789 00012 », encadrez « 123 456 789 00012 »."),
    _field("OFFER_NUMBER", "Numéro d'offre", "Encadrez uniquement l'identifiant qui suit un libellé d'offre, de devis ou de proposition commerciale.", "Dans « Suivant offre N° OF-2026-1842 », encadrez « OF-2026-1842 »."),
    _field("DEROGATION_NUMBER", "Numéro de dérogation", "Encadrez uniquement l'identifiant explicitement présenté comme dérogation, accord spécial ou référence de dérogation.", "Dans « Dérogation : DER-45821 », encadrez « DER-45821 »."),
    *tuple(
        _field(
            f"{role}_{suffix}",
            f"{role_name} — {component}",
            f"Encadrez uniquement {instruction}, pour {role_description}. Ne mélangez pas les blocs d'autres parties.",
            _ROLE_CORE_EXAMPLES.get(suffix, _ADDRESS_EXAMPLES.get(suffix, "Encadrez uniquement la valeur imprimée.")),
        )
        for role, role_name, role_description in _ROLE_IDENTITIES
        for suffix, component, instruction in (*_ROLE_CORE_COMPONENTS, *_ADDRESS_COMPONENTS)
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

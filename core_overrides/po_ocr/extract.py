from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
import statistics
from typing import Iterable

from .models import (
    PageResult, WordToken, ExtractedField, Evidence, Party, Address, LineItem,
    Totals, ExtraField, CommercialTerms, Logistics, TaxSummary
)
from .normalize import clean_text, normalize_currency, normalize_unit, parse_date, parse_number, ascii_fold
from .ocr import group_words_into_rows


FIELD_PATTERNS = {
    "number": [
        r"purchase\s+order\s*[:#-]?\s*((?:PO[-_])?[A-Z0-9][A-Z0-9._/\-]{3,})",
        r"(?:p\.?o\.?|po)\s*(?:number|no\.?|n[°º])\b\s*(?:[:#-]\s*|\s+)([A-Z0-9][A-Z0-9._/\-]{3,})",
        r"(?:purchase\s*(?:order\s*)?(?:number|no\.?|#)|bon\s+de\s+commande(?:\s+n[o°.]*)?|n[°o]\s+du\s+bon\s+de\s+commande|bestellnummer|bestellung\s*(?:nr\.?|nummer)?|n[uú]mero\s+de\s+pedido|pedido\s*(?:n[o°.]*)?)\s*[:#-]?\s*([A-Z0-9][A-Z0-9._/\-]{3,})",
        r"(?:order\s*(?:no|number)|n[o°.]\s*commande)\s*[:#-]?\s*([A-Z0-9][A-Z0-9._/\-]{3,})",
    ],
    "order_date": [
        r"(?:order\s*date|date\s*(?:de\s*)?commande|bestelldatum|fecha\s*(?:del\s*)?pedido)\s*[:\-]?\s*([^\n]{6,30})",
    ],
    "customer_reference": [
        r"(?:customer\s*(?:ref(?:erence)?|reference)|reference\s*client|référence\s*client|kundenreferenz)\s*[:#-]?\s*([^\n]{2,80})",
    ],
    "vendor_reference": [
        r"(?:vendor\s*(?:ref(?:erence)?|reference)|supplier\s*(?:ref(?:erence)?|reference)|référence\s*fournisseur)\s*[:#-]?\s*([^\n]{2,80})",
    ],
    "contract_number": [
        r"(?:contract|contrat|vertrag)\s*(?:n[o°.]*)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9._/\-]{2,})",
    ],
    "quote_number": [
        r"(?:quote|quotation|devis|angebot)\s*(?:n[o°.]*)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9._/\-]{2,})",
    ],
    "requisition_number": [
        r"(?:requisition|demande\s+d['’]?achat|banf)\s*(?:n[o°.]*)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9._/\-]{2,})",
    ],
    "expected_delivery_date": [
        r"(?:requested\s*delivery\s*date|delivery\s*date|required\s*date|date\s*(?:de\s*)?livraison|lieferdatum)\s*[:\-]?\s*([^\n]{6,30})",
    ],
}

PARTY_LABELS = {
    "buyer": ["buyer", "customer", "client", "acheteur", "ordering party", "donneur d'ordre", "auftraggeber"],
    "supplier": ["supplier", "vendor", "fournisseur", "vendeur", "lieferant"],
    "ship_to": ["ship to", "adresse de livraison", "livrer à", "livrer a", "warenempfänger"],
    "bill_to": ["bill to", "adresse de facturation", "facturer à", "facturer a", "rechnung an"],
    "sold_to": ["sold to", "sold-to", "client facturé", "client commandeur"],
    "deliver_to": ["deliver to", "delivery address", "adresse destinataire", "destinataire livraison"],
    "invoice_to": ["invoice to", "invoice address", "destinataire facture", "adresse facture"],
    "payer": ["payer", "payeur", "payment by", "zahlender"],
    "end_customer": ["end customer", "final customer", "client final", "endkunde"],
    "consignee": ["consignee", "consignataire", "réceptionnaire", "receptionnaire"],
    "ship_from": ["ship from", "expédié de", "expedie de", "site expéditeur", "site expediteur", "versand von"],
}

HEADER_ALIASES = {
    "line": {"line", "pos", "position", "item", "poste", "ligne"},
    "material": {"material", "article", "itemno", "item-no", "part", "reference", "référence", "ref", "sku", "code", "product"},
    "description": {"description", "designation", "désignation", "libelle", "libellé", "desc", "bezeichnung"},
    "product_family": {"family", "famille", "productfamily", "product-family", "gamme", "range"},
    "quantity": {"qty", "quantity", "quantite", "quantité", "menge"},
    "uom": {"uom", "unit", "unite", "unité", "me", "einheit"},
    "unit_price": {"price", "prix", "unitprice", "unit-price", "einzelpreis", "preis", "pu", "p.u"},
    "total": {"total", "amount", "montant", "value", "betrag", "extended"},
    "date": {"date", "delivery", "livraison", "lieferdatum"},
}

CUSTOMER_MATERIAL_QUALIFIERS = {
    "interne", "internal", "client", "customer", "acheteur", "buyer",
    "cust", "kunden",
}
SUPPLIER_MATERIAL_QUALIFIERS = {
    "fournisseur", "supplier", "vendor", "vendeur", "lieferant",
}

STOP_WORDS = {"subtotal", "sub total", "sous-total", "sous total", "net total", "total net", "grand total", "total", "vat", "tva", "tax", "taxe", "mwst", "shipping", "freight"}


def _is_line_table_stop(text: str) -> bool:
    low = ascii_fold(text).lower().strip()
    if any(low.startswith(value) for value in STOP_WORDS):
        return True
    # Summary labels can share their physical row with a left-hand note (for
    # example ``FRANCO ... Montant HT Net 95.48``), so they are not guaranteed
    # to appear at the beginning of the reconstructed text row.
    if re.search(r"\bmontant\s+(?:h\.?t\.?\s+)?net\b", low):
        return True
    if re.match(
        r"^(?:retrouvez\s+l['\u2019]?ensemble\s+de\s+nos\s+(?:activites|catalogues)|"
        r".*\bsiege\s+social\s*:|siren\s+\d|.*\bau\s+capital\s+de\b)",
        low,
    ):
        return True
    return bool(re.match(
        r"^(?:\(?\d+\)?\s+)?(?:a\s+livrer\b|adresse\s+de\s+livraison\b|"
        r"delivery\s+address\b|reglement\b|payment\b|poids\b|attention\b|"
        r"total\s+hors\b|montant\s+de\s+port\b|frais\s+de\s+(?:port|transport)\b|"
        r"(?:shipping|freight)\s+(?:charge|cost|fee)\b)",
        low,
    ))


@dataclass
class TextRow:
    page: int
    words: list[WordToken]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words).strip()

    @property
    def bbox(self):
        if not self.words:
            return None
        return (
            min(w.bbox[0] for w in self.words), min(w.bbox[1] for w in self.words),
            max(w.bbox[2] for w in self.words), max(w.bbox[3] for w in self.words),
        )

    @property
    def confidence(self) -> float:
        return statistics.fmean([w.confidence for w in self.words]) if self.words else 0.0


def all_rows(pages: list[PageResult], y_factor: float = 0.65) -> list[TextRow]:
    rows: list[TextRow] = []
    for page in pages:
        for ws in group_words_into_rows(page.words, y_factor=y_factor):
            rows.append(TextRow(page=page.page, words=ws))
    return rows


def compact_transaction_header_candidate(page: PageResult) -> dict | None:
    """Locate an unlabeled order series/number next to its date, never in products.

    This returns a candidate, not a trusted extraction. OCR candidates require a
    second reading of the original pixels before the business resolver uses them.
    """
    if page.page != 1:
        return None
    rows = all_rows([page], y_factor=0.50)
    title_rows = [row for row in rows if row.bbox and row.bbox[1] < page.height * 0.16]
    def is_order_title(row: TextRow) -> bool:
        match = re.search(
            r"\b(?:bon\s+de\s+commande|commande\s+fournisseur|purchase\s+order)\b",
            ascii_fold(row.text),
            flags=re.I,
        )
        return bool(match and match.group(0) == match.group(0).upper())

    if not any(is_order_title(row) for row in title_rows):
        return None
    candidates = []
    for row in rows:
        if not row.bbox or row.bbox[1] > page.height * 0.36:
            continue
        dates = [word for word in row.words if re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", word.text)
                 and parse_date(word.text)]
        for date in dates:
            left = [word for word in row.words if word.bbox[2] < date.bbox[0]]
            # In compact ERP headers the explicit DATE caption sits between the
            # order number and its date.  It is a column label, not part of the
            # identifier neighborhood (``N CF 0315792 DATE 01/03/24``).
            while left and ascii_fold(left[-1].text).lower().strip(" .:#-()[]") in {
                "date", "datum", "fecha",
            }:
                left.pop()
            if len(left) < 2 or len(left) > 4:
                continue
            prefix, number = left[-2:]
            if not re.fullmatch(r"[A-Za-z]{1,4}", prefix.text) or not re.fullmatch(r"\d{7,18}", number.text):
                continue
            if ascii_fold(prefix.text).lower() in {"tel", "fax", "tva", "vat", "iban", "bic", "ref", "siret"}:
                continue
            if any(re.search(r"\b(?:client|customer|fournisseur|supplier|compte|account|devis|quote|ref)\b",
                             ascii_fold(word.text).lower()) for word in left[:-2]):
                continue
            if not (0 <= number.bbox[0] - prefix.bbox[2] <= page.width * 0.06):
                continue
            if date.bbox[0] - number.bbox[2] > page.width * 0.25:
                continue
            words = [prefix, number]
            bbox = (min(word.bbox[0] for word in words), min(word.bbox[1] for word in words),
                    max(word.bbox[2] for word in words), max(word.bbox[3] for word in words))
            candidates.append({"value": prefix.text.upper() + number.text, "digits": number.text,
                               "raw_text": " ".join(word.text for word in words), "bbox": bbox,
                               "words": words, "row": row})
    return candidates[0] if len(candidates) == 1 else None


def verify_compact_header_number(page: PageResult, image, ocr_cfg: dict) -> None:
    """Confirm an OCR-only compact header against two small local image readings."""
    from .ocr import _tesseract_single
    candidate = compact_transaction_header_candidate(page)
    if not candidate or not any(word.source == "ocr" for word in candidate["words"]):
        return
    x0, y0, x1, y1 = candidate["bbox"]
    # A tight crop can clip the top horizontal stroke of ``F`` and turn the
    # printed ``CF`` series into ``CE``.  Keep roughly one character-height of
    # context on every side so the independent reads see complete glyphs.
    padding = max(12, (y1 - y0) * 1.20)
    crop = image.crop((max(0, int(x0-padding)), max(0, int(y0-padding)),
                       min(image.width, int(x1+padding)), min(image.height, int(y1+padding))))
    cfg = {
        **ocr_cfg,
        "languages": ["eng"],
        "char_whitelist": "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "min_word_confidence": 0,
        "timeout_seconds": 5,
        "single_pass_timeout_seconds": 5,
    }
    readings = []
    errors = []
    # PSM 13 treats the whole crop as a single glyph and is prone to appending
    # a border fragment as ``1``.  PSM 7 (single line) and PSM 11 (sparse text)
    # are deliberately different while preserving the token boundary.
    for psm in (7, 11):
        try:
            read = _tesseract_single(crop, page.page, cfg, psm=psm)
            text = " ".join(word.text for word in read.words).strip()
            normalized = re.sub(r"\s+", "", text).upper()
            valid = bool(re.fullmatch(r"[A-Z]{1,4}" + re.escape(candidate["digits"]), normalized))
            readings.append({"psm": psm, "text": text, "value": normalized if valid else None,
                             "confidence": read.confidence})
        except (RuntimeError, OSError) as exc:
            errors.append(str(exc))
    values = [reading["value"] for reading in readings]
    confirmed = len(values) == 2 and values[0] is not None and values[0] == values[1]
    raw_confidence = statistics.fmean(
        [reading["confidence"] for reading in readings]
    ) if readings else 0.0
    # Exact agreement across two materially different segmentation modes is
    # stronger than either engine score in isolation.  Keep the result below a
    # native-text certainty ceiling while avoiding the former minimum-score
    # collapse caused by one conservative PSM confidence.
    consensus_confidence = min(0.98, max(raw_confidence, 0.86)) if confirmed else raw_confidence
    page.ocr_diagnostics["compact_header_number"] = {
        "original": candidate["raw_text"], "bbox": candidate["bbox"],
        "confirmed": confirmed, "value": values[0] if confirmed else None,
        "confidence": consensus_confidence,
        "raw_mean_confidence": raw_confidence,
        "readings": readings, "errors": errors,
    }


def _field_from_match(match: re.Match, row: TextRow, normalizer=None, semantic=0.92) -> ExtractedField:
    raw = clean_text(match.group(1))
    normalized = normalizer(raw) if normalizer else raw
    source_conf = row.confidence
    final = max(0.0, min(1.0, 0.55 * source_conf + 0.45 * semantic))
    return ExtractedField(
        value=normalized, raw_value=raw, normalized_value=normalized,
        ocr_confidence=source_conf, semantic_confidence=semantic,
        final_confidence=final, validation_status="NOT_CHECKED",
        evidence=Evidence(page=row.page, bbox=row.bbox, source_text=row.text,
                          extraction_method=row.words[0].source if row.words else None),
    )


def extract_header(rows: list[TextRow], default_currency: str = "EUR") -> tuple[dict[str, ExtractedField], CommercialTerms, Logistics]:
    out: dict[str, ExtractedField] = {}
    for field, patterns in FIELD_PATTERNS.items():
        for row in rows:
            text = row.text
            for pat in patterns:
                m = re.search(pat, text, flags=re.I)
                if m:
                    if "date" in field:
                        out[field] = _field_from_match(m, row, parse_date)
                    else:
                        out[field] = _field_from_match(m, row)
                    break
            if field in out:
                break

    commercial = CommercialTerms()
    logistics = Logistics()
    joined = "\n".join(r.text for r in rows)

    currency = normalize_currency(joined, None)
    if currency:
        row = next((r for r in rows if currency in r.text.upper() or any(sym in r.text for sym in "€$£¥")), rows[0] if rows else None)
        if row:
            out["currency"] = ExtractedField(value=currency, raw_value=currency, normalized_value=currency,
                ocr_confidence=row.confidence, semantic_confidence=0.90,
                final_confidence=0.55 * row.confidence + 0.45 * 0.90,
                evidence=Evidence(page=row.page, bbox=row.bbox, source_text=row.text, extraction_method=row.words[0].source if row.words else None))

    inc = re.search(r"\b(EXW|FCA|FAS|FOB|CFR|CIF|CPT|CIP|DAP|DPU|DDP)\b(?:\s+([^\n]{2,60}))?", joined, flags=re.I)
    if inc:
        logistics.incoterm = inc.group(1).upper()
        if inc.group(2):
            logistics.incoterm_location = clean_text(inc.group(2))

    pm = re.search(r"(?:payment\s*terms|conditions?\s+de\s+paiement|zahlungsbedingungen)\s*[:\-]?\s*([^\n]{2,100})", joined, flags=re.I)
    if pm:
        commercial.payment_terms = clean_text(pm.group(1))
    days = re.search(r"\b(\d{1,3})\s*(?:days?|jours?|tage)\b", commercial.payment_terms or "", flags=re.I)
    if days:
        commercial.payment_due_days = int(days.group(1))

    return out, commercial, logistics


def _extract_party(rows: list[TextRow], labels: list[str]) -> Party:
    label_tokens = [ascii_fold(label).lower() for label in labels]
    for i, row in enumerate(rows):
        folded = ascii_fold(row.text).lower()
        # Avoid semantic collisions such as French "NET A PAYER", where "payer"
        # is an amount label and not a business party.
        if "payer" in label_tokens and re.search(r"\b(?:net|montant|total)\s+a\s+payer\b", folded):
            continue
        matched = [lbl for lbl in label_tokens if re.search(r"(?<![a-z0-9])" + re.escape(lbl) + r"(?![a-z0-9])", folded)]
        if matched:
            # Product and freight descriptions can legitimately contain role
            # words (for example ``PORT FOURNISSEUR 1 PI 20.0000``). A row with
            # quantity, unit and monetary cells is commercial table data, not a
            # party label.
            numeric_cells = sum(
                bool(re.fullmatch(
                    r"[-+]?\d+(?:[ .']\d{3})*(?:[,.]\d{1,6})?",
                    word.text.strip("€$£()[]{}:;"),
                ))
                for word in row.words
            )
            has_table_unit = any(
                re.fullmatch(
                    r"(?:U|UN|UNIT|PI|PIEC(?:E|ES)?|PCE|PCS|EA|KG|BOX|L|M)",
                    word.text.strip("|[]()."),
                    flags=re.I,
                )
                for word in row.words
            )
            if numeric_cells >= 2 and has_table_unit:
                continue
            txt = row.text
            value = txt
            for label in labels:
                value = re.sub(re.escape(label), "", value, flags=re.I)
            value = value.strip(" :-#")
            following = [r.text.strip() for r in rows[i+1:i+4] if r.page == row.page and r.text.strip()]
            party = Party(name=value or (following[0] if following else None))
            addr_lines = following if value else following[1:]
            if addr_lines:
                party.address = Address(
                    line1=addr_lines[0] if len(addr_lines) > 0 else None,
                    line2=addr_lines[1] if len(addr_lines) > 1 else None,
                    line3=addr_lines[2] if len(addr_lines) > 2 else None,
                )
                postal = re.search(r"\b(\d{4,6})\s+([A-Za-zÀ-ÿ .'-]{2,})", " ".join(addr_lines))
                if postal:
                    party.address.postal_code = postal.group(1)
                    party.address.city = clean_text(postal.group(2))
            return party
    return Party()


def extract_parties(rows: list[TextRow]) -> dict[str, Party]:
    return {name: _extract_party(rows, labels) for name, labels in PARTY_LABELS.items()}


def _canonical_header(token: str) -> str | None:
    t = ascii_fold(token).lower().strip(" .:/-_()[]")
    for key, aliases in HEADER_ALIASES.items():
        if t in {ascii_fold(a).lower() for a in aliases}:
            return key
    return None


def _header_token(token: str) -> str:
    return ascii_fold(token).lower().strip(" .:/-_()[]")


def _column_headers_from_rows(rows: list[TextRow]) -> dict[str, float]:
    """Resolve columns and the ownership of customer/supplier references.

    Headers are frequently split over two rows, for example ``CODE | REF`` then
    ``INTERNE | FOURNISSEUR``. Averaging both reference positions into one generic
    material column destroys the table geometry, so each anchor is paired with its
    nearest role qualifier.
    """
    found: dict[str, list[float]] = {}
    material_headers: list[float] = []
    qualifiers: list[tuple[str, float]] = []
    for row in rows:
        words = row.words
        folded = [_header_token(word.text) for word in words]
        phrase_words: set[int] = set()
        for index in range(len(words) - 1):
            pair = (folded[index], folded[index + 1])
            if pair in {("prix", "net"), ("net", "price"), ("netto", "preis")}:
                found.setdefault("net_unit_price", []).append(statistics.fmean([
                    (words[index].bbox[0] + words[index].bbox[2]) / 2,
                    (words[index + 1].bbox[0] + words[index + 1].bbox[2]) / 2,
                ]))
                # ``Prix`` belongs to ``Prix net`` here and must not be averaged
                # with a separate P.U. / gross-price column to its left.
                phrase_words.update({index, index + 1})
        for index, word in enumerate(words):
            center = (word.bbox[0] + word.bbox[2]) / 2
            key = None if index in phrase_words else _canonical_header(word.text)
            if key == "material":
                material_headers.append(center)
            elif key:
                found.setdefault(key, []).append(center)
            token = folded[index]
            if token in CUSTOMER_MATERIAL_QUALIFIERS:
                qualifiers.append(("customer_material", center))
            elif token in SUPPLIER_MATERIAL_QUALIFIERS:
                qualifiers.append(("supplier_material", center))

    columns = {key: statistics.fmean(values) for key, values in found.items()}
    assigned: set[str] = set()
    unresolved: list[float] = []
    for center in sorted(material_headers):
        nearby = [
            (abs(center - qualifier_center), role)
            for role, qualifier_center in qualifiers
            if abs(center - qualifier_center) <= 105.0
        ]
        role = min(nearby)[1] if nearby else None
        if role and role not in assigned:
            columns[role] = center
            assigned.add(role)
        else:
            unresolved.append(center)
    if unresolved:
        columns.setdefault("material", unresolved.pop(0))
    if unresolved:
        # Retain a second unqualified boundary without inventing its business role.
        columns.setdefault("secondary_material", unresolved.pop(0))
    return columns


def _column_headers(row: TextRow) -> dict[str, float]:
    return _column_headers_from_rows([row])


def _find_table_header(
    rows: list[TextRow], min_header_matches: int,
) -> tuple[int, int, dict[str, float]] | None:
    """Find a one-to-three-row commercial table header."""
    best = None
    for start in range(len(rows)):
        for width in range(1, 4):
            group = rows[start:start + width]
            if len(group) != width or any(row.page != group[0].page for row in group):
                continue
            heights = [max(1.0, row.bbox[3] - row.bbox[1]) for row in group if row.bbox]
            if not heights:
                continue
            span = group[-1].bbox[3] - group[0].bbox[1]
            if span > max(80.0, statistics.fmean(heights) * 6.0):
                continue
            columns = _column_headers_from_rows(group)
            semantic = set(columns)
            reference_keys = {
                "material", "customer_material", "supplier_material",
                "secondary_material",
            }
            has_reference = bool(semantic & reference_keys)
            distinct = len(semantic - {"customer_material", "supplier_material", "secondary_material"})
            distinct += int(bool(semantic & {"customer_material", "supplier_material", "secondary_material"}))
            if not (
                has_reference
                and "description" in semantic
                and "quantity" in semantic
                and ("unit_price" in semantic or "net_unit_price" in semantic or "total" in semantic)
                and distinct >= min_header_matches
            ):
                continue
            # Do not extend an already complete header into product or charge
            # rows merely because their description contains a qualifier such as
            # ``FOURNISSEUR``. Two numeric cells on a following row are a strong
            # commercial-row signal; genuine split qualifier rows such as
            # ``INTERNE | FOURNISSEUR`` do not contain commercial numbers.
            contains_commercial_row = any(
                sum(
                    bool(re.fullmatch(
                        r"[-+]?\d+(?:[ .']\d{3})*(?:[,.]\d{1,6})?",
                        word.text.strip("€$£()[]{}:;"),
                    ))
                    for word in candidate.words
                ) >= 2
                for candidate in group[1:]
            )
            explicit_roles = len(semantic & {"customer_material", "supplier_material"})
            # Prefer the narrowest header band once the same semantics are found;
            # otherwise the first product row can be swallowed as a third header.
            score = (int(not contains_commercial_row), explicit_roles, distinct, -width)
            candidate = (score, start, start + width, columns)
            if best is None or score > best[0]:
                best = candidate
        if best is not None and best[1] == start and best[0][0] >= 1 and best[0][1] >= 1:
            break
    return (best[1], best[2], best[3]) if best else None


def _parse_table_number(value: str | None) -> float | None:
    """Parse a number while ignoring an attached printed unit marker."""
    if value is None:
        return None
    source = str(value).replace("\u00a0", " ").strip()
    compact = re.sub(r"\s+", "", source)
    match = re.fullmatch(
        r"([-+]?\d+(?:[.,]\d{1,6})?)(?:[A-Za-z]{1,6}\.?|[€$£])?",
        compact,
    )
    if match:
        numeric = match.group(1)
    else:
        candidates = re.findall(
            r"(?<!\d)(?:\d{1,3}(?:[ .']\d{3})+|\d+)(?:[,.]\d{1,6})?(?!\d)",
            source,
        )
        # Never concatenate distinct price/date/tax fields that drifted into a
        # single geometry bucket. Callers with column anchors can recover the
        # intended number independently.
        if len(candidates) != 1:
            return None
        numeric = candidates[0]
    if re.fullmatch(r"[-+]?\d+\.\d{1,6}", numeric):
        try:
            return float(numeric)
        except ValueError:
            return None
    return parse_number(numeric)


def _quantity_unit(value: str | None) -> str | None:
    compact = re.sub(r"\s+", "", str(value or ""))
    match = re.fullmatch(r"[-+]?\d+(?:[.,]\d{1,6})?([A-Za-z]{1,6})\.?", compact)
    if not match:
        return None
    unit = match.group(1).upper()
    if unit in {"P", "PC", "PCS", "PCE", "PIEC", "PIECE", "PIECES"}:
        return "PCE"
    return normalize_unit(unit)


def _reference_cell(value: str | None) -> tuple[str | None, str | None]:
    """Split a reference from description text that drifted into its column."""
    text = clean_text(value)
    if not text:
        return None, None
    tokens = text.split()
    index = next((i for i, token in enumerate(tokens) if _looks_like_item_code(token)), None)
    if index is None:
        return None, text
    reference = tokens[index].strip("[](){}:;,. |")
    spill = clean_text(" ".join(tokens[:index] + tokens[index + 1:]))
    return reference, spill


def _partition_row(row: TextRow, columns: dict[str, float]) -> dict[str, str]:
    if not columns:
        return {}
    ordered = sorted(columns.items(), key=lambda kv: kv[1])
    buckets: dict[str, list[str]] = {k: [] for k, _ in ordered}
    centers = [x for _, x in ordered]
    keys = [k for k, _ in ordered]
    for w in row.words:
        cx = (w.bbox[0] + w.bbox[2]) / 2
        idx = min(range(len(centers)), key=lambda i: abs(cx - centers[i]))
        buckets[keys[idx]].append(w.text)
    return {k: " ".join(v).strip() for k, v in buckets.items()}


def _line_from_cells(cells: dict[str, str], row: TextRow, default_currency: str) -> LineItem | None:
    joined = " ".join(cells.values()).strip()
    if not joined or any(joined.lower().startswith(x) for x in STOP_WORDS):
        return None
    qty_raw = cells.get("quantity") or ""
    printed_unit_price = _parse_table_number(cells.get("unit_price"))
    net_unit_price = _parse_table_number(cells.get("net_unit_price"))
    if net_unit_price is None and cells.get("net_unit_price"):
        # Unlabelled date/tax columns can drift into the last known price bucket.
        # Its first value is still anchored by the explicit net-price header.
        net_unit_price = _parse_table_number(
            str(cells["net_unit_price"]).split()[0]
        )
    unit_price = net_unit_price if net_unit_price is not None else printed_unit_price
    total = _parse_table_number(cells.get("total"))
    generic_material, generic_spill = _reference_cell(cells.get("material"))
    customer_material, customer_spill = _reference_cell(cells.get("customer_material"))
    supplier_material, supplier_spill = _reference_cell(cells.get("supplier_material"))
    secondary_material, secondary_spill = _reference_cell(cells.get("secondary_material"))
    material = supplier_material or generic_material or customer_material or secondary_material
    desc = clean_text(" ".join(
        value for value in (
            customer_spill, generic_spill, clean_text(cells.get("description")),
            supplier_spill, secondary_spill,
        ) if value
    )).strip(" -–—|:;")
    line_no = clean_text(cells.get("line"))
    # When a description word sits close to the quantity column, nearest-column
    # assignment can place it with the numeric quantity. Recover the text prefix.
    qty_matches = list(re.finditer(
        r"[-+]?(?:\d{1,3}(?:[ .']\d{3})+|\d+)(?:[,.]\d{1,6})?",
        qty_raw,
    ))
    unit_bound = [
        match for match in qty_matches
        if re.match(
            r"\s*(?:p\.?|u|ea|pc|pcs|pce|piece|pieces|kg|g|m|m2|m3|l|h)\b",
            qty_raw[match.end():],
            flags=re.I,
        )
    ]
    # Description text can drift into the quantity cell (for example
    # ``18KW U 4,000 U``).  Prefer the rightmost number explicitly followed by
    # a quantity unit instead of treating a product dimension as the quantity.
    qty_match = unit_bound[-1] if unit_bound else (qty_matches[0] if qty_matches else None)
    qty = _parse_table_number(qty_match.group(0)) if qty_match else None
    inline_uom = _quantity_unit(qty_raw)
    if inline_uom is None and qty_match is not None:
        unit_tail = re.match(r"\s*([A-Za-z]{1,6})\.?", qty_raw[qty_match.end():])
        if unit_tail:
            inline_uom = normalize_unit(unit_tail.group(1))
    if qty_match:
        prefix = clean_text(qty_raw[:qty_match.start()].strip())
        suffix = clean_text(qty_raw[qty_match.end():].strip())
        if inline_uom:
            prefix = suffix = ""
        spill = clean_text(" ".join(x for x in [prefix, suffix] if x))
        if spill and re.search(r"[A-Za-zÀ-ÿ]", spill):
            desc = clean_text(" ".join(x for x in [desc, spill] if x))
    if qty is None and unit_price is None and total is None and not material and not desc:
        return None
    calculated_total = bool(
        "total" not in cells
        and qty is not None
        and unit_price is not None
    )
    if calculated_total:
        total = round(float(qty) * float(unit_price), 6)
    raw_uom = clean_text(cells.get("uom"))
    normalized_uom = normalize_unit(raw_uom)
    if _header_token(raw_uom or "") in {"p", "pc", "pcs", "pce", "pi", "piec", "piece", "pieces"}:
        normalized_uom = "PCE"
    completeness = sum(v is not None and v != "" for v in [material, desc, qty, unit_price, total]) / 5
    confidence = min(1.0, 0.6 * row.confidence + 0.4 * completeness)
    return LineItem(
        line_number=line_no,
        material_number=material,
        article_number=material,
        product_code=secondary_material if secondary_material != material else None,
        customer_material_number=customer_material,
        supplier_material_number=supplier_material,
        description=desc,
        product_family=clean_text(cells.get("product_family")),
        quantity=qty,
        ordered_quantity=qty,
        uom=normalized_uom or inline_uom,
        unit_price=unit_price,
        gross_unit_price=(
            printed_unit_price
            if net_unit_price is not None and printed_unit_price is not None
            else None
        ),
        net_unit_price=unit_price,
        currency=default_currency,
        line_total=total,
        line_net_amount=total,
        requested_delivery_date=parse_date(cells.get("date")),
        raw_text=row.text,
        page=row.page,
        bbox=row.bbox,
        confidence=confidence,
        warnings=(
            (["Gross/list unit price preserved separately; calculations use printed net unit price."]
             if net_unit_price is not None and printed_unit_price is not None else [])
            + (["Line total calculated from printed quantity and unit price; no printed line amount."]
               if calculated_total else [])
        ),
    )


def extract_lines(rows: list[TextRow], default_currency: str = "EUR", min_header_matches: int = 3) -> list[LineItem]:
    lines: list[LineItem] = []
    header = _find_table_header(rows, min_header_matches)
    if header:
        header_start, header_end, cols = header
        page = rows[header_start].page
        for next_row in rows[header_end:]:
            if next_row.page != page:
                break
            lower = ascii_fold(next_row.text).lower()
            if _is_line_table_stop(lower):
                break
            item = _line_from_cells(_partition_row(next_row, cols), next_row, default_currency)
            if item:
                lines.append(item)

    if lines:
        return _merge_continuations(lines)

    # Regex fallback for flat OCR text: line, material, description, qty, uom, unit price, amount.
    pattern = re.compile(
        r"^\s*(\d{1,4})\s+([A-Z0-9][A-Z0-9._/\-]{2,})\s+(.+?)\s+(\d[\d .,'-]*)\s+([A-Za-z]{1,6})\s+([\d .,'-]+)\s+([\d .,'-]+)\s*$"
    )
    for row in rows:
        m = pattern.match(row.text)
        if not m:
            continue
        qty, price, total = parse_number(m.group(4)), parse_number(m.group(6)), parse_number(m.group(7))
        lines.append(LineItem(
            line_number=m.group(1), material_number=m.group(2), description=clean_text(m.group(3)),
            quantity=qty, ordered_quantity=qty, uom=normalize_unit(m.group(5)), unit_price=price,
            net_unit_price=price, line_total=total, line_net_amount=total, currency=default_currency,
            raw_text=row.text, page=row.page, bbox=row.bbox, confidence=row.confidence * 0.9,
        ))
    return lines




def _partition_row_intervals(row: TextRow, columns: dict[str, float]) -> dict[str, str]:
    """Assign tokens to the nearest resolved header anchor.

    Supplier references are often right-aligned and therefore begin slightly to
    the left of the ``REF FOURNISSEUR`` label. Midpoint/nearest assignment keeps
    those values in the reference column instead of appending them to description.
    """
    if not columns:
        return {}
    ordered = sorted(columns.items(), key=lambda kv: kv[1])
    keys=[k for k,_ in ordered]; starts=[x for _,x in ordered]
    buckets={k:[] for k in keys}
    for w in row.words:
        cx=(w.bbox[0]+w.bbox[2])/2
        idx=min(range(len(starts)), key=lambda i: abs(cx-starts[i]))
        buckets[keys[idx]].append(w.text)
    return {k:" ".join(v).strip() for k,v in buckets.items()}

def extract_lines_from_pages(pages: list[PageResult], default_currency: str = "EUR", min_header_matches: int = 3) -> list[LineItem]:
    """Spatial table extraction tolerant to multi-row / fragmented OCR headers."""
    out: list[LineItem] = []
    for page in pages:
        rows = [
            TextRow(page=page.page, words=ws)
            for ws in group_words_into_rows(page.words, y_factor=0.55)
        ]
        header = _find_table_header(rows, min_header_matches)
        if not header:
            continue
        _, header_end, cols = header
        for row in rows[header_end:]:
            ws = row.words
            low=ascii_fold(row.text).lower().strip()
            if _is_line_table_stop(low):
                break
            # Ignore repeated headers / prose and require at least one numeric token.
            if sum(1 for w in ws if _canonical_header(w.text)) >= 2:
                continue
            item=_line_from_cells(_partition_row_intervals(row,cols),row,default_currency)
            commercial_values = item and (
                item.quantity is not None
                or item.unit_price is not None
                or item.line_total is not None
            )
            continuation = item and out and (
                not item.material_number
                and bool(item.description)
                and not commercial_values
            )
            if item and (commercial_values or continuation):
                # Reject obvious total rows that slipped through OCR noise.
                if item.description and ascii_fold(item.description).lower().startswith(tuple(STOP_WORDS)):
                    break
                out.append(item)
        if out:
            return _merge_continuations(out)
    return out

def _merge_continuations(lines: list[LineItem]) -> list[LineItem]:
    result: list[LineItem] = []
    for item in lines:
        if result and not item.material_number and item.description and item.quantity is None and item.unit_price is None:
            prev = result[-1]
            continuation = clean_text(item.description)
            if re.match(
                r"^(?:promo(?:tion)?\s+pa|promotion\b|remise\b)",
                ascii_fold(continuation or "").lower(),
            ):
                if continuation and continuation not in prev.notes:
                    prev.notes.append(continuation)
            else:
                prev.description = clean_text(
                    (prev.description or "") + " " + (continuation or "")
                )
            prev.raw_text = clean_text((prev.raw_text or "") + " " + (item.raw_text or ""))
            prev.confidence = min(prev.confidence, item.confidence)
        else:
            result.append(item)
    return result


def extract_totals(rows: list[TextRow], default_currency: str = "EUR") -> Totals:
    totals = Totals(currency=default_currency)
    patterns = [
        ("grand_total", r"(?:grand\s*total|total\s*(?:ttc|due|commande|order)?|gesamtbetrag|total\s+general)\s*[: ]+([^\n]+)$"),
        ("total_net", r"(?:net\s*total|total\s*(?:h\.?t\.?|hors\s+taxes?)|netto)\s*[: ]+([^\n]+)$"),
        ("subtotal", r"(?:sub\s*total|sous[- ]?total|zwischensumme)\s*[: ]+([^\n]+)$"),
        ("total_vat", r"(?:vat|tva|mwst)\s*(?:total)?\s*[: ]+([^\n]+)$"),
        ("total_tax", r"(?:tax|taxe)\s*(?:total)?\s*[: ]+([^\n]+)$"),
        ("total_shipping", r"(?:shipping|freight|transport|port)\s*[: ]+([^\n]+)$"),
    ]
    for row in rows:
        folded_row = ascii_fold(row.text).lower()
        legal_vat_identifier = bool(re.search(
            r"(?:n[\s.°ºo-]*|no\s+|numero\s+|number\s+)(?:de\s*)?(?:tva|vat)\b|"
            r"\b(?:tva|vat)\s*(?:ic\b|intra|id|no|number|registration|[:#-]\s*[a-z]{0,2}\d{8,})",
            folded_row,
            flags=re.I,
        ))
        for attr, pattern in patterns:
            if attr in {"total_vat", "total_tax"} and legal_vat_identifier:
                continue
            if attr in {"total_vat", "total_tax", "grand_total"} and re.search(
                r"\b(?:hors\s+taxes?|total\s+h\.?t\.?)\b", folded_row
            ):
                continue
            m = re.search(pattern, row.text, flags=re.I)
            if m:
                nums = re.findall(r"[-+]?\d[\d .,'-]*(?:[.,]\d+)?", m.group(1))
                if nums:
                    value = parse_number(nums[-1])
                    if value is not None:
                        setattr(totals, attr, value)
    return totals


def extract_extra_fields(rows: list[TextRow], known_values: Iterable[str | None]) -> list[ExtraField]:
    known = {clean_text(x) for x in known_values if x}
    known_labels = {
        "supplier", "vendor", "fournisseur", "buyer", "customer", "client",
        "payment terms", "conditions de paiement", "zahlungsbedingungen",
        "incoterm", "net total", "total net", "subtotal", "sub total",
        "vat", "tva", "tax", "taxe", "grand total", "total",
        "order date", "date commande", "customer reference", "vendor reference"
    }
    extras: list[ExtraField] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        m = re.match(r"^\s*([^:]{2,40})\s*:\s*(.{1,120})$", row.text)
        if not m:
            continue
        label, value = clean_text(m.group(1)), clean_text(m.group(2))
        if not label or not value or value in known:
            continue
        if ascii_fold(label).lower() in {ascii_fold(x).lower() for x in known_labels}:
            continue
        key = (label.lower(), value)
        if key in seen:
            continue
        seen.add(key)
        extras.append(ExtraField(label=label, value=value, page=row.page, bbox=row.bbox, confidence=row.confidence * 0.85))
    # Dotted ERP labels (e.g. "Initiales..... AD") and common legal identifiers.
    extra_patterns = [
        ("SIRET", r"\bSIRET\s*[:.]?\s*([0-9][0-9 ]{8,20})"),
        ("APE", r"\bAPE\s*[:.]?\s*([0-9]{4}\s*[A-Z])"),
        ("VAT number", r"(?:N[°ºo]?\s*TVA|VAT\s*(?:No|number))\s*[:.]?\s*([A-Z]{2}\s*[0-9][0-9 ]{6,20})"),
        ("Share capital", r"(?:capital\s+de|share\s+capital)\s*([0-9][0-9 ]+(?:[,.]\d{2})?\s*[€$£]?)"),
    ]
    for row in rows:
        dotted = re.match(r"^\s*([A-Za-zÀ-ÿ][^:]{1,35}?)\.{2,}\s*(.+?)\s*$", row.text)
        if dotted:
            label, value = clean_text(dotted.group(1)), clean_text(dotted.group(2))
            if label and value and (label.lower(), value) not in seen:
                seen.add((label.lower(), value)); extras.append(ExtraField(label=label, value=value, page=row.page, bbox=row.bbox, confidence=row.confidence*0.90))
        for label, pat in extra_patterns:
            m = re.search(pat, row.text, flags=re.I)
            if m:
                value = clean_text(m.group(1))
                key=(label.lower(), value or "")
                if value and key not in seen:
                    seen.add(key); extras.append(ExtraField(label=label, value=value, page=row.page, bbox=row.bbox, confidence=row.confidence*0.92))
    return extras[:150]

# --- V4.1 spatial extractors -------------------------------------------------


def _strict_text_rows(page: PageResult, tolerance_px: float | None = None) -> list[TextRow]:
    """Group OCR words by baseline without letting logos/oversized glyphs merge many rows."""
    if not page.words:
        return []
    normal_heights = [max(1.0, w.bbox[3] - w.bbox[1]) for w in page.words]
    med = statistics.median(normal_heights)
    tol = tolerance_px if tolerance_px is not None else max(9.0, min(32.0, med * 0.85))
    ordered = sorted(page.words, key=lambda w: (((w.bbox[1] + w.bbox[3]) / 2), w.bbox[0]))
    clusters: list[list[WordToken]] = []
    centers: list[float] = []
    for w in ordered:
        cy = (w.bbox[1] + w.bbox[3]) / 2
        best = None
        best_dist = None
        for i, c in enumerate(centers):
            d = abs(cy - c)
            if d <= tol and (best_dist is None or d < best_dist):
                best, best_dist = i, d
        if best is None:
            clusters.append([w]); centers.append(cy)
        else:
            clusters[best].append(w)
            centers[best] = statistics.fmean((x.bbox[1] + x.bbox[3]) / 2 for x in clusters[best])
    order = sorted(range(len(clusters)), key=lambda i: centers[i])
    return [TextRow(page=page.page, words=sorted(clusters[i], key=lambda w: w.bbox[0])) for i in order]

def _row_center(row: TextRow) -> float:
    if not row.words:
        return 0.0
    return statistics.fmean((w.bbox[1] + w.bbox[3]) / 2 for w in row.words)


def _row_xmin(row: TextRow) -> float:
    return min((w.bbox[0] for w in row.words), default=0.0)


def _row_xmax(row: TextRow) -> float:
    return max((w.bbox[2] for w in row.words), default=0.0)


def _field_from_words(value: str, words: list[WordToken], normalizer=None, semantic: float = 0.94, method: str = "spatial_anchor") -> ExtractedField:
    raw = clean_text(value) or ""
    normalized = normalizer(raw) if normalizer else raw
    conf = statistics.fmean([w.confidence for w in words]) if words else 0.0
    bbox = None
    if words:
        bbox = (
            min(w.bbox[0] for w in words), min(w.bbox[1] for w in words),
            max(w.bbox[2] for w in words), max(w.bbox[3] for w in words),
        )
    final = max(0.0, min(1.0, 0.55 * conf + 0.45 * semantic))
    return ExtractedField(
        value=normalized, raw_value=raw, normalized_value=normalized,
        ocr_confidence=conf, semantic_confidence=semantic,
        final_confidence=final, validation_status="NOT_CHECKED",
        evidence=Evidence(page=words[0].page if words else None, bbox=bbox,
                          source_text=" ".join(w.text for w in words), extraction_method=method),
    )


def _purchase_order_context(page: PageResult) -> bool:
    text = ascii_fold(page.text).lower()
    signals = [
        "purchase order", "bon de commande", "commande fournisseur", "commande achat",
        "bestellung", "pedido de compra", "order date", "fournisseur", "supplier",
    ]
    return sum(1 for s in signals if s in text) >= 1


def extract_spatial_header_fields(pages: list[PageResult], existing: dict[str, ExtractedField] | None = None) -> dict[str, ExtractedField]:
    """Recover header values from spatially separated labels and values.

    This targets forms where OCR flattens columns in the wrong order. It is generic:
    it looks for a PO-like document context, then interprets same-row label/value groups.
    """
    out = dict(existing or {})
    for page in pages:
        if not _purchase_order_context(page):
            continue
        rows = _strict_text_rows(page)
        top_limit = page.height * 0.35
        for row in rows:
            if _row_center(row) > top_limit:
                continue
            words = row.words
            folded = [ascii_fold(w.text).lower().strip(".:#()[]") for w in words]

            # PO number: support split forms such as "N° | CF | 000337886".
            number_idx = None
            for i, token in enumerate(folded):
                if token in {"n°", "nº", "no", "n", "numero", "number", "#"} or re.fullmatch(r"n[o0°º]?", token):
                    number_idx = i
                    break
            if number_idx is not None and not out.get("number"):
                collected: list[WordToken] = []
                for w, tok in zip(words[number_idx + 1:], folded[number_idx + 1:]):
                    if tok in {"date", "page", "datum", "fecha"}:
                        break
                    if collected and w.bbox[0] - collected[-1].bbox[2] > max(160.0, page.width * 0.09):
                        break
                    if re.fullmatch(r"[A-Z]{1,8}|[A-Z0-9][A-Z0-9._/\-]{2,}", w.text.upper().strip("[]()")):
                        collected.append(w)
                    elif collected:
                        break
                if collected:
                    parts = [re.sub(r"^[\[({]+|[\])}]+$", "", w.text.upper()) for w in collected]
                    joined = "".join(parts)
                    # Require enough digits to avoid accepting a generic label fragment.
                    if len(re.findall(r"\d", joined)) >= 4 and 5 <= len(joined) <= 40:
                        out["number"] = _field_from_words(joined, collected, semantic=0.98, method="spatial_po_number")

            # Order date: nearest date-looking token to a DATE label on the same row.
            if not out.get("order_date"):
                for i, tok in enumerate(folded):
                    if tok in {"date", "datum", "fecha"}:
                        candidates = []
                        label = words[i]
                        for w in words[i + 1:]:
                            if w.bbox[0] < label.bbox[2]:
                                continue
                            if w.bbox[0] - label.bbox[2] > page.width * 0.22:
                                break
                            parsed = parse_date(w.text)
                            if parsed:
                                candidates.append((w, parsed))
                        if candidates:
                            w, parsed = candidates[0]
                            out["order_date"] = _field_from_words(w.text, [w], parse_date, semantic=0.97, method="spatial_order_date")
                            break

        # Language hint from dense procurement vocabulary.
        if not out.get("language"):
            folded_text = ascii_fold(page.text).lower()
            fr_hits = sum(1 for x in ["commande", "fournisseur", "acheteur", "livraison", "designation", "quantite", "prix", "tva", "paiement"] if x in folded_text)
            de_hits = sum(1 for x in ["bestellung", "lieferant", "lieferung", "menge", "preis", "zahlung"] if x in folded_text)
            en_hits = sum(1 for x in ["purchase order", "supplier", "buyer", "delivery", "quantity", "price", "payment"] if x in folded_text)
            lang, hits = max([("fr", fr_hits), ("de", de_hits), ("en", en_hits)], key=lambda x: x[1])
            if hits >= 3:
                pseudo = [w for w in page.words if ascii_fold(w.text).lower() in {"commande", "fournisseur", "acheteur", "livraison", "bestellung", "supplier", "quantity", "prix", "price"}][:6]
                out["language"] = _field_from_words(lang, pseudo, semantic=0.90, method="language_hint")

    return out


def _extract_value_after_label(row: TextRow, label_pattern: str, stop_pattern: str | None = None) -> str | None:
    text = row.text
    m = re.search(label_pattern, text, flags=re.I)
    if not m:
        return None
    value = text[m.end():]
    if stop_pattern:
        value = re.split(stop_pattern, value, maxsplit=1, flags=re.I)[0]
    return clean_text(value.strip(" .:-#()"))



def _row_fragment(row: TextRow, *, min_x: float | None = None, max_x: float | None = None) -> TextRow | None:
    words = [w for w in row.words if (min_x is None or w.bbox[0] >= min_x) and (max_x is None or w.bbox[2] <= max_x)]
    return TextRow(page=row.page, words=words) if words else None

def enhance_parties_spatial(pages: list[PageResult], parties: dict[str, Party]) -> dict[str, Party]:
    """Improve buyer/supplier/ship-to segmentation from page geometry."""
    parties = dict(parties)
    for page in pages:
        rows = _strict_text_rows(page)
        width = page.width

        # Issuer/buyer organisation: combine large top-left branding words with a legal suffix.
        buyer = parties.get("buyer") or Party()
        top_left = [w for w in page.words if w.bbox[0] < width * 0.52 and ((w.bbox[1] + w.bbox[3]) / 2) < page.height * 0.15]
        heights = [w.bbox[3] - w.bbox[1] for w in page.words] or [20.0]
        median_h = statistics.median(heights)
        legal_tokens = [w for w in top_left if re.fullmatch(r"(?:S\.?A\.?|SAS|SARL|GMBH|LTD|INC|LLC|AG)", ascii_fold(w.text), flags=re.I)]
        if legal_tokens:
            legal = max(legal_tokens, key=lambda w: w.confidence)
            nearby = [w for w in top_left if abs(((w.bbox[1]+w.bbox[3])/2) - ((legal.bbox[1]+legal.bbox[3])/2)) <= max(85.0, median_h*3.0) and (w.bbox[3]-w.bbox[1]) >= median_h*1.8 and (len(re.sub(r"[^A-Za-zÀ-ÿ]", "", w.text)) >= 3 or w is legal)]
            if legal not in nearby:
                nearby.append(legal)
            nearby = sorted(set((w.text, w.bbox) for w in nearby), key=lambda x: x[1][0])
            name = clean_text(" ".join(x[0] for x in nearby))
            if name and 2 <= len(name) <= 80:
                buyer.name = name
                buyer.legal_name = name
                buyer.address = Address()
                # Recover compact head-office address from left header rows below branding.
                left_frags = []
                for r in rows:
                    cy = _row_center(r)
                    if page.height*0.075 <= cy <= page.height*0.14:
                        frag = _row_fragment(r, max_x=width*0.20)
                        if frag and frag.text.strip():
                            left_frags.append(frag)
                addr_lines = [clean_text(r.text) for r in left_frags if re.search(r"\b(?:rue|street|strasse|straße|bp|b\.p\.|cedex|\d{5})\b", ascii_fold(r.text), flags=re.I)]
                if addr_lines:
                    buyer.address.line1 = addr_lines[0]
                    if len(addr_lines) > 1:
                        buyer.address.line2 = addr_lines[1]

        # Buyer contact row is often explicitly labelled "Acheteur".
        for row in rows:
            folded = ascii_fold(row.text).lower()
            if "acheteur" in folded or re.search(r"\bbuyer\b", folded):
                val = _extract_value_after_label(row, r"(?:acheteur|buyer)\s*[. :\-]*", r"\b(?:tel|tél|phone)\b")
                if val:
                    buyer.contact.name = val
                phone = re.search(r"(?:tel|tél|phone)\s*[:.]?\s*([+\d][\d .\-]{7,})", row.text, flags=re.I)
                if phone:
                    buyer.contact.phone = clean_text(phone.group(1))
                break
        parties["buyer"] = buyer

        # Supplier code label anchors the supplier block. Search nearby right-side rows
        # for the supplier name/address instead of consuming the entire page header.
        supplier = parties.get("supplier") or Party()
        supplier_anchor = None
        for row in rows:
            folded = ascii_fold(row.text).lower()
            if re.search(r"\b(?:fournisseur|supplier|vendor|lieferant)\b", folded) and _row_center(row) < page.height * 0.45:
                # Avoid document title rows like "COMMANDE FOURNISSEUR".
                if "commande fournisseur" in folded or "purchase order" in folded:
                    continue
                supplier_anchor = row
                code = _extract_value_after_label(row, r"(?:fournisseur|supplier|vendor|lieferant)\s*[. :\-]*", r"\b(?:tel|tél|phone|fax)\b")
                if code:
                    code = code.split()[0]
                    if 2 <= len(code) <= 30:
                        upper_code = code.upper()
                        if re.fullmatch(r"[A-Z]{2}[O0]{2}\d", upper_code):
                            upper_code = upper_code[:2] + upper_code[2:].replace("O", "0")
                        supplier.code = upper_code
                break
        if supplier_anchor:
            ay = _row_center(supplier_anchor)
            right_rows = []
            for r in rows:
                if ay - 140 <= _row_center(r) <= ay + 120:
                    frag = _row_fragment(r, min_x=width * 0.50)
                    if frag and frag.text.strip():
                        right_rows.append(frag)
            name_candidates = []
            for r in right_rows:
                txt = clean_text(r.text) or ""
                folded = ascii_fold(txt).lower()
                if not txt or any(k in folded for k in ["fax", "tel", "tél", "fournisseur", "supplier", "vendor"]):
                    continue
                if re.match(r"^\d{1,5}\b", txt):
                    continue
                alpha_ratio = sum(ch.isalpha() for ch in txt) / max(1, len(txt))
                if alpha_ratio > 0.55 and len(txt) <= 90:
                    name_candidates.append(r)
            if name_candidates:
                name_row = min(name_candidates, key=lambda r: abs(_row_center(r) - (ay - 100)))
                name_tokens = [t for t in name_row.text.split() if len(re.sub(r"[^A-Za-zÀ-ÿ]", "", t)) >= 2]
                supplier.name = clean_text(" ".join(name_tokens)) or clean_text(name_row.text)
                supplier.legal_name = supplier.name
                supplier.address = Address()
                ny = _row_center(name_row)
                address_rows = [r for r in right_rows if _row_center(r) > ny and r is not name_row]
                addr_texts = []
                for r in sorted(address_rows, key=_row_center):
                    txt = clean_text(r.text) or ""
                    if re.search(r"\b(?:tel|tél|fax|phone)\b", ascii_fold(txt), flags=re.I):
                        continue
                    if txt:
                        addr_texts.append(txt)
                if addr_texts:
                    supplier.address.line1 = addr_texts[0]
                    if len(addr_texts) > 1:
                        supplier.address.line2 = addr_texts[1]
                    joined = " ".join(addr_texts)
                    m = re.search(r"\b(\d{4,6})\s*([A-Za-zÀ-ÿ .'-]{2,})", joined)
                    if m:
                        supplier.address.postal_code = m.group(1)
                        supplier.address.city = clean_text(m.group(2))
        # Phones/fax from supplier label neighbourhood.
        if supplier_anchor:
            ay = _row_center(supplier_anchor)
            neigh = "\n".join(r.text for r in rows if abs(_row_center(r) - ay) < 110)
            tel = re.search(r"(?:tel|tél|phone)\s*[:.]?\s*(\d{8,15})", neigh, flags=re.I)
            fax = re.search(r"fax\s*[:.]?\s*(\d{8,15})", neigh, flags=re.I)
            if tel:
                supplier.phone = tel.group(1)
            if fax:
                supplier.fax = fax.group(1)
        parties["supplier"] = supplier

        # Ship-to block: label defines the x-origin; collect only rows underneath in
        # that right-side region, preventing left-side depot/currency labels from mixing in.
        ship = parties.get("ship_to") or Party()
        ship_label = None
        for row in rows:
            folded = ascii_fold(row.text).lower()
            if any(k in folded for k in ["adresse de livraison", "ship to", "deliver to", "livrer a"]):
                ship_label = row
                break
        if ship_label:
            label_words = [w for w in ship_label.words if any(k in ascii_fold(w.text).lower() for k in ["adresse", "ship", "deliver"])]
            x0 = min((w.bbox[0] for w in label_words), default=width * 0.5)
            y0 = _row_center(ship_label)
            candidate_rows = []
            for r in rows:
                cy = _row_center(r)
                if y0 + 20 <= cy <= y0 + 260:
                    kept = [w for w in r.words if w.bbox[0] >= x0 - 10]
                    if kept:
                        frag = TextRow(page=r.page, words=kept)
                        if len((frag.text or "").strip()) > 1:
                            candidate_rows.append(frag)
            candidate_rows.sort(key=_row_center)
            if candidate_rows:
                ship.name = clean_text(candidate_rows[0].text)
                ship.address = Address()
                addr = [clean_text(r.text) for r in candidate_rows[1:] if clean_text(r.text)]
                if addr:
                    ship.address.line1 = addr[0]
                    if len(addr) > 1:
                        ship.address.line2 = addr[1]
                    joined = " ".join(addr)
                    m = re.search(r"\b(\d{4,6})\s*([A-Za-zÀ-ÿ .'-]{2,})", joined)
                    if m:
                        ship.address.postal_code = m.group(1)
                        ship.address.city = clean_text(m.group(2))
        parties["ship_to"] = ship
    return parties


def _multiword_columns(row: TextRow) -> dict[str, float]:
    cols = _column_headers(row)
    words = row.words
    folded = [ascii_fold(w.text).lower().strip(" .:/-_()[]") for w in words]
    for i in range(len(words) - 1):
        pair = f"{folded[i]} {folded[i+1]}"
        if pair in {"prix net", "net price", "netto preis"}:
            cols["net_unit_price"] = statistics.fmean([(words[i].bbox[0]+words[i].bbox[2])/2, (words[i+1].bbox[0]+words[i+1].bbox[2])/2])
            # A standalone "net" must not be interpreted as a line amount here.
            cols.pop("total", None)
    return cols


def _looks_like_item_code(token: str) -> bool:
    # OCR commonly turns the vertical separator before a reference into ``|``.
    # Treat it as table geometry, not as part of the identifier.
    t = token.strip("[](){}:;,. |")
    if len(t) < 4 or len(t) > 40:
        return False
    if t.upper() in {"N/REF", "TOTAL", "PAIEMENT", "ACOMPTE"}:
        return False
    if re.fullmatch(r"(?:[0-3]?\d)[/.-](?:0?\d|1[0-2])[/.-](?:\d{2}|\d{4})", t):
        return False
    has_digit = any(ch.isdigit() for ch in t)
    has_alpha = any(ch.isalpha() for ch in t)
    return has_digit and (has_alpha or len(re.sub(r"\D", "", t)) >= 6) and bool(re.fullmatch(r"[A-Za-z0-9._/\-]+", t))


def extract_multiline_lines_from_pages(pages: list[PageResult], default_currency: str = "EUR", min_header_matches: int = 3) -> list[LineItem]:
    """Segment line-item tables where an article code and its numeric detail live on different OCR rows."""
    for page in pages:
        rows = [TextRow(page=page.page, words=ws) for ws in group_words_into_rows(page.words, y_factor=0.58)]
        header = _find_table_header(rows, min_header_matches)
        if header is None:
            continue
        header_idx, header_end, cols = header

        ordered = sorted(cols.items(), key=lambda kv: kv[1])
        x_by = dict(ordered)
        material_x = (
            x_by.get("customer_material")
            or x_by.get("material")
            or x_by.get("supplier_material")
            or ordered[0][1]
        )
        qty_x = x_by.get("quantity")
        if qty_x is None:
            continue
        # Anchor item codes are expected near the first column, far left of quantity.
        anchor_right = min(qty_x * 0.42, page.width * 0.24)
        # Distances must scale with the page coordinate system. Native PDFs use
        # points (~595 wide) whereas 300-dpi OCR pages are often ~2500 px wide.
        desc_left = material_x + max(12.0, page.width * 0.012)
        desc_right = qty_x - max(10.0, page.width * 0.008)

        data_rows = rows[header_end:]
        # Find anchors.
        anchors: list[tuple[int, TextRow, list[WordToken]]] = []
        for ridx, row in enumerate(data_rows):
            low = ascii_fold(row.text).lower().strip()
            if _is_line_table_stop(low) or "net a payer" in low or "net à payer" in row.text.lower():
                break
            left_tokens = [w for w in row.words if w.bbox[0] <= anchor_right]
            id_tokens = [w for w in left_tokens if _looks_like_item_code(w.text)]
            if id_tokens:
                first = min(id_tokens, key=lambda w: w.bbox[0])
                # Reject prose/note rows: first code must be close to the material header x-position.
                if abs(first.bbox[0] - material_x) <= max(160.0, page.width * 0.08):
                    anchors.append((ridx, row, id_tokens))
        if not anchors:
            continue

        items: list[LineItem] = []
        pending_note_rows = [r for r in data_rows[:anchors[0][0]] if r.text.strip()]
        our_reference_hint = any(re.search(
            r"\b(?:n\s*/?\s*ref|notre\s+ref(?:erence)?|our\s+ref(?:erence)?)\b",
            ascii_fold(row.text).lower(),
        ) for row in pending_note_rows[-4:])

        def clean_code(value: str) -> str:
            return value.strip("[](){}:;,. |")

        # A vertical table rule is sometimes OCR'd as a leading ``1`` instead
        # of ``|``. Infer that artifact only from a repeated secondary-reference
        # column with a clear dominant numeric length and prefix.
        paired_reference_values = []
        for _, _, tokens in anchors:
            ordered_tokens = sorted(tokens, key=lambda word: word.bbox[0])
            paired_reference_values.extend(
                clean_code(word.text) for word in ordered_tokens[1:]
            )
        numeric_lengths = Counter(
            len(value) for value in paired_reference_values if value.isdigit()
        )
        dominant_reference_length = None
        dominant_reference_prefixes: set[str] = set()
        if numeric_lengths:
            length, support = numeric_lengths.most_common(1)[0]
            if support >= 2:
                dominant_reference_length = length
                dominant_reference_prefixes = {
                    value[:3] for value in paired_reference_values
                    if value.isdigit() and len(value) == length
                }

        def normalize_secondary_reference(value: str) -> str:
            candidate = clean_code(value)
            if (
                dominant_reference_length is not None
                and candidate.isdigit()
                and len(candidate) == dominant_reference_length + 1
                and candidate.startswith("1")
                and candidate[1:4] in dominant_reference_prefixes
            ):
                return candidate[1:]
            return candidate

        repeated_secondary_reference_column = len(paired_reference_values) >= 2
        for aidx, (ridx, anchor_row, id_tokens) in enumerate(anchors):
            next_ridx = anchors[aidx + 1][0] if aidx + 1 < len(anchors) else len(data_rows)
            segment = data_rows[ridx:next_ridx]
            # Stop segment at totals/footer cues.
            trimmed = []
            for r in segment:
                low = ascii_fold(r.text).lower()
                if _is_line_table_stop(low) or any(k in low for k in ["net a payer", "net total", "mt h.t", "grand total", "total ttc", "horaires de reception"]):
                    break
                if re.search(r"\battention\b", low) and sum(
                    _parse_table_number(word.text) is not None for word in r.words
                ) < 2:
                    break
                trimmed.append(r)
            segment = trimmed
            if not segment:
                continue

            first_token = min(id_tokens, key=lambda w: w.bbox[0])
            material = clean_code(first_token.text)
            extra_refs = [
                normalize_secondary_reference(word.text)
                for word in sorted(id_tokens, key=lambda word: word.bbox[0])
                if word is not first_token
            ]
            supplier_ref = None
            if "supplier_material" not in x_by and "customer_material" not in x_by:
                supplier_ref = next((x for x in extra_refs if _looks_like_item_code(x)), None)

            detail_row = None
            best_numeric = -1
            for r in segment:
                # Prefer whichever row carries the numeric detail.  Some compact
                # tables keep code/description/qty/price on the same row, while
                # ERP scans often put the numeric detail on the row after the code.
                nums = 0
                for w in r.words:
                    cx = (w.bbox[0] + w.bbox[2]) / 2
                    if cx >= qty_x - 90 and _parse_table_number(w.text) is not None:
                        nums += 1
                if nums > best_numeric:
                    best_numeric = nums
                    detail_row = r
            if detail_row is None or best_numeric < 2:
                continue

            def nearest_numeric(x: float | None, max_dist: float = 170.0):
                if x is None:
                    return None, None
                candidates = []
                for w in detail_row.words:
                    v = _parse_table_number(w.text)
                    if v is None:
                        continue
                    cx = (w.bbox[0] + w.bbox[2]) / 2
                    d = abs(cx - x)
                    if d <= max_dist:
                        candidates.append((d, w, v))
                if not candidates:
                    return None, None
                _, w, v = min(candidates, key=lambda t: t[0])
                return w, v

            qty_w, qty = nearest_numeric(x_by.get("quantity"), 150.0)
            up_w, unit_price = nearest_numeric(x_by.get("unit_price"), 180.0)
            np_w, net_price = nearest_numeric(x_by.get("net_unit_price"), 190.0)
            gross_unit_price = unit_price if net_price is not None else None
            if net_price is None:
                net_price = unit_price
            else:
                unit_price = net_price

            # Unit token nearest the UOM header.
            uom = _quantity_unit(qty_w.text) if qty_w is not None else None
            if x_by.get("uom") is not None:
                cand = []
                for w in detail_row.words:
                    cx = (w.bbox[0] + w.bbox[2]) / 2
                    if abs(cx - x_by["uom"]) <= 120 and re.fullmatch(r"[A-Za-z]{1,6}", w.text):
                        cand.append((abs(cx - x_by["uom"]), w.text))
                if cand:
                    uom = normalize_unit(min(cand)[1])

            desc_words = []
            for w in detail_row.words:
                cx = (w.bbox[0] + w.bbox[2]) / 2
                if desc_left <= cx <= desc_right:
                    # Exclude a lone unit marker near the quantity column.
                    if w.text.upper() in {"U", "EA", "PC", "PCS"} and cx > desc_right - 170:
                        continue
                    desc_words.append(w)
            description = clean_text(" ".join(w.text for w in desc_words))
            customer_material = None
            line_warnings: list[str] = []
            parsed_detail = _line_from_cells(
                _partition_row(detail_row, cols), detail_row, default_currency
            )
            if parsed_detail is not None:
                material = parsed_detail.material_number or material
                supplier_ref = parsed_detail.supplier_material_number or supplier_ref
                customer_material = (
                    parsed_detail.customer_material_number or customer_material
                )
                description = parsed_detail.description or description
                qty = parsed_detail.quantity if parsed_detail.quantity is not None else qty
                unit_price = (
                    parsed_detail.unit_price
                    if parsed_detail.unit_price is not None else unit_price
                )
                net_price = (
                    parsed_detail.net_unit_price
                    if parsed_detail.net_unit_price is not None else net_price
                )
                gross_unit_price = parsed_detail.gross_unit_price or gross_unit_price
                uom = parsed_detail.uom or uom
                line_warnings.extend(parsed_detail.warnings)

            # In many customer POs, ``Article`` is the customer's short internal
            # code while a repeated reference printed at the start of Designation
            # is the supplier's orderable material. A repeated secondary column or
            # an explicit N/REF / OUR REF note provides enough evidence to type it.
            if (
                supplier_ref
                and "supplier_material" not in x_by
                and "customer_material" not in x_by
                and (repeated_secondary_reference_column or our_reference_hint)
            ):
                customer_material = customer_material or material
                material = supplier_ref
            elif (
                not supplier_ref
                and our_reference_hint
                and material.isdigit()
                and len(material) >= 7
                and (first_token.bbox[0] + first_token.bbox[2]) / 2
                    > material_x + page.width * 0.02
            ):
                supplier_ref = material
            continuation_descriptions = []
            for continuation in segment:
                if continuation is detail_row:
                    continue
                partial = _line_from_cells(
                    _partition_row(continuation, cols), continuation, default_currency
                )
                if (
                    partial is not None
                    and partial.description
                    and not partial.material_number
                    and partial.quantity is None
                    and partial.unit_price is None
                    and partial.line_total is None
                ):
                    continuation_descriptions.append(partial.description)
            if continuation_descriptions:
                description = clean_text(" ".join(
                    [description or "", *continuation_descriptions]
                ))

            date_value = None
            date_word = None
            for w in detail_row.words:
                parsed = parse_date(w.text)
                if parsed:
                    date_value, date_word = parsed, w
                    break
            tax_code = None
            if date_word:
                after = [w for w in detail_row.words if w.bbox[0] > date_word.bbox[2] and re.fullmatch(r"[A-Za-z0-9]{1,4}", w.text)]
                if after:
                    tax_code = min(after, key=lambda w: w.bbox[0]).text

            raw_rows = [r.text for r in segment if r.text.strip()]
            # Notes after the detail row remain attached to this item.
            quote_number = None
            quote_date = None
            customer_ref = None
            line_notes: list[str] = []
            for r in segment:
                qm = re.search(r"(?:offre|quote|devis)\s*n?[°o.]?\s*([A-Z0-9][A-Z0-9._/\-]+)", r.text, flags=re.I)
                if qm:
                    quote_number = qm.group(1)
                    dm = re.search(r"\b(?:du|dated?|date)\s+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})", r.text, flags=re.I)
                    if dm:
                        quote_date = parse_date(dm.group(1))
                    line_notes.append(clean_text(r.text) or r.text)
                rm = re.search(r"n\s*/?\s*ref\s*[:.]?\s*(.+)$", r.text, flags=re.I)
                if rm:
                    customer_ref = clean_text(rm.group(1))
            if aidx == 0:
                for r in pending_note_rows[-3:]:
                    rm = re.search(r"n\s*/?\s*ref\s*[:.]?\s*(.+)$", r.text, flags=re.I)
                    if rm:
                        customer_ref = clean_text(rm.group(1))
                        line_notes.append(clean_text(r.text) or r.text)

            price_for_amount = net_price if net_price is not None else unit_price
            line_amount = (
                round(float(qty) * float(price_for_amount), 6)
                if qty is not None and price_for_amount is not None else None
            )
            all_words = [w for r in segment for w in r.words]
            conf = statistics.fmean([w.confidence for w in all_words]) if all_words else anchor_row.confidence
            bbox = (
                min(w.bbox[0] for w in all_words), min(w.bbox[1] for w in all_words),
                max(w.bbox[2] for w in all_words), max(w.bbox[3] for w in all_words),
            ) if all_words else anchor_row.bbox
            items.append(LineItem(
                material_number=material,
                article_number=material,
                customer_material_number=customer_material,
                supplier_material_number=supplier_ref,
                description=description,
                quantity=qty,
                ordered_quantity=qty,
                uom=uom,
                unit_price=unit_price,
                gross_unit_price=gross_unit_price,
                net_unit_price=net_price,
                currency=default_currency,
                line_total=line_amount,
                line_net_amount=line_amount,
                requested_delivery_date=date_value,
                tax_code=tax_code,
                quote_number=quote_number,
                quote_date=quote_date,
                customer_reference=customer_ref,
                notes=list(dict.fromkeys(x for x in line_notes if x)),
                raw_text=" | ".join(raw_rows),
                page=page.page,
                bbox=bbox,
                confidence=max(0.0, min(1.0, conf * 0.97)),
                warnings=list(dict.fromkeys(line_warnings)),
            ))
        if items:
            return items
    return []


def line_item_set_quality(items: list[LineItem]) -> float:
    """Score competing line-item parsers without relying on a specific template.

    Rewards completeness, arithmetic consistency and plausible identifiers;
    penalizes footer/prose fragments, absurd numeric values and over-segmentation.
    This lets multiline, spatial and legacy parsers compete safely.
    """
    if not items:
        return -1.0
    completeness = []
    arithmetic_ok = 0
    arithmetic_bad = 0
    arithmetic_seen = 0
    desc_ok = 0
    material_ok = 0
    fragment = 0
    absurd = 0
    for x in items:
        core = [x.material_number, x.description, x.quantity, x.unit_price, x.line_total]
        completeness.append(sum(v is not None and v != "" for v in core) / len(core))
        if x.description and any(ch.isalpha() for ch in x.description):
            desc_ok += 1
        mat = (x.material_number or "").strip()
        if mat and len(mat) <= 48 and (" " not in mat or len(mat.split()) <= 2):
            material_ok += 1
        if x.quantity is None and x.unit_price is None and x.line_total is None:
            fragment += 1
        for value in (x.quantity, x.unit_price, x.line_total):
            if value is not None and (abs(float(value)) > 1e9):
                absurd += 1
        price = x.net_unit_price if x.net_unit_price is not None else x.unit_price
        if x.quantity is not None and price is not None and x.line_total is not None:
            arithmetic_seen += 1
            expected = float(x.quantity) * float(price)
            tol = max(0.03, abs(float(x.line_total)) * 0.01)
            if abs(expected - float(x.line_total)) <= tol:
                arithmetic_ok += 1
            else:
                arithmetic_bad += 1
    n = len(items)
    score = 1.25 * (sum(completeness) / n)
    score += 0.22 * (desc_ok / n)
    score += 0.15 * (material_ok / n)
    score += min(n, 8) * 0.025
    score -= 0.45 * (fragment / n)
    score -= min(0.8, absurd * 0.18)
    if arithmetic_seen:
        score += 0.45 * (arithmetic_ok / arithmetic_seen)
        score -= 0.55 * (arithmetic_bad / arithmetic_seen)
    if n > 12:
        score -= min(0.75, (n - 12) * 0.055)
    return round(score, 6)


def choose_best_line_items(*candidates: list[LineItem]) -> list[LineItem]:
    """Return the highest-quality parse, preferring richer parses on near ties."""
    ranked = []
    for idx, items in enumerate(candidates):
        if not items:
            continue
        ranked.append((line_item_set_quality(items), min(len(items), 20), -idx, items))
    if not ranked:
        return []
    ranked.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    return ranked[0][3]


def enhance_totals_spatial(pages: list[PageResult], totals: Totals, default_currency: str = "EUR") -> Totals:
    """Recover totals from compact ERP footer grids using labels + horizontal position."""
    for page in pages:
        rows = _strict_text_rows(page)
        net_to_pay_y = None
        for row in rows:
            folded = ascii_fold(row.text).lower()
            nums = [(w, parse_number(w.text)) for w in row.words]
            nums = [(w, v) for w, v in nums if v is not None]
            if re.search(r"\bmt\s*h\.?\s*t\.?\b", folded) or "montant ht" in folded or "total ht" in folded:
                m = re.search(r"(?:mt\s*h\.?\s*t\.?|montant\s+ht|total\s+ht)\s*[:.]?\s*([0-9][0-9 .]*[,.]\s*\d{2}|[0-9][0-9 .]*)", row.text, flags=re.I)
                value = parse_number(m.group(1)) if m else None
                if value is not None:
                    totals.total_net = value
                    totals.total_before_tax = value
            if re.search(r"\bmt\s*tva\b", folded) or "montant tva" in folded:
                m = re.search(r"(?:mt\s*tva|montant\s+tva)\s*[:.]?\s*([0-9][0-9 .]*[,.]\s*\d{2}|[0-9][0-9 .]*)", row.text, flags=re.I)
                value = parse_number(m.group(1)) if m else None
                if value is not None:
                    totals.total_vat = value
                    totals.total_tax = value
            if "net a payer" in folded or "net à payer" in row.text.lower() or "amount due" in folded:
                net_to_pay_y = _row_center(row)
                vals = [v for w, v in nums if w.bbox[0] > page.width * 0.65]
                if vals:
                    totals.grand_total = vals[-1]
            # Some forms put the amount on the next row while NET A PAYER is on its own row.
            if net_to_pay_y is not None and 0 < _row_center(row) - net_to_pay_y <= 110:
                vals = [v for w, v in nums if w.bbox[0] > page.width * 0.65 and v >= 0]
                if vals:
                    totals.grand_total = max(vals)
        if totals.grand_total is not None:
            totals.amount_due = totals.grand_total
            totals.total_gross = totals.grand_total
            totals.total_after_tax = totals.grand_total
        totals.currency = totals.currency or default_currency
    return totals



def extract_tax_summaries(rows: list[TextRow], totals: Totals) -> list[TaxSummary]:
    out: list[TaxSummary] = []
    for row in rows:
        folded = ascii_fold(row.text).lower()
        if "taux tva" in folded or "vat rate" in folded or "mwst" in folded:
            m = re.search(r"(?:taux\s+tva|vat\s+rate|mwst)\s*[:.]?\s*(\d+(?:[.,]\d+)?)", row.text, flags=re.I)
            if m:
                rate = parse_number(m.group(1))
                if rate is not None:
                    out.append(TaxSummary(type="VAT", rate=rate, taxable_amount=totals.total_net, tax_amount=totals.total_vat))
                    break
    return out

def enhance_commercial_terms(rows: list[TextRow], commercial: CommercialTerms, logistics: Logistics) -> tuple[CommercialTerms, Logistics]:
    for row in rows:
        folded = ascii_fold(row.text).lower()
        if not commercial.payment_terms:
            m = re.search(r"\b(\d{1,3})\s*j(?:ours?)?\s+(?:le\s+)?(\d{1,2})\b", folded)
            if m:
                commercial.payment_terms = f"{m.group(1)} J le {m.group(2)}"
                commercial.payment_due_days = int(m.group(1))
        if not commercial.payment_method and re.search(r"\b(?:virement|virements|bank transfer|uberweisung)\b", folded):
            commercial.payment_method = "VIREMENTS" if "virement" in folded else re.search(r"(?:bank transfer|uberweisung)", row.text, flags=re.I).group(0).strip("\\ ").upper()
        if not commercial.freight_terms and "port franco" in folded:
            commercial.freight_terms = "Port franco"
        if not logistics.delivery_location:
            m = re.search(r"\bdepot\s+([a-zÀ-ÿ._-]+)", folded)
            if m:
                logistics.delivery_location = clean_text(m.group(1)).title()
        if not logistics.delivery_terms and re.search(r"type\s+livraison", folded):
            v = re.split(r"type\s+livraison\.?\s*", row.text, maxsplit=1, flags=re.I)
            if len(v) == 2:
                logistics.delivery_terms = clean_text(v[1].split("(")[0])
    return commercial, logistics


def extract_document_notes(rows: list[TextRow]) -> dict[str, list[str]]:
    notes: dict[str, list[str]] = {}
    delivery: list[str] = []
    active = False
    for row in rows:
        folded = ascii_fold(row.text).lower()
        if "horaires de reception" in folded or folded.strip() in {"attention", "attention !!!"}:
            active = True
        if active:
            if any(k in folded for k in ["paiement acompte", "net a payer", "mt h.t"]):
                active = False
                continue
            txt = clean_text(row.text)
            if txt:
                delivery.append(txt)
    if delivery:
        notes["delivery_instructions"] = delivery[:20]
    return notes

from __future__ import annotations

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
    "description": {"description", "designation", "désignation", "desc", "bezeichnung"},
    "quantity": {"qty", "quantity", "quantite", "quantité", "menge"},
    "uom": {"uom", "unit", "unite", "unité", "me", "einheit"},
    "unit_price": {"price", "prix", "unitprice", "unit-price", "einzelpreis", "preis", "pu", "p.u"},
    "total": {"total", "amount", "montant", "value", "betrag", "extended"},
    "date": {"date", "delivery", "livraison", "lieferdatum"},
}

STOP_WORDS = {"subtotal", "sub total", "sous-total", "sous total", "net total", "total net", "grand total", "total", "vat", "tva", "tax", "taxe", "mwst", "shipping", "freight"}


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
    if not any(re.search(r"\b(?:bon\s+de\s+commande|commande\s+fournisseur|purchase\s+order)\s*$",
                         ascii_fold(row.text).lower()) and row.text == row.text.upper() for row in title_rows):
        return None
    candidates = []
    for row in rows:
        if not row.bbox or row.bbox[1] > page.height * 0.36:
            continue
        dates = [word for word in row.words if re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", word.text)
                 and parse_date(word.text)]
        for date in dates:
            left = [word for word in row.words if word.bbox[2] < date.bbox[0]]
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
    padding = max(4, (y1 - y0) * 0.45)
    crop = image.crop((max(0, int(x0-padding)), max(0, int(y0-padding)),
                       min(image.width, int(x1+padding)), min(image.height, int(y1+padding))))
    cfg = {**ocr_cfg, "languages": ["eng"], "min_word_confidence": 0,
           "timeout_seconds": 5, "single_pass_timeout_seconds": 5}
    readings = []
    errors = []
    for psm in (7, 13):
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
    page.ocr_diagnostics["compact_header_number"] = {
        "original": candidate["raw_text"], "bbox": candidate["bbox"],
        "confirmed": confirmed, "value": values[0] if confirmed else None,
        "confidence": min((reading["confidence"] for reading in readings), default=0.0),
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


def _column_headers(row: TextRow) -> dict[str, float]:
    found: dict[str, list[float]] = {}
    for word in row.words:
        key = _canonical_header(word.text)
        if key:
            found.setdefault(key, []).append((word.bbox[0] + word.bbox[2]) / 2)
    return {k: statistics.fmean(v) for k, v in found.items()}


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
    unit_price = parse_number(cells.get("unit_price"))
    total = parse_number(cells.get("total"))
    material = clean_text(cells.get("material"))
    desc = clean_text(cells.get("description"))
    line_no = clean_text(cells.get("line"))
    # When a description word sits close to the quantity column, nearest-column
    # assignment can place it with the numeric quantity. Recover the text prefix.
    qty_match = re.search(r"[-+]?\d[\d .,'-]*", qty_raw)
    qty = parse_number(qty_match.group(0)) if qty_match else None
    if qty_match:
        prefix = clean_text(qty_raw[:qty_match.start()].strip())
        suffix = clean_text(qty_raw[qty_match.end():].strip())
        spill = clean_text(" ".join(x for x in [prefix, suffix] if x))
        if spill and re.search(r"[A-Za-zÀ-ÿ]", spill):
            desc = clean_text(" ".join(x for x in [desc, spill] if x))
    if qty is None and unit_price is None and total is None and not material:
        return None
    completeness = sum(v is not None and v != "" for v in [material, desc, qty, unit_price, total]) / 5
    confidence = min(1.0, 0.6 * row.confidence + 0.4 * completeness)
    return LineItem(
        line_number=line_no,
        material_number=material,
        description=desc,
        quantity=qty,
        ordered_quantity=qty,
        uom=normalize_unit(cells.get("uom")),
        unit_price=unit_price,
        net_unit_price=unit_price,
        currency=default_currency,
        line_total=total,
        line_net_amount=total,
        requested_delivery_date=parse_date(cells.get("date")),
        raw_text=row.text,
        page=row.page,
        bbox=row.bbox,
        confidence=confidence,
    )


def extract_lines(rows: list[TextRow], default_currency: str = "EUR", min_header_matches: int = 3) -> list[LineItem]:
    lines: list[LineItem] = []
    for i, row in enumerate(rows):
        cols = _column_headers(row)
        if len(cols) < min_header_matches:
            continue
        page = row.page
        for next_row in rows[i+1:]:
            if next_row.page != page:
                break
            lower = ascii_fold(next_row.text).lower()
            if any(lower.startswith(x) for x in STOP_WORDS):
                break
            item = _line_from_cells(_partition_row(next_row, cols), next_row, default_currency)
            if item:
                lines.append(item)
        if lines:
            break

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
    """Assign tokens to table columns using header start positions as boundaries."""
    if not columns:
        return {}
    ordered = sorted(columns.items(), key=lambda kv: kv[1])
    keys=[k for k,_ in ordered]; starts=[x for _,x in ordered]
    buckets={k:[] for k in keys}
    for w in row.words:
        cx=(w.bbox[0]+w.bbox[2])/2
        idx=0
        for i,start in enumerate(starts):
            if cx >= start-12:
                idx=i
            else:
                break
        buckets[keys[idx]].append(w.text)
    return {k:" ".join(v).strip() for k,v in buckets.items()}

def extract_lines_from_pages(pages: list[PageResult], default_currency: str = "EUR", min_header_matches: int = 3) -> list[LineItem]:
    """Spatial table extraction tolerant to multi-row / fragmented OCR headers."""
    out: list[LineItem] = []
    for page in pages:
        words = sorted(page.words, key=lambda w: (((w.bbox[1]+w.bbox[3])/2), w.bbox[0]))
        hits=[]
        for w in words:
            key=_canonical_header(w.text)
            if key:
                hits.append((key,w.bbox[0],(w.bbox[1]+w.bbox[3])/2,w))
        if not hits:
            continue
        # Candidate header bands of up to ~2 text lines.
        best=None
        for _,_,cy,_ in hits:
            band=[h for h in hits if abs(h[2]-cy) <= 55]
            distinct={h[0] for h in band}
            # quantity + price/total are strong table signals.
            strength=len(distinct)+(1 if "quantity" in distinct else 0)+(1 if "unit_price" in distinct or "total" in distinct else 0)
            if len(distinct)>=min_header_matches and (best is None or strength>best[0]):
                best=(strength,band)
        if not best:
            continue
        band=best[1]
        cols={}
        for key in {x[0] for x in band}:
            xs=[x[1] for x in band if x[0]==key]
            cols[key]=statistics.fmean(xs)
        header_bottom=max(x[3].bbox[3] for x in band)
        # Keep words below the header; stop after totals begin.
        data_words=[w for w in words if ((w.bbox[1]+w.bbox[3])/2) > header_bottom+3]
        grouped=group_words_into_rows(data_words,y_factor=0.55)
        for ws in grouped:
            row=TextRow(page=page.page,words=ws)
            low=ascii_fold(row.text).lower().strip()
            if any(low.startswith(x) for x in STOP_WORDS):
                break
            # Ignore repeated headers / prose and require at least one numeric token.
            if sum(1 for w in ws if _canonical_header(w.text)) >= 2:
                continue
            if not re.search(r"\d", row.text):
                continue
            item=_line_from_cells(_partition_row_intervals(row,cols),row,default_currency)
            if item and (item.quantity is not None or item.unit_price is not None or item.line_total is not None):
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
            prev.description = clean_text((prev.description or "") + " " + item.description)
            prev.raw_text = clean_text((prev.raw_text or "") + " " + (item.raw_text or ""))
            prev.confidence = min(prev.confidence, item.confidence)
        else:
            result.append(item)
    return result


def extract_totals(rows: list[TextRow], default_currency: str = "EUR") -> Totals:
    totals = Totals(currency=default_currency)
    patterns = [
        ("grand_total", r"(?:grand\s*total|total\s*(?:ttc|due|commande|order)?|gesamtbetrag|total\s+general)\s*[: ]+([^\n]+)$"),
        ("total_net", r"(?:net\s*total|total\s*ht|netto)\s*[: ]+([^\n]+)$"),
        ("subtotal", r"(?:sub\s*total|sous[- ]?total|zwischensumme)\s*[: ]+([^\n]+)$"),
        ("total_vat", r"(?:vat|tva|mwst)\s*(?:total)?\s*[: ]+([^\n]+)$"),
        ("total_tax", r"(?:tax|taxe)\s*(?:total)?\s*[: ]+([^\n]+)$"),
        ("total_shipping", r"(?:shipping|freight|transport|port)\s*[: ]+([^\n]+)$"),
    ]
    for row in rows:
        folded_row = ascii_fold(row.text).lower()
        legal_vat_identifier = bool(re.search(r"(?:n[°ºo]?|no|numero|number)\s*(?:de\s*)?(?:tva|vat)|tva\s*(?:ic\b|intra)|vat\s*(?:id|no|number|registration)", folded_row, flags=re.I))
        for attr, pattern in patterns:
            if attr in {"total_vat", "total_tax"} and legal_vat_identifier:
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
    t = token.strip("[](){}:;,. ")
    if len(t) < 4 or len(t) > 40:
        return False
    if t.upper() in {"N/REF", "TOTAL", "PAIEMENT", "ACOMPTE"}:
        return False
    has_digit = any(ch.isdigit() for ch in t)
    has_alpha = any(ch.isalpha() for ch in t)
    return has_digit and (has_alpha or len(re.sub(r"\D", "", t)) >= 6) and bool(re.fullmatch(r"[A-Za-z0-9._/\-]+", t))


def extract_multiline_lines_from_pages(pages: list[PageResult], default_currency: str = "EUR", min_header_matches: int = 3) -> list[LineItem]:
    """Segment line-item tables where an article code and its numeric detail live on different OCR rows."""
    for page in pages:
        rows = [TextRow(page=page.page, words=ws) for ws in group_words_into_rows(page.words, y_factor=0.58)]
        header_idx = None
        cols: dict[str, float] = {}
        for i, row in enumerate(rows):
            candidate = _multiword_columns(row)
            distinct = set(candidate)
            if len(distinct) >= min_header_matches and "quantity" in distinct and ("unit_price" in distinct or "net_unit_price" in distinct):
                header_idx, cols = i, candidate
                break
        if header_idx is None:
            continue

        ordered = sorted(cols.items(), key=lambda kv: kv[1])
        x_by = dict(ordered)
        material_x = x_by.get("material", ordered[0][1])
        qty_x = x_by.get("quantity")
        if qty_x is None:
            continue
        # Anchor item codes are expected near the first column, far left of quantity.
        anchor_right = min(qty_x * 0.42, page.width * 0.24)
        # Distances must scale with the page coordinate system. Native PDFs use
        # points (~595 wide) whereas 300-dpi OCR pages are often ~2500 px wide.
        desc_left = material_x + max(12.0, page.width * 0.012)
        desc_right = qty_x - max(10.0, page.width * 0.008)

        data_rows = rows[header_idx + 1:]
        # Find anchors.
        anchors: list[tuple[int, TextRow, list[WordToken]]] = []
        for ridx, row in enumerate(data_rows):
            low = ascii_fold(row.text).lower().strip()
            if any(low.startswith(x) for x in STOP_WORDS) or "net a payer" in low or "net à payer" in row.text.lower():
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
        for aidx, (ridx, anchor_row, id_tokens) in enumerate(anchors):
            next_ridx = anchors[aidx + 1][0] if aidx + 1 < len(anchors) else len(data_rows)
            segment = data_rows[ridx:next_ridx]
            # Stop segment at totals/footer cues.
            trimmed = []
            for r in segment:
                low = ascii_fold(r.text).lower()
                if any(k in low for k in ["net a payer", "net total", "mt h.t", "grand total", "total ttc", "horaires de reception"]):
                    break
                if low.strip() in {"attention", "attention !!!"}:
                    break
                trimmed.append(r)
            segment = trimmed
            if not segment:
                continue

            first_token = min(id_tokens, key=lambda w: w.bbox[0])
            material = first_token.text.strip("[](){}:;,. ")
            extra_refs = [w.text.strip("[](){}:;,. ") for w in sorted(id_tokens, key=lambda w: w.bbox[0]) if w is not first_token]
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
                    if cx >= qty_x - 90 and parse_number(w.text) is not None:
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
                    v = parse_number(w.text)
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
            if net_price is None:
                net_price = unit_price

            # Unit token nearest the UOM header.
            uom = None
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
            line_amount = qty * price_for_amount if qty is not None and price_for_amount is not None else None
            all_words = [w for r in segment for w in r.words]
            conf = statistics.fmean([w.confidence for w in all_words]) if all_words else anchor_row.confidence
            bbox = (
                min(w.bbox[0] for w in all_words), min(w.bbox[1] for w in all_words),
                max(w.bbox[2] for w in all_words), max(w.bbox[3] for w in all_words),
            ) if all_words else anchor_row.bbox
            items.append(LineItem(
                material_number=material,
                article_number=material,
                supplier_material_number=supplier_ref,
                description=description,
                quantity=qty,
                ordered_quantity=qty,
                uom=uom,
                unit_price=unit_price,
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

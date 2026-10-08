from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import uuid
import statistics

from po_ocr.confidence import score
from po_ocr.address_roles import build_business_addresses
from po_ocr.extract import (
    all_rows, extract_extra_fields, extract_header, extract_lines, extract_lines_from_pages, extract_parties, extract_totals,
    extract_spatial_header_fields, enhance_parties_spatial, extract_multiline_lines_from_pages, enhance_totals_spatial,
    enhance_commercial_terms, extract_document_notes, extract_tax_summaries, choose_best_line_items,
)
from po_ocr.ingest import describe_file
from po_ocr.models import DocumentMeta, PurchaseOrderHeader, PurchaseOrderResult
from po_ocr.validate import validate


def extract_purchase_order(path: str | Path, pages, config: dict) -> PurchaseOrderResult:
    fd = describe_file(path)
    config_layout = config["layout"]
    rows = all_rows(pages, y_factor=float(config_layout.get("line_y_tolerance_factor", 0.65)))
    header_fields, commercial, logistics = extract_header(
        rows, config["normalization"].get("default_currency", "EUR")
    )
    header_fields = extract_spatial_header_fields(pages, header_fields) if config_layout.get("spatial_header_recovery", True) else header_fields
    currency = (
        header_fields.get("currency").value if header_fields.get("currency") else None
    ) or config["normalization"].get("default_currency", "EUR")
    parties = extract_parties(rows)
    if config_layout.get("spatial_party_segmentation", True):
        parties = enhance_parties_spatial(pages, parties)
    commercial, logistics = enhance_commercial_terms(rows, commercial, logistics)
    min_headers = int(config["layout"].get("min_table_header_matches", 3))
    segmented = extract_multiline_lines_from_pages(pages, default_currency=currency, min_header_matches=min_headers) if config_layout.get("multiline_item_segmentation", True) else []
    spatial = extract_lines_from_pages(pages, default_currency=currency, min_header_matches=min_headers)
    legacy = extract_lines(rows, default_currency=currency, min_header_matches=min_headers)
    lines = choose_best_line_items(segmented, spatial, legacy)
    totals = extract_totals(rows, default_currency=currency)
    if config_layout.get("spatial_totals_recovery", True):
        totals = enhance_totals_spatial(pages, totals, default_currency=currency)

    po_header = PurchaseOrderHeader()
    for key, value in header_fields.items():
        if hasattr(po_header, key):
            setattr(po_header, key, value)

    known_values = [f.raw_value for f in header_fields.values()]
    known_values.extend([p.name for p in parties.values()])
    extras = extract_extra_fields(rows, known_values)

    page_types = [p.source_type for p in pages]
    is_pdf = fd.extension == "pdf"
    is_image = fd.extension in {"png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"}
    is_native = is_pdf and bool(page_types) and all(k == "native_pdf" for k in page_types)
    is_scan = bool(page_types) and all(k in {"scan_ocr", "image_ocr"} for k in page_types)
    is_mixed = is_pdf and len(set(page_types)) > 1
    any_ocr = any(k in {"scan_ocr", "image_ocr"} for k in page_types)

    meta = DocumentMeta(
        id=str(uuid.uuid4()), filename=fd.filename, mime_type=fd.mime_type, extension=fd.extension,
        file_size=fd.file_size, sha256=fd.sha256, page_count=len(pages), is_pdf=is_pdf, is_image=is_image,
        is_scan=is_scan, is_native_pdf=is_native, is_mixed_pdf=is_mixed,
        has_text_layer=is_native or is_mixed or any(k == "structured_file" for k in page_types),
        has_images=is_scan or is_mixed or is_image,
        ocr_required=any_ocr,
        ocr_engine=config["ocr"].get("engine") if any_ocr else None,
        processing_timestamp=datetime.now(timezone.utc).isoformat(),
        model_version=config["engine"].get("version", "1.0.0"),
    )

    result = PurchaseOrderResult(
        document=meta,
        purchase_order=po_header,
        buyer=parties.get("buyer"), supplier=parties.get("supplier"),
        ship_to=parties.get("ship_to"), bill_to=parties.get("bill_to"),
        sold_to=parties.get("sold_to"), deliver_to=parties.get("deliver_to"),
        invoice_to=parties.get("invoice_to"), payer=parties.get("payer"),
        end_customer=parties.get("end_customer"), consignee=parties.get("consignee"),
        ship_from=parties.get("ship_from"),
        commercial=commercial, logistics=logistics, lines=lines, totals=totals, taxes=extract_tax_summaries(rows, totals), notes=extract_document_notes(rows),
        extra_fields=extras, pages=pages if config["engine"].get("keep_page_words", True) else [],
    )
    result.business_addresses = build_business_addresses(result, pages, config)
    result = validate(result, config["validation"])
    result = score(result, {**config["confidence"], "manual_review_threshold": config["ocr"].get("manual_review_threshold", 0.80)})
    return result

# --- V4.3 multi-page native-table + page-layout recovery -----------------------

def _fold(value: str | None) -> str:
    from po_ocr.normalize import ascii_fold
    return " ".join(ascii_fold(value or "").casefold().split())


def _bad_party_name(value: str | None) -> bool:
    import re
    raw=" ".join(str(value or "").split()).strip()
    low = _fold(raw)
    if not low:
        return True
    bad = ("n° commande", "no commande", "date n°", "code article", "total h.t", "conditions generales",
           "code fournisseur", "facture a", "facture à", "adresse de livraison", "produit fini", "mode livraison")
    if len(low) > 100 or any(x in low for x in bad) or "@" in raw:
        return True
    if re.fullmatch(r"[+0-9 .()/-]{7,}", raw):
        return True
    if re.match(r"^\d+\s+(?:rue|avenue|route|impasse|boulevard|chemin)\b", low):
        return True
    if re.fullmatch(r"\d{5}\s+.+", raw):
        return True
    return False


def _make_table_field(value, page: int | None, *, kind: str = "text", confidence: float = 0.99):
    from po_ocr.models import ExtractedField, Evidence
    from po_ocr.normalize import parse_date
    raw = str(value).strip()
    normalized = parse_date(raw) if kind == "date" else raw
    if kind == "date" and normalized is None:
        normalized = raw
    return ExtractedField(
        value=normalized, raw_value=raw, normalized_value=normalized,
        ocr_confidence=confidence, semantic_confidence=0.99,
        final_confidence=min(1.0, 0.55 * confidence + 0.45 * 0.99),
        validation_status="NOT_CHECKED",
        evidence=Evidence(page=page, source_text=raw, extraction_method="native_table_anchor"),
    )


def _split_cell_lines(value) -> list[str]:
    if value is None:
        return []
    text = str(value).replace("\r", "\n")
    return [" ".join(x.split()) for x in text.split("\n") if " ".join(x.split())]


def _parse_ship_to_cell(value: str):
    from po_ocr.models import Party, Address
    import re
    lines = _split_cell_lines(value)
    # Drop the label itself when the cell contains label + value.
    if lines and _fold(lines[0]).startswith("adresse de livraison"):
        lines = lines[1:]
    party = Party()
    if lines:
        party.name = lines[0]
        addr = lines[1:]
        if addr:
            party.address = Address(line1=addr[0], line2=addr[1] if len(addr) > 1 else None,
                                    line3=addr[2] if len(addr) > 2 else None)
            joined = " ".join(addr)
            m = re.search(r"\b(\d{4,6})\s+([A-Za-zÀ-ÿ .'-]{2,})", joined)
            if m:
                party.address.postal_code = m.group(1)
                party.address.city = " ".join(m.group(2).split())
    return party


def _extract_native_table_header_fields(po, tables):
    """Use aligned native PDF table cells before falling back to flattened text."""
    import re
    for table in tables:
        data = table.data or []
        for r_idx, row in enumerate(data):
            next_row = data[r_idx + 1] if r_idx + 1 < len(data) else []
            for c_idx, cell in enumerate(row):
                label = _fold(str(cell) if cell is not None else "")
                if not label:
                    continue
                below = next_row[c_idx] if c_idx < len(next_row) else None
                below_text = " ".join(_split_cell_lines(below))
                page = table.source.page
                if ("n° commande" in label or "no commande" in label or label in {"commande n°", "commande no"}) and below_text:
                    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/\-]{4,39}", below_text):
                        po.purchase_order.number = _make_table_field(below_text, page)
                elif label == "date" and below_text:
                    from po_ocr.normalize import parse_date
                    if parse_date(below_text):
                        po.purchase_order.order_date = _make_table_field(below_text, page, kind="date")
                elif ("n° client" in label or "no client" in label or "customer no" in label) and below_text:
                    po.purchase_order.account_number = _make_table_field(below_text, page)
                elif "adresse de livraison" in label:
                    # Sometimes the address is in the same merged cell, sometimes below.
                    raw = str(cell or "")
                    candidate = raw if len(_split_cell_lines(raw)) > 1 else str(below or "")
                    ship = _parse_ship_to_cell(candidate)
                    if ship.name:
                        po.ship_to = ship
                elif label.startswith("correspondant"):
                    raw = str(cell or "")
                    m = re.search(r"correspondant\s*:\s*(.+)", raw, flags=re.I)
                    if m:
                        po.ship_to.contact.name = " ".join(m.group(1).split())
    return po


def _make_spatial_field(value, page: int | None, bbox=None, *, kind: str = "text", confidence: float = 0.985, method: str = "spatial_anchor"):
    from po_ocr.models import ExtractedField, Evidence
    from po_ocr.normalize import parse_date
    raw = " ".join(str(value).split())
    normalized = parse_date(raw) if kind == "date" else raw
    if kind == "date" and normalized is None:
        normalized = raw
    return ExtractedField(
        value=normalized, raw_value=raw, normalized_value=normalized,
        ocr_confidence=confidence, semantic_confidence=0.985,
        final_confidence=min(1.0, 0.55 * confidence + 0.45 * 0.985),
        validation_status="NOT_CHECKED",
        evidence=Evidence(page=page, bbox=bbox, source_text=raw, extraction_method=method),
    )


def _bbox_union(words):
    if not words:
        return None
    return (min(w.bbox[0] for w in words), min(w.bbox[1] for w in words), max(w.bbox[2] for w in words), max(w.bbox[3] for w in words))


def _enhance_order_header_from_page_layout(po, pages, config: dict):
    """Recover split order identifiers/dates using page geometry.

    Handles ERP headers where a label is in one row/column and an alphanumeric value
    such as ``ST C CSP 33443`` is printed on the following row while supplier address
    text occupies the same horizontal band on the right.
    """
    from po_ocr.extract import all_rows
    from po_ocr.normalize import parse_date
    import re
    if not pages:
        return po
    page = pages[0]
    rows = all_rows([page], y_factor=0.55)
    width, height = page.width, page.height
    layout_cfg = config.get("layout", {}) if isinstance(config, dict) else {}
    left_ratio = float(layout_cfg.get("header_order_number_left_region_max_x_ratio", 0.48))
    max_gap = float(layout_cfg.get("header_order_number_max_row_gap_ratio", 0.055)) * height
    max_tokens = int(layout_cfg.get("header_order_number_max_tokens", 8))

    anchor_idx = None
    anchor_y = None
    for idx, row in enumerate(rows):
        low = _fold(row.text)
        if _row_center_safe(row) > height * 0.42:
            break
        if re.search(r"\b(?:commande\s+client|customer\s+order|pour\s+la\s+commande)\b", low):
            continue
        if re.search(r"\bcommande\b", low) and ("n°" in row.text.casefold() or "nº" in row.text.casefold() or re.search(r"\b(?:no|n)\b", low)):
            anchor_idx = idx
            anchor_y = _row_center_safe(row)
            break

    if anchor_idx is not None and (not po.purchase_order.number.value or float(po.purchase_order.number.final_confidence or 0.0) < 0.97):
        best = None
        for row in rows[anchor_idx: anchor_idx + 5]:
            cy = _row_center_safe(row)
            if anchor_y is not None and cy - anchor_y > max_gap:
                break
            words = [w for w in row.words if w.bbox[0] < width * left_ratio]
            if not words:
                continue
            # Remove the label itself when evaluating the anchor row.
            cand_words = [w for w in words if _fold(w.text) not in {"commande", "n°", "nº", "no", "n"}]
            text = " ".join(w.text for w in cand_words).strip(" :-")
            if not text or len(cand_words) > max_tokens or not re.search(r"\d", text):
                continue
            if re.search(r"\b(?:tel|fax|mail|adresse|livraison|client|page)\b", _fold(text)):
                continue
            if parse_date(text):
                continue
            # Multi-token identifiers or compact alphanumeric IDs are preferred over
            # postal codes and other isolated numbers.
            alpha = bool(re.search(r"[A-Za-z]", text))
            score = (2.0 if alpha else 0.0) + min(2.0, len(cand_words) * 0.35) + (1.0 if len(text) >= 7 else 0.0)
            if re.fullmatch(r"\d{4,6}", text):
                score -= 3.0
            if best is None or score > best[0]:
                best = (score, text, cand_words)
        if best and best[0] >= 2.0:
            po.purchase_order.number = _make_spatial_field(best[1], page.page, _bbox_union(best[2]), method="multiblock_order_number")

    # Header date recovery: only inspect the compact transaction header zone, never
    # item rows or legal/footer dates.
    if not po.purchase_order.order_date.value:
        date_re = re.compile(r"\b(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b")
        candidates = []
        for row in rows:
            cy = _row_center_safe(row)
            if cy > height * 0.34:
                break
            for m in date_re.finditer(row.text):
                raw = m.group(1)
                norm = parse_date(raw)
                if norm:
                    score = 1.0
                    low = _fold(row.text)
                    if re.search(r"\ble\s+" + re.escape(_fold(raw)), low):
                        score += 1.0
                    if "date" in low:
                        score += 1.2
                    # Prefer dates near but above the PO number/title region.
                    if anchor_y is not None:
                        score += max(0.0, 1.0 - abs(cy - anchor_y) / max(1.0, height * 0.18))
                    words = [w for w in row.words if raw in w.text or w.text in raw]
                    candidates.append((score, raw, words or row.words))
        if candidates:
            _, raw, words = max(candidates, key=lambda x: x[0])
            po.purchase_order.order_date = _make_spatial_field(raw, page.page, _bbox_union(words), kind="date", method="header_date_anchor")

    # Internal reference frequently appears as N/REF: XXX.
    if not po.purchase_order.internal_number.value:
        for row in rows:
            if _row_center_safe(row) > height * 0.38:
                break
            m = re.search(r"\bN\s*/\s*REF\s*[:.-]?\s*([A-Z0-9._/-]{2,40})", row.text, flags=re.I)
            if m:
                po.purchase_order.internal_number = _make_spatial_field(m.group(1), page.page, row.bbox, method="internal_reference_anchor")
                break
    return po


def _normalize_inline_po_number(raw: str, config: dict | None = None) -> tuple[str | None, str | None]:
    """Normalize a transaction identifier while preserving meaningful alpha groups.

    Examples:
      ``FBC27834`` -> ``FBC27834``
      ``193 444 / Nice`` -> (``193444``, ``Nice``)
      ``ST C CSP 33443`` -> ``ST C CSP 33443``

    The optional second return value is a site/suffix printed after a slash.  We do not
    silently append it to the purchase-order number because it is usually a location,
    branch or operational qualifier rather than part of the identifier itself.
    """
    import re
    cfg=(config or {}).get("layout", {}) if isinstance(config, dict) else {}
    text=" ".join(str(raw or "").replace("\u00a0", " ").split()).strip(" :-")
    if not text:
        return None, None
    # Flattened two-column headers can place the next semantic heading on the
    # same extracted row: ``COMMANDE FOURNISSEUR : 72 475 Adresse de
    # livraison``.  The heading is a cell boundary, never part of the PO ID.
    text=re.split(
        r"\s+(?=(?:ADRESSE\s+(?:DE\s+)?LIVRAISON|LIEU\s+DE\s+LIVRAISON|"
        r"SHIP\s+TO|DELIVERY\s+ADDRESS|ADRESSE\s+DE\s+FACTURATION)\b)",
        text,
        maxsplit=1,
        flags=re.I,
    )[0].strip()
    # Strip a trailing date context first.
    text=re.sub(r"\s+(?:au|du|le|date)\s+\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\s*$", "", text, flags=re.I)
    # Split an operational suffix such as "/ Nice" when the right side is not itself
    # a numeric identifier fragment.
    site=None
    m=re.match(r"^(.*?)(?:\s*/\s*)([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 .'-]{1,29})$", text)
    if m:
        text=m.group(1).strip()
        site=m.group(2).strip()
    # Remove common label remnants.
    text=re.sub(r"^(?:n[°ºo]?|no|number|num(?:ero|éro)?)\s*[:#.-]?\s*", "", text, flags=re.I)
    text=text.strip(" :-")
    if not text:
        return None, site

    # Numeric identifiers are often typeset in visual groups: 193 444 -> 193444.
    parts=text.split()
    if cfg.get("inline_order_number_normalize_digit_groups", True) and len(parts)>1 and all(re.fullmatch(r"\d{1,6}", p) for p in parts):
        text="".join(parts)
    else:
        # Preserve meaningful alpha groups but normalize excessive whitespace.
        text=" ".join(parts)

    min_digits=int(cfg.get("inline_order_number_min_digits", 3))
    max_chars=int(cfg.get("inline_order_number_max_chars", 48))
    if len(text)>max_chars or sum(ch.isdigit() for ch in text)<min_digits:
        return None, site
    if re.search(r"\b(?:tel|fax|siret|tva|page|total|conditionnement|poids|volume)\b", _fold(text)):
        return None, site
    return text, site


def _enhance_inline_transaction_header_v46(po, pages, config: dict):
    """Grammar-based recovery for compact transaction headers and labelled metadata.

    This handles layouts where order number/date/code/contact are printed on the same
    physical line rather than in a table.  It is intentionally layout-agnostic and only
    scans the transaction header zone of the first page.
    """
    from po_ocr.extract import all_rows
    from po_ocr.normalize import parse_date
    import re
    if not pages or not config.get("layout", {}).get("inline_transaction_header_recovery", True):
        return po
    pg=pages[0]
    rows=all_rows([pg], y_factor=0.50)
    max_y=pg.height*0.48

    for row in rows:
        if _row_center_safe(row)>max_y:
            break
        text=row.text
        low=_fold(text)

        # Commande N° FBC27834 au 17/08/26
        # COMMANDE FOURNISSEUR : 193 444 / Nice
        if ("commande" in low or re.search(r"\bcde\b", low)) and not po.purchase_order.number.value:
            m=re.search(
                r"(?:COMMANDE(?:\s+FOURNISSEUR)?|BON\s+DE\s+COMMANDE|CDE)"
                r"\s*(?:N[°ºO]?|NO|#)?\s*[:.-]?\s*(.+)$",
                text, flags=re.I,
            )
            if m:
                candidate=m.group(1)
                # Remove an inline date without discarding a site qualifier.
                dm=re.search(r"\b(?:AU|DU|LE)\s+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b", candidate, flags=re.I)
                if dm:
                    candidate=candidate[:dm.start()].strip()
                    if not po.purchase_order.order_date.value and parse_date(dm.group(1)):
                        po.purchase_order.order_date=_make_spatial_field(dm.group(1), pg.page, row.bbox, kind="date", method="inline_transaction_header")
                number, site=_normalize_inline_po_number(candidate, config)
                if number:
                    po.purchase_order.number=_make_spatial_field(number, pg.page, row.bbox, method="inline_transaction_header")
                    if site and not po.purchase_order.project_name.value:
                        po.purchase_order.project_name=_make_spatial_field(site, pg.page, row.bbox, method="inline_order_site_suffix", confidence=0.94)

        # Date commande : 06/08/2026
        if not po.purchase_order.order_date.value:
            dm=re.search(r"\bDATE\s+(?:DE\s+)?COMMANDE\s*[:.-]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b", text, flags=re.I)
            if not dm:
                # A plain ``Date`` line in the transaction-header zone is the
                # document date. Delivery dates use their own qualified label
                # and therefore do not match this full-line grammar.
                dm=re.fullmatch(r"\s*DATE\s*[:.-]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*", text, flags=re.I)
            if dm and parse_date(dm.group(1)):
                method="labeled_order_date" if "commande" in low else "standalone_header_date"
                po.purchase_order.order_date=_make_spatial_field(dm.group(1), pg.page, row.bbox, kind="date", method=method)

        # Code fournisseur : 20 501 -> 20501
        cm=re.search(r"\bCODE\s+FOURNISSEUR\s*[:.-]?\s*([0-9][0-9 ]{1,20})", text, flags=re.I)
        if cm:
            code="".join(re.findall(r"\d+", cm.group(1)))
            if 2<=len(code)<=20:
                po.supplier.code=code

        # Acheteur : THAON Gabriel - Tél ...
        am=re.search(r"\bACHETEUR\s*[:.-]?\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]{2,60}?)(?=\s+-\s+T[eé]l|\s+T[eé]l|$)", text, flags=re.I)
        if am:
            po.buyer.contact.name=" ".join(am.group(1).split())
            tm=re.search(r"\bT[eé]l\s*[:.-]?\s*([0-9][0-9 .-]{7,})", text[am.end():], flags=re.I)
            if tm:
                po.buyer.contact.phone=" ".join(tm.group(1).split()).strip(" -")

        # Réf : Cde Auto : NICE / **OMEO
        if not po.purchase_order.internal_number.value:
            rm=re.search(r"\bR[eé]f(?:[eé]rence)?\s*[:.-]\s*(.+?)(?=\s+Document\s+cr[eé][eé]|$)", text, flags=re.I)
            if rm:
                val=" ".join(rm.group(1).split()).strip(" *:-")
                if 1<len(val)<=80:
                    po.purchase_order.internal_number=_make_spatial_field(val, pg.page, row.bbox, method="labeled_internal_reference", confidence=0.97)

        # Port : Livre is logistics, not a monetary freight amount.
        pm=re.search(r"\bPORT\s*[:.-]?\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 .'-]{1,40})", text, flags=re.I)
        if pm and not re.search(r"\d+[,.]\d+", pm.group(1)):
            po.logistics.shipping_method=" ".join(pm.group(1).split())

    # Landscape purchase orders often place the metadata block below 48% of
    # page height. Scan farther only for the unambiguous full-line Date grammar;
    # the broader transaction-header rules above deliberately keep their tighter
    # header-zone boundary.
    if not po.purchase_order.order_date.value:
        for row in rows:
            if _row_center_safe(row)>pg.height*0.72: break
            dm=re.fullmatch(r"\s*DATE\s*[:.-]?\s*(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*",row.text,flags=re.I)
            if dm and parse_date(dm.group(1)):
                po.purchase_order.order_date=_make_spatial_field(dm.group(1),pg.page,row.bbox,kind="date",method="standalone_header_date")
                break

    # Split label/value columns: a short business reference can be printed below a
    # generic "Référence" label while another unrelated label shares the same row.
    if not po.purchase_order.internal_number.value:
        for idx,row in enumerate(rows):
            low=_fold(row.text)
            if _row_center_safe(row)>pg.height*0.50: break
            ref_words=[w for w in row.words if _fold(w.text) in {"reference","ref"}]
            if not ref_words:
                continue
            ref_word=ref_words[0]
            # The next label to the right defines the local value column.  This works
            # even when both labels happen to sit in the left half of the page.
            right_labels=[w for w in row.words if w.bbox[0]>ref_word.bbox[2]+10]
            right_boundary=(ref_word.bbox[2]+min(w.bbox[0] for w in right_labels))/2 if right_labels else pg.width*0.33
            for rr in rows[idx+1:idx+4]:
                left=" ".join(w.text for w in rr.words if w.bbox[2]<right_boundary).strip(" *:-")
                if not left: continue
                if len(left)<=80 and not re.search(r"\b(?:date|livraison|tel|fax)\b",_fold(left)):
                    po.purchase_order.internal_number=_make_spatial_field(left,pg.page,rr.bbox,method="below_label_internal_reference",confidence=0.96)
                    break
            if po.purchase_order.internal_number.value: break

    # Commercial offer/quotation references can appear on a continuation page.
    if not po.purchase_order.quote_number.value:
        for p in pages:
            for row in all_rows([p], y_factor=0.50):
                qm=re.search(r"(?:offre\s+de\s+prix|offre|devis|quotation|quote)\s*(?:n[°ºo]?|no|#)?\s*[:.-]?\s*([A-Za-z0-9._/-]{3,50})",row.text,flags=re.I)
                if qm:
                    po.purchase_order.quote_number=_make_spatial_field(qm.group(1),p.page,row.bbox,method="quote_reference_anchor",confidence=0.96)
                    break
            if po.purchase_order.quote_number.value: break

    # A legal corporate header in the first 18% is a stronger buyer identity than a
    # truncated token such as "SAS" recovered by generic spatial segmentation.
    if not po.buyer.name or len(po.buyer.name.split())<=1:
        legal_re=re.compile(r"^(?:SAS|SARL|SA|SASU|EURL|SNC)\s+[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 &'._-]{2,80}$", re.I)
        for row in rows:
            if _row_center_safe(row)>pg.height*0.18:
                break
            candidate=" ".join(row.text.split())
            if legal_re.match(candidate):
                po.buyer.name=candidate; po.buyer.legal_name=candidate
                break
    return po


def _extract_geometry_table_items_v46(pages, config: dict, default_currency: str="EUR"):
    """Recover line items from visible PDF columns even when no native table object exists."""
    from po_ocr.extract import all_rows
    from po_ocr.models import LineItem, AdditionalCharge, Evidence
    from po_ocr.normalize import parse_number
    import re

    cfg=config.get("layout", {}) if isinstance(config, dict) else {}
    if not cfg.get("geometry_table_recovery", True):
        return [], []
    items=[]; charges=[]
    min_terms=int(cfg.get("geometry_table_header_min_terms", 4))
    min_digits=int(cfg.get("geometry_table_min_material_digits", 5))
    max_mat=int(cfg.get("geometry_table_max_material_chars", 30))
    stop_terms=tuple(_fold(x) for x in cfg.get("geometry_table_stop_terms", ["total ht","total net","total ttc","montant ht :"]))

    def center(w): return (w.bbox[0]+w.bbox[2])/2
    def header_token(text):
        from difflib import get_close_matches
        token=_fold(text).strip("|[](){}.,:")
        known=("reference","article","designation","description","quantite","montant")
        if len(token)>=5 and token not in known:
            close=get_close_matches(token,known,n=1,cutoff=0.84)
            if close: return close[0]
        return token
    def number_from_words(ws):
        if not ws: return None
        raw=" ".join(w.text for w in ws).strip()
        return parse_number(raw)

    for pg in pages:
        rows=all_rows([pg], y_factor=0.50)
        header_idx=None; anchors={}
        for idx,row in enumerate(rows):
            if _row_center_safe(row)>pg.height*float(cfg.get("geometry_table_max_header_y_ratio", 0.68)):
                continue
            low=" ".join(header_token(w.text) for w in row.words)
            hits=sum(bool(re.search(pattern, low)) for pattern in (
                r"\b(?:reference|article|sku)\b", r"\b(?:designation|description)\b",
                r"\b(?:quantite|qte|qty)\b", r"\b(?:pu|px|prix|price|net)\b", r"\b(?:montant|mnt|amount)\b"))
            if hits<min_terms:
                continue
            # Derive anchors from actual header word geometry.
            for w in row.words:
                wl=header_token(w.text)
                if ("reference" in wl or wl in {"article", "sku"}) and "reference" not in anchors: anchors["reference"]=center(w)
                if "adressage" in wl or ("code" in wl and "reference" not in wl): anchors.setdefault("secondary", center(w))
                if "designation" in wl or "description" in wl: anchors["description"]=center(w)
                if "quantite" in wl or wl in {"qte","qty"}: anchors["quantity"]=center(w)
                # P.A (prix d'achat) is a common ERP label for the net unit
                # purchase price. Keep the dotted form because ``header_token``
                # intentionally preserves punctuation inside an OCR token.
                if wl in {"pu","px","prix","pa","p.a"} or "unitaire" in wl: anchors.setdefault("price", center(w))
                if wl == "net" and "price" in anchors and center(w)>anchors["price"]:
                    anchors["price"]=(anchors["price"]+center(w))/2
                elif wl == "net": anchors.setdefault("price", center(w))
                if "montant" in wl or "amount" in wl or wl=="mnt": anchors["amount"]=center(w)
                if wl in {"unite", "uom", "unit"}: anchors["uom"]=center(w)
                if wl in {"ua", "up"}: anchors[wl]=center(w)
                if wl=="ref" and "interne" in low: anchors["internal_reference"]=center(w)
            if "quantity" not in anchors and all(k in anchors for k in ("reference","description","price","amount")):
                # OCR can lose a quantity label while repeated "2,000 PCE" cells
                # remain readable. Require several aligned unit/quantity pairs.
                pairs=[]
                for rr in rows[idx+1:idx+35]:
                    for wi,word in enumerate(rr.words[1:],1):
                        if not re.fullmatch(r"[|\[\s]*(?:PIEC(?:E|ES)?|PCE|PCS|EA|KG|BOX|L|M)[|\]\s]*",word.text,re.I):
                            continue
                        previous=rr.words[wi-1]
                        if re.fullmatch(r"\d[\d.,]*",previous.text) and 0<=word.bbox[0]-previous.bbox[2]<=pg.width*0.035:
                            pairs.append((center(previous),center(word)))
                if len(pairs)>=2 and max(x for x,u in pairs)-min(x for x,u in pairs)<pg.width*0.03:
                    anchors["quantity"]=statistics.median(x for x,u in pairs)
                    anchors["uom"]=statistics.median(u for x,u in pairs)
            if all(k in anchors for k in ("quantity","price","amount")):
                header_idx=idx; break
        if header_idx is None:
            continue

        # Boundaries are midway between numeric anchors. Left columns remain flexible.
        qx,px,ax=anchors["quantity"],anchors["price"],anchors["amount"]
        # Item descriptions commonly extend far beyond the header label itself.  Use a
        # narrow guard immediately before the quantity anchor rather than the midpoint
        # between DESCRIPTION and QUANTITY, otherwise trailing brand/model words can be
        # mistaken for the quantity column.
        q_left=qx-max(20.0, pg.width*0.06)
        qp=(qx+px)/2; pa=(px+ax)/2
        ref_x=anchors.get("reference", 30.0)
        desc_x=anchors.get("description", q_left-30)
        sec_x=anchors.get("secondary")
        sec_desc_boundary=(sec_x+desc_x)/2 if sec_x is not None else desc_x-12
        current=None
        for row in rows[header_idx+1:]:
            low=_fold(row.text)
            if any(term and term in low for term in stop_terms) or re.search(r"\btotal\s+h\.?\s*t\.?(?=\s|$)|\b(?:montant\s+ht|total\s+(?:ht|net|ttc))\s*[:=]",low):
                break
            if _row_center_safe(row)>pg.height*0.90:
                break
            left=[w for w in row.words if center(w)<q_left]
            qws=[w for w in row.words if q_left<=center(w)<qp]
            pws=[w for w in row.words if qp<=center(w)<pa]
            aws=[w for w in row.words if center(w)>=pa]
            qty=number_from_words(qws); price=number_from_words(pws); amount=number_from_words(aws)

            # Numeric columns in many print engines are right-aligned and can straddle
            # a midpoint (e.g. ``1,75`` starts just inside the amount region).  Rebuild
            # the ordered numeric groups from geometry and map them left-to-right to
            # quantity, unit price and line amount.  Thousand-separated amounts such as
            # ``3 640,00`` are kept as one group because their word boxes almost touch.
            numeric_words=[]
            for w in row.words:
                if center(w)<q_left:
                    continue
                token=w.text.strip().replace("€","")
                if re.fullmatch(r"[-+]?\d[\d.,']*", token):
                    numeric_words.append(w)
            groups=[]
            for w in numeric_words:
                if not groups:
                    groups.append([w]); continue
                gap=w.bbox[0]-groups[-1][-1].bbox[2]
                # Small gaps are thousands-group spacing, not a new numeric column.
                if gap <= max(5.0, pg.width*0.012):
                    groups[-1].append(w)
                else:
                    groups.append([w])
            parsed_groups=[(number_from_words(g), statistics.fmean(center(w) for w in g)) for g in groups]
            parsed_groups=[(v,x) for v,x in parsed_groups if v is not None]
            if len(parsed_groups)==3:
                qty,price,amount=[v for v,x in parsed_groups]
            elif len(parsed_groups)>3:
                # A gross/list price or discount can occupy an extra column. Keep
                # the visible quantity/net-price/amount anchors authoritative.
                from itertools import combinations
                quantity_idx=min(range(len(parsed_groups)),key=lambda i:abs(parsed_groups[i][1]-qx))
                choices=[ids for ids in combinations(range(len(parsed_groups)),3) if ids[0]==quantity_idx]
                consistent=[ids for ids in choices if abs(parsed_groups[ids[0]][0]*parsed_groups[ids[1]][0]-parsed_groups[ids[2]][0])
                            <=max(0.04,abs(parsed_groups[ids[0]][0]*parsed_groups[ids[1]][0])*0.006)]
                if consistent:
                    choices=consistent
                if not choices:
                    continue
                qi,pi,ai=min(choices,key=lambda ids: sum(abs(parsed_groups[i][1]-anchor)
                            for i,anchor in zip(ids,(qx,px,ax))))
                qty,price,amount=[parsed_groups[i][0] for i in (qi,pi,ai)]

            # A wrapped continuation line has no right-side numerical columns.
            if qty is None and price is None and amount is None:
                if current is not None and left:
                    cont_words=[w.text for w in left if sec_x is None or center(w)>=sec_desc_boundary]
                    cont=" ".join(cont_words).strip()
                    if cont in {current.material_number,current.supplier_material_number}:
                        continue
                    if cont and not re.search(r"\b(?:page|siret|tva|capital|tel|fax|client|remises?)\b", low) and not re.search(r":\s*[A-Z0-9._/-]*\d[A-Z0-9._/-]*\s*$", row.text, re.I):
                        current.description=" ".join(x for x in [current.description,cont] if x).strip()
                        current.raw_text=" | ".join(x for x in [current.raw_text,cont] if x)
                continue

            # The first code-like token left of the description anchor is the material/fee code.
            code_word=None
            for w in left:
                t=w.text.strip()
                if len(t)<=max_mat and sum(ch.isdigit() for ch in t)>=min_digits and re.fullmatch(r"[A-Za-z0-9._/-]+", t):
                    code_word=w; break
                if re.match(r"^(?:ECO|D3EE|DEEE|WEEE)", t, flags=re.I):
                    code_word=w; break
                if _fee_kind(low) and center(w)<desc_x and re.fullmatch(r"[A-Za-z][A-Za-z0-9._/-]{1,29}",t):
                    # Charges can have wholly alphabetic article codes (PORTSTD,
                    # FREIGHT). Their explicit fee label is the type evidence.
                    code_word=w; break
            if code_word is None:
                if _fee_kind(low) and price is None and amount is not None and (
                    (qty is None and len(parsed_groups)==1) or (qty is not None and len(parsed_groups)==2)):
                    kind="shipping" if re.search(r"\b(?:livraison|port|shipping|freight)\b",low) else _fee_kind(low)
                    charges.append(AdditionalCharge(charge_type=kind,description=" ".join(w.text for w in left),
                        quantity=qty,amount=amount,currency=default_currency,page=pg.page,confidence=float(row.confidence)*0.95,
                        evidence=Evidence(page=pg.page,bbox=row.bbox,source_text=row.text,extraction_method="geometry_labeled_charge")))
                    current=None
                continue
            code=code_word.text.strip()
            secondary=None
            if sec_x is not None:
                sec_words=[w.text for w in left if center(w)>center(code_word)+15 and center(w)<sec_desc_boundary]
                secondary=" ".join(sec_words).strip() or None
            elif len(left)>1 and all(re.fullmatch(r"[A-Za-z0-9._/-]*\d[A-Za-z0-9._/-]*",w.text) for w in left):
                secondary=" ".join(w.text for w in left if w is not code_word) or None
            unit_anchor=anchors.get("uom")
            auxiliary=[anchors[key] for key in ("ua","up","internal_reference") if key in anchors]
            auxiliary_start=min(auxiliary)-max(8,pg.width*0.012) if auxiliary else None
            desc_words=[w.text for w in left if w is not code_word and w.text!=secondary and (sec_x is None or center(w)>=sec_desc_boundary)
                        and (unit_anchor is None or center(w)<unit_anchor-15)
                        and (auxiliary_start is None or center(w)<auxiliary_start)]
            desc=" ".join(desc_words).strip()
            unit_words=[w.text.strip("|[]") for w in row.words if unit_anchor is not None and abs(center(w)-unit_anchor)<max(15,pg.width*0.012)]
            kind=_fee_kind(f"{code} {desc}")
            if kind and re.search(r"\b(?:frais\s+de\s+(?:port|livraison)|shipping|freight)\b",_fold(desc)):
                kind="shipping"
            conf=max(0.0,min(1.0,float(row.confidence)*0.99))
            if kind:
                parent=None if kind=="shipping" else current
                charges.append(AdditionalCharge(
                    charge_type=kind, code=code, description=desc or code,
                    parent_line_number=parent.line_number if parent else None,
                    parent_material_number=parent.material_number if parent else None,
                    supplier_reference=parent.supplier_material_number if parent else None,
                    quantity=qty, uom=parent.uom if parent else None,
                    unit_price=price, amount=amount if amount is not None else (qty*price if qty is not None and price is not None else None),
                    currency=default_currency, page=pg.page, confidence=conf,
                    evidence=Evidence(page=pg.page,bbox=row.bbox,source_text=row.text,extraction_method="geometry_table_recovery"),
                ))
                continue
            if qty is None or price is None:
                continue
            computed=qty*price
            if amount is None:
                amount=computed
            # A printed amount that is wildly inconsistent with q*p is more likely a
            # column-boundary error than a genuine commercial amount.
            abs_tol=float(cfg.get("geometry_table_amount_tolerance_abs", 0.04))
            rel_tol=float(cfg.get("geometry_table_amount_tolerance_rel", 0.006))
            tol=max(abs_tol,abs(computed)*rel_tol)
            if abs(amount-computed)>tol and cfg.get("geometry_table_validate_quantity_price", True):
                continue
            current=LineItem(
                line_number=str(len(items)+1), material_number=code, article_number=code,
                supplier_material_number=secondary, description=desc or None,
                quantity=qty, ordered_quantity=qty, unit_price=price, net_unit_price=price,
                uom=" ".join(unit_words) or None,
                line_total=amount, line_net_amount=amount, currency=default_currency,
                raw_text=row.text, page=pg.page, bbox=row.bbox, confidence=conf,
            )
            items.append(current)
    return _dedupe_line_items(items), charges


def _quantity_first_table_items(pages, default_currency="EUR"):
    """Read explicit quantity / description / reference / price tables without totals.

    Row amounts are transparently calculated, never represented as printed values.
    A quantity followed by a recognized unit and a reference-column value are
    required, so commercial notes and signatures cannot become product rows.
    """
    import re
    from po_ocr.extract import all_rows
    from po_ocr.normalize import parse_number, normalize_unit
    from po_ocr.models import LineItem, AdditionalCharge, Evidence

    items=[]; charges=[]
    for page in pages:
        rows=all_rows([page],y_factor=0.50)
        anchors=None; header_idx=None
        for idx,row in enumerate(rows):
            found={}
            for word in row.words:
                token=_fold(word.text).strip(" .:/")
                key=next((kind for kind,aliases in {
                    "quantity":{"qte","quantite","qty","quantity"},
                    "description":{"designation","description"},
                    "reference":{"reference","article","sku"},
                    "price":{"prix","pu","price"},
                }.items() if token in aliases),None)
                if key: found[key]=(word.bbox[0]+word.bbox[2])/2
            if len(found)==4 and found["quantity"]<found["description"]<found["reference"]<found["price"]:
                anchors=found; header_idx=idx; break
        if anchors is None: continue
        for row in rows[header_idx+1:]:
            if len(row.words)<4: continue
            quantity_word,unit_word=row.words[:2]
            if not re.fullmatch(r"\d[\d.,]*",quantity_word.text): continue
            if not re.fullmatch(r"(?:PIEC(?:E|ES)?|PCE|PCS|EA|KG|BOX|L|M)",unit_word.text,re.I): continue
            if (unit_word.bbox[0]+unit_word.bbox[2])/2>anchors["description"]: continue
            price_boundary=(anchors["reference"]+anchors["price"])/2
            price_words=[word for word in row.words if (word.bbox[0]+word.bbox[2])/2>=price_boundary]
            code_words=[word for word in row.words if anchors["reference"]-page.width*0.10<=
                        (word.bbox[0]+word.bbox[2])/2<price_boundary]
            if not code_words: continue
            code=" ".join(word.text for word in code_words)
            description=" ".join(word.text for word in row.words if word.bbox[0]>=unit_word.bbox[2]
                                  and word.bbox[2]<=code_words[0].bbox[0])
            quantity=parse_number(quantity_word.text)
            price=parse_number(" ".join(word.text for word in price_words))
            if quantity is None or price is None or not description: continue
            amount=round(quantity*price,2)
            confidence=float(row.confidence)*0.94
            if _fee_kind(description):
                kind="shipping" if re.search(r"\b(?:port|livraison|shipping|freight)\b",_fold(description)) else _fee_kind(description)
                charges.append(AdditionalCharge(charge_type=kind,code=code,description=description,
                    quantity=quantity,uom=normalize_unit(unit_word.text),unit_price=price,amount=amount,
                    currency=default_currency,page=page.page,confidence=confidence,
                    evidence=Evidence(page=page.page,bbox=row.bbox,source_text=row.text,
                                      extraction_method="quantity_price_computed_amount")))
                continue
            if not re.fullmatch(r"[A-Za-z0-9._/ -]+",code) or not any(ch.isdigit() for ch in code): continue
            items.append(LineItem(material_number=code,article_number=code,description=description,
                quantity=quantity,ordered_quantity=quantity,uom=normalize_unit(unit_word.text),
                unit_price=price,net_unit_price=price,line_total=amount,line_net_amount=amount,
                currency=default_currency,page=page.page,bbox=row.bbox,raw_text=row.text,confidence=confidence,
                warnings=["Line total calculated from printed quantity and unit price; no printed line amount."]))
    return _dedupe_line_items(items),charges


def _price_then_quantity_table_items(pages, default_currency="EUR"):
    """Parse order rows whose explicit unit-price column precedes quantity.

    This layout is common in purchasing portals and may omit a printed line
    amount entirely. The supplier reference and description are mandatory; the
    line amount is then transparently calculated from the printed price and
    quantity.
    """
    import re
    from po_ocr.extract import all_rows
    from po_ocr.models import LineItem

    items=[]
    for page in pages:
        rows=all_rows([page],y_factor=0.50)
        header_idx=None; bounds=None
        for idx,row in enumerate(rows):
            words=row.words
            norms=[_fold(word.text).strip(" .:/") for word in words]
            supplier_i=next((i for i,n in enumerate(norms) if n=="fournisseur" and i>0 and norms[i-1] in {"ref","reference"}),None)
            code_i=next((i for i,n in enumerate(norms) if n=="code" and supplier_i is not None and i>supplier_i),None)
            desc_i=next((i for i,n in enumerate(norms) if n in {"designation","description","libelle"}),None)
            # A quote-number column may itself contain the word ``prix``.
            # The actual unit-price heading must sit after the description.
            price_i=next((i for i,n in enumerate(norms) if desc_i is not None and i>desc_i and n in {"tarif","prix","pu","p.a","pa"}),None)
            qty_i=next((i for i,n in enumerate(norms) if price_i is not None and i>price_i and n in {"quantite","qte","qty","quantity"}),None)
            if None in (supplier_i,desc_i,price_i,qty_i): continue
            supplier_start=words[supplier_i-1].bbox[0]
            code_start=words[code_i].bbox[0] if code_i is not None else words[desc_i].bbox[0]
            desc_start=words[desc_i].bbox[0]; price_start=words[price_i].bbox[0]; qty_start=words[qty_i].bbox[0]
            if not (supplier_start < code_start <= desc_start < price_start < qty_start): continue
            header_idx=idx; bounds=(supplier_start,code_start,desc_start,price_start,qty_start)
            break
        if header_idx is None: continue
        supplier_start,code_start,desc_start,price_start,qty_start=bounds
        for row in rows[header_idx+1:]:
            if _row_center_safe(row)>page.height*0.90: break
            low=_fold(row.text)
            if re.search(r"\b(?:total\s+(?:ht|ttc|net)|valeur\s+tva)\b",low): break
            def zone(x0,x1=None):
                return [word for word in row.words if (word.bbox[0]+word.bbox[2])/2>=x0 and (x1 is None or (word.bbox[0]+word.bbox[2])/2<x1)]
            supplier=" ".join(word.text for word in zone(supplier_start,code_start)).strip()
            internal=" ".join(word.text for word in zone(code_start,desc_start)).strip()
            description=" ".join(word.text for word in zone(desc_start,price_start)).strip()
            price_raw=" ".join(word.text for word in zone(price_start,qty_start)).strip()
            qty_raw=" ".join(word.text for word in zone(qty_start)).strip()
            if not supplier or sum(ch.isdigit() for ch in supplier)<5 or not re.fullmatch(r"[A-Za-z0-9._/ -]+",supplier): continue
            quantity=_parse_native_decimal(qty_raw); price=_parse_native_decimal(price_raw)
            if quantity is None or price is None or not description: continue
            amount=round(float(quantity)*float(price),6)
            items.append(LineItem(
                line_number=str(len(items)+1),material_number=supplier,article_number=supplier,
                supplier_material_number=supplier,customer_material_number=internal or None,
                description=description,quantity=quantity,ordered_quantity=quantity,
                unit_price=price,net_unit_price=price,line_total=amount,line_net_amount=amount,
                currency=default_currency,page=page.page,bbox=row.bbox,raw_text=row.text,
                confidence=float(row.confidence)*0.97,
                warnings=["Line total calculated from printed quantity and unit price; no printed line amount."],
            ))
    return _dedupe_line_items(items)


def _two_row_erp_table_items(pages, default_currency="EUR"):
    """Parse ERP tables where description/quantity precede code/price/amount.

    A logical product is printed on two physical rows: the first contains its
    description, ordered quantity and delivery week; the second contains buyer
    code, supplier reference, UOM, unit price and amount. Activation requires
    the explicit two-line table header and arithmetic consistency.
    """
    import re
    from po_ocr.extract import all_rows
    from po_ocr.models import LineItem

    number=r"[-+]?(?:\d{1,3}(?:[ .']\d{3})+|\d+)(?:[,.]\d+)?"
    description_re=re.compile(rf"^(?P<desc>.+?)\s+(?P<qty>{number})\s+(?P<week>\d{{1,2}}\s*/\s*\d{{2,4}})\s*$",re.I)
    detail_re=re.compile(
        rf"^(?P<code>[A-Z0-9][A-Z0-9._/-]{{2,29}})\s+"
        rf"(?P<supplier>[A-Z0-9][A-Z0-9._/-]{{5,29}})"
        rf"(?:\s+(?P=supplier))?\s+(?P<meta>.*?)\s+"
        rf"(?P<uom>PIEC(?:E|ES)?|PCE|PCS|EA|UN|U|KG|BOX|L|M)\s+"
        rf"(?P<price>{number})\s+(?P<amount>{number})(?:\s*[€$£])?\s*$",re.I,
    )
    items=[]
    for page in pages:
        rows=all_rows([page],y_factor=0.50)
        start=None
        for idx,row in enumerate(rows[:-1]):
            low=_fold(row.text); next_low=_fold(rows[idx+1].text)
            if all(term in low for term in ("designation","quantite","delai","montant")) and all(term in next_low for term in ("code","reference","fournisseur")):
                start=idx+2; break
        if start is None: continue
        pending=None
        for row in rows[start:]:
            text=" ".join(row.text.split())
            low=_fold(text)
            if any(term in low for term in ("horaires de reception","siege social","siret","total ht","total ttc")):
                pending=None
                if "horaires de reception" in low or "siege social" in low: break
                continue
            dm=description_re.fullmatch(text)
            if dm:
                qty=_parse_native_decimal(dm.group("qty"))
                if qty is not None:
                    pending=(dm.group("desc").strip(),qty,re.sub(r"\s+","",dm.group("week")),row)
                continue
            detail=detail_re.fullmatch(text)
            if detail is None or pending is None: continue
            description,quantity,week,description_row=pending
            price=_parse_native_decimal(detail.group("price")); amount=_parse_native_decimal(detail.group("amount"))
            if price is None or amount is None: pending=None; continue
            expected=float(quantity)*float(price); tolerance=max(0.04,abs(expected)*0.006)
            if abs(float(amount)-expected)>tolerance: pending=None; continue
            metadata=detail.group("meta").strip(" /-;:")
            raw=" | ".join(x for x in (description,text) if x)
            items.append(LineItem(
                line_number=str(len(items)+1),material_number=detail.group("code"),article_number=detail.group("code"),
                supplier_material_number=detail.group("supplier"),description=description,
                quantity=quantity,ordered_quantity=quantity,uom=detail.group("uom"),
                unit_price=price,net_unit_price=price,line_total=amount,line_net_amount=amount,
                currency=default_currency,delivery_week=week,delivery_period=week,
                notes=[metadata] if metadata else [],raw_text=raw,page=page.page,
                bbox=_bbox_union(description_row.words+row.words),
                confidence=min(float(description_row.confidence),float(row.confidence))*0.985,
            ))
            pending=None
    return _dedupe_line_items(items)


def _enhance_paired_party_zones_v46(po, pages, config: dict):
    """Recover side-by-side attention/delivery zones and supplier contact columns."""
    from po_ocr.extract import all_rows
    from po_ocr.models import Party, Address
    import re
    if not pages or not config.get("layout", {}).get("paired_label_zone_recovery", True):
        return po
    pg=pages[0]; rows=all_rows([pg],y_factor=0.50); width,height=pg.width,pg.height
    split=width*float(config.get("layout",{}).get("paired_label_column_split_ratio",0.50))
    ship_labels=tuple(_fold(x) for x in config.get("layout",{}).get("ship_to_labels",[]))
    attention_labels=tuple(_fold(x) for x in config.get("layout",{}).get("attention_labels",[]))

    for idx,row in enumerate(rows):
        low=_fold(row.text)
        if _row_center_safe(row)>height*0.52: break
        if not any(lbl in low for lbl in ship_labels):
            continue
        # Determine which side contains the delivery label.
        ship_words=[w for w in row.words if any(lbl in _fold(w.text) for lbl in ("livraison","delivery","ship"))]
        label_x=min((w.bbox[0] for w in ship_words),default=split)
        ship_right=label_x>=split
        vals=[]; contact_phone=None; contact_fax=None
        base_y=_row_center_safe(row)
        for rr in rows[idx+1:]:
            if _row_center_safe(rr)-base_y>height*0.125: break
            side=[w for w in rr.words if ((w.bbox[0]>=split) if ship_right else (w.bbox[2]<split))]
            txt=" ".join(w.text for w in side).strip()
            if not txt: continue
            fl=_fold(txt)
            if any(x in fl for x in ("designation","quantite","prix","montant","reference date")): break
            tm=re.search(r"\bT[eé]l\.?\s*[:.-]?\s*([+0-9][0-9 .-]{6,})",txt,flags=re.I)
            fm=re.search(r"\bFax\s*[:.-]?\s*([+0-9][0-9 .-]{6,})",txt,flags=re.I)
            if tm or fm:
                if tm: contact_phone=" ".join(tm.group(1).split()).strip(" -")
                if fm: contact_fax=" ".join(fm.group(1).split()).strip(" -")
                continue
            if re.fullmatch(r"(?:\d{2}[ .-]?){5,10}",txt):
                continue
            # Delivery instructions are context, not an address line.
            if re.search(r"\b(?:livraisons?|horaires?|possibilit[eé]|matin|apr[eè]s[- ]?midi|reception)\b",fl):
                if any(re.search(r"\b\d{5}\b",v) for v in vals): break
                continue
            vals.append(txt)
        if vals:
            # Split party name from address at first address-like line.
            def is_addr(v):
                return bool(re.search(r"\b(?:rue|avenue|av\.?|route|boulevard|chemin|zac|zae|zi|z\.i\.|bp|tsa)\b",_fold(v)) or re.search(r"\b\d{5}\b",v))
            ai=next((i for i,v in enumerate(vals[1:],start=1) if is_addr(v)), len(vals))
            name=vals[0]; pre=vals[1:ai]; addr=vals[ai:]
            party=Party(name=name,department=" / ".join(pre) or None,address=Address(
                line1=addr[0] if len(addr)>0 else None,line2=addr[1] if len(addr)>1 else None,line3=addr[2] if len(addr)>2 else None))
            party.phone=contact_phone; party.fax=contact_fax
            party.contact.phone=contact_phone; party.contact.fax=contact_fax
            for a in addr:
                m=re.search(r"\b(\d{5})\s+([A-Za-zÀ-ÿ .'-]{2,})",a)
                if m: party.address.postal_code=m.group(1); party.address.city=" ".join(m.group(2).split()); break
            po.ship_to=party
        # Attention contact is usually in the opposite column immediately below.
        if any(lbl in low for lbl in attention_labels):
            for rr in rows[idx+1:idx+3]:
                opp=[w for w in rr.words if ((w.bbox[2]<split) if ship_right else (w.bbox[0]>=split))]
                txt=" ".join(w.text for w in opp).strip(" *:-")
                if txt and re.fullmatch(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]{1,50}",txt):
                    po.ship_to.contact.name=txt; break
        break

    # Supplier contact recovery from the right-side supplier column, preferring rows
    # explicitly labelled Tel/Fax on that side.
    for rr in rows:
        if _row_center_safe(rr)>height*0.345: break
        right=" ".join(w.text for w in rr.words if w.bbox[0]>=split).strip()
        if not right: continue
        if any(x in _fold(right) for x in ("acheteur","code fournisseur","date commande")):
            break
        tm=re.search(r"\bT[eé]l\.?\s*[:.-]?\s*([+0-9][0-9 .-]{6,})",right,flags=re.I)
        fm=re.search(r"\bFax\s*[:.-]?\s*([+0-9][0-9 .-]{6,})",right,flags=re.I)
        if tm:
            po.supplier.phone=" ".join(tm.group(1).split()).strip(" -"); po.supplier.contact.phone=po.supplier.phone
        if fm:
            po.supplier.fax=" ".join(fm.group(1).split()).strip(" -"); po.supplier.contact.fax=po.supplier.fax

    # If the buyer was not printed as a separate block, the root of a delivery-site
    # name such as "THERM'ENERGIE - LEZENNES" is useful business evidence.
    if config.get("layout",{}).get("infer_buyer_from_ship_to_root",True) and (not po.buyer.name or _bad_party_name(po.buyer.name)) and po.ship_to.name:
        root=re.split(r"\s+-\s+",po.ship_to.name,maxsplit=1)[0].strip()
        if len(root)>=int(config.get("layout",{}).get("infer_buyer_from_ship_to_min_name_chars",5)):
            po.buyer.name=root; po.buyer.legal_name=root
            if not po.buyer.address.line1:
                po.buyer.address=po.ship_to.address.model_copy(deep=True)
    return po


def _sanitize_summary_totals_v46(po, pages, config: dict, default_currency: str="EUR"):
    """Type-safe summary parsing: weight/volume/pack count cannot become money totals."""
    from po_ocr.extract import all_rows
    from po_ocr.normalize import parse_number
    import re
    if not pages or not config.get("layout",{}).get("summary_type_guards",True):
        return po
    # Structured Office/data readers already carry semantically typed totals.  This
    # guard is meant for visually parsed PDF/image summaries where type confusion is
    # possible (weight/volume/pack count versus money).
    if not any(getattr(pg, "source_type", None) in {"native_pdf", "scan_ocr", "image_ocr", "azure"} for pg in pages):
        return po
    rows=all_rows(pages,y_factor=0.50)
    monetary=[]; weight=[]; volume=[]; packaging_counts=[]; explicit_grand=[]; shipping=[]
    for row in rows:
        text=row.text; low=_fold(text)
        # Find numbers after strongly typed labels, not arbitrary numbers in legal prose.
        for pat in (r"TOTAL\s+(?:H\.?T\.?|NET\s+HT|HT\s+DU\s+BON)\s*[:.-]?\s*([0-9][0-9 .']*(?:[,.]\d+)?)",
                    r"MONTANT\s+H\.?T\.?\s*[:.-]?\s*([0-9][0-9 .']*(?:[,.]\d+)?)"):
            m=re.search(pat,text,flags=re.I)
            if m:
                v=parse_number(m.group(1))
                if v is not None: monetary.append((v,row))
        gm=re.search(r"(?:TOTAL\s+TTC|NET\s+[AÀ]\s+PAYER|TOTAL\s+[AÀ]\s+PAYER)\s*[:.-]?\s*([0-9][0-9 .']*(?:[,.]\d+)?)",text,flags=re.I)
        if gm:
            v=parse_number(gm.group(1));
            if v is not None: explicit_grand.append((v,row))
        wm=re.search(r"TOTAL\s+POIDS(?:\s+DU\s+BON)?\s*[:.-]?\s*([0-9][0-9 .']*(?:[,.]\d+)?)\s*(KG|G|T)?",text,flags=re.I)
        if wm:
            v=parse_number(wm.group(1));
            if v is not None: weight.append((v,(wm.group(2) or "kg").lower(),row))
        vm=re.search(r"TOTAL\s+VOLUME(?:\s+DU\s+BON)?\s*[:.-]?\s*([0-9][0-9 .']*(?:[,.]\d+)?)\s*(M3|M³|L)?",text,flags=re.I)
        if vm:
            v=parse_number(vm.group(1));
            if v is not None: volume.append((v,row))
        cm=re.search(r"TOTAL\s+CONDITIONNEMENT\s*[:.-]?\s*([0-9][0-9 .']*(?:[,.]\d+)?)",text,flags=re.I)
        if cm:
            v=parse_number(cm.group(1));
            if v is not None: packaging_counts.append((v,row))
        sm=re.search(r"(?:FRAIS\s+DE\s+PORT|PORT\s+HT|FREIGHT|SHIPPING)\s*[:.-]?\s*([0-9][0-9 .']*(?:[,.]\d+)?)",text,flags=re.I)
        if sm:
            v=parse_number(sm.group(1));
            if v is not None: shipping.append((v,row))

    if monetary:
        v,_=monetary[-1]
        po.totals.total_net=v; po.totals.total_before_tax=v; po.totals.currency=po.totals.currency or default_currency
    if weight:
        v,u,_=weight[-1]; po.totals.total_weight=v; po.totals.weight_unit=u
    nonmoney_values={round(float(x[0]),6) for x in weight+volume+packaging_counts}
    if explicit_grand:
        v,_=explicit_grand[-1]
        po.totals.total_gross=v; po.totals.total_after_tax=v; po.totals.amount_due=v; po.totals.grand_total=v
    elif config.get("layout",{}).get("reset_unlabeled_grand_totals",True):
        # Keep plausible pre-existing gross totals (e.g. HT + VAT) and only clear values
        # that are demonstrably non-monetary or wildly implausible.  This preserves
        # legitimate TTC totals extracted by the base parser while fixing cases where
        # TOTAL CONDITIONNEMENT or TOTAL POIDS was mistaken for money.
        current=po.totals.grand_total if po.totals.grand_total is not None else po.totals.total_gross
        base=po.totals.total_net or po.totals.total_before_tax or po.totals.calculated_document_net
        suspicious=False
        if current is not None:
            if round(float(current),6) in nonmoney_values:
                suspicious=True
            if base not in (None,0):
                ratio=abs(float(current))/max(abs(float(base)),1e-9)
                cap=float(config.get("layout",{}).get("suspicious_total_ratio_cap",20.0))
                if ratio>cap or ratio<0.10:
                    suspicious=True
        if suspicious:
            po.totals.total_gross=None; po.totals.total_after_tax=None; po.totals.amount_due=None; po.totals.grand_total=None
    if shipping:
        po.totals.total_shipping=shipping[-1][0]
    elif config.get("layout",{}).get("reset_unlabeled_shipping_totals",True):
        # Clear only values that look like a type-confusion artefact or are implausible
        # relative to the document net total.  Do not discard a plausible shipping
        # amount extracted elsewhere simply because this footer lacks the label.
        base=po.totals.total_net or po.totals.total_before_tax or po.totals.calculated_document_net
        for attr in ("total_shipping","total_freight"):
            val=getattr(po.totals,attr)
            if val is None: continue
            suspicious=round(float(val),6) in nonmoney_values
            if base not in (None,0):
                ratio=abs(float(val))/max(abs(float(base)),1e-9)
                if ratio>float(config.get("layout",{}).get("suspicious_total_ratio_cap",20.0)):
                    suspicious=True
            if suspicious:
                setattr(po.totals,attr,None)
    return po


def _is_continuation_marker(text: str | None) -> bool:
    import re
    low = _fold(text)
    if not low:
        return False
    patterns = (
        r"^suite\s+page\s+\d+(?:\s*/\s*\d+)?$",
        r"^page\s+\d+(?:\s*/\s*\d+)?$",
        r"^continued(?:\s+on)?\s+page\s+\d+$",
        r"^fortsetzung(?:\s+auf)?\s+seite\s+\d+$",
    )
    return any(re.fullmatch(p, low) for p in patterns)


def _dedupe_line_items(items):
    """Remove parser duplicates while preserving intentional repeated order lines.

    Page is part of the signature, so identical materials on separate continuation pages
    remain distinct. On the same page quantity/price/amount differences also keep lines
    separate, which is important for repeated SKUs with different commercial conditions.
    """
    out = []
    seen = set()
    for item in items:
        if _is_continuation_marker(item.raw_text) or _is_continuation_marker(item.description):
            continue
        sig = (
            item.page,
            _fold(item.material_number or item.article_number or ""),
            _fold(item.supplier_material_number or ""),
            _fold(item.description or ""),
            item.quantity,
            item.unit_price,
            item.line_total,
        )
        if sig in seen:
            continue
        seen.add(sig)
        out.append(item)
    for idx, item in enumerate(out, 1):
        if not item.line_number:
            item.line_number = str(idx)
    return out


def _packed_native_table_items(table, default_currency: str = "EUR"):
    """Parse compact ERP tables where several logical line items are stored in one PDF row.

    A frequent print-engine pattern is a single cell containing repeated blocks such as:
      description\ninternal_code supplier_ref\nsupplier_ref\n...
    while quantity/UOM, week/price and amount are stacked in neighboring cells.  The
    generic PDF table extractor sees one physical row; this routine reconstructs the
    logical rows by aligning those vertical stacks.
    """
    from po_ocr.models import LineItem
    from po_ocr.normalize import parse_number
    import re

    data = table.data or []
    if len(data) < 2:
        return []
    header = [_fold(str(x) if x is not None else "") for x in data[0]]
    compact = [re.sub(r"[^a-z0-9]+", "", h) for h in header]

    def find_col(pred):
        for idx, (h, c) in enumerate(zip(header, compact)):
            if pred(h, c):
                return idx
        return None

    combined_col = find_col(lambda h, c: ("designation" in c or "description" in c or "libelle" in c) and ("referencefournisseur" in c or "codereference" in c or "codefournisseur" in c))
    qty_col = find_col(lambda h, c: "quantite" in c or c in {"qty", "quantity", "ordered"})
    delay_price_col = find_col(lambda h, c: ("semann" in c) or (("delai" in c or "delivery" in c) and ("pu" in c or "price" in c)))
    amount_col = find_col(lambda h, c: "montant" in c or "netht" in c or "linetotal" in c)
    if combined_col is None or qty_col is None or delay_price_col is None or amount_col is None:
        return []

    all_items = []
    code_pair_re = re.compile(r"^([A-Z0-9][A-Z0-9._/-]{1,17})\s+([A-Z0-9][A-Z0-9._/-]{5,29})$", re.I)
    week_re = re.compile(r"^(?:S(?:EM(?:AINE)?)?\s*)?(\d{1,2})\s*[/.-]\s*(\d{2,4})$", re.I)
    numeric_re = re.compile(r"^[-+]?\d[\d .']*(?:[,.]\d+)?$")

    for row in data[1:]:
        if combined_col >= len(row):
            continue
        raw_combined = _split_cell_lines(row[combined_col])
        if not raw_combined:
            continue

        # Segment the description/code stream at rows containing two code-like identifiers.
        groups = []
        pending_desc = []
        i = 0
        while i < len(raw_combined):
            token = raw_combined[i].strip()
            m = code_pair_re.fullmatch(token)
            # Avoid treating normal prose as an identifier pair: at least one token must
            # be strongly numeric and the second is usually the longer supplier reference.
            if m and any(ch.isdigit() for ch in m.group(1)) and sum(ch.isdigit() for ch in m.group(2)) >= 5:
                internal, supplier = m.group(1), m.group(2)
                desc = " ".join(pending_desc).strip()
                if desc.endswith(supplier):
                    desc = desc[: -len(supplier)].rstrip(" -/;")
                groups.append({"code": internal, "supplier": supplier, "description": desc})
                pending_desc = []
                # ERP prints often repeat the supplier reference alone on the next line.
                if i + 1 < len(raw_combined) and _fold(raw_combined[i + 1]) == _fold(supplier):
                    i += 1
            else:
                # Continuation/footer notes after the final identified item are intentionally
                # not converted into products; before the next code pair they are description.
                pending_desc.append(token)
            i += 1

        if not groups:
            continue

        # Quantity cell typically alternates quantity and UOM.
        qty_lines = _split_cell_lines(row[qty_col] if qty_col < len(row) else None)
        quantities = []
        q = 0
        while q < len(qty_lines):
            raw = qty_lines[q]
            val = parse_number(raw) if numeric_re.fullmatch(raw.replace("\u00a0", " ")) else None
            if val is None:
                q += 1
                continue
            uom = None
            if q + 1 < len(qty_lines) and not numeric_re.fullmatch(qty_lines[q + 1].replace("\u00a0", " ")):
                uom = qty_lines[q + 1].strip()
                q += 1
            quantities.append((val, uom))
            q += 1

        # Delay/price cell typically alternates week/year and unit price.
        dp_lines = _split_cell_lines(row[delay_price_col] if delay_price_col < len(row) else None)
        delay_prices = []
        q = 0
        while q < len(dp_lines):
            delivery_week = None
            price = None
            wm = week_re.fullmatch(dp_lines[q].strip())
            if wm:
                delivery_week = f"{int(wm.group(1)):02d}/{wm.group(2)}"
                if q + 1 < len(dp_lines):
                    price = parse_number(dp_lines[q + 1])
                    q += 1
            else:
                price = parse_number(dp_lines[q])
            if delivery_week is not None or price is not None:
                delay_prices.append((delivery_week, price))
            q += 1

        amount_lines = _split_cell_lines(row[amount_col] if amount_col < len(row) else None)
        amounts = [parse_number(x) for x in amount_lines if parse_number(x) is not None]

        n = len(groups)
        # A packed table is only accepted when its neighboring stacks are broadly aligned.
        aligned_counts = [len(quantities), len(delay_prices), len(amounts)]
        if sum(1 for x in aligned_counts if x == n) < 2:
            continue

        for idx, group in enumerate(groups):
            qty, uom = quantities[idx] if idx < len(quantities) else (None, None)
            delivery_week, unit_price = delay_prices[idx] if idx < len(delay_prices) else (None, None)
            printed_amount = amounts[idx] if idx < len(amounts) else None
            computed_amount = qty * unit_price if qty is not None and unit_price is not None else None
            line_total = printed_amount if printed_amount is not None else computed_amount
            raw_parts = [group["description"], group["code"], group["supplier"], str(qty) if qty is not None else "", uom or "", delivery_week or "", str(unit_price) if unit_price is not None else "", str(line_total) if line_total is not None else ""]
            all_items.append(LineItem(
                material_number=group["code"], article_number=group["code"],
                supplier_material_number=group["supplier"], description=group["description"] or None,
                quantity=qty, ordered_quantity=qty, uom=uom,
                unit_price=unit_price, net_unit_price=unit_price,
                line_total=line_total, line_net_amount=line_total,
                currency=default_currency, delivery_week=delivery_week, delivery_period=delivery_week,
                notes=[f"Delivery week: {delivery_week}"] if delivery_week else [],
                raw_text=" | ".join(x for x in raw_parts if x),
                page=table.source.page, confidence=float(table.confidence) * 0.985,
            ))
    return all_items


def _standard_native_table_items(table, default_currency: str = "EUR"):
    from po_ocr.models import LineItem
    from po_ocr.normalize import parse_number
    import re
    data = table.data or []
    if not data:
        return []
    header = [_fold(str(x) if x is not None else "") for x in data[0]]

    def find_col(pred):
        for idx, h in enumerate(header):
            if pred(h):
                return idx
        return None

    code_col = find_col(lambda h: "code article" in h or h in {"article", "code", "reference", "référence"})
    desc_col = find_col(lambda h: "libelle" in h or "designation" in h or "description" in h)
    qty_col = find_col(lambda h: "commande" in h or "quantite" in h or "qte" in h or h in {"qty", "quantity", "ordered", "qte cdee", "qte commandee"})
    price_col = find_col(lambda h: "pu h.t" in h or "pu ht" in h or "unit price" in h or "prix unitaire" in h or "prix net" in h or "net price" in h)
    amount_col = find_col(lambda h: "montant ht" in h or "montant net" in h or "line total" in h or h in {"montant", "amount"})
    lot_col = find_col(lambda h: h == "lot" or "colis" in h or "pack" in h)
    quote_col = find_col(lambda h: "offre" in h or "devis" in h or "quote" in h)
    if code_col is None or desc_col is None or qty_col is None or price_col is None or code_col == desc_col:
        return []
    items = []
    for row in data[1:]:
        if code_col >= len(row):
            continue
        codes = _split_cell_lines(row[code_col])
        descs = _split_cell_lines(row[desc_col] if desc_col < len(row) else None)
        qtys = _split_cell_lines(row[qty_col] if qty_col < len(row) else None)
        prices = _split_cell_lines(row[price_col] if price_col < len(row) else None)
        amounts = _split_cell_lines(row[amount_col] if amount_col is not None and amount_col < len(row) else None)
        lots = _split_cell_lines(row[lot_col] if lot_col is not None and lot_col < len(row) else None)
        quotes = _split_cell_lines(row[quote_col] if quote_col is not None and quote_col < len(row) else None)
        # Wrapped descriptions are continuation text, not duplicate products, when all
        # transactional columns contain exactly one value.
        singleton_stacks=[len(codes),len(qtys),len(prices),len(amounts)]
        if singleton_stacks == [1,1,1,1] and len(descs)>1:
            descs=[" ".join(descs)]
        n = max(map(len, [codes, descs, qtys, prices, amounts, lots, quotes]), default=0)
        for i in range(n):
            code = codes[i] if i < len(codes) else (codes[0] if len(codes) == 1 else "")
            desc = descs[i] if i < len(descs) else (descs[0] if len(descs) == 1 else "")
            qty_raw = qtys[i] if i < len(qtys) else (qtys[0] if len(qtys) == 1 else "")
            price_raw = prices[i] if i < len(prices) else (prices[0] if len(prices) == 1 else "")
            amount_raw = amounts[i] if i < len(amounts) else (amounts[0] if len(amounts) == 1 else "")
            lot_raw = lots[i] if i < len(lots) else (lots[0] if len(lots) == 1 else "")
            quote_raw = quotes[i] if i < len(quotes) else (quotes[0] if len(quotes) == 1 else "")
            # Material identifiers may be numeric or alpha-numeric (e.g. PORTSTD).
            if not code or not re.search(r"[A-Za-z0-9]", code) or _is_continuation_marker(code) or _is_continuation_marker(desc):
                continue
            qty = _parse_native_decimal(qty_raw)
            price = _parse_native_decimal(price_raw)
            printed_amount = _parse_native_decimal(amount_raw)
            lot = _parse_native_decimal(lot_raw)
            # Explicit quantity columns turn quantity-less rows into footer/summary
            # annotations, not products. This blocks rows such as Signature/TOTAL.
            if qty is None:
                continue
            if price is None and printed_amount is None:
                continue
            computed = qty * price if qty is not None and price is not None else None
            amount = printed_amount if printed_amount is not None else computed
            # Explicit native columns are high-authority, but still require arithmetic
            # consistency before they can beat competing hypotheses.
            if printed_amount is not None and computed is not None:
                tol=max(0.03, abs(computed)*0.005)
                if abs(printed_amount-computed)>tol:
                    continue
            items.append(LineItem(
                material_number=code, article_number=code, description=desc or None,
                quantity=qty, ordered_quantity=qty, pack_quantity=lot,
                unit_price=price, net_unit_price=price, line_total=amount, line_net_amount=amount,
                quote_number=quote_raw or None,
                currency=default_currency, raw_text=" | ".join(x for x in [code, desc, qty_raw, price_raw, amount_raw, lot_raw, quote_raw] if x),
                page=table.source.page, confidence=float(table.confidence) * 0.997,
            ))
    return items


def _stacked_column_native_table_items_v46(table, default_currency: str = "EUR"):
    """Parse native tables whose logical rows are stacked inside each column cell.

    Typical print layout: one PDF table row contains newline-separated material codes,
    descriptions, quantities, prices and amounts.  This parser aligns those stacks and
    discards surrounding narrative/footer lines from the description column.
    """
    from po_ocr.models import LineItem
    from po_ocr.normalize import parse_number
    import re
    data=table.data or []
    if len(data)<2:
        return []
    headers=[_fold(str(x or "")) for x in data[0]]
    compact=[re.sub(r"[^a-z0-9]+","",h) for h in headers]
    def col(pred):
        for i,(h,c) in enumerate(zip(headers,compact)):
            if pred(h,c): return i
        return None
    code_col=col(lambda h,c: c in {"reference","referencearticle","article","code","material"})
    desc_col=col(lambda h,c: "designation" in c or "description" in c or "libelle" in c)
    qty_col=col(lambda h,c: "quantite" in c or c in {"qty","quantity"})
    price_col=col(lambda h,c: c in {"prixht","prixnetht","puht","prixunitaire"} or "prixht" in c)
    amount_col=col(lambda h,c: "montantht" in c or "montantnet" in c or "linetotal" in c)
    gross_col=col(lambda h,c: c in {"ppht","prixpublicht","tarifht","grossprice"} or "ppht" in c)
    if None in (code_col,desc_col,qty_col,price_col,amount_col):
        return []
    out=[]
    for row in data[1:]:
        codes=_split_cell_lines(row[code_col] if code_col<len(row) else None)
        prices=[parse_number(x) for x in _split_cell_lines(row[price_col] if price_col<len(row) else None)]
        amounts=[parse_number(x) for x in _split_cell_lines(row[amount_col] if amount_col<len(row) else None)]
        gross=[parse_number(x) for x in _split_cell_lines(row[gross_col] if gross_col is not None and gross_col<len(row) else None)]
        qty_raw=_split_cell_lines(row[qty_col] if qty_col<len(row) else None)
        qty=[]
        for x in qty_raw:
            m=re.match(r"\s*([-+]?\d[\d .']*(?:[,.]\d+)?)\s*([A-Za-zÀ-ÿ]{1,8})?\s*$",x)
            if m:
                qty.append((parse_number(m.group(1)),m.group(2)))
        n=len(codes)
        if n<1 or len(prices)!=n or len(amounts)!=n or len(qty)!=n:
            continue
        # Description column often has narrative before/after the actual product lines.
        descs=[]
        for x in _split_cell_lines(row[desc_col] if desc_col<len(row) else None):
            low=_fold(x)
            if not x or re.fullmatch(r"\*+",x): continue
            if any(k in low for k in ("commande par le depot","commande par le dépôt","horaires de reception","horaires de réception","du lundi au vendredi")):
                continue
            if x.lstrip().startswith("-"): continue
            descs.append(x)
        # Choose the n most plausible description rows: textual, not pure numeric/footer.
        descs=[x for x in descs if re.search(r"[A-Za-zÀ-ÿ]{2,}",x) and not re.search(r"\b(?:total|siret|tva|capital|rcs|ape)\b",_fold(x))]
        if len(descs)>n:
            descs=descs[:n]
        if len(descs)!=n:
            continue
        for i,code in enumerate(codes):
            if not re.search(r"\d",code): continue
            q,u=qty[i]; p=prices[i]; a=amounts[i]
            if q is None or p is None or a is None: continue
            expected=q*p; tol=max(0.04,abs(expected)*0.006)
            if abs(a-expected)>tol: continue
            out.append(LineItem(
                line_number=str(len(out)+1),material_number=code,article_number=code,
                description=descs[i],quantity=q,ordered_quantity=q,uom=u,
                gross_unit_price=gross[i] if i<len(gross) else None,
                unit_price=p,net_unit_price=p,line_total=a,line_net_amount=a,
                currency=default_currency,page=table.source.page,confidence=float(table.confidence)*0.995,
                raw_text=" | ".join(x for x in (code,descs[i],qty_raw[i] if i<len(qty_raw) else "",str(p),str(a)) if x),
            ))
    return out



def _parse_native_decimal(value):
    """Parse ERP-native decimal strings without assuming <=3 decimal places.

    Procurement systems often print 5 decimal unit prices such as ``244,49000``.
    The generic number normalizer intentionally treats long comma groups as thousands;
    native monetary table cells need the opposite interpretation.
    """
    from po_ocr.normalize import parse_number
    import re
    if value is None:
        return None
    raw="".join(str(value).replace("\u00a0", " ").split())
    raw=re.sub(r"[^0-9,.'+\-]", "", raw)
    if not raw:
        return None
    if "," in raw and "." not in raw and raw.count(",") == 1:
        left,right=raw.split(",",1)
        if 1 <= len(right) <= 6:
            try: return float(f"{left}.{right}")
            except ValueError: pass
    return parse_number(raw)


def _fee_kind(text: str | None) -> str | None:
    low = _fold(text)
    if not low:
        return None
    if any(x in low for x in ("deee", "eco-participation", "eco participation", "ecoparticipation", "weee")):
        return "environmental_fee"
    if any(x in low for x in ("surcharge", "supplement", "supplement", "frais")):
        return "surcharge"
    return None


def _rexellike_product_fee_table(table, default_currency: str = "EUR"):
    """Parse line-item tables where product rows and environmental-fee rows are interleaved.

    This is layout-generic: it keys off semantic headers (line/product/qty/UOM/unit price/
    amount/date), not a supplier name. Product and fee events may be vertically stacked in
    one physical PDF row. Fees are kept as separate business objects and attached to the
    preceding product instead of corrupting the next product line.
    """
    from po_ocr.models import LineItem, AdditionalCharge, Evidence
    from po_ocr.normalize import parse_number, parse_date
    import re
    data = table.data or []
    if len(data) < 2:
        return [], []
    header = [_fold(str(x) if x is not None else "") for x in data[0]]
    compact = [re.sub(r"[^a-z0-9]+", "", h) for h in header]

    def col(pred):
        for i, (h, c) in enumerate(zip(header, compact)):
            if pred(h, c):
                return i
        return None

    line_col = col(lambda h, c: c in {"lig", "ligne", "line", "pos", "position"} or h.startswith("lig."))
    product_col = col(lambda h, c: c in {"produit", "product", "article", "designation"} or "produit" in c)
    qty_col = col(lambda h, c: c in {"qte", "qty", "quantity", "quantite"} or "quantite" in c)
    uom_col = col(lambda h, c: c in {"condit", "condition", "uom", "unite", "uf"} or "condit" in c)
    price_col = col(lambda h, c: "prixunitaire" in c or "unitprice" in c or c in {"pu", "prix"})
    amount_col = col(lambda h, c: "montantligne" in c or "linetotal" in c or "netht" in c or c == "montant")
    date_col = col(lambda h, c: "datedelivraison" in c or "deliverydate" in c)
    if product_col is None or qty_col is None or price_col is None or amount_col is None:
        return [], []

    out, charges = [], []
    for row in data[1:]:
        products = _split_cell_lines(row[product_col] if product_col < len(row) else None)
        if not products:
            continue
        events = []
        for raw in products:
            m = re.match(r"^([A-Z0-9][A-Z0-9._/-]{3,30})\s+(.+)$", raw.strip(), flags=re.I)
            if not m:
                continue
            code, desc = m.group(1), " ".join(m.group(2).split())
            kind = _fee_kind(f"{code} {desc}")
            # Strong fee codes should never become material lines.
            if code.upper().startswith(("D3EE", "DEEE", "WEEE")):
                kind = "environmental_fee"
            events.append({"code": code, "description": desc, "fee_kind": kind})
        if not events:
            continue

        def nums_at(idx):
            vals=[]
            if idx is None or idx >= len(row): return vals
            for x in _split_cell_lines(row[idx]):
                v=_parse_native_decimal(x)
                if v is not None: vals.append(v)
            return vals
        quantities=nums_at(qty_col); prices=nums_at(price_col); amounts=nums_at(amount_col)
        uoms=_split_cell_lines(row[uom_col] if uom_col is not None and uom_col < len(row) else None)
        dates=_split_cell_lines(row[date_col] if date_col is not None and date_col < len(row) else None)
        line_nums=_split_cell_lines(row[line_col] if line_col is not None and line_col < len(row) else None)
        product_count=sum(1 for e in events if not e["fee_kind"])
        # Require the monetary stacks to align with semantic events. This prevents
        # accidental activation on prose tables.
        if len(prices) != len(events) or len(amounts) != len(events) or len(quantities) != len(events):
            continue
        if line_nums and len(line_nums) != product_count:
            continue

        prod_i=0
        current=None
        for ev_i, ev in enumerate(events):
            qty=quantities[ev_i]; price=prices[ev_i]; amount=amounts[ev_i]
            if ev["fee_kind"]:
                if current is None:
                    continue
                charges.append(AdditionalCharge(
                    charge_type=ev["fee_kind"], code=ev["code"], description=ev["description"],
                    parent_line_number=current.line_number, parent_material_number=current.material_number,
                    supplier_reference=current.supplier_material_number,
                    quantity=qty, uom=current.uom, unit_price=price, amount=amount,
                    currency=default_currency, page=table.source.page, confidence=float(table.confidence)*0.99,
                    evidence=Evidence(page=table.source.page, source_text=f"{ev['code']} {ev['description']}", extraction_method="native_product_fee_table"),
                ))
                continue
            line_no = line_nums[prod_i] if prod_i < len(line_nums) else str(len(out)+1)
            uom = uoms[prod_i] if prod_i < len(uoms) else None
            raw_date = dates[prod_i] if prod_i < len(dates) else None
            delivery_date = parse_date(raw_date) if raw_date else None
            current=LineItem(
                line_number=str(line_no), material_number=ev["code"], article_number=ev["code"],
                description=ev["description"], quantity=qty, ordered_quantity=qty, uom=uom,
                unit_price=price, net_unit_price=price, line_total=amount, line_net_amount=amount,
                currency=default_currency, requested_delivery_date=delivery_date,
                raw_text=f"{ev['code']} {ev['description']} | {qty} | {uom or ''} | {price} | {amount} | {raw_date or ''}",
                page=table.source.page, confidence=float(table.confidence)*0.99,
            )
            out.append(current); prod_i += 1
    return out, charges


def _repair_packed_fee_rows(table, items, default_currency: str = "EUR"):
    """Separate fee amounts embedded between packed product rows.

    Some ERP tables keep only product quantities/prices but insert a fee description and
    an extra amount between product amounts. We match product arithmetic first and turn
    unmatched fee amounts into AdditionalCharge objects.
    """
    from po_ocr.models import AdditionalCharge, Evidence
    from po_ocr.normalize import parse_number
    import re
    if not items or not table.data or len(table.data) < 2:
        return items, []
    header=[_fold(str(x) if x is not None else "") for x in table.data[0]]
    amount_idx=next((i for i,h in enumerate(header) if "montant" in h or "net h.t" in h or "net ht" in h), None)
    combined_idx=next((i for i,h in enumerate(header) if "designation" in h or "description" in h or "libelle" in h), None)
    if amount_idx is None or combined_idx is None:
        return items, []
    amounts=[]; combined_text=""
    for row in table.data[1:]:
        if amount_idx < len(row):
            for x in _split_cell_lines(row[amount_idx]):
                v=parse_number(x)
                if v is not None: amounts.append(v)
        if combined_idx < len(row) and row[combined_idx]:
            combined_text += "\n" + str(row[combined_idx])
    fee_lines=[x for x in _split_cell_lines(combined_text) if _fee_kind(x)]
    if not fee_lines or len(amounts) <= len(items):
        return items, []

    remaining=list(enumerate(amounts))
    # Correct each product amount by arithmetic match, not positional coincidence.
    for item in items:
        if item.quantity is None or item.unit_price is None:
            continue
        expected=float(item.quantity)*float(item.unit_price)
        if not remaining: break
        best=min(remaining, key=lambda kv: abs(kv[1]-expected))
        tol=max(0.03, abs(expected)*0.005)
        if abs(best[1]-expected) <= tol:
            item.line_total=best[1]; item.line_net_amount=best[1]
            remaining=[x for x in remaining if x[0] != best[0]]

    # Remove fee text that was prepended to the following product description.
    for item in items:
        desc=item.description or ""
        for fee in fee_lines:
            f=" ".join(fee.split())
            if _fold(desc).startswith(_fold(f)):
                item.description=desc[len(f):].strip(" -:;") or item.description
                break

    charges=[]
    for idx, fee in enumerate(fee_lines[:len(remaining)]):
        _, amount=remaining[idx]
        # Parent = last supplier/material reference printed before the fee marker.
        pos=_fold(combined_text).find(_fold(fee))
        parent=None
        for item in items:
            ref=item.supplier_material_number or item.material_number or ""
            rpos=_fold(combined_text).find(_fold(ref)) if ref else -1
            if 0 <= rpos < pos and (parent is None or rpos > parent[0]):
                parent=(rpos,item)
        parent_item=parent[1] if parent else (items[max(0, min(idx, len(items)-1))])
        charges.append(AdditionalCharge(
            charge_type=_fee_kind(fee) or "surcharge", description=" ".join(fee.split()),
            parent_line_number=parent_item.line_number or str(items.index(parent_item)+1), parent_material_number=parent_item.material_number,
            supplier_reference=parent_item.supplier_material_number, amount=amount,
            currency=default_currency, page=table.source.page, confidence=float(table.confidence)*0.98,
            evidence=Evidence(page=table.source.page, source_text=fee, extraction_method="packed_fee_recovery"),
        ))
    return items, charges


def _native_table_line_items_and_charges(tables, default_currency: str = "EUR"):
    from po_ocr.extract import line_item_set_quality
    all_items=[]; all_charges=[]
    for table in tables:
        fee_items, fee_charges = _rexellike_product_fee_table(table, default_currency=default_currency)
        standard = _standard_native_table_items(table, default_currency=default_currency)
        stacked = _stacked_column_native_table_items_v46(table, default_currency=default_currency)
        packed = _packed_native_table_items(table, default_currency=default_currency)
        packed, packed_charges = _repair_packed_fee_rows(table, packed, default_currency=default_currency)
        candidates=[x for x in (fee_items, standard, stacked, packed) if x]
        if candidates:
            ranked=sorted(((line_item_set_quality(x), len(x), x) for x in candidates), key=lambda z:(z[0],z[1]), reverse=True)
            items=ranked[0][2]
            all_items.extend(items)
            # Only charges belonging to the selected parse are retained.
            if items is fee_items:
                all_charges.extend(fee_charges)
            elif items is packed:
                all_charges.extend(packed_charges)
    return _dedupe_line_items(all_items), all_charges


def _native_table_line_items(tables, default_currency: str = "EUR"):
    """Backward-compatible line-only view of the V4.5 native table parser."""
    lines, _ = _native_table_line_items_and_charges(tables, default_currency=default_currency)
    return lines



def _parse_labeled_party_cell(value: str, *, role: str = "party"):
    from po_ocr.models import Party, Address
    import re
    lines=_split_cell_lines(value)
    if not lines:
        return Party()
    # Remove the semantic address label, keeping any value on following lines.
    while lines and any(x in _fold(lines[0]) for x in ("adresse de facturation", "adresse de livraison", "adresse de commande fournisseur", "billing address", "shipping address", "supplier address", "facture a", "facture à", "billed to", "bill to")):
        lines=lines[1:]
    phone=None; email=None; clean=[]
    for line in lines:
        tm=re.search(r"(?:t[eé]l(?:[eé]phone)?|phone)\s*[:.]?\s*([+0-9][0-9 .-]{6,})", line, flags=re.I)
        em=re.search(r"(?:e[_ -]?mail|email|mail)\s*[:.]?\s*([^\s]+@[^\s]+)", line, flags=re.I)
        if tm: phone=" ".join(tm.group(1).split()).strip(" -")
        if em: email=em.group(1).strip()
        if tm or em: continue
        clean.append(line)
    if not clean:
        return Party(phone=phone, email=email)

    def is_addr(line):
        low=_fold(line)
        return bool(re.search(r"\b(?:rue|avenue|av\.?|boulevard|bd\.?|route|chemin|street|strasse|straße|zac|zae|zi|z\.i\.|tsa|bp)\b", low) or re.search(r"\b\d{5}\b", line))
    first_addr=next((i for i,x in enumerate(clean) if is_addr(x)), len(clean))
    pre=clean[:first_addr]
    addr_lines=clean[first_addr:]
    # Departments/services are not legal party names even when printed in uppercase.
    dept_words=("comptabilite", "factures", "controle", "service", "approvisionnement", "reception")
    candidates=[]
    for i,line in enumerate(pre):
        score=_party_name_strength(line)
        low=_fold(line)
        if any(w in low for w in dept_words): score -= 2.8
        if role == "supplier" and any(w in low for w in ("bosch", "leblanc", "supplier", "fournisseur", "sas", "sa", "sarl")): score += 1.2
        if role in {"bill_to","ship_to","buyer"} and any(w in low for w in ("rexel", "prolians", "garanka", "verney")): score += 1.0
        candidates.append((score,i,line))
    if candidates:
        _, name_idx, name=max(candidates, key=lambda x:x[0])
    else:
        name_idx=0; name=clean[0]
    department=" / ".join(x for i,x in enumerate(pre) if i != name_idx) or None
    address=Address()
    if addr_lines:
        address.line1=addr_lines[0]
        if len(addr_lines)>1: address.line2=addr_lines[1]
        if len(addr_lines)>2: address.line3=addr_lines[2]
        for line in addr_lines:
            m=re.search(r"\b(\d{5})\s+([A-Za-zÀ-ÿ0-9 .'-]{2,})", line)
            if m:
                address.postal_code=m.group(1); address.city=" ".join(m.group(2).split())
                break
        if any(_fold(x) in {"france","fr"} for x in addr_lines):
            address.country="France"; address.country_code="FR"
    party=Party(name=name, legal_name=name, department=department, address=address, phone=phone, email=email)
    party.contact.phone=phone; party.contact.email=email; party.contact.department=department
    return party


def _enhance_header_terms_addresses_v45(po, tables):
    """High-confidence extraction from semantically labelled native PDF tables."""
    from po_ocr.normalize import parse_date
    import re
    for table in tables:
        for row in table.data or []:
            for cell in row:
                raw=str(cell or "")
                low=_fold(raw)
                if not low:
                    continue
                # Same-cell order header: CDE n° 061056882 / Du 18-08-2026.
                if "cde n" in low or "commande n" in low or "bon de commande" in low:
                    m=re.search(r"(?:CDE|COMMANDE)\s*N[°ºo]?\s*[:#.-]?\s*(.+?)(?=\s+DU\s+|\n|$)", raw, flags=re.I|re.S)
                    if m:
                        candidate=" ".join(m.group(1).split()).strip(" :-")
                        candidate=re.sub(r"\s+(?:DU|DATE)$", "", candidate, flags=re.I)
                        if re.search(r"\d{4,}", candidate) and len(candidate) <= 40:
                            po.purchase_order.number=_make_table_field(candidate, table.source.page, confidence=float(table.confidence))
                    dm=re.search(r"\bDU\s+(\d{1,2}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{2,4})", raw, flags=re.I)
                    if dm:
                        dr=re.sub(r"\s+", "", dm.group(1))
                        if parse_date(dr): po.purchase_order.order_date=_make_table_field(dr, table.source.page, kind="date", confidence=float(table.confidence))
                if "adresse de facturation" in low or "billing address" in low:
                    party=_parse_labeled_party_cell(raw, role="bill_to")
                    if party.name: po.bill_to=party
                elif "adresse de livraison" in low or "shipping address" in low:
                    party=_parse_labeled_party_cell(raw, role="ship_to")
                    if party.name: po.ship_to=party
                elif "adresse de commande fournisseur" in low or "supplier address" in low:
                    party=_parse_labeled_party_cell(raw, role="supplier")
                    if party.name: po.supplier=party

        # Two-row commercial metadata table (code client / delivery / payment / currency).
        data=table.data or []
        if len(data) >= 2:
            headers=[_fold(str(x or "")) for x in data[0]]
            vals=[str(x or "").strip() for x in data[1]]
            for i,h in enumerate(headers):
                if i >= len(vals): continue
                value=vals[i]
                if not value: continue
                if "code client" in h or "customer code" in h:
                    po.purchase_order.account_number=_make_table_field(value, table.source.page, confidence=float(table.confidence))
                elif "date de livraison" in h:
                    # Preserve semantic text like "Voir lignes" instead of pretending it is a date.
                    parsed=parse_date(value)
                    if parsed:
                        po.purchase_order.expected_delivery_date=_make_table_field(value, table.source.page, kind="date", confidence=float(table.confidence))
                    else:
                        po.logistics.requested_delivery_date=value
                elif "mode" in h and "livraison" in h:
                    po.logistics.shipping_method=value
                elif "type" in h and ("delai" in h or "livraison" in h):
                    po.logistics.delivery_terms=value
                elif "mode de reglement" in h or "mode de règlement" in h:
                    po.commercial.payment_terms=value
                    if re.search(r"\bVIRT|VIREMENT|TRANSFER\b", value, flags=re.I): po.commercial.payment_method="VIREMENT"
                elif h == "devise" or "currency" in h:
                    po.purchase_order.currency=_make_table_field(value, table.source.page, confidence=float(table.confidence))
    # Buyer/issuer: a CL d'emission/service label is not a legal party. If a strong
    # billing entity exists, preserve the emitting centre as department and use the
    # corporate billing entity as buyer.
    buyer_low=_fold(po.buyer.name)
    issuer_label = any(x in buyer_low for x in ("cl d emission", "centre logistique", "centre d emission"))
    if po.bill_to.name and (_bad_party_name(po.buyer.name) or not po.buyer.name or issuer_label):
        if issuer_label:
            dep=re.sub(r"^.*?[:.-]\s*", "", po.buyer.name or "").strip()
            if dep: po.buyer.department=dep
        po.buyer.name=po.bill_to.name; po.buyer.legal_name=po.bill_to.legal_name
        if issuer_label or not po.buyer.address.line1:
            po.buyer.address=po.bill_to.address.model_copy(deep=True)

    # Approvisionneur / buyer contact table.
    for table in tables:
        data=table.data or []
        if len(data) < 2: continue
        hdr=[_fold(str(x or "")) for x in data[0]]
        for i,h in enumerate(hdr):
            if "interlocuteur" in h or "approvisionneur" in h:
                raw=str(data[1][i] if i < len(data[1]) else "")
                flat=" ".join(_split_cell_lines(raw))
                nm=re.search(r"Contact\s*:\s*([A-Za-zÀ-ÿ .'-]+?)(?=\s+T[eé]l|$)", flat, flags=re.I)
                tm=re.search(r"T[eé]l\s*:\s*([0-9][0-9 .-]{7,})", flat, flags=re.I)
                fm=re.search(r"Fax\s*:\s*([0-9][0-9 .-]{7,})", flat, flags=re.I)
                em=re.search(r"(?:E[-_ ]?mail|Mail)\s*:\s*([^\s]+@[^\s]+)", flat, flags=re.I)
                if nm: po.buyer.contact.name=" ".join(nm.group(1).split())
                if tm: po.buyer.contact.phone=" ".join(tm.group(1).split()).strip(" -")
                if fm: po.buyer.contact.fax=" ".join(fm.group(1).split()).strip(" -")
                if em: po.buyer.contact.email=em.group(1).strip()

    # Never keep a non-date label/value fragment in a date field.
    if po.purchase_order.expected_delivery_date.value and not parse_date(str(po.purchase_order.expected_delivery_date.value)):
        from po_ocr.models import ExtractedField
        po.purchase_order.expected_delivery_date=ExtractedField()
    return po


def _enhance_totals_from_native_tables(po, tables, default_currency: str = "EUR"):
    import re
    # Structured summary rows first: map each header to the value below it. This is
    # far safer than choosing the right-most number from a row that also contains weight.
    for table in tables:
        data=table.data or []
        if len(data) >= 2:
            headers=[_fold(str(x or "")) for x in data[0]]
            vals=[_parse_native_decimal(x) for x in data[1]]
            if any("total net ht" in h for h in headers):
                for i,h in enumerate(headers):
                    v=vals[i] if i < len(vals) else None
                    if v is None: continue
                    if "total brut ht" in h or "lignes produit" in h:
                        po.totals.total_product_lines=v; po.totals.subtotal=v
                    elif "total charges" in h:
                        po.totals.total_line_charges=v
                    elif "total taxes" in h:
                        po.totals.total_line_taxes=v; po.totals.total_tax=v
                    elif "frais admin" in h:
                        po.totals.total_admin_fees=v
                    elif "frais de port" in h:
                        po.totals.total_freight=v
                    elif "frais d'embal" in h or "frais embal" in h:
                        po.totals.total_packaging=v
                    elif "autres frais" in h:
                        po.totals.total_other_fees=v
                    elif "remise pied" in h:
                        po.totals.total_discount=v
                    elif "total net ht" in h:
                        po.totals.total_net=v; po.totals.total_before_tax=v
                    elif "poids" in h and "tot" in h:
                        po.totals.total_weight=v; po.totals.weight_unit="kg"
        # Compact/simple forms with an explicit Total HT/Net label in a cell.
        for row in data:
            for idx,cell in enumerate(row):
                low=_fold(str(cell or ""))
                if any(x in low for x in ("total h.t. net", "total h.t net", "total ht net", "total net ht")):
                    # Same cell amount first, then adjacent cells.
                    candidates=[]
                    for c in row[idx:]:
                        for tok in re.findall(r"[-+]?\d[\d .']*(?:[,.]\d+)?", str(c or "")):
                            v=_parse_native_decimal(tok)
                            if v is not None: candidates.append(v)
                    if candidates and po.totals.total_net is None:
                        po.totals.total_net=candidates[0]; po.totals.total_before_tax=candidates[0]
    po.totals.currency=po.totals.currency or default_currency
    # Do not invent TTC when the source only gives HT/line taxes.
    if po.totals.grand_total is None and po.totals.total_vat is None:
        po.totals.total_gross=None; po.totals.total_after_tax=None; po.totals.amount_due=None
    return po


def _enhance_parties_from_page_layout(po, pages):
    """Recover issuer, supplier and delivery parties from split left/right headers."""
    from po_ocr.extract import all_rows
    from po_ocr.models import Party, Address
    import re
    if not pages:
        return po
    page = pages[0]
    rows = all_rows([page], y_factor=0.55)
    width, height = page.width, page.height

    # Main transactional title row. Supplier is often printed to the far right of COMMANDE.
    title_row = None
    command_word = None
    for row in rows:
        low = _fold(row.text)
        if re.search(r"(^|\s)commande($|\s)", low) and "n° commande" not in low and "no commande" not in low and _row_center_safe(row) < height * 0.35:
            cw = next((w for w in row.words if _fold(w.text) == "commande"), None)
            if cw:
                title_row, command_word = row, cw
                break
    supplier_x = None
    if title_row is not None and command_word is not None:
        after = [w for w in title_row.words if w.bbox[0] > command_word.bbox[2] + width * 0.08]
        name = " ".join(w.text for w in after).strip()
        generic = _fold(name) in {"fournisseur", "achat", "purchase order", "supplier"}
        if name and not generic and len(name) <= 90:
            if _bad_party_name(po.supplier.name) or not po.supplier.name:
                po.supplier.name = name
                po.supplier.legal_name = name
            supplier_x = min(w.bbox[0] for w in after)
    if supplier_x is not None:
        frags = []
        for row in rows:
            cy = _row_center_safe(row)
            if title_row and cy <= _row_center_safe(title_row) + 5:
                continue
            if cy > height * 0.36:
                break
            words = [w for w in row.words if w.bbox[0] >= supplier_x - 5]
            if not words:
                continue
            text = " ".join(w.text for w in words).strip()
            low = _fold(text)
            if not text or any(low.startswith(x) for x in ("page", "livraison prevue", "code article")):
                continue
            # Filter values that belong to the left/middle order header rather than supplier block.
            if re.fullmatch(r"\d{8,}|\d{1,2}/\d{1,2}/\d{2,4}", text):
                continue
            frags.append(text)
        # Keep address-like fragments only.
        addr = [x for x in frags if re.search(r"(?:\b(?:rue|avenue|av\.?|boulevard|bd\.?|route|chemin|street|strasse|straße)\b|^\d{5}\s+[A-Za-zÀ-ÿ])", _fold(x), flags=re.I)]
        if addr:
            po.supplier.address.line1 = addr[0]
            if len(addr) > 1:
                po.supplier.address.line2 = addr[1]
            for addr_line in addr:
                m = re.search(r"\b(\d{5})\s+([A-Za-zÀ-ÿ .'-]{2,})$", addr_line.strip())
                if m:
                    po.supplier.address.postal_code = m.group(1)
                    po.supplier.address.city = " ".join(m.group(2).split()); break

    # Buyer/issuer: strong first top-left organisation row followed by address/SIRET.
    if _bad_party_name(po.buyer.name):
        top_rows = [r for r in rows if _row_center_safe(r) < height * 0.22 and (r.bbox[0] if r.bbox else width) < width * 0.45]
        candidate = None
        for r in top_rows:
            low = _fold(r.text)
            if any(x in low for x in ("tel", "fax", "siret", "capital")):
                continue
            if re.search(r"\d{5}", r.text) or re.match(r"^\d+\s+(?:rue|avenue|route|boulevard)", low):
                continue
            if 3 <= len(r.text.strip()) <= 80 and any(ch.isalpha() for ch in r.text):
                candidate = r
                break
        if candidate:
            po.buyer.name = candidate.text.strip()
            po.buyer.legal_name = candidate.text.strip()
            after = [r.text.strip() for r in top_rows if _row_center_safe(r) > _row_center_safe(candidate) and _row_center_safe(r) < (title_row and _row_center_safe(title_row) or height*.25)]
            addr = [x for x in after if re.search(r"\b(?:rue|avenue|route|boulevard|\d{5})\b", _fold(x))]
            if addr:
                po.buyer.address = Address(line1=addr[0], line2=addr[1] if len(addr)>1 else None)
                for addr_line in addr:
                    m=re.search(r"\b(\d{5})\s+([A-Za-zÀ-ÿ .'-]{2,})$", addr_line.strip())
                    if m:
                        po.buyer.address.postal_code=m.group(1); po.buyer.address.city=" ".join(m.group(2).split()); break

    # Delivery block: parse rows following the explicit label, constrained to the left half.
    for idx, row in enumerate(rows):
        if "adresse de livraison" in _fold(row.text):
            vals=[]
            y0=_row_center_safe(row)
            for r in rows[idx+1:]:
                if _row_center_safe(r)-y0 > height*0.10:
                    break
                words=[w for w in r.words if w.bbox[0] < width*0.48]
                text=" ".join(w.text for w in words).strip()
                if text and "correspondant" not in _fold(text):
                    vals.append(text)
            if vals:
                po.ship_to=Party(name=vals[0], address=Address(line1=vals[1] if len(vals)>1 else None,
                                                               line2=vals[2] if len(vals)>2 else None))
                for addr_line in vals[1:]:
                    m=re.search(r"\b(\d{5})\s+([A-Za-zÀ-ÿ .'-]{2,})$", addr_line.strip())
                    if m:
                        po.ship_to.address.postal_code=m.group(1); po.ship_to.address.city=" ".join(m.group(2).split()); break
            break
    for row in rows:
        m=re.search(r"Correspondant\s*:\s*(.+?)(?:\s+Page\s*:|$)", row.text, flags=re.I)
        if m:
            po.ship_to.contact.name=" ".join(m.group(1).split())
        fm=re.search(r"Fax\s+Destinataire\s*:\s*([0-9 +.\-]+).*?T[eé]l\s*:\s*([0-9 +.\-]+)", row.text, flags=re.I)
        if fm:
            po.supplier.contact.fax=" ".join(fm.group(1).split()); po.supplier.contact.phone=" ".join(fm.group(2).split())
        # Issuer contact in the top-left corporate block.
        if _row_center_safe(row) < height * 0.22:
            tm=re.search(r"T[eé]l\.?\s*:\s*([0-9][0-9 .\-]{7,})", row.text, flags=re.I)
            if tm and not po.buyer.phone:
                po.buyer.phone=" ".join(tm.group(1).split())
                po.buyer.contact.phone=po.buyer.phone
        sm=re.search(r"\bSIRET\s+([0-9 ]{10,20})", row.text, flags=re.I)
        if sm:
            po.buyer.company_registration_number=" ".join(sm.group(1).split())
    return po


def _party_name_strength(text: str | None) -> float:
    import re
    raw = " ".join((text or "").split())
    low = _fold(raw)
    if not raw or len(raw) > 90 or not any(ch.isalpha() for ch in raw):
        return -10.0
    score = 0.5
    if 2 <= len(raw.split()) <= 8:
        score += 1.0
    letters = [c for c in raw if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.72:
        score += 1.3
    if re.search(r"\b(?:sas|sasu|sa|sarl|gmbh|ltd|llc|inc|ag|spa|s\.a\.)\b", low):
        score += 2.0
    if re.search(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", raw):
        score -= 4.0
    if any(x in low for x in ("tel", "fax", "mail", "adresse", "commande", "purchase order", "page", "virement", "reglement", "rglt", "sem/ann", "montant", "quantite", "capital", "siret", "rcs", "tva", "iban", "bic")):
        score -= 3.0
    if re.search(r"\b(?:rue|avenue|av\.?|boulevard|route|zi|z\.i\.|bp|cedex)\b", low):
        score -= 2.5
    if re.fullmatch(r"(?:st|saint)\s+[a-z -]+", low):
        score -= 2.0
    return score


def _enhance_parties_zone_v2(po, pages, config: dict):
    """Geometric party segmentation for repeated ERP headers.

    Separates the issuer corporate block, internal buyer/service strip, supplier block,
    delivery address and payment/contact fields.  It deliberately uses zones anchored
    by the order title rather than flattened line order.
    """
    from po_ocr.extract import all_rows
    from po_ocr.models import Address
    import re
    if not pages:
        return po
    page = pages[0]
    rows = all_rows([page], y_factor=0.55)
    width, height = page.width, page.height
    cfg = (config or {}).get("layout", {})
    buyer_y_max = float(cfg.get("buyer_corporate_zone_max_y_ratio", 0.16)) * height
    supplier_x = float(cfg.get("supplier_zone_min_x_ratio", 0.48)) * width
    supplier_y_max = float(cfg.get("supplier_zone_max_y_ratio", 0.34)) * height

    # Locate the transaction title; it separates corporate/service headers from
    # order/supplier details on most ERP printouts.
    command_y = None
    for row in rows:
        if re.search(r"\bcommande\b", _fold(row.text)):
            command_y = _row_center_safe(row)
            break

    # Explicit English labels are stronger than geometry and occur frequently in
    # lightweight/generated POs. Keep French "Acheteur/Fournisseur" out of this rule
    # because ERP forms often use those labels for contact IDs rather than legal parties.
    explicit_buyer = False
    explicit_supplier = False
    for row in rows:
        if _row_center_safe(row) > height * 0.42:
            break
        mb = re.match(r"\s*Buyer\s*[:.-]\s*(.+?)\s*$", row.text, flags=re.I)
        ms = re.match(r"\s*(?:Supplier|Vendor)\s*[:.-]\s*(.+?)\s*$", row.text, flags=re.I)
        if mb and _party_name_strength(mb.group(1)) > -1:
            name = " ".join(mb.group(1).split())
            po.buyer.name = name; po.buyer.legal_name = name; explicit_buyer = True
        if ms and _party_name_strength(ms.group(1)) > -1:
            name = " ".join(ms.group(1).split())
            po.supplier.name = name; po.supplier.legal_name = name; explicit_supplier = True

    # Corporate buyer/issuer: strongest short organisation-like line in the very top block.
    buyer_candidates = []
    for row in rows:
        cy = _row_center_safe(row)
        if cy > buyer_y_max:
            break
        text = row.text.strip()
        strength = _party_name_strength(text)
        if strength > 0:
            buyer_candidates.append((strength - cy / max(1.0, height) * 0.4, text, row))
    if buyer_candidates:
        _, candidate, crow = max(buyer_candidates, key=lambda x: x[0])
        current_strength = _party_name_strength(po.buyer.name)
        candidate_strength = _party_name_strength(candidate)
        if not explicit_buyer and (_bad_party_name(po.buyer.name) or current_strength < candidate_strength - 0.35):
            po.buyer.name = candidate
            po.buyer.legal_name = candidate
        # Address and corporate phone immediately below the organisation row.
        addr_lines = []
        for row in rows:
            cy = _row_center_safe(row)
            if cy <= _row_center_safe(crow) or cy > min(buyer_y_max, _row_center_safe(crow) + height * 0.095):
                continue
            text = row.text.strip()
            low = _fold(text)
            if re.search(r"\b(?:tel|fax|siret|capital)\b", low):
                tm = re.search(r"T[eé]l\.?\s*[:.]?\s*([0-9][0-9 .-]{7,})", text, flags=re.I)
                fm = re.search(r"Fax\s*[:.]?\s*([0-9][0-9 .-]{7,})", text, flags=re.I)
                if tm and not po.buyer.phone:
                    po.buyer.phone = " ".join(tm.group(1).split()).strip(" -")
                    po.buyer.contact.phone = po.buyer.phone
                if fm and not po.buyer.fax:
                    po.buyer.fax = " ".join(fm.group(1).split()).strip(" -")
                    po.buyer.contact.fax = po.buyer.fax
                sm = re.search(r"\bSIRET\s*[:.]?\s*([0-9 ]{10,20})", text, flags=re.I)
                if sm:
                    po.buyer.company_registration_number = " ".join(sm.group(1).split())
                continue
            if ("z.i" in low or "zone industrielle" in low or re.search(r"\b(?:rue|avenue|route|boulevard|zi|\d{5})\b", low)):
                addr_lines.append(text)
        if addr_lines:
            po.buyer.address = Address(line1=addr_lines[0], line2=addr_lines[1] if len(addr_lines) > 1 else None, line3=addr_lines[2] if len(addr_lines) > 2 else None)
            for a in addr_lines:
                m = re.search(r"\b(\d{5})\s+([A-Za-zÀ-ÿ0-9 .'-]{2,})", a)
                if m:
                    po.buyer.address.postal_code = m.group(1)
                    po.buyer.address.city = " ".join(m.group(2).split())
                    break

    # Internal service / date row (e.g. "PF CHAUFFAGE LE 16/09/26").
    service_date_re = re.compile(r"^(.*?)\s+LE\s+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*$", re.I)
    for row in rows:
        if command_y is not None and _row_center_safe(row) >= command_y:
            break
        m = service_date_re.match(row.text.strip())
        if m:
            service = " ".join(m.group(1).split())
            if service and _party_name_strength(service) > -1:
                po.buyer.department = service
            if not po.purchase_order.order_date.value:
                po.purchase_order.order_date = _make_spatial_field(m.group(2), page.page, row.bbox, kind="date", method="service_date_anchor")
            break

    # Contact/reference/mail belong to the buyer's service zone.
    for row in rows:
        cy = _row_center_safe(row)
        if command_y is not None and cy > command_y + height * 0.09:
            break
        m = re.search(r"\bContact\s*:\s*([A-Za-zÀ-ÿ .'-]+?)\s+([0-9][0-9 .-]{7,})\s*$", row.text, flags=re.I)
        if m:
            po.buyer.contact.name = " ".join(m.group(1).split())
            po.buyer.contact.phone = " ".join(m.group(2).split())
        em = re.search(r"\bMail\s*:\s*([^\s]+@[^\s]+)", row.text, flags=re.I)
        if em:
            po.buyer.contact.email = em.group(1)
            po.buyer.email = po.buyer.email or em.group(1)
        fm = re.match(r"\s*Fax\s*[:.]?\s*([0-9][0-9 .-]{7,})", row.text, flags=re.I)
        if fm:
            po.buyer.fax = " ".join(fm.group(1).split()).strip(" -")
            po.buyer.contact.fax = po.buyer.fax

    # Supplier identity: right-side organisation between the corporate strip and the
    # order header. This avoids confusing payment terms/cities with supplier names.
    zone_start = buyer_y_max * 0.92
    zone_end = min(supplier_y_max, (command_y + height * 0.04) if command_y is not None else supplier_y_max)
    supplier_candidates = []
    for row in rows:
        cy = _row_center_safe(row)
        if cy < zone_start or cy > zone_end:
            continue
        right_words = [w for w in row.words if w.bbox[0] >= supplier_x]
        if not right_words:
            continue
        text = " ".join(w.text for w in right_words).strip()
        strength = _party_name_strength(text)
        if strength > 0.4:
            supplier_candidates.append((strength, text, row, right_words))
    supplier_row = None
    if supplier_candidates:
        _, supplier_name, supplier_row, _ = max(supplier_candidates, key=lambda x: x[0])
        if not explicit_supplier and (_bad_party_name(po.supplier.name) or _party_name_strength(po.supplier.name) < _party_name_strength(supplier_name) - 0.2):
            po.supplier.name = supplier_name
            po.supplier.legal_name = supplier_name

    if supplier_row is not None:
        addr = []
        sy = _row_center_safe(supplier_row)
        for row in rows:
            cy = _row_center_safe(row)
            if cy <= sy or cy > min(height * 0.34, sy + height * 0.11):
                continue
            text = " ".join(w.text for w in row.words if w.bbox[0] >= supplier_x).strip()
            if not text:
                continue
            low = _fold(text)
            if any(x in low for x in ("commande", "cd rglt", "page", "virement")):
                continue
            if re.search(r"\b(?:rue|avenue|av\.?|boulevard|bp|cedex|\d{5})\b", low):
                addr.append(text)
        if addr:
            po.supplier.address = Address(line1=addr[0], line2=addr[1] if len(addr)>1 else None, line3=addr[2] if len(addr)>2 else None)
            for a in addr:
                m = re.search(r"\b(\d{5})\s*([A-Za-zÀ-ÿ .'-]{2,})", a)
                if m:
                    po.supplier.address.postal_code = m.group(1)
                    po.supplier.address.city = " ".join(m.group(2).split())
                    break

    # Delivery address: after an explicit delivery label, collect only the side of
    # the page where the address starts. This avoids swallowing payment terms printed
    # in the same horizontal band on the opposite side.
    ship_low = _fold(po.ship_to.name)
    ship_bad = (not po.ship_to.name) or any(x in ship_low for x in ("depot", "dépôt", "type livraison", "livre", "delivery type"))
    if ship_bad:
        for idx, row in enumerate(rows):
            low = _fold(row.text)
            if "adresse de livraison" not in low and "adresse livraison" not in low:
                continue
            address_words = [w for w in row.words if "adresse" in _fold(w.text)]
            label_words = address_words or [w for w in row.words if "livraison" in _fold(w.text)]
            label_x = min((w.bbox[0] for w in label_words), default=0.0)
            side_left = label_x < width * 0.5
            vals = []
            y0 = _row_center_safe(row)
            for rr in rows[idx + 1:]:
                cy = _row_center_safe(rr)
                if cy - y0 > height * 0.12:
                    break
                words = [w for w in rr.words if (w.bbox[0] < width * 0.48 if side_left else w.bbox[0] >= width * 0.48)]
                text = " ".join(w.text for w in words).strip()
                # OCR table borders occasionally survive as isolated single letters.
                text = re.sub(r"(?<=\w)\s+[A-Z]$", "", text).strip()
                tlow = _fold(text)
                if not text or len(text) < 2:
                    continue
                if any(x in tlow for x in ("designation", "code reference", "quantite", "quantites", "prix net", "u.f", "p.u", "page ")):
                    break
                if re.search(r"\b(?:tel|fax|cd rglt|virement)\b", tlow):
                    continue
                vals.append(text)
            if vals:
                from po_ocr.models import Party
                def _looks_address_start(v):
                    lowv=_fold(v)
                    return bool(re.search(r"\b(?:rue|avenue|av\.?|boulevard|bd\.?|route|chemin|zac|zae|zi|z\.i\.|batiment|bâtiment|bp|tsa)\b", lowv) or re.search(r"\b\d{5}\b", v))
                addr_start=next((i for i,v in enumerate(vals[1:], start=1) if _looks_address_start(v)), 1 if len(vals)>1 else len(vals))
                pre_addr=vals[1:addr_start]
                addr_vals=vals[addr_start:]
                po.ship_to = Party(name=vals[0], department=" / ".join(pre_addr) or None, address=Address(
                    line1=addr_vals[0] if len(addr_vals) > 0 else None,
                    line2=addr_vals[1] if len(addr_vals) > 1 else None,
                    line3=addr_vals[2] if len(addr_vals) > 2 else None,
                ))
                po.ship_to.contact.department=po.ship_to.department
                for a in addr_vals:
                    m = re.search(r"\b(\d{5})\s*([A-Za-zÀ-ÿ .'-]{2,})", a)
                    if m:
                        po.ship_to.address.postal_code = m.group(1)
                        po.ship_to.address.city = " ".join(m.group(2).split())
                        break
            break

    # Supplier phone/fax can be printed on the right side of the delivery block.
    for row in rows:
        if _row_center_safe(row) > height * 0.43:
            break
        right_text = " ".join(w.text for w in row.words if w.bbox[0] >= supplier_x).strip()
        tm = re.search(r"\bT[eé]l\.?\s*[:.]?\s*([0-9][0-9 .-]{7,})", right_text, flags=re.I)
        fm = re.search(r"\bFax\s*[:.]?\s*([0-9][0-9 .-]{7,})", right_text, flags=re.I)
        if tm:
            val = " ".join(tm.group(1).split()).strip(" -")
            po.supplier.phone = val
            po.supplier.contact.phone = val
        if fm:
            val = " ".join(fm.group(1).split()).strip(" -")
            po.supplier.fax = val
            po.supplier.contact.fax = val

    # Remove table-header artefacts accidentally interpreted as supplier codes.
    if po.supplier.code and _fold(po.supplier.code) in {"sem/ann", "sem ann", "u.f", "uf", "p.u", "pu", "quantites", "delai"}:
        po.supplier.code = None
    if po.buyer.phone:
        po.buyer.phone = po.buyer.phone.strip(" -")
    if po.supplier.phone:
        po.supplier.phone = po.supplier.phone.strip(" -")
    if po.supplier.fax:
        po.supplier.fax = po.supplier.fax.strip(" -")
    return po



# --- V4.7 semantic type guards + labelled-zone recovery -----------------------

def _is_invalid_po_number_v47(value: str | None) -> bool:
    """Reject obvious role/phone/email contamination before a PO number is trusted."""
    import re
    text=" ".join(str(value or "").split()).strip()
    low=_fold(text)
    if not text:
        return True
    if "@" in text or ":" in text:
        return True
    if any(tok in low for tok in (
        "code fournisseur","supplier code","vendor code","telephone","téléphone",
        "tel ","fax","telecopie","télécopie","n° tva","no tva","siret","iban",
        "total","conditionnement","poids","volume","courriel","email","mail",
        "date de livraison","livraison souhaitee","livraison souhaitée"
    )):
        return True
    # Scheduling prose can contain enough digits to look superficially like an
    # identifier (for example ``Reprise le 05 Janvier 2026.``).
    if re.search(
        r"\b(?:janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|"
        r"octobre|novembre|decembre)\b",
        low,
    ) and re.search(r"\b(?:le|du|au|reprise|fermeture)\b", low):
        return True
    if len(re.findall(r"[A-Za-zÀ-ÿ]{2,}", text)) >= 4:
        return True
    # Phone/fax punctuation is never accepted as an order identifier. Pure numeric PO
    # numbers remain valid because many procurement systems use 8-12 digits.
    if re.fullmatch(r"\+?\d{1,3}(?:[ .-]\d{2,4}){2,}", text):
        return True
    if re.fullmatch(r"(?:\d{2}[ .-]){4,}\d{2}", text):
        return True
    digits=sum(ch.isdigit() for ch in text)
    if digits < 2 or len(text) > 64:
        return True
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{1,63}", text):
        return True
    return False


def _enhance_strict_order_number_v47(po, pages, config: dict):
    """High-precision PO-number extraction with label→value spatial association.

    Strong explicit labels outrank generic numbers. Existing values are retained only
    when they pass semantic type guards. This prevents fax/phone/supplier-code leakage.
    """
    from po_ocr.extract import all_rows
    from po_ocr.normalize import parse_date
    import re
    if not pages or not config.get("layout",{}).get("strict_po_number_resolution", True):
        return po
    pg=pages[0]
    rows=all_rows([pg], y_factor=float(config.get("layout",{}).get("strict_label_row_y_factor",0.50)))
    candidates=[]
    learned_family=config.get("_learned_family")
    learned_family_conf=float(config.get("_learned_family_confidence",0.0) or 0.0)
    shape_profiles=config.get("_learned_number_shape_profiles",{}) or {}

    def _shape_v5(value):
        out=[]
        for ch in str(value or "").upper():
            c="A" if ch.isalpha() else ("9" if ch.isdigit() else (" " if ch.isspace() else ch))
            if c in {"A","9"}:
                if not out or out[-1]!=c: out.append(c)
            else:
                if not out or out[-1]!=c: out.append(c)
        return "".join(out).strip()

    def _shape_prior_v5(value):
        if learned_family_conf < float(config.get("layout",{}).get("learned_shape_min_family_confidence",0.78)):
            return 0.0
        sh=_shape_v5(value)
        for item in shape_profiles.get(str(learned_family),[]):
            if str(item.get("shape"))==sh:
                return float(item.get("probability",0.0) or 0.0)
        return 0.0

    def add(value, score, row, method, words=None):
        val=" ".join(str(value or "").split()).strip(" :-")
        if _is_invalid_po_number_v47(val) or parse_date(val):
            return
        # Postal codes and tiny standalone numbers are weak unless an explicit order
        # label is attached to them.
        if re.fullmatch(r"\d{4,6}", val) and score < 5.0:
            score -= 2.0
        prior=_shape_prior_v5(val)
        if prior>0:
            score += float(config.get("layout",{}).get("learned_shape_match_bonus",0.85))*prior
        elif learned_family_conf >= float(config.get("layout",{}).get("learned_shape_penalty_min_family_confidence",0.92)) and shape_profiles.get(str(learned_family)):
            score -= float(config.get("layout",{}).get("learned_shape_mismatch_penalty",0.10))
        candidates.append((float(score), val, row, method, words or getattr(row,"words",[])))

    # Preserve segmented identifiers printed after an explicit number marker,
    # for example ``N° 02 - 9260205710``.  The short left segment is part of
    # the customer's identifier (often an agency/series), not disposable noise.
    #
    # Some scanned C.C.L. forms consistently lose the thin printed hyphen while
    # OCR still reads both aligned numeric segments as ``N° 04 9260202817``.
    # Reconstruct that separator only when the issuer/layout is corroborated;
    # other unseparated numeric groups remain untouched.
    page_folded=_fold(pg.text)
    has_order_title=bool(re.search(
        r"\b(?:bon\s+de\s+commande(?:\s+d[' ]?achat)?|purchase\s+order)\b",
        page_folded,
    ))
    is_ccl_order=bool(
        has_order_title
        and (
            re.search(r"(?:^|\W)c\s*[.]?\s*c\s*[.]?\s*l(?:\W|$)",page_folded)
            or "ccl.fr" in page_folded
            or "comptoir commercial du languedoc" in page_folded
        )
    )
    reconstructed_segmented_sources={}
    for row in rows:
        if _row_center_safe(row) > pg.height*0.25:
            break
        ordered=sorted(row.words,key=lambda word:word.bbox[0])
        compact_tokens=[re.sub(r"[^a-z0-9]","",_fold(word.text)) for word in ordered]
        marker_positions=[
            pos for pos,token in enumerate(compact_tokens)
            if token in {"n","no","numero"}
        ]
        for marker in marker_positions:
            tail=ordered[marker+1:marker+6]
            if len(tail)<2:
                continue
            prefix_pos=None
            for pos,word in enumerate(tail[:2]):
                if re.fullmatch(r"\d{1,4}",word.text.strip(" .:#()[]")):
                    prefix_pos=pos
                    break
            if prefix_pos is None:
                continue
            prefix=tail[prefix_pos].text.strip(" .:#()[]")
            core_pos=None
            core=None
            for pos in range(prefix_pos+1,min(len(tail),prefix_pos+4)):
                token=tail[pos].text.strip(" .:#()[]")
                if re.fullmatch(r"\d{6,14}",token):
                    core_pos=pos; core=token
                    break
                if re.search(r"[A-Za-z]",token):
                    break
            if core_pos is None:
                continue
            between=tail[prefix_pos+1:core_pos]
            printed_separator=any(
                re.fullmatch(r"[-\u2010-\u2015]+",word.text.strip())
                for word in between
            )
            reconstructed=bool(
                not printed_separator
                and is_ccl_order
                and re.fullmatch(r"\d{2}",prefix)
                and re.fullmatch(r"\d{10}",core)
            )
            if not (printed_separator and has_order_title) and not reconstructed:
                continue
            selected=tail[prefix_pos:core_pos+1]
            value=f"{prefix} - {core}"
            method=(
                "explicit_segmented_order_number_reconstructed_separator"
                if reconstructed else "explicit_segmented_order_number"
            )
            add(value,11.2 if printed_separator else 11.0,row,method,selected)
            if reconstructed:
                reconstructed_segmented_sources[value]=" ".join(word.text for word in selected)
            break

    # Some procurement portals repeat their identifier in a dense flattened
    # heading such as ``BON DE COMMANDE REXEL - CDE n° 026432682``.  The nearby
    # supplier address can otherwise manufacture a plausible alphanumeric ID.
    for row in rows:
        if _row_center_safe(row) > pg.height*0.25:
            break
        match=re.search(
            r"\bBON\s+DE\s+COMMANDE\b[^\n]{0,120}?\bCDE\s+N\s*[°ºo]?\s*[:#.-]?\s*(\d{6,14})\b",
            row.text,
            flags=re.I,
        )
        if match:
            number_word=next((word for word in row.words if re.sub(r"\D","",word.text)==match.group(1)),None)
            add(match.group(1),10.6,row,"explicit_order_label",[number_word] if number_word else row.words)
            break

    # Legacy mainframe forms render the title as ``C O M M A N D E`` and print
    # ``N. : 009-2460-220126`` on the next visual row.
    has_spaced_order_title=bool(re.search(
        r"\bC\s+O\s+M\s+M\s+A\s+N\s+D\s+E\b",
        pg.text,
        flags=re.I,
    ))
    if has_spaced_order_title:
        for row in rows:
            if _row_center_safe(row) > pg.height*0.30:
                break
            match=re.search(
                r"(?:^|\s)N\s*[.°ºo]?\s*:\s*([A-Z0-9][A-Z0-9._/\-]{4,63})\b",
                row.text,
                flags=re.I,
            )
            if match:
                number_word=next((word for word in row.words if match.group(1) in word.text),None)
                add(match.group(1),10.4,row,"explicit_attached_numero_below_order_title",[number_word] if number_word else row.words)
                break

    # A series/number beside the order date can be unlabeled in ERP headers.
    # Never promote an OCR guess: use it only when targeted image readings agree.
    from po_ocr.extract import compact_transaction_header_candidate
    compact = compact_transaction_header_candidate(pg)
    compact_check = getattr(pg, "ocr_diagnostics", {}).get("compact_header_number", {})
    if compact:
        native = all(word.source == "native_pdf" for word in compact["words"])
        if native:
            add(compact["value"], 8.9, compact["row"], "compact_native_order_header", compact["words"])
        elif compact_check.get("confirmed") and compact_check.get("value"):
            add(compact_check["value"], 8.9, compact["row"], "compact_order_header_verified_ocr", compact["words"])

    # Same-row explicit labels: "N° de commande : 11035CA...".
    explicit_patterns=[
        (r"\bN\s*[°ºo]?\s+DE\s+COMMANDE\s*[:#.-]?\s*([A-Za-z0-9][A-Za-z0-9._/-]{2,63})", 8.0, "explicit_n_de_commande"),
        (r"\b(?:BON\s+DE\s+COMMANDE|PURCHASE\s+ORDER)\s*(?:N\s*[°ºo]?|NO|#)?\s*[:#.-]?\s*([A-Za-z0-9][A-Za-z0-9 ._/-]{2,63})", 9.6, "explicit_order_label"),
        (r"\bCOMMANDE\s*(?:N\s*[°ºo]?|NO|#)?\s*[:#.-]?\s*([A-Za-z0-9][A-Za-z0-9 ._/-]{2,63})", 7.2, "explicit_order_label"),
    ]
    for row in rows:
        if _row_center_safe(row) > pg.height*0.58:
            break
        text=row.text
        for pat,base,method in explicit_patterns:
            m=re.search(pat,text,flags=re.I)
            if m:
                if base < 8.0 and re.search(
                    r"\b(?:TABLEAU|BOITIER|CARTE|COFFRET|MODULE|PANNEAU)\s+DE\s+COMMANDE\b",
                    text,
                    flags=re.I,
                ):
                    continue
                raw=m.group(1)
                # Cut at common next-field labels/supplier block separators.
                raw=re.split(r"\s{2,}|\b(?:DATE|PAGE|CODE\s+FOURNISSEUR|FOURNISSEUR|SUPPLIER)\b",raw,maxsplit=1,flags=re.I)[0]
                number,_=_normalize_inline_po_number(raw,config)
                if number: add(number,base,row,method)

    # ERP header with an explicit number marker and internal routing groups:
    # ``N° CF 15 4 000572686``.  The title can be lost by scan OCR, while this
    # compact header remains sufficient when a DATE cell is aligned nearby.
    for row in rows:
        if _row_center_safe(row) > pg.height*0.35:
            break
        ordered=sorted(row.words,key=lambda word:word.bbox[0])
        tokens=[re.sub(r"[^a-z0-9]", "", _fold(word.text)) for word in ordered]
        for marker,token in enumerate(tokens):
            if not re.fullmatch(r"n[a-z]{0,2}",token):
                continue
            tail=[(pos,tokens[pos]) for pos in range(marker+1,min(len(tokens),marker+7)) if tokens[pos]]
            if len(tail)<4 or tail[0][1].upper() not in {"CF","CDE","CMD","CM","PO"}:
                continue
            final_positions=[
                pos for pos,(_,value) in enumerate(tail[1:],start=1)
                if re.fullmatch(r"\d{6,12}",value)
            ]
            if not final_positions:
                continue
            final_pos=final_positions[0]
            routing=[value for _,value in tail[1:final_pos]]
            if not (1<=len(routing)<=3) or not all(re.fullmatch(r"\d{1,3}",value) for value in routing):
                continue
            date_context=any(
                abs(_row_center_safe(candidate_row)-_row_center_safe(row)) <= pg.height*0.025
                and (
                    any(_fold(word.text).strip(" .:#-") == "date" for word in candidate_row.words)
                    or any(parse_date(word.text) for word in candidate_row.words)
                )
                for candidate_row in rows
            )
            if not date_context:
                continue
            selected=[ordered[tail[pos][0]] for pos in range(final_pos+1)]
            add(
                tail[0][1].upper()+tail[final_pos][1],
                9.75,
                row,
                "explicit_composite_erp_order_id",
                selected,
            )
            break

    # Some ERP headers print a bare ``N°`` value on the row below the title.
    # Associate it only with a nearby explicit order heading, never with phone,
    # fax or VAT labels elsewhere in the header.
    for idx,row in enumerate(rows):
        if _row_center_safe(row) > pg.height*0.55: break
        recent_order=any(
            re.search(
                r"(?:^|\s)(?:commande\s+fournisseur|bon\s+de\s+commande|purchase\s+order)\s*$|^commande\s*$",
                _fold(previous.text).strip(" .:#-"),
            )
            and 0 <= _row_center_safe(row)-_row_center_safe(previous) <= pg.height*0.22
            for previous in rows[max(0,idx-12):idx]
        )
        if not recent_order: continue
        attached_number=re.match(
            r"^\s*N\s*[°ºo]\s*[:#.-]?\s*([A-Za-z0-9][A-Za-z0-9 ._/-]{3,63})\s*$",
            row.text,
            flags=re.I,
        )
        if attached_number:
            number,_=_normalize_inline_po_number(attached_number.group(1),config)
            if number:
                add(number,9.5,row,"explicit_attached_numero_below_order_title")
        words=sorted(row.words,key=lambda word:word.bbox[0])
        for pos,label in enumerate(words[:-1]):
            folded_label=_fold(label.text).strip(".:#")
            if not (folded_label in {"n","no","numero"} or (folded_label.startswith("n") and len(folded_label)<=3)):
                continue
            following=[]
            for word in words[pos+1:]:
                if _fold(word.text).strip(".:#") in {"date","page"}:
                    break
                following.append(word)
            if not following: continue
            raw=" ".join(word.text for word in following)
            compact=re.match(r"^([A-Za-z]{1,5})(?:\s+\d{1,3}){1,3}\s+(\d{6,})$",raw)
            if compact and re.search(r"\bdate\b",_fold(row.text)):
                candidate=compact.group(1)+compact.group(2)
            else:
                candidate=following[0].text
                # A bare numeric token is too ambiguous here (account, phone,
                # page or branch code). Require an ERP-like alpha+digit value.
                if not (re.search(r"[A-Za-z]",candidate) and re.search(r"\d",candidate) and len(candidate)>=5):
                    continue
            add(candidate,9.35,row,"order_title_bare_number",following if compact else [following[0]])
            break

    # Label/value columns. Standalone "Numéro" is accepted only when the same header
    # row also contains Date/Commande context and not fax/telephone context.
    for idx,row in enumerate(rows):
        cy=_row_center_safe(row)
        if cy>pg.height*0.55: break
        low=_fold(row.text)
        # Compact transaction tables often have "Fournisseur | Date | N° CDE".
        # The supplier label in a neighboring cell must not invalidate the order
        # cell; associate the CDE label with its own aligned value instead.
        piece_header=bool(re.search(r"\bdate\b",low) and re.search(r"\b(?:fourn(?:isseur)?|client|reference)\b",low)
            and not re.search(r"\b(?:qte|quantite|designation|description)\b",low)
            and any(re.match(r"^(?:commande|bon\s+de\s+commande|purchase\s+order)\b",_fold(previous.text))
                    and 0<=cy-_row_center_safe(previous)<=pg.height*0.055
                    for previous in rows[max(0,idx-3):idx]))
        current_piece_value=po.purchase_order.number.value
        # This Date | Piece | Client layout is an explicit ERP order-number
        # column.  Recover it even when an earlier generic extractor has picked
        # a product description containing "... DE COMMANDE".
        piece_recovery=piece_header
        order_labels=[w for w in row.words if _fold(w.text).strip(".:#") == "cde"
                      or (piece_recovery and _fold(w.text).strip(".:#") == "piece")]
        if piece_recovery and not order_labels:
            # Encoding-damaged ``Pièce`` labels are common in old PDFs. The
            # value row still has a deterministic Date | PO | Client layout.
            for rr in rows[idx+1:idx+4]:
                if _row_center_safe(rr)-cy > pg.height*0.055: break
                ordered=sorted(rr.words,key=lambda word:word.bbox[0])
                date_positions=[pos for pos,word in enumerate(ordered) if parse_date(word.text)]
                if date_positions and date_positions[0]+1 < len(ordered):
                    candidate=ordered[date_positions[0]+1]
                    add(candidate.text,9.1,rr,"date_piece_client_order_column",[candidate])
                    break
        for label in order_labels:
            x=(label.bbox[0]+label.bbox[2])/2
            for rr in rows[idx+1:idx+4]:
                dy=_row_center_safe(rr)-cy
                if dy > pg.height*0.055: break
                near=[w for w in rr.words if abs((w.bbox[0]+w.bbox[2])/2-x)<=pg.width*0.055]
                valid=[w for w in near if not _is_invalid_po_number_v47(w.text) and not parse_date(w.text)]
                if valid:
                    nearest=min(valid,key=lambda w:abs((w.bbox[0]+w.bbox[2])/2-x))
                    add(nearest.text,9.0,rr,"below_explicit_order_column",[nearest])
                    break
        if any(x in low for x in (
            "telécopie","telecopie","telephone","téléphone","fax","tva",
            "client","customer","code fournisseur","fournisseur","supplier",
            "vendor","compte","account","siret","iban"
        )):
            continue
        num_words=[w for w in row.words if _fold(w.text) in {"numero","numéro","n°","nº","no"}]
        if not num_words:
            continue
        contextual=("date" in low or "commande" in low or "order" in low or "bon de commande" in _fold("\n".join(r.text for r in rows[max(0,idx-4):idx+1])))
        if not contextual:
            continue
        # Prefer the rightmost numeric-label word (e.g. "Date  Numéro").
        label=max(num_words,key=lambda w:w.bbox[0])
        x=(label.bbox[0]+label.bbox[2])/2
        for rr in rows[idx+1:idx+5]:
            dy=_row_center_safe(rr)-cy
            if dy<0: continue
            if dy>pg.height*float(config.get("layout",{}).get("strict_label_value_max_gap_ratio",0.075)):
                break
            near=[w for w in rr.words if abs(((w.bbox[0]+w.bbox[2])/2)-x) <= pg.width*float(config.get("layout",{}).get("strict_label_value_x_tolerance_ratio",0.12))]
            # A visually spaced identifier such as ``20 736`` can straddle
            # the left edge of the Numéro column.  Reconstruct all tightly
            # adjacent numeric tokens following the date before applying the
            # narrower x-aligned fallback below.
            ordered_row=sorted(rr.words,key=lambda word:word.bbox[0])
            date_positions=[pos for pos,word in enumerate(ordered_row) if parse_date(word.text)]
            if date_positions:
                numeric_group=[]
                previous=None
                for word in ordered_row[date_positions[0]+1:]:
                    if not re.fullmatch(r"\d{1,6}",word.text.strip()):
                        if numeric_group:
                            break
                        continue
                    if previous is not None and word.bbox[0]-previous.bbox[2] > pg.width*0.035:
                        break
                    numeric_group.append(word)
                    previous=word
                if len(numeric_group)>=2 and sum(len(word.text.strip()) for word in numeric_group)>=5:
                    add(" ".join(word.text.strip() for word in numeric_group),9.2,rr,"date_numero_spaced_value",numeric_group)
            ordered_near=sorted(near,key=lambda word:word.bbox[0])
            for start in range(len(ordered_near)):
                groups=[]; previous=None
                for word in ordered_near[start:]:
                    if not re.fullmatch(r"\d{1,6}",word.text):
                        if groups: break
                        continue
                    if previous is not None and word.bbox[0]-previous.bbox[2] > pg.width*0.045:
                        break
                    groups.append(word); previous=word
                if len(groups)>=2 and sum(len(word.text) for word in groups)>=5:
                    add(" ".join(word.text for word in groups),8.2,rr,"below_numero_label",groups)
            for w in near:
                add(w.text,7.6,rr,"below_numero_label",[w])

    # Scanned supplier-order forms sometimes place a compact ERP identifier
    # (``CF 001968062``) below a standalone COMMANDE FOURNISSEUR heading without
    # repeating a Numéro label.  Limit this recovery to the upper header and a
    # nearby explicit heading; exclude country/VAT prefixes.
    title_rows=[
        row for row in rows
        if re.search(r"(?:^|\s)commande\s+fournisseur\s*$",_fold(row.text).strip(" .:#-"))
        and len(row.words) >= 2
        and any(_fold(word.text) == "commande" for word in row.words)
        and any(_fold(word.text) == "fournisseur" for word in row.words)
        and _row_center_safe(row) <= pg.height*0.25
    ]
    for title in title_rows:
        for row in rows:
            dy=_row_center_safe(row)-_row_center_safe(title)
            if dy <= 0 or dy > pg.height*0.32:
                continue
            # Stop this header-only recovery before the product grid.  Article
            # rows commonly begin with pairs such as ``H44606 7736504817``;
            # joining those tokens would manufacture a document identifier.
            between_table_header=any(
                _row_center_safe(title) < _row_center_safe(candidate) <= _row_center_safe(row)
                and len({
                    token
                    for token in (
                        re.sub(r"[^a-z0-9]", "", _fold(word.text))
                        for word in candidate.words
                    )
                    if token in {
                        "article", "code", "reference", "ref", "designation",
                        "description", "qte", "quantite", "quantity", "pu",
                        "puht", "montant", "amount",
                    }
                }) >= 3
                for candidate in rows
            )
            if between_table_header:
                break
            ordered=sorted(row.words,key=lambda word:word.bbox[0])
            # Legacy ERP header: ``N° CF 15 4 000572686 DATE ...``. The short
            # numeric groups are routing codes; the customer PO is the series
            # plus the final long number. The nearby title and DATE/PAGE
            # boundary keep this reconstruction local and deterministic.
            folded_tokens=[
                re.sub(r"[^a-z0-9]", "", _fold(word.text))
                for word in ordered
            ]
            boundary_positions=[
                pos for pos, token in enumerate(folded_tokens)
                if token in {"date", "page"}
            ]
            for boundary in boundary_positions:
                start=max(0,boundary-7)
                segment=folded_tokens[start:boundary]
                segment_words=ordered[start:boundary]
                marker_positions=[
                    pos for pos, token in enumerate(segment)
                    if re.fullmatch(r"n[a-z]{0,2}", token)
                ]
                if not marker_positions:
                    continue
                marker=marker_positions[-1]
                values=[token for token in segment[marker+1:] if token]
                if not (
                    3 <= len(values) <= 5
                    and re.fullmatch(r"[a-z]{1,5}", values[0])
                    and all(re.fullmatch(r"\d{1,3}", token) for token in values[1:-1])
                    and re.fullmatch(r"\d{6,}", values[-1])
                ):
                    continue
                prefix=values[0].upper()
                if prefix in {"FR","DE","IT","ES","BE","NL","LU","GB","TVA","TEL","FAX"}:
                    continue
                add(
                    prefix+values[-1],
                    9.7,
                    row,
                    "supplier_order_heading_composite_id",
                    segment_words,
                )
                break
            for left,right in zip(ordered,ordered[1:]):
                prefix=re.sub(r"[^a-z]","",_fold(left.text)).upper()
                digits=re.sub(r"\D","",right.text)
                if (
                    1 <= len(prefix) <= 4
                    and prefix not in {"FR","DE","IT","ES","BE","NL","LU","GB","TVA","TEL","FAX"}
                    and len(digits) >= 6
                    and right.bbox[0]-left.bbox[2] <= pg.width*0.035
                ):
                    add(prefix+digits,9.25,row,"supplier_order_heading_compact_id",[left,right])
                    break

    # Preserve a reliable existing identifier. The strict resolver replaces
    # contaminated candidates; it must not truncate good multi-token IDs.
    current=po.purchase_order.number.value
    current_field=po.purchase_order.number
    current_evidence=getattr(current_field,"evidence",None)
    current_method=getattr(current_evidence,"extraction_method",None) if current_evidence else None
    current_source=getattr(current_evidence,"source_text",None) if current_evidence else None
    source_low=_fold(current_source or "")
    current_semantic_contamination=any(x in source_low for x in (
        "code fournisseur","supplier code","vendor code","n° client","no client",
        "numero client","numéro client","customer","telephone","téléphone",
        "telécopie","telecopie","fax","tva","siret","iban"
    ))
    if current and not _is_invalid_po_number_v47(str(current)) and not current_semantic_contamination:
        trusted_methods=set(config.get("layout",{}).get("strict_po_trusted_existing_methods",[
            "native_table_anchor","multiblock_order_number","explicit_order_label",
            "explicit_n_de_commande","inline_transaction_header","spatial_order_number"
        ]))
        min_conf=float(config.get("layout",{}).get("strict_po_trusted_existing_min_confidence",0.965))
        conf=float(getattr(current_field,"final_confidence",0.0) or 0.0)
        score_existing=8.65 if (current_method in trusted_methods and conf>=min_conf) else 6.25
        if len(str(current).split())>=2:
            # Prefix-bearing ERP identifiers (PR S CH 01078, ST C CSP 33443, ...)
            # must not be truncated by a weaker standalone Numéro cell underneath.
            score_existing=max(score_existing,8.25)
        class Dummy: pass
        d=Dummy(); d.words=[]; d.bbox=None
        add(str(current),score_existing,d,"existing_candidate",[])

    if candidates:
        best=max(candidates,key=lambda x:(x[0],sum(ch.isalpha() for ch in x[1]),len(x[1])))
        score_v,val,row,method,words=best
        if method == "existing_candidate":
            # The winning field already owns its original evidence/confidence.
            # Repackaging the placeholder row would erase its bbox and inflate OCR
            # confidence merely because the same candidate survived another pass.
            return po
        conf=min(0.999,0.93+0.008*score_v)
        if method == "compact_order_header_verified_ocr":
            conf=min(conf, max(0.0, float(compact_check.get("confidence", 0.0))))
        po.purchase_order.number=_make_spatial_field(val,pg.page,_bbox_union(words) if words else getattr(row,"bbox",None),confidence=conf,method=method)
        if method == "explicit_segmented_order_number_reconstructed_separator":
            source_text=reconstructed_segmented_sources.get(val)
            if source_text:
                po.purchase_order.number.raw_value=source_text
                po.purchase_order.number.evidence.source_text=source_text
            po.purchase_order.number.warnings.append(
                "Separator reconstructed from corroborated C.C.L. order layout after scan OCR omitted the printed hyphen."
            )
        if method == "compact_order_header_verified_ocr":
            po.purchase_order.number.raw_value=compact_check.get("original")
            po.purchase_order.number.evidence.source_text=compact_check.get("original")
            po.purchase_order.number.final_confidence=min(po.purchase_order.number.final_confidence,conf)
            original_compact=re.sub(r"\s+", "", str(compact_check.get("original") or "")).upper()
            if original_compact != val:
                po.purchase_order.number.warnings.append("Order series corrected after two agreeing local OCR crop readings.")
    elif current and _is_invalid_po_number_v47(str(current)):
        # Never preserve a semantically impossible identifier just because another
        # parser returned it with high OCR confidence.
        po.purchase_order.number.value=None
        po.purchase_order.number.normalized_value=None
        po.purchase_order.number.raw_value=None
        po.purchase_order.number.final_confidence=0.0
        po.purchase_order.number.warnings.append("Rejected by V4.7 PO-number type guard")
    return po


def _party_from_lines_v47(lines: list[str], *, role: str):
    """Parse a compact party/address block while keeping service/contact separate."""
    from po_ocr.models import Party, Address
    import re
    clean=[]; phone=None; fax=None; email=None; contact=None
    stop_markers=("article","designation","quantite","qte","prix","montant","total")
    for raw in lines:
        text=" ".join(str(raw or "").split()).strip()
        if not text: continue
        low=_fold(text)
        if any(low.startswith(x) for x in stop_markers): break
        combo=re.search(r"T[eé]l\s*/\s*Fax\s*/\s*Mail\s+([+0-9][0-9 .-]{6,})\s*/\s*([+0-9][0-9 .-]{6,})\s*/\s*([^\s]+@[^\s]+)",text,flags=re.I)
        if combo:
            phone=" ".join(combo.group(1).split()).strip(" -")
            fax=" ".join(combo.group(2).split()).strip(" -")
            email=combo.group(3).strip()
            continue
        em=re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",text)
        if em: email=em.group(0)
        fm=re.search(r"\b(?:fax|telecopie|télécopie)\s*[:.-]?\s*([+0-9][0-9 .-]{6,})",text,flags=re.I)
        tm=re.search(r"\b(?:tel|t[eé]l[eé]phone|t[eé]l\.)\s*[:.-]?\s*([+0-9][0-9 .-]{6,})",text,flags=re.I)
        if fm: fax=" ".join(fm.group(1).split()).strip(" -")
        if tm: phone=" ".join(tm.group(1).split()).strip(" -")
        if em or fm or tm:
            # Retain non-contact prefix only if meaningful.
            prefix=text[:min([x.start() for x in (em,fm,tm) if x] or [0])].strip(" /:-")
            if not prefix: continue
            text=prefix; low=_fold(text)
        if re.fullmatch(r"France\s+\d{3,10}",text,flags=re.I):
            text="France"
        if re.fullmatch(r"\d{3,10}",text):
            continue
        clean.append(text)
    if not clean:
        p=Party(phone=phone,fax=fax,email=email); p.contact.phone=phone;p.contact.fax=fax;p.contact.email=email; return p

    def address_like(v):
        low=_fold(v)
        return bool(re.search(r"\b(?:rue|avenue|av\.?|boulevard|bd\.?|route|impasse|chemin|zac|zae|zi|z\.i\.|bp|cs|tsa|street|strasse|straße)\b",low) or re.search(r"\b\d{5}\b",v))
    first_addr=next((i for i,v in enumerate(clean) if address_like(v)),len(clean))
    pre=clean[:first_addr]; addr=clean[first_addr:]
    name=pre[0] if pre else (clean[0] if not address_like(clean[0]) else None)
    department=" / ".join(pre[1:]) if len(pre)>1 else None
    # Delivery-site labels are legitimate party names even if no legal suffix exists.
    if role=="buyer" and name and _bad_party_name(name): name=None
    address=Address()
    if addr:
        address.line1=addr[0]; address.line2=addr[1] if len(addr)>1 else None; address.line3=addr[2] if len(addr)>2 else None
        for line in addr:
            m=re.search(r"\b(\d{5})\s+([A-Za-zÀ-ÿ0-9 .'-]{2,})",line)
            if m:
                address.postal_code=m.group(1); address.city=" ".join(m.group(2).split()); break
        if any(_fold(x) in {"france","fr"} for x in addr): address.country="France";address.country_code="FR"
    p=Party(name=name,legal_name=name,department=department,address=address,phone=phone,fax=fax,email=email)
    p.contact.phone=phone;p.contact.fax=fax;p.contact.email=email;p.contact.name=contact;p.contact.department=department
    return p


def _enhance_native_party_blocks_v47(po, tables, pages, config: dict):
    """Prioritize explicit native party blocks and labelled address zones."""
    import re
    if not config.get("layout",{}).get("native_party_block_recovery",True): return po
    # Native tables often preserve supplier/bill-to/ship-to blocks more faithfully than
    # flattened row order. Use them first.
    supplier_candidates=[]
    for table in tables:
        for row in table.data or []:
            for cell in row:
                raw=str(cell or "")
                if not raw.strip(): continue
                low=_fold(raw)
                lines=_split_cell_lines(raw)
                if "facture a" in low or "facturé à" in raw.casefold() or "facture à" in raw.casefold():
                    party=_parse_labeled_party_cell(raw,role="bill_to")
                    if party.name: po.bill_to=party
                if "adresse de livraison" in low or "shipping address" in low:
                    # Strip delivery instructions/footer identifiers after a country or
                    # explicit instruction marker.
                    stripped=[]
                    for line in lines:
                        ll=_fold(line)
                        if re.search(r"\b(?:merci de livrer|livraison le|horaires|instruction)\b",ll):
                            continue
                        if re.fullmatch(r"France\s+\d+",line,flags=re.I): line="France"
                        stripped.append(line)
                    party=_parse_labeled_party_cell("\n".join(stripped),role="ship_to")
                    if party.name: po.ship_to=party
                # Unlabelled multiline block with company + address + tel/fax/mail is a
                # strong supplier candidate in procurement headers.
                if len(lines)>=3 and "@" in raw and any(re.search(r"\b\d{5}\b",x) for x in lines) and not any(x in low for x in ("facture a","adresse de livraison")):
                    party=_party_from_lines_v47(lines,role="supplier")
                    if party.name:
                        supplier_candidates.append((len(lines)+_party_name_strength(party.name),party))
    if supplier_candidates:
        party=max(supplier_candidates,key=lambda x:x[0])[1]
        if _bad_party_name(po.supplier.name) or _party_name_strength(po.supplier.name)<_party_name_strength(party.name)-0.1:
            code=po.supplier.code
            po.supplier=party
            po.supplier.code=code

    # A billing party is usually the ordering legal entity when no stronger buyer block
    # was extracted. Preserve contact metadata already found elsewhere.
    if po.bill_to.name and (_bad_party_name(po.buyer.name) or not po.buyer.name or "contact" in _fold(po.buyer.name)):
        old_contact=po.buyer.contact.model_copy(deep=True)
        po.buyer=po.bill_to.model_copy(deep=True)
        for attr in ("name","email","phone","fax"):
            val=getattr(old_contact,attr,None)
            if val: setattr(po.buyer.contact,attr,val)

    # Header contact/email belong to the buyer/ordering organization, not supplier.
    if pages:
        from po_ocr.extract import all_rows
        rows=all_rows([pages[0]],y_factor=0.50)
        for row in rows:
            if _row_center_safe(row)>pages[0].height*0.30: break
            text=row.text
            cm=re.search(r"\bContact\s*[:.-]?\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]{2,80}?)(?=\s{2,}|\s+[A-Za-z0-9._%+-]+@|$)",text,flags=re.I)
            if cm: po.buyer.contact.name=" ".join(cm.group(1).split())
            mm=re.search(r"\b(?:Mail|Courriel|E-?mail)\s*[:.-]?\s*([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",text,flags=re.I)
            if mm: po.buyer.contact.email=mm.group(1);po.buyer.email=po.buyer.email or mm.group(1)
            tm=re.search(r"\bT[eé]l\.?\s*[:.-]?\s*([+0-9][0-9 .-]{6,})",text,flags=re.I)
            if tm and ("contact" in _fold(text) or _row_center_safe(row)<pages[0].height*0.24):
                po.buyer.contact.phone=" ".join(tm.group(1).split()).strip(" -");po.buyer.phone=po.buyer.phone or po.buyer.contact.phone
            cm2=re.search(r"\bCode\s+fournisseur\s*[:.-]?\s*([A-Za-z0-9._/-]{2,30})",text,flags=re.I)
            if cm2: po.supplier.code=cm2.group(1)
    return po


def _enhance_column_labeled_parties_v47(po, pages, config: dict):
    """Recover party/address roles from multi-column label/value layouts."""
    from po_ocr.extract import all_rows
    from po_ocr.models import Address
    import re
    if not pages or not config.get("layout",{}).get("column_labeled_party_recovery",True): return po
    pg=pages[0]; rows=all_rows([pg],y_factor=0.50); page_width=pg.width; h=pg.height
    # Exact "Adresse de livraison" column: use the x-position of Adresse, not the first
    # generic word "livraison" elsewhere on the row.
    for idx,row in enumerate(rows):
        low=_fold(row.text)
        if _row_center_safe(row)>h*0.62: break
        if "adresse de livraison" not in low: continue
        addr_word=next((x for x in row.words if _fold(x.text)=="adresse"),None)
        if not addr_word: continue
        left=max(0.0,addr_word.bbox[0]-page_width*0.025); right=page_width
        # If another label exists to the right, stop before it.
        right_labels=[x for x in row.words if x.bbox[0]>addr_word.bbox[2]+20 and _fold(x.text) in {"merci","instructions","facturation"}]
        if right_labels: right=min(x.bbox[0] for x in right_labels)-5
        vals=[]; y0=_row_center_safe(row)
        for rr in rows[idx+1:]:
            cy=_row_center_safe(rr)
            if cy-y0>h*0.18: break
            if any(k in _fold(rr.text) for k in ("reference designation","article designation","quantite prix","montant ht")): break
            txt=" ".join(x.text for x in rr.words if x.bbox[0]>=left and x.bbox[2]<=right).strip()
            if not txt: continue
            fl=_fold(txt)
            if re.search(r"\b(?:merci de livrer|livraison le matin|page \d+)\b",fl): continue
            if re.fullmatch(r"France\s+\d+",txt,flags=re.I): txt="France"
            vals.append(txt)
        party=_party_from_lines_v47(vals,role="ship_to")
        current_good = bool(po.ship_to.name and not _bad_party_name(po.ship_to.name) and (po.ship_to.address.postal_code or po.ship_to.address.line1))
        if party.name and not current_good: po.ship_to=party
        break

    # Top supplier candidate on the right side for sparse forms (e.g. supplier name is
    # native text but not wrapped in an address table).
    candidates=[]
    for row in rows:
        cy=_row_center_safe(row)
        if cy>h*0.34: break
        right_words=[x for x in row.words if x.bbox[0]>=page_width*0.48]
        txt=" ".join(x.text for x in right_words).strip()
        if not txt: continue
        low=_fold(txt)
        if any(x in low for x in ("date","numero","numéro","telécopie","telecopie","fax","total")): continue
        if "@" in txt or re.fullmatch(r"[0-9 .+-]+",txt): continue
        if re.search(r"\b(?:rue|avenue|route|impasse|\d{5})\b",low): continue
        strength=_party_name_strength(txt)
        # Short all-cap company names such as ELM LEBLANC are valid even without SAS.
        if strength>=0.8 and len(txt)<=80:
            candidates.append((strength,cy,txt,row))
    if candidates and (_bad_party_name(po.supplier.name) or not po.supplier.name):
        _,_,name,row=max(candidates,key=lambda x:(x[0],-x[1]))
        po.supplier.name=name;po.supplier.legal_name=name

    # If buyer name is still an address/phone/email (or accidentally equals the
    # supplier), infer from a non-generic corporate email domain as a last resort.
    buyer_conflicted = bool(po.buyer.name and po.supplier.name and _fold(po.buyer.name)==_fold(po.supplier.name))
    if _bad_party_name(po.buyer.name) or not po.buyer.name or "@" in str(po.buyer.name) or buyer_conflicted:
        email_hits=[]
        for ridx,row in enumerate(rows):
            for m in re.finditer(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})",row.text):
                email_hits.append((ridx,m.group(1),m.group(2).lower()))
        generic={"gmail.com","outlook.com","hotmail.com","yahoo.com","orange.fr","wanadoo.fr"}
        supplier_domains=set()
        if po.supplier.email and "@" in po.supplier.email: supplier_domains.add(po.supplier.email.rsplit("@",1)[1].lower())
        for ridx,local,domain in email_hits:
            if domain in generic or domain in supplier_domains: continue
            root=domain.split(".")[0]
            if len(root)>=3:
                name=re.sub(r"[-_]+"," ",root).upper()
                if len(name)>=4:
                    po.buyer.name=name;po.buyer.legal_name=name
                    # Recover nearby left-column address/phone evidence when the logo
                    # itself is raster-only and absent from the PDF text layer.
                    nearby=rows[max(0,ridx-6):ridx+1]
                    addr=[]
                    for rr in nearby:
                        txt=" ".join(w.text for w in rr.words if w.bbox[2] < page_width*0.48).strip()
                        if re.search(r"\b(?:rue|avenue|route|impasse|boulevard|chemin)\b",_fold(txt)) or re.search(r"\b\d{5}\b",txt):
                            addr.append(txt)
                        tm=re.search(r"\bT[eé]l[eé]phone\s*[:.-]?\s*([+0-9][0-9 .-]{6,})",txt,flags=re.I)
                        if tm: po.buyer.phone=" ".join(tm.group(1).split()).strip(" -");po.buyer.contact.phone=po.buyer.phone
                    if addr:
                        from po_ocr.models import Address
                        po.buyer.address=Address(line1=addr[0],line2=addr[1] if len(addr)>1 else None)
                        for a in addr:
                            pm=re.search(r"\b(\d{5})\s+([A-Za-zÀ-ÿ .'-]{2,})",a)
                            if pm: po.buyer.address.postal_code=pm.group(1);po.buyer.address.city=" ".join(pm.group(2).split());break
                    break

    # Explicit fax/telephone labels in a supplier column are typed evidence and must
    # never be reused as PO numbers or party names.
    for idx,row in enumerate(rows):
        low=_fold(row.text)
        fax_label_words=[wd for wd in row.words if _fold(wd.text) in {"fax","telécopie","telecopie"}]
        # Supplier fax recovery is restricted to the supplier/right column. Buyer-side
        # contact fax or VAT numbers must never overwrite a valid supplier contact.
        if ("telécopie" in low or "telecopie" in low or "fax" in low) and fax_label_words and max(wd.bbox[0] for wd in fax_label_words)>=page_width*0.48:
            local=[row]+rows[idx+1:idx+3]
            vals=[]
            for rr in local:
                right_text=" ".join(wd.text for wd in rr.words if wd.bbox[0]>=page_width*0.48)
                for m in re.finditer(r"(?:\+?\d[\d .-]{7,}\d)",right_text): vals.append(m.group(0))
            if vals:
                po.supplier.fax=vals[-1].strip();po.supplier.contact.fax=po.supplier.fax
                break
    # A billing party is stronger than an address-fragment buyer name. This condition
    # specifically detects when the guessed buyer name occurs inside a delivery address.
    if po.bill_to.name:
        ship_addr=" ".join(x for x in (po.ship_to.address.line1,po.ship_to.address.line2,po.ship_to.address.line3) if x)
        if _bad_party_name(po.buyer.name) or (po.buyer.name and _fold(po.buyer.name) in _fold(ship_addr)):
            contact=po.buyer.contact.model_copy(deep=True)
            po.buyer=po.bill_to.model_copy(deep=True)
            if contact.name: po.buyer.contact.name=contact.name
            if contact.email: po.buyer.contact.email=contact.email;po.buyer.email=po.buyer.email or contact.email
            if contact.phone: po.buyer.contact.phone=contact.phone;po.buyer.phone=po.buyer.phone or contact.phone
    return po


def _extract_amount_only_geometry_v47(pages, config: dict, default_currency: str="EUR"):
    """Parse tables with Quantity + Line Amount but no unit-price column."""
    from po_ocr.extract import all_rows
    from po_ocr.models import LineItem
    from po_ocr.normalize import parse_number
    import re
    if not config.get("layout",{}).get("amount_only_geometry_recovery",True): return []
    out=[]
    for pg in pages:
        rows=all_rows([pg],y_factor=0.50); header_idx=None; centers={}
        for i,row in enumerate(rows):
            low=_fold(row.text)
            if "reference" in low and ("designation" in low or "description" in low) and ("qte" in low or "quantite" in low) and "montant" in low:
                for wd in row.words:
                    wl=_fold(wd.text); cx=(wd.bbox[0]+wd.bbox[2])/2
                    if "reference" in wl: centers.setdefault("ref",cx)
                    elif "designation" in wl or "description" in wl: centers.setdefault("desc",cx)
                    elif wl in {"qte","qty","quantite"}: centers.setdefault("qty",cx)
                    elif "montant" in wl: centers.setdefault("amount",cx)
                if all(k in centers for k in ("ref","desc","qty","amount")):
                    header_idx=i;break
        if header_idx is None: continue
        qx,ax=centers["qty"],centers["amount"]
        ref_desc=(centers["ref"]+centers["desc"])/2
        # Keep the description column wide; product model tokens such as "5600S"
        # often sit far to the right of the DESIGNATION header but still well before QTE.
        q_left=qx-max(26.0,pg.width*float(config.get("layout",{}).get("amount_only_quantity_left_margin_ratio",0.060)))
        qa=(qx+ax)/2
        current=None
        for row in rows[header_idx+1:]:
            low=_fold(row.text)
            if "total ht" in low: break
            left=[w for w in row.words if w.bbox[2]<ref_desc]
            code=" ".join(w.text for w in left).strip()
            qty_txt=" ".join(w.text for w in row.words if q_left<=((w.bbox[0]+w.bbox[2])/2)<qa).strip()
            amount_txt=" ".join(w.text for w in row.words if ((w.bbox[0]+w.bbox[2])/2)>=qa).strip()
            desc=" ".join(w.text for w in row.words if ref_desc<=((w.bbox[0]+w.bbox[2])/2)<q_left).strip()
            if code and re.fullmatch(r"[A-Za-z0-9._/-]{5,40}",code) and re.search(r"\d",code):
                qty=parse_number(qty_txt); amount=parse_number(amount_txt)
                if qty is None or amount is None: continue
                current=LineItem(line_number=str(len(out)+1),material_number=code,article_number=code,description=desc or None,
                    quantity=qty,ordered_quantity=qty,line_total=amount,line_net_amount=amount,currency=default_currency,
                    raw_text=row.text,page=pg.page,bbox=row.bbox,confidence=float(row.confidence)*0.995)
                out.append(current); continue
            if current and desc:
                # Continuation rows: quote/ref notes and wrapped product description.
                qm=re.search(r"(?:offre|devis)\s*n[°ºo]?\s*[:.-]?\s*([A-Za-z0-9._/-]{3,50})",desc,flags=re.I)
                if qm: current.quote_number=qm.group(1); current.notes.append(desc)
                elif re.fullmatch(r"\d{8,14}",desc) or re.search(r"[A-Za-zÀ-ÿ]{2,}",desc):
                    current.description=" ".join(x for x in (current.description,desc) if x)
    return _dedupe_line_items(out)


def _enhance_strict_totals_v47(po, pages, config: dict):
    """Recover labelled totals conservatively and preserve reconciled totals."""
    from po_ocr.extract import all_rows
    from po_ocr.normalize import parse_number
    import re
    if not pages or not config.get("layout",{}).get("strict_total_label_recovery",True):
        return po
    existing=po.totals.total_net if po.totals.total_net is not None else po.totals.subtotal
    line_vals=[x.line_total for x in po.lines if x.line_total is not None]
    calc=sum(float(x) for x in line_vals) + sum(float(x.amount or 0.0) for x in getattr(po,"additional_charges",[]))
    if existing is not None and line_vals and config.get("layout",{}).get("strict_total_prefer_reconciled_existing",True):
        tol=max(float(config.get("validation",{}).get("amount_absolute_tolerance",0.03)),
                abs(float(existing))*float(config.get("validation",{}).get("amount_relative_tolerance",0.005)))
        if abs(float(existing)-calc)<=tol:
            return po
    for pg in pages:
        rows=all_rows([pg],y_factor=0.50)
        for i,row in enumerate(rows):
            low=_fold(row.text)
            if not re.search(r"\btotal\s+(?:h\.?t\.?|net\s+ht)\b",low):
                continue
            label_words=[w for w in row.words if any(k in _fold(w.text) for k in ("total","net"))]
            label_x=min((w.bbox[0] for w in label_words),default=0.0)
            candidates=[]
            max_next=max(1,int(config.get("layout",{}).get("strict_total_search_next_rows",2)))
            for ridx,rr in enumerate([row]+rows[i+1:i+1+max_next]):
                # Row-level parsing keeps grouped thousands together (e.g.
                # ``3 856,61``) whereas word-level parsing may see only ``856,61``.
                grouped=[]
                for m in re.finditer(r"(?<!\d)(?:\d{1,3}(?:[ .']\d{3})+|\d+)[,.]\d{2}(?!\d)", rr.text):
                    v=parse_number(m.group(0))
                    if v is not None:
                        grouped.append(float(v))
                        candidates.append(((4.0 if ridx==0 else 3.0),float(v),None))
                # Do not let a fragment word (``856,61``) outrank its grouped row
                # representation (``3 856,61``).
                if grouped:
                    continue
                for w in rr.words:
                    txt=str(w.text).strip()
                    if config.get("layout",{}).get("strict_total_require_decimal_token",True) and not re.search(r"[,.]\d{2}\b",txt):
                        continue
                    if w.bbox[0] < label_x - pg.width*0.03:
                        continue
                    v=parse_number(txt)
                    if v is None or abs(float(v)) > float(config.get("layout",{}).get("strict_total_max_abs_value",1_000_000_000.0)):
                        continue
                    if ridx==1 and w.bbox[0] < max(label_x,pg.width*0.45):
                        continue
                    score=(2.0 if ridx==0 else 1.0)+(w.bbox[0]/max(1.0,pg.width))
                    candidates.append((score,float(v),w))
            if candidates:
                _,value,_=max(candidates,key=lambda x:x[0])
                if line_vals and calc:
                    ratio=max(abs(value),abs(calc))/max(0.01,min(abs(value),abs(calc)))
                    if ratio > float(config.get("layout",{}).get("strict_total_max_reconciliation_ratio",5.0)):
                        continue
                po.totals.total_net=value
                po.totals.subtotal=value
                po.totals.total_before_tax=value
                po.totals.currency=po.totals.currency or "EUR"
                return po
    return po


def _sanitize_double_counted_shipping_v47(po, config: dict):
    """Drop separate freight/shipping totals when product-line sum already equals net.

    This prevents a line item such as PORTSTD/FRAIS DE PORT from being counted both as a
    product line and again as a document-level shipping component.
    """
    if not config.get("layout",{}).get("prevent_shipping_double_count",True): return po
    if not po.lines or po.totals.total_net is None: return po
    vals=[x.line_total for x in po.lines if x.line_total is not None]
    if not vals: return po
    line_sum=sum(vals)+sum(x.amount or 0.0 for x in getattr(po,"additional_charges",[]))
    tol=max(float(config.get("validation",{}).get("amount_absolute_tolerance",0.03)),abs(po.totals.total_net)*float(config.get("validation",{}).get("amount_relative_tolerance",0.005)))
    if abs(line_sum-po.totals.total_net)<=tol:
        po.totals.total_shipping=None
        po.totals.total_freight=None
    return po


def _sanitize_internal_reference_v47(po):
    import re
    val=po.purchase_order.internal_number.value
    if val and (re.fullmatch(r"\\d{5}\\s+[A-Za-zÀ-ÿ .'-]+",str(val).strip()) or (po.supplier.address.postal_code and str(po.supplier.address.postal_code) in str(val))):
        po.purchase_order.internal_number.value=None;po.purchase_order.internal_number.normalized_value=None;po.purchase_order.internal_number.raw_value=None;po.purchase_order.internal_number.final_confidence=0.0
    return po

def _row_center_safe(row) -> float:
    if not getattr(row, "words", None):
        return 0.0
    return sum((w.bbox[1] + w.bbox[3]) / 2 for w in row.words) / len(row.words)



# --- V4.8 role-aware extraction, footer guards, and summary-table recovery -------

def _company_name_quality_v48(value: str | None) -> float:
    """Score whether a string plausibly names a business/site rather than an instruction/address."""
    import re
    text=" ".join(str(value or "").split()).strip()
    low=_fold(text)
    if not text: return 0.0
    if "@" in text or re.fullmatch(r"[+0-9 .()/-]{7,}",text): return 0.0
    instruction_terms=(
        "retourner sous", "merci de livrer", "suivant offre", "franco", "date de livraison",
        "a facturer", "a livrer", "adresse de livraison", "commande n", "bon de commande",
        "conditions de paiement", "mode livraison", "total ht", "net a payer", "contact :",
    )
    if any(x in low for x in instruction_terms): return 0.05
    if re.match(r"^(?:rue|avenue|av\.?|route|impasse|boulevard|bd\.?|chemin|zac|zae|zi|z\.i\.)\b",low): return 0.08
    if re.match(r"^\d+[a-z]?\s+(?:rue|avenue|av\.?|route|impasse|boulevard|bd\.?|chemin)\b",low): return 0.05
    if re.fullmatch(r"\d{4,6}\s+.+",text): return 0.05
    if len(text)>95: return 0.25
    alpha=sum(ch.isalpha() for ch in text)
    if alpha<3: return 0.15
    business_bonus=0.12 if any(x in low for x in ("sas","sarl","sa ","sasu","groupe","holding","distribution","energie","chauffage","leblanc","bosch","ccl","iserba","regmatherm","unicia")) else 0.0
    return min(1.0,0.50+alpha/max(12,len(text))*0.42+business_bonus)


def _clean_po_number_v48(po, config: dict):
    """Remove semantic tail contamination from otherwise strong order identifiers.

    Examples: ``CDEA-138140 ELM LEBLANC`` -> ``CDEA-138140`` while preserving
    legitimate multi-token identifiers such as ``ST C CSP 33443``.
    """
    import re
    if not config.get("layout",{}).get("po_number_tail_cleanup",True): return po
    field=po.purchase_order.number
    raw=" ".join(str(field.value or "").split()).strip()
    if not raw: return po
    # Hybrid/native PDF layers can duplicate every visual token. Preserve the
    # complete composite identifier instead of dropping its slash suffix:
    # ``5416718 5416718 / 1877 1877`` -> ``5416718 / 1877``.
    # Keep the separator spacing printed by the customer.  This value is
    # exposed directly to API/UI consumers, so removing the space after the
    # slash made an otherwise correct identifier look truncated.
    repeated_composite = re.fullmatch(
        r"([A-Za-z0-9][A-Za-z0-9._-]{3,39})\s+\1\s*/\s*"
        r"([A-Za-z0-9][A-Za-z0-9._-]{1,39})\s+\2",
        raw, flags=re.I,
    )
    if repeated_composite:
        clean = f"{repeated_composite.group(1)} / {repeated_composite.group(2)}"
        field.value=field.raw_value=field.normalized_value=clean
        field.semantic_confidence=max(float(field.semantic_confidence or 0.0),0.99)
        field.final_confidence=max(float(field.final_confidence or 0.0),0.985)
        if getattr(field,"evidence",None): field.evidence.extraction_method="po_number_composite_duplicate_cleanup_v60"
        return po
    repeated = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]{3,39})\s+\1", raw, flags=re.I)
    if repeated:
        clean = repeated.group(1)
        field.value=field.raw_value=field.normalized_value=clean
        field.semantic_confidence=max(float(field.semantic_confidence or 0.0),0.99)
        field.final_confidence=max(float(field.final_confidence or 0.0),0.985)
        if getattr(field,"evidence",None): field.evidence.extraction_method="po_number_duplicate_cleanup_v52"
        return po
    prefixed = re.match(r"^(?:client|customer)\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9._/-]{4,39})$", raw, flags=re.I)
    if prefixed and any(ch.isdigit() for ch in prefixed.group(1)):
        clean = prefixed.group(1)
        field.value=field.raw_value=field.normalized_value=clean
        field.semantic_confidence=max(float(field.semantic_confidence or 0.0),0.99)
        field.final_confidence=max(float(field.final_confidence or 0.0),0.985)
        if getattr(field,"evidence",None): field.evidence.extraction_method="po_number_label_cleanup_v52"
        raw=clean
    instruction_tail = re.match(
        r"^([A-Za-z0-9][A-Za-z0-9._/-]{3,39})\s+(?:POUR\s+FRANCO|FRANCO|SVP|MERCI\b).*$",
        raw, flags=re.I,
    )
    if instruction_tail and any(ch.isdigit() for ch in instruction_tail.group(1)):
        clean=instruction_tail.group(1)
        field.value=field.raw_value=field.normalized_value=clean
        field.semantic_confidence=max(float(field.semantic_confidence or 0.0),0.98)
        field.final_confidence=max(float(field.final_confidence or 0.0),0.975)
        if getattr(field,"evidence",None): field.evidence.extraction_method="po_number_instruction_cleanup_v52"
        raw=clean
    # First token is an ERP-like identifier containing both digit and a strong separator/alpha signal.
    parts=raw.split()
    if len(parts)>=2:
        first=parts[0]
        rest=" ".join(parts[1:])
        first_is_id=bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{3,39}",first) and any(ch.isdigit() for ch in first))
        # Do not truncate compact multi-block IDs such as ST C CSP 33443 where the first token has no digit.
        if first_is_id and ("-" in first or "/" in first or (any(ch.isalpha() for ch in first) and len(first)>=8)):
            if _company_name_quality_v48(rest)>=float(config.get("layout",{}).get("po_number_tail_party_quality_threshold",0.68)):
                field.value=field.raw_value=field.normalized_value=first
                field.semantic_confidence=max(float(field.semantic_confidence or 0.0),0.99)
                field.final_confidence=max(float(field.final_confidence or 0.0),0.985)
                if getattr(field,"evidence",None): field.evidence.extraction_method="po_number_tail_cleanup_v48"
    return po


_FR_VAT_RE_V52 = __import__("re").compile(
    r"(?<![A-Z0-9])F\s*R\s*([A-Z0-9](?:[ .-]*[A-Z0-9]))\s*"
    r"((?:[0-9OIL][ .-]*){8}[0-9OIL])(?![A-Z0-9])",
    __import__("re").I,
)


def _valid_french_vat_v52(value: str) -> bool:
    if not __import__("re").fullmatch(r"FR\d{11}", value):
        return False
    key=int(value[2:4]); siren=int(value[4:])
    return key == (12 + 3 * (siren % 97)) % 97


def _extract_vat_identifiers_v52(po, pages, config: dict):
    """Extract all French VAT identifiers, including validated unlabeled footers."""
    if not config.get("layout",{}).get("vat_identifier_extraction",True): return po
    import re
    labelled_terms=("tva","vat","intracom","intra communautaire","tax number")
    seen=set(); found=[]
    for row in all_rows(pages,y_factor=0.58):
        text=" ".join(str(row.text or "").split())
        folded=_fold(text)
        for match in _FR_VAT_RE_V52.finditer(text.upper()):
            compact="".join((match.group(1)+match.group(2)).split()).replace(".","").replace("-","")
            corrected=compact.translate(str.maketrans({"O":"0","I":"1","L":"1"}))
            candidate="FR"+corrected
            labelled=any(term in folded for term in labelled_terms)
            valid=_valid_french_vat_v52(candidate)
            if not labelled and not valid:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            role="supplier" if any(term in folded for term in ("fournisseur","supplier","vendor")) else (
                "buyer" if any(term in folded for term in ("client","acheteur","buyer")) else "buyer_or_issuer"
            )
            warnings=[]
            if compact != corrected: warnings.append("ocr_characters_normalized")
            if not valid: warnings.append("french_vat_checksum_not_verified")
            found.append({
                "type":"vat", "vat_number":candidate, "country_code":"FR", "role":role,
                "confidence":0.995 if valid and labelled else (0.97 if valid else 0.86),
                "evidence":{"page":row.page,"bbox":row.bbox,"source_text":text,
                            "extraction_method":"vat_regex_checksum_v52"},
                "warnings":warnings,
            })
    po.tax_identifiers=found
    for item in found:
        target=po.supplier if item["role"]=="supplier" else po.buyer
        if target is not None and not target.vat_number:
            target.vat_number=item["vat_number"]
    return po


def _legal_footer_line_v48(line, config: dict) -> bool:
    import re
    if not config.get("layout",{}).get("reject_legal_footer_lines",True): return False
    raw=" ".join(str(getattr(line,"raw_text","") or "").split())
    desc=" ".join(str(getattr(line,"description","") or "").split())
    material=str(getattr(line,"material_number","") or "")
    low=_fold(" ".join((raw,desc)))
    terms=tuple(_fold(x) for x in config.get("layout",{}).get("legal_footer_line_terms",[
        "siren","siret","rcs","iban","bic","capital de","capital social","n° intracom","no intracom","tva intracom","ape ","naf "
    ]))
    hits=sum(1 for x in terms if x and x in low)
    if hits>=int(config.get("layout",{}).get("legal_footer_min_term_hits",1)):
        # Legitimate material rows rarely contain corporate-registration vocabulary.
        return True
    if material and re.fullmatch(r"\d{9}|\d{14}",material) and any(x in low for x in ("siren","siret","rcs")):
        return True
    return False


def _sanitize_legal_footer_lines_v48(po, config: dict):
    kept=[]; removed=[]
    for line in po.lines:
        if _legal_footer_line_v48(line,config): removed.append(line)
        else: kept.append(line)
    if removed:
        po.lines=kept
        # Preserve traceability in document notes instead of silently dropping evidence.
        bucket=po.notes.setdefault("footer_notes",[])
        for line in removed:
            raw=" ".join(str(line.raw_text or line.description or "").split())
            if raw and raw not in bucket:
                bucket.append(raw[:1000])
    for i,line in enumerate(po.lines,1):
        line.line_number=str(i)
    return po


def _summary_table_totals_v48(po, tables, config: dict, default_currency: str="EUR"):
    """Recover labelled totals from compact 2-column native PDF summary tables."""
    import re
    from po_ocr.normalize import parse_number
    if not config.get("layout",{}).get("native_summary_table_totals",True): return po
    for table in tables:
        data=table.data or []
        for row in data:
            if len(row)<2: continue
            # Some ERP PDFs encode each summary column as a self-contained
            # ``label\nvalue`` cell instead of using separate header/value rows.
            # Parse only explicit monetary labels: ``TVA (%)\n20`` is a rate,
            # while ``Valeur TVA\n1 391,47`` is the tax amount.
            for cell in row:
                parts=_split_cell_lines(cell)
                if len(parts)<2: continue
                label=" ".join(parts[:-1]); low=_fold(label)
                num=_parse_native_decimal(parts[-1])
                if num is None: continue
                if re.search(r"\b(?:total\s+ttc|ttc\s+total|net\s+a\s+payer|amount\s+due)\b",low):
                    po.totals.amount_due=float(num); po.totals.grand_total=float(num)
                    po.totals.total_gross=float(num); po.totals.total_after_tax=float(num)
                elif ("valeur tva" in low or re.fullmatch(r"t\.?v\.?a\.?(?:\s+montant)?",low)) and not re.search(r"%|taux",low):
                    po.totals.total_vat=float(num); po.totals.total_tax=float(num)
                elif re.search(r"\b(?:valeur\s+h\.?t\.?|total\s+h\.?t\.?|total\s+net\s+h\.?t\.?)\b",low):
                    po.totals.total_net=float(num); po.totals.subtotal=float(num); po.totals.total_before_tax=float(num)
                else:
                    continue
                po.totals.currency=po.totals.currency or default_currency
            labels=_split_cell_lines(row[0]); values=_split_cell_lines(row[1])
            if len(labels)>=2 and len(values)>=2 and abs(len(labels)-len(values))<=1:
                for lab,val in zip(labels,values):
                    num=parse_number(val)
                    if num is None: continue
                    low=_fold(lab)
                    if re.search(r"\b(?:montant|total)\s*(?:h\.?t\.?|net\s*ht)?\b",low) and "tva" not in low and "net a payer" not in low:
                        po.totals.total_net=float(num); po.totals.subtotal=float(num); po.totals.total_before_tax=float(num)
                    elif "tva" in low:
                        po.totals.total_vat=float(num); po.totals.total_tax=float(num)
                    elif "net a payer" in low or "net payable" in low or "amount due" in low:
                        po.totals.amount_due=float(num); po.totals.grand_total=float(num); po.totals.total_gross=float(num); po.totals.total_after_tax=float(num)
                    po.totals.currency=po.totals.currency or default_currency
    return po


def _strict_text_totals_v48(po, pages, config: dict, default_currency: str="EUR"):
    """Recover labels such as Montant HT Net and Net à payer from native text rows."""
    import re
    from po_ocr.extract import all_rows
    from po_ocr.normalize import parse_number
    if not config.get("layout",{}).get("strict_text_totals_v48",True): return po
    for pg in pages:
        rows=all_rows([pg],y_factor=0.50)
        for idx,row in enumerate(rows):
            text=" ".join(row.text.split()); low=_fold(text)
            kind=None
            # An explicit ISO currency at the start of a monetary summary row
            # is stronger than stray OCR symbols elsewhere in the document.
            currency_match=re.match(r"^\s*(EUR|USD|GBP|CHF|CAD|AUD)\b(?=.*\d)",text,flags=re.I)
            if currency_match:
                currency=currency_match.group(1).upper()
                po.purchase_order.currency=_make_spatial_field(currency,pg.page,row.bbox,method="explicit_summary_currency",confidence=0.995)
                po.totals.currency=currency
            # Footer grids can collapse all header cells into one native-table
            # string. Use the printed NET H.T. column and its aligned next-row
            # amount, not an earlier merchandise subtotal or the neighboring VAT.
            if "net a payer" in low and re.search(r"\bnet\s+h\.?\s*t\.?",low):
                anchors={}
                for wi,word in enumerate(row.words[:-1]):
                    first=_fold(word.text)
                    second=re.sub(r"[^a-z]","",_fold(row.words[wi+1].text))
                    label_words=[]; category=None
                    if first=="net" and second=="ht": category="net"; label_words=row.words[wi:wi+2]
                    elif first=="montant" and second=="tva": category="vat"; label_words=row.words[wi:wi+2]
                    elif first=="montant" and second=="ttc": category="gross"; label_words=row.words[wi:wi+2]
                    elif first=="net" and second=="a" and wi+2<len(row.words) and _fold(row.words[wi+2].text)=="payer":
                        category="due"; label_words=row.words[wi:wi+3]
                    if category:
                        anchors[category]=(label_words[0].bbox[0]+label_words[-1].bbox[2])/2
                for category,anchor in anchors.items():
                    for following in rows[idx+1:idx+3]:
                        if _row_center_safe(following)-_row_center_safe(row)>pg.height*0.045: break
                        aligned=[word for word in following.words
                                 if abs((word.bbox[0]+word.bbox[2])/2-anchor)<pg.width*0.045
                                 and re.fullmatch(r"\d[\d.,']*",word.text.strip("€$£"))]
                        if not aligned: continue
                        raw=" ".join(word.text.strip("€$£") for word in aligned)
                        if not re.search(r"[,.]\d{2}$",raw): continue
                        value=parse_number(raw)
                        if value is not None:
                            if category=="net":
                                po.totals.total_net=value; po.totals.total_before_tax=value
                            elif category=="vat":
                                po.totals.total_vat=value; po.totals.total_tax=value
                            else:
                                po.totals.grand_total=value; po.totals.amount_due=value
                                po.totals.total_gross=value; po.totals.total_after_tax=value
                            po.totals.currency=po.totals.currency or default_currency
                            break
            if "net a payer" in low or "amount due" in low or re.search(r"\btotal\s+t\.?t\.?c\.?\b",low): kind="grand"
            elif re.search(r"\b(?:montant\s+ht\s+net|total\s+ht|total\s+net\s+ht|montant\s+ht)\b",low): kind="net"
            elif re.search(r"\btva\s*\d*(?:[.,]\d+)?\s*%?\b",low) and not re.search(r"\btva\s*(?:intra|ic\b)|\b(?:numero|n[°ºo])\s*tva",low): kind="vat"
            if not kind: continue
            nums=[]
            for m in re.finditer(r"(?<!\d)(?:\d{1,3}(?:[ .']\d{3})+|\d+)[,.]\d{2}(?!\d)",text):
                v=parse_number(m.group(0))
                if v is not None: nums.append(float(v))
            if not nums: continue
            value=nums[-1]
            if kind=="net":
                po.totals.total_net=value; po.totals.subtotal=value; po.totals.total_before_tax=value
            elif kind=="vat":
                po.totals.total_vat=value; po.totals.total_tax=value
            else:
                po.totals.amount_due=value; po.totals.grand_total=value; po.totals.total_gross=value; po.totals.total_after_tax=value
            po.totals.currency=po.totals.currency or default_currency
    return po


def _collect_rows_in_xzone_v48(pages, *, page_no:int, y0:float, y1:float, x0:float, x1:float):
    from po_ocr.extract import all_rows
    vals=[]
    pg=next((p for p in pages if int(p.page)==int(page_no)),None)
    if pg is None: return vals
    for row in all_rows([pg],y_factor=0.48):
        cy=_row_center_safe(row)
        if cy<y0 or cy>y1: continue
        words=[w for w in row.words if (w.bbox[0]+w.bbox[2])/2>=x0 and (w.bbox[0]+w.bbox[2])/2<=x1]
        text=" ".join(w.text for w in words).strip()
        if text: vals.append((cy,text,words))
    return vals


def _party_from_labeled_zone_v48(pages, label_patterns, config:dict, *, prefer_role:str):
    """Extract a business party from an explicitly labelled spatial column."""
    import re
    from po_ocr.extract import all_rows
    if not config.get("layout",{}).get("explicit_role_zone_recovery_v48",True): return None
    for pg in pages:
        rows=all_rows([pg],y_factor=0.48)
        for i,row in enumerate(rows):
            low=_fold(row.text)
            def _pat_core(pat):
                return [t for t in re.findall(r"[a-zA-ZÀ-ÿ]+",pat) if len(_fold(t))>=4]
            matched=False
            for pat in label_patterns:
                core=_pat_core(pat)
                if re.search(pat,low) or (core and all(_fold(t) in low for t in core)):
                    matched=True; break
            if not matched: continue
            # Locate a distinctive anchor token for multi-word labels so mixed rows
            # such as "Facturé à :   Adresse de livraison :" resolve to the right column.
            literals=[]
            for pat in label_patterns:
                literals.extend(re.findall(r"[a-zA-ZÀ-ÿ]+",pat.replace("\\s"," ")))
            folded_literals=[_fold(x) for x in literals if len(_fold(x))>=4]
            label_words=[w for w in row.words if len(_fold(w.text))>=4 and any(tok in _fold(w.text) or _fold(w.text) in tok for tok in folded_literals)]
            if not label_words:
                label_words=[w for w in row.words if any(re.search(pat,_fold(w.text)) for pat in label_patterns)]
            if label_words:
                lx=min(w.bbox[0] for w in label_words); rx=max(w.bbox[2] for w in label_words)
            else:
                lx=row.bbox[0]; rx=row.bbox[2]
            # Use the label's half/column. This avoids mixing left bill-to with right ship-to blocks.
            mid=pg.width*0.50
            if (lx+rx)/2 < mid:
                x0=max(0.0,lx-pg.width*0.03); x1=mid-pg.width*0.015
            else:
                x0=max(mid*0.90,lx-pg.width*0.03); x1=pg.width
            start_y=max(w.bbox[3] for w in row.words) if row.words else row.bbox[3]
            max_h=pg.height*float(config.get("layout",{}).get("role_zone_max_height_ratio",0.24))
            zone=_collect_rows_in_xzone_v48(pages,page_no=pg.page,y0=start_y-2,y1=min(pg.height,start_y+max_h),x0=x0,x1=x1)
            lines=[]
            # Some ERP layouts print the party name immediately below the label but
            # inside the same PDF text row (different baseline). Capture that first.
            if label_words:
                anchor_y=min((w.bbox[1]+w.bbox[3])/2 for w in label_words)
                same=[w for w in row.words if x0 <= (w.bbox[0]+w.bbox[2])/2 <= x1 and (w.bbox[1]+w.bbox[3])/2 > anchor_y+2.5]
                same_text=" ".join(w.text for w in same).strip()
                if same_text and _company_name_quality_v48(same_text)>=0.35:
                    lines.append(same_text)
            stop_terms=tuple(_fold(x) for x in config.get("layout",{}).get("role_zone_stop_terms",[
                "article","designation","reference - designation","mode livraison","condition paiement","total ht","suivant offre","date de livraison"
            ]))
            for _,txt,_ in zone:
                lowtxt=_fold(txt)
                if any(x in lowtxt for x in stop_terms): break
                if lowtxt in {_fold(row.text),"france"} and not lines: continue
                has_address=any(re.search(r"\b(?:rue|avenue|route|impasse|boulevard|chemin|zac|zae|zi|bp|cs|tsa)\b",_fold(x)) or re.search(r"\b\d{5}\b",x) for x in lines)
                if has_address and lowtxt not in {"france","fr"} and _company_name_quality_v48(txt)>=0.72 and not re.search(r"\b(?:rue|avenue|route|impasse|boulevard|chemin|zac|zae|zi|bp|cs|tsa)\b",lowtxt) and not re.search(r"\b\d{5}\b",txt) and not (lines and re.fullmatch(r"\d{5}",lines[-1])):
                    break
                if txt not in lines: lines.append(txt)
            if lines:
                party=_party_from_lines_v47(lines,role=prefer_role)
                # V4.8: if parser treated a street as the party name, promote the preceding company line.
                if party and _company_name_quality_v48(party.name)<0.35:
                    company=next((x for x in lines if _company_name_quality_v48(x)>=0.62),None)
                    if company:
                        party.name=company
                if party:
                    # Native PDFs sometimes split postal code and city onto separate baselines.
                    for j,x in enumerate(lines):
                        if re.fullmatch(r"\d{5}",x) and j+1<len(lines):
                            city=lines[j+1]
                            if re.fullmatch(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]{2,60}",city) and not any(t in _fold(city) for t in ("sas","sarl","sa ","holding","distribution","contact","livraison")):
                                party.address.postal_code=x; party.address.city=city
                                joined=f"{x} {city}"
                                if party.address.line2 is None: party.address.line2=joined
                                elif party.address.line3 is None and joined not in (party.address.line1,party.address.line2): party.address.line3=joined
                    # City, 67100 ordering.
                    for x in lines:
                        cm=re.match(r"^([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]{2,60})[, ]+([0-9]{5})$",x)
                        if cm:
                            party.address.city=cm.group(1).strip(); party.address.postal_code=cm.group(2)
                return party
    return None


def _extract_ordered_by_block_v48(po,pages,config:dict):
    """Recover buyer/site/contact from explicit 'Commandé par' style blocks."""
    import re
    from po_ocr.extract import all_rows
    if not config.get("layout",{}).get("ordered_by_block_recovery_v48",True): return po
    for pg in pages:
        rows=all_rows([pg],y_factor=0.48)
        for row in rows:
            low=_fold(row.text)
            if not re.search(r"\b(?:commande|commande[eé])\s+par\b|\bordered\s+by\b",low): continue
            mid=pg.width*0.50
            x1=mid if _row_center_safe(row)>=0 else mid
            start=row.bbox[3]
            zone=_collect_rows_in_xzone_v48(pages,page_no=pg.page,y0=start-1,y1=min(pg.height,start+pg.height*0.18),x0=0,x1=x1)
            texts=[]
            for _,txt,_ in zone:
                l=_fold(txt)
                if any(k in l for k in ("a livrer","adresse de livraison","reference - designation","suivant offre")): break
                if txt and txt not in texts: texts.append(txt)
            # discard label and contact metadata
            candidates=[x for x in texts if not any(k in _fold(x) for k in ("commande par","commande par :","tel :","mel :","mail :","fax :"))]
            contact=None; company=None
            for x in candidates:
                if re.search(r"\b(?:tel|mel|mail|fax)\b",_fold(x)): continue
                q=_company_name_quality_v48(x)
                if company is None and q>=0.62 and (x.upper()==x or any(k in _fold(x) for k in ("sas","sarl","ccl","iserba","groupe","holding"))): company=x
                elif contact is None and re.fullmatch(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]{3,60}",x): contact=x
            if company:
                po.buyer.name=company
                if contact and contact!=company: po.buyer.contact.name=contact
                # copy contact channels from nearby raw text
                blob=" ".join(texts)
                tm=re.search(r"(?:T[eé]l|Tel)\s*:\s*([0-9 .-]{8,})",blob,re.I)
                em=re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",blob)
                if tm: po.buyer.phone=" ".join(tm.group(1).split())
                if em: po.buyer.email=em.group(0)
                return po
    return po


def _anchor_party_address_v48(po,pages,config:dict):
    """Enrich known buyer/supplier names with address lines immediately adjacent in the same column."""
    import re
    from po_ocr.extract import all_rows
    from po_ocr.models import Address
    if not config.get("layout",{}).get("party_anchor_address_recovery_v48",True): return po
    for role in ("buyer","supplier"):
        party=getattr(po,role,None)
        if not party or not party.name: continue
        if party.address and (party.address.street or (party.address.line1 and re.search(r"\b(?:rue|avenue|route|impasse|boulevard|chemin)\b",_fold(party.address.line1)))):
            continue
        target=_fold(party.name)
        best=None
        for pg in pages:
            rows=all_rows([pg],y_factor=0.48)
            for idx,row in enumerate(rows):
                if target and target in _fold(row.text):
                    # pick the word cluster that actually contains the party name
                    hits=[w for w in row.words if any(tok in _fold(w.text) for tok in target.split() if len(tok)>=3)]
                    if not hits: continue
                    cx=sum((w.bbox[0]+w.bbox[2])/2 for w in hits)/len(hits)
                    half=pg.width/2
                    x0=0 if cx<half else half; x1=half if cx<half else pg.width
                    y0=row.bbox[3]-1; y1=min(pg.height,y0+pg.height*0.16)
                    zone=_collect_rows_in_xzone_v48(pages,page_no=pg.page,y0=y0,y1=y1,x0=x0,x1=x1)
                    vals=[]
                    for _,txt,_ in zone:
                        low=_fold(txt)
                        if any(k in low for k in ("date commande","n° preneur","no preneur","contact ","a livrer","facture a","article","ligne ")): break
                        if txt and _fold(txt)!=target: vals.append(txt)
                    if vals:
                        best=vals; break
            if best: break
        if best:
            # Remove contact names accidentally prepended to postal lines: "Guillaume 93700 Drancy".
            cleaned=[]
            for v in best:
                m=re.search(r"\b(\d{5})\s+(.+)$",v)
                if m and not re.search(r"\b(?:rue|avenue|route|impasse|boulevard|chemin)\b",_fold(v[:m.start()])):
                    v=f"{m.group(1)} {m.group(2)}"
                cleaned.append(v)
            party.address=Address(line1=cleaned[0] if cleaned else None,line2=cleaned[1] if len(cleaned)>1 else None,line3=cleaned[2] if len(cleaned)>2 else None)
    return po



def _sanitize_party_addresses_v48(po, config:dict):
    """Remove contact/role contamination from structured address lines."""
    import re
    if not config.get("layout",{}).get("party_address_sanitization_v48",True): return po
    roles=("buyer","supplier","ship_to","bill_to","sold_to","deliver_to","invoice_to","payer","end_customer","consignee","ship_from")
    for role in roles:
        party=getattr(po,role,None)
        if not party: continue
        vals=[party.address.line1,party.address.line2,party.address.line3]
        out=[]
        for v in vals:
            if not v: continue
            text=" ".join(str(v).split()).strip()
            low=_fold(text)
            if any(x in low for x in ("commande par", "commande par :", "adresse de livraison :", "a facturer a :")):
                continue
            if any(x in low for x in ("intracom","n° tva","no tva","tva intracom","siret","siren","rcs","iban","bic")):
                vm=re.search(r"\bFR\s*[A-Z0-9 ]{4,20}\b",text,re.I)
                if vm and not party.vat_number: party.vat_number="".join(vm.group(0).split()).upper()
                continue
            if low in {"tel","telephone","fax","mel","mail","e-mail"} or re.match(r"^(?:tel|telephone|fax|mel|mail|e-mail)\s*[:.-]",low) or "@" in text:
                continue
            if re.fullmatch(r"[:\s]*[+0-9 .-]{7,}",text):
                continue
            # Strip a contact firstname/label before a postal code: "Guillaume 93700 Drancy".
            m=re.search(r"\b(\d{5})\s+(.+)$",text)
            if m and not re.search(r"\b(?:rue|avenue|route|impasse|boulevard|chemin)\b",_fold(text[:m.start()])):
                text=f"{m.group(1)} {m.group(2)}"
            if text and text not in out: out.append(text)
        party.address.line1=out[0] if out else None
        party.address.line2=out[1] if len(out)>1 else None
        party.address.line3=out[2] if len(out)>2 else None
        joined=" ".join(out)
        explicit_pc=None
        city_pc=None
        for v in out:
            em=re.match(r"^\s*(\d{5})\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 .'-]{1,60})$",v)
            if em and not re.match(r"^(?:CS|TSA|BP)\s",v,re.I): explicit_pc=em; break
            cm=re.match(r"^([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]{2,60})[, ]+([0-9]{5})$",v)
            if cm: city_pc=cm
        if explicit_pc:
            party.address.postal_code=explicit_pc.group(1); party.address.city=explicit_pc.group(2).strip()
        elif city_pc:
            party.address.city=city_pc.group(1).strip(); party.address.postal_code=city_pc.group(2)
        else:
            m=re.search(r"(?<!CS )(?<!BP )(?<!TSA )(\d{5})\s+([A-Za-zÀ-ÿ0-9 .'-]{2,})",joined,flags=re.I)
            if m:
                party.address.postal_code=m.group(1)
                city=re.split(r"\b(?:TEL|FAX|EMAIL|MAIL)\b",m.group(2),maxsplit=1,flags=re.I)[0].strip(" ,-;")
                party.address.city=city
    # If buyer and bill-to are the same legal entity, the invoice address is a strong
    # address source for buyer; keep buyer contact metadata separate.
    if po.buyer and po.bill_to and _fold(po.buyer.name)==_fold(po.bill_to.name) and po.bill_to.address.line1:
        po.buyer.address=po.bill_to.address.model_copy(deep=True)
    return po

def _recover_roles_v48(po,tables,pages,config:dict):
    """Resolve explicit buyer/bill-to/ship-to zones and instruction-vs-party confusions."""
    if not config.get("layout",{}).get("role_resolution_v48",True): return po
    # Explicit role labels are stronger than heuristic party segmentation.
    ship=_party_from_labeled_zone_v48(pages,[r"adresse\s+de\s+livraison",r"a\s+livrer\s+a",r"livre\s+a"],config,prefer_role="ship_to")
    bill=_party_from_labeled_zone_v48(pages,[r"facture\s+a",r"facture\s+a",r"a\s+facturer\s+a",r"bill\s+to"],config,prefer_role="bill_to")
    if ship and _company_name_quality_v48(ship.name)>=0.35:
        po.ship_to=ship
    if bill and _company_name_quality_v48(bill.name)>=0.35:
        po.bill_to=bill
        if po.bill_to.department and not any(k in _fold(po.bill_to.department) for k in ("service","departement","department","agence","division")):
            vals=[po.bill_to.department,po.bill_to.address.line1,po.bill_to.address.line2]
            po.bill_to.address.line1=vals[0]; po.bill_to.address.line2=vals[1]; po.bill_to.address.line3=vals[2]
            po.bill_to.department=None
    po=_extract_ordered_by_block_v48(po,pages,config)
    # When buyer is instruction-like but bill-to identifies the purchasing legal entity,
    # use the bill-to identity while retaining any buyer contact metadata already recovered.
    if _company_name_quality_v48(po.buyer.name)<float(config.get("layout",{}).get("party_name_repair_threshold",0.35)):
        if po.bill_to and _company_name_quality_v48(po.bill_to.name)>=float(config.get("layout",{}).get("party_name_promotion_threshold",0.62)):
            old_contact=po.buyer.contact.model_copy(deep=True); old_phone=po.buyer.phone; old_email=po.buyer.email
            po.buyer.name=po.bill_to.name; po.buyer.legal_name=po.bill_to.legal_name or po.bill_to.name
            if not po.buyer.address.line1: po.buyer.address=po.bill_to.address.model_copy(deep=True)
            if old_contact.name or old_contact.email or old_contact.phone: po.buyer.contact=old_contact
            po.buyer.phone=old_phone or po.buyer.phone; po.buyer.email=old_email or po.buyer.email
    po=_anchor_party_address_v48(po,pages,config)
    po=_sanitize_party_addresses_v48(po,config)
    return po

def enhance_purchase_order_with_native_structure(po, tables, pages, config: dict):
    """V4.7 post-processor: strict semantic guards + native/geometry hypothesis fusion."""
    from po_ocr.extract import line_item_set_quality
    default_currency = config.get("normalization", {}).get("default_currency", "EUR")
    po = _extract_native_table_header_fields(po, tables)
    po = _enhance_order_header_from_page_layout(po, pages, config)
    po = _enhance_inline_transaction_header_v46(po, pages, config)
    po = _enhance_strict_order_number_v47(po, pages, config)
    table_lines, table_charges = _native_table_line_items_and_charges(tables, default_currency=default_currency)
    geometry_lines, geometry_charges = _extract_geometry_table_items_v46(pages, config, default_currency=default_currency)
    amount_only_lines = _extract_amount_only_geometry_v47(pages, config, default_currency=default_currency)
    quantity_first_lines, quantity_first_charges = _quantity_first_table_items(pages, default_currency)
    price_then_quantity_lines = _price_then_quantity_table_items(pages, default_currency)
    two_row_erp_lines = _two_row_erp_table_items(pages, default_currency)

    # V4.6 hypothesis competition.  Structural quality alone is not enough: a bad
    # parser can be arithmetically self-consistent while misreading the columns.  We
    # therefore also penalize fee/legal pseudo-products and reward clean material rows.
    def hypothesis_score(lines, charges, source_bonus=0.0):
        if not lines:
            return -1.0
        base=float(line_item_set_quality(lines))
        arithmetic=[]; noise=0.0; complete=0.0
        for line in lines:
            if all(v is not None for v in (line.quantity,line.unit_price,line.line_total)):
                expected=float(line.quantity)*float(line.unit_price)
                tol=max(0.04,abs(expected)*0.006)
                arithmetic.append(1.0 if abs(float(line.line_total)-expected)<=tol else 0.0)
            complete += sum(v is not None and v != "" for v in (line.material_number,line.description,line.quantity,line.unit_price,line.line_total))/5.0
            low=_fold(" ".join(x for x in (line.material_number,line.description,line.raw_text) if x))
            if _fee_kind(low) or any(x in low for x in ("siret","n° tva","no tva","capital de","conditions generales","page ")):
                noise += 1.0
        arithmetic_score=sum(arithmetic)/len(arithmetic) if arithmetic else 0.55
        completeness=complete/max(1,len(lines))
        fee_bonus=min(0.08,len(charges)*0.02)
        noise_penalty=min(0.40,noise/max(1,len(lines))*0.50)
        return 0.30*base + 0.34*arithmetic_score + 0.24*completeness + source_bonus + fee_bonus - noise_penalty

    hypotheses=[("current",po.lines,po.additional_charges,0.0)]
    if table_lines: hypotheses.append(("native_table",table_lines,table_charges,0.18))
    if geometry_lines: hypotheses.append(("geometry",geometry_lines,geometry_charges,0.08))
    if amount_only_lines: hypotheses.append(("amount_only_geometry",amount_only_lines,[],0.11))
    if quantity_first_lines: hypotheses.append(("quantity_first_table",quantity_first_lines,quantity_first_charges,0.12))
    if price_then_quantity_lines: hypotheses.append(("price_then_quantity_table",price_then_quantity_lines,[],0.14))
    if two_row_erp_lines: hypotheses.append(("two_row_erp_table",two_row_erp_lines,[],0.16))
    ranked=sorted(((hypothesis_score(ls,ch,sb),name,ls,ch) for name,ls,ch,sb in hypotheses),reverse=True,key=lambda x:x[0])
    parser_diag={"selected":"current","scores":{name:round(hypothesis_score(ls,ch,sb),4) for name,ls,ch,sb in hypotheses}}
    if ranked and ranked[0][2]:
        _,chosen_name,chosen_lines,chosen_charges=ranked[0]
        po.lines=chosen_lines; po.additional_charges=chosen_charges
        parser_diag["selected"]=chosen_name
    po.lines = _dedupe_line_items(po.lines)
    po = _sanitize_legal_footer_lines_v48(po, config)
    # Fill parent line ids after deduplication assigned stable sequence numbers.
    for charge in po.additional_charges:
        if not charge.parent_line_number and charge.parent_material_number:
            parent = next((x for x in po.lines if x.material_number == charge.parent_material_number), None)
            if parent: charge.parent_line_number = parent.line_number
        if not charge.supplier_reference and charge.parent_material_number:
            charge.supplier_reference = charge.parent_material_number
    po = _enhance_totals_from_native_tables(po, tables, default_currency=default_currency)
    po = _summary_table_totals_v48(po, tables, config, default_currency=default_currency)
    po = _strict_text_totals_v48(po, pages, config, default_currency=default_currency)
    po = _sanitize_summary_totals_v46(po, pages, config, default_currency=default_currency)
    po = _enhance_strict_totals_v47(po, pages, config)
    po = _enhance_parties_from_page_layout(po, pages)
    if config.get("layout", {}).get("party_zone_segmentation_v2", True):
        po = _enhance_parties_zone_v2(po, pages, config)
    po = _enhance_paired_party_zones_v46(po, pages, config)
    # Explicit native blocks and exact column labels are stronger than heuristic zones.
    po = _enhance_native_party_blocks_v47(po, tables, pages, config)
    po = _enhance_column_labeled_parties_v47(po, pages, config)
    po = _enhance_header_terms_addresses_v45(po, tables)
    po = _recover_roles_v48(po, tables, pages, config)
    # Re-apply strict semantic resolution last so weaker parsers cannot reintroduce a
    # supplier code, fax or address as the PO number.
    po = _enhance_inline_transaction_header_v46(po, pages, config)
    po = _enhance_strict_order_number_v47(po, pages, config)
    po = _clean_po_number_v48(po, config)
    po = _extract_vat_identifiers_v52(po, pages, config)
    po = _enhance_native_party_blocks_v47(po, tables, pages, config)
    po = _enhance_column_labeled_parties_v47(po, pages, config)
    po = _recover_roles_v48(po, tables, pages, config)
    po = _sanitize_summary_totals_v46(po, pages, config, default_currency=default_currency)
    po = _enhance_strict_totals_v47(po, pages, config)
    po = _summary_table_totals_v48(po, tables, config, default_currency=default_currency)
    po = _strict_text_totals_v48(po, pages, config, default_currency=default_currency)
    po = _sanitize_double_counted_shipping_v47(po, config)
    po = _sanitize_internal_reference_v47(po)
    # Refresh normalized address-role records after structural party corrections.
    po.business_addresses = build_business_addresses(po, pages, config)
    # Re-run validation/confidence after structural corrections.
    po = validate(po, config.get("validation", {}))
    po.validation.checks["line_parser_hypothesis"]=parser_diag
    po = score(po, {**config.get("confidence", {}), "manual_review_threshold": config.get("ocr", {}).get("manual_review_threshold", 0.80)})
    return po

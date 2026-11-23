"""Source-backed extraction for supplier quotations with a priced article grid.

This is deliberately separate from purchase-order extraction: an offer number
is never a customer's order number and quoted quantities are not ordered goods.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
import unicodedata
from typing import Any

import pymupdf


_ARTICLE = re.compile(r"\d{8,13}$")
_DATE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
_MONEY = re.compile(r"\d[\d .\u00a0\u202f]*[,.]\d{2}")
_TERMS = {
    "documentation": "documentation",
    "garanties": "warranties",
    "livraisons": "delivery",
    "conditions": "conditions",
}


def _fold(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(char)
    ).casefold()


def _rows(page: Any, page_number: int) -> list[dict[str, Any]]:
    words = sorted(page.get_text("words"), key=lambda word: ((word[1] + word[3]) / 2, word[0]))
    rows: list[dict[str, Any]] = []
    for word in words:
        x0, y0, x1, y1, token = word[:5]
        cy = (y0 + y1) / 2
        if rows and abs(cy - rows[-1]["cy"]) <= 4.2:
            rows[-1]["words"].append((x0, y0, x1, y1, token))
        else:
            rows.append({"cy": cy, "page": page_number, "words": [(x0, y0, x1, y1, token)]})
    for row in rows:
        row["words"].sort(key=lambda word: word[0])
        row["text"] = " ".join(word[4] for word in row["words"])
    return rows


def _column(row: dict[str, Any], low: float, high: float) -> str:
    return " ".join(word[4] for word in row["words"] if low <= word[0] < high).strip()


def _evidence(row: dict[str, Any], source_text: str, method: str) -> dict[str, Any]:
    words = row["words"]
    return {
        "page": row["page"],
        "bbox": [min(w[0] for w in words), min(w[1] for w in words),
                 max(w[2] for w in words), max(w[3] for w in words)],
        "source_text": source_text, "method": method,
    }


def _field(value: str, row: dict[str, Any], *, date: bool = False) -> dict[str, Any]:
    raw = " ".join(value.split()).strip()
    if date:
        try:
            normalized = datetime.strptime(raw, "%d/%m/%Y").date().isoformat()
        except ValueError:
            return {}
    else:
        normalized = raw
    return {
        "value": normalized, "raw_value": raw, "confidence": .995,
        "status": "SOURCE_SUPPORTED",
        "evidence": _evidence(row, raw, "explicit_quotation_label_v1"),
    }


def _money(value: str) -> float | None:
    match = _MONEY.search(value)
    if not match:
        return None
    raw = re.sub(r"[\s.\u00a0\u202f]", "", match.group()).replace(",", ".")
    try:
        return float(Decimal(raw))
    except InvalidOperation:
        return None


def _labeled(rows: list[dict[str, Any]], pattern: str, low: float, high: float,
             *, date: bool = False) -> dict[str, Any] | None:
    for row in rows:
        segment_words = [word for word in row["words"] if low <= word[0] < high]
        match = re.search(pattern, _column(row, low, high), re.I)
        if match and match.group(1).strip():
            # The value is the final token sequence after its label. Never
            # give a left-column seller field the right-column buyer bbox.
            value_words = segment_words[-len(match.group(1).split()):]
            return _field(match.group(1), {**row, "words": value_words}, date=date) or None
    return None


def _header(rows: list[dict[str, Any]], first_page: Any) -> dict[str, Any]:
    header_rows = [row for row in rows if row["cy"] < 320]
    seller_name = next((_column(row, 0, 300) for row in header_rows
                        if row["cy"] < 70 and "SAS" in _column(row, 0, 300)), None)
    seller_address = [
        _column(row, 0, 300) for row in header_rows
        if row["cy"] < 75 and 0 < row["cy"] and row["cy"] > 9
        and _column(row, 0, 300) and _column(row, 0, 300) != seller_name
    ]
    recipient_name = _labeled(header_rows, r"\bSoci[eé]t[eé]\s*:\s*(.+)$", 320, 610)
    recipient_attention = _labeled(header_rows, r"\bl'attention\s+de\s*:\s*(.+)$", 320, 610)
    recipient_address = _labeled(header_rows, r"\bAdresse\s*:\s*(.+)$", 320, 610)
    if not recipient_address:
        # Some quotations leave the street line blank but print a locality
        # directly below "Adresse :". Preserve the partial source value.
        address_label = next((row for row in header_rows
                              if re.fullmatch(r"Adresse\s*:\s*", _column(row, 320, 610), re.I)), None)
        if address_label:
            following = next((row for row in header_rows
                              if 0 < row["cy"] - address_label["cy"] < 18
                              and re.match(r"\d{5}\b", _column(row, 320, 610))), None)
            if following:
                words = [word for word in following["words"] if 320 <= word[0] < 610]
                recipient_address = _field(_column(following, 320, 610),
                                           {**following, "words": words})
    if recipient_address:
        address_row = next((row for row in header_rows
                            if row["page"] == recipient_address["evidence"]["page"]
                            and row["text"].find(recipient_address["raw_value"]) >= 0), None)
        if address_row:
            following = next((row for row in header_rows
                              if 0 < row["cy"] - address_row["cy"] < 18
                              and re.match(r"\d{5}\b", _column(row, 320, 610))), None)
            if following:
                recipient_address["value"] += ", " + _column(following, 320, 610)
                recipient_address["raw_value"] = recipient_address["value"]
                evidence = recipient_address["evidence"]
                next_evidence = _evidence(following, following["text"], "explicit_quotation_label_v1")
                evidence["bbox"] = [min(evidence["bbox"][0], next_evidence["bbox"][0]),
                                    min(evidence["bbox"][1], next_evidence["bbox"][1]),
                                    max(evidence["bbox"][2], next_evidence["bbox"][2]),
                                    max(evidence["bbox"][3], next_evidence["bbox"][3])]
                evidence["source_text"] = recipient_address["value"]
    seller = {"name": seller_name, "address": ", ".join(seller_address)}
    sender_name = _labeled(header_rows, r"Offre\s+r[eé]dig[eé]e\s+par\s*:\s*(.+)$", 0, 300)
    commercial_contact = _labeled(header_rows, r"Contact\s+commercial\s*:\s*(.+)$", 0, 300)
    sender_phone = _labeled(header_rows, r"T[eé]l\.?\s*:\s*(.+)$", 0, 300)
    sender_email = _labeled(header_rows, r"E-mail\s*:\s*(.+)$", 0, 300)
    if sender_name or sender_phone or sender_email:
        seller["contact"] = {key: field for key, field in (
            ("name", sender_name), ("phone", sender_phone), ("email", sender_email)) if field}
    if commercial_contact:
        seller["commercial_contact"] = commercial_contact
    siren = re.search(r"\bSIREN\s+[A-Z]?\s*(\d[\d ]{8,12}\d)\b", first_page.get_text())
    if siren:
        seller["siren"] = re.sub(r"\D", "", siren.group(1))[:9]
    recipient = {"name": recipient_name, "attention": recipient_attention,
                 "address": recipient_address}
    recipient_phone = _labeled(header_rows, r"T[eé]l\.?\s*:\s*(.+)$", 320, 610)
    recipient_email = _labeled(header_rows, r"E-mail\s*:\s*(.+)$", 320, 610)
    if recipient_phone or recipient_email:
        recipient["contact"] = {key: field for key, field in (
            ("phone", recipient_phone), ("email", recipient_email)) if field}
    return {
        "number": _labeled(header_rows, r"\bOffre\s+n[°ºo]?\s*:\s*(.+)$", 0, 300),
        "date": _labeled(header_rows, r"\bDate\s+de\s+l['’]offre\s*:\s*(\d{2}/\d{2}/\d{4})", 0, 300, date=True),
        "valid_until": _labeled(header_rows, r"\bOffre\s+valable\s+jusqu['’]au\s*:\s*(\d{2}/\d{2}/\d{4})", 0, 300, date=True),
        "project": _labeled(header_rows, r"\bAffaire\s*/\s*projet\s*:\s*(.+)$", 0, 320),
        "seller": seller, "recipient": recipient,
    }


def _project_details(rows: list[dict[str, Any]], page_height: float) -> dict[str, Any]:
    start = next((row["cy"] for row in rows if "informations specifiques a votre projet" in _fold(row["text"])), None)
    if start is None:
        return {}
    details: dict[str, Any] = {}
    notes: list[str] = []
    for row in rows:
        if row["cy"] <= start or row["cy"] >= page_height - 85:
            continue
        line = row["text"].strip()
        if not line:
            continue
        match = re.match(r"(DISTRIBUTEUR|INSTALLATEUR|PROJET|DONNEES CLIENT)\s*:\s*(.*)", _fold(line), re.I)
        if match:
            key = {"distributeur": "distributor", "installateur": "installer",
                   "projet": "project", "donnees client": "customer_data"}[match.group(1).lower()]
            if match.group(2).strip():
                details[key] = _field(line.split(":", 1)[1], row)
            continue
        notes.append(line)
    if notes:
        details["notes"] = notes
    return details


def _line_grid(all_pages: list[list[dict[str, Any]]], page_heights: list[float]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    items: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    totals: dict[str, Any] = {}
    current_section = None
    in_table = False
    description_left = 110.0
    for rows, page_height in zip(all_pages, page_heights):
        headers = {idx for idx, row in enumerate(rows)
                   if "code article" in _fold(row["text"])
                   and "designation" in _fold(row["text"])}
        categories = {idx - 1 for idx in headers if idx > 0}
        for idx, row in enumerate(rows):
            if row["cy"] > page_height - 85:
                continue
            # The supplier letterhead is repeated at the top of continuation
            # pages. Do not append its address to the preceding article. A
            # real article may start as early as this zone, so keep rows that
            # contain an explicit article code.
            if row["cy"] < 68 and not any(
                word[0] < 110 and _ARTICLE.fullmatch(word[4])
                for word in row["words"]
            ):
                continue
            text = row["text"]
            folded = _fold(text)
            if idx in categories:
                current_section = text.strip()
                continue
            if idx in headers:
                in_table = True
                article_header = next((word for word in row["words"]
                                       if _fold(word[4]) == "article" and word[0] < 120), None)
                if article_header:
                    description_left = article_header[2] + 5
                continue
            amount = _money(_column(row, 440, 610))
            if "sous-total" in folded and "ht" in folded:
                if amount is not None:
                    sections.append({"name": current_section, "subtotal": amount,
                                     "currency": "EUR", "evidence": _evidence(row, text, "quotation_section_subtotal_v1")})
                continue
            if "prix total net hors taxes et hors eco-participation" in folded:
                if amount is not None:
                    totals["before_eco_participation"] = amount
                in_table = False
                continue
            if "total eco-participation net hors taxes" in folded:
                if amount is not None:
                    totals["eco_participation"] = amount
                continue
            if "prix total net" in folded and "hors taxes" in folded:
                if amount is not None:
                    totals["total_net"] = amount
                continue
            if not in_table or "prix unit" in folded or "ht*" in folded:
                continue
            code_word = next((word for word in row["words"]
                              if word[0] < 110 and _ARTICLE.fullmatch(word[4])), None)
            if code_word:
                code = code_word[4]
                # Some page breaks repeat an unfinished article at the top of
                # the continuation page. Keep one item, with both descriptions.
                if items and items[-1]["references"]["article_number"] == code and "unit_price" not in items[-1]["pricing"]:
                    item = items[-1]
                else:
                    item = {
                        "line_number": len(items) + 1,
                        "references": {"article_number": code}, "description": "",
                        "product_family": current_section,
                        "quantity": None, "pricing": {}, "amounts": {},
                        "evidence": _evidence({**row, "words": [code_word]}, code, "quotation_article_grid_v1"),
                    }
                    items.append(item)
            description = _column(row, description_left, 333)
            if description and items and not re.match(r"^(?:Code article|D[eé]signation|S[eé]lection)", description, re.I):
                target = items[-1]
                target["description"] = (target["description"] + " " + description).strip()
            unit_price = _money(_column(row, 333, 385))
            quantity_text = _column(row, 385, 415)
            quantity = int(quantity_text) if quantity_text.isdigit() else None
            line_amount = amount
            if unit_price is not None and quantity is not None and line_amount is not None:
                target = next((candidate for candidate in items if "unit_price" not in candidate["pricing"]), None)
                if target:
                    target["quantity"] = quantity
                    target["pricing"] = {
                        "unit_price": unit_price, "eco_participation_unit": _money(_column(row, 415, 455)),
                        "currency": "EUR",
                    }
                    target["amounts"] = {"net": line_amount}
    return items, sections, totals


def _period(all_pages: list[list[dict[str, Any]]]) -> dict[str, Any]:
    period: dict[str, Any] = {}
    for rows in all_pages:
        for row in rows:
            text = row["text"]
            folded = _fold(text)
            date = _DATE.search(text)
            if not date:
                continue
            if "date de demarrage du" in folded:
                period["start"] = _field(date.group(), row, date=True)
                kind = re.search(r"date de d[eé]marrage du\s+(\w+)(?:\s*\(([^)]+)\))?", text, re.I)
                if kind:
                    period["kind"] = kind.group(1)
                    if kind.group(2):
                        period["code"] = kind.group(2)
            elif "date de fin du" in folded:
                period["end"] = _field(date.group(), row, date=True)
    return period


def _terms(all_pages: list[list[dict[str, Any]]], page_heights: list[float]) -> dict[str, str]:
    result: dict[str, list[str]] = {}
    current = None
    in_additional = False
    for rows, height in zip(all_pages, page_heights):
        for row in rows:
            if row["cy"] >= height - 65:
                continue
            text = row["text"].strip()
            folded = _fold(text)
            if folded == "informations additionnelles":
                in_additional = True
                current = "introduction"
                result.setdefault(current, [])
                continue
            if not in_additional:
                continue
            if folded in _TERMS:
                current = _TERMS[folded]
                result.setdefault(current, [])
                continue
            if re.fullmatch(r"elm[.]?leblanc\s+s[.]?a[.]?s[.]?", folded):
                continue
            if current and text and not text.startswith(("124, 126 rue", "F-93711")):
                result[current].append(text)
    return {key: " ".join(lines) for key, lines in result.items() if lines}


def extract_quotation(pdf_data: bytes, payload: dict[str, Any]) -> dict[str, Any]:
    """Add a typed quotation extraction only for a clearly titled offer."""
    doc_type = (payload.get("document") or {}).get("primary_document_type")
    if doc_type != "quotation":
        return payload
    try:
        document = pymupdf.open(stream=pdf_data, filetype="pdf")
    except Exception:
        return payload
    try:
        if not document or "offre de prix" not in _fold(document[0].get_text()):
            return payload
        pages = [_rows(page, index + 1) for index, page in enumerate(document)]
        offer = _header(pages[0], document[0])
        offer["project_details"] = _project_details(pages[0], document[0].rect.height)
        offer["line_items"], offer["sections"], offer["totals"] = _line_grid(
            pages, [page.rect.height for page in document]
        )
        offer["period"] = _period(pages)
        offer["terms"] = _terms(pages, [page.rect.height for page in document])
        offer["totals"]["currency"] = "EUR"
        items = offer["line_items"]
        product_sum = round(sum(item["amounts"].get("net", 0) for item in items), 2)
        eco_sum = round(sum((item["pricing"].get("eco_participation_unit") or 0)
                            * (item["quantity"] or 0) for item in items), 2)
        section_sum = round(sum(section["subtotal"] for section in offer["sections"]), 2)
        total = offer["totals"]
        lines_complete = bool(items) and all(
            item["description"] and item["quantity"] is not None
            and item["pricing"].get("unit_price") is not None
            and item["amounts"].get("net") is not None
            and abs(item["quantity"] * item["pricing"]["unit_price"] - item["amounts"]["net"]) <= .02
            for item in items
        )
        totals_match = (
            total.get("before_eco_participation") is not None
            and total.get("eco_participation") is not None
            and total.get("total_net") is not None
            and abs(product_sum - total["before_eco_participation"]) <= .02
            and abs(eco_sum - total["eco_participation"]) <= .02
            and abs(total["before_eco_participation"] + total["eco_participation"] - total["total_net"]) <= .02
            and bool(offer["sections"])
            and abs(section_sum - total["before_eco_participation"]) <= .02
        )
        offer["validation"] = {
            "status": "PASS" if lines_complete and totals_match else "WARNING",
            "line_count": len(items), "lines_complete": lines_complete,
            "product_sum": product_sum, "eco_sum": eco_sum,
            "section_sum": section_sum,
            "totals_match": totals_match,
        }
        payload["quotation_extraction"] = offer
        # The core had no quotation business parser. Its sole warning for this
        # missing parser should not survive once source-backed data is present.
        quality = payload.setdefault("quality", {})
        warnings = quality.get("warnings") or []
        quality["warnings"] = [warning for warning in warnings
                               if "no structured business extraction was produced" not in str(warning)]
        if not quality["warnings"]:
            quality["requires_human_review"] = offer["validation"]["status"] != "PASS"
        quality["business_extraction_confidence"] = .95 if lines_complete else .65
        return payload
    finally:
        document.close()

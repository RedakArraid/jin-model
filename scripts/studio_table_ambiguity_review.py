"""Evidence-led review of selected Studio drafts with printed line tables.

The grouped orders use printed ``SEQ:`` references; other selected forms use
fixed columns, OCR, or native PDF words. Only source-aligned cells with the
expected row structure are promoted. The script never marks a draft as
human-validated or usable for supervised training.

Run ``--apply`` in a Linux container sharing the Studio volume, not on Windows.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import math
import os
import re

import fitz

from jin_runtime.studio_store import StudioStore
from jin_runtime.studio_contract import training_annotation_issues
from scripts.studio_annotation_pdf_audit import avoid_new_training_conflicts, normalized, printed_region


PIECES_XPRESS = {
    "jdoc-ecbb948261634b42a765e4a45fe406ba",
    "jdoc-0cfedc54f94d495e83d1fc100261cdfb",
}
GROUPED_ORDERS = {
    "jdoc-31f2849551b745baabf1aa6b4d1d8b42",
    "jdoc-c838570783984910862be63889dabaef",
}
TARGETS = PIECES_XPRESS | GROUPED_ORDERS
REXEL = "jdoc-b7040db144cc46bfa3948db39bbf9397"
REXEL_ORDERS = {
    REXEL,
    "jdoc-dfbfdfe3e5ac49caac6dcfb71a5be773",
    "jdoc-546ebe9d98774184a15aaa1ef70e8452",
}
GARANKA = "jdoc-3665359d28b7474f92c92535e70841f7"
SCANNED_PROLIANS = "jdoc-663300cb119344a891926c58a4ec9ac4"
VERNEY = "jdoc-4dac1e6dcdb04542abdd739138bc90ed"
SOROFI = "jdoc-32770cf7221a4260a81b4d7425141031"
SISCA = "jdoc-c830a032ec0d41e4b37e98f2a72eed4e"
GERONDEAU = "jdoc-aa0c0e18589e4769b8184aad72ffe0ce"
CCL = "jdoc-fd987e9bff4a4b62afc2d044ab07ea51"
VCFM = "jdoc-2b5b99eac8bb4a3bbc1f2efc09bfe96d"
PARTEDIS_RODEZ = "jdoc-2ae0637fd2734e3bb9ed4eff9527c923"
DESENFANS = "jdoc-d45782d2a4244dca901764ba1a4edc11"
TARGETS |= REXEL_ORDERS | {GARANKA, SCANNED_PROLIANS, VERNEY, SOROFI, SISCA, GERONDEAU, CCL, VCFM, PARTEDIS_RODEZ, DESENFANS}
LINE_PATH = re.compile(r"order\.line_items\[(\d+)\]")
MONEY = re.compile(r"^-?\d{1,3}(?:[ .]\d{3})*,\d{2}$|^-?\d+,\d{2}$")


def decimal_value(raw: object) -> Decimal | None:
    try:
        value = str(raw or "").strip().replace("\u00a0", "").replace(" ", "")
        if not value:
            return None
        if "," in value:
            value = value.replace(".", "").replace(",", ".")
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def same_value(label: str, expected: object, source: str) -> bool:
    if label in {"LINE_ITEM_QUANTITY", "LINE_ITEM_UNIT_PRICE", "LINE_ITEM_TOTAL"}:
        left, right = decimal_value(expected), decimal_value(source)
        return left is not None and right is not None and abs(left - right) <= Decimal("0.005")
    return normalized(expected) == normalized(source) and bool(normalized(source))


def pdf_box(page: fitz.Page, words: list[tuple]) -> dict:
    x0, y0 = min(w[0] for w in words), min(w[1] for w in words)
    x1, y1 = max(w[2] for w in words), max(w[3] for w in words)
    if not (0 <= x0 < x1 <= page.rect.width and 0 <= y0 < y1 <= page.rect.height):
        raise ValueError("PDF word outside visible page")
    return {
        "page": page.number + 1,
        "bbox": [
            max(0, math.floor(x0 / page.rect.width * 1000)),
            max(0, math.floor(y0 / page.rect.height * 1000)),
            min(1000, math.ceil(x1 / page.rect.width * 1000)),
            min(1000, math.ceil(y1 / page.rect.height * 1000)),
        ],
    }


def grouped_pdf_rows(pdf: fitz.Document) -> list[dict]:
    """Read printed rows; never use model values to infer the PDF table."""
    result = []
    for page in pdf:
        by_line = defaultdict(list)
        for word in page.get_text("words"):
            by_line[(word[5], word[6])].append(word)
        lines = sorted(by_line.values(), key=lambda words: min(w[1] for w in words))
        for index, words in enumerate(lines):
            words.sort(key=lambda w: w[0])
            if not any(w[4].upper().rstrip(":") == "SEQ" for w in words):
                continue
            refs = [w for w in words if re.fullmatch(r"\d{4,12}", w[4])]
            if len(refs) != 1:
                continue
            seq_y = min(w[1] for w in words)
            product = None
            for earlier in reversed(lines[:index]):
                y = min(w[1] for w in earlier)
                if seq_y - y > 42:
                    break
                if any(275 <= w[0] < 325 and MONEY.fullmatch(w[4]) for w in earlier):
                    product = sorted(earlier, key=lambda w: w[0])
                    break
            if product is None:
                continue
            descriptions = [w for w in product if w[0] < 275]
            quantities = [w for w in product if 275 <= w[0] < 325 and MONEY.fullmatch(w[4])]
            units = [w for w in product if 315 <= w[0] < 365 and re.fullmatch(r"[A-Za-z]{2,12}", w[4])]
            prices = [w for w in product if 405 <= w[0] < 465 and MONEY.fullmatch(w[4])]
            totals = [w for w in product if w[0] > 500 and MONEY.fullmatch(w[4])]
            if not (descriptions and len(quantities) == len(units) == 1 and len(prices) == len(totals) <= 1):
                continue
            if prices:
                quantity, price, total = (decimal_value(ws[0][4]) for ws in (quantities, prices, totals))
                if any(v is None for v in (quantity, price, total)) or abs(quantity * price - total) > Decimal("0.05"):
                    continue
            row = {
                "LINE_ITEM_REFERENCE": (refs[0][4], pdf_box(page, refs)),
                "LINE_ITEM_DESCRIPTION": (" ".join(w[4] for w in descriptions), pdf_box(page, descriptions)),
                "LINE_ITEM_QUANTITY": (quantities[0][4], pdf_box(page, quantities)),
                "LINE_ITEM_UNIT": (units[0][4], pdf_box(page, units)),
            }
            if prices:
                row["LINE_ITEM_UNIT_PRICE"] = (prices[0][4], pdf_box(page, prices))
                row["LINE_ITEM_TOTAL"] = (totals[0][4], pdf_box(page, totals))
            result.append(row)
    return result


def update_from_printed(field: dict, source: str, evidence: dict, *, reason: str) -> bool:
    if field.get("status") not in {"AMBIGUOUS", "UNREVIEWED"}:
        return False
    label = field["label"]
    if same_value(label, field.get("corrected_value") or field.get("extracted_value"), source):
        field["status"] = "CONFIRMED" if not field.get("corrected_value") else "CORRECTED"
    else:
        field["status"] = "CORRECTED"
        field["corrected_value"] = str(decimal_value(source)) if label in {"LINE_ITEM_QUANTITY", "LINE_ITEM_UNIT_PRICE", "LINE_ITEM_TOTAL"} else source
    field["evidence"] = evidence
    field["comment"] = reason + " Relecture humaine requise."
    return True


def review_grouped(fields: list[dict], pdf: fitz.Document) -> Counter:
    rows = grouped_pdf_rows(pdf)
    counts: Counter[str] = Counter()
    if len(rows) != 72:
        counts["incomplete_table"] += 1
        return counts
    for field in fields:
        match = LINE_PATH.search(str(field.get("path") or ""))
        if not match or field.get("status") not in {"AMBIGUOUS", "UNREVIEWED"}:
            continue
        index = int(match.group(1))
        if index >= len(rows):
            continue
        label = field.get("label")
        if label == "LINE_ITEM_NUMBER":
            # The two rendered forms have no printed line-number column: 1
            # beside the amount is the UV multiplier, not a line number.
            field["status"] = "ABSENT"
            field.pop("evidence", None)
            field["comment"] = "Aucun numéro de ligne imprimé ; la colonne 1/UV n'est pas un numéro de ligne. Relecture humaine requise."
            counts["line_number_absent"] += 1
            continue
        if label not in rows[index]:
            continue
        source, evidence = rows[index][label]
        if update_from_printed(field, source, evidence, reason="Cellule du tableau confirmée par la ligne produit, la référence SEQ et le calcul quantité × prix = montant."):
            counts[field["status"].lower()] += 1
    return counts


PIECES_COLUMNS = {
    "LINE_ITEM_REFERENCE": (5, 120),
    "LINE_ITEM_DESCRIPTION": (120, 435),
    "LINE_ITEM_UNIT": (410, 515),
    "LINE_ITEM_QUANTITY": (540, 620),
    "LINE_ITEM_UNIT_PRICE": (790, 865),
    "LINE_ITEM_TOTAL": (885, 965),
}


def review_pieces(fields: list[dict], pdf: fitz.Document) -> Counter:
    counts: Counter[str] = Counter()
    references = {
        int(match.group(1)): field.get("evidence")
        for field in fields
        if field.get("label") == "LINE_ITEM_REFERENCE"
        and (match := LINE_PATH.search(str(field.get("path") or "")))
        and (field.get("evidence") or {}).get("bbox")
    }
    for field in fields:
        if field.get("status") not in {"AMBIGUOUS", "UNREVIEWED"}:
            continue
        label = field.get("label")
        match = LINE_PATH.search(str(field.get("path") or ""))
        if not match or int(match.group(1)) >= 72:
            continue
        if label == "LINE_ITEM_NUMBER":
            field["status"] = "ABSENT"
            field.pop("evidence", None)
            field["comment"] = "Le tableau imprimé ne comporte pas de colonne de numéro de ligne. Relecture humaine requise."
            counts["line_number_absent"] += 1
            continue
        if label not in PIECES_COLUMNS or label == "LINE_ITEM_DESCRIPTION":
            # Multi-line descriptions often extend beyond their first box.
            continue
        evidence = field.get("evidence") or {}
        box = evidence.get("bbox")
        if not isinstance(box, list) or len(box) != 4 or not 1 <= int(evidence.get("page") or 0) <= len(pdf):
            continue
        low, high = PIECES_COLUMNS[label]
        center = (box[0] + box[2]) / 2
        if not low <= center <= high or not 255 <= box[1] <= 715:
            continue
        row_evidence = references.get(int(match.group(1)))
        if not row_evidence or evidence["page"] != row_evidence["page"]:
            continue
        row_box = row_evidence["bbox"]
        if label != "LINE_ITEM_REFERENCE" and abs((box[1] + box[3]) - (row_box[1] + row_box[3])) > 14:
            continue
        source, error = printed_region(pdf, evidence)
        if error or not same_value(label, field.get("corrected_value") or field.get("extracted_value"), source):
            continue
        if label == "LINE_ITEM_REFERENCE" and not re.fullmatch(r"\d{7,12}", str(field.get("extracted_value") or "")):
            continue
        if label == "LINE_ITEM_UNIT" and source.strip().upper() != "PIECE":
            continue
        if update_from_printed(field, source, evidence, reason="Valeur contrôlée dans la colonne correspondante du tableau imprimé (en-tête graphique vérifié visuellement)."):
            counts[field["status"].lower()] += 1
    return counts


def rexel_rows(pdf: fitz.Document, fields: list[dict]) -> dict[int, dict]:
    """Anchor each product row on its numeric article code, not DEEE subrows."""
    rows = {}
    words_by_page = [page.get_text("words") for page in pdf]
    for field in fields:
        if field.get("label") != "LINE_ITEM_REFERENCE":
            continue
        match = LINE_PATH.search(str(field.get("path") or ""))
        if not match:
            continue
        reference = str(field.get("corrected_value") or field.get("extracted_value") or "")
        if not re.fullmatch(r"\d{7,12}", reference):
            continue
        occurrences = [
            (page_index, word)
            for page_index, words in enumerate(words_by_page)
            for word in words
            if word[4] == reference and 40 <= word[0] <= 100
        ]
        if len(occurrences) != 1:
            continue
        page_index, anchor = occurrences[0]
        page = pdf[page_index]
        same_line = [w for w in words_by_page[page_index] if abs(w[1] - anchor[1]) <= 3.5]
        columns = {
            "LINE_ITEM_NUMBER": [w for w in same_line if 20 <= w[0] < 45 and re.fullmatch(r"\d{1,3}", w[4])],
            "LINE_ITEM_REFERENCE": [anchor],
            "LINE_ITEM_DESCRIPTION": [w for w in same_line if 210 <= w[0] < 510 and w[1] >= anchor[1] - 3.5],
            "LINE_ITEM_QUANTITY": [w for w in same_line if 540 <= w[0] < 570 and re.fullmatch(r"\d+", w[4])],
            "LINE_ITEM_UNIT": [w for w in same_line if 570 <= w[0] < 620 and re.fullmatch(r"[A-Za-z]{2,12}", w[4])],
            "LINE_ITEM_UNIT_PRICE": [w for w in same_line if 630 <= w[0] < 710 and decimal_value(w[4]) is not None],
            "LINE_ITEM_TOTAL": [w for w in same_line if 715 <= w[0] < 779 and decimal_value(w[4]) is not None],
        }
        if not all(columns[k] for k in ("LINE_ITEM_NUMBER", "LINE_ITEM_DESCRIPTION", "LINE_ITEM_QUANTITY", "LINE_ITEM_UNIT", "LINE_ITEM_UNIT_PRICE", "LINE_ITEM_TOTAL")):
            continue
        if any(len(columns[k]) != 1 for k in ("LINE_ITEM_NUMBER", "LINE_ITEM_QUANTITY", "LINE_ITEM_UNIT", "LINE_ITEM_UNIT_PRICE", "LINE_ITEM_TOTAL")):
            continue
        number = int(match.group(1)) + 1
        if columns["LINE_ITEM_NUMBER"][0][4] != str(number):
            continue
        quantity = decimal_value(columns["LINE_ITEM_QUANTITY"][0][4])
        price = decimal_value(columns["LINE_ITEM_UNIT_PRICE"][0][4])
        total = decimal_value(columns["LINE_ITEM_TOTAL"][0][4])
        if abs(quantity * price - total) > Decimal("0.005"):
            continue
        rows[number - 1] = {
            label: (" ".join(w[4] for w in selected), pdf_box(page, selected))
            for label, selected in columns.items()
        }
    return rows


def review_rexel(fields: list[dict], pdf: fitz.Document) -> Counter:
    rows = rexel_rows(pdf, fields)
    counts: Counter[str] = Counter()
    counts["rows_verified"] = len(rows)
    for field in fields:
        if field.get("status") not in {"AMBIGUOUS", "UNREVIEWED"}:
            continue
        match = LINE_PATH.search(str(field.get("path") or ""))
        if not match:
            continue
        row = rows.get(int(match.group(1))) or {}
        label = field.get("label")
        if label not in row:
            continue
        source, evidence = row[label]
        if label == "LINE_ITEM_DESCRIPTION" and not same_value(label, field.get("extracted_value"), source):
            # Wrapped descriptions need a larger source rectangle and review.
            continue
        if update_from_printed(field, source, evidence, reason="Ligne Rexel vérifiée : référence numérique, numéro imprimé, colonnes et quantité × prix = montant."):
            counts[field["status"].lower()] += 1
    return counts


def review_garanka(fields: list[dict], pdf: fitz.Document) -> Counter:
    counts: Counter[str] = Counter()
    refs = {
        int(match.group(1)): field.get("evidence")
        for field in fields
        if field.get("label") == "LINE_ITEM_REFERENCE"
        and (match := LINE_PATH.search(str(field.get("path") or "")))
        and (field.get("evidence") or {}).get("bbox")
    }
    if sum(f.get("label") == "LINE_ITEM_REFERENCE" for f in fields) != 27:
        counts["incomplete_table"] += 1
        return counts
    for field in fields:
        if field.get("status") not in {"AMBIGUOUS", "UNREVIEWED"}:
            continue
        match = LINE_PATH.search(str(field.get("path") or ""))
        if not match:
            continue
        label = field.get("label")
        if label in {"LINE_ITEM_NUMBER", "LINE_ITEM_TOTAL"}:
            # Both pages of this order print Code article, Libellé, Lot,
            # Commandé, PU H.T., Casier — but no line index/line amount.
            field["status"] = "ABSENT"
            field.pop("evidence", None)
            field["comment"] = "Champ absent du tableau imprimé Garanka ; ne pas utiliser une valeur calculée comme preuve PDF. Relecture humaine requise."
            counts["absent"] += 1
            continue
        if label not in {"LINE_ITEM_QUANTITY", "LINE_ITEM_DESCRIPTION"}:
            continue
        evidence = field.get("evidence") or {}
        ref = refs.get(int(match.group(1))) or {}
        box, ref_box = evidence.get("bbox"), ref.get("bbox")
        if not box or not ref_box or evidence.get("page") != ref.get("page"):
            continue
        if abs((box[1] + box[3]) - (ref_box[1] + ref_box[3])) > 15:
            continue
        center = (box[0] + box[2]) / 2
        if label == "LINE_ITEM_QUANTITY" and not 690 <= center <= 755:
            continue
        if label == "LINE_ITEM_DESCRIPTION" and not 185 <= center <= 650:
            continue
        source, error = printed_region(pdf, evidence)
        if error or not same_value(label, field.get("corrected_value") or field.get("extracted_value"), source):
            continue
        if update_from_printed(field, source, evidence, reason="Cellule relue dans le tableau Garanka et alignée sur sa référence article."):
            counts["confirmed"] += 1
    return counts


def ocr_pdf_words(pdf: fitz.Document, *, min_confidence: float = 75) -> list[dict]:
    """High-confidence OCR tokens with normalized PDF-page rectangles."""
    from PIL import Image
    import pytesseract

    words = []
    for page in pdf:
        pix = page.get_pixmap(matrix=fitz.Matrix(190 / 72, 190 / 72), alpha=False)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        data = pytesseract.image_to_data(
            image, lang="fra+eng", config="--psm 6", output_type=pytesseract.Output.DICT
        )
        for index, raw in enumerate(data["text"]):
            value = raw.strip()
            confidence = float(data["conf"][index])
            if not value or confidence < min_confidence:
                continue
            x0, y0 = data["left"][index], data["top"][index]
            x1, y1 = x0 + data["width"][index], y0 + data["height"][index]
            words.append({
                "text": value,
                "page": page.number + 1,
                "bbox": [
                    math.floor(x0 / pix.width * 1000),
                    math.floor(y0 / pix.height * 1000),
                    math.ceil(x1 / pix.width * 1000),
                    math.ceil(y1 / pix.height * 1000),
                ],
            })
    return words


def scanned_prolians_rows(pdf: fitz.Document, fields: list[dict]) -> list[dict]:
    """OCR the image pages, retaining only complete arithmetically valid rows."""
    words = ocr_pdf_words(pdf)
    expected_refs = [
        str(field.get("corrected_value") or field.get("extracted_value") or "")
        for field in fields if field.get("label") == "LINE_ITEM_REFERENCE"
    ]
    if len(expected_refs) != 17 or len(set(expected_refs)) != 17:
        return []
    rows = []
    for reference in expected_refs:
        matches = [
            word for word in words
            if word["text"] == reference and 140 <= word["bbox"][0] <= 280
        ]
        if len(matches) != 1:
            return []
        anchor = matches[0]
        page_number, ay = anchor["page"], anchor["bbox"][1]

        def cell(low_x: int, high_x: int, low_dy: int, high_dy: int, *, digit: bool = False) -> list[dict]:
            found = [
                w for w in words
                if w["page"] == page_number and low_x <= w["bbox"][0] < high_x
                and low_dy <= w["bbox"][1] - ay <= high_dy
            ]
            if digit:
                found = [w for w in found if decimal_value(w["text"]) is not None and "," in w["text"]]
            return sorted(found, key=lambda w: w["bbox"][0])

        quantity = cell(600, 690, -25, -4, digit=True)
        unit = [w for w in cell(625, 690, -5, 7) if w["text"].upper() == "PIECE"]
        price = cell(740, 855, -5, 8, digit=True)
        total = cell(860, 970, -5, 8, digit=True)
        description = cell(35, 600, -25, -4)
        if any(len(items) != 1 for items in (quantity, unit, price, total)) or not description:
            rows.append({"LINE_ITEM_REFERENCE": (reference, {"page": page_number, "bbox": anchor["bbox"]})})
            continue
        q, p, t = (decimal_value(items[0]["text"]) for items in (quantity, price, total))
        if abs(q * p - t) > Decimal("0.05"):
            rows.append({"LINE_ITEM_REFERENCE": (reference, {"page": page_number, "bbox": anchor["bbox"]})})
            continue

        def entry(selected: list[dict]) -> tuple[str, dict]:
            box = [min(w["bbox"][0] for w in selected), min(w["bbox"][1] for w in selected),
                   max(w["bbox"][2] for w in selected), max(w["bbox"][3] for w in selected)]
            return " ".join(w["text"] for w in selected), {"page": page_number, "bbox": box}

        rows.append({
            "LINE_ITEM_REFERENCE": entry([anchor]),
            "LINE_ITEM_DESCRIPTION": entry(description),
            "LINE_ITEM_QUANTITY": entry(quantity),
            "LINE_ITEM_UNIT": entry(unit),
            "LINE_ITEM_UNIT_PRICE": entry(price),
            "LINE_ITEM_TOTAL": entry(total),
        })
    return rows


def review_scanned_prolians(fields: list[dict], pdf: fitz.Document) -> Counter:
    rows = scanned_prolians_rows(pdf, fields)
    counts: Counter[str] = Counter()
    if len(rows) != 17:
        counts["incomplete_ocr"] += 1
        return counts
    counts["rows_ocr_matched"] = sum("LINE_ITEM_TOTAL" in row for row in rows)
    for field in fields:
        if field.get("status") not in {"AMBIGUOUS", "UNREVIEWED"}:
            continue
        match = LINE_PATH.search(str(field.get("path") or ""))
        if not match:
            continue
        label = field.get("label")
        if label == "LINE_ITEM_NUMBER":
            field["status"] = "ABSENT"
            field.pop("evidence", None)
            field["comment"] = "Aucune colonne de numéro de ligne imprimée dans le tableau scanné. Relecture humaine requise."
            counts["absent"] += 1
            continue
        row = rows[int(match.group(1))]
        if label not in row:
            continue
        source, evidence = row[label]
        if label == "LINE_ITEM_DESCRIPTION" and not same_value(label, field.get("extracted_value"), source):
            continue
        if update_from_printed(field, source, evidence, reason="Ligne OCR contrôlée par référence unique, colonne et calcul quantité × prix = montant."):
            counts[field["status"].lower()] += 1
    return counts


def verney_rows(pdf: fitz.Document) -> list[dict]:
    words = ocr_pdf_words(pdf, min_confidence=60)
    anchors = sorted(
        [
            word for word in words
            if 70 <= word["bbox"][0] <= 170
            and 380 <= word["bbox"][1] <= 850
            and re.fullmatch(r"\d{10,11}", word["text"])
        ],
        key=lambda word: (word["page"], word["bbox"][1]),
    )
    # The topmost article on pages 2 and 3 touches the graphic table heading.
    # Tesseract sees its full reference again in the description; keep that
    # printed duplicate as evidence and use the visible product row above it
    # only to align the other cells.
    repeated_page2 = [
        word for word in words
        if word["page"] == 2 and word["text"] == "7733702239"
        and 250 <= word["bbox"][0] <= 400 and 400 <= word["bbox"][1] <= 430
    ]
    if len(repeated_page2) == 1:
        source = repeated_page2[0]
        pseudo = dict(source)
        pseudo["_source_bbox"] = source["bbox"]
        pseudo["bbox"] = [source["bbox"][0], source["bbox"][1] - 27,
                          source["bbox"][2], source["bbox"][3] - 27]
        anchors.append(pseudo)
    for anchor in anchors:
        if anchor["page"] == 3 and anchor["text"] == "7733701822" and anchor["bbox"][1] >= 400:
            anchor["_source_bbox"] = anchor["bbox"]
            anchor["bbox"] = [anchor["bbox"][0], anchor["bbox"][1] - 27,
                              anchor["bbox"][2], anchor["bbox"][3] - 27]
    anchors.sort(key=lambda word: (word["page"], word["bbox"][1]))
    rows = []
    for anchor in anchors:
        page, ay = anchor["page"], anchor["bbox"][1]

        def area(x0: int, x1: int, dy0: int, dy1: int) -> list[dict]:
            return sorted(
                [w for w in words if w["page"] == page and x0 <= w["bbox"][0] < x1
                 and dy0 <= w["bbox"][1] - ay <= dy1],
                key=lambda w: (w["bbox"][1], w["bbox"][0]),
            )

        quantity_words = [w for w in area(545, 620, 7, 22) if re.fullmatch(r"\d+,\d{3}", w["text"])]
        unit_words = [w for w in area(615, 660, 7, 22) if w["text"].upper() == "U"]
        price_words = [w for w in area(660, 750, 7, 22) if re.fullmatch(r"\d+[,.]?\d*|,\d+", w["text"])]
        net_words = [w for w in area(780, 870, 7, 22) if re.fullmatch(r"\d+[,.]?\d*|,\d+", w["text"])]
        if len(quantity_words) != 1 or len(unit_words) != 1 or not price_words or not net_words:
            rows.append({"LINE_ITEM_REFERENCE": (anchor["text"], {"page": page, "bbox": anchor.get("_source_bbox", anchor["bbox"])})})
            continue
        price_text = "".join(w["text"] for w in price_words)
        net_text = "".join(w["text"] for w in net_words)
        if "," not in price_text or decimal_value(price_text) != decimal_value(net_text):
            rows.append({"LINE_ITEM_REFERENCE": (anchor["text"], {"page": page, "bbox": anchor.get("_source_bbox", anchor["bbox"])})})
            continue

        def entry(selected: list[dict], value: str | None = None) -> tuple[str, dict]:
            box = [min(w["bbox"][0] for w in selected), min(w["bbox"][1] for w in selected),
                   max(w["bbox"][2] for w in selected), max(w["bbox"][3] for w in selected)]
            return value if value is not None else " ".join(w["text"] for w in selected), {"page": page, "bbox": box}

        description_words = [
            w for w in area(90, 500, 7, 34)
            if w["text"] != anchor["text"] and w["text"].upper() not in {"U", "|"}
        ]
        rows.append({
            "LINE_ITEM_REFERENCE": (anchor["text"], {"page": page, "bbox": anchor.get("_source_bbox", anchor["bbox"])}),
            "LINE_ITEM_DESCRIPTION": entry(description_words) if description_words else None,
            "LINE_ITEM_QUANTITY": entry(quantity_words),
            "LINE_ITEM_UNIT": entry(unit_words),
            "LINE_ITEM_UNIT_PRICE": entry(price_words, price_text),
        })
    return rows


VERNEY_VISUALLY_READ_DESCRIPTIONS = {
    10: "UNITE INT CASSETTE 4 VOIES 600X600 4,5KW FROID AFZ-4CC 45-1 P R32+R410A",
    11: "UNITE INT CASSETTE 4 VOIES 600X600 3,6KW FROID AFZ-4CC 36-1 P R32+R410A",
    12: "UNITE INT CASSETTE 4 VOIES 600X600 2,8KW FROID AFZ-4CC 28-1 P R32+R410A",
    13: "UNITE INT CASSETTE 4 VOIES 600X600 1,5KW FROID AFZ-4CC 15-1 P R32+R410A",
    14: "UNITE INT CASSETTE 4 VOIES 600X600 2,2KW FROID AFZ-4CC 22-1 P R32+R410A",
    15: "FACADE PR CASSETTE 4 VOIES 600X600 MINI VRF AF4300A-AFZ-P 4CC",
    16: "RACCORD DE DERIVATION PR UNITÉS INTERIEURES VRF AIR FLUX AF-BJ02",
    17: "RACCORD DE DERIVATION PR UNITÉS INTERIEURES VRF AIR FLUX AF-BJ01",
    18: "RACCORD DE DERIVATION PR UNITÉS INTERIEURES VRF AIR FLUX AF-BJ03",
    19: "COMMANDE INDIVIDUELLE FILAIRE PR UNITÉ INTERIEURE VRF-ARC L",
    20: "COMMANDE INDIVIDUELLE FILAIRE PR UNITÉ INTERIEURE VRF-ARC L",
    21: "COMMANDE CENTRALISÉE TACTILE PR VRF ACC MT",
}


def review_verney(fields: list[dict], pdf: fitz.Document) -> Counter:
    rows = verney_rows(pdf)
    counts: Counter[str] = Counter()
    counts["rows_ocr_found"] = len(rows)
    counts["rows_ocr_complete"] = sum("LINE_ITEM_QUANTITY" in row for row in rows)
    if len(rows) != 22 or counts["rows_ocr_complete"] != 22:
        counts["incomplete_ocr"] += 1
        return counts
    old_refs = [f for f in fields if f.get("label") == "LINE_ITEM_REFERENCE"]
    if len(old_refs) != 10 or any(
        str(field.get("extracted_value")) != rows[index]["LINE_ITEM_REFERENCE"][0]
        for index, field in enumerate(old_refs)
    ):
        counts["existing_row_mismatch"] += 1
        return counts
    for field in fields:
        if field.get("status") not in {"AMBIGUOUS", "UNREVIEWED"}:
            continue
        match = LINE_PATH.search(str(field.get("path") or ""))
        if not match:
            continue
        label = field.get("label")
        if label in {"LINE_ITEM_NUMBER", "LINE_ITEM_TOTAL"}:
            field["status"] = "ABSENT"
            field.pop("evidence", None)
            field["comment"] = "Aucune colonne de numéro de ligne ni de montant de ligne ; « Prix net » est un prix unitaire. Relecture humaine requise."
            counts["absent"] += 1
            continue
        source = rows[int(match.group(1))].get(label)
        if not source or label == "LINE_ITEM_DESCRIPTION":
            continue
        if update_from_printed(field, *source, reason="Valeur OCR alignée sur la référence et le prix net imprimé ; brouillon à vérifier visuellement."):
            counts[field["status"].lower()] += 1

    for index, row in enumerate(rows[10:], 10):
        for label, suffix, compartment in (
            ("LINE_ITEM_REFERENCE", "references.material_number", "LINE_ITEMS"),
            ("LINE_ITEM_DESCRIPTION", "description", "LINE_ITEMS"),
            ("LINE_ITEM_QUANTITY", "quantity", "LINE_ITEMS"),
            ("LINE_ITEM_UNIT", "unit", "LINE_ITEMS"),
            ("LINE_ITEM_UNIT_PRICE", "pricing.unit_price", "LINE_ITEMS"),
        ):
            source = row.get(label)
            if not source:
                continue
            value, evidence = source
            if label in {"LINE_ITEM_QUANTITY", "LINE_ITEM_UNIT_PRICE"}:
                value = str(decimal_value(value))
            if label == "LINE_ITEM_DESCRIPTION":
                value = VERNEY_VISUALLY_READ_DESCRIPTIONS[index]
            fields.append({
                "label": label,
                "path": f"order.line_items[{index}].{suffix}",
                "compartment": compartment,
                "extracted_value": None,
                "corrected_value": value,
                "evidence": evidence,
                "status": "CORRECTED",
                "comment": "Ligne manquante ajoutée après OCR et transcription visuelle des pages 2-3 ; relecture humaine requise.",
            })
            counts["added"] += 1
        for label, suffix in (("LINE_ITEM_NUMBER", "line_number"), ("LINE_ITEM_TOTAL", "amounts.net")):
            fields.append({
                "label": label,
                "path": f"order.line_items[{index}].{suffix}",
                "compartment": "LINE_ITEMS",
                "extracted_value": None,
                "corrected_value": None,
                "status": "ABSENT",
                "comment": "Champ non imprimé dans le tableau VERNEY ; relecture humaine requise.",
            })
            counts["added_absent"] += 1
    return counts


def review_two_page_trade_order(fields: list[dict], pdf: fitz.Document, *, kind: str) -> Counter:
    """Check printed cells in the SOROFI and SISCA tables reviewed visually."""
    counts: Counter[str] = Counter()
    expected_rows = 10 if kind == "sorofi" else 9
    references = {
        int(match.group(1)): field.get("evidence")
        for field in fields
        if field.get("label") == "LINE_ITEM_REFERENCE"
        and (match := LINE_PATH.search(str(field.get("path") or "")))
        and (field.get("evidence") or {}).get("bbox")
    }
    if len(references) != expected_rows or len(pdf) != 2:
        counts["incomplete_table"] += 1
        return counts
    by_row = defaultdict(dict)
    for field in fields:
        match = LINE_PATH.search(str(field.get("path") or ""))
        if match:
            by_row[int(match.group(1))][field.get("label")] = field
    for index, row in by_row.items():
        q = decimal_value((row.get("LINE_ITEM_QUANTITY") or {}).get("corrected_value") or (row.get("LINE_ITEM_QUANTITY") or {}).get("extracted_value"))
        p = decimal_value((row.get("LINE_ITEM_UNIT_PRICE") or {}).get("corrected_value") or (row.get("LINE_ITEM_UNIT_PRICE") or {}).get("extracted_value"))
        t = decimal_value((row.get("LINE_ITEM_TOTAL") or {}).get("corrected_value") or (row.get("LINE_ITEM_TOTAL") or {}).get("extracted_value"))
        if any(v is None for v in (q, p, t)) or abs(q * p - t) > Decimal("0.02"):
            continue
        ref = references.get(index) or {}
        for label, field in row.items():
            if field.get("status") not in {"AMBIGUOUS", "UNREVIEWED"}:
                continue
            if label == "LINE_ITEM_NUMBER":
                field["status"] = "ABSENT"
                field.pop("evidence", None)
                field["comment"] = "Le tableau imprimé ne comporte pas de colonne de numéro de ligne ; relecture humaine requise."
                counts["absent"] += 1
                continue
            if label not in ({"LINE_ITEM_QUANTITY", "LINE_ITEM_UNIT", "LINE_ITEM_DESCRIPTION"} if kind == "sorofi" else {"LINE_ITEM_UNIT_PRICE", "LINE_ITEM_TOTAL"}):
                continue
            evidence = field.get("evidence") or {}
            box, ref_box = evidence.get("bbox"), ref.get("bbox")
            if not box or not ref_box or evidence.get("page") != ref.get("page"):
                continue
            if abs((box[1] + box[3]) - (ref_box[1] + ref_box[3])) > 14:
                continue
            center = (box[0] + box[2]) / 2
            low, high = (
                {"LINE_ITEM_QUANTITY": (560, 620), "LINE_ITEM_UNIT": (610, 650), "LINE_ITEM_DESCRIPTION": (220, 555)}
                if kind == "sorofi" else
                {"LINE_ITEM_UNIT_PRICE": (815, 890), "LINE_ITEM_TOTAL": (895, 960)}
            )[label]
            if not low <= center <= high:
                continue
            source, error = printed_region(pdf, evidence)
            if error or not same_value(label, field.get("corrected_value") or field.get("extracted_value"), source):
                continue
            if update_from_printed(field, source, evidence, reason="Cellule de tableau vérifiée dans la bonne ligne et la bonne colonne ; quantité × prix cohérent avec le montant."):
                counts["confirmed"] += 1
    return counts


def gerondeau_rows(pdf: fitz.Document) -> list[dict]:
    words = ocr_pdf_words(pdf, min_confidence=60)
    anchors = sorted(
        [w for w in words if w["page"] == 1 and 70 <= w["bbox"][0] <= 180
         and 400 <= w["bbox"][1] <= 850 and re.fullmatch(r"\d{10,11}", w["text"])],
        key=lambda w: w["bbox"][1],
    )
    rows = []
    for anchor in anchors:
        ay = anchor["bbox"][1]

        def cell(x0: int, x1: int, dy0: int, dy1: int) -> list[dict]:
            return sorted([w for w in words if w["page"] == 1 and x0 <= w["bbox"][0] < x1
                           and dy0 <= w["bbox"][1] - ay <= dy1],
                          key=lambda w: w["bbox"][0])

        quantity = [w for w in cell(510, 570, 8, 22) if re.fullmatch(r"\d+,\d{3}", w["text"])]
        unit = [w for w in cell(570, 615, 8, 22) if w["text"].upper() == "P"]
        price = [w for w in cell(650, 740, 8, 22) if decimal_value(w["text"]) is not None and "," in w["text"]]
        total = [w for w in cell(870, 965, 8, 22) if decimal_value(w["text"]) is not None and "," in w["text"]]
        description = [w for w in cell(70, 410, 8, 22) if w["text"] != anchor["text"]]

        def entry(selected: list[dict]) -> tuple[str, dict]:
            return " ".join(w["text"] for w in selected), {"page": 1, "bbox": [
                min(w["bbox"][0] for w in selected), min(w["bbox"][1] for w in selected),
                max(w["bbox"][2] for w in selected), max(w["bbox"][3] for w in selected),
            ]}

        row = {"LINE_ITEM_REFERENCE": entry([anchor])}
        for label, selected in (("LINE_ITEM_QUANTITY", quantity), ("LINE_ITEM_UNIT", unit),
                                ("LINE_ITEM_UNIT_PRICE", price), ("LINE_ITEM_TOTAL", total)):
            if len(selected) == 1:
                row[label] = entry(selected)
        if description:
            row["LINE_ITEM_DESCRIPTION"] = entry(description)
        q = decimal_value(row.get("LINE_ITEM_QUANTITY", (None,))[0])
        p = decimal_value(row.get("LINE_ITEM_UNIT_PRICE", (None,))[0])
        t = decimal_value(row.get("LINE_ITEM_TOTAL", (None,))[0])
        if t is not None and (q is None or p is None or abs(q * p - t) > Decimal("0.02")):
            row.pop("LINE_ITEM_TOTAL", None)
        rows.append(row)
    return rows


def review_gerondeau(fields: list[dict], pdf: fitz.Document) -> Counter:
    rows = gerondeau_rows(pdf)
    counts: Counter[str] = Counter()
    counts["rows_ocr_found"] = len(rows)
    if len(rows) != 8 or len(pdf) != 2:
        counts["incomplete_ocr"] += 1
        return counts
    references = [f for f in fields if f.get("label") == "LINE_ITEM_REFERENCE"]
    if len(references) != 8 or any(
        str(field.get("extracted_value")) != rows[index]["LINE_ITEM_REFERENCE"][0]
        for index, field in enumerate(references)
    ):
        counts["row_mismatch"] += 1
        return counts
    for field in fields:
        if field.get("status") not in {"AMBIGUOUS", "UNREVIEWED"}:
            continue
        match = LINE_PATH.search(str(field.get("path") or ""))
        if not match:
            continue
        label = field.get("label")
        if label == "LINE_ITEM_NUMBER":
            field["status"] = "ABSENT"
            field.pop("evidence", None)
            field["comment"] = "Aucun numéro de ligne imprimé dans le tableau GERONDEAU ; relecture humaine requise."
            counts["absent"] += 1
            continue
        row = rows[int(match.group(1))]
        if label not in row:
            continue
        source, evidence = row[label]
        if label == "LINE_ITEM_DESCRIPTION" and not same_value(label, field.get("extracted_value"), source):
            continue
        if label == "LINE_ITEM_UNIT" and source.strip() != "P":
            continue
        if update_from_printed(field, source, evidence, reason="Valeur OCR alignée sur la référence article et la colonne imprimée ; relecture humaine requise."):
            counts[field["status"].lower()] += 1
    return counts


def ccl_pdf_rows(pdf: fitz.Document) -> list[dict]:
    """Read the two-page CCL table, including left-margin description wraps.

    The form prints only reference/description, quantity, unit and unit price.
    The line index and amount are not printed, even though the model supplied
    both.  Repeated article references are kept as separate physical rows.
    """
    rows: list[dict] = []
    if len(pdf) != 2:
        return rows
    for page in pdf:
        words = page.get_text("words")
        lower, upper = (520, 690) if page.number == 0 else (95, 230)
        refs = sorted(
            (word for word in words
             if 28 <= word[0] <= 35 and lower <= word[1] < upper
             and re.fullmatch(r"\d{10}", word[4])),
            key=lambda word: word[1],
        )
        for index, ref in enumerate(refs):
            y = ref[1]
            next_y = refs[index + 1][1] if index + 1 < len(refs) else upper
            same_line = [word for word in words if abs(word[1] - y) < 3.5]
            descriptions = sorted(
                (word for word in same_line if 88 <= word[0] < 430),
                key=lambda word: word[0],
            )
            continuation = sorted(
                (word for word in words
                 if 28 <= word[0] < 40 and y + 8 <= word[1] <= y + 15
                 and word[1] < next_y - 3 and not re.fullmatch(r"\d{10}", word[4])),
                key=lambda word: (word[1], word[0]),
            )
            quantities = [word for word in same_line if 448 <= word[0] < 458 and re.fullmatch(r"\d+", word[4])]
            units = [word for word in same_line if 477 <= word[0] < 487 and word[4] == "PI"]
            prices = [word for word in same_line if 520 <= word[0] < 565 and re.fullmatch(r"\d+\.\d{4}", word[4])]
            if not (descriptions and len(quantities) == len(units) == len(prices) == 1):
                continue
            description_words = descriptions + continuation
            rows.append({
                "LINE_ITEM_REFERENCE": (ref[4], pdf_box(page, [ref])),
                "LINE_ITEM_DESCRIPTION": (" ".join(word[4] for word in description_words), pdf_box(page, description_words)),
                "LINE_ITEM_QUANTITY": (quantities[0][4], pdf_box(page, quantities)),
                "LINE_ITEM_UNIT": (units[0][4], pdf_box(page, units)),
                "LINE_ITEM_UNIT_PRICE": (prices[0][4], pdf_box(page, prices)),
            })
    return rows


def review_ccl(fields: list[dict], pdf: fitz.Document) -> Counter:
    rows = ccl_pdf_rows(pdf)
    counts: Counter[str] = Counter(rows_found=len(rows))
    if len(rows) != 11 or [sum(1 for row in rows if row["LINE_ITEM_REFERENCE"][1]["page"] == page) for page in (1, 2)] != [5, 6]:
        counts["incomplete_table"] += 1
        return counts
    old_refs = [field for field in fields if field.get("label") == "LINE_ITEM_REFERENCE"]
    if len(old_refs) != 11 or any(
        str(field.get("corrected_value") or field.get("extracted_value")) != rows[index]["LINE_ITEM_REFERENCE"][0]
        for index, field in enumerate(old_refs)
    ):
        counts["existing_row_mismatch"] += 1
        return counts
    total = sum(decimal_value(row["LINE_ITEM_UNIT_PRICE"][0]) for row in rows)
    if total.quantize(Decimal("0.01")) != Decimal("5347.97"):
        counts["sum_mismatch"] += 1
        return counts

    for field in fields:
        if field.get("status") not in {"AMBIGUOUS", "UNREVIEWED"}:
            continue
        match = LINE_PATH.search(str(field.get("path") or ""))
        if not match:
            continue
        index = int(match.group(1))
        if index >= 11:
            continue
        label = field.get("label")
        if label in {"LINE_ITEM_NUMBER", "LINE_ITEM_TOTAL"}:
            field["status"] = "ABSENT"
            field.pop("evidence", None)
            field["comment"] = "Aucune colonne de numéro ou de montant par ligne : seul le prix unitaire est imprimé. Relecture humaine requise."
            counts["absent"] += 1
        elif label in rows[index] and update_from_printed(
            field, *rows[index][label],
            reason="Cellule relue dans le tableau CCL sur deux pages ; prix et total général rapprochés."
        ):
            counts[field["status"].lower()] += 1

    return counts


def vcfm_pdf_rows(pdf: fitz.Document) -> list[dict]:
    """Read FREI SODIAM rows by printed columns, not PDF text stream order."""
    if len(pdf) != 1:
        return []
    page = pdf[0]
    words = page.get_text("words")
    references = sorted(
        (word for word in words if 25 <= word[0] < 29 and 340 <= word[1] < 710
         and re.fullmatch(r"\d{8,10}", word[4])),
        key=lambda word: word[1],
    )
    result = []
    for ref in references:
        y = ref[1]
        row_words = [word for word in words if abs(word[1] - y) < 3.5]
        number = [word for word in words if 2 <= word[0] < 12 and y + 6 <= word[1] <= y + 10
                  and re.fullmatch(r"\d{1,2}", word[4])]
        description = sorted(
            (word for word in words if 25 <= word[0] < 185 and y + 11 <= word[1] <= y + 16),
            key=lambda word: word[0],
        )
        quantity = [word for word in row_words if 325 <= word[0] < 333 and re.fullmatch(r"\d+", word[4])]
        unit = [word for word in row_words if 334 <= word[0] < 345 and word[4] == "PC"]
        net = sorted((word for word in row_words if 470 <= word[0] < 525.5), key=lambda word: word[0])
        amount = sorted((word for word in row_words if 525.5 <= word[0] < 585), key=lambda word: word[0])
        if not (len(number) == len(quantity) == len(unit) == 1 and description and net and amount):
            continue
        price_value = decimal_value(" ".join(word[4] for word in net))
        amount_value = decimal_value(" ".join(word[4] for word in amount))
        quantity_value = decimal_value(quantity[0][4])
        if any(value is None for value in (price_value, amount_value, quantity_value)):
            continue
        if abs(quantity_value * price_value - amount_value) > Decimal("0.011"):
            continue
        result.append({
            "LINE_ITEM_NUMBER": (number[0][4], pdf_box(page, number)),
            "LINE_ITEM_REFERENCE": (ref[4], pdf_box(page, [ref])),
            "LINE_ITEM_DESCRIPTION": (" ".join(word[4] for word in description), pdf_box(page, description)),
            "LINE_ITEM_QUANTITY": (quantity[0][4], pdf_box(page, quantity)),
            "LINE_ITEM_UNIT": (unit[0][4], pdf_box(page, unit)),
            "LINE_ITEM_UNIT_PRICE": (str(price_value), pdf_box(page, net)),
            "LINE_ITEM_TOTAL": (str(amount_value), pdf_box(page, amount)),
        })
    return result


VCFM_LINE_SUFFIXES = (
    ("LINE_ITEM_NUMBER", "line_number"),
    ("LINE_ITEM_REFERENCE", "references.material_number"),
    ("LINE_ITEM_DESCRIPTION", "description"),
    ("LINE_ITEM_QUANTITY", "quantity"),
    ("LINE_ITEM_UNIT", "unit"),
    ("LINE_ITEM_UNIT_PRICE", "pricing.unit_price"),
    ("LINE_ITEM_TOTAL", "amounts.net"),
)


def review_vcfm(fields: list[dict], pdf: fitz.Document) -> Counter:
    """Rebuild this draft's line list after two middle rows were omitted."""
    rows = vcfm_pdf_rows(pdf)
    counts: Counter[str] = Counter(rows_found=len(rows))
    if len(rows) != 11 or [row["LINE_ITEM_NUMBER"][0] for row in rows] != [str(i) for i in range(1, 12)]:
        counts["incomplete_table"] += 1
        return counts
    total = sum(decimal_value(row["LINE_ITEM_TOTAL"][0]) for row in rows)
    if total != Decimal("7600.67"):
        counts["sum_mismatch"] += 1
        return counts
    old_line_fields = [field for field in fields if LINE_PATH.search(str(field.get("path") or ""))]
    old_ref_by_value = {
        str(field.get("corrected_value") or field.get("extracted_value")): int(LINE_PATH.search(field["path"]).group(1))
        for field in old_line_fields if field.get("label") == "LINE_ITEM_REFERENCE"
    }
    if len(old_ref_by_value) != 9 or any(
        old_ref_by_value.get(row["LINE_ITEM_REFERENCE"][0]) != old_index
        for old_index, row in enumerate(rows[:4] + rows[6:])
    ):
        counts["existing_row_mismatch"] += 1
        return counts
    old_by_key = {
        (int(LINE_PATH.search(field["path"]).group(1)), field["label"]): field
        for field in old_line_fields
    }
    rebuilt = []
    for index, row in enumerate(rows):
        old_index = old_ref_by_value.get(row["LINE_ITEM_REFERENCE"][0])
        for label, suffix in VCFM_LINE_SUFFIXES:
            source, evidence = row[label]
            original = old_by_key.get((old_index, label)) if old_index is not None else None
            field = deepcopy(original) if original else {
                "label": label, "compartment": "LINE_ITEMS", "extracted_value": None,
            }
            field["path"] = f"order.line_items[{index}].{suffix}"
            field["evidence"] = evidence
            old_value = (original.get("corrected_value") or original.get("extracted_value")) if original else None
            if original and same_value(label, old_value, source):
                field["status"] = "CORRECTED" if original.get("corrected_value") else "CONFIRMED"
                counts["confirmed"] += 1
            else:
                field["status"] = "CORRECTED"
                field["corrected_value"] = source
                counts["corrected"] += 1
            field["comment"] = "Ligne et colonne relues sur le PDF FREI SODIAM ; quantité × prix net et total HT contrôlés. Relecture humaine requise."
            rebuilt.append(field)
    fields[:] = [field for field in fields if not LINE_PATH.search(str(field.get("path") or ""))] + rebuilt
    counts["added_fields"] = len(rebuilt) - len(old_line_fields)
    return counts


def partedis_pdf_rows(pdf: fitz.Document) -> list[dict]:
    """Read the seven printed Partedis rows, separating internal/supplier refs."""
    if len(pdf) != 1:
        return []
    page = pdf[0]
    words = page.get_text("words")
    internal = sorted(
        (word for word in words if 18 <= word[0] < 36 and 300 <= word[1] < 425
         and re.fullmatch(r"\d{6,7}", word[4])),
        key=lambda word: word[1],
    )
    if len(internal) != 7:
        return []
    rows = []
    for index, code in enumerate(internal):
        y = code[1]
        same = [word for word in words if abs(word[1] - y) < 2]
        description = sorted((word for word in same if 58 <= word[0] < 320), key=lambda word: word[0])
        quantity = [word for word in same if 463 <= word[0] < 490 and re.fullmatch(r"\d+\.\d{2}p\.", word[4])]
        price = [word for word in same if 503 <= word[0] < 540 and re.fullmatch(r"\d+\.\d{2}p\.", word[4])]
        total = [word for word in same if 545 <= word[0] < 580 and re.fullmatch(r"\d+\.\d{2}", word[4])]
        if not (description and len(quantity) == len(price) == len(total) == 1):
            return []
        supplier_refs = [word for word in same if 355 <= word[0] < 430 and re.fullmatch(r"\d{8,11}", word[4])]
        if index == 5:
            supplier_refs = [word for word in words if 100 <= word[0] < 145 and y + 10 <= word[1] <= y + 14
                             and word[4] == "87160109600"]
        if len(supplier_refs) != 1:
            return []
        if index == 4:
            description += [word for word in words if 58 <= word[0] < 130 and y + 10 <= word[1] <= y + 14
                            and word[4] == "8718579065(1)"]
        quantity_value = quantity[0][4][:-2]
        price_value = price[0][4][:-2]
        total_value = total[0][4]
        if abs(Decimal(quantity_value) * Decimal(price_value) - Decimal(total_value)) > Decimal("0.005"):
            return []
        rows.append({
            "LINE_ITEM_REFERENCE": (supplier_refs[0][4], pdf_box(page, supplier_refs)),
            "LINE_ITEM_DESCRIPTION": (" ".join(word[4] for word in description), pdf_box(page, description)),
            "LINE_ITEM_QUANTITY": (quantity_value, pdf_box(page, quantity)),
            "LINE_ITEM_UNIT_PRICE": (price_value, pdf_box(page, price)),
            "LINE_ITEM_TOTAL": (total_value, pdf_box(page, total)),
        })
    if sum(Decimal(row["LINE_ITEM_TOTAL"][0]) for row in rows) != Decimal("266.85"):
        return []
    return rows


def pdf_words_at(page: fitz.Page, x0: float, y0: float, x1: float, y1: float) -> tuple[str, dict] | None:
    """Return one visually selected text run and its box, rejecting emptiness."""
    words = sorted(
        (word for word in page.get_text("words") if x0 <= word[0] < x1 and y0 <= word[1] < y1),
        key=lambda word: (word[1], word[0]),
    )
    if not words:
        return None
    # The Partedis PDF repeats a hidden text layer. Keep only each physical word.
    unique = list({(round(word[0], 2), round(word[1], 2), word[4]): word for word in words}.values())
    unique.sort(key=lambda word: (word[1], word[0]))
    return " ".join(word[4] for word in unique), pdf_box(page, unique)


PARTEDIS_HEADER_REGIONS = {
    "ORDER_NUMBER": (185, 124, 290, 128),
    "BILL_TO_NAME": (59, 557, 180, 561),
    "BILL_TO_HOUSE_NUMBER": (59, 568, 65, 572),
    "BILL_TO_STREET_TYPE": (65, 568, 78, 572),
    "BILL_TO_STREET_NAME": (78, 568, 170, 572),
    "BILL_TO_POSTAL_CODE": (59, 579, 80, 583),
    "BILL_TO_CITY": (80, 579, 120, 583),
    "BUYER_NAME": (250, 37, 365, 42),
    "BUYER_HOUSE_NUMBER": (250, 62, 267, 67),
    "BUYER_STREET_TYPE": (267, 62, 292, 67),
    "BUYER_STREET_NAME": (292, 62, 380, 67),
    "BUYER_POSTAL_CODE": (250, 92, 290, 98),
    "BUYER_CITY": (290, 92, 335, 98),
    "SHIP_TO_NAME": (59, 469, 150, 474),
    "SHIP_TO_POSTAL_CODE": (59, 502, 80, 507),
    "SHIP_TO_CITY": (80, 502, 120, 507),
    "SHIP_TO_CONTACT_PHONE": (77, 524, 132, 529),
    "TOTAL_NET": (544, 716, 575, 722),
    "BUYER_VAT_NUMBER": (461, 811, 570, 816),
}


def review_partedis_rodez(fields: list[dict], pdf: fitz.Document) -> Counter:
    rows = partedis_pdf_rows(pdf)
    counts: Counter[str] = Counter(rows_found=len(rows))
    if len(rows) != 7:
        counts["incomplete_table"] += 1
        return counts
    page = pdf[0]
    refs = [field for field in fields if field.get("label") == "LINE_ITEM_REFERENCE"]
    if len(refs) != 7 or any(
        str(field.get("corrected_value") or field.get("extracted_value")) != row["LINE_ITEM_REFERENCE"][0]
        for field, row in zip(refs[:5] + refs[6:], rows[:5] + rows[6:])
    ) or str(refs[5].get("corrected_value") or refs[5].get("extracted_value")) != "1420872":
        counts["existing_row_mismatch"] += 1
        return counts
    refs[5]["status"] = "CORRECTED"
    refs[5]["corrected_value"] = rows[5]["LINE_ITEM_REFERENCE"][0]
    refs[5]["evidence"] = rows[5]["LINE_ITEM_REFERENCE"][1]
    refs[5]["comment"] = "Le code interne 1420872 n'est pas la référence fournisseur ; la description imprime REFERENCE 87160109600. Relecture humaine requise."
    counts["corrected"] += 1
    for field in fields:
        if field.get("status") not in {"AMBIGUOUS", "UNREVIEWED"}:
            continue
        match = LINE_PATH.search(str(field.get("path") or ""))
        if match:
            index = int(match.group(1))
            label = field.get("label")
            if label not in rows[index]:
                continue  # p. is fused into a numeric token; leave unit for review.
            source, evidence = rows[index][label]
            if label == "LINE_ITEM_DESCRIPTION" and index == 5:
                source = "JEU D'ISOLATION(1)"  # visually legible; PDF encoding says DïISOLATION.
            if update_from_printed(field, source, evidence, reason="Colonne PARTEDIS relue ; quantité × prix = montant et rôle du code article contrôlés."):
                counts[field["status"].lower()] += 1
            continue
        label = field.get("label")
        if label in {"SUPPLIER_CODE", "CUSTOMER_CODE"}:
            field["status"] = "ABSENT"
            field.pop("evidence", None)
            field["comment"] = "Valeur non imprimée comme code de partie : REF est un en-tête de colonne, PIECE.RODEZ provient d'un courriel. Relecture humaine requise."
            counts["absent"] += 1
            continue
        if label == "ORDER_DATE":
            source = pdf_words_at(page, 360, 124, 425, 129)
            if source and source[0] == "02.11.2026" and field.get("extracted_value") == "2026-11-02":
                field["status"] = "CONFIRMED"
                field["evidence"] = source[1]
                field["comment"] = "Date imprimée à côté du numéro de commande ; relecture humaine requise."
                counts["confirmed"] += 1
            continue
        region = PARTEDIS_HEADER_REGIONS.get(label)
        if not region:
            continue
        source = pdf_words_at(page, *region)
        if source and same_value(label, field.get("corrected_value") or field.get("extracted_value"), source[0]):
            if update_from_printed(field, *source, reason="Champ relié à son bloc imprimé : commande, acheteur, livraison ou facturation."):
                counts["confirmed"] += 1
    return counts


DESENFANS_REFERENCES = (
    "7736507168", "7736606771", "7733701932", "7733701997",
    "7733701997", "7733701935", "7733701566", "7736506273", "7738112340",
)
DESENFANS_DESCRIPTIONS = (
    "CHAUFFE-BAINS GAZ ONDEA HYDROSMART BAS NOX 2 LC12-5 HFPB, VENTOUSE 12 LITRES",
    "MODULE WI-FI (CLE USB) HOMECOM EASY POUR PAC AIR/AIR (CL3/5/6000I) - G10-3",
    "PAC AIR/AIR - U.EXT. BISPLIT - CLIMATE 5000 M - CL5000M 41/2 E - 4,1KW - R32",
    "PAC AIR/AIR - U.INT. MURALE - CLIMATE 5000IM - CL3000IUW20E - 2KW - R32",
    "PAC AIR/AIR - U.INT. MURALE - CLIMATE 5000IM - CL3000IUW20E - 2KW - R32",
    "PAC AIR/AIR - U.EXT. TRISPLIT - CLIMATE 5000 M - CL5000M 79/3 E - 7,9KW - R32",
    "PAC AIR/AIR U.INT MONO/MULTI CLIMATE 3000I 3.5 KW CL3000IUW35E 3,5KW - R32",
    "CHAUFFE-BAIN ONDEA SANS VEILLEUSE LC14-4 PVHYN",
    "RECULATION SANS FIL CE KCR 110 RF - BOSCH",
)


def ocr_box(words: list[dict]) -> dict:
    if not words or len({word["page"] for word in words}) != 1:
        raise ValueError("OCR evidence must be on one page")
    return {
        "page": words[0]["page"],
        "bbox": [
            min(word["bbox"][0] for word in words),
            min(word["bbox"][1] for word in words),
            max(word["bbox"][2] for word in words),
            max(word["bbox"][3] for word in words),
        ],
    }


def desenfans_ocr_rows(words: list[dict]) -> list[dict]:
    """Select the nine printed rows by aligned reference and quantity cells."""
    anchors = sorted(
        (word for word in words if word["page"] == 1 and 90 <= word["bbox"][0] < 125
         and 390 <= word["bbox"][1] < 870 and re.fullmatch(r"\d{10}", word["text"])),
        key=lambda word: word["bbox"][1],
    )
    rows = []
    for anchor in anchors:
        y = anchor["bbox"][1]

        def cell(low: int, high: int) -> list[dict]:
            return [word for word in words if word["page"] == 1
                    and low <= word["bbox"][0] < high
                    and abs(word["bbox"][1] - y) <= 2]

        qty, unit, net, amount = (cell(*span) for span in ((530, 570), (580, 625), (775, 825), (830, 900)))
        amount = [word for word in amount if decimal_value(word["text"].strip("|]")) is not None]
        if not net and not cell(710, 755):
            # On this form gross and net are identical when discount is blank.
            net = cell(640, 700)
        if any(len(items) != 1 for items in (qty, unit, net, amount)):
            continue  # Repeated references in descriptions have no numeric row.
        qty_text = qty[0]["text"].strip("|]")
        net_text = net[0]["text"].strip("|]")
        amount_text = amount[0]["text"].strip("|]")
        if unit[0]["text"].strip("|]") != "PIEC":
            continue
        q, p, total = (decimal_value(value) for value in (qty_text, net_text, amount_text))
        if any(value is None for value in (q, p, total)) or abs(q * p - total) > Decimal("0.02"):
            continue
        description_words = [word for word in words if word["page"] == 1
                             and 90 <= word["bbox"][0] < 490
                             and (y + 8 <= word["bbox"][1] <= y + 14
                                  or (len(rows) < 7 and y + 18 <= word["bbox"][1] <= y + 25))]
        if not description_words:
            continue
        rows.append({
            "LINE_ITEM_REFERENCE": (anchor["text"], ocr_box([anchor])),
            "LINE_ITEM_DESCRIPTION": (DESENFANS_DESCRIPTIONS[len(rows)], ocr_box(description_words)),
            "LINE_ITEM_QUANTITY": (qty_text, ocr_box(qty)),
            "LINE_ITEM_UNIT": ("PCE", ocr_box(unit)),  # Canonical unit for printed PIEC.
            "LINE_ITEM_UNIT_PRICE": (net_text, ocr_box(net)),  # PU Net, not gross P. unit.
            "LINE_ITEM_TOTAL": (amount_text, ocr_box(amount)),
        })
    if [row["LINE_ITEM_REFERENCE"][0] for row in rows] != list(DESENFANS_REFERENCES):
        return []
    if sum(decimal_value(row["LINE_ITEM_TOTAL"][0]) for row in rows) != Decimal("10729.68"):
        return []
    return rows


DESENFANS_NEW_LINE_SUFFIXES = (
    ("LINE_ITEM_NUMBER", "line_number"),
    ("LINE_ITEM_REFERENCE", "references.material_number"),
    ("LINE_ITEM_DESCRIPTION", "description"),
    ("LINE_ITEM_QUANTITY", "quantity"),
    ("LINE_ITEM_UNIT", "unit"),
    ("LINE_ITEM_UNIT_PRICE", "pricing.unit_price"),
    ("LINE_ITEM_TOTAL", "amounts.net"),
)


def review_desenfans(fields: list[dict], pdf: fitz.Document) -> Counter:
    counts: Counter[str] = Counter()
    if len(pdf) != 2 or any(page.get_text().strip() for page in pdf):
        counts["unexpected_pdf_layout"] += 1
        return counts
    words = ocr_pdf_words(pdf, min_confidence=35)
    rows = desenfans_ocr_rows(words)
    counts["rows_found"] = len(rows)
    if len(rows) != 9:
        counts["incomplete_table"] += 1
        return counts
    old_refs = [field for field in fields if field.get("label") == "LINE_ITEM_REFERENCE"]
    if len(old_refs) != 4 or [str(field.get("extracted_value")) for field in old_refs] != list(DESENFANS_REFERENCES[:4]):
        counts["existing_row_mismatch"] += 1
        return counts

    def selected(page: int, x0: int, x1: int, y0: int, y1: int) -> list[dict]:
        return [word for word in words if word["page"] == page and x0 <= word["bbox"][0] < x1
                and y0 <= word["bbox"][1] < y1]

    header = {
        "DOCUMENT_TYPE": ("purchase_order", selected(1, 680, 920, 36, 52)),
        "ORDER_NUMBER": ("CF001946664", selected(1, 80, 225, 214, 230)),
        "ORDER_DATE": ("2026-01-14", selected(1, 295, 395, 214, 230)),
        "DELIVERY_DATE": ("2026-01-15", selected(1, 175, 245, 308, 322)),
        "SUPPLIER_CODE": ("EL003", selected(1, 175, 235, 238, 252)),
        "SUPPLIER_NAME": ("ELM LEBLANC", selected(1, 515, 665, 160, 176)),
        "BUYER_NAME": ("MATHY Emmanuelle", selected(1, 175, 305, 269, 282)),
        "BUYER_CONTACT_PHONE": ("03 27 70 72 50", selected(1, 515, 580, 70, 81)),
        "BUYER_VAT_NUMBER": ("FR24345130660", selected(1, 530, 610, 56, 66)),
        "SHIP_TO_NAME": ("DESENFANS", selected(1, 495, 551, 270, 282)),
        "SHIP_TO_HOUSE_NUMBER": ("83", selected(1, 705, 721, 270, 282)),
        "SHIP_TO_STREET": ("ROUTE DE BAPAUME", selected(1, 722, 823, 270, 282)),
        "SHIP_TO_POSTAL_CODE": ("59400", selected(2, 339, 383, 433, 448)),
        "SHIP_TO_CITY": ("CAMBRAI", selected(2, 386, 446, 433, 448)),
        "TOTAL_NET": ("10729.68", selected(2, 392, 463, 919, 931)),
        "TOTAL_VAT": ("2145.94", selected(2, 401, 463, 940, 952)),
        "OFFER_NUMBER": ("CCH-080725-0558", selected(1, 150, 330, 382, 397)),
        "DEROGATION_NUMBER": ("CRO-130126-1701", selected(1, 155, 330, 444, 454)),
    }
    for field in fields:
        if field.get("status") not in {"AMBIGUOUS", "UNREVIEWED"}:
            continue
        match = LINE_PATH.search(str(field.get("path") or ""))
        if match:
            index = int(match.group(1))
            label = field.get("label")
            if label == "LINE_ITEM_NUMBER":
                field["status"] = "ABSENT"
                field.pop("evidence", None)
                field["comment"] = "No printed row ordinal; the left-hand alphanumeric code is an internal item code. Human review required."
                counts["absent"] += 1
            elif label in rows[index]:
                source, evidence = rows[index][label]
                if update_from_printed(field, source, evidence, reason="Scanned table cell checked against the reference, aligned columns and printed net amount."):
                    counts[field["status"].lower()] += 1
            continue
        label = field.get("label")
        if label in {"SUPPLIER_VAT_NUMBER", "SHIP_TO_COUNTRY", "SHIP_TO_COUNTRY_CODE"}:
            field["status"] = "ABSENT"
            field.pop("evidence", None)
            field["comment"] = "Not printed for this role. The VAT in the buyer letterhead is DESENFANS's; country was inferred. Human review required."
            counts["absent"] += 1
            continue
        if label in header:
            source, evidence_words = header[label]
            if evidence_words and same_value(label, field.get("corrected_value") or field.get("extracted_value"), source):
                if update_from_printed(field, source, ocr_box(evidence_words), reason="Field visually checked in the corresponding printed header or footer block."):
                    counts["confirmed"] += 1

    for index in range(4, 9):
        row = rows[index]
        for label, suffix in DESENFANS_NEW_LINE_SUFFIXES:
            source, evidence = row.get(label, (None, None))
            fields.append({
                "label": label,
                "path": f"order.line_items[{index}].{suffix}",
                "compartment": "LINE_ITEMS",
                "extracted_value": None,
                "corrected_value": source,
                "status": "CORRECTED" if source else "ABSENT",
                **({"evidence": evidence} if evidence else {}),
                "comment": "Missing model line added from the scanned source; human review required.",
            })
            counts["added"] += 1
    return counts


def review_document(document: dict, store: StudioStore) -> tuple[list[dict], Counter]:
    before = document["latestAnnotation"]["field_annotations"]
    fields = deepcopy(before)
    with fitz.open(store.pdf_path(document["documentId"])) as pdf:
        if document["documentId"] in GROUPED_ORDERS:
            counts = review_grouped(fields, pdf)
        elif document["documentId"] in REXEL_ORDERS:
            counts = review_rexel(fields, pdf)
        elif document["documentId"] == GARANKA:
            counts = review_garanka(fields, pdf)
        elif document["documentId"] == SCANNED_PROLIANS:
            counts = review_scanned_prolians(fields, pdf)
        elif document["documentId"] == VERNEY:
            counts = review_verney(fields, pdf)
        elif document["documentId"] in {SOROFI, SISCA}:
            counts = review_two_page_trade_order(fields, pdf, kind="sorofi" if document["documentId"] == SOROFI else "sisca")
        elif document["documentId"] == GERONDEAU:
            counts = review_gerondeau(fields, pdf)
        elif document["documentId"] == CCL:
            counts = review_ccl(fields, pdf)
        elif document["documentId"] == VCFM:
            counts = review_vcfm(fields, pdf)
        elif document["documentId"] == PARTEDIS_RODEZ:
            counts = review_partedis_rodez(fields, pdf)
        elif document["documentId"] == DESENFANS:
            counts = review_desenfans(fields, pdf)
        else:
            counts = review_pieces(fields, pdf)
    baseline = before + [
        {"label": field.get("label"), "path": field.get("path"), "status": "UNREVIEWED"}
        for field in fields[len(before):]
    ]
    if document["documentId"] == VCFM and counts.get("added_fields"):
        # The rebuilt table reorders rows, so index-by-index conflict history
        # does not apply. Accept it only when the entire result is conflict-free.
        conflicts = training_annotation_issues(fields)
        counts["new_training_conflicts"] = len(conflicts)
        if conflicts:
            fields = deepcopy(before)
    else:
        counts["conflicts_downgraded"] = avoid_new_training_conflicts(baseline, fields)
    counts["ambiguous_before"] = sum(f.get("status") == "AMBIGUOUS" for f in before)
    counts["ambiguous_after"] = sum(f.get("status") == "AMBIGUOUS" for f in fields)
    return fields, counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--document-id", choices=sorted(TARGETS))
    args = parser.parse_args()
    if args.apply and os.name == "nt":
        parser.error("Run --apply in Linux against the shared Studio volume, not on Windows")
    store = StudioStore()
    results = []
    for document in store.list_documents(limit=10000):
        if document["documentId"] not in TARGETS or args.document_id and document["documentId"] != args.document_id:
            continue
        if document.get("status") != "DRAFT":
            continue
        fields, counts = review_document(document, store)
        if args.apply and counts["ambiguous_after"] < counts["ambiguous_before"]:
            store.save_annotation(document["documentId"], {
                "status": "DRAFT",
                "reviewed_by": "jin-table-ambiguity-review",
                "field_annotations": fields,
                "note": "Révision des lignes depuis les pages et colonnes du PDF ; brouillon non validé pour l'apprentissage.",
            })
        results.append({"document_id": document["documentId"], "filename": document["filename"], "counts": counts})
    print(json.dumps(results, ensure_ascii=False, default=dict))


if __name__ == "__main__":
    main()

from __future__ import annotations

from collections import Counter

import fitz

from scripts import studio_table_ambiguity_review as review


def test_french_number_parsing_keeps_decimal_and_thousands_distinct():
    assert str(review.decimal_value("2,000")) == "2.000"
    assert str(review.decimal_value("2.168,70")) == "2168.70"
    assert review.same_value("LINE_ITEM_QUANTITY", "2.0", "2,000")
    assert not review.same_value("LINE_ITEM_TOTAL", "12168.7", "2.168,70")


def test_grouped_order_corrects_uv_prefix_and_does_not_invent_line_number(monkeypatch):
    pdf = fitz.open()
    pdf.new_page()
    row = {
        "LINE_ITEM_REFERENCE": ("94137", {"page": 1, "bbox": [90, 500, 130, 515]}),
        "LINE_ITEM_TOTAL": ("2.168,70", {"page": 1, "bbox": [865, 470, 930, 485]}),
    }
    monkeypatch.setattr(review, "grouped_pdf_rows", lambda _: [row] * 72)
    fields = [
        {"label": "LINE_ITEM_NUMBER", "path": "order.line_items[0].line_number", "extracted_value": "1", "status": "AMBIGUOUS", "evidence": {"page": 1, "bbox": [770, 470, 785, 485]}},
        {"label": "LINE_ITEM_TOTAL", "path": "order.line_items[0].amounts.net", "extracted_value": "12168.7", "status": "AMBIGUOUS"},
    ]
    counts = review.review_grouped(fields, pdf)
    assert counts == Counter({"line_number_absent": 1, "corrected": 1})
    assert fields[0]["status"] == "ABSENT"
    assert "evidence" not in fields[0]
    assert fields[1]["status"] == "CORRECTED"
    assert fields[1]["corrected_value"] == "2168.70"
    assert fields[1]["evidence"] == row["LINE_ITEM_TOTAL"][1]


def test_pieces_xpress_confirms_only_matching_cell_in_expected_column():
    pdf = fitz.open()
    page = pdf.new_page(width=595, height=842)
    page.insert_text((10, 250), "87377026500")
    page.insert_text((339, 250), "2,000")
    word = next(w for w in page.get_text("words") if w[4] == "2,000")
    ref_word = next(w for w in page.get_text("words") if w[4] == "87377026500")
    correct_box = review.pdf_box(page, [word])
    fields = [
        {"label": "LINE_ITEM_REFERENCE", "path": "order.line_items[0].references.material_number", "extracted_value": "87377026500", "status": "CONFIRMED", "evidence": review.pdf_box(page, [ref_word])},
        {"label": "LINE_ITEM_QUANTITY", "path": "order.line_items[0].quantity", "extracted_value": "2.0", "status": "AMBIGUOUS", "evidence": correct_box},
        {"label": "LINE_ITEM_TOTAL", "path": "order.line_items[0].amounts.net", "extracted_value": "2.0", "status": "AMBIGUOUS", "evidence": correct_box},
    ]
    counts = review.review_pieces(fields, pdf)
    assert counts["confirmed"] == 1
    assert fields[1]["status"] == "CONFIRMED"
    assert fields[2]["status"] == "AMBIGUOUS"


def test_rexel_uses_printed_price_and_amount_columns():
    pdf = fitz.open()
    page = pdf.new_page(width=842, height=595)
    for x, value in ((32, "1"), (50, "8718599385"), (216, "BOUTEILLE DECOUPLAGE"),
                     (554, "2"), (575, "PIECE"), (649, "10,02000"), (726, "20,04000")):
        page.insert_text((x, 100), value, fontsize=9)
    fields = [
        {"label": "LINE_ITEM_REFERENCE", "path": "order.line_items[0].references.material_number", "extracted_value": "8718599385", "status": "AMBIGUOUS"},
        {"label": "LINE_ITEM_UNIT_PRICE", "path": "order.line_items[0].pricing.unit_price", "extracted_value": "10.02", "status": "AMBIGUOUS", "evidence": {"page": 1, "bbox": [860, 150, 920, 170]}},
        {"label": "LINE_ITEM_TOTAL", "path": "order.line_items[0].amounts.net", "extracted_value": "20.04", "status": "AMBIGUOUS", "evidence": {"page": 1, "bbox": [770, 150, 830, 170]}},
    ]
    counts = review.review_rexel(fields, pdf)
    assert counts["rows_verified"] == 1
    assert counts["confirmed"] == 3
    assert fields[1]["evidence"]["bbox"][0] < fields[2]["evidence"]["bbox"][0]


def test_scanned_prolians_corrects_unit_only_when_ocr_row_is_complete(monkeypatch):
    pdf = fitz.open()
    pdf.new_page()
    row = {
        "LINE_ITEM_REFERENCE": ("7733701564", {"page": 1, "bbox": [165, 500, 250, 510]}),
        "LINE_ITEM_UNIT": ("PIECE", {"page": 1, "bbox": [640, 500, 680, 510]}),
        "LINE_ITEM_TOTAL": ("1.583,85", {"page": 1, "bbox": [870, 500, 950, 510]}),
    }
    monkeypatch.setattr(review, "scanned_prolians_rows", lambda *_: [row] * 17)
    fields = [
        {"label": "LINE_ITEM_NUMBER", "path": "order.line_items[0].line_number", "extracted_value": "1", "status": "AMBIGUOUS"},
        {"label": "LINE_ITEM_UNIT", "path": "order.line_items[0].unit", "extracted_value": "EA", "status": "AMBIGUOUS"},
    ]
    counts = review.review_scanned_prolians(fields, pdf)
    assert counts["absent"] == 1
    assert counts["corrected"] == 1
    assert fields[1]["corrected_value"] == "PIECE"


def test_verney_adds_missing_pages_without_inventing_printed_line_totals(monkeypatch):
    pdf = fitz.open()
    pdf.new_page()
    rows = []
    for index in range(22):
        evidence = {"page": 1, "bbox": [100, 450 + index, 200, 460 + index]}
        rows.append({
            "LINE_ITEM_REFERENCE": (str(7733700000 + index), evidence),
            "LINE_ITEM_DESCRIPTION": ("OCR text", evidence),
            "LINE_ITEM_QUANTITY": ("2,000", evidence),
            "LINE_ITEM_UNIT": ("U", evidence),
            "LINE_ITEM_UNIT_PRICE": ("10,00", evidence),
        })
    monkeypatch.setattr(review, "verney_rows", lambda _: rows)
    fields = [
        {"label": "LINE_ITEM_REFERENCE", "path": f"order.line_items[{index}].references.material_number",
         "extracted_value": str(7733700000 + index), "status": "AMBIGUOUS"}
        for index in range(10)
    ]
    fields.append({"label": "LINE_ITEM_TOTAL", "path": "order.line_items[0].amounts.net",
                   "extracted_value": "20.0", "status": "AMBIGUOUS"})
    counts = review.review_verney(fields, pdf)
    assert counts["rows_ocr_found"] == 22
    assert counts["added"] == 60
    assert counts["added_absent"] == 24
    assert fields[10]["status"] == "ABSENT"
    assert any(f["path"] == "order.line_items[21].references.material_number" for f in fields)
    assert next(f for f in fields if f["path"] == "order.line_items[21].amounts.net")["status"] == "ABSENT"


def test_sisca_requires_row_arithmetic_before_confirming_prices(monkeypatch):
    pdf = fitz.open()
    pdf.new_page()
    pdf.new_page()
    ref = {"page": 1, "bbox": [51, 526, 126, 537]}
    fields = [
        {"label": "LINE_ITEM_REFERENCE", "path": f"order.line_items[{i}].references.material_number",
         "extracted_value": str(7736500000 + i), "evidence": ref, "status": "CONFIRMED"}
        for i in range(9)
    ]
    fields.extend([
        {"label": "LINE_ITEM_QUANTITY", "path": "order.line_items[0].quantity", "extracted_value": "2", "status": "CONFIRMED"},
        {"label": "LINE_ITEM_UNIT_PRICE", "path": "order.line_items[0].pricing.unit_price", "extracted_value": "10", "evidence": {"page": 1, "bbox": [830, 526, 875, 537]}, "status": "AMBIGUOUS"},
        {"label": "LINE_ITEM_TOTAL", "path": "order.line_items[0].amounts.net", "extracted_value": "20", "evidence": {"page": 1, "bbox": [910, 526, 950, 537]}, "status": "AMBIGUOUS"},
        {"label": "LINE_ITEM_QUANTITY", "path": "order.line_items[1].quantity", "extracted_value": "3", "status": "CONFIRMED"},
        {"label": "LINE_ITEM_UNIT_PRICE", "path": "order.line_items[1].pricing.unit_price", "extracted_value": "10", "evidence": {"page": 1, "bbox": [830, 526, 875, 537]}, "status": "AMBIGUOUS"},
        {"label": "LINE_ITEM_TOTAL", "path": "order.line_items[1].amounts.net", "extracted_value": "20", "evidence": {"page": 1, "bbox": [910, 526, 950, 537]}, "status": "AMBIGUOUS"},
    ])
    monkeypatch.setattr(review, "printed_region", lambda _, e: ("10,00" if e["bbox"][0] == 830 else "20,00", ""))
    counts = review.review_two_page_trade_order(fields, pdf, kind="sisca")
    assert counts["confirmed"] == 2
    assert fields[-2]["status"] == "AMBIGUOUS"


def test_gerondeau_corrects_printed_unit_and_leaves_unprinted_index_absent(monkeypatch):
    pdf = fitz.open()
    pdf.new_page()
    pdf.new_page()
    row = {
        "LINE_ITEM_REFERENCE": ("7716705075", {"page": 1, "bbox": [80, 430, 170, 440]}),
        "LINE_ITEM_UNIT": ("P", {"page": 1, "bbox": [580, 445, 590, 455]}),
    }
    monkeypatch.setattr(review, "gerondeau_rows", lambda _: [row] * 8)
    fields = [
        {"label": "LINE_ITEM_REFERENCE", "path": f"order.line_items[{i}].references.material_number",
         "extracted_value": "7716705075", "status": "AMBIGUOUS"}
        for i in range(8)
    ]
    fields.extend([
        {"label": "LINE_ITEM_NUMBER", "path": "order.line_items[0].line_number", "extracted_value": "1", "status": "AMBIGUOUS"},
        {"label": "LINE_ITEM_UNIT", "path": "order.line_items[0].unit", "extracted_value": "PCE", "status": "AMBIGUOUS"},
    ])
    counts = review.review_gerondeau(fields, pdf)
    assert counts["absent"] == 1
    assert counts["corrected"] == 1
    assert fields[-1]["corrected_value"] == "P"


def test_ccl_keeps_all_eleven_physical_rows_but_not_unprinted_totals(monkeypatch):
    pdf = fitz.open()
    pdf.new_page()
    pdf.new_page()
    rows = []
    fields = []
    for index in range(11):
        evidence = {"page": 1 if index < 5 else 2, "bbox": [100, 100 + index, 160, 110 + index]}
        ref = str(7733700000 + index)
        price = "5347.9700" if index == 0 else "0.0000"
        rows.append({
            "LINE_ITEM_REFERENCE": (ref, evidence),
            "LINE_ITEM_DESCRIPTION": ("ARTICLE", evidence),
            "LINE_ITEM_QUANTITY": ("1", evidence),
            "LINE_ITEM_UNIT": ("PI", evidence),
            "LINE_ITEM_UNIT_PRICE": (price, evidence),
        })
        fields.extend([
            {"label": "LINE_ITEM_REFERENCE", "path": f"order.line_items[{index}].references.material_number", "extracted_value": ref, "status": "AMBIGUOUS"},
            {"label": "LINE_ITEM_NUMBER", "path": f"order.line_items[{index}].line_number", "extracted_value": str(index + 1), "status": "AMBIGUOUS"},
            {"label": "LINE_ITEM_TOTAL", "path": f"order.line_items[{index}].amounts.net", "extracted_value": price, "status": "AMBIGUOUS"},
        ])
    monkeypatch.setattr(review, "ccl_pdf_rows", lambda _: rows)
    counts = review.review_ccl(fields, pdf)
    assert counts["rows_found"] == 11
    assert counts["confirmed"] == 11
    assert counts["absent"] == 22
    assert len(fields) == 33
    assert all(field["status"] == "ABSENT" for field in fields if field["label"] in {"LINE_ITEM_NUMBER", "LINE_ITEM_TOTAL"})


def test_vcfm_rebuilds_two_omitted_middle_rows_and_reindexes_following_rows(monkeypatch):
    pdf = fitz.open()
    pdf.new_page()
    rows = []
    for index in range(11):
        evidence = {"page": 1, "bbox": [100, 300 + index * 20, 180, 310 + index * 20]}
        price = "7600.67" if index == 0 else "0"
        rows.append({
            "LINE_ITEM_NUMBER": (str(index + 1), evidence),
            "LINE_ITEM_REFERENCE": (str(7733700000 + index), evidence),
            "LINE_ITEM_DESCRIPTION": ("ARTICLE", evidence),
            "LINE_ITEM_QUANTITY": ("1", evidence),
            "LINE_ITEM_UNIT": ("PC", evidence),
            "LINE_ITEM_UNIT_PRICE": (price, evidence),
            "LINE_ITEM_TOTAL": (price, evidence),
        })
    monkeypatch.setattr(review, "vcfm_pdf_rows", lambda _: rows)
    fields = [{"label": "ORDER_NUMBER", "path": "order.customer_order_number.value", "status": "AMBIGUOUS"}]
    for old_index, row in enumerate(rows[:4] + rows[6:]):
        for label, suffix in review.VCFM_LINE_SUFFIXES:
            fields.append({
                "label": label,
                "path": f"order.line_items[{old_index}].{suffix}",
                "extracted_value": row[label][0],
                "status": "AMBIGUOUS",
            })
    counts = review.review_vcfm(fields, pdf)
    assert counts["rows_found"] == 11
    assert counts["added_fields"] == 14
    assert len(fields) == 1 + 11 * 7
    assert fields[1 + 4 * 7 + 1]["path"] == "order.line_items[4].references.material_number"
    assert fields[1 + 4 * 7 + 1]["corrected_value"] == str(7733700004)
    assert fields[1 + 6 * 7 + 1]["path"] == "order.line_items[6].references.material_number"
    assert fields[1 + 6 * 7 + 1]["extracted_value"] == str(7733700006)


def test_partedis_distinguishes_internal_code_from_printed_supplier_reference(monkeypatch):
    pdf = fitz.open()
    pdf.new_page()
    evidence = {"page": 1, "bbox": [100, 300, 200, 310]}
    rows = [
        {
            "LINE_ITEM_REFERENCE": (str(87000000000 + index), evidence),
            "LINE_ITEM_DESCRIPTION": ("ARTICLE", evidence),
            "LINE_ITEM_QUANTITY": ("1.00", evidence),
            "LINE_ITEM_UNIT_PRICE": ("10.00", evidence),
            "LINE_ITEM_TOTAL": ("10.00", evidence),
        }
        for index in range(7)
    ]
    rows[5]["LINE_ITEM_REFERENCE"] = ("87160109600", evidence)
    monkeypatch.setattr(review, "partedis_pdf_rows", lambda _: rows)
    monkeypatch.setattr(review, "pdf_words_at", lambda *_: None)
    fields = [
        {
            "label": "LINE_ITEM_REFERENCE",
            "path": f"order.line_items[{index}].references.material_number",
            "extracted_value": "1420872" if index == 5 else row["LINE_ITEM_REFERENCE"][0],
            "status": "CONFIRMED",
        }
        for index, row in enumerate(rows)
    ]
    fields += [
        {"label": "LINE_ITEM_UNIT", "path": "order.line_items[5].unit", "extracted_value": "p.", "status": "AMBIGUOUS"},
        {"label": "LINE_ITEM_QUANTITY", "path": "order.line_items[5].quantity", "extracted_value": "1", "status": "AMBIGUOUS"},
        {"label": "SUPPLIER_CODE", "path": "order.parties.supplier.code", "extracted_value": "REF", "status": "AMBIGUOUS"},
    ]
    counts = review.review_partedis_rodez(fields, pdf)
    assert counts["corrected"] == 1
    assert fields[5]["corrected_value"] == "87160109600"
    assert fields[8]["status"] == "CONFIRMED"
    assert fields[9]["status"] == "ABSENT"
    assert fields[7]["status"] == "AMBIGUOUS"


def test_desenfans_ocr_keeps_repeated_reference_as_two_physical_rows():
    values = [
        ("15,000", "434,30", "6514,50"),
        ("5,000", "30,78", "153,90"),
        ("1,000", "525,83", "525,83"),
        ("2,000", "100,04", "200,08"),
        ("2,000", "100,04", "200,08"),
        ("1,000", "975,56", "975,56"),
        ("1,000", "126,11", "126,11"),
        ("3,000", "589,43", "1768,30"),
        ("2,000", "132,66", "265,32"),
    ]
    words = []

    def word(value, x, y):
        return {"text": value, "page": 1, "bbox": [x, y, x + 30, y + 8]}

    for index, (quantity, price, total) in enumerate(values):
        y = 395 + index * 50
        words.extend([
            word(review.DESENFANS_REFERENCES[index], 92, y),
            word(quantity, 540, y),
            word("|PIEC", 590, y),
            word(price, 784, y),
            word(total, 845, y),
            word("ARTICLE", 92, y + 10),
        ])
    words.append(word(review.DESENFANS_REFERENCES[3], 92, 580))
    rows = review.desenfans_ocr_rows(words)
    assert len(rows) == 9
    assert [row["LINE_ITEM_REFERENCE"][0] for row in rows] == list(review.DESENFANS_REFERENCES)
    assert rows[7]["LINE_ITEM_TOTAL"][0] == "1768,30"


def test_desenfans_marks_wrong_supplier_vat_absent_and_adds_five_missing_lines(monkeypatch):
    pdf = fitz.open()
    pdf.new_page()
    pdf.new_page()
    evidence = {"page": 1, "bbox": [100, 400, 200, 410]}
    rows = [
        {
            "LINE_ITEM_REFERENCE": (ref, evidence),
            "LINE_ITEM_DESCRIPTION": ("ARTICLE", evidence),
            "LINE_ITEM_QUANTITY": ("1,000", evidence),
            "LINE_ITEM_UNIT": ("PCE", evidence),
            "LINE_ITEM_UNIT_PRICE": ("1,00", evidence),
            "LINE_ITEM_TOTAL": ("1,00", evidence),
        }
        for ref in review.DESENFANS_REFERENCES
    ]
    monkeypatch.setattr(review, "ocr_pdf_words", lambda *_, **__: [])
    monkeypatch.setattr(review, "desenfans_ocr_rows", lambda _: rows)
    fields = [
        {"label": "LINE_ITEM_REFERENCE", "path": f"order.line_items[{index}].references.material_number",
         "extracted_value": ref, "status": "AMBIGUOUS"}
        for index, ref in enumerate(review.DESENFANS_REFERENCES[:4])
    ]
    fields.append({"label": "SUPPLIER_VAT_NUMBER", "path": "order.parties.supplier.vat_number",
                   "extracted_value": "FR24345130660", "status": "AMBIGUOUS"})
    counts = review.review_desenfans(fields, pdf)
    assert counts["rows_found"] == 9
    assert counts["added"] == 35
    assert fields[4]["status"] == "ABSENT"
    assert len([field for field in fields if field["label"] == "LINE_ITEM_REFERENCE"]) == 9

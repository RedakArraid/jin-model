from __future__ import annotations

from decimal import Decimal
import fitz

from scripts.prepare_rejected_studio_drafts import _line_field_boxes, _printed_numbers


def _fields():
    return [
        {
            "path": f"order.line_items[0].{path}",
            "label": label,
            "extracted_value": value,
            "evidence": {"page": 1, "bbox": [1, 1, 2, 2]},
        }
        for path, label, value in (
            ("quantity", "LINE_ITEM_QUANTITY", 1),
            ("pricing.unit_price", "LINE_ITEM_UNIT_PRICE", 10.26),
            ("amounts.net", "LINE_ITEM_TOTAL", 10.26),
        )
    ]


def test_numeric_row_boxes_separate_quantity_unit_price_and_total(tmp_path):
    source = tmp_path / "order.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 100), "87168315670 ARTICLE")
    page.insert_text((300, 100), "1.00p.")
    page.insert_text((380, 100), "10.26p.")
    page.insert_text((480, 100), "10.26")
    doc.save(source)
    doc.close()

    with fitz.open(source) as pdf:
        row = [60, 88, 520, 102]
        fields = _fields()
        output = {
            "order": {
                "line_items": [{
                    "evidence": {"page": 1, "bbox": row},
                    "pricing": {"unit_price": 10.26},
                    "amounts": {"net": 10.26},
                }]
            }
        }
        assert _line_field_boxes(pdf, fields, output) == 3
        assert [field["evidence"]["bbox"][0] for field in fields] == sorted(
            field["evidence"]["bbox"][0] for field in fields
        )
        assert fields[1]["evidence"]["bbox"] != fields[2]["evidence"]["bbox"]


def test_ambiguous_numeric_box_is_removed(tmp_path):
    source = tmp_path / "order.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((60, 100), "87168315670 ARTICLE 10.26")
    doc.save(source)
    doc.close()
    fields = _fields()
    output = {
        "order": {"line_items": [{
            "evidence": {"page": 1, "bbox": [60, 88, 520, 102]},
            "pricing": {"unit_price": 10.26},
            "amounts": {"net": 10.26},
        }]}
    }
    with fitz.open(source) as pdf:
        assert _line_field_boxes(pdf, fields, output) == 0
    assert all("evidence" not in field for field in fields)


def test_printed_numbers_preserve_separate_values_and_thousands():
    assert _printed_numbers("193,96500 2.999,64000 1 964,35") == [
        Decimal("193.965"), Decimal("2999.64"), Decimal("1964.35")
    ]

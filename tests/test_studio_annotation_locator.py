import fitz

from scripts.studio_annotation_locator import indexed_occurrences, lookup_keys


def test_locator_finds_full_article_code_from_numeric_preannotation():
    document = fitz.open()
    page = document.new_page()
    page.insert_text((70, 100), "EL87167734910")
    keys = lookup_keys({"label": "LINE_ITEM_REFERENCE", "extracted_value": "87167734910"})
    index = indexed_occurrences(document, keys)
    assert [item["text"] for item in index["87167734910"]] == ["EL87167734910"]


def test_locator_generates_printed_numeric_and_date_variants():
    assert "1" in lookup_keys({"label": "LINE_ITEM_QUANTITY", "extracted_value": 1.0})
    assert "1000" in lookup_keys({"label": "LINE_ITEM_QUANTITY", "extracted_value": 1.0})
    assert "07082026" in lookup_keys({"label": "ORDER_DATE", "extracted_value": "2026-08-07"})

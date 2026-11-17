"""PO presentation follows the source; compact keys are comparison-only."""
import copy
import re

import pytest

from jin_runtime.clean_output import build_clean_output
from jin_runtime.header_fields import reconcile_header_fields
from jin_runtime.order_numbers import check_order_number


def _block(text, bbox):
    return {"text": text, "source": {"page": 1, "bbox": bbox}}


def _payload(number, *, pages=None, blocks=None):
    return {
        "document": {"primary_document_type": "purchase_order"},
        "pages": pages or [], "blocks": blocks or [],
        "business_extractions": {"purchase_order": {"purchase_order": {
            "number": {"value": number, "evidence": {
                "page": 1, "bbox": [300, 148, 555, 162],
                "source_text": number, "extraction_method": "inline_transaction_header",
            }},
        }}},
    }


@pytest.mark.parametrize("raw,expected", [
    ("26 365 / CPM", "26 365 / CPM"),
    ("26  365   /  CPM", "26 365 / CPM"),
    ("26\u00a0365\u202f/\tCPM", "26 365 / CPM"),
    ("26 365/CPM", "26 365/CPM"),
    ("26 365/ CPM", "26 365/ CPM"),
    ("26 365 /CPM", "26 365 /CPM"),
    ("26365/CPM", "26365/CPM"),
    ("00 365 / EX", "00 365 / EX"),
    ("AB 12 345 / EX", "AB 12 345 / EX"),
])
@pytest.mark.parametrize("compact_input", [False, True])
def test_suffix_recovery_preserves_source_spacing_through_clean_contract(raw, expected, compact_input):
    base = raw.split("/", 1)[0].strip()
    current = re.sub(r"\s+", "", base) if compact_input else " ".join(base.split())
    doc = _payload(current, pages=[
        {"page": page, "text": "COMMANDE FOURNISSEUR : " + raw}
        for page in (1, 2, 3)
    ])
    original_pages = copy.deepcopy(doc["pages"])
    reconcile_header_fields(doc)
    po = doc["business_extractions"]["purchase_order"]
    field = po["purchase_order"]["number"]
    assert field["value"] == field["normalized_value"] == expected
    assert field["raw_value"] == raw
    assert field["evidence"]["source_text"] == raw
    doc["order_number_check"] = check_order_number(po, {})
    assert doc["order_number_check"]["issues"] == []
    clean = build_clean_output(doc)
    assert clean["order"]["customer_order_number"]["value"] == expected
    assert clean["quality"]["order_number"]["value"] == expected
    assert doc["pages"] == original_pages
    first_pass = copy.deepcopy(doc)
    reconcile_header_fields(doc)
    assert doc == first_pass


@pytest.mark.parametrize("source", ["COMMANDE FOURNISSEUR : 26 366 / CPM", "N° Cde : 26 365 / CPM"])
def test_suffix_is_not_attached_without_matching_base_or_strong_context(source):
    doc = _payload("26 365", pages=[{"page": 1, "text": source}])
    reconcile_header_fields(doc)
    assert doc["business_extractions"]["purchase_order"]["purchase_order"]["number"]["value"] == "26 365"


def test_conflicting_suffixes_do_not_overwrite_number():
    doc = _payload("26 365", pages=[
        {"page": 1, "text": "COMMANDE FOURNISSEUR : 26 365 / CPM"},
        {"page": 2, "text": "N° Cde : 26 365 / AUTRE"},
    ])
    reconcile_header_fields(doc)
    assert doc["business_extractions"]["purchase_order"]["purchase_order"]["number"]["value"] == "26 365"


@pytest.mark.parametrize("initial", ["248061", "061 / ENGAGEMENT 3"])
def test_reference_column_preserves_spaces_even_when_compact_number_already_agrees(initial):
    doc = _payload(initial, blocks=[
        _block("N° / Référence Commande", [114, 223, 240, 235]),
        _block("17/03/2026 248  061 / ENGAGEMENT 3", [28, 244, 239, 256]),
    ])
    reconcile_header_fields(doc)
    field = doc["business_extractions"]["purchase_order"]["purchase_order"]["number"]
    assert field["value"] == "248 061"


def test_piece_column_keeps_digit_groups_and_leading_zeroes():
    doc = _payload("002193", blocks=[
        _block("Date", [20, 228, 45, 239]), _block("Pièce", [100, 228, 130, 239]),
        _block("Client", [170, 228, 205, 239]), _block("Commercial", [250, 228, 305, 239]),
        _block("08/04/2026", [20, 246, 75, 256]),
        _block("00  2193", [98, 246, 140, 256]),
    ])
    reconcile_header_fields(doc)
    assert doc["business_extractions"]["purchase_order"]["purchase_order"]["number"]["value"] == "00 2193"


def test_repeated_inline_title_preserves_first_source_spacing():
    doc = _payload("DU05001100", blocks=[_block(
        "COMMANDE N° 061 066 532 du 14-09-2026 CDE N° 061066532", [300, 3, 580, 60],
    )])
    reconcile_header_fields(doc)
    assert doc["business_extractions"]["purchase_order"]["purchase_order"]["number"]["value"] == "061 066 532"

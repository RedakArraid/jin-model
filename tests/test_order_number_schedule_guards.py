"""A reception schedule must never supersede the explicit customer PO header."""
import copy

import pytest

from jin_runtime.clean_output import build_clean_output
from jin_runtime.geometry_fields import _explicit_order_number_v3, visual_lines
from jin_runtime.order_numbers import check_order_number
from jin_runtime.weak_field_reconciliation import reconcile_weak_fields


def token(text, x, y, width=40, height=12):
    return {"text": text, "bbox": [x, y, x + width, y + height],
            "page_width": 595, "page_height": 842}


def source_number(value="PR R CH 01054"):
    return {"value": value, "final_confidence": .9927, "evidence": {
        "page": 1, "bbox": [22, 206, 137, 227], "source_text": value,
        "extraction_method": "below_numero_label_grouped_erp_value",
    }}


def candidate(value):
    return {"value": value, "source_text": value, "page": 1,
            "bbox": [275, 572, 507, 588], "confidence": .998,
            "source": "geometry_explicit_order_label_v3"}


def payload(number=None, suggestion=None):
    return {
        "document": {"primary_document_type": "purchase_order"},
        "business_extractions": {"purchase_order": {"purchase_order":
            {"number": number} if number else {}}},
        "weak_field_suggestions": {"anchored_fields":
            {"order_number": suggestion} if suggestion else {}},
    }


def test_explicit_header_between_fax_and_grouped_identifier_breaks_contact_ownership():
    # The PDF's native word boxes overlap because of its italic font. The fax
    # belongs to the agency block above, not to the new COMMANDE N° cell.
    words = [
        token("Fax", 14, 160, 20), token("0000000000", 50, 160, 60),
        token("Mail", 14, 172, 25), token("agency@example.test", 45, 172, 140),
        token("COMMANDE", 23, 196, 52), token("N°", 77, 196, 11),
        token("DRANCY", 309, 197, 36),
        token("PR", 22, 206, 26, 20), token("R", 45, 206, 17, 20),
        token("CH", 60, 206, 28, 20), token("01054", 90, 206, 46, 20),
        token("93700", 309, 209, 30), token("DRANCY", 345, 209, 36),
    ]
    result = _explicit_order_number_v3(visual_lines([words]))
    assert result is not None
    assert result["value"] == "PR R CH 01054"
    assert result["source_text"] == "PR R CH 01054"


@pytest.mark.parametrize("following", ["8H00-11H45", "12345678"])
def test_instruction_to_repeat_complete_order_reference_is_not_a_column_label(following):
    words = [
        token("DE", 38, 469, 17), token("COMMANDE", 54, 469, 63),
        token("AVEC", 116, 469, 31), token("REFERENCE", 145, 469, 61),
        token("COMMANDE", 205, 469, 64), token("COMPLETE", 267, 469, 57),
        token("HORAIRES", 38, 482, 54), token("DE", 91, 482, 17),
        token("RECEPTION", 106, 482, 59), token("MARCHANDISES", 164, 482, 83),
        token(following, 245, 482, 57),
    ]
    assert _explicit_order_number_v3(visual_lines([words])) is None


SCHEDULES = ["MARCHANDISES 8H00-11H45", "08:00 - 11:45", "8 h 00 à 11 h 45", "8h-12h"]


@pytest.mark.parametrize("schedule", SCHEDULES)
@pytest.mark.parametrize("existing", [False, True])
def test_reception_time_range_is_never_promoted_even_with_high_confidence(schedule, existing):
    original = source_number() if existing else None
    doc = payload(copy.deepcopy(original), candidate(schedule))
    reconcile_weak_fields(doc)
    header = doc["business_extractions"]["purchase_order"]["purchase_order"]
    assert header.get("number") == original
    assert "number_original" not in header


@pytest.mark.parametrize("schedule", SCHEDULES)
def test_final_audit_rejects_source_supported_time_range(schedule):
    doc = {"purchase_order": {"number": source_number(schedule)}}
    result = check_order_number(doc, {})
    assert "ORDER_NUMBER_FORMAT_SUSPICIOUS" in result["issues"]
    assert result["status"] == "REVIEW_REQUIRED"


@pytest.mark.parametrize("schedule", SCHEDULES)
def test_time_range_is_not_independent_order_number_corroboration(schedule):
    result = check_order_number({"purchase_order": {"number": source_number()}}, {
        "anchored_fields": {"order_number": candidate(schedule)},
    })
    assert result["issues"] == []
    assert result["corroborating_candidates"] == []


def test_grouped_explicit_core_identifier_is_preserved_but_real_conflict_is_reported():
    original = source_number()
    doc = payload(copy.deepcopy(original), candidate("PO-987654"))
    reconcile_weak_fields(doc)
    po = doc["business_extractions"]["purchase_order"]
    assert po["purchase_order"]["number"] == original
    check = check_order_number(po, doc["weak_field_suggestions"])
    assert "ORDER_NUMBER_CANDIDATES_DISAGREE" in check["issues"]


def test_replacement_of_grouped_source_identifier_is_audited():
    po = {"purchase_order": {"number": source_number("PO-987654"),
                              "number_original": source_number()}}
    assert "ORDER_NUMBER_REPLACED_DIFFERENT_CORE_VALUE" in check_order_number(po, {})["issues"]


@pytest.mark.parametrize("value", ["PR R CH 01054", "AB8H0011H45", "PO-0800-1145", "PO-8H00-11H45"])
def test_legitimate_identifiers_keep_source_spacing_and_are_not_time_ranges(value):
    doc = payload(source_number(value), candidate("MARCHANDISES 8H00-11H45"))
    reconcile_weak_fields(doc)
    po = doc["business_extractions"]["purchase_order"]
    doc["order_number_check"] = check_order_number(po, doc["weak_field_suggestions"])
    clean = build_clean_output(doc)
    assert clean["order"]["customer_order_number"]["value"] == value
    assert clean["quality"]["order_number"]["status"] == "SOURCE_SUPPORTED"

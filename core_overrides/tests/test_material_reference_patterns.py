from types import SimpleNamespace

from uda.material_references import (
    enrich_supplier_material_roles,
    line_material_pattern_summary,
    load_material_reference_profile,
    reference_signature,
    resolve_material_reference,
    score_material_reference,
)


def _line(reference, *, description="Produit", quantity=1, price=10, total=10):
    return SimpleNamespace(
        material_number=reference,
        article_number=reference,
        product_code=None,
        customer_material_number=None,
        supplier_material_number=None,
        manufacturer_part_number=None,
        sku=None,
        description=description,
        quantity=quantity,
        unit_price=price,
        line_total=total,
    )


def test_reference_signature_preserves_shape_without_exposing_reference():
    assert reference_signature("8750772763T03") == "D10-A1-D2"
    assert reference_signature(" T00432214A ") == "A1-D8-A1"
    assert reference_signature("8716 860 570") == "D10"


def test_material_profile_supports_families_not_only_exact_known_values():
    profile = load_material_reference_profile({})
    assert profile is not None
    # Synthetic values follow high-support families but do not need exact-list lookup.
    assert score_material_reference("8719999999", profile).supported
    assert score_material_reference("7739999999", profile).supported
    assert score_material_reference("63099999", profile).supported


def test_material_profile_rejects_common_header_numeric_noise():
    profile = load_material_reference_profile({})
    assert profile is not None
    for value in (
        "9260205710",       # CCL purchase-order number
        "0612345678",       # French telephone number
        "20261008",         # compact date
        "FR38790553242",    # VAT identifier
        "06-253460",        # customer order/reference with separator
        "00013",            # SIRET establishment suffix
        "60000",            # postal code / commercial line number
    ):
        assert not score_material_reference(value, profile).supported, value


def test_pattern_bonus_requires_a_detected_commercial_line():
    supported = line_material_pattern_summary([_line("8719999999")], {})
    assert supported["supported_lines"] == 1
    assert supported["bonus"] > 0

    no_description = line_material_pattern_summary(
        [_line("8719999999", description="")], {}
    )
    assert no_description["eligible_lines"] == 0
    assert no_description["bonus"] == 0

    no_commercial_values = line_material_pattern_summary(
        [_line("8719999999", quantity=None, price=None, total=None)], {}
    )
    assert no_commercial_values["eligible_lines"] == 0
    assert no_commercial_values["bonus"] == 0


def test_role_enrichment_copies_source_value_but_never_rewrites_it():
    line = _line("8716 999 999")
    assert enrich_supplier_material_roles([line], {}) == 1
    assert line.material_number == "8716 999 999"
    assert line.supplier_material_number == "8716 999 999"

    order_number = _line("9260205710")
    assert enrich_supplier_material_roles([order_number], {}) == 0
    assert order_number.supplier_material_number is None


def test_configured_source_prefix_is_separated_from_canonical_reference():
    profile = load_material_reference_profile({})
    assert profile is not None
    for source in (
        "EL 8719999999",
        "EL+8719999999",
        "EL-8719999999",
        "EL8719999999",
    ):
        resolved = resolve_material_reference(source, profile, {})
        assert resolved.match.supported, source
        assert resolved.source_prefix == "EL"
        assert resolved.reference_value == "8719999999"


def test_source_prefix_supports_confirmed_short_code_without_weakening_global_safety():
    profile = load_material_reference_profile({})
    assert profile is not None
    assert not score_material_reference("73924", profile).supported
    resolved = resolve_material_reference("EL 73924", profile, {})
    assert resolved.match.supported
    assert resolved.reference_value == "73924"


def test_source_prefix_does_not_consume_words_or_unconfigured_prefixes():
    profile = load_material_reference_profile({})
    assert profile is not None
    for source in ("ELECTRODE", "FR38790553242", "ABC 8719999999"):
        resolved = resolve_material_reference(source, profile, {})
        assert resolved.source_prefix is None
        assert not resolved.match.supported


def test_role_enrichment_preserves_prefixed_source_and_types_canonical_reference():
    line = _line("EL 8719999999")
    assert enrich_supplier_material_roles([line], {}) == 1
    assert line.material_number == "EL 8719999999"
    assert line.supplier_material_number == "8719999999"
    assert line.material_reference_source_prefix == "EL"

    supplier_only = _line(None)
    supplier_only.supplier_material_number = "EL 8719999999"
    assert enrich_supplier_material_roles([supplier_only], {}) == 1
    assert supplier_only.material_number == "EL 8719999999"
    assert supplier_only.supplier_material_number == "8719999999"
    assert supplier_only.material_reference_source_prefix == "EL"


def test_pattern_summary_reports_prefixed_support_for_auditing():
    summary = line_material_pattern_summary([_line("EL 8719999999")], {})
    assert summary["supported_lines"] == 1
    assert summary["prefixed_supported_lines"] == 1
    assert summary["source_prefix_counts"] == {"EL": 1}


def test_repeated_high_confidence_unknown_prefix_is_discovered_per_document():
    config = {"layout": {"material_reference_source_prefixes": []}}
    first = _line("ZX 8719999999")
    second = _line("ZX+7739999999")

    summary = line_material_pattern_summary([first, second], config)
    assert summary["source_prefix_counts"] == {"ZX": 2}

    assert enrich_supplier_material_roles([first, second], config) == 2
    assert first.supplier_material_number == "8719999999"
    assert second.supplier_material_number == "7739999999"
    assert first.material_reference_source_prefix == "ZX"
    assert second.material_reference_source_prefix == "ZX"


def test_unknown_prefix_is_not_inferred_from_a_single_line():
    config = {"layout": {"material_reference_source_prefixes": []}}
    line = _line("ZX 8719999999")
    assert enrich_supplier_material_roles([line], config) == 0
    assert line.supplier_material_number is None

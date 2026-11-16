from scripts.studio_annotation_pdf_audit import value_matches


def test_pdf_audit_requires_exact_short_tokens():
    assert value_matches("LINE_ITEM_QUANTITY", "1.0", "1")
    assert value_matches("LINE_ITEM_QUANTITY", "1.0", "1,000")
    assert not value_matches("LINE_ITEM_QUANTITY", "1.0", "10")
    assert value_matches("LINE_ITEM_UNIT_PRICE", "249.15", "249,15")


def test_pdf_audit_normalizes_dates_but_not_document_type_from_arbitrary_text():
    assert value_matches("ORDER_DATE", "2026-08-07", "07/08/2026")
    assert value_matches("DOCUMENT_TYPE", "purchase_order", "BON DE COMMANDE")
    assert not value_matches("DOCUMENT_TYPE", "purchase_order", "OFFRE DE PRIX")


def test_pdf_audit_requires_full_reference_not_partial_numeric_suffix():
    assert value_matches("LINE_ITEM_REFERENCE", "EL87167734910", "EL87167734910")
    assert not value_matches("LINE_ITEM_REFERENCE", "EL87167734910", "87167734910")

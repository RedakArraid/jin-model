from types import SimpleNamespace

from uda.engine import _promote_transaction_continuations


def test_bracketed_materials_promote_product_only_continuation_page():
    classifications = [
        {"page": 1, "type": "purchase_order", "confidence": 0.99},
        {"page": 2, "type": "generic_document", "confidence": 0.35},
    ]
    pages = [
        SimpleNamespace(page=1, text="BON DE COMMANDE #P02919"),
        SimpleNamespace(page=2, text=(
            "[7733703609] Eco- 10/11/2026 4,00 80,370 321,48 €\n"
            "[7733701938] Eco- 10/11/2026 1,00 1 427,120 1 427,12 €\n"
            "Montant hors taxes 7 367,94 €"
        )),
    ]

    result = _promote_transaction_continuations(classifications, [], pages, {})

    assert result[1]["type"] == "purchase_order"
    assert "bracketed_materials=2" in result[1]["signals"][0]


def test_legal_annex_without_product_amounts_is_not_promoted():
    classifications = [
        {"page": 1, "type": "purchase_order", "confidence": 0.99},
        {"page": 2, "type": "generic_document", "confidence": 0.35},
    ]
    pages = [
        SimpleNamespace(page=1, text="BON DE COMMANDE #P02919"),
        SimpleNamespace(page=2, text="Conditions générales : [7733703609] [7733701938]"),
    ]

    result = _promote_transaction_continuations(classifications, [], pages, {})

    assert result[1]["type"] == "generic_document"

from scripts.audit_delivery_dates import audit_item, source_delivery_date_candidates


def test_source_campaign_finds_line_receipt_dates_without_quote_validity_date():
    text = """N° Référence fournisseur N° Quantité Code Date P. Unit P. Unit Montant
ligne unité réception brut Net
7716701550 504201498 1 PC 03/03/26 1 346,00 1 346,00 1 346,00
Egalis ballon Icondens GVD IC 24
Offre n° VBe-030226-1411 (31/07/26)
87168402200 504201042 1 PC 04/03/26 131,00 49,78 49,78
TOTAL HT 1 395,78 EUR"""

    result = source_delivery_date_candidates(text)

    assert {item["value"] for item in result["candidates"]} == {
        "2026-03-03", "2026-03-04",
    }
    assert {item["scope"] for item in result["candidates"]} == {"line"}


def test_delivery_campaign_compares_source_and_model_dates():
    item = {
        "filename": "order.pdf",
        "sha256": "x",
        "source_text": "Date de livraison souhaitée : 18/03/2026",
        "model": {"requested_delivery_date": "2026-03-18"},
    }

    audited = audit_item(item)

    assert audited["status"] == "SOURCE_MATCH"
    assert audited["source_order_dates"] == ["2026-03-18"]


def test_source_campaign_does_not_mix_order_date_before_delivery_label():
    result = source_delivery_date_candidates(
        "Date commande 07/04/2026 Commande "
        "Date de livraison souhaitée : 13/04/2026"
    )

    assert [item["value"] for item in result["candidates"]] == ["2026-04-13"]


def test_source_campaign_supports_requested_date_qualifiers():
    demanded = source_delivery_date_candidates(
        "Date de livraison demandée 14/03/2026 ELM LEBLANC SAS"
    )
    estimated = source_delivery_date_candidates(
        "Date de livraison estimée: 04.05.2026 1 PC"
    )

    assert [item["value"] for item in demanded["candidates"]] == ["2026-03-14"]
    assert [item["value"] for item in estimated["candidates"]] == ["2026-05-04"]

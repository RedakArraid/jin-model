import json
from pathlib import Path

from po_ocr.models import PageResult
from po_ocr.extract import (
    all_rows, extract_header, extract_spatial_header_fields, extract_parties,
    enhance_parties_spatial, extract_multiline_lines_from_pages,
    extract_totals, enhance_totals_spatial, enhance_commercial_terms,
    extract_document_notes,
)

ROOT = Path(__file__).resolve().parents[1]
PAGE = PageResult.model_validate(json.loads((ROOT / 'tests/fixtures/cf000337886_page.json').read_text(encoding='utf-8')))


def test_cf000337886_spatial_header_and_parties():
    rows = all_rows([PAGE], y_factor=0.65)
    fields, commercial, logistics = extract_header(rows, 'EUR')
    fields = extract_spatial_header_fields([PAGE], fields)
    parties = enhance_parties_spatial([PAGE], extract_parties(rows))
    commercial, logistics = enhance_commercial_terms(rows, commercial, logistics)

    assert fields['number'].value == 'CF000337886'
    assert fields['order_date'].value == '2026-06-23'
    assert fields['language'].value == 'fr'
    assert parties['buyer'].name == 'VERNEY S.A.'
    assert parties['buyer'].contact.name == 'AG DELATTRE'
    assert parties['supplier'].code == 'EL001'
    assert parties['supplier'].name == 'ELM LEBLANC'
    assert parties['supplier'].address.postal_code == '54000'
    assert parties['supplier'].address.city == 'NANCY'
    assert parties['ship_to'].name == 'VERNEY SA'
    assert parties['ship_to'].address.postal_code == '21076'
    assert commercial.payment_terms == '30 J le 15'
    assert commercial.payment_method == 'VIREMENTS'
    assert commercial.freight_terms == 'Port franco'
    assert logistics.delivery_terms == 'Livre'
    assert logistics.delivery_location == 'Dijon'


def test_cf000337886_multiline_items_and_totals():
    lines = extract_multiline_lines_from_pages([PAGE], 'EUR', min_header_matches=3)
    assert len(lines) == 3
    assert [
        (x.material_number, x.supplier_material_number, x.customer_material_number)
        for x in lines
    ] == [
        ('7733701812', '7733701812', 'C27040'),
        ('7733701813', '7733701813', 'C27044'),
        ('7716704754', '7716704754', 'C26952'),
    ]
    assert [x.quantity for x in lines] == [1.0, 1.0, 4.0]
    assert [x.unit_price for x in lines] == [254.34, 603.45, 560.0]
    assert [x.line_total for x in lines] == [254.34, 603.45, 2240.0]
    assert [x.requested_delivery_date for x in lines] == ['2026-06-22'] * 3
    assert [x.tax_code for x in lines] == ['8', '8', '8']
    assert lines[0].customer_reference == "A L'ATTENTION D'EMINA"
    assert lines[1].quote_number == 'MCH-100626-1912'
    assert lines[2].quote_number == 'X27/JGR-2026-001'

    rows = all_rows([PAGE], y_factor=0.65)
    totals = enhance_totals_spatial([PAGE], extract_totals(rows, 'EUR'), 'EUR')
    assert totals.total_net == 3097.79
    assert totals.total_vat == 619.56
    assert totals.grand_total == 3717.35

    notes = extract_document_notes(rows)
    assert 'HORAIRES DE RECEPTION' in notes['delivery_instructions']
    assert not any('C27040' in x for x in notes['delivery_instructions'])

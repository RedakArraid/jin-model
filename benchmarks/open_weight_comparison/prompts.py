from __future__ import annotations

import json

SCHEMA_EXAMPLE = {
    "document_type": None,
    "order_number": None,
    "order_date": None,
    "customer_reference": None,
    "supplier_reference": None,
    "parties": {
        "buyer": {"name": None, "address": {}},
        "supplier": {"name": None, "address": {}},
        "ship_to": {"name": None, "address": {}},
        "bill_to": {"name": None, "address": {}},
    },
    "lines": [{"line_index": 0, "reference": None, "description": None, "quantity": None, "unit": None, "unit_price": None, "line_total": None}],
    "totals": {"net": None, "vat": None, "gross": None, "amount_due": None},
    "spatial": {"fields": [{"path": "order_number", "page": 1, "bbox": [0, 0, 0, 0]}], "zones": []},
}


def extraction_prompt() -> str:
    schema = json.dumps(SCHEMA_EXAMPLE, ensure_ascii=False, indent=2)
    return f"""You are evaluating document information extraction.
Extract only information explicitly visible in the provided purchase-order or business document image.
Return one JSON object only. Do not add commentary, Markdown or invented values.
Use null or omit a field when the document does not support it.
Preserve order numbers and material references exactly, including leading zeroes and punctuation.
Amounts must be numeric values or numeric strings without currency words.
For addresses, distinguish buyer, supplier, ship_to and bill_to roles. Do not copy one address into another role unless the document explicitly shows that role.
For line items, keep the document row order and do not merge unrelated rows.
If you can localize a field reliably, add an optional spatial.fields entry with bbox coordinates normalized to 0..1000 relative to that page. Otherwise omit the spatial bbox rather than guessing.

Target JSON shape:
{schema}
"""

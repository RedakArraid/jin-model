# V5.6 - Spatial Zone Intelligence

V5.6 changes the extraction order from **field-first** to **zone-first**.

Instead of accepting a token because it looks like an address component anywhere on the page, JIN now builds semantic page regions and constrains learned spans to compatible regions.

## Region model

Every region can expose four different boxes:

- `structural_bbox`: the physical container (vector frame/table frame when available);
- `content_bbox`: the union of matched useful content;
- `content_regions[]`: separate line/band boxes for the actual useful text;
- `search_bbox`: the compact operational area used to accept/reject learned fields.

This avoids using a large physical frame as the actual extraction search area.

## Boundary sources

Priority is content-first:

1. locate already-recognized components in the real PDF/OCR words;
2. build tight content bands;
3. snap those bands to the smallest relevant vector frame when one exists;
4. otherwise use a compact content-density fallback.

Supported sources include `VECTOR_BORDER`, `CONTENT_DENSITY`, `VECTOR_TABLE` and `TEXT_DENSITY`.

## Semantic regions

Current first-page regions:

- `SUPPLIER`
- `SHIP_TO`
- `BILL_TO`
- `UNKNOWN` address
- `ORDER_METADATA`
- `LINE_ITEMS`
- `TOTALS`

Address candidates and anchored fields receive a `zone_id`.

## Zone-constrained statistical extraction

When compatible regions exist:

- `ADDRESS_*` spans must fall inside an address-region `search_bbox`;
- `ORDER_*` spans must fall inside `ORDER_METADATA`;
- `TOTAL_*` spans must fall inside `TOTALS`.

This turns the flow into:

```text
PAGE
  -> structural/content segmentation
  -> semantic zone
  -> field acceptance inside that zone
  -> component bbox
```

rather than searching the whole page first.

## Sandbox spatial benchmark

Measured over **15 unique real PDFs / 39 detected address zones**:

| Metric | Before | V5.6 |
|---|---:|---:|
| median search area / useful content area | ~6.4x | **2.17x** |
| V5.6 mean | - | **2.00x** |
| V5.6 p90 | - | **2.41x** |
| V5.6 max | - | **3.53x** |

The median operational noise area is reduced by roughly **66%**.

WENDEL examples:

- supplier: 2.32x;
- ship-to: 2.28x;
- bill-to: 1.85x.

The bill-to matcher also uses both X and Y distance when a component such as `CS` occurs multiple times, preventing the region from stretching toward a footer occurrence.

These numbers measure **spatial compactness**, not universal field accuracy.

## API output

`POST /learning/field-route` and the `weak_field_suggestions` block in `/extract` now include:

```json
{
  "zone_intelligence_version": "spatial-zone-v1",
  "page_regions": [
    {
      "zone_id": "p1_ship_to_01",
      "zone_type": "SHIP_TO",
      "boundary_source": "VECTOR_BORDER",
      "structural_bbox": [470, 315, 930, 430],
      "content_bbox": [505, 330, 760, 405],
      "content_regions": [
        [505, 330, 710, 348],
        [505, 357, 760, 376],
        [505, 389, 690, 405]
      ],
      "search_bbox": [495, 322, 770, 413],
      "requires_review": true
    }
  ]
}
```

All coordinates are normalized to `[0,1000]`.

# V5.7 - Cell & Sub-Zone Intelligence

V5.7 refines the V5.6 semantic regions into columns, rows and cells.

## Why

V5.6 answers **where the business zone is**. V5.7 answers **which exact sub-zone/cell owns a value**.

The extraction chain is now:

```text
PAGE
  -> semantic region
  -> column / row / component cell
  -> learned span acceptance
  -> field value
```

## Line-item tables

For `LINE_ITEMS`, V5.7 detects semantic columns from headers such as:

- Article / Code art / Référence;
- Désignation / Description;
- Qté / Quantité;
- U / Unité;
- Prix net / P.A HT / PX Base / PU;
- Montant HT / Mnt Net;
- Délai;
- Vendu par.

Column boundaries use the strongest available source:

1. vector header cell;
2. vector grid line;
3. header alignment midpoint.

The runtime emits:

- `subzones[]` for semantic columns;
- `rows[]` with `data / continuation / note` classification;
- `cells[]` for each non-empty row/column intersection.

## Address cells

Inside V5.6 address zones, structured values are rematched to exact word boxes for:

`building`, `industrial_zone`, `house_number`, `house_number_suffix`,
`street_type`, `street_name`, `bp`, `postal_box`, `cs`, `tsa`,
`postal_code` and `city`.

Each cell receives a stable `cell_id`.

## Order metadata and totals

Anchored fields inside `ORDER_METADATA` and `TOTALS` also become cells. The original field receives its `cell_id`.

## Span constraints

When a compatible cell exists, learned spans are linked to it:

- `LINE_ITEM_REFERENCE` -> product/reference cells;
- `LINE_ITEM_DESCRIPTION` -> description;
- `LINE_ITEM_QUANTITY` -> quantity;
- `LINE_ITEM_UNIT_PRICE` -> unit price;
- `LINE_ITEM_TOTAL` -> line total;
- `ADDRESS_*` -> matching address component cell;
- `ORDER_*` -> order metadata cell;
- `TOTAL_*` -> totals cell.

All weak-supervised outputs remain `requires_review=true`.

## Sandbox benchmark

Current regression material: **15 unique PDFs**, 14 with native text.

Address-cell rematch coverage:

- **218 / 219 components (99.54%)** across 39 address zones;
- postal code: 39/39;
- street type: 38/38;
- street name: 38/38;
- house number: 37/37;
- city: 38/39;
- industrial zone: 14/14;
- CS: 11/11;
- building: 3/3.

This is **not human-reviewed spatial accuracy**. It measures whether already-structured V5.5.2 values can be localized precisely inside the V5.6 zone.

First-page line-item segmentation on the 14 native-text PDFs:

- tables detected: **13/14**;
- reconstructed data rows: **41**;
- non-header data cells: **309**;
- median semantic columns: **7**.

Boundary evidence observed:

- header alignment: 72 columns;
- vector grid: 5 columns;
- vector header cell: 12 columns.

## Spatial IoU evaluation

V5.7 adds `jin_runtime.spatial_metrics` and:

```bash
python training/annotations/evaluate_spatial_iou.py \
  --predictions prediction.json \
  --truth reviewed-spatial-annotations.json \
  --output spatial-metrics.json
```

Metrics include:

- IoU;
- truth coverage;
- predicted-box contamination;
- recall at IoU 0.50;
- recall at IoU 0.75;
- recall at IoU 0.90.

These metrics become authoritative once reviewed spatial ground truth is available.

## Example

```json
{
  "zone_id": "p1_line_items_01",
  "zone_type": "LINE_ITEMS",
  "subzones": [
    {
      "subzone_type": "COLUMN",
      "column_type": "quantity",
      "bbox": [590, 520, 680, 820]
    }
  ],
  "rows": [
    {
      "row_index": 0,
      "row_type": "data",
      "cells": [
        "p1_line_items_01_r000_product_code",
        "p1_line_items_01_r000_quantity",
        "p1_line_items_01_r000_unit_price"
      ]
    }
  ],
  "cells": [
    {
      "cell_id": "p1_line_items_01_r000_quantity",
      "cell_type": "quantity",
      "text": "2",
      "bbox": [610, 560, 640, 575],
      "requires_review": true
    }
  ]
}
```

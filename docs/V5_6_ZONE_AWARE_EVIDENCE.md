# V5.6 - Zone-Aware Evidence

V5.6 addresses a different failure mode from text recognition: a model can draw a very tight box around the wrong occurrence of a value.

Examples observed in real PDFs included monetary values such as `469,30` classified as postal codes, `EUR` classified as a city, contact text such as `RODAS DE` classified as a street, and footer addresses selected instead of the intended business address block.

## Zone first, field second

The runtime now separates three geometries:

1. **Exact value evidence** - the source word/token rectangle for each component.
2. **Business region** - the tight union of the exact components belonging to one address or anchored field.
3. **Semantic search zone** - the broader area in which a component is allowed to be associated with that role.

This distinction prevents a search window from being presented as if it were the exact value box.

## Address evidence

Each address candidate can expose:

```json
{
  "component_evidence": {
    "postal_code": {
      "value": "78300",
      "tokens": ["78300"],
      "bbox": [517, 188, 575, 200],
      "bbox_pdf_tl": [310.2, 150.4, 345.0, 160.0],
      "bbox_pdf_bl": [310.2, 640.0, 345.0, 649.6]
    }
  },
  "region_bbox": [ ... ],
  "search_zone_bbox": [ ... ]
}
```

The exact component box is the preferred geometry for highlighting a value in a UI.

## Coordinate spaces

V5.6 explicitly names its coordinate systems:

- `bbox`: normalized 0..1000, top-left origin;
- `bbox_pdf_tl`: PyMuPDF page points, top-left origin;
- `bbox_pdf_bl`: PDF-style points, bottom-left origin.

This removes ambiguity for renderers that previously interpreted normalized Y coordinates as bottom-left PDF coordinates.

## Weak-span gating

Weak token predictions are no longer published solely because their lexical classifier is confident. Address spans must be supported by compatible geometry evidence.

Rejected predictions remain available in `suppressed_spans` with an audit reason such as:

- `component_evidence_mismatch`;
- `component_not_supported_by_geometry`;
- `anchored_field_zone_mismatch`;
- `offer_not_order`.

The original model output also remains available in `raw_spans`.

## Regression evidence

Across 16 native-text regression executions:

- raw weak spans: **272**;
- zone-consistent spans kept: **217**;
- inconsistent spans suppressed: **55 (20.22%)**;
- address candidates: **46**;
- meaningful address components: **277**;
- components matched back to exact source boxes: **277/277**.

A scanned GARANKA check also matched **10/10** meaningful components across two address candidates after OCR.

These measurements are not a human-annotated IoU/mAP benchmark. They measure semantic consistency and exact source-word recovery on the regression set.

## Runtime bug fixed

The V5.5.2 runtime had a postal filter written as a literal-backslash regular expression. V5.6 uses a real five-digit pattern, so genuine postal-code token boxes are no longer rejected by that filter.

All weak suggestions remain `requires_review=true`.

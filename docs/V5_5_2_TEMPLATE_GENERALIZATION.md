# V5.5.2 - Template generalization

V5.5.2 extends the geometry/CPU learning stack beyond the ISERBA-style template.

## Sandbox regression

The test set now contains 17 real documents:

- the previous 9-order V5.5.1 regression set;
- 8 additional documents covering WENDEL, SISCA, GARANKA, ISERBA/GAZ SERVICE RAPIDE and an SFCP price offer.

The previous nine remain unchanged at 9/9 for their expected order/date/Total HT and supplier/ship-to/bill-to address checks.

On the eight added documents:

- 7 purchase orders expose the expected order number/date;
- 1 price offer exposes `offer_number` and `offer_date` instead of falsely becoming an order;
- all six documents with visible summary financial totals recover the expected net/VAT/gross fields;
- monetary values are not accepted as postal codes;
- contact names are not accepted as street types.

## Geometry V3

`visual-lines-v3` adds:

- generic header/value table matching for `N° Document`, `Pièce`, `N° Commande`, and `Date`;
- summary-table geometry for `NET H.T.`, `TOTAL H.T`, `VALEUR/MONTANT TVA`, `TOTAL/MONTANT TTC`, and `NET A PAYER`;
- role assignment by distance to address anchors instead of fixed left/right columns;
- house-number range normalization for `124 126`, `124,126`, and `123 - 125`;
- support for French postal forms such as `F-93711` / `FR-93711`;
- separate offer metadata;
- Tesseract sparse-text mode (`--psm 11`) for scanned forms/tables.

## Hierarchical PDF router V3

The document-level router now learns `P(decision | family)` from the **training split only**, with Laplace smoothing, and blends that prior with the existing decision classifier.

This fixes unstable ties such as CA documents predicted `remove` despite every one of the 411 labeled CA examples being `keep`.

Held-out 396-document validation:

- family accuracy: **99.75%**;
- raw decision accuracy: **93.43%**;
- decision accuracy with family prior: **94.19%**;
- prior-enhanced decision macro-F1: **89.76%**.

The prior weight is fixed to **1.0** and was not tuned on validation.

Artifact:

```text
jin-pdf-fusion-router-v3-cpu.joblib
SHA-256: 6ecee7438887929c41339e4c3ff7b7620ed17a16267ba60aabc8311f2e590c32
Size: 1,734,027 bytes
```

All weak field/address suggestions remain `requires_review=true`.

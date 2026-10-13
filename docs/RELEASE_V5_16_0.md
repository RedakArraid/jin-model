# JIN runtime 5.16.0 — scanned table reliability

This release fixes a scanned French purchase-order layout whose commercial
table uses `Code article`, `Libellé`, `Famille`, `Quantité`, `PU H.T.` and
`Prix total` headers.

`Libellé` is now a first-class description synonym, `Prix total` supplies the
line-amount anchor, and `Famille` is extracted separately as `product_family`.
The family text therefore no longer contaminates the product description.

The runtime also detects a recurrent scan-only OCR signature in which a narrow
euro sign is read as a trailing digit `1`, for example `8.55€` becoming
`8.551`. A value is repaired only when at least three suspicious values are
right-aligned on the same non-native page, one apparent value is repeated, and
the repaired product amount reconciles with quantity multiplied by unit price.
A lone legitimate three-decimal value is left untouched.

On order `CM-00407938`, JIN now returns article `87167717410`, description
`Flexible de manomètre`, family `Pièces détachées`, quantity `1`, unit price
and line total `8.55 EUR`. The explicit freight row is retained once at
`0.00 EUR`, and the document totals are normalized to `8.55 EUR`.

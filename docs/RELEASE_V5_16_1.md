# JIN runtime 5.16.1 — native table header guard

This patch fixes native purchase-order tables where a complete one-row header is
followed by a freight row containing a header-like word such as `FOURNISSEUR`.
Those data rows can no longer extend the header band and hide the actual product
lines.

For tables without a printed line-amount column, JIN now exposes a calculated
line amount only when both the printed quantity and unit price are available. The
calculation remains explicitly identified in the line warnings. French `PI` units
are normalized to `PCE`, and explicit freight rows remain separate typed charges.

On C.C.L. order `03 - 9260207221`, the expected result is product
`87167712610 — PRESSOS.`, quantity `1`, unit price and calculated amount
`75.48 EUR`, plus a `PORT FOURNISSEUR` shipping charge of `20.00 EUR`. Their sum
reconciles with the printed net total of `95.48 EUR`.

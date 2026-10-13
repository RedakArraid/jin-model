# JIN runtime 5.16.2 — stacked references and net prices

This patch hardens scanned purchase-order tables where each product uses two
physical rows: a customer article code plus a supplier material reference,
followed by the description and commercial values.

JIN now keeps the customer code in `customer_material_number`, promotes the
supplier's orderable reference to the primary article fields, and repairs an OCR
table-rule artifact only when the repeated reference column establishes a clear
dominant numeric shape. A separate `Prix net` column is no longer merged with
`P.U.`: calculations use the printed net unit price and preserve the list price
as `gross_unit_price`.

The legal-footer filter also uses token boundaries, preventing `APE` from
matching inside product names such as `SOUPAPE`.

On VERNEY order `CF0315701`, all nine printed products are retained. Their net
amounts total `743.79 EUR`, which is within the document's printed rounding of
`743.84 EUR` and passes the configured reconciliation tolerance.

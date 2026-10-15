# JIN runtime 5.16.9 — requested delivery dates and receipt-date lines

JIN now resolves requested delivery dates as first-class order data. Explicit
labels such as `Date de livraison souhaitée`, `Date de livraison demandée`,
`Date de livraison estimée`, `À livrer le`, `Requested delivery date` and
`Liefertermin` are recognized from their page geometry. Only a date following
the label is eligible, which prevents a purchase-order date printed earlier on
the same row from being reused accidentally.

Line-level receipt dates remain attached to their individual order lines. When
all dated lines agree, JIN also promotes that value to the order's requested
delivery date. Mixed line dates deliberately remain line-level only.

The line parser also supports ERP tables with:

- separate supplier and customer article-reference columns;
- a header split over two physical rows;
- a requested receipt date on each product row;
- gross, net and amount columns;
- net prices using three or four decimal places;
- line numbers and descriptions printed below the numeric row.

Candidate lines must repeat and satisfy quantity × net unit price = amount.
This keeps the recovery narrowly grounded in printed structure.

Campaign tooling now retains order and line delivery dates and produces a
dedicated evidence audit (`delivery_date_audit.json`, CSV, review bundle and
summary). The web UI displays the order requested date and the per-line date.

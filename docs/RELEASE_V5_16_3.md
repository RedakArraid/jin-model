# JIN runtime 5.16.3 — complete line hypotheses

This patch hardens purchase orders for which several line parsers compete. A
small parser result can be internally clean while still omitting most of the
printed table. JIN now uses candidate-set coverage and reconciliation with an
explicit printed net total when ranking already detected hypotheses.

For tables containing both `Code art` and `Reference`, the customer article
code is retained in `customer_material_number`, while the supplier reference is
used as the primary material/article identifier. Reference extraction is also
restricted to the code-shaped token nearest the printed reference header, so
the first words of the description cannot be appended to it.

On Wendel order `CF332267`, JIN now extracts all 19 printed product rows instead
of selecting a two-row subset. Their amounts total exactly `10,151.54 EUR`, the
selected parser is `geometry`, the business validation is `PASS`, and all 19
supplier references remain free of description text.

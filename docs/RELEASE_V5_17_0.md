# JIN runtime 5.17.0 — financial and party-role guards

This release hardens purchase orders whose table or header layout creates a
plausible but materially wrong extraction.

French ERP unit prices printed with four to six decimal places, for example
`620,0000`, are now interpreted through their exact source token. A correction
is applied only when the generic interpretation reproduces JIN's inflated
value and differs from the native decimal value by at least three orders of
magnitude. Line amounts are recalculated only when JIN had derived them from
quantity and price and no distinct printed amount exists. If an arithmetic
line still fails the printed document total, its confidence is capped and it
is marked for review.

The order header receives two additional semantic guards:

- an explicitly printed order date can replace a duplicated delivery date;
- on a purchase order, an explicit billing party is promoted to buyer and the
  centred addressee to supplier only when the current supplier is unusable and
  all layout signals agree.

An internal order number identical to any line article reference is removed.
Delivery-address reconciliation also retains concise, clean site complements
such as `ALBANORD`, but filters product references, prices, postal fragments,
legal identifiers and shipping text. Finally, slash-separated offer numbers
following one explicit offer label are kept as separate commercial references.

Regression coverage includes safe printed-amount preservation, date
disambiguation, party-role repair, confidence downgrade, internal-reference
collision, address-complement filtering and slash-separated offers.

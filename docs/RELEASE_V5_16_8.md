# JIN runtime 5.16.8 — customer registration roles

French registrations printed without an explicit party label are now assigned
using the semantics of a purchase order: the document is normally issued by
the customer, while JIN is the supplier receiving it. This customer default is
used only after stronger evidence has been considered.

The resolver applies the following hierarchy:

1. an explicit buyer or supplier label on the identifier line;
2. an exact or strong name match with customer parties (`buyer`, `bill_to` or
   `ship_to`);
3. a preceding party heading;
4. the customer-issuer default for purchase orders;
5. `unknown` for non-orders without evidence.

A supplier-name match without an explicit supplier label is retained as weak
support but cannot by itself overrule the customer-issuer default. This guards
against dense layouts where the core party parser duplicates the customer's
legal entity into the supplier slot.

Headings that begin after the registration are never allowed to own it. For
example, a customer SIRET immediately followed by `INFOS FOURNISSEUR` remains
attached to the customer.

Every tax identifier now exposes `role`, `party_role`, `role_confidence` and a
traceable `role_assignment`. Campaign exports retain the same fields, and a
calculated VAT can enrich the matched billing party instead of being attached
blindly to the first buyer record.

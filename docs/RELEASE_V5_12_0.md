# JIN 5.12.0 - Commercial metadata

This release separates three kinds of commercial information that must not be
confused with product lines: quotation references, derogation references and
additional charges.

An order can now carry several quotation or derogation numbers. When the PDF
prints a reference in a product-table column, JIN links it to the matching line
using the material reference, quantity and amount. Repeated material references
are therefore disambiguated without relying on row position alone. A reference
found only in a document header remains document-scoped and is never assigned to
an arbitrary product.

French VAT enrichment now also uses explicitly labelled SIREN and SIRET values.
The registration number must have the exact expected length and pass its Luhn
checksum. If VAT is absent, the computed number is returned with a
`DERIVED_FROM_VALID_SIREN` or `DERIVED_FROM_VALID_SIRET` status and a warning
that it was not printed. If VAT is printed, a matching registration identifier
raises the validation status to `CHECKSUM_AND_REGISTRATION_MATCH`.

Finally, a universal post-processing pass moves explicit freight, transport,
delivery, tax, environmental-fee and surcharge rows from `lines` to
`additional_charges`. The original description, reference, amount, page and
evidence are preserved. Ordinary products containing ambiguous words are not
reclassified unless the row is explicitly charge-like.

The clean JSON exposes:

- `order.quote_references` and `order.derogation_references`;
- plural `quote_numbers` and `derogation_numbers` on each affected line;
- line/material links and source evidence for every commercial reference;
- typed `additional_charges` beside product lines;
- VAT validation provenance through `document.tax_identifiers` and
  `order.tax_identifiers`.

The frontend mirrors the same separation. Quotation and derogation references
show their document or line scope, affected line and material numbers, confidence
and first source. VAT cards distinguish printed values from values derived from a
valid SIREN/SIRET. Product rows and additional charges use separate tables, and
the interface offers both the compact normalized JSON and the complete audited
payload as downloads.

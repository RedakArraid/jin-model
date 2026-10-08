# JIN 5.11.1 - Client material-reference prefixes

Some customers qualify supplier references with a customer-side source code. In
the audited GARANKA documents, this appears as `EL <reference>` in the article
column. The complete printed value remains in `material_number` for source
traceability, while `supplier_material_number` now contains the clean reference
without `EL`.

The implementation is not tied to GARANKA's layout. Configured prefixes support
space, `+`, `-`, `/`, `:` and contiguous notation. A previously unknown prefix can
also be confirmed within a document when at least two distinct commercial lines
contain suffixes strongly supported by the aggregate material profile. Short
numeric references are accepted only behind an explicitly configured prefix.

Safety remains conservative:

- the suffix must start with a digit and match the aggregate reference profile;
- a new prefix requires repeated evidence from distinct product references;
- VAT, telephone, company, postal and order-number prefixes are excluded from
  automatic discovery;
- words such as `ELECTRODE` cannot be split as an `EL` reference;
- the original source value is never lost.

The campaign review found 67 unique GARANKA documents, 493 `EL` occurrences and
220 distinct suffixes. Of those distinct suffixes, 211 were present in the source
master data (95.91%); the remaining shapes were handled by the aggregate profile
rather than an exact, rapidly obsolete lookup list.

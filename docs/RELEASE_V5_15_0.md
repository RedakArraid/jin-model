# JIN runtime 5.15.0 — calibrated article-reference confidence

This release calibrates each extracted order line with a privacy-safe aggregate
profile of material-reference shapes. The runtime does not contain or query the
exact article list.

The profile records aggregate length, character nature, run signature and
prefix frequencies. In the current master sample, numeric references account
for 34,443 of 34,671 unique references and 10–11 character references account
for 31,903. These dominant shapes may corroborate an already detected product
row, but can never create a row or replace its source value.

Each product line now exposes `reference_confidence` and `reference_status` in
the clean output. A compact, supported shape is marked
`SUPPORTED_DOMINANT_NUMERIC` or `SUPPORTED_PROFILE`. Rare compact shapes remain
available as `ATYPICAL` with capped confidence. Missing references are also
capped. A reference made only of product wording, or one that captures a full
description/row, is marked `ALPHA_ONLY` or `TEXT_CAPTURE`, receives a strong
confidence cap, and forces review.

Grouped source notations such as `8-703-406-153-0`, short suffix qualifiers
such as `/LM`, and harmless trailing separator noise are compared through a
canonical form while the original document value is preserved. A missing
reference may also be recovered from the numeric prefix of a description, but
only when the row already contains a quantity and price/amount and the 10/11
digit candidate is strongly supported by the aggregate profile.

The final 100-document regression sample produced 287 product lines: 268
dominant numeric references, 6 supported alphanumeric profiles and 13 retained
atypical references. It contained no missing reference, no text capture and no
alpha-only reference; five grouped references were safely recovered from row
descriptions. All 100 API extractions completed successfully.

Telephone numbers, dates, VAT numbers and order numbers are still rejected by
the existing prefix/signature guards even when their length is common. The
shape score remains bounded corroborating evidence and never increases the
line confidence above its source/layout confidence.

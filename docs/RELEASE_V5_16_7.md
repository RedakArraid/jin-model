# JIN runtime 5.16.7 — trusted derived VAT

JIN now treats a French VAT number calculated from a validated SIREN or SIRET
as a first-class tax identifier. The derivation is allowed only when the source
registration has the exact French length and both the SIREN and SIRET checksums
(when applicable) are valid.

The calculated identifier retains the registration number, party role, source
evidence, confidence and deterministic formula. Its validation status remains
explicit (`DERIVED_FROM_VALID_SIREN` or `DERIVED_FROM_VALID_SIRET`), so the UI
can distinguish calculation from verbatim extraction without lowering its
business validity.

The audit pipeline now validates that provenance instead of searching for the
calculated VAT string in the PDF. It reports
`CALCULEE_DEPUIS_SIREN_VALIDE` or `CALCULEE_DEPUIS_SIRET_VALIDE` and no longer
adds `TVA_NON_RETROUVEE_DANS_SOURCE`. When OCR produces a malformed printed VAT
beside a valid registration, the checksum-valid calculated value wins.

Structured buyer and supplier registrations are also used when PDF line
segmentation separated the label from its digits and the generic page-text
matcher could not recover the identifier directly.

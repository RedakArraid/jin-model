# JIN runtime 5.16.6 — contextual OCR normalization

This release hardens scanned order headers, labelled party codes, postal routing
components, department labels, and numeric product dimensions. Corrections are
restricted to explicit structural contexts: independent crop readings must agree
for order identifiers, party-code substitutions apply only to a trailing numeric
mask, and `Z` becomes `2` only inside a numeric dimension.

Regression coverage includes the two VERNEY purchase orders `CF0315792` and
`CF0315701`, including `EL001`, `BP 77605 / 21076 DIJON CEDEX`, a clean
`Dépôt Dijon` department, and `15X21` dimension output.

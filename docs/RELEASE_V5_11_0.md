# JIN 5.11.0 - Material-pattern line extraction

This release uses an aggregate profile derived from `10564_Materials.csv` to
support purchase-order line extraction on CPU.

The runtime does not contain the source master-data rows, descriptions or exact
article list. The 8 KB profile contains only reference lengths, character-shape
counts and prefix counts with a minimum support. Consequently, newly created
references with the same stable families can be recognized without rebuilding
an exact lookup table.

The profile is corroborating evidence only:

- it cannot create a product line without table structure, a description and a
  commercial value;
- it never rewrites a reference read from the document;
- short numeric values are excluded because they collide with postal codes,
  SIRET suffixes and commercial line numbers;
- dates, VAT identifiers, telephone numbers and separated order numbers do not
  receive a material-reference bonus.

It is used to rank competing table parsers, type an already extracted supplier
reference, and reconstruct split rows where the customer code/description and
quantity are followed by a standalone supplier material reference. A following
environmental fee remains an additional charge instead of being merged into the
product price. A second fallback recovers degraded OCR rows only when a recognized
material reference, a quantity/unit cell, an adjacent description and an arithmetic
price/amount pair agree. Partial fallbacks cannot replace a better-covered existing
table extraction.

Offline evaluation on 1,298 unique campaign documents found 1,445 distinct
master references in source text. The aggregate profile supported 99.03% of
those observed references. It also detected current references absent from the
input CSV; manual context inspection confirmed that the most frequent such
values occurred in product rows. These are coverage measurements, not reviewed
ground-truth accuracy.

The focused line regression suite contains 55 passing tests. On 11 representative
real PDFs, eight line outputs remained unchanged and three known failures were
corrected: a split PPC fee row, an OCR-degraded thermostat line and a delivery date
misclassified as a product.

Regenerate the aggregate profile after a structural master-data change with:

```powershell
python scripts/build_material_reference_profile.py `
  C:\path\to\10564_Materials.csv `
  core_overrides\config\material_reference_profile_v1.json
```

# JIN runtime 5.16.5 — semantic OCR table recovery

This release closes the zero-line extraction failures found in the 150-document ESKER campaign.

The new fallback works from OCR text only when a semantic order-line header is present. It supports:

- quantity, unit, description, supplier reference and unit-price rows without a printed line amount;
- conventional rows with separate customer and supplier article references;
- ERP layouts where description/quantity and code/price/amount are printed on two physical rows;
- `NBR`/`UAC` tables with repeated quantity fields;
- freight and environmental contributions as typed additional charges.

Every product with a printed amount must satisfy quantity × unit price = amount within a configurable tolerance. The fallback therefore increases coverage without relaxing the existing rejection guards for totals, legal footers and unrelated numeric text.

When both an internal/customer code and a supplier reference are printed, the supplier reference becomes `material_number`, `article_number` and `supplier_material_number`; the internal code is preserved in `customer_material_number`.

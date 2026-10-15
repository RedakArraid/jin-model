# Changelog

## 5.16.8-customer-registration-role

- Attribute unqualified SIREN/SIRET identifiers on purchase orders to the customer issuer when no stronger supplier evidence exists.
- Match registration context against buyer, billing, shipping and supplier party names before applying the purchase-order issuer fallback.
- Prevent a following `INFOS FOURNISSEUR` block from stealing the customer SIRET printed immediately before that heading.
- Preserve the role, matched party role, role confidence and assignment method through clean output and campaign audit reports.
- Attach calculated VAT and registration identifiers to the matched customer party, including `bill_to`, while explicit supplier identity remains authoritative.

## 5.16.7-trusted-derived-vat

- Treat a French VAT number deterministically calculated from a checksum-valid SIREN or SIRET as a trusted tax identifier, with the same confidence as its validated source registration.
- Reuse checksum-valid buyer/supplier registration identifiers already resolved by the structured extractor when page text layout prevents the generic registration regex from seeing them.
- Preserve calculation provenance, source registration, validation status and confidence through batch audit reports.
- Prefer a correct calculated VAT over a malformed OCR VAT candidate, while keeping checksum-valid printed and calculated VAT values on the same trust tier.
- Stop source-audit finalization from flagging calculated VAT values as missing merely because the calculated value is not printed verbatim in the PDF.

## 5.16.6-contextual-ocr-normalization

- Re-read compact scanned order headers with two independent OCR layouts and full-glyph padding, correcting ambiguous series characters only when both readings agree.
- Repair digit-like OCR characters in explicitly labelled supplier/customer codes through a constrained alphabetic-prefix plus numeric-suffix mask.
- Separate postal routing or PO-box numbers from the actual postal code and align clean party addresses with their normalized role-specific address records.
- Remove adjacent currency/form metadata from department names and repair `Z`/`2` OCR confusion only inside numeric product dimensions.
- Add regression coverage for the two supplied VERNEY orders and the generalized normalization guards.

## 5.16.5-semantic-text-table-recovery

- Recover scanned order lines when OCR preserves row order but table geometry is unreliable, using semantic headers and strict quantity × unit-price = amount validation.
- Support split two-row ERP tables, quantity-first tables without line amounts, conventional dual-reference rows, and `NBR`/`UAC` purchasing tables.
- Promote explicit supplier references to the primary article fields and retain printed internal codes as customer material references.
- Separate freight and environmental contributions from product lines, including per-unit environmental fees linked to their parent products.
- Add regression coverage for the four structures behind the nine zero-line failures found in the 150-document ESKER campaign.

## 5.16.4-stacked-dual-reference-charges

- Parse ERP tables that stack several logical order lines in one physical row while exposing separate customer `RÃ©fÃ©rence` and supplier `Ref Four` columns.
- Keep the buyer's internal code in `customer_material_number` and promote the printed supplier reference to the primary material/article fields.
- Align missing supplier-reference cells semantically so a freight row cannot shift the supplier reference onto the wrong product.
- Remove document-routing preambles from stacked descriptions while preserving wrapped product descriptions.
- Recognize `PORT/ACHATS` as shipping and compact environmental-fee codes such as `DEEE7.69` as typed additional charges.
- Add regression coverage for both stacked dual-reference variants, `PORTA`, and compact DEEE rows.

## 5.16.3-complete-line-hypotheses

- Rank competing line-table parsers with document-level evidence: candidate coverage and reconciliation with an explicitly printed net total.
- Prevent a small set of individually clean rows from outranking a complete, arithmetically valid product table.
- In explicit `Code art` plus `Reference` layouts, retain the customer code in `customer_material_number` and promote the supplier reference to the primary article fields before scoring.
- Keep early description tokens out of supplier references even when the physical description starts to the left of its printed column header.
- Add regression coverage for dual-reference tables and the supplied Wendel order `CF332267`, whose 19 product lines reconcile exactly to `10,151.54 EUR`.

## 5.16.2-stacked-reference-net-price

- Preserve product words such as `SOUPAPE` by matching legal-footer acronyms (`APE`, `NAF`, etc.) only as complete tokens.
- Recognize a distinct `Prix net` / `Net price` column beside `P.U.` and calculate line amounts from the printed net unit price while retaining the gross/list price.
- Type repeated references printed in the designation area as supplier material references and retain short left-column article codes as customer material references.
- Repair leading OCR table-rule artifacts (`|871...` or `1871...`) only when a repeated numeric reference column establishes a dominant length and prefix.
- Stop multiline descriptions at standalone attention/footer rows and expose `gross_unit_price` in the clean JSON output.
- Add regression coverage for the supplied VERNEY order `CF0315701` layout.

## 5.16.1-native-table-header-guard

- Prevent complete one-row table headers from swallowing product and freight rows whose descriptions contain header-like words such as `FOURNISSEUR`.
- Calculate line amounts transparently from printed quantity and unit price when the document has no line-amount column.
- Normalize the French unit abbreviation `PI` to `PCE` in generic table extraction.
- Preserve `PORT FOURNISSEUR` as a typed shipping charge and reconcile product plus freight with the printed net total.
- Add regression coverage for the supplied C.C.L. order `03 - 9260207221` layout.

## 5.16.0-scanned-table-reliability

- Recognize `Libellé` as a description header and `Prix total` as the line-amount column in scanned French purchase orders.
- Extract `Famille` independently as `product_family` instead of appending it to the product description.
- Repair the recurrent scan-only OCR substitution where a right-aligned euro sign becomes a trailing `1` (`8.55€` -> `8.551`), only when repeated page geometry and line arithmetic corroborate the correction.
- Preserve explicit zero-value shipping rows while preventing fragmented `FRAIS DE PORT` labels from becoming duplicate surcharge records.
- Add regression coverage for the supplied IZI by EDF order layout and expose `product_family` in the clean JSON contract.

## 5.12.0-commercial-metadata

- Preserve several customer quote references per order and per product line, with explicit source-backed links between each quote and the affected lines.
- Extract derogation numbers at document and line level without assigning document-only references to arbitrary products.
- Recover quote and derogation references from structured PDF table columns, including repeated material references disambiguated by quantity and amount.
- Derive a French VAT number from an explicitly labelled, checksum-valid SIREN or SIRET when no printed VAT number is present, while marking the result as derived rather than printed.
- Cross-check printed French VAT numbers against valid SIREN/SIRET evidence and retain the supporting registration identifier.
- Move explicit freight, shipping, environmental-fee, tax and surcharge rows out of product lines into typed additional charges, preserving amounts and source evidence.
- Expose plural commercial references and their line associations in the clean JSON and UI.
- Refresh the complete frontend with dedicated commercial-reference and tax-identifier views, explicit derived-VAT provenance, separate product/charge tables, source-aware overlays and clean/full JSON downloads.

## 5.11.1-client-reference-prefixes

- Separate configured customer source qualifiers such as GARANKA `EL` from the canonical supplier material reference while retaining the complete printed value for traceability.
- Support space, `+`, `-`, `/`, `:` and contiguous prefix notation without weakening ordinary numeric-reference safeguards.
- Confirm previously unknown prefixes only when at least two distinct commercial lines have strongly supported material-reference suffixes in the same document.
- Keep VAT, telephone, company and postal prefixes blocked from automatic discovery.
- Add prefix counts to material-pattern diagnostics and cover short numeric and alphanumeric GARANKA references in regression tests.

## 5.10.0-header-reliability

- Audited the complete SHA-256-deduplicated 2,925-document order archive through iterative unseen discovery and validation batches.
- Hardened source-backed customer order-number extraction across C.C.L., PPC/PARTEDIS, SISCA and segmented or table-based layouts without dropping separators or leading zeroes.
- Added generic numeric and textual customer-agency-code recovery, including wrapped explicit agency mailboxes, while keeping business identifiers out of postal labels.
- Reconciled buyer, supplier, delivery party and contact roles across multi-column and interleaved PDF text layers.
- Cleaned delivery labels by separating VAT text, industrial zones, BP/CS/TSA routing, payment terms and contact data from postal components.
- Added compact, consumer-oriented JSON normalization and UI-safe party/address output with source evidence, scores and explicit review warnings.
- Added reproducible campaign tooling, source-support audits, selective replay and recoverable PDF deduplication.
- Final unseen batch: 48/48 customer order numbers source-supported, 47/47 explicit delivery addresses source-supported, one non-explicit address intentionally left blank.

## 5.9.2-segmented-order-number

- Preserve complete segmented customer order numbers such as `02 - 9260205710` instead of retaining only the long numeric segment.
- Reconstruct a scan-dropped hyphen only when the C.C.L. document family and both aligned numeric segments are corroborated.
- Keep the raw OCR value and extraction method in evidence whenever a separator is reconstructed.
- Add regression guards so unrelated unseparated numeric groups are never reformatted as segmented order numbers.

## 5.9.1-customer-agency-code

- Added the source-backed `customer_agency_code` field to the verbose and clean JSON outputs.
- Recognize explicitly labelled agency/site/branch codes across customers without a customer-specific dictionary.
- Corroborate unlabelled short codes between the order header and the delivery block before promoting them.
- Keep agency codes available as business metadata while excluding them from the clean postal label.
- Display the separated customer agency code in the UI summary and delivery-address card.

## 5.9.0-audit-hardening

- Audited a third reproducible, SHA-256-deduplicated random batch of 50 production PDFs.
- Preserved complete compound customer references under explicit `Référence Commande` columns.
- Restored street numbers followed by commas and multi-number forms such as `3 et 5`.
- Prevented BP/CS/TSA routing numbers from being interpreted as postal codes.
- Preserved source street types when BAN canonical data disagrees with the printed document.
- Retained Techniparc and parenthesized delivery-site complements while removing delivery instructions.
- Cleaned merged `Contact Code` column headers and the recurrent `D4OZENAY` apostrophe glyph error.
- Added a conservative non-order route for acknowledgement reminders and rejected `commande à distance` as an order number.

## 5.5.2-template-generalization

- Generalized visual-line extraction across WENDEL, SISCA, GARANKA, ISERBA/GAZ SERVICE RAPIDE and SFCP offer layouts.
- Added table header/value matching for N° Document, Pièce, N° Commande and Date.
- Added summary financial anchors for NET/TOTAL HT, VAT, TTC and amount due.
- Replaced fixed left/right address-role assumptions with proximity-based role anchor bands.
- Normalized house-number ranges such as 124 126, 124,126 and 123 - 125.
- Added F-/FR-prefixed French postal-code support.
- Separated offer_number/offer_date from order metadata.
- Switched scan fallback to Tesseract PSM 11 for sparse forms/tables.
- Added learned P(decision|family) priors to the CPU PDF router; holdout decision accuracy improves from 93.43% to 94.19%.
- Added 17-document multi-template regression benchmark; prior 9-document regression remains stable.

## 5.5.1-geometry-hardening

- Reconstruct visual lines across PDF block boundaries using word Y-coordinates.
- Recover order number, order date and Total HT from explicit visual anchors.
- Join postal code/city across adjacent internal PDF blocks.
- Add role-aware supplier/ship_to/bill_to geometry address candidates.
- Attach ZI/ZA/ZAC/ZAE, building and BP/CS/TSA components to address candidates.
- Enforce strict five-digit raw postal validation and a street-type whitelist.
- Eliminate the nine-order regression false positives where monetary amounts became postal codes and `RODAS` became a street.
- Harden future weak-label training so punctuation-stripped money cannot become a postal-code label.
- Nine supplied-order regression: 9/9 on command number, date, Total HT, supplier/delivery/billing core address components; 0 monetary-CP false positives.

## 5.5.0-weak-field-learning

- Added reproducible CPU weak-supervised token/field learning on real first-page PDF words and geometry.
- Training: 1,468 native-text train PDFs; holdout scoring: 372 native-text validation PDFs.
- Weak-label agreement: 98.97% token accuracy and 97.82% macro-F1.
- Added OCR fallback for image-only PDFs at inference time (Tesseract fra+eng+deu).
- Added spatial guardrails for multi-column documents and structured review-required address candidates.
- Added `POST /learning/field-route` and non-destructive `weak_field_suggestions` enrichment in `/extract`.
- Explicitly keeps all weak-field outputs `requires_review=true`; no core field is overwritten.
- Artifact: `jin-field-weak-router-v2-cpu.joblib`, SHA-256 `1eae5cfe609c286d777a7ccc83dbaf233a917a989cf1408b681abaeedf467517`.

## 5.4.0-real-pdf-cpu-learning

- Validated and trained on 1,968 real PDFs / 2,877 pages: 1,572 train and 396 untouched validation documents.
- Verified zero Git LFS pointers and zero exact SHA-256 duplicates across train/validation.
- Added CPU-only multimodal training from native PDF text, word/page geometry and first-page low-resolution vision.
- Added calibrated family classifier: 99.75% family accuracy on the 396-document holdout.
- Kept the stronger raw decision classifier: 93.43% keep/review/remove accuracy.
- Added `CpuPdfRouter`, `POST /learning/pdf-route`, health status and automatic `document_pdf_router` enrichment on PDF extraction when the model is mounted.
- Kept V5.3 output-quality repairs, including duplicated-city repair in `formatted_address`.
- Documented that field-level extraction learning still requires reviewed field/region/token labels; GPU compute is not the blocker.
- Added a weak-label bootstrap that projects corrected JIN JSON values onto native PDF word boxes and marks every generated region as review-required.
- Added train-only temperature scaling (T=7.0) for decision confidence without changing keep/review/remove classes.

## 5.3.0-corpus-learning-output-quality

- Trained a compact statistical document router from the supplied 1,968-document corpus evidence with the 396 validation documents held out.
- Added family and keep/review/remove predictions without overriding the core engine.
- Added structured `formatted_address` repair for duplicated city output.
- Added JSON-wide output-quality auditing for address verification, coordinates, lines and totals.
- Added corpus training and output-quality documentation.
- Confirmed the uploaded PDF archive contains Git LFS pointers only; layout/vision training remains gated on materialized PDF objects.

## 5.2.0-statistical-learning

- Couche runtime versionnée au-dessus du moteur JIN empaqueté.
- Mémoire statistique supervisée à partir des corrections historiques.
- Classifieurs appris pour rôles et composants d'adresse avec garde-fous.
- Endpoints `POST /feedback` et `GET /learning/status`.
- Enrichissement de `/health` et `/extract`.
- Vérification officielle d'adresse maintenue séparée de l'inférence statistique.
- Politique Git LFS pour PDF/images du corpus.
- Contrôle des pointeurs LFS non matérialisés.
- Préparation OCR/layout des pages PDF complètes.
- Pipeline de fine-tuning LayoutLMv3 pour champs et cellules de tableaux.
- CI runtime et workflow manuel d'entraînement layout/vision.
- `prepare-model.sh` généralisé aux archives moteur V4.8/V5.x contenant `uda/`.

## 4.8.0

- Docker Compose et interface web autour du moteur Universal Document AI empaqueté.

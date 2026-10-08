# Changelog

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

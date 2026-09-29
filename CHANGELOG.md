# Changelog

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

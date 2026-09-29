# V5.4 - Real PDF training

The real corpus has now been validated and used for training.

## Corpus integrity

- 1,572 real training PDFs
- 396 real validation PDFs
- 1,968 total PDFs
- 2,877 total pages
- 0 Git LFS pointers
- 0 non-PDF files
- 0 SHA-256 duplicates crossing train/validation
- 1,864 documents with native PDF text
- 128 scanned/image-only documents

Seven filenames exported Unicode characters as `#Uxxxx`; label matching therefore decodes this escape form and/or uses SHA-256 rather than trusting filenames.

## Real PDF models trained

Three baselines were trained from the actual PDFs, without filename or email-subject features.

| Model | Family accuracy | Family macro-F1 | keep/review/remove accuracy | Decision macro-F1 |
|---|---:|---:|---:|---:|
| Text + PDF geometry | 93.69% | 89.51% | 89.14% | 73.75% |
| First-page vision + geometry | **99.49%** | 98.92% | 92.93% | **88.87%** |
| Text + vision + geometry fusion V2 CPU | **99.75%** | **99.30%** | **93.43%** | 88.12% |

The visual model uses the actual first page rendered from each PDF plus page/word geometry. The production fusion V2 adds native PDF text where available and calibrates the family classifier on training-only cross-validation. It remains fully CPU-only.

The 396 validation PDFs were never used to fit model weights.

### Decision confidence calibration

The raw decision classifier was accurate but over-confident on some mistakes. A temperature is therefore selected **only from 3-fold out-of-fold predictions on the 1,572 training PDFs**. The selected temperature is **7.0**. It does not change `keep/review/remove` classes; it only calibrates their reported confidence.

Final holdout log loss:

- family: **0.0865**;
- decision after temperature scaling: **0.2401**.

A full runtime replay on all 396 validation PDF binaries reproduced the same classes in about **18.3 seconds** in the sandbox.

Final CPU artifact:

```text
jin-pdf-fusion-router-v2-cpu.joblib
SHA-256: c225df1455376f2af32d7c953c440f5424b8481ee7e7989e221f24b98150da62
Size: 1,733,706 bytes
```

## What is still missing for field extraction learning

The corpus metadata contains document-family and keep/review/remove labels, but it does **not** contain region/token labels for:

- address components;
- order number/date;
- supplier/buyer/ship-to/bill-to fields;
- line-item columns/cells;
- charges/taxes/totals.

Therefore a LayoutLMv3 token-classification model cannot honestly be trained for field extraction from these PDFs yet without generating and reviewing field-level annotations.

A document-classification LayoutLMv3 pipeline is prepared and can use the existing family labels. For field extraction, annotations must be added under `corpus/annotations/`.

## Address/output quality

V5.3 output-quality checks remain active. In particular, duplicated city values in `formatted_address` are rebuilt from structured components and the original value is kept in `formatted_address_original`.

## Compute limitation

The production path is now explicitly CPU-only. The V2 model was trained completely in this sandbox and requires no GPU for training or inference. LayoutLMv3 remains optional research code, not a deployment dependency.


## Runtime integration

Place the trained artifact at:

```text
data/learning/jin-pdf-fusion-router-v2-cpu.joblib
```

The Docker runtime loads it through `JIN_PDF_ROUTER_MODEL`. `POST /learning/pdf-route` predicts directly from an uploaded PDF, and `/extract` adds `document_pdf_router` when the model is mounted.

## Remaining supervised-data gap

The remaining limitation is not compute. It is label coverage. The current corpus has document-level labels, but no reviewed bounding boxes/token labels for address components, order fields, line-item cells or totals. Those labels are required before a CPU token/region extractor can be trained honestly for field extraction.


## Bootstrapping field annotations

V5.4 includes `training/annotations/bootstrap_from_extractions.py`. Given corrected JIN JSON outputs and their real PDFs, it searches the native PDF words for the structured values and creates normalized `[0,1000]` regions for addresses, order fields, line items and totals.

The bootstrap explicitly emits `requires_review=true` and reports unmatched or ambiguous values. This creates a review queue rather than silently turning model output into ground truth.

Once reviewed, these annotations can feed the existing token/layout dataset preparation pipeline or a future CPU region/token extractor.

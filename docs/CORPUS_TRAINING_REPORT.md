# Corpus training report - 1,968 purchase-order PDFs

The supplied corpus contains 1,968 Git LFS pointer files: 1,572 in `test` and 396 in `validation`. The archive also contains `dataset_manifest.csv`, `metadata_classified.csv` and `document_type_audit.csv`, including subject/sender metadata and a 240-character text preview previously extracted from each document.

Because the uploaded ZIP contains LFS pointers rather than PDF binaries, pixel/layout fine-tuning cannot be honestly run from this archive alone. The text-level statistical router can nevertheless be trained from the existing extracted evidence from the same document set.

## Holdout results

Training uses only the 1,572 `test` documents. The 396 `validation` documents stay out of training.

Compact HashingVectorizer + SGD logistic models (`2^10` features):

- document family accuracy: **98.99%** on 396 validation documents;
- keep/review/remove accuracy: **98.48%** on 396 validation documents.

A larger `2^13` feature configuration reaches 99.49% family accuracy and 98.99% decision accuracy; the compact model is the deployable default because it is small enough to mount locally while preserving strong validation performance.

## Corpus characteristics visible in previews

- CEDEX appears in 539 document previews;
- BP appears in 93;
- CS appears in 204;
- ZI/ZAC/ZAE-like zones appear in 89;
- street-type evidence appears in 1,119;
- 128 documents have no text preview in the audit CSV.

These distributions reinforce the need to keep BP/CS/CEDEX/zones as distinct address components rather than flattening them into `street_name` or `city`.

## Layout/vision blocker

All 1,968 PDF files in the uploaded archive are Git LFS pointer text files (~129-132 bytes). The actual PDF objects are required for rendering/OCR/layout training. Once the source LFS remote is available, `scripts/materialize-lfs-corpus.sh` and the LayoutLMv3 pipeline can operate on the real pages.

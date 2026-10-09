# Open-weight information-extraction benchmark

This benchmark compares the current JIN runtime against open-weight document/VLM baselines on the **same PDFs, same target schema and same reviewed ground truth**.

## Tracks

### Direct information extraction

These models receive the same rendered document image and the same JSON extraction prompt:

- `jin` — current repository runtime (V5.12 at benchmark creation time);
- `granite_docling_258m` — `ibm-granite/granite-docling-258M`;
- `glm_ocr` — `zai-org/GLM-OCR`;
- `paddleocr_vl_1_6` — `PaddlePaddle/PaddleOCR-VL-1.6`;
- `qwen3_vl_2b` — `Qwen/Qwen3-VL-2B-Instruct`;
- optional heavier upper bound: `qwen3_vl_4b`.

### Zone semantics

`embeddinggemma2_zone` uses `google/embeddinggemma-2` to rerank text from JIN-localized semantic zones. It uses the Sentence Transformers retrieval interface: semantic labels are encoded as queries with `encode_query()`, while localized zone text is encoded as documents with `encode_document()`. It is intentionally reported in a **separate track** because an embedding model is not a generative field extractor.

Do not compare its zone score directly with field exact-match scores from the direct extraction track.

## Metrics

When reviewed ground truth is provided, the runner reports:

- normalized field exact match;
- average token F1;
- missing-field rate;
- hallucination rate;
- line-item reference recall;
- field bbox IoU and recall at IoU 0.50/0.75;
- semantic-zone type accuracy;
- zone bbox IoU and recall at IoU 0.50/0.75;
- JSON validity;
- latency per PDF;
- peak process RSS;
- parameter count / parameter-memory estimate for Transformers VLMs;
- Hugging Face cache size when available;
- JIN local model-artifact size.

No factual accuracy is claimed when reviewed ground truth is absent. Draft entries marked `needs_review` are processed for raw inference but excluded from accuracy metrics.

## Ground truth

Do not use JIN or competitor predictions as ground truth automatically. Create a review scaffold from real validation PDFs, then fill and approve it manually:

```bash
python -m benchmarks.open_weight_comparison.prepare_ground_truth \\
  --pdf-dir corpus/validation \\
  --limit 100 \\
  --output benchmarks/open_weight_comparison/ground_truth.json
```

The scaffold marks every new document as `needs_review` and also creates `ground_truth_review.csv`. The runner scores a document only when `_review.status` is `reviewed`, `approved` or `validated`. Existing ground-truth files without `_review` remain backward-compatible and are treated as implicitly reviewed.

Example review metadata:

```json
"_review": {
  "status": "reviewed",
  "reviewer": "initials-or-team",
  "reviewed_at": "2026-10-09",
  "notes": null
}
```

Coordinates are normalized to `[0,1000]` for each page.

```json
{
  "documents": {
    "order.pdf": {
      "order_number": "ABC123",
      "parties": {
        "ship_to": {
          "address": {
            "postal_code": "78300",
            "city": "POISSY"
          }
        }
      },
      "spatial": {
        "fields": [
          {"path": "order_number", "page": 1, "bbox": [700, 50, 900, 90]}
        ],
        "zones": [
          {"zone_type": "SHIP_TO", "page": 1, "bbox": [500, 200, 950, 400]}
        ]
      }
    }
  }
}
```

## Install

JIN itself uses the normal repository dependencies and model bundle.

Install the optional open-weight benchmark dependencies in a separate environment:

```bash
pip install -r runtime-requirements.txt
pip install -r benchmarks/open_weight_comparison/requirements.txt

# Transformers >= 5.19 is required for the current Qwen3-VL / EmbeddingGemma2 adapters.
```

A Hugging Face account/token may be needed if a model repository requires authentication. The benchmark does not store tokens in its outputs.

## List adapters

```bash
python -m benchmarks.open_weight_comparison.runner --list-models
```

## Preflight

Before a full campaign, validate the corpus, reviewed truth, full JIN core, local JIN model bundle, Hugging Face cache/offline mode, RAM and free disk:

```bash
python -m benchmarks.open_weight_comparison.preflight \\
  --pdf-dir corpus/validation \\
  --ground-truth benchmarks/open_weight_comparison/ground_truth.json \\
  --models-dir data/learning \\
  --models jin,qwen3_vl_2b,qwen3_vl_4b,embeddinggemma2_zone \\
  --output artifacts/open_weight_preflight.json
```

A full benchmark must not start while a blocking preflight check is red. In particular, GitHub does not contain the private PDF corpus, the prepared full JIN core under `model/`, or the `JIN_MODELS_AVAILABLE.zip` runtime bundle; these inputs must be present on the execution machine.

## Synthetic smoke test

A deterministic one-page purchase order can validate the VLM plumbing independently of Bosch data:

```bash
MODEL=qwen3_vl_2b sh scripts/run-open-weight-smoke.sh
MODEL=qwen3_vl_4b sh scripts/run-open-weight-smoke.sh
```

Synthetic smoke truth is explicitly marked `_review.synthetic=true`, and `decision_report.md` prints a warning that these results are plumbing-only and must never be published as Bosch/JIN business accuracy.

## CPU-first smoke benchmark

JIN is the baseline and should be present in every direct-extraction campaign. Start with JIN and Granite-Docling:

```bash
python -m benchmarks.open_weight_comparison.runner \
  --pdf-dir /path/to/pdfs \
  --ground-truth /path/to/reviewed_ground_truth.json \
  --models jin,granite_docling_258m \
  --models-dir data/learning \
  --device cpu \
  --limit 10 \
  --output-dir artifacts/open_weight_comparison
```

## Full default panel

```bash
python -m benchmarks.open_weight_comparison.runner \
  --pdf-dir /path/to/pdfs \
  --ground-truth /path/to/reviewed_ground_truth.json \
  --models jin,granite_docling_258m,glm_ocr,paddleocr_vl_1_6,qwen3_vl_2b,embeddinggemma2_zone \
  --models-dir data/learning \
  --device auto \
  --output-dir artifacts/open_weight_comparison
```

For a CPU-only machine, the runner automatically executes each requested model in an isolated subprocess before merging the results, so peak RSS is not inherited from the previous model. `qwen3_vl_4b` is intentionally not part of the default list because it is much heavier. The full three-campaign panel is available as `sh scripts/run-open-weight-cpu-benchmark.sh`; it runs the preflight first and stops immediately if the private corpus, reviewed truth, JIN core or local models are missing.

Recommended JIN-vs-Qwen CPU campaigns:

```bash
python -m benchmarks.open_weight_comparison.runner \
  --pdf-dir corpus/validation \
  --ground-truth benchmarks/open_weight_comparison/ground_truth.json \
  --models jin,qwen3_vl_2b \
  --models-dir data/learning \
  --device cpu \
  --limit 20 \
  --output-dir artifacts/jin_vs_qwen3_vl_2b

python -m benchmarks.open_weight_comparison.runner \
  --pdf-dir corpus/validation \
  --ground-truth benchmarks/open_weight_comparison/ground_truth.json \
  --models jin,qwen3_vl_4b \
  --models-dir data/learning \
  --device cpu \
  --limit 20 \
  --output-dir artifacts/jin_vs_qwen3_vl_4b
```

EmbeddingGemma2 stays in its separate zone-semantics track:

```bash
python -m benchmarks.open_weight_comparison.runner \
  --pdf-dir corpus/validation \
  --ground-truth benchmarks/open_weight_comparison/ground_truth.json \
  --models jin,embeddinggemma2_zone \
  --models-dir data/learning \
  --device cpu \
  --limit 20 \
  --output-dir artifacts/jin_vs_embeddinggemma2
```

## Output

The output directory contains:

```text
comparison.json
summary.csv
documents.csv
head_to_head.csv
field_comparison.csv
decision_report.md
raw/
  jin/
  granite_docling_258m/
  qwen3_vl_2b/
  qwen3_vl_4b/
  ...
```

`summary.csv` contains aggregate model metrics plus explicit deltas against JIN for direct-extraction models. `head_to_head.csv` compares every competitor against JIN PDF by PDF. `field_comparison.csv` records whether JIN or the competitor wins for each reviewed field. `decision_report.md` summarizes the strongest gains/losses and the most discriminating business fields. `documents.csv` keeps the original per-document metrics and `raw/` preserves raw responses for auditability.

## Fairness rules

1. JIN is the explicit baseline for the direct information-extraction track.
2. Same PDF set and reviewed truth for every model.
3. Same common business schema for direct extractors.
4. Same page limit and rendered contact-sheet resolution.
5. Deterministic generation (`do_sample=False`).
6. No JIN post-processing is applied to competitor outputs.
7. EmbeddingGemma2 is kept in a separate zone-semantics track.
8. Public benchmark scores from model cards are **not** mixed with these Bosch/JIN field metrics.

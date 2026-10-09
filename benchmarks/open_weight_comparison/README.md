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

`embeddinggemma2_zone` uses `google/embeddinggemma-2` to rerank text from JIN-localized semantic zones. It is intentionally reported in a **separate track** because an embedding model is not a generative field extractor.

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

No factual accuracy is claimed when reviewed ground truth is absent.

## Ground truth

Copy `ground_truth.example.json` and replace the example with reviewed values.

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
```

A Hugging Face account/token may be needed if a model repository requires authentication. The benchmark does not store tokens in its outputs.

## List adapters

```bash
python -m benchmarks.open_weight_comparison.runner --list-models
```

## CPU-first smoke benchmark

Start with JIN and Granite-Docling:

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

For a CPU-only machine, run the VLMs one at a time. `qwen3_vl_4b` is intentionally not part of the default list because it is much heavier.

## Output

The output directory contains:

```text
comparison.json
summary.csv
documents.csv
raw/
  jin/
  granite_docling_258m/
  ...
```

`summary.csv` is the decision table. `documents.csv` exposes per-document metrics. `raw/` preserves each model's raw response for auditability.

## Fairness rules

1. Same PDF set and reviewed truth for every model.
2. Same common business schema for direct extractors.
3. Same page limit and rendered contact-sheet resolution.
4. Deterministic generation (`do_sample=False`).
5. No JIN post-processing is applied to competitor outputs.
6. EmbeddingGemma2 is kept in a separate zone-semantics track.
7. Public benchmark scores from model cards are **not** mixed with these Bosch/JIN field metrics.

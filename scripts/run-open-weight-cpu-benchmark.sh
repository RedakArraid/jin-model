#!/bin/sh
set -eu

PDF_DIR="${PDF_DIR:-corpus/validation}"
GROUND_TRUTH="${GROUND_TRUTH:-benchmarks/open_weight_comparison/ground_truth.json}"
MODELS_DIR="${MODELS_DIR:-data/learning}"
LIMIT="${LIMIT:-20}"
DEVICE="${DEVICE:-cpu}"

run_pair() {
  candidate="$1"
  output="$2"

  echo "=== JIN vs ${candidate} ==="
  python -m benchmarks.open_weight_comparison.runner \
    --pdf-dir "$PDF_DIR" \
    --ground-truth "$GROUND_TRUTH" \
    --models "jin,${candidate}" \
    --models-dir "$MODELS_DIR" \
    --device "$DEVICE" \
    --limit "$LIMIT" \
    --output-dir "$output"
}

if [ ! -f "$GROUND_TRUTH" ]; then
  echo "Ground truth not found: $GROUND_TRUTH" >&2
  echo "Create it first with:" >&2
  echo "python -m benchmarks.open_weight_comparison.prepare_ground_truth --pdf-dir $PDF_DIR --limit 100 --output $GROUND_TRUTH" >&2
  exit 4
fi

run_pair qwen3_vl_2b artifacts/jin_vs_qwen3_vl_2b
run_pair qwen3_vl_4b artifacts/jin_vs_qwen3_vl_4b
run_pair embeddinggemma2_zone artifacts/jin_vs_embeddinggemma2

echo
echo "Benchmark campaigns completed."
echo "Decision reports:"
echo "  artifacts/jin_vs_qwen3_vl_2b/decision_report.md"
echo "  artifacts/jin_vs_qwen3_vl_4b/decision_report.md"
echo "  artifacts/jin_vs_embeddinggemma2/decision_report.md"

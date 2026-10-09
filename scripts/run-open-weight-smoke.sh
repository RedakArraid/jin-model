#!/bin/sh
set -eu

PYTHON_BIN="${PYTHON_BIN:-python}"

MODEL="${MODEL:-qwen3_vl_2b}"
DEVICE="${DEVICE:-cpu}"
OUT_ROOT="${OUT_ROOT:-artifacts/open_weight_smoke}"

"$PYTHON_BIN" -m benchmarks.open_weight_comparison.make_smoke_fixture \
  --output-dir "$OUT_ROOT/fixture"

"$PYTHON_BIN" -m benchmarks.open_weight_comparison.runner \
  --pdf-dir "$OUT_ROOT/fixture" \
  --ground-truth "$OUT_ROOT/fixture/ground_truth.json" \
  --models "$MODEL" \
  --device "$DEVICE" \
  --limit 1 \
  --output-dir "$OUT_ROOT/$MODEL"

echo
echo "Synthetic smoke completed for $MODEL."
echo "This is a plumbing test only, not a Bosch/JIN business benchmark."
echo "Report: $OUT_ROOT/$MODEL/decision_report.md"

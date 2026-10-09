#!/bin/sh
set -eu

PYTHON_BIN="${PYTHON_BIN:-python}"

PDF_DIR="${PDF_DIR:-corpus/validation}"
GROUND_TRUTH="${GROUND_TRUTH:-benchmarks/open_weight_comparison/ground_truth.json}"
MODELS_DIR="${MODELS_DIR:-data/learning}"
LIMIT="${LIMIT:-20}"
DEVICE="${DEVICE:-cpu}"
CPU_THREADS="${CPU_THREADS:-4}"
JIN_MODELS_BUNDLE="${JIN_MODELS_BUNDLE:-JIN_MODELS_AVAILABLE.zip}"
JIN_CORE_ENGINE="${JIN_CORE_ENGINE:-}"
OPEN_WEIGHT_MODEL_ROOT="${OPEN_WEIGHT_MODEL_ROOT:-data/open_weight_models}"
AUTO_DOWNLOAD_QWEN="${AUTO_DOWNLOAD_QWEN:-1}"

if [ "$AUTO_DOWNLOAD_QWEN" = "1" ]; then
  PYTHON_BIN="$PYTHON_BIN" TARGET_ROOT="$OPEN_WEIGHT_MODEL_ROOT" \
    sh scripts/download-qwen-benchmark-models.sh
fi

if [ -d "$OPEN_WEIGHT_MODEL_ROOT/qwen3_vl_2b" ]; then
  export JIN_BENCH_QWEN3_VL_2B_PATH="$OPEN_WEIGHT_MODEL_ROOT/qwen3_vl_2b"
fi
if [ -d "$OPEN_WEIGHT_MODEL_ROOT/qwen3_vl_4b" ]; then
  export JIN_BENCH_QWEN3_VL_4B_PATH="$OPEN_WEIGHT_MODEL_ROOT/qwen3_vl_4b"
fi

if [ ! -f "$MODELS_DIR/jin-field-weak-router-v2-cpu.joblib" ] && [ -f "$JIN_MODELS_BUNDLE" ]; then
  echo "Installing JIN runtime models from $JIN_MODELS_BUNDLE"
  "$PYTHON_BIN" scripts/install_models.py --source "$JIN_MODELS_BUNDLE" --target "$MODELS_DIR"
fi

if [ ! -f model/uda/engine.py ] && [ -n "$JIN_CORE_ENGINE" ] && [ -f "$JIN_CORE_ENGINE" ]; then
  echo "Preparing full JIN core from $JIN_CORE_ENGINE"
  ./prepare-model.sh "$JIN_CORE_ENGINE"
fi

run_pair() {
  candidate="$1"
  output="$2"

  echo "=== JIN vs ${candidate} ==="
  "$PYTHON_BIN" -m benchmarks.open_weight_comparison.runner \
    --pdf-dir "$PDF_DIR" \
    --ground-truth "$GROUND_TRUTH" \
    --models "jin,${candidate}" \
    --models-dir "$MODELS_DIR" \
    --device "$DEVICE" \
    --cpu-threads "$CPU_THREADS" \
    --limit "$LIMIT" \
    --output-dir "$output"
}

"$PYTHON_BIN" -m benchmarks.open_weight_comparison.preflight \
  --pdf-dir "$PDF_DIR" \
  --ground-truth "$GROUND_TRUTH" \
  --models-dir "$MODELS_DIR" \
  --models "jin,qwen3_vl_2b,qwen3_vl_4b,embeddinggemma2_zone" \
  --output artifacts/open_weight_preflight.json

run_pair qwen3_vl_2b artifacts/jin_vs_qwen3_vl_2b
run_pair qwen3_vl_4b artifacts/jin_vs_qwen3_vl_4b
run_pair embeddinggemma2_zone artifacts/jin_vs_embeddinggemma2

echo
echo "Benchmark campaigns completed."
echo "Decision reports:"
echo "  artifacts/jin_vs_qwen3_vl_2b/decision_report.md"
echo "  artifacts/jin_vs_qwen3_vl_4b/decision_report.md"
echo "  artifacts/jin_vs_embeddinggemma2/decision_report.md"

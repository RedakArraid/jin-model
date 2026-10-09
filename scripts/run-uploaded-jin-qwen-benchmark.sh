#!/bin/sh
set -eu

BENCHMARK_INPUT_ZIP="${BENCHMARK_INPUT_ZIP:-}"
JIN_BUNDLE_ZIP="${JIN_BUNDLE_ZIP:-}"
WORK_ROOT="${WORK_ROOT:-artifacts/uploaded_jin_qwen_benchmark}"
CPU_THREADS="${CPU_THREADS:-4}"
DEVICE="${DEVICE:-cpu}"
LIMIT="${LIMIT:-100}"
PYTHON_BIN="${PYTHON_BIN:-.venv-open-weight/bin/python}"

if [ -z "$BENCHMARK_INPUT_ZIP" ] || [ ! -f "$BENCHMARK_INPUT_ZIP" ]; then
  echo "ERROR: BENCHMARK_INPUT_ZIP must point to jin_qwen_benchmark_input_100.zip" >&2
  exit 2
fi
if [ -z "$JIN_BUNDLE_ZIP" ] || [ ! -f "$JIN_BUNDLE_ZIP" ]; then
  echo "ERROR: JIN_BUNDLE_ZIP must point to jin-model-v5.12.0-commercial-metadata-cpu.zip" >&2
  exit 2
fi

mkdir -p "$WORK_ROOT"
BENCH_DIR="$WORK_ROOT/input"
JIN_TMP="$WORK_ROOT/jin_bundle"

rm -rf "$BENCH_DIR" "$JIN_TMP"
mkdir -p "$BENCH_DIR" "$JIN_TMP"

unzip -q "$BENCHMARK_INPUT_ZIP" -d "$BENCH_DIR"
unzip -q "$JIN_BUNDLE_ZIP" -d "$JIN_TMP"

JIN_ROOT="$(find "$JIN_TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
if [ -z "$JIN_ROOT" ]; then
  JIN_ROOT="$JIN_TMP"
fi

if [ ! -f "$JIN_ROOT/model/uda/engine.py" ]; then
  echo "ERROR: uploaded JIN bundle does not contain model/uda/engine.py" >&2
  exit 3
fi

mkdir -p model data/learning
rm -rf model/*
cp -a "$JIN_ROOT/model/." model/
cp -a "$JIN_ROOT/data/learning/." data/learning/

if [ ! -x "$PYTHON_BIN" ]; then
  sh scripts/setup-open-weight-benchmark-env.sh
fi

PYTHON_BIN="$(cd "$(dirname "$PYTHON_BIN")" && pwd)/$(basename "$PYTHON_BIN")"

PDF_DIR="$BENCH_DIR/pdfs"
GROUND_TRUTH="$BENCH_DIR/ground_truth_metadata_100_validated.json"

if [ ! -d "$PDF_DIR" ] || [ ! -f "$GROUND_TRUTH" ]; then
  echo "ERROR: benchmark ZIP is missing pdfs/ or ground_truth_metadata_100_validated.json" >&2
  exit 4
fi

PYTHON_BIN="$PYTHON_BIN" TARGET_ROOT="data/open_weight_models" \
  sh scripts/download-qwen-benchmark-models.sh

export JIN_BENCH_QWEN3_VL_2B_PATH="$(pwd)/data/open_weight_models/qwen3_vl_2b"
export JIN_BENCH_QWEN3_VL_4B_PATH="$(pwd)/data/open_weight_models/qwen3_vl_4b"

"$PYTHON_BIN" -m benchmarks.open_weight_comparison.preflight \
  --pdf-dir "$PDF_DIR" \
  --ground-truth "$GROUND_TRUTH" \
  --models-dir data/learning \
  --models jin,qwen3_vl_2b,qwen3_vl_4b \
  --output "$WORK_ROOT/preflight.json"

run_pair() {
  candidate="$1"
  output="$WORK_ROOT/jin_vs_$candidate"
  "$PYTHON_BIN" -m benchmarks.open_weight_comparison.runner \
    --pdf-dir "$PDF_DIR" \
    --ground-truth "$GROUND_TRUTH" \
    --models "jin,$candidate" \
    --models-dir data/learning \
    --device "$DEVICE" \
    --cpu-threads "$CPU_THREADS" \
    --limit "$LIMIT" \
    --output-dir "$output"
}

run_pair qwen3_vl_2b
run_pair qwen3_vl_4b

(
  cd "$WORK_ROOT"
  rm -f JIN_QWEN_FINAL_RESULTS.zip
  zip -qr JIN_QWEN_FINAL_RESULTS.zip \
    preflight.json \
    jin_vs_qwen3_vl_2b \
    jin_vs_qwen3_vl_4b
)

echo
echo "DONE"
echo "Results: $WORK_ROOT/JIN_QWEN_FINAL_RESULTS.zip"
echo "2B report: $WORK_ROOT/jin_vs_qwen3_vl_2b/decision_report.md"
echo "4B report: $WORK_ROOT/jin_vs_qwen3_vl_4b/decision_report.md"

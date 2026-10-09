#!/bin/sh
set -eu

PYTHON_BIN="${PYTHON_BIN:-python}"
TARGET_ROOT="${TARGET_ROOT:-data/open_weight_models}"
MODELS="${MODELS:-qwen3_vl_2b,qwen3_vl_4b}"

mkdir -p "$TARGET_ROOT"

verify_2b() {
  dir="$1"
  test -f "$dir/model.safetensors" || return 1
  actual="$(sha256sum "$dir/model.safetensors" | awk '{print $1}')"
  test "$actual" = "7de1838c87a5349b016c26a1c3f7d2bc400a3d485f95ef39a7059ffd734977a0"
}

verify_4b() {
  dir="$1"
  test -f "$dir/model-00001-of-00002.safetensors" || return 1
  test -f "$dir/model-00002-of-00002.safetensors" || return 1
  a="$(sha256sum "$dir/model-00001-of-00002.safetensors" | awk '{print $1}')"
  b="$(sha256sum "$dir/model-00002-of-00002.safetensors" | awk '{print $1}')"
  test "$a" = "30a01a0556622645a3cce87b655bbbbbc1f170c196099f1b666c93202c3339a9" &&
  test "$b" = "046296a2a387efb43b0c997d5833c789604d168834f6e0d3064bf7bb13d002a6"
}

ensure_modelscope() {
  if command -v modelscope >/dev/null 2>&1; then
    return 0
  fi
  "$PYTHON_BIN" -m pip install "modelscope>=1.30,<2"
}

download_model() {
  name="$1"
  repo="$2"
  dir="$3"

  echo "=== Downloading $name ==="
  mkdir -p "$dir"

  if command -v hf >/dev/null 2>&1; then
    echo "Trying Hugging Face Hub..."
    if hf download "$repo" --local-dir "$dir"; then
      return 0
    fi
    echo "Hugging Face download failed; trying ModelScope." >&2
  fi

  ensure_modelscope
  modelscope download --model "$repo" --local_dir "$dir"
}

old_ifs="$IFS"
IFS=','
for name in $MODELS; do
  IFS="$old_ifs"
  case "$name" in
    qwen3_vl_2b)
      dir="$TARGET_ROOT/qwen3_vl_2b"
      if verify_2b "$dir"; then
        echo "qwen3_vl_2b already present and SHA-256 verified."
      else
        download_model qwen3_vl_2b Qwen/Qwen3-VL-2B-Instruct "$dir"
        verify_2b "$dir" || {
          echo "ERROR: qwen3_vl_2b official weight SHA-256 verification failed." >&2
          exit 5
        }
      fi
      ;;
    qwen3_vl_4b)
      dir="$TARGET_ROOT/qwen3_vl_4b"
      if verify_4b "$dir"; then
        echo "qwen3_vl_4b already present and SHA-256 verified."
      else
        download_model qwen3_vl_4b Qwen/Qwen3-VL-4B-Instruct "$dir"
        verify_4b "$dir" || {
          echo "ERROR: qwen3_vl_4b official weight SHA-256 verification failed." >&2
          exit 5
        }
      fi
      ;;
    *)
      echo "ERROR: unsupported model $name" >&2
      exit 2
      ;;
  esac
  IFS=','
done
IFS="$old_ifs"

echo
echo "Qwen benchmark models ready under $TARGET_ROOT"
echo "export JIN_BENCH_QWEN3_VL_2B_PATH=$TARGET_ROOT/qwen3_vl_2b"
echo "export JIN_BENCH_QWEN3_VL_4B_PATH=$TARGET_ROOT/qwen3_vl_4b"

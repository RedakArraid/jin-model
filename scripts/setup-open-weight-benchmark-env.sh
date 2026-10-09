#!/bin/sh
set -eu

PYTHON_BOOTSTRAP="${PYTHON_BOOTSTRAP:-python3}"
VENV_DIR="${VENV_DIR:-.venv-open-weight}"

"$PYTHON_BOOTSTRAP" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r runtime-requirements.txt
"$VENV_DIR/bin/python" -m pip install -r benchmarks/open_weight_comparison/requirements.txt

"$VENV_DIR/bin/python" -m benchmarks.open_weight_comparison.runner --list-models

echo
echo "Open-weight benchmark environment is ready."
echo "Use: PYTHON_BIN=$VENV_DIR/bin/python make benchmark-preflight"
echo "Then: PYTHON_BIN=$VENV_DIR/bin/python make benchmark-open-weight"

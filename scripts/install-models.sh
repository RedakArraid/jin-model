#!/usr/bin/env sh
set -eu

SOURCE=${1:-JIN_MODELS_AVAILABLE.zip}
python3 scripts/install_models.py --source "$SOURCE"
python3 scripts/verify_models.py

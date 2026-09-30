param(
    [string]$Source = ".\JIN_MODELS_AVAILABLE.zip"
)

$ErrorActionPreference = "Stop"
python scripts/install_models.py --source $Source
python scripts/verify_models.py

# JIN runtime models

The runtime expects trained artifacts in `data/learning/`.

Required for the standalone PDF/field API:

- `corpus_text_router.joblib`
- `jin-pdf-fusion-router-v3-cpu.joblib`
- `jin-field-weak-router-v2-cpu.joblib`

Optional:

- `address_models.joblib` — produced only from real correction feedback.

Install the ready artifacts from a local model bundle:

```bash
python scripts/install_models.py --source JIN_MODELS_AVAILABLE.zip
python scripts/verify_models.py
```

Windows PowerShell:

```powershell
.\scripts\install-models.ps1 .\JIN_MODELS_AVAILABLE.zip
```

The expected SHA-256 values and byte sizes are recorded in
`models/JIN_MODELS_MANIFEST.json`.

To build the optional address statistical memory after collecting corrections:

```bash
python training/train_statistical.py \
  --feedback feedback/learning_feedback.jsonl \
  --model-dir data/learning
```

Do not create an empty `address_models.joblib`: without reviewed correction
feedback it would not represent a trained model.

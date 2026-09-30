# JIN runtime setup

This runbook covers the two supported runtime modes.

## 1. Prerequisites

Recommended:

- Docker Engine / Docker Desktop with Compose V2;
- alternatively Python 3.12;
- CPU only; no GPU is required.

The OCR runtime uses Tesseract languages `fra+eng+deu`.

## 2. Install the trained model artifacts

Place `JIN_MODELS_AVAILABLE.zip` at the repository root, then run:

### Linux / macOS / Git Bash

```bash
./scripts/install-models.sh JIN_MODELS_AVAILABLE.zip
```

### Windows PowerShell

```powershell
.\scripts\install-models.ps1 .\JIN_MODELS_AVAILABLE.zip
```

Or directly:

```bash
python scripts/install_models.py --source JIN_MODELS_AVAILABLE.zip
python scripts/verify_models.py
```

The installer verifies exact byte sizes and SHA-256 hashes from
`models/JIN_MODELS_MANIFEST.json`.

The installed runtime directory becomes:

```text
data/learning/
├── corpus_text_router.joblib
├── jin-pdf-fusion-router-v3-cpu.joblib
└── jin-field-weak-router-v2-cpu.joblib
```

`address_models.joblib` is optional and must only be trained from real
reviewed corrections.

## 3. Standalone mode — no uda core required

This mode exposes the learned CPU models, zones and cells directly.

```bash
docker compose -f docker-compose.standalone.yml up --build
```

or:

```bash
make standalone
```

API: `http://localhost:8000`

Health:

```bash
curl http://localhost:8000/health
```

Analyze one PDF with both the document router and field/zone/cell router:

```bash
curl -F "file=@commande.pdf" \
  http://localhost:8000/analyze
```

Document family / keep-review-remove only:

```bash
curl -F "file=@commande.pdf" \
  http://localhost:8000/learning/pdf-route
```

Weak field + spatial zone + cell suggestions:

```bash
curl -F "file=@commande.pdf" \
  http://localhost:8000/learning/field-route
```

The weak field output remains `requires_review=true`.

## 4. Full JIN mode — packaged uda core

The full `/extract` proxy requires the packaged JIN core engine.

Prepare it from the source archive:

```bash
./prepare-model.sh <jin-core-engine.zip>
```

The command must create:

```text
model/
├── requirements.txt
└── uda/
```

Then:

```bash
python scripts/verify_models.py
docker compose up --build
```

The web/API stack is exposed through the existing full Docker Compose setup.

## 5. Optional statistical address memory

When reviewed corrections exist in:

```text
feedback/learning_feedback.jsonl
```

train the address memory with:

```bash
python training/train_statistical.py \
  --feedback feedback/learning_feedback.jsonl \
  --model-dir data/learning
```

This produces:

```text
data/learning/address_models.joblib
```

The runtime automatically loads it.

## 6. Verify before deployment

Model integrity:

```bash
python scripts/verify_models.py
```

Local dependency/OCR check:

```bash
python scripts/preflight.py
```

Full mode including the uda core:

```bash
python scripts/preflight.py --full
```

Tests:

```bash
python -m unittest discover -s tests -v
```

## 7. Runtime environment variables

See `.env.example`.

Important defaults:

```text
JIN_CORPUS_ROUTER_MODEL=/app/data/learning/corpus_text_router.joblib
JIN_PDF_ROUTER_MODEL=/app/data/learning/jin-pdf-fusion-router-v3-cpu.joblib
JIN_FIELD_ROUTER_MODEL=/app/data/learning/jin-field-weak-router-v2-cpu.joblib
JIN_OCR_LANGS=fra+eng+deu
JIN_FIELD_ROUTER_THRESHOLD=0.80
```

## 8. What is optional

The standalone runtime works without:

- a GPU;
- the `uda` core package;
- `address_models.joblib`.

The full `/extract` workflow requires `uda`.

The statistical address memory requires genuine correction feedback and is
therefore intentionally not bootstrapped with an empty model.

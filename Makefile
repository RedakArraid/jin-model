SOURCE ?= JIN_MODELS_AVAILABLE.zip
PYTHON_BIN ?= python

.PHONY: models verify standalone full test benchmark-env benchmark-preflight benchmark-smoke benchmark-open-weight benchmark-qwen2b benchmark-qwen4b benchmark-embedding

models:
	"$(PYTHON_BIN)" scripts/install_models.py --source "$(SOURCE)"

verify:
	"$(PYTHON_BIN)" scripts/verify_models.py

standalone: verify
	docker compose -f docker-compose.standalone.yml up --build

full: verify
	test -d model/uda || (echo "Missing model/uda; run ./prepare-model.sh <engine.zip>" && exit 2)
	docker compose up --build

test:
	"$(PYTHON_BIN)" -m unittest discover -s tests -v

PDF_DIR ?= corpus/validation
GROUND_TRUTH ?= benchmarks/open_weight_comparison/ground_truth.json
MODELS_DIR ?= data/learning
LIMIT ?= 20
DEVICE ?= cpu
SMOKE_MODEL ?= qwen3_vl_2b

benchmark-preflight:
	"$(PYTHON_BIN)" -m benchmarks.open_weight_comparison.preflight --pdf-dir "$(PDF_DIR)" --ground-truth "$(GROUND_TRUTH)" --models-dir "$(MODELS_DIR)" --models jin,qwen3_vl_2b,qwen3_vl_4b,embeddinggemma2_zone --output artifacts/open_weight_preflight.json

benchmark-smoke:
	PYTHON_BIN="$(PYTHON_BIN)" MODEL="$(SMOKE_MODEL)" DEVICE="$(DEVICE)" sh scripts/run-open-weight-smoke.sh

benchmark-qwen2b:
	"$(PYTHON_BIN)" -m benchmarks.open_weight_comparison.runner --pdf-dir "$(PDF_DIR)" --ground-truth "$(GROUND_TRUTH)" --models jin,qwen3_vl_2b --models-dir "$(MODELS_DIR)" --device "$(DEVICE)" --limit "$(LIMIT)" --output-dir artifacts/jin_vs_qwen3_vl_2b

benchmark-qwen4b:
	"$(PYTHON_BIN)" -m benchmarks.open_weight_comparison.runner --pdf-dir "$(PDF_DIR)" --ground-truth "$(GROUND_TRUTH)" --models jin,qwen3_vl_4b --models-dir "$(MODELS_DIR)" --device "$(DEVICE)" --limit "$(LIMIT)" --output-dir artifacts/jin_vs_qwen3_vl_4b

benchmark-embedding:
	"$(PYTHON_BIN)" -m benchmarks.open_weight_comparison.runner --pdf-dir "$(PDF_DIR)" --ground-truth "$(GROUND_TRUTH)" --models jin,embeddinggemma2_zone --models-dir "$(MODELS_DIR)" --device "$(DEVICE)" --limit "$(LIMIT)" --output-dir artifacts/jin_vs_embeddinggemma2

benchmark-open-weight:
	PYTHON_BIN="$(PYTHON_BIN)" PDF_DIR="$(PDF_DIR)" GROUND_TRUTH="$(GROUND_TRUTH)" MODELS_DIR="$(MODELS_DIR)" LIMIT="$(LIMIT)" DEVICE="$(DEVICE)" sh scripts/run-open-weight-cpu-benchmark.sh


benchmark-env:
	sh scripts/setup-open-weight-benchmark-env.sh

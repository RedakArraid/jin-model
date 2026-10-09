SOURCE ?= JIN_MODELS_AVAILABLE.zip

.PHONY: models verify standalone full test benchmark-preflight benchmark-smoke benchmark-open-weight benchmark-qwen2b benchmark-qwen4b benchmark-embedding

models:
	python scripts/install_models.py --source "$(SOURCE)"

verify:
	python scripts/verify_models.py

standalone: verify
	docker compose -f docker-compose.standalone.yml up --build

full: verify
	test -d model/uda || (echo "Missing model/uda; run ./prepare-model.sh <engine.zip>" && exit 2)
	docker compose up --build

test:
	python -m unittest discover -s tests -v

PDF_DIR ?= corpus/validation
GROUND_TRUTH ?= benchmarks/open_weight_comparison/ground_truth.json
MODELS_DIR ?= data/learning
LIMIT ?= 20
DEVICE ?= cpu
SMOKE_MODEL ?= qwen3_vl_2b

benchmark-preflight:
	python -m benchmarks.open_weight_comparison.preflight --pdf-dir "$(PDF_DIR)" --ground-truth "$(GROUND_TRUTH)" --models-dir "$(MODELS_DIR)" --models jin,qwen3_vl_2b,qwen3_vl_4b,embeddinggemma2_zone --output artifacts/open_weight_preflight.json

benchmark-smoke:
	MODEL="$(SMOKE_MODEL)" DEVICE="$(DEVICE)" sh scripts/run-open-weight-smoke.sh

benchmark-qwen2b:
	python -m benchmarks.open_weight_comparison.runner --pdf-dir "$(PDF_DIR)" --ground-truth "$(GROUND_TRUTH)" --models jin,qwen3_vl_2b --models-dir "$(MODELS_DIR)" --device "$(DEVICE)" --limit "$(LIMIT)" --output-dir artifacts/jin_vs_qwen3_vl_2b

benchmark-qwen4b:
	python -m benchmarks.open_weight_comparison.runner --pdf-dir "$(PDF_DIR)" --ground-truth "$(GROUND_TRUTH)" --models jin,qwen3_vl_4b --models-dir "$(MODELS_DIR)" --device "$(DEVICE)" --limit "$(LIMIT)" --output-dir artifacts/jin_vs_qwen3_vl_4b

benchmark-embedding:
	python -m benchmarks.open_weight_comparison.runner --pdf-dir "$(PDF_DIR)" --ground-truth "$(GROUND_TRUTH)" --models jin,embeddinggemma2_zone --models-dir "$(MODELS_DIR)" --device "$(DEVICE)" --limit "$(LIMIT)" --output-dir artifacts/jin_vs_embeddinggemma2

benchmark-open-weight:
	PDF_DIR="$(PDF_DIR)" GROUND_TRUTH="$(GROUND_TRUTH)" MODELS_DIR="$(MODELS_DIR)" LIMIT="$(LIMIT)" DEVICE="$(DEVICE)" sh scripts/run-open-weight-cpu-benchmark.sh

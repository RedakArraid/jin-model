SOURCE ?= JIN_MODELS_AVAILABLE.zip

.PHONY: models verify standalone full test

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

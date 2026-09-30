#!/usr/bin/env sh
set -eu

python3 scripts/verify_models.py
docker compose -f docker-compose.standalone.yml up --build

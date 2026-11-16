#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$REPOSITORY_ROOT"

PYTHON_BIN=${PYTHON_BIN:-python3}
COMPOSE=(docker compose -f "$REPOSITORY_ROOT/docker-compose.yml")
for env_file in "${JIN_RUNTIME_SETTINGS_FILE:-}" "${JIN_RELEASE_ENV_FILE:-}" "${JIN_COMPOSE_ENV_FILE:-}"; do
  if [ -n "$env_file" ]; then
    COMPOSE+=(--env-file "$env_file")
  fi
done
# Read only the resolved backup mount from Compose, without printing secrets.
# This also honors paths specified in .env instead of the shell environment.
BACKUP_ROOT=$("${COMPOSE[@]}" --profile ops config --format json | "$PYTHON_BIN" -c \
  'import json,sys; c=json.load(sys.stdin); print(next(v["source"] for v in c["services"]["backup"]["volumes"] if v["target"]=="/app/backups"))')
mkdir -p "$BACKUP_ROOT"

JIN_BACKUP_GIT_REVISION=${JIN_BACKUP_GIT_REVISION:-$(git rev-parse HEAD)}
export JIN_BACKUP_GIT_REVISION
"${COMPOSE[@]}" --profile ops run --rm --build backup

LATEST_BACKUP=$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
  -name 'jin-model-*' -printf '%T@ %p\n' | sort -nr | head -n 1 | cut -d' ' -f2-)
if [ -z "$LATEST_BACKUP" ]; then
  echo "No JIN backup directory was created" >&2
  exit 2
fi

"$PYTHON_BIN" scripts/jin_backup.py verify "$LATEST_BACKUP"

if [ "${JIN_BACKUP_AZURE_LOGIN_IDENTITY:-1}" = "1" ]; then
  az login --identity --allow-no-subscriptions --output none
fi

"$PYTHON_BIN" scripts/jin_backup.py azure-upload "$LATEST_BACKUP"

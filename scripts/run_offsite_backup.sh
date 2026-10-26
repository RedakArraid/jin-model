#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$REPOSITORY_ROOT"

BACKUP_ROOT_VALUE=${JIN_BACKUP_ROOT:-./backups}
case "$BACKUP_ROOT_VALUE" in
  /*) BACKUP_ROOT=$BACKUP_ROOT_VALUE ;;
  *) BACKUP_ROOT=$REPOSITORY_ROOT/${BACKUP_ROOT_VALUE#./} ;;
esac
mkdir -p "$BACKUP_ROOT"

JIN_BACKUP_GIT_REVISION=${JIN_BACKUP_GIT_REVISION:-$(git rev-parse HEAD)}
export JIN_BACKUP_GIT_REVISION
docker compose --profile ops run --rm --build backup

LATEST_BACKUP=$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d \
  -name 'jin-model-*' -printf '%T@ %p\n' | sort -nr | head -n 1 | cut -d' ' -f2-)
if [ -z "$LATEST_BACKUP" ]; then
  echo "No JIN backup directory was created" >&2
  exit 2
fi

PYTHON_BIN=${PYTHON_BIN:-python3}
"$PYTHON_BIN" scripts/jin_backup.py verify "$LATEST_BACKUP"

if [ "${JIN_BACKUP_AZURE_LOGIN_IDENTITY:-1}" = "1" ]; then
  az login --identity --allow-no-subscriptions --output none
fi

"$PYTHON_BIN" scripts/jin_backup.py azure-upload "$LATEST_BACKUP"

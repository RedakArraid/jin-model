#!/usr/bin/env sh
set -eu

ARCHIVE=${1:-jin-model-v4.8-source.zip}

if [ ! -f "$ARCHIVE" ]; then
  echo "Archive introuvable: $ARCHIVE" >&2
  echo "Place le ZIP V4.8 dans le depot puis relance: ./prepare-model.sh <archive.zip>" >&2
  exit 1
fi

rm -rf model .model-tmp
mkdir -p model .model-tmp
unzip -q "$ARCHIVE" -d .model-tmp

if [ -d ".model-tmp/universal_document_ai_rl_v48" ]; then
  cp -a .model-tmp/universal_document_ai_rl_v48/. model/
else
  cp -a .model-tmp/. model/
fi

rm -rf .model-tmp

if [ ! -f model/requirements.txt ] || [ ! -d model/uda ]; then
  echo "Archive invalide: le moteur V4.8 n'a pas ete trouve." >&2
  exit 1
fi

echo "Modele prepare dans ./model"
echo "Lance maintenant: docker compose up --build"

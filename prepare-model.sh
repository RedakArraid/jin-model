#!/usr/bin/env sh
set -eu

ARCHIVE=${1:-jin-model-source.zip}

if [ ! -f "$ARCHIVE" ]; then
  echo "Archive introuvable: $ARCHIVE" >&2
  echo "Place le ZIP du moteur JIN (V4.8 ou V5.x) dans le depot puis relance: ./prepare-model.sh <archive.zip>" >&2
  exit 1
fi

rm -rf model .model-tmp
mkdir -p model .model-tmp
unzip -q "$ARCHIVE" -d .model-tmp

UDA_DIR=$(find .model-tmp -maxdepth 3 -type d -name uda -print -quit)
if [ -z "$UDA_DIR" ]; then
  echo "Archive invalide: aucun package uda/ trouve." >&2
  rm -rf .model-tmp
  exit 1
fi
ENGINE_ROOT=$(dirname "$UDA_DIR")
cp -a "$ENGINE_ROOT"/. model/
rm -rf .model-tmp

if [ ! -f model/requirements.txt ] || [ ! -d model/uda ]; then
  echo "Archive invalide: requirements.txt ou uda/ manquant." >&2
  exit 1
fi

echo "Moteur JIN prepare dans ./model"
echo "Couche statistique versionnee: ./jin_runtime"
echo "Lance maintenant: docker compose up --build"

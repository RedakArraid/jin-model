#!/usr/bin/env sh
set -eu
OUT=${1:-source}
mkdir -p "$OUT"
cat bundle_parts/part_* | base64 -d > /tmp/jin-model.tar.xz
tar -xJf /tmp/jin-model.tar.xz -C "$OUT"
rm -f /tmp/jin-model.tar.xz
echo "Source extracted to $OUT/universal_document_ai_rl_v48"

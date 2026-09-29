#!/usr/bin/env sh
set -eu

if ! command -v git >/dev/null 2>&1; then
  echo "git is required" >&2
  exit 2
fi
if ! git lfs version >/dev/null 2>&1; then
  echo "git-lfs is required" >&2
  exit 2
fi

mkdir -p corpus/pdfs
git lfs install --local >/dev/null
git lfs pull --include="corpus/pdfs/**" --exclude=""

if ! find corpus/pdfs -type f -name '*.pdf' -print -quit | grep -q .; then
  echo "No PDF corpus found under corpus/pdfs/." >&2
  exit 4
fi

bad_file=$(mktemp)
trap 'rm -f "$bad_file"' EXIT HUP INT TERM

find corpus/pdfs -type f -name '*.pdf' -exec sh -c '
  file=$1
  if head -n 1 "$file" | grep -q "^version https://git-lfs.github.com/spec/v1$"; then
    echo "LFS pointer still present: $file"
  elif ! head -c 5 "$file" | grep -q "%PDF-"; then
    echo "Not a PDF binary: $file"
  else
    echo "checked: $file" >&2
  fi
' _ {} \; > "$bad_file"

if [ -s "$bad_file" ]; then
  cat "$bad_file" >&2
  exit 3
fi

echo "Git LFS corpus materialized successfully."

#!/usr/bin/env bash
# TesseractAtlas end-to-end demo. Non-interactive, safe to re-run, exits when done.
# State goes to the default ./.tesseractatlas (so the tool is signed in afterwards);
# print files go to ./demo_out.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
OUT="demo_out"

run() {
  printf '\n$ %s tesseract_atlas.py %s\n' "$PY" "$*"
  "$PY" tesseract_atlas.py "$@"
}

run --version
run demo --out-dir "$OUT"
run status
run list
run open spiral
run show spiral --rows 16
run show spiral --cell 3,3
run edit spiral --cell 1,1=K --add-symbol K=Ochre
run edit spiral --cell 1,1=Z --add-symbol Z=Ochre --dry-run
run edit spiral --undo
run fold preview spiral
run colors list
run export txt spiral --out-dir "$OUT" --force
run export html spiral --out-dir "$OUT" --force
run export list spiral

printf '\ndemo complete. state: .tesseractatlas/   print files: %s/\n' "$OUT"

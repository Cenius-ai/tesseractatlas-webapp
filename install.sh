#!/usr/bin/env bash
# TesseractAtlas install: project-level dependencies, the console entry point and
# the demo seed. It installs, seeds and EXITS -- starting the tool is a separate
# command (`python3 tesseract_atlas.py --help`, or `tesseract-atlas --help`).
#
# Runs as an unprivileged user: no sudo, no system package manager, no services.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

echo "==> python: $($PY --version 2>&1)"

# 1. Tooling first, so an editable install of this pyproject resolves.
if ! "$PY" -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1; then
  echo "warning: could not upgrade pip/setuptools/wheel (offline?). Continuing: this project has no runtime dependencies."
fi

# 2. Console entry point (tesseract-atlas). Optional: the single file also runs directly.
if "$PY" -m pip install -e . >/dev/null 2>&1; then
  echo "==> console script installed: tesseract-atlas"
else
  echo "warning: editable install of the console script failed; run the tool as: $PY tesseract_atlas.py"
fi

# 3. Cheap import self-check: fails loudly here rather than at first use.
"$PY" -c 'import tesseract_atlas; print("==> import ok:", tesseract_atlas.VERSION)'

# 4. Idempotent demo seed (profile cenius / cenius, 16x16 pattern, colourways, fold plan, exports).
"$PY" tesseract_atlas.py demo

cat <<'DONE'

==> Setup complete. Nothing is running in the foreground.
    Start using the tool:
      python3 tesseract_atlas.py --help
      python3 tesseract_atlas.py status
      python3 tesseract_atlas.py show spiral-tessellation
      bash demo.sh            # recorded end-to-end demo
      python3 -m unittest discover -s tests
    Demo profile: cenius / cenius   (also cenius@cenius.ai)
    Print files from the seed live in demo_out/
DONE

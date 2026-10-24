# Installing TesseractAtlas

## Prerequisites (install these yourself; the install script does not)

* **Python 3.9 or newer** with the standard library. Check with `python3 --version`.
  On Debian/Ubuntu: `sudo apt-get install python3` (system prerequisite, done by you,
  not by `install.sh`).
* A terminal at least 100 columns wide (grids render with rulers at that width).
* Nothing else. No database, no service, no network access, no API keys.

## Step 1 - install (installs, seeds, exits)

```bash
bash install.sh
```

What it does, in order:

1. `pip install --upgrade pip setuptools wheel` (tooling only; skipped gracefully offline).
2. `pip install -e .` so the optional `tesseract-atlas` console script lands on `PATH`.
   If that fails (restricted site-packages, offline), the script warns and continues -
   the tool itself is a single file with no dependencies.
3. `python3 -c 'import tesseract_atlas'` as a cheap import self-check.
4. `python3 tesseract_atlas.py demo` - idempotent seed: demo profile `cenius` /
   `cenius` (also `cenius@cenius.ai`), a 16x16 "Spiral Tessellation" pattern, four
   colourways with six dye lots, a six-face fold plan, and both print exports in
   `demo_out/`.

The script exits. It never starts a long-running process, so it is safe to run again.

## Step 2 - run the tool (separate command)

```bash
python3 tesseract_atlas.py --help
python3 tesseract_atlas.py status
python3 tesseract_atlas.py list
python3 tesseract_atlas.py show spiral-tessellation --rows 16
```

No environment variables are required and none are read: there is no server, no
session secret and no datastore URL. State is a relative JSON file at
`.tesseractatlas/state.json` (override with `--state-dir DIR`).

## Step 3 - the recorded demo (optional)

```bash
bash demo.sh
```

Non-interactive, safe to re-run, exits 0, and writes only into `.demo_state/` and
`demo_out/`.

## Step 4 - tests

```bash
python3 -m unittest discover -s tests
```

The suite is standard-library `unittest`; no network, no fixtures outside `tempfile`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `the state file is not valid JSON: .../state.json` | Re-run the same command with `--reset-state` (archives the broken file as `state.json.corrupt`). |
| `not signed in` | `python3 tesseract_atlas.py login --name <profile>` (or `login --demo`). |
| Forgot a passphrase | Profiles are local only: delete `.tesseractatlas/state.json` (or run `--reset-state`) and sign up again. |
| `tesseract-atlas: command not found` | Use `python3 tesseract_atlas.py ...`, or re-run `bash install.sh`. |
| Output wraps oddly | Widen the terminal to 100+ columns. |

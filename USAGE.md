# Using TesseractAtlas

Every command runs as `python3 tesseract_atlas.py <command> ...` (or `tesseract-atlas
<command> ...` after `bash install.sh`). Global flags work before or after the
command: `--state-dir DIR`, `--reset-state`, `--version`.

Exit codes: `0` success, `1` a validation/auth/data error (message on stderr, nothing
half-written), `2` a bad command line.

## 1. Local profiles

```bash
python3 tesseract_atlas.py signup --name ana --passphrase 'teal-lattice-7' --confirm 'teal-lattice-7'
python3 tesseract_atlas.py login  --name ana --passphrase 'teal-lattice-7'
python3 tesseract_atlas.py status
python3 tesseract_atlas.py logout
```

* Passphrases are stored only as `hashlib.scrypt` hashes (n=16384, r=8, p=1, 32-byte
  digest) with a fresh 16-byte random salt per profile.
* A new passphrase needs at least 8 characters - the message says how long yours was.
* Unknown profile and wrong passphrase produce the **same** one-line message and leave
  the active profile untouched.
* With more than one profile, `login` without `--name` prints the profile picker.
* `signup`, `login` and `logout` write/clear the active-profile marker in the state
  file; every other command requires it.
* Recovery is local: delete `.tesseractatlas/state.json` (or run any command with
  `--reset-state`, which archives the old file instead of deleting it).

## 2. Import a grid

```bash
python3 tesseract_atlas.py import examples/sample.stitch.csv
python3 tesseract_atlas.py import my-grid.txt --name "Spring Lattice"
python3 tesseract_atlas.py import my-grid.csv --delimiter semicolon --dry-run
```

Grid file format:

```
# A = Teal
# B = Coral
A,B,C,D
B,C,D,A
```

* `#` lines are legend lines (`# SYMBOL = Colourway name`, `=` or `:`) or plain comments.
* Delimiters are sniffed across comma, semicolon and tab, then whitespace, then a
  fixed-width character grid (`AABC` per line). Force one with
  `--delimiter comma|semicolon|tab|whitespace`.
* Every data row must have the same width. A mismatch names the row, the expected
  width and the found width and saves **nothing**.
* Symbols are single characters. Characters used in the grid but missing from the
  legend are auto-assigned (`x -> Auto colour 1`) and listed in the report.
* Limits: 400x400 cells, 64 distinct symbols.
* `--dry-run` prints the full report (dimensions, delimiter, symbol set, legend)
  without writing anything.

## 3. The library

```bash
python3 tesseract_atlas.py list
python3 tesseract_atlas.py open spring        # id or unique name prefix
python3 tesseract_atlas.py rename <id> "Summer Lattice"
python3 tesseract_atlas.py delete <id> --confirm "Summer Lattice"
```

`list` shows id, name, width x height, distinct symbol count and last-modified time,
newest first. An empty library names the exact `import` command. An ambiguous name
prefix lists every candidate id and opens nothing. Destructive commands need the
name typed back (`--confirm`; prompted when run on a TTY).

## 4. Reading the grid

```bash
python3 tesseract_atlas.py show <id> --rows 20
python3 tesseract_atlas.py show <id> --page 2          # 120-row pattern, 60 rows/page
python3 tesseract_atlas.py show <id> --cell 3,4
```

The viewer prints a column ruler (numbered at column 1 and every 5), the right-aligned
row numbers, a legend with colourway, brand, code, dye lots and total stock, a
`stock warning: N symbols have no stock (C)` line when a used symbol has no colourway,
no lots or zero quantity, and a one-line hint for the fold command. `--cell R,C`
(1-based) probes one cell: symbol, colourway and its dye lots with notes and dates.

## 5. Cell touch-ups

```bash
python3 tesseract_atlas.py edit <id> --cell 3,4=B
python3 tesseract_atlas.py edit <id> --rect 2,2,6,6=C
python3 tesseract_atlas.py edit <id> --cell 1,1=K --add-symbol K=Ochre
python3 tesseract_atlas.py edit <id> --cell 3,4=B --dry-run
python3 tesseract_atlas.py edit <id> --undo
```

* One `old -> new` line per changed cell, then the affected rows re-rendered.
* A rectangle prints `applying N changes...` before the diff.
* `--add-symbol SYM=ColourwayName` extends the palette inline; the new symbol shows up
  in the legend on the next `show`.
* The last 20 edits persist in the state file; `--undo` reverts the most recent one and
  reports which edit was reverted.
* `--dry-run` prints the same change lines, leaves `updated_at` alone and adds no
  history entry. Cells already holding the target symbol are not counted as changes.

## 6. Colourway inventory and dye lots

```bash
python3 tesseract_atlas.py colors list
python3 tesseract_atlas.py colors add --name Teal --brand Malabrigo --code '#2f6f6a'
python3 tesseract_atlas.py colors lot-add Teal --code D-118 --quantity 12 --unit g \
    --note "slightly dark" --date 2024-04-02
python3 tesseract_atlas.py colors lot-delete Teal --code D-118 --confirm D-118
python3 tesseract_atlas.py colors delete Teal --confirm Teal
```

`colors list` prints one block per colourway: name, brand, code, lot count, total stock
per unit, every lot with quantity, unit, acquired date and note, and which patterns use
it. Units are stored exactly as typed (no gram/packet conversion). Duplicate names,
malformed quantities and unknown colourways are rejected with a one-line reason.
Deleting warns when patterns still reference the colourway.

## 7. Folding the grid into a net

```bash
python3 tesseract_atlas.py fold set <id> r4 r8 c8
python3 tesseract_atlas.py fold preview <id>
```

`rN` is a horizontal fold line after row N, `cN` a vertical one after column N. The
plan slices the grid into regions and labels them A-F (at most six cube faces), prints
the face-to-row/column range table and an ASCII isometric face stack. Creases outside
the grid, duplicate creases (two faces claiming one region) or plans with more than six
regions print a warning and are **not** saved. Saved plans are reused by `show` and
both exporters.

## 8. Print-friendly exports

```bash
python3 tesseract_atlas.py export txt  <id> --out-dir demo_out
python3 tesseract_atlas.py export html <id> --out-dir demo_out
python3 tesseract_atlas.py export list <id>
```

* `PATTERN.stitchmap.txt` - header (pattern, size, symbol count, palette, fold plan),
  ruler-prefixed grid bands, legend table and a dye-lot table with quantities and notes.
* `PATTERN.stitchmap.html` - one labelled table per column band (`columns 1-60`,
  `columns 61-120`, ...), the legend repeated in every band, a `<style>` block with
  `@media print` (A4 page, black on white, page break per band) and **no** URLs, so it
  prints offline.
* Both are refused when the file already exists - add `--force` to overwrite.
* Exports are byte-for-byte deterministic for the same pattern and fold plan.

## 9. One-command demo

```bash
python3 tesseract_atlas.py demo            # state in .tesseractatlas/, files in demo_out/
bash demo.sh                               # the recorded end-to-end transcript
```

The demo creates (or reuses) the `cenius` profile, four colourways with six dye lots, a
16x16 pattern with a six-face fold plan, and both exports - re-running it never
duplicates profiles, colourways, lots or patterns.

## State file

```json
{
  "version": 1,
  "active_profile_id": "u_1a2b3c4d",
  "profiles": [{"id": "...", "name": "ana", "salt": "...", "passphrase_hash": "...", "kdf": "scrypt n=16384,r=8,p=1"}],
  "patterns": [{"id": "p_...", "width": 16, "height": 16, "rows": ["..."], "palette": {"A": "Teal"}}],
  "colorways": [], "dye_lots": [], "fold_plans": [], "exports": []
}
```

No plaintext passphrase is ever written. A corrupt or unreadable file is reported with
its path plus the `--reset-state` recovery flag instead of a traceback.

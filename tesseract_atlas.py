#!/usr/bin/env python3
"""TesseractAtlas - a virtual pattern-folding grid for beadwork.

Single-file, standard-library-only command line tool:

* import a beaded pattern from a plain-text or CSV grid
* touch up single cells or rectangles, with a persisted undo
* preview the grid as a foldable net mapped onto up to six cube faces
* keep a colourway inventory with dye-lot notes and stock counts
* export a print-friendly stitch map as fixed-width .txt and print-CSS .html

Everything the tool remembers lives in ``<state-dir>/state.json`` and is
written atomically (temp file plus ``os.replace``).  No network calls, no
third-party packages.  Run ``python3 tesseract_atlas.py --help``.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import getpass
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

VERSION = "1.0.0"
PROG = "tesseract_atlas.py"
DEFAULT_STATE_DIR = ".tesseractatlas"
STATE_VERSION = 1

MAX_GRID_SIDE = 400
MAX_SYMBOLS = 64
DEFAULT_ROWS_PER_PAGE = 60
DEFAULT_BAND_COLUMNS = 60
EDIT_HISTORY_LIMIT = 20
BLANK_SYMBOL = "."

MIN_PASSPHRASE = 8
SALT_BYTES = 16
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
KDF_LABEL = "scrypt n=16384,r=8,p=1"

DEMO_PROFILE_NAME = "cenius"
DEMO_PROFILE_EMAIL = "cenius@cenius.ai"
DEMO_PASSPHRASE = "cenius"
DEMO_PATTERN_NAME = "Spiral Tessellation"

EXIT_OK = 0
EXIT_ERROR = 1

RESET_HINT = "re-run the same command with --reset-state to archive the broken file and start fresh"

# Committed design tokens (calm-precise, terminal dark, one accent).
ACCENT_HEX = "#6872c0"
ACCENT_OKLCH = "oklch(0.58 0.12 276)"
MONO_STACK = 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", "Courier New", monospace'


# ---------------------------------------------------------------------------
# Output helpers and errors
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """A user-facing failure: message to stderr, non-zero exit, no traceback."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def write(text: str = "", stream: Any = None) -> None:
    """Print a line of human output to stdout (or the given stream)."""
    print(text, file=stream if stream is not None else sys.stdout)


def warn(text: str) -> None:
    """Diagnostics and warnings go to stderr so stdout stays pipeable."""
    print(text, file=sys.stderr)


def state_path(state_dir: Path) -> Path:
    return Path(state_dir) / "state.json"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


def today_iso() -> str:
    return datetime.date.today().isoformat()


def days_ago_iso(days: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def display_time(value: str) -> str:
    try:
        stamp = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return str(value)
    return stamp.strftime("%Y-%m-%d %H:%M")


def valid_date_iso(value: str) -> bool:
    try:
        datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------
# Atomic JSON state store
# ---------------------------------------------------------------------------


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "active_profile_id": None,
        "profiles": [],
        "patterns": [],
        "colorways": [],
        "dye_lots": [],
        "fold_plans": [],
        "exports": [],
    }


_STATE_LISTS = ("profiles", "patterns", "colorways", "dye_lots", "fold_plans", "exports")


def normalise_state(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ToolError(f"the state file does not contain a JSON object: {path}", RESET_HINT)
    state = empty_state()
    for key, value in data.items():
        state[key] = value
    for key in _STATE_LISTS:
        if not isinstance(state.get(key), list):
            raise ToolError(f"the state file is missing its '{key}' list: {path}", RESET_HINT)
    active = state.get("active_profile_id")
    if active is not None and not isinstance(active, str):
        raise ToolError(f"the state file has a damaged active profile marker: {path}", RESET_HINT)
    state["version"] = STATE_VERSION
    return state


def archive_state_file(path: Path) -> Path:
    index = 1
    while True:
        suffix = ".corrupt" if index == 1 else f".corrupt.{index}"
        candidate = path.with_name(path.name + suffix)
        if not candidate.exists():
            break
        index += 1
    os.replace(path, candidate)
    return candidate


def load_state(state_dir: Path, reset: bool = False) -> dict[str, Any]:
    state_dir = Path(state_dir)
    path = state_path(state_dir)
    if reset and path.exists():
        archived = archive_state_file(path)
        warn(f"reset: archived the previous state file to {archived}")
    if not path.exists():
        return empty_state()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"cannot read the state file {path}: {exc.strerror or exc}", RESET_HINT)
    if not raw.strip():
        raise ToolError(f"the state file is empty: {path}", RESET_HINT)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"the state file is not valid JSON: {path} ({exc.msg}, line {exc.lineno} column {exc.colno})",
            RESET_HINT,
        )
    return normalise_state(data, path)


def save_state(state_dir: Path, state: dict[str, Any]) -> None:
    state_dir = Path(state_dir)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolError(f"cannot create the state directory {state_dir}: {exc.strerror or exc}")
    path = state_path(state_dir)
    payload = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
    handle_fd, tmp_name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=str(state_dir))
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except OSError as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise ToolError(f"cannot write the state file {path}: {exc.strerror or exc}")


# ---------------------------------------------------------------------------
# Local passphrase profiles (scrypt)
# ---------------------------------------------------------------------------


def hash_passphrase(passphrase: str, salt_hex: str) -> str:
    digest = hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=bytes.fromhex(salt_hex),
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return digest.hex()


def verify_passphrase(profile: dict[str, Any], passphrase: str) -> bool:
    try:
        salt = profile["salt"]
        expected = profile["passphrase_hash"]
    except KeyError:
        return False
    candidate = hash_passphrase(passphrase, salt)
    return hmac.compare_digest(candidate, expected)


def find_profile(state: dict[str, Any], needle: str) -> dict[str, Any] | None:
    needle = (needle or "").strip().lower()
    if not needle:
        return None
    for profile in state["profiles"]:
        if profile["name"].lower() == needle:
            return profile
    for profile in state["profiles"]:
        if str(profile.get("email", "")).lower() == needle:
            return profile
    return None


def active_profile(state: dict[str, Any]) -> dict[str, Any] | None:
    active_id = state.get("active_profile_id")
    if not active_id:
        return None
    for profile in state["profiles"]:
        if profile["id"] == active_id:
            return profile
    return None


def login_command_hint() -> str:
    return f"sign in with: python3 {PROG} login --name <profile>"


def require_profile(state: dict[str, Any]) -> dict[str, Any]:
    profile = active_profile(state)
    if profile is not None:
        return profile
    if not state["profiles"]:
        raise ToolError(
            "not signed in and no profiles exist on this machine",
            f"create a profile with: python3 {PROG} signup --name <profile>",
        )
    raise ToolError("not signed in", login_command_hint())


def activate_profile(state: dict[str, Any], profile: dict[str, Any]) -> None:
    state["active_profile_id"] = profile["id"]
    for candidate in state["profiles"]:
        candidate["active"] = candidate["id"] == profile["id"]


def clear_active_profile(state: dict[str, Any]) -> None:
    state["active_profile_id"] = None
    for candidate in state["profiles"]:
        candidate["active"] = False


_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{1,31}$")


def create_profile(
    state: dict[str, Any],
    name: str,
    passphrase: str,
    confirm: str,
    email: str | None = None,
    enforce_strength: bool = True,
) -> dict[str, Any]:
    cleaned = (name or "").strip()
    if not _PROFILE_NAME_RE.match(cleaned):
        raise ToolError(
            "a profile name needs 2-32 characters: letters, digits, dot, dash or underscore",
            "for example: --name ana",
        )
    existing = find_profile(state, cleaned) or (find_profile(state, email) if email else None)
    if existing is not None:
        raise ToolError(
            f"the profile '{cleaned}' already exists",
            f"sign in with: python3 {PROG} login --name {existing['name']}",
        )
    if enforce_strength and len(passphrase) < MIN_PASSPHRASE:
        raise ToolError(
            f"passphrase must be at least {MIN_PASSPHRASE} characters (got {len(passphrase)}); "
            "use a short phrase you can retype",
            "nothing was written",
        )
    if passphrase != confirm:
        raise ToolError("the two passphrases do not match", "nothing was written")
    salt = secrets.token_hex(SALT_BYTES)
    profile = {
        "id": "u_" + secrets.token_hex(4),
        "name": cleaned,
        "email": email or "",
        "salt": salt,
        "passphrase_hash": hash_passphrase(passphrase, salt),
        "kdf": KDF_LABEL,
        "created_at": now_iso(),
        "active": False,
    }
    state["profiles"].append(profile)
    return profile


def read_passphrase(args: argparse.Namespace) -> str:
    value = getattr(args, "passphrase", None)
    if value is not None:
        return value
    if sys.stdin.isatty():
        return getpass.getpass("passphrase: ")
    raise ToolError("passphrase required", "re-run with --passphrase <phrase> for non-interactive use")


def read_new_passphrase(args: argparse.Namespace) -> tuple[str, str]:
    first = getattr(args, "passphrase", None)
    second = getattr(args, "confirm", None)
    if first is None:
        if not sys.stdin.isatty():
            raise ToolError(
                "passphrase required",
                "re-run with --passphrase <phrase> --confirm <phrase> for non-interactive use",
            )
        first = getpass.getpass("passphrase: ")
        if second is None:
            second = getpass.getpass("repeat passphrase: ")
        return first, second
    if second is None:
        if sys.stdin.isatty():
            second = getpass.getpass("repeat passphrase: ")
        else:
            raise ToolError("confirmation required", "re-run with --confirm <phrase>")
    return first, second


def read_confirmation(provided: str | None, expected: str, action: str) -> bool:
    if provided is None:
        if not sys.stdin.isatty():
            raise ToolError(
                f"{action} needs a typed confirmation",
                f're-run with --confirm "{expected}"',
            )
        provided = input(f'type "{expected}" to confirm: ')
    return provided.strip() == expected.strip()


# ---------------------------------------------------------------------------
# Pattern and inventory lookups
# ---------------------------------------------------------------------------


def new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(4)


def patterns_for_profile(state: dict[str, Any], profile_id: str) -> list[dict[str, Any]]:
    return [p for p in state["patterns"] if p["profile_id"] == profile_id]


def find_pattern(state: dict[str, Any], profile_id: str, reference: str) -> dict[str, Any]:
    reference = (reference or "").strip()
    patterns = patterns_for_profile(state, profile_id)
    if not reference:
        raise ToolError("no pattern given", f"list them with: python3 {PROG} list")
    for pattern in patterns:
        if pattern["id"] == reference:
            return pattern
    exact = [p for p in patterns if p["name"].lower() == reference.lower()]
    if len(exact) == 1:
        return exact[0]
    prefix = [p for p in patterns if p["name"].lower().startswith(reference.lower())]
    if len(exact) > 1:
        prefix = exact
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        candidates = "\n".join(f"  {p['id']}  {p['name']}" for p in prefix)
        raise ToolError(
            f"the name prefix '{reference}' matches {len(prefix)} patterns:\n{candidates}",
            "use a full pattern id or a longer prefix",
        )
    raise ToolError(
        f"no pattern matches '{reference}'",
        f"list stored patterns with: python3 {PROG} list",
    )


def symbols_in_order(pattern: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for row in pattern["rows"]:
        for symbol in row:
            if symbol not in seen:
                seen.append(symbol)
    return seen


def distinct_symbol_count(pattern: dict[str, Any]) -> int:
    return len(symbols_in_order(pattern))


def find_colorway(state: dict[str, Any], profile_id: str, name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    needle = name.strip().lower()
    for colorway in state["colorways"]:
        if colorway["profile_id"] == profile_id and colorway["name"].lower() == needle:
            return colorway
    return None


def lots_for(state: dict[str, Any], colorway_id: str) -> list[dict[str, Any]]:
    return [lot for lot in state["dye_lots"] if lot["colorway_id"] == colorway_id]


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    if count == 1:
        return f"{count} {singular}"
    return f"{count} {plural_form or singular + 's'}"


def format_qty(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def stock_by_unit(lots: Sequence[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for lot in lots:
        unit = lot.get("unit") or "unit"
        totals[unit] = totals.get(unit, 0.0) + float(lot.get("quantity", 0.0))
    return totals


def format_stock(lots: Sequence[dict[str, Any]]) -> str:
    totals = stock_by_unit(lots)
    if not totals:
        return ""
    parts = [f"{format_qty(quantity)} {unit}" for unit, quantity in totals.items()]
    return " + ".join(parts)


def has_stock(lots: Sequence[dict[str, Any]]) -> bool:
    return any(float(lot.get("quantity", 0.0)) > 0 for lot in lots)


def lots_summary(lots: Sequence[dict[str, Any]], limit: int = 46) -> str:
    if not lots:
        return "-"
    parts = []
    for lot in lots:
        text = f"{lot['lot_code']} {format_qty(float(lot['quantity']))} {lot['unit']}"
        if lot.get("note"):
            text += f" ({lot['note']})"
        parts.append(text)
    joined = "; ".join(parts)
    if len(joined) > limit:
        return joined[: limit - 3] + "..."
    return joined


# ---------------------------------------------------------------------------
# Grid import: legend parsing, delimiter sniffing, row validation
# ---------------------------------------------------------------------------


LEGEND_RE = re.compile(r"^#\s*(\S)\s*[=:]\s*(\S.*?)\s*$")
_DELIMITER_LABELS = {",": "comma (,)", ";": "semicolon (;)", "\t": "tab"}


def _tokenise(lines: Sequence[str], delimiter: str) -> list[list[str]]:
    if delimiter == "whitespace":
        return [line.split() for line in lines]
    reader = csv.reader(list(lines), delimiter=delimiter)
    return [row for row in reader]


def _consistent_width(rows: Sequence[Sequence[str]]) -> bool:
    if not rows:
        return False
    width = len(rows[0])
    return width >= 1 and all(len(row) == width for row in rows)


def detect_delimiter(data_lines: Sequence[str]) -> tuple[str, str, list[list[str]]]:
    """Return (delimiter, human label, token rows) using sniffing plus a fallback chain."""
    sniffed: list[str] = []
    sample = "\n".join(data_lines[:24])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        sniffed.append(dialect.delimiter)
    except csv.Error:
        pass
    ordered: list[str] = []
    for candidate in sniffed + [",", ";", "\t"]:
        if candidate not in ordered:
            ordered.append(candidate)
    for delimiter in ordered:
        rows = _tokenise(data_lines, delimiter)
        if _consistent_width(rows) and len(rows[0]) >= 2:
            return delimiter, _DELIMITER_LABELS[delimiter], rows
    rows = _tokenise(data_lines, "whitespace")
    if _consistent_width(rows) and len(rows[0]) >= 2:
        return "whitespace", "whitespace", rows
    lengths = {len(line) for line in data_lines}
    if len(lengths) == 1 and not any(ch in line for line in data_lines for ch in ",;\t"):
        width = lengths.pop()
        if width >= 1:
            return "chars", "characters (no separator)", [list(line) for line in data_lines]
    if rows and all(len(row) == 1 for row in rows):
        return "whitespace", "single column", rows
    raise ToolError(
        "cannot detect the grid layout: the data rows do not share one delimiter",
        "pass --delimiter comma|semicolon|tab|whitespace, or check the file for ragged rows",
    )


class GridParse:
    """Parsed grid plus the report the import command prints."""

    def __init__(
        self,
        rows: list[list[str]],
        palette: dict[str, str],
        auto_assigned: list[tuple[str, str]],
        delimiter_label: str,
        comments: int,
    ) -> None:
        self.rows = rows
        self.palette = palette
        self.auto_assigned = auto_assigned
        self.delimiter_label = delimiter_label
        self.comments = comments

    @property
    def width(self) -> int:
        return len(self.rows[0]) if self.rows else 0

    @property
    def height(self) -> int:
        return len(self.rows)

    @property
    def symbols(self) -> list[str]:
        seen: list[str] = []
        for row in self.rows:
            for symbol in row:
                if symbol not in seen:
                    seen.append(symbol)
        return seen


def parse_grid(text: str, delimiter_choice: str | None = None) -> GridParse:
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    legend: dict[str, str] = {}
    comments = 0
    data_lines: list[str] = []
    for raw in raw_lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            match = LEGEND_RE.match(line.strip())
            if match:
                legend[match.group(1)] = match.group(2).strip()
            else:
                comments += 1
            continue
        data_lines.append(line)
    if not data_lines:
        raise ToolError(
            "no grid rows found in that file",
            "grid rows are lines of symbols; legend lines start with '#', e.g. '# A = Teal'",
        )
    if delimiter_choice:
        delimiter = {"comma": ",", "semicolon": ";", "tab": "\t", "whitespace": "whitespace"}[delimiter_choice]
        rows = _tokenise(data_lines, delimiter)
        label = _DELIMITER_LABELS.get(delimiter, "whitespace")
    else:
        delimiter, label, rows = detect_delimiter(data_lines)

    cleaned: list[list[str]] = []
    for index, row in enumerate(rows, start=1):
        tokens = [token.strip() for token in row]
        for column, token in enumerate(tokens, start=1):
            if token == "":
                raise ToolError(
                    f"row {index} column {column} is empty; every cell needs a symbol character",
                    "nothing was saved",
                )
            if len(token) != 1:
                raise ToolError(
                    f"row {index} column {column}: the symbol '{token}' is {len(token)} characters; "
                    "symbols must be a single character",
                    "nothing was saved",
                )
        cleaned.append(tokens)

    expected = len(cleaned[0])
    for index, row in enumerate(cleaned, start=1):
        if len(row) != expected:
            raise ToolError(
                f"row {index}: expected {expected} symbols, found {len(row)}",
                "nothing was saved; fix that row and run the import again",
            )

    height = len(cleaned)
    width = expected
    if width > MAX_GRID_SIDE or height > MAX_GRID_SIDE:
        raise ToolError(
            f"the grid is {width} x {height}; the limit is {MAX_GRID_SIDE} x {MAX_GRID_SIDE} cells",
            "nothing was saved",
        )

    seen_symbols: list[str] = []
    for row in cleaned:
        for symbol in row:
            if symbol not in seen_symbols:
                seen_symbols.append(symbol)
    if len(seen_symbols) > MAX_SYMBOLS:
        raise ToolError(
            f"the grid uses {len(seen_symbols)} distinct symbols; the limit is {MAX_SYMBOLS}",
            "nothing was saved",
        )

    palette = {symbol: legend[symbol] for symbol in seen_symbols if symbol in legend}
    auto_assigned: list[tuple[str, str]] = []
    for symbol in seen_symbols:
        if symbol in palette:
            continue
        placeholder = f"Auto colour {len(auto_assigned) + 1}"
        palette[symbol] = placeholder
        auto_assigned.append((symbol, placeholder))

    return GridParse(cleaned, palette, auto_assigned, label, comments)


# ---------------------------------------------------------------------------
# Rendering: rulers, grid rows, legend
# ---------------------------------------------------------------------------


def render_ruler(width: int, prefix_width: int) -> str:
    cells = [" "] * (prefix_width + width * 2)
    for column in range(1, width + 1):
        if column != 1 and column % 5 != 0:
            continue
        label = str(column)
        end = prefix_width + (column - 1) * 2
        start = end - len(label) + 1
        if start < 0:
            continue
        for offset, char in enumerate(label):
            if start + offset < len(cells):
                cells[start + offset] = char
    return "".join(cells).rstrip()


def render_border(width: int, prefix_width: int) -> str:
    return "-" * (prefix_width + 1) + "+" + "-" * max(width * 2 - 1, 1)


def render_row(row: str, row_number: int, prefix_width: int) -> str:
    return f"{row_number:>{prefix_width}}|" + " ".join(row)


def render_grid_block(pattern: dict[str, Any], first_row: int, last_row: int) -> list[str]:
    width = pattern["width"]
    prefix_width = max(3, len(str(pattern["height"])))
    lines = [render_ruler(width, prefix_width + 1), render_border(width, prefix_width)]
    for row_number in range(first_row, last_row + 1):
        lines.append(render_row(pattern["rows"][row_number - 1], row_number, prefix_width))
    lines.append(render_border(width, prefix_width))
    return lines


def legend_entries(state: dict[str, Any], profile_id: str, pattern: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for symbol in symbols_in_order(pattern):
        name = pattern["palette"].get(symbol, "")
        colorway = find_colorway(state, profile_id, name)
        lots = lots_for(state, colorway["id"]) if colorway else []
        if colorway is None:
            status = "no colourway"
            stock = "-"
        elif not lots:
            status = "no lots"
            stock = "-"
        elif not has_stock(lots):
            status = "zero stock"
            stock = format_stock(lots) or "0"
        else:
            status = "in stock"
            stock = format_stock(lots)
        entries.append(
            {
                "symbol": symbol,
                "colourway": colorway["name"] if colorway else (name or "(no colourway)"),
                "brand": colorway.get("brand", "") if colorway else "",
                "code": colorway.get("code", "") if colorway else "",
                "lots": lots,
                "lots_text": lots_summary(lots),
                "stock": stock,
                "status": status,
                "ok": status == "in stock",
            }
        )
    return entries


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], indent: str = "  ") -> list[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [indent + "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)).rstrip()]
    lines.append(indent + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append(indent + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return lines


def render_legend_block(state: dict[str, Any], profile_id: str, pattern: dict[str, Any]) -> list[str]:
    entries = legend_entries(state, profile_id, pattern)
    headers = ["sym", "colourway", "brand", "code", "lots", "stock"]
    rows = [
        [entry["symbol"], entry["colourway"], entry["brand"] or "-", entry["code"] or "-", entry["lots_text"], entry["stock"]]
        for entry in entries
    ]
    lines = ["legend (symbol -> colourway and dye-lot stock)"] + _table(headers, rows)
    warnings = [entry for entry in entries if not entry["ok"]]
    if warnings:
        symbols = ", ".join(entry["symbol"] for entry in warnings)
        verb = "has" if len(warnings) == 1 else "have"
        lines.append(f"stock warning: {plural(len(warnings), 'symbol')} {verb} no stock ({symbols})")
    return lines


def pattern_is_blank(pattern: dict[str, Any]) -> bool:
    return all(set(row) <= {BLANK_SYMBOL} for row in pattern["rows"])


def fold_plan_for(state: dict[str, Any], pattern: dict[str, Any]) -> dict[str, Any] | None:
    plan_id = pattern.get("fold_plan_id")
    if not plan_id:
        return None
    for plan in state["fold_plans"]:
        if plan["id"] == plan_id:
            return plan
    return None


def fold_hint(state: dict[str, Any], pattern: dict[str, Any]) -> str:
    plan = fold_plan_for(state, pattern)
    if plan:
        creases = ", ".join(plan["creases"])
        count = len(plan["creases"])
        return (
            f"fold plan: {count} crease{'s' if count != 1 else ''} ({creases}) - "
            f"preview the net with: python3 {PROG} fold preview {pattern['id']}"
        )
    return (
        "fold hint: crease this grid into a cube net with "
        f"python3 {PROG} fold set {pattern['id']} r4 r8 (horizontal) and c4 (vertical)"
    )


# ---------------------------------------------------------------------------
# Fold plans: creases, regions, faces and the isometric net preview
# ---------------------------------------------------------------------------


CREASE_RE = re.compile(r"^([rc])(\d+)$")


def parse_crease(token: str) -> tuple[str, int]:
    match = CREASE_RE.match((token or "").strip().lower())
    if not match:
        raise ToolError(
            f"'{token}' is not a crease line",
            "use rN for a horizontal fold line and cN for a vertical one, e.g. r4 or c8",
        )
    return match.group(1), int(match.group(2))


def band_bounds(length: int, creases: Sequence[int]) -> list[tuple[int, int]]:
    bounds: list[tuple[int, int]] = []
    start = 1
    for crease in sorted(creases):
        if crease < start:
            continue
        bounds.append((start, crease))
        start = crease + 1
    bounds.append((start, length))
    return bounds


FACE_LABELS = "ABCDEF"


def build_faces(creases: Sequence[str], width: int, height: int) -> dict[str, dict[str, int]]:
    row_creases = sorted({parse_crease(token)[1] for token in creases if parse_crease(token)[0] == "r"})
    col_creases = sorted({parse_crease(token)[1] for token in creases if parse_crease(token)[0] == "c"})
    faces: dict[str, dict[str, int]] = {}
    index = 0
    for row_start, row_end in band_bounds(height, row_creases):
        for col_start, col_end in band_bounds(width, col_creases):
            if index >= len(FACE_LABELS):
                return faces
            faces[FACE_LABELS[index]] = {
                "row_start": row_start,
                "row_end": row_end,
                "col_start": col_start,
                "col_end": col_end,
            }
            index += 1
    return faces


def face_block(label: str, face: dict[str, int]) -> list[str]:
    rows_label = f"rows {face['row_start']}-{face['row_end']}"
    cols_label = f"cols {face['col_start']}-{face['col_end']}"
    cell_count = (face["row_end"] - face["row_start"] + 1) * (face["col_end"] - face["col_start"] + 1)
    lines = [
        "  +" + "-" * 20 + "+",
        " /{:<20}/|".format(f"{label}  {rows_label}"),
        "+" + "-" * 20 + "+ |",
        f"| {cols_label:<20} | +",
        "| {:<20} |/".format(f"{cell_count} cells"),
        "+" + "-" * 20 + "+",
    ]
    width = max(len(line) for line in lines)
    return [line.ljust(width) for line in lines]


def render_face_sketch(faces: dict[str, dict[str, int]]) -> list[str]:
    blocks = [face_block(label, face) for label, face in faces.items()]
    lines = ["isometric net preview (faces stack left to right, three per row)"]
    for start in range(0, len(blocks), 3):
        group = blocks[start : start + 3]
        for index in range(6):
            lines.append("  " + "  ".join(block[index] for block in group).rstrip())
    return lines


def render_face_table(faces: dict[str, dict[str, int]]) -> list[str]:
    rows = []
    for label, face in faces.items():
        cells = (face["row_end"] - face["row_start"] + 1) * (face["col_end"] - face["col_start"] + 1)
        rows.append(
            [
                label,
                f"{face['row_start']}-{face['row_end']}",
                f"{face['col_start']}-{face['col_end']}",
                str(cells),
            ]
        )
    return _table(["face", "rows", "columns", "cells"], rows)


def crease_warnings(creases: Sequence[str], width: int, height: int) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for token in creases:
        kind, number = parse_crease(token)
        key = f"{kind}{number}"
        if key in seen:
            problems.append(f"crease {key} is listed twice; two faces would claim the same region")
            continue
        seen.add(key)
        if kind == "r":
            if number < 1 or number > height - 1:
                problems.append(
                    f"fold line r{number} is outside the {height}-row grid (valid: r1-r{max(height - 1, 1)})"
                )
        else:
            if number < 1 or number > width - 1:
                problems.append(
                    f"fold line c{number} is outside the {width}-column grid (valid: c1-c{max(width - 1, 1)})"
                )
    return problems


# ---------------------------------------------------------------------------
# Print exports: fixed-width .txt and print-CSS .html
# ---------------------------------------------------------------------------


def slugify(text: str, fallback: str = "pattern") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or fallback


def band_ranges(width: int, band_columns: int = DEFAULT_BAND_COLUMNS) -> list[tuple[int, int]]:
    bands: list[tuple[int, int]] = []
    start = 1
    while start <= width:
        end = min(start + band_columns - 1, width)
        bands.append((start, end))
        start = end + 1
    return bands or [(1, 1)]


def export_filename(pattern: dict[str, Any], suffix: str) -> str:
    return f"{slugify(pattern['name'], pattern['id'])}.stitchmap{suffix}"


def _grid_section(pattern: dict[str, Any], col_start: int, col_end: int) -> list[str]:
    width = col_end - col_start + 1
    prefix_width = max(3, len(str(pattern["height"]))) + 1
    cells = [" "] * (prefix_width + width * 2)
    for column in range(col_start, col_end + 1):
        if column != col_start and column % 5 != 0:
            continue
        label = str(column)
        end = prefix_width + (column - col_start) * 2
        start = end - len(label) + 1
        for offset, char in enumerate(label):
            if 0 <= start + offset < len(cells):
                cells[start + offset] = char
    lines = ["".join(cells).rstrip()]
    lines.append("-" * (prefix_width + 1) + "+" + "-" * max(width * 2 - 1, 1))
    for row_number in range(1, pattern["height"] + 1):
        segment = pattern["rows"][row_number - 1][col_start - 1 : col_end]
        lines.append(f"{row_number:>{prefix_width}}|" + " ".join(segment))
    lines.append("-" * (prefix_width + 1) + "+" + "-" * max(width * 2 - 1, 1))
    return lines


def build_txt_export(state: dict[str, Any], profile_id: str, pattern: dict[str, Any]) -> str:
    plan = fold_plan_for(state, pattern)
    lines: list[str] = []
    lines.append("TESSERACTATLAS STITCH MAP")
    lines.append("=" * 24)
    lines.append(f"pattern : {pattern['name']} ({pattern['id']})")
    lines.append(f"size    : {pattern['width']} columns x {pattern['height']} rows "
                 f"({pattern['width'] * pattern['height']} cells)")
    lines.append(f"symbols : {distinct_symbol_count(pattern)} distinct")
    legend_pairs = "  ".join(f"{symbol}={pattern['palette'].get(symbol, '?')}"
                             for symbol in symbols_in_order(pattern))
    lines.append(f"legend  : {legend_pairs}")
    if plan:
        lines.append(f"fold    : {len(plan['creases'])} creases ({', '.join(plan['creases'])}); "
                     f"faces {''.join(sorted(plan['faces']))}")
    else:
        lines.append("fold    : no fold plan")
    lines.append("")

    bands = band_ranges(pattern["width"])
    for band_index, (col_start, col_end) in enumerate(bands, start=1):
        lines.append(f"band {band_index} of {len(bands)}: columns {col_start}-{col_end}")
        lines.extend(_grid_section(pattern, col_start, col_end))
        if band_index != len(bands):
            lines.append("")

    lines.append("")
    lines.append("legend and dye lots")
    headers = ["sym", "colourway", "brand", "code", "stock"]
    rows = []
    for entry in legend_entries(state, profile_id, pattern):
        rows.append([entry["symbol"], entry["colourway"], entry["brand"] or "-", entry["code"] or "-", entry["stock"]])
    lines.extend(_table(headers, rows))

    lot_rows = []
    for symbol in symbols_in_order(pattern):
        colorway = find_colorway(state, profile_id, pattern["palette"].get(symbol, ""))
        if colorway is None:
            continue
        for lot in lots_for(state, colorway["id"]):
            lot_rows.append(
                [
                    colorway["name"],
                    lot["lot_code"],
                    format_qty(float(lot["quantity"])),
                    lot["unit"],
                    lot.get("acquired_on", "-"),
                    lot.get("note", ""),
                ]
            )
    lines.append("")
    lines.append("dye lots")
    if lot_rows:
        lines.extend(_table(["colourway", "lot", "qty", "unit", "acquired", "note"], lot_rows))
    else:
        lines.append("  (no dye lots recorded for the symbols in this pattern)")
    return "\n".join(lines) + "\n"


_HTML_STYLE_TEMPLATE = """
:root{
  --accent:__ACCENT_HEX__;
  --accent-oklch:__ACCENT_OKLCH__;
  --ink:#e8ebf2;
  --muted:#9aa4b2;
  --line:#2a303a;
  --surface:#101318;
  --paper:#ffffff;
  --paper-ink:#101318;
  --mono:__MONO_STACK__;
  --space-1:4px;
  --space-2:8px;
  --space-3:16px;
  --space-4:24px;
  --step-0:12px;
  --step-1:14px;
  --step-2:18px;
}
*{box-sizing:border-box;}
html{-webkit-text-size-adjust:100%;}
body{
  margin:0;
  padding:var(--space-4);
  background:var(--surface);
  color:var(--ink);
  font-family:var(--mono);
  font-size:var(--step-1);
  line-height:1.5;
}
main{max-width:1100px;margin:0 auto;}
h1{font-size:var(--step-2);margin:0 0 var(--space-2);color:var(--accent);}
h2{font-size:var(--step-1);margin:var(--space-4) 0 var(--space-2);color:var(--accent);}
p{margin:0 0 var(--space-2);color:var(--muted);}
dl.meta{display:grid;grid-template-columns:max-content 1fr;gap:var(--space-1) var(--space-3);margin:0 0 var(--space-3);}
dl.meta dt{color:var(--muted);}
dl.meta dd{margin:0;}
.band{margin:0 0 var(--space-4);}
.band-meta{font-size:var(--step-0);color:var(--muted);margin:0 0 var(--space-1);}
.band-legend{font-size:var(--step-0);color:var(--ink);border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:var(--space-1) 0;margin:0 0 var(--space-1);}
table{border-collapse:collapse;margin:0 0 var(--space-3);}
th,td{border:1px solid var(--line);padding:2px 4px;text-align:center;font-size:var(--step-0);}
th{color:var(--muted);font-weight:400;}
td.rowno{color:var(--muted);}
table.legend th,table.legend td{text-align:left;}
@media print{
  :root{--ink:#000000;--muted:#333333;--line:#000000;--surface:#ffffff;}
  @page{size:A4;margin:12mm;}
  body{background:var(--paper);color:var(--paper-ink);padding:0;font-size:10pt;}
  main{max-width:none;}
  h1,h2{color:var(--paper-ink);}
  p,dt,th,td.rowno{color:#333333;}
  .band{break-after:page;page-break-after:always;}
  .band:last-of-type{break-after:auto;page-break-after:auto;}
  table{break-inside:avoid;page-break-inside:avoid;}
  thead{display:table-header-group;}
  th,td{border:1px solid #000000;color:#000000;}
}
"""

HTML_STYLE = (
    _HTML_STYLE_TEMPLATE.replace("__ACCENT_HEX__", ACCENT_HEX)
    .replace("__ACCENT_OKLCH__", ACCENT_OKLCH)
    .replace("__MONO_STACK__", MONO_STACK)
)


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def build_html_export(state: dict[str, Any], profile_id: str, pattern: dict[str, Any]) -> tuple[str, int]:
    plan = fold_plan_for(state, pattern)
    bands = band_ranges(pattern["width"])
    legend_pairs = " &middot; ".join(
        f"{_esc(symbol)} = {_esc(pattern['palette'].get(symbol, '?'))}" for symbol in symbols_in_order(pattern)
    )
    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="en">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append(f"<title>{_esc(pattern['name'])} - stitch map</title>")
    parts.append("<style>")
    parts.append(HTML_STYLE.strip())
    parts.append("</style>")
    parts.append("</head>")
    parts.append("<body>")
    parts.append("<main>")
    parts.append(f"<h1>{_esc(pattern['name'])} &mdash; stitch map</h1>")
    parts.append('<dl class="meta">')
    parts.append(f"<dt>pattern</dt><dd>{_esc(pattern['id'])}</dd>")
    parts.append(
        f"<dt>size</dt><dd>{pattern['width']} columns &times; {pattern['height']} rows "
        f"({pattern['width'] * pattern['height']} cells)</dd>"
    )
    parts.append(f"<dt>symbols</dt><dd>{distinct_symbol_count(pattern)} distinct</dd>")
    if plan:
        parts.append(
            f"<dt>fold plan</dt><dd>{len(plan['creases'])} creases "
            f"({_esc(', '.join(plan['creases']))}); faces {_esc(''.join(sorted(plan['faces'])))}</dd>"
        )
    else:
        parts.append("<dt>fold plan</dt><dd>none saved</dd>")
    parts.append(f"<dt>bands</dt><dd>{len(bands)} (labelled column ranges)</dd>")
    parts.append("</dl>")

    for band_index, (col_start, col_end) in enumerate(bands, start=1):
        parts.append('<section class="band">')
        parts.append(
            f'<p class="band-meta">band {band_index} of {len(bands)} &mdash; columns {col_start}-{col_end}</p>'
        )
        parts.append(f'<p class="band-legend">{legend_pairs}</p>')
        parts.append("<table>")
        parts.append("<thead>")
        header_cells = ['<th scope="col">row</th>']
        for column in range(col_start, col_end + 1):
            label = str(column) if column == col_start or column % 5 == 0 else ""
            header_cells.append(f'<th scope="col">{label}</th>')
        parts.append("<tr>" + "".join(header_cells) + "</tr>")
        parts.append("</thead>")
        parts.append("<tbody>")
        for row_number in range(1, pattern["height"] + 1):
            segment = pattern["rows"][row_number - 1][col_start - 1 : col_end]
            cells = [f'<th scope="row" class="rowno">{row_number}</th>']
            for symbol in segment:
                cells.append(f"<td>{_esc(symbol)}</td>")
            parts.append("<tr>" + "".join(cells) + "</tr>")
        parts.append("</tbody>")
        parts.append("</table>")
        parts.append("</section>")

    parts.append("<h2>Colourway legend</h2>")
    parts.append('<table class="legend">')
    parts.append("<thead><tr><th>symbol</th><th>colourway</th><th>brand</th><th>code</th><th>stock</th></tr></thead>")
    parts.append("<tbody>")
    for entry in legend_entries(state, profile_id, pattern):
        parts.append(
            "<tr>"
            f"<td>{_esc(entry['symbol'])}</td>"
            f"<td>{_esc(entry['colourway'])}</td>"
            f"<td>{_esc(entry['brand'] or '-')}</td>"
            f"<td>{_esc(entry['code'] or '-')}</td>"
            f"<td>{_esc(entry['stock'])}</td>"
            "</tr>"
        )
    parts.append("</tbody>")
    parts.append("</table>")

    parts.append("<h2>Dye lots</h2>")
    parts.append('<table class="legend">')
    parts.append(
        "<thead><tr><th>colourway</th><th>lot</th><th>qty</th><th>unit</th>"
        "<th>acquired</th><th>note</th></tr></thead>"
    )
    parts.append("<tbody>")
    lot_rows = 0
    for symbol in symbols_in_order(pattern):
        colorway = find_colorway(state, profile_id, pattern["palette"].get(symbol, ""))
        if colorway is None:
            continue
        for lot in lots_for(state, colorway["id"]):
            lot_rows += 1
            parts.append(
                "<tr>"
                f"<td>{_esc(colorway['name'])}</td>"
                f"<td>{_esc(lot['lot_code'])}</td>"
                f"<td>{_esc(format_qty(float(lot['quantity'])))}</td>"
                f"<td>{_esc(lot['unit'])}</td>"
                f"<td>{_esc(lot.get('acquired_on', '-'))}</td>"
                f"<td>{_esc(lot.get('note', ''))}</td>"
                "</tr>"
            )
    if lot_rows == 0:
        parts.append('<tr><td colspan="6">no dye lots recorded for this pattern</td></tr>')
    parts.append("</tbody>")
    parts.append("</table>")
    parts.append("</main>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts) + "\n", len(bands)


def write_export_file(path: Path, content: str, force: bool) -> int:
    if path.exists() and not force:
        raise ToolError(
            f"{path} already exists",
            "re-run with --force to overwrite it",
        )
    payload = content.encode("utf-8")
    directory = path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolError(f"cannot create {directory}: {exc.strerror or exc}")
    handle_fd, tmp_name = tempfile.mkstemp(prefix=".export-", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
        os.chmod(path, 0o644)  # print files carry no secrets; leave them readable
    except OSError as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise ToolError(f"cannot write {path}: {exc.strerror or exc}", "check that the directory is writable")
    return len(payload)


def record_export(
    state: dict[str, Any], pattern: dict[str, Any], kind: str, path: Path, size: int, bands: int
) -> None:
    recorded = {
        "pattern_id": pattern["id"],
        "kind": kind,
        "path": str(path),
        "bytes": size,
        "bands": bands,
        "created_at": now_iso(),
    }
    kept = [
        entry
        for entry in state["exports"]
        if not (entry["pattern_id"] == pattern["id"] and entry["path"] == str(path))
    ]
    kept.append(recorded)
    state["exports"] = kept


# ---------------------------------------------------------------------------
# Show: pattern header, ruled grid, legend, probe
# ---------------------------------------------------------------------------


def render_pattern_header(pattern: dict[str, Any]) -> list[str]:
    lines = []
    lines.append(
        f"pattern {pattern['id']}  {pattern['name']}  "
        f"{pattern['width']}x{pattern['height']}  symbols {distinct_symbol_count(pattern)}  "
        f"updated {display_time(pattern['updated_at'])}"
    )
    return lines


def render_show(
    state: dict[str, Any],
    profile: dict[str, Any],
    pattern: dict[str, Any],
    page: int,
    rows_per_page: int,
    probe: str | None,
) -> list[str]:
    height = pattern["height"]
    lines = render_pattern_header(pattern)

    if rows_per_page <= 0:
        rows_per_page = height
    total_pages = max(1, (height + rows_per_page - 1) // rows_per_page)
    if page < 1 or page > total_pages:
        raise ToolError(
            f"page {page} is out of range: '{pattern['name']}' has {total_pages} "
            f"page{'s' if total_pages != 1 else ''} of {rows_per_page} rows",
            f"use --page 1 to --page {total_pages}",
        )
    first_row = (page - 1) * rows_per_page + 1
    last_row = min(page * rows_per_page, height)
    if total_pages > 1:
        lines.append(f"rendering page {page}/{total_pages} (rows {first_row}-{last_row} of {height})")
    else:
        lines.append(f"rendering page 1/1 (rows 1-{height} of {height})")
    lines.append("")

    if pattern_is_blank(pattern):
        lines.append("pattern is empty, use edit to fill cells")
        lines.append("")

    lines.extend(render_grid_block(pattern, first_row, last_row))
    lines.append("")
    lines.extend(render_legend_block(state, profile["id"], pattern))
    lines.append("")

    if probe:
        lines.extend(render_cell_probe(state, profile["id"], pattern, probe))
        lines.append("")
    lines.append(fold_hint(state, pattern))
    return lines


def parse_cell_probe(spec: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", spec or "")
    if not match:
        raise ToolError(
            f"'{spec}' is not a cell reference",
            "use --cell R,C with 1-based row and column, e.g. --cell 3,4",
        )
    return int(match.group(1)), int(match.group(2))


def check_bounds(pattern: dict[str, Any], row: int, column: int, what: str = "cell") -> None:
    if 1 <= row <= pattern["height"] and 1 <= column <= pattern["width"]:
        return
    raise ToolError(
        f"{what} {row},{column} is outside the {pattern['width']}x{pattern['height']} grid "
        f"(rows 1-{pattern['height']}, columns 1-{pattern['width']})",
        "rows and columns are 1-based",
    )


def render_cell_probe(
    state: dict[str, Any], profile_id: str, pattern: dict[str, Any], spec: str
) -> list[str]:
    row, column = parse_cell_probe(spec)
    check_bounds(pattern, row, column)
    symbol = pattern["rows"][row - 1][column - 1]
    name = pattern["palette"].get(symbol, "")
    colorway = find_colorway(state, profile_id, name)
    lines = []
    if colorway is None:
        lines.append(f"cell {row},{column}: symbol {symbol} -> colourway {name or '(none)'} (no colourway recorded)")
        lines.append("  lots: none")
        return lines
    lines.append(f"cell {row},{column}: symbol {symbol} -> {colorway['name']} ({colorway.get('brand') or '-'} {colorway.get('code') or '-'})")
    lots = lots_for(state, colorway["id"])
    if not lots:
        lines.append("  lots: none recorded for this colourway")
    for lot in lots:
        note = f' note "{lot["note"]}"' if lot.get("note") else ""
        lines.append(
            f"  lot {lot['lot_code']}: {format_qty(float(lot['quantity']))} {lot['unit']}"
            f" acquired {lot.get('acquired_on', '-')}{note}"
        )
    return lines


# ---------------------------------------------------------------------------
# Edit: cell, rectangle, add symbol, undo, dry-run
# ---------------------------------------------------------------------------


CELL_SPEC_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*=\s*(\S)\s*$")
RECT_SPEC_RE = re.compile(r"^\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*=\s*(\S)\s*$")
SYMBOL_SPEC_RE = re.compile(r"^\s*(\S)\s*=\s*(.+?)\s*$")


def apply_changes(pattern: dict[str, Any], changes: Sequence[tuple[int, int, str, str]]) -> None:
    rows = [list(row) for row in pattern["rows"]]
    for row, column, _old, new in changes:
        rows[row - 1][column - 1] = new
    pattern["rows"] = ["".join(row) for row in rows]


def push_edit_history(pattern: dict[str, Any], label: str, changes: Sequence[tuple[int, int, str, str]]) -> None:
    history = pattern.setdefault("edit_history", [])
    history.append(
        {
            "id": "e_" + secrets.token_hex(3),
            "label": label,
            "cells": [
                {"row": row, "column": column, "old": old, "new": new}
                for row, column, old, new in changes
            ],
            "at": now_iso(),
        }
    )
    pattern["edit_history"] = history[-EDIT_HISTORY_LIMIT:]


def changed_row_window(changes: Sequence[tuple[int, int, str, str]], height: int) -> tuple[int, int]:
    rows = [row for row, _column, _old, _new in changes]
    first = max(1, min(rows) - 1)
    last = min(height, max(rows) + 1)
    return first, last


def parse_add_symbol(spec: str) -> tuple[str, str]:
    match = SYMBOL_SPEC_RE.match(spec or "")
    if not match:
        raise ToolError(
            f"'{spec}' is not a symbol mapping",
            "use --add-symbol X=ColourwayName, e.g. --add-symbol K=Ochre",
        )
    return match.group(1), match.group(2)


# ---------------------------------------------------------------------------
# Colourway inventory helpers
# ---------------------------------------------------------------------------


def used_by_patterns(state: dict[str, Any], profile_id: str, colorway_name: str) -> list[str]:
    used: list[str] = []
    for pattern in patterns_for_profile(state, profile_id):
        if any(name.lower() == colorway_name.lower() for name in pattern["palette"].values()):
            used.append(pattern["name"])
    return used


def render_colorway_blocks(state: dict[str, Any], profile: dict[str, Any]) -> list[str]:
    profile_id = profile["id"]
    colorways = [c for c in state["colorways"] if c["profile_id"] == profile_id]
    if not colorways:
        return []
    lines: list[str] = []
    total_lots = sum(len(lots_for(state, colorway["id"])) for colorway in colorways)
    lines.append(
        f"colourway inventory - {len(colorways)} colourway{'s' if len(colorways) != 1 else ''}, "
        f"{total_lots} dye lot{'s' if total_lots != 1 else ''}"
    )
    for index, colorway in enumerate(sorted(colorways, key=lambda item: item["name"].lower()), start=1):
        lots = lots_for(state, colorway["id"])
        stock = format_stock(lots) or "0"
        lines.append("")
        lines.append(
            f"{index}. {colorway['name']}  -  {colorway.get('brand') or '-'}  -  {colorway.get('code') or '-'}"
        )
        lines.append(f"   lots {len(lots)}   stock {stock}")
        if lots:
            lot_rows = [
                [
                    lot["lot_code"],
                    f"{format_qty(float(lot['quantity']))} {lot['unit']}",
                    lot.get("acquired_on", "-"),
                    lot.get("note", "") or "-",
                ]
                for lot in lots
            ]
            lines.extend(_table(["lot", "qty", "acquired", "note"], lot_rows, indent="   "))
        else:
            lines.append("   no dye lots yet -> attach one with: "
                         f"python3 {PROG} colors lot-add \"{colorway['name']}\" --code D-118 --quantity 12 --unit g")
        patterns = used_by_patterns(state, profile_id, colorway["name"])
        if patterns:
            lines.append(f"   used by: {', '.join(patterns)}")
    return lines


# ---------------------------------------------------------------------------
# Demo seed
# ---------------------------------------------------------------------------


DEMO_COLORWAYS = [
    {
        "name": "Teal",
        "brand": "Malabrigo",
        "code": "#2f6f6a",
        "lots": [
            {"lot_code": "D-118", "quantity": 12.0, "unit": "g", "note": "slightly dark, batch 22", "days_ago": 52},
            {"lot_code": "D-121", "quantity": 6.0, "unit": "g", "note": "matches the first sleeve", "days_ago": 9},
        ],
    },
    {
        "name": "Coral",
        "brand": "Rowan",
        "code": "#d96a5c",
        "lots": [
            {"lot_code": "C-204", "quantity": 25.0, "unit": "g", "note": "class stock, even finish", "days_ago": 74},
        ],
    },
    {
        "name": "Sand",
        "brand": "Miyuki",
        "code": "#d9c8a5",
        "lots": [
            {"lot_code": "S-031", "quantity": 40.0, "unit": "g", "note": "delica, very even", "days_ago": 96},
            {"lot_code": "S-033", "quantity": 8.0, "unit": "g", "note": "greener in daylight", "days_ago": 23},
        ],
    },
    {
        "name": "Indigo",
        "brand": "Toho",
        "code": "#2b3a67",
        "lots": [
            {"lot_code": "I-077", "quantity": 0.0, "unit": "g", "note": "used up on the March cuff", "days_ago": 141},
        ],
    },
]

DEMO_FOLD_CREASES = ["r4", "r8", "c8"]


def demo_grid(size: int = 16) -> tuple[list[str], dict[str, str]]:
    """A concentric ring tessellation: centre D, then C, B, A repeating outward."""
    letters = "DCBA"
    rows: list[str] = []
    centre = (size - 1) / 2
    for row in range(size):
        cells = []
        for column in range(size):
            ring = int(max(abs(row - centre), abs(column - centre)) - 0.5)
            cells.append(letters[ring % len(letters)])
        rows.append("".join(cells))
    palette = {"A": "Teal", "B": "Coral", "C": "Sand", "D": "Indigo"}
    return rows, palette


def seed_demo(state: dict[str, Any], out_dir: Path, force: bool = True) -> list[str]:
    """Create or reuse the demo profile, colourways, pattern, fold plan and exports."""
    report: list[str] = []
    profile = find_profile(state, DEMO_PROFILE_NAME)
    if profile is None:
        profile = create_profile(
            state,
            DEMO_PROFILE_NAME,
            DEMO_PASSPHRASE,
            DEMO_PASSPHRASE,
            email=DEMO_PROFILE_EMAIL,
            enforce_strength=False,
        )
        report.append(f"demo profile created: {profile['name']} ({profile['id']})")
    else:
        report.append(f"demo profile reused: {profile['name']} ({profile['id']})")
    activate_profile(state, profile)

    created_colorways = 0
    created_lots = 0
    for spec in DEMO_COLORWAYS:
        colorway = find_colorway(state, profile["id"], spec["name"])
        if colorway is None:
            colorway = {
                "id": "c_" + secrets.token_hex(4),
                "profile_id": profile["id"],
                "name": spec["name"],
                "brand": spec["brand"],
                "code": spec["code"],
                "created_at": now_iso(),
            }
            state["colorways"].append(colorway)
            created_colorways += 1
        existing_codes = {lot["lot_code"] for lot in lots_for(state, colorway["id"])}
        for lot_spec in spec["lots"]:
            if lot_spec["lot_code"] in existing_codes:
                continue
            state["dye_lots"].append(
                {
                    "id": "l_" + secrets.token_hex(4),
                    "colorway_id": colorway["id"],
                    "lot_code": lot_spec["lot_code"],
                    "quantity": float(lot_spec["quantity"]),
                    "unit": lot_spec["unit"],
                    "note": lot_spec["note"],
                    "acquired_on": days_ago_iso(lot_spec["days_ago"]),
                }
            )
            created_lots += 1
    report.append(
        f"colourways: {len(DEMO_COLORWAYS)} ({created_colorways} new), "
        f"dye lots: {sum(len(spec['lots']) for spec in DEMO_COLORWAYS)} ({created_lots} new)"
    )

    pattern = None
    for candidate in patterns_for_profile(state, profile["id"]):
        if candidate["name"] == DEMO_PATTERN_NAME:
            pattern = candidate
            break
    if pattern is None:
        rows, palette = demo_grid(16)
        stamp = now_iso()
        pattern = {
            "id": "p_" + secrets.token_hex(4),
            "profile_id": profile["id"],
            "name": DEMO_PATTERN_NAME,
            "width": len(rows[0]),
            "height": len(rows),
            "rows": rows,
            "palette": palette,
            "edit_history": [],
            "source_file": "(demo seed)",
            "fold_plan_id": None,
            "created_at": stamp,
            "updated_at": stamp,
        }
        state["patterns"].append(pattern)
        report.append(f"demo pattern created: {pattern['name']} ({pattern['id']}) 16x16")
    else:
        report.append(f"demo pattern reused: {pattern['name']} ({pattern['id']}) {pattern['width']}x{pattern['height']}")

    faces = build_faces(DEMO_FOLD_CREASES, pattern["width"], pattern["height"])
    plan = fold_plan_for(state, pattern)
    if plan is None:
        plan = {
            "id": "f_" + secrets.token_hex(4),
            "pattern_id": pattern["id"],
            "creases": list(DEMO_FOLD_CREASES),
            "faces": faces,
            "updated_at": now_iso(),
        }
        state["fold_plans"].append(plan)
        pattern["fold_plan_id"] = plan["id"]
        report.append(f"fold plan created: {', '.join(DEMO_FOLD_CREASES)} -> faces {''.join(sorted(faces))}")
    else:
        report.append(f"fold plan reused: {', '.join(plan['creases'])} -> faces {''.join(sorted(plan['faces']))}")

    txt_path = out_dir / export_filename(pattern, ".txt")
    txt_size = write_export_file(txt_path, build_txt_export(state, profile["id"], pattern), force)
    record_export(state, pattern, "txt", txt_path, txt_size, 1)
    html_content, band_count = build_html_export(state, profile["id"], pattern)
    html_path = out_dir / export_filename(pattern, ".html")
    html_size = write_export_file(html_path, html_content, force)
    record_export(state, pattern, "html", html_path, html_size, band_count)
    report.append(f"wrote {txt_path} ({txt_size} bytes)")
    report.append(f"wrote {html_path} ({html_size} bytes, {band_count} band{'s' if band_count != 1 else ''})")
    return report


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _state_dir(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "state_dir", None) or DEFAULT_STATE_DIR)


def handle_signup(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    passphrase, confirm = read_new_passphrase(args)
    profile = create_profile(state, args.name, passphrase, confirm)
    activate_profile(state, profile)
    save_state(state_dir, state)
    write(f"profile created: {profile['name']} ({profile['id']})")
    write(f"signed in as {profile['name']}")
    write(f"state file: {state_path(state_dir)}")
    return EXIT_OK


def handle_login(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profiles = state["profiles"]
    if not profiles:
        raise ToolError(
            "no profiles exist on this machine yet",
            f"create one with: python3 {PROG} signup --name <profile>",
        )
    if args.demo:
        profile = find_profile(state, DEMO_PROFILE_NAME) or find_profile(state, DEMO_PROFILE_EMAIL)
        if profile is None:
            raise ToolError(
                "the demo profile is not seeded yet",
                f"run: python3 {PROG} demo",
            )
        passphrase = DEMO_PASSPHRASE
        write("checking passphrase...", stream=sys.stderr)
    else:
        name = (args.name or "").strip()
        if not name:
            if len(profiles) == 1:
                name = profiles[0]["name"]
            else:
                write("profiles on this machine:")
                for candidate in sorted(profiles, key=lambda item: item["name"].lower()):
                    write(f"  {candidate['name']}  (created {display_time(candidate['created_at'])})")
                sys.stdout.flush()
                if find_profile(state, DEMO_PROFILE_NAME) is not None:
                    write(f"demo login: python3 {PROG} login --demo  (cenius / {DEMO_PASSPHRASE})")
                raise ToolError(
                    "choose which profile to sign in to",
                    f"python3 {PROG} login --name <profile>",
                )
        profile = find_profile(state, name)
        passphrase = read_passphrase(args)
        write("checking passphrase...", stream=sys.stderr)
    ok = profile is not None and verify_passphrase(profile, passphrase)
    if not ok:
        raise ToolError(
            "sign in failed: unknown profile or wrong passphrase",
            "profiles are local to this machine; recovery means deleting .tesseractatlas/state.json",
        )
    activate_profile(state, profile)
    save_state(state_dir, state)
    write(f"signed in as {profile['name']} ({profile['id']})")
    write(f"sign out with: python3 {PROG} logout")
    return EXIT_OK


def handle_logout(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = active_profile(state)
    if profile is None:
        raise ToolError("not signed in", login_command_hint())
    clear_active_profile(state)
    profile["active"] = False
    save_state(state_dir, state)
    write(f"signed out of {profile['name']}")
    return EXIT_OK


def handle_status(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = active_profile(state)
    if profile is not None:
        write(f"signed in as {profile['name']} ({profile['id']})")
        write(f"profiles on this machine: {len(state['profiles'])}")
        write(f"sign out with: python3 {PROG} logout")
    else:
        write("not signed in")
        if state["profiles"]:
            write(f"profiles on this machine: {len(state['profiles'])}")
            write(login_command_hint())
        else:
            write("no profiles yet")
            write(f"create one with: python3 {PROG} signup --name <profile>")
    write(f"state file: {state_path(state_dir)}")
    return EXIT_OK


def handle_import(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    path = Path(args.path).expanduser()
    if not path.exists():
        raise ToolError(f"no such file: {path.resolve()}", "check the path and run the import again")
    if path.is_dir():
        raise ToolError(f"{path.resolve()} is a directory", "point --import at a text or CSV grid file")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise ToolError(f"{path.resolve()} is not UTF-8 text", "save the grid as plain UTF-8 and try again")
    except OSError as exc:
        raise ToolError(f"cannot read {path.resolve()}: {exc.strerror or exc}")
    if not text.strip():
        raise ToolError(f"{path.resolve()} is empty", "grid rows are lines of symbols; legend lines start with '#'")

    write(f"parsing {path}...")
    parsed = parse_grid(text, args.delimiter)
    write(f"detected delimiter: {parsed.delimiter_label}")
    write(f"dimensions: {parsed.width} columns x {parsed.height} rows ({parsed.width * parsed.height} cells)")
    write(f"symbols: {len(parsed.symbols)} distinct ({', '.join(parsed.symbols)})")
    write("legend:")
    for symbol in parsed.symbols:
        origin = "auto" if any(symbol == auto for auto, _name in parsed.auto_assigned) else "file"
        write(f"  {symbol} -> {parsed.palette[symbol]} ({origin})")
    if parsed.auto_assigned:
        write("auto-assigned symbols:")
        for symbol, name in parsed.auto_assigned:
            write(f"  {symbol} -> {name} (no legend line in the file)")
    if parsed.comments:
        write(f"skipped {parsed.comments} comment line{'s' if parsed.comments != 1 else ''}")

    if args.dry_run:
        write("dry run: nothing was saved")
        return EXIT_OK

    stamp = now_iso()
    pattern = {
        "id": "p_" + secrets.token_hex(4),
        "profile_id": profile["id"],
        "name": (args.name or path.stem or "untitled").strip(),
        "width": parsed.width,
        "height": parsed.height,
        "rows": ["".join(row) for row in parsed.rows],
        "palette": parsed.palette,
        "edit_history": [],
        "source_file": str(path.resolve()),
        "fold_plan_id": None,
        "created_at": stamp,
        "updated_at": stamp,
    }
    state["patterns"].append(pattern)
    save_state(state_dir, state)
    write(f"saved pattern {pattern['id']} \"{pattern['name']}\" ({pattern['width']}x{pattern['height']})")
    write(f"preview it with: python3 {PROG} show {pattern['id']}")
    return EXIT_OK


def handle_list(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    patterns = patterns_for_profile(state, profile["id"])
    if not patterns:
        write("no patterns yet")
        write(f"import one with: python3 {PROG} import examples/sample.stitch.csv")
        write(f"or seed the demo: python3 {PROG} demo")
        return EXIT_OK
    patterns.sort(key=lambda item: (item["updated_at"], item["id"]), reverse=True)
    headers = ["id", "name", "size", "symbols", "updated"]
    rows = [
        [
            pattern["id"],
            pattern["name"],
            f"{pattern['width']}x{pattern['height']}",
            str(distinct_symbol_count(pattern)),
            display_time(pattern["updated_at"]),
        ]
        for pattern in patterns
    ]
    write(f"patterns - {len(patterns)} for profile {profile['name']} (newest first)")
    for line in _table(headers, rows, indent=""):
        write(line)
    write("")
    write(f"hints: python3 {PROG} open <id>  |  python3 {PROG} rename <id> <new name>  |  "
          f"python3 {PROG} delete <id> --confirm \"<name>\"")
    return EXIT_OK


def handle_open(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    pattern = find_pattern(state, profile["id"], args.pattern)
    plan = fold_plan_for(state, pattern)
    exports = [entry for entry in state["exports"] if entry["pattern_id"] == pattern["id"]]
    write(f"pattern {pattern['id']}  {pattern['name']}")
    write(f"size      {pattern['width']} x {pattern['height']} ({pattern['width'] * pattern['height']} cells)")
    write(f"symbols   {distinct_symbol_count(pattern)} distinct ({', '.join(symbols_in_order(pattern))})")
    write("palette   " + "  ".join(f"{symbol}={pattern['palette'].get(symbol, '?')}"
                                    for symbol in symbols_in_order(pattern)))
    write(f"created   {display_time(pattern['created_at'])}")
    write(f"updated   {display_time(pattern['updated_at'])}")
    write(f"source    {pattern.get('source_file') or '-'}")
    if plan:
        write(f"fold plan {len(plan['creases'])} creases ({', '.join(plan['creases'])}) -> "
              f"faces {''.join(sorted(plan['faces']))}")
    else:
        write("fold plan none saved")
    write(f"edits     {len(pattern.get('edit_history', []))} kept (last {EDIT_HISTORY_LIMIT})")
    write(f"exports   {len(exports)} recorded")
    write("")
    write(f"show it with:    python3 {PROG} show {pattern['id']}")
    write(f"edit a cell:     python3 {PROG} edit {pattern['id']} --cell 1,1=A")
    write(f"fold the net:    python3 {PROG} fold preview {pattern['id']}")
    return EXIT_OK


def handle_show(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    pattern = find_pattern(state, profile["id"], args.pattern)
    for line in render_show(state, profile, pattern, args.page, args.rows, args.cell):
        write(line)
    return EXIT_OK


def handle_rename(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    pattern = find_pattern(state, profile["id"], args.pattern)
    new_name = (args.newname or "").strip()
    if not new_name:
        raise ToolError("the new name cannot be empty", "use: rename <pattern> <new name>")
    if len(new_name) > 48:
        raise ToolError("the new name is longer than 48 characters", "shorten it and try again")
    if new_name == pattern["name"]:
        write(f"name unchanged: {pattern['name']}")
        return EXIT_OK
    old_name = pattern["name"]
    pattern["name"] = new_name
    pattern["updated_at"] = now_iso()
    save_state(state_dir, state)
    write(f"renamed '{old_name}' -> '{new_name}' ({pattern['id']})")
    return EXIT_OK


def handle_delete(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    pattern = find_pattern(state, profile["id"], args.pattern)
    if not read_confirmation(args.confirm, pattern["name"], f"deleting '{pattern['name']}'"):
        raise ToolError("delete cancelled: the confirmation word did not match", "nothing was deleted")
    state["patterns"] = [item for item in state["patterns"] if item["id"] != pattern["id"]]
    plan = fold_plan_for(state, pattern)
    if plan is not None:
        state["fold_plans"] = [item for item in state["fold_plans"] if item["id"] != plan["id"]]
    state["exports"] = [item for item in state["exports"] if item["pattern_id"] != pattern["id"]]
    save_state(state_dir, state)
    write(f"deleted pattern '{pattern['name']}' ({pattern['id']})")
    return EXIT_OK


def handle_edit(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    pattern = find_pattern(state, profile["id"], args.pattern)
    history = pattern.setdefault("edit_history", [])

    if args.undo:
        if not history:
            raise ToolError(
                f"no edits to undo on '{pattern['name']}'",
                f"change a cell first: python3 {PROG} edit {pattern['id']} --cell 1,1=A",
            )
        edit = history.pop()
        inverse = [(cell["row"], cell["column"], cell["new"], cell["old"]) for cell in edit["cells"]]
        apply_changes(pattern, inverse)
        pattern["updated_at"] = now_iso()
        save_state(state_dir, state)
        write(f"reverted edit {edit['id']} ({edit['label']}): {len(inverse)} cell"
              f"{'s' if len(inverse) != 1 else ''} restored")
        for row, column, old, new in inverse[:12]:
            write(f"{row},{column}: {old} -> {new}")
        if len(inverse) > 12:
            write(f"... and {len(inverse) - 12} more cells")
        first, last = changed_row_window(inverse, pattern["height"])
        write("")
        write(f"rows {first}-{last} now")
        for line in render_grid_block(pattern, first, last):
            write(line)
        return EXIT_OK

    if not args.cell and not args.rect:
        raise ToolError(
            "nothing to change",
            f"pass --cell R,C=SYM, --rect R1,C1,R2,C2=SYM or --undo (see: python3 {PROG} edit --help)",
        )

    added_symbol = None
    if args.add_symbol:
        symbol, colourway_name = parse_add_symbol(args.add_symbol)
        existing = pattern["palette"].get(symbol)
        if existing is not None and existing.lower() != colourway_name.lower():
            raise ToolError(
                f"symbol '{symbol}' already maps to '{existing}'",
                "pick a different symbol for the new colourway",
            )
        added_symbol = (symbol, colourway_name)

    if args.cell:
        match = CELL_SPEC_RE.match(args.cell)
        if not match:
            raise ToolError(f"'{args.cell}' is not a cell change",
                            "use --cell R,C=SYM, e.g. --cell 3,4=B")
        row, column, symbol = int(match.group(1)), int(match.group(2)), match.group(3)
        check_bounds(pattern, row, column)
        targets = [(row, column, symbol)]
        label = f"cell {row},{column} to {symbol}"
    else:
        match = RECT_SPEC_RE.match(args.rect)
        if not match:
            raise ToolError(f"'{args.rect}' is not a rectangle change",
                            "use --rect R1,C1,R2,C2=SYM, e.g. --rect 2,2,6,6=C")
        row1, col1, row2, col2 = (int(match.group(i)) for i in range(1, 5))
        symbol = match.group(5)
        check_bounds(pattern, min(row1, row2), min(col1, col2), "cell")
        check_bounds(pattern, max(row1, row2), max(col1, col2), "cell")
        rows = range(min(row1, row2), max(row1, row2) + 1)
        columns = range(min(col1, col2), max(col1, col2) + 1)
        targets = [(row, column, symbol) for row in rows for column in columns]
        label = f"rect {min(row1, row2)},{min(col1, col2)}-{max(row1, row2)},{max(col1, col2)} to {symbol}"

    if symbol not in pattern["palette"]:
        if added_symbol and added_symbol[0] == symbol:
            pass
        elif added_symbol:
            raise ToolError(
                f"symbol '{symbol}' is not in the palette and --add-symbol adds only '{added_symbol[0]}'",
                f"use --add-symbol {symbol}=ColourwayName",
            )
        else:
            raise ToolError(
                f"symbol '{symbol}' is not in the palette of '{pattern['name']}'",
                f"add it inline with --add-symbol {symbol}=ColourwayName",
            )

    changes: list[tuple[int, int, str, str]] = []
    for row, column, new_symbol in targets:
        old_symbol = pattern["rows"][row - 1][column - 1]
        if old_symbol != new_symbol:
            changes.append((row, column, old_symbol, new_symbol))

    if args.dry_run:
        if len(changes) > 1:
            write(f"applying {len(changes)} changes... (dry run)")
        if not changes:
            write(f"no cells change: '{pattern['name']}' already holds {symbol} there")
            write(f"dry run: nothing was written; pattern unchanged (updated {display_time(pattern['updated_at'])})")
            return EXIT_OK
        for row, column, old, new in changes:
            write(f"{row},{column}: {old} -> {new}")
        write(f"dry run: nothing was written; pattern unchanged (updated {display_time(pattern['updated_at'])})")
        return EXIT_OK

    if not changes:
        write(f"no cells change: '{pattern['name']}' already holds {symbol} there")
        return EXIT_OK

    if len(changes) > 1:
        write(f"applying {len(changes)} changes...")
    if added_symbol and added_symbol[0] == symbol and symbol not in pattern["palette"]:
        pattern["palette"][symbol] = added_symbol[1]
        write(f"symbol {symbol} added to the palette -> {added_symbol[1]}")
    elif added_symbol and added_symbol[0] != symbol:
        warn(f"note: '{added_symbol[0]}' was not added because this edit sets '{symbol}'")
    apply_changes(pattern, changes)
    push_edit_history(pattern, label, changes)
    pattern["updated_at"] = now_iso()
    save_state(state_dir, state)
    for row, column, old, new in changes:
        write(f"{row},{column}: {old} -> {new}")
    write(f"saved {len(changes)} change{'s' if len(changes) != 1 else ''} to '{pattern['name']}' "
          f"(updated {display_time(pattern['updated_at'])})")
    first, last = changed_row_window(changes, pattern["height"])
    write("")
    write(f"rows {first}-{last} now")
    for line in render_grid_block(pattern, first, last):
        write(line)
    return EXIT_OK


def handle_fold_set(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    pattern = find_pattern(state, profile["id"], args.pattern)
    tokens = list(args.crease or []) + list(args.creases or [])
    if not tokens:
        raise ToolError(
            "no crease lines given",
            f"for example: python3 {PROG} fold set {pattern['id']} r4 r8 c8",
        )
    problems = crease_warnings(tokens, pattern["width"], pattern["height"])
    if problems:
        for problem in problems:
            warn(f"warning: {problem}")
        raise ToolError(
            f"fold plan not saved for '{pattern['name']}'",
            "fix the crease lines and run fold set again",
        )
    faces = build_faces(tokens, pattern["width"], pattern["height"])
    if len(faces) > 6:
        raise ToolError(
            f"that crease plan makes {len(faces)} regions; a cube net has at most 6 faces",
            "use fewer crease lines, for example r4 r8 c8",
        )
    rows_used = sorted({int(token.strip().lower()[1:]) for token in tokens if token.strip().lower().startswith("r")})
    cols_used = sorted({int(token.strip().lower()[1:]) for token in tokens if token.strip().lower().startswith("c")})
    creases = [f"r{number}" for number in rows_used] + [f"c{number}" for number in cols_used]

    plan = fold_plan_for(state, pattern)
    if plan is None:
        plan = {
            "id": "f_" + secrets.token_hex(4),
            "pattern_id": pattern["id"],
            "creases": creases,
            "faces": faces,
            "updated_at": now_iso(),
        }
        state["fold_plans"].append(plan)
        pattern["fold_plan_id"] = plan["id"]
    else:
        plan["creases"] = creases
        plan["faces"] = faces
        plan["updated_at"] = now_iso()
    pattern["updated_at"] = now_iso()
    save_state(state_dir, state)
    write(f"fold plan saved for '{pattern['name']}': creases {', '.join(creases)}; "
          f"faces {''.join(sorted(faces))}")
    write("")
    for line in render_face_table(faces):
        write(line)
    return EXIT_OK


def handle_fold_preview(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    pattern = find_pattern(state, profile["id"], args.pattern)
    plan = fold_plan_for(state, pattern)
    if plan is None:
        raise ToolError(
            f"no fold plan saved for '{pattern['name']}'",
            f"set one with: python3 {PROG} fold set {pattern['id']} r4 r8 c8",
        )
    faces = plan["faces"]
    write(f"fold plan {plan['id']} for pattern {pattern['id']}  {pattern['name']}  "
          f"{pattern['width']}x{pattern['height']}")
    write(f"creases: {', '.join(plan['creases'])}")
    write(f"faces: {len(faces)} (max 6)")
    write("")
    for line in render_face_table(faces):
        write(line)
    write("")
    for line in render_face_sketch(faces):
        write(line)
    return EXIT_OK


def handle_colors_list(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    if not any(colorway["profile_id"] == profile["id"] for colorway in state["colorways"]):
        write("no colourways yet")
        write(f"add one with: python3 {PROG} colors add --name Teal --brand Malabrigo --code \"#2f6f6a\"")
        write(f"example line: python3 {PROG} colors lot-add Teal --code D-118 --quantity 12 --unit g "
              f"--note \"slightly dark\" --date 2024-04-02")
        return EXIT_OK
    for line in render_colorway_blocks(state, profile):
        write(line)
    return EXIT_OK


def handle_colors_add(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    name = (args.name or "").strip()
    if not name:
        raise ToolError("a colourway needs a name", "use: colors add --name Teal --brand Malabrigo --code #2f6f6a")
    if find_colorway(state, profile["id"], name) is not None:
        raise ToolError(f"a colourway named '{name}' already exists",
                        f"list them with: python3 {PROG} colors list")
    colorway = {
        "id": "c_" + secrets.token_hex(4),
        "profile_id": profile["id"],
        "name": name,
        "brand": (args.brand or "").strip(),
        "code": (args.code or "").strip(),
        "created_at": now_iso(),
    }
    state["colorways"].append(colorway)
    save_state(state_dir, state)
    write(f"colourway added: {colorway['name']} ({colorway['brand'] or '-'} {colorway['code'] or '-'})")
    write(f"attach a dye lot with: python3 {PROG} colors lot-add \"{colorway['name']}\" "
          f"--code D-118 --quantity 12 --unit g")
    return EXIT_OK


def handle_colors_delete(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    colorway = find_colorway(state, profile["id"], args.name)
    if colorway is None:
        raise ToolError(f"no colourway named '{args.name}'",
                        f"list them with: python3 {PROG} colors list")
    if not read_confirmation(args.confirm, colorway["name"], f"deleting colourway '{colorway['name']}'"):
        raise ToolError("delete cancelled: the confirmation word did not match", "nothing was deleted")
    patterns = used_by_patterns(state, profile["id"], colorway["name"])
    state["colorways"] = [item for item in state["colorways"] if item["id"] != colorway["id"]]
    state["dye_lots"] = [lot for lot in state["dye_lots"] if lot["colorway_id"] != colorway["id"]]
    save_state(state_dir, state)
    write(f"deleted colourway '{colorway['name']}' and its dye lots")
    if patterns:
        warn(f"warning: {len(patterns)} pattern{'s' if len(patterns) != 1 else ''} still reference "
             f"'{colorway['name']}' ({', '.join(patterns)}); they now show as no stock")
    return EXIT_OK


def handle_lot_add(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    colorway = find_colorway(state, profile["id"], args.name)
    if colorway is None:
        raise ToolError(f"no colourway named '{args.name}'",
                        f"add it first with: python3 {PROG} colors add --name {args.name}")
    code = (args.code or "").strip()
    if not code:
        raise ToolError("a dye lot needs a lot code", "use: colors lot-add Teal --code D-118 --quantity 12 --unit g")
    try:
        quantity = float(args.quantity)
    except (TypeError, ValueError):
        raise ToolError(f"'{args.quantity}' is not a number", "quantities are plain numbers, e.g. --quantity 12")
    if quantity < 0:
        raise ToolError("quantity cannot be negative", "quantities are zero or more")
    unit = (args.unit or "g").strip()
    if not unit:
        raise ToolError("a dye lot needs a unit", "for example --unit g or --unit packets")
    acquired = (args.date or today_iso()).strip()
    if not valid_date_iso(acquired):
        raise ToolError(f"'{acquired}' is not a date", "use --date YYYY-MM-DD")
    if any(lot["lot_code"].lower() == code.lower() for lot in lots_for(state, colorway["id"])):
        raise ToolError(f"colourway '{colorway['name']}' already has a lot '{code}'",
                        "lot codes must be unique per colourway")
    lot = {
        "id": "l_" + secrets.token_hex(4),
        "colorway_id": colorway["id"],
        "lot_code": code,
        "quantity": quantity,
        "unit": unit,
        "note": (args.note or "").strip(),
        "acquired_on": acquired,
    }
    state["dye_lots"].append(lot)
    save_state(state_dir, state)
    write(f"dye lot added: {colorway['name']} / {code} ({format_qty(quantity)} {unit}, acquired {acquired})")
    total = format_stock(lots_for(state, colorway["id"])) or "0"
    held = lots_for(state, colorway["id"])
    write(f"{colorway['name']} now holds {plural(len(held), 'lot')}, {total} total")
    return EXIT_OK


def handle_lot_delete(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    colorway = find_colorway(state, profile["id"], args.name)
    if colorway is None:
        raise ToolError(f"no colourway named '{args.name}'",
                        f"list them with: python3 {PROG} colors list")
    lot = next((item for item in lots_for(state, colorway["id"]) if item["lot_code"].lower() == (args.code or "").strip().lower()), None)
    if lot is None:
        codes = ", ".join(item["lot_code"] for item in lots_for(state, colorway["id"])) or "none"
        raise ToolError(f"'{args.code}' is not a lot of '{colorway['name']}' (lots: {codes})",
                        "check the lot code and try again")
    if not read_confirmation(args.confirm, lot["lot_code"], f"deleting lot {lot['lot_code']}"):
        raise ToolError("delete cancelled: the confirmation word did not match", "nothing was deleted")
    state["dye_lots"] = [item for item in state["dye_lots"] if item["id"] != lot["id"]]
    save_state(state_dir, state)
    remaining = lots_for(state, colorway["id"])
    write(f"deleted lot {lot['lot_code']} from '{colorway['name']}'")
    write(f"{colorway['name']} now holds {plural(len(remaining), 'lot')}, "
          f"{format_stock(remaining) or '0'} total")
    return EXIT_OK


def print_recorded_exports(state: dict[str, Any], pattern: dict[str, Any]) -> None:
    entries = [entry for entry in state["exports"] if entry["pattern_id"] == pattern["id"]]
    if not entries:
        return
    entries.sort(key=lambda entry: entry["kind"])
    write("exports recorded for this pattern:")
    for entry in entries:
        bands = f", {plural(entry['bands'], 'band')}" if entry["kind"] == "html" else ""
        write(f"  {entry['kind']:<4} {entry['path']}  {entry['bytes']} bytes{bands}")


def _resolve_export_target(state: dict[str, Any], profile: dict[str, Any], args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    pattern = find_pattern(state, profile["id"], args.pattern)
    out_dir = Path(args.out_dir).expanduser()
    return pattern, out_dir


def handle_export_txt(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    pattern, out_dir = _resolve_export_target(state, profile, args)
    path = out_dir / export_filename(pattern, ".txt")
    write(f"writing {path}...")
    content = build_txt_export(state, profile["id"], pattern)
    size = write_export_file(path, content, args.force)
    record_export(state, pattern, "txt", path, size, len(band_ranges(pattern["width"])))
    save_state(state_dir, state)
    write(f"wrote {path} ({size} bytes)")
    print_recorded_exports(state, pattern)
    return EXIT_OK


def handle_export_html(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    pattern, out_dir = _resolve_export_target(state, profile, args)
    path = out_dir / export_filename(pattern, ".html")
    write(f"writing {path}...")
    content, band_count = build_html_export(state, profile["id"], pattern)
    size = write_export_file(path, content, args.force)
    record_export(state, pattern, "html", path, size, band_count)
    save_state(state_dir, state)
    write(f"wrote {path} ({size} bytes, {plural(band_count, 'band')})")
    write("the HTML is print-ready: A4, black on white, one labelled table per column band")
    print_recorded_exports(state, pattern)
    return EXIT_OK


def handle_export_list(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    profile = require_profile(state)
    pattern = find_pattern(state, profile["id"], args.pattern)
    entries = [entry for entry in state["exports"] if entry["pattern_id"] == pattern["id"]]
    if not entries:
        write(f"no exports yet for '{pattern['name']}'")
        write(f"write them with: python3 {PROG} export txt {pattern['id']} --out-dir demo_out")
        return EXIT_OK
    entries.sort(key=lambda entry: (entry["kind"], entry["path"]))
    write(f"exports for {pattern['id']} '{pattern['name']}'")
    for entry in entries:
        bands = f", {plural(entry['bands'], 'band')}" if entry["kind"] == "html" else ""
        write(f"  {entry['kind']:<4} {entry['path']}  {entry['bytes']} bytes{bands}  "
              f"{display_time(entry['created_at'])}")
    return EXIT_OK


def handle_demo(args: argparse.Namespace) -> int:
    state_dir = _state_dir(args)
    state = load_state(state_dir, reset=args.reset_state)
    out_dir = Path(args.out_dir).expanduser()
    report = seed_demo(state, out_dir)
    save_state(state_dir, state)
    for line in report:
        write(line)
    profile = find_profile(state, DEMO_PROFILE_NAME)
    pattern = None
    if profile is not None:
        pattern = next((item for item in patterns_for_profile(state, profile["id"])
                        if item["name"] == DEMO_PATTERN_NAME), None)
    write("")
    write(f"state directory: {state_dir}")
    write(f"signed in as: {DEMO_PROFILE_NAME} (demo passphrase: {DEMO_PASSPHRASE})")
    if pattern is not None:
        write(f"look at it with: python3 {PROG} show {pattern['id']} --state-dir {state_dir}")
        write(f"fold preview:    python3 {PROG} fold preview {pattern['id']} --state-dir {state_dir}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Argument tree
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", metavar="DIR", default=argparse.SUPPRESS,
                        help=f"state directory (default: {DEFAULT_STATE_DIR})")
    common.add_argument("--reset-state", action="store_true", default=argparse.SUPPRESS,
                        help="archive an unreadable state.json and start fresh")

    parser = argparse.ArgumentParser(
        prog=PROG,
        description="TesseractAtlas - virtual pattern-folding grid for beadwork, colourway "
                    "inventory with dye-lot notes and print-friendly stitch maps.",
        epilog=(
            "examples:\n"
            f"  python3 {PROG} signup --name ana --passphrase 'teal-lattice-7' --confirm 'teal-lattice-7'\n"
            f"  python3 {PROG} import examples/sample.stitch.csv\n"
            f"  python3 {PROG} list\n"
            f"  python3 {PROG} show 1 --rows 20\n"
            f"  python3 {PROG} edit 1 --cell 1,1=B\n"
            f"  python3 {PROG} fold set 1 r4 r8 c8\n"
            f"  python3 {PROG} export txt 1 --out-dir demo_out\n"
            f"  python3 {PROG} demo\n"
            f"  python3 {PROG} demo --help\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"TesseractAtlas {VERSION}")
    parser.add_argument("--state-dir", metavar="DIR", default=None,
                        help=f"state directory (default: {DEFAULT_STATE_DIR})")
    parser.add_argument("--reset-state", action="store_true",
                        help="archive an unreadable state.json and start fresh")
    sub = parser.add_subparsers(dest="command", metavar="command")

    def leaf(container: argparse._SubParsersAction, name: str, help_text: str, handler: Any) -> argparse.ArgumentParser:
        sp = container.add_parser(name, parents=[common], help=help_text, description=help_text)
        sp.set_defaults(func=handler)
        return sp

    sp = leaf(sub, "signup", "create a local profile with a confirmed passphrase", handle_signup)
    sp.add_argument("--name", required=True, help="profile name (2-32 characters)")
    sp.add_argument("--passphrase", help="passphrase, at least 8 characters (prompted when omitted on a TTY)")
    sp.add_argument("--confirm", help="repeat of the passphrase")

    sp = leaf(sub, "login", "verify a passphrase and mark the profile active", handle_login)
    sp.add_argument("--name", help="profile name or e-mail (the picker runs when several profiles exist)")
    sp.add_argument("--passphrase", help="passphrase (prompted when omitted on a TTY)")
    sp.add_argument("--demo", action="store_true", help="sign in with the seeded demo profile (cenius / cenius)")

    leaf(sub, "logout", "clear the active-profile marker", handle_logout)
    leaf(sub, "status", "report which profile is active", handle_status)

    sp = leaf(sub, "import", "import a text or CSV grid as a pattern", handle_import)
    sp.add_argument("path", help="path to the grid file, e.g. examples/sample.stitch.csv")
    sp.add_argument("--name", help="pattern name (defaults to the file name)")
    sp.add_argument("--delimiter", choices=["comma", "semicolon", "tab", "whitespace"],
                    help="force a delimiter instead of sniffing it")
    sp.add_argument("--dry-run", action="store_true", help="validate and print the report without saving")

    leaf(sub, "list", "list stored patterns, newest first", handle_list)

    sp = leaf(sub, "show", "render the ruled grid with legend and stock markers", handle_show)
    sp.add_argument("pattern", help="pattern id or a unique name prefix")
    sp.add_argument("--page", type=int, default=1, help="page number for grids taller than --rows")
    sp.add_argument("--rows", type=int, default=DEFAULT_ROWS_PER_PAGE,
                    help=f"rows per page (default {DEFAULT_ROWS_PER_PAGE}; 0 shows every row)")
    sp.add_argument("--cell", help="probe one cell, e.g. --cell 3,4")

    sp = leaf(sub, "open", "show a pattern's detail card", handle_open)
    sp.add_argument("pattern", help="pattern id or a unique name prefix")

    sp = leaf(sub, "rename", "rename a stored pattern", handle_rename)
    sp.add_argument("pattern", help="pattern id or a unique name prefix")
    sp.add_argument("newname", help="new pattern name")

    sp = leaf(sub, "delete", "delete a pattern after a typed confirmation", handle_delete)
    sp.add_argument("pattern", help="pattern id or a unique name prefix")
    sp.add_argument("--confirm", help='type the pattern name to confirm, e.g. --confirm "My Pattern"')

    sp = leaf(sub, "edit", "set a cell or rectangle, add a symbol, or undo", handle_edit)
    sp.add_argument("pattern", help="pattern id or a unique name prefix")
    sp.add_argument("--cell", help="set one cell: --cell R,C=SYM (1-based)")
    sp.add_argument("--rect", help="set a rectangle: --rect R1,C1,R2,C2=SYM")
    sp.add_argument("--add-symbol", help="add a new palette symbol inline: --add-symbol X=ColourwayName")
    sp.add_argument("--undo", action="store_true", help="revert the most recent persisted edit")
    sp.add_argument("--dry-run", action="store_true", help="print the changes without writing them")

    fold = sub.add_parser("fold", parents=[common], help="crease the grid into a cube-net preview",
                          description="Crease the grid into a net of at most six labelled cube faces.")
    fold_sub = fold.add_subparsers(dest="action", metavar="action")
    fold.set_defaults(func=lambda a: (fold.print_help(), EXIT_OK)[1])
    fsp = leaf(fold_sub, "set", "validate and save crease lines for a pattern", handle_fold_set)
    fsp.add_argument("pattern", help="pattern id or a unique name prefix")
    fsp.add_argument("creases", nargs="*", help="crease lines: rN (horizontal) or cN (vertical)")
    fsp.add_argument("--crease", action="append", help="crease line, repeatable: --crease r4")
    fsp = leaf(fold_sub, "preview", "show the saved face map and isometric net", handle_fold_preview)
    fsp.add_argument("pattern", help="pattern id or a unique name prefix")

    colors = sub.add_parser("colors", parents=[common], help="colourway and dye-lot inventory",
                            description="Colourway inventory with dye-lot notes and stock counts.")
    colors_sub = colors.add_subparsers(dest="action", metavar="action")
    colors.set_defaults(func=lambda a: (colors.print_help(), EXIT_OK)[1])
    csp = leaf(colors_sub, "list", "list colourways with lots, notes and stock", handle_colors_list)
    csp.set_defaults(func=handle_colors_list)
    csp = leaf(colors_sub, "add", "add a colourway with brand and code", handle_colors_add)
    csp.add_argument("--name", required=True, help="colourway name, e.g. Teal")
    csp.add_argument("--brand", default="", help="brand, e.g. Malabrigo")
    csp.add_argument("--code", default="", help="hex or short code, e.g. #2f6f6a")
    csp = leaf(colors_sub, "delete", "delete a colourway and its lots (typed confirmation)", handle_colors_delete)
    csp.add_argument("name", help="colourway name")
    csp.add_argument("--confirm", help="type the colourway name to confirm")
    csp = leaf(colors_sub, "lot-add", "attach a dye lot to a colourway", handle_lot_add)
    csp.add_argument("name", help="colourway name")
    csp.add_argument("--code", required=True, help="dye-lot code, e.g. D-118")
    csp.add_argument("--quantity", required=True, help="quantity on hand, e.g. 12")
    csp.add_argument("--unit", default="g", help="unit as typed, e.g. g or packets")
    csp.add_argument("--note", default="", help="free-text note, e.g. 'slightly dark'")
    csp.add_argument("--date", help="acquired date YYYY-MM-DD (default: today)")
    csp = leaf(colors_sub, "lot-delete", "delete one dye lot (typed confirmation)", handle_lot_delete)
    csp.add_argument("name", help="colourway name")
    csp.add_argument("--code", required=True, help="dye-lot code to delete")
    csp.add_argument("--confirm", help="type the lot code to confirm")

    export = sub.add_parser("export", parents=[common], help="write print-ready stitch maps",
                            description="Write a fixed-width .txt and a print-CSS .html stitch map.")
    export_sub = export.add_subparsers(dest="action", metavar="action")
    export.set_defaults(func=lambda a: (export.print_help(), EXIT_OK)[1])
    esp = leaf(export_sub, "txt", "write PATTERN.stitchmap.txt", handle_export_txt)
    esp.add_argument("pattern", help="pattern id or a unique name prefix")
    esp.add_argument("--out-dir", default=".", help="directory for the export (default: .)")
    esp.add_argument("--force", action="store_true", help="overwrite an existing file")
    esp = leaf(export_sub, "html", "write PATTERN.stitchmap.html with @media print", handle_export_html)
    esp.add_argument("pattern", help="pattern id or a unique name prefix")
    esp.add_argument("--out-dir", default=".", help="directory for the export (default: .)")
    esp.add_argument("--force", action="store_true", help="overwrite an existing file")
    esp = leaf(export_sub, "list", "list exports recorded for a pattern", handle_export_list)
    esp.add_argument("pattern", help="pattern id or a unique name prefix")

    sp = leaf(sub, "demo", "seed demo data, exports and a demo profile", handle_demo)
    sp.add_argument("--out-dir", default="demo_out", help="directory for the demo exports (default: demo_out)")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help()
        write("")
        write(f"tip: seed demo data with `python3 {PROG} demo`, or run `bash demo.sh` for the recorded walkthrough.")
        try:
            state = load_state(_state_dir(args))
        except ToolError:
            state = None
        if state is not None:
            profile = active_profile(state)
            if profile is not None:
                count = len(patterns_for_profile(state, profile["id"]))
                write(f"now: signed in as {profile['name']} - {plural(count, 'pattern')} "
                      f"in {_state_dir(args)}/")
            elif state["profiles"]:
                write(f"now: not signed in - {plural(len(state['profiles']), 'profile')} on this machine, "
                      f"sign in with `python3 {PROG} login`")
        return EXIT_OK
    try:
        return int(handler(args))
    except ToolError as exc:
        warn(f"error: {exc.message}")
        if exc.hint:
            warn(f"hint: {exc.hint}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        warn("cancelled")
        return 130
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

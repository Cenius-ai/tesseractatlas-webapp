"""Smoke and behaviour tests for TesseractAtlas.

Run from the project root:

    python3 -m unittest discover -s tests

Every test drives the real command tree in-process against a throwaway state
directory, so nothing outside ``tempfile`` is touched and no network is used.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import tesseract_atlas as atlas

PASSPHRASE = "teal-lattice-7"


class AtlasTestCase(unittest.TestCase):
    """Shared harness: a temp state directory and an in-process CLI runner."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "state"
        self.out_dir = self.root / "out"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = atlas.main(["--state-dir", str(self.state_dir), *argv])
            except SystemExit as exc:  # argparse usage errors
                code = int(exc.code or 0)
        return code, stdout.getvalue(), stderr.getvalue()

    def ok(self, *argv: str) -> str:
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, f"command {argv} failed: {err}\n{out}")
        return out

    def fail(self, *argv: str) -> tuple[str, str]:
        code, out, err = self.run_cli(*argv)
        self.assertNotEqual(code, 0, f"command {argv} should have failed: {out}")
        return out, err

    def signup(self, name: str = "ana", passphrase: str = PASSPHRASE) -> None:
        self.ok("signup", "--name", name, "--passphrase", passphrase, "--confirm", passphrase)

    def write_grid(self, filename: str, rows, legend: dict[str, str] | None = None, sep: str = ",") -> Path:
        lines = [f"# {symbol} = {name}" for symbol, name in (legend or {}).items()]
        for row in rows:
            lines.append(sep.join(row) if isinstance(row, (list, tuple)) else row)
        path = self.root / filename
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def import_pattern(self, rows, legend=None, name=None, filename="grid.csv") -> str:
        path = self.write_grid(filename, rows, legend or {})
        argv = ["import", str(path)]
        if name:
            argv += ["--name", name]
        out = self.ok(*argv)
        for token in out.split():
            if token.startswith("p_"):
                return token.rstrip(",")
        self.fail(f"no pattern id in import output: {out}")

    def state(self) -> dict:
        return json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))

    def small_pattern(self, name: str = "Spring Lattice") -> str:
        rows = [list("ABCD"), list("BCDA"), list("CDAB"), list("DABC")]
        legend = {"A": "Teal", "B": "Coral", "C": "Sand", "D": "Indigo"}
        pattern_id = self.import_pattern(rows, legend, name=name)
        return pattern_id


class HelpAndAuthTests(AtlasTestCase):
    def test_help_lists_every_subcommand(self) -> None:
        out = self.ok("--help")
        for command in (
            "signup", "login", "logout", "status", "import", "list",
            "show", "open", "rename", "delete", "edit", "fold", "colors", "export", "demo",
        ):
            self.assertIn(command, out)

    def test_short_passphrase_is_rejected_and_nothing_is_written(self) -> None:
        code, _out, err = self.run_cli("signup", "--name", "ana", "--passphrase", "short", "--confirm", "short")
        self.assertNotEqual(code, 0)
        self.assertIn("at least 8 characters", err)
        self.assertFalse((self.state_dir / "state.json").exists())

    def test_signup_stores_only_a_scrypt_hash(self) -> None:
        self.signup()
        raw = (self.state_dir / "state.json").read_text(encoding="utf-8")
        self.assertNotIn(PASSPHRASE, raw)
        state = self.state()
        profile = state["profiles"][0]
        self.assertEqual(profile["name"], "ana")
        self.assertEqual(profile["kdf"], "scrypt n=16384,r=8,p=1")
        self.assertEqual(len(bytes.fromhex(profile["salt"])), 16)
        self.assertEqual(state["active_profile_id"], profile["id"])

    def test_wrong_passphrase_and_unknown_profile_share_one_message(self) -> None:
        self.signup()
        _out1, err_wrong = self.fail("login", "--name", "ana", "--passphrase", "wrong-passphrase")
        _out2, err_unknown = self.fail("login", "--name", "nobody", "--passphrase", "wrong-passphrase")
        self.assertIn("unknown profile or wrong passphrase", err_wrong)
        self.assertEqual(err_wrong.splitlines()[0], err_unknown.splitlines()[0])
        self.assertEqual(self.state()["active_profile_id"], self.state()["profiles"][0]["id"])

    def test_logout_then_commands_refuse_with_login_hint(self) -> None:
        self.signup()
        out = self.ok("logout")
        self.assertIn("signed out of ana", out)
        self.assertIsNone(self.state()["active_profile_id"])
        _out, err = self.fail("list")
        self.assertIn("not signed in", err)
        self.assertIn("login --name", err)

    def test_status_reports_active_profile_and_logout_hint(self) -> None:
        self.signup()
        out = self.ok("status")
        self.assertIn("signed in as ana", out)
        self.assertIn("logout", out)


class ImportTests(AtlasTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.signup()

    def test_import_sample_grid_saves_dimensions_and_legend(self) -> None:
        out = self.ok("import", str(PROJECT_ROOT / "examples" / "sample.stitch.csv"))
        self.assertIn("detected delimiter: comma (,)", out)
        self.assertIn("4 columns x 4 rows", out)
        state = self.state()
        pattern = state["patterns"][0]
        self.assertEqual((pattern["width"], pattern["height"]), (4, 4))
        self.assertEqual(pattern["palette"], {"A": "Teal", "B": "Coral", "C": "Sand", "D": "Indigo"})

    def test_row_width_mismatch_names_row_and_saves_nothing(self) -> None:
        path = self.write_grid(
            "ragged.csv",
            [["A", "B", "C", "D"], ["A", "B", "C", "D"], ["A", "B", "C", "D", "A"]],
            {"A": "Teal"},
        )
        _out, err = self.fail("import", str(path))
        self.assertIn("row 3", err)
        self.assertIn("expected 4 symbols", err)
        self.assertIn("found 5", err)
        self.assertNotIn("patterns", self.state() or {})
        self.assertEqual(self.state()["patterns"], [])

    def test_missing_legend_symbols_get_auto_assigned(self) -> None:
        path = self.write_grid("auto.csv", [["A", "x"], ["x", "A"]], {"A": "Teal"})
        out = self.ok("import", str(path))
        self.assertIn("auto-assigned symbols:", out)
        self.assertIn("x -> Auto colour 1", out)
        palette = self.state()["patterns"][0]["palette"]
        self.assertEqual(palette["A"], "Teal")
        self.assertEqual(palette["x"], "Auto colour 1")

    def test_character_grid_without_separators_imports(self) -> None:
        path = self.write_grid("chars.txt", ["AABC", "ABCA", "BCAA"])
        out = self.ok("import", str(path))
        self.assertIn("characters (no separator)", out)
        pattern = self.state()["patterns"][0]
        self.assertEqual((pattern["width"], pattern["height"]), (4, 3))

    def test_dry_run_import_saves_nothing(self) -> None:
        path = self.write_grid("dry.csv", [["A", "B"], ["B", "A"]], {"A": "Teal", "B": "Coral"})
        out = self.ok("import", str(path), "--dry-run")
        self.assertIn("dry run: nothing was saved", out)
        self.assertEqual(self.state()["patterns"], [])

    def test_missing_file_reports_resolved_path(self) -> None:
        missing = self.root / "nope.csv"
        _out, err = self.fail("import", str(missing))
        self.assertIn(str(missing.resolve()), err)


class LibraryTests(AtlasTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.signup()

    def test_list_shows_columns_sorted_newest_first(self) -> None:
        first = self.small_pattern("Alpha Tile")
        second = self.small_pattern("Beta Tile")
        out = self.ok("list")
        self.assertIn(first, out)
        self.assertIn(second, out)
        self.assertIn("4x4", out)
        self.assertLess(out.index(second), out.index(first))
        headers = out.splitlines()[1]
        for column in ("id", "name", "size", "symbols", "updated"):
            self.assertIn(column, headers)

    def test_empty_library_names_the_import_command(self) -> None:
        out = self.ok("list")
        self.assertIn("no patterns yet", out)
        self.assertIn("import examples/sample.stitch.csv", out)

    def test_open_by_ambiguous_prefix_shows_candidates(self) -> None:
        first = self.small_pattern("Teal Lattice")
        second = self.small_pattern("Teal Maze")
        _out, err = self.fail("open", "Teal")
        self.assertIn("matches 2 patterns", err)
        self.assertIn(first, err)
        self.assertIn(second, err)

    def test_rename_and_delete_with_confirmation(self) -> None:
        pattern_id = self.small_pattern("Old Name")
        out = self.ok("rename", pattern_id, "New Name")
        self.assertIn("renamed 'Old Name' -> 'New Name'", out)
        self.assertEqual(self.state()["patterns"][0]["name"], "New Name")
        _out, err = self.fail("delete", pattern_id, "--confirm", "wrong word")
        self.assertIn("delete cancelled", err)
        self.assertEqual(len(self.state()["patterns"]), 1)
        out = self.ok("delete", pattern_id, "--confirm", "New Name")
        self.assertIn("deleted pattern", out)
        self.assertEqual(self.state()["patterns"], [])


class ViewerTests(AtlasTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.signup()

    def test_ruler_row_numbers_and_legend(self) -> None:
        rows = [list("ABCDEFGHI") for _ in range(9)]
        legend = {"A": "Teal", "B": "Coral"}
        pattern_id = self.import_pattern(rows, legend, name="Ruled")
        self.ok("colors", "add", "--name", "Teal", "--brand", "Malabrigo", "--code", "#2f6f6a")
        self.ok("colors", "lot-add", "Teal", "--code", "D-118", "--quantity", "12", "--unit", "g")
        self.ok("colors", "add", "--name", "Coral")
        self.ok("colors", "lot-add", "Coral", "--code", "C-204", "--quantity", "25", "--unit", "g")
        out = self.ok("show", pattern_id)
        lines = out.splitlines()
        ruler = next(line for line in lines if "5" in line and "1" in line and "|" not in line)
        self.assertIn("5", ruler)
        self.assertIn("1|A B C D E F G H I", out)
        self.assertIn("9|A B C D E F G H I", out)
        self.assertIn("legend", out)
        self.assertIn("Teal", out)
        self.assertIn("stock warning", out)  # C..I have no colourway

    def test_paging_renders_second_page(self) -> None:
        rows = [[f"{index % 10}"] * 10 for index in range(120)]
        pattern_id = self.import_pattern(rows, {"0": "Teal"}, name="Tall")
        out = self.ok("show", pattern_id, "--page", "2")
        self.assertIn("page 2/2", out)
        self.assertIn("rows 61-120", out)
        self.assertIn(" 61|", out)
        self.assertIn("120|", out)
        self.assertNotIn("  1|", out)

    def test_cell_probe_and_out_of_bounds(self) -> None:
        pattern_id = self.small_pattern()
        self.ok("colors", "add", "--name", "Teal")
        self.ok("colors", "lot-add", "Teal", "--code", "D-118", "--quantity", "12", "--unit", "g", "--note", "slightly dark")
        out = self.ok("show", pattern_id, "--cell", "1,1")
        self.assertIn("cell 1,1: symbol A -> Teal", out)
        self.assertIn("slightly dark", out)
        _out, err = self.fail("show", pattern_id, "--cell", "9,9")
        self.assertIn("outside the 4x4 grid", err)

    def test_page_out_of_range_is_reported(self) -> None:
        pattern_id = self.small_pattern()
        _out, err = self.fail("show", pattern_id, "--page", "5")
        self.assertIn("out of range", err)


class EditTests(AtlasTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.signup()
        self.pattern_id = self.small_pattern()

    def test_cell_edit_prints_change_and_show_reflects_it(self) -> None:
        out = self.ok("edit", self.pattern_id, "--cell", "3,4=B")
        self.assertIn("3,4: A -> B", out)
        show = self.ok("show", self.pattern_id)
        self.assertIn("3|C D A B", show)

    def test_rectangle_edit_and_undo_restore(self) -> None:
        out = self.ok("edit", self.pattern_id, "--rect", "1,1,2,2=A")
        self.assertIn("applying 2 changes", out)
        self.assertIn("1,1: A -> A", out.splitlines()[-6:][0] or out)  # change lines printed below
        out = self.ok("edit", self.pattern_id, "--rect", "1,2,2,2=D")
        self.assertIn("1,2: B -> D", out)
        before = self.state()["patterns"][0]["rows"]
        out = self.ok("edit", self.pattern_id, "--undo")
        self.assertIn("reverted edit", out)
        after = self.state()["patterns"][0]["rows"]
        self.assertNotEqual(before, after)
        self.assertEqual(after[0], "ABCD")
        self.assertEqual(after[1], "BCDA")

    def test_dry_run_keeps_pattern_and_history_untouched(self) -> None:
        before = self.state()["patterns"][0]
        out = self.ok("edit", self.pattern_id, "--cell", "1,1=D", "--dry-run")
        self.assertIn("1,1: A -> D", out)
        self.assertIn("dry run: nothing was written", out)
        after = self.state()["patterns"][0]
        self.assertEqual(before["rows"], after["rows"])
        self.assertEqual(before["updated_at"], after["updated_at"])
        self.assertEqual(after["edit_history"], [])

    def test_add_symbol_inline_appears_in_legend(self) -> None:
        out = self.ok("edit", self.pattern_id, "--cell", "1,1=K", "--add-symbol", "K=Ochre")
        self.assertIn("added to the palette", out)
        self.assertIn("1,1: A -> K", out)
        show = self.ok("show", self.pattern_id)
        self.assertIn("K", show)
        self.assertIn("Ochre", show)

    def test_unknown_symbol_without_add_symbol_is_rejected(self) -> None:
        _out, err = self.fail("edit", self.pattern_id, "--cell", "1,1=K")
        self.assertIn("not in the palette", err)

    def test_out_of_bounds_cell_is_rejected(self) -> None:
        _out, err = self.fail("edit", self.pattern_id, "--cell", "10,1=A")
        self.assertIn("outside the 4x4 grid", err)

    def test_undo_without_history_is_reported(self) -> None:
        _out, err = self.fail("edit", self.pattern_id, "--undo")
        self.assertIn("no edits to undo", err)


class StockTests(AtlasTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.signup()

    def test_missing_colourway_is_flagged_once_per_symbol(self) -> None:
        pattern_id = self.import_pattern([list("ABC")], {"A": "Teal", "B": "Coral", "C": "Sand"})
        self.ok("colors", "add", "--name", "Teal")
        self.ok("colors", "lot-add", "Teal", "--code", "D-118", "--quantity", "12", "--unit", "g")
        self.ok("colors", "add", "--name", "Coral")
        self.ok("colors", "lot-add", "Coral", "--code", "C-204", "--quantity", "25", "--unit", "g")
        out = self.ok("show", pattern_id)
        self.assertIn("stock warning: 1 symbol has no stock (C)", out)

    def test_no_warning_when_every_symbol_is_stocked(self) -> None:
        pattern_id = self.import_pattern([list("AB")], {"A": "Teal", "B": "Coral"})
        for name, code in (("Teal", "D-118"), ("Coral", "C-204")):
            self.ok("colors", "add", "--name", name)
            self.ok("colors", "lot-add", name, "--code", code, "--quantity", "12", "--unit", "g")
        out = self.ok("show", pattern_id)
        self.assertNotIn("stock warning", out)

    def test_zero_stock_lot_is_flagged(self) -> None:
        pattern_id = self.import_pattern([list("A")], {"A": "Teal"})
        self.ok("colors", "add", "--name", "Teal")
        self.ok("colors", "lot-add", "Teal", "--code", "D-118", "--quantity", "0", "--unit", "g")
        out = self.ok("show", pattern_id)
        self.assertIn("stock warning: 1 symbol has no stock (A)", out)


class InventoryTests(AtlasTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.signup()

    def test_empty_inventory_shows_add_command_and_example(self) -> None:
        out = self.ok("colors", "list")
        self.assertIn("no colourways yet", out)
        self.assertIn("colors add --name Teal", out)
        self.assertIn("colors lot-add Teal --code D-118", out)

    def test_lot_totals_and_notes_render(self) -> None:
        self.ok("colors", "add", "--name", "Teal", "--brand", "Malabrigo", "--code", "#2f6f6a")
        self.ok("colors", "lot-add", "Teal", "--code", "D-118", "--quantity", "12", "--unit", "g",
                "--note", "slightly dark", "--date", "2024-04-02")
        out = self.ok("colors", "list")
        self.assertIn("Teal", out)
        self.assertIn("Malabrigo", out)
        self.assertIn("#2f6f6a", out)
        self.assertIn("lots 1", out)
        self.assertIn("stock 12 g", out)
        self.assertIn("slightly dark", out)
        self.assertIn("2024-04-02", out)

    def test_duplicate_colourway_is_rejected(self) -> None:
        self.ok("colors", "add", "--name", "Teal")
        _out, err = self.fail("colors", "add", "--name", "teal")
        self.assertIn("already exists", err)

    def test_malformed_quantity_is_rejected(self) -> None:
        self.ok("colors", "add", "--name", "Teal")
        _out, err = self.fail("colors", "lot-add", "Teal", "--code", "D-118", "--quantity", "twelve")
        self.assertIn("not a number", err)

    def test_unknown_colourway_for_lot_is_reported(self) -> None:
        _out, err = self.fail("colors", "lot-add", "Missing", "--code", "D-118", "--quantity", "1")
        self.assertIn("no colourway named 'Missing'", err)

    def test_lot_delete_needs_matching_confirmation(self) -> None:
        self.ok("colors", "add", "--name", "Teal")
        self.ok("colors", "lot-add", "Teal", "--code", "D-118", "--quantity", "12", "--unit", "g")
        self.ok("colors", "lot-add", "Teal", "--code", "D-121", "--quantity", "6", "--unit", "g")
        _out, err = self.fail("colors", "lot-delete", "Teal", "--code", "D-118", "--confirm", "nope")
        self.assertIn("delete cancelled", err)
        self.assertEqual(len(self.state()["dye_lots"]), 2)
        out = self.ok("colors", "lot-delete", "Teal", "--code", "D-118", "--confirm", "D-118")
        self.assertIn("deleted lot D-118", out)
        self.assertIn("1 lot, 6 g total", out)
        self.assertEqual(len(self.state()["dye_lots"]), 1)

    def test_colourway_delete_removes_its_lots(self) -> None:
        self.ok("colors", "add", "--name", "Teal")
        self.ok("colors", "lot-add", "Teal", "--code", "D-118", "--quantity", "12", "--unit", "g")
        out = self.ok("colors", "delete", "Teal", "--confirm", "Teal")
        self.assertIn("deleted colourway 'Teal'", out)
        state = self.state()
        self.assertEqual(state["colorways"], [])
        self.assertEqual(state["dye_lots"], [])


class FoldTests(AtlasTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.signup()
        self.pattern_id = self.import_pattern([list("ABCD" * 3) for _ in range(12)],
                                              {"A": "Teal"}, name="Twelve")

    def test_fold_set_and_preview_list_faces(self) -> None:
        out = self.ok("fold", "set", self.pattern_id, "r4", "r8")
        self.assertIn("fold plan saved", out)
        self.assertIn("faces ABC", out)
        preview = self.ok("fold", "preview", self.pattern_id)
        self.assertIn("creases: r4, r8", preview)
        for face in ("A", "B", "C"):
            self.assertIn(face, preview)
        self.assertIn("1-4", preview)
        self.assertIn("9-12", preview)
        self.assertIn("isometric net preview", preview)

    def test_out_of_range_crease_is_not_saved(self) -> None:
        _out, err = self.fail("fold", "set", self.pattern_id, "r40")
        self.assertIn("fold line r40 is outside the 12-row grid", err)
        self.assertIn("not saved", err)
        self.assertEqual(self.state()["fold_plans"], [])

    def test_duplicate_crease_is_not_saved(self) -> None:
        _out, err = self.fail("fold", "set", self.pattern_id, "r4", "r4")
        self.assertIn("two faces would claim the same region", err)
        self.assertEqual(self.state()["fold_plans"], [])

    def test_saved_plan_survives_a_new_run(self) -> None:
        self.ok("fold", "set", self.pattern_id, "r4", "r8")
        self.tearDown_state = None
        out = self.ok("show", self.pattern_id)
        self.assertIn("fold plan: 2 creases (r4, r8)", out)
        preview = self.ok("fold", "preview", self.pattern_id)
        self.assertIn("faces: 3 (max 6)", preview)

    def test_preview_without_plan_explains_the_command(self) -> None:
        _out, err = self.fail("fold", "preview", self.pattern_id)
        self.assertIn("no fold plan saved", err)
        self.assertIn("fold set", err)


class ExportTests(AtlasTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.signup()
        self.pattern_id = self.small_pattern("Spring Lattice")
        self.ok("colors", "add", "--name", "Teal", "--brand", "Malabrigo", "--code", "#2f6f6a")
        self.ok("colors", "lot-add", "Teal", "--code", "D-118", "--quantity", "12", "--unit", "g",
                "--note", "slightly dark", "--date", "2024-04-02")

    def test_txt_export_has_header_ruler_and_lots(self) -> None:
        self.ok("export", "txt", self.pattern_id, "--out-dir", str(self.out_dir))
        path = self.out_dir / "spring-lattice.stitchmap.txt"
        self.assertTrue(path.exists())
        body = path.read_text(encoding="utf-8")
        self.assertIn("TESSERACTATLAS STITCH MAP", body)
        self.assertIn("pattern : Spring Lattice", body)
        self.assertIn("size    : 4 columns x 4 rows", body)
        self.assertIn("band 1 of 1", body)
        self.assertIn("|A B C D", body)
        self.assertIn("legend and dye lots", body)
        self.assertIn("slightly dark", body)

    def test_txt_export_is_byte_identical_and_refuses_overwrite(self) -> None:
        self.ok("export", "txt", self.pattern_id, "--out-dir", str(self.out_dir))
        path = self.out_dir / "spring-lattice.stitchmap.txt"
        first = path.read_bytes()
        _out, err = self.fail("export", "txt", self.pattern_id, "--out-dir", str(self.out_dir))
        self.assertIn("already exists", err)
        self.assertIn("--force", err)
        self.assertEqual(path.read_bytes(), first)
        self.ok("export", "txt", self.pattern_id, "--out-dir", str(self.out_dir), "--force")
        self.assertEqual(path.read_bytes(), first)

    def test_html_export_has_print_css_and_no_network_references(self) -> None:
        out = self.ok("export", "html", self.pattern_id, "--out-dir", str(self.out_dir))
        self.assertIn("print-ready", out)
        path = self.out_dir / "spring-lattice.stitchmap.html"
        body = path.read_text(encoding="utf-8")
        self.assertIn("@media print", body)
        self.assertIn("oklch(0.58 0.12 276)", body)
        self.assertIn("band 1 of 1", body)
        self.assertIn("columns 1-4", body)
        self.assertNotIn("http", body.lower())

    def test_wide_html_export_splits_into_labelled_bands(self) -> None:
        rows = [list("ABCD" * 30) for _ in range(4)]  # 120 columns
        pattern_id = self.import_pattern(rows, {"A": "Teal"}, name="Wide")
        self.ok("export", "html", pattern_id, "--out-dir", str(self.out_dir))
        body = (self.out_dir / "wide.stitchmap.html").read_text(encoding="utf-8")
        self.assertIn("band 1 of 2", body)
        self.assertIn("band 2 of 2", body)
        self.assertIn("columns 1-60", body)
        self.assertIn("columns 61-120", body)
        self.assertEqual(body.count("<table>"), 2)

    def test_export_list_reports_paths_and_sizes(self) -> None:
        self.ok("export", "txt", self.pattern_id, "--out-dir", str(self.out_dir))
        self.ok("export", "html", self.pattern_id, "--out-dir", str(self.out_dir))
        out = self.ok("export", "list", self.pattern_id)
        self.assertIn("spring-lattice.stitchmap.txt", out)
        self.assertIn("spring-lattice.stitchmap.html", out)
        self.assertIn("bytes", out)

    def test_export_commands_report_every_recorded_path_with_bytes(self) -> None:
        txt_out = self.ok("export", "txt", self.pattern_id, "--out-dir", str(self.out_dir))
        self.assertIn("bytes", txt_out)
        html_out = self.ok("export", "html", self.pattern_id, "--out-dir", str(self.out_dir))
        self.assertIn("spring-lattice.stitchmap.txt", html_out)
        self.assertIn("spring-lattice.stitchmap.html", html_out)
        self.assertIn("bytes", html_out)

    def test_export_list_empty_state_names_the_command(self) -> None:
        out = self.ok("export", "list", self.pattern_id)
        self.assertIn("no exports yet", out)
        self.assertIn("export txt", out)


class StateTests(AtlasTestCase):
    def test_corrupt_state_reports_path_and_recovers_with_reset(self) -> None:
        self.signup()
        state_file = self.state_dir / "state.json"
        state_file.write_text('{"profiles": [', encoding="utf-8")
        _out, err = self.fail("list")
        self.assertIn(str(state_file), err)
        self.assertIn("--reset-state", err)
        self.assertIn("not valid JSON", err)
        out = self.ok("status", "--reset-state")
        self.assertIn("not signed in", out)
        archived = list(self.state_dir.glob("state.json.corrupt*"))
        self.assertEqual(len(archived), 1)
        _out, err = self.fail("list")
        self.assertIn("signup --name", err)

    def test_state_write_is_atomic_and_leaves_no_temp_files(self) -> None:
        self.signup()
        self.small_pattern()
        leftovers = [p.name for p in self.state_dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        self.assertIsInstance(self.state(), dict)


class DemoTests(AtlasTestCase):
    def test_demo_seeds_everything_and_reports_paths(self) -> None:
        out = self.ok("demo", "--out-dir", str(self.out_dir))
        self.assertIn("demo profile created: cenius", out)
        self.assertIn("Spiral Tessellation", out)
        self.assertIn("colourways: 4", out)
        self.assertIn("dye lots: 6", out)
        self.assertIn("faces ABCDEF", out)
        txt = self.out_dir / "spiral-tessellation.stitchmap.txt"
        html = self.out_dir / "spiral-tessellation.stitchmap.html"
        self.assertTrue(txt.exists())
        self.assertTrue(html.exists())
        self.assertIn(str(txt), out)
        state = self.state()
        self.assertEqual(len(state["patterns"]), 1)
        pattern = state["patterns"][0]
        self.assertEqual((pattern["width"], pattern["height"]), (16, 16))

    def test_demo_is_idempotent_and_demo_login_works(self) -> None:
        self.ok("demo", "--out-dir", str(self.out_dir))
        first = self.state()
        out = self.ok("demo", "--out-dir", str(self.out_dir))
        self.assertIn("demo profile reused: cenius", out)
        second = self.state()
        self.assertEqual(len(second["profiles"]), 1)
        self.assertEqual(len(second["patterns"]), 1)
        self.assertEqual(len(second["colorways"]), 4)
        self.assertEqual(len(second["dye_lots"]), 6)
        self.assertEqual(len(second["fold_plans"]), 1)
        self.assertEqual(first["patterns"][0]["id"], second["patterns"][0]["id"])
        login = self.ok("login", "--name", "cenius", "--passphrase", "cenius")
        self.assertIn("signed in as cenius", login)
        self.ok("show", second["patterns"][0]["id"])
        self.ok("colors", "list")

    def test_demo_profile_signs_in_by_email_and_demo_flag(self) -> None:
        self.ok("demo", "--out-dir", str(self.out_dir))
        out = self.ok("login", "--name", "cenius@cenius.ai", "--passphrase", "cenius")
        self.assertIn("signed in as cenius", out)
        self.ok("logout")
        out = self.ok("login", "--demo")
        self.assertIn("signed in as cenius", out)


if __name__ == "__main__":
    unittest.main()

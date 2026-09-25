"""The .xlsx reader (SPEC-PATCH-005 P1.5): uploads become the evaluator's grid, bad cells become
warnings, never crashes, and nothing a warning says quotes a cell (B.6)."""

from __future__ import annotations

import io
import os
import zipfile
from datetime import date, datetime
from pathlib import Path

import pytest

from app.adapters import xlsx_reader
from app.adapters.xlsx_reader import XlsxError, read_xlsx
from app.schemas.config import ConfigSpec
from app.services.grid import CellFormat, DataValidation, Workbook
from app.services.preview import compute_preview
from tests.conftest import load_reference_raw
from tests.fixtures import reference_workbook
from tests.xlsx_builder import Cell, Sheet, build, from_workbook

TZ = "Asia/Kolkata"


def read(sheets: list[Sheet], **kw: bool) -> xlsx_reader.XlsxRead:
    return read_xlsx(build(sheets, **kw), timezone=TZ)


def one(cells: dict[tuple[int, int], Cell], **sheet_kw: object) -> xlsx_reader.XlsxRead:
    return read([Sheet("T", cells, **sheet_kw)])  # type: ignore[arg-type]


def with_date_formats(workbook: Workbook) -> Workbook:
    for tab in workbook.tabs:
        for r, row in enumerate(tab.values, start=1):
            for c, v in enumerate(row, start=1):
                if isinstance(v, (date, datetime)):
                    tab.number_formats[(r, c)] = "d/m/yyyy"
    return workbook


# ---------------------------------------------------------------- the grid, round trip

def test_the_reference_workbook_survives_a_round_trip() -> None:
    """Written out as a Sheets export would carry it, and read back: every tab, value, colour,
    strike-through and number format is what the evaluator started with."""
    original = with_date_formats(reference_workbook.build())
    machines = original.tab("MACHINES")
    assert machines is not None
    machines.formats[3][5] = CellFormat(font="#CC0000", background="#FFF2CC", strike=True)
    got = read_xlsx(from_workbook(original), timezone=TZ)
    assert got.warnings == []
    assert got.grid.timezone == TZ
    assert [t.name for t in got.grid.workbook.tabs] == [t.name for t in original.tabs]
    for want in original.tabs:
        tab = got.grid.workbook.tab(want.name)
        assert tab is not None
        assert tab.values == [[None if v == "" else v for v in row] for row in want.values], want.name
        assert tab.formats == want.formats, want.name
        assert tab.number_formats == want.number_formats, want.name


def test_a_read_upload_previews_like_the_fixture() -> None:
    """End to end on the P2 path: upload -> grid -> the evaluator's preview, same as the fixture."""
    config = ConfigSpec.model_validate(load_reference_raw())
    now = datetime(2026, 9, 16, 10, 0)
    uploaded = read_xlsx(from_workbook(reference_workbook.build()), timezone=TZ).grid
    from app.adapters.base import Grid
    workbook = reference_workbook.build()
    for tab in workbook.tabs:  # the hand-built fixture writes "" where a read sheet has None
        tab.values = [[None if v == "" else v for v in row] for row in tab.values]
    fixture = Grid(workbook, timezone=TZ)
    assert compute_preview(uploaded, config, now=now) == compute_preview(fixture, config, now=now)


# ---------------------------------------------------------------- the two known bad cells

def test_a_date_in_the_year_95637_is_a_warning_not_a_crash() -> None:
    """The reference workbook holds a date-formatted number far past year 9999. It is read as
    the number, and the warning names the cell but not what is in it."""
    far = 34_219_000  # a serial in the year 95637
    got = one({(1, 1): Cell("DATE"), (2, 1): Cell(far, number_format="d/m/yyyy"),
               (3, 1): Cell(date(2026, 9, 1))})
    tab = got.grid.workbook.tabs[0]
    assert tab.values[1][0] == far and tab.values[2][0] == date(2026, 9, 1)
    assert len(got.warnings) == 1 and got.warnings[0].startswith("T!A2: date is outside the range")
    assert str(far) not in got.warnings[0]


def test_a_column_without_a_header_is_read_and_the_evaluator_runs_over_it() -> None:
    """Data in a column whose header cell is blank: read as is; the evaluator ignores the column
    (it has no name to match) and the preview still runs."""
    workbook = reference_workbook.build()
    machines = workbook.tab("MACHINES")
    assert machines is not None
    machines.ensure_size(machines.height, machines.width + 1)
    machines.values[4][machines.width - 1] = "note without a header"
    got = read_xlsx(from_workbook(workbook), timezone=TZ)
    tab = got.grid.workbook.tab("MACHINES")
    assert tab is not None and tab.values[1][-1] is None and tab.values[4][-1] == "note without a header"
    config = ConfigSpec.model_validate(load_reference_raw())  # trailing blank headers do not change the hash
    compute_preview(got.grid, config, now=datetime(2026, 9, 16, 10, 0))


# ---------------------------------------------------------------- cells

def test_value_kinds() -> None:
    got = one({(1, 1): Cell("shared"), (1, 2): Cell("inline", kind="inlineStr"), (1, 3): Cell(True),
               (1, 4): Cell(3.5), (1, 5): Cell(7), (1, 6): Cell("#N/A", kind="e"),
               (1, 7): Cell(datetime(2026, 9, 16, 14, 30)), (1, 8): Cell(date(2026, 9, 16))})
    assert got.grid.workbook.tabs[0].values[0] == [
        "shared", "inline", True, 3.5, 7, "#N/A", datetime(2026, 9, 16, 14, 30), date(2026, 9, 16)]


def test_formulas_keep_their_result_and_are_flagged() -> None:
    got = one({(1, 1): Cell(2), (1, 2): Cell(4, formula="A1*2"), (1, 3): Cell("x", formula='"x"', kind="str")})
    assert got.grid.workbook.tabs[0].values[0] == [2, 4, "x"]
    assert got.grid.tabs["T"].formula_cells == {(1, 2), (1, 3)}


def test_colours_in_every_form_a_file_uses() -> None:
    got = one({(1, 1): Cell("rgb", font={"rgb": "FFCC0000"}),
               (1, 2): Cell("theme", fill={"theme": "4"}),
               (1, 3): Cell("tinted", fill={"theme": "4", "tint": "0.3999"}),
               (1, 4): Cell("indexed", font={"indexed": "10"}),
               (1, 5): Cell("auto", font={"auto": "1"}),
               (1, 6): Cell("struck", strike=True),
               (1, 7): Cell("dark text", font={"theme": "1"}),
               (1, 8): Cell("light fill", fill={"theme": "0"})})
    fmts = got.grid.workbook.tabs[0].formats[0]
    assert fmts[0].font == "#CC0000"
    assert fmts[1].background == "#4472C4"
    assert fmts[2].background == "#8FAADC"  # accent1, 40% lighter, as Sheets/Excel show it
    assert fmts[3].font == "#FF0000"
    assert fmts[4].font is None
    assert fmts[5].strike is True
    assert fmts[6].font == "#000000" and fmts[7].background == "#FFFFFF", "theme 0 is light 1, 1 is dark 1"


def test_merged_cells_keep_their_value_in_the_first_cell() -> None:
    got = one({(1, 1): Cell("TITLE"), (2, 1): Cell("A"), (2, 2): Cell("B"), (3, 1): Cell(1), (3, 2): Cell(2)},
              merges=["A1:B1"])
    tab = got.grid.workbook.tabs[0]
    assert tab.values[0] == ["TITLE", None]
    assert got.grid.tabs["T"].merged == ("A1:B1",)


def test_list_dropdowns_are_read_and_other_kinds_ignored() -> None:
    got = one({(1, 1): Cell("STATUS"), (2, 1): Cell("Open"), (3, 1): Cell("Shut")},
              validations=[("A2:A3", "Open,Shut", True), ("A9:A20", "x", True), ("A1:A3", "$B$1:$B$3", True)])
    assert got.grid.workbook.tabs[0].validations == {
        (2, 1): DataValidation(("Open", "Shut"), allow_invalid=False),
        (3, 1): DataValidation(("Open", "Shut"), allow_invalid=False)}


def test_the_grid_ends_at_the_last_value() -> None:
    """Formatted but empty rows and columns past the data are not part of the tab (getLastRow)."""
    got = one({(1, 1): Cell("A"), (2, 1): Cell(1), (9, 1): Cell(None, fill={"rgb": "FFFFFF00"}),
               (2, 6): Cell(None, fill={"rgb": "FFFFFF00"})})
    tab = got.grid.workbook.tabs[0]
    assert (tab.height, tab.width) == (2, 1)


def test_hidden_tabs_and_the_1904_date_system() -> None:
    got = read([Sheet("V", {(1, 1): Cell("x")}), Sheet("H", {(1, 1): Cell("y")}, hidden=True)])
    assert [t.hidden for t in got.grid.workbook.tabs] == [False, True]
    raw_1904 = read([Sheet("T", {(1, 1): Cell(0, number_format="yyyy-mm-dd")})], date1904=True)
    assert raw_1904.grid.workbook.tabs[0].values[0][0] == date(1904, 1, 1)


def test_date_formats_are_recognised_and_other_formats_are_not() -> None:
    assert xlsx_reader.is_date_format(14, None)
    assert xlsx_reader.is_date_format(164, "dd-mmm-yyyy")
    assert xlsx_reader.is_date_format(164, "[$-409]d/m/yy h:mm AM/PM;@")
    assert not xlsx_reader.is_date_format(164, '0.00" days"')
    assert not xlsx_reader.is_date_format(164, "[Red]#,##0")
    assert not xlsx_reader.is_date_format(0, "General")


# ---------------------------------------------------------------- bad files

def test_not_an_xlsx() -> None:
    with pytest.raises(XlsxError, match="not an .xlsx"):
        read_xlsx(b"PK\x03\x04 not really", timezone=TZ)


def test_a_zip_without_a_workbook() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(XlsxError, match="no workbook"):
        read_xlsx(out.getvalue(), timezone=TZ)


def test_the_timezone_is_required_and_checked() -> None:
    with pytest.raises(XlsxError, match="unknown timezone"):
        read_xlsx(build([Sheet("T", {(1, 1): Cell("x")})]), timezone="Mars/Olympus")


def test_a_file_that_unpacks_too_large_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xlsx_reader, "MAX_UNPACKED_BYTES", 100)
    with pytest.raises(XlsxError, match="unpacks to more"):
        read_xlsx(build([Sheet("T", {(1, 1): Cell("x")})]), timezone=TZ)


def test_warnings_and_errors_never_quote_cell_values() -> None:
    secret = "Customer Secret Ltd"
    got = one({(1, 1): Cell(secret), (2, 1): Cell(99_999_999, number_format="d/m/yyyy"),
               (3, 1): Cell(secret, raw="not-a-number", kind="n")})
    assert got.warnings and all(secret not in w and "not-a-number" not in w for w in got.warnings)
    assert "Grid(" in repr(got.grid) and secret not in repr(got.grid)


# ---------------------------------------------------------------- the real reference workbook

REAL = os.environ.get("REFERENCE_XLSX")


@pytest.mark.skipif(not REAL, reason="set REFERENCE_XLSX to a local export of the reference workbook")
def test_the_real_reference_workbook_reads() -> None:
    """P1.5 exit: the owner's own export, never committed. It must read without crashing, with
    the two known bad cells reported as warnings rather than errors."""
    got = read_xlsx(Path(REAL or "").read_bytes(), timezone=TZ)
    names = [t.name for t in got.grid.workbook.tabs]
    assert {"MACHINES", "SPARES"} <= set(names), names
    assert any("outside the range" in w for w in got.warnings), got.warnings


def test_the_header_row_is_found_on_an_upload() -> None:
    """The compile step's header detection works on a read upload: the title banner is row 1,
    the headers row 2 (the reference layout)."""
    from app.agent.profile import detect_header_row
    grid = read_xlsx(from_workbook(reference_workbook.build()), timezone=TZ).grid
    for name in ("MACHINES", "SPARES"):
        tab = grid.workbook.tab(name)
        assert tab is not None and detect_header_row(tab) == 2, name


@pytest.mark.skipif(not REAL, reason="set REFERENCE_XLSX to a local export of the reference workbook")
def test_the_reference_rules_agree_on_the_real_layout() -> None:
    """The reference rules on the owner's real layout (headers re-fingerprinted from the file): the
    generated script and the evaluator produce the same sheet. Failures name cells, never values."""
    from app.emitters.apps_script import emit
    from app.services.conditions import EvalContext
    from app.services.executor import execute_run
    from app.services.grid import col_letter
    from app.services.headers import build_view
    from app.services.preflight import schema_hashes_for
    from app.services.runner import RunEvent
    from tests.test_apps_script_parity import run_generated
    from tests.test_engine_python_parity import NODE, _engine_fmt, _engine_values, _py_fmt, _py_values

    if NODE is None:
        pytest.skip("Node.js not installed")
    wb = read_xlsx(Path(REAL or "").read_bytes(), timezone=TZ).grid.workbook
    raw = load_reference_raw()
    probe = ConfigSpec.model_validate(raw)
    governed = [t.name for t in wb.tabs if t.name != "SUMMARY" and build_view(t, probe).has("STATUS")]
    raw["schema_hashes"] = schema_hashes_for(wb, governed, raw["header_row"])
    config = ConfigSpec.model_validate(raw)
    script = emit(config, workbook=wb).script
    assert script is not None
    py = execute_run(config, wb, RunEvent.manual(),
                     EvalContext(run_id="real", today=date(2026, 9, 16), now=datetime(2026, 9, 16, 10)))
    assert py.plan.status == "OK"
    got = {t["name"]: t for t in run_generated(script, wb)["tabs"]}
    assert list(got) == [t.name for t in py.workbook.tabs]
    where: list[str] = []
    for tab in py.workbook.tabs:
        ev, pv = _engine_values(got[tab.name]), _py_values(tab)
        for r in range(max(len(ev), len(pv))):
            er, pr = (ev[r] if r < len(ev) else []), (pv[r] if r < len(pv) else [])
            for c in range(max(len(er), len(pr))):
                a, b = (er[c] if c < len(er) else None), (pr[c] if c < len(pr) else None)
                if a != b and not (a in ("", None) and b in ("", None)):
                    where.append(f"{tab.name}!{col_letter(c + 1)}{r + 1} value")
                cell = got[tab.name]["cells"][r][c] if r < len(got[tab.name]["cells"]) and \
                    c < len(got[tab.name]["cells"][r]) else None
                if cell and r < tab.height and c < tab.width and _engine_fmt(cell) != _py_fmt(tab.formats[r][c]):
                    where.append(f"{tab.name}!{col_letter(c + 1)}{r + 1} format")
    assert not where, where[:20]

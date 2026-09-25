"""Offline parity: the generated Apps Script vs the Python evaluator (PATCH-005 P1).

Both targets are fed the same grid and compared cell by cell, values and formats. The Python
evaluator is the oracle (PATCH-004 D.6, PATCH-005 B.1): a mismatch is a bug in the emitter.

The generated script runs in `engine/test/gas_mock.js`, the in-memory Apps Script mock that was
built for the retired Engine.gs and is already validated against `engine/reference/legacy.gs`
by `engine/test/engine.test.js` (the legacy golden files). This is the offline half of the proof;
the live half (PATCH-004 D.3, PATCH-005 P4) still has to run the same script in a real
spreadsheet before release.

The unmodified reference config, triggers included, is covered in test_apps_script_triggers.py.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from typing import Any

import pytest

from app.emitters.apps_script import ENTRY_POINT, emit
from app.schemas.config import ConfigSpec
from app.services.conditions import EvalContext
from app.services.executor import execute_run
from app.services.grid import Tab, Workbook
from app.services.preflight import schema_hashes_for
from app.services.runner import RunEvent
from tests.conftest import load_reference_raw
from tests.fixtures import reference_workbook
from tests.test_engine_python_parity import (
    HARNESS,
    NODE,
    _compare_formats,
    _compare_validations,
    _encode_workbook,
    _engine_values,
    _py_values,
)

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js not installed")

TODAY = date(2026, 9, 16)
NOW = f"{TODAY.isoformat()}T10:00:00"


def run_generated(script: str, workbook: Workbook) -> dict[str, Any]:
    """Run the emitted script's entry point over `workbook` in the mocked spreadsheet."""
    request = {
        "script": "generated", "scriptSource": script, "now": NOW,
        "workbook": _encode_workbook(workbook), "steps": [{"call": ENTRY_POINT}],
        "dump_min_rows": 40, "dump_min_cols": 12,
    }
    proc = subprocess.run([str(NODE), str(HARNESS)], input=json.dumps(request), capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stderr
    return dict(json.loads(proc.stdout))


def compare_grids(py_workbook: Workbook, response: dict[str, Any], header_row: int, label: str) -> None:
    tabs = {t["name"]: t for t in response["tabs"]}
    assert list(tabs) == [t.name for t in py_workbook.tabs], f"{label}: tab order"
    for tab in py_workbook.tabs:
        got = tabs[tab.name]
        assert _engine_values(got) == _py_values(tab), f"{label}: {tab.name} values"
        _compare_formats(tab, got, header_row, f"{label}: {tab.name}")
        _compare_validations(tab, got, f"{label}: {tab.name}")
        compare_presentation(tab, got, f"{label}: {tab.name}")
        for r, row in enumerate(got["cells"][tab.height:], start=tab.height + 1):
            # below the evaluator's grid the sheet must be untouched: nothing written, nothing painted
            for c, cell in enumerate(row, start=1):
                assert (cell["v"], cell["fc"], cell["bg"], cell["fl"]) == ("", "#000000", None, "none"), \
                    f"{label}: {tab.name} R{r}C{c} is below the data but was written"


def compare_presentation(tab: Tab, got: dict[str, Any], label: str) -> None:
    """Number formats per cell, widths per column, banding theme and range: as intent (2026-09-20)."""
    formats = {(r + 1, c + 1): cell["nf"] for r, row in enumerate(got["cells"])
               for c, cell in enumerate(row) if cell.get("nf")}
    assert formats == tab.number_formats, f"{label}: number formats"
    assert got["hidden"] == tab.hidden, f"{label}: hidden"
    assert {int(c): w for c, w in got["colWidths"].items()} == tab.column_widths, f"{label}: column widths"
    bands = [(b["theme"], b["row"], b["col"], b["nr"], b["nc"], b["header"], b["footer"])
             for b in got["bandings"]]
    b = tab.banding
    assert bands == ([] if b is None else [(b.theme, b.row, 1, b.rows, b.cols, False, False)]), \
        f"{label}: banding"


def config_header_row() -> int:
    return int(load_reference_raw()["header_row"])


def sort_and_format_config() -> ConfigSpec:
    raw = load_reference_raw()
    raw["rules"] = [r for r in raw["rules"] if r["action"] in ("sort", "format")]
    return ConfigSpec.model_validate(raw)


@pytest.fixture(scope="module")
def reference_case() -> tuple[ConfigSpec, Workbook, str]:
    config = sort_and_format_config()
    workbook = reference_workbook.build()
    result = emit(config, workbook=workbook)
    assert result.ok, [r.message for r in result.refusals]
    assert result.script is not None
    return config, workbook, result.script


def test_generated_script_matches_the_evaluator_on_the_reference_workbook(
    reference_case: tuple[ConfigSpec, Workbook, str],
) -> None:
    config, workbook, script = reference_case
    py = execute_run(config, workbook, RunEvent.manual(), EvalContext(run_id="parity", today=TODAY))
    assert py.plan.status == "OK"
    response = run_generated(script, workbook)
    compare_grids(py.workbook, response, config.header_row, "reference sort+format")


def test_running_the_script_twice_changes_nothing_the_second_time(
    reference_case: tuple[ConfigSpec, Workbook, str],
) -> None:
    """The evaluator is idempotent on its own output; the script must be too."""
    config, workbook, script = reference_case
    first = run_generated(script, workbook)
    once = _decode_workbook(first)
    second = run_generated(script, once)
    assert second["tabs"] == first["tabs"], "second run changed the sheet"


def test_held_rows_are_untouched_by_the_generated_script() -> None:
    """A `!hold` column is part of the header row, so the config is hashed against that sheet."""
    workbook = reference_workbook.build()
    tab = workbook.tab("MACHINES")
    assert tab is not None
    hold_col = tab.width
    tab.ensure_size(tab.height, hold_col + 1)
    tab.values[config_header_row() - 1][hold_col] = "!hold"
    for row in range(2, 5):
        tab.values[row][hold_col] = "YES"

    raw = load_reference_raw()
    raw["rules"] = [r for r in raw["rules"] if r["action"] in ("sort", "format")]
    raw["schema_hashes"] = schema_hashes_for(workbook, list(raw["schema_hashes"]), raw["header_row"])
    config = ConfigSpec.model_validate(raw)
    result = emit(config, workbook=workbook)
    assert result.ok and result.script is not None, [r.message for r in result.refusals]

    py = execute_run(config, workbook, RunEvent.manual(), EvalContext(run_id="hold", today=TODAY))
    assert py.plan.status == "OK"
    response = run_generated(result.script, workbook)
    compare_grids(py.workbook, response, config.header_row, "held rows")


def test_a_renamed_header_stops_the_whole_run_in_both_targets() -> None:
    """Invariant 4 parity: one drifted tab pauses everything, it does not organise the others."""
    config, workbook, script = None, reference_workbook.build(), None
    config = sort_and_format_config()
    result = emit(config, workbook=workbook)
    assert result.ok and result.script is not None
    script = result.script

    drifted = reference_workbook.build()
    tab = drifted.tab("MACHINES")
    assert tab is not None
    tab.values[config.header_row - 1][0] = "RENAMED HEADER"

    py = execute_run(config, drifted, RunEvent.manual(), EvalContext(run_id="drift", today=TODAY))
    assert py.plan.status == "PAUSED_DRIFT"
    response = run_generated(script, drifted)
    compare_grids(py.workbook, response, config.header_row, "drifted header")
    toast = " ".join(str(t) for t in response.get("results", []) or [])
    assert response["tabs"], toast


def _decode_workbook(response: dict[str, Any]) -> Workbook:
    """Rebuild a Workbook from the mock's dump, so one run's output can feed the next."""
    from app.services.grid import Banding, CellFormat

    tabs = []
    for dumped in response["tabs"]:
        cells = dumped["cells"]
        tab = Tab(dumped["name"], [[_decode_cell(c["v"]) for c in row] for row in cells])
        for r, row in enumerate(cells):
            for c, cell in enumerate(row):
                tab.formats[r][c] = CellFormat(font=cell["fc"], background=cell["bg"],
                                               strike=cell["fl"] == "line-through")
                if cell.get("nf"):
                    tab.number_formats[(r + 1, c + 1)] = cell["nf"]
        tab.column_widths = {int(c): w for c, w in dumped["colWidths"].items()}
        tab.protected = dumped["protected"]
        tab.hidden = dumped["hidden"]
        for b in dumped["bandings"]:
            tab.banding = Banding(b["theme"], b["row"], b["nr"], b["nc"])
        tabs.append(tab)
    return Workbook(tabs)


def _decode_cell(value: Any) -> Any:
    from datetime import datetime

    if isinstance(value, dict):
        if "$date" in value:
            return date.fromisoformat(value["$date"])
        if "$datetime" in value:
            return datetime.fromisoformat(value["$datetime"])
    return "" if value is None else value


# ---------------------------------------------------------------- presentation as intent

def with_date_formats(workbook: Workbook) -> Workbook:
    """The real workbook's sources format their dispatch dates, and not alike."""
    for name, pattern in (("MACHINES", "d/m/yyyy"), ("SPARES", "dd-mmm-yyyy")):
        tab = workbook.tab(name)
        assert tab is not None
        headers = [str(h).strip().upper() for h in tab.values[config_header_row() - 1]]
        col = next(i for i, h in enumerate(headers, start=1) if "DISPATCH DATE" in h)
        for row in range(config_header_row() + 1, tab.height + 1):
            tab.number_formats[(row, col)] = pattern
    return workbook


def run_both(raw: dict[str, Any], workbook: Workbook) -> tuple[Workbook, dict[str, Any]]:
    config = ConfigSpec.model_validate(raw)
    result = emit(config, workbook=workbook)
    assert result.ok and result.script is not None, [r.message for r in result.refusals]
    py = execute_run(config, workbook, RunEvent.manual(), EvalContext(run_id="present", today=TODAY))
    assert py.plan.status == "OK"
    response = run_generated(result.script, workbook)
    compare_grids(py.workbook, response, config.header_row, "presentation")
    return py.workbook, response


def summary_formats(workbook: Workbook) -> dict[str, set[str]]:
    tab = workbook.tab("SUMMARY")
    assert tab is not None
    headers = tab.values[config_header_row() - 1]
    out: dict[str, set[str]] = {}
    for (_, c), fmt in tab.number_formats.items():
        out.setdefault(str(headers[c - 1]).upper(), set()).add(fmt)
    return out


def test_summary_dates_match_the_first_source_and_the_rows_are_banded() -> None:
    py, _ = run_both(load_reference_raw(), with_date_formats(reference_workbook.build()))
    assert summary_formats(py) == {"DISPATCH DATE": {"d/m/yyyy"}}
    summary = py.tab("SUMMARY")
    assert summary is not None and summary.banding is not None
    assert (summary.banding.theme, summary.banding.row) == ("LIGHT_GREY", 3)
    assert summary.column_widths[1] == 120, "SOURCE SHEET's configured width, on a new tab"


def test_without_a_date_sort_key_the_first_date_header_decides() -> None:
    """No sort_like: match_source falls back to the first source's first header containing DATE."""
    raw = load_reference_raw()
    raw["rules"][2]["sort_like"] = None
    workbook = with_date_formats(reference_workbook.build())
    machines = workbook.tab("MACHINES")
    assert machines is not None
    machines.number_formats = {k: "yyyy-mm-dd" for k in machines.number_formats}
    py, _ = run_both(raw, workbook)
    assert summary_formats(py)["DISPATCH DATE"] == {"yyyy-mm-dd"}


def test_an_explicit_date_format_and_no_banding() -> None:
    raw = load_reference_raw()
    raw["rules"][2]["presentation"] |= {"date_format": "dd mmm yyyy", "banding": None}
    py, response = run_both(raw, with_date_formats(reference_workbook.build()))
    assert summary_formats(py)["DISPATCH DATE"] == {"dd mmm yyyy"}
    assert next(t for t in response["tabs"] if t["name"] == "SUMMARY")["bandings"] == []


def test_an_existing_summary_keeps_the_widths_its_owner_set() -> None:
    """Widths are set when the target is created; after that, dragged widths are the owner's."""
    workbook = reference_workbook.build()
    existing = Tab("SUMMARY", [["OLD TITLE"], ["SOURCE SHEET"]])
    existing.column_widths = {1: 333, 2: 44}
    workbook.tabs.insert(0, existing)
    py, _ = run_both(load_reference_raw(), workbook)
    summary = py.tab("SUMMARY")
    assert summary is not None and summary.column_widths == {1: 333, 2: 44}


def test_a_source_without_data_rows_does_not_decide_the_date_format() -> None:
    """MACHINES keeps a formatted but empty dispatch column; the first source WITH rows decides."""
    workbook = with_date_formats(reference_workbook.build())
    machines = workbook.tab("MACHINES")
    assert machines is not None
    machines.values = machines.values[:config_header_row()]
    machines.formats = machines.formats[:config_header_row()]
    machines.number_formats = {k: f for k, f in machines.number_formats.items() if k[0] == config_header_row() + 1}
    py, _ = run_both(load_reference_raw(), workbook)
    assert summary_formats(py)["DISPATCH DATE"] == {"dd-mmm-yyyy"}


def test_rebuilding_a_banded_summary_leaves_exactly_one_banding() -> None:
    """The second run finds the first run's banding on SUMMARY and must replace it, not stack."""
    config = ConfigSpec.model_validate(load_reference_raw())
    result = emit(config, workbook=reference_workbook.build())
    assert result.script is not None
    first = run_generated(result.script, with_date_formats(reference_workbook.build()))
    second = run_generated(result.script, _decode_workbook(first))
    assert second["tabs"] == first["tabs"], "a second run over its own output changes nothing"


def test_only_headers_containing_date_get_the_date_format() -> None:
    """'DATA OWNER' contains DAT but not DATE, and is left alone."""
    workbook = with_date_formats(reference_workbook.build())
    spares = workbook.tab("SPARES")
    assert spares is not None
    spares.ensure_size(spares.height, spares.width + 1)
    spares.values[config_header_row() - 1][spares.width - 1] = "DATA OWNER"
    spares.values[config_header_row()][spares.width - 1] = "Ravi"
    raw = load_reference_raw()
    raw["schema_hashes"] = schema_hashes_for(workbook, list(raw["schema_hashes"]), raw["header_row"])
    py, _ = run_both(raw, workbook)
    assert set(summary_formats(py)) == {"DISPATCH DATE"}

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
from app.services.grid import Workbook
from app.services.preflight import schema_hashes_for
from app.services.runner import RunEvent
from tests.conftest import load_reference_raw
from tests.fixtures import reference_workbook
from tests.test_engine_python_parity import (
    HARNESS,
    NODE,
    _compare_formats,
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
    from app.services.grid import CellFormat, Tab

    tabs = []
    for dumped in response["tabs"]:
        cells = dumped["cells"]
        tab = Tab(dumped["name"], [[_decode_cell(c["v"]) for c in row] for row in cells])
        for r, row in enumerate(cells):
            for c, cell in enumerate(row):
                tab.formats[r][c] = CellFormat(font=cell["fc"], background=cell["bg"],
                                               strike=cell["fl"] == "line-through")
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

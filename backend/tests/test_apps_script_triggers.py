"""Trigger parity: what starts each rule in the generated script vs the evaluator's rule selection.

The evaluator decides which rules an event runs (`runner.select_rules`): an edit runs the on_edit
rules watching that tab and column and marks debounced rules dirty; a debounced or scheduled event
runs the rules it names; `after` rules follow their parent on the same tabs. The generated script
wires the same decisions to Apps Script triggers. Each test drives the script through the mock's
triggers and compares the sheet with the evaluator run for the same event (PATCH-004 D.6: the
evaluator is the oracle).
"""

from __future__ import annotations

import copy
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
from app.services.runner import RunEvent
from tests.conftest import load_reference_raw
from tests.fixtures import reference_workbook
from tests.test_apps_script_parity import compare_grids
from tests.test_engine_python_parity import HARNESS, NODE, _encode_workbook

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js not installed")

TODAY = date(2026, 9, 16)
NOW = f"{TODAY.isoformat()}T10:00:00"
STATUS_COL = 6   # MACHINES: SR NO, CUSTOMER NAME, MACHINE NAME, QTY, DISPATCH DATE, STATUS, ...
QTY_COL = 4
FIRST_DATA_ROW = 3


def script_for(config: ConfigSpec, extra: str = "") -> str:
    result = emit(config, workbook=reference_workbook.build())
    assert result.ok and result.script is not None, [r.message for r in result.refusals]
    return result.script + extra


def drive(script: str, workbook: Workbook, steps: list[dict[str, Any]], now: str = NOW) -> dict[str, Any]:
    request = {"script": "generated", "scriptSource": script, "now": now,
               "workbook": _encode_workbook(workbook), "steps": steps,
               "dump_min_rows": 40, "dump_min_cols": 12}
    proc = subprocess.run([str(NODE), str(HARNESS)], input=json.dumps(request), capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stderr
    return dict(json.loads(proc.stdout))


def evaluate(config: ConfigSpec, workbook: Workbook, event: RunEvent) -> Workbook:
    result = execute_run(config, workbook, event, EvalContext(run_id="triggers", today=TODAY))
    return result.workbook


def edited(workbook: Workbook, tab: str, row: int, col: int, value: Any) -> Workbook:
    out = copy.deepcopy(workbook)
    target = out.tab(tab)
    assert target is not None
    target.ensure_size(max(target.height, row), max(target.width, col))
    target.values[row - 1][col - 1] = value
    return out


def edit_event(tab: str, col: int) -> RunEvent:
    return RunEvent("edit", tab=tab, columns=(col,))


@pytest.fixture(scope="module")
def reference() -> ConfigSpec:
    return ConfigSpec.model_validate(load_reference_raw())


def test_run_now_on_the_unmodified_reference_config(reference: ConfigSpec) -> None:
    """PATCH-004 D.4, offline: the reference config as the owner wrote it - sort on edit, format
    after it, the summary debounced - emits whole, and Run now matches the evaluator."""
    workbook = reference_workbook.build()
    response = drive(script_for(reference), workbook, [{"call": ENTRY_POINT}])
    compare_grids(evaluate(reference, workbook, RunEvent.manual()), response, reference.header_row,
                  "run now")
    summary = next(t for t in response["tabs"] if t["name"] == "SUMMARY")
    assert summary["protected"] is True


def test_installing_sets_up_exactly_the_triggers_the_rules_need(reference: ConfigSpec) -> None:
    orphan = ("\nfunction makeOrphan_() {\n  ScriptApp.newTrigger('onEditAutoSort')"
              ".forSpreadsheet(SpreadsheetApp.getActive()).onEdit().create();\n}\n")
    response = drive(script_for(reference, orphan), reference_workbook.build(),
                     [{"call": "makeOrphan_"}, {"call": "installTrigger"}, {"call": "installTrigger"}])
    got = sorted((t["fn"], t["kind"], json.dumps(t["extra"], sort_keys=True)) for t in response["triggers"])
    assert got == [("handleEdit", "ON_EDIT", "{}"), ("handleOpen", "ON_OPEN", "{}"),
                   ("handleTick", "CLOCK", '{"minutes": 1}')], \
        "installing twice replaces its own triggers, and the legacy script's orphan is removed"
    assert response["toasts"][-1]["msg"] == ("Installed. Rules now run straight after edits and a "
                                             "short while after editing stops.")


def test_the_menu_offers_run_now_and_pause(reference: ConfigSpec) -> None:
    script = script_for(reference)
    response = drive(script, reference_workbook.build(), [{"call": "installTrigger"}, {"open": True}])
    menu = response["menus"][-1]
    assert [label for label, _ in menu["items"]] == ["Run now", "Pause automatic runs", "Resume automatic runs"]
    for _, fn in menu["items"]:
        assert f"function {fn}(" in script, fn


def test_an_edit_in_a_watched_column_organises_that_tab_only(reference: ConfigSpec) -> None:
    """Sort (on_edit STATUS) and the format chained after it run on MACHINES; SPARES is not
    touched, and the debounced summary is only marked, not built."""
    workbook = reference_workbook.build()
    response = drive(script_for(reference), workbook,
                     [{"call": "installTrigger"}, {"edit": ["MACHINES", FIRST_DATA_ROW, STATUS_COL, "Cancelled"]}])
    after_edit = edited(workbook, "MACHINES", FIRST_DATA_ROW, STATUS_COL, "Cancelled")
    expected = evaluate(reference, after_edit, edit_event("MACHINES", STATUS_COL))
    compare_grids(expected, response, reference.header_row, "edit STATUS")
    assert "SUMMARY" not in [t["name"] for t in response["tabs"]]
    assert "dirty.summary" in response["props"]


def test_an_edit_outside_the_watched_columns_sorts_nothing(reference: ConfigSpec) -> None:
    workbook = reference_workbook.build()
    response = drive(script_for(reference), workbook,
                     [{"call": "installTrigger"}, {"edit": ["MACHINES", FIRST_DATA_ROW, QTY_COL, 99]}])
    after_edit = edited(workbook, "MACHINES", FIRST_DATA_ROW, QTY_COL, 99)
    expected = evaluate(reference, after_edit, edit_event("MACHINES", QTY_COL))
    compare_grids(expected, response, reference.header_row, "edit QTY")
    compare_grids(after_edit, response, reference.header_row, "edit QTY changes nothing else")
    assert "dirty.summary" in response["props"], "any edit to a source still dirties the summary"


def test_the_summary_waits_for_the_quiet_period_then_rebuilds_once(reference: ConfigSpec) -> None:
    workbook = reference_workbook.build()
    script = script_for(reference)
    edit = {"edit": ["MACHINES", FIRST_DATA_ROW, STATUS_COL, "Cancelled"]}
    after_edit = edited(workbook, "MACHINES", FIRST_DATA_ROW, STATUS_COL, "Cancelled")
    sorted_only = evaluate(reference, after_edit, edit_event("MACHINES", STATUS_COL))

    early = drive(script, workbook, [{"call": "installTrigger"}, edit, {"advance": 60}, {"tick": True}])
    compare_grids(sorted_only, early, reference.header_row, "60 s after the edit: still quiet-waiting")

    due = drive(script, workbook, [{"call": "installTrigger"}, edit, {"advance": 121}, {"tick": True},
                                   {"advance": 120}, {"tick": True}])
    rebuilt = evaluate(reference, sorted_only, RunEvent("debounced", rule_ids=("summary",)))
    compare_grids(rebuilt, due, reference.header_row, "after the quiet period")
    assert "dirty.summary" not in due["props"], "a rebuilt summary is no longer dirty"
    assert due["results"][-1] == ["handleTick"]


def test_edits_to_the_built_tab_start_nothing(reference: ConfigSpec) -> None:
    workbook = reference_workbook.build()
    organised = evaluate(reference, workbook, RunEvent.manual())
    target_row = reference.data_start_row
    response = drive(script_for(reference), workbook,
                     [{"call": "installTrigger"}, {"call": ENTRY_POINT},
                      {"edit": ["SUMMARY", target_row, 2, "typed by hand"]}])
    after_edit = edited(organised, "SUMMARY", target_row, 2, "typed by hand")
    expected = evaluate(reference, after_edit, edit_event("SUMMARY", 2))
    compare_grids(expected, response, reference.header_row, "edit on the target")
    assert "dirty.summary" not in response["props"]


def test_paused_automation_ignores_edits_but_run_now_still_works(reference: ConfigSpec) -> None:
    workbook = reference_workbook.build()
    edit = {"edit": ["MACHINES", FIRST_DATA_ROW, STATUS_COL, "Cancelled"]}
    paused = drive(script_for(reference), workbook,
                   [{"call": "installTrigger"}, {"call": "pauseAutomation"}, edit])
    after_edit = edited(workbook, "MACHINES", FIRST_DATA_ROW, STATUS_COL, "Cancelled")
    compare_grids(after_edit, paused, reference.header_row, "paused: the edit stands, nothing runs")
    assert "dirty.summary" not in paused["props"]

    manual = drive(script_for(reference), workbook,
                   [{"call": "installTrigger"}, {"call": "pauseAutomation"}, edit, {"call": ENTRY_POINT}])
    compare_grids(evaluate(reference, after_edit, RunEvent.manual()), manual, reference.header_row,
                  "run now while paused")


def scheduled_config() -> ConfigSpec:
    raw = load_reference_raw()
    raw["rules"][0]["trigger"] = {"schedule": {"cron": "0 9 * * *"}}
    return ConfigSpec.model_validate(raw)


def test_a_schedule_runs_in_its_hour_once_and_its_chain_follows() -> None:
    config = scheduled_config()
    workbook = reference_workbook.build()
    script = script_for(config)
    response = drive(script, workbook, [{"call": "installTrigger"}, {"tick": True}, {"tick": True}],
                     now=f"{TODAY.isoformat()}T09:10:00")
    expected = evaluate(config, workbook, RunEvent("schedule", rule_ids=("sort_by_stage",)))
    compare_grids(expected, response, config.header_row, "09:10, scheduled for 09:00")
    assert response["props"]["schedule.sort_by_stage"] == f"{TODAY.isoformat()} 09"
    assert ("handleSchedule", "CLOCK", {"hours": 1}) in [(t["fn"], t["kind"], t["extra"])
                                                          for t in response["triggers"]]

    # Disorder the sorted tab, then let the hourly trigger fire again within the same hour: the
    # rule already ran at 09, so the edit stays exactly where it was typed. (The same tick also
    # rebuilds the summary the edit dirtied - that rule is still debounced in this config.)
    unsorting = {"edit": ["MACHINES", FIRST_DATA_ROW, STATUS_COL, "Cancelled"]}
    again = drive(script, workbook, [{"call": "installTrigger"}, {"tick": True}, unsorting,
                                     {"advance": 30 * 60}, {"tick": True}],
                  now=f"{TODAY.isoformat()}T09:10:00")
    still_unsorted = edited(expected, "MACHINES", FIRST_DATA_ROW, STATUS_COL, "Cancelled")
    summary_only = evaluate(config, still_unsorted, RunEvent("debounced", rule_ids=("summary",)))
    compare_grids(summary_only, again, config.header_row, "second tick in the same hour")


def test_a_schedule_does_not_run_outside_its_hour() -> None:
    config = scheduled_config()
    workbook = reference_workbook.build()
    response = drive(script_for(config), workbook, [{"call": "installTrigger"}, {"tick": True}],
                     now=f"{TODAY.isoformat()}T10:10:00")
    compare_grids(workbook, response, config.header_row, "10:10: not due")
    assert "schedule.sort_by_stage" not in response["props"]


def test_a_blocked_rule_leaves_the_whole_sheet_untouched() -> None:
    """Never half-apply (invariant 5): the evaluator aborts the run when the row guard trips, so
    the script must write nothing either - not even the tabs planned before the guard tripped."""
    raw = load_reference_raw()
    raw["guards"]["max_rows_per_run"] = 3
    config = ConfigSpec.model_validate(raw)
    workbook = reference_workbook.build()
    result = execute_run(config, workbook, RunEvent.manual(), EvalContext(run_id="guard", today=TODAY))
    assert result.plan.status == "BLOCKED"
    response = drive(script_for(config), workbook, [{"call": ENTRY_POINT}])
    compare_grids(result.workbook, response, config.header_row, "blocked run")
    assert "nothing was changed" in response["toasts"][-1]["msg"]

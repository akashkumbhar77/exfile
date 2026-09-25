"""Offline parity for the remaining actions: validate, copy (and, as they land, move/dedupe/clear).

Each case runs the reference rules plus the action under test, so the new action is exercised
together with the sort, format and consolidate it will live alongside, then compares the
generated script with the evaluator cell by cell: values, formats, dropdowns, number formats,
widths and banding (PATCH-004 D.6: the evaluator is the oracle).
"""

from __future__ import annotations

from datetime import date, datetime
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
from tests.fixtures.reference_workbook import MACHINES_HEADERS
from tests.test_apps_script_parity import _decode_workbook, compare_grids, run_generated
from tests.test_apps_script_triggers import drive, edit_event, edited
from tests.test_engine_python_parity import NODE

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js not installed")

TODAY = date(2026, 9, 16)
STARTED = datetime(2026, 9, 16, 10, 0, 0)   # the harness clock: backup stamps
HEADER_ROW = 2
DISPATCHED = {"enum": "STATUS", "is": "DISPATCHED"}


def with_archive(rows: list[list[Any]] | None = None) -> Workbook:
    """The reference workbook plus a governed ARCHIVE tab laid out like MACHINES."""
    workbook = reference_workbook.build()
    workbook.tabs.append(Tab("ARCHIVE", [["ARCHIVE"], list(MACHINES_HEADERS), *(rows or [])]))
    return workbook


def config_for(workbook: Workbook, *extra: dict[str, Any], before: bool = False) -> dict[str, Any]:
    raw = load_reference_raw()
    raw["rules"] = [*extra, *raw["rules"]] if before else [*raw["rules"], *extra]
    governed = [t.name for t in workbook.tabs if t.name in ("MACHINES", "SPARES", "ARCHIVE")]
    raw["schema_hashes"] = schema_hashes_for(workbook, governed, raw["header_row"])
    return raw


def both(raw: dict[str, Any], workbook: Workbook, label: str) -> tuple[Any, dict[str, Any]]:
    """Run now in both targets; returns (the evaluator's result, the script's response)."""
    config = ConfigSpec.model_validate(raw)
    result = emit(config, workbook=workbook)
    assert result.ok and result.script is not None, [r.message for r in result.refusals]
    py = execute_run(config, workbook, RunEvent.manual(),
                     EvalContext(run_id="actions", today=TODAY, now=STARTED))
    response = run_generated(result.script, workbook)
    compare_grids(py.workbook, response, config.header_row, label)
    return py, response


def rows_of(workbook: Workbook, tab: str) -> list[list[Any]]:
    t = workbook.tab(tab)
    assert t is not None
    return [r for r in t.values[HEADER_ROW:] if any(v not in (None, "") for v in r)]


# ---------------------------------------------------------------- validate

def test_a_status_dropdown_from_the_enum_skips_held_rows() -> None:
    workbook = reference_workbook.build()
    machines = workbook.tab("MACHINES")
    assert machines is not None
    hold = machines.width
    machines.ensure_size(machines.height, hold + 1)
    machines.values[HEADER_ROW - 1][hold] = "!hold"
    machines.values[HEADER_ROW + 1][hold] = "YES"
    rule = {"id": "status_dropdown", "action": "validate", "tabs": "all_with:STATUS",
            "trigger": {"after": "sort_by_stage"}, "column": "STATUS", "from_enum": "STATUS"}
    py, _ = both(config_for(workbook, rule), workbook, "validate from enum")
    validated = py.workbook.tab("MACHINES")
    assert validated is not None
    rows = {r for (r, _) in validated.validations}
    assert HEADER_ROW + 2 not in rows, "the held row keeps no dropdown"
    assert len(rows) == len(rows_of(workbook, "MACHINES")) - 1


def test_a_fixed_list_that_allows_other_values() -> None:
    workbook = reference_workbook.build()
    rule = {"id": "spares_dropdown", "action": "validate", "tabs": ["SPARES"], "trigger": {"on_edit": {}},
            "column": "STATUS", "values": ["Completed", "In Process", "Disputed"], "allow_invalid": True}
    py, _ = both(config_for(workbook, rule), workbook, "validate list")
    spares = py.workbook.tab("SPARES")
    assert spares is not None and spares.validations
    assert all(v.allow_invalid for v in spares.validations.values())


# ---------------------------------------------------------------- copy

COPY = {"id": "archive_dispatched", "action": "copy", "tabs": "all_with:STATUS",
        "trigger": {"on_edit": {"columns": ["STATUS"]}}, "when": DISPATCHED,
        "to_tab": "ARCHIVE", "key_columns": ["SR NO"]}


@pytest.mark.parametrize("position", ["bottom", "top"])
def test_copy_adds_matching_rows_once(position: str) -> None:
    """ARCHIVE already holds row 2; only the dispatched row 7 is new. Sort and format then run
    over ARCHIVE as well, since it has a STATUS column."""
    workbook = with_archive([[2, "Beta", "Mill", 1, date(2026, 9, 1), "Dispatched", "NO", "Outsource"]])
    raw = config_for(workbook, {**COPY, "position": position}, before=True)
    py, first = both(raw, workbook, f"copy {position}")
    assert len(rows_of(py.workbook, "ARCHIVE")) == 2

    config = ConfigSpec.model_validate(raw)
    script = emit(config, workbook=workbook).script
    assert script is not None
    again = run_generated(script, _decode_workbook(first))

    def archive(response: dict[str, Any]) -> dict[str, Any]:  # sheet size is reset by reloading
        return {k: v for k, v in next(t for t in response["tabs"] if t["name"] == "ARCHIVE").items()
                if k != "maxRows"}

    assert archive(again) == archive(first), "a second run copies nothing new"


def test_copy_on_an_edit_looks_only_at_the_edited_tab() -> None:
    workbook = with_archive()
    raw = config_for(workbook, COPY, before=True)
    config = ConfigSpec.model_validate(raw)
    script = emit(config, workbook=workbook).script
    assert script is not None
    response = drive(script, workbook, [{"call": "installTrigger"},
                                        {"edit": ["MACHINES", HEADER_ROW + 1, 6, "Dispatched"]}])
    after_edit = edited(workbook, "MACHINES", HEADER_ROW + 1, 6, "Dispatched")
    expected = execute_run(config, after_edit, edit_event("MACHINES", 6),
                           EvalContext(run_id="copy-edit", today=TODAY)).workbook
    compare_grids(expected, response, config.header_row, "copy on edit")
    assert len(rows_of(expected, "ARCHIVE")) == 2, "rows 1 (just edited) and 7"


def test_a_value_with_nowhere_to_go_stops_the_whole_run() -> None:
    """SPARES has TYPE OF WORK and INVOICE NO, ARCHIVE does not: copying would drop them, so the
    evaluator errors and nothing at all is written - not even the sort that ran first."""
    workbook = with_archive()
    rule = {**COPY, "id": "archive_spares", "tabs": ["SPARES"],
            "when": {"column": "STATUS", "equals": "Completed"}}
    raw = config_for(workbook, rule)
    py, response = both(raw, workbook, "copy refuses to drop data")
    assert py.plan.status == "ERROR"
    assert "nothing was changed" in response["toasts"][-1]["msg"]


# ---------------------------------------------------------------- destructive: dedupe, clear, move

def backup_rows(workbook: Workbook) -> list[list[Any]]:
    tab = workbook.tab("_backup")
    return [] if tab is None else [r for r in tab.values[1:] if any(v not in (None, "") for v in r)]


def destructive(workbook: Workbook, *rules: dict[str, Any], before: bool = True) -> dict[str, Any]:
    raw = config_for(workbook, *rules, before=before)
    raw["guards"]["backup_tab"] = True
    return raw


@pytest.mark.parametrize("keep", ["first", "last"])
def test_dedupe_removes_repeats_and_backs_them_up(keep: str) -> None:
    """Rows 3 and 7 appear twice. The repeats go, into the hidden backup tab first; then the
    reference sort and format run over what is left."""
    workbook = reference_workbook.build()
    machines = workbook.tab("MACHINES")
    assert machines is not None
    repeats = [list(machines.values[HEADER_ROW + 2]), list(machines.values[HEADER_ROW + 6])]
    repeats[-1][2] = "Router (again)"
    machines.insert_rows(machines.height + 1, repeats)
    rule = {"id": "one_per_sr", "action": "dedupe", "tabs": ["MACHINES"], "trigger": {"on_edit": {}},
            "key_columns": ["SR NO"], "keep": keep}
    py, response = both(destructive(workbook, rule), workbook, f"dedupe keep {keep}")
    assert len(rows_of(py.workbook, "MACHINES")) == 10
    backed = backup_rows(py.workbook)
    assert [(r[0], r[1], r[2]) for r in backed] == [(STARTED, "one_per_sr", "MACHINES")] * 2
    assert next(t for t in response["tabs"] if t["name"] == "_backup")["hidden"] is True


def test_clear_blanks_the_named_columns_and_backs_the_row_up() -> None:
    """Dispatched and cancelled rows lose their FREEZE? flag; row 7's 'yes' is backed up. Row 5
    (cancelled) has no flag, so there is nothing to clear or back up."""
    workbook = reference_workbook.build()
    rule = {"id": "unfreeze_done", "action": "clear", "tabs": "all_with:STATUS", "trigger": {"on_edit": {}},
            "when": {"any": [DISPATCHED, {"enum": "STATUS", "is": "CANCELLED"}]}, "columns": ["FREEZE?"]}
    py, _ = both(destructive(workbook, rule), workbook, "clear")
    backed = backup_rows(py.workbook)
    assert [(r[1], r[2], r[3]) for r in backed] == [("unfreeze_done", "MACHINES", HEADER_ROW + 7)]


@pytest.mark.parametrize("position", ["bottom", "top"])
def test_move_takes_rows_out_backs_them_up_and_puts_them_in_the_target(position: str) -> None:
    """Dispatched rows leave MACHINES for ARCHIVE. The format after the sort still covers the rows
    the move vacated, as the evaluator does."""
    workbook = with_archive([[99, "Old", "Kiln", 1, date(2026, 1, 1), "Dispatched", "", ""]])
    rule = {"id": "archive_dispatched", "action": "move", "tabs": ["MACHINES", "SPARES"],
            "trigger": {"on_edit": {"columns": ["STATUS"]}}, "when": DISPATCHED,
            "to_tab": "ARCHIVE", "position": position}
    py, _ = both(destructive(workbook, rule), workbook, f"move {position}")
    assert len(rows_of(py.workbook, "MACHINES")) == 9
    assert len(rows_of(py.workbook, "ARCHIVE")) == 2
    assert [(r[1], r[2]) for r in backup_rows(py.workbook)] == [("archive_dispatched", "MACHINES")]


def test_move_on_an_edit_moves_only_from_the_edited_tab() -> None:
    workbook = with_archive()
    rule = {"id": "archive_dispatched", "action": "move", "tabs": ["MACHINES"],
            "trigger": {"on_edit": {"columns": ["STATUS"]}}, "when": DISPATCHED, "to_tab": "ARCHIVE"}
    raw = destructive(workbook, rule)
    config = ConfigSpec.model_validate(raw)
    script = emit(config, workbook=workbook).script
    assert script is not None
    response = drive(script, workbook, [{"call": "installTrigger"},
                                        {"edit": ["MACHINES", HEADER_ROW + 1, 6, "Dispatched"]}])
    after_edit = edited(workbook, "MACHINES", HEADER_ROW + 1, 6, "Dispatched")
    expected = execute_run(config, after_edit, edit_event("MACHINES", 6),
                           EvalContext(run_id="move-edit", today=TODAY, now=STARTED)).workbook
    compare_grids(expected, response, config.header_row, "move on edit")
    assert len(rows_of(expected, "ARCHIVE")) == 2


def test_the_backup_keeps_only_the_last_ten_runs() -> None:
    """Ten earlier runs are already in the backup tab; this run makes eleven, so the oldest goes."""
    workbook = with_archive()
    earlier = [[datetime(2026, 9, d, 9, 0, 0), "archive_dispatched", "MACHINES", 3, d] for d in range(1, 11)]
    backup = Tab("_backup", [["BACKED UP", "RULE", "TAB", "ROW", "ROW AS IT WAS"], *earlier])
    backup.hidden = True
    workbook.tabs.append(backup)
    rule = {"id": "archive_dispatched", "action": "move", "tabs": ["MACHINES"],
            "trigger": {"on_edit": {}}, "when": DISPATCHED, "to_tab": "ARCHIVE"}
    py, _ = both(destructive(workbook, rule), workbook, "backup retention")
    stamps = [r[0] for r in backup_rows(py.workbook)]
    assert stamps[0] == datetime(2026, 9, 2, 9, 0, 0) and stamps[-1] == STARTED
    assert len(set(stamps)) == 10


def test_a_move_over_the_row_guard_writes_nothing_not_even_the_backup() -> None:
    workbook = with_archive()
    rule = {"id": "archive_all", "action": "move", "tabs": ["MACHINES"], "trigger": {"on_edit": {}},
            "when": {"column": "STATUS", "is_blank": False}, "to_tab": "ARCHIVE"}
    raw = destructive(workbook, rule)
    raw["guards"]["max_rows_per_run"] = 10
    py, response = both(raw, workbook, "move blocked")
    assert py.plan.status == "BLOCKED"
    assert "_backup" not in [t["name"] for t in response["tabs"]]


@pytest.mark.parametrize("action", ["copy", "move"])
def test_a_second_run_after_moving_or_copying_changes_nothing(action: str) -> None:
    workbook = with_archive()
    rule: dict[str, Any] = {"id": "archive_dispatched", "action": action, "tabs": ["MACHINES"],
                            "trigger": {"on_edit": {}}, "when": DISPATCHED, "to_tab": "ARCHIVE"}
    if action == "copy":
        rule["key_columns"] = ["SR NO"]
    raw = destructive(workbook, rule)
    config = ConfigSpec.model_validate(raw)
    script = emit(config, workbook=workbook).script
    assert script is not None
    first = run_generated(script, workbook)
    second = run_generated(script, _decode_workbook(first))

    def content(response: dict[str, Any]) -> list[dict[str, Any]]:  # reloading resets sheet size
        return [{k: v for k, v in t.items() if k != "maxRows"} for t in response["tabs"]]

    assert content(second) == content(first)


# ---------------------------------------------------------------- rows that move keep their own look

def only(workbook: Workbook, *rules: dict[str, Any], max_rows: int = 500) -> dict[str, Any]:
    """Just these rules (no reference rules), backup on, guard as given."""
    raw = config_for(workbook)
    raw["rules"] = list(rules)
    raw["guards"] |= {"backup_tab": True, "max_rows_per_run": max_rows}
    return raw


MOVE_DONE = {"id": "archive_done", "action": "move", "tabs": ["MACHINES"], "trigger": {"on_edit": {}},
             "when": {"enum": "STATUS", "is": "COMPLETED"}, "to_tab": "ARCHIVE"}
# Paints only a fill, so each row's own font colour must survive - and must be the right row's.
TINT = {"id": "tint", "action": "format", "tabs": "all_with:STATUS", "trigger": {"after": "archive_done"},
        "default": {"background": "#FAFAFA"},
        "row_rules": [{"when": {"enum": "STATUS", "is": "IN-PROCESS"}, "background": "#FFF2CC"}]}


def coloured(workbook: Workbook) -> Workbook:
    """Every data row of MACHINES and ARCHIVE gets its own font colour."""
    from app.services.grid import CellFormat
    for name in ("MACHINES", "ARCHIVE"):
        tab = workbook.tab(name)
        assert tab is not None
        for r in range(HEADER_ROW, tab.height):
            colour = f"#{(r * 37) % 256:02X}00{(r * 91) % 256:02X}"
            tab.formats[r] = [CellFormat(font=colour) for _ in tab.formats[r]]
    return workbook


@pytest.mark.parametrize("position", ["top", "bottom"])
def test_formatting_after_a_move_follows_each_row(position: str) -> None:
    """The format reads each row's colours as the move left them: rows below the removed one
    moved up, the inserted row has none, and the vacated bottom row is still painted."""
    workbook = coloured(with_archive([[99, "Old", "Kiln", 1, date(2026, 1, 1), "Dispatched", "", ""]]))
    both(only(workbook, {**MOVE_DONE, "position": position}, TINT), workbook, f"tint after move {position}")


def test_match_source_reads_the_row_that_is_first_after_a_move() -> None:
    """MACHINES' first data row (completed) moves out; the summary's dates then take the format of
    the row that is first now, not the one that was."""
    workbook = with_archive()
    machines = workbook.tab("MACHINES")
    assert machines is not None
    for r in range(HEADER_ROW + 1, machines.height + 1):
        machines.number_formats[(r, 5)] = "d/m/yyyy" if r == HEADER_ROW + 1 else "yyyy-mm-dd"
    raw = config_for(workbook, MOVE_DONE, before=True)
    raw["guards"]["backup_tab"] = True
    py, _ = both(raw, workbook, "match_source after a move")
    summary = py.workbook.tab("SUMMARY")
    assert summary is not None
    assert "yyyy-mm-dd" in set(summary.number_formats.values())


@pytest.mark.parametrize(("action", "limit", "blocked"), [
    ("move", 7, True),     # 4 rows out + 4 rows in = 8 (rows 2, 3, 4 and 10 are in process)
    ("move", 8, False),
    ("copy", 3, True),     # 4 rows in
    ("copy", 4, False),
])
def test_the_row_guard_counts_moves_twice_and_copies_once(action: str, limit: int, blocked: bool) -> None:
    workbook = with_archive()
    rule: dict[str, Any] = {"id": "archive_open", "action": action, "tabs": ["MACHINES"],
                            "trigger": {"on_edit": {}}, "when": {"enum": "STATUS", "is": "IN-PROCESS"},
                            "to_tab": "ARCHIVE"}
    if action == "copy":
        rule["key_columns"] = ["SR NO"]
    py, _ = both(only(workbook, rule, max_rows=limit), workbook, f"{action} guard {limit}")
    assert (py.plan.status == "BLOCKED") is blocked


def test_a_row_emptied_by_clear_is_not_sorted_but_is_still_formatted() -> None:
    """Clearing every column of the last row leaves an empty row. The sort stops above it (blanks
    first would otherwise pull it to the top); the format still paints it, as the evaluator's
    run-start extent says."""
    workbook = reference_workbook.build()
    every = ["SR NO", "CUSTOMER NAME", "MACHINE NAME", "QTY", "DISPATCH DATE", "STATUS", "FREEZE?",
             "PANEL IN-HOUSE OR OUTSOURCE?"]
    wipe = {"id": "wipe_ten", "action": "clear", "tabs": ["MACHINES"], "trigger": {"on_edit": {}},
            "when": {"column": "SR NO", "equals": 10}, "columns": every}
    by_customer = {"id": "by_customer", "action": "sort", "tabs": ["MACHINES"], "trigger": {"after": "wipe_ten"},
                   "keys": [{"column": "CUSTOMER NAME", "blanks": "first", "order": "desc"}]}
    tint = {**TINT, "tabs": ["MACHINES"], "trigger": {"after": "by_customer"}}
    py, _ = both(only(workbook, wipe, by_customer, tint), workbook, "emptied row")
    machines = py.workbook.tab("MACHINES")
    assert machines is not None
    assert all(v in (None, "") for v in machines.values[-1]), "the emptied row stays at the bottom"
    assert machines.formats[-1][0].background == "#FAFAFA", "and is painted"


# ---------------------------------------------------------------- formulas (owner ruling 2026-09-25)

def run_with_formulas(script: str, workbook: Workbook, formulas: list[tuple[str, int, int]]) -> dict[str, Any]:
    """Like run_generated, with formula cells (tab, row, col); their values are the workbook's."""
    import json
    import subprocess

    from tests.test_engine_python_parity import HARNESS, _encode_workbook
    request = {"script": "generated", "scriptSource": script, "now": "2026-09-16T10:00:00",
               "workbook": _encode_workbook(workbook), "steps": [{"call": ENTRY_POINT}],
               "dump_min_rows": 40, "dump_min_cols": 12}
    for tab in request["workbook"]["tabs"]:
        tab["formulas"] = [{"row": r, "col": c, "formula": f"=R{r}C{c}"} for t, r, c in formulas if t == tab["name"]]
    proc = subprocess.run([str(NODE), str(HARNESS)], input=json.dumps(request), capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stderr
    return dict(json.loads(proc.stdout))


def formula_rows(response: dict[str, Any], tab: str, col: int) -> set[int]:
    cells = next(t for t in response["tabs"] if t["name"] == tab)["cells"]
    return {r + 1 for r, row in enumerate(cells) if col <= len(row) and row[col - 1].get("f")}


QTY = 4
DATA_ROWS = range(HEADER_ROW + 1, HEADER_ROW + 11)


def test_clear_empties_only_its_own_cells_and_keeps_formulas() -> None:
    """Row 7's FREEZE? is cleared; its QTY formula, and every other row's, stays a formula."""
    workbook = reference_workbook.build()
    rule = {"id": "unfreeze_dispatched", "action": "clear", "tabs": ["MACHINES"], "trigger": {"on_edit": {}},
            "when": DISPATCHED, "columns": ["FREEZE?"]}
    raw = only(workbook, rule)
    config = ConfigSpec.model_validate(raw)
    script = emit(config, workbook=workbook).script
    assert script is not None
    response = run_with_formulas(script, workbook, [("MACHINES", r, QTY) for r in DATA_ROWS])
    py = execute_run(config, workbook, RunEvent.manual(), EvalContext(run_id="f", today=TODAY, now=STARTED))
    compare_grids(py.workbook, response, config.header_row, "clear with formulas")
    assert formula_rows(response, "MACHINES", QTY) == set(DATA_ROWS)


def test_sort_keeps_the_formulas_of_rows_that_stay_put() -> None:
    """A row the sort leaves in place keeps its formula; a row that moves arrives as its value.
    That second half is the stated limit (DECISIONS, formulas)."""
    workbook = reference_workbook.build()
    raw = load_reference_raw()
    raw["rules"] = [raw["rules"][0]]           # just the stage sort
    config = ConfigSpec.model_validate(raw)
    script = emit(config, workbook=workbook).script
    assert script is not None
    response = run_with_formulas(script, workbook, [("MACHINES", r, QTY) for r in DATA_ROWS])
    py = execute_run(config, workbook, RunEvent.manual(), EvalContext(run_id="f", today=TODAY))
    compare_grids(py.workbook, response, config.header_row, "sort with formulas")
    before, after = workbook.tab("MACHINES"), py.workbook.tab("MACHINES")
    assert before is not None and after is not None
    stayed = {r for r in DATA_ROWS if before.values[r - 1][0] == after.values[r - 1][0]}
    assert stayed and stayed != set(DATA_ROWS), "the fixture must have both kinds of row"
    assert formula_rows(response, "MACHINES", QTY) == stayed


def test_a_sort_then_a_move_in_the_same_run() -> None:
    """The sort's planned write must not change when the move later removes rows from the same
    tab in the run's picture: each write is taken as it was planned."""
    workbook = with_archive()
    raw = load_reference_raw()
    move = {"id": "archive_done", "action": "move", "tabs": ["MACHINES"], "trigger": {"after": "sort_by_stage"},
            "when": {"enum": "STATUS", "is": "COMPLETED"}, "to_tab": "ARCHIVE"}
    raw = config_for(workbook, move)
    raw["guards"]["backup_tab"] = True
    both(raw, workbook, "sort then move")

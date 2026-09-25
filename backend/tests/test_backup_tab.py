"""The backup tab in the evaluator: what goes in, and how much is kept (services/backup.py)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.schemas.config import ConfigSpec
from app.services.backup import BACKUP_TAB, KEEP_RUNS, with_backup
from app.services.conditions import EvalContext
from app.services.executor import execute_run
from app.services.grid import Tab, Workbook
from app.services.rules.base import BackupRow, RulePlan
from app.services.runner import RunEvent
from tests.conftest import load_reference_raw
from tests.fixtures import reference_workbook

STARTED = datetime(2026, 9, 16, 10, 0, 0)


def plan_with(*rows: tuple[str, int, list[Any]]) -> RulePlan:
    plan = RulePlan("purge", "clear")
    plan.backup = [BackupRow(tab, row, values) for tab, row, values in rows]
    return plan


def test_nothing_backed_up_means_no_tab() -> None:
    workbook = reference_workbook.build()
    assert with_backup(workbook, [RulePlan("s", "sort")], STARTED) is workbook


def test_rows_are_stamped_and_the_tab_is_hidden_and_last() -> None:
    out = with_backup(reference_workbook.build(), [plan_with(("MACHINES", 7, [7, "Eta", "Router"]))], STARTED)
    tab = out.tabs[-1]
    assert tab.name == BACKUP_TAB and tab.hidden
    assert tab.values[1][:6] == [STARTED, "purge", "MACHINES", 7, 7, "Eta"]


def test_only_the_last_runs_are_kept() -> None:
    workbook = reference_workbook.build()
    for d in range(1, KEEP_RUNS + 3):
        workbook = with_backup(workbook, [plan_with(("MACHINES", 3, [d]))], datetime(2026, 9, d, 9, 0))
    tab = workbook.tab(BACKUP_TAB)
    assert tab is not None
    stamps = [r[0] for r in tab.values[1:]]
    assert len(stamps) == KEEP_RUNS and stamps[0] == datetime(2026, 9, 3, 9, 0)


def test_the_guard_decides_whether_the_evaluator_writes_it() -> None:
    raw = load_reference_raw()
    raw["rules"] = [{"id": "purge", "action": "clear", "tabs": ["MACHINES"], "trigger": {"on_edit": {}},
                     "when": {"enum": "STATUS", "is": "DISPATCHED"}, "columns": ["FREEZE?"]}]
    for on in (False, True):
        raw["guards"]["backup_tab"] = on
        result = execute_run(ConfigSpec.model_validate(raw), reference_workbook.build(), RunEvent.manual(),
                             EvalContext(run_id="b", today=date(2026, 9, 16), now=STARTED))
        assert (result.workbook.tab(BACKUP_TAB) is not None) is on


def test_an_existing_tab_keeps_its_place() -> None:
    workbook = reference_workbook.build()
    backup = Tab(BACKUP_TAB, [["BACKED UP"], [datetime(2026, 9, 1), "x", "MACHINES", 3]])
    workbook.tabs.insert(0, backup)
    out = with_backup(Workbook(workbook.tabs), [plan_with(("MACHINES", 4, [4]))], STARTED)
    assert out.tabs[0].name == BACKUP_TAB and len(out.tabs[0].values) == 3

"""consolidate: union + header canonicalization + derived fill + sort/format + lock."""

from __future__ import annotations

from app.schemas.config import ConfigSpec
from app.services.conditions import EvalContext
from app.services.executor import execute_run
from app.services.grid import Tab, Workbook, is_empty
from app.services.runner import RunEvent
from tests.conftest import make_config


def _summary(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> Tab:
    result = execute_run(config, workbook, RunEvent.manual(), ctx)
    assert result.plan.status == "OK"
    t = result.workbook.tab("SUMMARY")
    assert t is not None
    return t


def test_header_union_canonicalized(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    s = _summary(config, workbook, ctx)
    assert s.values[0][0] == "CONSOLIDATED ORDER SUMMARY — ALL SHEETS"
    assert s.values[1] == [
        "SOURCE SHEET", "SR NO", "CUSTOMER NAME", "MACHINE NAME", "QTY", "DISPATCH DATE", "STATUS",
        "FREEZE?", "PANEL IN-HOUSE OR OUTSOURCE?", "Type of Work", "INVOICE NO",
    ]


def test_rows_aligned_filled_sorted(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    s = _summary(config, workbook, ctx)
    rows = s.values[2:]
    # 10 MACHINES + 3 non-blank SPARES; LEGENDS excluded; blank row skipped
    assert len(rows) == 13
    by_sr = {r[1]: r for r in rows}
    assert by_sr["S1"][9] == "SPARES"  # Type of Work auto-filled from tab name
    assert by_sr["S2"][9] == "Repair"  # existing value kept
    assert by_sr[1][9] == "MACHINES"
    assert by_sr["S2"][7] == "YES"  # TECHNICAL FREEZE? -> FREEZE?
    assert by_sr[1][7] == "YES"  # WORK ORDER FREEZE? -> FREEZE?
    assert by_sr["S1"][10] == "INV1"
    assert is_empty(by_sr[1][10])
    # stage order across tabs, then date
    assert [r[1] for r in rows] == [2, "S2", 10, 4, 3, "S1", 1, 8, "S3", 7, 5, 6, 9]


def test_summary_formatting_and_lock(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    s = _summary(config, workbook, ctx)
    assert s.protected is True
    first = s.formats[2]  # SR 2: overdue
    assert {f.background for f in first} == {"#FCE4EC"}
    s2 = s.formats[3]
    assert s2[7].font == "#008000"  # freeze cell green in summary too


def test_summary_is_regenerated_not_appended(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    first = execute_run(config, workbook, RunEvent.manual(), ctx).workbook
    second = execute_run(config, first, RunEvent.manual(), ctx).workbook
    assert second.tab("SUMMARY") == first.tab("SUMMARY")
    assert second.names()[0] == "SUMMARY"


def test_summary_edits_never_trigger_rules(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    built = execute_run(config, workbook, RunEvent.manual(), ctx).workbook
    r = execute_run(config, built, RunEvent("edit", tab="SUMMARY", columns=(7,)), ctx)
    assert r.plan.plans == [] and r.plan.dirty == []


def test_duplicate_canonical_columns_first_non_empty_wins(workbook: Workbook, ctx: EvalContext) -> None:
    tab = Tab("DUP", [["t"], ["STATUS", "DISPATCH DATE", "TENTATIVE DISPATCH DATE"],
                      ["Completed", "", "2026-01-01"], ["Completed", "x", "y"]])
    wb = Workbook([tab])
    cfg = make_config(wb, ["DUP"], [{
        "id": "summary", "trigger": {"debounced": {"quiet_seconds": 60}}, "action": "consolidate",
        "sources": "all_with:STATUS", "target_tab": "SUMMARY",
    }])
    s = execute_run(cfg, wb, RunEvent.manual(), ctx).workbook.tab("SUMMARY")
    assert s is not None
    assert s.values[1] == ["STATUS", "DISPATCH DATE"]
    assert [r[1] for r in s.values[2:]] == ["2026-01-01", "x"]


def test_ungoverned_tab_excluded_with_warning(workbook: Workbook, ctx: EvalContext) -> None:
    cfg = make_config(workbook, ["MACHINES"], [{
        "id": "summary", "trigger": {"debounced": {"quiet_seconds": 60}}, "action": "consolidate",
        "sources": "all_with:STATUS", "target_tab": "SUMMARY",
    }])
    r = execute_run(cfg, workbook, RunEvent.manual(), ctx)
    s = r.workbook.tab("SUMMARY")
    assert s is not None and len(s.values) == 2 + 10
    assert any("SPARES" in w and "not governed" in w for p in r.plan.plans for w in p.warnings)

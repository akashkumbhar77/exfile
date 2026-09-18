"""Pre-flight, trigger selection, guards, atomicity, snapshots/undo, dry-run sharing the executor path."""

from __future__ import annotations

import hashlib
from typing import Any

from app.schemas.config import ConfigSpec
from app.services.conditions import EvalContext
from app.services.dry_run import dry_run
from app.services.executor import execute_run
from app.services.grid import Workbook
from app.services.preflight import compute_schema_hash
from app.services.runner import RunEvent, plan_run
from app.services.snapshots import decode_tab, encode_tab, restore_snapshots
from tests.conftest import load_reference_raw, make_config

# ---------------------------------------------------------------- pre-flight


def test_schema_hash_algorithm_is_pinned() -> None:
    expected = hashlib.sha256("STATUS\x1fDISPATCH DATE".encode()).hexdigest()
    assert compute_schema_hash(["STATUS", "  DISPATCH DATE ", "", None]) == expected
    assert compute_schema_hash(["STATUS", "DISPATCH DATE"]) == expected
    assert compute_schema_hash(["status", "DISPATCH DATE"]) != expected  # case is significant
    assert compute_schema_hash(["", "STATUS"]) != compute_schema_hash(["STATUS"])  # leading blanks count


def test_renamed_header_pauses_before_any_rule(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    t = workbook.tab("MACHINES")
    assert t is not None
    t.values[1][5] = "STAGE"  # STATUS renamed
    before = workbook.clone()
    r = execute_run(config, workbook, RunEvent("edit", tab="SPARES", columns=(5,)), ctx)
    assert r.plan.status == "PAUSED_DRIFT"
    assert r.plan.plans == []  # nothing evaluated, not even for the undrifted tab
    assert [d.tab for d in r.plan.drift] == ["MACHINES"]
    assert r.records[0].status == "PAUSED_DRIFT"
    assert r.workbook == before and not r.snapshots


def test_missing_governed_tab_is_drift(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    workbook.tabs = [t for t in workbook.tabs if t.name != "SPARES"]
    r = dry_run(config, workbook, ctx)
    assert r.status == "PAUSED_DRIFT"
    assert r.drift[0].tab == "SPARES" and r.drift[0].actual is None


def test_data_edits_do_not_drift(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    t = workbook.tab("MACHINES")
    assert t is not None
    t.values[0][0] = "NEW TITLE"
    t.values.append(["11", "x"] + [""] * 6)
    t.__post_init__()
    assert dry_run(config, workbook, ctx).status == "OK"


# ---------------------------------------------------------------- trigger selection


def _rule_ids(config: ConfigSpec, wb: Workbook, event: RunEvent, ctx: EvalContext) -> list[str]:
    return [p.rule_id for p in plan_run(config, wb, event, ctx).plans]


def test_edit_on_trigger_column_runs_chain_on_that_tab_only(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    run = plan_run(config, workbook, RunEvent("edit", tab="MACHINES", columns=(6,)), ctx)
    assert [p.rule_id for p in run.plans] == ["sort_by_stage", "status_formatting"]
    assert {c.tab for p in run.plans for c in p.changes} == {"MACHINES"}
    assert run.dirty == ["summary"]  # consolidate is debounced, only marked dirty


def test_edit_on_other_column_runs_nothing(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    run = plan_run(config, workbook, RunEvent("edit", tab="MACHINES", columns=(2,)), ctx)
    assert run.plans == [] and run.dirty == ["summary"]


def test_edit_on_variant_header_matches_canonical(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    # MACHINES col 5 is "TENTATIVE DISPATCH DATE" -> DISPATCH DATE
    assert _rule_ids(config, workbook, RunEvent("edit", tab="MACHINES", columns=(5,)), ctx)[0] == "sort_by_stage"


def test_edit_on_ungoverned_tab_runs_nothing(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    run = plan_run(config, workbook, RunEvent("edit", tab="LEGENDS", columns=(1,)), ctx)
    assert run.plans == [] and run.dirty == []


def test_debounced_event_runs_only_consolidate(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    assert _rule_ids(config, workbook, RunEvent("debounced"), ctx) == ["summary"]


def test_manual_runs_everything_in_dependency_order(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    raw = load_reference_raw()
    raw["rules"] = [raw["rules"][1], raw["rules"][2], raw["rules"][0]]  # child listed before parent
    cfg = ConfigSpec.model_validate(raw)
    assert _rule_ids(cfg, workbook, RunEvent.manual(), ctx) == ["summary", "sort_by_stage", "status_formatting"]
    assert _rule_ids(cfg, workbook, RunEvent.manual("sort_by_stage"), ctx) == ["sort_by_stage", "status_formatting"]


def test_schedule_event(workbook: Workbook, ctx: EvalContext) -> None:
    rules: list[dict[str, Any]] = [
        {"id": "nightly", "tabs": "all_with:STATUS", "trigger": {"schedule": {"cron": "0 2 * * *"}},
         "action": "dedupe", "key_columns": ["STATUS"]},
        {"id": "other", "tabs": "all_with:STATUS", "trigger": {"on_edit": {}}, "action": "sort",
         "keys": [{"column": "STATUS"}]},
    ]
    cfg = make_config(workbook, ["MACHINES", "SPARES"], rules)
    assert _rule_ids(cfg, workbook, RunEvent("schedule", rule_ids=("nightly",)), ctx) == ["nightly"]


# ---------------------------------------------------------------- guards & atomicity


def test_guard_blocks_whole_run(workbook: Workbook, ctx: EvalContext) -> None:
    raw = load_reference_raw()
    cfg = make_config(workbook, ["MACHINES", "SPARES"], raw["rules"], guards={"max_rows_per_run": 3})
    r = execute_run(cfg, workbook, RunEvent.manual(), ctx)
    assert r.plan.status == "BLOCKED"
    assert r.workbook == workbook and not r.snapshots
    assert all(rec.status == "ERROR" for rec in r.records)
    assert "max_rows_per_run" in (r.records[0].error or "")
    report = dry_run(cfg, workbook, ctx)
    assert report.status == "BLOCKED" and report.rules[0].blocked


def test_error_in_later_rule_rolls_back_earlier_rules(workbook: Workbook, ctx: EvalContext) -> None:
    rules: list[dict[str, Any]] = [
        load_reference_raw()["rules"][0],
        {"id": "archive", "tabs": ["MACHINES"], "trigger": {"after": "sort_by_stage"}, "action": "move",
         "when": {"enum": "STATUS", "is": "CANCELLED"}, "to_tab": "SPARES"},  # SPARES lacks MACHINE NAME
    ]
    cfg = make_config(workbook, ["MACHINES", "SPARES"], rules)
    snapshot = workbook.clone()
    r = execute_run(cfg, workbook, RunEvent.manual(), ctx)
    assert r.plan.status == "ERROR"
    assert r.plan.plans[0].changes  # the sort was planned...
    assert r.workbook == snapshot  # ...but nothing was applied
    assert workbook == snapshot  # and the input was never mutated


# ---------------------------------------------------------------- snapshots & undo


def test_snapshot_roundtrip_is_exact(workbook: Workbook) -> None:
    for tab in workbook.tabs:
        assert decode_tab(encode_tab(tab)) == tab


def test_undo_restores_exact_prior_state(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    original = workbook.clone()
    r = execute_run(config, workbook, RunEvent.manual(), ctx)
    assert r.workbook != original
    assert {s.run_id for s in r.snapshots} == {"run_test"}
    assert any(not s.existed for s in r.snapshots)  # SUMMARY was created by this run
    restored = restore_snapshots(r.workbook, r.snapshots)
    assert restored == original


def test_every_destructive_record_points_at_snapshots(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    r = execute_run(config, workbook, RunEvent.manual(), ctx)
    by_rule = {rec.rule_id: rec for rec in r.records}
    assert by_rule["sort_by_stage"].snapshot_tabs == ("MACHINES", "SPARES")
    assert by_rule["summary"].snapshot_tabs == ("SUMMARY",)
    assert all(rec.run_id == "run_test" and rec.config_version == 3 for rec in r.records)


# ---------------------------------------------------------------- dry-run


def test_dry_run_reports_without_mutating(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    snapshot = workbook.clone()
    report = dry_run(config, workbook, ctx)
    assert workbook == snapshot
    assert report.status == "OK"
    assert report.per_tab["MACHINES"].rows_moved == 9
    assert report.per_tab["MACHINES"].rows_formatted == 10
    assert report.per_tab["SUMMARY"].rows_added == 13
    assert [r.rule_id for r in report.rules] == ["sort_by_stage", "status_formatting", "summary"]
    assert report.rules[0].destructive and not report.rules[1].destructive


def test_dry_run_matches_execution(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    run = plan_run(config, workbook, RunEvent.manual(), ctx)
    executed = execute_run(config, workbook, RunEvent.manual(), ctx)
    assert run.projected == executed.workbook
    report = dry_run(config, workbook, ctx)
    assert [r.rows_affected for r in report.rules] == [rec.rows_affected for rec in executed.records]

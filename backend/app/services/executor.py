"""Executor = plan_run + snapshot + all-or-nothing commit.

M1 executes against an in-memory Workbook. M3 swaps the commit step for batched
Sheets API writes; planning stays identical (shared with dry-run).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from app.schemas.config import ConfigSpec
from app.services.conditions import EvalContext
from app.services.grid import Workbook
from app.services.runner import RunEvent, RunPlan, plan_run
from app.services.snapshots import Snapshot, take_snapshot

RecordStatus = Literal["OK", "RETRY_EXHAUSTED", "PAUSED_DRIFT", "ERROR"]


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    rule_id: str | None
    config_version: int
    trigger_type: str
    rows_affected: int
    duration_ms: int
    status: RecordStatus
    error: str | None = None
    snapshot_tabs: tuple[str, ...] = ()


@dataclass
class ExecutionResult:
    workbook: Workbook  # committed state (identical to the input unless status == OK)
    plan: RunPlan
    records: list[RunRecord] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        return self.plan.status == "OK"


def execute_run(config: ConfigSpec, workbook: Workbook, event: RunEvent, ctx: EvalContext) -> ExecutionResult:
    started = time.monotonic()
    plan = plan_run(config, workbook, event, ctx)
    elapsed = int((time.monotonic() - started) * 1000)
    trigger = event.kind
    result = ExecutionResult(workbook=workbook, plan=plan)

    if plan.status == "PAUSED_DRIFT":
        detail = ", ".join(d.tab for d in plan.drift)
        result.records.append(
            RunRecord(ctx.run_id, None, config.config_version, trigger, 0, elapsed, "PAUSED_DRIFT",
                      f"header drift on: {detail}")
        )
        return result

    if plan.status != "OK":
        # Never half-apply: any rule error or guard block aborts the entire run.
        for p in plan.plans:
            reason = p.error or p.blocked or f"aborted: another rule in run failed ({plan.status})"
            result.records.append(
                RunRecord(ctx.run_id, p.rule_id, config.config_version, trigger, 0, elapsed, "ERROR", reason)
            )
        return result

    for p in plan.plans:
        # Snapshot every touched tab (destructive or not) so every run is revertible.
        tabs: list[str] = []
        for change in p.changes:
            result.snapshots.append(take_snapshot(ctx.run_id, p.rule_id, change.tab, change.before))
            tabs.append(change.tab)
        result.records.append(
            RunRecord(ctx.run_id, p.rule_id, config.config_version, trigger, p.rows_affected, elapsed, "OK",
                      snapshot_tabs=tuple(tabs))
        )

    assert plan.projected is not None
    result.workbook = plan.projected  # atomic swap: built entirely from copies
    return result

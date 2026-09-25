"""Plan a run against a grid that is already in memory: the shared path behind every target.

PATCH-005 B.1 made the evaluator the oracle rather than a runtime: it renders the preview the
owner approves and is the reference the generated script is proven against. Planning therefore
has no reader and no writer in it - callers bring a `Grid` (from an uploaded workbook, a fixture,
or in tests the sheets adapter) and get back what the rules would do to it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.adapters.base import Grid, GridOp, SourceRef
from app.schemas.config import ConfigSpec
from app.services.conditions import EvalContext
from app.services.dry_run import DryRunReport, summarize_plan
from app.services.executor import ExecutionResult, execute_run
from app.services.ops import OpSummary, diff_workbooks, summarize_ops
from app.services.runner import RunEvent

log = logging.getLogger("app.run")


class ConfigMismatch(ValueError):
    pass


@dataclass
class PreparedRun:
    run_id: str
    source_ref: SourceRef
    grid: Grid
    ctx: EvalContext
    report: DryRunReport
    result: ExecutionResult
    ops: list[GridOp] = field(default_factory=list)
    summary: dict[str, OpSummary] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return self.result.plan.status


def new_run_id() -> str:
    return "run_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]


def plan_grid(
    grid: Grid,
    config: ConfigSpec,
    event: RunEvent | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
    source_ref: SourceRef | None = None,
) -> PreparedRun:
    """What the config would do to this grid: the plan, the resulting workbook, and the ops."""
    run_id = run_id or new_run_id()
    event = event or RunEvent.manual()
    ref = source_ref if source_ref is not None else config.sheet_id
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(grid.timezone))
    ctx = EvalContext(run_id=run_id, today=local.date(), now=local.replace(tzinfo=None))

    result = execute_run(config, grid.workbook, event, ctx)
    report = summarize_plan(result.plan)
    ops = diff_workbooks(grid, result.workbook) if result.applied else []
    prepared = PreparedRun(run_id, ref, grid, ctx, report, result, ops, summarize_ops(grid, ops))
    log.info("run.planned run_id=%s source=%s status=%s ops=%d tabs=%s",
             run_id, ref, prepared.status, len(ops), sorted(prepared.summary))
    return prepared

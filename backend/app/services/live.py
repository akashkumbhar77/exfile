"""Live execution against an adapter: read -> plan_run (shared with dry-run) -> diff -> commit.

Dry-run and live runs share `prepare_run` entirely; they differ only in whether
`commit_run` is called.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.adapters.base import Adapter, Grid, GridOp, SourceRef, WriteResult
from app.schemas.config import ConfigSpec
from app.services.conditions import EvalContext
from app.services.dry_run import DryRunReport, summarize_plan
from app.services.executor import ExecutionResult, execute_run
from app.services.ops import OpSummary, diff_workbooks, summarize_ops
from app.services.runner import RunEvent
from app.services.snapshot_store import LocalSnapshotStore

log = logging.getLogger("app.run")


class StaleGrid(RuntimeError):
    """The sheet changed between planning and writing; nothing was written."""


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


def prepare_run(
    adapter: Adapter,
    source_ref: SourceRef,
    config: ConfigSpec,
    event: RunEvent | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
) -> PreparedRun:
    if config.sheet_id != source_ref:
        raise ConfigMismatch(f"config is for sheet {config.sheet_id!r}, not {source_ref!r}")
    run_id = run_id or new_run_id()
    event = event or RunEvent.manual()
    grid = adapter.read_grid(source_ref)
    today = (now or datetime.now(UTC)).astimezone(ZoneInfo(grid.timezone)).date()
    ctx = EvalContext(run_id=run_id, today=today)

    result = execute_run(config, grid.workbook, event, ctx)
    report = summarize_plan(result.plan)
    ops = diff_workbooks(grid, result.workbook) if result.applied else []
    prepared = PreparedRun(run_id, source_ref, grid, ctx, report, result, ops, summarize_ops(grid, ops))
    log.info("run.planned run_id=%s sheet=%s status=%s ops=%d tabs=%s",
             run_id, source_ref, prepared.status, len(ops), sorted(prepared.summary))
    for op in ops:
        log.debug("run.op run_id=%s %r", run_id, op)
    return prepared


def commit_run(adapter: Adapter, prepared: PreparedRun, store: LocalSnapshotStore) -> WriteResult:
    """Snapshot, re-check the sheet is unchanged since planning, then write all ops atomically."""
    run_id = prepared.run_id
    if not prepared.ops:
        log.info("run.noop run_id=%s sheet=%s rows_affected=0", run_id, prepared.source_ref)
        return WriteResult(0, 0, 0)

    started = time.monotonic()
    store.save(run_id, prepared.source_ref, prepared.result.snapshots)  # invariant 6: before any write
    fresh = adapter.read_grid(prepared.source_ref)
    if diff_workbooks(fresh, prepared.grid.workbook):
        log.warning("run.stale run_id=%s sheet=%s: sheet edited during run; nothing written",
                    run_id, prepared.source_ref)
        raise StaleGrid("the sheet changed while the run was being planned; nothing was written")
    result = adapter.write_ops(prepared.source_ref, prepared.ops)
    log.info("run.committed run_id=%s sheet=%s requests=%d cells=%d formats=%d duration_ms=%d",
             run_id, prepared.source_ref, result.requests, result.cells_written, result.formats_written,
             int((time.monotonic() - started) * 1000))
    return result

"""RQ job: execute one registered sheet (S2).

Pipeline (never half-applies; logs never contain cell values):
  1. skip unless the sheet is ACTIVE; take a per-sheet lock (else re-arm and exit)
  2. read the grid once; per-tab values fingerprint == last completed run -> NOOP
  3. plan (pre-flight first): drift -> PAUSED_DRIFT, nothing written
  4. snapshot (encrypted, Postgres) -> stale check -> one atomic batchUpdate
  5. record self-write watermark (Drive modifiedTime) + post-run fingerprint
"""

from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass
from datetime import datetime

from app.models import Sheet
from app.services.fingerprint import fingerprint, governed_tabs
from app.services.live import StaleGrid, commit_run, new_run_id, prepare_run
from app.services.registry import (
    ACTIVE,
    PAUSED_DRIFT,
    active_config,
    record_event,
    record_run,
    record_runs,
    safe_error,
)
from app.services.runner import RunEvent
from app.workers.context import WorkerContext, get_context

log = logging.getLogger("app.worker")

LOCK_TTL_SECONDS = 600


@dataclass(frozen=True)
class JobOutcome:
    run_id: str | None
    status: str  # OK | NOOP | PAUSED_DRIFT | BLOCKED | ERROR | STALE | SKIPPED | BUSY
    ops: int = 0


def run_sheet_job(sheet_id: int) -> str:
    """RQ entry point."""
    return execute_sheet(get_context(), sheet_id).status


def execute_sheet(ctx: WorkerContext, sheet_id: int, trigger: str = "change", force: bool = False) -> JobOutcome:
    lock = ctx.redis.lock(f"lock:run:{sheet_id}", timeout=LOCK_TTL_SECONDS, blocking=False)
    if not lock.acquire(blocking=False):
        ctx.debouncer.touch(sheet_id, ctx.clock())  # another run in progress: try again after it
        log.info("run.busy sheet_pk=%d re-armed", sheet_id)
        return JobOutcome(None, "BUSY")
    try:
        return _execute(ctx, sheet_id, trigger, force)
    finally:
        try:
            lock.release()
        except Exception as exc:  # lock expired under a very long run: log, never mask the outcome
            log.warning("run.lock_release_failed sheet_pk=%d %s", sheet_id, safe_error(exc))


def _execute(ctx: WorkerContext, sheet_id: int, trigger: str, force: bool) -> JobOutcome:
    started = time.monotonic()
    run_id = new_run_id()
    with ctx.factory() as s, s.begin():
        sheet = s.get(Sheet, sheet_id)
        if sheet is None or sheet.status != ACTIVE:
            log.info("run.skipped sheet_pk=%d status=%s", sheet_id, sheet.status if sheet else "MISSING")
            return JobOutcome(None, "SKIPPED")
        cfg_row, config = active_config(s, sheet)
        ref, last_fp, version = sheet.google_sheet_id, sheet.last_fingerprint, cfg_row.version
    tabs = governed_tabs(config)

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    def finish(status: str, ops: int = 0) -> JobOutcome:
        log.info("run.%s run_id=%s sheet=%s ops=%d duration_ms=%d", status.lower(), run_id, ref, ops, elapsed())
        return JobOutcome(run_id, status, ops)

    def single(status: str, error: str | None = None, fp: dict[str, str] | None = None) -> None:
        with ctx.factory() as s, s.begin():
            sheet = s.get(Sheet, sheet_id)
            assert sheet is not None
            if fp is not None:
                sheet.last_fingerprint = fp
            record_run(s, sheet, run_id, version, trigger, status, error=error, duration_ms=elapsed())

    try:
        grid = ctx.adapter.read_grid(ref)
        fp = fingerprint(grid.workbook, tabs)
        if not force and last_fp is not None and fp == last_fp:
            single("NOOP")  # PATCH-002 A.2: identical values -> no ops, rows_affected=0
            return finish("NOOP")

        p = prepare_run(ctx.adapter, ref, config, RunEvent("change"), run_id=run_id, now=ctx.clock(), grid=grid)

        if p.status == "PAUSED_DRIFT":
            with ctx.factory() as s, s.begin():
                sheet = s.get(Sheet, sheet_id)
                assert sheet is not None
                sheet.status = PAUSED_DRIFT
                record_runs(s, sheet, p.result.records)
                record_event(s, sheet, "sheet.paused_drift", {
                    "run_id": run_id,
                    "drift": [{"tab": d.tab, "expected": d.expected, "actual": d.actual} for d in p.report.drift],
                })
            return finish("PAUSED_DRIFT")

        if p.status != "OK":  # ERROR / BLOCKED: never half-apply; nothing written
            with ctx.factory() as s, s.begin():
                sheet = s.get(Sheet, sheet_id)
                assert sheet is not None
                record_runs(s, sheet, p.result.records)
            return finish(p.status)

        if not p.ops:
            single("NOOP", fp=fp)
            return finish("NOOP")

        before_write = ctx.feed.modified_time(ref)
        result = commit_run(ctx.adapter, p, ctx.store)
        watermark = self_write_watermark(ctx, ref, before_write, run_id)
        post_fp = fingerprint(p.result.workbook, tabs)
        with ctx.factory() as s, s.begin():
            sheet = s.get(Sheet, sheet_id)
            assert sheet is not None
            sheet.self_write_watermark = watermark
            sheet.last_fingerprint = post_fp
            record_runs(s, sheet, p.result.records)
            record_event(s, sheet, "run.committed", {
                "run_id": run_id, "requests": result.requests, "cells": result.cells_written,
                "formats": result.formats_written, "tabs": sorted(p.summary), "post_fingerprint": post_fp,
            })
        return finish("OK", ops=len(p.ops))
    except StaleGrid as exc:
        single("STALE", error=safe_error(exc))
        ctx.debouncer.touch(sheet_id, ctx.clock())  # the user is still editing: retry once they settle
        return finish("STALE")
    except Exception as exc:
        err = safe_error(exc)
        # traceback frames only: the exception message itself could echo input (B.6)
        log.error("run.error run_id=%s sheet=%s %s\n%s", run_id, ref, err,
                  "".join(traceback.format_tb(exc.__traceback__)))
        single("ERROR", error=err)
        return finish("ERROR")


def self_write_watermark(ctx: WorkerContext, ref: str, before_write: datetime, run_id: str) -> datetime:
    """modifiedTime our write produced (PATCH-002 A.3). If Drive has not caught up within the bound,
    fall back to the pre-write value: our change will then reach the watcher, and the fingerprint
    gate turns it into a logged NOOP (safe, costs one read)."""
    wm = ctx.feed.wait_modified_after(ref, before_write, sleep=ctx.sleep)
    if wm is None:
        log.warning("run.watermark_lagging run_id=%s sheet=%s: Drive modifiedTime did not advance; "
                    "the self-write will be absorbed by the fingerprint gate", run_id, ref)
        return before_write
    return wm

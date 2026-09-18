"""Owner operations on registered sheets: undo a run, resume a paused sheet (S2)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.base import Adapter
from app.adapters.drive_changes import DriveChangesFeed
from app.models import Event, Run, Sheet
from app.services.fingerprint import fingerprint, governed_tabs
from app.services.live import new_run_id
from app.services.ops import diff_workbooks
from app.services.preflight import Drift, preflight
from app.services.registry import (
    ACTIVE,
    RegistryError,
    active_config,
    record_event,
    record_run,
    sheet_by_ref,
)
from app.services.snapshot_store import SnapshotStore
from app.services.snapshots import restore_snapshots, take_snapshot
from app.workers.debounce import Debouncer

log = logging.getLogger("app.ops")


class UndoRefused(RuntimeError):
    pass


@dataclass
class UndoResult:
    undo_run_id: str
    tabs: list[str]
    ops: int


def undo_run(factory: sessionmaker[Session], adapter: Adapter, feed: DriveChangesFeed, store: SnapshotStore,
             run_id: str, force: bool = False, sleep: Callable[[float], None] = time.sleep) -> UndoResult:
    """Restore the tabs a run touched to their exact pre-run state (values, formats, validations).

    Refuses (unless force) if those tabs changed after the run: undo would also erase later edits.
    The undo itself is snapshotted first, so it can be undone too.
    """
    sheet_ref, snaps = store.load(run_id)  # raises SnapshotExpired with a clear message if purged
    with factory() as s:
        sheet = sheet_by_ref(s, sheet_ref)
        _, config = active_config(s, sheet)
        committed = s.scalar(select(Event).where(Event.kind == "run.committed",
                                                 Event.payload["run_id"].astext == run_id))
        post_fp = dict(committed.payload.get("post_fingerprint", {})) if committed else {}
        sheet_pk, version = sheet.id, config.config_version

    current = adapter.read_grid(sheet_ref)
    touched = sorted({sn.tab for sn in snaps})
    if not force and post_fp:
        now_fp = fingerprint(current.workbook, [t for t in touched if t in post_fp])
        changed = [t for t, h in now_fp.items() if post_fp.get(t) != h]
        if changed:
            raise UndoRefused(f"tabs changed since run {run_id}: {changed}; undo would erase those edits "
                              "(re-run with --force to undo anyway)")

    restored = restore_snapshots(current.workbook, snaps)
    ops = diff_workbooks(current, restored)
    undo_id = new_run_id()
    if ops:
        before = [take_snapshot(undo_id, "undo", t, current.workbook.tab(t)) for t in touched]
        store.save(undo_id, sheet_ref, before)
        before_write = feed.modified_time(sheet_ref)
        adapter.write_ops(sheet_ref, ops)
        watermark = feed.wait_modified_after(sheet_ref, before_write, sleep=sleep) or before_write
    tabs_fp = governed_tabs(config)
    with factory() as s, s.begin():
        row = s.get(Sheet, sheet_pk)
        assert row is not None
        if ops:
            row.self_write_watermark = watermark
            row.last_fingerprint = fingerprint(restored, tabs_fp)
        record_run(s, row, undo_id, version, "undo", "OK" if ops else "NOOP", rows_affected=0)
        record_event(s, row, "run.undone", {"undone_run_id": run_id, "undo_run_id": undo_id,
                                               "tabs": touched, "ops": len(ops), "forced": force})
    log.info("run.undone run_id=%s undo_run_id=%s tabs=%s ops=%d", run_id, undo_id, touched, len(ops))
    return UndoResult(undo_id, touched, len(ops))


@dataclass
class ResumeResult:
    resumed: bool
    drift: list[Drift] = field(default_factory=list)


def resume_sheet(factory: sessionmaker[Session], adapter: Adapter, debouncer: Debouncer, sheet_ref: str,
                 now: datetime) -> ResumeResult:
    """PAUSED_DRIFT/PAUSED -> ACTIVE, only if the live headers match the approved config again."""
    with factory() as s:
        sheet = sheet_by_ref(s, sheet_ref)
        _, config = active_config(s, sheet)
        sheet_pk = sheet.id
    grid = adapter.read_grid(sheet_ref)
    pre = preflight(config, grid.workbook)
    if not pre.ok:
        return ResumeResult(False, pre.drift)
    with factory() as s, s.begin():
        row = s.get(Sheet, sheet_pk)
        assert row is not None
        previous = row.status
        row.status = ACTIVE
        row.last_fingerprint = None  # force a real run
        record_event(s, row, "sheet.resumed", {"from": previous})
    debouncer.touch_now(sheet_pk, now)  # organize right away on the next dispatch
    return ResumeResult(True)


def recent_runs(factory: sessionmaker[Session], sheet_ref: str, limit: int = 20) -> list[Run]:
    with factory() as s:
        sheet = sheet_by_ref(s, sheet_ref)
        return list(s.scalars(select(Run).where(Run.sheet_id == sheet.id)
                              .order_by(Run.created_at.desc(), Run.id.desc()).limit(limit)))


__all__ = ["RegistryError", "UndoRefused", "recent_runs", "resume_sheet", "undo_run"]

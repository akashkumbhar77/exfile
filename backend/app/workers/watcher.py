"""Fleet watcher (SPEC-PATCH-002 A): ONE changes-feed loop for every enrolled sheet.

Each poll: changes.list(page_token) -> keep changes to ACTIVE registered sheets ->
drop our own writes (modifiedTime <= self-write watermark) -> debounce touch ->
persist the new page token -> dispatch sheets whose debounce expired -> daily
snapshot purge. Cost per poll is independent of fleet size.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from app.models import WatchState
from app.services.registry import active_sheets, ensure_org, record_event, safe_error
from app.services.snapshot_store import purge_expired_snapshots
from app.workers.context import WorkerContext

log = logging.getLogger("app.watcher")

PURGE_EVERY = timedelta(hours=24)


@dataclass
class PollStats:
    changes: int = 0
    matched: int = 0
    suppressed: int = 0
    touched: list[int] = field(default_factory=list)
    dispatched: list[int] = field(default_factory=list)
    purged: int = 0
    initialized: bool = False


def poll_once(ctx: WorkerContext) -> PollStats:
    stats = PollStats()
    now = ctx.clock()
    with ctx.factory() as s, s.begin():
        ensure_org(s, ctx.org_id)
        state = s.get(WatchState, ctx.org_id)
        if state is None:
            s.add(WatchState(org_id=ctx.org_id, page_token=ctx.feed.start_page_token()))
            stats.initialized = True
            log.info("watch.initialized org=%s", ctx.org_id)
            return stats

        changes, new_token = ctx.feed.list_changes(state.page_token)
        stats.changes = len(changes)
        sheets = active_sheets(s, ctx.org_id)
        per_sheet: Counter[int] = Counter()
        suppressed: Counter[int] = Counter()
        for c in changes:
            sheet = sheets.get(c.file_id)
            if sheet is None or c.removed:
                continue  # not ours, or not ACTIVE (paused sheets wait for `resume`)
            stats.matched += 1
            wm = sheet.self_write_watermark
            if wm is not None and c.modified_time is not None and c.modified_time <= wm:
                suppressed[sheet.id] += 1  # A.3: our own write; never trigger ourselves
                continue
            per_sheet[sheet.id] += 1
            ctx.debouncer.touch(sheet.id, now)
        stats.suppressed = sum(suppressed.values())
        stats.touched = sorted(per_sheet)
        for sheet in sheets.values():
            if per_sheet[sheet.id] or suppressed[sheet.id]:
                record_event(s, sheet, "change.seen",
                             {"changes": per_sheet[sheet.id], "self_writes_suppressed": suppressed[sheet.id]})
        state.page_token = new_token  # persisted with the touches: at-least-once, never lost

        if state.last_purge_at is None or now - state.last_purge_at >= PURGE_EVERY:
            stats.purged = purge_expired_snapshots(ctx.factory, now)
            state.last_purge_at = now

    stats.dispatched = ctx.debouncer.dispatch_due(now, ctx.queue)
    if stats.changes or stats.dispatched:
        log.info("watch.poll changes=%d matched=%d suppressed=%d touched=%s dispatched=%s",
                 stats.changes, stats.matched, stats.suppressed, stats.touched, stats.dispatched)
    return stats


def watch_forever(ctx: WorkerContext, interval_seconds: int) -> None:
    """Poll every `interval_seconds`; dispatch checks run every second so debounce latency stays low."""
    next_poll = 0.0
    while True:
        try:
            if time.monotonic() >= next_poll:
                poll_once(ctx)
                next_poll = time.monotonic() + interval_seconds
            else:
                ctx.debouncer.dispatch_due(ctx.clock(), ctx.queue)
        except Exception as exc:  # keep watching: a transient API/DB error must not stop the fleet
            log.error("watch.error %s", safe_error(exc))
            next_poll = time.monotonic() + interval_seconds
        time.sleep(1)

"""S2 exit criteria (SPEC-PATCH-001 S2, amended by SPEC-PATCH-002 A.5) with the fleet
watcher watching two enrolled sheets at once; edits on one never trigger the other.

Real Postgres (embedded) + Alembic migrations, fake Redis + real RQ jobs, fake Sheets/Drive.
"""

from __future__ import annotations

import ast
import gzip
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Event, Run, Sheet, SnapshotRow, WatchState
from app.services.operations import UndoRefused, resume_sheet, undo_run
from app.services.snapshot_store import SnapshotExpired, purge_expired_snapshots
from app.workers.context import set_context
from tests.fixtures import reference_workbook
from tests.s2_harness import BACKEND, SID_A, SID_B, Fleet, make_fleet, start_postgres

pytest.importorskip("pgserver")

MACHINES = "MACHINES"
STATUS_COL = reference_workbook.MACHINES_HEADERS.index("STATUS") + 1
FIRST_DATA_ROW = 3


@pytest.fixture(scope="session")
def pg_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    return start_postgres(tmp_path_factory.mktemp("pg"))


@pytest.fixture
def fleet(pg_url: str) -> Iterator[Fleet]:
    f = make_fleet(pg_url)
    f.poll()  # first poll initializes the page token; nothing runs
    yield f
    set_context(None)


def runs(f: Fleet, sid: str) -> list[Run]:
    with f.factory() as s:
        return list(s.scalars(select(Run).where(Run.sheet_id == f.pk[sid]).order_by(Run.id)))


def run_ids(f: Fleet, sid: str, status: str | None = None) -> set[str]:
    return {r.run_id for r in runs(f, sid) if status is None or r.status == status}


def sheet_row(f: Fleet, sid: str) -> Sheet:
    with f.factory() as s:
        row = s.get(Sheet, f.pk[sid])
        assert row is not None
        return row


def organize(f: Fleet, sid: str) -> None:
    """Bring a sheet to its organized state through the watcher (first real run)."""
    f.drive.unrelated_change(sid)
    f.tick(30)  # poll sees the change -> debounce armed
    f.tick(30)  # debounce expired -> dispatched -> RQ job runs
    assert sheet_row(f, sid).last_fingerprint is not None


def test_first_poll_only_initializes_the_page_token(fleet: Fleet) -> None:
    with fleet.factory() as s:
        assert s.get(WatchState, "org_test") is not None
    assert fleet.drive.calls == ["changes.getStartPageToken"]
    assert runs(fleet, SID_A) == [] and runs(fleet, SID_B) == []


def test_burst_of_ten_edits_gives_exactly_one_run_and_never_touches_the_other_sheet(fleet: Fleet) -> None:
    organize(fleet, SID_A)
    organize(fleet, SID_B)
    a_runs, b_runs = run_ids(fleet, SID_A), run_ids(fleet, SID_B)
    b_writes = fleet.batch_writes(SID_B)

    statuses = ["Completed", "In-Process", "Cancelled", "Disputed", "In-Process",
                "Completed", "Dispatched", "In-Process", "Cancelled", "Completed"]
    for i, st in enumerate(statuses):  # 10 quick edits over ~45 s, spanning a poll boundary
        fleet.drive.user_edit(SID_A, MACHINES, FIRST_DATA_ROW + (i % 4), STATUS_COL, st)
        fleet.clock.advance(4.5)
        if i == 6:
            fleet.poll()
            fleet.work()
    assert run_ids(fleet, SID_A) == a_runs  # still editing: debounce keeps re-arming

    fleet.tick(30)  # sees the rest of the burst, re-arms
    fleet.tick(30)  # quiet for one interval -> exactly one run
    new = run_ids(fleet, SID_A) - a_runs
    assert len(new) == 1
    assert {r.status for r in runs(fleet, SID_A) if r.run_id in new} == {"OK"}

    for _ in range(4):  # our own write shows up in the feed: suppressed, never re-triggers
        fleet.tick(30)
    assert run_ids(fleet, SID_A) - a_runs == new
    assert run_ids(fleet, SID_B) == b_runs and fleet.batch_writes(SID_B) == b_writes


def test_detection_cost_is_independent_of_fleet_size(fleet: Fleet) -> None:
    fleet.drive.calls.clear()
    for _ in range(5):
        fleet.tick(30)
    assert fleet.drive.calls == ["changes.list"] * 5  # one feed request per poll, no per-sheet polling
    assert all(m == "get" for m, _ in fleet.sheets.calls) or fleet.sheets.calls == []


def test_self_writes_are_suppressed_by_watermark(fleet: Fleet) -> None:
    organize(fleet, SID_A)
    stats = fleet.tick(30)  # the change our own write produced
    assert stats.suppressed >= 1 and stats.touched == []
    assert sheet_row(fleet, SID_A).self_write_watermark is not None


def test_self_write_suppressed_even_when_drive_modified_time_lags(fleet: Fleet) -> None:
    """Live finding (2026-09-18): files.get right after batchUpdate still returned the previous
    modifiedTime. The watermark must wait for Drive to catch up, not record the stale value."""
    fleet.drive.lag_reads_after_commit = 3
    organize(fleet, SID_A)
    wm = sheet_row(fleet, SID_A).self_write_watermark
    assert wm == fleet.drive.modified[SID_A]  # the time our write produced, not the user's edit
    before = run_ids(fleet, SID_A)
    for _ in range(3):
        stats = fleet.tick(30)
        assert stats.touched == []
    assert run_ids(fleet, SID_A) == before  # no extra NOOP run from our own write


def test_change_without_value_change_is_a_logged_noop(fleet: Fleet) -> None:
    organize(fleet, SID_A)
    writes = fleet.batch_writes(SID_A)
    fleet.drive.unrelated_change(SID_A)  # e.g. a comment or column resize: modifiedTime moves, values don't
    fleet.tick(30)
    fleet.tick(30)
    last = runs(fleet, SID_A)[-1]
    assert (last.status, last.rows_affected) == ("NOOP", 0)
    assert fleet.batch_writes(SID_A) == writes


def test_header_rename_pauses_before_any_rule_and_resume_recovers(fleet: Fleet) -> None:
    organize(fleet, SID_A)
    organize(fleet, SID_B)
    a_writes, b_runs = fleet.batch_writes(SID_A), run_ids(fleet, SID_B)

    fleet.drive.user_edit(SID_A, MACHINES, 2, STATUS_COL, "ORDER STATE")  # rename STATUS header
    fleet.tick(30)
    fleet.tick(30)
    assert sheet_row(fleet, SID_A).status == "PAUSED_DRIFT"
    assert fleet.batch_writes(SID_A) == a_writes  # nothing ran against the drifted schema
    assert runs(fleet, SID_A)[-1].status == "PAUSED_DRIFT"
    with fleet.factory() as s:
        ev = s.scalars(select(Event).where(Event.kind == "sheet.paused_drift")).one()
        assert ev.payload["drift"][0]["tab"] == MACHINES

    # while paused: edits on A are ignored; B keeps working independently
    fleet.drive.user_edit(SID_A, MACHINES, FIRST_DATA_ROW, STATUS_COL, "Completed")
    fleet.drive.user_edit(SID_B, MACHINES, FIRST_DATA_ROW, STATUS_COL, "Cancelled")
    fleet.tick(30)
    fleet.tick(30)
    assert fleet.batch_writes(SID_A) == a_writes
    assert len(run_ids(fleet, SID_B) - b_runs) == 1

    # resume refuses while the header is still wrong ...
    r = resume_sheet(fleet.factory, fleet.ctx.adapter, fleet.ctx.debouncer, SID_A, fleet.clock())
    assert not r.resumed and r.drift[0].tab == MACHINES
    # ... and recovers once it is restored
    fleet.drive.user_edit(SID_A, MACHINES, 2, STATUS_COL, "STATUS")
    r = resume_sheet(fleet.factory, fleet.ctx.adapter, fleet.ctx.debouncer, SID_A, fleet.clock())
    assert r.resumed and sheet_row(fleet, SID_A).status == "ACTIVE"
    fleet.tick(1)  # the header-restore edit is now seen and re-arms the debounce
    fleet.tick(30)
    assert runs(fleet, SID_A)[-1].status == "OK"
    assert fleet.batch_writes(SID_A) == a_writes + 1


def test_undo_restores_prior_values_verbatim(fleet: Fleet) -> None:
    before = fleet.ctx.adapter.read_grid(SID_A).workbook
    organize(fleet, SID_A)  # destructive: sorts MACHINES/SPARES, builds SUMMARY
    run_id = next(iter(run_ids(fleet, SID_A, "OK")))
    assert fleet.ctx.adapter.read_grid(SID_A).workbook.tab(MACHINES).values != before.tab(MACHINES).values  # type: ignore[union-attr]

    result = undo_run(fleet.factory, fleet.ctx.adapter, fleet.ctx.feed, fleet.ctx.store, run_id)
    after = fleet.ctx.adapter.read_grid(SID_A).workbook
    assert after.names() == before.names()  # SUMMARY (created by the run) removed again
    for t in before.tabs:
        got = after.tab(t.name)
        assert got is not None and got.values == t.values and got.formats == t.formats, t.name
    assert result.ops > 0

    # the undo's own write is suppressed: the sheet stays as restored until a human edits it
    fleet.tick(30)
    fleet.tick(30)
    assert fleet.ctx.adapter.read_grid(SID_A).workbook.tab(MACHINES).values == before.tab(MACHINES).values  # type: ignore[union-attr]


def test_undo_refuses_when_tabs_changed_since_the_run(fleet: Fleet) -> None:
    organize(fleet, SID_A)
    run_id = next(iter(run_ids(fleet, SID_A, "OK")))
    fleet.drive.user_edit(SID_A, MACHINES, FIRST_DATA_ROW, 2, "a later human edit")
    with pytest.raises(UndoRefused, match="MACHINES"):
        undo_run(fleet.factory, fleet.ctx.adapter, fleet.ctx.feed, fleet.ctx.store, run_id)
    undo_run(fleet.factory, fleet.ctx.adapter, fleet.ctx.feed, fleet.ctx.store, run_id, force=True)


def test_snapshots_encrypted_in_postgres_and_purge_makes_undo_fail_clearly(fleet: Fleet) -> None:
    organize(fleet, SID_A)
    run_id = next(iter(run_ids(fleet, SID_A, "OK")))
    with fleet.factory() as s:
        bodies = [r.body_gz for r in s.scalars(select(SnapshotRow).where(SnapshotRow.run_id == run_id))]
    assert bodies and all(b is not None for b in bodies)
    for b in bodies:
        assert b is not None and b"Acme" not in b and b"Lathe" not in b
        with pytest.raises(OSError):
            gzip.decompress(b)  # ciphertext, not a readable gzip body

    now = datetime.now(UTC)  # rows are stamped by Postgres now(), not the test clock
    assert purge_expired_snapshots(fleet.factory, now + timedelta(days=29)) == 0
    assert purge_expired_snapshots(fleet.factory, now + timedelta(days=31)) == len(bodies)
    with pytest.raises(SnapshotExpired, match="30-day snapshot retention"):
        undo_run(fleet.factory, fleet.ctx.adapter, fleet.ctx.feed, fleet.ctx.store, run_id)


def test_runs_and_events_rows_hold_no_cell_values(fleet: Fleet) -> None:
    organize(fleet, SID_A)
    fleet.drive.user_edit(SID_A, MACHINES, 2, STATUS_COL, "ORDER STATE")
    fleet.tick(30)
    fleet.tick(30)
    secrets = {"Acme", "Beta", "Gamma", "Lathe", "Mill", "Press", "Grinder", "Omega", "Sigma", "INV1"}
    with fleet.factory() as s:
        text = repr([(r.status, r.error, r.rule_id, r.trigger_type) for r in s.scalars(select(Run))])
        text += repr([e.payload for e in s.scalars(select(Event))])
    leaked = {x for x in secrets if x in text}
    assert not leaked


def test_unregistered_files_in_the_feed_are_ignored(fleet: Fleet) -> None:
    fleet.drive.unrelated_change("someone-elses-file")
    stats = fleet.tick(30)
    assert stats.changes == 1 and stats.matched == 0
    assert fleet.sheets.calls == []


def test_no_drive_listing_or_search_calls_in_app_code() -> None:
    """B.9: the changes feed is the only Drive listing surface; never files.list / search."""
    offenders: list[str] = []
    for path in sorted((BACKEND / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "list":
                inner = node.func.value
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) \
                        and inner.func.attr in {"files", "drives", "permissions", "teamdrives"}:
                    offenders.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.keyword) and node.arg == "q":
                offenders.append(f"{path.name}:{node.value.lineno} (search query)")
    assert not offenders

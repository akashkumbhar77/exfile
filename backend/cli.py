"""Operator CLI.

S1:  run --sheet-id X --config path.json [--dry-run]       (file config, local snapshots)
S2:  register --config path.json --approved-by WHO           (propose -> dry-run -> approve)
     watch                                                   (fleet watcher, Drive changes feed)
     worker                                                  (RQ worker executing runs)
     run --sheet-id X [--dry-run]                            (registered sheet, now)
     undo --run-id Y [--force] | resume --sheet-id X | runs --sheet-id X | purge-snapshots
`enroll` (S3, plain-English onboarding) is not built yet.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.adapters.base import StaticRegistry
from app.adapters.sheets_adapter import SheetsAdapter
from app.core.logging import configure
from app.core.settings import Settings, get_settings
from app.schemas.config import ConfigSpec
from app.services.live import PreparedRun, commit_run, prepare_run
from app.services.registry import (
    DbRegistry,
    RegistryError,
    active_config,
    approve_config,
    propose_config,
    reject_config,
    sheet_by_ref,
)
from app.services.runner import RunEvent
from app.services.snapshot_store import DbSnapshotStore, LocalSnapshotStore, SnapshotCipher, SnapshotError
from app.services.validator import validate_config

log = logging.getLogger("app.cli")


def _load_config(path: Path) -> ConfigSpec:
    raw = path.read_text(encoding="utf-8")
    result = validate_config(raw)
    if not result.ok or result.config is None:
        for issue in result.errors:
            print(f"  {issue.pointer}: [{issue.code}] {issue.message}", file=sys.stderr)
        raise SystemExit(f"config {path} is invalid")
    return result.config


def _store(settings: Settings) -> LocalSnapshotStore:
    if not settings.snapshot_key:
        raise SystemExit("SNAPSHOT_KEY is not set: live runs must be able to snapshot (encrypted) first")
    cipher = SnapshotCipher.from_key(settings.snapshot_key, settings.snapshot_key_id)
    return LocalSnapshotStore(settings.snapshot_dir, cipher, settings.snapshot_retention_days)


def _key(settings: Settings) -> str:
    if settings.google_application_credentials is None:
        raise SystemExit("GOOGLE_APPLICATION_CREDENTIALS is not set (see README: operator setup)")
    return str(settings.google_application_credentials)


def _factory(settings: Settings) -> sessionmaker[Session]:
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not set (see README: S2)")
    from app.core.db import default_session_factory

    return default_session_factory()


def _db_store(settings: Settings, factory: sessionmaker[Session]) -> DbSnapshotStore:
    if not settings.snapshot_key:
        raise SystemExit("SNAPSHOT_KEY is not set: runs must be able to snapshot (encrypted) first")
    return DbSnapshotStore(factory, SnapshotCipher.from_key(settings.snapshot_key, settings.snapshot_key_id))


def print_plan(p: PreparedRun) -> None:
    print(f"run {p.run_id}  sheet {p.source_ref}  status {p.status}  (today={p.ctx.today}, tz={p.grid.timezone})")
    for d in p.report.drift:
        print(f"  DRIFT tab {d.tab!r}: expected header hash {d.expected}, actual {d.actual}")
    for r in p.report.rules:
        flag = f"  ERROR: {r.error}" if r.error else f"  BLOCKED: {r.blocked}" if r.blocked else ""
        print(f"  rule {r.rule_id:<22} {r.action:<12} rows_affected={r.rows_affected:<4} tabs={r.tabs}{flag}")
    for w in p.report.warnings:
        print(f"  warning: {w}")
    if not p.summary:
        print("  no changes: 0 ops")
        return
    print(f"  {'tab':<20} {'rows reordered/rewritten':>24} {'cells recolored':>16} {'validations':>12} {'structural':>11}")
    for tab, s in p.summary.items():
        print(f"  {tab:<20} {s.rows_rewritten:>24} {s.cells_recolored:>16} {s.validations:>12} {s.structural:>11}")
    print(f"  total ops: {len(p.ops)}")


# ---- run -----------------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.config is None:
        return _run_registered(args, settings)
    config = _load_config(Path(args.config))
    store = None if args.dry_run else _store(settings)
    adapter = SheetsAdapter.from_service_account(_key(settings), StaticRegistry(settings.registered_ids()))
    prepared = prepare_run(adapter, args.sheet_id, config)
    print_plan(prepared)
    if prepared.status != "OK":
        return 2
    if args.dry_run:
        print("dry-run: nothing written")
        return 0
    assert store is not None
    purged = store.purge_expired()
    if purged:
        log.info("snapshots.purged count=%d", len(purged))
    result = commit_run(adapter, prepared, store)
    print(f"committed: {result.requests} requests, {result.cells_written} cells, {result.formats_written} formats")
    return 0


def _run_registered(args: argparse.Namespace, settings: Settings) -> int:
    """Registered sheet: the same job the watcher dispatches (DB config, DB snapshots, watermark)."""
    factory = _factory(settings)
    if args.dry_run:
        with factory() as s:
            _, config = active_config(s, sheet_by_ref(s, args.sheet_id))
        adapter = SheetsAdapter.from_service_account(_key(settings), DbRegistry(factory))
        prepared = prepare_run(adapter, args.sheet_id, config, RunEvent("change"))
        print_plan(prepared)
        print("dry-run: nothing written")
        return 0 if prepared.status == "OK" else 2
    from app.workers.context import get_context
    from app.workers.execute_run import execute_sheet

    with factory() as s:
        pk = sheet_by_ref(s, args.sheet_id).id
    outcome = execute_sheet(get_context(), pk, trigger="manual", force=True)
    print(f"run {outcome.run_id}: {outcome.status} ({outcome.ops} ops)")
    return 0 if outcome.status in ("OK", "NOOP") else 2


# ---- S2 registry / watcher -----------------------------------------------------------------


def cmd_register(args: argparse.Namespace) -> int:
    """Enroll a sheet with a hand-written config: propose -> dry-run preview -> owner approval."""
    settings = get_settings()
    config = _load_config(Path(args.config))
    factory = _factory(settings)
    with factory() as s, s.begin():
        _, row = propose_config(s, settings.org_id, config, title=args.title or "")
        config_pk, version = row.id, row.version
    adapter = SheetsAdapter.from_service_account(_key(settings), DbRegistry(factory))
    prepared = prepare_run(adapter, config.sheet_id, config, RunEvent("change"))
    print_plan(prepared)
    if prepared.status != "OK":
        with factory() as s, s.begin():
            reject_config(s, config_pk, "cli: dry-run not OK")
        print("config rejected: the dry-run did not pass (nothing activated, nothing written)")
        return 2
    prompt = f"Approve config v{version} for {config.sheet_id} and go live? [y/N] "
    approved = bool(args.yes) or input(prompt).strip().lower() == "y"
    with factory() as s, s.begin():
        if not approved:
            reject_config(s, config_pk, args.approved_by)
            print("rejected: the sheet stays PENDING and is not watched")
            return 1
        approve_config(s, config_pk, args.approved_by)
    print(f"approved by {args.approved_by}: sheet is ACTIVE; the watcher organizes it on its next change "
          f"(or now: cli.py run --sheet-id {config.sheet_id})")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    from app.workers.context import get_context
    from app.workers.watcher import watch_forever

    settings = get_settings()
    print(f"watching org {settings.org_id}: Drive changes feed every {settings.poll_interval_seconds}s, "
          f"debounce {settings.debounce_seconds}s (Ctrl+C to stop)")
    watch_forever(get_context(), settings.poll_interval_seconds)
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    import os

    from rq import SimpleWorker, Worker

    from app.workers.context import get_context

    ctx = get_context()
    queue = ctx.queue.queue  # type: ignore[attr-defined]
    cls = SimpleWorker if os.name == "nt" else Worker  # no fork() on Windows
    cls([queue], connection=ctx.redis).work(with_scheduler=False)
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    from app.adapters.drive_changes import DriveChangesFeed
    from app.services.operations import UndoRefused, undo_run

    settings = get_settings()
    factory = _factory(settings)
    registry = DbRegistry(factory)
    try:
        r = undo_run(factory, SheetsAdapter.from_service_account(_key(settings), registry),
                     DriveChangesFeed.from_service_account(_key(settings), registry),
                     _db_store(settings, factory), args.run_id, force=args.force)
    except (UndoRefused, SnapshotError) as exc:
        print(f"undo refused: {exc}", file=sys.stderr)
        return 2
    print(f"undone {args.run_id}: restored tabs {r.tabs} ({r.ops} ops); this undo is run {r.undo_run_id}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    from app.services.operations import resume_sheet
    from app.workers.context import get_context

    ctx = get_context()
    r = resume_sheet(ctx.factory, ctx.adapter, ctx.debouncer, args.sheet_id, ctx.clock())
    if not r.resumed:
        for d in r.drift:
            print(f"  still drifted: tab {d.tab!r} expected {d.expected}, actual {d.actual}")
        print("not resumed: restore the headers (or approve a new config) first")
        return 2
    print("resumed: ACTIVE; the watcher organizes it on its next dispatch")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    from app.services.operations import recent_runs

    for r in recent_runs(_factory(get_settings()), args.sheet_id, args.limit):
        err = f"  {r.error}" if r.error else ""
        print(f"{r.created_at:%Y-%m-%d %H:%M:%S}  {r.run_id}  {r.trigger_type:<7} {r.status:<12} "
              f"rule={r.rule_id or '-':<20} rows={r.rows_affected}{err}")
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    from app.services.snapshot_store import purge_expired_snapshots

    n = purge_expired_snapshots(_factory(get_settings()))
    print(f"purged {n} expired snapshot bodies")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cli.py")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="organize one sheet now (registered sheet, or --config for S1 mode)")
    run.add_argument("--sheet-id", required=True)
    run.add_argument("--config", help="S1 mode: run this config file instead of the registered one")
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=cmd_run)

    reg = sub.add_parser("register", help="enroll a sheet: propose config, dry-run, approve")
    reg.add_argument("--config", required=True)
    reg.add_argument("--approved-by", required=True, help="who approves (recorded on the config)")
    reg.add_argument("--title")
    reg.add_argument("--yes", action="store_true", help="approve without the interactive prompt")
    reg.set_defaults(func=cmd_register)

    sub.add_parser("watch", help="run the fleet watcher (Drive changes feed)").set_defaults(func=cmd_watch)
    sub.add_parser("worker", help="run the RQ worker that executes runs").set_defaults(func=cmd_worker)

    undo = sub.add_parser("undo", help="restore the tabs a run changed to their exact prior state")
    undo.add_argument("--run-id", required=True)
    undo.add_argument("--force", action="store_true", help="undo even if those tabs changed since")
    undo.set_defaults(func=cmd_undo)

    res = sub.add_parser("resume", help="re-activate a paused sheet once its headers match again")
    res.add_argument("--sheet-id", required=True)
    res.set_defaults(func=cmd_resume)

    runs = sub.add_parser("runs", help="recent runs for a sheet")
    runs.add_argument("--sheet-id", required=True)
    runs.add_argument("--limit", type=int, default=20)
    runs.set_defaults(func=cmd_runs)

    sub.add_parser("purge-snapshots", help="drop expired snapshot bodies now").set_defaults(func=cmd_purge)

    args = parser.parse_args(argv)
    configure(args.verbose)
    from googleapiclient.errors import HttpError

    try:
        return int(args.func(args))
    except HttpError as exc:
        # Google's reason text names requests and permissions, never cell contents.
        print(f"Google API error {exc.status_code}: {exc.reason}", file=sys.stderr)
        print("Nothing was written: the batch is atomic.", file=sys.stderr)
        return 3
    except RegistryError as exc:
        print(f"registry: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

"""Operator CLI (SPEC-PATCH-001 S1).

    uv run python cli.py run --sheet-id X --config path.json [--dry-run] [-v]

`watch` (S2 fleet watcher), `undo`/`resume` (S2) and `enroll` (S3) are not built yet.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from app.adapters.base import StaticRegistry
from app.adapters.sheets_adapter import SheetsAdapter
from app.core.logging import configure
from app.core.settings import Settings, get_settings
from app.schemas.config import ConfigSpec
from app.services.live import PreparedRun, commit_run, prepare_run
from app.services.snapshot_store import LocalSnapshotStore, SnapshotCipher
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


def cmd_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    config = _load_config(Path(args.config))
    if settings.google_application_credentials is None:
        raise SystemExit("GOOGLE_APPLICATION_CREDENTIALS is not set (see README: operator setup)")
    store = None if args.dry_run else _store(settings)
    adapter = SheetsAdapter.from_service_account(
        str(settings.google_application_credentials), StaticRegistry(settings.registered_ids())
    )
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cli.py")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="organize one sheet now (manual / S1)")
    run.add_argument("--sheet-id", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=cmd_run)
    args = parser.parse_args(argv)
    configure(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

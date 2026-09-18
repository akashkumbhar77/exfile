"""S2 test harness: real Postgres (embedded, migrated by Alembic), fake Redis + real RQ
jobs, fake Sheets + Drive changes feed, and a controllable clock."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import fakeredis
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.drive_changes import DriveChangesFeed
from app.adapters.sheets_adapter import SheetsAdapter
from app.schemas.config import ConfigSpec
from app.services.registry import DbRegistry, register_sheet
from app.services.snapshot_store import DbSnapshotStore, SnapshotCipher
from app.workers.context import WorkerContext, set_context
from app.workers.debounce import Debouncer, RqRunQueue
from app.workers.watcher import PollStats, poll_once
from tests.conftest import load_reference_raw
from tests.fake_sheets import FakeClock, FakeDriveService, FakeSheetsService, seed
from tests.fixtures import reference_workbook

BACKEND = Path(__file__).resolve().parents[1]
SID_A = "1SheetAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SID_B = "1SheetBbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
ORG = "org_test"
T0 = datetime(2026, 9, 16, 5, 0, tzinfo=UTC)  # 10:30 IST, reference TODAY
TABLES = ["llm_calls", "profiles", "flags", "events", "runs", "snapshots", "watch_state", "configs", "sheets", "orgs"]


def start_postgres(tmp: Path) -> str:
    import pgserver

    srv = pgserver.get_server(tmp, cleanup_mode="stop")
    url = srv.get_uri().replace("postgresql://", "postgresql+psycopg://")
    r = subprocess.run([sys.executable, "-m", "alembic", "-x", f"url={url}", "upgrade", "head"],
                       cwd=BACKEND, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("alembic upgrade failed:\n" + r.stderr[-2000:])
    return url


def reset_db(factory: sessionmaker[Session]) -> None:
    with factory() as s, s.begin():
        s.execute(text("ALTER TABLE sheets DROP CONSTRAINT IF EXISTS fk_sheets_active_config"))
        s.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
        s.execute(text("ALTER TABLE sheets ADD CONSTRAINT fk_sheets_active_config "
                       "FOREIGN KEY (active_config_id) REFERENCES configs(id)"))


def reference_config(sid: str) -> ConfigSpec:
    raw = load_reference_raw()
    raw["sheet_id"] = sid
    return ConfigSpec.model_validate(raw)


@dataclass
class Fleet:
    ctx: WorkerContext
    sheets: FakeSheetsService
    drive: FakeDriveService
    clock: FakeClock
    redis: Any
    queue: RqRunQueue
    pk: dict[str, int]

    @property
    def factory(self) -> sessionmaker[Session]:
        return self.ctx.factory

    def poll(self) -> PollStats:
        return poll_once(self.ctx)

    def work(self) -> int:
        """Run every queued RQ job in-process (SimpleWorker, burst)."""
        from rq import SimpleWorker

        before = self.queue.queue.finished_job_registry.count + self.queue.queue.failed_job_registry.count
        SimpleWorker([self.queue.queue], connection=self.redis).work(burst=True, logging_level="WARNING")
        failed = self.queue.queue.failed_job_registry.count
        assert failed == 0, "an RQ job crashed (see worker log)"
        return self.queue.queue.finished_job_registry.count + failed - before

    def tick(self, seconds: float) -> PollStats:
        self.clock.advance(seconds)
        stats = self.poll()
        self.work()
        return stats

    def batch_writes(self, sid: str) -> int:
        return sum(1 for m, s in self.sheets.calls if m == "batchUpdate" and s == sid)


def make_fleet(url: str) -> Fleet:
    factory = sessionmaker(create_engine(url, future=True), expire_on_commit=False)
    reset_db(factory)
    clock = FakeClock(T0)
    sheets = FakeSheetsService({SID_A: seed(reference_workbook.build()), SID_B: seed(reference_workbook.build())})
    drive = FakeDriveService(sheets, clock)
    registry = DbRegistry(factory)
    redis = fakeredis.FakeStrictRedis()
    queue = RqRunQueue(redis)
    ctx = WorkerContext(
        factory=factory,
        adapter=SheetsAdapter(sheets, registry, editor_email="sa@example.iam"),
        feed=DriveChangesFeed(drive, registry),
        store=DbSnapshotStore(factory, SnapshotCipher.from_key(Fernet.generate_key().decode(), "k1")),
        redis=redis,
        debouncer=Debouncer(redis, timedelta(seconds=30)),
        queue=queue,
        org_id=ORG,
        clock=clock,
    )
    set_context(ctx)
    pk: dict[str, int] = {}
    with factory() as s, s.begin():
        for sid in (SID_A, SID_B):
            pk[sid] = register_sheet(s, ORG, reference_config(sid), approved_by="owner@test").id
    return Fleet(ctx, sheets, drive, clock, redis, queue, pk)

"""Wiring for workers/CLI: one place that builds adapters, stores and queues from settings.

Tests install their own context (fakes) with `set_context`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.adapters.base import Adapter
from app.adapters.drive_changes import DriveChangesFeed
from app.workers.debounce import Debouncer, RunQueue


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class WorkerContext:
    factory: sessionmaker[Session]
    adapter: Adapter
    feed: DriveChangesFeed
    store: Any  # SnapshotStore (DbSnapshotStore in production)
    redis: Any
    debouncer: Debouncer
    queue: RunQueue
    org_id: str
    clock: Callable[[], datetime] = field(default=utcnow)


_override: WorkerContext | None = None
_default: WorkerContext | None = None


def set_context(ctx: WorkerContext | None) -> None:
    global _override
    _override = ctx


def get_context() -> WorkerContext:
    global _default
    if _override is not None:
        return _override
    if _default is None:
        _default = build_default_context()
    return _default


def build_default_context() -> WorkerContext:
    from redis import Redis

    from app.adapters.sheets_adapter import SheetsAdapter
    from app.core.db import default_session_factory
    from app.core.settings import get_settings
    from app.services.registry import DbRegistry
    from app.services.snapshot_store import DbSnapshotStore, SnapshotCipher
    from app.workers.debounce import RqRunQueue

    settings = get_settings()
    if settings.google_application_credentials is None:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS is not set")
    if not settings.snapshot_key:
        raise RuntimeError("SNAPSHOT_KEY is not set: runs must be able to snapshot (encrypted) first")
    factory = default_session_factory()
    registry = DbRegistry(factory)
    key = str(settings.google_application_credentials)
    redis = Redis.from_url(settings.redis_url)
    return WorkerContext(
        factory=factory,
        adapter=SheetsAdapter.from_service_account(key, registry),
        feed=DriveChangesFeed.from_service_account(key, registry),
        store=DbSnapshotStore(factory, SnapshotCipher.from_key(settings.snapshot_key, settings.snapshot_key_id)),
        redis=redis,
        debouncer=Debouncer(redis, timedelta(seconds=settings.debounce_seconds)),
        queue=RqRunQueue(redis),
        org_id=settings.org_id,
    )

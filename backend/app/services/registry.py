"""Registry operations over the S2 tables: sheets, configs, runs, events, watch state.

Every row carries org_id. Nothing written here contains cell values (B.6): runs
and events hold ids, statuses, counts, tab names, hashes and error summaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.base import SourceRef
from app.models import Config, Event, Org, Run, Sheet, WatchState
from app.schemas.config import ConfigSpec
from app.services.executor import RunRecord

ACTIVE = "ACTIVE"
PAUSED_DRIFT = "PAUSED_DRIFT"
PAUSED = "PAUSED"
PENDING = "PENDING"


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DbRegistry:
    """B.9: the adapter may open a spreadsheet only if it is in the `sheets` table."""

    factory: sessionmaker[Session]

    def is_registered(self, source_ref: SourceRef) -> bool:
        with self.factory() as s:
            return s.scalar(select(Sheet.id).where(Sheet.google_sheet_id == source_ref)) is not None


def ensure_org(s: Session, org_id: str, name: str | None = None) -> Org:
    org = s.get(Org, org_id)
    if org is None:
        org = Org(id=org_id, org_id=org_id, name=name or org_id)
        s.add(org)
        s.flush()
    return org


def sheet_by_ref(s: Session, google_sheet_id: str) -> Sheet:
    sheet = s.scalar(select(Sheet).where(Sheet.google_sheet_id == google_sheet_id))
    if sheet is None:
        raise RegistryError(f"sheet {google_sheet_id} is not registered")
    return sheet


def active_config(s: Session, sheet: Sheet) -> tuple[Config, ConfigSpec]:
    if sheet.active_config_id is None:
        raise RegistryError(f"sheet {sheet.google_sheet_id} has no approved config")
    row = s.get(Config, sheet.active_config_id)
    assert row is not None
    return row, ConfigSpec.model_validate(row.body)


def ensure_pending_sheet(s: Session, org_id: str, google_sheet_id: str, title: str = "") -> Sheet:
    """Enter a sheet into the registry (PENDING) before anything reads it (B.9). Existing rows are
    returned unchanged, so re-onboarding an ACTIVE sheet keeps it running until a new config is approved."""
    sheet = s.scalar(select(Sheet).where(Sheet.google_sheet_id == google_sheet_id))
    if sheet is None:
        sheet = Sheet(org_id=org_id, google_sheet_id=google_sheet_id, title=title, status=PENDING)
        s.add(sheet)
        s.flush()
        record_event(s, sheet, "sheet.pending", {})
    elif sheet.org_id != org_id:
        raise RegistryError("sheet belongs to another org")
    return sheet


def propose_config(s: Session, org_id: str, config: ConfigSpec, title: str = "") -> tuple[Sheet, Config]:
    """Step 1 of enrollment: the sheet enters the registry as PENDING (so B.9 allows reading it for the
    dry-run) with its config PENDING_APPROVAL. The watcher ignores it until approval."""
    ensure_org(s, org_id)
    sheet = s.scalar(select(Sheet).where(Sheet.google_sheet_id == config.sheet_id))
    if sheet is None:
        sheet = Sheet(org_id=org_id, google_sheet_id=config.sheet_id, title=title, status=PENDING)
        s.add(sheet)
        s.flush()
    elif sheet.org_id != org_id:
        raise RegistryError("sheet belongs to another org")
    version = (s.scalar(select(Config.version).where(Config.sheet_id == sheet.id).order_by(Config.version.desc()))
               or 0) + 1
    body = config.model_dump(mode="json", by_alias=True, exclude_none=True)
    body["config_version"] = version
    row = Config(org_id=org_id, sheet_id=sheet.id, version=version, body=body, status="PENDING_APPROVAL")
    s.add(row)
    s.flush()
    record_event(s, sheet, "config.proposed", {"config_version": version})
    return sheet, row


def approve_config(s: Session, config_id: int, approved_by: str) -> Sheet:
    """Step 2 (invariant 11): explicit owner approval after the dry-run preview."""
    row = s.get(Config, config_id)
    if row is None or row.status != "PENDING_APPROVAL":
        raise RegistryError("config is not pending approval")
    sheet = s.get(Sheet, row.sheet_id)
    assert sheet is not None
    for old in s.scalars(select(Config).where(Config.sheet_id == sheet.id, Config.status == ACTIVE)):
        old.status = "SUPERSEDED"
    row.status, row.approved_by, row.approved_at = ACTIVE, approved_by, datetime.now(UTC)
    sheet.active_config_id = row.id
    sheet.status = ACTIVE
    sheet.last_fingerprint = None
    record_event(s, sheet, "config.approved", {"config_version": row.version, "approved_by": approved_by})
    return sheet


def reject_config(s: Session, config_id: int, rejected_by: str) -> None:
    row = s.get(Config, config_id)
    if row is None or row.status != "PENDING_APPROVAL":
        raise RegistryError("config is not pending approval")
    row.status = "REJECTED"
    sheet = s.get(Sheet, row.sheet_id)
    record_event(s, sheet, "config.rejected", {"config_version": row.version, "rejected_by": rejected_by})


def register_sheet(s: Session, org_id: str, config: ConfigSpec, approved_by: str, title: str = "") -> Sheet:
    """propose + approve in one step (tests / scripted setups where approval was already given)."""
    _, row = propose_config(s, org_id, config, title)
    return approve_config(s, row.id, approved_by)


def record_event(s: Session, sheet: Sheet | None, kind: str, payload: dict[str, Any]) -> None:
    org_id = sheet.org_id if sheet is not None else payload.get("org_id", "")
    s.add(Event(org_id=org_id, sheet_id=sheet.id if sheet else None, kind=kind, payload=payload))


def record_runs(s: Session, sheet: Sheet, records: list[RunRecord]) -> None:
    for r in records:
        s.add(Run(org_id=sheet.org_id, sheet_id=sheet.id, run_id=r.run_id, config_version=r.config_version,
                  rule_id=r.rule_id, trigger_type=r.trigger_type, rows_affected=r.rows_affected,
                  duration_ms=r.duration_ms, status=r.status, error=r.error))


def record_run(s: Session, sheet: Sheet, run_id: str, config_version: int, trigger: str, status: str,
               error: str | None = None, rows_affected: int = 0, duration_ms: int = 0) -> None:
    s.add(Run(org_id=sheet.org_id, sheet_id=sheet.id, run_id=run_id, config_version=config_version,
              rule_id=None, trigger_type=trigger, rows_affected=rows_affected, duration_ms=duration_ms,
              status=status, error=error))


def active_sheets(s: Session, org_id: str) -> dict[str, Sheet]:
    rows = s.scalars(select(Sheet).where(Sheet.org_id == org_id, Sheet.status == ACTIVE))
    return {r.google_sheet_id: r for r in rows}


def get_watch_state(s: Session, org_id: str) -> WatchState | None:
    return s.get(WatchState, org_id)


_QUOTED = re.compile(r"(\"[^\"]*\"|'[^']*')")


def safe_error(exc: BaseException) -> str:
    """Error summary safe to persist/log (B.6): our own messages, or type + HTTP status only.

    Google error texts can echo rejected input, so quoted fragments are removed.
    """
    from googleapiclient.errors import HttpError

    from app.services.live import ConfigMismatch, StaleGrid
    from app.services.snapshot_store import SnapshotError

    if isinstance(exc, HttpError):
        return f"HttpError {exc.status_code}: " + _QUOTED.sub("<redacted>", str(exc.reason))[:300]
    if isinstance(exc, (StaleGrid, ConfigMismatch, SnapshotError, RegistryError)):
        return f"{type(exc).__name__}: {exc}"[:500]
    return type(exc).__name__

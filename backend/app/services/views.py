"""Read models for the PATCH-003 pages. Everything here is metadata: ids, statuses, counts,
times, tab/header names. No cell values (B.6).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Config, Event, Org, Run, Sheet, SnapshotRow
from app.schemas.config import ConfigSpec
from app.services.describe import describe_config
from app.services.grid import Workbook

STATUS_RANK = {"ERROR": 6, "PAUSED_DRIFT": 5, "BLOCKED": 4, "STALE": 3, "OK": 2, "NOOP": 1}


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def describe_json(body: dict[str, Any], workbook: Workbook | None = None) -> dict[str, Any]:
    """Readback payload; with the live workbook, tabs resolve as "currently: ..." in sheet order."""
    out = describe_config(ConfigSpec.model_validate(body), workbook).json()
    out["live"] = workbook is not None
    return out


def config_json(c: Config, include_body: bool = False) -> dict[str, Any]:
    body = c.body
    out: dict[str, Any] = {
        "id": c.id, "sheet_id": c.sheet_id, "version": c.version, "status": c.status,
        "created_at": iso(c.created_at),
        "approved_by": c.approved_by, "approved_at": iso(c.approved_at),
        "rejected_by": c.rejected_by, "rejected_at": iso(c.rejected_at), "decision_reason": c.decision_reason,
        "summary_title": c.summary_title, "source": c.source or {},
        "governed_tabs": sorted(body.get("schema_hashes", {})),
        "rule_count": len(body.get("rules", [])),
        "has_consolidate": any(r.get("action") == "consolidate" for r in body.get("rules", [])),
    }
    if include_body:
        out["body"] = body
    return out


def first_run(s: Session, c: Config) -> dict[str, Any] | None:
    """The run an approval queued (DECISIONS "Approval queues the first run"), from the DB.

    None unless a `run.queued` event with trigger "approval" exists for this config version, so
    configs activated without a shown preview never claim a run. Otherwise `state` is "queued"
    until the first run under this version is recorded (whatever its trigger: a BUSY auto-run
    re-arms the debouncer and lands as a "change" run), then "done" with that run's outcome."""
    queued = s.scalar(
        select(Event).where(Event.sheet_id == c.sheet_id, Event.kind == "run.queued",
                            Event.payload["config_version"].as_integer() == c.version)
        .order_by(Event.id).limit(1)
    )
    if queued is None:
        return None
    rows = list(s.scalars(
        select(Run).where(Run.sheet_id == c.sheet_id, Run.config_version == c.version,
                          Run.id == select(func.min(Run.id)).where(Run.sheet_id == c.sheet_id,
                                                                   Run.config_version == c.version)
                          .scalar_subquery())
    ))
    if not rows:
        return {"state": "queued", "queued_at": iso(queued.received_at)}
    run_id = rows[0].run_id
    group = list(s.scalars(select(Run).where(Run.sheet_id == c.sheet_id, Run.run_id == run_id)))
    status = max((r.status for r in group), key=lambda st: STATUS_RANK.get(st, 0))
    return {"state": "done", "queued_at": iso(queued.received_at), "run_id": run_id, "status": status,
            "trigger": group[0].trigger_type,
            "rows_affected": sum(r.rows_affected for r in group) if status == "OK" else 0}


# ---- runs + undo availability ------------------------------------------------------------


@dataclass(frozen=True)
class UndoStatus:
    available: bool
    reason: str | None  # why not, for the tooltip
    snapshot_at: datetime | None
    expires_at: datetime | None

    def json(self, now: datetime) -> dict[str, Any]:
        age = (now - self.snapshot_at).total_seconds() if self.snapshot_at else None
        return {"available": self.available, "reason": self.reason, "snapshot_at": iso(self.snapshot_at),
                "snapshot_age_seconds": int(age) if age is not None else None, "expires_at": iso(self.expires_at)}


def retention_days(s: Session, org_id: str) -> int:
    org = s.get(Org, org_id)
    return org.snapshot_retention_days if org else 30


def undo_status(s: Session, sheet: Sheet, run_id: str, now: datetime) -> UndoStatus:
    snaps = list(s.scalars(select(SnapshotRow).where(SnapshotRow.run_id == run_id)))
    days = retention_days(s, sheet.org_id)
    if not snaps:
        return UndoStatus(False, "this run changed nothing, so there is nothing to undo", None, None)
    taken = min(r.created_at for r in snaps)
    expires = taken + timedelta(days=days)
    if any(r.body_gz is None for r in snaps) or now >= expires:
        return UndoStatus(False, f"snapshots are kept for {days} days; this one has expired", taken, expires)
    undone = s.scalar(select(Event.id).where(Event.kind == "run.undone",
                                              Event.payload["undone_run_id"].astext == run_id))
    if undone is not None:
        return UndoStatus(False, "this run was already undone", taken, expires)
    return UndoStatus(True, None, taken, expires)


def grouped_runs(s: Session, sheet: Sheet, limit: int, now: datetime) -> list[dict[str, Any]]:
    """One entry per run_id (runs rows are per rule), newest first."""
    recent_ids = list(s.execute(
        select(Run.run_id, func.max(Run.id).label("last")).where(Run.sheet_id == sheet.id)
        .group_by(Run.run_id).order_by(func.max(Run.id).desc()).limit(limit)
    ))
    ids = [r.run_id for r in recent_ids]
    rows = list(s.scalars(select(Run).where(Run.sheet_id == sheet.id, Run.run_id.in_(ids)).order_by(Run.id)))
    by: dict[str, list[Run]] = defaultdict(list)
    for r in rows:
        by[r.run_id].append(r)
    out = []
    for run_id in ids:
        group = by[run_id]
        status = max((r.status for r in group), key=lambda st: STATUS_RANK.get(st, 0))
        out.append({
            "run_id": run_id,
            "time": iso(min(r.created_at for r in group)),
            "trigger": group[0].trigger_type,
            "rule_ids": [r.rule_id for r in group if r.rule_id],
            "rows_affected": sum(r.rows_affected for r in group) if status in ("OK",) else 0,
            "status": status,
            "error": next((r.error for r in group if r.error), None),
            "config_version": group[0].config_version,
            "undo": undo_status(s, sheet, run_id, now).json(now),
        })
    return out


def last_undoable_run(s: Session, sheet: Sheet, now: datetime) -> str | None:
    for run in grouped_runs(s, sheet, 50, now):
        if run["undo"]["available"]:
            return str(run["run_id"])
    return None


# ---- flags (derived; the flags table is schema-only in the pilot, PATCH-001 B) --------------


def open_flags(s: Session, sheet: Sheet) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    if sheet.status == "PAUSED_DRIFT":
        ev = s.scalars(select(Event).where(Event.sheet_id == sheet.id, Event.kind == "sheet.paused_drift")
                       .order_by(Event.id.desc())).first()
        flags.append({"kind": "drift", "opened_at": iso(ev.received_at) if ev else None,
                      "tabs": [d.get("tab") for d in (ev.payload.get("drift", []) if ev else [])],
                      "message": "A header in a governed tab changed; automation is paused for this sheet."})
    last_fail = s.scalars(select(Event).where(Event.sheet_id == sheet.id, Event.kind == "onboarding.needs_human")
                          .order_by(Event.id.desc())).first()
    if last_fail is not None:
        later = s.scalar(select(Event.id).where(Event.sheet_id == sheet.id, Event.id > last_fail.id,
                                                Event.kind.in_(["onboarding.proposed", "config.approved"])))
        if later is None:
            flags.append({"kind": "onboarding_failed", "opened_at": iso(last_fail.received_at),
                          "message": str(last_fail.payload.get("reason", ""))[:500]})
    return flags


# ---- sheets ------------------------------------------------------------------------------


def pending_approvals(s: Session, sheet_id: int | None = None) -> list[int]:
    q = select(Config.id).where(Config.status == "PENDING_APPROVAL")
    if sheet_id is not None:
        q = q.where(Config.sheet_id == sheet_id)
    return list(s.scalars(q.order_by(Config.id)))


def sheet_row_json(s: Session, sheet: Sheet, now: datetime) -> dict[str, Any]:
    runs = grouped_runs(s, sheet, 1, now)
    return {
        "id": sheet.id, "google_sheet_id": sheet.google_sheet_id, "title": sheet.title or sheet.google_sheet_id,
        "status": sheet.status, "last_run": runs[0] if runs else None,
        "open_flags": len(open_flags(s, sheet)), "pending_approvals": len(pending_approvals(s, sheet.id)),
    }


def sheet_detail_json(s: Session, sheet: Sheet, now: datetime) -> dict[str, Any]:
    active = s.get(Config, sheet.active_config_id) if sheet.active_config_id else None
    last = last_undoable_run(s, sheet, now)
    return {
        **sheet_row_json(s, sheet, now),
        "active_config": ({**config_json(active), "describe": describe_json(active.body)} if active else None),
        "undo_last": ({"run_id": last, **undo_status(s, sheet, last, now).json(now)} if last
                      else {"run_id": None, "available": False, "reason": "no run can be undone"}),
        "flags": open_flags(s, sheet),
        "pending_config_ids": pending_approvals(s, sheet.id),
        "retention_days": retention_days(s, sheet.org_id),
    }


def fleet_summary(s: Session, org_id: str) -> dict[str, Any]:
    sheets = list(s.scalars(select(Sheet).where(Sheet.org_id == org_id)))
    counts = {"active": 0, "paused": 0, "paused_drift": 0, "pending": 0}
    for sh in sheets:
        key = {"ACTIVE": "active", "PAUSED": "paused", "PAUSED_DRIFT": "paused_drift"}.get(sh.status, "pending")
        counts[key] += 1
    day_ago = datetime.now(UTC) - timedelta(hours=24)
    total = s.scalar(select(func.count(func.distinct(Run.run_id))).where(Run.org_id == org_id,
                                                                        Run.created_at >= day_ago)) or 0
    errors = s.scalar(select(func.count(func.distinct(Run.run_id))).where(
        Run.org_id == org_id, Run.created_at >= day_ago, Run.status == "ERROR")) or 0
    return {
        "sheets": len(sheets), **counts,
        "pending_approvals": len([c for c in pending_approvals(s) if s.get(Config, c).org_id == org_id]),  # type: ignore[union-attr]
        "open_flags": sum(len(open_flags(s, sh)) for sh in sheets),
        "runs_24h": total, "error_runs_24h": errors,
        "error_rate_24h": round(errors / total, 4) if total else 0.0,
    }

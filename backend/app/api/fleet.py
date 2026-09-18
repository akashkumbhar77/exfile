"""Fleet summary, jobs, meta, health (SPEC §3, PATCH-003 B2/B4)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import Auth, Ctx, job_queue
from app.api.errors import ApiError
from app.core.settings import get_settings
from app.models import Enrollment
from app.services.views import fleet_summary

router = APIRouter(tags=["fleet"], dependencies=[Auth])
public = APIRouter(tags=["health"])


@router.get("/fleet/summary")
def summary(ctx: Ctx) -> dict[str, Any]:
    with ctx.factory() as s:
        return fleet_summary(s, ctx.org_id)


@router.get("/meta")
def meta(ctx: Ctx) -> dict[str, Any]:
    s = get_settings()
    return {
        "service_account_email": ctx.service_account_email,
        "org_id": ctx.org_id,
        "poll_interval_seconds": s.poll_interval_seconds,
        "debounce_seconds": s.debounce_seconds,
        "snapshot_retention_days_default": s.snapshot_retention_days,
        "models": {"primary": s.llm_primary_model, "escalation": s.llm_escalation_model},
        "share_instructions": [
            "Open the sheet in Google Sheets and click Share.",
            f"Add {ctx.service_account_email or 'the service account'} as Editor (untick 'Notify people').",
            "If a tab is protected (e.g. a locked summary), add the service account to that protection too.",
        ],
    }


@router.get("/jobs/{job_id}")
def job(ctx: Ctx, job_id: str) -> dict[str, Any]:
    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    try:
        j = Job.fetch(job_id, connection=job_queue().connection)
    except NoSuchJobError as exc:
        raise ApiError(404, f"job {job_id} not found") from exc
    meta = dict(j.meta or {})
    state = meta.get("state", "queued")
    failure: str | None = None
    failures: list[str] = []
    enrollment_id = meta.get("enrollment_id")
    if enrollment_id is not None:  # failure text lives in Postgres only (B.6 addendum)
        with ctx.factory() as s:
            row = s.get(Enrollment, int(enrollment_id))
            if row is None or row.org_id != ctx.org_id:
                raise ApiError(404, f"job {job_id} not found")
            failure, failures = row.failure, list(row.failures or [])
    if j.is_failed and state not in ("failed", "proposed"):
        state, failure = "failed", "onboarding stopped unexpectedly; see server logs"
    return {
        "job_id": j.id, "state": state, "sheet_id": meta.get("sheet_id"),
        "config_id": meta.get("config_id"), "config_version": meta.get("config_version"),
        "failure": failure if state == "failed" else None, "failures": failures if state == "failed" else [],
        "session_id": meta.get("session_id"), "model": meta.get("model"), "updated_at": meta.get("updated_at"),
        "done": state in ("proposed", "failed"),
    }


@public.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

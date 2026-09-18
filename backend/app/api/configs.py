"""Configs + approvals (SPEC §3, SPEC-PATCH-003 B1)."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import Auth, Ctx, registered_config
from app.api.errors import ApiError
from app.models import Config, Sheet
from app.schemas.config import ConfigSpec
from app.services.preview import compute_preview
from app.services.registry import (
    RegistryError,
    apply_summary_title,
    approve_config,
    record_event,
    reject_config,
    safe_error,
)
from app.services.views import config_json, describe_json, first_run

router = APIRouter(prefix="/configs", tags=["configs"], dependencies=[Auth])
log = logging.getLogger("app.api")


class ApproveBody(BaseModel):
    actor: Annotated[str, Field(min_length=1, max_length=200)]
    summary_title: Annotated[str | None, Field(max_length=200)] = None


class RejectBody(BaseModel):
    actor: Annotated[str, Field(min_length=1, max_length=200)]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


def _load(ctx: Ctx, config_id: int) -> tuple[Config, Sheet]:
    return registered_config(ctx, config_id)


def _sheet_ref(sheet: Sheet) -> dict[str, Any]:
    return {"id": sheet.id, "google_sheet_id": sheet.google_sheet_id,
            "title": sheet.title or sheet.google_sheet_id, "status": sheet.status}


@router.get("")
def list_configs(ctx: Ctx, status: str | None = None, sheet_id: int | None = None) -> dict[str, Any]:
    with ctx.factory() as s:
        q = select(Config).where(Config.org_id == ctx.org_id).order_by(Config.created_at.desc(), Config.id.desc())
        if status:
            q = q.where(Config.status == status)
        if sheet_id is not None:
            q = q.where(Config.sheet_id == sheet_id)
        items = []
        for c in s.scalars(q.limit(200)):
            sheet = s.get(Sheet, c.sheet_id)
            assert sheet is not None
            items.append({**config_json(c), "sheet": _sheet_ref(sheet)})
    return {"items": items}


@router.get("/{config_id}")
def get_config(ctx: Ctx, config_id: int) -> dict[str, Any]:
    row, sheet = _load(ctx, config_id)
    with ctx.factory() as s:
        run = first_run(s, row)
    return {**config_json(row, include_body=True), "sheet": _sheet_ref(sheet), "first_run": run}


@router.get("/{config_id}/describe")
def describe(ctx: Ctx, config_id: int, summary_title: str | None = None, live: bool = True) -> dict[str, Any]:
    """Plain-English readback. With live=true (default) tab selectors are resolved against the sheet
    as it is now ("currently: ..."); if the sheet can't be read, the static readback is returned
    with live=false."""
    row, sheet = _load(ctx, config_id)
    body = apply_summary_title(row.body, summary_title) if summary_title and summary_title.strip() else row.body
    workbook = None
    if live:
        try:
            workbook = ctx.adapter.read_grid(sheet.google_sheet_id).workbook
        except Exception:  # unreadable right now: fall back to the static readback, flagged live=false
            workbook = None
    return describe_json(body, workbook)


@router.get("/{config_id}/dry-run/preview")
def dry_run_preview(ctx: Ctx, config_id: int, summary_title: str | None = None,
                    limit: Annotated[int, Query(ge=1, le=20)] = 20) -> dict[str, Any]:
    """Computed on demand from the live sheet; not persisted (PATCH-003 A.3, B.6)."""
    row, sheet = _load(ctx, config_id)
    body = apply_summary_title(row.body, summary_title) if summary_title and summary_title.strip() else row.body
    config = ConfigSpec.model_validate(body)
    return {"config_id": config_id, **compute_preview(ctx.adapter, sheet.google_sheet_id, config, ctx.clock(), limit)}


@router.post("/{config_id}/approve")
def approve(ctx: Ctx, config_id: int, body: ApproveBody) -> dict[str, Any]:
    """Owner approval from the Approvals page, where the dry-run preview is shown above the button.
    The preview is the consent, so approval queues exactly one run to apply it (DECISIONS "Approval
    queues the first run"). `approve_config` itself never runs anything: a path that activates a
    config without a shown preview gets no automatic write."""
    _load(ctx, config_id)
    try:
        with ctx.factory() as s, s.begin():
            sheet = approve_config(s, config_id, body.actor.strip(), summary_title=body.summary_title)
            sheet_pk, version = sheet.id, s.get(Config, config_id).version  # type: ignore[union-attr]
    except RegistryError as exc:
        raise ApiError(409, str(exc)) from exc
    queue_first_run(ctx, sheet_pk, version)
    return get_config(ctx, config_id)


def queue_first_run(ctx: Ctx, sheet_pk: int, version: int) -> None:
    """Best effort: the approval is already committed. If the queue is down the sheet is still
    ACTIVE and the watcher applies the config on the next edit; the page then shows no first run."""
    try:
        job_id = ctx.queue.enqueue_run(sheet_pk, trigger="approval")
    except Exception as exc:  # logged (B.6-safe), never raised: approval must not fail after commit
        log.warning("approve.first_run_not_queued sheet_pk=%d version=%d %s", sheet_pk, version, safe_error(exc))
        return
    with ctx.factory() as s, s.begin():
        record_event(s, s.get(Sheet, sheet_pk), "run.queued",
                     {"trigger": "approval", "config_version": version, "job_id": job_id})


@router.post("/{config_id}/reject")
def reject(ctx: Ctx, config_id: int, body: RejectBody) -> dict[str, Any]:
    _load(ctx, config_id)
    try:
        with ctx.factory() as s, s.begin():
            reject_config(s, config_id, body.actor.strip(), body.reason)
    except RegistryError as exc:
        raise ApiError(409, str(exc)) from exc
    return get_config(ctx, config_id)

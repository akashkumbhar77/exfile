"""Configs + approvals (SPEC §3, SPEC-PATCH-003 B1)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import Auth, Ctx, registered_config
from app.api.errors import ApiError
from app.models import Config, Sheet
from app.schemas.config import ConfigSpec
from app.services.preview import compute_preview
from app.services.registry import RegistryError, apply_summary_title, approve_config, reject_config
from app.services.views import config_json, describe_json

router = APIRouter(prefix="/configs", tags=["configs"], dependencies=[Auth])


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
    return {**config_json(row, include_body=True), "sheet": _sheet_ref(sheet)}


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
    _load(ctx, config_id)
    try:
        with ctx.factory() as s, s.begin():
            approve_config(s, config_id, body.actor.strip(), summary_title=body.summary_title)
    except RegistryError as exc:
        raise ApiError(409, str(exc)) from exc
    return get_config(ctx, config_id)


@router.post("/{config_id}/reject")
def reject(ctx: Ctx, config_id: int, body: RejectBody) -> dict[str, Any]:
    _load(ctx, config_id)
    try:
        with ctx.factory() as s, s.begin():
            reject_config(s, config_id, body.actor.strip(), body.reason)
    except RegistryError as exc:
        raise ApiError(409, str(exc)) from exc
    return get_config(ctx, config_id)

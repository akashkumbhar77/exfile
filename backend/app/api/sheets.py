"""Sheets: fleet list, enrollment, detail, runs, drift, pause/resume, undo (SPEC §3, PATCH-003 B2-B4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import Auth, Ctx, job_queue, registered_sheet
from app.api.errors import ApiError
from app.models import Config, Sheet
from app.schemas.config import ConfigSpec
from app.services.grid import cell_text
from app.services.operations import UndoRefused, resume_sheet, undo_run
from app.services.registry import (
    ACTIVE,
    PAUSED,
    PAUSED_DRIFT,
    ensure_org,
    ensure_pending_sheet,
    record_event,
)
from app.services.sheet_ref import BadSheetRef, extract_sheet_id
from app.services.snapshot_store import SnapshotError, SnapshotExpired
from app.services.views import (
    config_json,
    grouped_runs,
    last_undoable_run,
    sheet_detail_json,
    sheet_row_json,
    undo_status,
)

router = APIRouter(prefix="/sheets", tags=["sheets"], dependencies=[Auth])


def wall() -> datetime:
    """Real time for ages/expiry: snapshot and run rows are stamped by Postgres now()."""
    return datetime.now(UTC)


class SheetInput(BaseModel):
    sheet: Annotated[str, Field(min_length=1, max_length=500, description="Google Sheets URL or spreadsheet id")]


class EnrollBody(SheetInput):
    instruction: Annotated[str, Field(min_length=1, max_length=4000)]


class UndoBody(BaseModel):
    run_id: str | None = None  # default: the most recent run that can be undone
    force: bool = False


def _sheet(ctx: Ctx, sheet_id: int) -> Sheet:
    return registered_sheet(ctx, sheet_id)


def _ref(text: str) -> str:
    try:
        return extract_sheet_id(text)
    except BadSheetRef as exc:
        raise ApiError(400, str(exc)) from exc


@router.get("")
def list_sheets(ctx: Ctx) -> dict[str, Any]:
    with ctx.factory() as s:
        sheets = list(s.scalars(select(Sheet).where(Sheet.org_id == ctx.org_id).order_by(Sheet.id)))
        return {"items": [sheet_row_json(s, sh, wall()) for sh in sheets]}


@router.post("/access-check")
def access_check(ctx: Ctx, body: SheetInput) -> dict[str, Any]:
    """Registers the sheet as PENDING first (B.9: we only open registered files), then tries a
    metadata read. Reports ok / forbidden / not_found; never reads cell data."""
    ref = _ref(body.sheet)
    with ctx.factory() as s, s.begin():
        ensure_org(s, ctx.org_id)
        sheet = ensure_pending_sheet(s, ctx.org_id, ref)
        sheet_pk = sheet.id
    read_meta = getattr(ctx.adapter, "read_meta", None)
    if read_meta is None:
        raise ApiError(501, "this adapter cannot check access")
    try:
        meta = read_meta(ref)
    except Exception as exc:
        status = getattr(exc, "status_code", None) or getattr(getattr(exc, "resp", None), "status", None)
        if status in (403, 401):
            return {"sheet_id": ref, "id": sheet_pk, "access": "forbidden", "ok": False,
                    "message": f"share the sheet with {ctx.service_account_email or 'the service account'} "
                               "as Editor, then check again"}
        if status == 404:
            return {"sheet_id": ref, "id": sheet_pk, "access": "not_found", "ok": False,
                    "message": "no spreadsheet with that id is visible to the service account"}
        raise
    with ctx.factory() as s, s.begin():
        row = s.get(Sheet, sheet_pk)
        assert row is not None
        if meta.get("title"):
            row.title = str(meta["title"])[:300]
    return {"sheet_id": ref, "id": sheet_pk, "access": "ok", "ok": True, "title": meta.get("title", ""),
            "tabs": meta.get("tabs", []), "timezone": meta.get("timezone", "")}


@router.post("", status_code=202)
def enroll(ctx: Ctx, body: EnrollBody) -> dict[str, Any]:
    """Kicks the onboarding agent as a job; poll GET /jobs/{job_id}."""
    from app.workers.onboarding_job import run_onboarding

    ref = _ref(body.sheet)
    with ctx.factory() as s, s.begin():
        ensure_org(s, ctx.org_id)
        sheet_pk = ensure_pending_sheet(s, ctx.org_id, ref).id
    job = job_queue().enqueue(run_onboarding, ref, body.instruction, job_timeout=900, result_ttl=86400,
                              failure_ttl=86400, meta={"state": "queued", "sheet_id": sheet_pk})
    return {"job_id": job.id, "sheet_id": sheet_pk, "google_sheet_id": ref, "state": "queued"}


@router.get("/{sheet_id}")
def get_sheet(ctx: Ctx, sheet_id: int) -> dict[str, Any]:
    sheet = _sheet(ctx, sheet_id)
    with ctx.factory() as s:
        return sheet_detail_json(s, s.get(Sheet, sheet.id), wall())  # type: ignore[arg-type]


@router.get("/{sheet_id}/runs")
def runs(ctx: Ctx, sheet_id: int, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict[str, Any]:
    sheet = _sheet(ctx, sheet_id)
    with ctx.factory() as s:
        return {"items": grouped_runs(s, sheet, limit, wall())}


@router.get("/{sheet_id}/configs")
def config_history(ctx: Ctx, sheet_id: int) -> dict[str, Any]:
    _sheet(ctx, sheet_id)
    with ctx.factory() as s:
        rows = s.scalars(select(Config).where(Config.sheet_id == sheet_id).order_by(Config.version.desc()))
        return {"items": [config_json(c) for c in rows]}


@router.get("/{sheet_id}/drift")
def drift(ctx: Ctx, sheet_id: int) -> dict[str, Any]:
    """What changed in the governed headers: approved header text vs the live sheet (structure only)."""
    sheet = _sheet(ctx, sheet_id)
    with ctx.factory() as s:
        cfg_row = s.get(Config, sheet.active_config_id) if sheet.active_config_id else None
    if cfg_row is None:
        raise ApiError(409, "the sheet has no approved config")
    config = ConfigSpec.model_validate(cfg_row.body)
    from app.services.preflight import compute_schema_hash

    grid = ctx.adapter.read_grid(sheet.google_sheet_id)
    expected_all = cfg_row.governed_headers or {}
    tabs = []
    for name, expected_hash in config.schema_hashes.items():
        tab = grid.workbook.tab(name)
        current = [cell_text(h).strip() for h in tab.row(config.header_row)] if tab else None
        ok = tab is not None and compute_schema_hash(tab.row(config.header_row)) == expected_hash
        expected = expected_all.get(name)
        entry: dict[str, Any] = {"tab": name, "ok": ok, "tab_missing": tab is None,
                                 "expected_headers": expected, "current_headers": current}
        if expected is not None and current is not None and not ok:
            entry["changes"] = [{"column": i + 1, "expected": e, "current": c}
                                for i, (e, c) in enumerate(_pad(expected, current)) if e != c]
        tabs.append(entry)
    return {"sheet_id": sheet_id, "status": sheet.status, "drifted": any(not t["ok"] for t in tabs), "tabs": tabs}


def _pad(a: list[str], b: list[str]) -> list[tuple[str, str]]:
    n = max(len(a), len(b))
    return list(zip(a + [""] * (n - len(a)), b + [""] * (n - len(b))))


@router.post("/{sheet_id}/pause")
def pause(ctx: Ctx, sheet_id: int) -> dict[str, Any]:
    """Stops automation for this sheet only. Never protects ranges or blocks editing (invariant 8)."""
    _sheet(ctx, sheet_id)
    with ctx.factory() as s, s.begin():
        sheet = s.get(Sheet, sheet_id)
        assert sheet is not None
        if sheet.status not in (ACTIVE, PAUSED_DRIFT):
            raise ApiError(409, f"a {sheet.status} sheet cannot be paused")
        previous, sheet.status = sheet.status, PAUSED
        record_event(s, sheet, "sheet.paused", {"from": previous})
    return get_sheet(ctx, sheet_id)


@router.post("/{sheet_id}/resume")
def resume(ctx: Ctx, sheet_id: int) -> dict[str, Any]:
    sheet = _sheet(ctx, sheet_id)
    if sheet.status not in (PAUSED, PAUSED_DRIFT):
        raise ApiError(409, f"a {sheet.status} sheet is not paused")
    r = resume_sheet(ctx.factory, ctx.adapter, ctx.debouncer, sheet.google_sheet_id, ctx.clock())
    if not r.resumed:
        raise ApiError(409, "the headers still differ from the approved config; fix them, then resume",
                       code="still_drifted", details=[{"tab": d.tab} for d in r.drift])
    return get_sheet(ctx, sheet_id)


@router.get("/{sheet_id}/undo-preview")
def undo_preview(ctx: Ctx, sheet_id: int, run_id: str | None = None) -> dict[str, Any]:
    """For the confirm dialog: snapshot age and which tabs will be restored (no cell values)."""
    sheet = _sheet(ctx, sheet_id)
    with ctx.factory() as s:
        target = run_id or last_undoable_run(s, sheet, wall())
        if target is None:
            raise ApiError(404, "no run of this sheet can be undone")
        status = undo_status(s, sheet, target, wall()).json(wall())
        from app.models import SnapshotRow

        tabs = sorted({r.tab for r in s.scalars(select(SnapshotRow).where(SnapshotRow.run_id == target))})
    return {"run_id": target, "tabs": tabs, **status,
            "note": "restores these tabs exactly as they were just before the run"}


@router.post("/{sheet_id}/undo")
def undo(ctx: Ctx, sheet_id: int, body: UndoBody) -> dict[str, Any]:
    sheet = _sheet(ctx, sheet_id)
    with ctx.factory() as s:
        target = body.run_id or last_undoable_run(s, sheet, wall())
        if target is None:
            raise ApiError(404, "no run of this sheet can be undone")
        status = undo_status(s, sheet, target, wall())
    if not status.available:
        raise ApiError(410 if status.reason and "expired" in status.reason else 409, status.reason or "cannot undo")
    try:
        result = undo_run(ctx.factory, ctx.adapter, ctx.feed, ctx.store, target, force=body.force)
    except UndoRefused as exc:
        raise ApiError(409, str(exc), code="tabs_changed_since_run") from exc
    except SnapshotExpired as exc:
        raise ApiError(410, str(exc)) from exc
    except SnapshotError as exc:
        raise ApiError(404, str(exc)) from exc
    return {"undone_run_id": target, "undo_run_id": result.undo_run_id, "tabs": result.tabs, "ops": result.ops}

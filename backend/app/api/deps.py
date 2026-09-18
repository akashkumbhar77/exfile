"""API dependencies: pilot-grade bearer auth (PATCH-003 C), shared context, job queue."""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import Depends, Header

from app.api.errors import ApiError
from app.core.settings import get_settings
from app.models import Config, Sheet
from app.workers.context import WorkerContext, get_context

_context: WorkerContext | None = None
_job_queue: Any = None


def set_api_context(ctx: WorkerContext | None, job_queue: Any = None) -> None:
    """Tests (and the ASGI factory) install the context; production builds it from settings."""
    global _context, _job_queue
    _context, _job_queue = ctx, job_queue


def context() -> WorkerContext:
    return _context or get_context()


def job_queue() -> Any:
    global _job_queue
    if _job_queue is None:
        from rq import Queue

        from app.workers.onboarding_job import QUEUE

        _job_queue = Queue(QUEUE, connection=context().redis)
    return _job_queue


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().api_token
    if not expected:
        raise ApiError(503, "API_TOKEN is not configured on the server")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.strip(), expected):
        raise ApiError(401, "missing or invalid bearer token")


Ctx = Annotated[WorkerContext, Depends(context)]
Auth = Depends(require_token)


def registered_sheet(ctx: WorkerContext, sheet_id: int) -> Sheet:
    """B.9 at the API layer: the id must be a row in the `sheets` registry of this org. Anything
    else is 404 before any Google call is made (the adapter's registry guard is the second layer)."""
    with ctx.factory() as s:
        sheet = s.get(Sheet, sheet_id)
        if sheet is None or sheet.org_id != ctx.org_id:
            raise ApiError(404, f"sheet {sheet_id} not found")
        return sheet


def registered_config(ctx: WorkerContext, config_id: int) -> tuple[Config, Sheet]:
    """A config is reachable only through its registered sheet in this org (same 404 otherwise)."""
    with ctx.factory() as s:
        row = s.get(Config, config_id)
        sheet = s.get(Sheet, row.sheet_id) if row is not None else None
        if row is None or sheet is None or row.org_id != ctx.org_id or sheet.org_id != ctx.org_id:
            raise ApiError(404, f"config {config_id} not found")
        return row, sheet

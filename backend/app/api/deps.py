"""API dependencies: pilot-grade bearer auth (PATCH-003 C), shared context, job queue."""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import Depends, Header

from app.api.errors import ApiError
from app.core.settings import get_settings
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

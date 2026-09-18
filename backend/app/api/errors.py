"""One JSON error envelope for every failure (no raw tracebacks reach the client).

    {"error": {"code": "not_found", "message": "...", "request_id": "req_...", "details": [...]}}

B.6: messages are either ours (known, value-free exceptions) or generic. Unknown exceptions are
logged by type + traceback frames only (never their message, which could echo data), and the
client gets "internal error" plus the request id to correlate. Request-validation errors list
field locations and reasons but never the submitted input.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("app.api")

_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
          409: "conflict", 410: "gone", 422: "invalid_request", 429: "rate_limited", 503: "unavailable"}


class ApiError(Exception):
    """Raised by routes for expected failures; `message` must never contain cell values."""

    def __init__(self, status: int, message: str, code: str | None = None, details: list[Any] | None = None):
        super().__init__(message)
        self.status, self.message, self.code, self.details = status, message, code or _CODES.get(status, "error"), details


def request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if not rid:
        rid = "req_" + uuid.uuid4().hex[:12]
        request.state.request_id = rid
    return str(rid)


def envelope(request: Request, status: int, code: str, message: str, details: list[Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message, "request_id": request_id(request)}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body, headers={"X-Request-ID": request_id(request)})


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def _request_id(request: Request, call_next: Any) -> Any:
        request_id(request)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id(request)
        return response

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return envelope(request, exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else _CODES.get(exc.status_code, "error")
        return envelope(request, exc.status_code, _CODES.get(exc.status_code, "error"), message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": [str(p) for p in e.get("loc", ())], "reason": e.get("msg", "")} for e in exc.errors()]
        return envelope(request, 422, "invalid_request", "the request is not valid", details)  # no `input` echo

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        rid = request_id(request)
        log.error("api.unhandled request_id=%s path=%s type=%s\n%s", rid, request.url.path, type(exc).__name__,
                  "".join(traceback.format_tb(exc.__traceback__)))
        return envelope(request, 500, "internal_error", "internal error")

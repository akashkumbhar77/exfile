"""FastAPI app: /api/v1 (SPEC §3 + SPEC-PATCH-003 section B).

Run: `uv run python cli.py api` (uvicorn on :8000). The Vite dev server proxies /api to it;
CORS allows the configured origins for direct calls.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import configs, errors, fleet, sheets
from app.core.settings import get_settings

API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    app = FastAPI(title="Sheets Automation API", version="0.5.0", docs_url=f"{API_PREFIX}/docs",
                  openapi_url=f"{API_PREFIX}/openapi.json")
    errors.install(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins,
        allow_credentials=False,  # bearer token in a header, no cookies
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Request-ID"],
    )
    api = APIRouter(prefix=API_PREFIX)
    for r in (configs.router, sheets.router, fleet.router, fleet.public):
        api.include_router(r)
    app.include_router(api)
    return app

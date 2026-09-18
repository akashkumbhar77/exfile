"""Route-table security checks for /api/v1, driven by the app's own route table so that any
endpoint added later is covered automatically.

(a) every endpoint except /health answers 401 without a bearer token;
(b) B.9 at the API layer: every endpoint taking a {sheet_id} or {config_id} answers 404 for an id
    that is not in this org's registry -- and makes no Google call for it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import set_api_context
from app.api.main import API_PREFIX, create_app
from app.core.settings import get_settings
from app.models import Config, Org, Sheet
from app.workers.context import set_context
from tests.conftest import load_reference_raw
from tests.s2_harness import Fleet, make_fleet, start_postgres

pytest.importorskip("pgserver")

TOKEN = "sec-token"
PUBLIC = {f"{API_PREFIX}/health"}
PARAM = re.compile(r"\{(\w+)\}")
BODIES: dict[str, dict[str, Any]] = {"approve": {"actor": "t"}, "reject": {"actor": "t", "reason": "r"},
                                     "undo": {}, "access-check": {"sheet": "x" * 30},
                                     "": {"sheet": "x" * 30, "instruction": "i"}}


def routes() -> list[tuple[str, str]]:
    """Every (method, path) the app serves, from its OpenAPI document (the public contract;
    independent of how FastAPI nests included routers internally)."""
    out = []
    for path, ops in create_app().openapi()["paths"].items():
        if path.startswith(API_PREFIX) and path not in PUBLIC:
            out += [(m.upper(), path) for m in sorted(ops) if m in {"get", "post", "put", "patch", "delete"}]
    return out


def body_for(path: str) -> dict[str, Any]:
    return BODIES.get(path.rstrip("/").rsplit("/", 1)[-1], BODIES[""] if path.endswith("/sheets") else {})


@pytest.fixture(scope="module")
def pg_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    return start_postgres(tmp_path_factory.mktemp("pg_sec"))


@pytest.fixture
def env(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Fleet]]:
    monkeypatch.setenv("API_TOKEN", TOKEN)
    get_settings.cache_clear()
    fleet = make_fleet(pg_url)
    set_api_context(fleet.ctx)
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client, fleet
    set_api_context(None)
    set_context(None)
    get_settings.cache_clear()


def call(client: TestClient, method: str, path: str, auth: bool, ids: dict[str, Any]) -> Any:
    url = PARAM.sub(lambda m: str(ids.get(m.group(1), "x")), path)
    headers = {"Authorization": f"Bearer {TOKEN}"} if auth else {}
    return client.request(method, url, json=body_for(path) if method == "POST" else None, headers=headers)


def test_route_table_is_not_empty() -> None:
    paths = {p for _, p in routes()}
    assert len(routes()) >= 18
    assert {f"{API_PREFIX}/configs/{{config_id}}/approve", f"{API_PREFIX}/sheets/{{sheet_id}}/undo"} <= paths


def test_every_endpoint_requires_the_bearer_token(env: tuple[TestClient, Fleet]) -> None:
    client, fleet = env
    ids = {"sheet_id": fleet.pk[next(iter(fleet.pk))], "config_id": 1, "job_id": "j"}
    failures = []
    for method, path in routes():
        for auth_header in ({}, {"Authorization": "Bearer nope"}, {"Authorization": TOKEN}):
            url = PARAM.sub(lambda m: str(ids.get(m.group(1), "x")), path)
            r = client.request(method, url, json=body_for(path) if method == "POST" else None, headers=auth_header)
            if r.status_code != 401 or r.json().get("error", {}).get("code") != "unauthorized":
                failures.append(f"{method} {path} {auth_header or 'no header'} -> {r.status_code}")
    assert not failures, "\n".join(failures)
    assert fleet.sheets.calls == []  # nothing was read from Google without auth


def test_id_endpoints_refuse_unregistered_and_other_org_ids_before_any_google_call(
    env: tuple[TestClient, Fleet],
) -> None:
    client, fleet = env
    with fleet.factory() as s, s.begin():  # a sheet + config registered to ANOTHER org
        s.add(Org(id="org_other", org_id="org_other", name="other"))
        s.flush()
        other = Sheet(org_id="org_other", google_sheet_id="1OtherOrgSheetxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", status="ACTIVE")
        s.add(other)
        s.flush()
        raw = load_reference_raw()
        raw["sheet_id"] = other.google_sheet_id
        cfg = Config(org_id="org_other", sheet_id=other.id, version=1, body=raw, status="PENDING_APPROVAL")
        s.add(cfg)
        s.flush()
        other_ids = {"sheet_id": other.id, "config_id": cfg.id}
    id_routes = [(m, p) for m, p in routes() if "{sheet_id}" in p or "{config_id}" in p]
    assert len(id_routes) >= 13  # 5 config + 8 sheet routes today; new ones are picked up automatically
    fleet.sheets.calls.clear()
    failures = []
    for label, ids in (("missing", {"sheet_id": 999999, "config_id": 999999}), ("other org", other_ids)):
        for method, path in id_routes:
            r = call(client, method, path, True, ids)
            if r.status_code != 404 or r.json()["error"]["code"] != "not_found":
                failures.append(f"{label}: {method} {path} -> {r.status_code}")
    assert not failures, "\n".join(failures)
    assert fleet.sheets.calls == []  # B.9: no Google call for an id outside this org's registry
    with fleet.factory() as s:  # and nothing was changed on the other org's config
        assert s.get(Config, other_ids["config_id"]).status == "PENDING_APPROVAL"  # type: ignore[union-attr]

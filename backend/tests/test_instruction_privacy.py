"""B.6 addendum (CLAUDE.md): owner instruction text is treated like cell values.

It is persisted only in its designated column (`enrollments.instruction`, plus the agent's failure
text in `enrollments.failure`/`failures`), never in logs, RQ job payloads or meta (Redis), events,
error messages or metrics. Instructions quote data: "move rows for client X".

The onboarding job goes through a real (async) RQ queue and SimpleWorker on fakeredis, so the job
hash, its compressed payload, meta and registries are all inspected after the job runs.
"""

from __future__ import annotations

import json
import logging
import zlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from rq import Queue, SimpleWorker
from rq.job import Job
from sqlalchemy import select

from app.agent.onboarding import ModelPlan
from app.api.deps import set_api_context
from app.api.main import create_app
from app.core.settings import get_settings
from app.models import Enrollment, Event, LlmCall, Run
from app.workers.context import set_context
from tests.fake_sheets import seed
from tests.fixtures import reference_workbook
from tests.s2_harness import Fleet, make_fleet, start_postgres
from tests.test_onboarding import ScriptedLlm, call, compiled_reference, say

pytest.importorskip("pgserver")

TOKEN = "test-token-priv"
SID = "1PrivacyEnrollSheetxxxxxxxxxxxxxxxxxxxxxxxxx"
CLIENT = "ZEPHYR-QUILL-7731"  # stands in for a client name the owner typed
INSTRUCTION = f"Move the rows for client {CLIENT} to the bottom and sort the rest by STATUS"


@pytest.fixture(scope="module")
def pg_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    return start_postgres(tmp_path_factory.mktemp("pg_priv"))


@pytest.fixture
def env(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Fleet, TestClient, dict[str, Any], Queue]]:
    monkeypatch.setenv("API_TOKEN", TOKEN)
    get_settings.cache_clear()
    fleet = make_fleet(pg_url)
    fleet.sheets.docs[SID] = seed(reference_workbook.build())
    script: dict[str, Any] = {}
    fleet.ctx.llm_factory = lambda: ScriptedLlm({m: list(r) for m, r in script.items()})
    fleet.ctx.plan = ModelPlan("fake-mini", "fake-large", 1, 0, 6, True)
    queue = Queue("onboarding", connection=fleet.redis)  # async: the job really sits in Redis
    set_api_context(fleet.ctx, queue)
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield fleet, client, script, queue
    set_api_context(None)
    set_context(None)
    get_settings.cache_clear()


def redis_blobs(redis: Any) -> list[bytes]:
    """Every byte string stored in Redis, with RQ's zlib-compressed job payloads decompressed."""
    out: list[bytes] = []
    for key in redis.keys("*"):
        kind = redis.type(key).decode()
        if kind == "string":
            values = [redis.get(key)]
        elif kind == "hash":
            values = [v for pair in redis.hgetall(key).items() for v in pair]
        elif kind == "list":
            values = redis.lrange(key, 0, -1)
        elif kind == "set":
            values = list(redis.smembers(key))
        elif kind == "zset":
            values = [m for m, _ in redis.zrange(key, 0, -1, withscores=True)]
        else:
            values = []
        for v in [key, *values]:
            raw = v if isinstance(v, bytes) else str(v).encode()
            out.append(raw)
            try:
                out.append(zlib.decompress(raw))
            except zlib.error:
                pass
    return out


def enroll_and_work(fleet: Fleet, client: TestClient, queue: Queue) -> tuple[str, dict[str, Any]]:
    r = client.post("/api/v1/sheets", json={"sheet": SID, "instruction": INSTRUCTION},
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    blobs = redis_blobs(fleet.redis)
    assert any(b"run_onboarding" in b for b in blobs)  # the scan does see the decompressed job payload
    assert not any(CLIENT.encode() in b for b in blobs), "instruction queued in Redis"
    SimpleWorker([queue], connection=fleet.redis).work(burst=True, logging_level="DEBUG")
    job = client.get(f"/api/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    return job_id, job


def assert_only_in_enrollments(fleet: Fleet, job_id: str, caplog: pytest.LogCaptureFixture) -> None:
    rq_job = Job.fetch(job_id, connection=fleet.redis)
    with fleet.factory() as s:
        row = s.scalars(select(Enrollment)).one()
        assert row.instruction == INSTRUCTION and row.job_id == job_id  # the designated column
        assert list(rq_job.args) == [row.id] and rq_job.kwargs == {}  # ids only on the queue
        assert rq_job.description == f"onboarding:{row.id}"
        events = [json.dumps(e.payload) for e in s.scalars(select(Event))]
        runs = [f"{r.error} {r.trigger_type}" for r in s.scalars(select(Run))]
        calls = [json.dumps({c.name: str(getattr(x, c.name)) for c in LlmCall.__table__.columns})
                 for x in s.scalars(select(LlmCall))]
    for where, texts in (("events", events), ("runs", runs), ("llm_calls", calls)):
        assert not any(CLIENT in t for t in texts), f"instruction text persisted in {where}"
    assert not any(CLIENT.encode() in b for b in redis_blobs(fleet.redis)), "instruction text in Redis"
    logged = "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records)
    assert f"onboarding:{row.id}" in logged  # RQ logged the job (by its value-free description)
    assert CLIENT not in logged, "instruction text in logs"


def test_instruction_never_leaves_its_column_on_success(env: Any, caplog: pytest.LogCaptureFixture) -> None:
    fleet, client, script, queue = env
    script["fake-mini"] = [call("propose_config", {"config": compiled_reference()})]
    with caplog.at_level(logging.DEBUG):
        job_id, job = enroll_and_work(fleet, client, queue)
    assert job["state"] == "proposed" and job["config_id"]
    assert_only_in_enrollments(fleet, job_id, caplog)


def test_failure_text_quoting_the_instruction_stays_in_postgres(env: Any, caplog: pytest.LogCaptureFixture) -> None:
    fleet, client, script, queue = env
    script["fake-mini"] = [say(f"CANNOT: moving rows for client {CLIENT} needs a client column this sheet lacks.")]
    with caplog.at_level(logging.DEBUG):
        job_id, job = enroll_and_work(fleet, client, queue)
    assert job["state"] == "failed" and CLIENT in job["failure"]  # shown to the owner on the page
    assert_only_in_enrollments(fleet, job_id, caplog)
    with fleet.factory() as s:
        row = s.scalars(select(Enrollment)).one()
        assert row.state == "failed" and CLIENT in (row.failure or "")
    # the sheet's open flag reads the failure from the enrollment, not from an event payload
    detail = client.get(f"/api/v1/sheets/{job['sheet_id']}", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    [flag] = [f for f in detail["flags"] if f["kind"] == "onboarding_failed"]
    assert CLIENT in flag["message"]

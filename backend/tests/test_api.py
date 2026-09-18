"""HTTP API for the PATCH-003 pages (real Postgres, fake Sheets/Drive, fake Redis, scripted LLM).

Covers every endpoint a page needs, the pilot bearer auth, the JSON error envelope, CORS, and
B.6 on the new paths (preview payloads and error envelopes never persist or log cell values).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from rq import Queue
from sqlalchemy import func, select

from app.agent.onboarding import ModelPlan
from app.api.deps import set_api_context
from app.api.errors import ApiError
from app.api.main import create_app
from app.core.settings import get_settings
from app.models import Config, Event, LlmCall, Run, Sheet, SnapshotRow
from app.schemas.config import ConfigSpec
from app.services.registry import headers_of
from app.workers.context import set_context
from tests.fake_sheets import seed
from tests.fixtures import reference_workbook
from tests.s2_harness import SID_A, SID_B, Fleet, make_fleet, start_postgres
from tests.test_onboarding import ScriptedLlm, call, compiled_reference, say

pytest.importorskip("pgserver")

TOKEN = "test-token-123"
SID_NEW = "1NewSheetForEnrollxxxxxxxxxxxxxxxxxxxxxxxxxx"
SID_LOCKED = "1NotSharedWithUsxxxxxxxxxxxxxxxxxxxxxxxxxxx"
SECRETS = {"Acme", "Lathe", "Grinder", "Omega", "Sigma", "Beta"}


@pytest.fixture(scope="module")
def pg_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    return start_postgres(tmp_path_factory.mktemp("pg_api"))


class Harness:
    def __init__(self, fleet: Fleet, client: TestClient, llm_script: dict[str, Any]) -> None:
        self.fleet, self.client, self.llm_script = fleet, client, llm_script

    def get(self, path: str, **kw: Any) -> Any:
        return self.client.get(f"/api/v1{path}", headers={"Authorization": f"Bearer {TOKEN}"}, **kw)

    def post(self, path: str, json: dict[str, Any] | None = None) -> Any:
        return self.client.post(f"/api/v1{path}", json=json or {}, headers={"Authorization": f"Bearer {TOKEN}"})

    def pk(self, sid: str) -> int:
        return self.fleet.pk[sid]


@pytest.fixture
def api(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.setenv("API_TOKEN", TOKEN)
    get_settings.cache_clear()
    fleet = make_fleet(pg_url)
    fleet.poll()  # store the changes-feed start token, as the watcher does on first start
    fleet.sheets.docs[SID_NEW] = seed(reference_workbook.build())
    fleet.sheets.forbidden.add(SID_LOCKED)
    script: dict[str, Any] = {}
    fleet.ctx.llm_factory = lambda: ScriptedLlm({m: list(r) for m, r in script.items()})
    fleet.ctx.plan = ModelPlan("fake-mini", "fake-large", 2, 1, 6, True)
    fleet.ctx.service_account_email = "sa@test-project.iam.gserviceaccount.com"
    set_api_context(fleet.ctx, Queue("onboarding", connection=fleet.redis, is_async=False))
    app = create_app()

    @app.get("/api/v1/_boom")  # test-only route: an exception whose message carries cell data
    def boom() -> None:
        cell = reference_workbook.MACHINES_ROWS[0][1]  # a real cell value, only known at runtime
        raise ValueError(f"row 7 holds {cell} / {reference_workbook.MACHINES_ROWS[0][2]}")

    @app.get("/api/v1/_known")
    def known() -> None:
        raise ApiError(409, "a known, value-free conflict")

    with TestClient(app, raise_server_exceptions=False) as client:
        yield Harness(fleet, client, script)
    set_api_context(None)
    set_context(None)
    get_settings.cache_clear()


# ---- auth, envelope, CORS -----------------------------------------------------------------


def test_bearer_token_required_and_health_is_public(api: Harness) -> None:
    assert api.client.get("/api/v1/health").json() == {"status": "ok"}
    r = api.client.get("/api/v1/sheets")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    r = api.client.get("/api/v1/sheets", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401 and set(r.json()) == {"error"}
    assert api.get("/sheets").status_code == 200


def test_error_envelope_hides_exception_text_and_never_logs_it(api: Harness, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        r = api.get("/_boom")
    body = r.json()
    assert r.status_code == 500
    assert body == {"error": {"code": "internal_error", "message": "internal error",
                              "request_id": r.headers["X-Request-ID"]}}
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "api.unhandled" in logged and "ValueError" in logged
    assert not any(x in logged or x in r.text for x in SECRETS)  # B.6 on the envelope path
    assert "Traceback" not in r.text
    k = api.get("/_known").json()["error"]
    assert (k["code"], k["message"]) == ("conflict", "a known, value-free conflict")


def test_validation_errors_do_not_echo_submitted_input(api: Harness) -> None:
    r = api.post(f"/configs/1/reject", {"actor": "", "reason": "Acme Industries secret reason", "extra": 1})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "invalid_request" and err["details"]
    assert "Acme" not in r.text  # the submitted values are not reflected back


def test_cors_allows_the_vite_dev_origin(api: Harness) -> None:
    r = api.client.options("/api/v1/sheets", headers={"Origin": "http://localhost:5173",
                                                      "Access-Control-Request-Method": "GET",
                                                      "Access-Control-Request-Headers": "authorization"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "http://localhost:5173"
    r = api.client.options("/api/v1/sheets", headers={"Origin": "http://evil.example",
                                                      "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers


# ---- B2 enroll ------------------------------------------------------------------------------


def test_meta_gives_the_service_account_email(api: Harness) -> None:
    m = api.get("/meta").json()
    assert m["service_account_email"] == "sa@test-project.iam.gserviceaccount.com"
    assert any("Editor" in line for line in m["share_instructions"])


def test_access_check_accepts_url_or_id_and_reports_forbidden(api: Harness) -> None:
    ok = api.post("/sheets/access-check", {"sheet": f"https://docs.google.com/spreadsheets/d/{SID_NEW}/edit#gid=0"}).json()
    assert ok["ok"] and ok["access"] == "ok" and ok["sheet_id"] == SID_NEW and ok["title"] == "Order Tracker (test)"
    assert "MACHINES" in ok["tabs"]
    no = api.post("/sheets/access-check", {"sheet": SID_LOCKED}).json()
    assert no["access"] == "forbidden" and "sa@test-project" in no["message"]
    bad = api.post("/sheets/access-check", {"sheet": "https://docs.google.com/x#gid=551344265"})
    assert bad.status_code == 400 and "gid" in bad.json()["error"]["message"]


def test_enroll_job_reaches_pending_approval_with_progress_states(api: Harness) -> None:
    api.llm_script["fake-mini"] = [call("load_skill", {"names": ["config-basics"]}),
                                   call("propose_config", {"config": compiled_reference()})]
    r = api.post("/sheets", {"sheet": SID_NEW, "instruction": "sort by stage, colour rows, build SUMMARY"})
    assert r.status_code == 202
    job = api.get(f"/jobs/{r.json()['job_id']}").json()
    assert job["state"] == "proposed" and job["done"] and job["config_id"]
    cfgs = api.get("/configs", params={"status": "PENDING_APPROVAL"}).json()["items"]
    [item] = [c for c in cfgs if c["id"] == job["config_id"]]
    assert item["sheet"]["title"] == "Order Tracker (test)" and item["source"]["kind"] == "onboarding"
    assert item["has_consolidate"] is True


def test_nonsense_instruction_shows_a_readable_failure(api: Harness) -> None:
    api.llm_script["fake-mini"] = [say("CANNOT: booking flights is not something this workbook automation can do.")]
    r = api.post("/sheets", {"sheet": SID_NEW, "instruction": "book me a flight"}).json()
    job = api.get(f"/jobs/{r['job_id']}").json()
    assert job["state"] == "failed" and "booking flights" in job["failure"] and job["config_id"] is None
    assert api.get("/configs", params={"status": "PENDING_APPROVAL"}).json()["items"] == []


# ---- B1 approvals ---------------------------------------------------------------------------


def _pending(api: Harness) -> int:
    api.llm_script["fake-mini"] = [call("propose_config", {"config": compiled_reference()})]
    r = api.post("/sheets", {"sheet": SID_NEW, "instruction": "organize"}).json()
    return int(api.get(f"/jobs/{r['job_id']}").json()["config_id"])


def test_describe_preview_and_approve_with_owner_title(api: Harness, caplog: pytest.LogCaptureFixture) -> None:
    cid = _pending(api)
    d = api.get(f"/configs/{cid}/describe").json()
    assert d["live"] is True
    assert d["rules"][0]["headline"].startswith(
        "Keep every tab with a STATUS column (currently: MACHINES and SPARES) sorted by STATUS stage")
    assert d["summary"] == "Organizes 2 tabs with 3 rules — currently: MACHINES and SPARES."
    swatch = next(s for r in d["rules"] for x in r["details"] for s in x["segments"] if s["kind"] == "color")
    assert set(swatch) == {"kind", "name", "hex"}

    before = _counts(api)
    with caplog.at_level(logging.DEBUG):
        p = api.get(f"/configs/{cid}/dry-run/preview", params={"summary_title": "Q3 ORDERS"}).json()
    assert p["status"] == "OK" and p["ops"] > 0
    machines = next(t for t in p["tabs"] if t["tab"] == "MACHINES")
    assert machines["rows_to_reorder"] > 0 and 0 < len(machines["sample"]) <= 20
    row = machines["sample"][0]
    assert row["changed"] and len(row["before"]) == len(row["after"]) == len(machines["headers"])
    assert {"v", "font", "bg", "strike"} <= set(row["after"][0])
    summary = next(t for t in p["tabs"] if t["tab"] == "SUMMARY")
    assert summary["exists"] is False and summary["rows_added"] > 0
    # B.6: preview values are in the response only -- nothing persisted, nothing logged
    assert _counts(api) == before
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert not any(x in logged for x in SECRETS)
    assert any(x in str(p) for x in SECRETS)  # (they are on screen)

    a = api.post(f"/configs/{cid}/approve", {"actor": "Akash", "summary_title": "Q3 ORDERS"}).json()
    assert a["status"] == "ACTIVE" and a["approved_by"] == "Akash" and a["summary_title"] == "Q3 ORDERS"
    cons = [r for r in a["body"]["rules"] if r["action"] == "consolidate"][0]
    assert cons["presentation"]["title"] == "Q3 ORDERS"
    again = api.post(f"/configs/{cid}/approve", {"actor": "Akash"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "conflict"


def test_approval_queues_one_run_that_applies_the_previewed_changes(api: Harness) -> None:
    cid = _pending(api)
    preview = api.get(f"/configs/{cid}/dry-run/preview").json()
    assert preview["ops"] > 0
    a = api.post(f"/configs/{cid}/approve", {"actor": "Akash"}).json()
    assert a["first_run"]["state"] == "queued"
    assert api.fleet.batch_writes(SID_NEW) == 0  # approving itself writes nothing

    assert api.fleet.work() == 1  # exactly one job: the approval's run
    assert api.fleet.batch_writes(SID_NEW) == 1
    first = api.get(f"/configs/{cid}").json()["first_run"]
    assert first["state"] == "done" and first["status"] == "OK" and first["trigger"] == "approval"
    assert first["rows_affected"] > 0
    [run] = [r for r in api.get(f"/sheets/{a['sheet_id']}/runs").json()["items"] if r["run_id"] == first["run_id"]]
    assert run["config_version"] == a["version"] and run["undo"]["available"] is True  # undoable like any run

    # the watcher sees our own write and does not re-run it
    stats = api.fleet.tick(30)
    api.fleet.tick(30)
    assert api.fleet.batch_writes(SID_NEW) == 1, stats


def test_activation_without_a_shown_preview_claims_no_run(api: Harness) -> None:
    """register_sheet (the harness's hand-written configs) activates without the page's preview."""
    with api.fleet.factory() as s:
        cfg_id = s.scalar(select(Config.id).where(Config.sheet_id == api.pk(SID_A), Config.status == "ACTIVE"))
    assert api.get(f"/configs/{cfg_id}").json()["first_run"] is None


def test_approval_stands_when_the_run_queue_is_down(api: Harness, monkeypatch: pytest.MonkeyPatch,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    cid = _pending(api)

    def down(sheet_id: int, trigger: str = "change") -> str:
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr(api.fleet.ctx.queue, "enqueue_run", down)
    with caplog.at_level(logging.WARNING):
        a = api.post(f"/configs/{cid}/approve", {"actor": "Akash"})
    assert a.status_code == 200 and a.json()["status"] == "ACTIVE" and a.json()["first_run"] is None
    assert "approve.first_run_not_queued" in caplog.text


def test_reject_needs_a_reason_and_it_shows_in_history(api: Harness) -> None:
    cid = _pending(api)
    assert api.post(f"/configs/{cid}/reject", {"actor": "Akash", "reason": ""}).status_code == 422
    r = api.post(f"/configs/{cid}/reject", {"actor": "Akash", "reason": "wrong stage order"}).json()
    assert r["status"] == "REJECTED" and r["decision_reason"] == "wrong stage order"
    hist = api.get(f"/sheets/{r['sheet_id']}/configs").json()["items"]
    assert any(h["decision_reason"] == "wrong stage order" and h["rejected_by"] == "Akash" for h in hist)


def _counts(api: Harness) -> tuple[int, ...]:
    with api.fleet.factory() as s:
        return tuple(s.scalar(select(func.count()).select_from(m)) or 0
                     for m in (Run, Event, LlmCall, SnapshotRow, Config))


# ---- B3 sheet detail + B4 fleet --------------------------------------------------------------


def _organize(api: Harness, sid: str) -> None:
    api.fleet.drive.unrelated_change(sid)
    api.fleet.tick(30)
    api.fleet.tick(30)


def test_sheet_detail_runs_and_undo_from_the_api(api: Harness) -> None:
    before = api.fleet.ctx.adapter.read_grid(SID_A).workbook
    _organize(api, SID_A)
    pk = api.pk(SID_A)
    detail = api.get(f"/sheets/{pk}").json()
    assert detail["status"] == "ACTIVE" and detail["active_config"]["describe"]["rules"]
    assert detail["undo_last"]["available"] and detail["retention_days"] == 30
    [run] = [r for r in api.get(f"/sheets/{pk}/runs").json()["items"] if r["status"] == "OK"]
    assert run["rule_ids"] and run["undo"]["available"] and run["undo"]["snapshot_age_seconds"] >= 0

    prev = api.get(f"/sheets/{pk}/undo-preview").json()
    assert prev["run_id"] == run["run_id"] and "MACHINES" in prev["tabs"] and prev["available"]
    u = api.post(f"/sheets/{pk}/undo", {}).json()
    assert u["undone_run_id"] == run["run_id"] and u["ops"] > 0
    after = api.fleet.ctx.adapter.read_grid(SID_A).workbook
    assert after.tab("MACHINES").values == before.tab("MACHINES").values  # type: ignore[union-attr]
    runs = api.get(f"/sheets/{pk}/runs").json()["items"]
    original = next(r for r in runs if r["run_id"] == run["run_id"])
    assert not original["undo"]["available"] and "already undone" in original["undo"]["reason"]
    assert api.post(f"/sheets/{pk}/undo", {"run_id": run["run_id"]}).status_code == 409


def test_drift_flag_shows_what_changed_and_resume_after_fix(api: Harness) -> None:
    _organize(api, SID_A)
    pk = api.pk(SID_A)
    with api.fleet.factory() as s, s.begin():  # header snapshot as a proposal would store it
        sheet = s.get(Sheet, pk)
        cfg = s.get(Config, sheet.active_config_id)  # type: ignore[union-attr]
        cfg.governed_headers = headers_of(api.fleet.ctx.adapter.read_grid(SID_A).workbook,  # type: ignore[union-attr]
                                          ConfigSpec.model_validate(cfg.body))  # type: ignore[union-attr]
    status_col = reference_workbook.MACHINES_HEADERS.index("STATUS") + 1
    api.fleet.drive.user_edit(SID_A, "MACHINES", 2, status_col, "ORDER STATE")
    api.fleet.tick(30)
    api.fleet.tick(30)
    d = api.get(f"/sheets/{pk}").json()
    assert d["status"] == "PAUSED_DRIFT" and d["flags"][0]["kind"] == "drift" and d["flags"][0]["tabs"] == ["MACHINES"]
    drift = api.get(f"/sheets/{pk}/drift").json()
    tab = next(t for t in drift["tabs"] if t["tab"] == "MACHINES")
    assert tab["changes"] == [{"column": status_col, "expected": "STATUS", "current": "ORDER STATE"}]
    r = api.post(f"/sheets/{pk}/resume")
    assert r.status_code == 409 and r.json()["error"]["code"] == "still_drifted"
    api.fleet.drive.user_edit(SID_A, "MACHINES", 2, status_col, "STATUS")
    assert api.post(f"/sheets/{pk}/resume").json()["status"] == "ACTIVE"
    assert api.get(f"/sheets/{pk}").json()["flags"] == []


def test_pause_never_blocks_editing_and_resume(api: Harness) -> None:
    pk = api.pk(SID_B)
    p = api.post(f"/sheets/{pk}/pause").json()
    assert p["status"] == "PAUSED"
    locked = [s for s in api.fleet.sheets.docs[SID_B].sheets if s.protected]
    assert all(s.title == "SUMMARY" for s in locked)  # pausing added no protection (invariant 8)
    api.fleet.drive.user_edit(SID_B, "MACHINES", 3, 6, "Completed")
    api.fleet.tick(30)
    api.fleet.tick(30)
    assert api.get(f"/sheets/{pk}/runs").json()["items"] == []  # paused: nothing ran
    assert api.post(f"/sheets/{pk}/resume").json()["status"] == "ACTIVE"


def test_fleet_summary_and_sheet_list(api: Harness) -> None:
    _organize(api, SID_A)
    api.post(f"/sheets/{api.pk(SID_B)}/pause")
    f = api.get("/fleet/summary").json()
    assert f["active"] == 1 and f["paused"] == 1 and f["runs_24h"] >= 1 and f["error_rate_24h"] == 0.0
    items = {i["google_sheet_id"]: i for i in api.get("/sheets").json()["items"]}
    assert items[SID_A]["last_run"]["status"] == "OK" and items[SID_B]["status"] == "PAUSED"
    assert items[SID_A]["title"] == SID_A or items[SID_A]["title"]


def test_a_google_timeout_is_a_retryable_503_not_internal_error(api: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = _pending(api)

    def slow(_ref: str) -> Any:
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(api.fleet.ctx.adapter, "read_grid", slow)
    r = api.get(f"/configs/{cid}/dry-run/preview")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "google_unavailable" and "try again" in r.json()["error"]["message"]
    d = api.get(f"/configs/{cid}/describe").json()  # describe degrades to the static readback instead
    assert d["live"] is False and d["rules"]

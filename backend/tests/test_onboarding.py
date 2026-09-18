"""S3 onboarding agent with a scripted fake LLM (real Postgres, fake Sheets).

Exit criteria (SPEC-PATCH-001 S3), provider-independent part:
  * instruction -> valid config -> PENDING_APPROVAL -> dry-run equivalent to the hand-written one
  * validator errors are fed back; escalation after the primary model's attempts
  * a nonsense instruction ends in a human-readable failure, never an ACTIVE config
  * B.6/B.7: nothing sent to the LLM contains a raw cell value; llm_calls hold metadata only
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.sheets_adapter import SheetsAdapter
from app.agent.llm import LlmReply, ToolCall
from app.agent.masking import is_enum_column
from app.agent.onboarding import ModelPlan, OnboardingResult, onboard
from app.models import Config, Event, LlmCall, Profile, Sheet
from app.schemas.config import ConfigSpec
from app.services.conditions import EvalContext
from app.services.dry_run import dry_run
from app.services.grid import cell_text
from app.services.registry import DbRegistry, approve_config
from tests.conftest import load_reference_raw
from tests.fake_sheets import FakeSheetsService, seed
from tests.fixtures import reference_workbook
from tests.s2_harness import reset_db, start_postgres

pytest.importorskip("pgserver")

SID = "1OnboardSheetxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
ORG = "org_onb"
PLAN = ModelPlan("fake-mini", "fake-large", primary_attempts=2, escalation_attempts=1, max_turns_per_attempt=6)


class ScriptedLlm:
    """Replays scripted replies per model; records every message list it was sent."""

    provider = "fake"

    def __init__(self, script: dict[str, list[LlmReply]], models: set[str] | None = None) -> None:
        self.script = {m: list(r) for m, r in script.items()}
        self.models = models if models is not None else {"fake-mini", "fake-large"}
        self.sent: list[tuple[str, list[dict[str, Any]]]] = []

    def available_models(self) -> set[str]:
        return self.models

    def chat(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LlmReply:
        self.sent.append((model, copy.deepcopy(messages)))
        replies = self.script.get(model, [])
        if not replies:
            return LlmReply(content="I have nothing more to add.", tool_calls=(), input_tokens=10)
        return replies.pop(0)


_n = 0


def call(name: str, args: dict[str, Any]) -> LlmReply:
    global _n
    _n += 1
    return LlmReply(content=None, tool_calls=(ToolCall(f"call_{_n}", name, json.dumps(args)),),
                    input_tokens=1200, cached_tokens=1000, output_tokens=150, latency_ms=5)


def say(text: str) -> LlmReply:
    return LlmReply(content=text, tool_calls=(), input_tokens=900, output_tokens=40)


def compiled_reference() -> dict[str, Any]:
    """What a good model would propose for the reference instruction (hashes left to the server)."""
    raw = load_reference_raw()
    for k in ("sheet_id", "org_id", "config_version"):
        raw.pop(k)
    raw["schema_hashes"] = {t: "auto" for t in raw["schema_hashes"]}
    return raw


def invalid_config() -> dict[str, Any]:
    raw = compiled_reference()
    raw["rules"][0]["action"] = "shuffle"  # unknown action -> validator rejects
    return raw


@pytest.fixture(scope="module")
def pg_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    return start_postgres(tmp_path_factory.mktemp("pg_onb"))


@pytest.fixture
def env(pg_url: str) -> Iterator[tuple[sessionmaker[Session], SheetsAdapter]]:
    factory = sessionmaker(create_engine(pg_url, future=True), expire_on_commit=False)
    reset_db(factory)
    svc = FakeSheetsService({SID: seed(reference_workbook.build())})
    yield factory, SheetsAdapter(svc, DbRegistry(factory))


def run(env: tuple[sessionmaker[Session], SheetsAdapter], llm: ScriptedLlm,
        instruction: str = "Sort every tab by status stage then dispatch date; colour rows by status; "
                           "highlight overdue in-process rows; build a locked SUMMARY of all tabs.") -> OnboardingResult:
    factory, adapter = env
    return onboard(factory, adapter, llm, PLAN, ORG, SID, instruction)


def configs(factory: sessionmaker[Session]) -> list[Config]:
    with factory() as s:
        return list(s.scalars(select(Config).order_by(Config.id)))


def test_instruction_compiles_to_pending_config_equivalent_to_hand_written(env: Any) -> None:
    llm = ScriptedLlm({"fake-mini": [call("get_profile", {}), call("sample_rows", {"tab": "MACHINES", "n": 5}),
                                     call("propose_config", {"config": compiled_reference()})]})
    result = run(env, llm)
    assert result.status == "PENDING_APPROVAL" and result.model == "fake-mini"
    factory, adapter = env
    [row] = configs(factory)
    assert row.status == "PENDING_APPROVAL"
    with factory() as s:
        sheet = s.scalars(select(Sheet)).one()
        assert sheet.status == "PENDING"  # nothing live until a human approves
        assert s.scalars(select(Profile)).one().version == 1

    # exit criterion: dry-run summary equivalent to the hand-written reference config
    hand = load_reference_raw()
    hand["sheet_id"] = SID
    wb = adapter.read_grid(SID).workbook
    ctx = EvalContext("x", reference_workbook.TODAY)
    ours = dry_run(result.config, wb, ctx)  # type: ignore[arg-type]
    theirs = dry_run(ConfigSpec.model_validate(hand), wb, ctx)
    assert ours.model_dump() == theirs.model_dump()

    with factory() as s, s.begin():
        approve_config(s, row.id, "owner@test")
    with factory() as s:
        assert s.scalars(select(Sheet)).one().status == "ACTIVE"


def test_validator_errors_fed_back_then_escalation_succeeds(env: Any) -> None:
    llm = ScriptedLlm({
        "fake-mini": [call("propose_config", {"config": invalid_config()}),
                      call("propose_config", {"config": invalid_config()})],
        "fake-large": [call("propose_config", {"config": compiled_reference()})],
    })
    result = run(env, llm)
    assert result.status == "PENDING_APPROVAL" and result.model == "fake-large"
    assert len(result.failures) == 2
    # the escalation model saw the structured validator errors from the earlier attempts
    _, last_messages = llm.sent[-1]
    tool_texts = [m["content"] for m in last_messages if m["role"] == "tool"]
    assert any('"stage": "validate"' in t and "/rules/0" in t for t in tool_texts)
    factory, _ = env
    with factory() as s:
        models = [c.model for c in s.scalars(select(LlmCall).order_by(LlmCall.id))]
    assert models == ["fake-mini", "fake-mini", "fake-large"]


def test_nonsense_instruction_fails_readably_and_never_activates(env: Any) -> None:
    llm = ScriptedLlm({"fake-mini": [call("get_profile", {}),
                                     say("CANNOT: the instruction asks to book flights, which has nothing to do "
                                         "with organizing this workbook.")]})
    result = run(env, llm, instruction="book me a flight to the moon and pay with the invoice column")
    assert result.status == "FAILED"
    assert "book flights" in result.reason
    factory, _ = env
    assert configs(factory) == []
    with factory() as s:
        assert s.scalars(select(Sheet)).one().status == "PENDING"
        assert s.scalars(select(Event).where(Event.kind == "onboarding.needs_human")).one() is not None


def test_exhausted_attempts_open_a_human_ticket(env: Any) -> None:
    llm = ScriptedLlm({"fake-mini": [call("propose_config", {"config": invalid_config()})] * 2,
                       "fake-large": [call("propose_config", {"config": invalid_config()})]})
    result = run(env, llm)
    assert result.status == "FAILED" and len(result.failures) == 3
    assert "no valid config after 3 attempts" in result.reason
    factory, _ = env
    assert configs(factory) == []  # rejected proposals are never stored


def test_model_not_available_fails_before_any_call(env: Any) -> None:
    llm = ScriptedLlm({}, models={"something-else"})
    result = run(env, llm)
    assert result.status == "FAILED" and "not available" in result.reason
    assert llm.sent == []


def test_nothing_sent_to_the_llm_contains_raw_cell_values(env: Any) -> None:
    llm = ScriptedLlm({"fake-mini": [call("get_profile", {}),
                                     call("sample_rows", {"tab": "MACHINES", "n": 50}),
                                     call("sample_rows", {"tab": "SPARES", "n": 50}),
                                     call("propose_config", {"config": compiled_reference()})]})
    run(env, llm)
    wb = reference_workbook.build()
    secrets: set[str] = set()
    for tab in wb.tabs:
        headers = tab.row(2)
        for j, h in enumerate(headers):
            col = [r[j] for r in tab.values[2:] if j < len(r)]
            if is_enum_column(h, col):
                continue  # status/category labels are structure by design (B.7)
            secrets |= {cell_text(v).strip() for v in col if isinstance(v, str) and len(cell_text(v).strip()) >= 4}
    assert {"Acme", "Lathe", "Grinder", "Omega"} <= secrets
    sent = json.dumps([m for _, msgs in llm.sent for m in msgs])
    leaked = {x for x in secrets if x in sent}
    assert not leaked, f"sent to LLM: {sorted(leaked)}"


def test_llm_calls_rows_hold_metadata_only(env: Any) -> None:
    llm = ScriptedLlm({"fake-mini": [call("get_profile", {}), call("propose_config", {"config": compiled_reference()})]})
    run(env, llm)
    factory, _ = env
    with factory() as s:
        rows = list(s.scalars(select(LlmCall).order_by(LlmCall.id)))
    assert [r.tool_calls for r in rows] == ["get_profile", "propose_config"]
    assert rows[0].cached_tokens == 1000 and rows[0].provider == "fake"
    assert all(r.org_id == ORG and r.purpose == "onboarding" for r in rows)


def test_prompt_renders_and_worked_example_is_a_valid_config() -> None:
    from app.agent.onboarding import PROMPTS, system_prompt
    from app.services.validator import validate_config

    text = system_prompt()
    assert "{schema}" not in text and "{example}" not in text and '"$defs"' in text
    example = json.loads((PROMPTS / "example_config.json").read_text(encoding="utf-8"))
    example.update(sheet_id="x", org_id="o", schema_hashes={t: "0" * 64 for t in example["schema_hashes"]})
    result = validate_config(example)
    assert result.ok, [e.message for e in result.errors]
    # the example is deliberately a different domain than the reference workbook
    assert "STATUS" not in json.dumps(example["enums"])

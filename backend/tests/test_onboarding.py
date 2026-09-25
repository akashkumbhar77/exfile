"""The compile path with a scripted fake LLM (fake Sheets, nothing persisted).

  * instruction -> valid config -> proposed -> dry-run equivalent to the hand-written one
  * validator errors are fed back; escalation after the primary model's attempts
  * a nonsense instruction ends in a human-readable failure, and proposes nothing
  * B.6/B.7: nothing sent to the LLM contains a raw cell value; metering holds metadata only

Under PATCH-005 the compile keeps its by-products in a `MemoryRecorder` and writes nothing to
a database; the approval-gate tests moved with the managed tier into `v0-managed-tier`.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any

import pytest

from app.adapters.base import StaticRegistry
from app.agent.llm import LlmReply, ToolCall
from app.agent.masking import is_enum_column
from app.agent.onboarding import ModelPlan, OnboardingResult, onboard
from app.agent.recorder import MemoryRecorder
from app.schemas.config import ConfigSpec
from app.services.conditions import EvalContext
from app.services.dry_run import dry_run
from app.services.grid import cell_text
from tests.conftest import load_reference_raw
from tests.fake_sheets import FakeSheetsService, seed
from tests.fixtures import reference_workbook
from tests.live.sheets_adapter import SheetsAdapter

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


@pytest.fixture
def env() -> tuple[MemoryRecorder, SheetsAdapter]:
    recorder = MemoryRecorder()
    svc = FakeSheetsService({SID: seed(reference_workbook.build())})
    return recorder, SheetsAdapter(svc, StaticRegistry(frozenset({SID})))


def run(env: tuple[MemoryRecorder, SheetsAdapter], llm: ScriptedLlm,
        instruction: str = "Sort every tab by status stage then dispatch date; colour rows by status; "
                           "highlight overdue in-process rows; build a locked SUMMARY of all tabs.") -> OnboardingResult:
    recorder, adapter = env
    return onboard(recorder, adapter, llm, PLAN, ORG, SID, instruction)


def proposed(recorder: MemoryRecorder) -> list[ConfigSpec]:
    return list(recorder.configs.values())


def test_instruction_compiles_to_pending_config_equivalent_to_hand_written(env: Any) -> None:
    llm = ScriptedLlm({"fake-mini": [call("get_profile", {}), call("sample_rows", {"tab": "MACHINES", "n": 5}),
                                     call("propose_config", {"config": compiled_reference()})]})
    result = run(env, llm)
    assert result.status == "PENDING_APPROVAL" and result.model == "fake-mini"
    recorder, adapter = env
    assert len(proposed(recorder)) == 1
    assert recorder.profiles, "the sheet's structure was profiled"

    # exit criterion: dry-run summary equivalent to the hand-written reference config
    hand = load_reference_raw()
    hand["sheet_id"] = SID
    wb = adapter.read_grid(SID).workbook
    ctx = EvalContext("x", reference_workbook.TODAY)
    ours = dry_run(result.config, wb, ctx)  # type: ignore[arg-type]
    theirs = dry_run(ConfigSpec.model_validate(hand), wb, ctx)
    assert ours.model_dump() == theirs.model_dump()



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
    recorder, _ = env
    models = [c["model"] for c in recorder.llm_calls]
    assert models == ["fake-mini", "fake-mini", "fake-large"]


def test_nonsense_instruction_fails_readably_and_never_activates(env: Any) -> None:
    llm = ScriptedLlm({"fake-mini": [call("get_profile", {}),
                                     say("CANNOT: the instruction asks to book flights, which has nothing to do "
                                         "with organizing this workbook.")]})
    result = run(env, llm, instruction="book me a flight to the moon and pay with the invoice column")
    assert result.status == "FAILED"
    assert "book flights" in result.reason
    recorder, _ = env
    assert proposed(recorder) == []
    assert [k for k, _ in recorder.events] == ["onboarding.needs_human"]


def test_exhausted_attempts_open_a_human_ticket(env: Any) -> None:
    llm = ScriptedLlm({"fake-mini": [call("propose_config", {"config": invalid_config()})] * 2,
                       "fake-large": [call("propose_config", {"config": invalid_config()})]})
    result = run(env, llm)
    assert result.status == "FAILED" and len(result.failures) == 3
    assert "no valid config after 3 attempts" in result.reason
    recorder, _ = env
    assert proposed(recorder) == []  # a refused proposal is never kept


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
    recorder, _ = env
    rows = recorder.llm_calls
    assert [r["tool_calls"] for r in rows] == ["get_profile", "propose_config"]
    assert rows[0]["cached_tokens"] == 1000 and rows[0]["provider"] == "fake"
    assert all(r["purpose"] == "onboarding" for r in rows)
    # metering only: counts, latency, tool names - never a prompt or a cell value
    assert not any(isinstance(v, str) and len(v) > 300 for r in rows for v in r.values())


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


def test_ambiguous_header_pattern_is_rejected_with_the_exact_headers(env: Any) -> None:
    """Live finding: contains:STATUS also matched CONTROL PANEL STATUS and merged the columns."""
    bad = compiled_reference()
    bad["canonical_headers"].append({"canonical": "PANEL", "match": ["contains:PANEL", "contains:IN-HOUSE"]})
    bad["canonical_headers"] = [c if c["canonical"] != "STATUS" else {"canonical": "STATUS", "match": ["contains:S"]}
                                for c in bad["canonical_headers"]]
    llm = ScriptedLlm({"fake-mini": [call("propose_config", {"config": bad}),
                                     call("propose_config", {"config": compiled_reference()})]})
    result = run(env, llm)
    assert result.status == "PENDING_APPROVAL" and len(result.failures) == 1
    assert '"stage": "headers"' in result.failures[0]
    rejected = next(m["content"] for _, msgs in llm.sent for m in msgs if m["role"] == "tool" and '"headers"' in m["content"])
    assert "SR NO" in rejected or "STATUS" in rejected  # names the colliding headers


def test_consolidate_target_in_schema_hashes_is_dropped_by_the_server(env: Any) -> None:
    cfg = compiled_reference()
    cfg["schema_hashes"]["SUMMARY"] = "auto"
    llm = ScriptedLlm({"fake-mini": [call("propose_config", {"config": cfg})]})
    result = run(env, llm)
    assert result.status == "PENDING_APPROVAL" and result.config is not None
    assert "SUMMARY" not in result.config.schema_hashes
    reply = next(m["content"] for _, msgs in llm.sent for m in msgs if m["role"] == "tool") if len(llm.sent) > 1 else ""
    assert reply == "" or "removed consolidate target" in reply


def test_config_sent_as_json_string_is_accepted(env: Any) -> None:
    llm = ScriptedLlm({"fake-mini": [call("propose_config", {"config": json.dumps(compiled_reference())})]})
    assert run(env, llm).status == "PENDING_APPROVAL"


def test_validator_errors_carry_hints(env: Any) -> None:
    bad = compiled_reference()
    bad["rules"][1]["row_rules"][4]["when"]["all"][1] = {"date": {"column": "DISPATCH DATE", "before": "today"}}
    llm = ScriptedLlm({"fake-mini": [call("propose_config", {"config": bad}),
                                     call("propose_config", {"config": compiled_reference()})]})
    run(env, llm)
    rejected = next(m["content"] for _, msgs in llm.sent for m in msgs if m["role"] == "tool")
    assert "`date` is the column name as a string" in rejected


def test_skills_load_on_demand_with_their_schema_slice(env: Any) -> None:
    from app.agent.skills import catalogue, render

    assert {"config-basics", "triggers", "stages-and-sort", "conditions-and-format", "consolidate",
            "move-copy-cleanup"} <= set(catalogue())
    text = render(["conditions-and-format", "triggers"])
    assert '"DateCondition"' in text and '"OnEditTrigger"' in text and '"ConsolidateRule"' not in text
    llm = ScriptedLlm({"fake-mini": [call("load_skill", {"names": ["config-basics", "nope"]}),
                                     call("load_skill", {"names": ["config-basics", "consolidate"]}),
                                     call("propose_config", {"config": compiled_reference()})]})
    assert run(env, llm).status == "PENDING_APPROVAL"
    tool_msgs = [m["content"] for _, msgs in llm.sent[-1:] for m in msgs if m["role"] == "tool"]
    assert "unknown skill" in tool_msgs[0] and "available" in tool_msgs[0]
    assert "# Consolidate" in tool_msgs[1]
    system = llm.sent[0][1][0]["content"]
    assert "load_skill" in system and '"$defs"' not in system  # slim core prompt, no full schema


def test_skills_never_contain_the_reference_workbook_solution() -> None:
    """Prompt/skill examples must not leak the answer the S3 exit criterion checks against."""
    from app.agent.onboarding import PROMPTS
    from app.agent.skills import SKILLS_DIR

    texts = [p.read_text(encoding="utf-8") for p in [*SKILLS_DIR.glob("*.md"), *PROMPTS.glob("*")]]
    for term in ("DISPATCH", "FREEZE", "SOURCE SHEET", "Type of Work", "FCE4EC", "D4A017", "PROCESS", "MACHINES"):
        assert not any(term.lower() in t.lower() for t in texts), term


def test_renaming_an_existing_summary_is_reviewed_once(env: Any) -> None:
    """First proposal that would rename existing SUMMARY columns is sent back with the diff;
    proposing the same headers again is accepted (an intended rename still works)."""
    recorder, adapter = env
    first = ScriptedLlm({"fake-mini": [call("propose_config", {"config": compiled_reference()})]})
    assert run(env, first).status == "PENDING_APPROVAL"
    # materialize SUMMARY in the fake sheet so it "exists" for the next compile
    from app.services.planning import plan_grid
    from app.services.runner import RunEvent as _E
    plan = plan_grid(adapter.read_grid(SID), proposed(recorder)[0], _E("change"), source_ref=SID)
    # only SUMMARY's values matter here; the test adapter does not write presentation
    from app.adapters.base import SetBanding, SetColumnWidth, WriteNumberFormats
    adapter.write_ops(SID, [op for op in plan.ops
                            if not isinstance(op, SetBanding | SetColumnWidth | WriteNumberFormats)])

    renamed = compiled_reference()
    renamed["canonical_headers"] = [c if c["canonical"] != "FREEZE?" else {**c, "canonical": "FROZEN"}
                                    for c in renamed["canonical_headers"]]
    for rule in renamed["rules"]:
        for rr in rule.get("cell_rules", []):
            rr["column"] = "FROZEN"
        if "on_edit" in rule.get("trigger", {}):
            rule["trigger"]["on_edit"]["columns"] = ["STATUS", "DISPATCH DATE", "FROZEN"]
    llm = ScriptedLlm({"fake-mini": [call("propose_config", {"config": renamed}),
                                     call("propose_config", {"config": renamed})]})
    result = run(env, llm)
    assert result.status == "PENDING_APPROVAL" and len(result.failures) == 1
    assert '"stage": "review"' in result.failures[0] and "FREEZE?" in result.failures[0]

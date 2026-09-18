"""Onboarding agent (SPEC §6, SPEC-PATCH-001 S3): profile -> compile -> validate loop -> dry-run.

Plain tool-use loop (no framework). The agent's only write artifact is a config JSON
(invariant 2): on the first proposal that validates and dry-runs cleanly, the server stores it
as PENDING_APPROVAL and STOPS. A human approves it (CLI) before anything goes live (invariant 11).

Escalation: up to `primary_attempts` failed proposals on the primary model, then
`escalation_attempts` on the escalation model, then a human ticket (event
`onboarding.needs_human`) -- never an ACTIVE config.

Privacy (B.6/B.7): the model sees the prompt, JSON Schema, the profile (structure + status
labels), masked samples, validator errors and dry-run counts. `llm_calls` rows hold metadata only.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.base import Adapter, Grid
from app.agent.llm import LlmClient, LlmError, LlmReply, parse_args, tool_spec
from app.agent.profile import build_profile
from app.agent.tools import ToolError, sample_rows
from app.models import LlmCall, Profile
from app.schemas.config import ConfigSpec
from app.services.dry_run import DryRunReport
from app.services.live import prepare_run
from app.services.preflight import compute_schema_hash
from app.services.registry import ensure_org, ensure_pending_sheet, propose_config, record_event
from app.services.runner import RunEvent
from app.services.validator import validate_config

log = logging.getLogger("app.agent")

PROMPTS = Path(__file__).parent / "prompts"
PROMPT_VERSION = "onboarding-v1"
DECLINE_PREFIX = "CANNOT:"


@dataclass(frozen=True)
class ModelPlan:
    primary: str
    escalation: str
    primary_attempts: int = 2
    escalation_attempts: int = 1
    max_turns_per_attempt: int = 8


@dataclass
class OnboardingResult:
    status: str  # PENDING_APPROVAL | FAILED
    session_id: str
    reason: str = ""
    config_id: int | None = None
    config_version: int | None = None
    config: ConfigSpec | None = None
    report: DryRunReport | None = None
    failures: list[str] = field(default_factory=list)
    model: str | None = None


def system_prompt() -> str:
    schema = json.dumps(ConfigSpec.model_json_schema(by_alias=True), separators=(",", ":"))
    example = (PROMPTS / "example_config.json").read_text(encoding="utf-8")
    text = (PROMPTS / "onboarding.md").read_text(encoding="utf-8")
    return text.replace("{schema}", schema).replace("{example}", example)


TOOLS = [
    tool_spec("get_profile", "Structural profile of the workbook: tabs, header rows, headers, column types, "
              "fill rates and real status-label distributions.", {}, []),
    tool_spec("sample_rows", "Up to n MASKED data rows of one tab (headers and status labels are real).",
              {"tab": {"type": "string"}, "n": {"type": "integer", "minimum": 1, "maximum": 50}}, ["tab", "n"]),
    tool_spec("propose_config", "Validate and dry-run a complete config. Set schema_hashes values to \"auto\"; "
              "the server fills sheet_id, org_id, config_version and real hashes. Returns errors or a dry-run.",
              {"config": {"type": "object", "description": "the full config JSON"}}, ["config"]),
]
# The config object is free-form for the function schema; the validator is the real contract.
TOOLS[2]["function"]["parameters"]["properties"]["config"]["additionalProperties"] = True


class _Attempt(Exception):
    """Internal: one failed attempt (reason is shown back to the model and kept for the ticket)."""


@dataclass
class _Session:
    factory: sessionmaker[Session]
    adapter: Adapter
    llm: LlmClient
    org_id: str
    sheet_pk: int
    sheet_ref: str
    grid: Grid
    profile: dict[str, Any]
    session_id: str
    now: datetime | None
    messages: list[dict[str, Any]] = field(default_factory=list)
    accepted: OnboardingResult | None = None

    # -- tools --
    def run_tool(self, name: str, args: dict[str, Any], model: str) -> str:
        if name == "get_profile":
            return json.dumps(self.profile, ensure_ascii=False)
        if name == "sample_rows":
            return sample_rows(self.grid.workbook, str(args.get("tab", "")), int(args.get("n", 10)))
        if name == "propose_config":
            return self.propose(args.get("config"), model)
        raise ToolError(f"unknown tool {name!r}")

    def propose(self, raw: Any, model: str) -> str:
        if not isinstance(raw, dict):
            raise _Attempt("propose_config: `config` must be a JSON object")
        raw = dict(raw)
        raw.update(sheet_id=self.sheet_ref, org_id=self.org_id, config_version=1)
        hashes = raw.get("schema_hashes")
        if not isinstance(hashes, dict) or not hashes:
            raise _Attempt("schema_hashes must list the governed tabs, e.g. {\"TAB\": \"auto\"}")
        header_row = raw.get("header_row", 1)
        filled: dict[str, str] = {}
        tab_names = self.grid.workbook.names()  # structure only
        for tab_name in hashes:
            tab = self.grid.workbook.tab(str(tab_name))
            if tab is None or not isinstance(header_row, int):
                raise _Attempt(f"schema_hashes: tab {tab_name!r} does not exist; tabs are {tab_names}")
            filled[str(tab_name)] = compute_schema_hash(tab.row(header_row))
        raw["schema_hashes"] = filled

        result = validate_config(raw)
        if not result.ok or result.config is None:
            errors = [{"pointer": e.pointer, "code": e.code, "message": e.message} for e in result.errors]
            raise _Attempt(json.dumps({"ok": False, "stage": "validate", "errors": errors}))
        config = result.config
        prepared = prepare_run(self.adapter, self.sheet_ref, config, RunEvent("change"), now=self.now, grid=self.grid)
        report = prepared.report
        summary = {
            "status": report.status,
            "rules": [{"id": r.rule_id, "action": r.action, "rows_affected": r.rows_affected, "tabs": r.tabs,
                       "error": r.error, "blocked": r.blocked} for r in report.rules],
            "per_tab": {t: s.model_dump() for t, s in report.per_tab.items()},
            "warnings": report.warnings[:20],
        }
        if report.status != "OK":
            raise _Attempt(json.dumps({"ok": False, "stage": "dry_run", "dry_run": summary}))

        with self.factory() as s, s.begin():
            _, row = propose_config(s, self.org_id, config)
            record_event(s, _sheet(s, self.sheet_pk), "onboarding.proposed", {
                "session_id": self.session_id, "config_version": row.version, "model": model,
                "prompt_version": PROMPT_VERSION, "dry_run_status": report.status,
                "rules": [r.rule_id for r in report.rules],
            })
            config_id, version = row.id, row.version
        self.accepted = OnboardingResult("PENDING_APPROVAL", self.session_id, config_id=config_id,
                                         config_version=version, config=config, report=report, model=model)
        return json.dumps({"ok": True, "stored_as": "PENDING_APPROVAL", "dry_run": summary})

    # -- logging --
    def log_call(self, model: str, attempt: int, turn: int, reply: LlmReply | None, error: str | None) -> None:
        with self.factory() as s, s.begin():
            s.add(LlmCall(
                org_id=self.org_id, sheet_id=self.sheet_pk, session_id=self.session_id, purpose="onboarding",
                provider=self.llm.provider, model=model, attempt=attempt, turn=turn,
                input_tokens=reply.input_tokens if reply else 0, cached_tokens=reply.cached_tokens if reply else 0,
                output_tokens=reply.output_tokens if reply else 0, latency_ms=reply.latency_ms if reply else 0,
                tool_calls=",".join(t.name for t in reply.tool_calls)[:300] if reply else "",
                status="ERROR" if error else "OK", error=error,
            ))


def _sheet(s: Session, pk: int) -> Any:
    from app.models import Sheet

    return s.get(Sheet, pk)


def onboard(factory: sessionmaker[Session], adapter: Adapter, llm: LlmClient, plan: ModelPlan, org_id: str,
            sheet_ref: str, instruction: str, now: datetime | None = None) -> OnboardingResult:
    session_id = "onb_" + uuid.uuid4().hex[:12]
    if not instruction.strip():
        return OnboardingResult("FAILED", session_id, reason="the instruction is empty")

    with factory() as s, s.begin():
        ensure_org(s, org_id)
        sheet_pk = ensure_pending_sheet(s, org_id, sheet_ref).id  # B.9: registered before we read it
    grid = adapter.read_grid(sheet_ref)
    profile = build_profile(grid.workbook, grid.timezone)
    with factory() as s, s.begin():
        version = (s.scalar(select(Profile.version).where(Profile.sheet_id == sheet_pk)
                            .order_by(Profile.version.desc())) or 0) + 1
        s.add(Profile(org_id=org_id, sheet_id=sheet_pk, version=version, body=profile))

    try:
        available = llm.available_models()
    except Exception as exc:
        return _fail(factory, sheet_pk, session_id, f"could not reach the LLM provider: {type(exc).__name__}", [])
    missing = [m for m in (plan.primary, plan.escalation) if m not in available]
    if missing:
        return _fail(factory, sheet_pk, session_id,
                     f"model(s) {missing} are not available to this API key; set LLM_PRIMARY_MODEL / "
                     "LLM_ESCALATION_MODEL", [])

    sess = _Session(factory, adapter, llm, org_id, sheet_pk, sheet_ref, grid, profile, session_id, now)
    sess.messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "system", "content": "Workbook profile:\n" + json.dumps(profile, ensure_ascii=False)},
        {"role": "user", "content": instruction.strip()},
    ]
    failures: list[str] = []
    attempt = 0
    for model, allowed in ((plan.primary, plan.primary_attempts), (plan.escalation, plan.escalation_attempts)):
        for _ in range(allowed):
            attempt += 1
            outcome = _run_attempt(sess, model, attempt, plan.max_turns_per_attempt)
            if sess.accepted is not None:
                log.info("onboarding.proposed session=%s sheet=%s model=%s attempts=%d",
                         session_id, sheet_ref, model, attempt)
                sess.accepted.failures = failures
                return sess.accepted
            if outcome.startswith(DECLINE_PREFIX):
                return _fail(factory, sheet_pk, session_id, outcome[len(DECLINE_PREFIX):].strip()[:500], failures)
            failures.append(f"attempt {attempt} ({model}): {outcome[:300]}")
    return _fail(factory, sheet_pk, session_id,
                 f"no valid config after {attempt} attempts; last problem: {failures[-1] if failures else 'none'}",
                 failures)


def _run_attempt(sess: _Session, model: str, attempt: int, max_turns: int) -> str:
    """Run turns until the model proposes (accepted or rejected), declines, or runs out of turns.
    Returns a failure description ('' never: acceptance is signalled via sess.accepted)."""
    for turn in range(1, max_turns + 1):
        try:
            reply = sess.llm.chat(model, sess.messages, TOOLS)
        except LlmError as exc:
            sess.log_call(model, attempt, turn, None, str(exc))
            return f"LLM request failed: {exc}"
        sess.log_call(model, attempt, turn, reply, None)
        sess.messages.append(reply.as_message())

        if not reply.tool_calls:
            text = (reply.content or "").strip()
            if text.upper().startswith(DECLINE_PREFIX):
                return DECLINE_PREFIX + text[len(DECLINE_PREFIX):]
            sess.messages.append({"role": "user", "content":
                                  "No config has been accepted yet. Call propose_config with a complete config, "
                                  f"or reply starting with '{DECLINE_PREFIX}' and the reason it cannot be done."})
            return "the model replied without proposing a config"

        rejected: str | None = None
        for call in reply.tool_calls:
            try:
                content = sess.run_tool(call.name, parse_args(call), model)
            except _Attempt as exc:
                content, rejected = str(exc), str(exc)
            except (ToolError, LlmError) as exc:
                content = json.dumps({"ok": False, "error": str(exc)})
            sess.messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
            if sess.accepted is not None:
                return ""
        if rejected is not None:
            return f"proposal rejected: {rejected}"
    return f"no accepted proposal within {max_turns} turns"


def _fail(factory: sessionmaker[Session], sheet_pk: int, session_id: str, reason: str,
          failures: list[str]) -> OnboardingResult:
    with factory() as s, s.begin():
        record_event(s, _sheet(s, sheet_pk), "onboarding.needs_human", {
            "session_id": session_id, "reason": reason[:500], "failed_attempts": len(failures),
        })
    log.warning("onboarding.failed session=%s attempts=%d", session_id, len(failures))
    return OnboardingResult("FAILED", session_id, reason=reason, failures=failures)

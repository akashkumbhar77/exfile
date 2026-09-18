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
from app.agent.skills import catalogue_text, render
from app.agent.tools import ToolError, sample_rows
from app.models import LlmCall, Profile
from app.schemas.config import ConfigSpec
from app.services.dry_run import DryRunReport
from app.services.grid import cell_text
from app.services.live import prepare_run
from app.services.preflight import compute_schema_hash
from app.services.registry import ensure_org, ensure_pending_sheet, headers_of, propose_config, record_event
from app.services.runner import RunEvent
from app.services.validator import validate_config

log = logging.getLogger("app.agent")

PROMPTS = Path(__file__).parent / "prompts"
PROMPT_VERSION = "onboarding-v3"
SKILLS_PROMPT_VERSION = "onboarding-v4-skills"
DECLINE_PREFIX = "CANNOT:"


@dataclass(frozen=True)
class ModelPlan:
    primary: str
    escalation: str
    primary_attempts: int = 2
    escalation_attempts: int = 1
    max_turns_per_attempt: int = 8
    use_skills: bool = True  # v4: slim prompt + load_skill (see app/agent/skills.py)


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


def system_prompt(use_skills: bool = False) -> str:
    if use_skills:
        text = (PROMPTS / "onboarding_skills.md").read_text(encoding="utf-8")
        return text.replace("{catalogue}", catalogue_text())
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
LOAD_SKILL = tool_spec(
    "load_skill", "Load reference skills (syntax, pitfalls, JSON Schema) for parts of the config. "
    "Request all you need in one call.",
    {"names": {"type": "array", "items": {"type": "string"}, "minItems": 1}}, ["names"])


def tools_for(use_skills: bool) -> list[dict[str, Any]]:
    return [*TOOLS, LOAD_SKILL] if use_skills else TOOLS


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
    use_skills: bool = False
    reviewed_targets: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def prompt_version(self) -> str:
        return SKILLS_PROMPT_VERSION if self.use_skills else PROMPT_VERSION

    # -- tools --
    def run_tool(self, name: str, args: dict[str, Any], model: str) -> str:
        if name == "get_profile":
            return json.dumps(self.profile, ensure_ascii=False)
        if name == "sample_rows":
            return sample_rows(self.grid.workbook, str(args.get("tab", "")), int(args.get("n", 10)))
        if name == "propose_config":
            return self.propose(args.get("config"), model)
        if name == "load_skill" and self.use_skills:
            names = args.get("names")
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise ToolError("load_skill: `names` must be a list of skill names")
            try:
                return render(names)
            except KeyError as exc:
                raise ToolError(str(exc)) from exc
        raise ToolError(f"unknown tool {name!r}")

    def propose(self, raw: Any, model: str) -> str:
        if isinstance(raw, str):  # some models send the object as a JSON string
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise _Attempt(f"propose_config: `config` is not valid JSON ({exc.msg})") from exc
        if not isinstance(raw, dict):
            raise _Attempt("propose_config: `config` must be a JSON object")
        raw = dict(raw)
        raw.update(sheet_id=self.sheet_ref, org_id=self.org_id, config_version=1)
        hashes = raw.get("schema_hashes")
        if not isinstance(hashes, dict) or not hashes:
            raise _Attempt("schema_hashes must list the governed tabs, e.g. {\"TAB\": \"auto\"}")
        notes: list[str] = []
        # schema_hashes is server-owned: consolidate targets are generated, never governed
        targets = {str(r.get("target_tab")) for r in raw.get("rules", []) if isinstance(r, dict)
                   and r.get("action") == "consolidate" and r.get("target_tab")}
        for t in sorted(targets & set(hashes)):
            notes.append(f"removed consolidate target {t!r} from schema_hashes (targets are never governed)")
        hashes = {k: v for k, v in hashes.items() if k not in targets}
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
            errors = [{"pointer": e.pointer, "code": e.code, "message": e.message, **_hint(e.pointer, e.code)}
                      for e in result.errors]
            raise _Attempt(json.dumps({"ok": False, "stage": "validate", "errors": errors, "notes": notes}))
        config = result.config
        ambiguous = ambiguous_header_patterns(config, self.grid)
        if ambiguous:
            raise _Attempt(json.dumps({"ok": False, "stage": "headers", "errors": ambiguous, "notes": notes}))
        prepared = prepare_run(self.adapter, self.sheet_ref, config, RunEvent("change"), now=self.now, grid=self.grid)
        report = prepared.report
        targets_info = target_headers(config, self.grid, prepared.result.workbook)
        summary = {
            "status": report.status,
            "rules": [{"id": r.rule_id, "action": r.action, "rows_affected": r.rows_affected, "tabs": r.tabs,
                       "error": r.error, "blocked": r.blocked} for r in report.rules],
            "per_tab": {t: s.model_dump() for t, s in report.per_tab.items()},
            "warnings": report.warnings[:20],
            "consolidate_targets": targets_info,
            "notes": notes,
        }
        if report.status != "OK":
            raise _Attempt(json.dumps({"ok": False, "stage": "dry_run", "dry_run": summary}))
        renames = self.review_target_renames(targets_info)
        if renames:
            raise _Attempt(json.dumps({"ok": False, "stage": "review", "renamed_columns": renames,
                                       "hint": "an existing summary tab would change its column names. Reuse "
                                               "its existing spellings as canonical names; if the instruction "
                                               "really asks for these names, propose the same config again."}))

        with self.factory() as s, s.begin():
            _, row = propose_config(s, self.org_id, config, governed_headers=headers_of(self.grid.workbook, config),
                                    source={"kind": "onboarding", "session_id": self.session_id, "model": model,
                                            "prompt_version": self.prompt_version})
            record_event(s, _sheet(s, self.sheet_pk), "onboarding.proposed", {
                "session_id": self.session_id, "config_version": row.version, "model": model,
                "prompt_version": self.prompt_version, "dry_run_status": report.status,
                "rules": [r.rule_id for r in report.rules],
            })
            config_id, version = row.id, row.version
        self.accepted = OnboardingResult("PENDING_APPROVAL", self.session_id, config_id=config_id,
                                         config_version=version, config=config, report=report, model=model)
        return json.dumps({"ok": True, "stored_as": "PENDING_APPROVAL", "dry_run": summary})

    def review_target_renames(self, targets: dict[str, Any]) -> list[dict[str, Any]]:
        """Existing consolidate targets whose headers would change. Each distinct header set is sent
        back once; proposing the same headers again accepts them (the model saw the diff)."""
        out: list[dict[str, Any]] = []
        for target, info in targets.items():
            old, new = info.get("existing_headers"), info.get("resulting_headers") or []
            if not old:
                continue  # a new target tab: nothing to preserve
            key = tuple(new)
            if [h for h in new if h] == [h for h in old if h] or self.reviewed_targets.get(target) == key:
                continue
            self.reviewed_targets[target] = key
            gone = [h for h in old if h and h not in new]
            added = [h for h in new if h and h not in old]
            out.append({"target_tab": target, "existing_columns_lost": gone, "new_columns": added})
        return out

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


_HINTS: list[tuple[str, str, str]] = [
    # (pointer suffix, code substring, hint)
    ("/date", "string", "`date` is the column name as a string: {\"date\": \"DISPATCH DATE\", \"before\": \"today\"}"),
    ("/equals", "", "`equals` takes a string: {\"column\": \"FREEZE?\", \"equals\": \"YES\"}"),
    ("/contains", "", "`contains` takes a string"),
    ("/target_tab", "target_is_governed", "remove the target tab from schema_hashes"),
    ("", "target_in_sources", "never list a consolidate target in tabs/sources; use sort_like/format_like"),
]


def _hint(pointer: str, code: str) -> dict[str, str]:
    for suffix, code_part, hint in _HINTS:
        if (not suffix or pointer.endswith(suffix)) and code_part in code:
            return {"hint": hint}
    return {}


def ambiguous_header_patterns(config: ConfigSpec, grid: Grid) -> list[dict[str, Any]]:
    """A canonical pattern that matches two different headers on one governed tab would merge those
    columns (e.g. contains:STATUS also matching CONTROL PANEL STATUS). Reject with the exact headers."""
    from app.services.headers import match_expr

    problems: list[dict[str, Any]] = []
    for tab_name in config.schema_hashes:
        tab = grid.workbook.tab(tab_name)
        if tab is None:
            continue
        headers = [cell_text(h).strip() for h in tab.row(config.header_row) if cell_text(h).strip()]
        for ch in config.canonical_headers:
            hits = [h for h in headers if any(match_expr(m, h) for m in ch.match)]
            if len(hits) > 1:
                problems.append({
                    "tab": tab_name, "canonical": ch.canonical, "patterns": list(ch.match), "matches": hits,
                    "hint": "this would merge different columns; use equals: patterns or a more specific fragment",
                })
    return problems


def target_headers(config: ConfigSpec, grid: Grid, projected: Any) -> dict[str, Any]:
    """Headers the consolidate targets will have, next to the existing target's headers (structure only)."""
    out: dict[str, Any] = {}
    for rule in config.rules:
        if rule.action != "consolidate":
            continue
        target = getattr(rule, "target_tab")
        new = projected.tab(target)
        old = grid.workbook.tab(target)
        out[target] = {
            "resulting_headers": [cell_text(h).strip() for h in new.row(config.header_row)] if new else [],
            "existing_headers": [cell_text(h).strip() for h in old.row(config.header_row)] if old else None,
        }
    return out


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

    sess = _Session(factory, adapter, llm, org_id, sheet_pk, sheet_ref, grid, profile, session_id, now,
                    use_skills=plan.use_skills)
    sess.messages = [
        {"role": "system", "content": system_prompt(plan.use_skills)},
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
            reply = sess.llm.chat(model, sess.messages, tools_for(sess.use_skills))
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

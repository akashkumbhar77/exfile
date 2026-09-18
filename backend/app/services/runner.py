"""plan_run — the one path from (config, workbook, event) to rule plans.

Used by BOTH dry-run and execution. Order of operations:
  1. pre-flight header hashes; any drift -> PAUSED_DRIFT, nothing evaluated
  2. select rules for the event (+ their `after:` chains, parents first)
  3. evaluate each rule against the projection left by the previous ones
  4. mark plans that exceed guards as blocked
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from app.schemas.config import (
    GUARDED_ACTIONS,
    AfterTrigger,
    ConfigSpec,
    ConsolidateRule,
    DebouncedTrigger,
    OnEditTrigger,
    Rule,
    ScheduleTrigger,
)
from app.services.conditions import EvalContext
from app.services.grid import Workbook
from app.services.headers import build_view, canon_key
from app.services.preflight import Drift, preflight
from app.services.rules import RulePlan, apply_plan, evaluate_rule
from app.services.rules.base import config_targets
from app.services.rules.tabs import resolve_tabs

EventKind = Literal["edit", "debounced", "schedule", "manual"]
RunStatus = Literal["OK", "PAUSED_DRIFT", "BLOCKED", "ERROR"]


@dataclass(frozen=True)
class RunEvent:
    kind: EventKind
    tab: str | None = None  # edit: edited tab
    columns: tuple[int, ...] = ()  # edit: 1-based edited columns
    rule_ids: tuple[str, ...] = ()  # schedule/manual: restrict to these roots (empty = all eligible)

    @staticmethod
    def manual(*rule_ids: str) -> RunEvent:
        return RunEvent("manual", rule_ids=rule_ids)


@dataclass
class RunPlan:
    status: RunStatus
    event: RunEvent
    plans: list[RulePlan] = field(default_factory=list)
    projected: Workbook | None = None
    drift: list[Drift] = field(default_factory=list)
    dirty: list[str] = field(default_factory=list)  # debounced rules to mark dirty (edit events)
    warnings: list[str] = field(default_factory=list)


def _children(config: ConfigSpec) -> dict[str, list[Rule]]:
    out: dict[str, list[Rule]] = {}
    for r in config.rules:
        if isinstance(r.trigger, AfterTrigger):
            out.setdefault(r.trigger.after, []).append(r)
    return out


def select_rules(
    config: ConfigSpec, workbook: Workbook, event: RunEvent
) -> tuple[list[tuple[Rule, list[str] | None]], list[str]]:
    """Returns ([(rule, scope)], dirty_debounced_rule_ids). Children follow their parent."""
    roots: list[tuple[Rule, list[str] | None]] = []
    dirty: list[str] = []

    if event.kind == "edit":
        tab = workbook.tab(event.tab or "")
        if tab is None or tab.name not in config.schema_hashes or tab.name in config_targets(config):
            return [], []
        view = build_view(tab, config)
        by_index = dict(view.all_columns)  # any column mapping to a canonical name counts
        edited = {by_index[c - 1] for c in event.columns if (c - 1) in by_index}
        for r in config.rules:
            selector = r.sources if isinstance(r, ConsolidateRule) else r.tabs
            if not isinstance(r.trigger, (OnEditTrigger, DebouncedTrigger)):
                continue
            if tab.name not in resolve_tabs(selector, workbook, config):
                continue
            if isinstance(r.trigger, DebouncedTrigger):
                dirty.append(r.id)
                continue
            wanted = {canon_key(c) for c in r.trigger.on_edit.columns}
            if wanted and not (wanted & edited):
                continue
            # consolidate always rebuilds from every source; other rules touch only the edited tab
            roots.append((r, None if isinstance(r, ConsolidateRule) else [tab.name]))
    else:
        wanted_kind = {"debounced": DebouncedTrigger, "schedule": ScheduleTrigger}.get(event.kind)
        for r in config.rules:
            if isinstance(r.trigger, AfterTrigger):
                continue
            if event.rule_ids and r.id not in event.rule_ids:
                continue
            if wanted_kind is not None and not isinstance(r.trigger, wanted_kind):
                continue
            roots.append((r, None))

    children = _children(config)
    ordered: list[tuple[Rule, list[str] | None]] = []

    def fire(rule: Rule, scope: list[str] | None) -> None:
        ordered.append((rule, scope))
        for child in children.get(rule.id, []):
            fire(child, scope)

    for rule, scope in roots:
        fire(rule, scope)
    return ordered, dirty


def plan_run(config: ConfigSpec, workbook: Workbook, event: RunEvent, ctx: EvalContext) -> RunPlan:
    pre = preflight(config, workbook)
    if not pre.ok:
        return RunPlan("PAUSED_DRIFT", event, drift=pre.drift)

    selected, dirty = select_rules(config, workbook, event)
    ctx = replace(ctx, extents={t.name: t.last_content_row() for t in workbook.tabs})
    run = RunPlan("OK", event, dirty=dirty)
    working = workbook
    guarded_rows = 0
    for rule, scope in selected:
        plan = evaluate_rule(rule, working, scope, config, ctx)
        if plan.error is None and rule.action in GUARDED_ACTIONS:
            guarded_rows += plan.rows_affected
            if guarded_rows > config.guards.max_rows_per_run:
                plan.blocked = (
                    f"max_rows_per_run exceeded ({guarded_rows} > {config.guards.max_rows_per_run})"
                )
        if plan.error is not None and run.status == "OK":
            run.status = "ERROR"
        elif plan.blocked is not None and run.status == "OK":
            run.status = "BLOCKED"
        run.plans.append(plan)
        working = apply_plan(working, plan)
    run.projected = working
    return run

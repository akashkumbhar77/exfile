"""Rule evaluator: the single code path shared by dry-run and execution."""

from __future__ import annotations

from app.schemas.config import (
    ClearRule,
    ConfigSpec,
    ConsolidateRule,
    CopyRule,
    DedupeRule,
    FormatRule,
    MoveRule,
    Rule,
    SortRule,
    ValidateRule,
)
from app.services.conditions import EvalContext
from app.services.grid import Workbook
from app.services.rules.base import RulePlan, TabChange, TabStats, apply_plan
from app.services.rules.cleanup import evaluate_clear, evaluate_dedupe, evaluate_validate
from app.services.rules.consolidate import evaluate_consolidate
from app.services.rules.format import evaluate_format
from app.services.rules.sort import evaluate_sort
from app.services.rules.transfer import evaluate_transfer

__all__ = ["RulePlan", "TabChange", "TabStats", "apply_plan", "evaluate_rule"]


def evaluate_rule(
    rule: Rule, workbook: Workbook, scope: list[str] | None, config: ConfigSpec, ctx: EvalContext
) -> RulePlan:
    """Pure: compute what `rule` would do to `workbook`. `scope` limits the tabs considered
    (None = every tab the rule selects)."""
    match rule:
        case SortRule():
            return evaluate_sort(rule, workbook, scope, config, ctx)
        case FormatRule():
            return evaluate_format(rule, workbook, scope, config, ctx)
        case ConsolidateRule():
            return evaluate_consolidate(rule, workbook, scope, config, ctx)
        case MoveRule() | CopyRule():
            return evaluate_transfer(rule, workbook, scope, config, ctx)
        case DedupeRule():
            return evaluate_dedupe(rule, workbook, scope, config, ctx)
        case ClearRule():
            return evaluate_clear(rule, workbook, scope, config, ctx)
        case ValidateRule():
            return evaluate_validate(rule, workbook, scope, config, ctx)
    raise TypeError(f"unsupported rule {type(rule).__name__}")

"""dedupe / clear / validate — per-tab row maintenance actions."""

from __future__ import annotations

from app.schemas.config import ClearRule, ConfigSpec, DedupeRule, ValidateRule
from app.services.conditions import EvalContext, evaluate
from app.services.grid import DataValidation, Workbook, is_empty, norm_text, row_is_empty
from app.services.headers import build_view
from app.services.rules.base import RulePlan, TabChange, TabStats
from app.services.rules.tabs import resolve_tabs


def evaluate_dedupe(
    rule: DedupeRule, workbook: Workbook, scope: list[str] | None, config: ConfigSpec, ctx: EvalContext
) -> RulePlan:
    """Remove rows whose key duplicates another candidate row. Rows with an all-empty key,
    held rows and empty rows are never removed nor used as the kept original."""
    plan = RulePlan(rule.id, rule.action)
    for name in resolve_tabs(rule.tabs, workbook, config, warnings=plan.warnings):
        if scope is not None and name not in scope:
            continue
        before = workbook.tab(name)
        assert before is not None
        view = build_view(before, config)
        idxs = [view.col(c) for c in rule.key_columns]
        if any(i is None for i in idxs):
            plan.warnings.append(f"tab {name!r}: missing dedupe key column; tab skipped")
            continue
        candidates: list[tuple[int, tuple[str, ...]]] = []
        for offset, row in enumerate(view.data_rows()):
            if row_is_empty(row) or view.is_held(row):
                continue
            key = tuple(norm_text(row[i]) for i in idxs if i is not None)
            if any(key):
                candidates.append((view.data_start_row + offset, key))
        if rule.keep == "last":
            candidates.reverse()
        seen: set[tuple[str, ...]] = set()
        drop: set[int] = set()
        for row_no, key in candidates:
            if key in seen:
                drop.add(row_no)
            seen.add(key)
        if drop:
            after = before.clone()
            after.delete_rows(drop)
            plan.changes.append(TabChange(name, before, after, TabStats(rows_removed=len(drop))))
    return plan


def evaluate_clear(
    rule: ClearRule, workbook: Workbook, scope: list[str] | None, config: ConfigSpec, ctx: EvalContext
) -> RulePlan:
    """Blank the listed columns in matching rows (rows themselves are kept)."""
    plan = RulePlan(rule.id, rule.action)
    for name in resolve_tabs(rule.tabs, workbook, config, warnings=plan.warnings):
        if scope is not None and name not in scope:
            continue
        before = workbook.tab(name)
        assert before is not None
        view = build_view(before, config)
        idxs: list[int] = []
        for c in rule.columns:
            i = view.col(c)
            if i is None:
                plan.warnings.append(f"tab {name!r}: clear column {c!r} missing; skipped")
            else:
                idxs.append(i)
        after = before.clone()
        cleared = 0
        for offset, row in enumerate(view.data_rows()):
            if row_is_empty(row) or view.is_held(row):
                continue
            if not evaluate(rule.when, row, view, config, ctx):
                continue
            target = after.values[view.data_start_row - 1 + offset]
            if any(not is_empty(target[i]) for i in idxs):
                for i in idxs:
                    target[i] = None
                cleared += 1
        if cleared:
            plan.changes.append(TabChange(name, before, after, TabStats(rows_cleared=cleared)))
    return plan


def evaluate_validate(
    rule: ValidateRule, workbook: Workbook, scope: list[str] | None, config: ConfigSpec, ctx: EvalContext
) -> RulePlan:
    """Attach a dropdown to the column's data cells (held rows excluded). Existing values are
    never changed; values outside the list are reported as warnings."""
    plan = RulePlan(rule.id, rule.action)
    if rule.from_enum is not None:
        values = tuple(s.value for s in config.enums[rule.from_enum].stages)
    else:
        values = tuple(rule.values or ())
    dv = DataValidation(values=values, allow_invalid=rule.allow_invalid)
    allowed = {norm_text(v) for v in values}

    for name in resolve_tabs(rule.tabs, workbook, config, warnings=plan.warnings):
        if scope is not None and name not in scope:
            continue
        before = workbook.tab(name)
        assert before is not None
        view = build_view(before, config)
        idx = view.col(rule.column)
        if idx is None:
            plan.warnings.append(f"tab {name!r}: validate column {rule.column!r} missing; skipped")
            continue
        after = before.clone()
        count = 0
        invalid = 0
        for offset, row in enumerate(view.data_rows()):
            if view.is_held(row):
                continue
            after.validations[(view.data_start_row + offset, idx + 1)] = dv
            count += 1
            if not is_empty(row[idx]) and norm_text(row[idx]) not in allowed:
                invalid += 1
        if invalid:
            plan.warnings.append(f"tab {name!r}: {invalid} existing value(s) in {rule.column!r} not in list (left as-is)")
        change = TabChange(name, before, after, TabStats(cells_validated=count))
        if change.changed:
            plan.changes.append(change)
    return plan

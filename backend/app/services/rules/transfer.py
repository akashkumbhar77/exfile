"""move / copy: send matching rows to another governed tab, aligned by canonical header.

* A source column with a value but no matching target column aborts the whole
  rule (never drop data, never half-apply).
* copy is idempotent: rows whose key_columns already exist in the target are skipped;
  rows with all key cells empty are skipped with a warning.
* Held rows and all-empty rows are never candidates.
"""

from __future__ import annotations

from app.schemas.config import ConfigSpec, CopyRule, MoveRule
from app.services.conditions import EvalContext, evaluate
from app.services.grid import Row, Workbook, is_empty, norm_text, row_is_empty
from app.services.headers import TabView, build_view
from app.services.rules.base import RuleAbort, RulePlan, TabChange, TabStats
from app.services.rules.tabs import resolve_tabs


def _key(row: Row, view: TabView, columns: list[str]) -> tuple[str, ...] | None:
    idxs = [view.col(c) for c in columns]
    if any(i is None for i in idxs):
        return None
    return tuple(norm_text(row[i]) for i in idxs if i is not None)


def _to_target(row: Row, src: TabView, dst: TabView) -> Row:
    out: Row = [None] * dst.tab.width
    for idx, key in src.all_columns:
        v = row[idx]
        if is_empty(v):
            continue
        t = dst.columns.get(key)
        if t is None:
            raise RuleAbort(
                f"tab {src.tab.name!r} column {src.display.get(key, key)!r} has no match in "
                f"target {dst.tab.name!r}; refusing to drop data"
            )
        if is_empty(out[t]):
            out[t] = v
    return out


def evaluate_transfer(
    rule: MoveRule | CopyRule, workbook: Workbook, scope: list[str] | None, config: ConfigSpec, ctx: EvalContext
) -> RulePlan:
    plan = RulePlan(rule.id, rule.action)
    target_before = workbook.tab(rule.to_tab)
    if target_before is None or rule.to_tab not in config.schema_hashes:
        plan.error = f"target tab {rule.to_tab!r} missing or not governed"
        return plan
    dst = build_view(target_before, config)
    is_copy = isinstance(rule, CopyRule)

    existing: set[tuple[str, ...]] = set()
    if isinstance(rule, CopyRule):
        if any(dst.col(c) is None for c in rule.key_columns):
            plan.error = f"target tab {rule.to_tab!r} lacks key columns {rule.key_columns}"
            return plan
        for r in dst.data_rows():
            k = _key(r, dst, rule.key_columns)
            if k is not None and any(k):
                existing.add(k)

    incoming: list[Row] = []
    try:
        for name in resolve_tabs(rule.tabs, workbook, config, exclude={rule.to_tab}, warnings=plan.warnings):
            if scope is not None and name not in scope:
                continue
            before = workbook.tab(name)
            assert before is not None
            src = build_view(before, config)
            taken: set[int] = set()
            for offset, row in enumerate(src.data_rows()):
                if row_is_empty(row) or src.is_held(row):
                    continue
                if not evaluate(rule.when, row, src, config, ctx):
                    continue
                if isinstance(rule, CopyRule):
                    k = _key(row, src, rule.key_columns)
                    if k is None or not any(k):
                        plan.warnings.append(f"tab {name!r} row {src.data_start_row + offset}: empty copy key; skipped")
                        continue
                    if k in existing:
                        continue
                    existing.add(k)
                incoming.append(_to_target(row, src, dst))
                taken.add(src.data_start_row + offset)
            if taken and not is_copy:
                after = before.clone()
                after.delete_rows(taken)
                plan.changes.append(TabChange(name, before, after, TabStats(rows_removed=len(taken))))
    except RuleAbort as exc:
        plan.changes.clear()
        plan.error = str(exc)
        return plan

    if incoming:
        after = target_before.clone()
        at = dst.data_start_row if rule.position == "top" else dst.data_end_row + 1
        after.insert_rows(at, incoming)
        plan.changes.append(TabChange(rule.to_tab, target_before, after, TabStats(rows_added=len(incoming))))
    return plan

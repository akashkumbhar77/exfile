"""sort: multi-key stable sort with enum stage order (legacy.gs sortSheet).

Held rows keep their absolute positions; the remaining rows are sorted into the
remaining slots. Values move, formats stay put (they are re-applied by format).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime
from functools import cmp_to_key

from app.schemas.config import ConfigSpec, SortKey, SortRule
from app.services.conditions import EvalContext
from app.services.grid import CellValue, Row, Workbook, as_datetime, cell_text, is_empty
from app.services.headers import TabView, build_view, classify
from app.services.rules.base import RulePlan, TabChange, TabStats
from app.services.rules.tabs import resolve_tabs

_EPOCH = datetime(1970, 1, 1)
Sortable = tuple[int, float | str]  # (type rank, value); None = blank
Comparator = Callable[[Row, Row], int]
# Kept deliberately narrow so Engine.gs can reproduce them exactly.
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NUMBER_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _date_value(v: CellValue) -> float | None:
    dt = as_datetime(v)
    if dt is None and isinstance(v, str) and ISO_DATE_RE.match(v.strip()):
        try:
            dt = as_datetime(date.fromisoformat(v.strip()))
        except ValueError:  # e.g. 2026-02-30
            dt = None
    return None if dt is None else (dt - _EPOCH).total_seconds()


def _sortable(v: CellValue, kind: str) -> Sortable | None:
    if is_empty(v):
        return None
    if kind == "date":
        d = _date_value(v)
        return None if d is None else (0, d)
    if kind == "number":
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return (0, float(v))
        text = cell_text(v).strip()
        return (0, float(text)) if NUMBER_RE.match(text) else None
    if kind == "text":
        return (0, cell_text(v).lower())
    # auto
    if isinstance(v, (date, datetime)):
        d = _date_value(v)
        return None if d is None else (0, d)
    if isinstance(v, bool):
        return (2, float(v))
    if isinstance(v, (int, float)):
        return (1, float(v))
    return (3, cell_text(v).lower())


def _cmp(a: int | Sortable, b: int | Sortable) -> int:
    return int(a > b) - int(a < b)  # type: ignore[operator]


def key_comparator(key: SortKey, view: TabView, config: ConfigSpec) -> Comparator | None:
    idx = view.col(key.column)
    if idx is None:
        return None
    sign = -1 if key.order == "desc" else 1
    if key.using_enum is not None:
        spec = config.enums[key.using_enum]
        return lambda a, b: sign * _cmp(classify(spec, a[idx])[1], classify(spec, b[idx])[1])

    blank_sign = 1 if key.blanks == "last" else -1

    def compare(a: Row, b: Row) -> int:
        sa, sb = _sortable(a[idx], key.type), _sortable(b[idx], key.type)
        if sa is None and sb is None:
            return 0
        if sa is None:
            return blank_sign
        if sb is None:
            return -blank_sign
        return sign * _cmp(sa, sb)

    return compare


def build_comparators(
    keys: list[SortKey], view: TabView, config: ConfigSpec, warnings: list[str]
) -> list[Comparator]:
    comparators: list[Comparator] = []
    for key in keys:
        c = key_comparator(key, view, config)
        if c is None:
            warnings.append(f"tab {view.tab.name!r}: sort column {key.column!r} missing; key skipped")
        else:
            comparators.append(c)
    return comparators


def sort_rows(rows: list[Row], comparators: list[Comparator]) -> list[Row]:
    def compare(a: Row, b: Row) -> int:
        for c in comparators:
            r = c(a, b)
            if r:
                return r
        return 0

    return sorted(rows, key=cmp_to_key(compare))  # stable: ties keep original order


def evaluate_sort(
    rule: SortRule, workbook: Workbook, scope: list[str] | None, config: ConfigSpec, ctx: EvalContext
) -> RulePlan:
    plan = RulePlan(rule.id, rule.action)
    for name in resolve_tabs(rule.tabs, workbook, config, warnings=plan.warnings):
        if scope is not None and name not in scope:
            continue
        before = workbook.tab(name)
        assert before is not None
        view = build_view(before, config)
        comparators = build_comparators(rule.keys, view, config, plan.warnings)
        rows = view.data_rows()
        slots = [i for i, r in enumerate(rows) if not view.is_held(r)]
        ordered = sort_rows([rows[i] for i in slots], comparators)

        after = before.clone()
        moved = 0
        for slot, row in zip(slots, ordered):
            if row is not rows[slot]:
                moved += 1
            after.values[view.data_start_row - 1 + slot] = list(row)
        if moved:
            plan.changes.append(TabChange(name, before, after, TabStats(rows_moved=moved)))
    return plan

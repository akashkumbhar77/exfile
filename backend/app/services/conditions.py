"""Evaluate ConfigSpec conditions against one row.

A condition that references a column the tab doesn't have evaluates to False
(safe default: the rule simply doesn't touch that row).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.schemas.config import (
    AllCondition,
    AnyCondition,
    Condition,
    ConfigSpec,
    DateCondition,
    EnumCondition,
    NotCondition,
    ValueCondition,
)
from app.services.grid import Row, as_datetime, is_empty, norm_text
from app.services.headers import TabView, canon_key, classify


@dataclass(frozen=True)
class EvalContext:
    run_id: str
    today: date  # "today" in the sheet's timezone; injected for determinism
    # tab -> last content row at run start. Formatting covers this whole region even after a
    # sort/move in the same run has vacated rows (legacy.gs formats its pre-sort range).
    extents: Mapping[str, int] = field(default_factory=dict)


def _resolve_date(ref: str, today: date) -> datetime:
    d = today if ref == "today" else date.fromisoformat(ref)
    return datetime(d.year, d.month, d.day)


def evaluate(
    cond: Condition,
    row: Row,
    view: TabView,
    config: ConfigSpec,
    ctx: EvalContext,
    cell_col: int | None = None,
) -> bool:
    match cond:
        case AllCondition():
            return all(evaluate(c, row, view, config, ctx, cell_col) for c in cond.all)
        case AnyCondition():
            return any(evaluate(c, row, view, config, ctx, cell_col) for c in cond.any)
        case NotCondition():
            return not evaluate(cond.not_, row, view, config, ctx, cell_col)
        case EnumCondition():
            idx = view.col(cond.column or cond.enum)
            spec = config.enums.get(cond.enum)
            if idx is None or spec is None:
                return False
            stage, _ = classify(spec, row[idx])
            return stage is not None and canon_key(stage) == canon_key(cond.is_)
        case DateCondition():
            idx = view.col(cond.date)
            dt = as_datetime(row[idx]) if idx is not None else None
            if dt is None:
                return False
            if cond.before is not None:
                # "before D" = strictly earlier than D 00:00 (legacy overdue test).
                return dt < _resolve_date(cond.before, ctx.today)
            assert cond.after is not None
            # "after D" = on or after the following midnight.
            return dt >= _resolve_date(cond.after, ctx.today) + timedelta(days=1)
        case ValueCondition():
            idx = view.col(cond.column) if cond.column is not None else cell_col
            if idx is None:
                return False
            v = row[idx]
            if cond.is_blank is not None:
                return is_empty(v) == cond.is_blank
            if cond.contains is not None:
                return cond.contains.strip().upper() in norm_text(v)
            return norm_text(v) == norm_text(cond.equals)
    raise TypeError(f"unsupported condition {type(cond).__name__}")

"""Evaluate ConfigSpec conditions against one row.

A condition that references a column the tab doesn't have evaluates to False
(safe default: the rule simply doesn't touch that row).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.schemas.config import (
    AllCondition,
    AnyCondition,
    Condition,
    ConfigSpec,
    DateCondition,
    EnumCondition,
    NotCondition,
    NumericAbs,
    NumericAdd,
    NumericColumn,
    NumericCondition,
    NumericDivide,
    NumericExpression,
    NumericLiteral,
    NumericMultiply,
    NumericRange,
    NumericRound,
    NumericSubtract,
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
    # when the run started, in the sheet's timezone; stamps the backup tab's rows
    now: datetime | None = None


def _resolve_date(ref: str, today: date) -> datetime:
    d = today if ref == "today" else date.fromisoformat(ref)
    return datetime(d.year, d.month, d.day)


def _number(expr: NumericExpression, row: Row, view: TabView) -> float | None:
    """Safely evaluate one row-level numeric expression.

    Sheets numbers arrive as int/float. Text that merely looks numeric is intentionally not
    coerced: unknown data must be safe by default. A bad operand or division by zero simply
    makes the condition false.
    """
    def many(parts: list[NumericExpression]) -> list[float] | None:
        values: list[float] = []
        for part in parts:
            item = _number(part, row, view)
            if item is None:
                return None
            values.append(item)
        return values

    value: float | None
    match expr:
        case NumericLiteral():
            value = float(expr.literal)
        case NumericColumn():
            idx = view.col(expr.column)
            raw = row[idx] if idx is not None else None
            value = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
        case NumericAdd():
            values = many(expr.add)
            value = sum(values) if values is not None else None
        case NumericSubtract():
            values = many(expr.subtract)
            value = values[0] - values[1] if values is not None else None
        case NumericMultiply():
            values = many(expr.multiply)
            if values is None:
                value = None
            else:
                value = 1.0
                for item in values:
                    value *= item
        case NumericDivide():
            values = many(expr.divide)
            value = None if values is None or values[1] == 0 else values[0] / values[1]
        case NumericAbs():
            raw = _number(expr.abs, row, view)
            value = abs(raw) if raw is not None else None
        case NumericRound():
            raw = _number(expr.round.value, row, view)
            value = sheets_round(raw, expr.round.digits) if raw is not None else None
        case _:
            raise TypeError(f"unsupported numeric expression {type(expr).__name__}")
    return value if value is not None and math.isfinite(value) else None


def sheets_round(value: float, digits: int) -> float:
    """Round half away from zero, like Google Sheets' ROUND (2.5 -> 3, -2.5 -> -3, 2.675 -> 2.68).

    Python's round() rounds half to even on the binary float (round(2.5) == 2,
    round(2.675, 2) == 2.67), which would silently disagree with what the owner sees in the sheet.
    Going through the shortest decimal repr matches the displayed number."""
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(repr(value)).quantize(q, rounding=ROUND_HALF_UP))


def _numeric_match(cond: NumericCondition, row: Row, view: TabView) -> bool:
    left = _number(cond.numeric.left, row, view)
    if left is None:
        return False
    right = cond.numeric.right
    if isinstance(right, NumericRange):
        lower, upper = _number(right.minimum, row, view), _number(right.maximum, row, view)
        if lower is None or upper is None:
            return False
        inside = lower <= left <= upper
        return inside if cond.numeric.op == "between" else not inside
    value = _number(right, row, view)
    if value is None:
        return False
    return {
        "eq": left == value, "ne": left != value, "gt": left > value, "gte": left >= value,
        "lt": left < value, "lte": left <= value,
    }[cond.numeric.op]


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
        case NumericCondition():
            return _numeric_match(cond, row, view)
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

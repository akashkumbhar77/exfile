"""GridOp translation: diff the grid as read against the evaluator's committed workbook.

Planning stays in `plan_run` (shared with dry-run). This module only turns the
difference between "before" (as read) and "after" (the projection) into the
minimal set of writes, so a second run over an organized sheet emits zero ops.

Only rows whose values differ are rewritten (unchanged rows, e.g. held rows, keep
their formulas). Formats and validations are written per changed cell run.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date

from app.adapters.base import (
    AddTab,
    DeleteTab,
    EnsureSize,
    Grid,
    GridOp,
    SetProtection,
    SetValidation,
    WriteFormats,
    WriteValues,
)
from app.core.redaction import Redacted
from app.services.grid import DEFAULT_FORMAT, CellFormat, CellValue, Tab, Workbook, date_serial, is_empty

# Colours that read back identically to "unset" (white fill, black text).
_NEUTRAL_BG = {"#FFFFFF"}
_NEUTRAL_FONT = {"#000000"}


def norm_format(f: CellFormat) -> CellFormat:
    bg = None if f.background is None or f.background.upper() in _NEUTRAL_BG else f.background.upper()
    font = None if f.font is None or f.font.upper() in _NEUTRAL_FONT else f.font.upper()
    return CellFormat(font=font, background=bg, strike=f.strike)


def _same_value(a: CellValue, b: CellValue) -> bool:
    if is_empty(a) and is_empty(b):
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, date) and isinstance(b, date):
        return type(a) is type(b) and a == b
    if isinstance(a, date) or isinstance(b, date):
        # Same stored serial, different number format (e.g. a number sitting in a date-formatted
        # cell). Rewriting would not change what Sheets stores, so it is not a change.
        d, n = (a, b) if isinstance(a, date) else (b, a)
        return isinstance(n, (int, float)) and abs(date_serial(d) - n) < 1e-9  # type: ignore[arg-type]
    return a == b


def _row(tab: Tab | None, r: int, width: int) -> list[CellValue]:
    if tab is None or r >= tab.height:
        return [None] * width
    row = tab.values[r]
    return list(row) + [None] * (width - len(row))


def _fmt_row(tab: Tab | None, r: int, width: int) -> list[CellFormat]:
    if tab is None or r >= tab.height:
        return [DEFAULT_FORMAT] * width
    row = tab.formats[r]
    return [norm_format(f) for f in row] + [DEFAULT_FORMAT] * (width - len(row))


def _runs(flags: Sequence[bool]) -> Iterator[tuple[int, int]]:
    """Contiguous [start, end) runs of True."""
    start: int | None = None
    for i, f in enumerate(list(flags) + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            yield start, i
            start = None


def _changed_rows(height: int, differs: Callable[[int], bool]) -> list[bool]:
    return [differs(r) for r in range(height)]


def diff_workbooks(base: Grid, target: Workbook) -> list[GridOp]:
    ops: list[GridOp] = []
    before_wb = base.workbook
    all_patterns = next(
        (p for meta in base.tabs.values() for p in meta.date_patterns.values()), None
    )

    for index, after in enumerate(target.tabs):
        before = before_wb.tab(after.name)
        height = max(after.height, before.height if before else 0)
        width = max(after.width, before.width if before else 0)
        if before is None:
            ops.append(AddTab(after.name, index, max(after.height, 1), max(after.width, 1)))
        elif after.height > before.height or after.width > before.width:
            ops.append(EnsureSize(after.name, after.height, after.width))

        meta = base.tabs.get(after.name)
        value_rows = _changed_rows(
            height,
            lambda r: not all(_same_value(a, b) for a, b in zip(_row(before, r, width), _row(after, r, width))),
        )
        for s, e in _runs(value_rows):
            block = [_row(after, r, width) for r in range(s, e)]
            ops.append(
                WriteValues(
                    after.name, s + 1, 1, Redacted(block),
                    date_patterns=dict(meta.date_patterns) if meta else {},
                    default_date_pattern=all_patterns,
                )
            )

        fmt_rows = _changed_rows(height, lambda r: _fmt_row(before, r, width) != _fmt_row(after, r, width))
        for s, e in _runs(fmt_rows):
            block_f = tuple(tuple(_fmt_row(after, r, width)) for r in range(s, e))
            ops.append(WriteFormats(after.name, s + 1, 1, block_f))

        old_v = before.validations if before else {}
        for key in sorted(set(old_v) | set(after.validations)):
            if old_v.get(key) != after.validations.get(key):
                ops.append(SetValidation(after.name, key[0], key[1], after.validations.get(key)))

        if (before.protected if before else False) != after.protected:
            ops.append(SetProtection(after.name, after.protected))

    target_names = set(target.names())
    for t in before_wb.tabs:
        if t.name not in target_names:
            ops.append(DeleteTab(t.name))
    return ops


@dataclass
class OpSummary:
    rows_rewritten: int = 0
    cells_written: int = 0
    rows_reformatted: int = 0
    cells_recolored: int = 0
    validations: int = 0
    structural: int = 0

    def total(self) -> int:
        return self.cells_written + self.cells_recolored + self.validations + self.structural


def summarize_ops(base: Grid, ops: Sequence[GridOp]) -> dict[str, OpSummary]:
    out: dict[str, OpSummary] = {}
    for op in ops:
        s = out.setdefault(op.tab, OpSummary())
        match op:
            case WriteValues():
                s.rows_rewritten += op.rows
                s.cells_written += op.rows * op.cols
            case WriteFormats():
                s.rows_reformatted += len(op.formats)
                before = base.workbook.tab(op.tab)
                for i, row in enumerate(op.formats):
                    old = _fmt_row(before, op.row - 1 + i, len(row))
                    s.cells_recolored += sum(1 for a, b in zip(old, row) if a != b)
            case SetValidation():
                s.validations += 1
            case _:
                s.structural += 1
    return out

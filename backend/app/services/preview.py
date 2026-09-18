"""Dry-run preview for the Approvals page (SPEC-PATCH-003 B1).

Computed on demand from the SAME plan the executor uses (prepare_run), returned in the HTTP
response, and never persisted or logged (PATCH-003 A.3, B.6): this module writes nothing and
has no logger. Cell values appear only in the returned structure.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.adapters.base import Adapter
from app.schemas.config import ConfigSpec
from app.services.grid import CellFormat, CellValue, Tab
from app.services.live import prepare_run
from app.services.ops import norm_format, same_value
from app.services.runner import RunEvent

MAX_SAMPLE_ROWS = 20


def _cell(v: CellValue, f: CellFormat) -> dict[str, Any]:
    n = norm_format(f)
    value: Any = v.isoformat() if isinstance(v, (date, datetime)) else v
    return {"v": value, "font": n.font, "bg": n.background, "strike": n.strike}


def _row(tab: Tab | None, r: int, width: int) -> list[dict[str, Any]]:
    cells = []
    for c in range(width):
        v = tab.values[r][c] if tab and r < tab.height and c < len(tab.values[r]) else None
        f = tab.formats[r][c] if tab and r < len(tab.formats) and c < len(tab.formats[r]) else CellFormat()
        cells.append(_cell(v, f))
    return cells


def _cell_changed(before: Tab | None, after: Tab, r: int, c: int) -> bool:
    """Same equivalence as the write diff (ops.py): a date and its serial, "" and blank, white and
    no fill are the same stored cell -- so the preview never shows a change the run won't make."""
    bv = before.values[r][c] if before and r < before.height and c < len(before.values[r]) else None
    av = after.values[r][c] if r < after.height and c < len(after.values[r]) else None
    bf = before.formats[r][c] if before and r < len(before.formats) and c < len(before.formats[r]) else CellFormat()
    af = after.formats[r][c] if r < len(after.formats) and c < len(after.formats[r]) else CellFormat()
    return not same_value(bv, av) or norm_format(bf) != norm_format(af)


def changed_rows(before: Tab | None, after: Tab, header_row: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    width = max(after.width, before.width if before else 0)
    height = max(after.height, before.height if before else 0)
    samples: list[dict[str, Any]] = []
    total = 0
    for r in range(height):  # every row: a title banner or header change is a change too
        changed = [c for c in range(width) if _cell_changed(before, after, r, c)]
        if not changed:
            continue
        total += 1
        if len(samples) < limit:
            samples.append({"row": r + 1, "kind": "title" if r < header_row - 1 else
                            "header" if r == header_row - 1 else "data",
                            "before": _row(before, r, width), "after": _row(after, r, width), "changed": changed})
    return samples, total


def compute_preview(adapter: Adapter, sheet_ref: str, config: ConfigSpec, now: datetime | None = None,
                    limit: int = MAX_SAMPLE_ROWS) -> dict[str, Any]:
    p = prepare_run(adapter, sheet_ref, config, RunEvent("change"), now=now)
    before_wb, after_wb = p.grid.workbook, p.result.workbook
    tabs: list[dict[str, Any]] = []
    for name in sorted(set(p.report.per_tab) | set(p.summary)):
        after = after_wb.tab(name)
        if after is None:
            continue
        before = before_wb.tab(name)
        counts = p.report.per_tab.get(name)
        ops = p.summary.get(name)
        samples, total = changed_rows(before, after, config.header_row, limit)
        header = after.row(config.header_row)
        tabs.append({
            "tab": name,
            "exists": before is not None,
            "rows_to_reorder": counts.rows_moved if counts else 0,
            "rows_to_recolor": counts.rows_formatted if counts else 0,
            "rows_added": counts.rows_added if counts else 0,
            "rows_removed": counts.rows_removed if counts else 0,
            "rows_cleared": counts.rows_cleared if counts else 0,
            "cells_to_write": ops.cells_written if ops else 0,
            "cells_to_recolor": ops.cells_recolored if ops else 0,
            "changed_rows_total": total,
            "headers": ["" if h is None else str(h) for h in header],
            "sample": samples,
        })
    return {
        "status": p.status,
        "today": p.ctx.today.isoformat(),
        "timezone": p.grid.timezone,
        "ops": len(p.ops),
        "drift": [{"tab": d.tab, "expected": d.expected, "actual": d.actual} for d in p.report.drift],
        "warnings": p.report.warnings,
        "tabs": tabs,
    }

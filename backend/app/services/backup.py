"""The backup tab: the generated script's only undo (SPEC-PATCH-004 D.5, forced on by PATCH-005 I.7).

Before rows are removed or overwritten, their contents go to a hidden `_backup` tab in the same
spreadsheet, one row per affected row:

    BACKED UP | RULE | TAB | ROW | ROW AS IT WAS ...
    run start | rule id | tab the row was in | its row number then | its cells, in that tab's order

Only the last `KEEP_RUNS` runs are kept (runs told apart by their start time, to the second);
older ones are dropped when a new run writes. It is NOT a snapshot: formats, dropdowns and
formulas are not kept, and nothing restores it automatically.

The evaluator models the tab so the script's backup is parity-checked like any other write
(PATCH-004 A.3: an emitter never invents behaviour the oracle cannot check).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.services.grid import CellValue, Row, Tab, Workbook, row_is_empty
from app.services.rules.base import RulePlan

BACKUP_TAB = "_backup"
KEEP_RUNS = 10
HEADER: list[CellValue] = ["BACKED UP", "RULE", "TAB", "ROW", "ROW AS IT WAS"]


def with_backup(workbook: Workbook, plans: Sequence[RulePlan], stamp: datetime | str) -> Workbook:
    """`workbook` plus this run's backed-up rows, pruned to the last KEEP_RUNS runs."""
    entries: list[Row] = [[stamp, p.rule_id, b.tab, b.row, *b.values] for p in plans for b in p.backup]
    if not entries:
        return workbook
    out = workbook.clone()
    existing = out.tab(BACKUP_TAB)
    rows = [list(r) for r in existing.values[1:] if not row_is_empty(r)] if existing else []
    rows += entries
    runs = list(dict.fromkeys(r[0] for r in rows))  # distinct stamps, oldest first
    keep = set(runs[-KEEP_RUNS:])
    tab = Tab(BACKUP_TAB, [list(HEADER), *(r for r in rows if r[0] in keep)])
    tab.hidden = True
    if existing is None:
        out.tabs.append(tab)
    else:
        out.put(tab)
    return out

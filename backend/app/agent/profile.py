"""Profile: stored structural description of a sheet (CLAUDE.md definitions).

Holds tabs, header row, headers, column types, fill rates and -- for enum (status-like)
columns only -- value distributions. Never sampled row values (B.7): free-text columns
contribute counts and types, not contents.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Any

from app.agent.masking import is_enum_column
from app.services.grid import CellValue, Tab, Workbook, cell_text, is_empty

MAX_HEADER_SCAN = 5
MAX_ENUM_VALUES = 30


def detect_header_row(tab: Tab) -> int:
    """1-based row among the first few with the most non-empty text cells (title banners have one)."""
    best, best_n = 1, -1
    for r in range(1, min(tab.height, MAX_HEADER_SCAN) + 1):
        n = sum(1 for v in tab.row(r) if isinstance(v, str) and v.strip())
        if n > best_n:
            best, best_n = r, n
    return best


def _type(v: CellValue) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, date):
        return "date"
    if isinstance(v, (int, float)):
        return "number"
    return "text"


def profile_tab(tab: Tab) -> dict[str, Any]:
    header_row = detect_header_row(tab)
    headers = [cell_text(v).strip() for v in tab.row(header_row)]
    data = [r for r in tab.values[header_row:] if any(not is_empty(v) for v in r)]
    columns: list[dict[str, Any]] = []
    for j, header in enumerate(headers):
        if not header:
            continue
        values = [r[j] for r in data if j < len(r)]
        present = [v for v in values if not is_empty(v)]
        types = Counter(_type(v) for v in present)
        col: dict[str, Any] = {
            "index": j + 1,
            "header": header,
            "type": types.most_common(1)[0][0] if types else "empty",
            "types": dict(types),
            "fill_rate": round(len(present) / len(data), 3) if data else 0.0,
            "distinct": len({cell_text(v).strip().upper() for v in present}),
        }
        if is_enum_column(header, present):
            dist = Counter(cell_text(v).strip() for v in present)
            col["enum"] = dict(dist.most_common(MAX_ENUM_VALUES))
        columns.append(col)
    return {
        "name": tab.name,
        "header_row": header_row,
        "data_rows": len(data),
        "protected": tab.protected,
        "columns": columns,
    }


def build_profile(workbook: Workbook, timezone: str) -> dict[str, Any]:
    return {"version": 1, "timezone": timezone, "tabs": [profile_tab(t) for t in workbook.tabs if t.height]}


def enum_columns(tab: Tab, header_row: int) -> set[int]:
    """0-based columns whose values pass through masking unchanged."""
    headers = tab.row(header_row)
    data = tab.values[header_row:]
    return {j for j, h in enumerate(headers)
            if not is_empty(h) and is_enum_column(h, [r[j] for r in data if j < len(r)])}

"""Onboarding agent tools that expose sheet data (SPEC §6). Read-only.

`sample_rows` is the only tool that returns row data, and it always masks (B.7): the
serialized payload is built from `Masker` output, never from the raw grid.
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime
from typing import Any

from app.agent.masking import Masker
from app.agent.profile import detect_header_row, enum_columns
from app.services.grid import CellValue, Workbook, cell_text

MAX_SAMPLE = 50


class ToolError(ValueError):
    pass


def _json_cell(v: CellValue) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def sample_rows(workbook: Workbook, tab_name: str, n: int = 10, rng: random.Random | None = None) -> str:
    """Up to n (<= 50) masked data rows of one tab, as JSON: {tab, header_row, headers, rows}."""
    if not 1 <= n <= MAX_SAMPLE:
        raise ToolError(f"n must be between 1 and {MAX_SAMPLE}")
    tab = workbook.tab(tab_name)
    if tab is None:
        raise ToolError(f"no tab named {tab_name!r}")
    rng = rng or random.Random()
    header_row = detect_header_row(tab)
    headers = list(tab.row(header_row))
    data = [r for r in tab.values[header_row:] if any(cell_text(v).strip() for v in r)]
    picked = sorted(rng.sample(range(len(data)), min(n, len(data))))
    masked = Masker(rng).mask_rows(headers, [data[i] for i in picked], enum_columns(tab, header_row))
    return json.dumps({
        "tab": tab.name,
        "header_row": header_row,
        "headers": [cell_text(h).strip() for h in headers],
        "rows": [[_json_cell(v) for v in row] for row in masked],
        "note": "values are masked samples; headers and status labels are real",
    }, ensure_ascii=False)

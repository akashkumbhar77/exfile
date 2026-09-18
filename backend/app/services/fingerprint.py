"""Per-tab values fingerprint (SPEC-PATCH-002 A.2). Stored hashes only -- never contents (B.6)."""

from __future__ import annotations

import hashlib
from datetime import date

from app.schemas.config import ConfigSpec
from app.services.grid import CellValue, Workbook, cell_text, date_serial, is_empty
from app.services.rules.base import config_targets

_CELL = "\x1f"
_ROW = "\x1e"


def governed_tabs(config: ConfigSpec) -> list[str]:
    return sorted(set(config.schema_hashes) | config_targets(config))


def fingerprint(workbook: Workbook, tabs: list[str]) -> dict[str, str]:
    """tab -> sha256 of its values (type-tagged so 1 and "1" differ). Missing tab -> ""."""
    out: dict[str, str] = {}
    for name in tabs:
        tab = workbook.tab(name)
        if tab is None:
            out[name] = ""
            continue
        h = hashlib.sha256()
        for row in tab.values[: tab.last_content_row()]:
            cells = [_enc(v) for v in row]
            while cells and cells[-1] == "":
                cells.pop()
            h.update(_CELL.join(cells).encode("utf-8"))
            h.update(_ROW.encode())
        out[name] = h.hexdigest()
    return out


def _enc(v: CellValue) -> str:
    """Encode what the spreadsheet stores: text vs number (dates as serials) vs boolean."""
    if is_empty(v):
        return ""
    if isinstance(v, bool):
        return "b" + cell_text(v)
    if isinstance(v, date):
        return f"n{date_serial(v):.15g}"
    if isinstance(v, (int, float)):
        return f"n{float(v):.15g}"
    return "s" + cell_text(v)

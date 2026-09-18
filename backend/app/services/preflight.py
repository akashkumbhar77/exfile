"""Pre-flight schema check (invariant 4): header hash must match before any rule runs.

Hash algorithm (Engine.gs must reproduce it byte-for-byte):
  1. take the header row cells (config.header_row), left to right
  2. text of each cell (String(v) semantics), trimmed; case preserved
  3. drop trailing empty cells
  4. join with U+001F (unit separator), UTF-8 encode, SHA-256, lowercase hex
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.schemas.config import ConfigSpec
from app.services.grid import CellValue, Workbook, cell_text

HEADER_SEPARATOR = ""


def compute_schema_hash(header_cells: Sequence[CellValue]) -> str:
    cells = [cell_text(v).strip() for v in header_cells]
    while cells and cells[-1] == "":
        cells.pop()
    return hashlib.sha256(HEADER_SEPARATOR.join(cells).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Drift:
    tab: str
    expected: str
    actual: str | None  # None = tab no longer exists


@dataclass
class PreflightResult:
    drift: list[Drift] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.drift


def preflight(config: ConfigSpec, workbook: Workbook) -> PreflightResult:
    result = PreflightResult()
    for name, expected in config.schema_hashes.items():
        tab = workbook.tab(name)
        actual = compute_schema_hash(tab.row(config.header_row)) if tab is not None else None
        if actual != expected:
            result.drift.append(Drift(name, expected, actual))
    return result


def schema_hashes_for(workbook: Workbook, tabs: Sequence[str], header_row: int) -> dict[str, str]:
    """Helper for onboarding/tests: hashes of the given tabs as they are now."""
    out: dict[str, str] = {}
    for name in tabs:
        tab = workbook.tab(name)
        if tab is None:
            raise KeyError(name)
        out[name] = compute_schema_hash(tab.row(header_row))
    return out

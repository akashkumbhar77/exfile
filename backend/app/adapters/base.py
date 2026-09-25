"""Adapter interface (SPEC-PATCH-001 A.2). Everything format-specific lives behind it.

GridOps are the adapter-agnostic write vocabulary produced from evaluator output
(`app/services/ops.py`). Cell values inside ops are `Redacted` (B.6): they are
unwrapped only by an adapter's write path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from app.core.redaction import Redacted
from app.services.grid import Banding, CellFormat, CellValue, DataValidation, Workbook, col_letter

SourceRef = str  # Sheets: the spreadsheet id


class UnregisteredSource(PermissionError):
    """B.9: the system never opens a file that is not in the registry."""


class SourceRegistry(Protocol):
    def is_registered(self, source_ref: SourceRef) -> bool: ...


@dataclass(frozen=True)
class StaticRegistry:
    """S1 registry: ids from settings. S2 swaps in the `sheets` table."""

    ids: frozenset[str]

    def is_registered(self, source_ref: SourceRef) -> bool:
        return source_ref in self.ids


@dataclass(frozen=True)
class TabMeta:
    # column (0-based) -> number-format pattern of the date cells seen in it at read time
    date_patterns: Mapping[int, str] = field(default_factory=dict)


@dataclass(repr=False)
class Grid:
    workbook: Workbook
    timezone: str = "UTC"
    tabs: Mapping[str, TabMeta] = field(default_factory=dict)
    title: str = ""  # spreadsheet name (metadata)

    def __repr__(self) -> str:
        return f"Grid(timezone={self.timezone!r}, workbook={self.workbook!r})"


# ---- GridOps (all rows/cols 1-based) -------------------------------------------------------


@dataclass(frozen=True)
class AddTab:
    tab: str
    index: int
    rows: int
    cols: int


@dataclass(frozen=True)
class EnsureSize:
    tab: str
    rows: int
    cols: int


@dataclass(frozen=True)
class WriteValues:
    tab: str
    row: int
    col: int
    values: Redacted[list[list[CellValue]]]
    # 0-based column -> date pattern to apply to date cells written in that column
    date_patterns: Mapping[int, str] = field(default_factory=dict)
    default_date_pattern: str | None = None

    @property
    def rows(self) -> int:
        return len(self.values.reveal())

    @property
    def cols(self) -> int:
        return max((len(r) for r in self.values.reveal()), default=0)

    @property
    def a1(self) -> str:
        return _a1(self.row, self.col, self.rows, self.cols)


@dataclass(frozen=True)
class WriteFormats:
    tab: str
    row: int
    col: int
    formats: tuple[tuple[CellFormat, ...], ...]

    @property
    def a1(self) -> str:
        return _a1(self.row, self.col, len(self.formats), max((len(r) for r in self.formats), default=0))


@dataclass(frozen=True)
class SetValidation:
    tab: str
    row: int
    col: int
    validation: DataValidation | None  # None = clear


@dataclass(frozen=True)
class SetProtection:
    tab: str
    protected: bool


@dataclass(frozen=True)
class DeleteTab:
    tab: str


@dataclass(frozen=True)
class WriteNumberFormats:
    tab: str
    row: int
    col: int
    formats: tuple[tuple[str | None, ...], ...]  # None = back to the sheet default


@dataclass(frozen=True)
class SetColumnWidth:
    tab: str
    col: int
    width: int


@dataclass(frozen=True)
class SetBanding:
    tab: str
    banding: Banding | None  # replaces whatever banding the tab had; None = remove it


@dataclass(frozen=True)
class SetHidden:
    tab: str
    hidden: bool


GridOp = (AddTab | EnsureSize | WriteValues | WriteFormats | SetValidation | SetProtection | DeleteTab
          | WriteNumberFormats | SetColumnWidth | SetBanding | SetHidden)


def _a1(row: int, col: int, rows: int, cols: int) -> str:
    return f"{col_letter(col)}{row}:{col_letter(col + max(cols, 1) - 1)}{row + max(rows, 1) - 1}"


@dataclass(frozen=True)
class WriteResult:
    requests: int
    cells_written: int
    formats_written: int


@dataclass(frozen=True)
class ChangeInfo:
    changed: bool
    fingerprint: str


@dataclass(frozen=True)
class AdapterCaps:
    realtime: bool
    formats: tuple[str, ...]
    validations: bool
    protection: bool


class Adapter(Protocol):
    def read_grid(self, source_ref: SourceRef) -> Grid: ...

    def write_ops(self, source_ref: SourceRef, ops: list[GridOp]) -> WriteResult: ...

    def detect_changes(self, source_ref: SourceRef) -> ChangeInfo: ...

    def capabilities(self) -> AdapterCaps: ...

"""In-memory workbook model used by the rule evaluator, dry-run and executor.

Mirrors what the Sheets API / Apps Script returns: a rectangular value grid per
tab plus position-bound formatting (formats do NOT travel with values on a
sort, exactly like `range.setValues`; they DO shift when rows are inserted or
deleted). Rows and columns are 1-based in the public helpers.

Presentation is modelled as intent, not rendering (owner ruling 2026-09-20): a cell's number
format, a column's width, and row banding with its theme on a range. Fonts, borders, merges and
alignment are rendering and are not modelled.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TypeAlias

CellValue: TypeAlias = str | bool | int | float | date | datetime | None
Row: TypeAlias = list[CellValue]


@dataclass(frozen=True)
class CellFormat:
    font: str | None = None  # None = sheet default font colour
    background: str | None = None  # None = no fill (banding shows through)
    strike: bool = False


DEFAULT_FORMAT = CellFormat()


@dataclass(frozen=True)
class Banding:
    """Alternating row colours (no header or footer band) over a 1-based block of rows."""

    theme: str  # a Sheets BandingTheme name, e.g. LIGHT_GREY
    row: int
    rows: int
    cols: int  # from column 1


@dataclass(frozen=True)
class DataValidation:
    values: tuple[str, ...]
    allow_invalid: bool = False


@dataclass(repr=False)
class Tab:
    name: str
    values: list[Row]
    formats: list[list[CellFormat]] = field(default_factory=list)
    validations: dict[tuple[int, int], DataValidation] = field(default_factory=dict)
    protected: bool = False
    # 1-based (row, col) -> Sheets number format pattern; absent = the sheet default ("General")
    number_formats: dict[tuple[int, int], str] = field(default_factory=dict)
    # 1-based column -> width in pixels; absent = the sheet default
    column_widths: dict[int, int] = field(default_factory=dict)
    banding: Banding | None = None
    hidden: bool = False

    def __post_init__(self) -> None:
        width = max((len(r) for r in self.values), default=0)
        self.values = [list(r) + [None] * (width - len(r)) for r in self.values]
        fmts = [list(r) for r in self.formats[: len(self.values)]]
        fmts += [[] for _ in range(len(self.values) - len(fmts))]
        self.formats = [r[:width] + [DEFAULT_FORMAT] * (width - len(r)) for r in fmts]

    @property
    def width(self) -> int:
        return len(self.values[0]) if self.values else 0

    @property
    def height(self) -> int:
        return len(self.values)

    def row(self, n: int) -> Row:
        """1-based row; rows beyond the grid read as blank."""
        if 1 <= n <= self.height:
            return self.values[n - 1]
        return [None] * self.width

    def last_content_row(self) -> int:
        """Like Sheet.getLastRow(): last row holding any non-empty value (0 if none)."""
        for i in range(self.height, 0, -1):
            if not row_is_empty(self.values[i - 1]):
                return i
        return 0

    def ensure_size(self, rows: int, cols: int) -> None:
        width = max(self.width, cols)
        if width > self.width:
            extra = width - self.width
            for r in self.values:
                r.extend([None] * extra)
            for f in self.formats:
                f.extend([DEFAULT_FORMAT] * extra)
        while self.height < rows:
            self.values.append([None] * width)
            self.formats.append([DEFAULT_FORMAT] * width)

    def delete_rows(self, rows: set[int]) -> None:
        """Delete 1-based rows; everything below shifts up (values, formats, validations)."""
        if not rows:
            return
        keep = [i for i in range(1, self.height + 1) if i not in rows]
        new_pos = {old: new for new, old in enumerate(keep, start=1)}
        self.values = [self.values[i - 1] for i in keep]
        self.formats = [self.formats[i - 1] for i in keep]
        self.validations = {
            (new_pos[r], c): v for (r, c), v in self.validations.items() if r in new_pos
        }
        self.number_formats = {
            (new_pos[r], c): f for (r, c), f in self.number_formats.items() if r in new_pos
        }

    def insert_rows(self, before: int, rows: list[Row]) -> None:
        """Insert rows so the first lands at 1-based `before`; rows at/after it shift down."""
        if not rows:
            return
        width = max(self.width, max(len(r) for r in rows))
        self.ensure_size(max(self.height, before - 1), width)
        padded = [list(r) + [None] * (width - len(r)) for r in rows]
        idx = before - 1
        self.values[idx:idx] = padded
        self.formats[idx:idx] = [[DEFAULT_FORMAT] * width for _ in padded]
        n = len(rows)
        self.validations = {
            ((r + n if r >= before else r), c): v for (r, c), v in self.validations.items()
        }
        self.number_formats = {
            ((r + n if r >= before else r), c): f for (r, c), f in self.number_formats.items()
        }

    def clone(self) -> Tab:
        return copy.deepcopy(self)

    def __repr__(self) -> str:  # B.6: cell values never reach logs or tracebacks
        return f"Tab(name={self.name!r}, rows={self.height}, cols={self.width}, values=<redacted>)"


@dataclass(repr=False)
class Workbook:
    tabs: list[Tab]

    def tab(self, name: str) -> Tab | None:
        return next((t for t in self.tabs if t.name == name), None)

    def names(self) -> list[str]:
        return [t.name for t in self.tabs]

    def clone(self) -> Workbook:
        return copy.deepcopy(self)

    def __repr__(self) -> str:
        return f"Workbook(tabs={self.names()!r}, values=<redacted>)"

    def put(self, tab: Tab, *, insert_at: int = 0) -> None:
        """Replace the tab with the same name, or insert it at `insert_at` if new."""
        for i, t in enumerate(self.tabs):
            if t.name == tab.name:
                self.tabs[i] = tab
                return
        self.tabs.insert(insert_at, tab)


def is_empty(v: CellValue) -> bool:
    """Legacy semantics: only '' and null are empty (whitespace is content)."""
    return v is None or v == ""


def row_is_empty(row: Row) -> bool:
    return all(is_empty(v) for v in row)


def cell_text(v: CellValue) -> str:
    """Stable text form of a cell, matching JavaScript String() for the common cases."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        return v.isoformat(sep="T") if (v.hour or v.minute or v.second or v.microsecond) else v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        if math.isfinite(v) and v == int(v):
            return str(int(v))
        return repr(v)
    return str(v)


def norm_text(v: CellValue) -> str:
    return cell_text(v).strip().upper()


def as_datetime(v: CellValue) -> datetime | None:
    """Real date cells only (legacy treats anything else as 'not a date')."""
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    return None


SERIAL_EPOCH = datetime(1899, 12, 30)


def date_serial(v: date) -> float:
    """Spreadsheet serial number of a date/datetime (days since 1899-12-30)."""
    dt = v if isinstance(v, datetime) else datetime(v.year, v.month, v.day)
    delta = dt.replace(tzinfo=None) - SERIAL_EPOCH
    return delta.days + delta.seconds / 86400 + delta.microseconds / 86_400_000_000


def col_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def a1_range(rows: int, cols: int) -> str:
    return f"A1:{col_letter(max(cols, 1))}{max(rows, 1)}"

"""Header canonicalization, per-tab column resolution, enum classification, hold detection.

Columns are always found by canonical keyword matching against the header row,
never by fixed index (CLAUDE.md, engine rules).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.schemas.config import ConfigSpec, EnumSpec
from app.services.grid import CellValue, Row, Tab, Workbook, cell_text, is_empty, norm_text


def canon_key(name: str) -> str:
    """Identity of a column name: trimmed + upper-cased."""
    return name.strip().upper()


def match_expr(expr: str, value: CellValue) -> bool:
    kind, _, needle = expr.partition(":")
    hay = norm_text(value)
    target = needle.strip().upper()
    if kind == "contains":
        return target in hay
    if kind == "equals":
        return hay == target
    raise ValueError(f"unknown match kind {kind!r}")


def is_hold_header(raw: CellValue, config: ConfigSpec) -> bool:
    return norm_text(raw) == canon_key(config.guards.hold_column)


def canonicalize(raw: CellValue, config: ConfigSpec) -> str:
    """First canonical_headers entry whose any pattern matches; otherwise the trimmed raw header."""
    for ch in config.canonical_headers:
        if any(match_expr(m, raw) for m in ch.match):
            return ch.canonical
    return cell_text(raw).strip()


@dataclass(frozen=True)
class TabView:
    """A tab seen through a config: resolved columns (0-based) and data-row bounds."""

    tab: Tab
    columns: dict[str, int]  # canon_key -> 0-based column; first matching column wins
    display: dict[str, str]  # canon_key -> display name (canonical name or trimmed raw)
    all_columns: tuple[tuple[int, str], ...]  # every non-blank, non-hold header: (0-based col, canon_key)
    hold_idx: int | None
    header_row: int
    data_start_row: int
    min_end_row: int = 0

    def col(self, name: str) -> int | None:
        return self.columns.get(canon_key(name))

    def has(self, name: str) -> bool:
        return canon_key(name) in self.columns

    @property
    def data_end_row(self) -> int:
        pinned = min(self.min_end_row, self.tab.height)
        return max(self.tab.last_content_row(), pinned, self.data_start_row - 1)

    def data_rows(self) -> list[Row]:
        return [self.tab.row(n) for n in range(self.data_start_row, self.data_end_row + 1)]

    def is_held(self, row: Row) -> bool:
        return self.hold_idx is not None and hold_is_set(row[self.hold_idx])


def build_view(
    tab: Tab, config: ConfigSpec, *, canonical_headers: bool = True, min_end_row: int = 0
) -> TabView:
    """Resolve columns from the header row. `canonical_headers=False` keys raw headers as-is
    (used for generated tabs whose headers are already canonical)."""
    headers = tab.row(config.header_row)
    columns: dict[str, int] = {}
    display: dict[str, str] = {}
    all_columns: list[tuple[int, str]] = []
    hold_idx: int | None = None
    for i, raw in enumerate(headers):
        if is_empty(raw) or not cell_text(raw).strip():
            continue
        if is_hold_header(raw, config):
            if hold_idx is None:
                hold_idx = i
            continue
        name = canonicalize(raw, config) if canonical_headers else cell_text(raw).strip()
        key = canon_key(name)
        all_columns.append((i, key))
        if key not in columns:
            columns[key] = i
            display[key] = name
    return TabView(tab, columns, display, tuple(all_columns), hold_idx, config.header_row,
                   config.data_start_row, min_end_row)


def hold_is_set(v: CellValue) -> bool:
    """`!hold` marker: checked checkbox, non-zero number, date, or any text except FALSE/NO/0."""
    if v is None or v is False:
        return False
    if v is True:
        return True
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, date):
        return True
    return norm_text(v) not in ("", "FALSE", "NO", "0")


def classify(spec: EnumSpec, value: CellValue) -> tuple[str | None, int]:
    """(stage value, order) for the first stage (in list order) whose pattern matches;
    (None, unknown_order) otherwise. Blank never matches a stage."""
    if not is_empty(value):
        for stage in spec.stages:
            if match_expr(stage.match, value):
                return stage.value, stage.order
    return None, spec.unknown_order


def headers_of(workbook: Workbook, config: ConfigSpec) -> dict[str, list[str]]:
    """Header text of each governed tab (structure only), for the drift "what changed" view."""
    out: dict[str, list[str]] = {}
    for name in config.schema_hashes:
        tab = workbook.tab(name)
        if tab is not None:
            out[name] = [cell_text(h).strip() for h in tab.row(config.header_row)]
    return out

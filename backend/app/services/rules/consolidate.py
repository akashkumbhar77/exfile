"""consolidate: union of source tabs into one locked target (legacy.gs assembleConsolidated/writeConsolidated).

* master header = union of canonical headers, first-seen order (hold column excluded)
* derived columns appended if absent; prepend columns go first
* per row, the first non-empty value wins when several source columns share a canonical name
* all-empty rows and held rows are skipped
* sorted like `sort_like`, formatted like `format_like`
* the target is fully regenerated and locked (by-design exception to invariant 8)

Presentation, as intent (owner ruling 2026-09-20; the rules Engine.gs and legacy.gs applied):
* title banner and header row colours
* row banding with the configured theme over the data rows (none when there are no rows)
* every target column whose header contains DATE gets `date_format` on its data rows;
  `match_source` takes the number format of the first data cell of the date column in the first
  source tab with data rows (the date column is the sort_like rule's first date key, else the
  source's first header containing DATE), and d/m/yyyy when no source has one
* column widths are set when the target tab is created, from `column_widths` (header names
  compared trimmed and case-insensitively) or `default_column_width`; after that the owner's own
  widths are kept
"""

from __future__ import annotations

from app.schemas.config import ConfigSpec, ConsolidateRule, FormatRule, Presentation, SortRule
from app.services.conditions import EvalContext
from app.services.grid import Banding, CellFormat, Row, Tab, Workbook, is_empty, row_is_empty
from app.services.headers import build_view, canon_key
from app.services.rules.base import RulePlan, TabChange, TabStats
from app.services.rules.format import format_view
from app.services.rules.sort import build_comparators, sort_rows
from app.services.rules.tabs import resolve_tabs


def assemble(
    rule: ConsolidateRule, workbook: Workbook, config: ConfigSpec, warnings: list[str]
) -> tuple[list[str], list[Row]]:
    views = [
        build_view(t, config)
        for name in resolve_tabs(rule.sources, workbook, config, warnings=warnings)
        if (t := workbook.tab(name)) is not None
    ]

    master: list[str] = []
    pos: dict[str, int] = {}
    for view in views:
        for _, key in view.all_columns:
            if key not in pos:
                pos[key] = len(master)
                master.append(view.display[key])
    for d in rule.derived:
        key = canon_key(d.name)
        if key not in pos:
            pos[key] = len(master)
            master.append(d.name)

    headers = [p.name for p in rule.prepend_columns] + master
    rows: list[Row] = []
    for view in views:
        mapping = [(idx, pos[key]) for idx, key in view.all_columns]
        for row in view.data_rows():
            if row_is_empty(row) or view.is_held(row):
                continue
            out: Row = [None] * len(master)
            for src, dst in mapping:
                if is_empty(out[dst]):
                    out[dst] = row[src]
            for d in rule.derived:
                p = pos[canon_key(d.name)]
                if is_empty(out[p]):
                    out[p] = view.tab.name.strip()
            prefix: Row = [view.tab.name for _ in rule.prepend_columns]
            rows.append(prefix + out)
    return headers, rows


FALLBACK_DATE_FORMAT = "d/m/yyyy"
DEFAULT_NUMBER_FORMAT = "General"  # what Sheets reports for a cell nobody formatted


def column_width_map(presentation: Presentation) -> dict[str, int]:
    """`column_widths` keyed by canonical header. When two keys canonicalise alike, a key already
    written in canonical form wins; otherwise the later key does."""
    out: dict[str, int] = {}
    exact: set[str] = set()
    for name, width in presentation.column_widths.items():
        key = canon_key(name)
        if key in exact:
            continue
        out[key] = width
        if name == key:
            exact.add(key)
    return out


def is_date_header(header: object) -> bool:
    return "DATE" in str(header).upper()


def source_date_format(rule: ConsolidateRule, workbook: Workbook, config: ConfigSpec) -> str:
    """`match_source`: the first source with data rows that has the date column decides."""
    sort_rule = config.rule(rule.sort_like) if rule.sort_like else None
    date_key = None
    if isinstance(sort_rule, SortRule):
        date_key = next((canon_key(k.column) for k in sort_rule.keys if k.type == "date"), None)
    for name in resolve_tabs(rule.sources, workbook, config):
        tab = workbook.tab(name)
        if tab is None or tab.last_content_row() < config.data_start_row:
            continue
        view = build_view(tab, config)
        if date_key is not None:
            idx = view.columns.get(date_key)
        else:
            idx = next((i for i, key in view.all_columns if "DATE" in key), None)
        if idx is not None:
            return tab.number_formats.get((config.data_start_row, idx + 1), DEFAULT_NUMBER_FORMAT)
    return FALLBACK_DATE_FORMAT


def present(rule: ConsolidateRule, after: Tab, headers: list[str], n_rows: int, existing: Tab | None,
            workbook: Workbook, config: ConfigSpec) -> None:
    """Banding, date formats and widths on the rebuilt target (see the module docstring)."""
    p = rule.presentation
    ds = config.data_start_row
    if n_rows:
        if p.banding:
            after.banding = Banding(p.banding, ds, n_rows, len(headers))
        fmt = source_date_format(rule, workbook, config) if p.date_format == "match_source" else p.date_format
        for c, header in enumerate(headers, start=1):
            if is_date_header(header):
                for r in range(ds, ds + n_rows):
                    after.number_formats[(r, c)] = fmt
    if existing is None:
        widths = column_width_map(p)
        after.column_widths = {c: widths.get(canon_key(h), p.default_column_width)
                               for c, h in enumerate(headers, start=1)}
    else:
        after.column_widths = dict(existing.column_widths)


def evaluate_consolidate(
    rule: ConsolidateRule, workbook: Workbook, scope: list[str] | None, config: ConfigSpec, ctx: EvalContext
) -> RulePlan:
    plan = RulePlan(rule.id, rule.action)
    headers, rows = assemble(rule, workbook, config, plan.warnings)

    after = Tab(rule.target_tab, [])
    existing = workbook.tab(rule.target_tab)
    if config.header_row > 1 and rule.presentation.title:
        after.ensure_size(1, 1)
        after.values[0][0] = rule.presentation.title
        # merged banner: Sheets reports its style on the anchor cell only
        after.formats[0][0] = CellFormat(
            font=rule.presentation.title_font.upper(), background=rule.presentation.title_background.upper()
        )
    elif config.header_row > 1 and existing is not None and not is_empty(existing.row(1)[0] if existing.width else None):
        # Title fallback (addendum to PATCH-003 decision (c)): no configured title -> keep the target
        # tab's existing banner text and anchor style exactly as they are. Read by the engine only.
        after.ensure_size(1, 1)
        after.values[0][0] = existing.values[0][0]
        after.formats[0][0] = existing.formats[0][0]
    after.ensure_size(config.header_row, len(headers))
    after.values[config.header_row - 1][: len(headers)] = headers
    header_fmt = CellFormat(
        font=rule.presentation.header_font.upper(), background=rule.presentation.header_background.upper()
    )
    after.formats[config.header_row - 1][: len(headers)] = [header_fmt] * len(headers)
    view = build_view(after, config, canonical_headers=False)

    sort_rule = config.rule(rule.sort_like) if rule.sort_like else None
    if isinstance(sort_rule, SortRule):
        rows = sort_rows(rows, build_comparators(sort_rule.keys, view, config, plan.warnings))

    after.ensure_size(config.data_start_row - 1, len(headers))
    after.insert_rows(config.data_start_row, rows)
    after.protected = rule.lock
    present(rule, after, headers, len(rows), existing, workbook, config)

    format_rule = config.rule(rule.format_like) if rule.format_like else None
    if isinstance(format_rule, FormatRule):
        format_view(format_rule, build_view(after, config, canonical_headers=False), config, ctx, after,
                    plan.warnings)

    before = workbook.tab(rule.target_tab)
    change = TabChange(rule.target_tab, before, after, TabStats(rows_added=len(rows)))
    if change.changed:
        plan.changes.append(change)
    return plan

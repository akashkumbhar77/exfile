"""consolidate: union of source tabs into one locked target (legacy.gs assembleConsolidated/writeConsolidated).

* master header = union of canonical headers, first-seen order (hold column excluded)
* derived columns appended if absent; prepend columns go first
* per row, the first non-empty value wins when several source columns share a canonical name
* all-empty rows and held rows are skipped
* sorted like `sort_like`, formatted like `format_like`
* the target is fully regenerated and locked (by-design exception to invariant 8)

Presentation (title styling, banding, widths, date formats) is applied by Engine.gs.
"""

from __future__ import annotations

from app.schemas.config import ConfigSpec, ConsolidateRule, FormatRule, SortRule
from app.services.conditions import EvalContext
from app.services.grid import CellFormat, Row, Tab, Workbook, is_empty, row_is_empty
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


def evaluate_consolidate(
    rule: ConsolidateRule, workbook: Workbook, scope: list[str] | None, config: ConfigSpec, ctx: EvalContext
) -> RulePlan:
    plan = RulePlan(rule.id, rule.action)
    headers, rows = assemble(rule, workbook, config, plan.warnings)

    after = Tab(rule.target_tab, [])
    if config.header_row > 1 and rule.presentation.title:
        after.ensure_size(1, 1)
        after.values[0][0] = rule.presentation.title
        # merged banner: Sheets reports its style on the anchor cell only
        after.formats[0][0] = CellFormat(
            font=rule.presentation.title_font.upper(), background=rule.presentation.title_background.upper()
        )
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

    format_rule = config.rule(rule.format_like) if rule.format_like else None
    if isinstance(format_rule, FormatRule):
        format_view(format_rule, build_view(after, config, canonical_headers=False), config, ctx, after,
                    plan.warnings)

    before = workbook.tab(rule.target_tab)
    change = TabChange(rule.target_tab, before, after, TabStats(rows_added=len(rows)))
    if change.changed:
        plan.changes.append(change)
    return plan

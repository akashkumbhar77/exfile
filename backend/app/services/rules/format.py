"""format: row- and cell-scoped font / fill / strikethrough (legacy.gs applyStatusFormatting).

Resolution per cell: current format -> rule `default` -> matching row_rules in
order (later wins per attribute) -> matching cell_rules in order.
Held rows keep their current formatting.
"""

from __future__ import annotations

from app.schemas.config import ConfigSpec, FormatRule, Style
from app.services.conditions import EvalContext, evaluate
from app.services.grid import CellFormat, Tab, Workbook
from app.services.headers import TabView, build_view
from app.services.rules.base import RulePlan, TabChange, TabStats
from app.services.rules.tabs import resolve_tabs


def overlay(fmt: CellFormat, style: Style) -> CellFormat:
    font = style.font.upper() if style.font is not None else fmt.font
    if style.background is None:
        background = fmt.background
    elif style.background == "none":
        background = None
    else:
        background = style.background.upper()
    strike = fmt.strike if style.strike is None else style.strike
    return CellFormat(font=font, background=background, strike=strike)


def format_view(
    rule: FormatRule, view: TabView, config: ConfigSpec, ctx: EvalContext, target: Tab, warnings: list[str]
) -> int:
    """Write resolved formats for `view`'s data rows into `target` (same geometry). Returns rows changed."""
    cell_cols: list[int | None] = []
    for cr in rule.cell_rules:
        idx = view.col(cr.column)
        if idx is None:
            warnings.append(f"tab {view.tab.name!r}: format column {cr.column!r} missing; cell rule skipped")
        cell_cols.append(idx)

    changed_rows = 0
    for offset, row in enumerate(view.data_rows()):
        if view.is_held(row):
            continue
        r = view.data_start_row - 1 + offset
        target.ensure_size(r + 1, view.tab.width)
        style_row = [overlay(f, rule.default) for f in target.formats[r]]
        for rr in rule.row_rules:
            if evaluate(rr.when, row, view, config, ctx):
                style_row = [overlay(f, rr) for f in style_row]
        for cr, idx in zip(rule.cell_rules, cell_cols):
            if idx is not None and evaluate(cr.when, row, view, config, ctx, cell_col=idx):
                style_row[idx] = overlay(style_row[idx], cr)
        if style_row != target.formats[r]:
            target.formats[r] = style_row
            changed_rows += 1
    return changed_rows


def evaluate_format(
    rule: FormatRule, workbook: Workbook, scope: list[str] | None, config: ConfigSpec, ctx: EvalContext
) -> RulePlan:
    plan = RulePlan(rule.id, rule.action)
    for name in resolve_tabs(rule.tabs, workbook, config, warnings=plan.warnings):
        if scope is not None and name not in scope:
            continue
        before = workbook.tab(name)
        assert before is not None
        after = before.clone()
        view = build_view(before, config, min_end_row=ctx.extents.get(name, 0))
        n = format_view(rule, view, config, ctx, after, plan.warnings)
        if n:
            plan.changes.append(TabChange(name, before, after, TabStats(rows_formatted=n)))
    return plan

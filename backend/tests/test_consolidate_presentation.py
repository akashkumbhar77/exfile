"""Consolidate presentation as intent: number formats, column widths, banding (owner, 2026-09-20)."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.adapters.base import Grid, SetBanding, SetColumnWidth, WriteNumberFormats
from app.schemas.config import ConfigSpec, ConsolidateRule, Presentation
from app.services.conditions import EvalContext
from app.services.executor import execute_run
from app.services.grid import Banding, Workbook
from app.services.ops import diff_workbooks, summarize_ops
from app.services.rules.consolidate import column_width_map, source_date_format
from app.services.runner import RunEvent
from tests.conftest import load_reference_raw
from tests.fixtures import reference_workbook


def run(raw: dict[str, Any], workbook: Workbook) -> Workbook:
    config = ConfigSpec.model_validate(raw)
    result = execute_run(config, workbook, RunEvent.manual(), EvalContext(run_id="p", today=date(2026, 9, 16)))
    assert result.plan.status == "OK"
    return result.workbook


def summary(workbook: Workbook) -> Any:
    tab = workbook.tab("SUMMARY")
    assert tab is not None
    return tab


def test_width_keys_are_compared_trimmed_and_case_insensitively() -> None:
    p = Presentation(column_widths={" status ": 90, "Qty": 70, "QTY": 75, "qty ": 80})
    assert column_width_map(p) == {"STATUS": 90, "QTY": 75}, "a key already in canonical form wins"


def test_a_new_summary_gets_configured_widths_and_the_default_elsewhere() -> None:
    raw = load_reference_raw()
    tab = summary(run(raw, reference_workbook.build()))
    headers = [str(h).upper() for h in tab.values[raw["header_row"] - 1]]
    configured = column_width_map(Presentation.model_validate(raw["rules"][2]["presentation"]))
    for c, h in enumerate(headers, start=1):
        assert tab.column_widths[c] == configured.get(h, 130), h


def test_no_source_with_a_date_column_falls_back_to_d_m_yyyy() -> None:
    config = ConfigSpec.model_validate(load_reference_raw())
    rule = config.rules[2]
    assert isinstance(rule, ConsolidateRule)
    workbook = reference_workbook.build()
    assert source_date_format(rule, workbook, config) == "General", "found, but never formatted"
    for tab in workbook.tabs:
        tab.values[config.header_row - 1] = [
            "WHEN" if "DATE" in str(h).upper() else h for h in tab.values[config.header_row - 1]]
    assert source_date_format(rule, workbook, config) == "d/m/yyyy"


def test_banding_covers_exactly_the_data_rows() -> None:
    raw = load_reference_raw()
    tab = summary(run(raw, reference_workbook.build()))
    ds = raw["data_start_row"]
    assert tab.banding == Banding("LIGHT_GREY", ds, tab.last_content_row() - ds + 1, tab.width)


def test_an_empty_summary_has_no_banding_and_no_date_formats() -> None:
    raw = load_reference_raw()
    workbook = reference_workbook.build()
    for name in ("MACHINES", "SPARES"):
        tab = workbook.tab(name)
        assert tab is not None
        tab.values = tab.values[:raw["header_row"]]
        tab.formats = tab.formats[:raw["header_row"]]
    tab = summary(run(raw, workbook))
    assert tab.banding is None and tab.number_formats == {}
    assert tab.column_widths, "widths still apply to a newly created, empty summary"


def test_the_preview_counts_presentation_changes() -> None:
    """The diff that feeds the preview summary must not silently drop presentation."""
    workbook = reference_workbook.build()
    after = run(load_reference_raw(), workbook)
    ops = diff_workbooks(Grid(workbook), after)
    kinds = {type(op) for op in ops if op.tab == "SUMMARY"}
    assert {WriteNumberFormats, SetColumnWidth, SetBanding} <= kinds
    assert summarize_ops(Grid(workbook), ops)["SUMMARY"].presentation > 0
    assert diff_workbooks(Grid(after), after) == [], "an organised sheet diffs to nothing"

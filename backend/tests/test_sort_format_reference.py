"""Legacy.gs semantics on the reference workbook: stage sort, overdue tint, freeze cell."""

from __future__ import annotations

from app.schemas.config import ConfigSpec
from app.services.conditions import EvalContext
from app.services.executor import execute_run
from app.services.grid import CellFormat, Workbook
from app.services.runner import RunEvent
from tests.conftest import column
from tests.fixtures.reference_workbook import MACHINES_SORTED_SR

RED, GREEN, GOLD, BLACK, PINK = "#FF0000", "#008000", "#D4A017", "#000000", "#FCE4EC"


def _run(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> Workbook:
    result = execute_run(config, workbook, RunEvent.manual(), ctx)
    assert result.plan.status == "OK", result.plan
    return result.workbook


def test_stage_sort_then_date_blanks_last(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    out = _run(config, workbook, ctx)
    assert column(out, "MACHINES", "SR NO") == MACHINES_SORTED_SR


def test_rows_move_intact(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    before = {tuple(r) for r in workbook.tab("MACHINES").values[2:]}  # type: ignore[union-attr]
    out = _run(config, workbook, ctx)
    after = {tuple(r) for r in out.tab("MACHINES").values[2:]}  # type: ignore[union-attr]
    assert before == after
    assert out.tab("MACHINES").values[:2] == workbook.tab("MACHINES").values[:2]  # type: ignore[union-attr]


def test_spares_blank_row_sinks_and_nothing_dropped(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    out = _run(config, workbook, ctx)
    assert column(out, "SPARES", "SR NO") == ["S2", "S1", "S3", ""]


def _fmt_by_sr(wb: Workbook) -> dict[object, list[CellFormat]]:
    t = wb.tab("MACHINES")
    assert t is not None
    return {t.values[i][0]: t.formats[i] for i in range(2, t.height)}


def test_status_fonts_overdue_tint_and_freeze(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    fmts = _fmt_by_sr(_run(config, workbook, ctx))
    freeze_col = 6

    def row_font(sr: int) -> set[str | None]:
        return {f.font for i, f in enumerate(fmts[sr]) if i != freeze_col}

    # overdue In-Process: black text on pink, no strike
    for sr in (2, 10):
        assert row_font(sr) == {BLACK}
        assert {f.background for f in fmts[sr]} == {PINK}
    # future / blank-date In-Process: red, no fill
    for sr in (3, 4):
        assert row_font(sr) == {RED}
        assert {f.background for f in fmts[sr]} == {None}
    assert row_font(1) == {GREEN}
    assert row_font(8) == {GOLD}
    assert row_font(7) == {BLACK}  # DISPATCHED: neutral black
    assert {f.strike for f in fmts[5]} == {True} and row_font(5) == {BLACK}
    # unknown + blank status: neutral
    for sr in (6, 9):
        assert set(fmts[sr]) == {CellFormat(BLACK, None, False)}
    # freeze = YES (any case) -> only that cell's text goes green, fill untouched
    for sr in (1, 4, 7):
        assert fmts[sr][freeze_col].font == GREEN
    assert fmts[1][freeze_col].background is None
    assert fmts[2][freeze_col].font == BLACK  # "NO"


def test_frozen_overdue_row_keeps_pink_fill_on_freeze_cell(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    out = _run(config, workbook, ctx)
    t = out.tab("SPARES")
    assert t is not None
    s2 = next(i for i in range(t.height) if t.values[i][0] == "S2")
    assert t.formats[s2][5] == CellFormat(GREEN, PINK, False)
    assert t.formats[s2][0] == CellFormat(BLACK, PINK, False)


def test_formats_do_not_travel_with_values(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    out = _run(config, workbook, ctx)
    # title/header rows untouched
    t = out.tab("MACHINES")
    assert t is not None
    assert all(f == CellFormat() for f in t.formats[0] + t.formats[1])


def test_legends_untouched(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    out = _run(config, workbook, ctx)
    assert out.tab("LEGENDS") == workbook.tab("LEGENDS")


def test_rerun_is_idempotent(config: ConfigSpec, workbook: Workbook, ctx: EvalContext) -> None:
    once = _run(config, workbook, ctx)
    twice = execute_run(config, once, RunEvent.manual(), ctx)
    assert twice.workbook == once
    assert all(r.rows_affected == 0 for r in twice.records)

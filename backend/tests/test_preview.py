"""Preview (PATCH-003 B1) agrees with the write plan: a changed row is shown iff the run would change it."""

from __future__ import annotations

from datetime import date

from app.adapters.base import Grid
from app.services.grid import CellFormat, Tab, Workbook, date_serial
from app.services.preview import changed_rows


def test_date_vs_serial_and_white_vs_no_fill_are_not_changes() -> None:
    d = date(2026, 3, 1)
    before = Tab("S", [["TITLE"], ["H", "I"], [d, "x"]], formats=[[], [], [CellFormat(background="#FFFFFF")]])
    after = Tab("S", [["TITLE"], ["H", "I"], [int(date_serial(d)), "x"]])
    assert changed_rows(before, after, header_row=2, limit=20) == ([], 0)


def test_title_row_change_is_shown_and_labelled() -> None:
    before = Tab("S", [[""], ["H"], ["x"]])
    after = Tab("S", [["Q3 ORDERS"], ["H"], ["x"]], formats=[[CellFormat(font="#FFFFFF", background="#38761D")]])
    samples, total = changed_rows(before, after, header_row=2, limit=20)
    assert total == 1 and samples[0]["row"] == 1 and samples[0]["kind"] == "title" and samples[0]["changed"] == [0]


def test_preview_changes_match_write_ops() -> None:
    from app.services.ops import diff_workbooks

    before = Workbook([Tab("S", [["T"], ["H", "I"], ["b", 1], ["a", 2]])])
    after = Workbook([Tab("S", [["T"], ["H", "I"], ["a", 2], ["b", 1]])])
    samples, total = changed_rows(before.tab("S"), after.tab("S"), header_row=2, limit=20)  # type: ignore[arg-type]
    ops = diff_workbooks(Grid(before), after)
    assert total == 2 and [s["row"] for s in samples] == [3, 4] and len(ops) == 1

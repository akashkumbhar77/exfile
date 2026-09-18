"""move / copy / dedupe / clear / validate / sort options against small fixture grids."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.services.conditions import EvalContext
from app.services.executor import execute_run
from app.services.grid import CellFormat, DataValidation, Tab, Workbook
from app.services.runner import RunEvent
from tests.conftest import make_config

EDIT: dict[str, Any] = {"on_edit": {}}


def _wb() -> Workbook:
    return Workbook([
        Tab("OPEN", [["t"], ["ID", "STATUS", "DISPATCH DATE", "NOTE", "!hold"],
                     ["A1", "In Process", date(2026, 9, 1), "n1", ""],
                     ["A2", "Cancelled", date(2026, 9, 2), "n2", ""],
                     ["A3", "Cancelled", "", "", "x"],  # held
                     ["A4", "cancelled", date(2026, 9, 4), "n4", False],
                     ["A1", "Completed", "", "dup", ""]]),
        Tab("ARCHIVE", [["t"], ["ID", "STATUS", "TENTATIVE DISPATCH DATE", "NOTE"],
                        ["Z9", "Cancelled", "", "old"]]),
    ])


def _run(rules: list[dict[str, Any]], wb: Workbook, ctx: EvalContext, **extra: Any) -> Any:
    cfg = make_config(wb, ["OPEN", "ARCHIVE"], rules, **extra)
    return execute_run(cfg, wb, RunEvent.manual(), ctx)


def _ids(wb: Workbook, tab: str) -> list[object]:
    t = wb.tab(tab)
    assert t is not None
    return [r[0] for r in t.values[2:]]


CANCELLED = {"enum": "STATUS", "is": "CANCELLED"}


def test_move_rows_to_bottom_aligned_by_canonical_header(ctx: EvalContext) -> None:
    wb = _wb()
    r = _run([{"id": "archive", "tabs": ["OPEN"], "trigger": EDIT, "action": "move",
               "when": CANCELLED, "to_tab": "ARCHIVE"}], wb, ctx)
    assert r.plan.status == "OK"
    assert _ids(r.workbook, "OPEN") == ["A1", "A3", "A1"]  # held A3 stays
    assert _ids(r.workbook, "ARCHIVE") == ["Z9", "A2", "A4"]
    archive = r.workbook.tab("ARCHIVE")
    assert archive is not None
    assert archive.values[3] == ["A2", "Cancelled", date(2026, 9, 2), "n2"]
    assert {s.tab for s in r.snapshots} == {"OPEN", "ARCHIVE"}
    assert r.records[0].rows_affected == 4  # 2 removed + 2 added


def test_move_to_top(ctx: EvalContext) -> None:
    r = _run([{"id": "archive", "tabs": ["OPEN"], "trigger": EDIT, "action": "move",
               "when": CANCELLED, "to_tab": "ARCHIVE", "position": "top"}], _wb(), ctx)
    assert _ids(r.workbook, "ARCHIVE") == ["A2", "A4", "Z9"]


def test_move_aborts_when_target_lacks_a_column(ctx: EvalContext) -> None:
    wb = _wb()
    wb.tabs[1] = Tab("ARCHIVE", [["t"], ["ID", "STATUS"]])
    r = _run([{"id": "archive", "tabs": ["OPEN"], "trigger": EDIT, "action": "move",
               "when": CANCELLED, "to_tab": "ARCHIVE"}], wb, ctx)
    assert r.plan.status == "ERROR"
    assert "refusing to drop data" in (r.records[0].error or "")
    assert r.workbook == wb and not r.snapshots


def test_copy_is_idempotent(ctx: EvalContext) -> None:
    rules = [{"id": "mirror", "tabs": ["OPEN"], "trigger": EDIT, "action": "copy",
              "when": {"column": "STATUS", "contains": "CANCEL"}, "to_tab": "ARCHIVE", "key_columns": ["ID"]}]
    first = _run(rules, _wb(), ctx)
    assert _ids(first.workbook, "OPEN") == ["A1", "A2", "A3", "A4", "A1"]
    assert _ids(first.workbook, "ARCHIVE") == ["Z9", "A2", "A4"]
    cfg = make_config(_wb(), ["OPEN", "ARCHIVE"], rules)
    second = execute_run(cfg, first.workbook, RunEvent.manual(), ctx)
    assert second.workbook == first.workbook
    assert second.records[0].rows_affected == 0


def test_dedupe_keeps_first_skips_held_and_blank_keys(ctx: EvalContext) -> None:
    wb = _wb()
    open_tab = wb.tab("OPEN")
    assert open_tab is not None
    open_tab.values.append(["", "x", "", "", ""])
    open_tab.values.append(["", "y", "", "", ""])
    open_tab.values.append(["A3", "dup of held", "", "", ""])
    open_tab.__post_init__()
    r = _run([{"id": "dd", "tabs": ["OPEN"], "trigger": EDIT, "action": "dedupe", "key_columns": ["ID"]}], wb, ctx)
    assert _ids(r.workbook, "OPEN") == ["A1", "A2", "A3", "A4", "", "", "A3"]


def test_dedupe_keep_last(ctx: EvalContext) -> None:
    r = _run([{"id": "dd", "tabs": ["OPEN"], "trigger": EDIT, "action": "dedupe",
               "key_columns": ["ID"], "keep": "last"}], _wb(), ctx)
    t = r.workbook.tab("OPEN")
    assert t is not None
    assert [row[3] for row in t.values[2:]] == ["n2", "", "n4", "dup"]


def test_dedupe_shifts_formats_with_deleted_rows(ctx: EvalContext) -> None:
    wb = _wb()
    t = wb.tab("OPEN")
    assert t is not None
    t.formats[6] = [CellFormat(font="#123456")] * 5
    r = _run([{"id": "dd", "tabs": ["OPEN"], "trigger": EDIT, "action": "dedupe",
               "key_columns": ["ID"], "keep": "last"}], wb, ctx)
    out = r.workbook.tab("OPEN")
    assert out is not None and out.formats[5][0].font == "#123456"


def test_clear_blanks_columns_only_in_matching_rows(ctx: EvalContext) -> None:
    r = _run([{"id": "wipe", "tabs": "all_with:STATUS", "trigger": EDIT, "action": "clear",
               "when": CANCELLED, "columns": ["DISPATCH DATE"]}], _wb(), ctx)
    t = r.workbook.tab("OPEN")
    assert t is not None
    assert [row[2] for row in t.values[2:]] == [date(2026, 9, 1), None, "", None, ""]
    assert len(t.values) == 7  # rows kept
    assert r.records[0].rows_affected == 2  # ARCHIVE's Z9 had no date to clear


def test_validate_attaches_dropdown_except_held_rows(ctx: EvalContext) -> None:
    r = _run([{"id": "dd", "tabs": ["OPEN"], "trigger": EDIT, "action": "validate",
               "column": "STATUS", "from_enum": "STATUS"}], _wb(), ctx)
    t = r.workbook.tab("OPEN")
    assert t is not None
    dv = DataValidation(("IN-PROCESS", "COMPLETED", "DISPUTED", "DISPATCHED", "CANCELLED"), False)
    assert t.validations == {(3, 2): dv, (4, 2): dv, (6, 2): dv, (7, 2): dv}
    assert t.values == _wb().tab("OPEN").values  # type: ignore[union-attr]
    # dropdown matching is exact: "In Process" is not the stage value "IN-PROCESS"
    assert r.plan.plans[0].warnings == ["tab 'OPEN': 1 existing value(s) in 'STATUS' not in list (left as-is)"]


def test_validate_reports_but_keeps_unlisted_values(ctx: EvalContext) -> None:
    r = _run([{"id": "dd", "tabs": ["OPEN"], "trigger": EDIT, "action": "validate",
               "column": "STATUS", "values": ["In Process"]}], _wb(), ctx)
    assert r.plan.status == "OK"
    assert any("3 existing value(s)" in w for w in r.plan.plans[0].warnings)


def test_sort_desc_text_and_numbers(ctx: EvalContext) -> None:
    wb = Workbook([Tab("OPEN", [["t"], ["ID", "STATUS", "QTY"],
                                ["a", "x", 3], ["b", "x", ""], ["c", "x", 10], ["d", "x", "7"]])])
    cfg = make_config(wb, ["OPEN"], [{"id": "s", "tabs": ["OPEN"], "trigger": EDIT, "action": "sort",
                                      "keys": [{"column": "QTY", "order": "desc", "type": "number"}]}],
                      canonical_headers=[{"canonical": "QTY", "match": ["equals:QTY"]},
                                         {"canonical": "STATUS", "match": ["equals:STATUS"]}])
    out = execute_run(cfg, wb, RunEvent.manual(), ctx).workbook
    assert _ids(out, "OPEN") == ["c", "d", "a", "b"]


def test_sort_blanks_first(ctx: EvalContext) -> None:
    wb = Workbook([Tab("OPEN", [["t"], ["ID", "DISPATCH DATE"],
                                ["a", date(2026, 1, 2)], ["b", ""], ["c", date(2026, 1, 1)]])])
    cfg = make_config(wb, ["OPEN"], [{"id": "s", "tabs": ["OPEN"], "trigger": EDIT, "action": "sort",
                                      "keys": [{"column": "DISPATCH DATE", "blanks": "first"}]}])
    assert _ids(execute_run(cfg, wb, RunEvent.manual(), ctx).workbook, "OPEN") == ["b", "c", "a"]


def test_hold_rows_keep_position_and_format(ctx: EvalContext, workbook: Workbook) -> None:
    wb = Workbook([Tab("OPEN", [["t"], ["ID", "STATUS", "!HOLD"],
                                ["a", "Cancelled", ""], ["b", "Cancelled", True],
                                ["c", "In Process", "no"], ["d", "Completed", 0]])])
    wb.tabs[0].formats[3] = [CellFormat("#ABCDEF")] * 3
    cfg = make_config(wb, ["OPEN"], [
        {"id": "s", "tabs": ["OPEN"], "trigger": EDIT, "action": "sort",
         "keys": [{"column": "STATUS", "using_enum": "STATUS"}]},
        {"id": "f", "tabs": ["OPEN"], "trigger": {"after": "s"}, "action": "format",
         "row_rules": [{"when": {"enum": "STATUS", "is": "CANCELLED"}, "strike": True}]},
    ])
    out = execute_run(cfg, wb, RunEvent.manual(), ctx).workbook
    assert _ids(out, "OPEN") == ["c", "b", "d", "a"]
    t = out.tab("OPEN")
    assert t is not None
    assert t.formats[3] == [CellFormat("#ABCDEF")] * 3  # held row untouched
    assert t.formats[5][0] == CellFormat("#000000", None, True)


def test_unknown_enum_values_sink_neutral_never_dropped(ctx: EvalContext) -> None:
    wb = Workbook([Tab("OPEN", [["t"], ["ID", "STATUS"],
                                ["a", "BRAND NEW STATE"], ["b", "Completed"], ["c", ""], ["d", "In Process"]])])
    wb.tabs[0].formats[2] = [CellFormat("#FF0000", "#00FF00", True)] * 2
    cfg = make_config(wb, ["OPEN"], [
        {"id": "s", "tabs": ["OPEN"], "trigger": EDIT, "action": "sort",
         "keys": [{"column": "STATUS", "using_enum": "STATUS"}]},
        {"id": "f", "tabs": ["OPEN"], "trigger": {"after": "s"}, "action": "format",
         "row_rules": [{"when": {"enum": "STATUS", "is": "COMPLETED"}, "font": "#008000"}]},
    ])
    out = execute_run(cfg, wb, RunEvent.manual(), ctx).workbook
    assert _ids(out, "OPEN") == ["d", "b", "a", "c"]
    t = out.tab("OPEN")
    assert t is not None
    assert t.formats[4] == [CellFormat("#000000", None, False)] * 2

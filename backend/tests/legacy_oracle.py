"""Line-by-line Python port of engine/reference/legacy.gs (sortSheet, applyStatusFormatting,
assembleConsolidated, writeConsolidated sort+format). Used only as a test oracle.

Deliberately NOT sharing code with app/: parity tests compare two independent implementations.
Known legacy quirks kept as-is: first/last duplicate-header choice uses the LAST match,
and a tab with a single data row is neither sorted nor formatted.
"""

from __future__ import annotations

import math
from datetime import date, datetime

from app.services.grid import CellFormat, Tab, Workbook

HEADER_ROW = 2
DATA_START_ROW = 3
STAGE_FORMAT = {
    1: ("#FF0000", False), 2: ("#008000", False), 3: ("#D4A017", False),
    4: ("#000000", False), 5: ("#000000", True), 6: ("#000000", False),
}
FREEZE_YES_TEXT = "#008000"
OVERDUE_BG = "#FCE4EC"
OVERDUE_FONT = "#000000"
SUMMARY_SHEET = "SUMMARY"


def js_str(v: object) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


def blank(v: object) -> bool:
    return v == "" or v is None


def status_stage(value: object) -> int:
    u = ("" if value is None else js_str(value)).strip().upper()
    if "PROCESS" in u:
        return 1
    if "COMPLET" in u:
        return 2
    if "DISPUT" in u:
        return 3
    if "DISPATCH" in u:
        return 4
    if "CANCEL" in u:
        return 5
    return 6


def to_ms(v: object) -> float:
    if isinstance(v, datetime):
        return (v - datetime(1970, 1, 1)).total_seconds() * 1000
    if isinstance(v, date):
        return (datetime(v.year, v.month, v.day) - datetime(1970, 1, 1)).total_seconds() * 1000
    return math.nan


def date_ms(value: object) -> float:
    t = to_ms(value) if not blank(value) else math.nan
    return math.inf if math.isnan(t) else t


def last_row(tab: Tab) -> int:
    return tab.last_content_row()


def detect(headers: list[object]) -> tuple[int, int, int]:
    status = dispatch = freeze = 0
    for c, raw in enumerate(headers):
        h = ("" if raw is None else js_str(raw)).strip().upper()
        if h == "STATUS":
            status = c + 1
        if "DISPATCH" in h:
            dispatch = c + 1
        if "FREEZE" in h:
            freeze = c + 1
    return status, dispatch, freeze


def sort_sheet(tab: Tab, today_ms: float) -> None:
    lc, lr = tab.width, last_row(tab)
    if lc < 1 or lr <= DATA_START_ROW:
        return
    status, dispatch, freeze = detect(tab.values[HEADER_ROW - 1])
    if status == 0:
        return
    data = [list(r) for r in tab.values[DATA_START_ROW - 1: lr]]
    indexed = list(enumerate(data))

    def key(item: tuple[int, list[object]]) -> tuple[int, float, int]:
        i, row = item
        d = date_ms(row[dispatch - 1]) if dispatch > 0 else 0.0
        return (status_stage(row[status - 1]), d, i)

    indexed.sort(key=key)
    data = [r for _, r in indexed]
    tab.values[DATA_START_ROW - 1: lr] = data
    apply_status_formatting(tab, data, status, dispatch, freeze, lc, DATA_START_ROW, today_ms)


def apply_status_formatting(tab: Tab, data: list[list[object]], status: int, dispatch: int, freeze: int,
                            lc: int, start: int, today_ms: float) -> None:
    for i, row in enumerate(data):
        stage = status_stage(row[status - 1])
        color, strike = STAGE_FORMAT.get(stage, STAGE_FORMAT[6])
        frozen = freeze > 0 and ("" if row[freeze - 1] is None else js_str(row[freeze - 1])).strip().upper() == "YES"
        overdue = False
        if stage == 1 and dispatch > 0:
            t = to_ms(row[dispatch - 1])
            if not math.isnan(t) and t < today_ms:
                overdue = True
        bg = OVERDUE_BG if overdue else None
        font = OVERDUE_FONT if overdue else color
        fmts = []
        for c in range(lc):
            f = FREEZE_YES_TEXT if (frozen and c == freeze - 1) else font
            fmts.append(CellFormat(font=f, background=bg, strike=strike))
        tab.formats[start - 1 + i] = fmts


def canonical_header(raw: object) -> str:
    key = js_str(raw).strip()
    u = key.upper()
    if "DISPATCH" in u:
        return "DISPATCH DATE"
    if "FREEZE" in u:
        return "FREEZE?"
    return key


def category_sheets(wb: Workbook) -> list[Tab]:
    out = []
    for t in wb.tabs:
        if t.name == SUMMARY_SHEET or t.name.upper() == "LEGENDS" or t.width < 1:
            continue
        if any(("" if h is None else js_str(h)).strip().upper() == "STATUS" for h in t.row(HEADER_ROW)):
            out.append(t)
    return out


def assemble(wb: Workbook) -> tuple[list[str], list[list[object]]]:
    cats = category_sheets(wb)
    master: list[str] = []
    pos: dict[str, int] = {}
    for c in cats:
        for h in c.row(HEADER_ROW):
            if blank(h) or js_str(h).strip() == "":
                continue
            canon = canonical_header(h)
            u = canon.upper()
            if u not in pos:
                pos[u] = len(master)
                master.append(canon)
    if "TYPE OF WORK" not in pos:
        pos["TYPE OF WORK"] = len(master)
        master.append("Type of Work")
    tow = pos["TYPE OF WORK"]
    rows: list[list[object]] = []
    for c in cats:
        lr = last_row(c)
        if lr < DATA_START_ROW:
            continue
        hdrs = [canonical_header("" if h is None else h).upper() for h in c.row(HEADER_ROW)]
        for row in c.values[DATA_START_ROW - 1: lr]:
            if all(blank(v) for v in row):
                continue
            out: list[object] = [""] * len(master)
            for i, hk in enumerate(hdrs):
                p = pos.get(hk)
                if p is not None and blank(out[p]):
                    out[p] = row[i]
            if blank(out[tow]):
                out[tow] = c.name.strip()
            rows.append([c.name, *out])
    return ["SOURCE SHEET", *master], rows


def summary(wb: Workbook, today_ms: float) -> tuple[list[str], list[list[object]], list[list[CellFormat]]]:
    headers, rows = assemble(wb)
    status, dispatch, freeze = detect(list(headers))

    def key(row: list[object]) -> tuple[int, float]:
        sa = status_stage(row[status - 1]) if status else 0
        d = date_ms(row[dispatch - 1]) if dispatch else 0.0
        return (sa, d)

    rows.sort(key=key)
    tmp = Tab("tmp", [[None] * len(headers)] * 2 + [list(r) for r in rows])
    if status and rows:
        apply_status_formatting(tmp, rows, status, dispatch, freeze, len(headers), 3, today_ms)
    return headers, rows, tmp.formats[2:]


def today_ms(d: date) -> float:
    return to_ms(d)

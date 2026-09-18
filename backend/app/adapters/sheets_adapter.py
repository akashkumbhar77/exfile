"""Google Sheets adapter (SPEC-PATCH-001 S1).

read_grid : one `spreadsheets.get` (grid data, field-masked) -> Grid
write_ops : one metadata `spreadsheets.get` + one `spreadsheets.batchUpdate`.
            batchUpdate is atomic, so a run is either fully applied or not at all
            (invariant 5).

Privacy/scope (B.6, B.9): every call is refused unless the spreadsheet id is in
the registry; this module never calls Drive listing/search. Raw API payloads
stay local to this module and are never logged.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from app.adapters.base import (
    AdapterCaps,
    AddTab,
    ChangeInfo,
    DeleteTab,
    EnsureSize,
    Grid,
    GridOp,
    SetProtection,
    SetValidation,
    SourceRef,
    SourceRegistry,
    TabMeta,
    UnregisteredSource,
    WriteFormats,
    WriteResult,
    WriteValues,
)
from app.services.grid import (
    CellFormat,
    CellValue,
    DataValidation,
    Tab,
    Workbook,
    date_serial,
    is_empty,
    row_is_empty,
)

SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
LOCK_DESCRIPTION = "sheets-automation: generated tab (locked)"
_EPOCH = datetime(1899, 12, 30)
_DATE_TYPES = {"DATE", "DATE_TIME"}

READ_FIELDS = (
    "properties.timeZone,"
    "sheets(properties(sheetId,title,index,gridProperties(rowCount,columnCount)),"
    "protectedRanges(protectedRangeId,range,description),"
    "data(rowData(values(effectiveValue,formattedValue,effectiveFormat.numberFormat,"
    "userEnteredFormat(backgroundColor,backgroundColorStyle,"
    "textFormat(foregroundColor,foregroundColorStyle,strikethrough)),dataValidation))))"
)
META_FIELDS = (
    "sheets(properties(sheetId,title,index,gridProperties(rowCount,columnCount)),"
    "protectedRanges(protectedRangeId,range))"
)
_FORMAT_FIELDS = (
    "userEnteredFormat.backgroundColorStyle,"
    "userEnteredFormat.textFormat.foregroundColorStyle,"
    "userEnteredFormat.textFormat.strikethrough"
)


# ---- value / colour conversion -------------------------------------------------------------


def serial_to_datetime(serial: float) -> datetime:
    return _EPOCH + timedelta(days=serial)


date_to_serial = date_serial


def _color_hex(color_style: Mapping[str, Any] | None, legacy: Mapping[str, Any] | None) -> str | None:
    rgb: Mapping[str, Any] | None = None
    if color_style is not None:
        rgb = color_style.get("rgbColor")
        if rgb is None:
            return None  # theme colour: not representable in config styles; treated as unset
    elif legacy is not None:
        rgb = legacy
    if rgb is None:
        return None
    parts = (round(float(rgb.get(k, 0.0)) * 255) for k in ("red", "green", "blue"))
    return "#" + "".join(f"{p:02X}" for p in parts)


def _rgb(hex_color: str) -> dict[str, float]:
    h = hex_color.lstrip("#")
    return {k: int(h[i : i + 2], 16) / 255 for k, i in (("red", 0), ("green", 2), ("blue", 4))}


def _cell_value(cell: Mapping[str, Any]) -> CellValue:
    ev = cell.get("effectiveValue")
    if not ev:
        return None
    if "stringValue" in ev:
        return str(ev["stringValue"])
    if "boolValue" in ev:
        return bool(ev["boolValue"])
    if "numberValue" in ev:
        n = float(ev["numberValue"])
        ntype = cell.get("effectiveFormat", {}).get("numberFormat", {}).get("type")
        if ntype in _DATE_TYPES:
            try:
                dt = serial_to_datetime(n)
            except (OverflowError, ValueError):
                dt = None  # date-formatted cell holding a number outside the calendar: keep the number
            if dt is not None:
                return dt.date() if ntype == "DATE" else dt
        return int(n) if math.isfinite(n) and n.is_integer() else n
    if "errorValue" in ev:
        return str(cell.get("formattedValue", "#ERROR!"))
    return None


def _cell_format(cell: Mapping[str, Any]) -> CellFormat:
    uf = cell.get("userEnteredFormat") or {}
    tf = uf.get("textFormat") or {}
    bg = _color_hex(uf.get("backgroundColorStyle"), uf.get("backgroundColor"))
    if bg == "#FFFFFF":
        bg = None  # M2 decision: white fill reads the same as no fill
    return CellFormat(
        font=_color_hex(tf.get("foregroundColorStyle"), tf.get("foregroundColor")),
        background=bg,
        strike=bool(tf.get("strikethrough", False)),
    )


def _validation(cell: Mapping[str, Any]) -> DataValidation | None:
    dv = cell.get("dataValidation")
    if not dv or dv.get("condition", {}).get("type") != "ONE_OF_LIST":
        return None
    values = tuple(str(v.get("userEnteredValue", "")) for v in dv["condition"].get("values", []))
    return DataValidation(values, allow_invalid=not dv.get("strict", False))


def _whole_sheet(rng: Mapping[str, Any]) -> bool:
    return not any(k in rng for k in ("startRowIndex", "endRowIndex", "startColumnIndex", "endColumnIndex"))


def parse_sheet(sheet: Mapping[str, Any]) -> tuple[Tab, TabMeta]:
    """One API sheet -> Tab trimmed to its data range (like Apps Script getDataRange)."""
    name = sheet["properties"]["title"]
    raw_rows: list[list[Mapping[str, Any]]] = []
    for block in sheet.get("data", []):
        for rd in block.get("rowData", []):
            raw_rows.append(list(rd.get("values", [])))

    values = [[_cell_value(c) for c in r] for r in raw_rows]
    height = 0
    for i in range(len(values) - 1, -1, -1):
        if not row_is_empty(values[i]):
            height = i + 1
            break
    width = max(
        (max((j + 1 for j, v in enumerate(r) if not is_empty(v)), default=0) for r in values[:height]),
        default=0,
    )
    grid_vals = [(values[i] + [None] * width)[:width] for i in range(height)]
    formats = [
        [_cell_format(raw_rows[i][j]) if j < len(raw_rows[i]) else CellFormat() for j in range(width)]
        for i in range(height)
    ]
    validations: dict[tuple[int, int], DataValidation] = {}
    patterns: dict[int, str] = {}
    for i in range(height):
        for j, cell in enumerate(raw_rows[i][:width]):
            dv = _validation(cell)
            if dv is not None:
                validations[(i + 1, j + 1)] = dv
            nf = cell.get("effectiveFormat", {}).get("numberFormat", {})
            if nf.get("type") in _DATE_TYPES and nf.get("pattern") and j not in patterns:
                patterns[j] = str(nf["pattern"])
    protected = any(_whole_sheet(p.get("range", {})) for p in sheet.get("protectedRanges", []))
    tab = Tab(name, grid_vals, formats, validations, protected)
    return tab, TabMeta(date_patterns=patterns)


# ---- adapter -----------------------------------------------------------------------------


class SheetsAdapter:
    def __init__(self, service: Any, registry: SourceRegistry, editor_email: str | None = None) -> None:
        self._svc = service
        self._registry = registry
        self._editor = editor_email

    @classmethod
    def from_service_account(cls, key_path: str, registry: SourceRegistry) -> SheetsAdapter:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(key_path, scopes=list(SCOPES))  # type: ignore[no-untyped-call]
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return cls(service, registry, editor_email=creds.service_account_email)

    def _guard(self, source_ref: SourceRef) -> None:
        if not self._registry.is_registered(source_ref):
            raise UnregisteredSource(f"spreadsheet {source_ref!r} is not in the registry; refusing to open it")

    def capabilities(self) -> AdapterCaps:
        return AdapterCaps(realtime=False, formats=("font", "background", "strike"), validations=True, protection=True)

    def read_grid(self, source_ref: SourceRef) -> Grid:
        self._guard(source_ref)
        resp = self._svc.spreadsheets().get(
            spreadsheetId=source_ref, includeGridData=True, fields=READ_FIELDS
        ).execute()
        tabs: list[Tab] = []
        metas: dict[str, TabMeta] = {}
        for sheet in sorted(resp.get("sheets", []), key=lambda s: s["properties"].get("index", 0)):
            tab, meta = parse_sheet(sheet)
            tabs.append(tab)
            metas[tab.name] = meta
        tz = resp.get("properties", {}).get("timeZone") or "UTC"
        return Grid(Workbook(tabs), timezone=tz, tabs=metas)

    def detect_changes(self, source_ref: SourceRef) -> ChangeInfo:
        # SPEC-PATCH-002 A: detection is the fleet-level Drive changes feed (S2), not per sheet.
        raise NotImplementedError("change detection is the S2 fleet watcher")

    def write_ops(self, source_ref: SourceRef, ops: list[GridOp]) -> WriteResult:
        self._guard(source_ref)
        if not ops:
            return WriteResult(0, 0, 0)
        meta = self._svc.spreadsheets().get(spreadsheetId=source_ref, fields=META_FIELDS).execute()
        sheets = {s["properties"]["title"]: s for s in meta.get("sheets", [])}
        ids: dict[str, int] = {t: int(s["properties"]["sheetId"]) for t, s in sheets.items()}
        requests, cells, fmts = self._requests(ops, sheets, ids)
        self._svc.spreadsheets().batchUpdate(spreadsheetId=source_ref, body={"requests": requests}).execute()
        return WriteResult(len(requests), cells, fmts)

    # -- request building --

    def _requests(
        self, ops: list[GridOp], sheets: Mapping[str, Mapping[str, Any]], ids: dict[str, int]
    ) -> tuple[list[dict[str, Any]], int, int]:
        reqs: list[dict[str, Any]] = []
        cells = fmts = 0
        used = set(ids.values())

        def grid_size(tab: str) -> tuple[int, int]:
            gp = sheets[tab]["properties"].get("gridProperties", {}) if tab in sheets else {}
            return int(gp.get("rowCount", 0)), int(gp.get("columnCount", 0))

        sizes = {t: grid_size(t) for t in sheets}
        for op in ops:
            match op:
                case AddTab():
                    new_id = random.randint(1, 2**31 - 1)
                    while new_id in used:
                        new_id = random.randint(1, 2**31 - 1)
                    used.add(new_id)
                    ids[op.tab] = new_id
                    sizes[op.tab] = (op.rows, op.cols)
                    reqs.append({"addSheet": {"properties": {
                        "sheetId": new_id, "title": op.tab, "index": op.index,
                        "gridProperties": {"rowCount": op.rows, "columnCount": op.cols},
                    }}})
                case EnsureSize():
                    rows, cols = sizes.get(op.tab, (0, 0))
                    if op.rows > rows:
                        reqs.append({"appendDimension": {"sheetId": ids[op.tab], "dimension": "ROWS",
                                                         "length": op.rows - rows}})
                    if op.cols > cols:
                        reqs.append({"appendDimension": {"sheetId": ids[op.tab], "dimension": "COLUMNS",
                                                         "length": op.cols - cols}})
                    sizes[op.tab] = (max(rows, op.rows), max(cols, op.cols))
                case WriteValues():
                    reqs.extend(self._value_requests(op, ids[op.tab]))
                    cells += op.rows * op.cols
                case WriteFormats():
                    reqs.append({"updateCells": {
                        "start": {"sheetId": ids[op.tab], "rowIndex": op.row - 1, "columnIndex": op.col - 1},
                        "rows": [{"values": [_format_cell(f) for f in row]} for row in op.formats],
                        "fields": _FORMAT_FIELDS,
                    }})
                    fmts += sum(len(r) for r in op.formats)
                case SetValidation():
                    rng = {"sheetId": ids[op.tab], "startRowIndex": op.row - 1, "endRowIndex": op.row,
                           "startColumnIndex": op.col - 1, "endColumnIndex": op.col}
                    body: dict[str, Any] = {"range": rng}
                    if op.validation is not None:
                        body["rule"] = {
                            "condition": {"type": "ONE_OF_LIST",
                                          "values": [{"userEnteredValue": v} for v in op.validation.values]},
                            "strict": not op.validation.allow_invalid,
                            "showCustomUi": True,
                        }
                    reqs.append({"setDataValidation": body})
                case SetProtection():
                    if op.protected:
                        prot: dict[str, Any] = {"range": {"sheetId": ids[op.tab]},
                                                "description": LOCK_DESCRIPTION, "warningOnly": False}
                        if self._editor:
                            prot["editors"] = {"users": [self._editor]}
                        reqs.append({"addProtectedRange": {"protectedRange": prot}})
                    else:
                        for p in sheets.get(op.tab, {}).get("protectedRanges", []):
                            if _whole_sheet(p.get("range", {})):
                                reqs.append({"deleteProtectedRange": {"protectedRangeId": p["protectedRangeId"]}})
                case DeleteTab():
                    reqs.append({"deleteSheet": {"sheetId": ids[op.tab]}})
        return reqs, cells, fmts

    @staticmethod
    def _value_requests(op: WriteValues, sheet_id: int) -> list[dict[str, Any]]:
        block = op.values.reveal()  # write path: the only place ops' values are unwrapped
        start = {"sheetId": sheet_id, "rowIndex": op.row - 1, "columnIndex": op.col - 1}
        out: list[dict[str, Any]] = [{"updateCells": {
            "start": start,
            "rows": [{"values": [_value_cell(v) for v in row]} for row in block],
            "fields": "userEnteredValue",
        }}]
        # Values move, formats stay (setValues semantics) -- except a date landing in a cell
        # needs a date number format to display as a date, as Apps Script's setValues does.
        # One repeatCell per run of same-kind date cells in a column.
        width = max((len(r) for r in block), default=0)
        for j in range(width):
            col = op.col - 1 + j
            pattern = op.date_patterns.get(col) or op.default_date_pattern
            kinds = [_date_kind(row[j] if j < len(row) else None) for row in block] + [None]
            run_start = 0
            for i in range(1, len(kinds)):
                if kinds[i] != kinds[run_start]:
                    kind = kinds[run_start]
                    if kind is not None:
                        nf: dict[str, Any] = {"type": kind}
                        if pattern:
                            nf["pattern"] = pattern
                        out.append({"repeatCell": {
                            "range": {"sheetId": sheet_id, "startRowIndex": op.row - 1 + run_start,
                                      "endRowIndex": op.row - 1 + i, "startColumnIndex": col,
                                      "endColumnIndex": col + 1},
                            "cell": {"userEnteredFormat": {"numberFormat": nf}},
                            "fields": "userEnteredFormat.numberFormat",
                        }})
                    run_start = i
        return out


def _date_kind(v: CellValue) -> str | None:
    if isinstance(v, datetime):
        return "DATE_TIME"
    if isinstance(v, date):
        return "DATE"
    return None


def _value_cell(v: CellValue) -> dict[str, Any]:
    if is_empty(v):
        return {}
    if isinstance(v, bool):
        return {"userEnteredValue": {"boolValue": v}}
    if isinstance(v, date):
        return {"userEnteredValue": {"numberValue": date_to_serial(v)}}
    if isinstance(v, (int, float)):
        return {"userEnteredValue": {"numberValue": v}}
    return {"userEnteredValue": {"stringValue": str(v)}}


def _format_cell(f: CellFormat) -> dict[str, Any]:
    tf: dict[str, Any] = {"strikethrough": f.strike}
    if f.font is not None:
        tf["foregroundColorStyle"] = {"rgbColor": _rgb(f.font)}
    uf: dict[str, Any] = {"textFormat": tf}
    if f.background is not None:
        uf["backgroundColorStyle"] = {"rgbColor": _rgb(f.background)}
    return {"userEnteredFormat": uf}

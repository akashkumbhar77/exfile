"""In-memory stand-in for the Sheets v4 API surface the adapter uses.

Implements `spreadsheets().get(...)` and `spreadsheets().batchUpdate(...)` with the
real API's semantics that matter here: field-masked updateCells/repeatCell,
atomic batchUpdate (all requests or none), grid bounds errors, colour objects
that omit zero components, and dates stored as serial numbers + number format.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.adapters.sheets_adapter import date_to_serial
from app.services.grid import CellFormat, Workbook, is_empty


class FakeHttpError(Exception):
    pass


def _rgb(hex_color: str) -> dict[str, float]:
    h = hex_color.lstrip("#")
    out = {k: int(h[i : i + 2], 16) / 255 for k, i in (("red", 0), ("green", 2), ("blue", 4))}
    return {k: v for k, v in out.items() if v}  # the API omits zero components


@dataclass
class FakeSheet:
    sheet_id: int
    title: str
    rows: int
    cols: int
    cells: dict[tuple[int, int], dict[str, Any]] = field(default_factory=dict)  # 0-based
    protected: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FakeSpreadsheet:
    time_zone: str = "Asia/Kolkata"
    sheets: list[FakeSheet] = field(default_factory=list)
    next_protected_id: int = 1000


class _Request:
    def __init__(self, fn: Any) -> None:
        self._fn = fn

    def execute(self) -> Any:
        return self._fn()


class FakeSheetsService:
    def __init__(self, spreadsheets: dict[str, FakeSpreadsheet]) -> None:
        self.docs = spreadsheets
        self.calls: list[tuple[str, str]] = []  # (method, spreadsheet id)
        self.fail_next_batch = False
        self.on_commit: Any = None  # FakeDriveService hook: a committed write bumps modifiedTime

    def spreadsheets(self) -> FakeSheetsService:
        return self

    # -- get --
    def get(self, spreadsheetId: str, includeGridData: bool = False, fields: str = "") -> _Request:
        return _Request(lambda: self._get(spreadsheetId, includeGridData))

    def _get(self, sid: str, grid: bool) -> dict[str, Any]:
        self.calls.append(("get", sid))
        doc = self.docs.get(sid)
        if doc is None:
            raise FakeHttpError("404: Requested entity was not found.")
        sheets = []
        for index, sh in enumerate(doc.sheets):
            entry: dict[str, Any] = {
                "properties": {"sheetId": sh.sheet_id, "title": sh.title, "index": index,
                               "gridProperties": {"rowCount": sh.rows, "columnCount": sh.cols}},
            }
            if sh.protected:
                entry["protectedRanges"] = copy.deepcopy(sh.protected)
            if grid:
                max_r = max((r for r, _ in sh.cells), default=-1)
                row_data = []
                for r in range(max_r + 1):
                    max_c = max((c for rr, c in sh.cells if rr == r), default=-1)
                    row_data.append({"values": [self._cell_out(sh.cells.get((r, c), {})) for c in range(max_c + 1)]})
                entry["data"] = [{"rowData": row_data}]
            sheets.append(entry)
        return {"properties": {"timeZone": doc.time_zone}, "sheets": sheets}

    @staticmethod
    def _cell_out(cell: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        uv = cell.get("userEnteredValue")
        if uv:
            out["effectiveValue"] = copy.deepcopy(uv)
            out["formattedValue"] = str(next(iter(uv.values())))
        uf = cell.get("userEnteredFormat")
        if uf:
            out["userEnteredFormat"] = copy.deepcopy(uf)
            if "numberFormat" in uf:
                out["effectiveFormat"] = {"numberFormat": copy.deepcopy(uf["numberFormat"])}
        if "dataValidation" in cell:
            out["dataValidation"] = copy.deepcopy(cell["dataValidation"])
        return out

    # -- batchUpdate --
    def batchUpdate(self, spreadsheetId: str, body: dict[str, Any]) -> _Request:
        return _Request(lambda: self._batch(spreadsheetId, body["requests"]))

    def _batch(self, sid: str, requests: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(("batchUpdate", sid))
        if self.fail_next_batch:
            self.fail_next_batch = False
            raise FakeHttpError("500: backend error")
        work = copy.deepcopy(self.docs[sid])
        for req in requests:
            (kind, arg), = req.items()
            getattr(self, "_" + kind)(work, arg)
        self.docs[sid] = work  # atomic: only now does the change become visible
        if self.on_commit is not None:
            self.on_commit(sid)
        return {"replies": [{} for _ in requests]}

    @staticmethod
    def _sheet(doc: FakeSpreadsheet, sheet_id: int) -> FakeSheet:
        for sh in doc.sheets:
            if sh.sheet_id == sheet_id:
                return sh
        raise FakeHttpError(f"400: No grid with id: {sheet_id}")

    def _addSheet(self, doc: FakeSpreadsheet, arg: dict[str, Any]) -> None:
        p = arg["properties"]
        if any(s.title == p["title"] for s in doc.sheets):
            raise FakeHttpError("400: duplicate sheet name")
        gp = p.get("gridProperties", {})
        sh = FakeSheet(p["sheetId"], p["title"], gp.get("rowCount", 1000), gp.get("columnCount", 26))
        doc.sheets.insert(p.get("index", len(doc.sheets)), sh)

    def _deleteSheet(self, doc: FakeSpreadsheet, arg: dict[str, Any]) -> None:
        sh = self._sheet(doc, arg["sheetId"])
        doc.sheets.remove(sh)

    def _appendDimension(self, doc: FakeSpreadsheet, arg: dict[str, Any]) -> None:
        sh = self._sheet(doc, arg["sheetId"])
        if arg["dimension"] == "ROWS":
            sh.rows += arg["length"]
        else:
            sh.cols += arg["length"]

    @staticmethod
    def _check_bounds(sh: FakeSheet, r: int, c: int) -> None:
        if r >= sh.rows or c >= sh.cols:
            raise FakeHttpError(f"400: Range ({sh.title}!R{r + 1}C{c + 1}) exceeds grid limits")

    @staticmethod
    def _apply_mask(cell: dict[str, Any], new: dict[str, Any], fields: str) -> None:
        for path in fields.split(","):
            parts = path.strip().split(".")
            src: Any = new
            for p in parts:
                src = src.get(p) if isinstance(src, dict) else None
            dst = cell
            for p in parts[:-1]:
                dst = dst.setdefault(p, {})
            if src is None:
                dst.pop(parts[-1], None)
            else:
                dst[parts[-1]] = copy.deepcopy(src)
            # the API keeps the legacy colour fields in sync with the *Style fields
            legacy = {"backgroundColorStyle": "backgroundColor", "foregroundColorStyle": "foregroundColor"}
            if parts[-1] in legacy:
                dst.pop(legacy[parts[-1]], None)

    def _updateCells(self, doc: FakeSpreadsheet, arg: dict[str, Any]) -> None:
        start = arg["start"]
        sh = self._sheet(doc, start["sheetId"])
        for i, row in enumerate(arg["rows"]):
            for j, new in enumerate(row.get("values", [])):
                r, c = start["rowIndex"] + i, start["columnIndex"] + j
                self._check_bounds(sh, r, c)
                cell = sh.cells.setdefault((r, c), {})
                self._apply_mask(cell, new, arg["fields"])
                if not any(cell.values()):
                    sh.cells.pop((r, c), None)

    def _repeatCell(self, doc: FakeSpreadsheet, arg: dict[str, Any]) -> None:
        rng = arg["range"]
        sh = self._sheet(doc, rng["sheetId"])
        for r in range(rng["startRowIndex"], rng["endRowIndex"]):
            for c in range(rng["startColumnIndex"], rng["endColumnIndex"]):
                self._check_bounds(sh, r, c)
                self._apply_mask(sh.cells.setdefault((r, c), {}), arg["cell"], arg["fields"])

    def _setDataValidation(self, doc: FakeSpreadsheet, arg: dict[str, Any]) -> None:
        rng = arg["range"]
        sh = self._sheet(doc, rng["sheetId"])
        for r in range(rng["startRowIndex"], rng["endRowIndex"]):
            for c in range(rng["startColumnIndex"], rng["endColumnIndex"]):
                self._check_bounds(sh, r, c)
                cell = sh.cells.setdefault((r, c), {})
                if "rule" in arg:
                    cell["dataValidation"] = copy.deepcopy(arg["rule"])
                else:
                    cell.pop("dataValidation", None)

    def _addProtectedRange(self, doc: FakeSpreadsheet, arg: dict[str, Any]) -> None:
        pr = copy.deepcopy(arg["protectedRange"])
        sh = self._sheet(doc, pr["range"]["sheetId"])
        pr["protectedRangeId"] = doc.next_protected_id
        doc.next_protected_id += 1
        sh.protected.append(pr)

    def _deleteProtectedRange(self, doc: FakeSpreadsheet, arg: dict[str, Any]) -> None:
        for sh in doc.sheets:
            sh.protected = [p for p in sh.protected if p["protectedRangeId"] != arg["protectedRangeId"]]


def seed(workbook: Workbook, time_zone: str = "Asia/Kolkata", date_pattern: str = "dd-mmm-yyyy") -> FakeSpreadsheet:
    """Load an in-memory Workbook into a fake spreadsheet (as a user-entered sheet would look)."""
    doc = FakeSpreadsheet(time_zone=time_zone)
    for i, tab in enumerate(workbook.tabs):
        sh = FakeSheet(100 + i, tab.name, max(tab.height + 20, 1000), max(tab.width + 2, 26))
        for r, row in enumerate(tab.values):
            for c, v in enumerate(row):
                cell: dict[str, Any] = {}
                if not is_empty(v):
                    if isinstance(v, bool):
                        cell["userEnteredValue"] = {"boolValue": v}
                    elif isinstance(v, date):
                        cell["userEnteredValue"] = {"numberValue": date_to_serial(v)}
                        cell["userEnteredFormat"] = {"numberFormat": {"type": "DATE", "pattern": date_pattern}}
                    elif isinstance(v, (int, float)):
                        cell["userEnteredValue"] = {"numberValue": float(v)}
                    else:
                        cell["userEnteredValue"] = {"stringValue": str(v)}
                f = tab.formats[r][c] if r < len(tab.formats) and c < len(tab.formats[r]) else CellFormat()
                if f.font or f.background or f.strike:
                    uf = cell.setdefault("userEnteredFormat", {})
                    tf: dict[str, Any] = {}
                    if f.font:
                        tf["foregroundColorStyle"] = {"rgbColor": _rgb(f.font)}
                        tf["foregroundColor"] = _rgb(f.font)
                    if f.strike:
                        tf["strikethrough"] = True
                    if tf:
                        uf["textFormat"] = tf
                    if f.background:
                        uf["backgroundColorStyle"] = {"rgbColor": _rgb(f.background)}
                        uf["backgroundColor"] = _rgb(f.background)
                if cell:
                    sh.cells[(r, c)] = cell
        for (r, c), dv in tab.validations.items():
            sh.cells.setdefault((r - 1, c - 1), {})["dataValidation"] = {
                "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": x} for x in dv.values]},
                "strict": not dv.allow_invalid,
            }
        if tab.protected:
            sh.protected.append({"protectedRangeId": 1 + i, "range": {"sheetId": sh.sheet_id}})
        doc.sheets.append(sh)
    return doc


# ---- Drive changes feed + clock (S2) -------------------------------------------------------


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


class FakeDriveService:
    """changes.getStartPageToken / changes.list / files.get(modifiedTime) over a FakeSheetsService.

    Every committed batchUpdate (ours) and every `user_edit` bumps the file's modifiedTime
    (1 ms resolution like Drive) and appends a change.
    """

    def __init__(self, sheets: FakeSheetsService, clock: FakeClock) -> None:
        self.sheets = sheets
        self.clock = clock
        self.log: list[dict[str, Any]] = []
        self.modified: dict[str, datetime] = {}
        self.calls: list[str] = []
        self.unrelated_files: list[str] = []
        # Live Drive reports the *previous* modifiedTime for a moment after a Sheets write returns;
        # lag_reads_after_commit = how many files.get reads stay stale after each of our commits.
        self.lag_reads_after_commit = 0
        self._stale: dict[str, tuple[datetime, int]] = {}
        sheets.on_commit = self._commit

    def _commit(self, file_id: str) -> None:
        prev = self.modified.get(file_id)
        self._bump(file_id, by_self=True)
        if self.lag_reads_after_commit and prev is not None:
            self._stale[file_id] = (prev, self.lag_reads_after_commit)

    def _bump(self, file_id: str, by_self: bool = False) -> None:
        t = self.clock()
        prev = self.modified.get(file_id)
        if prev is not None and t <= prev:
            t = prev + timedelta(milliseconds=1)
        self.modified[file_id] = t
        stamp = t.isoformat().replace("+00:00", "Z")
        self.log.append({"fileId": file_id, "time": stamp,
                         "file": {"modifiedTime": stamp, "lastModifyingUser": {"me": by_self}}})

    def repeat_last_change(self, file_id: str) -> None:
        """Live Drive re-emits a change record for the same modification minutes later."""
        last = next(c for c in reversed(self.log) if c["fileId"] == file_id)
        self.log.append(copy.deepcopy(last))

    def user_edit(self, file_id: str, tab: str, row: int, col: int, value: Any) -> None:
        """A human edits one cell (1-based) in the sheet UI."""
        sheet = next(s for s in self.sheets.docs[file_id].sheets if s.title == tab)
        cell = sheet.cells.setdefault((row - 1, col - 1), {})
        if value is None or value == "":
            cell.pop("userEnteredValue", None)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            cell["userEnteredValue"] = {"numberValue": float(value)}
        else:
            cell["userEnteredValue"] = {"stringValue": str(value)}
        self._bump(file_id)

    def unrelated_change(self, file_id: str) -> None:
        self._bump(file_id)

    # -- API surface --
    def changes(self) -> FakeDriveService:
        return self

    def files(self) -> FakeDriveService:
        return self

    def getStartPageToken(self, **_: Any) -> _Request:
        return _Request(lambda: (self.calls.append("changes.getStartPageToken"),
                                 {"startPageToken": str(len(self.log))})[1])

    def list(self, pageToken: str, **_: Any) -> _Request:  # changes.list
        def run() -> dict[str, Any]:
            self.calls.append("changes.list")
            start = int(pageToken)
            return {"changes": copy.deepcopy(self.log[start:]), "newStartPageToken": str(len(self.log))}
        return _Request(run)

    def get(self, fileId: str, fields: str = "", **_: Any) -> _Request:  # files.get
        def run() -> dict[str, Any]:
            self.calls.append("files.get")
            t = self.modified.get(fileId, datetime(2026, 1, 1, tzinfo=UTC))
            stale = self._stale.get(fileId)
            if stale is not None:
                t = stale[0]
                self._stale[fileId] = (stale[0], stale[1] - 1)
                if stale[1] <= 1:
                    del self._stale[fileId]
            return {"modifiedTime": t.isoformat().replace("+00:00", "Z")}
        return _Request(run)

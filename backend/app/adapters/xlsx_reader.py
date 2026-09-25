"""Read an uploaded .xlsx into the evaluator's grid (SPEC-PATCH-005 P1.5).

The upload is the only way a sheet reaches us in the script-generator product: nothing here opens
a spreadsheet anywhere. The file is read in memory and never written to disk (I.6). Error and
warning text carries tab names and cell addresses only, never cell values (B.6).

Standard library only (zipfile + ElementTree): a general-purpose library would convert every
date-formatted number on load and fail on the ones Python cannot represent, which is exactly the
kind of cell a real workbook holds. Reading the parts ourselves lets a bad cell become a warning
instead of a crash.

What is read, cell by cell, into `Tab`:
  * values: strings (shared, inline, formula results), numbers, booleans, error codes as text;
    date-formatted numbers become `date` (whole days) or `datetime`
  * formatting: font colour, fill colour, strikethrough; theme, indexed and ARGB colours are
    resolved to #RRGGBB
  * number formats (non-General), list dropdowns, hidden tabs
Kept as metadata in `TabMeta`: which cells hold formulas (the readback warns: a sort rewrites
moved rows as values) and the merged ranges. Not read: column widths, conditional formatting,
charts, comments, images.

Each tab ends at its last row and column holding a value, like Sheets' getLastRow(): formatted
empty rows below the data are not part of the grid (docs/MOCK-DIVERGENCES.md #14).
"""

from __future__ import annotations

import colorsys
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.adapters.base import Grid, TabMeta
from app.services.grid import CellFormat, CellValue, DataValidation, Tab, Workbook, col_letter

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
_R_ID = "{%s}id" % NS["r"]

# Limits on what an upload may unpack to (the upload cap itself belongs to the session, I.6).
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_PART_BYTES = 100 * 1024 * 1024
MAX_CELLS = 2_000_000


class XlsxError(ValueError):
    """The file cannot be read. The message is shown to the owner and never quotes cell values."""


@dataclass
class XlsxRead:
    grid: Grid
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- number formats and dates

# Excel's built-in number formats (ECMA-376 18.8.30) that are dates or times, and their codes.
BUILTIN_FORMATS: dict[int, str] = {
    1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%", 11: "0.00E+00",
    12: "# ?/?", 13: "# ??/??", 14: "m/d/yyyy", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy",
    18: "h:mm AM/PM", 19: "h:mm:ss AM/PM", 20: "h:mm", 21: "h:mm:ss", 22: "m/d/yyyy h:mm",
    37: "#,##0 ;(#,##0)", 38: "#,##0 ;[Red](#,##0)", 39: "#,##0.00;(#,##0.00)",
    40: "#,##0.00;[Red](#,##0.00)", 45: "mm:ss", 46: "[h]:mm:ss", 47: "mmss.0", 48: "##0.0E+0",
    49: "@",
}
_DATE_IDS = set(range(14, 23)) | set(range(27, 37)) | {45, 46, 47} | set(range(50, 59))
_LITERALS = re.compile(r'"[^"]*"|\\.|_.|\*.')
_BRACKETS = re.compile(r"\[(?!h\]|hh\]|m\]|mm\]|s\]|ss\])[^\]]*\]", re.IGNORECASE)


def is_date_format(fmt_id: int, code: str | None) -> bool:
    if fmt_id in _DATE_IDS:
        return True
    if not code or code.lower() == "general":
        return False
    stripped = _BRACKETS.sub("", _LITERALS.sub("", code.split(";")[0]))
    return bool(re.search(r"[dmyhs]", stripped, re.IGNORECASE))


def from_serial(serial: float, date1904: bool) -> date | datetime | None:
    """A date serial as a date (whole day) or datetime; None when Python cannot represent it."""
    if date1904:
        base = datetime(1904, 1, 1)
    else:
        base = datetime(1899, 12, 30) if serial >= 61 else datetime(1899, 12, 31)  # Excel's 1900 leap bug
    try:
        moment = base + timedelta(days=serial)
    except OverflowError:
        return None
    moment = moment.replace(microsecond=0) + timedelta(seconds=round(moment.microsecond / 1e6))
    if serial == int(serial):
        return moment.date()
    return moment


# ---------------------------------------------------------------- colours

# The 64-colour legacy palette that `indexed` colours refer to (ECMA-376 18.8.27).
INDEXED = [
    "000000", "FFFFFF", "FF0000", "00FF00", "0000FF", "FFFF00", "FF00FF", "00FFFF",
    "000000", "FFFFFF", "FF0000", "00FF00", "0000FF", "FFFF00", "FF00FF", "00FFFF",
    "800000", "008000", "000080", "808000", "800080", "008080", "C0C0C0", "808080",
    "9999FF", "993366", "FFFFCC", "CCFFFF", "660066", "FF8080", "0066CC", "CCCCFF",
    "000080", "FF00FF", "FFFF00", "00FFFF", "800080", "800000", "008080", "0000FF",
    "00CCFF", "CCFFFF", "CCFFCC", "FFFF99", "99CCFF", "FF99CC", "CC99FF", "FFCC99",
    "3366FF", "33CCCC", "99CC00", "FFCC00", "FF9900", "FF6600", "666699", "969696",
    "003366", "339966", "003300", "333300", "993300", "993366", "333399", "333333",
]
_THEME_ORDER = ["lt1", "dk1", "lt2", "dk2", "accent1", "accent2", "accent3", "accent4", "accent5",
                "accent6", "hlink", "folHlink"]


def _theme_colours(xml: bytes | None) -> list[str]:
    if not xml:
        return []
    root = ET.fromstring(xml)
    scheme = root.find(".//a:clrScheme", NS)
    if scheme is None:
        return []
    found: dict[str, str] = {}
    for child in scheme:
        name = child.tag.split("}")[1]
        srgb = child.find("a:srgbClr", NS)
        sysclr = child.find("a:sysClr", NS)
        if srgb is not None:
            found[name] = srgb.get("val", "000000")
        elif sysclr is not None:
            found[name] = sysclr.get("lastClr", "000000")
    return [found.get(n, "000000").upper() for n in _THEME_ORDER]


def _tinted(rgb: str, tint: float) -> str:
    r, g, b = (int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, lum, s = colorsys.rgb_to_hls(r, g, b)
    lum = lum * (1 + tint) if tint < 0 else lum * (1 - tint) + tint
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, lum)), s)
    return "".join(f"{round(v * 255):02X}" for v in (r, g, b))


def resolve_colour(el: ET.Element | None, theme: list[str]) -> str | None:
    """#RRGGBB, or None for "automatic" (the sheet default)."""
    if el is None or el.get("auto") in ("1", "true"):
        return None
    rgb: str | None = None
    if el.get("rgb"):
        rgb = el.get("rgb", "")[-6:].upper()
    elif el.get("theme") is not None:
        idx = int(el.get("theme", "0"))
        rgb = theme[idx] if idx < len(theme) else None
    elif el.get("indexed") is not None:
        idx = int(el.get("indexed", "0"))
        rgb = INDEXED[idx] if idx < len(INDEXED) else None  # 64/65: system colours = default
    if rgb is None or not re.fullmatch(r"[0-9A-F]{6}", rgb):
        return None
    tint = float(el.get("tint", "0") or 0)
    return "#" + (_tinted(rgb, tint) if tint else rgb)


# ---------------------------------------------------------------- styles

@dataclass(frozen=True)
class _Style:
    fmt: CellFormat
    number_format: str | None  # None = General
    is_date: bool


def _styles(xml: bytes | None, theme: list[str]) -> list[_Style]:
    plain = _Style(CellFormat(), None, False)
    if not xml:
        return [plain]
    root = ET.fromstring(xml)
    codes = {int(n.get("numFmtId", "0")): n.get("formatCode", "") for n in root.findall("m:numFmts/m:numFmt", NS)}
    fonts: list[tuple[str | None, bool]] = []
    for font_el in root.findall("m:fonts/m:font", NS):
        strike_el = font_el.find("m:strike", NS)
        fonts.append((resolve_colour(font_el.find("m:color", NS), theme),
                      strike_el is not None and strike_el.get("val", "1") not in ("0", "false")))
    fills: list[str | None] = []
    for fill_el in root.findall("m:fills/m:fill", NS):
        pattern = fill_el.find("m:patternFill", NS)
        if pattern is None or pattern.get("patternType", "none") == "none":
            fills.append(None)
        else:
            fills.append(resolve_colour(pattern.find("m:fgColor", NS), theme))
    out = []
    for xf in root.findall("m:cellXfs/m:xf", NS):
        fmt_id = int(xf.get("numFmtId", "0"))
        font_id, fill_id = int(xf.get("fontId", "0")), int(xf.get("fillId", "0"))
        font, strike = fonts[font_id] if font_id < len(fonts) else (None, False)
        fill = fills[fill_id] if fill_id < len(fills) else None
        code = codes.get(fmt_id, BUILTIN_FORMATS.get(fmt_id))
        number_format = None if fmt_id == 0 or not code or code.lower() == "general" else code
        out.append(_Style(CellFormat(font=font, background=fill, strike=strike), number_format,
                          is_date_format(fmt_id, code)))
    return out or [plain]


# ---------------------------------------------------------------- package parts

class _Package:
    def __init__(self, data: bytes) -> None:
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise XlsxError("this is not an .xlsx file (it is not a zip package)") from exc
        total = sum(i.file_size for i in self.zip.infolist())
        if total > MAX_UNPACKED_BYTES:
            raise XlsxError("this file unpacks to more than the size we accept")
        self.names = {i.filename.lstrip("/") for i in self.zip.infolist()}

    def read(self, name: str) -> bytes | None:
        name = name.lstrip("/")
        if name not in self.names:
            return None
        if self.zip.getinfo(name).file_size > MAX_PART_BYTES:
            raise XlsxError("one part of this file is larger than the size we accept")
        return self.zip.read(name)


def _xml(pkg: _Package, name: str) -> ET.Element | None:
    raw = pkg.read(name)
    if raw is None:
        return None
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise XlsxError(f"part {name} of this file is damaged") from exc


def _shared_strings(pkg: _Package) -> list[str]:
    root = _xml(pkg, "xl/sharedStrings.xml")
    if root is None:
        return []
    return [_text(si) for si in root.findall("m:si", NS)]


def _text(node: ET.Element) -> str:
    """Rich text runs joined; phonetic hints (rPh) left out."""
    direct = node.find("m:t", NS)
    if direct is not None:
        return direct.text or ""
    return "".join(t.text or "" for r in node.findall("m:r", NS) for t in r.findall("m:t", NS))


_REF = re.compile(r"^([A-Z]+)(\d+)$")


def _cell_ref(ref: str) -> tuple[int, int]:
    m = _REF.match(ref)
    if not m:
        raise XlsxError(f"a cell reference in this file is malformed ({ref[:12]!r})")
    col = 0
    for ch in m.group(1):
        col = col * 26 + ord(ch) - 64
    return int(m.group(2)), col


# ---------------------------------------------------------------- the reader

def read_xlsx(data: bytes, *, timezone: str, title: str = "") -> XlsxRead:
    """The workbook in `data` as a Grid, plus warnings about cells that could not be read as is.

    `timezone` is required and never inferred (PATCH-005 I.3): an .xlsx carries none, and "today"
    for date conditions depends on it.
    """
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise XlsxError(f"unknown timezone {timezone!r}") from exc

    pkg = _Package(data)
    workbook = _xml(pkg, "xl/workbook.xml")
    if workbook is None:
        raise XlsxError("this .xlsx has no workbook part")
    rels = _xml(pkg, "xl/_rels/workbook.xml.rels")
    targets = {} if rels is None else {r.get("Id"): r.get("Target", "") for r in rels.findall("rel:Relationship", NS)}
    pr = workbook.find("m:workbookPr", NS)
    date1904 = pr is not None and pr.get("date1904", "0") in ("1", "true")
    theme = _theme_colours(pkg.read("xl/theme/theme1.xml"))
    styles = _styles(pkg.read("xl/styles.xml"), theme)
    strings = _shared_strings(pkg)

    warnings: list[str] = []
    tabs: list[Tab] = []
    metas: dict[str, TabMeta] = {}
    cells_seen = 0
    for sheet in workbook.findall("m:sheets/m:sheet", NS):
        name = sheet.get("name", "")
        target = targets.get(sheet.get(_R_ID), "")
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target
        root = _xml(pkg, path)
        if root is None:
            warnings.append(f"{name}: tab has no readable content; skipped")
            continue
        tab, meta, seen = _read_sheet(root, name, styles, strings, date1904, warnings)
        cells_seen += seen
        if cells_seen > MAX_CELLS:
            raise XlsxError("this file has more cells than we accept")
        tab.hidden = sheet.get("state", "visible") != "visible"
        tabs.append(tab)
        metas[name] = meta
    if not tabs:
        raise XlsxError("this .xlsx has no tabs we could read")
    return XlsxRead(Grid(Workbook(tabs), timezone=timezone, tabs=metas, title=title), warnings)


def _read_sheet(root: ET.Element, name: str, styles: list[_Style], strings: list[str], date1904: bool,
                warnings: list[str]) -> tuple[Tab, TabMeta, int]:
    values: dict[tuple[int, int], CellValue] = {}
    formats: dict[tuple[int, int], CellFormat] = {}
    number_formats: dict[tuple[int, int], str] = {}
    formulas: set[tuple[int, int]] = set()
    seen = 0
    for row_el in root.findall("m:sheetData/m:row", NS):
        row_no = int(row_el.get("r", "0") or 0)
        next_col = 1
        for cell in row_el.findall("m:c", NS):
            seen += 1
            ref = cell.get("r")
            r, col = _cell_ref(ref) if ref else (row_no, next_col)
            next_col = col + 1
            s_idx = int(cell.get("s", "0") or 0)
            style = styles[s_idx] if s_idx < len(styles) else styles[0]
            if style.fmt != CellFormat():
                formats[(r, col)] = style.fmt
            if style.number_format:
                number_formats[(r, col)] = style.number_format
            if cell.find("m:f", NS) is not None:
                formulas.add((r, col))
            value = _value(cell, style, strings, date1904, f"{name}!{col_letter(col)}{r}", warnings)
            if value is not None and value != "":
                values[(r, col)] = value

    height = max((r for r, _ in values), default=0)
    width = max((c for _, c in values), default=0)
    grid = [[values.get((r, c)) for c in range(1, width + 1)] for r in range(1, height + 1)]
    tab = Tab(name, grid)
    for (r, col), cell_fmt in formats.items():
        if r <= height and col <= width:
            tab.formats[r - 1][col - 1] = cell_fmt
    tab.number_formats = {k: v for k, v in number_formats.items() if k[0] <= height and k[1] <= width}
    tab.validations = _validations(root, height, width)
    merges = tuple(m.get("ref", "") for m in root.findall("m:mergeCells/m:mergeCell", NS))
    date_patterns: dict[int, str] = {}
    for (r, col), pattern in sorted(number_formats.items()):
        if isinstance(values.get((r, col)), (date, datetime)):
            date_patterns.setdefault(col - 1, pattern)
    meta = TabMeta(date_patterns=date_patterns,
                   formula_cells=frozenset(k for k in formulas if k[0] <= height and k[1] <= width),
                   merged=merges)
    return tab, meta, seen


def _value(c: ET.Element, style: _Style, strings: list[str], date1904: bool, where: str,
           warnings: list[str]) -> CellValue:
    kind = c.get("t", "n")
    if kind == "inlineStr":
        node = c.find("m:is", NS)
        return _text(node) if node is not None else ""
    v = c.find("m:v", NS)
    raw = v.text if v is not None and v.text is not None else None
    if raw is None:
        return None
    if kind == "s":
        idx = int(raw)
        if idx >= len(strings):
            warnings.append(f"{where}: refers to a missing shared string; read as empty")
            return None
        return strings[idx]
    if kind in ("str", "e"):
        return raw
    if kind == "b":
        return raw in ("1", "true")
    if kind == "d":  # ISO 8601 date cell (rare, strict OOXML)
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            warnings.append(f"{where}: date could not be read; read as text")
            return raw
        return parsed.date() if parsed.time() == datetime.min.time() else parsed
    try:
        number = float(raw)
    except ValueError:
        warnings.append(f"{where}: number could not be read; read as text")
        return raw
    if style.is_date:
        as_date = from_serial(number, date1904)
        if as_date is None:
            warnings.append(f"{where}: date is outside the range we can read (years 1-9999); "
                            "read as a number, so date rules will not treat it as a date")
            return int(number) if number.is_integer() else number
        return as_date
    return int(number) if number.is_integer() and abs(number) < 2**53 else number


def _validations(root: ET.Element, height: int, width: int) -> dict[tuple[int, int], DataValidation]:
    """List dropdowns with inline values ("a,b,c"); other kinds are not modelled."""
    out: dict[tuple[int, int], DataValidation] = {}
    for dv in root.findall("m:dataValidations/m:dataValidation", NS):
        if dv.get("type") != "list":
            continue
        formula = dv.find("m:formula1", NS)
        text = (formula.text or "") if formula is not None else ""
        if not (text.startswith('"') and text.endswith('"')):
            continue  # a range reference, not a literal list
        values = tuple(v for v in text[1:-1].split(","))
        rule = DataValidation(values=values, allow_invalid=dv.get("errorStyle", "stop") != "stop"
                              or dv.get("showErrorMessage", "0") in ("0", "false"))
        for area in (dv.get("sqref") or "").split():
            first, _, last = area.partition(":")
            r1, c1 = _cell_ref(first)
            r2, c2 = _cell_ref(last or first)
            for r in range(r1, min(r2, height) + 1):
                for col in range(c1, min(c2, width) + 1):
                    out[(r, col)] = rule
    return out

"""Builds real .xlsx packages for the reader's tests (zip + SpreadsheetML parts, nothing faked).

Colours are given as raw <color> attributes so tests can use every form a file may carry:
`{"rgb": "FFFF0000"}`, `{"theme": "4", "tint": "0.4"}`, `{"indexed": "10"}`, `{"auto": "1"}`.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from xml.sax.saxutils import escape

from app.services.grid import CellFormat, Workbook, col_letter

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


@dataclass
class Cell:
    value: Any = None
    font: dict[str, str] | None = None
    fill: dict[str, str] | None = None
    strike: bool = False
    number_format: str | None = None  # custom code; dates default to d/m/yyyy
    formula: str | None = None
    kind: str | None = None           # force the t= attribute ("e", "str", "inlineStr", ...)
    raw: str | None = None            # force the <v> text


@dataclass
class Sheet:
    name: str
    cells: dict[tuple[int, int], Cell] = field(default_factory=dict)
    merges: list[str] = field(default_factory=list)
    validations: list[tuple[str, str, bool]] = field(default_factory=list)  # (sqref, "a,b", strict)
    hidden: bool = False


def _attrs(d: dict[str, str]) -> str:
    return " ".join(f'{k}="{escape(v)}"' for k, v in d.items())


def serial(value: date | datetime) -> float:
    base = datetime(1899, 12, 30)
    moment = value if isinstance(value, datetime) else datetime(value.year, value.month, value.day)
    delta = moment - base
    return delta.days + delta.seconds / 86400


THEME = """<?xml version="1.0" encoding="UTF-8"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="t"><a:themeElements>
<a:clrScheme name="c"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>
<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="44546A"/></a:dk2>
<a:lt2><a:srgbClr val="E7E6E6"/></a:lt2><a:accent1><a:srgbClr val="4472C4"/></a:accent1>
<a:accent2><a:srgbClr val="ED7D31"/></a:accent2><a:accent3><a:srgbClr val="A5A5A5"/></a:accent3>
<a:accent4><a:srgbClr val="FFC000"/></a:accent4><a:accent5><a:srgbClr val="5B9BD5"/></a:accent5>
<a:accent6><a:srgbClr val="70AD47"/></a:accent6><a:hlink><a:srgbClr val="0563C1"/></a:hlink>
<a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme></a:themeElements></a:theme>"""


def build(sheets: list[Sheet], *, date1904: bool = False, theme: bool = True) -> bytes:
    strings: list[str] = []
    fonts = ["<font/>"]
    fills = ['<fill><patternFill patternType="none"/></fill>', '<fill><patternFill patternType="gray125"/></fill>']
    numfmts: dict[str, int] = {}
    xfs: list[tuple[int, int, int]] = [(0, 0, 0)]

    def style_of(cell: Cell) -> int:
        font_xml = "<font>" + (f"<color {_attrs(cell.font)}/>" if cell.font else "") + \
                   ("<strike/>" if cell.strike else "") + "</font>"
        font_id = fonts.index(font_xml) if font_xml in fonts else (fonts.append(font_xml) or len(fonts) - 1)
        fill_id = 0
        if cell.fill:
            fill_xml = f'<fill><patternFill patternType="solid"><fgColor {_attrs(cell.fill)}/></patternFill></fill>'
            fill_id = fills.index(fill_xml) if fill_xml in fills else (fills.append(fill_xml) or len(fills) - 1)
        code = cell.number_format
        if code is None and isinstance(cell.value, (date, datetime)):
            code = "d/m/yyyy" if not isinstance(cell.value, datetime) else "d/m/yyyy h:mm:ss"
        fmt_id = 0
        if code:
            fmt_id = numfmts.setdefault(code, 164 + len(numfmts))
        key = (fmt_id, font_id, fill_id)
        if key not in xfs:
            xfs.append(key)
        return xfs.index(key)

    sheet_parts = []
    for sheet in sheets:
        rows: dict[int, list[str]] = {}
        for (r, c), cell in sorted(sheet.cells.items()):
            ref = f"{col_letter(c)}{r}"
            attrs = f'r="{ref}" s="{style_of(cell)}"'
            inner = f"<f>{escape(cell.formula)}</f>" if cell.formula else ""
            v = cell.value
            kind = cell.kind
            if cell.raw is not None:
                text = cell.raw
            elif v is None:
                text = None
            elif isinstance(v, bool):
                kind, text = kind or "b", "1" if v else "0"
            elif isinstance(v, (date, datetime)):
                text = repr(serial(v))
            elif isinstance(v, (int, float)):
                text = repr(v)
            elif kind == "inlineStr":
                text = None
                inner += f"<is><t>{escape(v)}</t></is>"
            elif kind in ("str", "e"):
                text = v
            else:
                kind = "s"
                if v not in strings:
                    strings.append(v)
                text = str(strings.index(v))
            if kind:
                attrs += f' t="{kind}"'
            if text is not None:
                inner += f"<v>{escape(text)}</v>"
            rows.setdefault(r, []).append(f"<c {attrs}>{inner}</c>")
        data = "".join(f'<row r="{r}">{"".join(cs)}</row>' for r, cs in sorted(rows.items()))
        extra = ""
        if sheet.merges:
            extra += f'<mergeCells count="{len(sheet.merges)}">' + \
                     "".join(f'<mergeCell ref="{m}"/>' for m in sheet.merges) + "</mergeCells>"
        if sheet.validations:
            extra += f'<dataValidations count="{len(sheet.validations)}">' + "".join(
                f'<dataValidation type="list" sqref="{ref}" showErrorMessage="{1 if strict else 0}">'
                f'<formula1>{escape(values) if values.startswith("$") else chr(34) + escape(values) + chr(34)}'
                f'</formula1></dataValidation>'
                for ref, values, strict in sheet.validations) + "</dataValidations>"
        sheet_parts.append(f'<worksheet xmlns="{MAIN}"><sheetData>{data}</sheetData>{extra}</worksheet>')

    styles = (f'<styleSheet xmlns="{MAIN}">'
              + (f'<numFmts count="{len(numfmts)}">' + "".join(
                  f'<numFmt numFmtId="{i}" formatCode="{escape(code)}"/>' for code, i in numfmts.items())
                 + "</numFmts>" if numfmts else "")
              + f'<fonts count="{len(fonts)}">{"".join(fonts)}</fonts>'
              + f'<fills count="{len(fills)}">{"".join(fills)}</fills>'
              + '<borders count="1"><border/></borders>'
              + f'<cellXfs count="{len(xfs)}">' + "".join(
                  f'<xf numFmtId="{n}" fontId="{f}" fillId="{fl}" borderId="0"/>' for n, f, fl in xfs)
              + "</cellXfs></styleSheet>")
    shared = (f'<sst xmlns="{MAIN}" count="{len(strings)}" uniqueCount="{len(strings)}">'
              + "".join(f'<si><t xml:space="preserve">{escape(s)}</t></si>' for s in strings) + "</sst>")
    workbook = (f'<workbook xmlns="{MAIN}" xmlns:r="{REL}">'
                + (f'<workbookPr date1904="1"/>' if date1904 else "")
                + "<sheets>" + "".join(
                    f'<sheet name="{escape(s.name)}" sheetId="{i + 1}" r:id="rId{i + 1}"'
                    + (' state="hidden"' if s.hidden else "") + "/>" for i, s in enumerate(sheets))
                + "</sheets></workbook>")
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i + 1}" Type="{REL}/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                      for i in range(len(sheets))) + "</Relationships>")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/styles.xml", styles)
        z.writestr("xl/sharedStrings.xml", shared)
        if theme:
            z.writestr("xl/theme/theme1.xml", THEME)
        for i, part in enumerate(sheet_parts):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", part)
    return out.getvalue()


def from_workbook(workbook: Workbook) -> bytes:
    """The evaluator's grid written out as an .xlsx: what a Sheets export of it would carry."""
    sheets = []
    for tab in workbook.tabs:
        sheet = Sheet(tab.name, hidden=tab.hidden)
        for r, row in enumerate(tab.values, start=1):
            for c, value in enumerate(row, start=1):
                fmt = tab.formats[r - 1][c - 1]
                nf = tab.number_formats.get((r, c))
                if value in (None, "") and fmt == CellFormat() and nf is None:
                    continue
                sheet.cells[(r, c)] = Cell(
                    value=None if value == "" else value,
                    font={"rgb": "FF" + fmt.font[1:]} if fmt.font else None,
                    fill={"rgb": "FF" + fmt.background[1:]} if fmt.background else None,
                    strike=fmt.strike, number_format=nf)
        sheets.append(sheet)
    return build(sheets)

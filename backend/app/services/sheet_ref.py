"""Accept a Google Sheets URL or a bare spreadsheet ID (SPEC-PATCH-003 B2)."""

from __future__ import annotations

import re

_URL = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")
_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")


class BadSheetRef(ValueError):
    pass


def extract_sheet_id(text: str) -> str:
    t = text.strip()
    m = _URL.search(t)
    if m:
        return m.group(1)
    if _ID.match(t):
        return t
    if "gid=" in t and "/d/" not in t:
        raise BadSheetRef("that looks like a tab id (gid); use the long id between /d/ and /edit in the URL")
    raise BadSheetRef("not a Google Sheets URL or spreadsheet id")

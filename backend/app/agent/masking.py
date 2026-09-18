"""LLM masking boundary (SPEC-PATCH-002 B.7). Everything the agent's `sample_rows` tool returns
passes through `Masker` before it leaves the process.

Per call (one `Masker`):
  * person/company-like text -> "Company_A" / "Person_B" / "Text_C" (stable within the call)
  * emails / phones          -> shape-preserving dummies
  * codes (letters+digits)   -> same shape, random characters
  * numbers                  -> random value of the same magnitude and format
  * dates                    -> shifted by one constant random offset (ordering preserved)
Unmasked (structure, rules depend on it): headers, tab names, and the values of enum columns
(status labels). An enum column is one whose header looks like a status/flag column or whose
values are all boolean-like -- never inferred from low cardinality alone, so a column of a few
repeated customer names can never pass through.
"""

from __future__ import annotations

import random
import re
import string
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from app.services.grid import CellValue, cell_text, is_empty

ENUM_HEADER = re.compile(
    r"\b(STATUS|STAGE|STATE|FREEZE|FROZEN|PRIORITY|CATEGORY|TYPE|APPROVED|APPROVAL|DONE|FLAG|HOLD|"
    r"IN-HOUSE|OUTSOURCE|YES/NO|Y/N)\b|\?$"
)
BOOLEAN_LIKE = {"YES", "NO", "Y", "N", "TRUE", "FALSE", "DONE", "PENDING", "OK", "NA", "N/A"}
COMPANY_HEADER = re.compile(r"CUSTOMER|CLIENT|COMPANY|VENDOR|SUPPLIER|PARTY|FIRM|ORG|BUYER|DEALER")
PERSON_HEADER = re.compile(r"\b(NAME|CONTACT|PERSON|OWNER|ENGINEER|MANAGER|INCHARGE|IN-CHARGE|BY|SALES)\b")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE = re.compile(r"^\+?[\d\s().-]{7,}$")
HAS_DIGIT = re.compile(r"\d")


def is_enum_column(header: CellValue, values: Sequence[CellValue]) -> bool:
    """Status-like column whose labels are structure, not data (see module doc)."""
    h = cell_text(header).strip().upper()
    if COMPANY_HEADER.search(h) or "EMAIL" in h or "PHONE" in h or "MOBILE" in h:
        return False
    if ENUM_HEADER.search(h):
        return True
    texts = {cell_text(v).strip().upper() for v in values if not is_empty(v)}
    return bool(texts) and texts <= BOOLEAN_LIKE


def _letters(i: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA ..."""
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


class Masker:
    """One instance per tool call: placeholders are stable within the call, and differ across calls."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        self.day_offset = timedelta(days=self.rng.choice([-1, 1]) * self.rng.randint(30, 400))
        self._text: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}

    # -- public --
    def mask_value(self, v: CellValue, header: CellValue = "") -> CellValue:
        if is_empty(v) or isinstance(v, bool):
            return v
        if isinstance(v, datetime):
            return v + self.day_offset
        if isinstance(v, date):
            return v + self.day_offset
        if isinstance(v, int):
            return self._int(v)
        if isinstance(v, float):
            return self._float(v)
        return self._text_value(str(v), cell_text(header).strip().upper())

    def mask_rows(self, headers: Sequence[CellValue], rows: Sequence[Sequence[CellValue]],
                  enum_cols: set[int]) -> list[list[CellValue]]:
        out: list[list[CellValue]] = []
        for row in rows:
            masked: list[CellValue] = []
            for j, v in enumerate(row):
                header = headers[j] if j < len(headers) else ""
                masked.append(v if j in enum_cols else self.mask_value(v, header))
            out.append(masked)
        return out

    # -- numbers --
    def _int(self, v: int) -> int:
        digits = len(str(abs(v)))
        lo = 0 if digits == 1 else 10 ** (digits - 1)
        new = self.rng.randint(lo, 10**digits - 1)
        return -new if v < 0 else new

    def _float(self, v: float) -> float:
        text = repr(v)
        decimals = len(text.split(".")[1]) if "." in text and "e" not in text else 2
        mag = abs(v)
        lo, hi = (mag * 0.5, mag * 1.5) if mag else (0.0, 1.0)
        new = round(self.rng.uniform(lo, hi), decimals)
        return -new if v < 0 else new

    # -- text --
    def _text_value(self, s: str, header: str) -> str:
        t = s.strip()
        if EMAIL.match(t):
            return self._stable("email", t, lambda: self._shape_email(t))
        if PHONE.match(t) and sum(c.isdigit() for c in t) >= 7:
            return self._stable("phone", t, lambda: self._shape(t))
        if HAS_DIGIT.search(t):
            return self._stable("code", t, lambda: self._shape(t))
        kind = "Company" if COMPANY_HEADER.search(header) else "Person" if PERSON_HEADER.search(header) else "Text"
        return self._stable(kind, t.upper(), lambda: f"{kind}_{self._next(kind)}")

    def _stable(self, kind: str, key: str, make: object) -> str:
        k = (kind, key)
        if k not in self._text:
            assert callable(make)
            self._text[k] = str(make())
        return self._text[k]

    def _next(self, kind: str) -> str:
        n = self._counters.get(kind, 0)
        self._counters[kind] = n + 1
        return _letters(n)

    def _shape(self, s: str) -> str:
        """Same character classes and separators, random characters; never the input itself."""
        for _ in range(20):
            out = self._shape_once(s)
            if out != s or not any(c.isalnum() for c in s):
                return out
        return out

    def _shape_once(self, s: str) -> str:
        out = []
        for c in s:
            if c.isdigit():
                out.append(self.rng.choice(string.digits))
            elif c.isalpha():
                pool = string.ascii_uppercase if c.isupper() else string.ascii_lowercase
                out.append(self.rng.choice(pool))
            else:
                out.append(c)
        return "".join(out)

    def _shape_email(self, s: str) -> str:
        local, _, domain = s.partition("@")
        tld = domain.rsplit(".", 1)[-1]
        return f"{self._shape(local)}@{self._shape(domain[: -len(tld) - 1])}.example"

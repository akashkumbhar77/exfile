"""Clause coverage: what the instruction names that the compiled rules do not use (PATCH-005 D.2).

A mechanical diff, not a second model call (owner ruling I.8). The vocabulary comes from the upload
itself and from fixed word lists:
  * columns: header texts and canonical names, on the tabs the rules touch or the instruction names
  * tabs: tab names
  * values: labels in status-like columns (few distinct text values; free text such as customer
    names is left out, so ordinary words in the instruction do not trip it)
  * colours and strike-through words
  * timing phrases ("every morning", "hourly", "as soon as", "every 5 minutes")
Anything the instruction mentions from that vocabulary is checked against what the config actually
references. An unused mention is shown above the preview as something to check. It is a warning,
not a verdict: the owner may have mentioned a column only to describe the sheet.

The instruction is the owner's text (B.6 addendum): this module never logs it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from app.agent.profile import enum_columns
from app.schemas.config import (
    ConfigSpec,
    DebouncedTrigger,
    OnEditTrigger,
    ScheduleTrigger,
)
from app.services.describe import colour_name
from app.services.grid import Workbook, cell_text, is_empty
from app.services.headers import canon_key, canonicalize, match_expr

Kind = Literal["column", "tab", "value", "colour", "timing"]

COLOURS = ["red", "green", "yellow", "orange", "blue", "grey", "gray", "purple", "pink", "brown", "black",
           "white", "gold"]
STRIKE_WORDS = ["strike", "strikethrough", "strike through", "struck", "cross out", "crossed out", "line through"]
SCHEDULE = re.compile(
    r"\b(daily|hourly|nightly|weekly|monthly|every (day|morning|evening|night|week|month|hour|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekday)s?|each (day|morning|evening|"
    r"night|week)|at \d{1,2}(:\d{2})?\s*(am|pm)?|(morning|midnight|noon)s?)\b")
TOO_OFTEN = re.compile(r"\bevery (\d+ )?(minute|min|second|sec)s?\b")
ON_EDIT = re.compile(r"\b(as soon as|immediately|instantly|right away|"
                     r"when(ever)? [^.;]{0,40}?\b(edit|chang|updat|typ|enter|set)\w*)")


def names_timing(instruction: str) -> bool:
    """Whether the instruction says anything about when rules should run."""
    text = instruction.lower()
    return any(p.search(text) for p in (SCHEDULE, TOO_OFTEN, ON_EDIT))


@dataclass(frozen=True)
class Mention:
    kind: Kind
    text: str      # the vocabulary item, as the sheet (or the word list) spells it
    covered: bool
    note: str


def _norm(s: str) -> str:
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split()) + " "


def _mentions(instruction: str, phrase: str) -> bool:
    words = _norm(phrase)
    return len(words.strip()) > 2 and words in _norm(instruction)


def _walk(node: Any, keys: set[str], out: set[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in keys and isinstance(v, str):
                out.add(v)
            elif k in keys and isinstance(v, list):
                out.update(x for x in v if isinstance(x, str))
            _walk(v, keys, out)
    elif isinstance(node, list):
        for x in node:
            _walk(x, keys, out)


def used_columns(config: ConfigSpec) -> set[str]:
    raw = config.model_dump(mode="json", by_alias=True)
    names: set[str] = set()
    _walk(raw["rules"], {"column", "date", "columns", "key_columns", "enum"}, names)
    for rule in raw["rules"]:
        selector = rule.get("tabs") or rule.get("sources")
        if isinstance(selector, str) and ":" in selector:
            names.add(selector.split(":", 1)[1])
    return {canon_key(n) for n in names}


def used_literals(config: ConfigSpec) -> set[str]:
    raw = config.model_dump(mode="json", by_alias=True)
    found: set[str] = set()
    _walk(raw["rules"], {"equals", "contains", "is", "values"}, found)
    for spec in config.enums.values():
        found.update(s.value for s in spec.stages)
    return {canon_key(v) for v in found}


def _value_covered(value: str, config: ConfigSpec, literals: set[str]) -> bool:
    key = canon_key(value)
    if key in literals or any(lit and lit in key for lit in literals):
        return True
    return any(match_expr(s.match, value) for spec in config.enums.values() for s in spec.stages)


def _styles(config: ConfigSpec) -> tuple[set[str], bool]:
    colours: set[str] = set()
    strike = False
    raw = config.model_dump(mode="json", by_alias=True)
    hexes: set[str] = set()
    _walk(raw["rules"], {"font", "background", "title_font", "title_background", "header_font",
                         "header_background"}, hexes)
    for h in hexes:
        if h.startswith("#"):
            colours.add(colour_name(h.upper()))
    struck: set[str] = set()
    _walk_bools(raw["rules"], "strike", struck)
    strike = "true" in struck
    return colours, strike


def _walk_bools(node: Any, key: str, out: set[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, bool):
                out.add("true" if v else "false")
            _walk_bools(v, key, out)
    elif isinstance(node, list):
        for x in node:
            _walk_bools(x, key, out)


def clause_coverage(instruction: str, config: ConfigSpec, workbook: Workbook) -> list[Mention]:
    """Every vocabulary item the instruction mentions, with whether the rules use it."""
    out: list[Mention] = []
    seen: set[tuple[Kind, str]] = set()

    def add(kind: Kind, text: str, covered: bool, note: str) -> None:
        key = (kind, canon_key(text))
        if key not in seen:
            seen.add(key)
            out.append(Mention(kind, text, covered, note))

    governed = set(config.schema_hashes)
    touched_tabs = governed | {str(getattr(r, "target_tab", "")) for r in config.rules} | \
        {str(getattr(r, "to_tab", "")) for r in config.rules}
    columns = used_columns(config)
    literals = used_literals(config)

    for tab in workbook.tabs:
        if _mentions(instruction, tab.name):
            add("tab", tab.name, tab.name in touched_tabs,
                "the rules work on this tab" if tab.name in touched_tabs else "no rule works on this tab")
    header_row = config.header_row
    for tab in workbook.tabs:
        # Columns of the tabs the rules touch, and of any tab the instruction names: a header on an
        # unrelated tab that happens to be an ordinary word ("COLOUR") is not a mention.
        if tab.name not in touched_tabs and not _mentions(instruction, tab.name):
            continue
        headers = tab.row(header_row) if tab.height >= header_row else []
        for raw in headers:
            name = cell_text(raw).strip()
            if not name:
                continue
            canonical = canonicalize(raw, config).strip()
            for spoken in dict.fromkeys([name, canonical]):
                if _mentions(instruction, spoken):
                    used = canon_key(canonical) in columns
                    add("column", canonical, used, "used by the rules" if used else "no rule uses this column")
        if tab.name in governed:
            for idx in enum_columns(tab, header_row):
                labels = {cell_text(r[idx]).strip() for r in tab.values[header_row:]
                          if idx < len(r) and not is_empty(r[idx])}
                for label in sorted(labels):
                    if _mentions(instruction, label):
                        used = _value_covered(label, config, literals)
                        add("value", label, used, "the rules handle this value" if used
                            else "no rule mentions this value")

    colours, strike = _styles(config)
    for word in COLOURS:
        if _mentions(instruction, word):
            hit = any(word in name or (word == "gray" and "grey" in name) for name in colours)
            add("colour", word, hit, "a rule uses this colour" if hit else "no rule uses this colour")
    if any(_mentions(instruction, w) for w in STRIKE_WORDS):
        add("colour", "strike-through", strike, "a rule strikes rows through" if strike
            else "no rule strikes anything through")

    kinds = {type(r.trigger) for r in config.rules}
    text = instruction.lower()
    if (m := TOO_OFTEN.search(text)) is not None:
        add("timing", m.group(0), False, "the script can run at most once an hour")
    if (m := SCHEDULE.search(text)) is not None:
        ok = ScheduleTrigger in kinds
        add("timing", m.group(0), ok, "a rule runs on a schedule" if ok else "no rule runs on a schedule")
    if (m := ON_EDIT.search(text)) is not None:
        ok = OnEditTrigger in kinds
        add("timing", m.group(0), ok, "a rule runs straight after edits" if ok
            else "no rule runs straight after an edit" + (" (they wait for edits to stop)"
                                                          if DebouncedTrigger in kinds else ""))
    if not any(m.kind == "timing" for m in out):
        add("timing", "(not said)", True, "you did not say when; the readback shows the timing chosen")
    return out

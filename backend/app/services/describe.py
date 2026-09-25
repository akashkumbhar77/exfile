"""Plain-English readback of a config (SPEC-PATCH-003 A.2).

Deterministic templates per action type, trigger and condition. No LLM in the read path: the
same config (and live tab list) always yields the same text, so what the owner approves is
exactly what the engine will do.

Colours are structured segments ({kind: "color", name, hex}) so the page can draw a swatch with
the hex in a tooltip; the plain-text form uses the colour name only, never bare hex.
Rules refer to each other by their number in the list. When a live workbook is supplied, tab
selectors are resolved against it ("currently: ...", in sheet order) and missing governed tabs
are called out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas.config import (
    AfterTrigger,
    AllCondition,
    AnyCondition,
    ClearRule,
    Condition,
    ConfigSpec,
    ConsolidateRule,
    CopyRule,
    DateCondition,
    DebouncedTrigger,
    DedupeRule,
    EnumCondition,
    FormatRule,
    MoveRule,
    NotCondition,
    NumericAbs,
    NumericAdd,
    NumericColumn,
    NumericCondition,
    NumericDivide,
    NumericExpression,
    NumericLiteral,
    NumericMultiply,
    NumericRange,
    NumericRound,
    NumericSubtract,
    OnEditTrigger,
    Rule,
    ScheduleTrigger,
    SortKey,
    SortRule,
    Style,
    TabSelector,
    Trigger,
    ValidateRule,
    ValueCondition,
)
from app.services import cron
from app.services.backup import BACKUP_TAB, KEEP_RUNS
from app.services.grid import Workbook

NAMED_COLOURS: dict[str, str] = {
    "#000000": "black", "#FFFFFF": "white", "#FF0000": "red", "#CC0000": "dark red", "#008000": "green",
    "#38761D": "dark green", "#B6D7A8": "light green", "#D4A017": "gold", "#FCE4EC": "pink",
    "#FFF2CC": "light yellow", "#FFFF00": "yellow", "#1155CC": "blue", "#0000FF": "blue",
    "#666666": "grey", "#999999": "grey", "#B45F06": "brown", "#FF9900": "orange", "#9900FF": "purple",
}
_BASIC = {"black": (0, 0, 0), "white": (255, 255, 255), "red": (220, 0, 0), "green": (0, 150, 0),
          "blue": (0, 70, 220), "yellow": (240, 220, 0), "orange": (255, 150, 0), "purple": (140, 0, 200),
          "pink": (250, 200, 220), "grey": (128, 128, 128), "brown": (140, 80, 20)}

Segment = dict[str, str]  # {"kind": "text", "text"} | {"kind": "color", "name", "hex"}


def colour_name(hex_: str) -> str:
    h = hex_.upper()
    name = NAMED_COLOURS.get(h)
    if name is None:
        rgb = tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))
        name = min(_BASIC, key=lambda n: sum((a - b) ** 2 for a, b in zip(rgb, _BASIC[n]))) + "-ish"
    return name


def colour(hex_: str) -> Segment:
    return {"kind": "color", "name": colour_name(hex_), "hex": hex_.upper()}


def text(s: str) -> Segment:
    return {"kind": "text", "text": s}


def plain(segments: list[Segment]) -> str:
    return "".join(s["text"] if s["kind"] == "text" else s["name"] for s in segments)


def quote(s: str) -> str:
    return f"“{s}”"


def join_words(items: list[str], last: str = "and") -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" {last} " + items[-1]


def duration(seconds: int) -> str:
    if seconds % 60 == 0:
        m = seconds // 60
        return f"{m} minute{'s' if m != 1 else ''}"
    return f"{seconds} seconds"


@dataclass(frozen=True)
class Scope:
    """What a readback is rendered against: rule numbers, and optionally the live workbook."""

    config: ConfigSpec
    workbook: Workbook | None = None

    def rule_ref(self, rule_id: str) -> str:
        for i, r in enumerate(self.config.rules, 1):
            if r.id == rule_id:
                return f"rule {i}"
        return f"rule {quote(rule_id)}"

    def tabs(self, sel: TabSelector) -> str:
        from app.services.rules.tabs import resolve_tabs

        if isinstance(sel, str):
            base = f"every tab with a {sel.split(':', 1)[1]} column"
            if self.workbook is None:
                return base
            live = resolve_tabs(sel, self.workbook, self.config)
            return f"{base} (currently: {join_words(live) if live else 'none'})"
        names = list(sel)
        base = "the " + join_words(names) + (" tabs" if len(names) > 1 else " tab")
        if self.workbook is not None:
            missing = [n for n in names if self.workbook.tab(n) is None]
            if missing:
                return f"{base} (not in the sheet now: {join_words(missing)})"
        return base


def trigger_text(t: Trigger, scope: Scope) -> str:
    match t:
        case OnEditTrigger():
            cols = t.on_edit.columns
            return f"whenever {join_words(list(cols), 'or')} is edited" if cols else "whenever the tab is edited"
        case AfterTrigger():
            return f"right after {scope.rule_ref(t.after)} runs"
        case DebouncedTrigger():
            return f"{duration(t.debounced.quiet_seconds)} after edits stop"
        case ScheduleTrigger():
            return cron.describe(t.schedule.cron)
    raise TypeError(type(t).__name__)


def condition_text(c: Condition, cell_column: str | None = None, top: bool = True) -> str:
    match c:
        case EnumCondition():
            return f"{c.column or c.enum} is {c.is_}"
        case DateCondition():
            if c.before is not None:
                return f"{c.date} is before {c.before}"
            return f"{c.date} is after {c.after}"
        case NumericCondition():
            return numeric_condition_text(c)
        case ValueCondition():
            col = c.column or (f"the {cell_column} cell" if cell_column else "the cell")
            if c.is_blank is not None:
                return f"{col} is {'blank' if c.is_blank else 'not blank'}"
            if c.contains is not None:
                return f"{col} contains {quote(c.contains)}"
            return f"{col} is {quote(str(c.equals))}"
        case AllCondition():
            s = " and ".join(condition_text(x, cell_column, False) for x in c.all)
            return s if top or len(c.all) == 1 else f"({s})"
        case AnyCondition():
            s = " or ".join(condition_text(x, cell_column, False) for x in c.any)
            return s if top or len(c.any) == 1 else f"({s})"
        case NotCondition():
            return f"not ({condition_text(c.not_, cell_column, True)})"
    raise TypeError(type(c).__name__)


def _operand(expr: NumericExpression) -> str:
    """A sub-expression inside arithmetic: bracketed when it is itself arithmetic, so
    (A + B) × C never reads as A + B × C."""
    text = numeric_expression_text(expr)
    return f"({text})" if isinstance(expr, (NumericAdd, NumericSubtract, NumericMultiply, NumericDivide)) else text


def _literal_text(n: int | float) -> str:
    return str(int(n)) if isinstance(n, float) and n.is_integer() else str(n)


def numeric_expression_text(expr: NumericExpression) -> str:
    match expr:
        case NumericLiteral():
            return _literal_text(expr.literal)
        case NumericColumn():
            return expr.column
        case NumericAdd():
            return " + ".join(_operand(x) for x in expr.add)
        case NumericSubtract():
            return " − ".join(_operand(x) for x in expr.subtract)
        case NumericMultiply():
            return " × ".join(_operand(x) for x in expr.multiply)
        case NumericDivide():
            return " ÷ ".join(_operand(x) for x in expr.divide)
        case NumericAbs():
            return f"the absolute value of ({numeric_expression_text(expr.abs)})"
        case NumericRound():
            suffix = "place" if expr.round.digits == 1 else "places"
            return f"{_operand(expr.round.value)} rounded to {expr.round.digits} decimal {suffix}"
    raise TypeError(type(expr).__name__)


def numeric_condition_text(c: NumericCondition) -> str:
    left = numeric_expression_text(c.numeric.left)
    right = c.numeric.right
    if isinstance(right, NumericRange):
        lower = numeric_expression_text(right.minimum)
        upper = numeric_expression_text(right.maximum)
        phrase = "is between" if c.numeric.op == "between" else "is not between"
        return f"{left} {phrase} {lower} and {upper} (inclusive)"
    op = {
        "eq": "is equal to", "ne": "is not equal to", "gt": "is greater than", "gte": "is at least",
        "lt": "is less than", "lte": "is at most",
    }[c.numeric.op]
    return f"{left} {op} {numeric_expression_text(right)}"


def is_overdue(c: Condition) -> bool:
    """A condition that tests a date being before today marks the overdue case."""
    match c:
        case DateCondition():
            return c.before == "today"
        case AllCondition():
            return any(is_overdue(x) for x in c.all)
        case AnyCondition():
            return any(is_overdue(x) for x in c.any)
    return False


def cell_condition_text(c: Condition, column: str) -> str:
    """Cell-rule condition: 'cells that are “YES”' when it tests the cell itself."""
    if isinstance(c, ValueCondition) and c.column is None:
        if c.is_blank is not None:
            return "cells that are blank" if c.is_blank else "cells that are not blank"
        if c.contains is not None:
            return f"cells containing {quote(c.contains)}"
        return f"cells that are {quote(str(c.equals))}"
    return f"cells where {condition_text(c, column)}"


def style_segments(s: Style) -> list[Segment]:
    segs: list[Segment] = []
    if s.font is not None:
        segs += [colour(s.font), text(" text")]
    if s.background not in (None, "none"):
        segs += [text(" on " if segs else ""), colour(str(s.background))]
        if s.font is None:
            segs.append(text(" background"))
    extras = []
    if s.background == "none":
        extras.append("no fill")
    if s.strike is True:
        extras.append("struck through")
    elif s.strike is False and s.font is None and s.background is None:
        extras.append("not struck through")
    if extras:
        segs.append(text((", " if segs else "") + ", ".join(extras)))
    return segs or [text("unchanged")]


def sort_key_text(k: SortKey) -> str:
    if k.using_enum:
        return f"{k.column} stage (in the order shown under Stages)"
    direction = {("date", "asc"): "oldest first", ("date", "desc"): "newest first",
                 ("number", "asc"): "smallest first", ("number", "desc"): "largest first",
                 ("text", "asc"): "A to Z", ("text", "desc"): "Z to A"}.get(
        (k.type, k.order), "ascending" if k.order == "asc" else "descending")
    return f"{k.column}, {direction}, blanks {k.blanks}"


@dataclass(frozen=True)
class Detail:
    segments: list[Segment]

    @property
    def text(self) -> str:
        return plain(self.segments)


def d(*parts: str | Segment | list[Segment]) -> Detail:
    segs: list[Segment] = []
    for p in parts:
        if isinstance(p, str):
            segs.append(text(p))
        elif isinstance(p, list):
            segs += p
        else:
            segs.append(p)
    merged: list[Segment] = []
    for s in segs:  # merge adjacent text for a clean payload
        if s["kind"] == "text" and s["text"] == "":
            continue
        if s["kind"] == "text" and merged and merged[-1]["kind"] == "text":
            merged[-1] = text(merged[-1]["text"] + s["text"])
        else:
            merged.append(s)
    return Detail(merged)


@dataclass(frozen=True)
class RuleText:
    number: int
    id: str
    action: str
    headline: str
    when: str
    details: list[Detail] = field(default_factory=list)


@dataclass(frozen=True)
class ConfigText:
    summary: str
    governed_tabs: list[str]
    tabs_missing: list[str]
    stages: list[str]
    rules: list[RuleText]
    guards: list[str]

    def lines(self) -> list[str]:
        out = [self.summary, ""]
        out += self.stages
        if self.stages:
            out.append("")
        for r in self.rules:
            out.append(f"{r.number}. {r.headline} ({r.when}).")
            out += [f"   - {x.text}" for x in r.details]
        out.append("")
        out += self.guards
        return out

    def json(self) -> dict[str, Any]:
        return {
            "summary": self.summary, "governed_tabs": self.governed_tabs, "tabs_missing": self.tabs_missing,
            "stages": self.stages,
            "rules": [{"number": r.number, "id": r.id, "action": r.action, "headline": r.headline, "when": r.when,
                       "details": [{"text": x.text, "segments": x.segments} for x in r.details]}
                      for r in self.rules],
            "guards": self.guards, "lines": self.lines(),
        }


def rule_text(r: Rule, n: int, scope: Scope) -> RuleText:
    when = trigger_text(r.trigger, scope)
    match r:
        case SortRule():
            keys = [sort_key_text(k) for k in r.keys]
            return RuleText(n, r.id, r.action,
                            f"Keep {scope.tabs(r.tabs)} sorted by {join_words(keys, 'then by')}", when)
        case FormatRule():
            details = [d("Each row starts as ", style_segments(r.default),
                         "; the rules below are applied in order, and a later match wins.")]
            details += [d(f"Where {condition_text(rr.when)}{' (overdue)' if is_overdue(rr.when) else ''}: ",
                          style_segments(rr), ".") for rr in r.row_rules]
            details += [d(f"In the {cr.column} column, {cell_condition_text(cr.when, cr.column)}: ",
                          style_segments(cr), ".") for cr in r.cell_rules]
            return RuleText(n, r.id, r.action, f"Color rows in {scope.tabs(r.tabs)}", when, details)
        case ConsolidateRule():
            details = [d("Columns are lined up by name across tabs.")]
            details += [d(f"A {quote(p.name)} column comes first, holding the source tab's name.")
                        for p in r.prepend_columns]
            details += [d(f"Empty {quote(x.name)} cells are filled with the source tab's name.") for x in r.derived]
            if r.sort_like:
                details.append(d(f"Sorted the same way as {scope.rule_ref(r.sort_like)}."))
            if r.format_like:
                details.append(d(f"Colored the same way as {scope.rule_ref(r.format_like)}."))
            details.append(d(f"Title banner: {quote(r.presentation.title)}." if r.presentation.title
                             else "Keeps the tab's current title banner, if it has one."))
            details.append(d("Locked against manual edits (it is rebuilt automatically)." if r.lock
                             else "Not locked."))
            return RuleText(n, r.id, r.action, f"Rebuild the {r.target_tab} tab from {scope.tabs(r.sources)}",
                            when, details)
        case MoveRule():
            return RuleText(n, r.id, r.action, f"Move rows in {scope.tabs(r.tabs)} where {condition_text(r.when)} "
                            f"to the {r.position} of the {r.to_tab} tab", when,
                            [d("Values are matched by column name; if a value has nowhere to go, nothing is moved.")])
        case CopyRule():
            return RuleText(n, r.id, r.action, f"Copy rows in {scope.tabs(r.tabs)} where {condition_text(r.when)} "
                            f"to the {r.position} of the {r.to_tab} tab", when,
                            [d(f"A row is copied only once, identified by {join_words(list(r.key_columns))}.")])
        case ValidateRule():
            options = (join_words([quote(v) for v in r.values], "or") if r.values
                       else f"the {r.from_enum} stages")
            mode = "other values are allowed with a warning" if r.allow_invalid else "other values are rejected"
            return RuleText(n, r.id, r.action, f"Add a dropdown to the {r.column} column in {scope.tabs(r.tabs)} "
                            f"offering {options}", when, [d(f"Existing values are never changed; {mode}.")])
        case DedupeRule():
            return RuleText(n, r.id, r.action, f"Remove duplicate rows in {scope.tabs(r.tabs)} that share the "
                            f"same {join_words(list(r.key_columns))}, keeping the {r.keep} one", when,
                            [d("Rows whose key cells are all empty are never removed.")])
        case ClearRule():
            return RuleText(n, r.id, r.action, f"Clear {join_words(list(r.columns))} in {scope.tabs(r.tabs)} where "
                            f"{condition_text(r.when)}", when,
                            [d("Rows are never deleted, only those cells emptied.")])
    raise TypeError(type(r).__name__)


def stage_match_text(match: str) -> str:
    kind, _, needle = match.partition(":")
    return f"labels {'containing' if kind == 'contains' else 'equal to'} {quote(needle.strip())}"


def describe_config(config: ConfigSpec, workbook: Workbook | None = None) -> ConfigText:
    scope = Scope(config, workbook)
    governed = set(config.schema_hashes)
    if workbook is not None:
        present = [t for t in workbook.names() if t in governed]  # sheet order
        missing = sorted(governed - set(workbook.names()))
    else:
        present, missing = sorted(governed), []
    stages = []
    for name, spec in config.enums.items():
        parts = [f"{st.value} ({stage_match_text(st.match)})" for st in sorted(spec.stages, key=lambda s: s.order)]
        stages.append(f"{name} stages, in order: {join_words(parts)}; any other label sorts last with plain "
                      "formatting.")
    rules = [rule_text(r, i, scope) for i, r in enumerate(config.rules, 1)]
    n, k = len(governed), len(rules)
    head = f"Organizes {n} tab{'s' if n != 1 else ''} with {k} rule{'s' if k != 1 else ''}"
    if workbook is not None:
        summary = f"{head} — currently: {join_words(present) if present else 'none'}"
        if missing:
            summary += f"; not in the sheet now: {join_words(missing)}"
        summary += "."
    else:
        summary = f"{head} ({join_words(present)})."
    guards = [f"Rows with {quote(config.guards.hold_column)} set are never touched.",
              f"A run that would change more than {config.guards.max_rows_per_run} rows is stopped before "
              "writing anything.",
              "If a header in a governed tab is renamed, the sheet pauses instead of running rules."]
    if config.guards.backup_tab:
        guards.append(f"Rows that rules remove or overwrite are first copied to a hidden {BACKUP_TAB} tab "
                      f"(the last {KEEP_RUNS} runs are kept).")
    return ConfigText(summary, present, missing, stages, rules, guards)

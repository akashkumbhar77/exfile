"""Plain-English readback of a config (SPEC-PATCH-003 A.2).

Deterministic templates per action type, trigger and condition. No LLM in the read path: the
same config always yields the same text, so what the owner approves is exactly what the
engine will do. Colours are named from a fixed palette (nearest basic name for others) and
always shown with their hex.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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

NAMED_COLOURS: dict[str, str] = {
    "#000000": "black", "#FFFFFF": "white", "#FF0000": "red", "#CC0000": "dark red", "#008000": "green",
    "#38761D": "dark green", "#B6D7A8": "light green", "#D4A017": "gold", "#FCE4EC": "pink",
    "#FFF2CC": "light yellow", "#FFFF00": "yellow", "#1155CC": "blue", "#0000FF": "blue",
    "#666666": "grey", "#999999": "grey", "#B45F06": "brown", "#FF9900": "orange", "#9900FF": "purple",
}
_BASIC = {"black": (0, 0, 0), "white": (255, 255, 255), "red": (220, 0, 0), "green": (0, 150, 0),
          "blue": (0, 70, 220), "yellow": (240, 220, 0), "orange": (255, 150, 0), "purple": (140, 0, 200),
          "pink": (250, 200, 220), "grey": (128, 128, 128), "brown": (140, 80, 20)}


def colour(hex_: str) -> str:
    h = hex_.upper()
    name = NAMED_COLOURS.get(h)
    if name is None:
        rgb = tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))
        name = min(_BASIC, key=lambda n: sum((a - b) ** 2 for a, b in zip(rgb, _BASIC[n]))) + "-ish"
    return f"{name} ({h})"


def quote(s: str) -> str:
    return f"“{s}”"


def join_words(items: list[str], last: str = "and") -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" {last} " + items[-1]


def tabs_text(sel: TabSelector) -> str:
    if isinstance(sel, str):
        return f"every tab with a {sel.split(':', 1)[1]} column"
    return "the " + join_words(list(sel)) + (" tabs" if len(sel) > 1 else " tab")


def duration(seconds: int) -> str:
    if seconds % 60 == 0:
        m = seconds // 60
        return f"{m} minute{'s' if m != 1 else ''}"
    return f"{seconds} seconds"


def trigger_text(t: Trigger) -> str:
    match t:
        case OnEditTrigger():
            cols = t.on_edit.columns
            return f"whenever {join_words(list(cols), 'or')} is edited" if cols else "whenever the tab is edited"
        case AfterTrigger():
            return f"right after rule {quote(t.after)} runs"
        case DebouncedTrigger():
            return f"{duration(t.debounced.quiet_seconds)} after edits stop"
        case ScheduleTrigger():
            return f"on the schedule {quote(t.schedule.cron.strip())} (cron)"
    raise TypeError(type(t).__name__)


def condition_text(c: Condition, cell_column: str | None = None, top: bool = True) -> str:
    match c:
        case EnumCondition():
            return f"{c.column or c.enum} is {c.is_}"
        case DateCondition():
            if c.before is not None:
                return f"{c.date} is before {c.before}"
            return f"{c.date} is after {c.after}"
        case ValueCondition():
            col = c.column or (f"the {cell_column} cell" if cell_column else "the cell")
            if c.is_blank is not None:
                return f"{col} is {'blank' if c.is_blank else 'not blank'}"
            if c.contains is not None:
                return f"{col} contains {quote(c.contains)}"
            return f"{col} is {quote(str(c.equals))}"
        case AllCondition():
            text = " and ".join(condition_text(x, cell_column, False) for x in c.all)
            return text if top or len(c.all) == 1 else f"({text})"
        case AnyCondition():
            text = " or ".join(condition_text(x, cell_column, False) for x in c.any)
            return text if top or len(c.any) == 1 else f"({text})"
        case NotCondition():
            return f"not ({condition_text(c.not_, cell_column, True)})"
    raise TypeError(type(c).__name__)


def cell_condition_text(c: Condition, column: str) -> str:
    """Cell-rule condition: 'cells that are “YES”' when it tests the cell itself."""
    if isinstance(c, ValueCondition) and c.column is None:
        if c.is_blank is not None:
            return "cells that are blank" if c.is_blank else "cells that are not blank"
        if c.contains is not None:
            return f"cells containing {quote(c.contains)}"
        return f"cells that are {quote(str(c.equals))}"
    return f"cells where {condition_text(c, column)}"


def style_text(s: Style) -> str:
    parts: list[str] = []
    if s.font is not None and s.background not in (None, "none"):
        parts.append(f"{colour(s.font)} text on {colour(str(s.background))}")
    elif s.font is not None:
        parts.append(f"{colour(s.font)} text")
    elif s.background not in (None, "none"):
        parts.append(f"{colour(str(s.background))} background")
    if s.background == "none":
        parts.append("no fill")
    if s.strike is True:
        parts.append("struck through")
    elif s.strike is False and s.font is None and s.background is None:
        parts.append("not struck through")
    return ", ".join(parts) or "unchanged"


def sort_key_text(k: SortKey, config: ConfigSpec) -> str:
    if k.using_enum:
        spec = config.enums.get(k.using_enum)
        order = " → ".join(st.value for st in sorted(spec.stages, key=lambda s: s.order)) if spec else "?"
        return f"{k.column} stage ({order}; anything else last)"
    kind = k.type
    if kind == "date":
        direction = "oldest first" if k.order == "asc" else "newest first"
    elif kind == "number":
        direction = "smallest first" if k.order == "asc" else "largest first"
    elif kind == "text":
        direction = "A to Z" if k.order == "asc" else "Z to A"
    else:
        direction = "ascending" if k.order == "asc" else "descending"
    return f"{k.column}, {direction}, blanks {k.blanks}"


@dataclass(frozen=True)
class RuleText:
    id: str
    action: str
    headline: str
    when: str
    details: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ConfigText:
    summary: str
    governed_tabs: list[str]
    stages: list[str]
    rules: list[RuleText]
    guards: list[str]

    def lines(self) -> list[str]:
        out = [self.summary, ""]
        for s in self.stages:
            out.append(s)
        if self.stages:
            out.append("")
        for i, r in enumerate(self.rules, 1):
            out.append(f"{i}. {r.headline} ({r.when}).")
            out += [f"   - {d}" for d in r.details]
        out.append("")
        out += self.guards
        return out


def rule_text(r: Rule, config: ConfigSpec) -> RuleText:
    when = trigger_text(r.trigger)
    match r:
        case SortRule():
            keys = [sort_key_text(k, config) for k in r.keys]
            return RuleText(r.id, r.action, f"Keep {tabs_text(r.tabs)} sorted by {join_words(keys, 'then by')}", when)
        case FormatRule():
            details = [f"Where {condition_text(rr.when)}: {style_text(rr)}." for rr in r.row_rules]
            details += [f"In the {cr.column} column, {cell_condition_text(cr.when, cr.column)}: {style_text(cr)}."
                        for cr in r.cell_rules]
            details.append(f"Each row starts as {style_text(r.default)}; the rules above are applied in order, "
                           "and a later match wins.")
            return RuleText(r.id, r.action, f"Color rows in {tabs_text(r.tabs)}", when, details)
        case ConsolidateRule():
            details = ["Columns are lined up by name across tabs."]
            details += [f"A {quote(p.name)} column comes first, holding the source tab's name."
                        for p in r.prepend_columns]
            details += [f"Empty {quote(d.name)} cells are filled with the source tab's name." for d in r.derived]
            if r.sort_like:
                details.append(f"Sorted the same way as rule {quote(r.sort_like)}.")
            if r.format_like:
                details.append(f"Colored the same way as rule {quote(r.format_like)}.")
            if r.presentation.title:
                details.append(f"Title banner: {quote(r.presentation.title)}.")
            details.append("Locked against manual edits (it is rebuilt automatically)." if r.lock
                           else "Not locked.")
            return RuleText(r.id, r.action, f"Rebuild the {r.target_tab} tab from {tabs_text(r.sources)}",
                            when, details)
        case MoveRule():
            return RuleText(r.id, r.action, f"Move rows in {tabs_text(r.tabs)} where {condition_text(r.when)} to "
                            f"the {position(r.position)} of the {r.to_tab} tab", when,
                            ["Values are matched by column name; if a value has nowhere to go, nothing is moved."])
        case CopyRule():
            return RuleText(r.id, r.action, f"Copy rows in {tabs_text(r.tabs)} where {condition_text(r.when)} to "
                            f"the {position(r.position)} of the {r.to_tab} tab", when,
                            [f"A row is copied only once, identified by {join_words(list(r.key_columns))}."])
        case ValidateRule():
            options = (join_words([quote(v) for v in r.values], "or") if r.values
                       else f"the {r.from_enum} stages")
            mode = "other values are allowed with a warning" if r.allow_invalid else "other values are rejected"
            return RuleText(r.id, r.action, f"Add a dropdown to the {r.column} column in {tabs_text(r.tabs)} "
                            f"offering {options}", when, [f"Existing values are never changed; {mode}."])
        case DedupeRule():
            return RuleText(r.id, r.action, f"Remove duplicate rows in {tabs_text(r.tabs)} that share the same "
                            f"{join_words(list(r.key_columns))}, keeping the {r.keep} one", when,
                            ["Rows whose key cells are all empty are never removed."])
        case ClearRule():
            return RuleText(r.id, r.action, f"Clear {join_words(list(r.columns))} in {tabs_text(r.tabs)} where "
                            f"{condition_text(r.when)}", when, ["Rows are never deleted, only those cells emptied."])
    raise TypeError(type(r).__name__)


def position(p: str) -> str:
    return "top" if p == "top" else "bottom"


def describe_config(config: ConfigSpec) -> ConfigText:
    governed = list(config.schema_hashes)
    stages = []
    for name, spec in config.enums.items():
        parts = [f"{st.value} ({st.match.split(':', 1)[0]} {quote(st.match.split(':', 1)[1].strip())})"
                 for st in sorted(spec.stages, key=lambda s: s.order)]
        stages.append(f"{name} stages, in order: {join_words(parts)}; any other value sorts last with plain "
                      "formatting.")
    rules = [rule_text(r, config) for r in config.rules]
    summary = (f"Organizes {len(governed)} tab{'s' if len(governed) != 1 else ''} "
               f"({join_words(governed)}) with {len(rules)} rule{'s' if len(rules) != 1 else ''}.")
    guards = [f"Rows with {quote(config.guards.hold_column)} set are never touched.",
              f"A run that would change more than {config.guards.max_rows_per_run} rows is stopped before "
              "writing anything.",
              "If a header in a governed tab is renamed, the sheet pauses instead of running rules."]
    return ConfigText(summary, governed, stages, rules, guards)

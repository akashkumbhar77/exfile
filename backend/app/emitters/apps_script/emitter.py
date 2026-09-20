"""Compile a ConfigSpec into a standalone Apps Script file (SPEC-PATCH-004, target `apps_script`).

The output is specialized code, not an interpreter: one function per rule, with the config's
columns, stages, colours and thresholds written out as literals. The config JSON rides along as a
trailing comment for traceability only (C.3) and is never parsed at runtime.

Semantics come from ConfigSpec and mirror the Python evaluator, which stays the reference: a
parity difference is a bug in this emitter (PATCH-004 D.6). Anything this target cannot express is
refused by `capabilities.check_config` before a single line is emitted (A.3).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from string import Template

from app.emitters import capabilities
from app.schemas.config import (
    AllCondition,
    AnyCondition,
    CellFormatRule,
    Condition,
    ConfigSpec,
    DateCondition,
    EnumCondition,
    EnumSpec,
    FormatRule,
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
    Rule,
    RowFormatRule,
    SortKey,
    SortRule,
    Style,
)
from app.services.describe import (
    Scope,
    cell_condition_text,
    condition_text,
    describe_config,
    plain,
    style_segments,
)
from app.services.grid import Workbook
from app.services.headers import canon_key
from app.services.validator import ValidationIssue

PRODUCT = "Sheets Automation"
GENERATOR = f"apps-script-emitter 0.1.0 ({capabilities.MILESTONE})"
ENTRY_POINT = "organizeNow"
TEMPLATES = Path(__file__).resolve().parent / "templates"


@dataclass(frozen=True)
class EmitResult:
    target: str
    script: str | None
    refusals: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.script is not None


@lru_cache(maxsize=None)
def template(name: str) -> Template:
    return Template((TEMPLATES / name).read_text(encoding="utf-8"))


def fill(name: str, **values: object) -> str:
    return template(name).substitute(**values)


# ---------------------------------------------------------------- JS literals and names


def js_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def js_number(value: int | float) -> str:
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def js_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return js_number(value)
    return js_string(str(value))


def _ident(text: str) -> str:
    out = re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_")
    return out or "x"


def rule_fn(rule_id: str) -> str:
    return f"rule_{_ident(rule_id)}_"


def stage_fn(enum_name: str) -> str:
    return f"stage_{_ident(enum_name)}_"


# ---------------------------------------------------------------- per-rule column variables


class Columns:
    """Column lookups a rule needs, as `var col_X = view.cols['X'];` lines."""

    def __init__(self) -> None:
        self._vars: dict[str, str] = {}

    def var(self, column: str) -> str:
        key = canon_key(column)
        if key not in self._vars:
            name = f"col_{_ident(key)}"
            taken = set(self._vars.values())
            if name in taken:  # different headers, same identifier after sanitising
                name = f"{name}_{len(self._vars)}"
            self._vars[key] = name
        return self._vars[key]

    def declarations(self, indent: str = "  ") -> str:
        return "\n".join(f"{indent}var {var} = view.cols[{js_string(key)}];"
                         for key, var in self._vars.items())

    def present(self, column: str) -> str:
        return f"{self.var(column)} !== undefined"


WIDTH = 108


def wrap_comment(text: str, prefix: str, hanging: str | None = None) -> str:
    """A comment that stays inside the line budget; continuation lines are indented under it."""
    cont = hanging if hanging is not None else prefix
    lines: list[str] = []
    current = prefix
    for word in text.split():
        candidate = f"{current} {word}" if current not in (prefix, cont) else f"{current}{word}"
        if len(candidate) > WIDTH and current not in (prefix, cont):
            lines.append(current)
            current = f"{cont}{word}"
        else:
            current = candidate
    lines.append(current)
    return "\n".join(lines)


def wrap_condition(expr: str, indent: str) -> str:
    """`if (...) {`, broken at the top-level and/or when it would run long."""
    head = f"{indent}if ({expr}) {{"
    if len(head) <= WIDTH:
        return head
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(expr):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 1 and expr[i:i + 4] in (" && ", " || "):
            parts.append((expr[start:i], expr[i + 1:i + 3]))
            start = i + 4
    if not parts:
        return head
    parts.append((expr[start:], ""))
    out = [f"{indent}if ({parts[0][0]}"]
    for (text, _), (_, op) in zip(parts[1:], parts[:-1]):
        out.append(f"{indent}    {op} {text}")
    out[-1] += ") {"
    return "\n".join(out)


# ---------------------------------------------------------------- conditions


def numeric_expr(expr: NumericExpression, cols: Columns) -> str:
    match expr:
        case NumericLiteral():
            return js_number(expr.literal)
        case NumericColumn():
            var = cols.var(expr.column)
            return f"({var} === undefined ? null : numberCell_(row[{var}]))"
        case NumericAdd():
            return _fold("numAdd_", [numeric_expr(x, cols) for x in expr.add])
        case NumericSubtract():
            a, b = (numeric_expr(x, cols) for x in expr.subtract)
            return f"numSub_({a}, {b})"
        case NumericMultiply():
            return _fold("numMul_", [numeric_expr(x, cols) for x in expr.multiply])
        case NumericDivide():
            a, b = (numeric_expr(x, cols) for x in expr.divide)
            return f"numDiv_({a}, {b})"
        case NumericAbs():
            return f"numAbs_({numeric_expr(expr.abs, cols)})"
        case NumericRound():
            return f"numRound_({numeric_expr(expr.round.value, cols)}, {expr.round.digits})"
    raise TypeError(type(expr).__name__)  # pragma: no cover - the schema is a closed union


def _fold(fn: str, parts: list[str]) -> str:
    out = parts[0]
    for part in parts[1:]:
        out = f"{fn}({out}, {part})"
    return out


def _date_ms(ref: str) -> str:
    if ref == "today":
        return "today"
    d = datetime.strptime(ref, "%Y-%m-%d")
    return f"new Date({d.year}, {d.month - 1}, {d.day}).getTime()"


def condition_js(cond: Condition, cols: Columns, cell_column: str | None = None) -> str:
    """A JS boolean expression for one condition, false whenever a column is missing."""
    match cond:
        case AllCondition():
            return "(" + " && ".join(condition_js(c, cols, cell_column) for c in cond.all) + ")"
        case AnyCondition():
            return "(" + " || ".join(condition_js(c, cols, cell_column) for c in cond.any) + ")"
        case NotCondition():
            return f"!{condition_js(cond.not_, cols, cell_column)}"
        case EnumCondition():
            column = cond.column or cond.enum
            var = cols.var(column)
            want = js_string(canon_key(cond.is_))
            return f"({cols.present(column)} && {stage_fn(cond.enum)}(row[{var}]).value === {want})"
        case DateCondition():
            var = cols.var(cond.date)
            fn = "dateBefore_" if cond.before is not None else "dateAfter_"
            ref: str = cond.before if cond.before is not None else str(cond.after)
            return f"({cols.present(cond.date)} && {fn}(row[{var}], {_date_ms(ref)}))"
        case NumericCondition():
            left = numeric_expr(cond.numeric.left, cols)
            right = cond.numeric.right
            if isinstance(right, NumericRange):
                fn = "numBetween_" if cond.numeric.op == "between" else "numNotBetween_"
                lo, hi = numeric_expr(right.minimum, cols), numeric_expr(right.maximum, cols)
                return f"{fn}({left}, {lo}, {hi})"
            ops = {"eq": "numEq_", "ne": "numNe_", "gt": "numGt_", "gte": "numGte_",
                   "lt": "numLt_", "lte": "numLte_"}
            return f"{ops[cond.numeric.op]}({left}, {numeric_expr(right, cols)})"
        case _:  # ValueCondition
            target = cond.column or cell_column
            assert target is not None, "value condition without a column outside a cell rule"
            var = cols.var(target)
            if cond.is_blank is not None:
                test = f"isEmpty_(row[{var}])" if cond.is_blank else f"!isEmpty_(row[{var}])"
            elif cond.contains is not None:
                test = f"normText_(row[{var}]).indexOf({js_string(cond.contains.strip().upper())}) !== -1"
            else:
                want = js_string(canon_key(str(cond.equals)) if not isinstance(cond.equals, bool)
                                 else ("TRUE" if cond.equals else "FALSE"))
                test = f"normText_(row[{var}]) === {want}"
            return f"({cols.present(target)} && {test})"


# ---------------------------------------------------------------- rule bodies


def sort_comparisons(keys: list[SortKey], cols: Columns) -> str:
    out: list[str] = []
    for key in keys:
        var = cols.var(key.column)
        direction = -1 if key.order == "desc" else 1
        note = _sort_note(key)
        if key.using_enum is not None:
            fn = stage_fn(key.using_enum)
            expr = f"{fn}(a.row[{var}]).order - {fn}(b.row[{var}]).order"
            body = f"      d = {expr};" if direction == 1 else f"      d = -({expr});"
        else:
            blanks = 1 if key.blanks == "last" else -1
            reader = {"date": "sortDateMs_", "number": "sortNumber_", "text": "sortText_"}.get(key.type)
            if reader is None:  # auto
                body = (f"      d = compareAuto_(autoKey_(a.row[{var}]), autoKey_(b.row[{var}]), "
                        f"{direction}, {blanks});")
            else:
                body = (f"      d = compareKey_({reader}(a.row[{var}]), {reader}(b.row[{var}]), "
                        f"{direction}, {blanks});")
        out.append(f"    if ({cols.present(key.column)}) {{   // {note}\n{body}\n      if (d) return d;\n    }}")
    return "\n".join(out)


def _sort_note(key: SortKey) -> str:
    if key.using_enum is not None:
        order = "reverse stage order" if key.order == "desc" else "stage order"
        return f"{key.column}, by {key.using_enum} {order}"
    words = {"date": ("oldest first", "newest first"), "number": ("smallest first", "largest first")}
    ascending, descending = words.get(key.type, ("A to Z", "Z to A"))
    return f"{key.column}, {descending if key.order == 'desc' else ascending}, blanks {key.blanks}"


def style_lines(style: Style, indent: str) -> list[str]:
    out: list[str] = []
    if style.font is not None:
        out.append(f"{indent}fillRow_(font, {js_string(style.font.upper())});")
    if style.background is not None:
        value = "null" if style.background == "none" else js_string(style.background.upper())
        out.append(f"{indent}fillRow_(fill, {value});")
    if style.strike is not None:
        out.append(f"{indent}fillRow_(line, {js_string('line-through' if style.strike else 'none')});")
    return out


def cell_style_lines(style: Style, var: str, indent: str) -> list[str]:
    out: list[str] = []
    if style.font is not None:
        out.append(f"{indent}font[{var}] = {js_string(style.font.upper())};")
    if style.background is not None:
        value = "null" if style.background == "none" else js_string(style.background.upper())
        out.append(f"{indent}fill[{var}] = {value};")
    if style.strike is not None:
        out.append(f"{indent}line[{var}] = {js_string('line-through' if style.strike else 'none')};")
    return out


def format_body(rule: FormatRule, cols: Columns) -> str:
    out: list[str] = []
    default = style_lines(rule.default, "    ")
    if default:
        out.append("    // every row starts from this, then each matching rule below overrides it")
        out += default
    for rr in rule.row_rules:
        out.append(f"    // {_row_note(rr)}")
        out.append(wrap_condition(condition_js(rr.when, cols), "    "))
        out += style_lines(rr, "      ")
        out.append("    }")
    for cr in rule.cell_rules:
        var = cols.var(cr.column)
        out.append(f"    // {_cell_note(cr)}")
        out.append(wrap_condition(condition_js(cr.when, cols, cr.column), "    "))
        out += cell_style_lines(cr, var, "      ")
        out.append("    }")
    return "\n".join(out)


def _row_note(rr: RowFormatRule) -> str:
    return f"where {condition_text(rr.when)}: {plain(style_segments(rr))}"


def _cell_note(cr: CellFormatRule) -> str:
    return f"where {condition_text(cr.when, cr.column)}: {plain(style_segments(cr))}"


# ---------------------------------------------------------------- assembly


def _selector_js(rule: SortRule | FormatRule) -> tuple[str, str]:
    """(JS test for 'does this rule apply to this tab', human note for the comment)."""
    selector = rule.tabs
    if isinstance(selector, str):  # all_with:COLUMN
        column = canon_key(selector.split(":", 1)[1])
        return f"{js_string(column)} in view.cols", f"every governed tab with a {column} column"
    tests = " || ".join(f"name === {js_string(name)}" for name in selector)
    return f"({tests})", "tabs " + ", ".join(selector)


def _stage_functions(config: ConfigSpec, used: set[str]) -> list[str]:
    out = []
    for name in sorted(used):
        spec: EnumSpec = config.enums[name]
        branches = []
        for stage in sorted(spec.stages, key=lambda s: s.order):
            kind, _, needle = stage.match.partition(":")
            want = js_string(needle.strip().upper())
            test = f"v.indexOf({want}) !== -1" if kind == "contains" else f"v === {want}"
            branches.append(f"    if ({test}) return "
                            f"{{ value: {js_string(canon_key(stage.value))}, order: {stage.order} }};")
        summary = " < ".join(s.value for s in sorted(spec.stages, key=lambda s: s.order))
        out.append(fill("stage.js", enum_name=name, fn=stage_fn(name), order_summary=summary,
                        branches="\n".join(branches), unknown_order=spec.unknown_order))
    return out


def _canonical_function(config: ConfigSpec) -> str:
    branches = []
    for ch in config.canonical_headers:
        tests = []
        for m in ch.match:
            kind, _, needle = m.partition(":")
            want = js_string(needle.strip().upper())
            tests.append(f"v.indexOf({want}) !== -1" if kind == "contains" else f"v === {want}")
        branches.append(f"  if ({' || '.join(tests)}) return {js_string(ch.canonical)};")
    return fill("canonical.js", branches="\n".join(branches))


def _header(config: ConfigSpec, workbook: Workbook | None, generated_at: datetime) -> str:
    described = describe_config(config, workbook)
    summaries = [wrap_comment(f"{r.number}. {r.headline} ({r.when}).", " *   ", " *      ")
                 for r in described.rules]
    absent = [f" *   - {reason}" for reason in capabilities.ABSENT_BY_DESIGN.values()]
    return fill(
        "header.js", product=PRODUCT, generator=GENERATOR, config_version=config.config_version,
        generated_at=generated_at.strftime("%Y-%m-%d %H:%M UTC"),
        governed_tabs=", ".join(sorted(config.schema_hashes)),
        rule_summaries="\n".join(summaries), absent="\n".join(absent), entry_point=ENTRY_POINT,
    )


def _constants(config: ConfigSpec) -> str:
    governed = ",\n".join(
        f"  {{ tab: {js_string(tab)}, headerHash: {js_string(h)} }}"
        for tab, h in sorted(config.schema_hashes.items())
    )
    return fill("constants.js", product=PRODUCT, generator=GENERATOR,
                config_version=config.config_version, header_row=config.header_row,
                data_start_row=config.data_start_row,
                hold_column=canon_key(config.guards.hold_column),
                max_rows=config.guards.max_rows_per_run, governed=governed)


def emit(config: ConfigSpec, *, workbook: Workbook | None = None,
         generated_at: datetime | None = None) -> EmitResult:
    """The whole `.gs` file, or the reasons it cannot be generated (nothing partial, A.3)."""
    refusals = capabilities.check_config(config)
    if refusals:
        return EmitResult(capabilities.TARGET, None, refusals)

    generated_at = generated_at or datetime.now(UTC)
    scope = Scope(config, workbook)
    described = {r.id: r for r in describe_config(config, workbook).rules}
    functions: list[str] = []
    calls: list[str] = []
    enums_used: set[str] = set()

    for rule in config.rules:
        # capabilities.check_config already refused everything else, so this holds
        assert isinstance(rule, (SortRule, FormatRule)), rule.action
        text = described[rule.id]
        cols = Columns()
        selector, tab_note = _selector_js(rule)
        var_name = f"plan{text.number}"
        common = {"number": text.number,
                  "headline": wrap_comment(f"Rule {text.number}: {text.headline}", " * ", " *   ")[3:],
                  "when": wrap_comment(f"Runs: {text.when}", " * ", " *   ")[3:],
                  "fn": rule_fn(rule.id), "rule_id": rule.id}
        if isinstance(rule, SortRule):
            comparisons = sort_comparisons(rule.keys, cols)
            enums_used |= {k.using_enum for k in rule.keys if k.using_enum is not None}
            functions.append(fill("sort.js", columns=cols.declarations(), comparisons=comparisons, **common))
            calls.append(fill("sort_call.js", number=text.number, rule_id=rule.id, tab_note=tab_note,
                              selector=selector, var_name=var_name, fn=rule_fn(rule.id)))
        else:
            body = format_body(rule, cols)
            enums_used |= _enums_in_format(rule)
            functions.append(fill("format.js", columns=cols.declarations(), body=body, **common))
            calls.append(fill("format_call.js", number=text.number, rule_id=rule.id, tab_note=tab_note,
                              selector=selector, var_name=var_name, fn=rule_fn(rule.id)))

    parts = [
        _header(config, workbook, generated_at),
        _constants(config),
        (TEMPLATES / "runtime.js").read_text(encoding="utf-8").rstrip("\n"),
        _canonical_function(config),
        *_stage_functions(config, enums_used),
        *functions,
        fill("entry.js", entry_point=ENTRY_POINT, calls="\n".join(calls).rstrip("\n")),
        _config_comment(config, scope),
    ]
    return EmitResult(capabilities.TARGET, "\n\n".join(p.strip("\n") for p in parts) + "\n", [])


def _enums_in_format(rule: FormatRule) -> set[str]:
    found: set[str] = set()

    def walk(cond: Condition) -> None:
        match cond:
            case AllCondition():
                for c in cond.all:
                    walk(c)
            case AnyCondition():
                for c in cond.any:
                    walk(c)
            case NotCondition():
                walk(cond.not_)
            case EnumCondition():
                found.add(cond.enum)
            case _:
                pass

    for rr in rule.row_rules:
        walk(rr.when)
    for cr in rule.cell_rules:
        walk(cr.when)
    return found


def _config_comment(config: ConfigSpec, scope: Scope) -> str:
    """C.3: the config, for traceability and regeneration. Never read at runtime."""
    body = json.dumps(config.model_dump(mode="json", by_alias=True, exclude_none=True),
                      indent=2, ensure_ascii=False, sort_keys=True)
    body = body.replace("*/", "*\\/")  # a value containing */ must not close this comment block
    return ("/* ---------------------------------------------------------------------------\n"
            " * The configuration this script was generated from. It is here so the script can\n"
            " * be traced back and regenerated; nothing below is read while the script runs.\n"
            " *\n"
            f"{body}\n"
            " * ------------------------------------------------------------------------- */")

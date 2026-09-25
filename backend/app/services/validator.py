"""Config validator: shape (Pydantic) + semantics (cross references).

Every problem is reported as {pointer (RFC 6901), code, message} so the
onboarding agent can self-correct. Semantic checks only run once the shape is
valid. Rejects (SPEC §1): unknown action, unknown canonical header, missing
enum, cycles in `after:` chains — plus the extra safety checks listed below.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.schemas.config import (
    ACTIONS,
    AfterTrigger,
    AllCondition,
    AnyCondition,
    ClearRule,
    Condition,
    ConfigSpec,
    ConsolidateRule,
    CopyRule,
    DateCondition,
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
    NumericMultiply,
    NumericRange,
    NumericRound,
    NumericSubtract,
    OnEditTrigger,
    SortRule,
    ValidateRule,
    ValueCondition,
)
from app.services.headers import canon_key


class ValidationIssue(BaseModel):
    pointer: str
    code: str
    message: str


class ValidationResult(BaseModel):
    ok: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    config: ConfigSpec | None = Field(default=None, exclude=True)


def _escape(part: str | int) -> str:
    return str(part).replace("~", "~0").replace("/", "~1")


def _pointer(parts: list[str | int]) -> str:
    return "".join("/" + _escape(p) for p in parts)


def _loc_to_pointer(raw: Any, loc: tuple[str | int, ...]) -> str:
    """Pydantic locs include union tags ('sort', 'value', ...). Walk the raw input and keep only
    the parts that are real keys/indexes; the last part is always kept (missing-field errors)."""
    parts: list[str | int] = []
    node = raw
    for i, part in enumerate(loc):
        if _is_tag(part, node):
            continue
        if isinstance(node, Mapping) and part in node:
            parts.append(part)
            node = node[part]
        elif isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node):
            parts.append(part)
            node = node[part]
        elif i == len(loc) - 1:
            parts.append(part)
    return _pointer(parts)


def _is_tag(part: str | int, node: Any) -> bool:
    if not isinstance(part, str):
        return False
    if part.startswith(("cond:", "trigger:", "function-")) or "[" in part or part.endswith("str"):
        return True
    # rule-union tags are the action names; a real key of the same name wins
    return part in ACTIONS and not (isinstance(node, Mapping) and part in node)


def _code_for(err_type: str, loc: tuple[str | int, ...]) -> str:
    if err_type == "union_tag_invalid" and len(loc) >= 2 and loc[0] == "rules":
        return "unknown_action"
    if err_type == "union_tag_not_found" and len(loc) >= 2 and loc[0] == "rules":
        return "missing_action"
    return f"schema.{err_type}"


def validate_config(raw: Mapping[str, Any] | str | bytes) -> ValidationResult:
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return ValidationResult(ok=False, errors=[ValidationIssue(pointer="", code="invalid_json", message=str(exc))])
    else:
        data = raw
    if not isinstance(data, Mapping):
        return ValidationResult(ok=False, errors=[ValidationIssue(pointer="", code="schema.type", message="config must be an object")])

    try:
        config = ConfigSpec.model_validate(data)
    except ValidationError as exc:
        issues = [
            ValidationIssue(
                pointer=_loc_to_pointer(data, tuple(e["loc"])),
                code=_code_for(e["type"], tuple(e["loc"])),
                message=e["msg"],
            )
            for e in exc.errors(include_url=False)
        ]
        return ValidationResult(ok=False, errors=_dedupe(issues))

    issues = list(_semantic_issues(config))
    return ValidationResult(ok=not issues, errors=issues, config=config if not issues else None)


def _dedupe(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    seen: set[tuple[str, str, str]] = set()
    out: list[ValidationIssue] = []
    for i in issues:
        k = (i.pointer, i.code, i.message)
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out


# --------------------------------------------------------------------------- semantics


def _issue(pointer: list[str | int], code: str, message: str) -> ValidationIssue:
    return ValidationIssue(pointer=_pointer(pointer), code=code, message=message)


class _Checker:
    def __init__(self, config: ConfigSpec) -> None:
        self.c = config
        self.columns = {canon_key(h.canonical) for h in config.canonical_headers}
        self.enums = set(config.enums)
        self.rules = {r.id: r for r in config.rules}
        self.governed = set(config.schema_hashes)

    def column(self, name: str, at: list[str | int]) -> Iterator[ValidationIssue]:
        if canon_key(name) not in self.columns:
            yield _issue(at, "unknown_column", f"column {name!r} is not declared in canonical_headers")

    def enum(self, name: str, at: list[str | int]) -> Iterator[ValidationIssue]:
        if name not in self.enums:
            yield _issue(at, "unknown_enum", f"enum {name!r} is not declared in enums")

    def selector(self, sel: str | list[str], at: list[str | int]) -> Iterator[ValidationIssue]:
        if isinstance(sel, str):
            yield from self.column(sel.split(":", 1)[1], at)
            return
        for i, name in enumerate(sel):
            if name not in self.governed:
                yield _issue(at + [i], "unknown_tab", f"tab {name!r} has no schema_hashes entry (not governed)")

    def numeric_expression(self, expr: NumericExpression, at: list[str | int]) -> Iterator[ValidationIssue]:
        match expr:
            case NumericColumn():
                yield from self.column(expr.column, at + ["column"])
            case NumericAdd():
                for i, part in enumerate(expr.add):
                    yield from self.numeric_expression(part, at + ["add", i])
            case NumericSubtract():
                for i, part in enumerate(expr.subtract):
                    yield from self.numeric_expression(part, at + ["subtract", i])
            case NumericMultiply():
                for i, part in enumerate(expr.multiply):
                    yield from self.numeric_expression(part, at + ["multiply", i])
            case NumericDivide():
                for i, part in enumerate(expr.divide):
                    yield from self.numeric_expression(part, at + ["divide", i])
            case NumericAbs():
                yield from self.numeric_expression(expr.abs, at + ["abs"])
            case NumericRound():
                yield from self.numeric_expression(expr.round.value, at + ["round", "value"])

    def condition(self, cond: Condition, at: list[str | int], *, cell: bool) -> Iterator[ValidationIssue]:
        match cond:
            case AllCondition():
                for i, sub in enumerate(cond.all):
                    yield from self.condition(sub, at + ["all", i], cell=cell)
            case AnyCondition():
                for i, sub in enumerate(cond.any):
                    yield from self.condition(sub, at + ["any", i], cell=cell)
            case NotCondition():
                yield from self.condition(cond.not_, at + ["not"], cell=cell)
            case EnumCondition():
                if cond.enum not in self.enums:
                    yield from self.enum(cond.enum, at + ["enum"])
                    return
                if cond.column is not None:
                    yield from self.column(cond.column, at + ["column"])
                elif canon_key(cond.enum) not in self.columns:
                    yield _issue(at + ["enum"], "unknown_column",
                                 f"enum {cond.enum!r} is used as a column name but is not a canonical header; set 'column'")
                values = {canon_key(s.value) for s in self.c.enums[cond.enum].stages}
                if canon_key(cond.is_) not in values:
                    yield _issue(at + ["is"], "unknown_enum_value",
                                 f"{cond.is_!r} is not a stage of enum {cond.enum!r}")
            case DateCondition():
                yield from self.column(cond.date, at + ["date"])
            case NumericCondition():
                yield from self.numeric_expression(cond.numeric.left, at + ["numeric", "left"])
                if isinstance(cond.numeric.right, NumericRange):
                    yield from self.numeric_expression(cond.numeric.right.minimum, at + ["numeric", "right", "min"])
                    yield from self.numeric_expression(cond.numeric.right.maximum, at + ["numeric", "right", "max"])
                else:
                    yield from self.numeric_expression(cond.numeric.right, at + ["numeric", "right"])
            case ValueCondition():
                if cond.column is not None:
                    yield from self.column(cond.column, at + ["column"])
                elif not cell:
                    yield _issue(at, "missing_column", "row condition must name a 'column'")

    def run(self) -> Iterator[ValidationIssue]:
        c = self.c
        if c.data_start_row <= c.header_row:
            yield _issue(["data_start_row"], "data_start_before_header", "data_start_row must be after header_row")

        seen_canon: set[str] = set()
        for i, h in enumerate(c.canonical_headers):
            k = canon_key(h.canonical)
            if k in seen_canon:
                yield _issue(["canonical_headers", i, "canonical"], "duplicate_canonical_header", f"{h.canonical!r} declared twice")
            seen_canon.add(k)
        if canon_key(c.guards.hold_column) in self.columns:
            yield _issue(["guards", "hold_column"], "hold_column_conflict", "hold_column collides with a canonical header")

        for name, spec in c.enums.items():
            seen_vals: set[str] = set()
            for i, s in enumerate(spec.stages):
                if canon_key(s.value) in seen_vals:
                    yield _issue(["enums", name, "stages", i, "value"], "duplicate_enum_value", f"{s.value!r} declared twice")
                seen_vals.add(canon_key(s.value))
            if spec.unknown_order <= max(s.order for s in spec.stages):
                yield _issue(["enums", name, "unknown_order"], "unknown_order_not_last",
                             "unknown_order must be greater than every stage order (unknown values sort last)")

        ids: set[str] = set()
        targets: dict[str, int] = {}
        for i, r in enumerate(c.rules):
            if r.id in ids:
                yield _issue(["rules", i, "id"], "duplicate_rule_id", f"rule id {r.id!r} used twice")
            ids.add(r.id)
            if isinstance(r, ConsolidateRule):
                if r.target_tab in targets:
                    yield _issue(["rules", i, "target_tab"], "duplicate_target", f"{r.target_tab!r} is already a consolidate target")
                targets[r.target_tab] = i
                if r.target_tab in self.governed:
                    yield _issue(["rules", i, "target_tab"], "target_is_governed",
                                 "consolidate target is generated and must not appear in schema_hashes")

        for i, r in enumerate(c.rules):
            at: list[str | int] = ["rules", i]
            yield from self._rule(r, at, targets)

        yield from self._cycles()

    def _rule(self, r: Any, at: list[str | int], targets: dict[str, int]) -> Iterator[ValidationIssue]:
        trig = r.trigger
        if isinstance(trig, OnEditTrigger):
            for j, col in enumerate(trig.on_edit.columns):
                yield from self.column(col, at + ["trigger", "on_edit", "columns", j])
        elif isinstance(trig, AfterTrigger):
            parent = self.rules.get(trig.after)
            if parent is None:
                yield _issue(at + ["trigger", "after"], "unknown_rule", f"rule {trig.after!r} does not exist")

        if isinstance(r, ConsolidateRule):
            yield from self.selector(r.sources, at + ["sources"])
            if isinstance(r.sources, list) and r.target_tab in r.sources:
                yield _issue(at + ["sources"], "target_in_sources", "target_tab cannot be one of its own sources")
            for key, kind, cls in (("sort_like", "sort", SortRule), ("format_like", "format", FormatRule)):
                ref = getattr(r, key)
                if ref is None:
                    continue
                other = self.rules.get(ref)
                if other is None:
                    yield _issue(at + [key], "unknown_rule", f"rule {ref!r} does not exist")
                elif not isinstance(other, cls):
                    yield _issue(at + [key], "wrong_rule_kind", f"rule {ref!r} is not a {kind} rule")
            return

        yield from self.selector(r.tabs, at + ["tabs"])
        if isinstance(r.tabs, list):
            for j, name in enumerate(r.tabs):
                if name in targets:
                    yield _issue(at + ["tabs", j], "target_in_sources", f"{name!r} is a generated consolidate target")

        if isinstance(r, SortRule):
            for j, k in enumerate(r.keys):
                yield from self.column(k.column, at + ["keys", j, "column"])
                if k.using_enum is not None:
                    yield from self.enum(k.using_enum, at + ["keys", j, "using_enum"])
        elif isinstance(r, FormatRule):
            for j, rr in enumerate(r.row_rules):
                yield from self.condition(rr.when, at + ["row_rules", j, "when"], cell=False)
            for j, cr in enumerate(r.cell_rules):
                yield from self.column(cr.column, at + ["cell_rules", j, "column"])
                yield from self.condition(cr.when, at + ["cell_rules", j, "when"], cell=True)
        elif isinstance(r, (MoveRule, CopyRule)):
            yield from self.condition(r.when, at + ["when"], cell=False)
            if r.to_tab not in self.governed:
                yield _issue(at + ["to_tab"], "unknown_tab", f"target tab {r.to_tab!r} is not governed")
            if r.to_tab in targets:
                yield _issue(at + ["to_tab"], "target_in_sources", f"{r.to_tab!r} is a generated consolidate target")
            if isinstance(r.tabs, list) and r.to_tab in r.tabs:
                yield _issue(at + ["to_tab"], "move_into_source", "to_tab cannot also be a source tab")
            if isinstance(r, CopyRule):
                for j, col in enumerate(r.key_columns):
                    yield from self.column(col, at + ["key_columns", j])
        elif isinstance(r, ValidateRule):
            yield from self.column(r.column, at + ["column"])
            if r.from_enum is not None:
                yield from self.enum(r.from_enum, at + ["from_enum"])
        elif isinstance(r, DedupeRule):
            for j, col in enumerate(r.key_columns):
                yield from self.column(col, at + ["key_columns", j])
        elif isinstance(r, ClearRule):
            yield from self.condition(r.when, at + ["when"], cell=False)
            for j, col in enumerate(r.columns):
                yield from self.column(col, at + ["columns", j])

    def _cycles(self) -> Iterator[ValidationIssue]:
        index = {r.id: i for i, r in enumerate(self.c.rules)}
        parent = {r.id: r.trigger.after for r in self.c.rules if isinstance(r.trigger, AfterTrigger)}
        reported: set[str] = set()
        for start in parent:
            path: list[str] = []
            node: str | None = start
            while node is not None and node in parent and node not in path:
                path.append(node)
                node = parent[node]
            if node is not None and node in path:
                cycle = path[path.index(node):]
                key = min(cycle)
                if key in reported:
                    continue
                reported.add(key)
                yield _issue(["rules", index[key], "trigger", "after"], "trigger_cycle",
                             "after-chain cycle: " + " -> ".join(cycle + [node]))


def _semantic_issues(config: ConfigSpec) -> Iterator[ValidationIssue]:
    return _Checker(config).run()

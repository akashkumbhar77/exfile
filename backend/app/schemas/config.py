"""ConfigSpec — the declarative contract that governs one sheet.

The onboarding/repair agents emit JSON in this shape; the validator
(`app.services.validator`) checks it; the rule evaluator and Engine.gs execute
it. Shape and single-object constraints live here. Cross-references (columns,
enums, rule ids, trigger cycles) are checked by the validator so that it can
emit precise json-pointer errors.

JSON Schema export: `scripts/export_config_schema.py` -> docs/config.schema.json.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
    model_validator,
)

# Bump when the JSON shape changes incompatibly (stored as configs.schema_json_version).
SCHEMA_VERSION = 1

HexColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]
MatchExpr = Annotated[
    str,
    Field(
        pattern=r"^(contains|equals):\s*\S.*$",
        description="Case-insensitive, trimmed header/value match: 'contains:X' or 'equals:X'.",
    ),
]
RuleId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
ColumnRef = Annotated[str, Field(min_length=1, description="A canonical header name.")]
TabName = Annotated[str, Field(min_length=1, max_length=100)]
SchemaHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
DateRef = Annotated[str, Field(pattern=r"^(today|\d{4}-\d{2}-\d{2})$")]
TabSelector = Union[
    Annotated[
        str,
        Field(
            pattern=r"^all_with:.+$",
            description="'all_with:<CANONICAL>' = every governed tab that has that column.",
        ),
    ],
    Annotated[list[TabName], Field(min_length=1, description="Explicit governed tab names.")],
]
CellLiteral = Union[str, bool, int, float]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


# --------------------------------------------------------------------------- headers / enums


class CanonicalHeader(_Strict):
    canonical: Annotated[str, Field(min_length=1)]
    match: Annotated[list[MatchExpr], Field(min_length=1)]


class EnumStage(_Strict):
    value: Annotated[str, Field(min_length=1)]
    match: MatchExpr
    order: int


class EnumSpec(_Strict):
    stages: Annotated[list[EnumStage], Field(min_length=1)]
    unknown_order: int = Field(
        description="Sort position for values matching no stage. Must be greater than every stage order."
    )


# --------------------------------------------------------------------------- conditions


class EnumCondition(_Strict):
    enum: Annotated[str, Field(min_length=1)]
    is_: Annotated[str, Field(alias="is", min_length=1)]
    column: ColumnRef | None = Field(
        default=None, description="Column to classify; defaults to the enum's name."
    )


class DateCondition(_Strict):
    date: ColumnRef
    before: DateRef | None = None
    after: DateRef | None = None

    @model_validator(mode="after")
    def _one_comparator(self) -> DateCondition:
        if (self.before is None) == (self.after is None):
            raise ValueError("date condition needs exactly one of 'before' or 'after'")
        return self


class ValueCondition(_Strict):
    column: ColumnRef | None = Field(
        default=None, description="Required in row context; defaults to the cell's column in cell_rules."
    )
    equals: CellLiteral | None = None
    contains: Annotated[str, Field(min_length=1)] | None = None
    is_blank: bool | None = None

    @model_validator(mode="after")
    def _one_test(self) -> ValueCondition:
        set_tests = [t for t in (self.equals, self.contains, self.is_blank) if t is not None]
        if len(set_tests) != 1:
            raise ValueError("value condition needs exactly one of 'equals', 'contains', 'is_blank'")
        return self


# Numeric expressions are deliberately a small, deterministic language rather than arbitrary
# spreadsheet formulas. They evaluate against one row only; aggregates and external lookups do
# not belong in the runtime rule language.
class NumericLiteral(_Strict):
    literal: int | float

    @field_validator("literal", mode="before")
    @classmethod
    def _real_finite_number(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("numeric literal must be a finite JSON number")
        return value


class NumericColumn(_Strict):
    column: ColumnRef


class NumericAdd(_Strict):
    add: Annotated[list[NumericExpression], Field(min_length=2)]


class NumericSubtract(_Strict):
    subtract: Annotated[list[NumericExpression], Field(min_length=2, max_length=2)]


class NumericMultiply(_Strict):
    multiply: Annotated[list[NumericExpression], Field(min_length=2)]


class NumericDivide(_Strict):
    divide: Annotated[list[NumericExpression], Field(min_length=2, max_length=2)]


class NumericAbs(_Strict):
    abs: NumericExpression


class NumericRoundArgs(_Strict):
    value: NumericExpression
    digits: Annotated[int, Field(ge=0, le=12)] = 0


class NumericRound(_Strict):
    round: NumericRoundArgs


_NUMERIC_TAGS: dict[type[BaseModel], str] = {
    NumericLiteral: "num:literal",
    NumericColumn: "num:column",
    NumericAdd: "num:add",
    NumericSubtract: "num:subtract",
    NumericMultiply: "num:multiply",
    NumericDivide: "num:divide",
    NumericAbs: "num:abs",
    NumericRound: "num:round",
}


def _numeric_kind(v: Any) -> str | None:
    if isinstance(v, BaseModel):
        return _NUMERIC_TAGS.get(type(v))
    if not isinstance(v, dict) or len(v) != 1:
        return None
    key = next(iter(v))
    return f"num:{key}" if key in ("literal", "column", "add", "subtract", "multiply", "divide", "abs", "round") else None


NumericExpression = Annotated[
    Union[
        Annotated[NumericLiteral, Tag("num:literal")],
        Annotated[NumericColumn, Tag("num:column")],
        Annotated[NumericAdd, Tag("num:add")],
        Annotated[NumericSubtract, Tag("num:subtract")],
        Annotated[NumericMultiply, Tag("num:multiply")],
        Annotated[NumericDivide, Tag("num:divide")],
        Annotated[NumericAbs, Tag("num:abs")],
        Annotated[NumericRound, Tag("num:round")],
    ],
    Discriminator(
        _numeric_kind,
        custom_error_type="invalid_numeric_expression",
        custom_error_message="numeric expression must have exactly one supported operation",
    ),
]


class NumericRange(_Strict):
    minimum: NumericExpression = Field(alias="min")
    maximum: NumericExpression = Field(alias="max")

    @model_validator(mode="after")
    def _ordered_literals(self) -> NumericRange:
        if isinstance(self.minimum, NumericLiteral) and isinstance(self.maximum, NumericLiteral):
            if self.minimum.literal > self.maximum.literal:
                raise ValueError("numeric range min must not exceed max")
        return self


class NumericComparison(_Strict):
    left: NumericExpression
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "between", "not_between"]
    right: NumericExpression | NumericRange

    @model_validator(mode="after")
    def _right_matches_operator(self) -> NumericComparison:
        range_op = self.op in ("between", "not_between")
        if range_op != isinstance(self.right, NumericRange):
            expected = "a range with min and max" if range_op else "one numeric expression"
            raise ValueError(f"numeric operator {self.op!r} needs {expected} on the right")
        return self


class NumericCondition(_Strict):
    numeric: NumericComparison


class AllCondition(_Strict):
    all: Annotated[list[Condition], Field(min_length=1)]


class AnyCondition(_Strict):
    any: Annotated[list[Condition], Field(min_length=1)]


class NotCondition(_Strict):
    not_: Annotated[Condition, Field(alias="not")]


_CONDITION_TAGS: dict[type[BaseModel], str] = {
    AllCondition: "cond:all",
    AnyCondition: "cond:any",
    NotCondition: "cond:not",
    EnumCondition: "cond:enum",
    DateCondition: "cond:date",
    ValueCondition: "cond:value",
    NumericCondition: "cond:numeric",
}


def _condition_kind(v: Any) -> str | None:
    if isinstance(v, BaseModel):
        return _CONDITION_TAGS.get(type(v))
    if not isinstance(v, dict):
        return None
    for key in ("all", "any", "not", "enum", "date", "numeric"):
        if key in v:
            return f"cond:{key}"
    return "cond:value"


Condition = Annotated[
    Union[
        Annotated[AllCondition, Tag("cond:all")],
        Annotated[AnyCondition, Tag("cond:any")],
        Annotated[NotCondition, Tag("cond:not")],
        Annotated[EnumCondition, Tag("cond:enum")],
        Annotated[DateCondition, Tag("cond:date")],
        Annotated[ValueCondition, Tag("cond:value")],
        Annotated[NumericCondition, Tag("cond:numeric")],
    ],
    Discriminator(
        _condition_kind,
        custom_error_type="invalid_condition",
        custom_error_message="condition must be an object",
    ),
]


# --------------------------------------------------------------------------- triggers


class OnEditSpec(_Strict):
    columns: list[ColumnRef] = Field(
        default_factory=list, description="Edited canonical columns that fire the rule; empty = any column."
    )


class OnEditTrigger(_Strict):
    on_edit: OnEditSpec


class AfterTrigger(_Strict):
    after: RuleId


class DebouncedSpec(_Strict):
    quiet_seconds: Annotated[int, Field(ge=5, le=3600)]


class DebouncedTrigger(_Strict):
    debounced: DebouncedSpec


class ScheduleSpec(_Strict):
    cron: Annotated[str, Field(pattern=r"^\s*\S+(\s+\S+){4}\s*$", description="5-field cron expression.")]


class ScheduleTrigger(_Strict):
    schedule: ScheduleSpec


_TRIGGER_TAGS: dict[type[BaseModel], str] = {
    OnEditTrigger: "trigger:on_edit",
    AfterTrigger: "trigger:after",
    DebouncedTrigger: "trigger:debounced",
    ScheduleTrigger: "trigger:schedule",
}


def _trigger_kind(v: Any) -> str | None:
    if isinstance(v, BaseModel):
        return _TRIGGER_TAGS.get(type(v))
    if isinstance(v, dict) and len(v) == 1:
        tag = f"trigger:{next(iter(v))}"
        return tag if tag in _TRIGGER_TAGS.values() else None
    return None


Trigger = Annotated[
    Union[
        Annotated[OnEditTrigger, Tag("trigger:on_edit")],
        Annotated[AfterTrigger, Tag("trigger:after")],
        Annotated[DebouncedTrigger, Tag("trigger:debounced")],
        Annotated[ScheduleTrigger, Tag("trigger:schedule")],
    ],
    Discriminator(
        _trigger_kind,
        custom_error_type="invalid_trigger",
        custom_error_message="trigger must be an object with exactly one of: on_edit, after, debounced, schedule",
    ),
]


# --------------------------------------------------------------------------- formatting


class Style(_Strict):
    font: HexColor | None = None
    background: HexColor | Literal["none"] | None = Field(
        default=None, description="'none' clears the fill (banding shows through); null leaves it unchanged."
    )
    strike: bool | None = None


NEUTRAL_STYLE = Style(font="#000000", background="none", strike=False)


class RowFormatRule(Style):
    when: Condition


class CellFormatRule(Style):
    column: ColumnRef
    when: Condition


# --------------------------------------------------------------------------- rules


class _RuleBase(_Strict):
    id: RuleId
    trigger: Trigger
    description: str | None = None


class _TabbedRule(_RuleBase):
    tabs: TabSelector


class SortKey(_Strict):
    column: ColumnRef
    using_enum: str | None = None
    order: Literal["asc", "desc"] = "asc"
    blanks: Literal["first", "last"] = "last"
    type: Literal["auto", "date", "number", "text"] = "auto"

    @model_validator(mode="after")
    def _enum_is_typed(self) -> SortKey:
        if self.using_enum is not None and self.type != "auto":
            raise ValueError("'type' cannot be combined with 'using_enum'")
        return self


class SortRule(_TabbedRule):
    action: Literal["sort"]
    keys: Annotated[list[SortKey], Field(min_length=1)]


class FormatRule(_TabbedRule):
    action: Literal["format"]
    row_rules: list[RowFormatRule] = Field(default_factory=list)
    cell_rules: list[CellFormatRule] = Field(default_factory=list)
    default: Style = Field(
        default=NEUTRAL_STYLE,
        description="Base style for every governed row before row_rules apply (unknown values stay neutral).",
    )

    @model_validator(mode="after")
    def _has_rules(self) -> FormatRule:
        if not self.row_rules and not self.cell_rules:
            raise ValueError("format rule needs at least one of 'row_rules' or 'cell_rules'")
        return self


class PrependColumn(_Strict):
    name: Annotated[str, Field(min_length=1)]
    value: Literal["tab_name"]


class DerivedColumn(_Strict):
    name: Annotated[str, Field(min_length=1)]
    fill_if_empty: Literal["tab_name"]


BandingTheme = Literal[
    "LIGHT_GREY", "CYAN", "GREEN", "YELLOW", "ORANGE", "BLUE",
    "TEAL", "GREY", "BROWN", "LIGHT_GREEN", "INDIGO", "PINK",
]


class Presentation(_Strict):
    """How a consolidate target looks. All of it is evaluated (services/rules/consolidate.py):
    title and header colours, banding over the data rows, `date_format` on every column whose
    header contains DATE, and column widths when the target tab is first created."""

    title: str | None = None
    title_font: HexColor = "#FFFFFF"
    title_background: HexColor = "#38761D"
    header_font: HexColor = "#000000"
    header_background: HexColor = "#B6D7A8"
    banding: BandingTheme | None = "LIGHT_GREY"
    column_widths: dict[str, Annotated[int, Field(ge=10, le=2000)]] = Field(default_factory=dict)
    default_column_width: Annotated[int, Field(ge=10, le=2000)] = 130
    date_format: Annotated[str, Field(min_length=1)] = Field(
        default="match_source",
        description=("Number format for every target column whose header contains DATE. "
                     "'match_source' copies the format of the first source's date column "
                     "(sort_like's first date key); otherwise a Sheets number format."),
    )


class ConsolidateRule(_RuleBase):
    action: Literal["consolidate"]
    sources: TabSelector
    target_tab: TabName
    prepend_columns: list[PrependColumn] = Field(default_factory=list)
    derived: list[DerivedColumn] = Field(default_factory=list)
    sort_like: RuleId | None = None
    format_like: RuleId | None = None
    lock: bool = True
    presentation: Presentation = Field(default_factory=Presentation)


class MoveRule(_TabbedRule):
    action: Literal["move"]
    when: Condition
    to_tab: TabName
    position: Literal["top", "bottom"] = "bottom"


class CopyRule(_TabbedRule):
    action: Literal["copy"]
    when: Condition
    to_tab: TabName
    key_columns: Annotated[list[ColumnRef], Field(min_length=1)]
    position: Literal["top", "bottom"] = "bottom"


class ValidateRule(_TabbedRule):
    action: Literal["validate"]
    column: ColumnRef
    values: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)] | None = None
    from_enum: str | None = None
    allow_invalid: bool = False

    @model_validator(mode="after")
    def _one_source(self) -> ValidateRule:
        if (self.values is None) == (self.from_enum is None):
            raise ValueError("validate rule needs exactly one of 'values' or 'from_enum'")
        return self


class DedupeRule(_TabbedRule):
    action: Literal["dedupe"]
    key_columns: Annotated[list[ColumnRef], Field(min_length=1)]
    keep: Literal["first", "last"] = "first"


class ClearRule(_TabbedRule):
    action: Literal["clear"]
    when: Condition
    columns: Annotated[list[ColumnRef], Field(min_length=1)]


Rule = Annotated[
    Union[SortRule, FormatRule, ConsolidateRule, MoveRule, CopyRule, ValidateRule, DedupeRule, ClearRule],
    Field(discriminator="action"),
]
TabbedRule = Union[SortRule, FormatRule, MoveRule, CopyRule, ValidateRule, DedupeRule, ClearRule]
ACTIONS: tuple[str, ...] = ("sort", "format", "consolidate", "move", "copy", "validate", "dedupe", "clear")
# Actions that rewrite/move/delete user data and therefore snapshot first + count against max_rows_per_run.
DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset({"sort", "move", "copy", "dedupe", "clear", "consolidate"})
GUARDED_ACTIONS: frozenset[str] = frozenset({"sort", "move", "copy", "dedupe", "clear"})


# --------------------------------------------------------------------------- root


class Guards(_Strict):
    max_rows_per_run: Annotated[int, Field(ge=1, le=100_000)] = 500
    snapshot_destructive: Literal[True] = Field(
        default=True, description="Always true: destructive rules snapshot first (invariant 6)."
    )
    hold_column: Annotated[str, Field(min_length=1)] = "!hold"
    backup_tab: bool = Field(
        default=False,
        description="Generated scripts only: copy affected rows to a hidden _backup tab before a "
                    "destructive rule. Forced on there (PATCH-005 I.7); it is not a snapshot.",
    )


class ConfigSpec(_Strict):
    config_version: Annotated[int, Field(ge=1)]
    sheet_id: Annotated[str, Field(min_length=1)]
    org_id: Annotated[str, Field(min_length=1)]
    schema_hashes: Annotated[
        dict[TabName, SchemaHash],
        Field(min_length=1, description="Governed tabs -> sha256 of their header row (see preflight)."),
    ]
    header_row: Annotated[int, Field(ge=1)]
    data_start_row: Annotated[int, Field(ge=2)]
    canonical_headers: list[CanonicalHeader] = Field(default_factory=list)
    enums: dict[Annotated[str, Field(min_length=1)], EnumSpec] = Field(default_factory=dict)
    rules: Annotated[list[Rule], Field(min_length=1)]
    guards: Guards = Field(default_factory=Guards)

    def rule(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)


for _model in (NumericAdd, NumericSubtract, NumericMultiply, NumericDivide, NumericAbs, NumericRoundArgs,
               NumericRound, NumericRange, NumericComparison, NumericCondition, AllCondition, AnyCondition,
               NotCondition, RowFormatRule, CellFormatRule, MoveRule, CopyRule, ClearRule, FormatRule, ConfigSpec):
    _model.model_rebuild()

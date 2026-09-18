"""Universal, deterministic row-level numeric conditions."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.schemas.config import ConfigSpec
from app.services.conditions import EvalContext, evaluate
from app.services.executor import execute_run
from app.services.grid import Tab, Workbook
from app.services.headers import build_view
from app.services.runner import RunEvent
from app.services.validator import validate_config
from tests.conftest import make_config


HEADERS = ["ID", "DAYS", "ACTUAL", "BUDGET", "HOURS", "RATE", "!hold"]
CANONICAL = [{"canonical": name, "match": [f"equals:{name}"]} for name in HEADERS[:-1]]


def _workbook() -> Workbook:
    return Workbook([Tab("OPEN", [["title"], HEADERS,
                                  ["a", 21, 120, 100, 4, 30, ""],
                                  ["b", 20, 100, 100, 3, 20, ""],
                                  ["c", "21", 95, 100, 2, 0, ""],
                                  ["d", -2, 50, 100, 1, 10, ""]])])


def _config(condition: dict[str, Any]) -> ConfigSpec:
    wb = _workbook()
    return make_config(wb, ["OPEN"], [
        {"id": "format_numbers", "action": "format", "tabs": ["OPEN"], "trigger": {"on_edit": {}},
         "row_rules": [{"when": condition, "font": "#FF0000"}],}
    ], canonical_headers=CANONICAL, enums={})


def _matches(condition: dict[str, Any]) -> list[bool]:
    cfg = _config(condition)
    tab = _workbook().tab("OPEN")
    assert tab is not None
    view = build_view(tab, cfg)
    when = cfg.rules[0].row_rules[0].when  # type: ignore[union-attr]
    ctx = EvalContext("numeric", date(2026, 9, 18))
    return [evaluate(when, row, view, cfg, ctx) for row in view.data_rows()]


@pytest.mark.parametrize(("condition", "expected"), [
    ({"numeric": {"left": {"column": "DAYS"}, "op": "gt", "right": {"literal": 20}}},
     [True, False, False, False]),
    ({"numeric": {"left": {"column": "DAYS"}, "op": "between",
                  "right": {"min": {"literal": -2}, "max": {"literal": 20}}}},
     [False, True, False, True]),
    ({"numeric": {"left": {"column": "ACTUAL"}, "op": "gt", "right": {"column": "BUDGET"}}},
     [True, False, False, False]),
    ({"numeric": {"left": {"multiply": [{"column": "HOURS"}, {"column": "RATE"}]}, "op": "gte",
                  "right": {"literal": 100}}},
     [True, False, False, False]),
    ({"numeric": {"left": {"abs": {"subtract": [{"column": "ACTUAL"}, {"column": "BUDGET"}]}},
                  "op": "gte", "right": {"literal": 20}}},
     [True, False, False, True]),
    ({"numeric": {"left": {"round": {"value": {"divide": [{"column": "ACTUAL"}, {"column": "BUDGET"}]},
                                           "digits": 1}}, "op": "eq", "right": {"literal": 1.2}}},
     [True, False, False, False]),
])
def test_numeric_predicates_are_typed_and_row_local(condition: dict[str, Any], expected: list[bool]) -> None:
    assert _matches(condition) == expected


def test_division_by_zero_and_numeric_looking_text_are_safe_non_matches() -> None:
    assert _matches({"numeric": {"left": {"divide": [{"column": "ACTUAL"}, {"column": "RATE"}]},
                                  "op": "gt", "right": {"literal": 1}}}) == [True, True, False, True]
    assert _matches({"numeric": {"left": {"column": "DAYS"}, "op": "ne", "right": {"literal": 0}}}) == [True, True, False, True]


def test_numeric_condition_drives_the_existing_format_executor() -> None:
    wb = _workbook()
    cfg = _config({"numeric": {"left": {"column": "DAYS"}, "op": "gt", "right": {"literal": 20}}})
    out = execute_run(cfg, wb, RunEvent.manual(), EvalContext("numeric", date(2026, 9, 18))).workbook
    tab = out.tab("OPEN")
    assert tab is not None
    assert [row[1].font for row in tab.formats[2:]] == ["#FF0000", "#000000", "#000000", "#000000"]


def test_validator_recurses_into_numeric_expressions_and_rejects_bad_ranges() -> None:
    raw = _config({"numeric": {"left": {"column": "DAYS"}, "op": "gt", "right": {"literal": 20}}}).model_dump(mode="json", by_alias=True)
    raw["rules"][0]["row_rules"][0]["when"] = {
        "numeric": {"left": {"multiply": [{"column": "MISSING"}, {"literal": 2}]}, "op": "gt",
                    "right": {"literal": 2}}
    }
    result = validate_config(raw)
    assert not result.ok
    assert any(e.code == "unknown_column" and e.pointer.endswith("/numeric/left/multiply/0/column") for e in result.errors)

    raw["rules"][0]["row_rules"][0]["when"]["numeric"]["op"] = "between"
    raw["rules"][0]["row_rules"][0]["when"]["numeric"]["right"] = {
        "min": {"literal": 9}, "max": {"literal": 2}
    }
    result = validate_config(raw)
    assert any(e.code.startswith("schema.value_error") and "/numeric" in e.pointer for e in result.errors)


@pytest.mark.parametrize(("value", "digits", "expected"), [
    (2.5, 0, 3.0), (-2.5, 0, -3.0), (0.5, 0, 1.0), (1.5, 0, 2.0),  # half away from zero, like Sheets
    (2.675, 2, 2.68), (1.005, 2, 1.01),  # the displayed decimal, not the binary float
    (1.24, 1, 1.2), (7.0, 0, 7.0),
])
def test_round_matches_sheets_round_not_python_bankers_rounding(value: float, digits: int, expected: float) -> None:
    from app.services.conditions import sheets_round

    assert sheets_round(value, digits) == expected


def test_readback_brackets_nested_arithmetic() -> None:
    from app.schemas.config import NumericCondition
    from app.services.describe import numeric_condition_text

    cond = NumericCondition.model_validate({"numeric": {
        "left": {"multiply": [{"add": [{"column": "HOURS"}, {"literal": 2}]}, {"column": "RATE"}]},
        "op": "gt", "right": {"round": {"value": {"divide": [{"column": "ACTUAL"}, {"column": "BUDGET"}]},
                                         "digits": 1}}}})
    assert numeric_condition_text(cond) == (
        "(HOURS + 2) × RATE is greater than (ACTUAL ÷ BUDGET) rounded to 1 decimal place")


def test_conditions_skill_carries_every_numeric_schema_definition() -> None:
    """The model must see the whole numeric language, not a NumericCondition whose $ref dangles."""
    import json
    import re

    from app.agent.skills import render

    body = render(["conditions-and-format"]).split("```json\n")[-1].split("\n```")[0]
    defs = json.loads(body)
    refs = set(re.findall(r'"\$ref":"#/\$defs/(\w+)"', body))
    assert {"NumericCondition", "NumericComparison", "NumericRound"} <= set(defs)
    assert not {r for r in refs if r.startswith("Numeric")} - set(defs)

"""describe.py: deterministic plain-English readback (SPEC-PATCH-003 A.2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.schemas.config import ConfigSpec
from app.services.describe import Scope, colour, describe_config, rule_text
from tests.fixtures import reference_workbook
from tests.conftest import load_reference_raw

GOLDEN = Path(__file__).parent / "fixtures" / "reference_readback.txt"


def test_reference_config_readback_matches_golden_text() -> None:
    text = "\n".join(describe_config(ConfigSpec.model_validate(load_reference_raw())).lines()) + "\n"
    assert text == GOLDEN.read_text(encoding="utf-8")


def test_readback_is_deterministic_and_has_no_llm_dependency() -> None:
    import app.services.describe as d

    cfg = ConfigSpec.model_validate(load_reference_raw())
    assert describe_config(cfg) == describe_config(cfg)
    src = Path(d.__file__).read_text(encoding="utf-8")
    assert "openai" not in src.lower() and "app.agent" not in src


def _cfg(rules: list[dict[str, Any]], **extra: Any) -> ConfigSpec:
    raw = load_reference_raw()
    raw["rules"] = rules
    raw.update(extra)
    return ConfigSpec.model_validate(raw)


@pytest.mark.parametrize("rule,expected", [
    ({"id": "arch", "action": "move", "tabs": ["MACHINES"], "trigger": {"on_edit": {"columns": ["STATUS"]}},
      "when": {"enum": "STATUS", "is": "CANCELLED"}, "to_tab": "SPARES", "position": "top"},
     "Move rows in the MACHINES tab where STATUS is CANCELLED to the top of the SPARES tab"),
    ({"id": "cp", "action": "copy", "tabs": "all_with:STATUS", "trigger": {"schedule": {"cron": "0 7 * * 1"}},
      "when": {"column": "FREEZE?", "equals": "YES"}, "to_tab": "SPARES", "key_columns": ["STATUS"]},
     "Copy rows in every tab with a STATUS column where FREEZE? is “YES” to the bottom of the SPARES tab"),
    ({"id": "dd", "action": "dedupe", "tabs": ["MACHINES", "SPARES"], "trigger": {"on_edit": {"columns": []}},
      "key_columns": ["STATUS", "DISPATCH DATE"], "keep": "last"},
     "Remove duplicate rows in the MACHINES and SPARES tabs that share the same STATUS and DISPATCH DATE, "
     "keeping the last one"),
    ({"id": "cl", "action": "clear", "tabs": ["MACHINES"], "trigger": {"debounced": {"quiet_seconds": 45}},
      "when": {"not": {"any": [{"enum": "STATUS", "is": "COMPLETED"}, {"column": "FREEZE?", "is_blank": True}]}},
      "columns": ["FREEZE?"]},
     "Clear FREEZE? in the MACHINES tab where not (STATUS is COMPLETED or FREEZE? is blank)"),
    ({"id": "vd", "action": "validate", "tabs": "all_with:STATUS", "trigger": {"on_edit": {"columns": ["STATUS"]}},
      "column": "STATUS", "from_enum": "STATUS", "allow_invalid": True},
     "Add a dropdown to the STATUS column in every tab with a STATUS column offering the STATUS stages"),
])
def test_every_action_has_a_template(rule: dict[str, Any], expected: str) -> None:
    cfg = _cfg([rule])
    assert rule_text(cfg.rules[0], 1, Scope(cfg)).headline == expected


def test_triggers_and_details() -> None:
    cfg = _cfg([
        {"id": "cl", "action": "clear", "tabs": ["MACHINES"], "trigger": {"debounced": {"quiet_seconds": 45}},
         "when": {"date": "DISPATCH DATE", "after": "2026-01-31"}, "columns": ["FREEZE?"]},
        {"id": "cp", "action": "copy", "tabs": ["MACHINES"], "trigger": {"schedule": {"cron": "0 7 * * 1"}},
         "when": {"column": "STATUS", "contains": "hold"}, "to_tab": "SPARES", "key_columns": ["STATUS"]},
    ])
    a, b = (rule_text(r, i, Scope(cfg)) for i, r in enumerate(cfg.rules, 1))
    assert a.when == "45 seconds after edits stop" and "DISPATCH DATE is after 2026-01-31" in a.headline
    assert b.when == "on the schedule “0 7 * * 1” (cron)" and "STATUS contains “hold”" in b.headline
    assert [x.text for x in b.details] == ["A row is copied only once, identified by STATUS."]


def test_colours_are_segments_and_never_bare_hex_in_text() -> None:
    assert colour("#ff0000") == {"kind": "color", "name": "red", "hex": "#FF0000"}
    assert colour("#123456")["name"].endswith("-ish")
    text = "\n".join(describe_config(ConfigSpec.model_validate(load_reference_raw())).lines())
    assert "#" not in text  # hex only lives in structured segments (tooltip), never in prose
    fmt = describe_config(ConfigSpec.model_validate(load_reference_raw())).rules[1]
    swatches = [s for x in fmt.details for s in x.segments if s["kind"] == "color"]
    assert {"kind": "color", "name": "pink", "hex": "#FCE4EC"} in swatches


def test_page_ordering_and_wording() -> None:
    t = describe_config(ConfigSpec.model_validate(load_reference_raw()), reference_workbook.build())
    fmt = t.rules[1]
    assert fmt.details[0].text.startswith("Each row starts as black text, no fill; the rules below")
    overdue = [x.text for x in fmt.details if "(overdue)" in x.text]
    assert overdue == ["Where STATUS is IN-PROCESS and DISPATCH DATE is before today (overdue): "
                       "black text on pink."]
    assert "(in the order shown under Stages)" in t.rules[0].headline and "→" not in t.rules[0].headline
    assert t.rules[1].when == "right after rule 1 runs"
    cons = [x.text for x in t.rules[2].details]
    assert "Sorted the same way as rule 1." in cons and "Colored the same way as rule 2." in cons
    assert "IN-PROCESS (labels containing “PROCESS”)" in t.stages[0]


def test_tabs_resolve_live_with_currently_phrasing_in_sheet_order() -> None:
    from app.services.grid import Tab

    cfg = ConfigSpec.model_validate(load_reference_raw())
    wb = reference_workbook.build()
    t = describe_config(cfg, wb)
    assert t.summary == "Organizes 2 tabs with 3 rules — currently: MACHINES and SPARES."
    assert "every tab with a STATUS column (currently: MACHINES and SPARES)" in t.rules[0].headline
    wb.tabs = [x for x in wb.tabs if x.name != "SPARES"] + [Tab("NOTES", [["x"]])]
    gone = describe_config(cfg, wb)
    assert gone.summary == "Organizes 2 tabs with 3 rules — currently: MACHINES; not in the sheet now: SPARES."
    assert gone.tabs_missing == ["SPARES"]
    static = describe_config(cfg)
    assert static.summary == "Organizes 2 tabs with 3 rules (MACHINES and SPARES)." and "currently" not in static.summary


def test_owner_title_appears_in_readback() -> None:
    raw = load_reference_raw()
    raw["rules"][2]["presentation"]["title"] = "Q3 ORDERS"
    text = "\n".join(describe_config(ConfigSpec.model_validate(raw)).lines())
    assert "Title banner: “Q3 ORDERS”." in text


def test_numeric_conditions_have_a_plain_english_readback() -> None:
    raw = load_reference_raw()
    raw["canonical_headers"].extend([
        {"canonical": "DAYS REQUIRED", "match": ["equals:DAYS REQUIRED"]},
        {"canonical": "ACTUAL COST", "match": ["equals:ACTUAL COST"]},
        {"canonical": "BUDGET", "match": ["equals:BUDGET"]},
    ])
    raw["rules"] = [{
        "id": "long_tasks", "action": "format", "tabs": ["MACHINES"], "trigger": {"on_edit": {}},
        "row_rules": [{
            "when": {"numeric": {"left": {"subtract": [{"column": "ACTUAL COST"}, {"column": "BUDGET"}]},
                         "op": "between", "right": {"min": {"literal": 5}, "max": {"literal": 20}}}},
            "font": "#FF0000",
        }],
    }]
    cfg = ConfigSpec.model_validate(raw)
    detail = rule_text(cfg.rules[0], 1, Scope(cfg)).details[1].text
    assert detail == "Where ACTUAL COST − BUDGET is between 5 and 20 (inclusive): red text."


def test_frontend_readback_fixture_matches_the_describe_endpoint_payload() -> None:
    """The Approvals page's fixture is exactly what GET /configs/{id}/describe returns for the
    reference config, so UI work and tests can't drift from the API's readback."""
    from app.services.views import describe_json

    fixture = Path(__file__).resolve().parents[2] / "frontend" / "src" / "fixtures" / "describe.reference.json"
    live = describe_json(load_reference_raw(), reference_workbook.build())  # as the page receives it
    assert json.loads(fixture.read_text(encoding="utf-8")) == live

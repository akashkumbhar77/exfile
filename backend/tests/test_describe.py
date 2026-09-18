"""describe.py: deterministic plain-English readback (SPEC-PATCH-003 A.2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.schemas.config import ConfigSpec
from app.services.describe import colour, condition_text, describe_config, rule_text
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
    assert rule_text(cfg.rules[0], cfg).headline == expected


def test_triggers_and_details() -> None:
    cfg = _cfg([
        {"id": "cl", "action": "clear", "tabs": ["MACHINES"], "trigger": {"debounced": {"quiet_seconds": 45}},
         "when": {"date": "DISPATCH DATE", "after": "2026-01-31"}, "columns": ["FREEZE?"]},
        {"id": "cp", "action": "copy", "tabs": ["MACHINES"], "trigger": {"schedule": {"cron": "0 7 * * 1"}},
         "when": {"column": "STATUS", "contains": "hold"}, "to_tab": "SPARES", "key_columns": ["STATUS"]},
    ])
    a, b = (rule_text(r, cfg) for r in cfg.rules)
    assert a.when == "45 seconds after edits stop" and "DISPATCH DATE is after 2026-01-31" in a.headline
    assert b.when == "on the schedule “0 7 * * 1” (cron)" and "STATUS contains “hold”" in b.headline
    assert b.details == ["A row is copied only once, identified by STATUS."]


def test_colours_are_named_with_hex() -> None:
    assert colour("#ff0000") == "red (#FF0000)"
    assert colour("#123456").endswith("(#123456)") and "-ish" in colour("#123456")


def test_owner_title_appears_in_readback() -> None:
    raw = load_reference_raw()
    raw["rules"][2]["presentation"]["title"] = "Q3 ORDERS"
    text = "\n".join(describe_config(ConfigSpec.model_validate(raw)).lines())
    assert "Title banner: “Q3 ORDERS”." in text


def test_frontend_readback_fixture_matches_the_describe_endpoint_payload() -> None:
    """The Approvals page's fixture is exactly what GET /configs/{id}/describe returns for the
    reference config, so UI work and tests can't drift from the API's readback."""
    from app.services.views import describe_json

    fixture = Path(__file__).resolve().parents[2] / "frontend" / "src" / "fixtures" / "describe.reference.json"
    assert json.loads(fixture.read_text(encoding="utf-8")) == describe_json(load_reference_raw())

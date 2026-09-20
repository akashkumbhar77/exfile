"""The apps_script capability matrix refuses, by name and reason, what it cannot emit.

PATCH-004 A.3: silent partial emission is a build failure, so a refused rule means no script at
all. PATCH-005 I.7 settles move/copy (supported, in-file) and schedule (hourly or coarser), and
forces the backup tab on for destructive rules because this tier has no undo.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.emitters import capabilities
from app.emitters.apps_script import emit
from app.schemas.config import ConfigSpec
from tests.conftest import load_reference_raw


def config_with(rules: list[dict[str, Any]], **guards: Any) -> ConfigSpec:
    raw = load_reference_raw()
    raw["rules"] = rules
    if guards:
        raw["guards"] = {**raw["guards"], **guards}
    return ConfigSpec.model_validate(raw)


def only_sort() -> dict[str, Any]:
    raw = load_reference_raw()
    return next(r for r in raw["rules"] if r["action"] == "sort")


def refusals(config: ConfigSpec) -> list[tuple[str, str]]:
    result = emit(config)
    assert result.script is None, "a refused config must emit nothing at all"
    return [(r.pointer, r.code) for r in result.refusals]


def test_a_refused_rule_stops_the_whole_script() -> None:
    """The reference consolidate rule emits; its debounced trigger does not, and that is enough
    to withhold the whole script rather than ship one that quietly never runs that rule."""
    config = ConfigSpec.model_validate(load_reference_raw())
    codes = refusals(config)
    assert codes == [("/rules/2/trigger", "unsupported_trigger")]
    assert "not emitted yet" in emit(config).refusals[0].message


def test_move_and_copy_are_in_the_matrix_and_name_their_milestone() -> None:
    assert capabilities.ACTIONS["move"].in_matrix and capabilities.ACTIONS["copy"].in_matrix
    for action in ("move", "copy"):
        support = capabilities.ACTIONS[action]
        assert not support.emitted and "S7b" in support.note


def test_a_destructive_rule_requires_the_backup_tab() -> None:
    clear_rule = {"id": "purge", "action": "clear", "tabs": ["MACHINES"], "trigger": {"on_edit": {}},
                  "when": {"column": "STATUS", "equals": "CANCELLED"}, "columns": ["STATUS"]}
    without = refusals(config_with([clear_rule]))
    assert ("/guards/backup_tab", "backup_tab_required") in without

    with_backup = [c for c in refusals(config_with([clear_rule], backup_tab=True))
                   if c[1] == "backup_tab_required"]
    assert with_backup == [], "turning the backup tab on clears that refusal"


@pytest.mark.parametrize(("cron", "refused"), [
    ("0 2 * * *", False),        # 02:00 daily
    ("30 * * * *", False),       # half past every hour
    ("*/15 * * * *", True),      # every quarter hour
    ("* * * * *", True),         # every minute
    ("0,30 * * * *", True),      # twice an hour
])
def test_schedules_finer_than_hourly_are_refused(cron: str, refused: bool) -> None:
    rule = {**only_sort(), "trigger": {"schedule": {"cron": cron}}}
    codes = [c for c in refusals(config_with([rule])) if c[1] == "schedule_too_frequent"]
    assert bool(codes) is refused, cron

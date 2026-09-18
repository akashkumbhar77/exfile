"""Guards the CLAUDE.md rule that dry-run and execution share ONE rule-evaluation path.

plan_run (app/services/runner.py) is the only caller of the evaluator. dry_run and
execute_run both call plan_run; neither may call the evaluator directly or carry its own
rule logic. These tests fail if someone adds a second implementation later.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import app.services.dry_run as dry_run_mod
import app.services.executor as executor_mod
import app.services.runner as runner_mod
from app.schemas.config import ConfigSpec
from app.services.conditions import EvalContext
from app.services.grid import Workbook
from app.services.runner import RunEvent

APP = Path(__file__).resolve().parents[1] / "app"
RULES_PKG = APP / "services" / "rules"
EVALUATOR_NAMES = {
    "evaluate_rule", "evaluate_sort", "evaluate_format", "evaluate_consolidate", "evaluate_transfer",
    "evaluate_dedupe", "evaluate_clear", "evaluate_validate", "sort_rows", "format_view", "apply_plan",
}


def _referenced_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.name for a in node.names)
    return names


def test_only_runner_touches_the_evaluator() -> None:
    offenders = {}
    for path in APP.rglob("*.py"):
        if RULES_PKG in path.parents or path.name == "runner.py":
            continue
        hits = _referenced_names(path) & EVALUATOR_NAMES
        if hits:
            offenders[str(path.relative_to(APP))] = sorted(hits)
    assert offenders == {}, f"evaluator used outside plan_run: {offenders}"


@pytest.mark.parametrize("mod", [dry_run_mod, executor_mod])
def test_dry_run_and_executor_depend_on_plan_run(mod: object) -> None:
    assert getattr(mod, "plan_run") is runner_mod.plan_run


def test_both_paths_call_plan_run_exactly_once(
    monkeypatch: pytest.MonkeyPatch, config: ConfigSpec, workbook: Workbook, ctx: EvalContext
) -> None:
    calls: list[str] = []
    real = runner_mod.plan_run

    def spy(*args: object, **kwargs: object) -> runner_mod.RunPlan:
        calls.append("plan_run")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(dry_run_mod, "plan_run", spy)
    monkeypatch.setattr(executor_mod, "plan_run", spy)
    dry_run_mod.dry_run(config, workbook, ctx)
    executor_mod.execute_run(config, workbook, RunEvent.manual(), ctx)
    assert calls == ["plan_run", "plan_run"]

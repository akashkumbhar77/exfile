"""B.6: raw cell values never reach logs, error messages, reprs or run records.

Two layers:
  * dynamic: Redacted / grid containers stringify as <redacted>; a full live run
    at DEBUG level logs no cell content; errors carry no cell content.
  * static (lint): log / print / exception / warning / run-record formatting call
    sites in app code may not reference cell-value-carrying names or reveal().
"""

from __future__ import annotations

import ast
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.adapters.base import StaticRegistry, WriteValues
from tests.live.sheets_adapter import SheetsAdapter
from app.core.redaction import REDACTED, Redacted
from app.schemas.config import ConfigSpec
from app.services.executor import execute_run
from app.services.grid import Workbook, cell_text, is_empty
from app.services.runner import RunEvent
from app.services.conditions import EvalContext
from app.services.snapshots import decode_tab, take_snapshot
from tests.fake_sheets import FakeSheetsService, seed
from tests.fixtures import reference_workbook

BACKEND = Path(__file__).resolve().parents[1]
SID = "1AbCreferenceSheet"
NOW = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)


def _cell_texts(wb: Workbook, header_row: int = 2) -> set[str]:
    """Free-text data values (not headers / tab names / enum labels), long enough to grep for."""
    enum_like = {"YES", "NO", "COMPLETED", "IN-PROCESS", "IN PROCESS", "A. IN PROCESS", "CANCELLED",
                 "DISPUTED", "DISPATCHED", "ON HOLD", "IN-HOUSE", "OUTSOURCE", "REPAIR", "B. COMPLETED"}
    out: set[str] = set()
    for t in wb.tabs:
        for row in t.values[header_row:]:
            for v in row:
                s = cell_text(v).strip()
                if not is_empty(v) and isinstance(v, str) and len(s) >= 3 and s.upper() not in enum_like:
                    out.add(s)
    return out


# ---- dynamic ------------------------------------------------------------------------------


def test_redacted_never_stringifies_its_value() -> None:
    r = Redacted("Acme Industries")
    for s in (str(r), repr(r), f"{r}", f"{r!r}", f"{r:>20}", "%s" % r, "{}".format(r), str([r]), str({"k": r})):
        assert "Acme" not in s
        assert REDACTED in s
    assert r.reveal() == "Acme Industries"
    with pytest.raises(TypeError):
        import pickle

        pickle.dumps(r)


def test_grid_containers_repr_redacted(workbook: Workbook, config: ConfigSpec) -> None:
    secrets = _cell_texts(workbook)
    assert "Acme" in secrets and "Lathe" in secrets
    ctx = EvalContext("r", reference_workbook.TODAY)
    result = execute_run(config, workbook, RunEvent.manual(), ctx)
    tab = workbook.tabs[1]
    objs: list[object] = [workbook, tab, result.plan, result.plan.plans, result.snapshots, result.records,
                          take_snapshot("r", "x", tab.name, tab),
                          WriteValues(tab.name, 1, 1, Redacted([list(r) for r in tab.values]))]
    for o in objs:
        text = repr(o) + str(o)
        leaked = {s for s in secrets if s in text}
        assert not leaked, f"{type(o).__name__} repr leaks {len(leaked)} cell value(s)"


LINTED = [BACKEND / "app", BACKEND / "cli.py"]  # cli.py: the P2 `generate` command
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
LOGGER_NAMES = {"log", "logger", "logging", "LOG", "_log"}
# Names that, in this codebase, hold cell contents (rows, cells, grids, snapshot bodies).
VALUE_NAMES = {
    "v", "value", "values", "cell", "cells", "row", "raw_rows", "grid_vals", "block", "rows_data",
    "body_gz", "body", "before", "after", "workbook", "wb", "tab_values", "effectiveValue",
    "stringValue", "numberValue", "formattedValue", "userEnteredValue",
}
RECORD_ATTRS = {"error", "blocked"}  # RulePlan / RunRecord fields that are persisted to the runs table


def _sink_args(node: ast.AST) -> list[ast.AST] | None:
    """If node is a formatting sink, return the expressions that flow into the text."""
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name) and f.id in {"print", "_say"}:  # _say: cli.py's ASCII-safe print
            return [*node.args, *(k.value for k in node.keywords)]
        if isinstance(f, ast.Attribute):
            owner = f.value
            if f.attr in LOG_METHODS and isinstance(owner, ast.Name) and owner.id in LOGGER_NAMES:
                return [*node.args, *(k.value for k in node.keywords)]
            if f.attr in {"append", "extend"} and isinstance(owner, (ast.Name, ast.Attribute)):
                name = owner.id if isinstance(owner, ast.Name) else owner.attr
                if name == "warnings":
                    return list(node.args)
        # run records: RunRecord(..., error) / keyword error=...
        return [k.value for k in node.keywords if k.arg in RECORD_ATTRS] or None
    if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
        return [*node.exc.args, *(k.value for k in node.exc.keywords)]
    if isinstance(node, ast.Assign):
        if any(isinstance(t, ast.Attribute) and t.attr in RECORD_ATTRS for t in node.targets):
            return [node.value]
    return None


SAFE_CALLS = {"type", "len"}  # expose only a type name / a count, never content


def _offending(expr: ast.AST) -> list[str]:
    bad: list[str] = []
    stack = [expr]
    while stack:
        sub = stack.pop()
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in SAFE_CALLS:
            continue
        if isinstance(sub, ast.Name) and sub.id in VALUE_NAMES:
            bad.append(sub.id)
        elif isinstance(sub, ast.Attribute) and (sub.attr in VALUE_NAMES or sub.attr == "reveal"):
            bad.append("." + sub.attr)
        stack.extend(ast.iter_child_nodes(sub))
    return bad


def _files() -> list[Path]:
    out: list[Path] = []
    for p in LINTED:
        out.extend(sorted(p.rglob("*.py")) if p.is_dir() else [p])
    return out


# Functions whose output to the owner's own screen IS the product: the preview shows them their
# rows before and after (PATCH-003/005: values on screen only, never in logs or storage). Each entry
# is reviewed here; nothing is exempted by a comment in the code.
SCREEN_OUTPUT = {("cli.py", "_print_preview")}


def _enclosing_functions(tree: ast.AST) -> dict[int, str]:
    out: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for line in range(fn.lineno, (fn.end_lineno or fn.lineno) + 1):
                out.setdefault(line, fn.name)
    return out


def test_lint_no_cell_values_at_log_or_error_sites() -> None:
    violations: list[str] = []
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions = _enclosing_functions(tree)
        rel = path.relative_to(BACKEND).as_posix()
        for node in ast.walk(tree):
            args = _sink_args(node)
            if not args:
                continue
            line = getattr(node, "lineno", 0)
            if (rel, functions.get(line, "")) in SCREEN_OUTPUT:
                continue
            for a in args:
                for name in _offending(a):
                    violations.append(f"{rel}:{line or '?'}: {name}")
    assert not violations, "cell-value names reach log/error sites:\n" + "\n".join(violations)


def test_lint_catches_a_leak() -> None:
    """The lint must actually fire on the patterns it guards against."""
    bad = [
        "log.info('status %s', row[3])",
        "logger.warning(f'bad {value!r}')",
        "raise ValueError(f'bad cell {v!r}')",
        "plan.warnings.append(f'{cell}')",
        "plan.error = f'x {tab.values[2][1]}'",
        "print(op.values.reveal())",
        "RunRecord('r', None, 1, 't', 0, 0, 'ERROR', error=str(before))",
    ]
    for src in bad:
        node = ast.parse(src).body[0]
        target = node.value if isinstance(node, ast.Expr) else node
        args = _sink_args(target)
        assert args and any(_offending(a) for a in args), src


def test_reveal_only_in_evaluator_and_write_path() -> None:
    allowed = {Path("app/adapters/sheets_adapter.py"), Path("app/adapters/base.py"), Path("app/core/redaction.py")}
    users = {
        path.relative_to(BACKEND)
        for path in _files()
        if ".reveal()" in path.read_text(encoding="utf-8")
    }
    assert users <= allowed, f"reveal() used outside the write path: {sorted(map(str, users - allowed))}"

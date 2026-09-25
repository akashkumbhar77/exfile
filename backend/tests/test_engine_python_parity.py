"""Engine.gs vs the Python evaluator on random workbooks + random configs.

The backend dry-run (Python) is what a human approves; Engine.gs is what runs in the
sheet. This test proves they agree for every action (sort, format, consolidate, move,
copy, dedupe, clear, validate), hold rows, guards, aborts and edit events.

Requires Node.js (skipped otherwise). All cases run in one Node process.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.schemas.config import ConfigSpec
from app.services.conditions import EvalContext
from app.services.executor import ExecutionResult, execute_run
from app.services.grid import CellFormat, CellValue, Tab, Workbook
from app.services.preflight import schema_hashes_for
from app.services.runner import RunEvent
from app.services.validator import validate_config
from scripts.export_engine_fixtures import encode_cell
from tests.conftest import load_reference_raw

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "engine" / "test" / "harness.js"
TODAY = date(2026, 9, 16)
CASES = 120


def _node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    default = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe"
    return str(default) if default.exists() else None


NODE = _node()
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js not installed")

STATUSES = ["In Process", "A. IN PROCESS", "Completed", "Disputed", "Dispatched", "Cancelled",
            "cancel", "", "On Hold", "??"]
FONT_POOL = ["#FF0000", "#008000", "#D4A017", "#123456"]


# --------------------------------------------------------------------------- generation


def _cell(rng: random.Random, header: str, i: int) -> CellValue:
    h = header.upper()
    if h == "STATUS":
        return rng.choice(STATUSES)
    if "DISPATCH" in h:
        roll = rng.random()
        if roll < 0.15:
            return ""
        if roll < 0.25:
            return rng.choice(["2026-09-01", "2026-02-30", "soon", "20260901"])
        return TODAY + timedelta(days=rng.randint(-15, 15))
    if "FREEZE" in h:
        return rng.choice(["YES", "yes", "NO", ""])
    if h == "QTY":
        return rng.choice(["", 1, 2, 3, 2.5, "7", " 10 ", "x", True])
    if h == "ID":
        return rng.choice(["", f"K{rng.randint(1, 6)}", f"K{rng.randint(1, 6)}", rng.randint(1, 3)])
    if h == "!HOLD":
        return rng.choice(["", "", "", "", "x", True, False, 0, 1, "no"])
    return rng.choice(["", f"n{i}", f"N{i % 3}", "note"])


def _tab(rng: random.Random, name: str, headers: list[str], rows: int) -> Tab:
    data: list[list[CellValue]] = []
    for i in range(rows):
        row = [_cell(rng, h, i) for h in headers]
        if rng.random() < 0.07:
            row = [""] * len(headers)
        data.append(row)
    if data:
        data[-1][headers.index("STATUS")] = rng.choice(STATUSES[:6])
    tab = Tab(name, [[f"{name} title"], list(headers), *data])
    for r in range(2, tab.height):  # some pre-existing formatting on content rows
        if rng.random() < 0.15:
            tab.formats[r] = [CellFormat(rng.choice(FONT_POOL), rng.choice([None, "#ABCDEF"]), rng.random() < 0.3)
                              for _ in range(tab.width)]
    return tab


def random_workbook(rng: random.Random) -> Workbook:
    open_cols = ["ID", "STATUS", rng.choice(["DISPATCH DATE", "TENTATIVE DISPATCH DATE"]), "QTY", "NOTE",
                 rng.choice(["WORK ORDER FREEZE?", "TECHNICAL FREEZE?"])]
    if rng.random() < 0.6:
        open_cols.append("!hold")
    rng.shuffle(open_cols)
    archive_cols = ["ID", "STATUS", "DISPATCH DATE", "QTY"] + (["NOTE", "FREEZE?"] if rng.random() < 0.7 else [])
    rng.shuffle(archive_cols)
    other_cols = ["STATUS", "ID", "NOTE"]
    tabs = [Tab("LEGENDS", [["legend"], ["COLOUR", "MEANING"]])]
    tabs.append(_tab(rng, "OPEN", open_cols, rng.randint(2, 18)))
    tabs.append(_tab(rng, "ARCHIVE", archive_cols, rng.randint(0, 6)))
    if rng.random() < 0.6:
        tabs.append(_tab(rng, "OTHER", other_cols, rng.randint(1, 8)))
    if rng.random() < 0.3:
        tabs.append(_tab(rng, "UNGOVERNED", ["STATUS", "ID"], 3))
    return Workbook(tabs)


CONDITIONS: list[dict[str, Any]] = [
    {"enum": "STATUS", "is": "IN-PROCESS"},
    {"enum": "STATUS", "is": "CANCELLED"},
    {"enum": "STATUS", "is": "COMPLETED"},
    {"not": {"enum": "STATUS", "is": "COMPLETED"}},
    {"any": [{"enum": "STATUS", "is": "DISPUTED"}, {"column": "NOTE", "contains": "n1"}]},
    {"all": [{"enum": "STATUS", "is": "IN-PROCESS"}, {"date": "DISPATCH DATE", "before": "today"}]},
    {"date": "DISPATCH DATE", "after": "2026-09-20"},
    {"column": "QTY", "equals": 2},
    {"column": "NOTE", "is_blank": True},
    {"column": "FREEZE?", "equals": "YES"},
]
SORT_KEYS: list[dict[str, Any]] = [
    {"column": "STATUS", "using_enum": "STATUS"},
    {"column": "STATUS", "using_enum": "STATUS", "order": "desc"},
    {"column": "DISPATCH DATE", "type": "date"},
    {"column": "DISPATCH DATE", "type": "date", "order": "desc", "blanks": "first"},
    {"column": "QTY", "type": "number", "order": "desc"},
    {"column": "NOTE", "type": "text"},
    {"column": "ID"},
]


def random_config(rng: random.Random, wb: Workbook) -> dict[str, Any]:
    raw = load_reference_raw()
    raw["canonical_headers"] += [
        {"canonical": "QTY", "match": ["equals:QTY"]},
        {"canonical": "ID", "match": ["equals:ID"]},
        {"canonical": "NOTE", "match": ["equals:NOTE"]},
    ]
    governed = [t.name for t in wb.tabs if t.name in ("OPEN", "ARCHIVE", "OTHER")]
    raw["schema_hashes"] = schema_hashes_for(wb, governed, 2)
    rules: list[dict[str, Any]] = []
    selector: Any = "all_with:STATUS" if rng.random() < 0.6 else rng.sample(governed, rng.randint(1, len(governed)))

    edit_columns = rng.choice([[], ["STATUS"], ["STATUS", "DISPATCH DATE"], ["NOTE", "QTY"]])
    rules.append({"id": "sorter", "tabs": selector, "trigger": {"on_edit": {"columns": edit_columns}}, "action": "sort",
                  "keys": rng.sample(SORT_KEYS, rng.randint(1, 3))})
    if rng.random() < 0.85:
        fmt: dict[str, Any] = {
            "id": "painter", "tabs": selector, "trigger": {"after": "sorter"}, "action": "format",
            "row_rules": [{"when": c, "font": rng.choice(FONT_POOL), **rng.choice([{}, {"background": "#FCE4EC"},
                                                                                    {"strike": True}, {"background": "none"}])}
                          for c in rng.sample(CONDITIONS, rng.randint(0, 4))],
            "cell_rules": [{"column": "FREEZE?", "when": {"equals": "YES"}, "font": "#008000"}] if rng.random() < 0.7 else [],
        }
        if not fmt["row_rules"] and not fmt["cell_rules"]:
            fmt["cell_rules"] = [{"column": "NOTE", "when": {"contains": "n"}, "background": "#EEEEEE"}]
        if rng.random() < 0.3:
            fmt["default"] = {"font": "#333333"}
        rules.append(fmt)

    extra = rng.sample(["move", "copy", "dedupe", "clear", "validate"], rng.randint(0, 2))
    for kind in extra:
        trig: dict[str, Any] = rng.choice([{"on_edit": {}}, {"after": "sorter"}])
        if kind == "move":
            rules.append({"id": "mover", "tabs": ["OPEN"], "trigger": trig, "action": "move",
                          "when": rng.choice(CONDITIONS), "to_tab": "ARCHIVE",
                          "position": rng.choice(["top", "bottom"])})
        elif kind == "copy":
            rules.append({"id": "copier", "tabs": ["OPEN"], "trigger": trig, "action": "copy",
                          "when": rng.choice(CONDITIONS), "to_tab": "ARCHIVE", "key_columns": ["ID"]})
        elif kind == "dedupe":
            rules.append({"id": "deduper", "tabs": selector, "trigger": trig, "action": "dedupe",
                          "key_columns": rng.choice([["ID"], ["ID", "STATUS"]]),
                          "keep": rng.choice(["first", "last"])})
        elif kind == "clear":
            rules.append({"id": "clearer", "tabs": selector, "trigger": trig, "action": "clear",
                          "when": rng.choice(CONDITIONS), "columns": rng.sample(["NOTE", "QTY", "DISPATCH DATE"], 2)})
        else:
            rule: dict[str, Any] = {"id": "dropdown", "tabs": selector, "trigger": trig, "action": "validate",
                                    "column": "STATUS"}
            if rng.random() < 0.5:
                rule["from_enum"] = "STATUS"
            else:
                rule["values"] = ["In Process", "Completed", "Cancelled"]
            rules.append(rule)

    if rng.random() < 0.75:
        cons: dict[str, Any] = {
            "id": "summary", "trigger": {"debounced": {"quiet_seconds": 60}}, "action": "consolidate",
            "sources": "all_with:STATUS", "target_tab": "SUMMARY", "lock": rng.random() < 0.8,
            "prepend_columns": [{"name": "SOURCE SHEET", "value": "tab_name"}] if rng.random() < 0.7 else [],
            "derived": [{"name": "Type of Work", "fill_if_empty": "tab_name"}] if rng.random() < 0.6 else [],
            "presentation": {"title": "ALL"} if rng.random() < 0.5 else {},
        }
        if rng.random() < 0.7:
            cons["sort_like"] = "sorter"
        if rng.random() < 0.6 and any(r["id"] == "painter" for r in rules):
            cons["format_like"] = "painter"
        rules.append(cons)

    raw["rules"] = rules
    raw["guards"] = {"max_rows_per_run": rng.choice([500, 500, 500, 4]), "snapshot_destructive": True,
                     "hold_column": "!hold"}
    result = validate_config(raw)
    assert result.ok, result.errors
    return raw


# --------------------------------------------------------------------------- comparison


def _norm_value(v: Any) -> Any:
    if isinstance(v, dict):
        return v  # encoded date
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return ("bool", v)
    if isinstance(v, float) and v == int(v):
        return int(v)
    return encode_cell(v)


def _trim(rows: list[list[Any]]) -> list[list[Any]]:
    rows = [list(r) for r in rows]
    while rows and all(v == "" for v in rows[-1]):
        rows.pop()
    width = max((max((i + 1 for i, v in enumerate(r) if v != ""), default=0) for r in rows), default=0)
    return [r[:width] + [""] * (width - len(r[:width])) for r in rows]


def _py_values(tab: Tab) -> list[list[Any]]:
    return _trim([[_norm_value(v) for v in row] for row in tab.values])


def _engine_values(tab: dict[str, Any]) -> list[list[Any]]:
    return _trim([[_norm_value(c["v"]) for c in row] for row in tab["cells"]])


def _py_fmt(f: CellFormat) -> tuple[str, str | None, bool]:
    return ((f.font or "#000000").lower(), f.background.lower() if f.background else None, f.strike)


def _engine_fmt(c: dict[str, Any]) -> tuple[str, str | None, bool]:
    return (c["fc"], c["bg"], c["fl"] == "line-through")


def _compare_formats(py: Tab, eng: dict[str, Any], first_row: int, label: str) -> None:
    height = min(py.height, len(eng["cells"]))
    for r in range(first_row - 1, height):
        for c in range(py.width):
            ec = eng["cells"][r][c] if c < len(eng["cells"][r]) else None
            got = _engine_fmt(ec) if ec else ("#000000", None, False)
            assert got == _py_fmt(py.formats[r][c]), f"{label}: format at R{r + 1}C{c + 1}"


def _compare_validations(py: Tab, eng: dict[str, Any], label: str) -> None:
    got = {}
    for r, row in enumerate(eng["cells"]):
        for c, cell in enumerate(row):
            if cell.get("dv"):
                got[(r + 1, c + 1)] = (tuple(cell["dv"]["values"]), cell["dv"]["allowInvalid"])
    want = {k: (v.values, v.allow_invalid) for k, v in py.validations.items()}
    assert got == want, f"{label}: validations"


def _engine_status(records: list[dict[str, Any]]) -> str:
    runs = [r for r in records if r.get("type") == "run"]
    if any(r["status"] == "PAUSED_DRIFT" for r in runs):
        return "PAUSED_DRIFT"
    return "ERROR" if any(r["status"] == "ERROR" for r in runs) else "OK"


def compare(py: ExecutionResult, eng: dict[str, Any], config: ConfigSpec, label: str, *, targets_built: bool) -> None:
    py_status = {"OK": "OK", "ERROR": "ERROR", "BLOCKED": "ERROR", "PAUSED_DRIFT": "PAUSED_DRIFT"}[py.plan.status]
    assert _engine_status(eng["queue"]) == py_status, f"{label}: status {py.plan.status}"
    eng_tabs = {t["name"]: t for t in eng["tabs"] if t["name"] not in ("_config", "_engine_snapshots")}
    targets = {r.target_tab for r in config.rules if r.action == "consolidate"}
    py_names = [t.name for t in py.workbook.tabs if targets_built or t.name not in targets]
    eng_names = [n for n in eng_tabs if targets_built or n not in targets]
    assert eng_names == py_names, f"{label}: tab order"

    for tab in py.workbook.tabs:
        if tab.name in targets:
            if not targets_built:
                continue
            e = eng_tabs[tab.name]
            assert _engine_values(e)[config.header_row - 1:] == _py_values(tab)[config.header_row - 1:], \
                f"{label}: {tab.name} values"
            assert e["protected"] == tab.protected, f"{label}: {tab.name} lock"
            _compare_formats(tab, e, config.data_start_row, f"{label}: {tab.name}")
            continue
        e = eng_tabs[tab.name]
        assert _engine_values(e) == _py_values(tab), f"{label}: {tab.name} values"
        _compare_formats(tab, e, 1, f"{label}: {tab.name}")
        _compare_validations(tab, e, f"{label}: {tab.name}")

    if py.plan.status == "OK":
        eng_rows = {r["rule_id"]: r["rows_affected"] for r in eng["queue"]
                    if r.get("type") == "run" and r["action"] in ("sort", "move", "copy", "dedupe", "clear", "consolidate")}
        py_rows = {r.rule_id: r.rows_affected for r in py.records
                   if config.rule(r.rule_id or "") is not None
                   and config.rule(r.rule_id or "").action in ("sort", "move", "copy", "dedupe", "clear", "consolidate")}  # type: ignore[union-attr]
        assert eng_rows == py_rows, f"{label}: rows_affected"


# --------------------------------------------------------------------------- driver


def _encode_workbook(wb: Workbook) -> dict[str, Any]:
    tabs = []
    for t in wb.tabs:
        formats = []
        for r, row in enumerate(t.formats):
            for c, f in enumerate(row):
                if f != CellFormat():
                    formats.append({"row": r + 1, "col": c + 1, "font": f.font, "background": f.background,
                                    "strike": f.strike})
        tab: dict[str, Any] = {"name": t.name, "rows": [[encode_cell(v) for v in row] for row in t.values],
                               "formats": formats}
        if t.number_formats:
            tab["number_formats"] = [{"row": r, "col": c, "rows": 1, "format": f}
                                     for (r, c), f in sorted(t.number_formats.items())]
        if t.column_widths:
            tab["column_widths"] = {str(c): w for c, w in t.column_widths.items()}
        if t.banding is not None:
            b = t.banding
            tab["bandings"] = [{"row": b.row, "col": 1, "nr": b.rows, "nc": b.cols, "theme": b.theme,
                                "header": False, "footer": False}]
        tabs.append(tab)
    return {"tabs": tabs}


def _edit_for(rng: random.Random, wb: Workbook) -> tuple[str, int, int, CellValue]:
    tab = wb.tab("OPEN")
    assert tab is not None
    col = rng.randrange(tab.width)
    row = rng.randint(3, tab.height + 1)
    return "OPEN", row, col + 1, _cell(rng, str(tab.row(2)[col]), row)


@pytest.fixture(scope="module")
def cases() -> list[dict[str, Any]]:
    out = []
    for seed in range(CASES):
        rng = random.Random(10_000 + seed)
        wb = random_workbook(rng)
        raw = random_config(rng, wb)
        edit = _edit_for(rng, wb)
        out.append({"seed": seed, "wb": wb, "raw": raw, "edit": edit})
    requests = []
    for case in out:
        base = {"script": "engine", "now": f"{TODAY.isoformat()}T10:00:00",
                "workbook": _encode_workbook(case["wb"]), "dump_min_rows": 40, "dump_min_cols": 12}
        setup = [{"call": "engineSetConfig", "args": [{"$json": case["raw"]}]}, {"call": "engineInstall"}]
        requests.append({**base, "steps": setup + [{"call": "engineRunAll"}]})
        tab, row, col, value = case["edit"]
        requests.append({**base, "steps": setup + [{"edit": [tab, row, col, encode_cell(value)]}]})
    proc = subprocess.run([NODE, str(HARNESS)], input=json.dumps(requests), capture_output=True, text=True,
                          encoding="utf-8", timeout=600)
    assert proc.returncode == 0, proc.stderr
    responses = json.loads(proc.stdout)
    for i, case in enumerate(out):
        case["engine_run_all"] = responses[2 * i]
        case["engine_edit"] = responses[2 * i + 1]
    return out


def test_actions_are_all_exercised(cases: list[dict[str, Any]]) -> None:
    seen = {r["action"] for c in cases for r in c["raw"]["rules"]}
    assert seen == {"sort", "format", "consolidate", "move", "copy", "dedupe", "clear", "validate"}
    statuses = {c["engine_run_all"] and _engine_status(c["engine_run_all"]["queue"]) for c in cases}
    assert statuses == {"OK", "ERROR"}, "both success and refusal paths must be covered"


@pytest.mark.parametrize("seed", range(CASES))
def test_run_all_matches_python(cases: list[dict[str, Any]], seed: int) -> None:
    case = cases[seed]
    config = ConfigSpec.model_validate(case["raw"])
    ctx = EvalContext(run_id=f"x{seed}", today=TODAY)
    py = execute_run(config, case["wb"], RunEvent.manual(), ctx)
    compare(py, case["engine_run_all"], config, f"seed {seed} run-all", targets_built=True)


@pytest.mark.parametrize("seed", range(CASES))
def test_edit_event_matches_python(cases: list[dict[str, Any]], seed: int) -> None:
    case = cases[seed]
    config = ConfigSpec.model_validate(case["raw"])
    wb = case["wb"].clone()
    tab_name, row, col, value = case["edit"]
    tab = wb.tab(tab_name)
    assert tab is not None
    tab.ensure_size(row, col)
    tab.values[row - 1][col - 1] = value
    ctx = EvalContext(run_id=f"e{seed}", today=TODAY)
    event = RunEvent("edit", tab=tab_name, columns=(col,))
    py = execute_run(config, wb, event, ctx)
    compare(py, case["engine_edit"], config, f"seed {seed} edit R{row}C{col}={value!r}", targets_built=False)

"""Randomized parity: ConfigSpec evaluator vs. an independent port of legacy.gs."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from app.schemas.config import ConfigSpec
from app.services.conditions import EvalContext
from app.services.executor import execute_run
from app.services.grid import Tab, Workbook
from app.services.preflight import schema_hashes_for
from app.services.runner import RunEvent
from tests import legacy_oracle as legacy
from tests.conftest import load_reference_raw

STATUSES = ["IN-PROCESS", "In Process", "A. IN PROCESS", "Completed", "COMPLETED ", "Disputed",
            "Dispatched", "DISPATCHED", "Cancelled", "cancel", "", "On Hold", "??", "b. completed"]
FREEZE = ["YES", "yes", " YES ", "NO", "", None]
EXTRA = ["CUSTOMER NAME", "QTY", "INVOICE NO", "Type of Work", "REMARKS", "WORK ORDER NO"]
TODAY = date(2026, 9, 16)


def _rand_tab(rng: random.Random, name: str) -> Tab:
    cols = ["STATUS"]
    if rng.random() < 0.9:
        cols.append(rng.choice(["DISPATCH DATE", "TENTATIVE DISPATCH DATE"]))
    if rng.random() < 0.8:
        cols.append(rng.choice(["WORK ORDER FREEZE?", "TECHNICAL FREEZE?"]))
    cols += rng.sample(EXTRA, rng.randint(0, 4))
    rng.shuffle(cols)
    rows: list[list[object]] = []
    for i in range(rng.randint(2, 25)):
        row: list[object] = []
        for c in cols:
            if c == "STATUS":
                row.append(rng.choice(STATUSES))
            elif "DISPATCH" in c:
                row.append("" if rng.random() < 0.2 else TODAY + timedelta(days=rng.randint(-20, 20)))
            elif "FREEZE" in c:
                row.append(rng.choice(FREEZE))
            else:
                row.append(rng.choice(["", f"{c[:3]}{i}", rng.randint(1, 5)]))
        if rng.random() < 0.08:
            row = [""] * len(cols)
        rows.append(row)
    # legacy only sorts tabs with >1 data row whose last row has content
    rows[-1][cols.index("STATUS")] = rng.choice(STATUSES[:10])
    return Tab(name, [[f"{name} TITLE"], cols, *rows])  # type: ignore[list-item]


def _config(wb: Workbook, tabs: list[str]) -> ConfigSpec:
    raw = load_reference_raw()
    raw["schema_hashes"] = schema_hashes_for(wb, tabs, 2)
    return ConfigSpec.model_validate(raw)


def _norm(rows: list[list[object]]) -> list[list[object]]:
    return [["" if v is None else v for v in r] for r in rows]


@pytest.mark.parametrize("seed", range(60))
def test_parity_with_legacy(seed: int) -> None:
    rng = random.Random(seed)
    names = rng.sample(["MACHINES", "SPARES", "SERVICE", "PANELS"], rng.randint(1, 3))
    wb = Workbook([Tab("LEGENDS", [["x"], ["COLOUR", "MEANING"]]), *[_rand_tab(rng, n) for n in names]])
    config = _config(wb, names)
    ctx = EvalContext(run_id=f"parity{seed}", today=TODAY)

    ours = execute_run(config, wb, RunEvent.manual(), ctx)
    assert ours.plan.status == "OK"

    oracle = wb.clone()
    tms = legacy.today_ms(TODAY)
    for n in names:
        tab = oracle.tab(n)
        assert tab is not None
        legacy.sort_sheet(tab, tms)
        mine = ours.workbook.tab(n)
        assert mine is not None
        assert mine.values == tab.values, f"sort mismatch on {n}"
        assert mine.formats == tab.formats, f"format mismatch on {n}"

    headers, rows, formats = legacy.summary(oracle, tms)
    summary = ours.workbook.tab("SUMMARY")
    assert summary is not None
    assert summary.values[1] == headers
    assert _norm(summary.values[2:]) == _norm(rows)
    assert summary.formats[2:] == formats

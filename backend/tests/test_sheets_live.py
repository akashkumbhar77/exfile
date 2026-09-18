"""S1 exit criteria against the fake Sheets API (live Google checks are operator steps; see README).

  * --dry-run: per-tab summary, zero writes
  * live run: read back through the adapter == the M1 evaluator's result (== legacy.gs)
  * second consecutive run: 0 ops
plus atomicity, registry scoping (B.9), staleness and drift.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.adapters.base import StaticRegistry, UnregisteredSource
from app.adapters.sheets_adapter import SheetsAdapter
from app.schemas.config import ConfigSpec
from app.services.executor import execute_run
from app.services.grid import Tab, Workbook
from app.services.live import ConfigMismatch, StaleGrid, commit_run, prepare_run
from app.services.ops import diff_workbooks, norm_format
from app.services.runner import RunEvent
from app.services.snapshot_store import LocalSnapshotStore, SnapshotCipher
from app.services.snapshots import restore_snapshots
from app.services.conditions import EvalContext
from tests import legacy_oracle as legacy
from tests.fake_sheets import FakeHttpError, FakeSheetsService, seed
from tests.fixtures import reference_workbook
from tests.test_legacy_parity import _config as parity_config
from tests.test_legacy_parity import _norm, _rand_tab

SID = "1AbCreferenceSheet"
NOW = datetime(2026, 9, 16, 6, 0, tzinfo=UTC)  # 11:30 in Asia/Kolkata -> today = reference TODAY


@pytest.fixture
def store(tmp_path: Path) -> LocalSnapshotStore:
    return LocalSnapshotStore(tmp_path / "snaps", SnapshotCipher.from_key(Fernet.generate_key().decode(), "k1"), 30)


def _adapter(wb: Workbook, sid: str = SID) -> tuple[SheetsAdapter, FakeSheetsService]:
    svc = FakeSheetsService({sid: seed(wb)})
    return SheetsAdapter(svc, StaticRegistry(frozenset({sid})), editor_email="sa@example.iam"), svc


def _writes(svc: FakeSheetsService) -> int:
    return sum(1 for m, _ in svc.calls if m == "batchUpdate")


def _norm_formats(t: Tab) -> list[list[object]]:
    return [[norm_format(f) for f in row] for row in t.formats]


def test_read_grid_round_trips_reference_workbook(workbook: Workbook) -> None:
    adapter, _ = _adapter(workbook)
    grid = adapter.read_grid(SID)
    assert grid.timezone == "Asia/Kolkata"
    assert grid.workbook.names() == workbook.names()
    assert diff_workbooks(grid, workbook) == []  # nothing lost or altered by the read


def test_dry_run_summarizes_and_writes_nothing(workbook: Workbook, config: ConfigSpec) -> None:
    adapter, svc = _adapter(workbook)
    p = prepare_run(adapter, SID, config, now=NOW)
    assert p.status == "OK"
    assert p.ctx.today == reference_workbook.TODAY
    assert p.summary["MACHINES"].rows_rewritten > 0
    assert p.summary["MACHINES"].cells_recolored > 0
    assert "SUMMARY" in p.summary
    assert _writes(svc) == 0


def test_live_run_matches_evaluator_then_second_run_is_noop(
    workbook: Workbook, config: ConfigSpec, store: LocalSnapshotStore
) -> None:
    adapter, svc = _adapter(workbook)
    p = prepare_run(adapter, SID, config, now=NOW)
    commit_run(adapter, p, store)
    assert _writes(svc) == 1  # one atomic batchUpdate

    expected = execute_run(config, workbook, RunEvent.manual(), EvalContext("x", reference_workbook.TODAY)).workbook
    got = adapter.read_grid(SID).workbook
    assert got.names() == expected.names()
    for t in expected.tabs:
        g = got.tab(t.name)
        assert g is not None
        assert _norm(g.values) == _norm(t.values[: g.height]), t.name  # type: ignore[arg-type]
        assert _norm_formats(g) == _norm_formats(t)[: g.height], t.name
        assert g.protected == t.protected

    second = prepare_run(adapter, SID, config, now=NOW)
    assert second.ops == []
    commit_run(adapter, second, store)
    assert _writes(svc) == 1  # idempotent: nothing written


@pytest.mark.parametrize("seed_n", range(12))
def test_live_run_parity_with_legacy_through_adapter(seed_n: int, store: LocalSnapshotStore) -> None:
    """M1 parity assertions, now reading back through the adapter."""
    rng = random.Random(seed_n)
    names = rng.sample(["MACHINES", "SPARES", "SERVICE", "PANELS"], rng.randint(1, 3))
    wb = Workbook([Tab("LEGENDS", [["x"], ["COLOUR", "MEANING"]]), *[_rand_tab(rng, n) for n in names]])
    raw_config = parity_config(wb, names).model_dump(mode="json", by_alias=True, exclude_none=True)
    raw_config["sheet_id"] = SID
    config = ConfigSpec.model_validate(raw_config)
    adapter, _ = _adapter(wb)
    commit_run(adapter, prepare_run(adapter, SID, config, now=NOW), store)
    got = adapter.read_grid(SID).workbook

    oracle = wb.clone()
    tms = legacy.today_ms(reference_workbook.TODAY)
    for n in names:
        tab = oracle.tab(n)
        mine = got.tab(n)
        assert tab is not None and mine is not None
        legacy.sort_sheet(tab, tms)
        assert _norm(mine.values) == _norm(tab.values[: mine.height]), f"sort mismatch on {n}"  # type: ignore[arg-type]
        assert _norm_formats(mine) == _norm_formats(tab)[: mine.height], f"format mismatch on {n}"
    headers, rows, formats = legacy.summary(oracle, tms)
    summary = got.tab("SUMMARY")
    assert summary is not None
    assert summary.values[1] == headers
    assert _norm(summary.values[2:]) == _norm(rows)  # type: ignore[arg-type]
    assert [[norm_format(f) for f in r] for r in formats] == _norm_formats(summary)[2:]


def test_failed_batch_leaves_sheet_untouched(workbook: Workbook, config: ConfigSpec, store: LocalSnapshotStore) -> None:
    adapter, svc = _adapter(workbook)
    before = adapter.read_grid(SID)
    p = prepare_run(adapter, SID, config, now=NOW)
    svc.fail_next_batch = True
    with pytest.raises(FakeHttpError):
        commit_run(adapter, p, store)
    assert diff_workbooks(adapter.read_grid(SID), before.workbook) == []


def test_snapshot_saved_before_write_restores_exact_state(
    workbook: Workbook, config: ConfigSpec, store: LocalSnapshotStore
) -> None:
    adapter, _ = _adapter(workbook)
    original = adapter.read_grid(SID)
    p = prepare_run(adapter, SID, config, now=NOW)
    commit_run(adapter, p, store)
    sheet_ref, snaps = store.load(p.run_id)
    assert sheet_ref == SID
    after = adapter.read_grid(SID)
    restored = restore_snapshots(after.workbook, snaps)
    adapter.write_ops(SID, diff_workbooks(after, restored))
    assert diff_workbooks(adapter.read_grid(SID), original.workbook) == []


def test_unregistered_sheet_is_never_opened(workbook: Workbook, config: ConfigSpec) -> None:
    svc = FakeSheetsService({SID: seed(workbook), "someone-elses-sheet": seed(workbook)})
    adapter = SheetsAdapter(svc, StaticRegistry(frozenset({SID})))
    with pytest.raises(UnregisteredSource):
        adapter.read_grid("someone-elses-sheet")
    with pytest.raises(UnregisteredSource):
        adapter.write_ops("someone-elses-sheet", [])
    assert svc.calls == []


def test_config_for_another_sheet_is_refused(workbook: Workbook, config: ConfigSpec) -> None:
    adapter, svc = _adapter(workbook, sid="other")
    with pytest.raises(ConfigMismatch):
        prepare_run(adapter, "other", config, now=NOW)
    assert svc.calls == []


def test_edit_during_run_aborts_without_writing(
    workbook: Workbook, config: ConfigSpec, store: LocalSnapshotStore
) -> None:
    adapter, svc = _adapter(workbook)
    p = prepare_run(adapter, SID, config, now=NOW)
    sheet = svc.docs[SID].sheets[1]
    sheet.cells[(2, 1)] = {"userEnteredValue": {"stringValue": "edited mid-run"}}
    with pytest.raises(StaleGrid):
        commit_run(adapter, p, store)
    assert _writes(svc) == 0


def test_header_drift_pauses_and_writes_nothing(workbook: Workbook, config: ConfigSpec) -> None:
    adapter, svc = _adapter(workbook)
    machines = next(s for s in svc.docs[SID].sheets if s.title == "MACHINES")
    machines.cells[(1, 5)] = {"userEnteredValue": {"stringValue": "ORDER STATE"}}  # rename STATUS
    p = prepare_run(adapter, SID, config, now=NOW)
    assert p.status == "PAUSED_DRIFT"
    assert p.ops == []
    assert [d.tab for d in p.report.drift] == ["MACHINES"]
    assert _writes(svc) == 0


def test_dates_keep_date_type_after_reorder(workbook: Workbook, config: ConfigSpec, store: LocalSnapshotStore) -> None:
    adapter, svc = _adapter(workbook)
    commit_run(adapter, prepare_run(adapter, SID, config, now=NOW), store)
    got = adapter.read_grid(SID).workbook.tab("MACHINES")
    assert got is not None
    col = got.row(2).index("TENTATIVE DISPATCH DATE")
    assert all(isinstance(r[col], date) or r[col] in ("", None) for r in got.values[2:])


def test_date_formatted_number_outside_calendar_reads_as_number() -> None:
    from app.adapters.sheets_adapter import parse_sheet

    fmt = {"effectiveFormat": {"numberFormat": {"type": "DATE", "pattern": "dd-mm-yyyy"}}}
    sheet = {"properties": {"title": "T"}, "data": [{"rowData": [{"values": [
        {"effectiveValue": {"numberValue": 1e12}, **fmt},
        {"effectiveValue": {"numberValue": -1e7}, **fmt},
        {"effectiveValue": {"numberValue": 46281.0}, **fmt},
    ]}]}]}
    tab, _ = parse_sheet(sheet)
    assert tab.values[0] == [1_000_000_000_000, -10_000_000, date(2026, 9, 16)]


def test_summary_title_and_header_get_presentation_colours(workbook: Workbook, config: ConfigSpec) -> None:
    out = execute_run(config, workbook, RunEvent.manual(), EvalContext("x", reference_workbook.TODAY)).workbook
    summary = out.tab("SUMMARY")
    assert summary is not None
    assert (summary.formats[0][0].font, summary.formats[0][0].background) == ("#FFFFFF", "#38761D")
    assert {(f.font, f.background) for f in summary.formats[1][: summary.width]} == {("#000000", "#B6D7A8")}


def test_number_in_date_formatted_cell_is_not_a_change() -> None:
    from app.adapters.base import Grid
    from app.services.grid import date_serial

    d = date(2026, 3, 1)
    before = Workbook([Tab("T", [["H"], [d]])])
    after = Workbook([Tab("T", [["H"], [int(date_serial(d))]])])
    assert diff_workbooks(Grid(before), after) == []
    moved = Workbook([Tab("T", [["H"], [int(date_serial(d)) + 1]])])
    assert len(diff_workbooks(Grid(before), moved)) == 1

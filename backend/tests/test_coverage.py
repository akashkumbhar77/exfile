"""Clause coverage (PATCH-005 D.2): a mechanical diff of what the instruction names against what the
rules use. No model call; the vocabulary comes from the upload and fixed word lists."""

from __future__ import annotations

import pytest

from app.agent.coverage import clause_coverage
from app.schemas.config import ConfigSpec
from tests.conftest import load_reference_raw
from tests.fixtures import reference_workbook

CONFIG = ConfigSpec.model_validate(load_reference_raw())


def check(instruction: str) -> dict[tuple[str, str], bool]:
    return {(m.kind, m.text): m.covered for m in clause_coverage(instruction, CONFIG, reference_workbook.build())}


def test_what_the_reference_rules_cover() -> None:
    got = check("Sort MACHINES and SPARES by status stage then dispatch date, colour overdue rows red, "
                "and build a SUMMARY tab")
    assert got == {("tab", "MACHINES"): True, ("tab", "SPARES"): True, ("column", "DISPATCH DATE"): True,
                   ("column", "STATUS"): True, ("colour", "red"): True, ("timing", "(not said)"): True}


def test_unused_mentions_are_flagged() -> None:
    got = check("Every morning at 9 sort the sheet by customer name and highlight QTY over 5 in purple")
    assert {k for k, ok in got.items() if not ok} == {
        ("column", "CUSTOMER NAME"), ("column", "QTY"), ("colour", "purple"), ("timing", "every morning")}


def test_status_values_and_strike_through() -> None:
    got = check("Whenever status changes, sort by status and put Cancelled at the bottom; colour dispatched "
                "rows blue and strike them through")
    assert got[("value", "Cancelled")] and got[("value", "Dispatched")]
    assert got[("colour", "strike-through")] and not got[("colour", "blue")]
    assert got[("timing", "whenever status changes")], "the sort runs on STATUS edits"


def test_a_column_of_an_unrelated_tab_is_not_a_mention() -> None:
    """LEGENDS has a COLOUR column; "colour" as a verb must not flag it."""
    assert ("column", "COLOUR") not in check("colour overdue rows red")
    assert ("column", "COLOUR") in check("use the COLOUR column in LEGENDS")


def test_free_text_values_are_not_vocabulary() -> None:
    """Customer names are free text, not status labels: 'Acme' in the instruction is not checked."""
    assert not any(kind == "value" for kind, _ in check("sort Acme's rows first"))


@pytest.mark.parametrize("instruction", ["refresh every 5 minutes", "update every minute"])
def test_more_often_than_hourly_is_always_flagged(instruction: str) -> None:
    assert [ok for (kind, _), ok in check(instruction).items() if kind == "timing"] == [False]

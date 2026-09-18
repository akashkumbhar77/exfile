from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.schemas.config import ConfigSpec
from app.services.conditions import EvalContext
from app.services.grid import Workbook
from app.services.preflight import schema_hashes_for
from tests.fixtures import reference_workbook

FIXTURES = Path(__file__).parent / "fixtures"


def load_reference_raw() -> dict[str, Any]:
    return json.loads((FIXTURES / "reference_config.json").read_text(encoding="utf-8"))


@pytest.fixture
def raw_config() -> dict[str, Any]:
    return load_reference_raw()


@pytest.fixture
def config(raw_config: dict[str, Any]) -> ConfigSpec:
    return ConfigSpec.model_validate(raw_config)


@pytest.fixture
def workbook() -> Workbook:
    return reference_workbook.build()


@pytest.fixture
def ctx() -> EvalContext:
    return EvalContext(run_id="run_test", today=reference_workbook.TODAY)


def make_config(workbook: Workbook, governed: list[str], rules: list[dict[str, Any]], **extra: Any) -> ConfigSpec:
    """Reference headers/enums + custom rules, with hashes computed from `workbook`."""
    raw = load_reference_raw()
    raw["rules"] = copy.deepcopy(rules)
    raw["schema_hashes"] = schema_hashes_for(workbook, governed, raw["header_row"])
    raw.update(extra)
    return ConfigSpec.model_validate(raw)


def column(workbook: Workbook, tab: str, header: str, header_row: int = 2) -> list[object]:
    t = workbook.tab(tab)
    assert t is not None
    idx = t.row(header_row).index(header)
    return [r[idx] for r in t.values[header_row:]]

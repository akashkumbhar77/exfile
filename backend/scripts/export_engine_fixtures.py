"""Export the shared reference fixtures for the Engine.gs Node tests.

Writes engine/test/fixtures/{reference_workbook.json, reference_config.json} from the
Python fixtures so both implementations are tested against identical data.

Usage (from /backend):  uv run python -m scripts.export_engine_fixtures
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.grid import CellValue, Tab, Workbook
from tests.fixtures import reference_workbook

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "engine" / "test" / "fixtures"
CONFIG_SRC = ROOT / "backend" / "tests" / "fixtures" / "reference_config.json"

# Per-tab number formats on the dispatch-date column (legacy copies the first source's format).
NUMBER_FORMATS = {
    "MACHINES": [{"row": 3, "col": 5, "rows": 10, "format": "d/m/yyyy"}],
    "SPARES": [{"row": 3, "col": 4, "rows": 4, "format": "dd-mmm-yyyy"}],
}


def encode_cell(v: CellValue) -> Any:
    from datetime import date, datetime

    if isinstance(v, datetime):
        return {"$datetime": v.strftime("%Y-%m-%dT%H:%M:%S")}
    if isinstance(v, date):
        return {"$date": v.isoformat()}
    return "" if v is None else v


def encode_tab(tab: Tab) -> dict[str, Any]:
    return {
        "name": tab.name,
        "rows": [[encode_cell(v) for v in row] for row in tab.values],
        "number_formats": NUMBER_FORMATS.get(tab.name, []),
    }


def encode_workbook(wb: Workbook) -> dict[str, Any]:
    return {"tabs": [encode_tab(t) for t in wb.tabs]}


def render() -> dict[str, str]:
    workbook = encode_workbook(reference_workbook.build())
    workbook["today"] = reference_workbook.TODAY.isoformat()
    return {
        "reference_workbook.json": json.dumps(workbook, indent=1, ensure_ascii=False) + "\n",
        "reference_config.json": CONFIG_SRC.read_text(encoding="utf-8"),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in render().items():
        (OUT_DIR / name).write_text(text, encoding="utf-8")
        print(f"wrote {OUT_DIR / name}")


if __name__ == "__main__":
    main()

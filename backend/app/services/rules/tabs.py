"""Tab selector resolution.

Only governed tabs (present in `schema_hashes`) are ever eligible: a tab without
a stored header hash cannot be drift-checked, so no rule may touch it.
Consolidate targets are never eligible as rule inputs.
"""

from __future__ import annotations

from app.schemas.config import ConfigSpec, TabSelector
from app.services.grid import Workbook
from app.services.headers import build_view
from app.services.rules.base import config_targets


def resolve_tabs(
    selector: TabSelector,
    workbook: Workbook,
    config: ConfigSpec,
    *,
    exclude: set[str] | None = None,
    warnings: list[str] | None = None,
) -> list[str]:
    """Selected tab names in workbook order."""
    blocked = config_targets(config) | (exclude or set())
    out: list[str] = []
    for tab in workbook.tabs:
        if tab.name in blocked:
            continue
        if isinstance(selector, str):
            column = selector.split(":", 1)[1]
            if not build_view(tab, config).has(column):
                continue
            if tab.name not in config.schema_hashes:
                if warnings is not None:
                    warnings.append(f"tab {tab.name!r} has column {column!r} but is not governed; skipped")
                continue
        else:
            if tab.name not in selector or tab.name not in config.schema_hashes:
                continue
        out.append(tab.name)
    if isinstance(selector, list) and warnings is not None:
        for name in selector:
            if workbook.tab(name) is None:
                warnings.append(f"tab {name!r} not found in workbook; skipped")
    return out

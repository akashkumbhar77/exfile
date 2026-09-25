"""Upload -> compile -> readback + coverage + preview -> script (SPEC-PATCH-005 D, P2).

One function the CLI (and later the web page, P3) calls. Nothing here touches a spreadsheet or
stores anything: the upload is read in memory, the compile records into a MemoryRecorder, and the
caller decides what to write (the `.gs` and the `config.json` go to the owner, I.5).

Two ways in:
  * an instruction: the agent compiles it into a config, checked against what the generated
    script can express (a request it cannot meet comes back as a plain decline)
  * a config.json from an earlier download (D.5): no model call, same readback, preview and script
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from app.adapters.base import Grid
from app.adapters.xlsx_reader import read_xlsx
from app.agent.coverage import Mention, clause_coverage
from app.agent.llm import LlmClient
from app.agent.onboarding import ModelPlan, onboard
from app.agent.recorder import MemoryRecorder
from app.emitters import capabilities
from app.emitters.apps_script import emit
from app.schemas.config import ConfigSpec
from app.services.describe import ConfigText, describe_config
from app.services.grid import col_letter
from app.services.preview import compute_preview
from app.services.validator import validate_config

log = logging.getLogger("app.generate")

SHEET_REF = "uploaded-workbook"  # never the file name: it rides along in the script's config comment
ORG = "self-serve"
REWRITING_ACTIONS = set(capabilities.FORMULA_LIMITS)  # clear touches only the cells it blanks

Status = Literal["OK", "DECLINED", "REFUSED", "INVALID_CONFIG", "MODEL_UNAVAILABLE"]

# onboarding's reasons when the provider, not the instruction, is the problem
_PROVIDER_PROBLEMS = ("could not reach the LLM provider", "model(s) ")


@dataclass
class Generated:
    status: Status
    reason: str = ""
    config: ConfigSpec | None = None
    script: str | None = None
    readback: ConfigText | None = None
    coverage: list[Mention] = field(default_factory=list)
    preview: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)   # about the upload: addresses only (B.6)
    limits: list[str] = field(default_factory=list)     # what the script does not do, never softened

    @property
    def unmapped(self) -> list[Mention]:
        return [m for m in self.coverage if not m.covered]


def generate(upload: bytes, *, timezone: str, instruction: str | None = None,
             config_json: dict[str, Any] | None = None, llm: LlmClient | None = None,
             plan: ModelPlan | None = None, now: datetime | None = None,
             progress: Callable[[str], None] | None = None) -> Generated:
    if (instruction is None) == (config_json is None):
        raise ValueError("give exactly one of an instruction or a config")
    now = now or datetime.now(UTC)
    read = read_xlsx(upload, timezone=timezone)
    grid = read.grid

    if config_json is not None:
        loaded = _load_config(config_json, grid)
        if isinstance(loaded, str):
            return Generated("INVALID_CONFIG", reason=loaded, warnings=read.warnings)
        config = loaded
    else:
        assert instruction is not None
        if llm is None or plan is None:
            raise ValueError("compiling an instruction needs an LLM client and a model plan")
        result = onboard(MemoryRecorder(), None, llm, plan, ORG, SHEET_REF, instruction, now=now,
                         progress=progress, grid=grid)
        if result.config is None:
            status: Status = "MODEL_UNAVAILABLE" if result.reason.startswith(_PROVIDER_PROBLEMS) else "DECLINED"
            return Generated(status, reason=result.reason, warnings=read.warnings)
        config = result.config

    emitted = emit(config, workbook=grid.workbook, generated_at=now)
    if not emitted.ok:
        reasons = "; ".join(r.message for r in emitted.refusals)
        return Generated("REFUSED", reason=reasons, config=config, warnings=read.warnings)

    return Generated(
        "OK", config=config, script=emitted.script,
        readback=describe_config(config, grid.workbook),
        coverage=clause_coverage(instruction, config, grid.workbook) if instruction else [],
        preview=compute_preview(grid, config, now=now),
        warnings=read.warnings + formula_warnings(config, grid),
        limits=[*capabilities.ABSENT_BY_DESIGN.values(), *capabilities.formula_limits(config)],
    )


def _load_config(raw: dict[str, Any], grid: Grid) -> ConfigSpec | str:
    """A config.json from an earlier download. Its header fingerprints stay as they were: if the
    sheet's headers changed since, the preview says so instead of silently re-fingerprinting."""
    result = validate_config(raw)
    if not result.ok or result.config is None:
        first = result.errors[0] if result.errors else None
        return "this config.json is not valid" + (f": {first.pointer} {first.message}" if first else "")
    missing = [t for t in result.config.schema_hashes if grid.workbook.tab(t) is None]
    if missing:
        return f"this config.json governs tabs the upload does not have: {', '.join(missing)}"
    return result.config


def formula_warnings(config: ConfigSpec, grid: Grid) -> list[str]:
    """Governed tabs holding formulas that a rewriting rule could turn into values."""
    actions = {r.action for r in config.rules} & REWRITING_ACTIONS
    if not actions:
        return []
    out = []
    for tab in config.schema_hashes:
        meta = grid.tabs.get(tab)
        cells = [(r, c) for r, c in (meta.formula_cells if meta else ()) if r >= config.data_start_row]
        if cells:
            cols = sorted({c for _, c in cells})
            out.append(f"{tab}: {len(cells)} data cell(s) in column(s) {', '.join(col_letter(c) for c in cols)} "
                       f"hold formulas; " + " ".join(capabilities.FORMULA_LIMITS[a] for a in sorted(actions)))
    return out

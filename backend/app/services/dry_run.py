"""Dry-run = plan_run without apply. Summarizes what a config would do (API: POST /configs/{id}/dry-run)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.config import ConfigSpec
from app.services.conditions import EvalContext
from app.services.grid import Workbook
from app.services.runner import RunEvent, RunPlan, RunStatus, plan_run


class TabSummary(BaseModel):
    rows_moved: int = 0
    rows_formatted: int = 0
    rows_added: int = 0
    rows_removed: int = 0
    rows_cleared: int = 0
    cells_validated: int = 0


class RuleSummary(BaseModel):
    rule_id: str
    action: str
    tabs: list[str]
    rows_affected: int
    destructive: bool
    error: str | None = None
    blocked: str | None = None


class DriftSummary(BaseModel):
    tab: str
    expected: str
    actual: str | None


class DryRunReport(BaseModel):
    status: RunStatus
    per_tab: dict[str, TabSummary] = Field(default_factory=dict)
    rules: list[RuleSummary] = Field(default_factory=list)
    drift: list[DriftSummary] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def dry_run(
    config: ConfigSpec, workbook: Workbook, ctx: EvalContext, event: RunEvent | None = None
) -> DryRunReport:
    return summarize_plan(plan_run(config, workbook, event or RunEvent.manual(), ctx))


def summarize_plan(run: RunPlan) -> DryRunReport:
    report = DryRunReport(
        status=run.status,
        drift=[DriftSummary(tab=d.tab, expected=d.expected, actual=d.actual) for d in run.drift],
        warnings=list(run.warnings),
    )
    for plan in run.plans:
        report.warnings.extend(f"{plan.rule_id}: {w}" for w in plan.warnings)
        report.rules.append(
            RuleSummary(
                rule_id=plan.rule_id,
                action=plan.action,
                tabs=[c.tab for c in plan.changes],
                rows_affected=plan.rows_affected,
                destructive=plan.destructive,
                error=plan.error,
                blocked=plan.blocked,
            )
        )
        for change in plan.changes:
            s = report.per_tab.setdefault(change.tab, TabSummary())
            st = change.stats
            s.rows_moved += st.rows_moved
            s.rows_formatted += st.rows_formatted
            s.rows_added += st.rows_added
            s.rows_removed += st.rows_removed
            s.rows_cleared += st.rows_cleared
            s.cells_validated += st.cells_validated
    return report

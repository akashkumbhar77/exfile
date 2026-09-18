"""RQ job: onboarding from the Enroll page (SPEC-PATCH-003 B2).

Enqueued with an enrollment id only. The instruction is read from `enrollments` (CLAUDE.md B.6:
owner instruction text is treated like cell values), so it never transits Redis or the RQ logs.

Progress is published in the RQ job's meta for GET /jobs/{id}:
queued -> profiling -> compiling -> validating -> dry_running -> (compiling ...) -> proposed | failed.
Job meta holds ids and states only. The failure text (the agent's words, which can quote the
instruction) is written to enrollments.failure and read from there by the API.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.workers.context import WorkerContext, get_context

log = logging.getLogger("app.onboarding_job")
QUEUE = "onboarding"


def _plan_and_llm(ctx: WorkerContext) -> tuple[Any, Any]:
    from app.agent.llm import OpenAIClient
    from app.agent.onboarding import ModelPlan
    from app.core.settings import get_settings

    s = get_settings()
    plan = ctx.plan or ModelPlan(s.llm_primary_model, s.llm_escalation_model, s.llm_primary_attempts,
                                 s.llm_escalation_attempts, s.llm_max_turns_per_attempt, s.llm_use_skills)
    if ctx.llm_factory is not None:
        return plan, ctx.llm_factory()
    if not s.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return plan, OpenAIClient(s.openai_api_key)


def run_onboarding(enrollment_id: int) -> dict[str, Any]:
    from rq import get_current_job

    from app.agent.onboarding import onboard
    from app.models import Enrollment, Sheet
    from app.services.registry import safe_error

    ctx = get_context()
    job = get_current_job()

    def publish(state: str, **ids: Any) -> None:  # ids and states only: never text
        if job is not None:
            job.meta.update(state=state, updated_at=datetime.now(UTC).isoformat(), **ids)
            job.save_meta()  # type: ignore[no-untyped-call]

    def finish(state: str, **fields: Any) -> None:
        with ctx.factory() as s, s.begin():
            row = s.get(Enrollment, enrollment_id)
            assert row is not None
            row.state, row.finished_at = state, datetime.now(UTC)
            for k, v in fields.items():
                setattr(row, k, v)

    with ctx.factory() as s, s.begin():
        row = s.get(Enrollment, enrollment_id)
        sheet = s.get(Sheet, row.sheet_id) if row is not None else None
        if row is None or sheet is None or row.org_id != ctx.org_id:
            publish("failed")
            log.error("onboarding_job.missing enrollment=%d", enrollment_id)
            return {"state": "failed"}
        row.state = "running"
        sheet_ref, instruction = sheet.google_sheet_id, row.instruction

    try:
        plan, llm = _plan_and_llm(ctx)
        result = onboard(ctx.factory, ctx.adapter, llm, plan, ctx.org_id, sheet_ref, instruction,
                         progress=publish)
    except Exception as exc:  # the job itself failed (not a model failure): readable, value-free text
        finish("failed", failure=f"onboarding could not run: {safe_error(exc)}")
        publish("failed")
        log.error("onboarding_job.error enrollment=%d sheet=%s %s", enrollment_id, sheet_ref, safe_error(exc))
        return {"state": "failed"}
    if result.status == "PENDING_APPROVAL":
        finish("proposed", config_id=result.config_id, session_id=result.session_id)
        publish("proposed", config_id=result.config_id, config_version=result.config_version,
                session_id=result.session_id, model=result.model)
        return {"state": "proposed", "config_id": result.config_id}
    finish("failed", failure=result.reason, failures=result.failures, session_id=result.session_id)
    publish("failed", session_id=result.session_id)
    return {"state": "failed"}

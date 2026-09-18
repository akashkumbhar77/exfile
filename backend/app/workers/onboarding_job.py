"""RQ job: onboarding from the Enroll page (SPEC-PATCH-003 B2).

Progress is published in the RQ job's meta (Redis) for GET /jobs/{id}:
queued -> profiling -> compiling -> validating -> dry_running -> (compiling ...) -> proposed | failed.
Job meta holds ids, states and the human-readable failure text; never cell values.
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


def run_onboarding(sheet_ref: str, instruction: str) -> dict[str, Any]:
    from rq import get_current_job

    from app.agent.onboarding import onboard
    from app.services.registry import safe_error

    ctx = get_context()
    job = get_current_job()

    def publish(state: str, **extra: Any) -> None:
        if job is not None:
            job.meta.update(state=state, updated_at=datetime.now(UTC).isoformat(), **extra)
            job.save_meta()  # type: ignore[no-untyped-call]

    try:
        plan, llm = _plan_and_llm(ctx)
        result = onboard(ctx.factory, ctx.adapter, llm, plan, ctx.org_id, sheet_ref, instruction,
                         progress=publish)
    except Exception as exc:  # the job itself failed (not a model failure): readable, value-free text
        publish("failed", failure=f"onboarding could not run: {safe_error(exc)}")
        log.error("onboarding_job.error sheet=%s %s", sheet_ref, safe_error(exc))
        return {"state": "failed"}
    if result.status == "PENDING_APPROVAL":
        publish("proposed", config_id=result.config_id, config_version=result.config_version,
                session_id=result.session_id, model=result.model)
        return {"state": "proposed", "config_id": result.config_id}
    publish("failed", failure=result.reason, failures=result.failures, session_id=result.session_id)
    return {"state": "failed"}

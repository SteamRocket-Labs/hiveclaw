"""Self-evolution background daemon — heartbeat + workspace sync.

Decoupled from `trigger_daemon` so a slow heartbeat tick or workspace
volume I/O can't introduce schedule jitter into the trigger tick. Each
loop runs as an independent asyncio task; failures in one do not block
the others. Dream admission is persisted at session boundaries and this
loop reconciles legacy due-state files that have no RuntimeTask owner.

Lifespan wiring lives in `app/main.py` — the daemon is spawned alongside
`start_trigger_daemon` so both come up at server boot.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from loguru import logger
from sqlalchemy import select

# Heartbeat tick cadence — read from typed settings so production (60s)
# and dev (lower) configs are explicit. P1-W2-5: configurable via the
# HEARTBEAT_TICK_SECONDS env var bound to `Settings`.
from app.config import get_settings

_HEARTBEAT_INTERVAL_SECONDS = get_settings().HEARTBEAT_TICK_SECONDS


async def record_and_enqueue_heartbeat_dream(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    outcome_type: str | None,
):
    """Persist the cadence input before peripheral heartbeat work detaches."""

    from app.services.auto_dream import record_dream_activity
    from app.services.dream_runtime import enqueue_due_dream

    record_dream_activity(agent_id, outcome_type or "")
    if tenant_id is None:
        return None
    return await enqueue_due_dream(agent_id=agent_id, tenant_id=tenant_id, source="heartbeat")


async def _maybe_run_skill_distillation(
    *,
    agent_id: uuid.UUID,
    workspace: Path,
    tenant_id: uuid.UUID | None,
    runtime_config,
    model,
    current_session_id: str | None,
) -> dict | None:
    if not getattr(runtime_config, "skill_candidate_loop_enabled", False):
        return None

    from app.services.skill_distiller import run_skill_distillation_cycle

    try:
        return await run_skill_distillation_cycle(
            agent_id=agent_id,
            workspace=workspace,
            tenant_id=tenant_id,
            runtime_config=runtime_config,
            model=model,
            current_session_id=current_session_id,
        )
    except Exception as exc:
        logger.warning("[EvolutionDaemon] Skill distillation failed for {}: {}", agent_id, exc)
        return None


def _maybe_run_skill_curator(workspace: Path) -> dict | None:
    """Decay/archive unused agent-authored skills without deleting them."""
    try:
        from app.services.skill_curator import run_skill_curator_pass

        return run_skill_curator_pass(workspace)
    except Exception as exc:
        logger.warning("[EvolutionDaemon] Skill curator pass failed for {}: {}", workspace, exc)
        return None


async def _resolve_agent_model(agent_id: uuid.UUID, tenant_id: uuid.UUID | None):
    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.services.model_resolution import choose_runtime_model_pair, resolve_default_model_for_tenant

    async with tenant_scoped_session(tenant_id) as db:
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            return None

        model = None
        if agent.primary_model_id:
            model_result = await db.execute(
                select(LLMModel).where(LLMModel.id == agent.primary_model_id, LLMModel.tenant_id == agent.tenant_id)
            )
            model = model_result.scalar_one_or_none()

        fallback_model = None
        if agent.fallback_model_id:
            fallback_result = await db.execute(
                select(LLMModel).where(
                    LLMModel.id == agent.fallback_model_id,
                    LLMModel.tenant_id == agent.tenant_id,
                )
            )
            fallback_model = fallback_result.scalar_one_or_none()

        if not model and not fallback_model:
            return None

        if model and agent.tenant_id:
            default_runtime_model = await resolve_default_model_for_tenant(
                db,
                agent.tenant_id,
                exclude_model_id=model.id,
            )
            model, _fallback = choose_runtime_model_pair(model, fallback_model, default_runtime_model)
            return model

        if fallback_model:
            default_runtime_model = None
            if agent.tenant_id:
                default_runtime_model = await resolve_default_model_for_tenant(
                    db,
                    agent.tenant_id,
                    exclude_model_id=fallback_model.id,
                )
            model, _fallback = choose_runtime_model_pair(None, fallback_model, default_runtime_model)
            return model

        return model


async def run_heartbeat_evolution_maintenance(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    outcome_type: str | None,
    current_session_id: str | None,
) -> dict:
    """Run post-heartbeat evolution maintenance outside the heartbeat service.

    Heartbeat owns autonomous reflection and T0 event emission. This helper owns
    peripheral candidate/curation/repair lanes that can be scheduled after a
    heartbeat boundary without becoming heartbeat logic.
    """
    report: dict[str, object] = {
        "skill_distillation": None,
        "skill_curator": None,
        "dream_triggered": False,
        "t3_normalization": None,
        "enhancement_sync": None,
    }

    try:
        from app.runtime.invoker import _resolve_runtime_config
        from app.tools.workspace import ensure_workspace

        runtime_config = await _resolve_runtime_config(agent_id)
        if runtime_config.tenant_resolution_error:
            logger.warning(
                "[EvolutionDaemon] Skipping skill maintenance for {} — tenant resolution failed: {}",
                agent_id,
                runtime_config.tenant_resolution_error,
            )
        else:
            workspace = await ensure_workspace(agent_id, tenant_id=str(tenant_id) if tenant_id else None)
            model = await _resolve_agent_model(agent_id, tenant_id)
            if model is not None:
                report["skill_distillation"] = await _maybe_run_skill_distillation(
                    agent_id=agent_id,
                    workspace=workspace,
                    tenant_id=tenant_id,
                    runtime_config=runtime_config,
                    model=model,
                    current_session_id=current_session_id,
                )
            report["skill_curator"] = _maybe_run_skill_curator(workspace)
    except Exception as exc:
        logger.warning("[EvolutionDaemon] Skill maintenance setup failed for {}: {}", agent_id, exc)

    # Legacy flat-T3 shape normalization retired at the C7 cutover; two-plane
    # shape discipline is enforced by the Platform Gate operations themselves.

    try:
        from app.memory.enhancement import sync_t3_to_memory_enhancement

        sync_result = await sync_t3_to_memory_enhancement(agent_id, tenant_id)
        report["enhancement_sync"] = {
            "synced": sync_result.synced,
            "skipped": sync_result.skipped,
            "reason": sync_result.reason,
        }
        if sync_result.synced:
            logger.info(
                "[EvolutionDaemon] Memory enhancement sync: {} T3 items (agent={})", sync_result.synced, agent_id
            )
    except Exception as exc:
        logger.warning("[EvolutionDaemon] Memory enhancement sync outer guard tripped for {}: {}", agent_id, exc)

    return report


async def _heartbeat_loop() -> None:
    """Run heartbeat ticks at the configured cadence.

    Each tick is wrapped in its own try/except so transient errors never
    take the loop down. Pending-reply cleanup is piggybacked on the same
    cadence (it has no independent timer).
    """
    from app.database import async_session, enter_rls_bypass
    from app.services.daemon_liveness import mark_daemon_error, mark_daemon_tick
    from app.services.heartbeat import _heartbeat_tick
    from app.services.pending_reply_service import cleanup_expired_replies

    while True:
        heartbeat_ok = True
        try:
            await _heartbeat_tick()
        except Exception as e:
            heartbeat_ok = False
            mark_daemon_error("evolution_daemon", e)
            logger.error(f"[EvolutionDaemon] heartbeat tick error: {e}")

        try:
            async with (
                async_session() as db,
                enter_rls_bypass(db, reason="pending-reply expiry sweep — expire stale contexts across all tenants"),
            ):
                await cleanup_expired_replies(db)
                await db.commit()
        except Exception as e:
            logger.debug(f"[EvolutionDaemon] PendingReply cleanup error (non-fatal): {e}")

        try:
            await _drain_personal_kb_jobs()
        except Exception as e:
            logger.debug(f"[EvolutionDaemon] Personal KB import drain error (non-fatal): {e}")

        try:
            from app.services.dream_runtime import reconcile_due_dream_runtime_tasks

            await reconcile_due_dream_runtime_tasks(limit=200)
        except Exception as e:
            logger.debug(f"[EvolutionDaemon] Dream due-state reconciliation error (non-fatal): {e}")

        if heartbeat_ok:
            mark_daemon_tick("evolution_daemon")
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)


async def _drain_personal_kb_jobs() -> None:
    from app.database import async_session, enter_rls_bypass
    from app.services.personal_knowledge_service import PersonalKnowledgeService

    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="personal-kb import job drain — recover stale queued/running person-scope jobs"),
    ):
        summary = await PersonalKnowledgeService().claim_and_process_stuck_jobs(
            db,
            limit=10,
            queued_grace_seconds=30,
            running_timeout_seconds=600,
            max_attempts=5,
        )
        if summary.attempted:
            logger.info("[EvolutionDaemon] Personal KB import drain: {}", summary)
        await db.commit()


async def start_evolution_daemon() -> None:
    """Boot heartbeat + workspace sync loops as independent background tasks.

    Returns once the heartbeat loop is running — the workspace-sync loops
    are detached and run for the lifetime of the process. Errors during
    workspace-sync setup are logged but do not stop heartbeat from
    starting (workspace sync is best-effort persistence; heartbeat is the
    primary self-evolution path).
    """
    from app.services.daemon_liveness import mark_daemon_started

    mark_daemon_started("evolution_daemon")
    logger.info(
        "🌱 Evolution Daemon started (heartbeat every {}s, workspace sync continuous)",
        _HEARTBEAT_INTERVAL_SECONDS,
    )

    try:
        from app.services.heartbeat import _workspace_full_sweep_loop, _workspace_sync_loop
        from app.services.workspace_sync_dirty import start_redis_listener

        await start_redis_listener()
        asyncio.create_task(_workspace_sync_loop(), name="workspace_sync_loop")
        asyncio.create_task(_workspace_full_sweep_loop(), name="workspace_full_sweep_loop")
    except Exception as e:
        logger.error(f"[EvolutionDaemon] Failed to spawn workspace sync loops: {e}")

    await _heartbeat_loop()

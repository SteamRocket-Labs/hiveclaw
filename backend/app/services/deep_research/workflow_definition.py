from __future__ import annotations

import logging
import uuid
import zlib
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from app.database import tenant_scoped_session
from app.models.workflow import WorkflowDefinitionRecord
from app.runtime.workflow_definition import compute_definition_hash
from app.services.deep_research.plan_mode import deep_research_plan_signature, normalize_deep_research_output_format
from app.services.deep_research.schemas import ResearchRequest, to_jsonable
from app.services.workflow_definitions import WorkflowDefinitionService
from app.services.workflow_launch import resolve_agent_runtime, start_ephemeral_workflow_for_agent

DEEP_RESEARCH_WORKFLOW_NAME = "deep_research.v1"
DEEP_RESEARCH_WORKFLOW_LEAVES = {
    "deep_research_planner",
    "deep_research_explorer",
    "deep_research_synthesizer",
    "deep_research_critic",
}


def build_deep_research_workflow_definition() -> dict[str, Any]:
    """Registered Deep Research v1 definition.

    It is intentionally expressed in the generic workflow schema: plan,
    bounded fanout, synthesis, critic. The old Deep Research orchestrator
    remains available behind the rollout flag fallback.
    """

    return {
        "name": DEEP_RESEARCH_WORKFLOW_NAME,
        "description": "Deep Research workflow: plan -> bounded exploration fanout -> synthesize -> critic.",
        "args_schema": {
            "question": {"type": "string", "required": True},
            "worker_topics": {"type": "array", "required": True},
            "scope": {"type": "string", "required": False},
            "depth": {"type": "string", "required": False},
            "source_policy": {"type": "string", "required": False},
            "time_window": {"type": "string", "required": False},
            "output_format": {"type": "string", "required": False},
            "output_language": {"type": "string", "required": False},
            "deep_research_signature": {"type": "string", "required": False},
        },
        "default_budget": {"max_total_tokens": 500_000, "max_wall_clock_seconds": 7_200},
        "steps": [
            {
                "id": "plan",
                "type": "agent_step",
                "leaf": {"name": "deep_research_planner", "type": "worker", "max_tool_rounds": 8},
                "task": (
                    "Plan source-ledger-backed research for {{args.question}}. Respect scope {{args.scope}}, "
                    "time window {{args.time_window}}, source policy {{args.source_policy}}, and output language "
                    "{{args.output_language}}."
                ),
            },
            {
                "id": "explore",
                "type": "fanout_step",
                "leaf": {"name": "deep_research_explorer", "type": "explorer", "max_tool_rounds": 16},
                "items_from": "args.worker_topics",
                "per_item_task": (
                    "Explore worker topic {{item}} for question {{args.question}}. Use the plan context "
                    "{{steps.plan.output}} and return source-bound findings with gaps. "
                    "Write the digest in {{args.output_language}}; keep proper names and identifiers "
                    "in their original form."
                ),
                "max_concurrency": 4,
            },
            # I2 保真（P-Q2）：对抗评审发生在合成之前，合成必须回应批评。
            # critic/synthesize 的 pre_process 从分片总线重建真实输入；这里的
            # task 模板表达步间依赖（compiler 跨步引用校验用），运行时被改写。
            {
                "id": "critic",
                "type": "agent_step",
                "leaf": {"name": "deep_research_critic", "type": "critic", "max_tool_rounds": 8},
                "task": (
                    "Stress-test the exploration evidence {{steps.explore.output}} for {{args.question}} "
                    "before synthesis: cherry-picking, missing warrants, overclaims, alternatives."
                ),
            },
            {
                "id": "synthesize",
                "type": "agent_step",
                "leaf": {"name": "deep_research_synthesizer", "type": "worker", "max_tool_rounds": 12},
                "retry": {"max_attempts": 2},
                "task": (
                    "Synthesize a final report for {{args.question}} from exploration outputs "
                    "{{steps.explore.output}} addressing the adversarial review {{steps.critic.output}}. "
                    "Preserve unsupported claims as explicit gaps."
                ),
            },
        ],
    }


def deep_research_workflow_args(request: ResearchRequest) -> dict[str, Any]:
    worker_topics = [topic for topic in request.worker_topics if str(topic).strip()]
    if not worker_topics:
        worker_topics = [request.question]
    return {
        "question": request.question,
        "worker_topics": worker_topics,
        # Optional fields render into step task templates — "not specified"
        # reads correctly there, a literal "None" does not.
        "scope": request.scope or "not specified",
        "depth": request.depth or "standard",
        "source_policy": request.source_policy or "not specified",
        "time_window": request.time_window or "not specified",
        "output_format": normalize_deep_research_output_format(request.output_format),
        "output_language": request.output_language or "not specified",
        "deep_research_signature": deep_research_plan_signature(request, worker_topics=worker_topics),
    }


def _ensure_lock_key(tenant_id: uuid.UUID) -> int:
    """Per-tenant advisory-lock key for the ensure path (crc32-namespaced,
    same convention as ``PGRunLeaseManager``)."""
    return zlib.crc32(f"dr_ensure:{tenant_id}".encode()) & 0x7FFFFFFF


async def _find_active_matching_record(
    *, tenant_id: uuid.UUID, definition_hash: str, session_factory=None
) -> WorkflowDefinitionRecord | None:
    async with tenant_scoped_session(str(tenant_id), session_factory=session_factory) as session:
        existing = (
            (
                await session.execute(
                    select(WorkflowDefinitionRecord)
                    .where(
                        WorkflowDefinitionRecord.tenant_id == tenant_id,
                        WorkflowDefinitionRecord.name == DEEP_RESEARCH_WORKFLOW_NAME,
                        WorkflowDefinitionRecord.status == "active",
                    )
                    .order_by(WorkflowDefinitionRecord.definition_version.desc())
                )
            )
            .scalars()
            .all()
        )
    for record in existing:
        if record.definition_hash == definition_hash:
            return record
    return None


async def ensure_deep_research_workflow_definition(
    *,
    tenant_id: uuid.UUID,
    created_by_user_id: uuid.UUID | None = None,
    session_factory=None,
) -> WorkflowDefinitionRecord:
    definition = build_deep_research_workflow_definition()
    definition_hash = compute_definition_hash(definition)

    found = await _find_active_matching_record(
        tenant_id=tenant_id, definition_hash=definition_hash, session_factory=session_factory
    )
    if found is not None:
        return found

    # Concurrent ensures race create_draft: same-name auto-next-version never
    # hits the unique constraint, so two racers would register v1 AND v2 both
    # active. Serialise the check-then-create through a session-level advisory
    # lock held on a dedicated connection (released in ``finally``; connection
    # death also releases it, so a crashed worker cannot wedge the ensure).
    lock_key = _ensure_lock_key(tenant_id)
    async with tenant_scoped_session(str(tenant_id), session_factory=session_factory) as lock_session:
        await lock_session.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
        try:
            found = await _find_active_matching_record(
                tenant_id=tenant_id, definition_hash=definition_hash, session_factory=session_factory
            )
            if found is not None:
                return found

            service = WorkflowDefinitionService(session_factory=session_factory)
            draft = await service.create_draft(
                tenant_id=tenant_id,
                definition_data=definition,
                created_by_user_id=created_by_user_id,
                visibility_scope="tenant",
                owner_type="tenant",
                owner_id=tenant_id,
            )
            return await service.activate(
                draft.id,
                tenant_id=tenant_id,
                actor_user_id=created_by_user_id or uuid.UUID(int=0),
                allowed_leaves=DEEP_RESEARCH_WORKFLOW_LEAVES,
            )
        finally:
            await lock_session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})


async def start_deep_research_workflow_run(
    *,
    request: ResearchRequest,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    workspace: Path,
    plan_id: uuid.UUID | str | None = None,
    session_factory=None,
    spawn=None,
    run_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    import json

    from app.services.deep_research.leaf_presets import (
        register_deep_research_leaf_presets,
        run_artifact_dir,
    )

    register_deep_research_leaf_presets()

    agent, _model = await resolve_agent_runtime(agent_id, session_factory=session_factory)
    if agent.tenant_id is None:
        raise LookupError(f"agent {agent_id} has no tenant — refusing Deep Research workflow run")

    record = await ensure_deep_research_workflow_definition(
        tenant_id=agent.tenant_id,
        created_by_user_id=user_id,
        session_factory=session_factory,
    )

    # Pre-generate the run id so request.json — the domain context every
    # non-explorer leaf rebuilds from — is on disk BEFORE the first leaf runs.
    # The handler passes its own id so the background-start tool can hand the
    # caller a checkable task id immediately.
    run_id = run_id or uuid.uuid4()
    artifact_root = run_artifact_dir(agent_id, run_id)
    artifact_root.mkdir(parents=True, exist_ok=True)
    (artifact_root / "request.json").write_text(
        json.dumps(to_jsonable(request), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    launch_kwargs: dict[str, Any] = {
        "agent_id": agent_id,
        "user_id": user_id,
        "confirmed_plan_id": plan_id,
        "definition": record.definition_json,
        "args": deep_research_workflow_args(request),
        "definition_source": f"registered:{record.name}:v{record.definition_version}:{record.definition_hash}",
        "session_factory": session_factory,
        "run_id": run_id,
    }
    if spawn is not None:
        launch_kwargs["spawn"] = spawn
    handle = await start_ephemeral_workflow_for_agent(**launch_kwargs)

    output_format = normalize_deep_research_output_format(request.output_format)
    report_path = artifact_root / "report.md"
    output_path: Path | None = None
    if handle.outcome.status == "completed" and report_path.exists():
        # Product-surface parity (I4): requested-format export + workspace
        # packet mirror, exactly where the legacy path landed them. Lazy import
        # breaks the handler→workflow_definition→handler cycle.
        from app.tools.handlers.deep_research import (
            _materialize_requested_output_format,
            _publish_workspace_packet,
        )

        try:
            output_path = _materialize_requested_output_format(workspace, artifact_root, output_format)
        except Exception:  # export is best-effort; report.md is the deliverable
            logging.getLogger(__name__).warning("[DR-workflow] output-format export failed", exc_info=True)
        _publish_workspace_packet(workspace, run_id, artifact_root)

    return {
        "created_workflow_run_id": str(handle.run_id),
        "workflow_run_id": str(handle.run_id),
        "status": handle.outcome.status,
        "reason": handle.outcome.reason,
        "definition_name": record.name,
        "definition_version": record.definition_version,
        "definition_hash": record.definition_hash,
        "legacy_path_available": True,
        "workspace_artifact_dir": str(artifact_root),
        "report_path": str(report_path) if report_path.exists() else None,
        "output_path": str(output_path) if output_path else None,
        "output_format": output_format,
    }

"""Source-scoped observations for the native Skill awareness context."""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import uuid

from app.database import tenant_scoped_session
from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.services.content_input_receipts import ContentInputReceiptError, _runtime_input_actor, _validate_input_dependencies
from app.services.session_tool_runtime import _sha256


async def render_runtime_skill_digest(request, *, session_id):
    from app.config import get_settings
    from app.core.execution_context import get_execution_identity
    from app.services.evolution_view import render_skill_evolution_digest
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.session_workspace_snapshot import async_agent_workspace_lock
    from app.services.skill_source_receipts import SkillInputCapture, load_skill_input_snapshot
    from app.services.workspace_resource_authority import load_workspace_authority_scope, open_workspace_file

    metadata = getattr(request.session_context, "metadata", None) or {}
    try:
        run_id = uuid.UUID(str(metadata.get("runtime_task_id") or metadata.get("task_id")))
        session_id = uuid.UUID(str(session_id))
    except (ValueError, TypeError) as exc:
        raise ContentInputReceiptError("skill_observation_work_unavailable", "Restore the actual Skill observation reader.", 503) from exc
    identity = request.execution_identity or get_execution_identity()
    autonomous = getattr(identity, "identity_type", None) == "agent_bot"
    delegation = getattr(request.delegation_token, "delegation_id", None)
    principal = AgentRuntimePrincipal(agent_id=request.agent_id, requester_user_id=request.user_id,
        session_id=str(session_id), runtime_task_id=str(run_id), autonomous=autonomous,
        purpose="autonomous_agent" if autonomous else "a2a_delegation" if delegation else "interactive_session",
        delegation_id=str(delegation) if delegation else None)
    async with tenant_scoped_session(request.tenant_id) as db:
        _principal, user = await _runtime_input_actor(db, request.tenant_id, principal)
        task = await db.get(RuntimeTask, run_id, populate_existing=True)
        if (task is None or task.tenant_id != request.tenant_id or task.root_user_id != user.id
            or request.agent_id not in {task.parent_agent_id, task.child_agent_id}
            or str(session_id) not in {task.parent_session_id, task.child_session_id}):
            raise ContentInputReceiptError("skill_observation_work_unavailable", "The Skill observation has no matching work.", 403)
        scope = await load_workspace_authority_scope(db, user, agent_id=request.agent_id,
            session_id=session_id, runtime_principal=principal)
    root = Path(get_settings().AGENT_DATA_DIR) / str(request.agent_id)
    capture = SkillInputCapture(request.agent_id, allow_empty=True)
    async with async_agent_workspace_lock(request.agent_id, data_root=root.parent):
        await load_skill_input_snapshot(capture, request.tenant_id, runtime_request=principal)
        def allowed(path):
            try:
                relative = path.relative_to(root).as_posix()
                return scope.can_read(relative) and relative in capture.files
            except ValueError:
                return False

        def observed(path):
            relative = path.relative_to(root).as_posix()
            with open_workspace_file(root, relative) as file:
                capture.observe_file(relative, hashlib.file_digest(file, "sha256").hexdigest())

        content = render_skill_evolution_digest(root, source_authorizer=allowed, source_observer=observed)
    if not content:
        return "", []
    captured = capture.snapshot()
    if captured["unbound"]:
        raise ContentInputReceiptError("skill_observation_inputs_unavailable", "Reconcile the exact observed Skill versions.", 503)
    proof = {"schema": "hive.skill_evolution_observation.v1", "tenant_id": str(request.tenant_id),
        "agent_id": str(request.agent_id), "session_id": str(session_id), "run_id": str(run_id),
        "requester_user_id": str(request.user_id), "content": content, "sources": captured["sources"]}
    digest = _sha256(proof)
    identity = uuid.uuid5(run_id, f"skill-evolution-observation:{digest}")
    async with tenant_scoped_session(request.tenant_id) as db:
        await _runtime_input_actor(db, request.tenant_id, principal)
        await _validate_input_dependencies(db, request.tenant_id, captured["sources"], runtime_request=principal)
        row = await db.get(InvocationSpan, identity, populate_existing=True)
        if row is None:
            db.add(InvocationSpan(id=identity, tenant_id=request.tenant_id, agent_id=request.agent_id, user_id=request.user_id,
                session_id=str(session_id), runtime_task_id=run_id, trace_id=f"skill-evolution:{run_id}",
                span_id=f"skill-evolution:{identity}", span_type="skill_evolution_observation", name="skill.evolution.observation",
                status="captured", duration_ms=0, started_at=datetime.now(timezone.utc), metadata_json={"observation": proof}))
            await db.commit()
        elif row.metadata_json != {"observation": proof}:
            raise ContentInputReceiptError("skill_observation_conflict", "Reconcile the original Skill observation.", 409)
    return content, [{"source_kind": "skill_evolution_observation", "source_id": str(identity), "source_version": digest}]


async def validate_observation(db, tenant_id, dependency, *, request=None, user=None):
    from app.services.content_input_receipts import _authorize_session_input_source

    try:
        row = await db.get(InvocationSpan, uuid.UUID(dependency["source_id"]), populate_existing=True)
        proof = (row.metadata_json or {})["observation"]
        task = await db.get(RuntimeTask, uuid.UUID(proof["run_id"]), populate_existing=True)
        session = await db.get(ChatSession, uuid.UUID(proof["session_id"]), populate_existing=True)
        if (row.tenant_id != tenant_id or row.span_type != "skill_evolution_observation" or row.status != "captured"
            or row.name != "skill.evolution.observation" or proof.get("schema") != "hive.skill_evolution_observation.v1"
            or proof["tenant_id"] != str(tenant_id) or proof["agent_id"] != str(row.agent_id)
            or proof["requester_user_id"] != str(row.user_id) or proof["run_id"] != str(row.runtime_task_id)
            or proof["session_id"] != row.session_id or _sha256(proof) != dependency["source_version"]
            or row.id != uuid.uuid5(row.runtime_task_id, f"skill-evolution-observation:{_sha256(proof)}")
            or task is None or task.tenant_id != tenant_id or task.root_user_id != row.user_id
            or row.agent_id not in {task.parent_agent_id, task.child_agent_id}
            or row.session_id not in {task.parent_session_id, task.child_session_id}
            or session is None or session.tenant_id != tenant_id or session.agent_id != row.agent_id):
            raise ValueError("skill_observation_invalid")
        await _authorize_session_input_source(db, tenant_id, session, user=user, runtime_request=request)
        await _validate_input_dependencies(db, tenant_id, proof["sources"], user=user, runtime_request=request)
    except ContentInputReceiptError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ContentInputReceiptError("skill_observation_unavailable", "The exact Skill observation and its sources need reconciliation.", 403) from exc

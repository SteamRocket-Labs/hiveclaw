"""Bind learned Skill candidates and reviewed files to their actual inputs."""

from copy import deepcopy
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
import uuid

from sqlalchemy import select, text

from app.database import tenant_scoped_session
from app.models.config_revision import ConfigRevision
from app.models.chat_session import ChatSession
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.models.runtime_task import RuntimeTask
from app.services.config_versioning import canonical_content_hash, save_revision
from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
from app.services.explicit_memory_authority import (
    AUTHORITY_SCHEMA, _writer_input, audience_for_session, authorize_memory_audience, memory_effect_actor,
)
from app.services.session_tool_runtime import _sha256

CANDIDATE_SCHEMA = "hive.learned_skill_candidate.v1"
CANDIDATE_ENTITY = "learned_skill_candidate"
_REVIEW = ContextVar("learned_skill_review", default=None)


@dataclass
class SkillReviewInputs:
    agent_id: uuid.UUID
    tenant_id: uuid.UUID
    reader: object | None = None
    candidates: dict = field(default_factory=dict)
    scope: object | None = None
    unavailable: bool = False
    dependencies: list = field(default_factory=list)
    skill_capture: object | None = None
    source_scope: tuple = ()


def model_source(result):
    return {"source_kind": "session_model_result", "source_id": str(result.id),
        "source_version": _sha256({"model_request_hash": result.model_request_hash, "seal": result.seal_json})}


async def validate_model_source(db, tenant_id, dependency, *, request=None, user=None):
    from app.services.content_input_receipts import _authorize_session_input_source
    from app.services.session_model_inputs import _model_dependencies

    try:
        result = await db.get(SessionModelResult, uuid.UUID(dependency["source_id"]), populate_existing=True)
        session = await db.get(ChatSession, result.session_id, populate_existing=True) if result else None
        sources, complete = _model_dependencies(result)
        seal = result.seal_json if result is not None else None
        if (result is None or result.tenant_id != tenant_id or session is None or session.tenant_id != tenant_id
            or not complete or not isinstance(seal, dict) or seal.get("result_id") != str(result.id)
            or seal.get("run_id") != str(result.run_id) or seal.get("provider_request_id") != result.provider_request_id
            or seal.get("content_hash") != _sha256(seal.get("semantic_content")) or model_source(result) != dependency):
            raise ValueError("learned_model_source_invalid")
        await _authorize_session_input_source(db, tenant_id, session, user=user, runtime_request=request)
        await _validate_input_dependencies(db, tenant_id, sources, user=user, runtime_request=request)
        return result, session
    except ContentInputReceiptError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ContentInputReceiptError("learned_model_source_unavailable", "Restore the exact sealed workflow and its inputs.", 403) from exc


async def recheck_review_inputs():
    frame = current_review()
    if frame is None:
        return
    if frame.reader is None:
        raise ContentInputReceiptError("skill_review_sources_unavailable", "Restore the original authorized work.", 503)
    dependencies = [*frame.dependencies]
    if frame.skill_capture is not None:
        snapshot = frame.skill_capture.snapshot()
        if snapshot["unbound"]:
            raise ContentInputReceiptError("skill_review_inputs_unverified", "Reconcile the reviewed Skill inputs.", 503)
        dependencies.extend(snapshot["sources"])
    async with tenant_scoped_session(frame.tenant_id) as db:
        await _validate_input_dependencies(db, frame.tenant_id, dependencies, runtime_request=frame.reader)
    return dependencies


async def recheck_review_effect(*, workspace, target_path, rendered_markdown, source_candidate_id):
    import hashlib

    frame = current_review()
    if frame is None:
        return
    from app.services.approval_ticket import build_live_approval_policy_snapshot
    from app.tools.guard_policy import evaluate_guard_policy

    await recheck_review_inputs()
    _row, c = await review_candidate(source_candidate_id, workspace=workspace)
    if target_path != f"skills/{c['folder_name']}/SKILL.md":
        raise ContentInputReceiptError("learned_skill_target_denied", "Reconcile the exact reviewed target.", 403)
    if hashlib.sha256((rendered_markdown.rstrip() + "\n").encode()).hexdigest() != c["draft_sha256"]:
        raise ContentInputReceiptError("learned_skill_draft_changed", "Reconcile the exact reviewed Skill bytes before activation.", 403)
    async with tenant_scoped_session(frame.tenant_id) as db:
        from app.models.agent import Agent

        agent = await db.get(Agent, frame.agent_id, populate_existing=True)
        if agent is None or agent.tenant_id != frame.tenant_id or agent.deleted_at is not None or agent.deactivated_at is not None:
            raise ContentInputReceiptError("learned_skill_employee_inactive", "Restore current employee authority before activation.", 403)
        live = await build_live_approval_policy_snapshot(db=db, tenant_id=frame.tenant_id,
            agent_id=frame.agent_id, tool_name="save_skill")
        verdict = evaluate_guard_policy(tool_name="save_skill", arguments={"name": c["requested_folder"],
            "folder_name": c["folder_name"], "instructions": rendered_markdown, "overwrite": False},
            external_visible=False, snapshot=live["guard_policy"])
        if live["capability"]["denied"] or verdict.decision == "deny":
            raise ContentInputReceiptError("learned_skill_activation_denied", "Current policy denies Skill activation.", 403)
        if (live["capability"]["policy_found"] and live["capability"]["escalate_to_l3"]) or verdict.decision == "require_approval":
            raise ContentInputReceiptError("learned_skill_activation_approval_required",
                "Approve the exact reviewed Skill before activation; the submission approval is not reusable.", 409)


def observe_review_skill(skill):
    frame = current_review()
    if frame is not None and frame.skill_capture is not None:
        frame.skill_capture.observe_file(skill.relative_path, skill.source_sha256)


async def load_review_session_evidence(*, agent_id, state):
    """Use canonical sealed work, never a ChatMessage projection as source authority."""
    from app.services.skill_distiller import SessionWorkflowEvidence, _normalize_session_status
    from app.models.chat_transcript_event import ChatTranscriptEvent

    frame = current_review()
    if frame is None or frame.reader is None:
        return []
    evidence = []
    async with tenant_scoped_session(frame.tenant_id) as db:
        rows = await db.execute(select(SessionModelResult, ChatSession, ChatTranscriptEvent.created_at).join(ChatSession,
            ChatSession.id == SessionModelResult.session_id).join(ChatTranscriptEvent,
                ChatTranscriptEvent.id == SessionModelResult.round_committed_event_id).where(SessionModelResult.tenant_id == frame.tenant_id,
            ChatSession.tenant_id == frame.tenant_id, ChatSession.agent_id == agent_id,
            ChatTranscriptEvent.tenant_id == frame.tenant_id, ChatTranscriptEvent.result_id == SessionModelResult.id,
            SessionModelResult.state == "round_committed").order_by(ChatTranscriptEvent.created_at, SessionModelResult.id))
        for result, session, occurred_at in rows:
            reply = (result.seal_json or {}).get("semantic_content")
            if not isinstance(reply, str) or not reply.strip():
                continue
            source = model_source(result)
            try:
                await validate_model_source(db, frame.tenant_id, source, request=frame.reader)
            except ContentInputReceiptError as exc:
                import logging

                logging.getLogger(__name__).warning("Skill workflow source held: %s", exc.code)
                frame.unavailable = True
                continue
            from app.services.session_model_inputs import _model_dependencies

            dependencies, _complete = _model_dependencies(result)
            invocation_ids = [uuid.UUID(ref["source_id"]) for ref in dependencies if ref["source_kind"] == "session_tool_invocation"]
            calls = list(await db.scalars(select(SessionToolInvocation).where(
                SessionToolInvocation.tenant_id == frame.tenant_id, SessionToolInvocation.session_id == session.id,
                SessionToolInvocation.run_id == result.run_id, SessionToolInvocation.id.in_(invocation_ids))
                .order_by(SessionToolInvocation.round_id, SessionToolInvocation.id))) if invocation_ids else []
            if not calls:
                continue
            loaded = tuple(dict.fromkeys(str((call.effective_arguments_json or call.provider_arguments_json).get("name"))
                for call in calls if call.tool_name == "load_skill"))
            from app.services.heartbeat import _parse_heartbeat_outcome

            status = _normalize_session_status(reply)
            if status == "noop" and _parse_heartbeat_outcome(reply)[0] != "noop":
                status = "observed"
            evidence.append(SessionWorkflowEvidence(session_id=f"{session.id}:{result.id}", source=session.source_channel,
                occurred_at=occurred_at.isoformat(), status=status, used_skill=bool(loaded),
                summary=reply.strip(), assistant_reply=reply, tool_names=tuple(call.tool_name for call in calls),
                loaded_skill_names=loaded))
            frame.dependencies.append(source)
    return evidence


async def load_review_memory_candidates(*, data_root, agent_id, container):
    """Read every currently verifiable T3/explicit candidate in this source scope."""
    import hashlib
    from app.services.content_input_receipts import latest_workspace_receipt, validate_runtime_workspace_receipt
    from app.services.skill_distiller import _load_memory_container_candidates, _CANDIDATE_MARKERS, _PROMOTED_MARKER
    from app.memory.explicit_overlay import load_explicit_overlay_entries
    from app.services.explicit_memory_authority import latest_memory_receipt, _authorize_entry, source_dependency
    from app.services.workspace_resource_authority import open_workspace_file

    frame = current_review()
    if frame is None or frame.reader is None:
        return []
    candidates = []
    async with tenant_scoped_session(frame.tenant_id) as db:
        for item in _load_memory_container_candidates(data_root, agent_id, container=container):
            path = item["source"]
            try:
                if not frame.scope.can_read(path):
                    continue
                with open_workspace_file(Path(data_root) / str(agent_id), path) as file:
                    digest = hashlib.file_digest(file, "sha256").hexdigest()
                receipt = await latest_workspace_receipt(db, frame.tenant_id, agent_id, path)
                if receipt is not None:
                    await validate_runtime_workspace_receipt(db, frame.tenant_id, frame.reader, receipt, digest)
                    source = {"source_kind": "workspace_artifact", "source_id": str(receipt.id), "source_version": digest}
                else:
                    source = {"source_kind": "agent_resident_memory", "source_id": f"{agent_id}\n{path}", "source_version": digest}
                    await _validate_input_dependencies(db, frame.tenant_id, [source], runtime_request=frame.reader)
                candidates.append(item)
                frame.dependencies.append(source)
            except (ContentInputReceiptError, OSError, ValueError):
                frame.unavailable = True
        for entry in load_explicit_overlay_entries(data_root, agent_id):
            if entry.status != "active" or _CANDIDATE_MARKERS[container] not in entry.content or _PROMOTED_MARKER in entry.content:
                continue
            try:
                receipt = await latest_memory_receipt(db, frame.tenant_id, agent_id, entry.entry_id)
                await _authorize_entry(db, tenant_id=frame.tenant_id, agent_id=agent_id, request=frame.reader,
                    entry=entry, receipt=receipt)
                candidates.append({"entry_id": entry.entry_id, "content": entry.content, "timestamp": "",
                    "filename": f"memory/explicit/entries/{entry.entry_id}.md", "source": f"memory/explicit/entries/{entry.entry_id}.md"})
                frame.dependencies.append(source_dependency(receipt))
            except ContentInputReceiptError:
                frame.unavailable = True
    return candidates


def current_review():
    return _REVIEW.get()


def review_scope_id():
    frame = current_review()
    return uuid.uuid5(frame.agent_id, f"skill-review-scope:{canonical_content_hash(list(frame.source_scope))}") if frame is not None else None


def managed_learning_path(path):
    parts = Path(path).parts
    return ((len(parts) >= 2 and parts[0] == "skills" and parts[1].startswith("learned-"))
        or path == "evolution/skill_candidates" or path.startswith("evolution/skill_candidates/")
        or path == "evolution/source_scopes" or path.startswith("evolution/source_scopes/"))


def review_path_allowed(workspace, path):
    frame = current_review()
    if frame is None:
        return True
    try:
        relative = Path(path).resolve().relative_to(Path(workspace).resolve()).as_posix()
    except ValueError:
        return False
    return frame.scope is not None and frame.scope.can_read(relative)


def _candidate_entity(agent_id, candidate_id):
    return uuid.uuid5(agent_id, f"learned-skill-candidate:{candidate_id}")


def _family(agent_id, authority, slug):
    audience = ({"owner_user_id": authority["owner_user_id"]} if authority["audience_kind"] == "private" else
        {key: authority[key] for key in ("source_channel", "group_installation_ref", "group_subject_id")})
    return uuid.uuid5(agent_id, f"learned-skill-family:{canonical_content_hash(audience)}:{slug}")


def _revision_valid(row, *, tenant_id, entity_type):
    return (row is not None and row.tenant_id == tenant_id and row.entity_type == entity_type
        and isinstance(row.content, dict) and canonical_content_hash(row.content) == row.content_hash)


async def candidate_revision(db, *, tenant_id, agent_id, candidate_id):
    return await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
        ConfigRevision.entity_type == CANDIDATE_ENTITY, ConfigRevision.entity_id == _candidate_entity(agent_id, candidate_id),
        ConfigRevision.is_active.is_(True)).execution_options(populate_existing=True))


async def _candidate_proof(db, row, *, tenant_id, agent_id, allow_prepared=False):
    from app.services.session_model_inputs import _model_dependencies

    c = row.content if row is not None and isinstance(row.content, dict) else {}
    if c.get("origin_kind") == "distillation":
        if allow_prepared:
            raise ContentInputReceiptError("learned_skill_operation_denied", "Choose the original submitted Skill operation.", 403)
        try:
            task = await db.get(RuntimeTask, uuid.UUID(c["source_principal"]["runtime_task_id"]), populate_existing=True)
            session = await db.get(ChatSession, uuid.UUID(c["authority"]["source_session_id"]), populate_existing=True)
            if (not _revision_valid(row, tenant_id=tenant_id, entity_type=CANDIDATE_ENTITY) or not row.is_active
                or c.get("schema") != CANDIDATE_SCHEMA or c.get("agent_id") != str(agent_id)
                or row.entity_id != _candidate_entity(agent_id, c["candidate_id"]) or row.change_source != "evolve"
                or row.changed_by_agent_id != agent_id or str(row.changed_by_user_id) != c["authority"]["owner_user_id"]
                or session is None or session.tenant_id != tenant_id or session.agent_id != agent_id
                or task is None or task.tenant_id != tenant_id or str(task.root_user_id) != c["authority"]["owner_user_id"]
                or agent_id not in {task.parent_agent_id, task.child_agent_id}
                or str(session.id) not in {task.parent_session_id, task.child_session_id}
                or c["source_principal"]["session_id"] != str(session.id)
                or c["source_principal"]["agent_id"] != str(agent_id)
                or c["source_principal"]["requester_user_id"] != str(task.root_user_id) or not c.get("dependencies")):
                raise ValueError("distillation_origin_invalid")
            return c
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ContentInputReceiptError("learned_skill_sources_unverified", "Restore the original background review inputs.", 403) from exc
    try:
        invocation = await db.get(SessionToolInvocation, uuid.UUID(c["invocation_id"]), populate_existing=True)
        model = await db.get(SessionModelResult, uuid.UUID(c["model_result_id"]), populate_existing=True)
        session = await db.get(ChatSession, uuid.UUID(c["authority"]["source_session_id"]), populate_existing=True)
        task = await db.get(RuntimeTask, invocation.run_id, populate_existing=True) if invocation is not None else None
        sources, complete = _model_dependencies(model)
        valid = (_revision_valid(row, tenant_id=tenant_id, entity_type=CANDIDATE_ENTITY) and row.is_active
            and c.get("schema") == CANDIDATE_SCHEMA and c.get("agent_id") == str(agent_id)
            and (c.get("phase", "captured") == "captured" or allow_prepared and c.get("phase") in {"prepared", "cancelled"})
            and row.entity_id == _candidate_entity(agent_id, c["candidate_id"])
            and row.changed_by_user_id == uuid.UUID(c["authority"]["owner_user_id"])
            and invocation is not None and invocation.tenant_id == tenant_id
            and session is not None and session.tenant_id == tenant_id and session.agent_id == agent_id
            and invocation.session_id == session.id
            and task is not None and task.tenant_id == tenant_id
            and str(task.root_user_id) == c["authority"]["owner_user_id"]
            and agent_id in {task.parent_agent_id, task.child_agent_id}
            and str(session.id) in {task.parent_session_id, task.child_session_id}
            and c["source_principal"]["agent_id"] == str(agent_id)
            and c["source_principal"]["requester_user_id"] == str(task.root_user_id)
            and c["source_principal"]["runtime_task_id"] == str(task.id)
            and c["source_principal"]["session_id"] == str(session.id)
            and invocation.tool_name == "save_skill" and (invocation.effect_state == "effect_committed"
                or (allow_prepared or c.get("recovery")) and invocation.effect_state in {"failed", "needs_reconciliation", "prepared_not_started"})
            and invocation.authority_snapshot_hash == c["authority_hash"]
            and (invocation.effective_args_hash or invocation.args_hash) == c["args_hash"]
            and model is not None and model.tenant_id == tenant_id and model.session_id == session.id
            and model.run_id == invocation.run_id and model.state in {"sealed", "round_committed"}
            and model.provider_request_id == invocation.provider_request_id
            and model.model_request_hash == c["model_request_hash"]
            and _sha256(model.model_request_snapshot_json) == model.model_request_hash
            and complete and sources == c["dependencies"])
    except (ValueError, TypeError, KeyError, AttributeError):
        valid = False
    if not valid:
        raise ContentInputReceiptError("learned_skill_sources_unverified", "Restore the original Skill candidate inputs.", 403)
    if c.get("recovery"):
        from app.services.skill_candidate_operations import validate_recovery

        await validate_recovery(db, row, tenant_id=tenant_id, agent_id=agent_id)
    return c


async def authorize_candidate(db, row, *, tenant_id, agent_id, request, check_draft=False):
    c = await _candidate_proof(db, row, tenant_id=tenant_id, agent_id=agent_id)
    await authorize_memory_audience(db, tenant_id=tenant_id, agent_id=agent_id, request=request, authority=c["authority"])
    await _validate_input_dependencies(db, tenant_id, c["dependencies"], runtime_request=request)
    if c.get("recovery"):
        await _validate_input_dependencies(db, tenant_id, c["recovery"]["dependencies"], runtime_request=request)
    if check_draft:
        from app.services.content_input_receipts import _validate_source_bytes

        _validate_source_bytes(Path(str(agent_id)) / c["draft_path"], c["draft_sha256"])
    await authorize_memory_audience(db, tenant_id=tenant_id, agent_id=agent_id, request=request, authority=c["authority"])
    return c


async def candidate_result_sources(db, *, tenant_id, agent_id, invocation, content):
    candidate_id = f"save-skill-{uuid.uuid5(invocation.id, 'learned-skill-candidate')}"
    row = await candidate_revision(db, tenant_id=tenant_id, agent_id=agent_id, candidate_id=candidate_id)
    try:
        c = await _candidate_proof(db, row, tenant_id=tenant_id, agent_id=agent_id)
        if c["receipt"] != content or c["invocation_id"] != str(invocation.id):
            return [], False
        return [{"source_kind": "learned_skill_candidate", "source_id": str(row.id), "source_version": row.content_hash}], True
    except ContentInputReceiptError:
        return [], False


async def validate_candidate_dependency(db, tenant_id, dependency, *, request=None, user=None):
    try:
        row = await db.get(ConfigRevision, uuid.UUID(dependency["source_id"]), populate_existing=True)
        agent_id = uuid.UUID(row.content["agent_id"])
        if row.content_hash != dependency["source_version"]:
            raise ValueError("learned_skill_candidate_changed")
        if request is not None:
            await authorize_candidate(db, row, tenant_id=tenant_id, agent_id=agent_id, request=request)
        else:
            c = await _candidate_proof(db, row, tenant_id=tenant_id, agent_id=agent_id)
            await _browser_candidate(db, c, tenant_id=tenant_id, user=user)
    except ContentInputReceiptError:
        raise
    except (ValueError, AttributeError, TypeError, KeyError) as exc:
        raise ContentInputReceiptError("learned_skill_candidate_unavailable", "The Skill candidate input needs reconciliation.", 403) from exc


async def _browser_candidate(db, c, *, tenant_id, user):
    from app.core.permissions import is_scoped_business_admin
    from app.core.source_read_purpose import business_inspection_allowed
    from app.services.business_teams import _live_user
    from app.services.content_input_receipts import _authorize_session_input_source

    live = await _live_user(db, user, fresh=True) if user is not None else None
    authority = c["authority"]
    admin = (live is not None and business_inspection_allowed()
        and is_scoped_business_admin(live, resource_tenant_id=tenant_id))
    if live is None or not live.is_active or (live.tenant_id != tenant_id and not admin):
        raise ValueError("learned_skill_reader_unavailable")
    if authority["audience_kind"] == "private" and str(live.id) != authority["owner_user_id"] and not admin:
        raise ValueError("learned_skill_reader_denied")
    origin = await db.get(ChatSession, uuid.UUID(authority["source_session_id"]), populate_existing=True)
    await _authorize_session_input_source(db, tenant_id, origin, user=live, runtime_request=None)
    await _validate_input_dependencies(db, tenant_id, c["dependencies"], user=live)
    if c.get("recovery"):
        await _validate_input_dependencies(db, tenant_id, c["recovery"]["dependencies"], user=live)


def _scope_key(authority):
    return (authority["audience_kind"], authority["owner_user_id"]) if authority["audience_kind"] == "private" else tuple(
        authority[key] for key in ("audience_kind", "source_channel", "group_installation_ref", "group_subject_id"))


def _source_reader(c):
    from app.services.personal_knowledge_access import AgentRuntimePrincipal

    p = c["source_principal"]
    return AgentRuntimePrincipal(agent_id=uuid.UUID(p["agent_id"]), requester_user_id=uuid.UUID(p["requester_user_id"]),
        session_id=p["session_id"], runtime_task_id=p["runtime_task_id"], purpose=p["purpose"],
        autonomous=p["autonomous"], delegation_id=p["delegation_id"])


@asynccontextmanager
async def skill_maintenance_inputs(*, workspace, agent_id, tenant_id, target_path, candidate_id, catalog=False):
    """Read one installed version using its persisted source principal."""
    if tenant_id is None:
        yield None
        return
    from app.config import get_settings
    from app.services.skill_source_receipts import _installation_id

    parts = Path(target_path).parts
    if (agent_id is None or len(parts) != 3 or parts[0] != "skills" or parts[2] != "SKILL.md"
        or target_path != Path(target_path).as_posix()
        or Path(workspace).resolve() != (Path(get_settings().AGENT_DATA_DIR) / str(agent_id)).resolve()):
        raise ContentInputReceiptError("skill_maintenance_source_unavailable", "Restore the exact installed Skill source.", 503)
    async with tenant_scoped_session(tenant_id) as db:
        row = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
            ConfigRevision.entity_type == "agent_skill_install", ConfigRevision.is_active.is_(True),
            ConfigRevision.entity_id == _installation_id(agent_id, parts[1])))
        content = row.content if row is not None and isinstance(row.content, dict) else {}
        learning = content.get("learning") or {}
        if not learning or learning.get("gate_candidate_id") != candidate_id:
            raise ContentInputReceiptError("skill_maintenance_source_unavailable",
                "Reconcile the original reviewed installation before lifecycle review.", 503)
        original = await db.get(ConfigRevision, uuid.UUID(learning["candidate_revision_id"]), populate_existing=True)
        c = await _candidate_proof(db, original, tenant_id=tenant_id, agent_id=agent_id)
        reader = _source_reader(c)
        await validate_learned_installation(db, row, tenant_id=tenant_id, agent_id=agent_id, request=reader)
        await _maintenance_not_retired(db, tenant_id, row.id)
        source = {"source_kind": "learned_skill_file", "source_id": f"{agent_id}\n{target_path}\n{row.id}",
            "source_version": content["files"][target_path]}
    frame = SkillReviewInputs(agent_id=agent_id, tenant_id=tenant_id, reader=reader, dependencies=[source],
        source_scope=_scope_key(c["authority"]))
    if catalog:
        from app.models.user import User
        from app.services.skill_source_receipts import SkillInputCapture, load_skill_input_snapshot
        from app.services.workspace_resource_authority import load_workspace_authority_scope

        async with tenant_scoped_session(tenant_id) as db:
            user = await db.get(User, reader.requester_user_id, populate_existing=True)
            frame.scope = await load_workspace_authority_scope(db, user, agent_id=agent_id,
                session_id=uuid.UUID(reader.session_id), runtime_principal=reader)
        frame.skill_capture = SkillInputCapture(agent_id, allow_empty=True)
        await load_skill_input_snapshot(frame.skill_capture, tenant_id, runtime_request=reader)
    token = _REVIEW.set(frame)
    try:
        await recheck_review_inputs()
        yield frame
    finally:
        _REVIEW.reset(token)


async def skill_maintenance_targets(*, agent_id, tenant_id):
    async with tenant_scoped_session(tenant_id) as db:
        rows = list(await db.scalars(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
            ConfigRevision.entity_type == "agent_skill_install", ConfigRevision.is_active.is_(True),
            ConfigRevision.content["agent_id"].astext == str(agent_id)).order_by(ConfigRevision.created_at, ConfigRevision.id)))
        return [{"target_path": f"skills/{row.content['folder_name']}/SKILL.md",
            "candidate_id": row.content["learning"].get("gate_candidate_id"), "source_ref": str(row.id)}
            for row in rows if isinstance(row.content, dict) and isinstance(row.content.get("learning"), dict)]


def maintenance_catalog_path_allowed(workspace, path):
    frame = current_review()
    try:
        relative = Path(path).resolve().relative_to(Path(workspace).resolve()).as_posix()
        return frame is not None and frame.scope.can_read(relative) and relative in frame.skill_capture.files
    except (ValueError, AttributeError):
        return False


def observe_maintenance_catalog(path, content, *, workspace):
    import hashlib

    frame = current_review()
    if frame is not None and frame.skill_capture is not None:
        frame.skill_capture.observe_file(Path(path).resolve().relative_to(Path(workspace).resolve()).as_posix(),
            hashlib.sha256(content).hexdigest())


async def recheck_maintenance_effect(*, workspace, target_path):
    frame = current_review()
    if frame is None:
        return
    from app.models.agent import Agent
    from app.services.approval_ticket import build_live_approval_policy_snapshot
    from app.tools.guard_policy import evaluate_guard_policy

    dependencies = await recheck_review_inputs()
    async with tenant_scoped_session(frame.tenant_id) as db:
        for source in dependencies:
            if source["source_kind"] == "learned_skill_file" and source["source_id"].split("\n")[1] == target_path:
                await _maintenance_not_retired(db, frame.tenant_id, uuid.UUID(source["source_id"].rsplit("\n", 1)[1]))
        agent = await db.get(Agent, frame.agent_id, populate_existing=True)
        if agent is None or agent.deleted_at is not None or agent.deactivated_at is not None:
            raise ContentInputReceiptError("skill_maintenance_employee_inactive", "Restore current employee authority before lifecycle changes.", 403)
        live = await build_live_approval_policy_snapshot(db=db, tenant_id=frame.tenant_id,
            agent_id=frame.agent_id, tool_name="save_skill")
        verdict = evaluate_guard_policy(tool_name="save_skill", arguments={"folder_name": Path(target_path).parts[1],
            "instructions": (Path(workspace) / target_path).read_text()}, external_visible=False, snapshot=live["guard_policy"])
        if live["capability"]["denied"] or verdict.decision == "deny":
            raise ContentInputReceiptError("skill_maintenance_policy_denied", "Current policy denies Skill lifecycle changes.", 403)
        if (live["capability"]["policy_found"] and live["capability"]["escalate_to_l3"]) or verdict.decision == "require_approval":
            raise ContentInputReceiptError("skill_maintenance_approval_required", "Approve the exact lifecycle decision before applying it.", 409)


async def _maintenance_not_retired(db, tenant_id, installation_id):
    rows = await db.scalars(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
        ConfigRevision.entity_type == "learned_skill_lifecycle", ConfigRevision.is_active.is_(True),
        ConfigRevision.content["installation_revision_id"].astext == str(installation_id),
        ConfigRevision.content["state"].astext == "retired"))
    if any(_revision_valid(row, tenant_id=tenant_id, entity_type="learned_skill_lifecycle") for row in rows):
        raise ContentInputReceiptError("skill_maintenance_retired", "The explicitly retired version cannot be autonomously reactivated.", 403)


@asynccontextmanager
async def skill_review_inputs(*, agent_id, tenant_id, workspace, current_session_id):
    """Review one durable source audience, without borrowing another member's context."""
    if tenant_id is None:
        yield None
        return
    from app.models.user import User
    from app.services.workspace_resource_authority import load_workspace_authority_scope

    frame = SkillReviewInputs(agent_id=agent_id, tenant_id=tenant_id)
    async with tenant_scoped_session(tenant_id) as db:
        rows = list(await db.scalars(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
            ConfigRevision.entity_type == CANDIDATE_ENTITY, ConfigRevision.is_active.is_(True),
            ConfigRevision.content["agent_id"].astext == str(agent_id)).order_by(ConfigRevision.created_at, ConfigRevision.id)))
        installed = list(await db.scalars(select(ConfigRevision.content).where(ConfigRevision.tenant_id == tenant_id,
            ConfigRevision.entity_type == "agent_skill_install", ConfigRevision.content["agent_id"].astext == str(agent_id))))
        processed = {content["learning"]["candidate_revision_id"] for content in installed
            if isinstance(content, dict) and isinstance(content.get("learning"), dict)
            and isinstance(content["learning"].get("candidate_revision_id"), str)}
        for row in rows:
            if str(row.id) in processed and row.content.get("submitted_candidate_revision_id"):
                processed.add(row.content["submitted_candidate_revision_id"])
        current = await db.get(ChatSession, uuid.UUID(current_session_id), populate_existing=True) if current_session_id else None
        selected_scope = None
        # This schedules a source-bound review transaction; candidate semantics remain model-owned inside it.
        for row in rows:
            if str(row.id) in processed:
                continue
            try:
                c = await _candidate_proof(db, row, tenant_id=tenant_id, agent_id=agent_id)
                reader = _source_reader(c)
                if current is not None:
                    if current.tenant_id != tenant_id or current.agent_id != agent_id:
                        continue
                    if current.audience_kind in {None, "private"}:
                        if c["authority"]["audience_kind"] != "private" or str(current.user_id) != c["authority"]["owner_user_id"]:
                            continue
                    elif current.audience_kind != "group" or any(c["authority"].get(key) != getattr(current, key)
                        for key in ("source_channel", "group_installation_ref", "group_subject_id")):
                        continue
                scope_key = _scope_key(c["authority"])
                if selected_scope is not None and scope_key != selected_scope:
                    continue
                await authorize_candidate(db, row, tenant_id=tenant_id, agent_id=agent_id,
                    request=frame.reader or reader, check_draft=True)
                if frame.reader is None:
                    frame.reader, selected_scope = reader, scope_key
                frame.candidates[c["candidate_id"]] = row.id
            except (ContentInputReceiptError, ValueError, KeyError, TypeError):
                frame.unavailable = True
        if frame.reader is None:
            from app.services.personal_knowledge_access import AgentRuntimePrincipal
            from app.services.content_input_receipts import _runtime_input_actor

            origins = await db.execute(select(SessionModelResult, ChatSession, RuntimeTask)
                .join(ChatSession, ChatSession.id == SessionModelResult.session_id)
                .join(RuntimeTask, RuntimeTask.id == SessionModelResult.run_id).where(
                    SessionModelResult.tenant_id == tenant_id, ChatSession.tenant_id == tenant_id,
                    RuntimeTask.tenant_id == tenant_id, ChatSession.agent_id == agent_id,
                    RuntimeTask.root_user_id.is_not(None), SessionModelResult.state.in_(("sealed", "round_committed")),
                    *([ChatSession.id == current.id] if current is not None else []))
                .order_by(ChatSession.created_at, ChatSession.id))
            for result, session, task in origins:
                if task.task_type in {"delegation", "remote_a2a", "subagent"}:
                    # These lanes need their persisted carried principal, not a newly inferred delegation.
                    frame.unavailable = True
                    continue
                autonomous = task.task_type != "web_chat_turn"
                reader = AgentRuntimePrincipal(agent_id=agent_id, requester_user_id=task.root_user_id,
                    session_id=str(session.id), runtime_task_id=str(task.id),
                    purpose="autonomous_agent" if autonomous else "interactive_session", autonomous=autonomous)
                try:
                    await _runtime_input_actor(db, tenant_id, reader)
                    await validate_model_source(db, tenant_id, model_source(result), request=reader)
                    audience_for_session(session)
                    frame.reader = reader
                    break
                except ContentInputReceiptError:
                    frame.unavailable = True
        if frame.reader is not None:
            session = await db.get(ChatSession, uuid.UUID(frame.reader.session_id), populate_existing=True)
            frame.source_scope = _scope_key({"owner_user_id": str(frame.reader.requester_user_id), **audience_for_session(session)})
            user = await db.get(User, frame.reader.requester_user_id, populate_existing=True)
            frame.scope = await load_workspace_authority_scope(db, user, agent_id=agent_id,
                session_id=uuid.UUID(frame.reader.session_id), runtime_principal=frame.reader)
    if frame.reader is not None:
        from app.services.skill_source_receipts import SkillInputCapture, load_skill_input_snapshot

        frame.skill_capture = SkillInputCapture(agent_id, allow_empty=True)
        await load_skill_input_snapshot(frame.skill_capture, tenant_id, runtime_request=frame.reader)
    token = _REVIEW.set(frame)
    try:
        yield frame
    finally:
        _REVIEW.reset(token)


async def review_candidate(candidate_id, *, workspace):
    frame = current_review()
    if frame is None:
        return None
    identity = frame.candidates.get(candidate_id)
    if identity is None or frame.reader is None:
        raise ContentInputReceiptError("learned_skill_review_sources_unavailable", "Review this candidate from its original authorized work.", 503)
    async with tenant_scoped_session(frame.tenant_id) as db:
        row = await db.get(ConfigRevision, identity, populate_existing=True)
        c = await authorize_candidate(db, row, tenant_id=frame.tenant_id, agent_id=frame.agent_id,
            request=frame.reader, check_draft=True)
        return row, c


async def review_draft_target(*, workspace, gate_candidate_id, draft, source_candidate_id=None):
    from app.services.agent_tool_domains.workspace import _normalize_skill_folder_name

    frame = current_review()
    if source_candidate_id in frame.candidates:
        _row, prior = await review_candidate(source_candidate_id, workspace=workspace)
        authority, slug = prior["authority"], prior["requested_folder"]
    else:
        async with tenant_scoped_session(frame.tenant_id) as db:
            session = await db.get(ChatSession, uuid.UUID(frame.reader.session_id), populate_existing=True)
            authority = {"schema": AUTHORITY_SCHEMA, "tenant_id": str(frame.tenant_id), "agent_id": str(frame.agent_id),
                "owner_user_id": str(frame.reader.requester_user_id), "source_session_id": str(session.id), **audience_for_session(session)}
        slug = _normalize_skill_folder_name(draft.name)
    candidate_id = gate_candidate_id
    folder = f"learned-{_family(frame.agent_id, authority, slug)}-{uuid.uuid5(frame.agent_id, 'review:' + candidate_id).hex}"
    return candidate_id, folder, authority, slug


async def bind_reviewed_draft(*, workspace, gate_candidate_id, draft, source_candidate_id=None):
    """Pin the complete review inputs before the native installation effect."""
    import hashlib

    frame = current_review()
    if frame is None:
        return None
    dependencies = await recheck_review_inputs()
    if not dependencies:
        raise ContentInputReceiptError("skill_review_inputs_unverified", "Retain the review until its original inputs are available.", 503)
    candidate_id, folder, authority, slug = await review_draft_target(workspace=workspace,
        gate_candidate_id=gate_candidate_id, draft=draft, source_candidate_id=source_candidate_id)
    path = f"evolution/skill_candidates/{gate_candidate_id}/SKILL.md.draft"
    digest = hashlib.sha256((Path(workspace) / path).read_bytes()).hexdigest()
    content = {"schema": CANDIDATE_SCHEMA, "origin_kind": "distillation", "agent_id": str(frame.agent_id),
        "candidate_id": candidate_id, "source_principal": frame.reader.evidence(), "authority": authority,
        "dependencies": dependencies, "requested_folder": slug, "folder_name": folder, "draft_path": path, "draft_sha256": digest,
        "family_id": str(_family(frame.agent_id, authority, slug)),
        "submitted_candidate_revision_id": str(frame.candidates[source_candidate_id]) if source_candidate_id in frame.candidates else None}
    async with tenant_scoped_session(frame.tenant_id) as db:
        row = await save_revision(db, tenant_id=frame.tenant_id, entity_type=CANDIDATE_ENTITY,
            entity_id=_candidate_entity(frame.agent_id, candidate_id), content=content, change_source="evolve",
            changed_by_user_id=frame.reader.requester_user_id, changed_by_agent_id=frame.agent_id,
            change_message="Reviewed Skill draft and complete source inputs captured")
        await db.commit()
        frame.candidates[candidate_id] = row.id
    return candidate_id, f"skills/{folder}/SKILL.md"


async def record_reviewed_installation(db, *, agent_id, tenant_id, workspace, candidate_id, target_path, gate_candidate_id):
    """Called only by the existing native commit after its review gates succeeded."""
    from app.services.skill_source_receipts import _installation_id, _package_hashes

    checked = await review_candidate(candidate_id, workspace=workspace)
    if checked is None:
        return
    row, c = checked
    dependencies = await recheck_review_inputs()
    expected = f"skills/{c['folder_name']}/SKILL.md"
    if target_path != expected:
        raise ContentInputReceiptError("learned_skill_target_denied", "The review cannot replace another source audience's Skill.", 403)
    files = _package_hashes(Path(workspace), c["folder_name"])
    if files.get(expected) != c["draft_sha256"]:
        raise ContentInputReceiptError("learned_skill_installation_changed", "Reconcile the native Skill bytes with the reviewed draft.", 403)
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"registry-skill-install:{tenant_id}:{agent_id}"})
    await save_revision(db, tenant_id=tenant_id, entity_type="agent_skill_install",
        entity_id=_installation_id(agent_id, c["folder_name"]), change_source="evolve",
        changed_by_user_id=uuid.UUID(c["authority"]["owner_user_id"]), changed_by_agent_id=agent_id,
        content={"schema": "hive.skill_installation_inputs.v1", "agent_id": str(agent_id), "folder_name": c["folder_name"],
            "files": files, "unverified_inputs": [], "origins": [],
            "learning": {"candidate_revision_id": str(row.id), "candidate_content_hash": row.content_hash,
                "gate_candidate_id": gate_candidate_id, "authority": c["authority"], "family_id": c.get("family_id"),
                "review_dependencies": dependencies}},
        change_message="Reviewed learned Skill native bytes and original inputs captured")


async def validate_learned_installation(db, row, *, tenant_id, agent_id, request=None, user=None):
    from app.services.skill_source_receipts import _installation_id

    content = row.content if row is not None and isinstance(row.content, dict) else {}
    try:
        learning = content["learning"]
        original = await db.get(ConfigRevision, uuid.UUID(learning["candidate_revision_id"]), populate_existing=True)
        c = await _candidate_proof(db, original, tenant_id=tenant_id, agent_id=agent_id)
        if (not _revision_valid(row, tenant_id=tenant_id, entity_type="agent_skill_install")
            or row.entity_id != _installation_id(agent_id, c["folder_name"]) or row.change_source != "evolve"
            or row.changed_by_agent_id != agent_id or row.changed_by_user_id != uuid.UUID(c["authority"]["owner_user_id"])
            or original.content_hash != learning["candidate_content_hash"] or c["authority"] != learning["authority"]
            or c.get("family_id") != learning.get("family_id")
            or content.get("unverified_inputs") != [] or content.get("agent_id") != str(agent_id)
            or content.get("folder_name") != c["folder_name"] or not content.get("files")):
            raise ValueError("learned_skill_source_changed")
        if request is not None:
            await authorize_candidate(db, original, tenant_id=tenant_id, agent_id=agent_id, request=request)
        else:
            await _browser_candidate(db, c, tenant_id=tenant_id, user=user)
        await _validate_input_dependencies(db, tenant_id, learning.get("review_dependencies", []),
            user=user, runtime_request=request)
        return c
    except ContentInputReceiptError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ContentInputReceiptError("learned_skill_source_denied", "The learned Skill requires its current source audience and inputs.", 403) from exc


async def learning_file_access(db, *, tenant_id, agent_id, request=None, user=None):
    """Return exact protected roots and authorized bytes for the real source reader."""
    from app.config import get_settings
    from app.services.skill_evolution_registry import STATE_ROLLED_BACK, load_skill_evolution_registry

    base = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
    registry = load_skill_evolution_registry(base)
    rolled_back = {entry.get("target_path") for entry in registry["skills"].values()
        if isinstance(entry, dict) and entry.get("state") == STATE_ROLLED_BACK}
    roots, allowed = set(), {}
    rows = list(await db.scalars(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
        ConfigRevision.entity_type == "agent_skill_install", ConfigRevision.content["agent_id"].astext == str(agent_id),
        ConfigRevision.content.has_key("learning")).order_by(ConfigRevision.created_at.desc(), ConfigRevision.id.desc())))
    selected_families = set()
    lifecycle = list(await db.scalars(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
        ConfigRevision.entity_type == "learned_skill_lifecycle", ConfigRevision.is_active.is_(True),
        ConfigRevision.content["agent_id"].astext == str(agent_id))))
    retired = {row.content.get("installation_revision_id") for row in lifecycle
        if _revision_valid(row, tenant_id=tenant_id, entity_type="learned_skill_lifecycle")
        and row.content.get("schema") == "hive.learned_skill_lifecycle.v1" and row.content.get("state") == "retired"}
    for row in rows:
        folder = row.content.get("folder_name")
        if not isinstance(folder, str) or Path(folder).name != folder:
            continue
        roots.add(f"skills/{folder}")
        if not row.is_active:
            continue
        if f"skills/{folder}/SKILL.md" in rolled_back:
            continue
        family = row.content.get("learning", {}).get("family_id") or folder
        if family in selected_families:
            continue
        selected_families.add(family)
        if str(row.id) in retired:
            continue
        try:
            await validate_learned_installation(db, row, tenant_id=tenant_id, agent_id=agent_id, request=request, user=user)
            allowed.update({path: (str(row.id), digest) for path, digest in row.content["files"].items()})
        except ContentInputReceiptError:
            continue
    candidates = list(await db.scalars(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
        ConfigRevision.entity_type == CANDIDATE_ENTITY, ConfigRevision.content["agent_id"].astext == str(agent_id))))
    if candidates:
        roots.update({"evolution/evolution_ledger.jsonl", "evolution/skill_candidates.md", "evolution/skill_review.md",
            "evolution/skill_usage.jsonl", "evolution/skill_usage.json", "evolution/skill_registry.json",
            "evolution/skill_curator_reviews.jsonl"})
    for row in candidates:
        c = row.content
        root = f"evolution/skill_candidates/{c.get('candidate_id', '')}"
        roots.add(root)
        try:
            if request is None:
                continue
            await authorize_candidate(db, row, tenant_id=tenant_id, agent_id=agent_id, request=request)
            from app.services.workspace_resource_authority import open_workspace_file
            import hashlib

            for file in sorted((base / root).glob("*")):
                if file.is_file():
                    path = file.relative_to(base).as_posix()
                    with open_workspace_file(base, path) as stream:
                        allowed[path] = (str(row.id), hashlib.file_digest(stream, "sha256").hexdigest())
        except ContentInputReceiptError:
            continue
    return frozenset(roots), tuple((path, revision, digest) for path, (revision, digest) in sorted(allowed.items()))


async def retire_learned_installation(db, *, agent_id, user, workspace, folder_name):
    """Retire discovery atomically while keeping previously consumed bytes recoverable."""
    from app.core.permissions import require_agent_manage_access
    from app.services.ai_assets import register_projection
    from app.services.ai_asset_adapters import project_workspace_skill
    from app.services.skill_source_receipts import _installation_id

    agent = await require_agent_manage_access(db, user, agent_id)
    row = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == agent.tenant_id,
        ConfigRevision.entity_type == "agent_skill_install", ConfigRevision.entity_id == _installation_id(agent_id, folder_name),
        ConfigRevision.is_active.is_(True)).execution_options(populate_existing=True))
    if row is None or not (row.content or {}).get("learning"):
        if folder_name.startswith("learned-"):
            raise ContentInputReceiptError("learned_skill_source_unavailable", "Reconcile the original learned Skill before removal.", 409)
        return None
    c = await validate_learned_installation(db, row, tenant_id=agent.tenant_id, agent_id=agent_id, user=user)
    identity = uuid.uuid5(agent_id, f"learned-skill-lifecycle:{row.id}")
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"registry-skill-install:{agent.tenant_id}:{agent_id}"})
    previous = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == agent.tenant_id,
        ConfigRevision.entity_type == "learned_skill_lifecycle", ConfigRevision.entity_id == identity,
        ConfigRevision.is_active.is_(True)).execution_options(populate_existing=True))
    content = {"schema": "hive.learned_skill_lifecycle.v1", "agent_id": str(agent_id), "family_id": c["family_id"],
        "installation_revision_id": str(row.id), "state": "retired"}
    if previous is not None and previous.content == content and previous.content_hash == canonical_content_hash(content):
        return {"status": "already_retired", "folder_name": folder_name, "files_removed": 0}
    await save_revision(db, tenant_id=agent.tenant_id, entity_type="learned_skill_lifecycle", entity_id=identity,
        content=content, change_source="user", changed_by_user_id=user.id, change_message="Learned Skill retired with historical bytes retained")
    await register_projection(db, project_workspace_skill(workspace=workspace, folder_name=folder_name,
        tenant_id=agent.tenant_id, agent_id=agent_id, lifecycle_status="deprecated", evolution_state="archived"),
        change_source="retire", actor_user_id=user.id, change_message="Learned Skill retired from active discovery")
    await db.commit()
    return {"status": "retired", "folder_name": folder_name, "files_removed": 0}


async def authorize_browser_learning_path(db, user, *, agent_id, path, observed_sha256=None):
    from app.core.permissions import check_agent_access
    from app.core.resource_authority import normalize_workspace_resource_path

    path = normalize_workspace_resource_path(path)
    if Path(path).parts[:1] not in {("skills",), ("evolution",)}:
        return None
    agent, _access = await check_agent_access(db, user, agent_id)
    roots, files = await learning_file_access(db, tenant_id=agent.tenant_id, agent_id=agent_id, user=user)
    if not managed_learning_path(path) and not any(path == root or path.startswith(root + "/") for root in roots):
        return None
    selected = [(file, digest) for file, _revision, digest in files if file == path or file.startswith(path + "/")]
    if not selected or (observed_sha256 is not None and (path, observed_sha256) not in selected):
        raise ContentInputReceiptError("learned_skill_source_denied", "This Skill requires its current original source authority and exact bytes.", 403)
    return "learned_skill_source"


async def authorize_learned_asset_read(db, record, *, user, revision_content=None):
    """An enterprise projection is a consumer of Skill source authority, never a grant."""
    from app.services.skill_source_receipts import _installation_id
    import hashlib

    locator = record.native_locator_json or {}
    if record.asset_type != "skill" or locator.get("kind") != "agent_workspace":
        return
    try:
        agent_id, folder = uuid.UUID(locator["agent_id"]), locator["folder_name"]
        row = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == record.tenant_id,
            ConfigRevision.entity_type == "agent_skill_install", ConfigRevision.entity_id == _installation_id(agent_id, folder),
            ConfigRevision.is_active.is_(True)).execution_options(populate_existing=True))
        if not (row is not None and (row.content or {}).get("learning")) and not folder.startswith("learned-"):
            return
        if row is None or record.native_key != f"skill:agent:{agent_id}:{folder}":
            raise ValueError("learned_asset_origin_unavailable")
        await validate_learned_installation(db, row, tenant_id=record.tenant_id, agent_id=agent_id, user=user)
        active = await db.get(ConfigRevision, record.active_revision_id, populate_existing=True)
        if (active is None or active.tenant_id != record.tenant_id or active.entity_type != "ai_asset"
            or active.entity_id != record.id or active.content_hash != record.content_hash
            or canonical_content_hash(active.content) != active.content_hash):
            raise ValueError("learned_asset_revision_invalid")
        for content in (active.content, revision_content):
            if content is None:
                continue
            files = content.get("files")
            observed = {f"skills/{folder}/{file['path']}": hashlib.sha256(file["content"].encode()).hexdigest() for file in files}
            if observed != row.content["files"]:
                raise ValueError("learned_asset_bytes_unbound")
    except ContentInputReceiptError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ContentInputReceiptError("learned_skill_asset_unavailable", "Reconcile the exact learned Skill projection and its sources.", 403) from exc


async def submit_candidate(request, *, workspace, producer, session_factory=None):
    """The sealed tool call supplies identity; its draft supplies no authority."""
    from app.services.agent_tool_domains.workspace import _normalize_skill_folder_name, _render_skill_markdown
    import hashlib

    tenant_id = uuid.UUID(str(request.context.tenant_id))
    async with tenant_scoped_session(tenant_id, session_factory=session_factory) as db:
        facts = await _writer_input(db, request)
        _tenant, principal, actor, session, invocation, model, dependencies, args_hash = facts
        await memory_effect_actor(db, tenant_id, request)
        candidate_id = f"save-skill-{uuid.uuid5(invocation.id, 'learned-skill-candidate')}"
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"learned-skill-candidate:{tenant_id}:{candidate_id}"})
        previous = await candidate_revision(db, tenant_id=tenant_id, agent_id=request.context.agent_id, candidate_id=candidate_id)
        if previous is not None:
            if previous.content.get("args_hash") != args_hash or previous.content.get("invocation_id") != str(invocation.id):
                raise ContentInputReceiptError("learned_skill_candidate_conflict", "Reconcile the original Skill submission.", 409)
            if previous.content.get("phase", "captured") == "captured":
                return previous.content["receipt"]
            if previous.content.get("phase") != "prepared":
                raise ContentInputReceiptError("learned_skill_submission_unknown", "Reconcile the original Skill source intent.", 409)
        authority = {"schema": AUTHORITY_SCHEMA, "tenant_id": str(tenant_id), "agent_id": str(request.context.agent_id),
            "owner_user_id": str(actor.id), "source_session_id": str(session.id), **audience_for_session(session)}
        slug = _normalize_skill_folder_name(request.arguments.get("folder_name") or request.arguments.get("name", ""))
        if not slug:
            raise ContentInputReceiptError("learned_skill_name_invalid", "Use a nonempty Skill name or folder.", 400)
        folder = f"learned-{_family(request.context.agent_id, authority, slug)}"
        source = {"schema": CANDIDATE_SCHEMA, "agent_id": str(request.context.agent_id), "candidate_id": candidate_id,
            "invocation_id": str(invocation.id), "model_result_id": str(model.id), "model_request_hash": model.model_request_hash,
            "authority_hash": invocation.authority_snapshot_hash, "args_hash": args_hash, "authority": authority,
            "dependencies": deepcopy(dependencies), "folder_name": folder, "requested_folder": slug,
            "source_principal": {**principal.evidence(), "session_id": str(session.id), "runtime_task_id": str(invocation.run_id)}}
        rendered = _render_skill_markdown(name=str(request.arguments["name"]).strip(),
            description=str(request.arguments["description"]).strip(), instructions=str(request.arguments["instructions"]).strip(),
            declared_tools=tuple(request.arguments.get("tools") or ()), declared_packs=tuple(request.arguments.get("packs") or ()))
        plan = {"schema": "hive.skill_candidate_plan.v1", "arguments": deepcopy(request.arguments),
            "draft_path": f"evolution/skill_candidates/{candidate_id}/SKILL.md.draft", "draft_content": rendered,
            "draft_sha256": hashlib.sha256(rendered.encode()).hexdigest()}
        source.update(plan=plan, plan_hash=canonical_content_hash(plan))
        # The source intent survives a later file/SQL failure. No private package or mixed ledger is written first.
        if previous is None:
            await save_revision(db, tenant_id=tenant_id, entity_type=CANDIDATE_ENTITY,
                entity_id=_candidate_entity(request.context.agent_id, candidate_id), change_source="tool",
                changed_by_user_id=actor.id, changed_by_agent_id=request.context.agent_id,
                content={**source, "phase": "prepared"}, change_message="Authenticated Skill source intent prepared before native writes")
            await db.commit()
        await _writer_input(db, request)
        await memory_effect_actor(db, tenant_id, request)
        candidate_args = {key: request.arguments[key] for key in ("name", "description", "instructions")}
        def source_allowed(path):
            try:
                relative = Path(path).resolve().relative_to(Path(workspace).resolve()).as_posix()
            except ValueError:
                return False
            scope = request.context.workspace_authority_scope
            return scope is not None and scope.can_read(relative)

        produced = []
        receipt = producer(workspace, agent_id=request.context.agent_id, **candidate_args,
            declared_tools=tuple(request.arguments.get("tools") or ()), declared_packs=tuple(request.arguments.get("packs") or ()),
            folder_name=folder, overwrite=bool(request.arguments.get("overwrite", False)), candidate_id=candidate_id,
            source_authorizer=source_allowed, prepared_markdown=rendered, outcome_observer=produced.append)
        if not produced:
            from app.services.skill_candidate_operations import SkillCandidateOperationError

            raise SkillCandidateOperationError("learned_skill_submission_unknown", "Inspect the retained candidate before retrying.",
                _candidate_entity(request.context.agent_id, candidate_id))
        draft_path = f"evolution/skill_candidates/{candidate_id}/SKILL.md.draft"
        from app.services.workspace_resource_authority import open_workspace_file
        import hashlib

        try:
            with open_workspace_file(workspace, draft_path) as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            if digest != plan["draft_sha256"]:
                raise ValueError("skill_candidate_plan_changed")
        except (OSError, ValueError) as exc:
            if not (Path(workspace) / draft_path).exists():
                return receipt
            raise ContentInputReceiptError("learned_skill_submission_unknown", "Preserve the Skill candidate for reconciliation.", 409) from exc
        # The producer may await no model, but policy and source access can still change before the SQL fence.
        await _writer_input(db, request)
        await memory_effect_actor(db, tenant_id, request)
        await save_revision(db, tenant_id=tenant_id, entity_type=CANDIDATE_ENTITY,
            entity_id=_candidate_entity(request.context.agent_id, candidate_id), change_source="tool",
            changed_by_user_id=actor.id, changed_by_agent_id=request.context.agent_id,
            content={**source, "phase": "captured", "draft_path": draft_path, "draft_sha256": digest, "receipt": receipt},
            change_message="Authenticated Skill candidate inputs captured")
        await db.commit()
        return receipt

"""Permission resolution keeps real tool execution and bounded PostgreSQL work."""

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import inspect
import json
import time
import uuid

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine

from app.database import pin_rls_tenant_context
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.organization_identity import ContentInputReceipt
from app.models.plaza import PlazaPost
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.models.user import User
from tests.services.test_session_permission_runtime import _seed_waiting_batch

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
_PHASE = ContextVar("a2x_sql_phase", default="execution_lease_relock_and_settlement")


@contextmanager
def _statements(monkeypatch):
    from app.api import chat_sessions
    from app.services import agent_tools, session_control_input, session_permission_runtime, session_v2_persistence

    phases, callers = Counter(), Counter()

    def before(_connection, _cursor, statement, _parameters, _context, _many):
        from greenlet import getcurrent

        phase = _PHASE.get()
        if phase == "tool_checks" and statement.lstrip().startswith("INSERT INTO plaza_posts"):
            phase = "tool_effect"
        phases[phase] += 1
        current, frame = getcurrent(), inspect.currentframe()
        while current is not None:
            while frame is not None:
                if "/app/" in frame.f_code.co_filename:
                    callers[(phase, frame.f_code.co_name)] += 1
                    return
                frame = frame.f_back
            current = current.parent
            frame = current.gr_frame if current is not None else None
        callers[(phase, "framework")] += 1

    def wrap(patch, module, name, phase):
        original = getattr(module, name)

        async def measured(*args, **kwargs):
            token = _PHASE.set(phase)
            try:
                return await original(*args, **kwargs)
            finally:
                _PHASE.reset(token)

        patch.setattr(module, name, measured)

    with monkeypatch.context() as patch:
        wrap(patch, chat_sessions, "_get_run_session_and_agent", "route_authority")
        wrap(patch, session_v2_persistence, "resolve_session_mutation_authority", "route_authority")
        for name in ("accept_tool_permission_response", "apply_permission_response_control_input"):
            wrap(patch, session_control_input, name, "permission_acceptance_and_control_input")
        wrap(patch, agent_tools, "execute_session_permission_tool", "tool_checks")
        wrap(patch, session_permission_runtime, "_settle_unstarted_batch_siblings", "sibling_settlement")
        wrap(patch, session_permission_runtime, "_queue_same_run_continuation", "continuation")
        event.listen(Engine, "before_cursor_execute", before)
        try:
            yield phases, callers
        finally:
            event.remove(Engine, "before_cursor_execute", before)


async def _history(seed, owner, scale):
    """Exceed run22's four earlier calls/rounds and 104 pre-approval events.

    Three prior runs each retain four canonical tool/model settlements and
    source receipts. The approving model consumes the latest four results;
    doubling history also doubles that observed inventory, not just dead rows.
    """
    from app.services.session_model_inputs import _hash, tool_result_source_hash
    from app.services.session_model_round import commit_sealed_model_round, prepare_model_request, seal_model_response
    from app.services.session_permission_runtime import _pre_effect_abort_evidence
    from app.services.session_semantic_history import _event_payload
    from app.services.session_tool_runtime import complete_tool_invocation, prepare_tool_invocation
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events
    from app.models.user_assistant_binding import UserAssistantBinding
    from app.services.owner_action_policy import load_owner_action_policy
    from app.tools.workspace import ensure_workspace

    sources = []
    async with owner() as db:
        db.add(UserAssistantBinding(tenant_id=seed.tenant_id, user_id=seed.user_id, agent_id=seed.agent_id,
            status="active", source="explicit_selection", owner_user_id_at_bind=seed.user_id,
            requester_user_id=seed.user_id))
        await load_owner_action_policy(db, agent_id=seed.agent_id, tenant_id=seed.tenant_id, create_default=True)
        for index in range(3 * scale):
            run_id = uuid.uuid4()
            task = RuntimeTask(id=run_id, tenant_id=seed.tenant_id, task_type="web_chat_turn", status="completed",
                parent_agent_id=seed.agent_id, child_agent_id=seed.agent_id, root_user_id=seed.user_id,
                parent_session_id=str(seed.session_id), child_session_id=str(seed.session_id),
                root_session_id=str(seed.session_id), root_runtime_task_id=run_id,
                prompt="Inspect the company plaza guidance.", completed_at=datetime.now(timezone.utc))
            db.add(task)
            await db.flush()
            for round_index in range(1, 5):
                call_id = f"historical-{run_id}-{round_index}"
                arguments = {"query": "company plaza publishing guidance"}
                request = await prepare_model_request(db, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
                    session_id=seed.session_id, run_id=run_id, turn_id=f"turn-{run_id.hex}", round_index=round_index,
                    messages=[{"role": "user", "content": "Inspect the publishing guidance."}], tools=[],
                    provider="synthetic", model="local-history")
                await seal_model_response(db, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
                    session_id=seed.session_id, run_id=run_id, turn_id=f"turn-{run_id.hex}", round_index=round_index,
                    provider_request_id=request, response={"content": None, "tool_calls": [{"id": call_id,
                        "type": "function", "function": {"name": "tool_search", "arguments": json.dumps(arguments)}}],
                        "finish_reason": "tool_calls", "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
                await commit_sealed_model_round(db, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
                    session_id=seed.session_id, run_id=run_id, turn_id=f"turn-{run_id.hex}", round_index=round_index,
                    provider_request_id=request)
                invocation = await prepare_tool_invocation(db, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
                    session_id=seed.session_id, run_id=run_id, provider_request_id=request,
                    provider_tool_use_id=call_id, tool_name="tool_search", arguments=arguments)
                await complete_tool_invocation(db, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
                    session_id=seed.session_id, invocation_id=invocation.id,
                    provider_result_content="Historical preparation ended before executor entry.",
                    execution_evidence=_pre_effect_abort_evidence(receipt_ref=f"history:{invocation.id}"))
                payload = _event_payload(await db.get(ChatTranscriptEvent, invocation.result_event_id))
                source = {"source_kind": "session_tool_invocation", "source_id": str(invocation.id),
                    "source_version": tool_result_source_hash(payload)}
                sources.append(source)
                db.add(ContentInputReceipt(id=uuid.uuid4(), tenant_id=seed.tenant_id,
                    carrier_kind="workspace_artifact", carrier_ref=f"{seed.agent_id}\nworkspace/history-{call_id}.md",
                    revision=1, content_sha256=_hash(call_id), dependencies_json=[source], authority_json={}))
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed.run_id))
        snapshot = dict(model.model_request_snapshot_json)
        snapshot["input_bindings"] = {"schema": "hive.model_input_bindings.v1", "dependencies": sources[-4 * scale:],
            "dependency_coverage": {"complete": True}}
        model.model_request_snapshot_json = snapshot
        model.model_request_hash = _hash(snapshot)
        count = await db.scalar(select(func.count()).select_from(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == seed.session_id))
        if count < 104 * scale:
            await append_session_events(db, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
                session_id=seed.session_id, drafts=[SessionEventDraft(item_id=uuid.uuid4(),
                    item_kind="assistant_reasoning_private", lifecycle="delta",
                    scope={"level": "round", "session_id": str(seed.session_id), "thread_id": str(seed.session_id),
                        "run_id": str(seed.run_id), "turn_id": seed.turn_id, "round_id": model.round_id},
                    actor={"type": "agent", "id": str(seed.agent_id)}, payload={"text": "Inspecting publishing guidance."})
                    for _ in range(104 * scale - count)])
        await db.commit()
        await ensure_workspace(seed.agent_id, tenant_id=str(seed.tenant_id))
        counts = {}
        for row_type in (RuntimeTask, SessionToolInvocation, ChatTranscriptEvent, ContentInputReceipt):
            counts[row_type.__tablename__] = await db.scalar(select(func.count()).select_from(row_type).where(
                row_type.tenant_id == seed.tenant_id))
        return counts


async def test_allow_once_statement_budget_is_bounded_by_history_and_siblings(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.api.chat_sessions import ResolveSessionPermissionIn, resolve_session_permission
    from app.services import runtime_task_worker

    async def no_wakeup(**_kwargs):
        pass

    # Only the background worker notification is muted. Authority, lease,
    # governance, source checks, effect, settlement and continuation are real.
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", no_wakeup)
    observations = []
    for scale, pending in ((1, 1), (2, 1), (1, 2), (1, 3)):
        seed = await _seed_waiting_batch(owner_sessionmaker, tool_name="plaza_create_post",
            tool_arguments={"content": "[A2x] Approved local integration post."}, tool_count=pending)
        counts = await _history(seed, owner_sessionmaker, scale)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed.tenant_id)
            user = await db.get(User, seed.user_id)
            started = time.perf_counter()
            with _statements(monkeypatch) as (phases, callers):
                receipt = await resolve_session_permission(agent_id=seed.agent_id, session_id=seed.session_id,
                    permission_request_id=seed.permission_ids[0], body=ResolveSessionPermissionIn(action="allow_once"),
                    current_user=user, db=db)
            observation = {"scale": scale, "pending": pending, "history": counts,
                "statements": sum(phases.values()), "phases": dict(phases),
                "seconds": round(time.perf_counter() - started, 4),
                "callers": {f"{phase}/{caller}": count for (phase, caller), count in sorted(callers.items())}}
            observations.append(observation)
            print("A2X_RESOLVE", json.dumps(observation), flush=True)
            assert receipt["run_id"] == str(seed.run_id)
            assert receipt["run_status"] == ("resumable" if pending == 1 else "suspended")
            assert receipt["status"] == ("resolved" if pending == 1 else "waiting_for_sibling_permissions")
            invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
            assert invocation.result_event_id is not None
            assert invocation.effect_state == "effect_committed"
            assert await db.scalar(select(func.count()).select_from(PlazaPost).where(
                PlazaPost.tenant_id == seed.tenant_id, PlazaPost.author_id == seed.agent_id)) == 1
            assert phases["tool_effect"] == 1
            replay = await resolve_session_permission(agent_id=seed.agent_id, session_id=seed.session_id,
                permission_request_id=seed.permission_ids[0], body=ResolveSessionPermissionIn(action="allow_once"),
                current_user=user, db=db)
            assert replay["result_event_id"] == receipt["result_event_id"]
            assert await db.scalar(select(func.count()).select_from(PlazaPost).where(
                PlazaPost.tenant_id == seed.tenant_id, PlazaPost.author_id == seed.agent_id)) == 1
    assert observations[0]["statements"] <= 140, observations
    assert observations[1]["statements"] <= observations[0]["statements"] + 5, observations
    for phase in set(observations[2]["phases"]) | set(observations[3]["phases"]):
        assert observations[3]["phases"].get(phase, 0) <= observations[2]["phases"].get(phase, 0) + 3, observations


async def test_permission_sources_observe_owner_transfer_between_result_checks(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.services import content_input_receipts
    from app.services.delegation_permission_runtime import validate_permission_input_sources
    from tests.services.test_session_permission_runtime import _authority

    seed = await _seed_waiting_batch(owner_sessionmaker, tool_name="plaza_create_post",
        tool_arguments={"content": "[A2x] Source authority must stay current."})
    await _history(seed, owner_sessionmaker, 1)
    successor_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(User(id=successor_id, tenant_id=seed.tenant_id, username=f"successor-{successor_id.hex}",
            email=f"{successor_id.hex}@permission.test", password_hash="x", display_name="Successor"))
        await db.commit()
    original = content_input_receipts._validate_session_dependency
    checked = []

    async def transfer_after_first_check(*args, **kwargs):
        await original(*args, **kwargs)
        checked.append(args[2]["source_id"])
        if len(checked) == 1:
            async with owner_sessionmaker() as other:
                agent = await other.get(Agent, seed.agent_id)
                agent.owner_user_id = successor_id
                await other.commit()

    monkeypatch.setattr(content_input_receipts, "_validate_session_dependency", transfer_after_first_check)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        _user, authority = await _authority(db, seed)
        session = await db.get(ChatSession, seed.session_id)
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed.run_id))
        with pytest.raises(content_input_receipts.ContentInputReceiptError) as denied:
            await validate_permission_input_sources(db, authority=authority, session=session,
                invocation=invocation, model=model, execution={})
        assert denied.value.code == "content_source_access_denied"
        assert len(checked) == 1

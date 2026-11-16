"""Permission resolution keeps real tool execution and bounded PostgreSQL work."""

from datetime import datetime, timezone
import inspect
import json
import re
import time
import uuid

import pytest
from sqlalchemy import func, select

from app.database import pin_rls_tenant_context
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.organization_identity import ContentInputReceipt
from app.models.plaza import PlazaPost
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.models.user import User
from tests.integration._a2x_profile import permission_client, request_statements, save_inventory
from tests.services.test_session_permission_runtime import _seed_waiting_batch

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
RESOLVE_STATEMENT_CEILING = 184


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


@pytest.mark.parametrize("budget", ["ceiling", "history", "siblings"])
async def test_allow_once_statement_budget_is_bounded_by_history_and_siblings(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, _route_default_app_engine, budget,
):
    observations = []
    projection_violations = []
    for scale, pending in ((1, 1), (2, 1), (1, 2), (1, 3)):
        seed = await _seed_waiting_batch(owner_sessionmaker, tool_name="plaza_create_post",
            tool_arguments={"content": "[A2x] Approved local integration post."}, tool_count=pending)
        counts = await _history(seed, owner_sessionmaker, scale)
        async with permission_client(app_user_sessionmaker, seed, monkeypatch, _route_default_app_engine) as (client, app):
            started = time.perf_counter()
            url = f"/api/agents/{seed.agent_id}/sessions/{seed.session_id}/permissions/{seed.permission_ids[0]}/resolve"
            with request_statements(monkeypatch, app) as sample:
                response = await client.post(url, json={"action": "allow_once"})
            assert response.status_code == 200, response.text
            receipt = response.json()
            full_count = int(re.search(r'desc="(\d+)"', response.headers["server-timing"])[1])
            phases = sample["phases"]
            assert full_count == len(sample["sql"]) == sum(phases.values())
            assert sample["route_body"] == full_count - phases["authentication_and_middleware"]
            observation = {"budget": budget, "scale": scale, "pending": pending, "history": counts,
                "statements": full_count, "route_body": sample["route_body"], "phases": dict(phases),
                "seconds": round(time.perf_counter() - started, 4),
                "server_timing": response.headers["server-timing"]}
            observations.append(observation)
            save_inventory(f"{budget}_s{scale}_p{pending}", observation, sample)
            print("A2X_FULL_RESOLVE", json.dumps(observation), flush=True)
            if budget == "ceiling":
                for statement in sample["sql"]:
                    checkpoint = statement["checkpoint"]
                    if ("mark_tool_effect_started" in checkpoint or "_source_result#2" in checkpoint
                        or ("apply_permission_response_control_input" in checkpoint
                            and statement["stack"][0]["function"] == "_result_for_invocation")):
                        if "model_request_snapshot_json" in statement["sql"]:
                            projection_violations.append((checkpoint, statement["number"]))
                    if "complete_tool_invocation" in checkpoint:
                        if "session_model_results" in statement["tables"]:
                            projection_violations.append((checkpoint, statement["number"]))
            assert receipt["run_id"] == str(seed.run_id)
            assert receipt["run_status"] == ("resumable" if pending == 1 else "suspended")
            assert receipt["status"] == ("resolved" if pending == 1 else "waiting_for_sibling_permissions")
            replay = await client.post(url, json={"action": "allow_once"})
            assert replay.status_code == 200, replay.text
            assert replay.json()["result_event_id"] == receipt["result_event_id"]
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed.tenant_id)
            invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
            assert invocation.result_event_id is not None
            assert invocation.effect_state == "effect_committed"
            assert await db.scalar(select(func.count()).select_from(PlazaPost).where(
                PlazaPost.tenant_id == seed.tenant_id, PlazaPost.author_id == seed.agent_id)) == 1
            assert phases["tool_effect"] == 1
    if budget == "ceiling":
        assert observations[0]["statements"] <= RESOLVE_STATEMENT_CEILING, observations
        assert not projection_violations
    elif budget == "history":
        assert observations[1]["statements"] <= observations[0]["statements"] + 5, observations
    else:
        for previous, current in ((observations[0], observations[2]), (observations[2], observations[3])):
            for phase in set(previous["phases"]) | set(current["phases"]):
                assert current["phases"].get(phase, 0) <= previous["phases"].get(phase, 0) + 3, observations


@pytest.mark.parametrize("mutation", ["transfer", "revoke_member"])
@pytest.mark.parametrize("point,allows_effect", [
    ("pre_effect_sources", False),
    ("lease_before_sources", False),
    ("lease_after_source_failure", False),
    ("responsible_approver", False),
    ("effect_fence", True),
    ("fence_advisory", True),
    ("fence_invocation", True),
    ("fence_run_state", True),
    ("fence_model_binding", True),
    ("tool_extension_policy", True),
    ("tool_workspace_read", True),
    ("tool_workspace_inputs", True),
    ("plaza_hr", True),
    ("plaza_hr_tenant", True),
    ("plaza_hr_agent", True),
    ("plaza_effect_tenant", True),
    ("plaza_agent", True),
    ("plaza_insert", True),
    ("asset_usage", True),
    ("result_settlement", True),
    ("sibling_settlement", True),
    ("continuation", True),
])
async def test_distinct_permission_checkpoints_keep_baseline_authority(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, _route_default_app_engine,
    mutation, point, allows_effect,
):
    from app.models.agent import Agent
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
    from app.services import chat_transcript, content_input_receipts, delegation_permission_runtime
    from app.services import ai_assets, permission_execution, session_permission_runtime, session_tool_runtime
    from app.services import agent_tools, approval_service
    from app.services.agent_tool_domains import plaza
    from app.tools.service import ToolRuntimeService

    seed = await _seed_waiting_batch(owner_sessionmaker, tool_name="plaza_create_post",
        tool_arguments={"content": "[A2x] Source authority must stay current."})
    await _history(seed, owner_sessionmaker, 1)
    if point == "lease_after_source_failure":
        from app.services.session_model_inputs import _hash

        async with owner_sessionmaker() as db:
            model = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed.run_id))
            snapshot = dict(model.model_request_snapshot_json)
            bindings = dict(snapshot["input_bindings"])
            dependencies = [dict(row) for row in bindings["dependencies"]]
            dependencies[0]["source_version"] = "0" * 64
            snapshot["input_bindings"] = {**bindings, "dependencies": dependencies}
            model.model_request_snapshot_json, model.model_request_hash = snapshot, _hash(snapshot)
            await db.commit()
    successor_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(User(id=successor_id, tenant_id=seed.tenant_id, username=f"successor-{successor_id.hex}",
            email=f"{successor_id.hex}@permission.test", password_hash="x", display_name="Successor"))
        await db.commit()
    changed, sources_finished, source_attempted, lease_calls = False, False, False, 0
    reached, denied_sources, observed_owners = [], [], []
    approver_rows, approver_decisions, approver_errors = [], [], []
    lease_reads, lease_errors, replacement_connections = [], [], []
    lease_expected = {}
    deleted_marker = datetime(2026, 11, 16, tzinfo=timezone.utc)
    refreshed_email = f"fresh-{seed.user_id.hex}@permission.test"

    async def boundary(name):
        nonlocal changed
        scope = content_input_receipts._DISCLOSURE_SOURCE_VALIDATIONS.get()
        assert scope is None, (name, "A source observation escaped its checkpoint")
        reached.append(name)
        if name == point and not changed:
            changed = True
            async with owner_sessionmaker() as other:
                if mutation == "transfer":
                    agent = await other.get(Agent, seed.agent_id)
                    agent.owner_user_id = successor_id
                else:
                    member = await other.get(User, seed.user_id)
                    member.is_active = False
                if point == "responsible_approver":
                    (await other.get(Agent, seed.agent_id)).deleted_at = deleted_marker
                    (await other.get(User, seed.user_id)).email = refreshed_email
                await other.commit()

    def wrap(module, name, checkpoint, *, after_sources=False):
        original = getattr(module, name)

        async def observed(*args, **kwargs):
            if not after_sources or sources_finished:
                await boundary(checkpoint)
            return await original(*args, **kwargs)

        monkeypatch.setattr(module, name, observed)

    original_sources = delegation_permission_runtime.validate_permission_input_sources

    async def source_checkpoint(*args, **kwargs):
        nonlocal sources_finished, source_attempted
        source_attempted = True
        await boundary("pre_effect_sources")
        try:
            result = await original_sources(*args, **kwargs)
        except content_input_receipts.ContentInputReceiptError as exc:
            assert content_input_receipts._DISCLOSURE_SOURCE_VALIDATIONS.get() is None
            denied_sources.append(exc.code)
            raise
        sources_finished = True
        assert content_input_receipts._DISCLOSURE_SOURCE_VALIDATIONS.get() is None
        return result

    monkeypatch.setattr(delegation_permission_runtime, "validate_permission_input_sources", source_checkpoint)
    original_lease = permission_execution.PermissionExecutionLease.ensure_owned

    async def lease_owned(*args, **kwargs):
        nonlocal lease_calls
        name = ("lease_after_source_failure" if source_attempted else "lease_after_pre_effect_failure") \
            if lease_calls else "lease_before_sources"
        lease_calls += 1
        await boundary(name)
        lease = args[0]
        if name == point and point in {"lease_before_sources", "lease_after_source_failure"}:
            # A different physical connection cannot inherit the old lease.
            replacement = await lease.connection.engine.connect()
            replacement_connections.append(replacement)
            lease_expected[replacement] = (name, lease.pid)
            lease.connection = replacement
        try:
            value = await original_lease(*args, **kwargs)
        except permission_execution.PermissionExecutionUnavailable:
            lease_errors.append(name)
            raise
        if lease_calls == 1:
            await boundary("responsible_approver")
        return value

    monkeypatch.setattr(permission_execution.PermissionExecutionLease, "ensure_owned", lease_owned)
    connection_scalar = AsyncConnection.scalar

    async def lease_scalar(connection, statement, *args, **kwargs):
        value = await connection_scalar(connection, statement, *args, **kwargs)
        if connection in lease_expected and str(statement) == "SELECT pg_backend_pid()":
            name, expected = lease_expected[connection]
            lease_reads.append({"point": name, "expected_pid": expected, "observed_pid": value})
        return value

    monkeypatch.setattr(AsyncConnection, "scalar", lease_scalar)
    original_approver = approval_service.can_resolve_approval

    async def approver(db, *, agent, user, approval):
        own_point = inspect.currentframe().f_back.f_code.co_name == "_pre_effect"
        if own_point:
            approver_rows.append({"owner": str(agent.owner_user_id), "is_active": user.is_active,
                "deleted_at": agent.deleted_at, "email": user.email})
        decision = await original_approver(db, agent=agent, user=user, approval=approval)
        if own_point:
            approver_decisions.append(decision)
        return decision

    monkeypatch.setattr(approval_service, "can_resolve_approval", approver)
    original_tool = agent_tools.execute_session_permission_tool

    async def tool(*args, **kwargs):
        callback = kwargs["pre_effect_callback"]

        async def callback_observation(payload):
            try:
                return await callback(payload)
            except PermissionError as error:
                approver_errors.append(str(error))
                raise

        return await original_tool(*args, **{**kwargs, "pre_effect_callback": callback_observation})

    monkeypatch.setattr(agent_tools, "execute_session_permission_tool", tool)
    wrap(session_tool_runtime, "mark_tool_effect_started", "effect_fence")
    wrap(ToolRuntimeService, "_l2_extension_policy_block", "tool_extension_policy", after_sources=True)
    wrap(content_input_receipts, "authorize_workspace_receipt_read", "tool_workspace_read", after_sources=True)
    wrap(content_input_receipts, "authorize_workspace_execution_inputs", "tool_workspace_inputs", after_sources=True)
    wrap(plaza, "_is_system_hr", "plaza_hr", after_sources=True)
    original_tenant = plaza.resolve_tenant_for_agent

    async def plaza_tenant(*args, **kwargs):
        if sources_finished:
            name = "plaza_hr_tenant" if inspect.currentframe().f_back.f_code.co_name == "_is_system_hr" \
                else "plaza_effect_tenant"
            await boundary(name)
        return await original_tenant(*args, **kwargs)

    monkeypatch.setattr(plaza, "resolve_tenant_for_agent", plaza_tenant)
    wrap(ai_assets, "resolve_asset_ref", "asset_usage", after_sources=True)
    wrap(session_tool_runtime, "complete_tool_invocation", "result_settlement")
    wrap(session_permission_runtime, "_settle_unstarted_batch_siblings", "sibling_settlement")
    wrap(session_permission_runtime, "_queue_same_run_continuation", "continuation")
    original_lock = chat_transcript.lock_transcript_session

    async def lock(*args, **kwargs):
        if inspect.currentframe().f_back.f_code.co_name == "mark_tool_effect_started":
            await boundary("fence_advisory")
        return await original_lock(*args, **kwargs)

    monkeypatch.setattr(chat_transcript, "lock_transcript_session", lock)
    original_scalar, original_execute = AsyncSession.scalar, AsyncSession.execute
    original_commit = AsyncSession.commit

    async def scalar(self, statement, *args, **kwargs):
        caller = inspect.currentframe().f_back.f_code.co_name
        if sources_finished and caller == "mark_tool_effect_started":
            entity = statement.column_descriptions[0]["entity"]
            await boundary("fence_invocation" if entity is SessionToolInvocation else "fence_run_state")
        elif sources_finished and caller == "_result_for_invocation":
            await boundary("fence_model_binding")
        return await original_scalar(self, statement, *args, **kwargs)

    async def execute(self, statement, *args, **kwargs):
        caller = inspect.currentframe().f_back.f_code.co_name
        if sources_finished and caller == "_is_system_hr":
            await boundary("plaza_hr_agent")
        is_agent = (sources_finished and caller == "_plaza_create_post"
            and statement.column_descriptions[0]["entity"] is Agent)
        if is_agent:
            await boundary("plaza_agent")
        result = await original_execute(self, statement, *args, **kwargs)
        if is_agent:
            frozen = result.freeze()
            observed_owners.extend(str(agent.owner_user_id) for agent in frozen.data)
            return frozen()
        return result

    async def commit(self):
        if any(isinstance(row, PlazaPost) for row in self.new):
            await boundary("plaza_insert")
        return await original_commit(self)

    monkeypatch.setattr(AsyncSession, "scalar", scalar)
    monkeypatch.setattr(AsyncSession, "execute", execute)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    try:
        async with permission_client(app_user_sessionmaker, seed, monkeypatch, _route_default_app_engine) as (client, _app):
            response = await client.post(
                f"/api/agents/{seed.agent_id}/sessions/{seed.session_id}/permissions/{seed.permission_ids[0]}/resolve",
                json={"action": "allow_once"})
    finally:
        for connection in replacement_connections:
            await connection.close()
    lease_point = point in {"lease_before_sources", "lease_after_source_failure"}
    if lease_point:
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "permission_execution_unavailable"
        assert point in lease_errors and len(lease_reads) == 1, (lease_errors, lease_reads)
        assert lease_reads[0]["observed_pid"] != lease_reads[0]["expected_pid"]
        assert "effect_fence" not in reached and "result_settlement" not in reached
        if point == "lease_before_sources":
            assert not source_attempted and not approver_rows
        else:
            assert source_attempted and denied_sources and lease_calls == 2
        async with owner_sessionmaker() as db:
            invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
            assert invocation.effect_state == "prepared_not_started" and invocation.result_event_id is None
            assert await db.scalar(select(func.count()).select_from(PlazaPost).where(PlazaPost.tenant_id == seed.tenant_id)) == 0
        print("A2X_BOUNDARY", json.dumps({"point": point, "mutation": mutation, "http": 503,
            "own_lease_refusal": lease_errors, "lease_pid_readback": lease_reads,
            "checkpoints_without_source_scope": reached}), flush=True)
        return
    assert response.status_code == 200, response.text
    assert changed and point in reached, reached
    async with owner_sessionmaker() as db:
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
        event_row = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        posts = await db.scalar(select(func.count()).select_from(PlazaPost).where(PlazaPost.tenant_id == seed.tenant_id))
        observation = {"point": point, "mutation": mutation, "posts": posts,
            "effect_state": invocation.effect_state, "denied_sources": denied_sources,
            "checkpoints_without_source_scope": reached, "run_status": response.json()["run_status"]}
        print("A2X_BOUNDARY", json.dumps(observation), flush=True)
        assert posts == int(allows_effect), observation
        assert invocation.effect_state == ("effect_committed" if allows_effect else "prepared_not_started")
        assert response.json()["run_id"] == str(seed.run_id)
        assert response.json()["run_status"] == "resumable"
        assert invocation.result_event_id is not None
        if point == "pre_effect_sources":
            assert denied_sources == ["content_source_access_denied"]
            assert not sources_finished and "effect_fence" not in reached
        if point == "responsible_approver":
            assert approver_decisions == [False]
            assert approver_errors == ["Only the responsible approver can decide this action"]
            assert not source_attempted and not denied_sources and "effect_fence" not in reached
            assert approver_rows == [{"owner": str(successor_id if mutation == "transfer" else seed.user_id),
                "is_active": mutation != "revoke_member", "deleted_at": deleted_marker, "email": refreshed_email}]
            print("A2X_APPROVER_OBSERVATION", json.dumps(approver_rows, default=str), flush=True)
        if not allows_effect:
            assert json.loads(event_row.metadata_json["v2_payload"]["content"])["status"] == "failed"
        if point == "plaza_agent":
            assert observed_owners == [str(successor_id if mutation == "transfer" else seed.user_id)]

"""Keep legacy source refusals and delegated approval checkpoints bounded."""

import json
import uuid
import os
from pathlib import Path
import re
import sys

import pytest
from sqlalchemy import func, select, text, update

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.plaza import PlazaPost
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from tests.integration._a2x_profile import permission_client, request_statements, save_inventory
from tests.integration.test_a2x_permission_statement_budget_postgres import _history
from tests.services.test_session_permission_runtime import _seed_waiting_batch

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
DELEGATED_RESOLVE_STATEMENT_CEILING = 198


async def _legacy_oversized_source(owner, seed):
    from app.services.session_model_inputs import _hash, tool_result_source_hash
    from app.tools.output_limits import TOOL_OUTPUT_BYTES

    async with owner() as db:
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed.run_id))
        snapshot = dict(model.model_request_snapshot_json)
        bindings = dict(snapshot["input_bindings"])
        dependencies = [dict(row) for row in bindings["dependencies"]]
        source = await db.get(SessionToolInvocation, uuid.UUID(dependencies[0]["source_id"]))
        metadata = dict((await db.execute(text("SELECT metadata_json FROM chat_transcript_events WHERE id=:id"),
            {"id": source.result_event_id})).scalar_one())
        payload = dict(metadata["v2_payload"])
        content = "x" * (TOOL_OUTPUT_BYTES + 1024)
        payload.update(content=content, content_hash=_hash(content))
        # Legacy data predates bound_tool_output: write the stored bytes directly.
        metadata["v2_payload"] = payload
        await db.execute(update(ChatTranscriptEvent).where(ChatTranscriptEvent.id == source.result_event_id)
            .values(content=content, metadata_json=metadata, content_hash=_hash(payload)))
        dependencies[0]["source_version"] = tool_result_source_hash(payload)
        snapshot["input_bindings"] = {**bindings, "dependencies": dependencies}
        model.model_request_snapshot_json, model.model_request_hash = snapshot, _hash(snapshot)
        await db.commit()
        assert await db.scalar(text("SELECT octet_length(content) FROM chat_transcript_events WHERE id=:id"),
            {"id": source.result_event_id}) > TOOL_OUTPUT_BYTES


async def test_legacy_oversized_tool_result_keeps_base_refusal(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, _route_default_app_engine,
):
    from app.services import content_input_receipts, delegation_permission_runtime

    seed = await _seed_waiting_batch(owner_sessionmaker, tool_name="plaza_create_post",
        tool_arguments={"content": "[A2x] A legacy oversized source must remain refused."})
    await _history(seed, owner_sessionmaker, 1)
    await _legacy_oversized_source(owner_sessionmaker, seed)
    refused = []
    original = delegation_permission_runtime.validate_permission_input_sources

    async def sources(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except content_input_receipts.ContentInputReceiptError as error:
            refused.append((error.code, error.status_code))
            raise

    monkeypatch.setattr(delegation_permission_runtime, "validate_permission_input_sources", sources)
    async with permission_client(app_user_sessionmaker, seed, monkeypatch, _route_default_app_engine) as (client, _app):
        response = await client.post(
            f"/api/agents/{seed.agent_id}/sessions/{seed.session_id}/permissions/{seed.permission_ids[0]}/resolve",
            json={"action": "allow_once"})
    assert response.status_code == 200, response.text
    async with owner_sessionmaker() as db:
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
        posts = await db.scalar(select(func.count()).select_from(PlazaPost).where(PlazaPost.tenant_id == seed.tenant_id))
        event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        member_content = event.metadata_json["v2_payload"]["content"]
        outcome = {"refused": refused, "http": response.status_code, "posts": posts,
            "effect_state": invocation.effect_state, "member_content": member_content,
            "receipt_status": response.json()["status"], "run_status": response.json()["run_status"]}
        print("A2X3_LEGACY", json.dumps(outcome), flush=True)
        assert refused == [("content_source_changed", 403)], outcome
        assert posts == 0 and invocation.effect_state == "prepared_not_started", outcome
        result = json.loads(member_content)
        assert result["status"] == "failed", outcome
        assert response.json()["run_status"] == "resumable", outcome


async def _delegation(owner, seed):
    from types import SimpleNamespace
    from app.agents.orchestrator import AgentDelegationRequest, _delegation_authority_snapshot
    from app.core.execution_context import ExecutionIdentity, ExecutionPrincipal
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.runtime.ccplus_contracts import build_permission_profile, permission_profile_snapshot_hash
    from app.services.session_model_inputs import _hash

    parent_id, parent_session_id, parent_run_id = (uuid.uuid4() for _ in range(3))
    async with owner() as db:
        parent = Agent(id=parent_id, tenant_id=seed.tenant_id, name="Delegating assistant",
            creator_id=seed.user_id, owner_user_id=seed.user_id)
        db.add(parent)
        await db.flush()
        db.add(ChatSession(id=parent_session_id, tenant_id=seed.tenant_id, agent_id=parent_id,
            user_id=seed.user_id, source_channel="web"))
        db.add(RuntimeTask(id=parent_run_id, tenant_id=seed.tenant_id, task_type="web_chat_turn",
            status="completed", parent_agent_id=parent_id, child_agent_id=parent_id,
            parent_session_id=str(parent_session_id), child_session_id=str(parent_session_id),
            root_user_id=seed.user_id, root_session_id=str(parent_session_id), root_runtime_task_id=parent_run_id,
            prompt="Delegate the approved company plaza publication."))
        await db.flush()
        profile = build_permission_profile({})
        request = AgentDelegationRequest(target=SimpleNamespace(id=seed.agent_id, tenant_id=seed.tenant_id),
            target_model=None, conversation_messages=[], owner_id=seed.user_id, session_id=str(seed.session_id),
            parent_agent_id=parent_id, parent_session_id=str(parent_session_id), tenant_id=seed.tenant_id,
            trace_id=f"delegation:{seed.run_id}",
            runtime_task_id=str(seed.run_id), root_runtime_task_id=str(parent_run_id), budget_run_id=str(parent_run_id),
            permission_profile=profile, execution_identity=ExecutionIdentity("delegated_user", seed.user_id, "Member"),
            execution_principal=ExecutionPrincipal(tenant_id=seed.tenant_id, source_agent_id=parent_id,
                requester_user_id=seed.user_id, root_session_id=str(parent_session_id),
                root_runtime_task_id=str(parent_run_id)).to_evidence(), parent_agent_name=parent.name)
        snapshot = _delegation_authority_snapshot(request)
        task = await db.get(RuntimeTask, seed.run_id)
        task.task_type, task.parent_agent_id = "delegation", parent_id
        task.parent_session_id, task.root_session_id = str(parent_session_id), str(parent_session_id)
        task.root_runtime_task_id = parent_run_id
        task.metadata_json = {**task.metadata_json, "authority_snapshot": snapshot,
            "authority_snapshot_hash": _hash(snapshot), "authority_policy_hash": permission_profile_snapshot_hash(profile),
            "permission_profile": snapshot["permission_profile"], "parent_agent_name": parent.name,
            "tool_profile": "worker_safe", "execution_receipt": {"capability_snapshot_hash": _hash(snapshot)}}
        session = await db.get(ChatSession, seed.session_id)
        session.source_channel, session.session_kind, session.runtime_source = "agent", "delegation_run", "delegation"
        session.actor_type, session.visibility_scope = "agent", "agent_owner"
        session.peer_agent_id, session.parent_session_id, session.root_session_id = parent_id, parent_session_id, parent_session_id
        session.runtime_task_id = seed.run_id
        await db.commit()


@pytest.mark.parametrize("revoked", [False, True])
async def test_delegation_resolve_history_and_source_checkpoint(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, _route_default_app_engine, revoked,
):
    from app.models.user import User
    from app.services import content_input_receipts, delegation_permission_runtime

    observations = []
    for scale in (1, 2) if not revoked else (1,):
        seed = await _seed_waiting_batch(owner_sessionmaker, tool_name="plaza_create_post",
            tool_arguments={"content": "[A2x] Delegated approved publication."})
        await _history(seed, owner_sessionmaker, scale)
        await _delegation(owner_sessionmaker, seed)
        original = delegation_permission_runtime.validate_permission_input_sources
        seen, denied = [], []

        async def sources(*args, **kwargs):
            assert kwargs["execution"], "The test must reach the delegated ToolExecutionRequest branch"
            seen.append(True)
            if revoked:
                await original(*args, **kwargs)
                assert content_input_receipts._DISCLOSURE_SOURCE_VALIDATIONS.get() is None
                async with owner_sessionmaker() as other:
                    member = await other.get(User, seed.user_id)
                    member.is_active = False
                    await other.commit()
            try:
                return await original(*args, **kwargs)
            except content_input_receipts.ContentInputReceiptError as error:
                denied.append((error.code, error.status_code))
                raise

        with monkeypatch.context() as patch:
            patch.setattr(delegation_permission_runtime, "validate_permission_input_sources", sources)
            async with permission_client(app_user_sessionmaker, seed, patch, _route_default_app_engine) as (client, app):
                with request_statements(patch, app) as sample:
                    response = await client.post(
                        f"/api/agents/{seed.agent_id}/sessions/{seed.session_id}/permissions/{seed.permission_ids[0]}/resolve",
                        json={"action": "allow_once"})
        assert response.status_code == 200, response.text
        async with owner_sessionmaker() as db:
            invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
            posts = await db.scalar(select(func.count()).select_from(PlazaPost).where(PlazaPost.tenant_id == seed.tenant_id))
            full_request = int(re.search(r'desc="(\d+)"', response.headers["server-timing"])[1])
            assert full_request == sum(sample["phases"].values())
            assert sample["route_body"] == full_request - sample["phases"]["authentication_and_middleware"]
            result = {"scale": scale, "revoked": revoked, "statements": full_request, "route_body": sample["route_body"],
                "phases": dict(sample["phases"]), "seen": seen, "denied": denied, "posts": posts,
                "effect_state": invocation.effect_state, "receipt": response.json()}
            print("A2X3_DELEGATION", json.dumps(result), flush=True)
            save_inventory(f"delegation_s{scale}_revoked{int(revoked)}", result, sample)
            assert seen == [True], result
            assert posts == int(not revoked), result
            if revoked:
                assert denied == [("content_source_access_denied", 403)], result
            else:
                assert invocation.effect_state == "effect_committed", result
            observations.append(result)
    if not revoked:
        assert observations[1]["statements"] <= observations[0]["statements"] + 5, observations
        assert max(row["statements"] for row in observations) <= DELEGATED_RESOLVE_STATEMENT_CEILING, observations


@pytest.mark.parametrize("delegated", [False, True])
async def test_model_projections_measure_received_snapshot_bytes(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, _route_default_app_engine, delegated,
):
    from tests.integration._a2x_bytes import full_model_rows, snapshot_wire_bytes

    observations = []
    for scale in (1, 2):
        modes = []
        for projected in (False, True):
            seed = await _seed_waiting_batch(owner_sessionmaker, tool_name="plaza_create_post",
                tool_arguments={"content": "[A2x] Measure actual received snapshot bytes."})
            await _history(seed, owner_sessionmaker, scale)
            if delegated:
                await _delegation(owner_sessionmaker, seed)
            with monkeypatch.context() as patch:
                if not projected:
                    full_model_rows(patch)
                async with permission_client(app_user_sessionmaker, seed, patch, _route_default_app_engine) as (client, app):
                    with request_statements(patch, app) as statements, snapshot_wire_bytes(patch) as payload:
                        response = await client.post(
                            f"/api/agents/{seed.agent_id}/sessions/{seed.session_id}/permissions/{seed.permission_ids[0]}/resolve",
                            json={"action": "allow_once"})
                assert response.status_code == 200, response.text
                async with owner_sessionmaker() as db:
                    invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
                    assert invocation.effect_state == "effect_committed"
                    assert await db.scalar(select(func.count()).select_from(PlazaPost)
                        .where(PlazaPost.tenant_id == seed.tenant_id)) == 1
                count = int(re.search(r'desc="(\d+)"', response.headers["server-timing"])[1])
                observation = {"scale": scale, "delegated": delegated, "projected": projected, "statements": count, **payload}
                print("A2X3_BYTES", json.dumps(observation), flush=True)
                assert payload["snapshot_jsonb_bytes"] > 0 and payload["snapshot_cells"] > 0
                modes.append(observation)
        assert modes[0]["statements"] == modes[1]["statements"], modes
        assert modes[0]["snapshot_jsonb_bytes"] >= modes[1]["snapshot_jsonb_bytes"], modes
        observations.append({"scale": scale, "bytes_saved": modes[0]["snapshot_jsonb_bytes"] - modes[1]["snapshot_jsonb_bytes"],
            "unprojected": modes[0], "projected": modes[1]})
    if os.environ.get("FULL_RUN"):
        (Path(os.environ["FULL_RUN"]) / "evidence" / f"{sys.argv[1]}_snapshot_bytes_delegated{int(delegated)}.json").write_text(json.dumps(observations, indent=2))

"""Context-resource reads retain their actual source in the next native round."""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from tests.integration import test_unverified_t2_recovery_postgres as t2_support
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
t2_work = t2_support.t2_work


@pytest.mark.parametrize("ref", ["channels", "a2a-collaborators", "organization"])
async def test_native_context_page_retains_complete_source(
    t2_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, ref,
):
    import app.runtime.invoker as invoker
    from app.database import pin_rls_tenant_context
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run

    s = t2_work
    client = ScriptedLLMClient([
        {"tool_calls": [{"id": "read-native-context", "type": "function", "function": {
            "name": "read_context_resource", "arguments": json.dumps({"ref": ref})}}], "finish_reason": "tool_calls"},
        {"content": "360", "finish_reason": "stop"},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, s["run_id"])
        run.claim_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        await db.commit()
    fence = set_runtime_task_fence(task_id=s["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(s["run_id"])
    finally:
        reset_runtime_task_fence(fence)
    assert len(client.requests) == 2
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s["tenant_id"])
        models = list(await db.scalars(select(SessionModelResult).where(
            SessionModelResult.run_id == s["run_id"]).order_by(SessionModelResult.round_id)))
        calls = list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == s["run_id"])))
        assert len(models) == 2 and len(calls) == 1
        assert calls[0].tool_name == "read_context_resource" and calls[0].effect_state == "effect_committed"
        bindings = [model.model_request_snapshot_json["input_bindings"] for model in models]
        (s["agent_root"].parent / "native-context-bindings.json").write_text(json.dumps(bindings, indent=2))
        assert bindings[0]["dependency_coverage"]["complete"] is True
        assert bindings[1]["dependency_coverage"]["complete"] is True, bindings[1]


@pytest.mark.parametrize("relation", ["same_owner", "public", "group"])
async def test_directory_observation_preserves_bytes_and_rechecks_authority(
    t2_work, owner_sessionmaker, app_user_sessionmaker, relation,
):
    from dataclasses import replace
    from types import SimpleNamespace
    from uuid import uuid4

    from app.database import pin_rls_tenant_context
    from app.models.agent import Agent, AgentPermission
    from app.models.agent_collaboration import AgentCollaborationGroup, AgentCollaborationGroupMember
    from app.models.chat_session import ChatSession
    from app.models.invocation_span import InvocationSpan
    from app.models.user import User
    from app.services.agent_directory_sources import (
        directory_page_sources, observe_directory, owned_directory_source,
    )
    from app.services.content_input_receipts import (
        ContentInputReceiptError, _validate_input_dependencies, validate_runtime_input_dependencies,
    )
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    s = t2_work
    other, peer, group, membership, permission = (uuid4() for _ in range(5))
    async with owner_sessionmaker() as db:
        db.add(User(id=other, tenant_id=s["tenant_id"], username=f"directory-{other.hex}",
            email=f"{other.hex}@fixture.test", password_hash="fixture", display_name="Other directory owner", role="member"))
        await db.flush()
        peer_owner = s["user_id"] if relation == "same_owner" else other
        db.add(Agent(id=peer, tenant_id=s["tenant_id"], creator_id=peer_owner, owner_user_id=peer_owner,
            name="Observed colleague", status="idle"))
        await db.flush()
        if relation == "public":
            db.add(AgentPermission(id=permission, agent_id=peer, tenant_id=s["tenant_id"], scope_type="company"))
        if relation == "group":
            db.add(AgentCollaborationGroup(id=group, tenant_id=s["tenant_id"],
                name="Observed collaboration", created_by_user_id=s["user_id"]))
            await db.flush()
            for target, owner in [(s["agent_id"], s["user_id"]), (peer, other)]:
                db.add(AgentCollaborationGroupMember(id=membership if target == peer else uuid4(),
                    tenant_id=s["tenant_id"], group_id=group, agent_id=target,
                    agent_owner_user_id=owner, status="active"))
        await db.commit()
    principal = AgentRuntimePrincipal(agent_id=s["agent_id"], requester_user_id=s["user_id"],
        session_id=str(s["session_id"]), runtime_task_id=str(s["run_id"]))
    request = ToolExecutionRequest(tool_name="create_employee_from_request", arguments={},
        context=ToolExecutionContext(agent_id=s["agent_id"], user_id=s["user_id"],
            tenant_id=str(s["tenant_id"]), session_id=str(s["session_id"]),
            runtime_task_id=str(s["run_id"]), workspace=s["agent_root"]))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s["tenant_id"])
        content, source = await observe_directory(db, tenant_id=s["tenant_id"], agent_id=s["agent_id"],
            runtime_request=principal)
        await db.commit()
        await pin_rls_tenant_context(db, s["tenant_id"])
        retained = [await db.get(Agent, peer), await db.get(ChatSession, s["session_id"]),
            await db.get(InvocationSpan, UUID(source["source_id"]))]
        assert all(retained) and "Observed colleague" in content
        await validate_runtime_input_dependencies(db, request, [source])
        assert await owned_directory_source(db, dependency=source, agent_id=s["agent_id"],
            tenant_id=s["tenant_id"], session_id=s["session_id"], user_id=s["user_id"]) is (relation == "same_owner")
        for altered in [dict(source, source_id=str(uuid4())), dict(source, source_version="0" * 64)]:
            with pytest.raises(ContentInputReceiptError):
                await validate_runtime_input_dependencies(db, request, [altered])
        for altered in [replace(principal, requester_user_id=other), replace(principal, agent_id=peer)]:
            with pytest.raises(ContentInputReceiptError):
                await _validate_input_dependencies(db, s["tenant_id"], [source], runtime_request=altered)
        # Exact pages are checked against the immutable observation, including later offsets.
        call = SimpleNamespace(tool_name="read_context_resource", tenant_id=s["tenant_id"],
            session_id=s["session_id"], run_id=s["run_id"], provider_arguments_json={})
        import hashlib
        version = hashlib.sha256(content.encode()).hexdigest()
        for offset in range(0, len(content), 256):
            end = min(offset + 256, len(content))
            call.effective_arguments_json = {"ref": "a2a-collaborators", "offset": offset,
                "limit": 256, "expected_sha256": version}
            page = {"schema": "hive.agent_context_resource_page.v1", "status": "ok", "ref": "a2a-collaborators",
                "context_ref": "agent-context://a2a-collaborators", "source_ref": "agent-context://a2a-collaborators",
                "sha256": version, "offset": offset, "next_offset": end, "complete": end == len(content),
                "total_chars": len(content), "content": content[offset:end]}
            assert await directory_page_sources(db, agent_id=s["agent_id"], invocation=call,
                content=json.dumps(page), sources=[source]) == [source]
            with pytest.raises(ContentInputReceiptError):
                await directory_page_sources(db, agent_id=s["agent_id"], invocation=call,
                    content=json.dumps(dict(page, content="Forged page")), sources=[source])
        # A new employee and a later label do not rewrite a historical observation.
        async with owner_sessionmaker() as writer:
            writer.add(Agent(id=uuid4(), tenant_id=s["tenant_id"], creator_id=s["user_id"],
                owner_user_id=s["user_id"], name="Later colleague", status="idle"))
            (await writer.get(Agent, peer)).name = "Later label"
            await writer.commit()
        await validate_runtime_input_dependencies(db, request, [source])
        assert retained[-1].metadata_json["agent_directory_observation"]["content_sha256"] == version
        changes = [(User, s["user_id"], "is_active", False, True),
            (ChatSession, s["session_id"], "audience_kind", "group", "private"),
            (ChatSession, s["session_id"], "user_id", other, s["user_id"]),
            (Agent, s["agent_id"], "owner_user_id", other, s["user_id"]),
            (Agent, peer, "deleted_at", datetime.now(UTC), None),
            (Agent, peer, "deactivated_at", datetime.now(UTC), None)]
        if relation == "public":
            changes.append((AgentPermission, permission, "scope_type", "user", "company"))
        if relation == "group":
            changes.append((AgentCollaborationGroupMember, membership, "status", "revoked", "active"))
        for model, row_id, field, revoked, restored in changes:
            async with owner_sessionmaker() as writer:
                setattr(await writer.get(model, row_id), field, revoked)
                await writer.commit()
            with pytest.raises(ContentInputReceiptError):
                await validate_runtime_input_dependencies(db, request, [source])
            async with owner_sessionmaker() as writer:
                setattr(await writer.get(model, row_id), field, restored)
                await writer.commit()
            await validate_runtime_input_dependencies(db, request, [source])

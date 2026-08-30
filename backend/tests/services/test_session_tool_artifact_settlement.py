"""Real-PostgreSQL regression for canonical tool-result artifact settlement.

The production failure this covers performed a ``write_file`` effect, then
inserted ``ChatArtifact`` against a synthetic ``message_id`` that had no
``ChatMessage`` parent. PostgreSQL rejected the FK, the transaction lost the
tool terminal receipt, and the kernel nevertheless entered another provider
round. These tests keep the FK, artifact row, Session V2 result, and
reconciliation fence on the production-shaped path.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import func, select


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed(owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id, user_id, agent_id, session_id = (uuid.uuid4() for _ in range(4))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Tool Artifact Tenant", slug=f"tool-artifact-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"tool-artifact-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@tool-artifact.test",
                password_hash="x",
                display_name="Tool Artifact Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Tool Artifact Agent", creator_id=user_id))
        await db.flush()
        db.add(
            ChatSession(
                id=session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                session_kind="human_chat",
                runtime_source="web_chat",
            )
        )
        await db.commit()
    return tenant_id, user_id, agent_id, session_id


async def _prepare_live_round(
    owner_sessionmaker,
    *,
    tenant_id,
    user_id,
    agent_id,
    session_id,
):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.services.session_live_input import submit_live_human_input
    from app.services.session_model_round import bind_round_inputs, prepare_model_request

    input_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        user = await db.get(User, user_id)
        session = await db.get(ChatSession, session_id)
        receipt = await submit_live_human_input(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content="write the production-shaped report",
            source="web",
            input_id=input_id,
            idempotency_key=f"live:{input_id}",
        )
        await db.commit()
    run = receipt["run"]
    run_id = uuid.UUID(str(run["run_id"]))
    turn_id = str(run["turn_id"])
    async with owner_sessionmaker() as db:
        await bind_round_inputs(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
        )
        provider_request_id = await prepare_model_request(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            messages=[{"role": "user", "content": "write the production-shaped report"}],
            tools=None,
            provider="openai",
            model="gpt-test",
            wire_request={"messages": [], "tools": []},
            attempt_owner="tool-artifact-settlement-test",
        )
        await db.commit()
    return run_id, provider_request_id


def _execution_evidence(args_hash: str, provider_tool_use_id: str) -> dict:
    return {
        "schema": "hive.tool_execution_evidence.v1",
        "status": "settled",
        "retryable": False,
        "tool_decision": {
            "schema": "hive.tool_decision.v1",
            "decision_id": f"decision-{provider_tool_use_id}",
            "outcome": "allow",
            "input_hash": args_hash,
            "policy_snapshot_hash": "a" * 64,
            "capability_snapshot_hash": "b" * 64,
        },
        "execution_frame": {"status": "completed", "output_hash": "c" * 64},
    }


def _patch_runtime_database(monkeypatch, owner_sessionmaker, tenant_id):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime

    @asynccontextmanager
    async def scoped_session(_tenant_id):
        async with owner_sessionmaker() as db:
            yield db

    async def resolve_tenant(_agent_id):
        return tenant_id

    monkeypatch.setattr(runtime, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", resolve_tenant)


async def _start_effect(
    owner_sessionmaker,
    *,
    tenant_id,
    user_id,
    agent_id,
    session_id,
    run_id,
    provider_request_id,
    provider_tool_use_id,
    path,
):
    from app.models.session_v2 import SessionToolInvocation
    from app.services import web_chat_runtime as runtime

    base = {
        "name": "write_file",
        "args": {"path": path, "content": "production-shaped artifact\n"},
        "tool_call_id": provider_tool_use_id,
        "runtime_task_id": str(run_id),
        "provider_request_id": provider_request_id,
    }
    await runtime._persist_tool_call(
        agent_id=agent_id,
        user_id=user_id,
        session_id=str(session_id),
        data={**base, "status": "running"},
    )
    await runtime._persist_tool_call(
        agent_id=agent_id,
        user_id=user_id,
        session_id=str(session_id),
        data={**base, "status": "effect_started"},
    )
    async with owner_sessionmaker() as db:
        invocation = await db.scalar(
            select(SessionToolInvocation).where(
                SessionToolInvocation.tenant_id == tenant_id,
                SessionToolInvocation.run_id == run_id,
                SessionToolInvocation.provider_tool_use_id == provider_tool_use_id,
            )
        )
        assert invocation is not None
        return base, invocation.id, invocation.args_hash


@pytest.mark.asyncio
async def test_write_file_artifact_and_v2_tool_result_commit_against_real_fk(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.config import get_settings
    from app.models.audit import ChatMessage
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionEventOutbox, SessionToolInvocation
    from app.services import web_chat_runtime as runtime

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    run_id, provider_request_id = await _prepare_live_round(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    _patch_runtime_database(monkeypatch, owner_sessionmaker, tenant_id)
    rel_path = "workspace/report.md"
    target = Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("production-shaped artifact\n", encoding="utf-8")
    provider_tool_use_id = "provider-write-artifact"
    base, invocation_id, args_hash = await _start_effect(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        provider_request_id=provider_request_id,
        provider_tool_use_id=provider_tool_use_id,
        path=rel_path,
    )

    envelopes = await runtime._persist_tool_call(
        agent_id=agent_id,
        user_id=user_id,
        session_id=str(session_id),
        data={
            **base,
            "status": "done",
            "result": "Wrote workspace/report.md",
            "tool_execution_evidence": _execution_evidence(args_hash, provider_tool_use_id),
        },
    )
    target.unlink()
    replayed_envelopes = await runtime._persist_tool_call(
        agent_id=agent_id,
        user_id=user_id,
        session_id=str(session_id),
        data={
            **base,
            "status": "done",
            "result": "Wrote workspace/report.md",
            "tool_execution_evidence": _execution_evidence(args_hash, provider_tool_use_id),
        },
    )

    anchor_id = uuid.uuid5(invocation_id, "tool-result-artifact-message")
    async with owner_sessionmaker() as db:
        anchor = await db.get(ChatMessage, anchor_id)
        artifact = await db.scalar(
            select(ChatArtifact).where(
                ChatArtifact.message_id == anchor_id,
                ChatArtifact.runtime_task_id == run_id,
                ChatArtifact.path == rel_path,
            )
        )
        invocation = await db.get(SessionToolInvocation, invocation_id)
        result_event = await db.scalar(
            select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.invocation_id == invocation_id,
                ChatTranscriptEvent.item_kind == "tool_result",
                ChatTranscriptEvent.lifecycle == "completed",
            )
        )
        outbox = await db.scalar(select(SessionEventOutbox).where(SessionEventOutbox.event_id == result_event.id))
        anchor_count = await db.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.id == anchor_id))
        artifact_count = await db.scalar(
            select(func.count())
            .select_from(ChatArtifact)
            .where(
                ChatArtifact.runtime_task_id == run_id,
                ChatArtifact.path == rel_path,
            )
        )

    assert anchor is not None
    assert anchor.agent_id == agent_id
    assert anchor.tenant_id == tenant_id
    assert anchor.user_id == user_id
    assert anchor.role == "tool_call"
    assert anchor.conversation_id == str(session_id)
    assert artifact is not None
    assert invocation.effect_state == "effect_committed"
    assert invocation.result_event_id == result_event.id
    assert result_event.message_id == anchor_id
    assert len(result_event.parts_json) == 1
    assert result_event.parts_json[0]["type"] == "artifact"
    assert result_event.parts_json[0]["artifact_id"] == str(artifact.id)
    assert outbox.envelope_json["message_id"] == str(anchor_id)
    assert envelopes[-1]["message_id"] == str(anchor_id)
    assert envelopes[-1]["payload"]["parts"][0]["artifact_id"] == str(artifact.id)
    assert [envelope["event_id"] for envelope in replayed_envelopes] == [envelope["event_id"] for envelope in envelopes]
    assert anchor_count == 1
    assert artifact_count == 1


@pytest.mark.asyncio
async def test_artifact_settlement_failure_quarantines_effect_for_reconciliation(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionToolInvocation
    from app.services import web_chat_runtime as runtime

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    run_id, provider_request_id = await _prepare_live_round(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    _patch_runtime_database(monkeypatch, owner_sessionmaker, tenant_id)
    rel_path = "workspace/report.md"
    provider_tool_use_id = "provider-write-failed-settlement"
    base, invocation_id, args_hash = await _start_effect(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        provider_request_id=provider_request_id,
        provider_tool_use_id=provider_tool_use_id,
        path=rel_path,
    )

    async def fail_artifact_insert(**_kwargs):
        raise RuntimeError("simulated artifact settlement failure")

    monkeypatch.setattr(runtime, "create_chat_artifacts_for_message", fail_artifact_insert)
    with pytest.raises(RuntimeError, match="simulated artifact settlement failure"):
        await runtime._persist_tool_call(
            agent_id=agent_id,
            user_id=user_id,
            session_id=str(session_id),
            data={
                **base,
                "status": "done",
                "result": "Wrote workspace/report.md",
                "tool_execution_evidence": _execution_evidence(args_hash, provider_tool_use_id),
            },
        )

    async with owner_sessionmaker() as db:
        invocation = await db.get(SessionToolInvocation, invocation_id)
        reconciliation = await db.scalar(
            select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.invocation_id == invocation_id,
                ChatTranscriptEvent.item_kind == "tool_call",
                ChatTranscriptEvent.lifecycle == "needs_reconciliation",
            )
        )

    assert invocation.effect_state == "needs_reconciliation"
    assert invocation.result_event_id is None
    assert invocation.recovery_owner == "web_chat_runtime:tool_lifecycle_persistence"
    assert reconciliation is not None
    assert reconciliation.metadata_json["v2_payload"]["reason_code"] == "tool_lifecycle_persistence_failed"


@pytest.mark.asyncio
async def test_legacy_tool_result_materializes_artifact_owner_before_fk_insert(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.config import get_settings
    from app.models.audit import ChatMessage
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services import web_chat_runtime as runtime

    tenant_id, user_id, agent_id, session_id = await _seed(owner_sessionmaker)
    run_id, _provider_request_id = await _prepare_live_round(
        owner_sessionmaker,
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    _patch_runtime_database(monkeypatch, owner_sessionmaker, tenant_id)
    rel_path = "workspace/legacy-report.md"
    target = Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("legacy projection artifact\n", encoding="utf-8")

    result = await runtime._persist_legacy_tool_call(
        agent_id=agent_id,
        user_id=user_id,
        session_id=str(session_id),
        data={
            "name": "write_file",
            "args": {"path": rel_path, "content": "legacy projection artifact\n"},
            "status": "done",
            "result": "Wrote workspace/legacy-report.md",
            "tool_call_id": "legacy-write-artifact",
            "runtime_task_id": str(run_id),
        },
    )

    async with owner_sessionmaker() as db:
        message = await db.get(ChatMessage, result.message_id)
        artifact = await db.scalar(
            select(ChatArtifact).where(
                ChatArtifact.message_id == result.message_id,
                ChatArtifact.path == rel_path,
            )
        )
        event = await db.get(ChatTranscriptEvent, result.event_id)

    assert message is not None
    assert message.role == "tool_call"
    assert artifact is not None
    assert event.message_id == message.id
    assert event.parts_json[0]["artifact_id"] == str(artifact.id)

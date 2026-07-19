from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import AuditLog, ChatMessage
from app.models.chat_session import ChatSession
from app.models.channel_config import ChannelConfig
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.services.chat_transcript import append_session_event
from app.services.external_principal_service import (
    bind_authenticated_self_channel_principal,
    resolve_or_create_external_principal,
)


async def _seed(owner_sessionmaker):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Runtime External", slug=f"runtime-ext-{tenant_id.hex[:10]}"))
        db.add(
            User(
                id=user_id,
                username=f"runtime-owner-{user_id.hex[:8]}",
                email=f"{user_id.hex[:10]}@runtime-ext.test",
                password_hash="x",
                display_name="Runtime Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        agent = Agent(
            id=agent_id,
            tenant_id=tenant_id,
            name="Runtime External Agent",
            role_description="chat safely",
            creator_id=user_id,
            sponsor_user_id=user_id,
        )
        db.add(agent)
        await db.commit()
    return tenant_id, user_id, agent_id


async def _principal_session(owner_sessionmaker, *, linked: bool):
    tenant_id, user_id, agent_id = await _seed(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        config = ChannelConfig(
            tenant_id=tenant_id,
            agent_id=agent_id,
            channel_type="feishu",
            is_configured=True,
            is_connected=True,
            extra_config={"setup_method": "qr_registration"},
        )
        db.add(config)
        await db.flush()
        resolved = await resolve_or_create_external_principal(
            db,
            tenant_id=tenant_id,
            provider="feishu",
            installation_ref=str(config.id),
            channel_config_id=config.id,
            subject_id="ou_runtime",
            display_name="Runtime Guest",
        )
        if linked:
            resolved = await bind_authenticated_self_channel_principal(
                db,
                tenant_id=tenant_id,
                config=config,
                provider_subject_id="ou_runtime",
                user_id=user_id,
                actor_user_id=user_id,
            )
        session = ChatSession(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=resolved.actor.id,
            external_principal_id=resolved.principal.id,
            title="External runtime",
            source_channel="feishu",
            external_conv_id=f"external:{resolved.principal.id}",
            session_kind="human_chat",
            actor_type="external_principal",
            runtime_source="channel_chat",
            visibility_scope="direct_user",
            listed_surface="chat",
        )
        db.add(session)
        await db.flush()
        message = ChatMessage(
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=resolved.actor.id,
            external_principal_id=resolved.principal.id,
            role="user",
            content="hello",
            conversation_id=str(session.id),
        )
        db.add(message)
        await db.commit()
        agent = (
            await db.execute(select(Agent).options(selectinload(Agent.sponsor)).where(Agent.id == agent_id))
        ).scalar_one()
    return tenant_id, user_id, agent, resolved, session


@pytest.mark.usefixtures("migrated_pg_url")
async def test_unbound_external_run_carries_principal_and_locks_tools(monkeypatch, owner_sessionmaker):
    from app.services import web_chat_runtime

    tenant_id, _user_id, agent, resolved, session = await _principal_session(
        owner_sessionmaker,
        linked=False,
    )

    async def no_budget(**_kwargs):
        return None

    async def no_broadcast(*_args, **_kwargs):
        return None

    async def no_notify(**_kwargs):
        return None

    monkeypatch.setattr(web_chat_runtime, "_create_runtime_budget_root_run_for_chat", no_budget)
    monkeypatch.setattr(web_chat_runtime, "broadcast_web_chat_event", no_broadcast)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_notify)

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        payload = await web_chat_runtime.start_channel_chat_run_from_saved_turn(
            db=db,
            agent=agent,
            user=resolved.actor,
            session=session,
            content="hello",
            source_channel="slack",
        )
        task = (
            await db.execute(select(RuntimeTask).where(RuntimeTask.id == uuid.UUID(payload["run_id"])))
        ).scalar_one()

    assert task.metadata_json["user_id"] is None
    assert task.metadata_json["external_principal_id"] == str(resolved.principal.id)
    assert task.metadata_json["external_authority_bound"] is False
    assert task.metadata_json["disable_tools"] is True
    assert task.metadata_json["tool_policy"] == "disabled_for_unbound_external_principal"


@pytest.mark.usefixtures("migrated_pg_url")
async def test_linked_external_run_preserves_both_actor_and_user_authority(monkeypatch, owner_sessionmaker):
    from app.services import web_chat_runtime

    tenant_id, user_id, agent, resolved, session = await _principal_session(owner_sessionmaker, linked=True)

    async def no_budget(**_kwargs):
        return None

    async def no_broadcast(*_args, **_kwargs):
        return None

    async def no_notify(**_kwargs):
        return None

    monkeypatch.setattr(web_chat_runtime, "_create_runtime_budget_root_run_for_chat", no_budget)
    monkeypatch.setattr(web_chat_runtime, "broadcast_web_chat_event", no_broadcast)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_notify)

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        payload = await web_chat_runtime.start_channel_chat_run_from_saved_turn(
            db=db,
            agent=agent,
            user=resolved.actor,
            session=session,
            content="hello",
            source_channel="slack",
        )
        task = (
            await db.execute(select(RuntimeTask).where(RuntimeTask.id == uuid.UUID(payload["run_id"])))
        ).scalar_one()

    assert task.metadata_json["user_id"] == str(user_id)
    assert task.metadata_json["external_principal_id"] == str(resolved.principal.id)
    assert task.metadata_json["external_authority_bound"] is True
    assert task.metadata_json.get("disable_tools") is not True


@pytest.mark.usefixtures("migrated_pg_url")
async def test_transcript_materialization_keeps_external_actor_without_agent_user_fallback(owner_sessionmaker):
    tenant_id, _user_id, agent, resolved, session = await _principal_session(owner_sessionmaker, linked=False)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        result = await append_session_event(
            db=db,
            tenant_id=tenant_id,
            agent_id=agent.id,
            session_id=session.id,
            actor_type="external_principal",
            event_type="user_message",
            role="user",
            user_id=None,
            external_principal_id=resolved.principal.id,
            content="external message",
            bridge_to_t0=False,
        )
        await db.commit()
        message = (await db.execute(select(ChatMessage).where(ChatMessage.id == result.message_id))).scalar_one()

    assert message.user_id is None
    assert message.external_principal_id == resolved.principal.id


@pytest.mark.usefixtures("migrated_pg_url")
async def test_worker_reloads_unbound_external_actor_without_user_lookup(monkeypatch, owner_sessionmaker):
    from app.services import web_chat_runtime

    tenant_id, _user_id, agent, resolved, session = await _principal_session(
        owner_sessionmaker,
        linked=False,
    )

    async def no_budget(**_kwargs):
        return None

    async def no_broadcast(*_args, **_kwargs):
        return None

    async def no_notify(**_kwargs):
        return None

    async def no_snapshot(**_kwargs):
        return None

    monkeypatch.setattr(web_chat_runtime, "_create_runtime_budget_root_run_for_chat", no_budget)
    monkeypatch.setattr(web_chat_runtime, "broadcast_web_chat_event", no_broadcast)
    monkeypatch.setattr(web_chat_runtime, "_capture_user_checkpoint_workspace_snapshot", no_snapshot)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_notify)

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        payload = await web_chat_runtime.start_channel_chat_run_from_saved_turn(
            db=db,
            agent=agent,
            user=resolved.actor,
            session=session,
            content="hello",
            source_channel="slack",
        )

    monkeypatch.setattr(web_chat_runtime, "_async_session", owner_sessionmaker)
    (
        runtime_task,
        _agent,
        actor,
        _primary,
        _fallback,
        _history,
        loaded_session,
    ) = await web_chat_runtime._load_runtime_context(uuid.UUID(payload["run_id"]))

    assert actor.id is None
    assert actor.external_principal_id == resolved.principal.id
    assert actor.authority_bound is False
    assert loaded_session.external_principal_id == resolved.principal.id
    assert runtime_task.metadata_json["disable_tools"] is True


@pytest.mark.usefixtures("migrated_pg_url")
async def test_external_execution_identity_is_consumed_by_general_audit_rows(owner_sessionmaker):
    from app.core.execution_context import ExecutionIdentity, clear_execution_identity, set_execution_identity

    tenant_id, _user_id, agent, resolved, _session = await _principal_session(
        owner_sessionmaker,
        linked=False,
    )
    set_execution_identity(
        ExecutionIdentity(
            identity_type="external_principal",
            identity_id=resolved.principal.id,
            label="Runtime Guest via slack",
        )
    )
    try:
        async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
            audit = AuditLog(
                tenant_id=tenant_id,
                agent_id=agent.id,
                user_id=None,
                action="external_channel_runtime_started",
                details={"channel": "slack"},
            )
            db.add(audit)
            await db.commit()
            audit_id = audit.id
    finally:
        clear_execution_identity()

    async with owner_sessionmaker() as db:
        stored = await db.get(AuditLog, audit_id)

    assert stored is not None
    assert stored.user_id is None
    assert stored.external_principal_id == resolved.principal.id

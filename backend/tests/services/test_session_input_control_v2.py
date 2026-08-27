from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed_session(owner_sessionmaker, *, active_run: bool = False):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id, user_id, agent_id, session_id = (uuid.uuid4() for _ in range(4))
    run_id = uuid.uuid4() if active_run else None
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Input V2 Tenant", slug=f"input-v2-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"input-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@input-v2.test",
                password_hash="x",
                display_name="Input V2",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Input V2 Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        if run_id is not None:
            db.add(
                RuntimeTask(
                    id=run_id,
                    task_type="web_chat_turn",
                    status="running",
                    parent_agent_id=agent_id,
                    child_agent_id=agent_id,
                    tenant_id=tenant_id,
                    parent_session_id=str(session_id),
                    child_session_id=str(session_id),
                    root_user_id=user_id,
                    root_session_id=str(session_id),
                    root_runtime_task_id=run_id,
                    prompt="active",
                    metadata_json={"turn_id": f"turn-{run_id.hex}"},
                )
            )
        await db.commit()
    return tenant_id, user_id, agent_id, session_id, run_id


async def _authority(db, *, user_id, agent_id, session_id):
    from app.models.user import User
    from app.services.session_v2_persistence import resolve_session_mutation_authority

    user = await db.get(User, user_id)
    assert user is not None
    return await resolve_session_mutation_authority(
        db,
        user=user,
        agent_id=agent_id,
        session_id=session_id,
        action="mutate_session_input",
    )


async def _accepted_input(
    db,
    *,
    authority,
    session_id,
    run_id=None,
    kind="start_turn",
    content="Please continue",
):
    from app.services.session_v2_persistence import accept_human_input

    input_id = uuid.uuid4()
    intent = {
        "kind": kind,
        "input_id": str(input_id),
        "idempotency_key": f"input:{input_id}",
        "session_id": str(session_id),
        "content_parts": [{"type": "text", "text": content}],
    }
    if run_id is not None:
        intent.update(
            {
                "expected_turn_id": f"turn-{run_id.hex}",
                "expected_run_id": str(run_id),
                "terminal_fallback": "queue_next_turn",
            }
        )
    receipt = await accept_human_input(db, authority=authority, intent=intent)
    await db.commit()
    return receipt


async def test_live_rest_ws_adapter_persists_full_human_input_authority(owner_sessionmaker) -> None:
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import (
        SessionCommand,
        SessionEventOutbox,
        SessionInputAdmission,
        SessionTurnInput,
    )
    from app.models.user import User
    from app.services.session_live_input import submit_live_human_input

    _tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    input_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        user = await db.get(User, user_id)
        session = await db.get(ChatSession, session_id)
        assert agent is not None and user is not None and session is not None
        receipt = await submit_live_human_input(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content="live ingress prompt",
            source="test_live_rest",
            input_id=input_id,
            idempotency_key=f"live:{input_id}",
        )
        assert receipt["admission_state"] == "admitted"
        assert receipt["run"]["run_id"]

    async with owner_sessionmaker() as db:
        turn_input = await db.get(SessionTurnInput, input_id)
        assert turn_input is not None and turn_input.status == "queued"
        command = await db.get(SessionCommand, turn_input.command_id)
        assert command is not None and command.namespace == "human_input"
        admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == input_id))
        assert admission is not None and admission.state == "admitted"
        events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(ChatTranscriptEvent.input_id == input_id)
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert {event.event_type for event in events} >= {
            "human_input.accepted",
            "input_admission.prepared",
            "input_admission.admitted",
            "human_input.queued",
        }
        outbox_count = await db.scalar(
            select(func.count())
            .select_from(SessionEventOutbox)
            .where(SessionEventOutbox.event_id.in_([event.id for event in events]))
        )
        assert outbox_count == len(events)


@pytest.mark.parametrize(
    ("provider", "channel_type"),
    [
        ("discord", "discord"),
        ("dingtalk", "dingtalk"),
        ("feishu", "feishu"),
        ("slack", "slack"),
        ("teams", "microsoft_teams"),
        ("telegram", "telegram"),
        ("wechat_personal", "wechat_personal"),
        ("wecom", "wecom"),
    ],
)
async def test_unbound_external_channel_input_uses_principal_authority_and_safe_runtime(
    provider,
    channel_type,
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.models.agent import Agent
    from app.models.channel_config import ChannelConfig
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services import web_chat_runtime
    from app.services.external_principal_service import resolve_or_create_external_principal
    from app.services.runtime_budget_failover import not_applicable_runtime_budget_root_binding
    from app.services.session_live_input import submit_live_human_input

    async def admitted_budget(**_kwargs):
        return not_applicable_runtime_budget_root_binding()

    monkeypatch.setattr(web_chat_runtime, "_create_runtime_budget_root_run_for_chat", admitted_budget)

    tenant_id, owner_id, agent_id, config_id, session_id, input_id = (uuid.uuid4() for _ in range(6))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="External Input Tenant", slug=f"external-input-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=owner_id,
                username=f"external-owner-{owner_id.hex[:8]}",
                email=f"{owner_id.hex[:8]}@external-input.test",
                password_hash="x",
                display_name="External Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="External Input Agent", creator_id=owner_id))
        db.add(
            ChannelConfig(
                id=config_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                channel_type=channel_type,
                is_configured=True,
                is_connected=False,
                extra_config={},
            )
        )
        await db.flush()
        resolved = await resolve_or_create_external_principal(
            db,
            tenant_id=tenant_id,
            provider=provider,
            installation_ref=str(config_id),
            channel_config_id=config_id,
            subject_id=f"{provider}-user-42",
            display_name=f"{provider} Guest",
        )
        session = ChatSession(
            id=session_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=None,
            external_principal_id=resolved.principal.id,
            title="External input",
            source_channel=channel_type,
            external_conv_id=f"{provider}_42",
            session_kind="human_chat",
            actor_type="external_principal",
            runtime_source="channel_chat",
            visibility_scope="direct_user",
            listed_surface="chat",
        )
        db.add(session)
        await db.commit()

    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        session = await db.get(ChatSession, session_id)
        actor = await resolve_or_create_external_principal(
            db,
            tenant_id=tenant_id,
            provider=provider,
            installation_ref=str(config_id),
            channel_config_id=config_id,
            subject_id=f"{provider}-user-42",
            display_name=f"{provider} Guest",
        )
        assert agent is not None and session is not None
        receipt = await submit_live_human_input(
            db=db,
            agent=agent,
            user=actor.actor,
            session=session,
            content="Summarize the public message safely",
            source=channel_type,
            input_id=input_id,
            idempotency_key=f"{provider}:{input_id}",
            runtime_metadata={"channel": channel_type, "budget_interactive": False},
        )
        assert receipt["admission_state"] == "admitted"
        admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == input_id))
        assert admission is not None
        assert receipt["run"] is not None, {
            "receipt": receipt,
            "dispatch_last_error": admission.dispatch_last_error,
        }
        assert receipt["run"]["run_id"]

    async with owner_sessionmaker() as db:
        turn_input = await db.get(SessionTurnInput, input_id)
        assert turn_input is not None
        command = await db.get(SessionCommand, turn_input.command_id)
        assert command is not None
        assert command.principal_type == "external_principal"
        assert command.principal_id == resolved.principal.id
        accepted = await db.scalar(
            select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.command_id == command.id,
                ChatTranscriptEvent.item_kind == "human_input",
                ChatTranscriptEvent.lifecycle == "accepted",
            )
        )
        assert accepted is not None and accepted.actor_type == "external_principal"
        task = await db.get(RuntimeTask, uuid.UUID(receipt["run"]["run_id"]))
        assert task is not None
        assert task.metadata_json["external_principal_id"] == str(resolved.principal.id)
        assert task.metadata_json["external_authority_bound"] is False
        assert task.metadata_json["disable_tools"] is True
        assert task.metadata_json["tool_policy"] == "disabled_for_unbound_external_principal"


async def test_verified_feishu_qr_input_uses_bound_principal_authority(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.models.agent import Agent
    from app.models.channel_config import ChannelConfig
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionInputAdmission
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services import web_chat_runtime
    from app.services.external_principal_service import (
        bind_authenticated_self_channel_principal,
        resolve_or_create_external_principal,
    )
    from app.services.runtime_budget_failover import not_applicable_runtime_budget_root_binding
    from app.services.session_live_input import submit_live_human_input

    async def admitted_budget(**_kwargs):
        return not_applicable_runtime_budget_root_binding()

    monkeypatch.setattr(web_chat_runtime, "_create_runtime_budget_root_run_for_chat", admitted_budget)

    tenant_id, owner_id, agent_id, config_id, session_id, input_id = (uuid.uuid4() for _ in range(6))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Feishu QR Tenant", slug=f"feishu-qr-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=owner_id,
                username=f"feishu-owner-{owner_id.hex[:8]}",
                email=f"{owner_id.hex[:8]}@feishu-qr.test",
                password_hash="x",
                display_name="Feishu Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Feishu QR Agent", creator_id=owner_id))
        config = ChannelConfig(
            id=config_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            channel_type="feishu",
            is_configured=True,
            is_connected=True,
            extra_config={"connection_mode": "websocket", "identity_status": "verified"},
        )
        db.add(config)
        await db.flush()
        resolved = await bind_authenticated_self_channel_principal(
            db,
            tenant_id=tenant_id,
            config=config,
            provider_subject_id="ou_verified_scanner",
            user_id=owner_id,
            actor_user_id=owner_id,
        )
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=owner_id,
                external_principal_id=resolved.principal.id,
                title="Verified Feishu input",
                source_channel="feishu",
                external_conv_id="feishu_verified_scanner",
                session_kind="human_chat",
                actor_type="external_principal",
                runtime_source="channel_chat",
                visibility_scope="direct_user",
                listed_surface="chat",
            )
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        session = await db.get(ChatSession, session_id)
        resolved = await resolve_or_create_external_principal(
            db,
            tenant_id=tenant_id,
            provider="feishu",
            installation_ref=str(config_id),
            channel_config_id=config_id,
            subject_id="ou_verified_scanner",
            display_name="Feishu Owner",
        )
        assert agent is not None and session is not None
        assert resolved.actor.authority_bound is True
        receipt = await submit_live_human_input(
            db=db,
            agent=agent,
            user=resolved.actor,
            session=session,
            content="hi",
            source="feishu",
            input_id=input_id,
            idempotency_key=f"feishu:{input_id}",
            runtime_metadata={"channel": "feishu", "budget_interactive": False},
        )
        admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == input_id))
        assert admission is not None
        assert receipt["admission_state"] == "admitted"
        assert receipt["run"] is not None, admission.dispatch_last_error
        task = await db.get(RuntimeTask, uuid.UUID(receipt["run"]["run_id"]))
        assert task is not None
        assert task.metadata_json["external_principal_id"] == str(resolved.principal.id)
        assert task.metadata_json["external_authority_bound"] is True
        assert task.metadata_json.get("disable_tools") is not True


async def test_live_input_id_cannot_rebind_to_another_session(owner_sessionmaker) -> None:
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.services.session_live_input import submit_live_human_input
    from app.services.session_v2_persistence import IdempotencyConflict

    tenant_id, user_id, first_agent_id, first_session_id, _ = await _seed_session(owner_sessionmaker)
    second_agent_id, second_session_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Agent(id=second_agent_id, tenant_id=tenant_id, name="Input V2 Agent 2", creator_id=user_id))
        await db.flush()
        db.add(
            ChatSession(
                id=second_session_id,
                agent_id=second_agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        first_authority = await _authority(
            db,
            user_id=user_id,
            agent_id=first_agent_id,
            session_id=first_session_id,
        )
        accepted = await _accepted_input(
            db,
            authority=first_authority,
            session_id=first_session_id,
        )
        user = await db.get(User, user_id)
        second_agent = await db.get(Agent, second_agent_id)
        second_session = await db.get(ChatSession, second_session_id)
        assert user is not None and second_agent is not None and second_session is not None

        with pytest.raises(IdempotencyConflict):
            await submit_live_human_input(
                db=db,
                agent=second_agent,
                user=user,
                session=second_session,
                content="must not rebind",
                source="authority-collision-test",
                input_id=accepted.input_id,
                idempotency_key=f"collision:{accepted.input_id}",
            )


async def test_session_commands_persist_steer_and_interrupt_authority(owner_sessionmaker) -> None:
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionControlInput, SessionInputAdmission, SessionTurnInput
    from app.models.user import User
    from app.services.session_command_runtime import SessionCommandContext, execute_session_command

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(
        owner_sessionmaker,
        active_run=True,
    )
    assert run_id is not None
    input_id = uuid.uuid4()
    control_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        user = await db.get(User, user_id)
        session = await db.get(ChatSession, session_id)
        assert agent is not None and user is not None and session is not None
        steer = await execute_session_command(
            SessionCommandContext(
                db=db,
                agent=agent,
                user=user,
                access_level="use",
                session_id=session_id,
                arguments={
                    "content": "Use the exact production evidence.",
                    "input_id": str(input_id),
                    "idempotency_key": f"command-steer:{input_id}",
                },
            ),
            "turn_steer",
        )
        assert steer["status"] == "queued"
        interrupt = await execute_session_command(
            SessionCommandContext(
                db=db,
                agent=agent,
                user=user,
                access_level="use",
                session_id=session_id,
                arguments={
                    "run_id": str(run_id),
                    "control_id": str(control_id),
                    "idempotency_key": f"command-interrupt:{control_id}",
                },
            ),
            "interrupt",
        )
        assert interrupt["status"] == "applying"

    async with owner_sessionmaker() as db:
        turn_input = await db.get(SessionTurnInput, input_id)
        control = await db.get(SessionControlInput, control_id)
        run = await db.get(RuntimeTask, run_id)
        assert turn_input is not None
        assert turn_input.intent == "steer_current_turn"
        assert turn_input.status == "queued"
        assert turn_input.target_run_id == run_id
        admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == input_id))
        assert admission is not None and admission.state == "admitted"
        human_command = await db.get(SessionCommand, turn_input.command_id)
        assert human_command is not None and human_command.namespace == "human_input"
        assert control is not None and control.status == "applying" and control.expected_run_id == run_id
        cancel_command = await db.get(SessionCommand, control.command_id)
        assert cancel_command is not None and cancel_command.namespace == "control_input"
        assert run is not None
        assert run.metadata_json["cancel_control_id"] == str(control_id)
        assert run.metadata_json["cancel_state"] == "cancelling"


async def test_channel_durable_ingress_persists_canonical_human_input(owner_sessionmaker, monkeypatch) -> None:
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionEventOutbox, SessionInputAdmission, SessionTurnInput
    from app.models.user import User
    from app.services.channel_agent_runtime import call_agent_llm
    from app.services import web_chat_runtime
    from app.services.runtime_budget_failover import not_applicable_runtime_budget_root_binding

    async def admitted_budget(**_kwargs):
        return not_applicable_runtime_budget_root_binding()

    monkeypatch.setattr(web_chat_runtime, "_create_runtime_budget_root_run_for_chat", admitted_budget)

    _tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    ingress_event_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        session = await db.get(ChatSession, session_id)
        assert user is not None and session is not None
        reply = await call_agent_llm(
            db,
            agent_id,
            "channel ingress prompt",
            user_id=user_id,
            session_id=str(session_id),
            session_source="feishu",
            session_channel="feishu",
            durable_run=True,
            durable_session=session,
            durable_user=user,
            ingress_event_id=ingress_event_id,
        )
        assert "已接收" in reply
        first_input = await db.get(SessionTurnInput, ingress_event_id)
        assert first_input is not None
        first_command = await db.get(SessionCommand, first_input.command_id)
        assert first_command is not None
        first_admission = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == ingress_event_id,
                SessionInputAdmission.input_revision == first_input.revision,
            )
        )
        assert first_admission is not None
        assert first_admission.dispatch_state == "dispatched", first_admission.dispatch_last_error
        assert first_command.command_kind == "start_turn"
        assert first_command.target_json == {
            "runtime_metadata": {
                "budget_interactive": False,
                "channel": "feishu",
                "channel_ingress_event_id": str(ingress_event_id),
                "source": "feishu",
            }
        }
        assert first_command.idempotency_key == f"channel:feishu:ingress:{ingress_event_id}"
        assert first_command.request_json == {
            "input_id": str(ingress_event_id),
            "content_parts": [{"type": "text", "text": "channel ingress prompt"}],
        }
        before_replay = (
            await db.scalar(
                select(func.count()).select_from(SessionCommand).where(SessionCommand.session_id == session_id)
            ),
            await db.scalar(
                select(func.count()).select_from(SessionTurnInput).where(SessionTurnInput.session_id == session_id)
            ),
            await db.scalar(
                select(func.count())
                .select_from(SessionInputAdmission)
                .where(SessionInputAdmission.session_id == session_id)
            ),
            await db.scalar(
                select(func.count()).select_from(RuntimeTask).where(RuntimeTask.parent_session_id == str(session_id))
            ),
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(ChatTranscriptEvent.session_id == session_id)
            ),
            await db.scalar(
                select(func.count()).select_from(SessionEventOutbox).where(SessionEventOutbox.session_id == session_id)
            ),
        )

        replay_reply = await call_agent_llm(
            db,
            agent_id,
            "channel ingress prompt",
            user_id=user_id,
            session_id=str(session_id),
            session_source="feishu",
            session_channel="feishu",
            durable_run=True,
            durable_session=session,
            durable_user=user,
            ingress_event_id=ingress_event_id,
        )
        assert "已接收" in replay_reply
        after_replay = (
            await db.scalar(
                select(func.count()).select_from(SessionCommand).where(SessionCommand.session_id == session_id)
            ),
            await db.scalar(
                select(func.count()).select_from(SessionTurnInput).where(SessionTurnInput.session_id == session_id)
            ),
            await db.scalar(
                select(func.count())
                .select_from(SessionInputAdmission)
                .where(SessionInputAdmission.session_id == session_id)
            ),
            await db.scalar(
                select(func.count()).select_from(RuntimeTask).where(RuntimeTask.parent_session_id == str(session_id))
            ),
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(ChatTranscriptEvent.session_id == session_id)
            ),
            await db.scalar(
                select(func.count()).select_from(SessionEventOutbox).where(SessionEventOutbox.session_id == session_id)
            ),
        )
        assert after_replay == before_replay

        conflict_reply = await call_agent_llm(
            db,
            agent_id,
            "different bytes",
            user_id=user_id,
            session_id=str(session_id),
            session_source="feishu",
            session_channel="feishu",
            durable_run=True,
            durable_session=session,
            durable_user=user,
            ingress_event_id=ingress_event_id,
        )
        assert conflict_reply == (
            "⚠️ 这条 IM 消息与已接收消息使用了相同事件标识但内容不同；为防止重复执行，本次未启动。请重新发送。"
        )
        assert "IdempotencyConflict" not in conflict_reply

    async with owner_sessionmaker() as db:
        row = await db.scalar(
            select(SessionTurnInput)
            .where(SessionTurnInput.session_id == session_id)
            .order_by(SessionTurnInput.queue_ordinal.desc())
        )
        assert row is not None and row.intent == "start_turn" and row.status == "queued"
        assert row.id == ingress_event_id
        command = await db.get(SessionCommand, row.command_id)
        assert command is not None and command.namespace == "human_input"
        admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == row.id))
        assert admission is not None and admission.state == "admitted"
        assert (
            await db.scalar(
                select(func.count()).select_from(SessionTurnInput).where(SessionTurnInput.session_id == session_id)
            )
            == 1
        )


@pytest.mark.parametrize(
    ("decision", "admission_state", "input_state", "terminal_kind"),
    [
        ("block", "rejected", "rejected", "hook.blocked"),
        ("prevent", "cancelled", "cancelled", "hook.prevented"),
        ("failure", "admitted", "accepted", "hook.failed"),
        ("allow", "admitted", "accepted", "hook.completed"),
    ],
)
async def test_user_prompt_admission_is_durable_and_boundary_exact(
    owner_sessionmaker,
    decision,
    admission_state,
    input_state,
    terminal_kind,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionCarryForward, SessionInputAdmission, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.session_input_admission import run_user_prompt_admission

    tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    calls: list[dict] = []

    async def hook_executor(**kwargs):
        calls.append(kwargs)
        if decision == "block":
            return HookResult(block=True, reason="policy blocked")
        if decision == "prevent":
            return HookResult(prevent_continuation=True, stop_reason="keep for later")
        if decision == "failure":
            return HookResult(block=True, failure=True, failure_code="executor_failed")
        return HookResult(additional_contexts=["trusted context"])

    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        receipt = await _accepted_input(db, authority=authority, session_id=session_id)
        outcome = await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=receipt.input_id,
            worker_id="admission-worker",
            hook_executor=hook_executor,
        )
        await db.commit()

    assert outcome.state == admission_state
    assert len(calls) == 1
    assert calls[0]["hook_run_id"] == str(outcome.hook_run_id)
    async with owner_sessionmaker() as db:
        admission = await db.scalar(
            select(SessionInputAdmission).where(SessionInputAdmission.input_id == receipt.input_id)
        )
        input_row = await db.get(SessionTurnInput, receipt.input_id)
        assert admission is not None and admission.state == admission_state
        assert input_row is not None and input_row.status == input_state
        kinds = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent.event_type)
                    .where(ChatTranscriptEvent.session_id == session_id)
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert terminal_kind in kinds
        assert "input_admission.sealed" in kinds
        assert f"input_admission.{admission_state}" in kinds
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.item_kind == "turn",
                )
            )
            == 0
        )
        carry_count = await db.scalar(
            select(func.count()).select_from(SessionCarryForward).where(SessionCarryForward.session_id == session_id)
        )
        assert carry_count == (1 if decision == "prevent" else 0)


async def test_prevented_prompt_does_not_cancel_the_active_run(owner_sessionmaker) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.runtime.hooks import HookResult
    from app.services.session_input_admission import run_user_prompt_admission

    _tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        receipt = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=run_id,
            kind="steer_current_turn",
        )

        async def prevent(**_kwargs):
            return HookResult(prevent_continuation=True, stop_reason="carry")

        outcome = await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=receipt.input_id,
            worker_id="worker",
            hook_executor=prevent,
        )
        await db.commit()
        run = await db.get(RuntimeTask, run_id)
        assert outcome.state == "cancelled"
        assert run is not None and run.status == "running"
        assert not (run.metadata_json or {}).get("cancelled_by_user")


async def test_uncertain_legacy_hook_crash_never_reexecutes_effect(owner_sessionmaker) -> None:
    from app.models.session_v2 import SessionInputAdmission
    from app.services.session_input_admission import claim_user_prompt_admission, run_user_prompt_admission

    _tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        receipt = await _accepted_input(db, authority=authority, session_id=session_id)
        claim = await claim_user_prompt_admission(
            db,
            authority=authority,
            input_id=receipt.input_id,
            worker_id="crashed-worker",
            lease_duration=timedelta(seconds=1),
        )
        assert claim.claimed is True
        admission = await db.scalar(
            select(SessionInputAdmission).where(SessionInputAdmission.input_id == receipt.input_id)
        )
        assert admission is not None
        admission.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

        calls = 0

        async def must_not_run(**_kwargs):
            nonlocal calls
            calls += 1
            return None

        outcome = await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=receipt.input_id,
            worker_id="recovery-worker",
            hook_executor=must_not_run,
            managed_hook=False,
        )
        await db.commit()

    assert outcome.state == "needs_reconciliation"
    assert calls == 0


async def test_cancel_control_receipt_replays_and_settles_once(owner_sessionmaker) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionControlInput
    from app.services.session_control_input import (
        accept_cancel_control_input,
        begin_cancel_control_input,
        settle_cancel_control_input,
    )

    _tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    key = f"cancel:{run_id}"
    control_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        first = await accept_cancel_control_input(
            db,
            authority=authority,
            control_id=control_id,
            idempotency_key=key,
            expected_run_id=run_id,
        )
        await db.commit()
        replay = await accept_cancel_control_input(
            db,
            authority=authority,
            control_id=control_id,
            idempotency_key=key,
            expected_run_id=run_id,
        )
        assert replay.command_id == first.command_id
        assert replay.control_id == first.control_id
        assert replay.replayed is True

        applying = await begin_cancel_control_input(
            db,
            authority=authority,
            control_id=control_id,
            worker_id="cancel-worker",
        )
        await db.commit()
        applying_replay = await begin_cancel_control_input(
            db,
            authority=authority,
            control_id=control_id,
            worker_id="cancel-worker",
        )
        assert applying.status == applying_replay.status == "applying"

        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        task.status = "killed"
        task.completed_at = datetime.now(timezone.utc)
        task.metadata_json = {
            **dict(task.metadata_json or {}),
            "cancel_control_id": str(control_id),
            "terminal_execution_fence_ref": f"runtime-fence:{run_id}:1",
        }
        await db.commit()

        settled = await settle_cancel_control_input(
            db,
            authority=authority,
            control_id=control_id,
            execution_fence_ref=f"runtime-fence:{run_id}:1",
        )
        await db.commit()
        settled_replay = await settle_cancel_control_input(
            db,
            authority=authority,
            control_id=control_id,
            execution_fence_ref=f"runtime-fence:{run_id}:1",
        )
        await db.commit()

        task = await db.get(RuntimeTask, run_id)
        control = await db.get(SessionControlInput, control_id)
        assert settled.status == settled_replay.status == "applied"
        assert task is not None and task.status == "killed"
        assert control is not None and control.status == "applied"
        kinds = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent.event_type).where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.command_id == first.command_id,
                    )
                )
            ).scalars()
        )
        assert kinds.count("control_input.accepted") == 1
        assert kinds.count("control_input.started") == 1
        assert kinds.count("control_input.applied") == 1
        assert kinds.count("run.cancelled") == 1


async def test_cancel_same_semantics_with_different_idempotency_key_is_typed_replay(
    owner_sessionmaker,
) -> None:
    from app.models.session_v2 import SessionCommand, SessionControlInput
    from app.services.session_control_input import accept_cancel_control_input

    _tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    first_control_id = uuid.uuid5(run_id, f"cancel:{user_id}")
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        first = await accept_cancel_control_input(
            db,
            authority=authority,
            control_id=first_control_id,
            idempotency_key=f"cancel:first:{run_id}",
            expected_run_id=run_id,
        )
        await db.commit()
        replay = await accept_cancel_control_input(
            db,
            authority=authority,
            control_id=uuid.uuid4(),
            idempotency_key=f"cancel:retry:{run_id}",
            expected_run_id=run_id,
        )
        await db.commit()

        assert replay.replayed is True
        assert replay.command_id == first.command_id
        assert replay.control_id == first.control_id
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SessionControlInput)
                .where(SessionControlInput.expected_run_id == run_id)
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SessionCommand)
                .where(
                    SessionCommand.session_id == session_id,
                    SessionCommand.namespace == "control_input",
                )
            )
            == 1
        )


async def test_unknown_cancel_target_persists_rejected_command_receipt_and_event(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionCommand
    from app.services.session_control_input import accept_cancel_control_input

    _tenant_id, user_id, agent_id, session_id, _run_id = await _seed_session(owner_sessionmaker)
    missing_run_id = uuid.uuid4()
    control_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        receipt = await accept_cancel_control_input(
            db,
            authority=authority,
            control_id=control_id,
            idempotency_key=f"cancel:missing:{missing_run_id}",
            expected_run_id=missing_run_id,
        )
        await db.commit()

        command = await db.get(SessionCommand, receipt.command_id)
        rejected = await db.scalar(
            select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.command_id == receipt.command_id,
                ChatTranscriptEvent.item_kind == "control_input",
                ChatTranscriptEvent.lifecycle == "rejected",
            )
        )
        assert receipt.status == "rejected"
        assert receipt.reason_code == "active_run_not_found"
        assert command is not None and command.status == "rejected"
        assert command.rejection_json == {"reason_code": "active_run_not_found"}
        assert rejected is not None
        assert (rejected.metadata_json or {})["v2_payload"]["reason_code"] == "active_run_not_found"


async def test_terminal_run_settles_pending_cancel_once_and_rejects_late_cancel(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionControlInput
    from app.services.session_control_input import (
        accept_cancel_control_input,
        begin_cancel_control_input,
        settle_pending_controls_for_run,
    )

    _tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    control_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        accepted = await accept_cancel_control_input(
            db,
            authority=authority,
            control_id=control_id,
            idempotency_key=f"cancel:late:{run_id}",
            expected_run_id=run_id,
        )
        await begin_cancel_control_input(
            db,
            authority=authority,
            control_id=control_id,
            worker_id="late-cancel-worker",
        )
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        task.status = "completed"
        task.completed_at = datetime.now(timezone.utc)
        task.metadata_json = {
            **dict(task.metadata_json or {}),
            "terminal_execution_fence_ref": f"runtime-terminal:{run_id}:completed:1",
        }
        first = await settle_pending_controls_for_run(
            db,
            task=task,
            execution_fence_ref=f"runtime-terminal:{run_id}:completed:1",
            terminal_source="test_late_terminal",
        )
        second = await settle_pending_controls_for_run(
            db,
            task=task,
            execution_fence_ref=f"runtime-terminal:{run_id}:completed:1",
            terminal_source="test_double_terminal",
        )
        await db.commit()

        control = await db.get(SessionControlInput, control_id)
        command = await db.get(SessionCommand, accepted.command_id)
        assert first == {"applied": 0, "rejected": 1}
        assert second == {"applied": 0, "rejected": 0}
        assert control is not None and control.status == "rejected"
        assert command is not None and command.status == "rejected"
        assert command.rejection_json == {"reason_code": "run_terminal_before_cancel_effect"}
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.command_id == accepted.command_id,
                    ChatTranscriptEvent.event_type == "control_input.rejected",
                )
            )
            == 1
        )


async def test_cancel_recovery_resumes_accepted_and_applying_after_restart(owner_sessionmaker) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionControlInput
    from app.services.session_control_input import (
        accept_cancel_control_input,
        recover_stale_cancel_control_inputs_once,
        settle_pending_controls_for_run,
    )

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    control_id = uuid.uuid4()
    signalled: list[uuid.UUID] = []

    async def signal(*, run_id, **_kwargs):
        signalled.append(uuid.UUID(str(run_id)))

    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        accepted = await accept_cancel_control_input(
            db,
            authority=authority,
            control_id=control_id,
            idempotency_key=f"cancel:crash:{run_id}",
            expected_run_id=run_id,
        )
        await db.commit()  # crash window: accepted committed, begin/signal never ran

        first = await recover_stale_cancel_control_inputs_once(
            db,
            worker_id="cancel-recovery-1",
            signal_callback=signal,
            stale_after=timedelta(seconds=0),
            tenant_id=tenant_id,
        )
        await db.commit()
        control = await db.get(SessionControlInput, control_id)
        assert first == {
            "claimed": 1,
            "started": 1,
            "signalled": 1,
            "settled": 0,
            "unavailable": 0,
            "local_delivered": 0,
        }
        assert control is not None and control.status == "applying"
        assert signalled == [run_id]

        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        task.status = "killed"
        task.completed_at = datetime.now(timezone.utc)
        task.metadata_json = {
            **dict(task.metadata_json or {}),
            "terminal_execution_fence_ref": f"runtime-terminal:{run_id}:killed:1",
        }
        await settle_pending_controls_for_run(
            db,
            task=task,
            execution_fence_ref=f"runtime-terminal:{run_id}:killed:1",
            terminal_source="test_cancelled_worker_terminal",
        )
        await db.commit()

        second = await recover_stale_cancel_control_inputs_once(
            db,
            worker_id="cancel-recovery-2",
            signal_callback=signal,
            stale_after=timedelta(seconds=0),
            tenant_id=tenant_id,
        )
        await db.commit()
        control = await db.get(SessionControlInput, control_id)
        assert second == {
            "claimed": 0,
            "started": 0,
            "signalled": 0,
            "settled": 0,
            "unavailable": 0,
            "local_delivered": 0,
        }
        assert control is not None and control.status == "applied"
        assert signalled == [run_id]
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.command_id == accepted.command_id,
                    ChatTranscriptEvent.event_type == "control_input.started",
                )
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.command_id == accepted.command_id,
                    ChatTranscriptEvent.event_type == "control_input.applied",
                )
            )
            == 1
        )


async def test_cancel_signal_publish_failure_exposes_local_delivery_and_typed_retryable_unavailable(
    monkeypatch,
) -> None:
    from app.services.web_chat_runtime import (
        register_web_chat_run_for_test,
        signal_web_chat_cancel,
        unregister_web_chat_run_for_test,
    )

    run_id = uuid.uuid4()
    cancel_event = asyncio.Event()
    register_web_chat_run_for_test(run_id.hex, cancel_event=cancel_event)

    async def unavailable_bus(**_kwargs):
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr("app.services.runtime_control_bus.publish_web_chat_cancel", unavailable_bus)
    try:
        receipt = await signal_web_chat_cancel(
            run_id=run_id,
            agent_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )
    finally:
        unregister_web_chat_run_for_test(run_id.hex)

    assert cancel_event.is_set() is True
    assert receipt.delivery_state == "unavailable"
    assert receipt.retryable is True
    assert receipt.local_delivered is True
    assert receipt.cross_process_delivered is False
    assert receipt.error_class == "ConnectionError"


async def test_cancel_recovery_publish_failure_is_durable_retryable_and_never_counted_signalled(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionControlInput
    from app.services.session_control_input import (
        accept_cancel_control_input,
        recover_stale_cancel_control_inputs_once,
    )
    from app.services.web_chat_runtime import CancelSignalDeliveryReceipt

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(
        owner_sessionmaker,
        active_run=True,
    )
    assert run_id is not None
    control_id = uuid.uuid4()
    attempts: list[str] = []

    async def unavailable_signal(*, run_id, **_kwargs):
        attempts.append(f"unavailable:{run_id}")
        return CancelSignalDeliveryReceipt(
            run_id=str(run_id),
            delivery_state="unavailable",
            local_delivered=False,
            cross_process_delivered=False,
            retryable=True,
            error_class="ConnectionError",
        )

    async def delivered_signal(*, run_id, **_kwargs):
        attempts.append(f"delivered:{run_id}")

    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        await accept_cancel_control_input(
            db,
            authority=authority,
            control_id=control_id,
            idempotency_key=f"cancel:delivery-recovery:{run_id}",
            expected_run_id=run_id,
        )
        await db.commit()

        unavailable = await recover_stale_cancel_control_inputs_once(
            db,
            worker_id="cancel-delivery-unavailable",
            signal_callback=unavailable_signal,
            stale_after=timedelta(seconds=0),
            tenant_id=tenant_id,
        )

        control = await db.get(SessionControlInput, control_id)
        task = await db.get(RuntimeTask, run_id)
        failure = await db.scalar(
            select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.item_kind == "recovery_action",
                ChatTranscriptEvent.lifecycle == "failed",
            )
        )
        assert unavailable == {
            "claimed": 1,
            "started": 1,
            "signalled": 0,
            "settled": 0,
            "unavailable": 1,
            "local_delivered": 0,
        }
        assert control is not None and control.status == "applying"
        assert control.recovery_owner is None
        assert task is not None and task.status == "running"
        assert (task.metadata_json or {})["cancel_signal_delivery"] == {
            "attempt_id": f"cancel-signal-delivery:{control_id}:2",
            "delivery_state": "unavailable",
            "local_delivered": False,
            "cross_process_delivered": False,
            "retryable": True,
            "error_class": "ConnectionError",
        }
        assert failure is not None
        failure_payload = (failure.metadata_json or {})["v2_payload"]
        assert failure_payload["delivery_state"] == "unavailable"
        assert failure_payload["retryable"] is True
        assert failure_payload["error_class"] == "ConnectionError"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.event_type == "run.cancelled",
                )
            )
            == 0
        )

        delivered = await recover_stale_cancel_control_inputs_once(
            db,
            worker_id="cancel-delivery-retry",
            signal_callback=delivered_signal,
            stale_after=timedelta(seconds=0),
            tenant_id=tenant_id,
        )
        assert delivered == {
            "claimed": 1,
            "started": 0,
            "signalled": 1,
            "settled": 0,
            "unavailable": 0,
            "local_delivered": 0,
        }
        assert attempts == [f"unavailable:{run_id}", f"delivered:{run_id}"]
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.item_kind == "recovery_action",
                    ChatTranscriptEvent.lifecycle == "failed",
                )
            )
            == 1
        )


async def test_real_terminal_owner_atomically_settles_cancel_without_manual_settle(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionControlInput
    from app.services.session_control_input import accept_cancel_control_input, begin_cancel_control_input
    from app.services.web_chat_runtime import _apply_terminal_task_update_and_settle

    _tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    control_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        accepted = await accept_cancel_control_input(
            db,
            authority=authority,
            control_id=control_id,
            idempotency_key=f"cancel:real-finalizer:{run_id}",
            expected_run_id=run_id,
        )
        await begin_cancel_control_input(
            db,
            authority=authority,
            control_id=control_id,
            worker_id="real-finalizer-worker",
        )
        await db.commit()

        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        await _apply_terminal_task_update_and_settle(
            db,
            task,
            status="killed",
            result_summary="Generation stopped by user.",
            metadata_json={"cancel_control_id": str(control_id)},
            terminal_source="test_real_terminal_owner",
        )
        await db.commit()

        task = await db.get(RuntimeTask, run_id)
        control = await db.get(SessionControlInput, control_id)
        assert task is not None and task.status == "killed"
        assert (task.metadata_json or {})["terminal_execution_fence_ref"].startswith("runtime-task-terminal:")
        assert control is not None and control.status == "applied"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.command_id == accepted.command_id,
                    ChatTranscriptEvent.event_type == "control_input.applied",
                )
            )
            == 1
        )


async def test_existing_runtime_worker_tick_recovers_and_signals_cancel(owner_sessionmaker) -> None:
    from app.models.session_v2 import SessionControlInput
    from app.services.runtime_task_worker import recover_session_control_inputs_once
    from app.services.session_control_input import accept_cancel_control_input

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    control_id = uuid.uuid4()
    signalled: list[uuid.UUID] = []

    async def signal(*, run_id, **_kwargs):
        signalled.append(uuid.UUID(str(run_id)))

    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        await accept_cancel_control_input(
            db,
            authority=authority,
            control_id=control_id,
            idempotency_key=f"cancel:worker-tick:{run_id}",
            expected_run_id=run_id,
        )
        await db.commit()

    counts = await recover_session_control_inputs_once(
        worker_id="test-existing-runtime-worker",
        signal_callback=signal,
        stale_after=timedelta(seconds=0),
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    assert counts == {
        "claimed": 1,
        "started": 1,
        "signalled": 1,
        "settled": 0,
        "unavailable": 0,
        "local_delivered": 0,
    }
    assert signalled == [run_id]
    async with owner_sessionmaker() as db:
        control = await db.get(SessionControlInput, control_id)
        assert control is not None and control.status == "applying"


async def test_existing_runtime_worker_tick_claims_turn_replacement_saga(owner_sessionmaker) -> None:
    from app.models.session_v2 import SessionControlInput, SessionTurnReplacement
    from app.runtime.hooks import HookResult
    from app.services.runtime_task_worker import recover_turn_replacement_sagas_once
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_turn_replacement import request_turn_replacement

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    signals: list[uuid.UUID] = []

    async def signal(*, run_id, **_kwargs):
        signals.append(uuid.UUID(str(run_id)))

    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        input_receipt = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=run_id,
            kind="interrupt_and_replace",
        )

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=input_receipt.input_id,
            worker_id="replacement-worker-wrapper-admission",
            hook_executor=allow,
        )
        requested = await request_turn_replacement(db, authority=authority, input_id=input_receipt.input_id)
        await db.commit()

    counts = await recover_turn_replacement_sagas_once(
        worker_id="test-existing-runtime-worker-replacement",
        signal_callback=signal,
        stale_after=timedelta(seconds=0),
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    assert counts["claimed"] == 1
    assert counts["signalled"] == 1
    assert counts["needs_reconciliation"] == 0
    assert signals == [run_id]
    async with owner_sessionmaker() as db:
        saga = await db.get(SessionTurnReplacement, requested.saga_id)
        control = await db.get(SessionControlInput, saga.cancel_control_id if saga else None)
        assert saga is not None and saga.state == "cancel_accepted"
        assert control is not None and control.status == "applying"


async def test_unbound_input_revision_is_cas_and_cancellation_preserves_original_evidence(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.session_human_input import (
        InputRevisionConflict,
        cancel_unbound_human_input,
        revise_unbound_human_input,
    )
    from app.services.session_input_admission import run_user_prompt_admission

    _tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        accepted = await _accepted_input(db, authority=authority, session_id=session_id)

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=accepted.input_id,
            worker_id="revision-one-admission",
            hook_executor=allow,
        )
        original_admission = await db.scalar(
            select(SessionInputAdmission).where(SessionInputAdmission.input_id == accepted.input_id)
        )
        assert original_admission is not None and original_admission.state == "admitted"
        original_hook_run_id = original_admission.hook_run_id
        revised = await revise_unbound_human_input(
            db,
            authority=authority,
            input_id=accepted.input_id,
            expected_revision=1,
            content_parts=[{"type": "text", "text": "Revised complete prompt"}],
        )
        assert revised.revision == 2
        attempts = list(
            (
                await db.execute(
                    select(SessionInputAdmission)
                    .where(SessionInputAdmission.input_id == accepted.input_id)
                    .order_by(SessionInputAdmission.input_revision)
                )
            ).scalars()
        )
        assert [attempt.input_revision for attempt in attempts] == [1, 2]
        assert [attempt.state for attempt in attempts] == ["admitted", "admission_pending"]
        assert attempts[0].hook_run_id == original_hook_run_id
        assert attempts[1].hook_run_id != original_hook_run_id
        with pytest.raises(InputRevisionConflict) as stale:
            await revise_unbound_human_input(
                db,
                authority=authority,
                input_id=accepted.input_id,
                expected_revision=1,
                content_parts=[{"type": "text", "text": "stale"}],
            )
        assert stale.value.current_revision == 2
        cancelled = await cancel_unbound_human_input(
            db,
            authority=authority,
            input_id=accepted.input_id,
            expected_revision=2,
        )
        await db.commit()
        assert cancelled.status == "cancelled"
        assert cancelled.revision == 3
        row = await db.get(SessionTurnInput, accepted.input_id)
        assert row is not None and row.content_parts_json[0]["text"] == "Revised complete prompt"
        attempts = list(
            (
                await db.execute(
                    select(SessionInputAdmission)
                    .where(SessionInputAdmission.input_id == accepted.input_id)
                    .order_by(SessionInputAdmission.input_revision)
                )
            ).scalars()
        )
        assert [attempt.state for attempt in attempts] == ["admitted", "cancelled"]
        events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.item_id == accepted.input_id,
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert [event.lifecycle for event in events] == ["accepted", "revised", "cancelled"]
        assert (events[0].metadata_json or {})["v2_payload"]["content_parts"][0]["text"] == "Please continue"


async def test_stale_admission_recovery_looks_up_stable_hook_run_and_never_blindly_reruns(
    owner_sessionmaker,
) -> None:
    from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.session_input_admission import (
        ManagedHookLookup,
        claim_user_prompt_admission,
        recover_stale_input_admissions_once,
    )

    tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        recovered_input = await _accepted_input(db, authority=authority, session_id=session_id)
        unknown_input = await _accepted_input(db, authority=authority, session_id=session_id)
        recovered_claim = await claim_user_prompt_admission(
            db,
            authority=authority,
            input_id=recovered_input.input_id,
            worker_id="hook-before-crash-a",
            lease_duration=timedelta(seconds=0),
        )
        unknown_claim = await claim_user_prompt_admission(
            db,
            authority=authority,
            input_id=unknown_input.input_id,
            worker_id="hook-before-crash-b",
            lease_duration=timedelta(seconds=0),
        )
        await db.commit()

        lookups: list[uuid.UUID] = []

        async def lookup(*, hook_run_id, **_kwargs):
            stable_id = uuid.UUID(str(hook_run_id))
            lookups.append(stable_id)
            if stable_id == recovered_claim.hook_run_id:
                return ManagedHookLookup(found=True, result=HookResult())
            return ManagedHookLookup(found=False, result=None)

        counts = await recover_stale_input_admissions_once(
            db,
            worker_id="admission-recovery-worker",
            managed_result_lookup=lookup,
            tenant_id=tenant_id,
            stale_after=timedelta(seconds=0),
        )
        recovered = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == recovered_input.input_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        unknown = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == unknown_input.input_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        unknown_row = await db.get(SessionTurnInput, unknown_input.input_id)
        assert counts == {"claimed": 2, "recovered": 1, "needs_reconciliation": 1}
        assert set(lookups) == {recovered_claim.hook_run_id, unknown_claim.hook_run_id}
        assert recovered is not None and recovered.state == "admitted"
        assert unknown is not None and unknown.state == "needs_reconciliation"
        assert unknown_row is not None and unknown_row.status == "needs_reconciliation"


async def test_stale_pending_admission_is_safely_claimed_and_executes_hook_once(
    owner_sessionmaker,
) -> None:
    from app.models.session_v2 import SessionInputAdmission
    from app.runtime.hooks import HookResult
    from app.services.session_input_admission import recover_stale_input_admissions_once

    tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    calls: list[str] = []

    async def hook_executor(**kwargs):
        calls.append(str(kwargs["hook_run_id"]))
        return HookResult()

    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        accepted = await _accepted_input(db, authority=authority, session_id=session_id)
        first = await recover_stale_input_admissions_once(
            db,
            worker_id="pending-admission-recovery",
            pending_hook_executor=hook_executor,
            tenant_id=tenant_id,
            stale_after=timedelta(seconds=0),
        )
        second = await recover_stale_input_admissions_once(
            db,
            worker_id="pending-admission-recovery",
            pending_hook_executor=hook_executor,
            tenant_id=tenant_id,
            stale_after=timedelta(seconds=0),
        )
        admission = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == accepted.input_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        assert first == {"claimed": 1, "recovered": 1, "needs_reconciliation": 0}
        assert second == {"claimed": 0, "recovered": 0, "needs_reconciliation": 0}
        assert admission is not None and admission.state == "admitted"
        assert calls == [str(admission.hook_run_id)]


async def test_runtime_worker_recovers_stale_admission_from_invocation_span_receipt(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app import database
    from app.models.invocation_span import InvocationSpan
    from app.models.session_v2 import SessionInputAdmission
    from app.runtime.hooks import (
        HookContext,
        HookEvent,
        HookResult,
        _persist_hook_boundary_evidence,
    )
    from app.services.runtime_task_worker import recover_stale_session_input_admissions_once
    from app.services.session_input_admission import claim_user_prompt_admission

    # The production hook writer intentionally opens an independent committed
    # session. Bind that factory to the migrated Testcontainers database so the
    # test exercises the real independent writer instead of the developer's
    # process-global DATABASE_URL.
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)

    tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        accepted = await _accepted_input(db, authority=authority, session_id=session_id)
        claim = await claim_user_prompt_admission(
            db,
            authority=authority,
            input_id=accepted.input_id,
            worker_id="managed-hook-before-crash",
            lease_duration=timedelta(seconds=0),
        )
        await db.commit()

    await _persist_hook_boundary_evidence(
        HookContext(
            event=HookEvent.USER_PROMPT_SUBMIT,
            agent_id=agent_id,
            session_id=str(session_id),
            prompt="Please continue",
            source="session_input_admission",
            metadata={
                "tenant_id": str(tenant_id),
                "principal_id": str(user_id),
                "input_id": str(accepted.input_id),
                "hook_run_id": str(claim.hook_run_id),
            },
        ),
        result=HookResult(),
        duration_ms=1.0,
    )
    async with owner_sessionmaker() as db:
        span = await db.scalar(
            select(InvocationSpan).where(
                InvocationSpan.tenant_id == tenant_id,
                InvocationSpan.metadata_json["hook_run_id"].astext == str(claim.hook_run_id),
            )
        )
        assert span is not None
        assert (span.metadata_json or {})["hook_result_hash"]
        assert isinstance((span.metadata_json or {})["hook_result_payload"], dict)

    counts = await recover_stale_session_input_admissions_once(
        worker_id="managed-hook-recovery-worker",
        stale_after=timedelta(seconds=0),
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    assert counts == {"claimed": 1, "recovered": 1, "needs_reconciliation": 0}
    async with owner_sessionmaker() as db:
        admission = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == accepted.input_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        assert admission is not None and admission.state == "admitted"


async def test_queue_next_turn_is_fifo_and_creates_turn_only_after_admission(owner_sessionmaker) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.runtime.hooks import HookResult
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission

    _tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)

    async def allow(**_kwargs):
        return HookResult()

    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        accepted = [
            await _accepted_input(db, authority=authority, session_id=session_id, kind="queue_next_turn")
            for _ in range(2)
        ]
        queued = []
        for receipt in accepted:
            await run_user_prompt_admission(
                db,
                authority=authority,
                input_id=receipt.input_id,
                worker_id=f"worker-{receipt.input_id}",
                hook_executor=allow,
            )
            queued.append(await queue_admitted_human_input(db, authority=authority, input_id=receipt.input_id))
            await db.commit()

        assert [item.queue_ordinal for item in queued] == sorted(item.queue_ordinal for item in queued)
        assert all(item.status == "queued" for item in queued)
        assert len({item.target_turn_id for item in queued}) == 2
        turn_events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.item_kind == "turn",
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert [event.lifecycle for event in turn_events] == ["accepted", "queued", "accepted", "queued"]


async def test_runtime_worker_recovers_admitted_start_input_after_api_crash(owner_sessionmaker) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.runtime_task_worker import recover_session_input_dispatches_once
    from app.services.session_input_admission import run_user_prompt_admission

    tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)

    async def allow(**_kwargs):
        return HookResult()

    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        accepted = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            content="recover the accepted API input",
        )
        outcome = await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=accepted.input_id,
            worker_id="api-before-dispatch-crash",
            hook_executor=allow,
        )
        assert outcome.state == "admitted"

    first = await recover_session_input_dispatches_once(
        worker_id="input-dispatch-recovery",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    second = await recover_session_input_dispatches_once(
        worker_id="input-dispatch-recovery",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    expected_run_id = uuid.uuid5(accepted.input_id, "session-v2-runtime-run")
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, accepted.input_id)
        admission = await db.get(SessionInputAdmission, outcome.admission_id)
        run = await db.get(RuntimeTask, expected_run_id)
        assert first == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}
        assert second == {"claimed": 0, "dispatched": 0, "deferred": 0, "retried": 0}
        assert admission is not None and admission.dispatch_state == "dispatched"
        assert row is not None and row.status == "queued" and row.target_run_id == expected_run_id
        assert run is not None and run.prompt == "recover the accepted API input"


async def test_input_dispatch_fast_path_does_not_steal_live_worker_lease(owner_sessionmaker) -> None:
    from app.models.session_v2 import SessionInputAdmission
    from app.runtime.hooks import HookResult
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_input_dispatch import dispatch_admitted_input_fast_path

    _tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)

    async def allow(**_kwargs):
        return HookResult()

    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        accepted = await _accepted_input(db, authority=authority, session_id=session_id)
        admitted = await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=accepted.input_id,
            worker_id="admission-owner",
            hook_executor=allow,
        )
        admission = await db.get(SessionInputAdmission, admitted.admission_id)
        assert admission is not None
        admission.dispatch_state = "dispatching"
        admission.dispatch_attempts = 1
        admission.lease_owner = "durable-worker-a"
        admission.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        await db.commit()

        outcome = await dispatch_admitted_input_fast_path(
            db,
            admission_id=admission.id,
            worker_id="api-fast-path-b",
        )
        current = await db.get(SessionInputAdmission, admission.id)
        assert outcome.state == "dispatching" and outcome.deferred is True
        assert current is not None
        assert current.dispatch_state == "dispatching"
        assert current.dispatch_attempts == 1
        assert current.lease_owner == "durable-worker-a"


async def test_start_input_race_is_terminal_typed_conflict_not_permanent_retry(owner_sessionmaker) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.runtime_task_worker import recover_session_input_dispatches_once
    from app.services.session_input_admission import run_user_prompt_admission

    tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)

    async def allow(**_kwargs):
        return HookResult()

    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        accepted = await _accepted_input(db, authority=authority, session_id=session_id)
        admitted = await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=accepted.input_id,
            worker_id="start-race-admission",
            hook_executor=allow,
        )
        competing_run_id = uuid.uuid4()
        db.add(
            RuntimeTask(
                id=competing_run_id,
                task_type="web_chat_turn",
                status="running",
                parent_agent_id=agent_id,
                child_agent_id=agent_id,
                tenant_id=tenant_id,
                parent_session_id=str(session_id),
                child_session_id=str(session_id),
                root_user_id=user_id,
                root_session_id=str(session_id),
                root_runtime_task_id=competing_run_id,
                prompt="won the start race",
                metadata_json={"turn_id": f"turn-{competing_run_id.hex}"},
            )
        )
        await db.commit()

    counts = await recover_session_input_dispatches_once(
        worker_id="start-race-dispatch",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    replay = await recover_session_input_dispatches_once(
        worker_id="start-race-dispatch",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, accepted.input_id)
        command = await db.get(SessionCommand, accepted.command_id)
        admission = await db.get(SessionInputAdmission, admitted.admission_id)
        assert counts == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}
        assert replay == {"claimed": 0, "dispatched": 0, "deferred": 0, "retried": 0}
        assert row is not None and row.status == "rejected"
        assert command is not None and command.status == "rejected"
        assert (command.rejection_json or {})["reason_code"] == "active_turn_conflict_after_admission"
        assert admission is not None and admission.dispatch_state == "dispatched"
        assert (admission.dispatch_receipt_json or {})["kind"] == "rejected"


async def test_queue_next_turn_worker_starts_exactly_one_fifo_successor_after_terminal(
    owner_sessionmaker,
) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.runtime_task_worker import recover_session_input_dispatches_once
    from app.services.session_input_admission import run_user_prompt_admission

    tenant_id, user_id, agent_id, session_id, active_run_id = await _seed_session(
        owner_sessionmaker,
        active_run=True,
    )
    assert active_run_id is not None

    async def allow(**_kwargs):
        return HookResult()

    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        accepted = []
        for index in range(2):
            receipt = await _accepted_input(
                db,
                authority=authority,
                session_id=session_id,
                kind="queue_next_turn",
                content=f"fifo successor {index + 1}",
            )
            await run_user_prompt_admission(
                db,
                authority=authority,
                input_id=receipt.input_id,
                worker_id=f"queue-admission-{index}",
                hook_executor=allow,
            )
            accepted.append(receipt)

    waiting = await recover_session_input_dispatches_once(
        worker_id="queue-dispatch-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    assert waiting == {"claimed": 2, "dispatched": 0, "deferred": 2, "retried": 0}

    async with owner_sessionmaker() as db:
        active = await db.get(RuntimeTask, active_run_id)
        assert active is not None
        active.status = "completed"
        active.completed_at = datetime.now(timezone.utc)
        await db.commit()

    first_dispatch = await recover_session_input_dispatches_once(
        worker_id="queue-dispatch-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    first_run_id = uuid.uuid5(accepted[0].input_id, "session-v2-successor-run")
    second_run_id = uuid.uuid5(accepted[1].input_id, "session-v2-successor-run")
    async with owner_sessionmaker() as db:
        first_run = await db.get(RuntimeTask, first_run_id)
        second_run = await db.get(RuntimeTask, second_run_id)
        active_count = await db.scalar(
            select(func.count())
            .select_from(RuntimeTask)
            .where(
                RuntimeTask.tenant_id == tenant_id,
                RuntimeTask.parent_session_id == str(session_id),
                RuntimeTask.status.in_(("pending", "running")),
            )
        )
        assert first_dispatch == {"claimed": 2, "dispatched": 1, "deferred": 1, "retried": 0}
        assert first_run is not None and first_run.prompt == "fifo successor 1"
        assert second_run is None
        assert active_count == 1
        first_run.status = "completed"
        first_run.completed_at = datetime.now(timezone.utc)
        await db.commit()

    second_dispatch = await recover_session_input_dispatches_once(
        worker_id="queue-dispatch-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    async with owner_sessionmaker() as db:
        rows = [await db.get(SessionTurnInput, receipt.input_id) for receipt in accepted]
        admissions = [
            await db.scalar(
                select(SessionInputAdmission).where(
                    SessionInputAdmission.input_id == receipt.input_id,
                    SessionInputAdmission.input_revision == 1,
                )
            )
            for receipt in accepted
        ]
        second_run = await db.get(RuntimeTask, second_run_id)
        active_count = await db.scalar(
            select(func.count())
            .select_from(RuntimeTask)
            .where(
                RuntimeTask.tenant_id == tenant_id,
                RuntimeTask.parent_session_id == str(session_id),
                RuntimeTask.status.in_(("pending", "running")),
            )
        )
        assert second_dispatch == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}
        assert second_run is not None and second_run.prompt == "fifo successor 2"
        assert active_count == 1
        assert all(row is not None and row.status == "queued" for row in rows)
        assert [admission.dispatch_state for admission in admissions if admission is not None] == [
            "dispatched",
            "dispatched",
        ]


async def test_steer_terminal_fallback_rolls_over_atomically(owner_sessionmaker) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.runtime_task_worker import recover_session_input_dispatches_once
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission

    _tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        accepted = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=run_id,
            kind="steer_current_turn",
        )

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=accepted.input_id,
            worker_id="steer-worker",
            hook_executor=allow,
        )
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        task.status = "completed"
        await db.commit()
        rolled = await queue_admitted_human_input(
            db,
            authority=authority,
            input_id=accepted.input_id,
        )
        await db.commit()
        assert rolled.status == "rolled_over"
        assert rolled.rolled_over_to_turn_id
        kinds = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent.event_type)
                    .where(ChatTranscriptEvent.session_id == session_id)
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert kinds[-3:] == ["human_input.rolled_over", "turn.accepted", "turn.queued"]

    dispatched = await recover_session_input_dispatches_once(
        worker_id="rollover-successor-worker",
        tenant_id=_tenant_id,
        session_factory=owner_sessionmaker,
    )
    replay = await recover_session_input_dispatches_once(
        worker_id="rollover-successor-worker",
        tenant_id=_tenant_id,
        session_factory=owner_sessionmaker,
    )
    successor_run_id = uuid.uuid5(accepted.input_id, "session-v2-successor-run")
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, accepted.input_id)
        successor = await db.get(RuntimeTask, successor_run_id)
        assert dispatched == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}
        assert replay == {"claimed": 0, "dispatched": 0, "deferred": 0, "retried": 0}
        assert row is not None and row.status == "rolled_over"
        assert row.target_run_id is None
        assert successor is not None
        assert (successor.metadata_json or {})["session_v2_rolled_over_input_id"] == str(accepted.input_id)


async def test_dispatched_steer_rolls_over_once_after_target_run_terminalizes(owner_sessionmaker) -> None:
    """DAY1-A2A-TERMINAL-ROLLOVER-001 production race.

    A steer admitted and dispatched into a genuinely active target run
    becomes an orphan mailbox item when that run terminalizes without
    binding it: the admission is already ``dispatched`` so the pending /
    stale-dispatching sweep never re-examines it.  The durable terminal
    rollover lane must roll the steer to its deterministic successor turn
    and start exactly one successor RuntimeTask without user polling.
    """

    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.runtime_task_worker import (
        recover_session_input_dispatches_once,
        recover_terminal_target_session_inputs_once,
    )
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_v2_persistence import accept_human_input
    from app.services.web_chat_runtime import _apply_terminal_task_update_and_settle

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    fallbackless_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        steered = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=run_id,
            kind="steer_current_turn",
            content="Focus on the delegation result",
        )
        await accept_human_input(
            db,
            authority=authority,
            intent={
                "kind": "steer_current_turn",
                "input_id": str(fallbackless_id),
                "idempotency_key": f"input:{fallbackless_id}",
                "session_id": str(session_id),
                "content_parts": [{"type": "text", "text": "Steer without terminal fallback"}],
                "expected_turn_id": f"turn-{run_id.hex}",
                "expected_run_id": str(run_id),
            },
        )
        await db.commit()

        async def allow(**_kwargs):
            return HookResult()

        for input_id in (steered.input_id, fallbackless_id):
            await run_user_prompt_admission(
                db,
                authority=authority,
                input_id=input_id,
                worker_id="terminal-rollover-admission",
                hook_executor=allow,
            )

    dispatched_while_active = await recover_session_input_dispatches_once(
        worker_id="terminal-rollover-dispatch",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    successor_run_id = uuid.uuid5(steered.input_id, "session-v2-successor-run")
    async with owner_sessionmaker() as db:
        assert dispatched_while_active == {"claimed": 2, "dispatched": 2, "deferred": 0, "retried": 0}
        row = await db.get(SessionTurnInput, steered.input_id)
        admission = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == steered.input_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        assert row is not None and row.status == "queued"
        assert row.target_run_id == run_id
        assert row.bound_round_id is None
        assert admission is not None and admission.dispatch_state == "dispatched"
        assert (admission.dispatch_receipt_json or {}).get("kind") == "mailbox"

        # The production terminal settlement path closes the parent run after
        # the steer was already dispatched into it.
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        await _apply_terminal_task_update_and_settle(
            db,
            task,
            status="completed",
            result_summary="Delegation finished without consuming the steer mailbox.",
            metadata_json={},
            terminal_source="test_terminal_rollover_owner",
        )
        await db.commit()

    rollover = await recover_terminal_target_session_inputs_once(
        worker_id="terminal-rollover-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    replay = await recover_terminal_target_session_inputs_once(
        worker_id="terminal-rollover-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    pending_sweep = await recover_session_input_dispatches_once(
        worker_id="terminal-rollover-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, steered.input_id)
        admission = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == steered.input_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        command = await db.get(SessionCommand, steered.command_id)
        successor = await db.get(RuntimeTask, successor_run_id)
        active_successors = await db.scalar(
            select(func.count())
            .select_from(RuntimeTask)
            .where(
                RuntimeTask.tenant_id == tenant_id,
                RuntimeTask.parent_session_id == str(session_id),
                RuntimeTask.status.in_(("pending", "running")),
            )
        )
        rolled_over_events = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.event_type == "human_input.rolled_over",
            )
        )
        rollover_turn_events = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.item_kind == "turn",
                ChatTranscriptEvent.lifecycle.in_(("accepted", "queued")),
                ChatTranscriptEvent.scope_json["turn_id"].astext == (row.rolled_over_to_turn_id if row else ""),
            )
        )
        fallbackless_row = await db.get(SessionTurnInput, fallbackless_id)
        fallbackless_admission = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == fallbackless_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        assert rollover == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}
        assert replay == {"claimed": 0, "dispatched": 0, "deferred": 0, "retried": 0}
        assert pending_sweep == {"claimed": 0, "dispatched": 0, "deferred": 0, "retried": 0}
        assert row is not None and row.status == "rolled_over"
        assert row.rolled_over_to_turn_id is not None
        assert row.target_run_id is None
        assert row.settlement_ref is not None
        assert command is not None and command.status == "applied"
        assert admission is not None and admission.dispatch_state == "dispatched"
        assert (admission.dispatch_receipt_json or {}).get("kind") == "runtime"
        assert (admission.dispatch_receipt_json or {}).get("run_id") == str(successor_run_id)
        assert successor is not None and successor.prompt == "Focus on the delegation result"
        assert (successor.metadata_json or {})["session_v2_rolled_over_input_id"] == str(steered.input_id)
        assert active_successors == 1
        assert rolled_over_events == 1
        assert rollover_turn_events == 2
        # A dispatched steer without terminal_fallback stays a settled mailbox
        # item; the recovery lane must not reopen it.
        assert fallbackless_row is not None and fallbackless_row.status == "queued"
        assert fallbackless_row.target_run_id == run_id
        assert fallbackless_row.rolled_over_to_turn_id is None
        assert fallbackless_admission is not None and fallbackless_admission.dispatch_state == "dispatched"
        assert (fallbackless_admission.dispatch_receipt_json or {}).get("kind") == "mailbox"


async def test_terminal_steer_rollover_lane_respects_fifo_and_active_run_gate(owner_sessionmaker) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.runtime_task_worker import (
        recover_session_input_dispatches_once,
        recover_terminal_target_session_inputs_once,
    )
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.web_chat_runtime import _apply_terminal_task_update_and_settle

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        earlier = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            kind="queue_next_turn",
            content="fifo earlier successor",
        )
        steered = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=run_id,
            kind="steer_current_turn",
            content="Steer after terminal",
        )

        async def allow(**_kwargs):
            return HookResult()

        for receipt in (earlier, steered):
            await run_user_prompt_admission(
                db,
                authority=authority,
                input_id=receipt.input_id,
                worker_id="rollover-fifo-admission",
                hook_executor=allow,
            )

    waiting = await recover_session_input_dispatches_once(
        worker_id="rollover-fifo-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    assert waiting == {"claimed": 2, "dispatched": 1, "deferred": 1, "retried": 0}

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        await _apply_terminal_task_update_and_settle(
            db,
            task,
            status="completed",
            result_summary="Terminal before steer consumption.",
            metadata_json={},
            terminal_source="test_rollover_fifo_terminal",
        )
        await db.commit()

    deferred_rollover = await recover_terminal_target_session_inputs_once(
        worker_id="rollover-fifo-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    steer_successor_id = uuid.uuid5(steered.input_id, "session-v2-successor-run")
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, steered.input_id)
        admission = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == steered.input_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        assert deferred_rollover == {"claimed": 1, "dispatched": 0, "deferred": 1, "retried": 0}
        assert row is not None and row.status == "rolled_over"
        assert admission is not None and admission.dispatch_state == "pending"
        assert (admission.dispatch_receipt_json or {}).get("kind") == "successor"
        assert await db.get(RuntimeTask, steer_successor_id) is None

    # The regular dispatch sweep starts the FIFO-earlier successor first; the
    # rolled-over steer keeps waiting behind the now-active successor run.
    first_fifo = await recover_session_input_dispatches_once(
        worker_id="rollover-fifo-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    earlier_successor_id = uuid.uuid5(earlier.input_id, "session-v2-successor-run")
    async with owner_sessionmaker() as db:
        assert first_fifo == {"claimed": 2, "dispatched": 1, "deferred": 1, "retried": 0}
        assert await db.get(RuntimeTask, earlier_successor_id) is not None
        assert await db.get(RuntimeTask, steer_successor_id) is None

        earlier_run = await db.get(RuntimeTask, earlier_successor_id)
        assert earlier_run is not None
        await _apply_terminal_task_update_and_settle(
            db,
            earlier_run,
            status="completed",
            result_summary="FIFO successor finished.",
            metadata_json={},
            terminal_source="test_rollover_fifo_first_successor_terminal",
        )
        await db.commit()

    final_dispatch = await recover_session_input_dispatches_once(
        worker_id="rollover-fifo-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    async with owner_sessionmaker() as db:
        assert final_dispatch == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}
        steer_run = await db.get(RuntimeTask, steer_successor_id)
        assert steer_run is not None and steer_run.prompt == "Steer after terminal"
        assert (steer_run.metadata_json or {})["session_v2_rolled_over_input_id"] == str(steered.input_id)


async def test_terminal_steer_rollover_ack_loss_replay_never_duplicates(owner_sessionmaker) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.runtime_task_worker import (
        recover_session_input_dispatches_once,
        recover_terminal_target_session_inputs_once,
    )
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.web_chat_runtime import _apply_terminal_task_update_and_settle

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        steered = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=run_id,
            kind="steer_current_turn",
            content="Ack loss steer",
        )

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=steered.input_id,
            worker_id="rollover-ack-admission",
            hook_executor=allow,
        )

    await recover_session_input_dispatches_once(
        worker_id="rollover-ack-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        await _apply_terminal_task_update_and_settle(
            db,
            task,
            status="completed",
            result_summary="Terminal before steer consumption.",
            metadata_json={},
            terminal_source="test_rollover_ack_terminal",
        )
        await db.commit()

    await recover_terminal_target_session_inputs_once(
        worker_id="rollover-ack-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    successor_run_id = uuid.uuid5(steered.input_id, "session-v2-successor-run")

    # Simulate the ACK-loss window: the rollover and successor start committed
    # but the admission settle never ran, leaving a stale dispatching lease.
    async with owner_sessionmaker() as db:
        admission = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == steered.input_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        assert admission is not None
        admission.dispatch_state = "dispatching"
        admission.lease_owner = "rollover-ack-crashed-worker"
        admission.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        admission.version = int(admission.version) + 1
        await db.commit()

    stale_replay = await recover_session_input_dispatches_once(
        worker_id="rollover-ack-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    async with owner_sessionmaker() as db:
        admission = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == steered.input_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        row = await db.get(SessionTurnInput, steered.input_id)
        successor_tasks = await db.scalar(
            select(func.count())
            .select_from(RuntimeTask)
            .where(
                RuntimeTask.tenant_id == tenant_id,
                RuntimeTask.parent_session_id == str(session_id),
                RuntimeTask.id != run_id,
            )
        )
        rolled_over_events = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.event_type == "human_input.rolled_over",
            )
        )
        assert stale_replay == {"claimed": 1, "dispatched": 0, "deferred": 1, "retried": 0}
        assert row is not None and row.status == "rolled_over"
        assert admission is not None and admission.dispatch_state == "pending"
        assert successor_tasks == 1
        assert rolled_over_events == 1

    # Once the successor run itself terminalizes, the pending replay must
    # settle onto the SAME deterministic run instead of creating another.
    async with owner_sessionmaker() as db:
        successor = await db.get(RuntimeTask, successor_run_id)
        assert successor is not None
        await _apply_terminal_task_update_and_settle(
            db,
            successor,
            status="completed",
            result_summary="Successor finished.",
            metadata_json={},
            terminal_source="test_rollover_ack_successor_terminal",
        )
        await db.commit()

    settled_replay = await recover_session_input_dispatches_once(
        worker_id="rollover-ack-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    repeated_lane = await recover_terminal_target_session_inputs_once(
        worker_id="rollover-ack-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    async with owner_sessionmaker() as db:
        admission = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == steered.input_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        successor_tasks = await db.scalar(
            select(func.count())
            .select_from(RuntimeTask)
            .where(
                RuntimeTask.tenant_id == tenant_id,
                RuntimeTask.parent_session_id == str(session_id),
                RuntimeTask.id != run_id,
            )
        )
        rolled_over_events = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.event_type == "human_input.rolled_over",
            )
        )
        assert settled_replay == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}
        assert repeated_lane == {"claimed": 0, "dispatched": 0, "deferred": 0, "retried": 0}
        assert admission is not None and admission.dispatch_state == "dispatched"
        assert (admission.dispatch_receipt_json or {}).get("kind") == "runtime"
        assert successor_tasks == 1
        assert rolled_over_events == 1


@pytest.mark.parametrize("nonterminal_status", ["suspended", "resumable"])
async def test_dispatched_steer_is_never_rolled_over_while_target_run_is_nonterminal(
    owner_sessionmaker, nonterminal_status
) -> None:
    """DAY1-A2A-TERMINAL-ROLLOVER-REVIEW-001 Codex P1 negative pin.

    ``suspended`` (awaiting permission) and ``resumable`` are NONTERMINAL
    RuntimeTask states: the same run later becomes running again and can
    still bind the queued steer mailbox item, and the partial unique index
    ``uq_runtime_tasks_active_web_chat_session`` treats both as active.
    The terminal rollover lane must only roll steers whose target run is in
    the exact terminal set (completed/failed/killed/skipped/
    needs_reconciliation); it must never claim, re-attempt, or roll a steer
    away from its original live suspended/resumable target run.
    """

    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.runtime_task_worker import (
        recover_session_input_dispatches_once,
        recover_terminal_target_session_inputs_once,
    )
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        steered = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=run_id,
            kind="steer_current_turn",
            content=f"Steer into a {nonterminal_status} run",
        )

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=steered.input_id,
            worker_id=f"nonterminal-{nonterminal_status}-admission",
            hook_executor=allow,
        )

    dispatched = await recover_session_input_dispatches_once(
        worker_id=f"nonterminal-{nonterminal_status}-dispatch",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    assert dispatched == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}

    successor_run_id = uuid.uuid5(steered.input_id, "session-v2-successor-run")
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, steered.input_id)
        assert row is not None and row.status == "queued"
        assert row.target_run_id == run_id and row.bound_round_id is None
        # Truthful nonterminal transition: the awaiting-permission lane
        # suspends (and later resumes) the SAME RuntimeTask row; a resumable
        # run is equally recoverable.  Neither is a terminal settlement.
        task = await db.get(RuntimeTask, run_id)
        assert task is not None and task.status == "running"
        task.status = nonterminal_status
        await db.commit()
        settled_admission = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == steered.input_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        assert settled_admission is not None and settled_admission.dispatch_state == "dispatched"
        settled_attempts = int(settled_admission.dispatch_attempts)
        settled_version = int(settled_admission.version)
        settled_receipt = dict(settled_admission.dispatch_receipt_json or {})

    rollover = await recover_terminal_target_session_inputs_once(
        worker_id=f"nonterminal-{nonterminal_status}-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    # The locked replay recheck must also stay a pure replay for a live
    # suspended/resumable target: no terminal rollover, no requeue.
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        replay_receipt = await queue_admitted_human_input(db, authority=authority, input_id=steered.input_id)
        await db.commit()

    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, steered.input_id)
        admission = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == steered.input_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        command = await db.get(SessionCommand, steered.command_id)
        successor = await db.get(RuntimeTask, successor_run_id)
        session_tasks = await db.scalar(
            select(func.count())
            .select_from(RuntimeTask)
            .where(
                RuntimeTask.tenant_id == tenant_id,
                RuntimeTask.parent_session_id == str(session_id),
            )
        )
        rolled_over_events = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.event_type == "human_input.rolled_over",
            )
        )
        target = await db.get(RuntimeTask, run_id)
        assert rollover == {"claimed": 0, "dispatched": 0, "deferred": 0, "retried": 0}
        assert replay_receipt.replayed is True and replay_receipt.status == "queued"
        assert row is not None and row.status == "queued"
        assert row.target_run_id == run_id
        assert row.bound_round_id is None
        assert row.rolled_over_to_turn_id is None
        assert row.settlement_ref is None
        assert admission is not None and admission.dispatch_state == "dispatched"
        assert int(admission.dispatch_attempts) == settled_attempts
        assert int(admission.version) == settled_version
        assert dict(admission.dispatch_receipt_json or {}) == settled_receipt
        assert (admission.dispatch_receipt_json or {}).get("kind") == "mailbox"
        assert command is not None and command.status == "accepted"
        assert successor is None
        assert session_tasks == 1
        assert rolled_over_events == 0
        assert target is not None and target.status == nonterminal_status


@pytest.mark.parametrize("nonterminal_status", ["suspended", "resumable"])
async def test_auto_steer_to_nonterminal_active_like_run_stays_queued_on_target(
    owner_sessionmaker, nonterminal_status
) -> None:
    """DAY1-SESSION-ACTIVE-LIKE-STEER-001 initial-steer target validity.

    ``submit_live_human_input(requested_kind=\"auto\")`` resolves the session's
    active-like run through ``web_chat_runtime._find_active_run``, whose
    authoritative four-state set (mirroring the
    ``uq_runtime_tasks_active_web_chat_session`` partial unique index)
    includes suspended/resumable.  The initial steer target-validity check in
    ``queue_admitted_human_input`` must use the same active-like set: the
    steer stays queued on its original live target run instead of being
    rolled over (and then churning a successor against the ingress 409)
    merely because the run is awaiting permission / resumable.
    """

    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionInputAdmission, SessionTurnInput
    from app.models.user import User
    from app.services.session_live_input import submit_live_human_input

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    input_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        # Truthful nonterminal transition BEFORE the steer arrives: the
        # awaiting-permission lane suspends the SAME run; a resumable run is
        # equally recoverable.  Neither is a terminal settlement.
        task = await db.get(RuntimeTask, run_id)
        assert task is not None and task.status == "running"
        task.status = nonterminal_status
        await db.commit()

        agent = await db.get(Agent, agent_id)
        user = await db.get(User, user_id)
        session = await db.get(ChatSession, session_id)
        assert agent is not None and user is not None and session is not None
        payload = await submit_live_human_input(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content=f"Steer the {nonterminal_status} run",
            source=f"test_active_like_{nonterminal_status}",
            input_id=input_id,
            idempotency_key=f"active-like:{input_id}",
            requested_kind="auto",
        )

    successor_run_id = uuid.uuid5(input_id, "session-v2-successor-run")
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, input_id)
        command = await db.get(SessionCommand, row.command_id) if row is not None else None
        admission = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == input_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        target = await db.get(RuntimeTask, run_id)
        successor = await db.get(RuntimeTask, successor_run_id)
        session_tasks = await db.scalar(
            select(func.count())
            .select_from(RuntimeTask)
            .where(
                RuntimeTask.tenant_id == tenant_id,
                RuntimeTask.parent_session_id == str(session_id),
            )
        )
        rolled_over_events = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.event_type == "human_input.rolled_over",
            )
        )
        # requested_kind="auto" resolved through _find_active_run onto the SAME
        # suspended/resumable run, and dispatch mailed the steer into it.
        assert payload["intent"] == "steer_current_turn"
        assert payload["status"] == "queued"
        assert payload["target_run_id"] == str(run_id)
        assert payload["target_turn_id"] == f"turn-{run_id.hex}"
        assert payload["rolled_over_to_turn_id"] is None
        assert payload["admission_state"] == "admitted"
        assert payload["dispatch_status"] == "dispatched"
        assert (payload["dispatch"] or {}).get("kind") == "mailbox"
        assert payload["run"] is None
        assert row is not None and row.intent == "steer_current_turn"
        assert row.status == "queued"
        assert row.target_run_id == run_id
        assert row.bound_round_id is None
        assert row.rolled_over_to_turn_id is None
        assert row.settlement_ref is None
        assert command is not None and command.command_kind == "steer_current_turn"
        assert command.status == "accepted"
        assert (command.target_json or {}).get("expected_run_id") == str(run_id)
        assert admission is not None and admission.state == "admitted"
        assert admission.dispatch_state == "dispatched"
        assert admission.dispatch_last_error is None
        assert (admission.dispatch_receipt_json or {}).get("kind") == "mailbox"
        assert successor is None
        assert session_tasks == 1
        assert rolled_over_events == 0
        assert target is not None and target.status == nonterminal_status


@pytest.mark.parametrize("nonterminal_status", ["suspended", "resumable"])
async def test_queued_steer_binds_only_after_target_run_returns_to_executing(
    owner_sessionmaker, nonterminal_status
) -> None:
    """``bind_admitted_inputs_to_round`` stays executing-only (pending/running).

    A steer dispatched while the target run was running stays queued on that
    run when the run later suspends (awaiting permission) or becomes
    resumable.  The provider pre-dispatch bind boundary must NOT bind it
    while the run cannot execute; once the SAME run returns to running, the
    next provider round binds the SAME input to the SAME run/turn/round.
    This pins the boundary between the two mechanical status sets: widening
    initial target validity to the active-like set must never leak into the
    provider-round bindable set.
    """

    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.runtime_task_worker import recover_session_input_dispatches_once
    from app.services.session_human_input import bind_admitted_inputs_to_round
    from app.services.session_input_admission import run_user_prompt_admission

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    turn_id = f"turn-{run_id.hex}"
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        steered = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=run_id,
            kind="steer_current_turn",
            content=f"Bind me once the {nonterminal_status} run resumes",
        )

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=steered.input_id,
            worker_id=f"bind-{nonterminal_status}-admission",
            hook_executor=allow,
        )

    dispatched = await recover_session_input_dispatches_once(
        worker_id=f"bind-{nonterminal_status}-dispatch",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    assert dispatched == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}

    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, steered.input_id)
        assert row is not None and row.status == "queued" and row.target_run_id == run_id
        # The run now suspends (awaiting permission) / becomes resumable
        # WITHOUT binding the queued mailbox item.
        task = await db.get(RuntimeTask, run_id)
        assert task is not None and task.status == "running"
        task.status = nonterminal_status
        await db.commit()

        # The provider pre-dispatch bind boundary must not bind while the run
        # cannot execute a round.
        bound_while_nonterminal = await bind_admitted_inputs_to_round(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_id=f"round-{nonterminal_status}-1",
            model_request_snapshot_ref=f"snapshot:{nonterminal_status}:1",
        )
        await db.commit()
        assert bound_while_nonterminal == []
        row = await db.get(SessionTurnInput, steered.input_id)
        assert row is not None and row.status == "queued"
        assert row.bound_round_id is None
        assert row.model_request_snapshot_ref is None

        # The SAME run resumes; the next provider round binds the SAME input
        # to the SAME run/turn/round.
        task = await db.get(RuntimeTask, run_id)
        assert task is not None and task.status == nonterminal_status
        task.status = "running"
        await db.commit()
        bound_after_resume = await bind_admitted_inputs_to_round(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_id=f"round-{nonterminal_status}-2",
            model_request_snapshot_ref=f"snapshot:{nonterminal_status}:2",
        )
        await db.commit()
        assert [bound.id for bound in bound_after_resume] == [steered.input_id]
        row = await db.get(SessionTurnInput, steered.input_id)
        assert row is not None and row.status == "bound"
        assert row.target_run_id == run_id
        assert row.target_turn_id == turn_id
        assert row.bound_round_id == f"round-{nonterminal_status}-2"
        assert row.model_request_snapshot_ref == f"snapshot:{nonterminal_status}:2"
        bound_events = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.input_id == steered.input_id,
                ChatTranscriptEvent.event_type == "human_input.bound",
            )
        )
        assert bound_events == 1
        # An already-bound input is never re-bound by a later round.
        bound_replay = await bind_admitted_inputs_to_round(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_id=f"round-{nonterminal_status}-3",
            model_request_snapshot_ref=f"snapshot:{nonterminal_status}:3",
        )
        assert bound_replay == []


@pytest.mark.parametrize("nonterminal_status", ["suspended", "resumable"])
async def test_queue_next_turn_successor_defers_while_active_like_run_exists(
    owner_sessionmaker, nonterminal_status
) -> None:
    """DAY1-SESSION-ACTIVE-LIKE-STEER-001 FIFO successor active-like guard.

    A suspended (awaiting permission) or resumable run still OCCUPIES the
    session under ``uq_runtime_tasks_active_web_chat_session``.  A queued
    ``queue_next_turn`` must therefore stably defer its successor
    (``waiting_for_terminal``) instead of attempting a start that web-chat
    ingress correctly rejects with 409, which would retry-churn the
    admission on every worker sweep.  No new RuntimeTask may be created.
    """

    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.runtime_task_worker import recover_session_input_dispatches_once
    from app.services.session_input_admission import run_user_prompt_admission

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        assert task is not None and task.status == "running"
        task.status = nonterminal_status
        await db.commit()
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        queued = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            kind="queue_next_turn",
            content=f"Next turn behind a {nonterminal_status} run",
        )

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=queued.input_id,
            worker_id=f"successor-{nonterminal_status}-admission",
            hook_executor=allow,
        )

    first = await recover_session_input_dispatches_once(
        worker_id=f"successor-{nonterminal_status}-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    second = await recover_session_input_dispatches_once(
        worker_id=f"successor-{nonterminal_status}-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    successor_run_id = uuid.uuid5(queued.input_id, "session-v2-successor-run")
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, queued.input_id)
        admission = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == queued.input_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        successor = await db.get(RuntimeTask, successor_run_id)
        session_tasks = await db.scalar(
            select(func.count())
            .select_from(RuntimeTask)
            .where(
                RuntimeTask.tenant_id == tenant_id,
                RuntimeTask.parent_session_id == str(session_id),
            )
        )
        rolled_over_events = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.event_type == "human_input.rolled_over",
            )
        )
        target = await db.get(RuntimeTask, run_id)
        # Stable defer: each sweep re-claims the waiting admission, observes
        # the active-like run, and defers again WITHOUT a 409, an error, or a
        # retry count.
        assert first == {"claimed": 1, "dispatched": 0, "deferred": 1, "retried": 0}
        assert second == {"claimed": 1, "dispatched": 0, "deferred": 1, "retried": 0}
        assert row is not None and row.status == "queued"
        assert row.intent == "queue_next_turn"
        assert row.target_run_id is None
        assert row.target_turn_id is not None
        assert admission is not None and admission.dispatch_state == "pending"
        assert admission.dispatch_last_error is None
        assert dict(admission.dispatch_receipt_json or {}) == {
            "kind": "successor",
            "status": "waiting_for_terminal",
        }
        assert successor is None
        assert session_tasks == 1
        assert rolled_over_events == 0
        assert target is not None and target.status == nonterminal_status


@pytest.mark.parametrize("nonterminal_status", ["suspended", "resumable"])
async def test_queue_next_turn_successor_ignores_unrelated_non_chat_runtime_task(
    owner_sessionmaker, nonterminal_status
) -> None:
    """DAY1-SESSION-ACTIVE-LIKE-STEER-REVIEW-001 successor guard task-type scope.

    The authoritative active-like contract — the
    ``uq_runtime_tasks_active_web_chat_session`` partial unique index and
    ``web_chat_runtime._find_active_run`` — only treats EXECUTABLE CHAT task
    types (web_chat_turn/goal_continuation/team_member/advanced_plan) in an
    active-like status as occupying the session.  A legal non-chat
    RuntimeTask (workflow here) may share the same tenant/agent/session
    binding and be suspended/resumable WITHOUT occupying the web-chat
    session: ``start_web_chat_run`` does not 409 on it.  The FIFO successor
    guard must therefore ignore it: a queued ``queue_next_turn`` must start
    exactly one deterministic web-chat successor instead of deferring
    (``waiting_for_terminal``) forever behind the unrelated task.
    """

    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.runtime_task_worker import recover_session_input_dispatches_once
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.web_chat_runtime import _find_active_run

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None

    async def allow(**_kwargs):
        return HookResult()

    async with owner_sessionmaker() as db:
        # Retype the seeded row into a legal non-chat RuntimeTask bound to the
        # same tenant/agent/session.  Both the unique-index predicate and
        # ``_find_active_run`` exclude non-chat task types, so the session has
        # NO active executable-chat run even though this row is
        # suspended/resumable.
        task = await db.get(RuntimeTask, run_id)
        assert task is not None and task.status == "running"
        task.task_type = "workflow"
        task.status = nonterminal_status
        await db.commit()
        assert await _find_active_run(db, agent_id=agent_id, session_id=session_id) is None
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        queued = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            kind="queue_next_turn",
            content=f"Next turn beside a {nonterminal_status} workflow task",
        )
        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=queued.input_id,
            worker_id=f"non-chat-{nonterminal_status}-admission",
            hook_executor=allow,
        )

    first = await recover_session_input_dispatches_once(
        worker_id=f"non-chat-{nonterminal_status}-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    second = await recover_session_input_dispatches_once(
        worker_id=f"non-chat-{nonterminal_status}-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    successor_run_id = uuid.uuid5(queued.input_id, "session-v2-successor-run")
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, queued.input_id)
        admission = await db.scalar(
            select(SessionInputAdmission).where(
                SessionInputAdmission.input_id == queued.input_id,
                SessionInputAdmission.input_revision == 1,
            )
        )
        successor = await db.get(RuntimeTask, successor_run_id)
        workflow = await db.get(RuntimeTask, run_id)
        session_tasks = await db.scalar(
            select(func.count())
            .select_from(RuntimeTask)
            .where(
                RuntimeTask.tenant_id == tenant_id,
                RuntimeTask.parent_session_id == str(session_id),
            )
        )
        executable_chat_tasks = await db.scalar(
            select(func.count())
            .select_from(RuntimeTask)
            .where(
                RuntimeTask.tenant_id == tenant_id,
                RuntimeTask.parent_session_id == str(session_id),
                RuntimeTask.task_type.in_(("web_chat_turn", "goal_continuation", "team_member", "advanced_plan")),
            )
        )
        # The unrelated workflow row must not block the successor: exactly one
        # deterministic web-chat successor on the first sweep, a no-claim
        # replay on the second, and the workflow row untouched.
        assert first == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}
        assert second == {"claimed": 0, "dispatched": 0, "deferred": 0, "retried": 0}
        assert row is not None and row.intent == "queue_next_turn"
        assert row.status == "queued"
        # The started successor binds the queued input to itself
        # (``start_web_chat_run`` Session V2 binding).
        assert row.target_run_id == successor_run_id
        assert row.target_turn_id is not None
        assert admission is not None and admission.dispatch_state == "dispatched"
        assert admission.dispatch_last_error is None
        receipt = dict(admission.dispatch_receipt_json or {})
        assert receipt.get("kind") == "runtime"
        assert receipt.get("run_id") == str(successor_run_id)
        assert receipt.get("turn_id") == row.target_turn_id
        assert successor is not None
        assert successor.task_type == "web_chat_turn"
        assert successor.status == "pending"
        assert successor.prompt == f"Next turn beside a {nonterminal_status} workflow task"
        assert workflow is not None
        assert workflow.task_type == "workflow"
        assert workflow.status == nonterminal_status
        assert session_tasks == 2
        assert executable_chat_tasks == 1


@pytest.mark.parametrize("nonterminal_status", ["suspended", "resumable"])
async def test_steer_targeting_non_chat_runtime_task_rolls_over_to_successor(
    owner_sessionmaker, nonterminal_status
) -> None:
    """DAY1-SESSION-INPUT-TARGET-TYPE-001 initial steer target task-type gate.

    The authoritative session-occupancy contract — the
    ``uq_runtime_tasks_active_web_chat_session`` partial unique index and
    ``web_chat_runtime._find_active_run`` — only treats EXECUTABLE CHAT task
    types (web_chat_turn/goal_continuation/team_member/advanced_plan) as the
    session's active turn.  A suspended/resumable workflow RuntimeTask may
    legally share the same tenant/agent/session binding WITHOUT being a
    steer target: it has no web-chat provider round that could ever bind a
    queued steer, so the input would strand in the mailbox forever.  The
    initial target validity gate in ``queue_admitted_human_input`` must
    therefore judge executable-chat task type AND active-like status, and
    settle the steer through the existing invalid-target branch: with
    ``terminal_fallback=queue_next_turn`` it rolls over to the deterministic
    successor turn exactly once, with zero bind and zero orphan, and a
    replay never duplicates the successor.
    """

    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.runtime_task_worker import recover_session_input_dispatches_once
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    async with owner_sessionmaker() as db:
        # Retype the seeded row into a legal non-chat RuntimeTask bound to the
        # same tenant/agent/session, suspended/resumable, with an explicit
        # metadata turn_id that matches the steer's ``expected_turn_id``.
        task = await db.get(RuntimeTask, run_id)
        assert task is not None and task.status == "running"
        task.task_type = "workflow"
        task.status = nonterminal_status
        await db.commit()
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        accepted = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=run_id,
            kind="steer_current_turn",
            content=f"Steer at a {nonterminal_status} workflow task",
        )

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=accepted.input_id,
            worker_id=f"non-chat-steer-{nonterminal_status}-admission",
            hook_executor=allow,
        )
        settled = await queue_admitted_human_input(db, authority=authority, input_id=accepted.input_id)
        await db.commit()
        # The workflow task is not a valid steer target: the steer must roll
        # over to its deterministic successor turn instead of queueing onto a
        # run that can never bind it.
        assert settled.status == "rolled_over"
        assert settled.rolled_over_to_turn_id
        kinds = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent.event_type)
                    .where(ChatTranscriptEvent.session_id == session_id)
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert kinds[-3:] == ["human_input.rolled_over", "turn.accepted", "turn.queued"]

    dispatched = await recover_session_input_dispatches_once(
        worker_id=f"non-chat-steer-{nonterminal_status}-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    replay = await recover_session_input_dispatches_once(
        worker_id=f"non-chat-steer-{nonterminal_status}-worker",
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
    )
    successor_run_id = uuid.uuid5(accepted.input_id, "session-v2-successor-run")
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, accepted.input_id)
        successor = await db.get(RuntimeTask, successor_run_id)
        workflow = await db.get(RuntimeTask, run_id)
        rollover_events = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.event_type == "human_input.rolled_over",
            )
        )
        assert dispatched == {"claimed": 1, "dispatched": 1, "deferred": 0, "retried": 0}
        assert replay == {"claimed": 0, "dispatched": 0, "deferred": 0, "retried": 0}
        assert row is not None and row.status == "rolled_over"
        assert row.target_run_id is None
        # Zero bind / zero orphan: the workflow run never bound the steer and
        # exactly one rollover was recorded, never duplicated by the replay.
        assert row.bound_round_id is None
        assert rollover_events == 1
        assert successor is not None
        assert successor.task_type == "web_chat_turn"
        assert (successor.metadata_json or {})["session_v2_rolled_over_input_id"] == str(accepted.input_id)
        assert workflow is not None
        assert workflow.task_type == "workflow"
        assert workflow.status == nonterminal_status


@pytest.mark.parametrize("nonterminal_status", ["suspended", "resumable"])
async def test_answer_targeting_non_chat_runtime_task_is_typed_target_reject(
    owner_sessionmaker, nonterminal_status
) -> None:
    """DAY1-SESSION-INPUT-TARGET-TYPE-001 answer derived-run task-type gate.

    An ``answer_request`` derives its target run from the waiting
    ``user_question`` event scope.  When that scope names a non-chat
    RuntimeTask (workflow here) — even one whose DEFAULT turn id
    (``turn-<task.hex>``) matches and whose status is active-like — the same
    mechanical target validity gate must apply: the answer settles as a
    typed ``target_run_not_active`` reject through the existing
    invalid-target branch, never as a queued mailbox item that no web-chat
    provider round could ever bind.
    """

    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_v2_persistence import SessionEventDraft, accept_human_input, append_session_events

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    question_id = uuid.uuid4()
    run_scope = {
        "level": "run",
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "turn_id": f"turn-{run_id.hex}",
        "run_id": str(run_id),
    }
    async with owner_sessionmaker() as db:
        # Retype into a non-chat RuntimeTask and DROP the metadata turn_id so
        # the gate resolves the DEFAULT turn id (``turn-<task.hex>``): the
        # answer still matches turn/run on a workflow task and must be
        # rejected by task type, not by turn or status.
        task = await db.get(RuntimeTask, run_id)
        assert task is not None and task.status == "running"
        task.task_type = "workflow"
        task.status = nonterminal_status
        task.metadata_json = {}
        await db.commit()
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            drafts=[
                SessionEventDraft(
                    item_id=question_id,
                    item_kind="user_question",
                    lifecycle="waiting",
                    scope=run_scope,
                    actor={"type": "runtime"},
                    payload={"question": "Workflow approval?"},
                )
            ],
        )
        await db.commit()
        input_id = uuid.uuid4()
        accepted = await accept_human_input(
            db,
            authority=authority,
            intent={
                "kind": "answer_request",
                "input_id": str(input_id),
                "idempotency_key": f"answer:{input_id}",
                "session_id": str(session_id),
                "request_item_id": str(question_id),
                "content_parts": [{"type": "text", "text": "Approve"}],
            },
        )

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=input_id,
            worker_id=f"non-chat-answer-{nonterminal_status}-admission",
            hook_executor=allow,
        )
        settled = await queue_admitted_human_input(db, authority=authority, input_id=input_id)
        await db.commit()
        command = await db.get(SessionCommand, accepted.command_id)
        row = await db.get(SessionTurnInput, input_id)
        rejected_events = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.input_id == input_id,
                ChatTranscriptEvent.event_type == "human_input.rejected",
            )
        )
        assert settled.status == "rejected"
        assert settled.reason_code == "target_run_not_active"
        assert command is not None and command.status == "rejected"
        assert row is not None and row.status == "rejected"
        assert row.bound_round_id is None
        assert rejected_events == 1


async def test_round_binding_consumes_only_admitted_inputs_in_fifo_order(owner_sessionmaker) -> None:
    from app.runtime.hooks import HookResult
    from app.services.session_human_input import (
        bind_admitted_inputs_to_round,
        mark_bound_inputs_applied,
        queue_admitted_human_input,
    )
    from app.services.session_input_admission import run_user_prompt_admission

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)

        async def allow(**_kwargs):
            return HookResult()

        receipts = []
        for _ in range(2):
            accepted = await _accepted_input(
                db,
                authority=authority,
                session_id=session_id,
                run_id=run_id,
                kind="steer_current_turn",
            )
            await run_user_prompt_admission(
                db,
                authority=authority,
                input_id=accepted.input_id,
                worker_id=f"worker-{accepted.input_id}",
                hook_executor=allow,
            )
            receipts.append(await queue_admitted_human_input(db, authority=authority, input_id=accepted.input_id))
            await db.commit()

        bound = await bind_admitted_inputs_to_round(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=f"turn-{run_id.hex}",
            round_id="round-2",
            model_request_snapshot_ref="model-request:round-2",
        )
        await db.commit()
        assert [row.id for row in bound] == [item.input_id for item in receipts]
        assert all(row.status == "bound" for row in bound)
        applied = await mark_bound_inputs_applied(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            round_id="round-2",
            provider_response_ref="provider-response:round-2",
        )
        await db.commit()
        assert [row.id for row in applied] == [item.input_id for item in receipts]
        assert all(row.status == "applied" for row in applied)


async def test_model_round_persists_exact_snapshot_before_applying_bound_inputs(owner_sessionmaker) -> None:
    from types import SimpleNamespace

    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionModelResult, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_model_round import (
        bind_round_inputs,
        commit_model_response,
        prepare_model_request,
    )

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(
        owner_sessionmaker,
        active_run=True,
    )
    assert run_id is not None
    turn_id = f"turn-{run_id.hex}"

    async def allow(**_kwargs):
        return HookResult()

    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        receipts = []
        for text in ("first FIFO evidence", "second FIFO evidence"):
            accepted = await _accepted_input(
                db,
                authority=authority,
                session_id=session_id,
                run_id=run_id,
                kind="steer_current_turn",
            )
            row = await db.get(SessionTurnInput, accepted.input_id)
            assert row is not None
            row.content_parts_json = [{"type": "text", "text": text}]
            await run_user_prompt_admission(
                db,
                authority=authority,
                input_id=accepted.input_id,
                worker_id=f"model-round-{accepted.input_id}",
                hook_executor=allow,
            )
            receipts.append(await queue_admitted_human_input(db, authority=authority, input_id=accepted.input_id))
            await db.commit()

        bound_messages = await bind_round_inputs(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=2,
        )
        await db.commit()
        assert [message["content"] for message in bound_messages] == [
            "first FIFO evidence",
            "second FIFO evidence",
        ]

        provider_messages = [
            SimpleNamespace(role="system", content="SYSTEM"),
            SimpleNamespace(role="user", content="first FIFO evidence"),
            SimpleNamespace(role="user", content="second FIFO evidence"),
        ]
        provider_request_id = await prepare_model_request(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=2,
            messages=provider_messages,
            tools=[{"type": "function", "function": {"name": "read_file"}}],
            provider="openai",
            model="gpt-4.1",
        )
        await db.commit()
        result = await db.scalar(
            select(SessionModelResult).where(SessionModelResult.provider_request_id == provider_request_id)
        )
        assert result is not None and result.state == "prepared"
        assert result.model_request_snapshot_json["messages"] == [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "first FIFO evidence"},
            {"role": "user", "content": "second FIFO evidence"},
        ]
        assert result.bound_input_ids_json == [str(receipt.input_id) for receipt in receipts]
        assert result.model_request_snapshot_json["request_fence"] == {
            "hive_provider_request_id": provider_request_id,
            "provider_idempotency_key_applied": False,
            "provider_idempotency_supported": False,
        }
        bound_rows = [await db.get(SessionTurnInput, receipt.input_id) for receipt in receipts]
        assert all(row is not None and row.status == "bound" for row in bound_rows)

        await commit_model_response(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=2,
            provider_request_id=provider_request_id,
            response={"content_present": True, "tool_call_count": 0, "usage": {"total_tokens": 11}},
        )
        await db.commit()
        event_count = await db.scalar(
            select(func.count()).select_from(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session_id)
        )
        await commit_model_response(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=2,
            provider_request_id=provider_request_id,
            response={"content_present": True, "tool_call_count": 0, "usage": {"total_tokens": 11}},
        )
        await db.commit()
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(ChatTranscriptEvent.session_id == session_id)
            )
            == event_count
        )

    async with owner_sessionmaker() as db:
        result = await db.scalar(
            select(SessionModelResult).where(SessionModelResult.provider_request_id == provider_request_id)
        )
        assert result is not None and result.state == "round_committed"
        rows = list(
            (
                await db.execute(
                    select(SessionTurnInput)
                    .where(SessionTurnInput.id.in_([receipt.input_id for receipt in receipts]))
                    .order_by(SessionTurnInput.queue_ordinal)
                )
            ).scalars()
        )
        assert [row.status for row in rows] == ["applied", "applied"]
        model_and_input_lifecycles = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent.event_type)
                    .where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.event_type.in_(
                            (
                                "result_commit.sealed",
                                "human_input.applied",
                                "result_commit.round_committed",
                            )
                        ),
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert model_and_input_lifecycles == [
            "result_commit.sealed",
            "human_input.applied",
            "human_input.applied",
            "result_commit.round_committed",
        ]


async def test_first_provider_round_binds_canonical_start_input(owner_sessionmaker) -> None:
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.session_v2 import SessionTurnInput
    from app.models.user import User
    from app.services.session_live_input import submit_live_human_input
    from app.services.session_model_round import bind_round_inputs

    tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    input_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        user = await db.get(User, user_id)
        session = await db.get(ChatSession, session_id)
        assert agent is not None and user is not None and session is not None
        receipt = await submit_live_human_input(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content="canonical first-turn bytes",
            source="provider-round-test",
            input_id=input_id,
            idempotency_key=f"first-round:{input_id}",
        )
        run_id = uuid.UUID(str(receipt["run"]["run_id"]))
        row = await db.get(SessionTurnInput, input_id)
        assert row is not None and row.target_turn_id is not None
        messages = await bind_round_inputs(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=row.target_turn_id,
            round_index=1,
        )
        await db.commit()
        assert messages == [
            {
                "role": "user",
                "content": "canonical first-turn bytes",
                "session_input_id": str(input_id),
                "bound_round_id": f"{run_id}:round:1",
            }
        ]
        assert (await db.get(SessionTurnInput, input_id)).status == "bound"


async def test_prevented_hook_context_is_carried_once_into_next_turn_provider_snapshot(
    owner_sessionmaker,
) -> None:
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCarryForward, SessionTurnInput
    from app.models.user import User
    from app.runtime.hooks import HookResult
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_live_input import submit_live_human_input
    from app.services.session_model_round import bind_round_inputs, commit_model_response, prepare_model_request

    tenant_id, user_id, agent_id, session_id, old_run_id = await _seed_session(
        owner_sessionmaker,
        active_run=True,
    )
    assert old_run_id is not None
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        prevented = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=old_run_id,
            kind="steer_current_turn",
        )

        async def prevent(**_kwargs):
            return HookResult(
                prevent_continuation=True,
                stop_reason="wait for next turn",
                additional_contexts=["trusted carried context bytes"],
            )

        outcome = await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=prevented.input_id,
            worker_id="prevent-carry-test",
            hook_executor=prevent,
        )
        assert outcome.state == "cancelled"
        old_run = await db.get(RuntimeTask, old_run_id)
        assert old_run is not None
        old_run.status = "completed"
        await db.commit()

        agent = await db.get(Agent, agent_id)
        user = await db.get(User, user_id)
        session = await db.get(ChatSession, session_id)
        assert agent is not None and user is not None and session is not None
        next_input_id = uuid.uuid4()
        start = await submit_live_human_input(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content="next admitted turn",
            source="carry-forward-test",
            input_id=next_input_id,
            idempotency_key=f"carry-next:{next_input_id}",
        )
        next_run_id = uuid.UUID(str(start["run"]["run_id"]))
        next_input = await db.get(SessionTurnInput, next_input_id)
        assert next_input is not None and next_input.target_turn_id is not None
        messages = await bind_round_inputs(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=next_run_id,
            turn_id=next_input.target_turn_id,
            round_index=1,
        )
        await db.commit()
        assert messages[0]["content"] == "next admitted turn"
        assert messages[1]["role"] == "system"
        assert messages[1]["content"] == "trusted carried context bytes"
        carry = await db.scalar(
            select(SessionCarryForward).where(SessionCarryForward.source_input_id == prevented.input_id)
        )
        assert carry is not None and carry.state == "round_bound"

        provider_request_id = await prepare_model_request(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=next_run_id,
            turn_id=next_input.target_turn_id,
            round_index=1,
            messages=messages,
            tools=None,
            provider="openai",
            model="gpt-4.1",
        )
        await db.commit()
        await commit_model_response(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=next_run_id,
            turn_id=next_input.target_turn_id,
            round_index=1,
            provider_request_id=provider_request_id,
            response={"content_present": True, "tool_call_count": 0, "usage": {}},
        )
        await db.commit()
        carry = await db.get(SessionCarryForward, carry.id)
        assert carry is not None and carry.state == "consumed"
        assert carry.consumed_event_id is not None
        assert carry.model_request_snapshot_ref


async def test_prepared_provider_request_is_not_replayed_by_a_new_runtime_claim(owner_sessionmaker) -> None:
    from app.models.session_v2 import SessionModelResult
    from app.services.session_model_round import (
        ModelRoundNeedsReconciliation,
        bind_round_inputs,
        prepare_model_request,
    )

    tenant_id, _user_id, agent_id, session_id, run_id = await _seed_session(
        owner_sessionmaker,
        active_run=True,
    )
    assert run_id is not None
    turn_id = f"turn-{run_id.hex}"
    messages = [{"role": "user", "content": "exact provider bytes"}]
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
        await db.commit()
        provider_request_id = await prepare_model_request(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            messages=messages,
            tools=None,
            provider="openai",
            model="gpt-4.1",
            attempt_owner="worker-a:claim-1",
        )
        await db.commit()
        with pytest.raises(ModelRoundNeedsReconciliation):
            await prepare_model_request(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                round_index=1,
                messages=messages,
                tools=None,
                provider="openai",
                model="gpt-4.1",
                attempt_owner="worker-b:claim-2",
            )
        await db.commit()
        result = await db.scalar(
            select(SessionModelResult).where(SessionModelResult.provider_request_id == provider_request_id)
        )
        assert result is not None and result.state == "needs_reconciliation"
        assert "ambiguous_owner" in str(result.reconciliation_owner)


async def test_ambiguous_provider_failure_fences_result_run_and_event_chain(owner_sessionmaker) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services.session_model_round import bind_round_inputs, fail_model_request, prepare_model_request

    tenant_id, _user_id, agent_id, session_id, run_id = await _seed_session(
        owner_sessionmaker,
        active_run=True,
    )
    assert run_id is not None
    turn_id = f"turn-{run_id.hex}"
    async with owner_sessionmaker() as db:
        messages = await bind_round_inputs(
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
            messages=messages,
            tools=None,
            provider="openai",
            model="gpt-4.1",
            attempt_owner="worker-a:claim-1",
        )
        await db.commit()

        await fail_model_request(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            provider_request_id=provider_request_id,
            error_class="timeout",
            retry_safe=False,
        )
        await db.commit()

        result = await db.scalar(
            select(SessionModelResult).where(SessionModelResult.provider_request_id == provider_request_id)
        )
        task = await db.get(RuntimeTask, run_id)
        event_types = set(
            (
                await db.execute(
                    select(ChatTranscriptEvent.event_type).where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.event_type.in_(
                            ("result_commit.needs_reconciliation", "run.needs_reconciliation")
                        ),
                    )
                )
            ).scalars()
        )

        assert result is not None and result.state == "needs_reconciliation"
        assert result.seal_json == {
            "delivery_state": "unknown",
            "error_class": "timeout",
            "retry_safe": False,
        }
        assert task is not None and task.status == "needs_reconciliation"
        assert task.metadata_json["session_v2_reconciliation"]["provider_request_id"] == provider_request_id
        assert event_types == {"result_commit.needs_reconciliation", "run.needs_reconciliation"}


async def test_provider_reconciliation_handler_settles_once_after_canonical_fail_commit(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    """RC-10A: the ambiguous-send failure handler must not re-open a fenced run.

    ``fail_model_request(retry_safe=False)`` is the sole canonical terminal
    writer for an ambiguous provider send; committing that settlement bumps
    claim_version 1 -> 2. The run failure handler that afterwards receives
    ProviderRequestNeedsReconciliation must keep the stale worker fence
    intact (no second RuntimeTask write through the old claim), while still
    broadcasting the terminal reconciliation state exactly once.
    """

    from dataclasses import replace
    from types import SimpleNamespace

    import app.database as database
    from sqlalchemy import update

    from app.kernel.contracts import ProviderRequestNeedsReconciliation
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_root_item import RuntimeRootItem
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionControlInput, SessionModelResult
    from app.services import web_chat_run_orchestrator as orchestrator
    from app.services import web_chat_runtime
    from app.services.runtime_root_ledger import register_runtime_root_item
    from app.services.runtime_task_fence import (
        reset_runtime_task_fence,
        set_runtime_task_fence,
    )
    from app.services.session_control_input import accept_cancel_control_input
    from app.services.session_model_round import bind_round_inputs, prepare_model_request

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(
        owner_sessionmaker,
        active_run=True,
    )
    assert run_id is not None
    turn_id = f"turn-{run_id.hex}"
    cancel_control_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        await db.execute(
            update(RuntimeTask).where(RuntimeTask.id == run_id).values(claim_version=1, claimed_by="worker-a")
        )
        await register_runtime_root_item(
            db,
            tenant_id=tenant_id,
            root_runtime_task_id=run_id,
            source_agent_id=agent_id,
            intent_key=f"web-chat-turn:{run_id.hex}",
            work_type="web_chat_turn",
            target_ref=str(run_id),
            runtime_task_id=run_id,
            root_user_id=user_id,
            root_session_id=str(session_id),
            state="queued",
            admission_disposition="admitted",
        )
        await db.flush()
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        control_receipt = await accept_cancel_control_input(
            db,
            authority=authority,
            control_id=cancel_control_id,
            idempotency_key=f"cancel:{run_id}",
            expected_run_id=run_id,
        )
        await db.commit()
    assert control_receipt.status == "accepted"

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    monkeypatch.setattr(web_chat_runtime, "_async_session", owner_sessionmaker)

    ports = web_chat_runtime._web_chat_run_ports()
    broadcasts: list[dict] = []

    async def record_broadcast(_agent_id, _session_id, payload):
        broadcasts.append(dict(payload))

    ports = replace(ports, events=replace(ports.events, broadcast=record_broadcast))
    state = orchestrator._WebChatRunState(
        run_uuid=run_id,
        ports=ports,
        cancel_event=asyncio.Event(),
        run_key=run_id.hex,
        broadcast_token=None,
        agent=SimpleNamespace(id=agent_id, tenant_id=tenant_id),
        session_id=str(session_id),
        metadata={"turn_id": turn_id},
    )

    token = set_runtime_task_fence(task_id=run_id, claim_version=1, worker_id="worker-a")
    try:
        async with owner_sessionmaker() as db:
            messages = await bind_round_inputs(
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
                messages=messages,
                tools=None,
                provider="openai",
                model="gpt-4.1",
                attempt_owner="worker-a:1:1",
            )
            await db.commit()

        # Real kernel failure settlement: the canonical terminal commit runs
        # inside the worker fence and bumps claim_version 1 -> 2.
        await orchestrator._fail_session_model_request(
            state,
            round_index=1,
            provider_request_id=provider_request_id,
            error_class="read_error",
            delivery_state="unknown",
            retry_safe=False,
        )

        # The production failure path the kernel raises afterwards.
        await orchestrator._handle_web_chat_failure(
            state,
            ProviderRequestNeedsReconciliation(
                provider_request_id=provider_request_id,
                error_class="read_error",
            ),
        )
    finally:
        reset_runtime_task_fence(token)

    reconciliation_events = [
        payload for payload in broadcasts if payload.get("type") == "runtime_reconciliation_required"
    ]
    assert len(reconciliation_events) == 1
    assert reconciliation_events[0]["provider_request_id"] == provider_request_id
    assert reconciliation_events[0]["retryable"] is False

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        result = await db.scalar(
            select(SessionModelResult).where(SessionModelResult.provider_request_id == provider_request_id)
        )
        root_item = await db.scalar(select(RuntimeRootItem).where(RuntimeRootItem.runtime_task_id == run_id))
        control = await db.get(SessionControlInput, cancel_control_id)
        control_command = await db.scalar(select(SessionCommand).where(SessionCommand.id == control.command_id))
        control_events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent.event_type).where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.item_id == cancel_control_id,
                        ChatTranscriptEvent.event_type == "control_input.rejected",
                    )
                )
            ).scalars()
        )
        event_types = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent.event_type).where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.event_type.in_(
                            ("result_commit.needs_reconciliation", "run.needs_reconciliation")
                        ),
                    )
                )
            ).scalars()
        )

    assert task is not None and task.status == "needs_reconciliation"
    assert task.claim_version == 2
    assert task.completed_at is not None
    assert task.metadata_json["session_v2_reconciliation"]["reason"] == "ambiguous_provider_send"
    assert task.metadata_json["session_v2_reconciliation"]["provider_request_id"] == provider_request_id
    # The canonical fail commit owns the terminal settlement; the failure
    # handler must not have rewritten it through the stale worker fence.
    assert task.metadata_json["terminal_commit_source"] == "session_model_round:ambiguous_provider_send"
    assert task.metadata_json["terminal_committed_status"] == "needs_reconciliation"
    assert task.metadata_json["terminal_execution_fence_ref"]
    assert root_item is not None and root_item.state == "needs_reconciliation"
    assert root_item.metadata_json["terminal_execution_fence_ref"] == task.metadata_json["terminal_execution_fence_ref"]
    # The pending cancel control settles exactly once with the canonical
    # terminal commit: typed rejection, one settlement receipt, one event.
    assert control is not None and control.status == "rejected"
    assert control.settlement_ref
    assert control_command is not None and control_command.status == "rejected"
    assert control_command.rejection_json == {"reason_code": "run_terminal_before_cancel_effect"}
    assert control_command.receipt_ref == control.settlement_ref
    assert control_events == ["control_input.rejected"]
    assert result is not None and result.state == "needs_reconciliation"
    assert result.seal_json == {
        "delivery_state": "unknown",
        "error_class": "read_error",
        "retry_safe": False,
    }
    assert sorted(event_types) == [
        "result_commit.needs_reconciliation",
        "run.needs_reconciliation",
    ]


async def test_blocked_replacement_creates_no_saga_and_does_not_cancel_old_run(owner_sessionmaker) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionTurnReplacement
    from app.runtime.hooks import HookResult
    from app.services.session_input_admission import run_user_prompt_admission

    _tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        accepted = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=run_id,
            kind="interrupt_and_replace",
        )

        async def block(**_kwargs):
            return HookResult(block=True, reason="blocked")

        outcome = await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=accepted.input_id,
            worker_id="replacement-worker",
            hook_executor=block,
        )
        await db.commit()
        task = await db.get(RuntimeTask, run_id)
        assert outcome.state == "rejected"
        assert task is not None and task.status == "running"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SessionTurnReplacement)
                .where(SessionTurnReplacement.session_id == session_id)
            )
            == 0
        )


async def test_admitted_replacement_creates_input_first_saga_without_cancel_or_turn(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionCommand, SessionControlInput, SessionTurnReplacement
    from app.runtime.hooks import HookResult
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_turn_replacement import request_turn_replacement

    _tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        receipt = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=run_id,
            kind="interrupt_and_replace",
        )

        async def allow(**_kwargs):
            return HookResult()

        admitted = await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=receipt.input_id,
            worker_id="admission-worker",
            hook_executor=allow,
        )
        await db.commit()
        assert admitted.state == "admitted"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SessionTurnReplacement)
                .where(SessionTurnReplacement.session_id == session_id)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SessionControlInput)
                .where(SessionControlInput.session_id == session_id)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.item_kind == "turn",
                )
            )
            == 0
        )

        requested = await request_turn_replacement(
            db,
            authority=authority,
            input_id=receipt.input_id,
        )
        await db.commit()
        replay = await request_turn_replacement(
            db,
            authority=authority,
            input_id=receipt.input_id,
        )

        saga = await db.get(SessionTurnReplacement, requested.saga_id)
        saga_command = await db.get(SessionCommand, requested.saga_command_id)
        assert saga is not None and saga.state == "requested"
        assert saga.cancel_control_id is None and saga.cancel_command_id is None
        assert saga_command is not None and saga_command.causation_command_id == receipt.command_id
        assert replay.saga_id == requested.saga_id and replay.replayed is True
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SessionControlInput)
                .where(SessionControlInput.session_id == session_id)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.item_kind == "turn_replacement",
                    ChatTranscriptEvent.lifecycle == "requested",
                )
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.item_kind == "turn",
                )
            )
            == 0
        )


async def test_replacement_child_cancel_and_turn_admission_are_fenced_and_idempotent(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionControlInput, SessionTurnReplacement
    from app.runtime.hooks import HookResult
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_turn_replacement import (
        accept_replacement_cancel,
        admit_replacement_run,
        begin_replacement_cancel,
        complete_turn_replacement,
        fence_replacement_old_run,
        queue_fenced_replacement,
        request_turn_replacement,
    )
    from app.services.web_chat_runtime import _apply_terminal_task_update_and_settle

    _tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        input_receipt = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=run_id,
            kind="interrupt_and_replace",
        )

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=input_receipt.input_id,
            worker_id="admission-worker",
            hook_executor=allow,
        )
        await db.commit()
        requested = await request_turn_replacement(
            db,
            authority=authority,
            input_id=input_receipt.input_id,
        )
        await db.commit()

        cancelling = await accept_replacement_cancel(
            db,
            authority=authority,
            saga_id=requested.saga_id,
        )
        await db.commit()
        cancelling_replay = await accept_replacement_cancel(
            db,
            authority=authority,
            saga_id=requested.saga_id,
        )
        saga = await db.get(SessionTurnReplacement, requested.saga_id)
        child = await db.get(SessionCommand, cancelling.cancel_command_id)
        control = await db.get(SessionControlInput, cancelling.cancel_control_id)
        assert saga is not None and saga.state == "cancel_accepted"
        assert child is not None and child.causation_command_id == requested.saga_command_id
        assert control is not None and control.expected_run_id == run_id
        assert cancelling_replay.cancel_command_id == cancelling.cancel_command_id
        assert cancelling_replay.replayed is True
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.item_kind == "turn",
                )
            )
            == 0
        )

        await begin_replacement_cancel(
            db,
            authority=authority,
            saga_id=requested.saga_id,
            worker_id="replacement-worker",
        )
        await db.commit()
        old_run = await db.get(RuntimeTask, run_id)
        assert old_run is not None
        await _apply_terminal_task_update_and_settle(
            db,
            old_run,
            status="killed",
            result_summary="Generation stopped by replacement cancel.",
            metadata_json={"cancel_control_id": str(cancelling.cancel_control_id)},
            terminal_source="test_replacement_terminal_owner",
        )
        await db.commit()
        fenced = await fence_replacement_old_run(
            db,
            authority=authority,
            saga_id=requested.saga_id,
        )
        await db.commit()
        queued = await queue_fenced_replacement(
            db,
            authority=authority,
            saga_id=requested.saga_id,
        )
        await db.commit()
        queued_replay = await queue_fenced_replacement(
            db,
            authority=authority,
            saga_id=requested.saga_id,
        )

        saga = await db.get(SessionTurnReplacement, requested.saga_id)
        old_run = await db.get(RuntimeTask, run_id)
        assert fenced.state == "old_run_fenced"
        assert saga is not None and saga.state == "replacement_queued"
        assert old_run is not None and old_run.status == "killed"
        assert queued.replacement_turn_id == requested.replacement_turn_id
        assert queued_replay.replayed is True
        turn_events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent.lifecycle)
                    .where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.item_kind == "turn",
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert turn_events == ["accepted", "queued"]

        replacement_run_id = uuid.uuid5(requested.saga_id, "replacement-run")
        db.add(
            RuntimeTask(
                id=replacement_run_id,
                task_type="web_chat_turn",
                status="pending",
                parent_agent_id=agent_id,
                child_agent_id=agent_id,
                tenant_id=_tenant_id,
                parent_session_id=str(session_id),
                child_session_id=str(session_id),
                root_user_id=user_id,
                root_session_id=str(session_id),
                root_runtime_task_id=replacement_run_id,
                prompt="replacement",
                metadata_json={"turn_id": requested.replacement_turn_id},
            )
        )
        await db.flush()
        admitted = await admit_replacement_run(
            db,
            authority=authority,
            saga_id=requested.saga_id,
            run_id=replacement_run_id,
        )
        await db.commit()
        completed = await complete_turn_replacement(
            db,
            authority=authority,
            saga_id=requested.saga_id,
        )
        await db.commit()
        completed_replay = await complete_turn_replacement(
            db,
            authority=authority,
            saga_id=requested.saga_id,
        )
        saga_command = await db.get(SessionCommand, requested.saga_command_id)
        parent_command = await db.get(SessionCommand, input_receipt.command_id)
        assert admitted.state == "replacement_admitted"
        assert completed.state == "completed"
        assert completed_replay.replayed is True
        assert saga_command is not None and saga_command.status == "applied"
        assert parent_command is not None and parent_command.status == "accepted"
        replacement_lifecycles = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent.lifecycle)
                    .where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.item_kind == "turn_replacement",
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert replacement_lifecycles == ["requested", "cancelling", "fenced", "queued", "admitted", "completed"]


async def test_replacement_durable_owner_recovers_every_state_and_keeps_input_settlement_separate(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionControlInput, SessionTurnInput, SessionTurnReplacement
    from app.runtime.hooks import HookResult
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_model_round import bind_round_inputs, commit_model_response, prepare_model_request
    from app.services.session_turn_replacement import (
        admit_replacement_run,
        recover_turn_replacements_once,
        request_turn_replacement,
    )
    from app.services.web_chat_runtime import _apply_terminal_task_update_and_settle

    tenant_id, user_id, agent_id, session_id, old_run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert old_run_id is not None
    signals: list[uuid.UUID] = []
    starts: list[uuid.UUID] = []

    async def signal(*, run_id, **_kwargs):
        signals.append(uuid.UUID(str(run_id)))

    async def start_replacement(*, db, authority, saga, run_id, **_kwargs):
        starts.append(run_id)
        db.add(
            RuntimeTask(
                id=run_id,
                task_type="web_chat_turn",
                status="pending",
                parent_agent_id=agent_id,
                child_agent_id=agent_id,
                tenant_id=tenant_id,
                parent_session_id=str(session_id),
                child_session_id=str(session_id),
                root_user_id=user_id,
                root_session_id=str(session_id),
                root_runtime_task_id=run_id,
                prompt="replacement",
                metadata_json={"turn_id": saga.replacement_turn_id},
            )
        )
        await db.flush()
        await admit_replacement_run(db, authority=authority, saga_id=saga.id, run_id=run_id)

    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        input_receipt = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=old_run_id,
            kind="interrupt_and_replace",
        )

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=input_receipt.input_id,
            worker_id="replacement-admission",
            hook_executor=allow,
        )
        requested = await request_turn_replacement(
            db,
            authority=authority,
            input_id=input_receipt.input_id,
        )
        await db.commit()  # crash at requested

        expected_states = ["cancel_accepted", "cancel_accepted"]
        for expected_state in expected_states:
            await recover_turn_replacements_once(
                db,
                worker_id=f"replacement-recovery:{expected_state}",
                signal_callback=signal,
                start_replacement_callback=start_replacement,
                stale_after=timedelta(seconds=0),
                tenant_id=tenant_id,
                max_transitions_per_saga=1,
            )
            saga = await db.get(SessionTurnReplacement, requested.saga_id)
            assert saga is not None and saga.state == expected_state

        control = await db.get(
            SessionControlInput, (await db.get(SessionTurnReplacement, requested.saga_id)).cancel_control_id
        )
        assert control is not None and control.status == "applying"
        assert signals == [old_run_id]

        old_run = await db.get(RuntimeTask, old_run_id)
        assert old_run is not None
        await _apply_terminal_task_update_and_settle(
            db,
            old_run,
            status="killed",
            result_summary="cancelled for replacement",
            metadata_json={"cancel_control_id": str(control.id)},
            terminal_source="test_replacement_old_run_terminal",
        )
        await db.commit()  # crash after terminal before saga observes fence

        for expected_state in (
            "old_run_fenced",
            "replacement_queued",
            "replacement_admitted",
            "completed",
        ):
            await recover_turn_replacements_once(
                db,
                worker_id=f"replacement-recovery:{expected_state}",
                signal_callback=signal,
                start_replacement_callback=start_replacement,
                stale_after=timedelta(seconds=0),
                tenant_id=tenant_id,
                max_transitions_per_saga=1,
            )
            saga = await db.get(SessionTurnReplacement, requested.saga_id)
            assert saga is not None and saga.state == expected_state

        saga = await db.get(SessionTurnReplacement, requested.saga_id)
        input_row = await db.get(SessionTurnInput, input_receipt.input_id)
        parent_command = await db.get(SessionCommand, input_receipt.command_id)
        assert saga is not None and saga.state == "completed"
        assert input_row is not None and input_row.status == "queued"
        assert parent_command is not None and parent_command.status == "accepted"
        assert len(starts) == 1
        replacement_run_id = starts[0]
        completed_event_count = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.item_id == saga.id,
                ChatTranscriptEvent.event_type == "turn_replacement.completed",
            )
        )
        completed_event = await db.scalar(
            select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.item_id == saga.id,
                ChatTranscriptEvent.event_type == "turn_replacement.completed",
            )
        )
        assert completed_event_count == 1
        assert completed_event is not None
        assert (completed_event.metadata_json or {})["v2_payload"]["human_input_applied"] is False

        round_messages = await bind_round_inputs(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=replacement_run_id,
            turn_id=saga.replacement_turn_id,
            round_index=1,
        )
        provider_request_id = await prepare_model_request(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=replacement_run_id,
            turn_id=saga.replacement_turn_id,
            round_index=1,
            messages=round_messages,
            tools=[],
            provider="test",
            model="test-model",
        )
        await commit_model_response(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=replacement_run_id,
            turn_id=saga.replacement_turn_id,
            round_index=1,
            provider_request_id=provider_request_id,
            response={"role": "assistant", "content": "replacement accepted"},
        )
        await db.commit()

        saga = await db.get(SessionTurnReplacement, requested.saga_id)
        input_row = await db.get(SessionTurnInput, input_receipt.input_id)
        parent_command = await db.get(SessionCommand, input_receipt.command_id)
        assert saga is not None and saga.state == "completed"
        assert input_row is not None and input_row.status == "applied"
        assert parent_command is not None and parent_command.status == "applied"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RuntimeTask)
                .where(
                    RuntimeTask.parent_session_id == str(session_id),
                    RuntimeTask.metadata_json["turn_id"].astext == saga.replacement_turn_id,
                )
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.item_id == saga.id,
                    ChatTranscriptEvent.event_type == "turn_replacement.completed",
                )
            )
            == 1
        )


async def test_replacement_old_run_completed_race_is_typed_and_does_not_drop_input(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionControlInput, SessionTurnReplacement
    from app.runtime.hooks import HookResult
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_turn_replacement import recover_turn_replacements_once, request_turn_replacement
    from app.services.web_chat_runtime import _apply_terminal_task_update_and_settle

    tenant_id, user_id, agent_id, session_id, old_run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert old_run_id is not None

    async def noop(**_kwargs):
        return None

    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        input_receipt = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=old_run_id,
            kind="interrupt_and_replace",
        )

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=input_receipt.input_id,
            worker_id="replacement-race-admission",
            hook_executor=allow,
        )
        requested = await request_turn_replacement(db, authority=authority, input_id=input_receipt.input_id)
        await db.commit()
        for _ in range(2):
            await recover_turn_replacements_once(
                db,
                worker_id="replacement-race-worker",
                signal_callback=noop,
                start_replacement_callback=noop,
                stale_after=timedelta(seconds=0),
                tenant_id=tenant_id,
                max_transitions_per_saga=1,
            )
        saga = await db.get(SessionTurnReplacement, requested.saga_id)
        assert saga is not None and saga.cancel_control_id is not None
        old_run = await db.get(RuntimeTask, old_run_id)
        assert old_run is not None
        await _apply_terminal_task_update_and_settle(
            db,
            old_run,
            status="completed",
            result_summary="old answer won race",
            metadata_json={},
            terminal_source="test_old_run_completed_race",
        )
        await db.commit()
        control = await db.get(SessionControlInput, saga.cancel_control_id)
        assert control is not None and control.status == "rejected"

        await recover_turn_replacements_once(
            db,
            worker_id="replacement-race-fence",
            signal_callback=noop,
            start_replacement_callback=noop,
            stale_after=timedelta(seconds=0),
            tenant_id=tenant_id,
            max_transitions_per_saga=1,
        )
        saga = await db.get(SessionTurnReplacement, requested.saga_id)
        assert saga is not None and saga.state == "old_run_fenced"
        fenced_event = await db.scalar(
            select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.item_id == saga.id,
                ChatTranscriptEvent.event_type == "turn_replacement.fenced",
            )
        )
        assert fenced_event is not None
        payload = (fenced_event.metadata_json or {})["v2_payload"]
        assert payload["cancel_outcome"] == "old_run_terminal_before_cancel_effect"
        assert payload["old_run_status"] == "completed"


async def test_replacement_pre_fence_cannot_bind_and_broken_authority_is_quarantined(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionCommand, SessionTurnInput, SessionTurnReplacement
    from app.runtime.hooks import HookResult
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_model_round import bind_round_inputs
    from app.services.session_turn_replacement import recover_turn_replacements_once, request_turn_replacement

    tenant_id, user_id, agent_id, session_id, old_run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert old_run_id is not None
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        input_receipt = await _accepted_input(
            db,
            authority=authority,
            session_id=session_id,
            run_id=old_run_id,
            kind="interrupt_and_replace",
        )

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=input_receipt.input_id,
            worker_id="replacement-quarantine-admission",
            hook_executor=allow,
        )
        requested = await request_turn_replacement(db, authority=authority, input_id=input_receipt.input_id)
        await db.commit()

        messages = await bind_round_inputs(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=old_run_id,
            turn_id=f"turn-{old_run_id.hex}",
            round_index=1,
        )
        input_row = await db.get(SessionTurnInput, input_receipt.input_id)
        assert messages == []
        assert input_row is not None and input_row.status == "accepted"
        await db.rollback()

        async def broken_authority(*_args, **_kwargs):
            raise ValueError("replacement_recovery_principal_revoked")

        monkeypatch.setattr(
            "app.services.session_turn_replacement.resolve_session_mutation_authority",
            broken_authority,
        )
        counts = await recover_turn_replacements_once(
            db,
            worker_id="replacement-quarantine-worker",
            tenant_id=tenant_id,
            stale_after=timedelta(seconds=0),
        )
        saga = await db.get(SessionTurnReplacement, requested.saga_id)
        saga_command = await db.get(SessionCommand, requested.saga_command_id)
        event = await db.scalar(
            select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.item_id == requested.saga_id,
                ChatTranscriptEvent.event_type == "turn_replacement.needs_reconciliation",
            )
        )
        assert counts["needs_reconciliation"] == 1
        assert saga is not None and saga.state == "needs_reconciliation"
        assert saga_command is not None and saga_command.status == "needs_reconciliation"
        assert event is not None
        payload = (event.metadata_json or {})["v2_payload"]
        assert payload["resume_state"] == "requested"
        assert payload["recovery_owner"] == "runtime_task_worker:turn_replacement_reconciliation"
        assert payload["recovery_slo_at"]


async def test_answer_request_dispatches_only_to_the_exact_waiting_question(owner_sessionmaker) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionCommand
    from app.runtime.hooks import HookResult
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_v2_persistence import SessionEventDraft, accept_human_input, append_session_events

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    assert run_id is not None
    question_id = uuid.uuid4()
    run_scope = {
        "level": "run",
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "turn_id": f"turn-{run_id.hex}",
        "run_id": str(run_id),
    }
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            drafts=[
                SessionEventDraft(
                    item_id=question_id,
                    item_kind="user_question",
                    lifecycle="waiting",
                    scope=run_scope,
                    actor={"type": "runtime"},
                    payload={"question": "Which target?"},
                )
            ],
        )
        await db.commit()
        input_id = uuid.uuid4()
        accepted = await accept_human_input(
            db,
            authority=authority,
            intent={
                "kind": "answer_request",
                "input_id": str(input_id),
                "idempotency_key": f"answer:{input_id}",
                "session_id": str(session_id),
                "request_item_id": str(question_id),
                "content_parts": [{"type": "text", "text": "Production"}],
            },
        )

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=input_id,
            worker_id="answer-admission",
            hook_executor=allow,
        )
        queued = await queue_admitted_human_input(db, authority=authority, input_id=input_id)
        await db.commit()
        assert queued.status == "queued"
        assert queued.target_run_id == str(run_id)
        assert queued.target_turn_id == f"turn-{run_id.hex}"
        assert queued.reason_code is None

        closed_question_id = uuid.uuid4()
        await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            drafts=[
                SessionEventDraft(
                    item_id=closed_question_id,
                    item_kind="user_question",
                    lifecycle="waiting",
                    scope=run_scope,
                    actor={"type": "runtime"},
                    payload={"question": "Closed?"},
                ),
                SessionEventDraft(
                    item_id=closed_question_id,
                    item_kind="user_question",
                    lifecycle="completed",
                    scope=run_scope,
                    actor={"type": "runtime"},
                    payload={"answer": "already settled"},
                ),
            ],
        )
        await db.commit()
        closed_input_id = uuid.uuid4()
        closed_accepted = await accept_human_input(
            db,
            authority=authority,
            intent={
                "kind": "answer_request",
                "input_id": str(closed_input_id),
                "idempotency_key": f"answer:{closed_input_id}",
                "session_id": str(session_id),
                "request_item_id": str(closed_question_id),
                "content_parts": [{"type": "text", "text": "Too late"}],
            },
        )
        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=closed_input_id,
            worker_id="closed-answer-admission",
            hook_executor=allow,
        )
        rejected = await queue_admitted_human_input(
            db,
            authority=authority,
            input_id=closed_input_id,
        )
        await db.commit()
        command = await db.get(SessionCommand, closed_accepted.command_id)
        assert rejected.status == "rejected"
        assert rejected.reason_code == "user_question_not_waiting"
        assert command is not None and command.status == "rejected"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.input_id == closed_input_id,
                    ChatTranscriptEvent.event_type == "human_input.rejected",
                )
            )
            == 1
        )
        assert accepted.input_id == input_id


@pytest.mark.parametrize(
    ("delivery_state", "expected_status"),
    (("delivered", "applied"), ("unknown", "needs_reconciliation")),
)
async def test_fork_side_thread_dispatches_exact_sequence_to_one_deterministic_branch(
    owner_sessionmaker,
    monkeypatch,
    delivery_state,
    expected_status,
) -> None:
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionTurnInput
    from app.runtime.hooks import HookResult
    from app.services import web_chat_runtime
    from app.services.session_fork_input import (
        dispatch_fork_side_thread,
        settle_fork_input_provider_delivery,
    )
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_model_round import prepare_model_request
    from app.services.session_v2_persistence import SessionEventDraft, accept_human_input, append_session_events

    tenant_id, user_id, agent_id, session_id, _ = await _seed_session(owner_sessionmaker)
    anchor_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        events = await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            drafts=[
                SessionEventDraft(
                    item_id=anchor_id,
                    item_kind="human_input",
                    lifecycle="accepted",
                    scope={"level": "session", "session_id": str(session_id), "thread_id": str(session_id)},
                    actor={"type": "user", "id": str(user_id)},
                    payload={"content": "anchor"},
                )
            ],
        )
        await db.commit()
        input_id = uuid.uuid4()
        accepted = await accept_human_input(
            db,
            authority=authority,
            intent={
                "kind": "fork_side_thread",
                "input_id": str(input_id),
                "idempotency_key": f"fork:{input_id}",
                "session_id": str(session_id),
                "fork_after_sequence": events[0].sequence,
                "content_parts": [{"type": "text", "text": "Explore independently"}],
            },
        )

        async def allow(**_kwargs):
            return HookResult()

        await run_user_prompt_admission(
            db,
            authority=authority,
            input_id=input_id,
            worker_id="fork-admission",
            hook_executor=allow,
        )
        await db.commit()

        async def durable_run_starter(**kwargs):
            branch_session = kwargs["session"]
            run_id = kwargs["run_id"]
            db.add(
                RuntimeTask(
                    id=run_id,
                    task_type="web_chat_turn",
                    status="pending",
                    parent_agent_id=agent_id,
                    child_agent_id=agent_id,
                    tenant_id=tenant_id,
                    parent_session_id=str(branch_session.id),
                    child_session_id=str(branch_session.id),
                    root_user_id=user_id,
                    root_session_id=str(branch_session.root_session_id or branch_session.id),
                    root_runtime_task_id=run_id,
                    prompt=kwargs["content"],
                    metadata_json={
                        "turn_id": f"turn-{run_id.hex}",
                        **dict(kwargs.get("extra_metadata") or {}),
                    },
                )
            )
            await db.commit()
            return {"run_id": str(run_id), "status": "pending"}

        monkeypatch.setattr(web_chat_runtime, "start_web_chat_run", durable_run_starter)
        source_session = await db.get(ChatSession, session_id)
        from app.models.agent import Agent
        from app.models.user import User

        agent = await db.get(Agent, agent_id)
        user = await db.get(User, user_id)
        assert source_session is not None and agent is not None and user is not None
        receipt = await dispatch_fork_side_thread(
            db,
            authority=authority,
            agent=agent,
            user=user,
            source_session=source_session,
            input_id=input_id,
        )
        await db.commit()
        replay = await dispatch_fork_side_thread(
            db,
            authority=authority,
            agent=agent,
            user=user,
            source_session=source_session,
            input_id=input_id,
        )
        assert receipt.status == "queued" and replay.replayed is True
        provider_request_id = await prepare_model_request(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=receipt.branch_session_id,
            run_id=receipt.branch_run_id,
            turn_id=f"turn-{receipt.branch_run_id.hex}",
            round_index=1,
            messages=[{"role": "user", "content": "Explore independently"}],
            tools=[],
            provider="openai",
            model="gpt-4.1",
            wire_request={
                "messages": [{"role": "user", "content": "Explore independently"}],
                "tools": [],
            },
            attempt_owner="fork-side-thread-test",
        )
        await db.commit()
        settled = await settle_fork_input_provider_delivery(
            db,
            branch_run_id=receipt.branch_run_id,
            provider_response_ref=f"provider-response:{receipt.branch_run_id}",
            delivery_state=delivery_state,
        )
        await db.commit()
        settlement_replay = await settle_fork_input_provider_delivery(
            db,
            branch_run_id=receipt.branch_run_id,
            provider_response_ref=f"provider-response:{receipt.branch_run_id}",
            delivery_state=delivery_state,
        )
        with pytest.raises(ValueError, match="fork_input_provider_receipt_conflict"):
            await settle_fork_input_provider_delivery(
                db,
                branch_run_id=receipt.branch_run_id,
                provider_response_ref=f"different-provider-response:{receipt.branch_run_id}",
                delivery_state=delivery_state,
            )
        row = await db.get(SessionTurnInput, input_id)
        command = await db.get(SessionCommand, accepted.command_id)
        branch = await db.get(ChatSession, receipt.branch_session_id)
        run = await db.get(RuntimeTask, receipt.branch_run_id)
        assert provider_request_id == f"hive:{receipt.branch_run_id}:round:1:attempt:1"
        assert row is not None and row.bound_round_id == f"{receipt.branch_run_id}:round:1"
        assert row.model_request_snapshot_ref.startswith("session-model-result:")
        assert settled.status == expected_status
        assert settlement_replay.status == expected_status and settlement_replay.replayed is True
        assert replay.branch_session_id == receipt.branch_session_id
        assert branch is not None and branch.parent_session_id == session_id
        assert run is not None and run.parent_session_id == str(branch.id)
        assert row is not None and row.status == expected_status
        assert row.target_run_id is None
        assert command is not None
        assert command.status == ("applied" if delivery_state == "delivered" else "accepted")
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.input_id == input_id,
                    ChatTranscriptEvent.event_type == f"human_input.{expected_status}",
                )
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count()).select_from(ChatSession).where(ChatSession.parent_session_id == session_id)
            )
            == 1
        )
        copied = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == branch.id,
                        ChatTranscriptEvent.metadata_json["copied_from_sequence"].astext == str(events[0].sequence),
                    )
                )
            ).scalars()
        )
        assert len(copied) == 1

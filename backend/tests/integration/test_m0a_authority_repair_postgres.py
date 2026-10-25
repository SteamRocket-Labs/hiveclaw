"""Real PostgreSQL repairs for trigger authority lock order, recovery, and reclaim."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.trigger import AgentTrigger
from app.models.user import User
from tests.services.test_session_input_control_v2 import _seed_session


def _user(user_id, tenant_id, *, role, name):
    return User(
        id=user_id,
        tenant_id=tenant_id,
        username=f"{name}-{user_id.hex[:8]}",
        email=f"{user_id.hex[:8]}@m0a-repair.test",
        password_hash="test",
        display_name=name,
        role=role,
        is_active=True,
    )


async def test_offboarding_and_trigger_settlement_complete_without_deadlock(owner_sessionmaker, monkeypatch):
    from app.services import runtime_task_service as runtime
    from app.services import user_offboarding_service as offboarding

    tenant_id, departing_id, agent_id, session_id, _run_id = await _seed_session(owner_sessionmaker)
    successor_id, trigger_id, task_id = (uuid.uuid4() for _ in range(3))
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(_user(successor_id, tenant_id, role="org_admin", name="Successor"))
        await db.flush()
        db.add(
            AgentTrigger(
                id=trigger_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                name="Inflight",
                type="interval",
                reason="Repair",
                config={
                    "minutes": 30,
                    "created_by": str(departing_id),
                    "authority_state": "owned",
                    "_fire_inflight": {"runtime_task_id": str(task_id)},
                },
                is_enabled=True,
            )
        )
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                parent_agent_id=agent_id,
                parent_session_id=str(session_id),
                root_user_id=departing_id,
                task_type="trigger",
                status="running",
                metadata_json={"trigger_ids": [str(trigger_id)]},
            )
        )
    writer_locked = asyncio.Event()
    release_writer = asyncio.Event()
    original_settle = runtime._settle_trigger_runtime_task
    original_revoke = offboarding.revoke_user_authority

    async def barrier_settle(db, task, **kwargs):
        writer_locked.set()
        await asyncio.wait_for(release_writer.wait(), 5)
        return await original_settle(db, task, **kwargs)

    async def barrier_revoke(db, **kwargs):
        release_writer.set()
        return await original_revoke(db, **kwargs)

    monkeypatch.setattr(runtime, "async_session", owner_sessionmaker)
    monkeypatch.setattr(runtime, "_settle_trigger_runtime_task", barrier_settle)
    monkeypatch.setattr(offboarding, "revoke_user_authority", barrier_revoke)

    async def do_offboard():
        await asyncio.wait_for(writer_locked.wait(), 5)
        async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
            departing = (await db.execute(select(User).where(User.id == departing_id).with_for_update())).scalar_one()
            successor = (await db.execute(select(User).where(User.id == successor_id).with_for_update())).scalar_one()
            return await offboarding.offboard_loaded_user(
                db,
                target_user=departing,
                successor=successor,
                actor=successor,
                expected_agent_ids=[agent_id],
                reason="Repair lock-order regression",
                request_id=f"repair-{task_id}",
            )

    results = await asyncio.wait_for(
        asyncio.gather(
            runtime.update_runtime_task_record(str(task_id), status="skipped", result_summary="No effect performed"),
            do_offboard(),
            return_exceptions=True,
        ),
        12,
    )
    assert all(not isinstance(value, BaseException) for value in results), repr(results)
    assert results[0] is True
    assert results[1].status == "deactivated"
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        user = await db.get(User, departing_id)
        trigger = await db.get(AgentTrigger, trigger_id)
        assert user.is_active is False
        assert trigger.is_enabled is False
        assert trigger.config["created_by"] == str(departing_id)
        assert trigger.config["authority_state"] == "quarantined"
        assert trigger.config["authority_revoked_reason"] == "agent_owner_transferred"


async def test_successor_can_discover_and_reclaim_without_enabling(owner_sessionmaker):
    from app.api.triggers import list_agent_triggers, reclaim_trigger_authority
    from app.core.permissions import check_agent_access
    from app.services.autonomy_overview import build_agent_autonomy_overview

    tenant_id, old_owner_id, agent_id, _session_id, _run_id = await _seed_session(owner_sessionmaker)
    successor_id, admin_id, stranger_id = (uuid.uuid4() for _ in range(3))
    trigger_id, paused_id = uuid.uuid4(), uuid.uuid4()
    plan_id = str(uuid.uuid4())
    private_ref = f"session:{uuid.uuid4()}"
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(_user(successor_id, tenant_id, role="member", name="Successor"))
        db.add(_user(admin_id, tenant_id, role="org_admin", name="Admin"))
        db.add(_user(stranger_id, tenant_id, role="member", name="Stranger"))
        await db.flush()
        agent = await db.get(Agent, agent_id)
        agent.owner_user_id = successor_id
        db.add(
            AgentTrigger(
                id=trigger_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                name="Transferred",
                type="interval",
                reason="Repair",
                config={
                    "minutes": 30,
                    "created_by": str(old_owner_id),
                    "authority_state": "quarantined",
                    "authority_revoked_reason": "agent_owner_transferred",
                    "plan_id": plan_id,
                    "root_session_id": str(uuid.uuid4()),
                    "context_from": [private_ref],
                    "delivery_target_json": {"open_id": "ou_old"},
                    "metadata": {"plan_exempt_reason": "user_declined"},
                },
                reply_context={"open_id": "ou_old"},
                is_enabled=False,
            )
        )
        db.add(
            AgentTrigger(
                id=paused_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                name="Paused",
                type="interval",
                reason="Repair",
                config={"minutes": 30, "created_by": str(successor_id), "authority_state": "owned"},
                is_enabled=False,
            )
        )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        successor = await db.get(User, successor_id)
        admin = await db.get(User, admin_id)
        stranger = await db.get(User, stranger_id)
        owner_rows = await list_agent_triggers(
            agent_id, diagnostics=False, operator_view=False, operator_reason=None, current_user=successor, db=db
        )
        owner_row = next(row for row in owner_rows if row.id == str(trigger_id))
        assert owner_row.next_action == "reclaim_trigger_authority"
        assert "plan_id" not in owner_row.config
        assert "root_session_id" not in owner_row.config
        assert "context_from" not in owner_row.config
        assert "delivery_target_json" not in owner_row.config
        access = await check_agent_access(db, successor, agent_id)
        overview = await build_agent_autonomy_overview(
            db=db, agent=access[0], resource_user=successor, agent_access=access
        )
        assert any(
            item["id"] == str(trigger_id) and item["next_action"] == "reclaim_trigger_authority"
            for item in overview["triggers"]
        )
        admin_rows = await list_agent_triggers(
            agent_id, diagnostics=False, operator_view=False, operator_reason=None, current_user=admin, db=db
        )
        admin_matches = [row for row in admin_rows if row.id == str(trigger_id)]
        assert len(admin_matches) == 1
        assert admin_matches[0].config["plan_id"] == plan_id
        assert admin_matches[0].config["context_from"] == [private_ref]
        with pytest.raises(HTTPException) as denied:
            await list_agent_triggers(
                agent_id, diagnostics=False, operator_view=False, operator_reason=None, current_user=stranger, db=db
            )
        assert denied.value.status_code == 403
        stored = await db.get(AgentTrigger, trigger_id)
        assert stored.config["plan_id"] == plan_id
        assert stored.config["context_from"] == [private_ref]
        assert stored.reply_context == {"open_id": "ou_old"}

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        successor = await db.get(User, successor_id)
        with pytest.raises(HTTPException) as paused:
            await reclaim_trigger_authority(agent_id=agent_id, trigger_id=paused_id, current_user=successor, db=db)
        assert paused.value.status_code == 409
        assert paused.value.detail["code"] == "trigger_authority_not_revoked"
        paused_row = await db.get(AgentTrigger, paused_id)
        assert paused_row.is_enabled is False
        assert paused_row.config["authority_state"] == "owned"

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        successor = await db.get(User, successor_id)
        reclaimed = await reclaim_trigger_authority(
            agent_id=agent_id, trigger_id=trigger_id, current_user=successor, db=db
        )
        assert reclaimed.is_enabled is False
        assert reclaimed.next_action == "resume_wake_policy"
        assert "plan_id" not in reclaimed.config
        assert "context_from" not in reclaimed.config

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        stored = await db.get(AgentTrigger, trigger_id)
        assert stored.is_enabled is False
        assert stored.config["created_by"] == str(successor_id)
        assert stored.config["authority_state"] == "owned"
        assert stored.config["previous_authority_owner_id"] == str(old_owner_id)
        assert "plan_id" not in stored.config
        assert "root_session_id" not in stored.config
        assert "context_from" not in stored.config
        assert "delivery_target_json" not in stored.config
        assert "metadata" not in stored.config
        assert stored.reply_context is None


def _json_request(payload: dict):
    import json

    from starlette.requests import Request

    body = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": "/channel/feishu/card-callback",
            "raw_path": b"/channel/feishu/card-callback",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 9),
            "server": ("testserver", 80),
            "scheme": "http",
        },
        receive,
    )


def _card_body(open_id: str, approval_id) -> dict:
    import json

    return {
        "token": "synthetic-m0a-card-token",
        "open_id": open_id,
        "operator": {"open_id": open_id},
        "action": {"value": json.dumps({"approval_id": str(approval_id), "action": "approve"})},
    }


async def test_transferred_owner_card_and_web_resolve_follow_current_authority(owner_sessionmaker, monkeypatch):
    """Old Feishu cards and Web resolves die after handover; live owner and scoped admin still can."""
    from app.api.agents import resolve_agent_approval
    from app.api.feishu import feishu_card_callback
    from app.models.audit import ApprovalRequest
    from app.models.channel_config import ChannelConfig
    from app.services.agent_ownership_service import transfer_loaded_agent_owner
    from app.services.approval_service import approval_service
    from app.services.external_principal_service import bind_authenticated_self_channel_principal

    tenant_id, old_owner_id, agent_id, _session_id, _run_id = await _seed_session(owner_sessionmaker)
    successor_id, admin_id, requester_id = (uuid.uuid4() for _ in range(3))
    approval_ids = [uuid.uuid4() for _ in range(3)]
    notified: list[tuple] = []
    cards: list[tuple] = []
    worker_calls: list[dict] = []

    async def fake_send(_db, *, user_id, **kwargs):
        notified.append((user_id, kwargs.get("type")))

    async def fake_card(_app_id, _secret, receive_id, id_type, *_args, **_kwargs):
        cards.append((receive_id, id_type))

    async def fake_worker(**kwargs):
        worker_calls.append(kwargs)

    monkeypatch.setattr("app.services.notification_service.send_notification", fake_send)
    monkeypatch.setattr("app.services.approval_service.feishu_service.send_approval_card", fake_card)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", fake_worker)

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(_user(successor_id, tenant_id, role="member", name="Successor"))
        db.add(_user(admin_id, tenant_id, role="org_admin", name="Admin"))
        db.add(_user(requester_id, tenant_id, role="member", name="Requester"))
        await db.flush()
        for user_id, open_id in (
            (old_owner_id, "ou_old_owner"),
            (successor_id, "ou_successor"),
            (admin_id, "ou_admin"),
            (requester_id, "ou_requester"),
        ):
            (await db.get(User, user_id)).feishu_open_id = open_id
        config = ChannelConfig(
            tenant_id=tenant_id,
            agent_id=agent_id,
            channel_type="feishu",
            app_id="cli_m0a",
            app_secret="test-secret",
            verification_token="synthetic-m0a-card-token",
            is_configured=True,
            is_connected=True,
            extra_config={},
        )
        db.add(config)
        await db.flush()
        await bind_authenticated_self_channel_principal(
            db,
            tenant_id=tenant_id,
            config=config,
            provider_subject_id="ou_old_owner",
            user_id=old_owner_id,
            actor_user_id=old_owner_id,
        )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await transfer_loaded_agent_owner(
            db,
            agent=await db.get(Agent, agent_id),
            new_owner=await db.get(User, successor_id),
            actor=await db.get(User, old_owner_id),
            reason="Review handover",
            expected_owner_id=old_owner_id,
            mode="manual_admin",
        )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        config = (await db.execute(select(ChannelConfig).where(ChannelConfig.agent_id == agent_id))).scalar_one()
        await bind_authenticated_self_channel_principal(
            db,
            tenant_id=tenant_id,
            config=config,
            provider_subject_id="ou_successor",
            user_id=successor_id,
            actor_user_id=successor_id,
        )
        await bind_authenticated_self_channel_principal(
            db,
            tenant_id=tenant_id,
            config=config,
            provider_subject_id="ou_admin",
            user_id=admin_id,
            actor_user_id=admin_id,
        )
        for approval_id in approval_ids:
            db.add(
                ApprovalRequest(
                    id=approval_id,
                    agent_id=agent_id,
                    tenant_id=tenant_id,
                    action_type="send_file",
                    details={"note": "digest", "requested_by": str(requester_id)},
                    status="pending",
                    execution_status="pending",
                )
            )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        agent = await db.get(Agent, agent_id)
        approval = await db.get(ApprovalRequest, approval_ids[0])
        await approval_service._notify_pending_approval(db, agent, approval)

    assert {user_id for user_id, kind in notified if kind == "approval_pending"} == {successor_id}
    assert cards == [("ou_successor", "open_id")]
    notified.clear()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        with pytest.raises(HTTPException) as denied:
            await resolve_agent_approval(
                agent_id,
                approval_ids[0],
                {"action": "approve"},
                await db.get(User, old_owner_id),
                db,
            )
        assert denied.value.status_code == 403

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        old_card = await feishu_card_callback(_json_request(_card_body("ou_old_owner", approval_ids[0])), db)
        assert old_card["toast"]["type"] == "error"
        assert "current Agent owner" in old_card["toast"]["content"]
        assert (await db.get(ApprovalRequest, approval_ids[0])).status == "pending"

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        owner_card = await feishu_card_callback(_json_request(_card_body("ou_successor", approval_ids[0])), db)
        assert owner_card["toast"]["type"] == "success"
        assert (await db.get(ApprovalRequest, approval_ids[0])).resolved_by == successor_id

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        admin_card = await feishu_card_callback(_json_request(_card_body("ou_admin", approval_ids[1])), db)
        assert admin_card["toast"]["type"] == "success"
        assert (await db.get(ApprovalRequest, approval_ids[1])).resolved_by == admin_id

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        web = await resolve_agent_approval(
            agent_id,
            approval_ids[2],
            {"action": "approve"},
            await db.get(User, successor_id),
            db,
        )
        assert web["status"] == "approved"

    resolved_ids = {user_id for user_id, kind in notified if kind == "approval_resolved"}
    assert successor_id in resolved_ids
    assert requester_id in resolved_ids
    assert old_owner_id not in resolved_ids
    assert admin_id not in resolved_ids
    assert worker_calls == []


async def test_legacy_reply_recovery_follows_the_requesting_principal(owner_sessionmaker, monkeypatch):
    """A newer foreign channel is not a destination. The same principal's channel is."""

    from app.services import trigger_daemon as daemon

    tenant_id, previous_id, agent_id, foreign_session_id, _run_id = await _seed_session(owner_sessionmaker)
    successor_id, own_session_id = uuid.uuid4(), uuid.uuid4()
    same_id, previous_trigger_id, missing_id = (uuid.uuid4() for _ in range(3))
    stranger_id = uuid.uuid4()
    foreign_target = {"channel": "feishu", "chat_id": "oc_previous_private"}
    own_target = {"channel": "feishu", "chat_id": "oc_same_principal"}
    now = datetime.now(timezone.utc)

    def _trigger(trigger_id, name, owner_id):
        return AgentTrigger(
            id=trigger_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            name=name,
            type="interval",
            reason="Legacy delivery",
            is_enabled=True,
            reply_context=None,
            config={"minutes": 30, "created_by": str(owner_id), "authority_state": "owned"},
        )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(_user(successor_id, tenant_id, role="member", name="Successor"))
        await db.flush()
        foreign = await db.get(ChatSession, foreign_session_id)
        foreign.source_channel = "feishu"
        foreign.delivery_target_json = foreign_target
        foreign.last_message_at = now
        db.add(
            ChatSession(
                id=own_session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=successor_id,
                source_channel="feishu",
                title="Same principal",
                delivery_target_json=own_target,
                last_message_at=now - timedelta(days=1),
            )
        )
        db.add(_trigger(same_id, "Same principal wake", successor_id))
        db.add(_trigger(previous_trigger_id, "Previous principal wake", previous_id))
        db.add(_trigger(missing_id, "No origin wake", stranger_id))

    def scoped(*_args, **_kwargs):
        return tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker)

    async def tenant(*_args, **_kwargs):
        return tenant_id

    monkeypatch.setattr(daemon, "tenant_scoped_session", scoped)
    monkeypatch.setattr(daemon, "resolve_tenant_for_agent", tenant)
    monkeypatch.setattr(daemon, "async_session", owner_sessionmaker)

    async def _load():
        async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
            return [await db.get(AgentTrigger, trigger_id) for trigger_id in (same_id, previous_trigger_id, missing_id)]

    same, previous, missing = await _load()
    assert await daemon._recover_reply_target_from_session(agent_id, [same]) == own_target
    assert await daemon._recover_reply_target_from_session(agent_id, [previous]) == foreign_target
    missing_recovery = await daemon._recover_reply_target_from_session(agent_id, [missing])
    assert missing_recovery["status"] == "unavailable"
    assert missing_recovery["recoverable"] is True
    assert "channel" not in missing_recovery

    async def _clear():
        async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
            for trigger_id in (same_id, previous_trigger_id, missing_id):
                row = await db.get(AgentTrigger, trigger_id)
                row.reply_context = None
                row.is_enabled = True

    await _clear()
    same, previous, missing = await _load()
    mixed = await daemon._recover_reply_target_from_session(agent_id, [same, previous, missing])
    assert mixed["status"] == "unavailable"
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        assert (await db.get(AgentTrigger, same_id)).reply_context is None
        assert (await db.get(AgentTrigger, previous_trigger_id)).reply_context is None
        assert (await db.get(AgentTrigger, missing_id)).reply_context is None

    result = await daemon.backfill_null_reply_contexts()
    assert result == {
        "patched": 2,
        "skipped": 1,
        "unavailable_code": daemon.AUTHORIZED_DELIVERY_ORIGIN_UNAVAILABLE,
    }
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        same_row = await db.get(AgentTrigger, same_id)
        previous_row = await db.get(AgentTrigger, previous_trigger_id)
        missing_row = await db.get(AgentTrigger, missing_id)
        assert same_row.reply_context == own_target
        assert previous_row.reply_context == foreign_target
        assert missing_row.reply_context is None
        assert same_row.is_enabled and previous_row.is_enabled and missing_row.is_enabled

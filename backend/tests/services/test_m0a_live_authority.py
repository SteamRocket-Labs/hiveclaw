"""M0-A counterexamples: live authority, approval routing, audit scope, knowledge identity."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _TriggerDB:
    def __init__(self, triggers):
        self.triggers = list(triggers)
        self.added = []
        self.flushes = 0

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushes += 1

    async def execute(self, _statement):
        return _Rows(self.triggers)


def _user(**kwargs):
    payload = {"is_active": True, "role": "member", "display_name": "Person"}
    payload.update(kwargs)
    return SimpleNamespace(**payload)


def _member(**kwargs):
    payload = {
        "feishu_open_id": None,
        "open_id": None,
        "unionid": None,
        "feishu_user_id": None,
        "external_id": None,
        "email": None,
        "name": "Directory person",
        "status": "active",
    }
    payload.update(kwargs)
    return SimpleNamespace(**payload)


@pytest.mark.asyncio
async def test_transfer_quarantines_previous_owner_triggers_without_rewriting_creator(monkeypatch):
    from app.models.audit import AuditLog
    import app.services.agent_ownership_service as ownership_service
    from app.services.agent_ownership_service import transfer_loaded_agent_owner

    tenant_id = uuid4()
    creator_id = uuid4()
    old_owner_id = uuid4()
    other_owner_id = uuid4()
    new_owner_id = uuid4()
    owned = SimpleNamespace(
        id=uuid4(),
        config={"created_by": str(old_owner_id), "authority_state": "owned", "expr": "0 9 * * *"},
        is_enabled=True,
    )
    legacy = SimpleNamespace(id=uuid4(), config={"expr": "0 10 * * *"}, is_enabled=True)
    foreign = SimpleNamespace(
        id=uuid4(),
        config={"created_by": str(other_owner_id), "authority_state": "owned"},
        is_enabled=True,
    )
    agent = SimpleNamespace(
        id=uuid4(),
        name="Finance Analyst",
        tenant_id=tenant_id,
        creator_id=creator_id,
        sponsor_user_id=uuid4(),
        owner_user_id=old_owner_id,
    )
    db = _TriggerDB([owned, legacy, foreign])

    async def fake_register(*_args, **_kwargs):
        return None

    async def fake_rebind(*_args, **_kwargs):
        return []

    monkeypatch.setattr(ownership_service, "register_agent_asset", fake_register)
    monkeypatch.setattr(ownership_service, "_rebind_active_collaboration_memberships", fake_rebind)

    await transfer_loaded_agent_owner(
        db,
        agent=agent,
        new_owner=_user(id=new_owner_id, tenant_id=tenant_id, role="org_admin", display_name="Successor"),
        actor=_user(id=uuid4(), tenant_id=tenant_id),
        reason="Member offboarding",
        expected_owner_id=old_owner_id,
        mode="user_offboarding",
    )

    assert agent.creator_id == creator_id
    assert agent.owner_user_id == new_owner_id
    assert owned.is_enabled is False
    assert owned.config["authority_state"] == "quarantined"
    assert owned.config["created_by"] == str(old_owner_id)
    assert owned.config["authority_revoked_reason"] == "agent_owner_transferred"
    assert legacy.is_enabled is True
    assert "authority_state" not in legacy.config
    assert foreign.is_enabled is True
    assert foreign.config["created_by"] == str(other_owner_id)
    audits = [item for item in db.added if isinstance(item, AuditLog)]
    assert audits[0].details["quarantined_trigger_ids"] == [str(owned.id)]


@pytest.mark.asyncio
async def test_explicit_quarantine_does_not_evaluate_as_due():
    from datetime import datetime, timezone

    from app.services.trigger_daemon import _evaluate_trigger

    trigger = SimpleNamespace(
        is_enabled=True,
        type="webhook",
        name="wake",
        config={"authority_state": "quarantined", "_webhook_pending": True, "created_by": str(uuid4())},
        expires_at=None,
        max_fires=None,
        fire_count=0,
        last_fired_at=None,
        cooldown_seconds=0,
        created_at=datetime.now(timezone.utc),
        agent_id=uuid4(),
    )
    assert await _evaluate_trigger(trigger, datetime.now(timezone.utc)) is False


def test_quarantined_trigger_view_asks_for_reclaim_not_ordinary_resume():
    from app.services.autonomy_overview import build_trigger_view

    quarantined = SimpleNamespace(
        id=uuid4(),
        name="daily",
        reason="Daily",
        type="interval",
        is_enabled=False,
        config={"authority_state": "quarantined", "minutes": 30, "created_by": str(uuid4())},
        expires_at=None,
        max_fires=None,
        fire_count=0,
    )
    view = build_trigger_view(quarantined, attempts=[])
    assert view["attention_state"] == "authority_revoked"
    assert view["next_action"] == "reclaim_trigger_authority"

    paused = SimpleNamespace(
        id=uuid4(),
        name="daily",
        reason="Daily",
        type="interval",
        is_enabled=False,
        config={"minutes": 30},
        expires_at=None,
        max_fires=None,
        fire_count=0,
    )
    paused_view = build_trigger_view(paused, attempts=[])
    assert paused_view["attention_state"] == "paused"
    assert paused_view["next_action"] == "resume_wake_policy"


def test_inactive_owner_cannot_resolve_and_scoped_admin_still_can():
    from app.services.approval_service import _can_resolve_agent_approval

    tenant_id = uuid4()
    owner_id = uuid4()
    agent = SimpleNamespace(owner_user_id=owner_id, creator_id=uuid4(), tenant_id=tenant_id)
    inactive_owner = _user(id=owner_id, tenant_id=tenant_id, is_active=False)
    admin = _user(id=uuid4(), tenant_id=tenant_id, role="org_admin", is_active=True)
    assert _can_resolve_agent_approval(agent, inactive_owner) is False
    assert _can_resolve_agent_approval(agent, admin) is True


def test_live_effect_and_trigger_authority_rules():
    from app.services.agent_identity_lifecycle import LiveEffectPrincipal
    from app.services.trigger_resource_authority import trigger_execution_block_reason

    owner_id = uuid4()
    assert trigger_execution_block_reason(
        SimpleNamespace(config={"authority_state": "quarantined", "created_by": str(owner_id)}),
        owner_id,
    ) == "trigger_authority_quarantined"
    assert (
        trigger_execution_block_reason(SimpleNamespace(config={"expr": "* * * * *"}), owner_id)
        == "trigger_authority_quarantined"
    )
    assert (
        trigger_execution_block_reason(
            SimpleNamespace(config={"created_by": str(uuid4()), "authority_state": "owned"}),
            owner_id,
        )
        is None
    )
    assert LiveEffectPrincipal(user_id=owner_id, block_reason=None).user_id == owner_id


@pytest.mark.asyncio
async def test_task_effect_user_keeps_inactive_requester_and_uses_live_owner():
    from app.services.task_executor import resolve_task_effect_user

    tenant_id = uuid4()
    owner_id = uuid4()
    requester_id = uuid4()
    agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, owner_user_id=owner_id, creator_id=uuid4())
    owner = _user(id=owner_id, tenant_id=tenant_id, is_active=True)
    inactive = _user(id=requester_id, tenant_id=tenant_id, is_active=False)

    class _DB:
        def __init__(self, user):
            self.user = user

        async def execute(self, _statement):
            return _Rows([self.user] if self.user is not None else [])

    missing_user, missing_reason = await resolve_task_effect_user(_DB(owner), agent, None)
    assert missing_user == owner_id
    assert missing_reason is None
    kept_user, kept_reason = await resolve_task_effect_user(_DB(inactive), agent, requester_id)
    assert kept_user is None
    assert kept_reason == "requester_inactive"


def test_company_knowledge_audiences_use_explicit_subjects_only():
    from app.services.company_knowledge_identity import company_knowledge_grant_audiences

    tenant_id = uuid4()
    linked_id = uuid4()
    lone_id = uuid4()
    inactive_id = uuid4()
    users = [
        _user(
            id=linked_id,
            tenant_id=tenant_id,
            display_name="Linked",
            feishu_open_id="ou_linked",
            feishu_union_id=None,
            feishu_user_id="fu_linked",
            email="linked@example.test",
            oidc_sub=None,
        ),
        _user(
            id=lone_id,
            tenant_id=tenant_id,
            display_name="Lone",
            feishu_open_id=None,
            feishu_union_id=None,
            feishu_user_id=None,
            email="lone@example.test",
            oidc_sub="oidc-lone",
        ),
        _user(
            id=inactive_id,
            tenant_id=tenant_id,
            display_name="Inactive",
            is_active=False,
            feishu_open_id="ou_inactive",
            feishu_union_id=None,
            feishu_user_id=None,
            email="inactive@example.test",
            oidc_sub=None,
        ),
        _user(
            id=uuid4(),
            tenant_id=tenant_id,
            display_name="Open twin",
            feishu_open_id="ou_ambiguous",
            feishu_union_id=None,
            feishu_user_id=None,
            email="a@example.test",
            oidc_sub=None,
        ),
        _user(
            id=uuid4(),
            tenant_id=tenant_id,
            display_name="Union twin",
            feishu_open_id=None,
            feishu_union_id="on_ambiguous",
            feishu_user_id=None,
            email="b@example.test",
            oidc_sub=None,
        ),
    ]
    members = [
        _member(id=uuid4(), name="Linked person", feishu_open_id="ou_linked", email="different@example.test"),
        _member(id=uuid4(), name="Same email", email="lone@example.test"),
        _member(id=uuid4(), name="Inactive person", open_id="ou_inactive"),
        _member(id=uuid4(), name="Ambiguous person", feishu_open_id="ou_ambiguous", unionid="on_ambiguous"),
        _member(id=uuid4(), name="Directory only", external_id="fu_missing"),
    ]
    audiences = company_knowledge_grant_audiences(users, members)
    grantable = {item["user_id"] for item in audiences["grantable_users"]}
    assert grantable == {str(linked_id), str(lone_id)}
    reasons = {item["label"]: item["reason"] for item in audiences["visible_unlinked"]}
    assert reasons["Same email"] == "unregistered"
    assert reasons["Inactive person"] == "authenticated_user_inactive"
    assert reasons["Ambiguous person"] == "ambiguous_identity"
    assert reasons["Directory only"] == "unregistered"
    assert "Linked person" not in reasons
    assert all(item["org_member_id"] not in grantable for item in audiences["visible_unlinked"])


def test_enterprise_audit_scope_includes_personnel_rows_and_excludes_other_tenants():
    from sqlalchemy.dialects import postgresql

    from app.api.enterprise import enterprise_audit_log_scope

    tenant_id = uuid4()
    other_id = uuid4()
    agent_id = uuid4()
    compiled = str(
        enterprise_audit_log_scope(tenant_id).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "audit_logs.tenant_id" in compiled
    assert str(tenant_id) in compiled
    assert "IS NULL" in compiled.upper() or "is null" in compiled.lower()
    agent_compiled = str(
        enterprise_audit_log_scope(tenant_id, agent_id=agent_id).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert str(agent_id) in agent_compiled
    assert str(other_id) not in compiled


def test_reclaim_stamps_live_owner_and_refuses_previous_owner():
    from app.services.agent_identity_lifecycle import LiveEffectPrincipal
    from app.services.trigger_resource_authority import (
        apply_trigger_authority_reclaim,
        caller_may_reclaim_trigger_authority,
    )

    tenant_id = uuid4()
    old_owner_id = uuid4()
    new_owner_id = uuid4()
    creator_id = uuid4()
    agent = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        creator_id=creator_id,
        owner_user_id=new_owner_id,
    )
    trigger = SimpleNamespace(
        id=uuid4(),
        config={
            "created_by": str(old_owner_id),
            "authority_state": "quarantined",
            "authority_revoked_reason": "agent_owner_transferred",
            "root_session_id": str(uuid4()),
            "plan_id": str(uuid4()),
            "plan_authorization": {"approved_by": str(old_owner_id)},
            "metadata": {"plan_exempt_reason": "user_declined", "plan_recommendation_id": str(uuid4())},
        },
        is_enabled=False,
    )
    principal = LiveEffectPrincipal(user_id=new_owner_id, block_reason=None)
    assert caller_may_reclaim_trigger_authority(
        caller=_user(id=old_owner_id, tenant_id=tenant_id),
        agent=agent,
        principal=principal,
    ) is False
    assert caller_may_reclaim_trigger_authority(
        caller=_user(id=new_owner_id, tenant_id=tenant_id),
        agent=agent,
        principal=principal,
    ) is True
    assert caller_may_reclaim_trigger_authority(
        caller=_user(id=uuid4(), tenant_id=tenant_id, role="org_admin"),
        agent=agent,
        principal=principal,
    ) is True
    blocked = LiveEffectPrincipal(user_id=new_owner_id, block_reason="responsible_principal_inactive")
    assert caller_may_reclaim_trigger_authority(
        caller=_user(id=uuid4(), tenant_id=tenant_id, role="org_admin"),
        agent=agent,
        principal=blocked,
    ) is False
    previous = apply_trigger_authority_reclaim(trigger, live_owner_id=new_owner_id)
    assert previous == old_owner_id
    assert trigger.is_enabled is False
    assert trigger.config["created_by"] == str(new_owner_id)
    assert trigger.config["authority_state"] == "owned"
    assert trigger.config["previous_authority_owner_id"] == str(old_owner_id)
    assert "authority_revoked_reason" not in trigger.config
    assert "root_session_id" not in trigger.config
    assert "plan_id" not in trigger.config
    assert "plan_authorization" not in trigger.config
    assert "metadata" not in trigger.config
    assert agent.creator_id == creator_id
    paused = SimpleNamespace(
        id=uuid4(),
        config={"created_by": str(new_owner_id), "authority_state": "owned"},
        is_enabled=False,
    )
    with pytest.raises(ValueError, match="trigger_authority_not_revoked"):
        apply_trigger_authority_reclaim(paused, live_owner_id=new_owner_id)
    assert paused.is_enabled is False


@pytest.mark.asyncio
async def test_workflow_fire_blocks_inactive_responsible_principal_without_a_session(monkeypatch):
    from app.services import workflow_trigger
    from app.services.workflow_trigger import fire_workflow_for_trigger

    expected_agent_id = uuid4()
    expected_tenant_id = uuid4()
    created_sessions = []

    async def resolve_tenant(requested_agent_id, session_factory=None):
        assert requested_agent_id == expected_agent_id
        assert session_factory is None
        return expected_tenant_id

    async def blocked_effect_user(*, tenant_id, agent_id, session_factory=None, **_kwargs):
        assert tenant_id == expected_tenant_id
        assert agent_id == expected_agent_id
        assert session_factory is None
        return None, "responsible_principal_inactive"

    async def create_session(**kwargs):
        created_sessions.append(kwargs)
        return uuid4(), kwargs["effect_user_id"]

    monkeypatch.setattr("app.services.tenant_resolver.resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(workflow_trigger, "_workflow_effect_user_id", blocked_effect_user)
    monkeypatch.setattr(workflow_trigger, "_create_workflow_trigger_session", create_session)
    monkeypatch.setattr(workflow_trigger, "get_settings", lambda: SimpleNamespace(WORKFLOW_TRIGGER_ENABLED=True))

    result = await fire_workflow_for_trigger(
        agent_id=expected_agent_id,
        trigger_name="weekly",
        trigger_config={
            "workflow_ref": {
                "definition_name": "weekly",
                "definition_version": 1,
                "definition_hash": "abc",
            }
        },
    )

    assert result is not None
    assert result.status == "authority_unavailable"
    assert result.reason == "responsible_principal_inactive"
    assert created_sessions == []


class _Scalar:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Many:
    def __init__(self, rows):
        self.rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class _ScriptSession:
    def __init__(self, results):
        self.results = list(results)
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, statement):
        if "app.current_tenant_id" in str(statement):
            return _Scalar(None)
        if not self.results:
            raise AssertionError(f"unexpected execute: {statement}")
        return self.results.pop(0)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_recovery_rows_stay_with_the_live_owner_and_hide_private_source():
    from app.services.trigger_resource_authority import append_trigger_authority_recovery

    tenant_id = uuid4()
    owner_id = uuid4()
    agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, owner_user_id=owner_id, creator_id=uuid4())
    plan_id = str(uuid4())
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent.id,
        name="moved",
        type="interval",
        reason="Probe",
        is_enabled=False,
        reply_context={"channel": "feishu", "open_id": "ou_old"},
        config={
            "created_by": str(uuid4()),
            "authority_state": "quarantined",
            "plan_id": plan_id,
            "root_session_id": str(uuid4()),
            "context_from": [f"session:{uuid4()}"],
            "delivery_target_json": {"channel": "feishu", "open_id": "ou_old"},
            "metadata": {"plan_exempt_reason": "user_declined"},
        },
    )
    owner = _user(id=owner_id, tenant_id=tenant_id)

    class _DB:
        async def execute(self, _statement):
            return _Rows([owner])

    hidden = await append_trigger_authority_recovery(
        _DB(),
        _user(id=uuid4(), tenant_id=tenant_id),
        agent_id=agent.id,
        triggers=[trigger],
        visible=[],
        agent_access=(agent, "use"),
    )
    assert hidden == []
    shown = await append_trigger_authority_recovery(
        _DB(),
        owner,
        agent_id=agent.id,
        triggers=[trigger],
        visible=[],
        agent_access=(agent, "manage"),
    )
    view, decision = shown[0]
    assert decision.authority_source == "trigger_authority_recovery"
    assert view.name == "moved"
    assert "plan_id" not in view.config
    assert "root_session_id" not in view.config
    assert "context_from" not in view.config
    assert "delivery_target_json" not in view.config
    assert "metadata" not in view.config
    assert view.reply_context is None
    assert view.config["authority_state"] == "quarantined"
    assert trigger.config["plan_id"] == plan_id
    assert "context_from" in trigger.config
    assert trigger.reply_context == {"channel": "feishu", "open_id": "ou_old"}
    assert len(
        await append_trigger_authority_recovery(
            _DB(),
            owner,
            agent_id=agent.id,
            triggers=[trigger],
            visible=[(trigger, decision)],
            agent_access=(agent, "manage"),
        )
    ) == 1


@pytest.mark.asyncio
async def test_invoke_keeps_valid_requesters_when_one_trigger_is_refused(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon
    import app.services.workflow_trigger as workflow_trigger

    tenant_id = uuid4()
    agent_id = uuid4()
    owner_id = uuid4()
    collaborator_id = uuid4()
    retired_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id, tenant_id=tenant_id, owner_user_id=owner_id, creator_id=owner_id, status="running", name="Analyst"
    )
    owner = _user(id=owner_id, tenant_id=tenant_id)
    collaborator = _user(id=collaborator_id, tenant_id=tenant_id)
    retired = _user(id=retired_id, tenant_id=tenant_id, is_active=False)
    ref = {"definition_name": "weekly", "definition_version": 1, "definition_hash": "abc"}

    def trigger(created_by):
        config = {"workflow_ref": ref}
        if created_by is not None:
            config["created_by"] = str(created_by)
            config["authority_state"] = "owned"
        return SimpleNamespace(id=uuid4(), name=f"wake-{created_by or 'legacy'}", type="cron", config=config, is_enabled=True)

    owner_trigger = trigger(owner_id)
    collaborator_trigger = trigger(collaborator_id)
    retired_trigger = trigger(retired_id)
    legacy_trigger = trigger(None)
    use_grant = SimpleNamespace(scope_type="user", scope_id=collaborator_id, access_level="use")
    session = _ScriptSession(
        [
            _Scalar(agent),
            _Scalar(owner),
            _Many([owner_trigger, collaborator_trigger, retired_trigger, legacy_trigger]),
            _Many([owner, collaborator, retired]),
            _Scalar(agent),
            _Scalar(agent),
            _Many([use_grant]),
            _Many([retired_trigger, legacy_trigger]),
        ]
    )
    fired = []
    updates = []

    async def fake_fire(**kwargs):
        fired.append(kwargs["requester_user_id"])
        return SimpleNamespace(status="launched", run_id=uuid4(), run_status="running", session_id=uuid4(), reason=None)

    async def fake_update(_task_id, **fields):
        updates.append(fields)
        return True

    async def resolve_tenant(*_args, **_kwargs):
        return tenant_id

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(workflow_trigger, "fire_workflow_for_trigger", fake_fire)
    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update)

    await trigger_daemon._invoke_agent_for_triggers(
        agent_id,
        [owner_trigger, collaborator_trigger, retired_trigger, legacy_trigger],
        runtime_task_id="rt-mixed",
    )

    assert fired == [owner_id, collaborator_id]
    assert updates[0]["status"] == "completed"
    assert updates[0]["metadata_json"]["trigger_settlement_overrides"] == {
        str(owner_trigger.id): "success",
        str(collaborator_trigger.id): "success",
        str(retired_trigger.id): "release",
        str(legacy_trigger.id): "release",
    }
    assert retired_trigger.is_enabled is False
    assert retired_trigger.config["authority_state"] == "quarantined"
    assert retired_trigger.config["authority_revoked_reason"] == "trigger_requester_inactive"
    assert retired_trigger.config["created_by"] == str(retired_id)
    assert legacy_trigger.config["authority_revoked_reason"] == "trigger_authority_quarantined"
    assert "created_by" not in legacy_trigger.config


@pytest.mark.asyncio
async def test_workflow_effect_user_is_the_requester_not_the_agent_owner(monkeypatch):
    from app.services.workflow_trigger import _workflow_effect_user_id

    tenant_id = uuid4()
    owner_id = uuid4()
    requester_id = uuid4()
    agent = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, owner_user_id=owner_id, creator_id=owner_id)
    owner = _user(id=owner_id, tenant_id=tenant_id)
    requester = _user(id=requester_id, tenant_id=tenant_id)
    inactive = _user(id=requester_id, tenant_id=tenant_id, is_active=False)
    use_grant = SimpleNamespace(scope_type="user", scope_id=requester_id, access_level="use")
    sessions = [
        _ScriptSession(
            [_Scalar(agent), _Scalar(owner), _Scalar(requester), _Scalar(agent), _Many([use_grant])]
        ),
        _ScriptSession([_Scalar(agent), _Scalar(owner), _Scalar(inactive)]),
    ]
    monkeypatch.setattr("app.database.tenant_scoped_session", lambda *_args, **_kwargs: sessions.pop(0))

    effect_user_id, reason = await _workflow_effect_user_id(
        tenant_id=tenant_id, agent_id=agent.id, requester_user_id=requester_id
    )
    assert effect_user_id == requester_id
    assert reason is None
    blocked_user_id, blocked_reason = await _workflow_effect_user_id(
        tenant_id=tenant_id, agent_id=agent.id, requester_user_id=requester_id
    )
    assert blocked_user_id is None
    assert blocked_reason == "trigger_requester_inactive"


@pytest.mark.asyncio
async def test_pending_approval_routes_web_and_feishu_to_the_live_owner(monkeypatch):
    from app.services.approval_service import ApprovalService

    tenant_id = uuid4()
    owner_id = uuid4()
    creator_id = uuid4()
    agent = SimpleNamespace(id=uuid4(), name="Analyst", tenant_id=tenant_id, creator_id=creator_id, owner_user_id=owner_id)
    approval = SimpleNamespace(id=uuid4(), action_type="send_file", details={"tool": "send_file"})
    channel = SimpleNamespace(agent_id=agent.id, app_id="app", app_secret="secret", extra_config={})
    notified = []
    cards = []

    async def fake_send(_db, *, user_id, **_kwargs):
        notified.append(user_id)

    async def fake_card(app_id, _secret, receive_id, id_type, *_args, **_kwargs):
        cards.append((app_id, receive_id, id_type))

    async def fake_target(_db, *, user):
        return ("ou_owner", "open_id") if user.id == owner_id else None

    async def execute(statement):
        if "channel_config" in str(statement).lower():
            return _Rows([channel])
        return _Rows([_user(id=owner_id, tenant_id=tenant_id)])

    monkeypatch.setattr("app.services.notification_service.send_notification", fake_send)
    monkeypatch.setattr("app.services.approval_service.feishu_service.send_approval_card", fake_card)
    monkeypatch.setattr("app.services.approval_service.channel_user_service.get_feishu_delivery_target", fake_target)
    db = _TriggerDB([])
    db.execute = execute
    await ApprovalService()._notify_pending_approval(db, agent, approval)
    assert notified == [owner_id]
    assert creator_id not in notified
    assert cards == [("app", "ou_owner", "open_id")]


@pytest.mark.asyncio
async def test_pending_approval_falls_back_to_scoped_admin_when_owner_is_inactive(monkeypatch):
    from app.services.approval_service import ApprovalService

    tenant_id = uuid4()
    owner_id = uuid4()
    admin_id = uuid4()
    creator_id = uuid4()
    agent = SimpleNamespace(id=uuid4(), name="Analyst", tenant_id=tenant_id, creator_id=creator_id, owner_user_id=owner_id)
    approval = SimpleNamespace(id=uuid4(), action_type="send_file", details={})
    channel = SimpleNamespace(agent_id=agent.id, app_id="app", app_secret="secret", extra_config={})
    notified = []
    cards = []

    async def fake_send(_db, *, user_id, **_kwargs):
        notified.append(user_id)

    async def fake_card(_app_id, _secret, receive_id, id_type, *_args, **_kwargs):
        cards.append((receive_id, id_type))

    async def fake_target(_db, *, user):
        return ("ou_admin", "open_id") if user.id == admin_id else None

    queries = {"n": 0}

    async def execute(statement):
        text = str(statement).lower()
        if "channel_config" in text:
            return _Rows([channel])
        queries["n"] += 1
        if queries["n"] == 1:
            return _Rows([_user(id=owner_id, tenant_id=tenant_id, is_active=False)])
        return _Rows([_user(id=admin_id, tenant_id=tenant_id, role="org_admin")])

    monkeypatch.setattr("app.services.notification_service.send_notification", fake_send)
    monkeypatch.setattr("app.services.approval_service.feishu_service.send_approval_card", fake_card)
    monkeypatch.setattr("app.services.approval_service.channel_user_service.get_feishu_delivery_target", fake_target)
    db = _TriggerDB([])
    db.execute = execute
    await ApprovalService()._notify_pending_approval(db, agent, approval)
    assert notified == [admin_id]
    assert owner_id not in notified
    assert creator_id not in notified
    assert cards == [("ou_admin", "open_id")]


@pytest.mark.asyncio
async def test_previous_owner_cannot_resolve_an_old_approval_card():
    from datetime import datetime, timezone

    from app.services.approval_service import ApprovalService

    tenant_id = uuid4()
    owner_id = uuid4()
    old_id = uuid4()
    agent = SimpleNamespace(id=uuid4(), name="Analyst", tenant_id=tenant_id, creator_id=old_id, owner_user_id=owner_id)
    approval = SimpleNamespace(
        id=uuid4(),
        status="pending",
        agent_id=agent.id,
        tenant_id=tenant_id,
        details={},
        action_type="send_file",
        tool_name=None,
        created_at=datetime.now(timezone.utc),
    )
    results = [_Rows([approval]), _Rows([agent])]
    db = SimpleNamespace(commits=0)

    async def execute(_statement):
        return results.pop(0)

    async def commit():
        db.commits += 1

    db.execute = execute
    db.commit = commit
    with pytest.raises(ValueError, match="current Agent owner"):
        await ApprovalService().resolve_approval(db, approval.id, _user(id=old_id, tenant_id=tenant_id), "approve")
    assert approval.status == "pending"
    assert db.commits == 0


@pytest.mark.asyncio
async def test_resolution_notifies_current_owner_and_active_requester(monkeypatch):
    from datetime import datetime, timezone

    from app.services.approval_service import ApprovalService

    tenant_id = uuid4()
    owner_id = uuid4()
    admin_id = uuid4()
    requester_id = uuid4()
    old_id = uuid4()
    agent = SimpleNamespace(id=uuid4(), name="Analyst", tenant_id=tenant_id, creator_id=old_id, owner_user_id=owner_id)
    approval = SimpleNamespace(
        id=uuid4(),
        status="pending",
        agent_id=agent.id,
        tenant_id=tenant_id,
        details={"requested_by": str(requester_id)},
        action_type="send_file",
        tool_name=None,
        created_at=datetime.now(timezone.utc),
        execution_status=None,
        execution_receipt=None,
    )
    owner = _user(id=owner_id, tenant_id=tenant_id)
    requester = _user(id=requester_id, tenant_id=tenant_id)
    results = [_Rows([approval]), _Rows([agent]), _Rows([owner]), _Rows([owner]), _Rows([requester])]
    notified = []

    async def execute(_statement):
        return results.pop(0)

    async def fake_send(_db, *, user_id, **_kwargs):
        notified.append(user_id)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.notification_service.send_notification", fake_send)
    monkeypatch.setattr("app.services.local_agent_channel_service.apply_local_agent_action_approval", noop)
    monkeypatch.setattr("app.core.policy.write_audit_event", noop)
    db = SimpleNamespace(added=[])
    db.execute = execute
    db.add = db.added.append
    db.flush = noop
    db.commit = noop
    await ApprovalService().resolve_approval(db, approval.id, _user(id=admin_id, tenant_id=tenant_id, role="org_admin"), "approve")
    assert approval.status == "approved"
    assert approval.resolved_by == admin_id
    assert notified == [owner_id, requester_id]
    assert old_id not in notified

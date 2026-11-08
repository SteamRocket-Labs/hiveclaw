"""G1 round 2 review fixes — the restored employee runs again, refusals read 已停用,
deactivation withdraws running work, and list removal keeps its guards.

Each test fails on ``bfacf092`` (the round-1 G1 commit) and pins the reviewed
contract: ``status = "stopped"`` rides in ``prepare``'s deactivation commit so a
committed-then-conflicted deactivation still closes the status gates; restore
returns the employee to the runnable idle value; every member-facing deactivation
refusal shows the one plain word 已停用; the deactivate route settles unfinished
RuntimeTasks; the dashboard query skips conversations removed from the caller's
list; a group participant can remove a shared conversation from their own list;
and removal still refuses while work is unsettled.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _Result:
    def __init__(self, value=None, values=()):
        self._value = value
        self._values = list(values)

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        if self._value is None:
            return 1  # UPDATE ... RETURNING shape (sync-version bump)
        return self._value

    def all(self):
        return self._values

    def __iter__(self):
        return iter(self._values)


class _FakeDB:
    """Minimal async session: queued results, recorded statements and commits."""

    def __init__(self, results=()):
        self._results = list(results)
        self.statements: list[str] = []
        self.added: list[object] = []
        self.commits: list[int] = 0  # type: ignore[assignment]
        self.rollbacks = 0

    async def execute(self, stmt):
        self.statements.append(str(stmt))
        return self._results.pop(0) if self._results else _Result()

    async def scalar(self, stmt):
        self.statements.append(str(stmt))
        if self._results:
            return self._results.pop(0).scalar_one_or_none()
        return None

    async def scalars(self, stmt):
        self.statements.append(str(stmt))
        if self._results:
            return self._results.pop(0)
        return _Result()

    async def get(self, _model, _pk, **_kwargs):
        return None

    async def flush(self):
        return None

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


# ─── fix 1: stopped rides in the deactivation commit ────────────────────


@pytest.mark.asyncio
async def test_prepare_commits_stopped_with_deactivated_at_even_when_withdrawal_conflicts(
    monkeypatch,
) -> None:
    """A prepare that commits and then reports unsettled outward work must not
    leave the status gates (triggers, bot-to-bot, delegation) open."""
    from app.services import agent_retirement

    tenant_id, actor_id = uuid4(), uuid4()
    agent = SimpleNamespace(
        id=uuid4(), tenant_id=tenant_id, status="idle", deleted_at=None,
        deactivated_at=None, deactivation_reason=None,
    )
    actor = SimpleNamespace(id=actor_id, is_active=True, role="org_admin", tenant_id=tenant_id)
    remote_task = SimpleNamespace(id=uuid4(), child_agent_id=uuid4())  # outward work that never settles
    db = _FakeDB(results=[_Result(agent)])
    commits: list[tuple[bool, str]] = []

    async def snapshot_commit():
        commits.append((agent.deactivated_at is not None, agent.status))
        db.commits = len(commits)

    db.commit = snapshot_commit  # type: ignore[method-assign]

    async def fake_runtime_user(_db, _user_id, **_kwargs):
        return actor

    def fake_admin(_user, *, resource_tenant_id):
        return True

    calls = {"external_work": 0}

    async def never_settled(_db, _agent):
        calls["external_work"] += 1
        return [remote_task], []

    withdrawn: list[object] = []

    async def fake_withdraw(_db, *, task_id, retiring_agent, actor):
        withdrawn.append(task_id)

    monkeypatch.setattr("app.services.runtime_user_identity.load_runtime_user", fake_runtime_user)
    monkeypatch.setattr("app.core.permissions.is_scoped_business_admin", fake_admin)
    monkeypatch.setattr(agent_retirement, "external_work", never_settled)
    monkeypatch.setattr(agent_retirement, "withdraw_source_work", fake_withdraw)

    with pytest.raises(HTTPException) as exc_info:
        await agent_retirement.prepare(db, agent=agent, actor=actor)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "agent_retirement_pending", exc_info.value.detail
    assert withdrawn, "the outward work must have gone through the withdrawal flow"
    # The deactivation commit itself carries status="stopped": a deactivated
    # employee cannot be admitted to delegation by its leftover idle status.
    assert commits and commits[0] == (True, "stopped"), commits
    assert agent.status == "stopped"


@pytest.mark.asyncio
async def test_restore_returns_the_employee_to_the_runnable_idle_state(monkeypatch) -> None:
    """After 恢复 the gates that read status/lifecycle admit the employee again."""
    import app.api.agents as agents_api
    from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason
    from app.services.a2a_collaboration_policy import ACTIVE_AGENT_STATUSES

    tenant_id = uuid4()
    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id)
    agent = SimpleNamespace(
        id=uuid4(), tenant_id=tenant_id, name="恢复后要能跑", status="stopped",
        deleted_at=None, deactivated_at=datetime.now(timezone.utc),
        deactivation_reason="retirement_pending",
    )

    async def fake_history_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent.id
        return agent, "read"

    async def fake_audit(_db, **_kwargs):
        return None

    async def fake_register(_db, _projection, **_kwargs):
        return None

    from app.services import agent_retirement

    async def settled(_db, _agent):
        return [], []

    monkeypatch.setattr("app.core.permissions.check_agent_history_access", fake_history_access)
    monkeypatch.setattr("app.core.policy.write_audit_event", fake_audit)
    monkeypatch.setattr("app.services.ai_assets.register_projection", fake_register)
    monkeypatch.setattr(agent_retirement, "external_work", settled)

    db = _FakeDB(results=[_Result(agent)])  # the row-lock re-select
    await agents_api.restore_agent(agent_id=agent.id, current_user=admin, db=db)

    # Exactly what the trigger, messaging and delegation gates read.
    assert agent.status == "idle"
    assert agent.status in ACTIVE_AGENT_STATUSES
    assert get_agent_lifecycle_block_reason(agent) is None


# ─── fix 2: one plain 已停用 at every member-facing refusal ─────────────


@pytest.mark.asyncio
async def test_member_chat_refusal_for_deactivation_is_the_plain_word(monkeypatch) -> None:
    from app.core.permissions import DEACTIVATED_REFUSAL_DETAIL, check_agent_access

    tenant_id = uuid4()
    member = SimpleNamespace(id=uuid4(), role="member", tenant_id=tenant_id)
    agent = SimpleNamespace(
        id=uuid4(), tenant_id=tenant_id, deleted_at=None,
        deactivated_at=datetime.now(timezone.utc), deactivation_reason="retirement_pending",
    )

    async def noop_pin(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.database.pin_rls_tenant_context", noop_pin)
    db = _FakeDB(results=[_Result(agent)])

    with pytest.raises(HTTPException) as exc_info:
        await check_agent_access(db, member, agent.id)

    assert exc_info.value.status_code == 410
    assert exc_info.value.detail == DEACTIVATED_REFUSAL_DETAIL == "已停用"

    # Every other lifecycle reason keeps its exact detail.
    import app.core.permissions as permissions_api

    monkeypatch.setattr(permissions_api, "get_agent_lifecycle_block_reason", lambda _agent: "expired")
    with pytest.raises(HTTPException) as other:
        await check_agent_access(_FakeDB(results=[_Result(agent)]), member, agent.id)
    assert other.value.detail == "Agent is not active: expired"


def test_websocket_maps_a_deactivation_refusal_to_the_deactivated_notice() -> None:
    from app.api.websocket import _agent_access_refusal_code
    from app.core.permissions import DEACTIVATED_REFUSAL_DETAIL, AgentDeactivatedRefusal
    from app.services.session_subscription import (
        SESSION_SUBSCRIPTION_CLOSE_CODES,
        SessionSubscriptionError,
        session_subscription_error_frame,
    )

    assert _agent_access_refusal_code(AgentDeactivatedRefusal()) == "agent_deactivated"
    # The typed refusal keeps the member-facing copy and the 410 shape.
    refusal = AgentDeactivatedRefusal()
    assert refusal.status_code == 410 and refusal.detail == DEACTIVATED_REFUSAL_DETAIL
    assert _agent_access_refusal_code(HTTPException(410, DEACTIVATED_REFUSAL_DETAIL)) == "session_forbidden"
    assert _agent_access_refusal_code(HTTPException(410, "Agent is not active: expired")) == "session_forbidden"
    assert _agent_access_refusal_code(HTTPException(403, "nope")) == "session_forbidden"
    frame = session_subscription_error_frame(SessionSubscriptionError("agent_deactivated"))
    assert frame["error"]["message_key"] == "session.agent_deactivated"
    assert "agent_deactivated" in SESSION_SUBSCRIPTION_CLOSE_CODES


@pytest.mark.asyncio
async def test_delegation_to_a_deactivated_employee_refuses_with_the_plain_word() -> None:
    from app.services.a2a_collaboration_policy import resolve_a2a_collaboration_policy

    tenant_id = uuid4()

    def employee(name, *, status, deactivated_at=None):
        return SimpleNamespace(
            id=uuid4(), tenant_id=tenant_id, name=name, agent_class="standard",
            status=status, deleted_at=None, deactivated_at=deactivated_at,
            owner_user_id=uuid4(), owner_is_active=True,
        )

    source = employee("发消息的员工", status="idle")
    # The exact state a committed-then-conflicted prepare leaves: deactivated,
    # with the status gates closed in the same commit.
    deactivated = employee("被停用的员工", status="stopped", deactivated_at=datetime.now(timezone.utc))
    policy = await resolve_a2a_collaboration_policy(object(), source, deactivated, action="delegate")
    assert policy.allowed is False
    assert policy.reason == "target_unavailable"
    assert policy.message == "被停用的员工 已停用。", policy.message

    # A manually stopped employee that was never deactivated keeps its own wording.
    stopped = employee("手动停下的员工", status="stopped")
    policy = await resolve_a2a_collaboration_policy(object(), source, stopped, action="delegate")
    assert policy.allowed is False
    assert "currently stopped" in policy.message, policy.message


@pytest.mark.asyncio
async def test_task_executor_refusal_for_deactivation_reads_the_plain_word(monkeypatch) -> None:
    from app.services import task_executor

    tenant_id, agent_id, task_id = uuid4(), uuid4(), uuid4()
    agent = SimpleNamespace(
        id=agent_id, tenant_id=tenant_id, deleted_at=None,
        deactivated_at=datetime.now(timezone.utc),
    )
    task = SimpleNamespace(
        id=task_id, agent_id=agent_id, created_by=uuid4(), plan_authorization=None,
        status="pending", last_execution_status=None, title="t", description="d", type="todo",
    )
    logs: list[str] = []
    session_results = [
        _Result(task),                      # task lookup
        _Result(True),                      # tenant liveness
        _Result(agent),                     # agent lookup (second session)
    ]

    class _SessionFactory:
        def __init__(self, results):
            self._results = list(results)

        async def __aenter__(self):
            return _FakeDB(results=self._results)

        async def __aexit__(self, *_exc):
            return False

    sessions = [
        _SessionFactory([session_results[0], session_results[1]]),
        _SessionFactory([session_results[2]]),
    ]

    def fake_tenant_scope(_tenant_id):
        return sessions.pop(0)

    async def fake_resolve_tenant(_agent_id):
        return tenant_id

    async def fake_plan_gate(_db, **_kwargs):
        return True, None

    async def fake_log_error(_task_id, message, **_kwargs):
        logs.append(message)

    monkeypatch.setattr(task_executor, "resolve_tenant_for_agent", fake_resolve_tenant)
    monkeypatch.setattr(task_executor, "tenant_scoped_session", fake_tenant_scope)
    monkeypatch.setattr(task_executor, "_task_plan_gate_allows", fake_plan_gate)
    monkeypatch.setattr(task_executor, "_log_error", fake_log_error)

    outcome = await task_executor.execute_task(task_id, agent_id)

    assert outcome.error_code == "agent_lifecycle_blocked"
    assert outcome.summary == "Digital employee is deactivated.", outcome.summary
    assert outcome.retryable is False, "deactivation is a state, not an outage"
    assert logs == ["已停用"], logs


# ─── fix 3: the deactivate route settles unfinished runtime work ────────


@pytest.mark.asyncio
async def test_deactivate_settles_unfinished_runtime_tasks_after_prepare(monkeypatch) -> None:
    import app.api.agents as agents_api
    import app.services.agent_identity_lifecycle as lifecycle
    from app.services import agent_retirement

    tenant_id = uuid4()
    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id)
    agent = SimpleNamespace(
        id=uuid4(), tenant_id=tenant_id, name="停用时还有活", status="idle", agent_class="internal_tenant",  # G3f review 3: ordinary employee.
        deleted_at=None, deactivated_at=None, deactivation_reason=None,
    )
    settled: list[dict] = []

    async def fake_history_access(_db, _user, requested_agent_id):
        assert requested_agent_id == agent.id
        return agent, "manage"

    async def fake_prepare(_db, *, agent, actor):
        agent.deactivated_at = datetime.now(timezone.utc)
        agent.deactivation_reason = agent_retirement.PENDING_REASON
        agent.status = "stopped"

    async def fake_settle(_db, agent_in, *, actor_id, reason, terminal_source):
        settled.append({"agent": agent_in.id, "actor": actor_id, "reason": reason, "source": terminal_source})
        assert agent_in.deactivated_at is not None

    async def fake_audit(_db, **_kwargs):
        return None

    async def fake_register(_db, _projection, **_kwargs):
        return None

    monkeypatch.setattr("app.core.permissions.check_agent_history_access", fake_history_access)
    monkeypatch.setattr(agent_retirement, "prepare", fake_prepare)
    monkeypatch.setattr(lifecycle, "terminate_agent_runtime_tasks", fake_settle)
    monkeypatch.setattr("app.core.policy.write_audit_event", fake_audit)
    monkeypatch.setattr("app.services.ai_assets.register_projection", fake_register)
    # queue: bump_sync_version's UPDATE…RETURNING, then the in-lock re-select
    db = _FakeDB(results=[_Result(agent), _Result(agent)])

    await agents_api.deactivate_agent(agent_id=agent.id, current_user=admin, db=db)

    assert settled == [{
        "agent": agent.id, "actor": admin.id,
        "reason": "agent_deactivated", "source": "api.agents:deactivate_agent",
    }], "work running at deactivation must be withdrawn"


# ─── fix 4: a removed conversation leaves the home page too ─────────────


@pytest.mark.asyncio
async def test_dashboard_recent_work_query_excludes_own_list_removals() -> None:
    import app.api.dashboard as dashboard_api

    class _CaptureDB:
        def __init__(self):
            self.statement = None

        async def execute(self, statement):
            self.statement = statement
            return _Result(values=[])

    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), department_id=None)
    db = _CaptureDB()
    await dashboard_api._load_dashboard_sessions(db, user, agent_ids=[uuid4()], limit=5)
    sql = str(db.statement.compile(compile_kwargs={"literal_binds": True}))
    assert "removed_from_list_by" in sql, sql


# ─── fixes 5 + 6: who may remove, and unsettled work keeps it listed ────


def _group_session(agent_id, tenant_id):
    return SimpleNamespace(
        id=uuid4(), agent_id=agent_id, tenant_id=tenant_id, user_id=None,
        audience_kind="group", transcript_metadata_json={},
    )


@pytest.mark.asyncio
async def test_group_participant_removes_the_conversation_from_their_own_list(monkeypatch) -> None:
    import app.api.chat_sessions as chat_sessions_api
    import app.services.shared_group_authority as group_authority

    tenant_id = uuid4()
    participant = SimpleNamespace(id=uuid4(), role="member", tenant_id=tenant_id)
    agent_id = uuid4()
    session = _group_session(agent_id, tenant_id)
    member_checks: list[object] = []

    async def fake_member(_db, *, session, user):
        member_checks.append((session.id, user.id))
        return None

    async def fake_lock(_db, *, session_id):
        return None

    async def fake_unresolved(_db, **_kwargs):
        return []

    monkeypatch.setattr(group_authority, "require_current_group_member", fake_member)
    monkeypatch.setattr("app.services.chat_transcript.lock_transcript_session", fake_lock)
    monkeypatch.setattr("app.services.session_tool_runtime.list_unresolved_tool_effects", fake_unresolved)

    async def fake_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    monkeypatch.setattr("app.api.chat_sessions.check_agent_history_access", fake_agent_access)
    db = _FakeDB(results=[_Result(session), _Result(session)])

    await chat_sessions_api.delete_session(
        agent_id=agent_id, session_id=session.id, current_user=participant, db=db)

    assert member_checks and member_checks[0] == (session.id, participant.id)
    assert str(participant.id) in session.transcript_metadata_json["removed_from_list_by"]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_outsider_cannot_remove_someone_elses_conversation(monkeypatch) -> None:
    import app.api.chat_sessions as chat_sessions_api
    import app.services.shared_group_authority as group_authority

    tenant_id = uuid4()
    admin = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id)
    agent_id = uuid4()
    session = _group_session(agent_id, tenant_id)
    metadata_before = dict(session.transcript_metadata_json)

    async def refuse_member(_db, *, session, user):
        raise HTTPException(status_code=403, detail="not a member")

    async def fake_lock(_db, *, session_id):
        return None

    monkeypatch.setattr(group_authority, "require_current_group_member", refuse_member)
    monkeypatch.setattr("app.services.chat_transcript.lock_transcript_session", fake_lock)

    async def fake_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    monkeypatch.setattr("app.api.chat_sessions.check_agent_history_access", fake_agent_access)
    db = _FakeDB(results=[_Result(session), _Result(session)])

    with pytest.raises(HTTPException) as exc_info:
        await chat_sessions_api.delete_session(
            agent_id=agent_id, session_id=session.id, current_user=admin, db=db)

    assert exc_info.value.status_code == 403
    assert session.transcript_metadata_json == metadata_before


@pytest.mark.asyncio
async def test_removal_refuses_while_work_is_unsettled(monkeypatch) -> None:
    import app.api.chat_sessions as chat_sessions_api

    tenant_id = uuid4()
    owner = SimpleNamespace(id=uuid4(), role="member", tenant_id=tenant_id)
    agent_id = uuid4()
    session = SimpleNamespace(
        id=uuid4(), agent_id=agent_id, tenant_id=tenant_id, user_id=owner.id,
        audience_kind=None, transcript_metadata_json={},
    )

    async def fake_lock(_db, *, session_id):
        return None

    async def fake_unresolved(_db, **_kwargs):
        return []

    monkeypatch.setattr("app.services.chat_transcript.lock_transcript_session", fake_lock)
    monkeypatch.setattr("app.services.session_tool_runtime.list_unresolved_tool_effects", fake_unresolved)

    async def fake_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    monkeypatch.setattr("app.api.chat_sessions.check_agent_history_access", fake_agent_access)
    # One unfinished RuntimeTask still anchors this conversation. The session
    # is queued twice: the route's initial lookup and its in-lock re-select.
    db = _FakeDB(results=[_Result(session), _Result(session)])

    async def unfinished_task(_stmt):
        return uuid4()

    db.scalar = unfinished_task  # type: ignore[method-assign]

    with pytest.raises(HTTPException) as exc_info:
        await chat_sessions_api.delete_session(
            agent_id=agent_id, session_id=session.id, current_user=owner, db=db)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "session_work_unsettled", exc_info.value.detail
    assert "removed_from_list_by" not in session.transcript_metadata_json
    assert db.commits == 0


@pytest.mark.asyncio
async def test_approving_a_tool_permission_requires_an_active_employee(monkeypatch) -> None:
    """A pending permission on a deactivated employee cannot be approved into effect."""
    import app.api.chat_sessions as chat_sessions_api

    tenant_id = uuid4()
    owner = SimpleNamespace(id=uuid4(), role="member", tenant_id=tenant_id)
    agent_id = uuid4()
    session = SimpleNamespace(id=uuid4(), agent_id=agent_id, tenant_id=tenant_id, user_id=owner.id)
    agent = SimpleNamespace(
        id=agent_id, tenant_id=tenant_id, deleted_at=None,
        deactivated_at=datetime.now(timezone.utc), deactivation_reason="retirement_pending",
    )

    async def noop_pin(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.database.pin_rls_tenant_context", noop_pin)
    db = _FakeDB(results=[_Result(session), _Result(agent)])

    with pytest.raises(HTTPException) as exc_info:
        await chat_sessions_api.resolve_session_permission(
            agent_id=agent_id, session_id=session.id, permission_request_id=uuid4(),
            body=SimpleNamespace(action="allow_once"),
            current_user=owner, db=db,
        )

    assert exc_info.value.status_code == 410
    assert exc_info.value.detail == "已停用"

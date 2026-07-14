from __future__ import annotations

import contextlib
import uuid

import pytest


def _patch_tenant_scoped_session(monkeypatch, session_supplier, *, tenant_id=None):
    """RLS 阶段2b: activity_logger now resolves the agent's tenant and opens a
    ``tenant_scoped_session`` instead of a bare ``async_session``. Route the
    scoped session to the test's fake session factory and stub tenant
    resolution so no real DB / bypass read happens. Returns the tenant_id the
    stub resolves to, so the caller can assert the INSERTed row carries it."""
    resolved_tenant = tenant_id or uuid.uuid4()

    @contextlib.asynccontextmanager
    async def _fake_tenant_scoped_session(*_a, **_k):
        yield session_supplier()

    async def _fake_resolve_tenant_for_agent(*_a, **_k):
        return resolved_tenant

    monkeypatch.setattr("app.services.activity_logger.tenant_scoped_session", _fake_tenant_scoped_session)
    monkeypatch.setattr("app.services.activity_logger.resolve_tenant_for_agent", _fake_resolve_tenant_for_agent)
    return resolved_tenant


def test_activity_action_enum_includes_tool_runtime_approval_events():
    from app.models.activity_log import AgentActivityLog

    action_enum = AgentActivityLog.__table__.c.action_type.type

    assert "tool_call_direct" in action_enum.enums
    assert "tool_call_approved" in action_enum.enums
    assert "llm_error" in action_enum.enums


class _FailingSession:
    def __init__(self, *, fail_on_commit: bool):
        self.fail_on_commit = fail_on_commit
        self.rollback_calls = 0
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        if self.fail_on_commit:
            raise RuntimeError("commit failed")

    async def rollback(self):
        self.rollback_calls += 1


class _EnumDriftSession(_FailingSession):
    async def commit(self):
        raise RuntimeError('invalid input value for enum activity_action_enum: "tool_call_approved"')


class _LlmErrorEnumDriftSession(_FailingSession):
    async def commit(self):
        raise RuntimeError('invalid input value for enum activity_action_enum: "llm_error"')


@pytest.mark.asyncio
async def test_log_activity_rolls_back_on_commit_error(monkeypatch):
    from app.services.activity_logger import log_activity

    fake_session = _FailingSession(fail_on_commit=True)
    _patch_tenant_scoped_session(monkeypatch, lambda: fake_session)

    await log_activity(
        agent_id=uuid.uuid4(),
        action_type="delegation_started",
        summary="Delegation started",
        detail={"task_id": "task-1"},
    )

    assert fake_session.rollback_calls == 1


@pytest.mark.asyncio
async def test_log_activity_commits_successfully(monkeypatch):
    from app.services.activity_logger import log_activity

    fake_session = _FailingSession(fail_on_commit=False)
    tenant_id = _patch_tenant_scoped_session(monkeypatch, lambda: fake_session)

    await log_activity(
        agent_id=uuid.uuid4(),
        action_type="delegation_completed",
        summary="Delegation completed",
        detail={"task_id": "task-1"},
    )

    assert len(fake_session.added) == 1
    # RLS 阶段2b: the INSERTed activity row must carry the resolved tenant_id —
    # a NULL would be globally visible under the USING-only policy.
    assert fake_session.added[0].tenant_id == tenant_id
    assert fake_session.rollback_calls == 0


@pytest.mark.asyncio
async def test_activity_summary_preview_preserves_complete_text_in_durable_detail(monkeypatch):
    from app.services.activity_logger import log_activity

    fake_session = _FailingSession(fail_on_commit=False)
    _patch_tenant_scoped_session(monkeypatch, lambda: fake_session)
    decisive_tail = "ACTIVITY_DECISIVE_TAIL"
    summary = ("s" * 1_200) + decisive_tail

    await log_activity(
        agent_id=uuid.uuid4(),
        action_type="tool_call",
        summary=summary,
        detail={"tool": "example"},
    )

    row = fake_session.added[0]
    assert len(row.summary) <= 500
    assert row.detail_json["full_summary"] == summary
    assert row.detail_json["full_summary"].endswith(decisive_tail)


@pytest.mark.asyncio
async def test_log_activity_persists_owner_and_root_session_or_quarantines_unknown_provenance(monkeypatch):
    from app.services.activity_logger import log_activity

    owner_id = uuid.uuid4()
    root_session_id = uuid.uuid4()
    owned_session = _FailingSession(fail_on_commit=False)
    unknown_session = _FailingSession(fail_on_commit=False)
    sessions = iter([owned_session, unknown_session])
    _patch_tenant_scoped_session(monkeypatch, lambda: next(sessions))

    await log_activity(
        agent_id=uuid.uuid4(),
        action_type="tool_call",
        summary="Owned execution",
        owner_user_id=owner_id,
        root_session_id=root_session_id,
    )
    await log_activity(
        agent_id=uuid.uuid4(),
        action_type="heartbeat",
        summary="Autonomous legacy activity",
    )

    owned = owned_session.added[0]
    assert owned.owner_user_id == owner_id
    assert owned.root_session_id == root_session_id
    assert owned.authority_state == "owned"
    unknown = unknown_session.added[0]
    assert unknown.owner_user_id is None
    assert unknown.root_session_id is None
    assert unknown.authority_state == "quarantined"


@pytest.mark.asyncio
async def test_log_activity_falls_back_when_runtime_enum_value_is_missing(monkeypatch):
    from app.services.activity_logger import log_activity

    first_session = _EnumDriftSession(fail_on_commit=True)
    second_session = _FailingSession(fail_on_commit=False)
    sessions = iter([first_session, second_session])
    _patch_tenant_scoped_session(monkeypatch, lambda: next(sessions))

    await log_activity(
        agent_id=uuid.uuid4(),
        action_type="tool_call_approved",
        summary="Approved tool execution",
        detail={"tool": "write_file", "approved": True},
    )

    assert first_session.rollback_calls == 1
    assert len(second_session.added) == 1
    assert second_session.added[0].action_type == "tool_call"
    assert second_session.added[0].detail_json["activity_type_original"] == "tool_call_approved"
    assert second_session.added[0].detail_json["activity_type_fallback"] == "tool_call"


@pytest.mark.asyncio
async def test_log_activity_falls_back_when_llm_error_enum_value_is_missing(monkeypatch):
    from app.services.activity_logger import log_activity

    first_session = _LlmErrorEnumDriftSession(fail_on_commit=True)
    second_session = _FailingSession(fail_on_commit=False)
    sessions = iter([first_session, second_session])
    _patch_tenant_scoped_session(monkeypatch, lambda: next(sessions))

    await log_activity(
        agent_id=uuid.uuid4(),
        action_type="llm_error",
        summary="LLM failed",
        detail={"channel": "web"},
    )

    assert first_session.rollback_calls == 1
    assert len(second_session.added) == 1
    assert second_session.added[0].action_type == "error"
    assert second_session.added[0].detail_json["activity_type_original"] == "llm_error"
    assert second_session.added[0].detail_json["activity_type_fallback"] == "error"

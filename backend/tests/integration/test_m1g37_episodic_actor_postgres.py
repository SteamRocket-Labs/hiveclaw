"""Episodic memory privacy is enforced at the boundaries that still read T2."""

import json
import uuid
from types import SimpleNamespace

import pytest

from tests.services.test_session_input_control_v2 import _seed_session
from tests.memory.test_retrieval_pipeline import _write_t2_package

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def episodic_reader(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    tenant, user, agent, session, run = await _seed_session(owner_sessionmaker, active_run=True)
    other_tenant, _other_user, _other_agent, other_session, _other_run = await _seed_session(owner_sessionmaker)
    foreign, previous, foreign_session = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        from app.models.chat_session import ChatSession
        from app.models.user import User

        (await db.get(User, user)).role = "member"
        db.add(User(id=foreign, tenant_id=tenant, username=f"foreign-{foreign.hex}", email=f"{foreign.hex}@fixture.test",
            password_hash="fixture", display_name="Another requester", role="member"))
        await db.flush()
        db.add_all([ChatSession(id=previous, tenant_id=tenant, agent_id=agent, user_id=user, audience_kind="private"),
            ChatSession(id=foreign_session, tenant_id=tenant, agent_id=agent, user_id=foreign, audience_kind="private")])
        await db.commit()
    for source, body in [(previous, "OWN_PRIVATE_T2_BODY"), (foreign_session, "OTHER_REQUESTER_PRIVATE_T2_BODY"),
        (other_session, "OTHER_TENANT_PRIVATE_T2_BODY")]:
        _write_t2_package(tmp_path, agent, session_id=str(source), segment_id=f"seg-{source.hex}", summary=body)
    from app.config import get_settings
    from app.services import agent_tools

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", tmp_path)
    return SimpleNamespace(tenant=tenant, user=user, agent=agent, session=session, run=run,
        previous=previous, foreign=foreign, foreign_session=foreign_session, other_tenant=other_tenant, root=tmp_path)


@pytest.mark.parametrize("case", ["normal", "shared_worker"])
async def test_private_t2_reads_stay_bound_to_the_requesters_own_private_sessions(
    episodic_reader, owner_sessionmaker, app_user_sessionmaker, case,
):
    """On-demand memory has no prefetched T2; read_file keeps its authority."""
    from app.models.chat_session import ChatSession
    from app.services import agent_tools

    s = episodic_reader
    async with owner_sessionmaker() as db:
        if case == "shared_worker":
            (await db.get(ChatSession, s.session)).audience_kind = "group"
        await db.commit()
    service = agent_tools._get_tool_runtime_service()
    own_path = f"memory/t2/sessions/{s.previous}/segments/seg-{s.previous.hex}/summary.md"
    other_path = f"memory/t2/sessions/{s.foreign_session}/segments/seg-{s.foreign_session.hex}/summary.md"

    # The on-demand read boundary serves only the requester's own private sessions.
    from fastapi import HTTPException

    for path, allowed in ((own_path, case == "normal"), (other_path, False)):
        try:
            result = str(await service.execute("read_file", {"path": path},
                agent_id=s.agent, user_id=s.user, session_id=str(s.session), runtime_task_id=str(s.run),
                emit_runtime_hooks=False))
        except HTTPException as denial:
            # A shared worker session loses native session-source reads outright.
            assert not allowed and denial.status_code in {401, 403}, denial.detail
            continue
        assert ("OWN_PRIVATE_T2_BODY" in result) is allowed, result
        assert "OTHER_REQUESTER_PRIVATE_T2_BODY" not in result, result


async def test_memory_service_context_keeps_resident_identity_on_authority_outage(
    episodic_reader, monkeypatch,
):
    """A typed outage degrades memory without dropping the resident identity."""
    from types import SimpleNamespace as NS

    from app.services import memory_service

    s = episodic_reader

    async def activation(**_kwargs):
        return object()  # resolved accountability; the resident build proceeds

    monkeypatch.setattr(memory_service, "get_settings", lambda: NS(AGENT_DATA_DIR=str(s.root)))
    monkeypatch.setattr(memory_service, "_resolve_accountability_context", activation)
    from app.memory.profile_plane import ResidentMemory

    monkeypatch.setattr("app.memory.profile_plane.load_resident_memory", lambda **_kwargs:
        ResidentMemory(text="AUTHENTICATED_RESIDENT_CONTEXT", chars=30, budget_chars=20000,
            over_budget=False, sections=(), active_failure_modes=""))
    async def budget(**_kwargs):
        return None

    monkeypatch.setattr("app.memory.profile_plane.check_resident_budget", budget)

    async def failing_authorize(**_kwargs):
        raise OSError("synthetic explicit-authority outage")

    monkeypatch.setattr("app.services.explicit_memory_authority.authorized_explicit_entries", failing_authorize)
    result = await memory_service.build_memory_context(s.agent, s.tenant, session_id=str(s.session), query="review",
        current_user_id=s.user, return_result=True)
    assert result.status == "degraded", result
    assert result.degraded_components == ("explicit_memory_source_authority",), result
    assert result.retryable is True
    assert "AUTHENTICATED_RESIDENT_CONTEXT" in result.content
    assert result.conversation_available and result.external_effects_available and result.durable_write_available

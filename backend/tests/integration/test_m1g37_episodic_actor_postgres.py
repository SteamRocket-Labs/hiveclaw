"""T2 recall checks the live native reader before selection and prompt release."""

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.memory.activation import ActivationContext
from app.memory.retriever import MemoryRetriever, _selection_descriptor
from app.memory.types import MemoryItem, MemoryKind
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.principal_context import Principal, PrincipalRole, PrincipalStack
from tests.memory.test_retrieval_pipeline import _write_t2_package
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def episodic_reader(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    tenant, user, agent, session, _run = await _seed_session(owner_sessionmaker, active_run=True)
    other_tenant, _other_user, _other_agent, other_session, _other_run = await _seed_session(owner_sessionmaker)
    foreign, previous, foreign_session = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
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
    # This independent backend fixture is not a private explicit-memory grant.
    # Explicit memory needs native author receipts even when marked public.
    async def unrelated_backend(_self, *_args, **_kwargs):
        return [MemoryItem(kind=MemoryKind.SEMANTIC, content="UNRELATED_AVAILABLE_CONTEXT", score=0.5,
            source="synthetic-independent-backend")]
    monkeypatch.setattr(MemoryRetriever, "_retrieve_semantic_backend", unrelated_backend)
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    context = ActivationContext(query="review", principal_stack=PrincipalStack(
        current_user=Principal(PrincipalRole.CURRENT_USER, str(user)),
        direct_owner=Principal(PrincipalRole.OWNER, str(user))))
    return SimpleNamespace(tenant=tenant, user=user, agent=agent, session=session, previous=previous,
        foreign=foreign, foreign_session=foreign_session, other_tenant=other_tenant, context=context, root=tmp_path)


@asynccontextmanager
async def _unavailable(*_args, **_kwargs):
    raise OSError("synthetic source-authority database outage")
    yield


@pytest.mark.parametrize("case", ["normal", "admin", "transferred", "shared_worker", "shared_source",
    "inactive", "missing_principal", "after_selection", "after_selection_outage", "outage", "legacy"])
async def test_private_t2_never_reaches_another_requester_or_shared_work(
    episodic_reader, owner_sessionmaker, monkeypatch, case,
):
    s = episodic_reader
    async with owner_sessionmaker() as db:
        if case == "admin":
            (await db.get(User, s.user)).role = "org_admin"
        elif case == "transferred":
            (await db.get(Agent, s.agent)).owner_user_id = s.foreign
        elif case == "shared_worker":
            (await db.get(ChatSession, s.session)).audience_kind = "group"
        elif case == "shared_source":
            (await db.get(ChatSession, s.previous)).audience_kind = "group"
        elif case == "inactive":
            (await db.get(User, s.user)).is_active = False
        await db.commit()
    if case == "missing_principal":
        s.context = ActivationContext(query="review", principal_stack=PrincipalStack())
    if case == "outage":
        monkeypatch.setattr("app.database.tenant_scoped_session", _unavailable)
    if case == "legacy":
        _write_t2_package(s.root, s.agent, session_id="legacy-without-canonical-session", summary="UNKNOWN_LEGACY_BODY")
    originals = {path: path.read_bytes() for path in
        (s.root / str(s.agent) / "memory/t2/sessions").glob("*/segments/*/summary.md")}
    observed = []
    async def select(*, items, **_kwargs):
        observed.extend(json.dumps(_selection_descriptor(item)) for item in items)
        if case == "after_selection":
            async with owner_sessionmaker() as db:
                (await db.get(Agent, s.agent)).owner_user_id = s.foreign
                await db.commit()
        if case == "after_selection_outage":
            monkeypatch.setattr("app.database.tenant_scoped_session", _unavailable)
        return [item.metadata["selection_candidate_id"] for item in items], "Select available evidence"
    retriever = MemoryRetriever(data_root=s.root)
    monkeypatch.setattr(retriever, "_select_with_model", select)
    result = await retriever.retrieve(s.agent, "review", str(s.session), str(s.tenant),
        activation_context=s.context, rerank_model_config={"provider": "synthetic", "model": "no-provider"})
    assert "OTHER_REQUESTER_PRIVATE_T2_BODY" not in "\n".join(observed)
    assert "OTHER_TENANT_PRIVATE_T2_BODY" not in "\n".join(observed)
    assert "UNKNOWN_LEGACY_BODY" not in "\n".join(observed)
    assert any("UNRELATED_AVAILABLE_CONTEXT" in item.content for item in result)
    episodic = [item for item in result if item.kind == MemoryKind.EPISODIC]
    if case in {"normal", "admin", "legacy"}:
        assert len(episodic) == 1 and "OWN_PRIVATE_T2_BODY" in episodic[0].content
    else:
        assert episodic == []
    if case in {"after_selection", "after_selection_outage"}:
        assert "OWN_PRIVATE_T2_BODY" in "\n".join(observed)
    if case in {"shared_source", "shared_worker", "transferred", "inactive", "missing_principal",
        "after_selection", "after_selection_outage", "outage", "legacy"}:
        assert retriever.last_episodic_source_status == "unavailable"
    assert originals and all(path.is_file() and path.read_bytes() == body for path, body in originals.items())


async def test_actual_memory_service_reports_source_outage_without_disabling_other_context(
    episodic_reader, monkeypatch,
):
    from app.services import memory_service

    s = episodic_reader
    async def activation(**_kwargs):
        return s.context
    async def select(_self, *, items, **_kwargs):
        return [item.metadata["selection_candidate_id"] for item in items], "Select authorized available evidence"
    async def budget(**_kwargs):
        return None
    monkeypatch.setattr(memory_service, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(s.root)))
    monkeypatch.setattr(memory_service, "_resolve_activation_context", activation)
    monkeypatch.setattr(memory_service, "_get_rerank_model_config", lambda _tenant: {"provider": "synthetic", "model": "no-provider"})
    monkeypatch.setattr(MemoryRetriever, "_select_with_model", select)
    from app.memory.profile_plane import ResidentMemory

    monkeypatch.setattr("app.memory.profile_plane.load_resident_memory", lambda **_kwargs:
        ResidentMemory(text="AUTHENTICATED_RESIDENT_CONTEXT", chars=30, budget_chars=20000,
            over_budget=False, sections=(), active_failure_modes=""))
    monkeypatch.setattr("app.memory.profile_plane.check_resident_budget", budget)
    monkeypatch.setattr("app.database.tenant_scoped_session", _unavailable)
    result = await memory_service.build_memory_context(s.agent, s.tenant, session_id=str(s.session), query="review",
        current_user_id=s.user, return_result=True)
    assert result.status == "degraded" and result.code == "episodic_source_authority_unavailable", result
    assert "episodic_source_authority" in result.degraded_components and result.retryable is True
    assert "PRIVATE_T2_BODY" not in result.content
    assert "AUTHENTICATED_RESIDENT_CONTEXT" in result.content and "UNRELATED_AVAILABLE_CONTEXT" in result.content
    assert result.conversation_available and result.external_effects_available and result.durable_write_available

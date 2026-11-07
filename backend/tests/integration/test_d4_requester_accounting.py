"""The shared kernel charges the authenticated requester, including delegated turns."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.token_usage_event import TokenUsageEvent
from app.models.user import User
from tests.runtime.test_invoker import _FakeClient


@pytest.mark.parametrize("over_budget", [False, True])
async def test_requester_usage_and_quota_do_not_fall_back_to_bot_owner(owner_sessionmaker, monkeypatch, over_budget):
    import app.runtime.invoker as invoker
    from app.kernel import RuntimeConfig

    suffix = uuid4().hex
    async with owner_sessionmaker() as db:
        tenant = Tenant(name="D4 accounting", slug=f"d4-{suffix}")
        db.add(tenant)
        await db.flush()
        requester, owner = [User(username=f"{name}-{suffix}", email=f"{name}-{suffix}@example.test",
            tenant_id=tenant.id, display_name=name, password_hash="local-only", role="member", is_active=True,
            quota_tokens_per_day=10 if over_budget and name == "requester" else 1000,
            tokens_used_today=10 if over_budget and name == "requester" else 0)
            for name in ("requester", "owner")]
        db.add_all([requester, owner])
        await db.flush()
        bot = Agent(name="Target", role_description="Local D4 task", tenant_id=tenant.id, creator_id=owner.id, owner_user_id=owner.id)
        db.add(bot)
        await db.commit()

    async def empty(*a, **kw):
        return ""

    async def runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=tenant.id, max_tool_rounds=2)

    client = _FakeClient([SimpleNamespace(content="Finished", tool_calls=[], reasoning_content=None, usage={"total_tokens": 7})])
    for name in ("build_agent_context", "build_agent_runtime_context", "build_memory_context", "_resolve_retrieval_context"):
        monkeypatch.setattr(invoker, name, empty)
    monkeypatch.setattr(invoker, "_resolve_runtime_config", runtime_config)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *a, **kw: [])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **kw: client)
    monkeypatch.setattr(invoker, "get_max_tokens", lambda *a, **kw: 128)
    result = await invoker.invoke_agent(invoker.AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="local-fake", api_key="local", base_url=None),
        agent_id=bot.id, user_id=requester.id, tenant_id=tenant.id, agent_name=bot.name, role_description="",
        messages=[{"role": "user", "content": "Finish this task"}],
    ))
    async with owner_sessionmaker() as db:
        charged_requester = await db.get(User, requester.id)
        charged_owner = await db.get(User, owner.id)
        events = list(await db.scalars(select(TokenUsageEvent).where(TokenUsageEvent.agent_id == bot.id)))
        if over_budget:
            assert result.terminal_reason.value == "quota_denied"
            assert "Daily token limit" in result.content
            assert client.calls == []
            assert not events
            assert charged_requester.tokens_used_today == 10
        else:
            assert result.content == "Finished"
            assert charged_requester.tokens_used_total == 7
            assert len(events) == 1 and events[0].user_id == requester.id
        assert charged_owner.tokens_used_total == 0


async def _seed_delegated_run(owner_sessionmaker, tmp_path):
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    async with owner_sessionmaker() as db:
        owner = User(username=f"owner-{uuid4().hex}", email=f"{uuid4().hex}@example.test",
            tenant_id=seed["tenant_id"], display_name="Target owner", password_hash="local-only", role="member")
        db.add(owner)
        await db.flush()
        target = await db.get(Agent, seed["agent_id"])
        target.owner_user_id = target.creator_id = owner.id
        parent = Agent(name="Delegator", tenant_id=seed["tenant_id"],
            creator_id=seed["user_id"], owner_user_id=seed["user_id"])
        db.add(parent)
        await db.flush()
        parent_session = ChatSession(agent_id=parent.id, tenant_id=seed["tenant_id"], user_id=seed["user_id"])
        db.add(parent_session)
        await db.flush()
        task = await db.get(RuntimeTask, seed["run_id"])
        task.task_type = "delegation"
        task.parent_agent_id = parent.id
        task.parent_session_id = str(parent_session.id)
        child_session = await db.get(ChatSession, seed["session_id"])
        child_session.session_kind = "delegation_run"
        child_session.runtime_task_id = task.id
        child_session.peer_agent_id = parent.id
        child_session.parent_session_id = parent_session.id
        await db.commit()
    return {**seed, "owner_id": owner.id}


@pytest.mark.parametrize("has_requester", [True, False])
async def test_delegated_round_settlement_charges_requester_or_existing_owner_fallback(
    owner_sessionmaker, monkeypatch, tmp_path, has_requester,
):
    import app.runtime.invoker as invoker
    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import (
        ScriptedLLMClient, _patch_invoker_seams, _patch_runtime_seams, _run_turn,
    )
    from tests.integration.test_web_chat_worker_restart_post_commit_accounting import _sealed_round

    seed = await _seed_delegated_run(owner_sessionmaker, tmp_path)
    _patch_runtime_seams(monkeypatch, seed, owner_sessionmaker)
    client = ScriptedLLMClient([{"content": "Delegated result", "tool_calls": [], "finish_reason": "stop"}])
    _patch_invoker_seams(monkeypatch, client, seed, owner_sessionmaker)

    async def suppressed_kernel_charge(*args, **kwargs):
        return None

    monkeypatch.setattr(invoker, "record_token_usage", suppressed_kernel_charge)
    result = await _run_turn(owner_sessionmaker, seed, client, initial_round_index=0)
    assert result.content == "Delegated result"
    row, usage = await _sealed_round(owner_sessionmaker, seed["run_id"])
    assert row.state == "round_committed" and usage == {"prompt_tokens": 10, "completion_tokens": 5}
    async with owner_sessionmaker() as db:
        assert list(await db.scalars(select(TokenUsageEvent).where(TokenUsageEvent.agent_id == seed["agent_id"]))) == []
        task = await db.get(RuntimeTask, seed["run_id"])
        if not has_requester:
            task.root_user_id = None
            await db.commit()

    settlement = await runtime._settle_committed_round_token_usage(task, [row])
    assert settlement["settled"] == 1 and settlement["suppressed"] == 0
    repeated = await runtime._settle_committed_round_token_usage(task, [row])
    assert repeated["already_recorded"] == 1 and repeated["settled"] == 0
    async with owner_sessionmaker() as db:
        requester = await db.get(User, seed["user_id"])
        owner = await db.get(User, seed["owner_id"])
        events = list(await db.scalars(select(TokenUsageEvent).where(TokenUsageEvent.agent_id == seed["agent_id"])))
        assert requester.tokens_used_total == (15 if has_requester else 0)
        assert owner.tokens_used_total == (0 if has_requester else 15)
        assert len(events) == 1 and events[0].tokens == 15
        assert events[0].user_id == (requester.id if has_requester else owner.id)
        assert events[0].details["idempotency_key"] == row.provider_request_id


@pytest.mark.parametrize("has_requester", [True, False])
async def test_memory_selection_charges_requester_or_existing_owner_fallback(
    owner_sessionmaker, monkeypatch, tmp_path, has_requester,
):
    import json

    import app.services.llm_client as llm
    from app.memory.activation import ActivationContext
    from app.memory.retriever import MemoryRetriever
    from app.memory.types import MemoryItem, MemoryKind
    from app.services.principal_context import Principal, PrincipalRole, PrincipalStack

    seed = await _seed_delegated_run(owner_sessionmaker, tmp_path)
    retriever = MemoryRetriever(data_root=tmp_path)
    item = MemoryItem(kind=MemoryKind.SEMANTIC, content="Authorized memory", score=1.0,
        source="memory/knowledge/local.md", metadata={"title": "Local memory"})

    async def candidates(*args, **kwargs):
        return [item]

    async def no_team(*args, **kwargs):
        return []

    class LocalSelector:
        async def complete(self, **kwargs):
            return llm.LLMResponse(content=json.dumps({"selected_ids": [], "reason": "Not relevant"}),
                usage={"total_tokens": 7}, model="local-selector")

        async def close(self):
            pass

    monkeypatch.setattr(retriever, "_gather_authorized_candidates", candidates)
    monkeypatch.setattr(retriever, "_team_candidates", no_team)
    monkeypatch.setattr(llm, "create_llm_client", lambda **kwargs: LocalSelector())
    current_user = Principal(PrincipalRole.CURRENT_USER, str(seed["user_id"])) if has_requester else None
    await retriever.retrieve(seed["agent_id"], "current task", str(seed["session_id"]), str(seed["tenant_id"]),
        rerank_model_config={"provider": "openai", "model": "local-selector", "api_key": "local-only"},
        activation_context=ActivationContext(query="current task", principal_stack=PrincipalStack(current_user=current_user)))
    assert retriever.last_selection_status == "model_selected"
    async with owner_sessionmaker() as db:
        requester = await db.get(User, seed["user_id"])
        owner = await db.get(User, seed["owner_id"])
        events = list(await db.scalars(select(TokenUsageEvent).where(TokenUsageEvent.agent_id == seed["agent_id"])))
        assert requester.tokens_used_total == (7 if has_requester else 0)
        assert owner.tokens_used_total == (0 if has_requester else 7)
        assert len(events) == 1 and events[0].tokens == 7
        assert events[0].user_id == (requester.id if has_requester else owner.id)
        assert events[0].source == "memory_semantic_selection"

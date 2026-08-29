from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import uuid

import pytest


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, execute_values):
        self._execute_values = list(execute_values)
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        # tenant_scoped_session emits a `SET LOCAL app.current_tenant_id` before
        # the business query — it must not consume a prepared result.
        if "app.current_tenant_id" in str(_query):
            return _FakeScalarResult(None)
        if not self._execute_values:
            raise AssertionError("No fake execute result prepared")
        return _FakeScalarResult(self._execute_values.pop(0))

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_get_summary_model_config_falls_back_to_tenant_default_model(monkeypatch):
    from app.services import memory_service

    tenant_id = uuid4()
    model_id = uuid4()
    fake_model = SimpleNamespace(
        id=model_id,
        tenant_id=tenant_id,
        provider="openai",
        model="gpt-5.4-mini",
        api_key="test-key",
        base_url=None,
        enabled=True,
    )
    fake_session = _FakeSession([{}, {"model_id": str(model_id)}, fake_model])

    monkeypatch.setattr(memory_service, "tenant_scoped_session", lambda *a, **k: fake_session)

    config = await memory_service._get_summary_model_config(tenant_id)

    assert config == {
        "provider": "openai",
        "model": "gpt-5.4-mini",
        "api_key": "test-key",
        "base_url": None,
        "max_input_tokens": None,
        "max_output_tokens": None,
    }


@pytest.mark.asyncio
async def test_get_summary_model_config_prefers_main_conversation_model(monkeypatch):
    """P1-1 (docs/compaction-cc-alignment.md): without an explicit summary_model_id the
    summary runs on the CURRENT main conversation model (CC mainLoopModel philosophy)."""
    from app.services import memory_service

    tenant_id = uuid4()
    fake_main_model = SimpleNamespace(
        provider="anthropic",
        model="claude-opus-4-8",
        api_key="main-key",
        base_url=None,
        enabled=True,
        max_input_tokens=1_000_000,
    )
    # execute order: memory_config (empty) → main-model lookup (hit)
    fake_session = _FakeSession([{}, fake_main_model])
    monkeypatch.setattr(memory_service, "tenant_scoped_session", lambda *a, **k: fake_session)

    config = await memory_service._get_summary_model_config(
        tenant_id, main_provider="anthropic", main_model="claude-opus-4-8"
    )

    assert config is not None
    assert config["provider"] == "anthropic"
    assert config["model"] == "claude-opus-4-8"
    assert config["api_key"] == "main-key"
    # window threads through so _llm_summarize budgets input correctly
    assert config["max_input_tokens"] == 1_000_000


@pytest.mark.asyncio
async def test_get_summary_model_config_explicit_choice_beats_main_model(monkeypatch):
    """An operator-configured summary_model_id still wins over the main model."""
    from app.services import memory_service

    tenant_id = uuid4()
    summary_model_id = uuid4()
    fake_summary_model = SimpleNamespace(
        id=summary_model_id,
        tenant_id=tenant_id,
        provider="openai",
        model="gpt-5.4-mini",
        api_key="sum-key",
        base_url=None,
        enabled=True,
    )
    # execute order: memory_config (has summary_model_id) → model-by-id lookup (hit)
    fake_session = _FakeSession([{"summary_model_id": str(summary_model_id)}, fake_summary_model])
    monkeypatch.setattr(memory_service, "tenant_scoped_session", lambda *a, **k: fake_session)

    config = await memory_service._get_summary_model_config(
        tenant_id, main_provider="anthropic", main_model="claude-opus-4-8"
    )

    assert config is not None
    assert config["model"] == "gpt-5.4-mini"
    assert config["api_key"] == "sum-key"


# ── P1-2/P1-3: LLM summary circuit breaker + fallback metric ──
# (docs/compaction-cc-alignment.md §3)


def _breaker_test_messages() -> list[dict]:
    # 15 messages × 200 chars ≈ 857 tokens > trigger (~410 tokens with override=1000)
    return [{"role": "user", "content": f"m{i} " + "a" * 196} for i in range(15)]


@pytest.fixture
def _clean_breaker():
    from app.services import memory_service

    memory_service._summary_breaker.clear()
    yield
    memory_service._summary_breaker.clear()


async def _compress_once(monkeypatch, tenant_id, llm_calls: list, *, fail: bool):
    """Run maybe_compress_messages with a stubbed summary LLM.

    Test Double rationale: summary model resolution (DB) and the LLM call are
    external boundaries; breaker logic under test is pure in-process state.
    """
    import app.services.conversation_summarizer as summarizer_mod
    from app.services import memory_service

    async def fake_get_memory_config(_tenant_id):
        return {}

    async def fake_get_summary_model_config(_tenant_id, **_kwargs):
        return {"provider": "openai", "model": "m", "api_key": "k"}

    async def fake_llm_summarize(_messages, _model_config, **_kwargs):
        llm_calls.append(1)
        if fail:
            raise RuntimeError("llm down")
        return "fine summary"

    monkeypatch.setattr(memory_service, "_get_memory_config", fake_get_memory_config)
    monkeypatch.setattr(memory_service, "_get_summary_model_config", fake_get_summary_model_config)
    monkeypatch.setattr(summarizer_mod, "_llm_summarize", fake_llm_summarize)

    return await memory_service.maybe_compress_messages(
        _breaker_test_messages(),
        "openai",
        "m",
        1000,  # max_input_tokens_override → tiny window, forces compression
        tenant_id,
    )


@pytest.mark.asyncio
async def test_maybe_compress_does_not_use_cumulative_usage_anchor_as_context_pressure(monkeypatch):
    import app.services.conversation_summarizer as summarizer_mod
    from app.services import memory_service

    tenant_id = uuid4()
    llm_calls: list = []
    pre_compaction_calls: list[dict] = []
    messages = [{"role": "user", "content": "中文内容"} for _ in range(15)]

    async def fake_get_memory_config(_tenant_id):
        return {}

    async def fake_get_summary_model_config(_tenant_id, **_kwargs):
        return {"provider": "openai", "model": "m", "api_key": "k"}

    async def fake_llm_summarize(_messages, _model_config, **_kwargs):
        llm_calls.append(1)
        return "usage anchored summary"

    async def before_compaction(facts):
        pre_compaction_calls.append(facts)

    monkeypatch.setattr(memory_service, "_get_memory_config", fake_get_memory_config)
    monkeypatch.setattr(memory_service, "_get_summary_model_config", fake_get_summary_model_config)
    monkeypatch.setattr(memory_service, "estimate_tokens", lambda *_args, **_kwargs: 100)
    monkeypatch.setattr(summarizer_mod, "_llm_summarize", fake_llm_summarize)

    result = await memory_service.maybe_compress_messages(
        messages,
        "openai",
        "m",
        1000,
        tenant_id,
        usage_anchor_tokens=700,
        before_compaction=before_compaction,
    )

    assert llm_calls == []
    assert pre_compaction_calls == []
    assert result == messages


@pytest.mark.asyncio
async def test_summary_breaker_opens_after_consecutive_failures(monkeypatch, caplog, _clean_breaker):
    """3 consecutive LLM failures open the breaker: the 4th compression skips the
    LLM entirely (CC autoCompact breaker philosophy) and the hold path emits a metric."""

    tenant_id = uuid4()
    llm_calls: list = []

    with caplog.at_level("WARNING"):
        for _ in range(3):
            result = await _compress_once(monkeypatch, tenant_id, llm_calls, fail=True)
            assert result[0]["role"] == "system"  # degraded marker still compresses

        assert len(llm_calls) == 3

        result = await _compress_once(monkeypatch, tenant_id, llm_calls, fail=True)

    assert len(llm_calls) == 3  # breaker open — no 4th LLM attempt
    assert result[0]["role"] == "system"
    metrics = [getattr(r, "metric", None) for r in caplog.records]
    assert "compaction_llm_hold" in metrics  # P1-3 degradation metric
    assert "compaction_llm_breaker_open" in metrics


@pytest.mark.asyncio
async def test_compressed_summary_wrapper_aligns_cc(monkeypatch, _clean_breaker):
    """P3-2 (docs/compaction-cc-alignment.md): the post-compaction wrapper carries
    CC's resume-directly directive (auto-compaction is implicit — the user must not
    perceive a break) and a system-injected recovery pointer (never LLM-written)."""
    tenant_id = uuid4()
    llm_calls: list = []

    result = await _compress_once(monkeypatch, tenant_id, llm_calls, fail=False)

    wrapper = result[0]
    assert wrapper["role"] == "system"
    content = wrapper["content"]
    assert content.startswith("[Previous conversation summary]")  # stable marker
    assert "fine summary" in content  # the LLM summary body
    assert "Resume directly" in content
    assert "do not acknowledge the summary" in content
    assert "logs/" in content  # system-injected recovery pointer (CC transcriptPath pattern)


@pytest.mark.asyncio
async def test_summary_breaker_resets_on_success(monkeypatch, _clean_breaker):
    from app.services import memory_service

    tenant_id = uuid4()
    llm_calls: list = []

    for _ in range(2):
        await _compress_once(monkeypatch, tenant_id, llm_calls, fail=True)
    await _compress_once(monkeypatch, tenant_id, llm_calls, fail=False)

    assert tenant_id not in memory_service._summary_breaker  # success wipes the count
    assert len(llm_calls) == 3


@pytest.mark.asyncio
async def test_summary_breaker_half_opens_after_ttl(monkeypatch, _clean_breaker):
    from app.services import memory_service

    tenant_id = uuid4()
    llm_calls: list = []

    for _ in range(3):
        await _compress_once(monkeypatch, tenant_id, llm_calls, fail=True)
    assert len(llm_calls) == 3

    # Advance past the retry TTL → half-open probe allowed
    failures, last_ts = memory_service._summary_breaker[tenant_id]
    memory_service._summary_breaker[tenant_id] = (
        failures,
        last_ts - memory_service._SUMMARY_BREAKER_RETRY_AFTER_SECONDS - 1,
    )

    await _compress_once(monkeypatch, tenant_id, llm_calls, fail=False)

    assert len(llm_calls) == 4  # probe went through
    assert tenant_id not in memory_service._summary_breaker


@pytest.mark.asyncio
async def test_get_rerank_model_config_falls_back_to_tenant_default_model(monkeypatch):
    from app.services import memory_service

    tenant_id = uuid4()
    model_id = uuid4()
    fake_model = SimpleNamespace(
        id=model_id,
        tenant_id=tenant_id,
        provider="openai",
        model="gpt-5.4-mini",
        api_key="test-key",
        base_url=None,
        enabled=True,
    )
    fake_session = _FakeSession([{}, {"model_id": str(model_id)}, fake_model])

    monkeypatch.setattr(memory_service, "tenant_scoped_session", lambda *a, **k: fake_session)

    config = await memory_service._get_rerank_model_config(tenant_id)

    # The window hint stays in the dict; create_llm_client_from_config filters
    # non-client keys at the only consumption point (post-incident contract).
    assert config == {
        "provider": "openai",
        "model": "gpt-5.4-mini",
        "api_key": "test-key",
        "base_url": None,
        "max_input_tokens": None,
        "max_output_tokens": None,
    }


@pytest.mark.asyncio
async def test_persist_runtime_memory_persists_summary_without_direct_semantic_write(monkeypatch):
    from app.services.memory_service import persist_runtime_memory

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = str(uuid4())
    chat_session = SimpleNamespace(summary=None)
    fake_session = _FakeSession([chat_session])
    update_called = False

    async def fake_generate_session_summary(messages, _tenant_id, **_kwargs):
        assert len(messages) == 2
        return "rolled summary"

    async def fake_update_agent_memory(*_args, **_kwargs):
        nonlocal update_called
        update_called = True

    async def fake_get_memory_config(_tenant_id):
        return {}

    monkeypatch.setattr("app.services.memory_service.tenant_scoped_session", lambda *a, **k: fake_session)
    monkeypatch.setattr("app.services.memory_service._generate_session_summary", fake_generate_session_summary)
    monkeypatch.setattr("app.services.memory_service._update_agent_memory", fake_update_agent_memory, raising=False)
    monkeypatch.setattr("app.services.memory_service._get_memory_config", fake_get_memory_config)

    await persist_runtime_memory(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        messages=[
            {"role": "user", "content": "我更喜欢咖啡而不是茶，请记住。"},
            {"role": "assistant", "content": "已记录你的偏好。"},
        ],
    )

    assert chat_session.summary == "rolled summary"
    assert fake_session.commits == 1
    assert update_called is False


@pytest.mark.asyncio
async def test_persist_runtime_memory_strips_null_bytes_from_summary(monkeypatch):
    from app.services.memory_service import persist_runtime_memory

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = str(uuid4())
    chat_session = SimpleNamespace(summary=None)
    fake_session = _FakeSession([chat_session])

    async def fake_generate_session_summary(messages, _tenant_id, **_kwargs):
        assert len(messages) == 2
        return "safe\x00summary"

    async def fake_get_memory_config(_tenant_id):
        return {}

    monkeypatch.setattr("app.services.memory_service.tenant_scoped_session", lambda *a, **k: fake_session)
    monkeypatch.setattr("app.services.memory_service._generate_session_summary", fake_generate_session_summary)
    monkeypatch.setattr("app.services.memory_service._get_memory_config", fake_get_memory_config)

    await persist_runtime_memory(
        agent_id=agent_id,
        session_id=session_id,
        tenant_id=tenant_id,
        messages=[
            {"role": "user", "content": "contains extracted binary text"},
            {"role": "assistant", "content": "summary ready"},
        ],
    )

    assert chat_session.summary == "safesummary"
    assert fake_session.commits == 1


# ``_extract_summary`` re-export removed with the dead mechanical fallback (B-6);
# the LLM-holds-without-fallback behavior is covered by the test below.


@pytest.mark.asyncio
async def test_generate_session_summary_holds_without_llm_instead_of_mechanical_fallback(monkeypatch):
    from app.services import memory_service

    async def no_summary_model(_tenant_id, **_kwargs):
        return None

    monkeypatch.setattr(memory_service, "_get_summary_model_config", no_summary_model)

    summary = await memory_service._generate_session_summary(
        [
            {"role": "user", "content": "记住我喜欢 npm"},
            {"role": "assistant", "content": "收到"},
        ],
        uuid4(),
    )

    assert summary is None


@pytest.mark.asyncio
async def test_build_memory_context_requests_model_owned_semantic_selector(monkeypatch, tmp_path) -> None:
    """The live read path resolves a model; scores alone never select memory."""
    from app.services import memory_service

    called = {"rerank": False}

    async def spy(_tenant_id):
        called["rerank"] = True
        return None

    monkeypatch.setattr(memory_service, "_get_rerank_model_config", spy, raising=False)
    monkeypatch.setattr(
        memory_service,
        "get_settings",
        lambda: type("S", (), {"AGENT_DATA_DIR": str(tmp_path), "MEMORY_RESIDENT_BUDGET_CHARS": 12000.0})(),
    )
    from app.memory.activation import ActivationContext

    class _P:
        def can_access_sensitivity(self, _s):
            return True

    async def fake_activation(**_kw):
        return ActivationContext(query="q", principal_stack=_P())

    monkeypatch.setattr(memory_service, "_resolve_activation_context", lambda **kw: fake_activation(**kw))
    await memory_service.build_memory_context(uuid.uuid4(), uuid.uuid4(), query="q")

    assert called["rerank"] is True


@pytest.mark.asyncio
async def test_build_memory_context_fails_closed_when_activation_context_unresolved(monkeypatch):
    from app.services import memory_service

    agent_id = uuid4()
    tenant_id = uuid4()

    class _FakeRetriever:
        async def retrieve(self, *_args, **_kwargs):
            return ["PL3_SECRET_MEMORY"]

    class _FakeAssembler:
        def assemble(self, items, **_kwargs):
            return "\n".join(str(item) for item in items)

    async def fake_resolve_activation_context(*_args, **_kwargs):
        return None

    monkeypatch.setattr(memory_service, "MemoryRetriever", lambda **_kwargs: _FakeRetriever())
    monkeypatch.setattr(memory_service, "MemoryAssembler", lambda: _FakeAssembler())
    monkeypatch.setattr(memory_service, "_get_rerank_model_config", lambda _tenant_id: None, raising=False)
    monkeypatch.setattr(memory_service, "_resolve_activation_context", fake_resolve_activation_context)

    context = await memory_service.build_memory_context(
        agent_id,
        tenant_id,
        session_id="session-1",
        query="salary planning",
        current_user_id=uuid4(),
        current_user_name="Viewer",
    )

    assert context == ""


@pytest.mark.asyncio
async def test_build_memory_context_passes_activation_context(monkeypatch):
    from app.services import memory_service
    from app.services.agency_charter import build_default_accountability_context

    agent_id = uuid4()
    tenant_id = uuid4()
    owner_id = uuid4()
    viewer_id = uuid4()
    captured = {}

    class _FakeRetriever:
        async def retrieve(
            self,
            _agent_id,
            _query,
            _session_id,
            _tenant_id,
            *,
            rerank_model_config=None,
            limit=20,
            activation_context=None,
        ):
            del rerank_model_config, limit
            captured["activation_context"] = activation_context
            return ["memory-item"]

    class _FakeAssembler:
        def assemble(self, items):
            return "ASSEMBLED"

    async def fake_resolve_accountability_context(*_args, **_kwargs):
        return build_default_accountability_context(
            company_id=str(tenant_id),
            company_name="Acme",
            owner_id=str(owner_id),
            owner_name="Alice",
            current_user_id=str(viewer_id),
            current_user_name="Bob",
        )

    monkeypatch.setattr(memory_service, "MemoryRetriever", lambda **_kwargs: _FakeRetriever())
    monkeypatch.setattr(memory_service, "MemoryAssembler", lambda: _FakeAssembler())
    monkeypatch.setattr(memory_service, "_get_rerank_model_config", lambda _tenant_id: None, raising=False)
    monkeypatch.setattr(memory_service, "_resolve_accountability_context", fake_resolve_accountability_context)

    context = await memory_service.build_memory_context(
        agent_id,
        tenant_id,
        session_id="session-1",
        query="Q3 salary planning for Acme",
        current_user_id=viewer_id,
        current_user_name="Bob",
    )

    activation_context = captured["activation_context"]
    assert context == "ASSEMBLED"
    assert activation_context.query == "Q3 salary planning for Acme"
    assert activation_context.principal_stack.direct_owner.id == str(owner_id)
    assert activation_context.principal_stack.current_user.id == str(viewer_id)
    assert "alice" in activation_context.owner_terms
    assert "acme" in activation_context.company_terms


@pytest.mark.asyncio
async def test_build_memory_context_omits_pl3_for_non_owner(monkeypatch, tmp_path):
    from app.memory.md_store import ensure_t3_layout
    from app.services import memory_service
    from app.services.agency_charter import build_default_accountability_context

    agent_id = uuid4()
    tenant_id = uuid4()
    owner_id = uuid4()
    viewer_id = uuid4()
    mem_dir = ensure_t3_layout(tmp_path, agent_id)
    overlay = mem_dir / "explicit"
    (overlay / "entries").mkdir(parents=True, exist_ok=True)
    import json as _json

    (overlay / "manifest.jsonl").write_text(
        _json.dumps(
            {"id": "salary-private", "status": "active", "category": "constraint", "sensitivity": "PL3_sensitive"}
        )
        + "\n",
        encoding="utf-8",
    )
    (overlay / "entries" / "salary-private.md").write_text(
        "<normalized_memory>Q3 salary planning requires owner-only handling</normalized_memory>",
        encoding="utf-8",
    )
    with (overlay / "manifest.jsonl").open("a", encoding="utf-8") as _handle:
        _handle.write(
            _json.dumps(
                {"id": "salary-public", "status": "active", "category": "constraint", "sensitivity": "PL1_public"}
            )
            + "\n"
        )
    (overlay / "entries" / "salary-public.md").write_text(
        "<normalized_memory>Acme salary planning policy uses the approved budget template</normalized_memory>",
        encoding="utf-8",
    )

    async def fake_resolve_accountability_context(*_args, **_kwargs):
        return build_default_accountability_context(
            company_id=str(tenant_id),
            company_name="Acme",
            owner_id=str(owner_id),
            owner_name="Alice",
            current_user_id=str(viewer_id),
            current_user_name="Bob",
        )

    monkeypatch.setattr(memory_service, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(memory_service, "_get_rerank_model_config", lambda _tenant_id: None, raising=False)
    monkeypatch.setattr(memory_service, "_resolve_accountability_context", fake_resolve_accountability_context)

    context = await memory_service.build_memory_context(
        agent_id,
        tenant_id,
        query="salary planning",
        current_user_id=viewer_id,
        current_user_name="Bob",
    )

    assert "owner-only handling" not in context
    assert "approved budget template" in context


@pytest.mark.asyncio
async def test_build_memory_context_uses_adaptive_budget_profile(monkeypatch, tmp_path):
    from app.services import memory_service

    agent_id = uuid4()
    tenant_id = uuid4()
    captured = {}

    class _FakeRetriever:
        async def retrieve(
            self,
            _agent_id,
            _query,
            _session_id,
            _tenant_id,
            *,
            rerank_model_config=None,
            limit=20,
            retrieval_profile=None,
        ):
            captured["rerank_model_config"] = rerank_model_config
            captured["limit"] = limit
            captured["retrieval_profile"] = retrieval_profile
            return ["memory-item"]

    class _FakeAssembler:
        def assemble(self, items, budget_chars=20000):
            captured["assembled_items"] = items
            captured["budget_chars"] = budget_chars
            return "ASSEMBLED"

    monkeypatch.setattr(
        memory_service,
        "MemoryRetriever",
        lambda **_kwargs: _FakeRetriever(),
    )
    monkeypatch.setattr(
        memory_service,
        "MemoryAssembler",
        lambda: _FakeAssembler(),
    )
    monkeypatch.setattr(
        memory_service,
        "_get_rerank_model_config",
        lambda _tenant_id: {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "api_key": "test-key",
            "base_url": None,
        },
        raising=False,
    )

    async def fake_resolve_activation_context(*_args, **_kwargs):
        return object()

    monkeypatch.setattr(memory_service, "_resolve_activation_context", fake_resolve_activation_context)

    context = await memory_service.build_memory_context(
        agent_id,
        tenant_id,
        session_id="session-1",
        query="请研究最近的路线图变化并整理来源",
        context_window_tokens=256000,
    )

    assert context == "ASSEMBLED"
    assert captured["assembled_items"] == ["memory-item"]
    assert captured["budget_chars"] >= 24000
    assert captured["retrieval_profile"].semantic_limit >= 12
    assert captured["retrieval_profile"].rerank_max_select >= 8


@pytest.mark.asyncio
async def test_build_memory_context_exhausts_only_session_auto_surface_budget(monkeypatch, tmp_path) -> None:
    from app.memory.activation import ActivationContext
    from app.memory.session_surfacing import (
        SESSION_AUTO_SURFACE_BUDGET_BYTES,
        SESSION_AUTO_SURFACE_MIN_USEFUL_BYTES,
    )
    from app.memory.types import MemoryItem, MemoryKind
    from app.services import memory_service
    from app.services.principal_context import PrincipalStack

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = str(uuid4())

    class _Retriever:
        last_selection_status = "model_selected"

        async def retrieve(self, *_args, **_kwargs):
            return [
                MemoryItem(
                    kind=MemoryKind.SEMANTIC,
                    content="记忆正文" * 5000,
                    source="explicit",
                    metadata={"entry_id": "mem-large", "source_type": "explicit_overlay"},
                )
            ]

    async def activation(**kwargs):
        return ActivationContext(query=kwargs["query"], principal_stack=PrincipalStack())

    monkeypatch.setattr(
        memory_service,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path), MEMORY_RESIDENT_BUDGET_CHARS=25_000),
    )
    monkeypatch.setattr(memory_service, "MemoryRetriever", lambda **_kwargs: _Retriever())
    monkeypatch.setattr(memory_service, "_resolve_activation_context", activation)
    monkeypatch.setattr(
        memory_service,
        "_get_rerank_model_config",
        lambda _tenant_id: {"provider": "fake", "model": "fake"},
    )

    results = []
    for index in range(20):
        result = await memory_service.build_memory_context(
            agent_id,
            tenant_id,
            session_id=session_id,
            turn_id=f"turn-{index}",
            query="继续当前任务",
            return_result=True,
        )
        results.append(result)
        if result.code == "memory_auto_surface_budget_exhausted":
            break

    final = results[-1]
    assert len(results) > 1
    assert final.auto_surface_total_bytes <= SESSION_AUTO_SURFACE_BUDGET_BYTES
    assert final.auto_surface_total_bytes + final.auto_surface_remaining_bytes == SESSION_AUTO_SURFACE_BUDGET_BYTES
    assert final.auto_surface_remaining_bytes < SESSION_AUTO_SURFACE_MIN_USEFUL_BYTES
    assert final.code == "memory_auto_surface_budget_exhausted"
    assert final.status == "degraded"
    assert final.conversation_available is True
    assert final.external_effects_available is True
    assert "search_memory" in final.user_message
    assert "load_memory" in final.user_message
    assert all(len(result.content.encode("utf-8")) <= 4096 for result in results)


@pytest.mark.asyncio
async def test_missing_memory_selector_is_ref_only_typed_degradation(monkeypatch, tmp_path) -> None:
    from app.memory.activation import ActivationContext
    from app.services import memory_service
    from app.services.principal_context import PrincipalStack

    class _Retriever:
        last_selection_status = "model_unavailable"
        last_selection_receipt = "memory/control/selection_receipts/receipt.json"

        async def retrieve(self, *_args, **_kwargs):
            return []

    async def activation(**kwargs):
        return ActivationContext(query=kwargs["query"], principal_stack=PrincipalStack())

    monkeypatch.setattr(
        memory_service,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path), MEMORY_RESIDENT_BUDGET_CHARS=25_000),
    )
    monkeypatch.setattr(memory_service, "MemoryRetriever", lambda **_kwargs: _Retriever())
    monkeypatch.setattr(memory_service, "_resolve_activation_context", activation)
    monkeypatch.setattr(memory_service, "_get_rerank_model_config", lambda _tenant_id: None)

    result = await memory_service.build_memory_context(
        uuid4(),
        uuid4(),
        session_id=str(uuid4()),
        turn_id="turn-selector-unavailable",
        query="use relevant memory",
        return_result=True,
    )

    assert result.content == ""
    assert result.status == "degraded"
    assert result.code == "memory_semantic_selection_unavailable"
    assert result.retryable is True
    assert result.conversation_available is True
    assert result.external_effects_available is True
    assert "search_memory" in result.user_message
    assert "load_memory" in result.user_message


@pytest.mark.asyncio
async def test_build_memory_context_evolves_session_working_set(monkeypatch, tmp_path):
    """M4 wiring: each turn loads W_t into the activation context and advances
    it with the refs actually activated — the working set must not be an
    orphan mechanism."""
    from app.memory.activation import ActivationContext
    from app.memory.session_working_set import load_working_set
    from app.services import memory_service
    from app.services.principal_context import PrincipalStack

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = "11111111-2222-4333-8444-555555555555"

    memory_dir = tmp_path / str(agent_id) / "memory" / "knowledge"
    memory_dir.mkdir(parents=True)
    (memory_dir / "railway-deployment.md").write_text(
        "---\ntitle: Railway Deployment\nstatus: active\n---\n\nRailway production deploy notes.",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        memory_service,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path), MEMORY_RESIDENT_BUDGET_CHARS=24_000),
    )

    seen_working_sets: list[tuple] = []

    async def fake_resolver(**kwargs):
        return ActivationContext(query=kwargs.get("query", ""), principal_stack=PrincipalStack())

    monkeypatch.setattr(memory_service, "_resolve_activation_context", fake_resolver)
    monkeypatch.setattr(
        memory_service,
        "_get_rerank_model_config",
        lambda _tenant_id: {"provider": "fake", "model": "fake"},
    )

    import functools

    from app.memory.retriever import MemoryRetriever

    original_retrieve = MemoryRetriever.retrieve

    async def select_railway(self, *, items, **_kwargs):
        selected = next(item for item in items if "Railway Deployment" in item.content)
        return [selected.metadata["selection_candidate_id"]], "railway page answers the query"

    @functools.wraps(original_retrieve)
    async def spying_retrieve(self, *args, **kwargs):
        context = kwargs.get("activation_context")
        seen_working_sets.append(context.working_set if context else None)
        return await original_retrieve(self, *args, **kwargs)

    monkeypatch.setattr(MemoryRetriever, "retrieve", spying_retrieve)
    monkeypatch.setattr(MemoryRetriever, "_select_with_model", select_railway)

    first = await memory_service.build_memory_context(
        agent_id, tenant_id, session_id=session_id, query="railway production deploy"
    )
    assert "Railway" in first

    state = load_working_set(tmp_path, agent_id, session_id)
    assert state.turn_index == 1
    assert any(item["ref"] == "knowledge/railway-deployment" for item in state.items)

    await memory_service.build_memory_context(
        agent_id, tenant_id, session_id=session_id, query="railway production deploy"
    )

    assert seen_working_sets[0] == ()
    assert ("knowledge/railway-deployment", 1.0) in (seen_working_sets[1] or ())
    assert load_working_set(tmp_path, agent_id, session_id).turn_index == 2


@pytest.mark.asyncio
async def test_activation_context_carries_active_goal_terms(monkeypatch, tmp_path):
    """M6 TaskModulation (deterministic tier): the active session goal's
    objective must reach ActivationContext.goal_terms — until now the field
    existed but was never populated, so goal_relevance was a misnomer for
    plain query overlap."""
    from app.memory.activation import ActivationContext
    from app.services import memory_service
    from app.services.principal_context import PrincipalStack

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = str(uuid4())

    monkeypatch.setattr(
        memory_service,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path), MEMORY_RESIDENT_BUDGET_CHARS=24_000),
    )

    async def fake_resolver(**kwargs):
        base = ActivationContext(query=kwargs.get("query", ""), principal_stack=PrincipalStack())
        return await memory_service._attach_goal_terms(
            base, agent_id=kwargs["agent_id"], tenant_id=kwargs["tenant_id"], session_id=session_id
        )

    async def fake_goal_objective(**_kwargs):
        return "ship the railway deployment overhaul milestone"

    monkeypatch.setattr(memory_service, "_load_active_goal_objective", fake_goal_objective)

    captured: dict = {}

    class _GoalFakeRetriever:
        async def retrieve(self, _agent_id, _query, _session_id, _tenant_id, *, activation_context=None, **_kwargs):
            captured["context"] = activation_context
            return []

    monkeypatch.setattr(memory_service, "MemoryRetriever", lambda **_kwargs: _GoalFakeRetriever())
    monkeypatch.setattr(memory_service, "_resolve_activation_context", fake_resolver)

    await memory_service.build_memory_context(agent_id, tenant_id, session_id=session_id, query="status update")

    context = captured["context"]
    assert context is not None
    assert "railway" in context.goal_terms
    assert "deployment" in context.goal_terms
    assert "the" not in context.goal_terms, "stopword-ish single-letter/article noise should be filtered"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [TimeoutError("vector backend timed out with secret details"), ValueError("corrupt index with secret details")],
)
async def test_semantic_retrieval_failure_retries_then_keeps_resident_identity(
    monkeypatch, tmp_path, failure: Exception
) -> None:
    from app.memory.activation import ActivationContext
    from app.services import memory_service
    from app.services.principal_context import PrincipalStack

    agent_id = uuid4()
    tenant_id = uuid4()
    owner_path = tmp_path / str(agent_id) / "memory" / "profiles" / "owner.md"
    owner_path.parent.mkdir(parents=True)
    owner_path.write_text("Owner requires evidence-first delivery.", encoding="utf-8")
    calls = 0

    class _BrokenRetriever:
        async def retrieve(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise failure

    async def activation(**kwargs):
        return ActivationContext(query=kwargs["query"], principal_stack=PrincipalStack())

    monkeypatch.setattr(
        memory_service,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path), MEMORY_RESIDENT_BUDGET_CHARS=12_000),
    )
    monkeypatch.setattr(memory_service, "MemoryRetriever", lambda **_kwargs: _BrokenRetriever())
    monkeypatch.setattr(memory_service, "_resolve_activation_context", activation)

    result = await memory_service.build_memory_context(
        agent_id,
        tenant_id,
        session_id=str(uuid4()),
        query="latest decision",
        return_result=True,
    )

    assert result.status == "degraded"
    assert result.code == "semantic_retrieval_unavailable"
    assert result.retryable is True
    assert result.attempts == 2
    assert calls == 2
    assert "evidence-first" in result.content
    assert "secret details" not in result.user_message


@pytest.mark.asyncio
async def test_corrupt_working_set_is_explicit_auxiliary_degradation(monkeypatch, tmp_path) -> None:
    from app.memory.activation import ActivationContext
    from app.services import memory_service
    from app.services.principal_context import PrincipalStack

    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = str(uuid4())
    state_path = tmp_path / str(agent_id) / "memory" / "control" / "working_sets" / f"{session_id}.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{broken", encoding="utf-8")

    class _Retriever:
        async def retrieve(self, *_args, **_kwargs):
            return []

    class _Assembler:
        def assemble(self, _items, **_kwargs):
            return "SEMANTIC_MEMORY"

    async def activation(**kwargs):
        return ActivationContext(query=kwargs["query"], principal_stack=PrincipalStack())

    monkeypatch.setattr(
        memory_service,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path), MEMORY_RESIDENT_BUDGET_CHARS=12_000),
    )
    monkeypatch.setattr(memory_service, "MemoryRetriever", lambda **_kwargs: _Retriever())
    monkeypatch.setattr(memory_service, "MemoryAssembler", lambda: _Assembler())
    monkeypatch.setattr(memory_service, "_resolve_activation_context", activation)

    result = await memory_service.build_memory_context(
        agent_id,
        tenant_id,
        session_id=session_id,
        query="hello",
        return_result=True,
    )

    assert result.status == "degraded"
    assert result.code == "memory_auxiliary_degraded"
    assert "working_set" in result.degraded_components
    assert result.content == "SEMANTIC_MEMORY"


@pytest.mark.asyncio
async def test_resident_profile_failure_is_critical_not_empty_context(monkeypatch, tmp_path) -> None:
    from app.memory.activation import ActivationContext
    from app.services import memory_service
    from app.services.principal_context import PrincipalStack

    async def activation(**kwargs):
        return ActivationContext(query=kwargs["query"], principal_stack=PrincipalStack())

    def broken_resident(**_kwargs):
        raise OSError("owner profile cannot be read")

    monkeypatch.setattr(
        memory_service,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path), MEMORY_RESIDENT_BUDGET_CHARS=12_000),
    )
    monkeypatch.setattr(memory_service, "_resolve_activation_context", activation)
    monkeypatch.setattr("app.memory.profile_plane.load_resident_memory", broken_resident)

    result = await memory_service.build_memory_context(
        uuid4(),
        uuid4(),
        query="hello",
        return_result=True,
    )

    assert result.status == "unavailable"
    assert result.code == "resident_profile_unavailable"
    assert result.conversation_available is True
    assert result.authority_context_available is False
    assert result.durable_write_available is False
    assert result.external_effects_available is False
    assert result.content == ""


@pytest.mark.asyncio
async def test_unresolved_memory_authority_is_typed_fail_closed(monkeypatch, tmp_path) -> None:
    from app.services import memory_service

    async def unresolved(**_kwargs):
        return None

    monkeypatch.setattr(
        memory_service,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path), MEMORY_RESIDENT_BUDGET_CHARS=12_000),
    )
    monkeypatch.setattr(memory_service, "_resolve_activation_context", unresolved)

    result = await memory_service.build_memory_context(
        uuid4(),
        uuid4(),
        query="hello",
        return_result=True,
    )

    assert result.status == "blocked_authority"
    assert result.code == "memory_authority_unresolved"
    assert result.conversation_available is True
    assert result.authority_context_available is False
    assert result.durable_write_available is False
    assert result.external_effects_available is False

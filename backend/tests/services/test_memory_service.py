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


async def _compress_once(monkeypatch, tenant_id, llm_calls: list, *, fail: bool, messages=None, summary_inputs=None):
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
        if summary_inputs is not None:
            summary_inputs.append(_messages)
        if fail:
            raise RuntimeError("llm down")
        return "fine summary"

    monkeypatch.setattr(memory_service, "_get_memory_config", fake_get_memory_config)
    monkeypatch.setattr(memory_service, "_get_summary_model_config", fake_get_summary_model_config)
    monkeypatch.setattr(summarizer_mod, "_llm_summarize", fake_llm_summarize)

    return await memory_service.maybe_compress_messages(
        _breaker_test_messages() if messages is None else messages,
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
@pytest.mark.parametrize("stopped", [True, False])
async def test_auto_compaction_returns_context_without_reopening_stopped_work(monkeypatch, _clean_breaker, stopped):
    """Auto-compaction returns to its caller without directing stopped work to resume."""
    tenant_id = uuid4()
    llm_calls: list = []

    messages = _breaker_test_messages()
    stop = {
        "role": "user",
        "content": "[Request interrupted by user; its plan and todos are stopped. Resume only if asked.]",
        "source_ref": f"session-run-cancelled:{uuid4()}",
    }
    if stopped:
        messages.insert(1, stop)
    result = await _compress_once(monkeypatch, tenant_id, llm_calls, fail=False, messages=messages)
    assert result[-10:] == messages[-10:]
    if stopped:
        # Round 3: compaction no longer pins the stop line; the summary carries it.
        assert stop not in result

    wrapper = result[0]
    assert wrapper["role"] == "system"
    content = wrapper["content"]
    assert content.startswith("[Previous conversation summary]")  # stable marker
    assert "fine summary" in content  # the LLM summary body
    assert (
        "Continue the current request directly; do not acknowledge or recap this summary, "
        "and do not ask the user about it."
    ) in content
    assert "A request the user interrupted stays stopped unless the user asks to resume it." in content
    assert "Resume directly" not in content
    assert "Pick up the last task" not in content
    assert "Continue the conversation from where it left off" not in content
    assert "memory/t0/sessions/<session_id>/segments/<segment_id>/source.md" in content
    assert "logs/" not in content  # Native session readers cannot authorize legacy log names.


@pytest.mark.asyncio
async def test_mid_run_compaction_summarizes_later_request_without_pinning_earlier_stop(monkeypatch, _clean_breaker):
    stop = {
        "role": "user",
        "content": "[Request interrupted by user; its plan and todos are stopped. Resume only if asked.]",
        "source_ref": f"session-run-cancelled:{uuid4()}",
    }
    messages = [
        {"role": "user", "content": "A " + "a" * 200},
        {"role": "assistant", "content": "A partial " + "a" * 200},
        stop,
        {"role": "user", "content": "C (current request) " + "c" * 200},
        *[{"role": "assistant", "content": f"C step {i} " + "c" * 200} for i in range(12)],
    ]
    summarized = []
    result = await _compress_once(monkeypatch, uuid4(), [], fail=False, messages=messages, summary_inputs=summarized)
    assert summarized == [messages[:6]]  # A, partial, STOP, C, C steps 0-1.
    # Round 3: compaction no longer pins the stop line; the summary carries it.
    assert result[1:] == messages[6:]
    assert stop not in result


def test_summary_prompt_records_interruption_without_reopening_stopped_tasks():
    from app.services.conversation_summarizer import _SUMMARIZE_SYSTEM_PROMPT

    rule = (
        'A user message that starts with "[Request interrupted by user" stops the request before it: '
        'record that request under Primary Request and Intent as stopped by the user, and keep it and its plan '
        'and todos out of Pending Tasks, Current Work and Next Step unless a later user message asks to resume it.'
    )
    summary_format = _SUMMARIZE_SYSTEM_PROMPT.split("<summary_format>", 1)[1].split("</summary_format>", 1)[0]
    assert summary_format.splitlines()[-1] == rule
    assert summary_format.splitlines()[-2].startswith("**Next Step:**")


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


@pytest.mark.asyncio
async def test_persist_runtime_memory_bounds_terminal_critical_summary_transport(monkeypatch):
    from app.services.memory_service import persist_runtime_memory

    captured: dict = {}

    async def fake_generate_session_summary(_messages, _tenant_id, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr("app.services.memory_service._generate_session_summary", fake_generate_session_summary)

    await persist_runtime_memory(
        agent_id=uuid4(),
        session_id=str(uuid4()),
        tenant_id=uuid4(),
        messages=[
            {"role": "user", "content": "finish the accepted turn"},
            {"role": "assistant", "content": "durable answer already committed"},
        ],
    )

    assert captured["http_max_attempts"] == 1


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
async def test_build_memory_context_makes_no_model_call(monkeypatch, tmp_path) -> None:
    """Resident memory + explicit index build with zero model interaction."""
    from app.services import memory_service
    from tests.memory.authority_fixtures import reference_explicit_memory_authority

    reference_explicit_memory_authority(monkeypatch)

    def _forbidden(_config):
        raise AssertionError("memory context assembly must not call a model")

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", _forbidden)
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
    result = await memory_service.build_memory_context(uuid.uuid4(), uuid.uuid4(), query="q",
        current_user_id=uuid4(), return_result=True)

    assert result.status == "ready"
    assert result.code == "memory_context_ready"


@pytest.mark.asyncio
async def test_build_memory_context_fails_closed_when_activation_context_unresolved(monkeypatch):
    from app.services import memory_service

    agent_id = uuid4()
    tenant_id = uuid4()

    async def fake_resolve_activation_context(*_args, **_kwargs):
        return None

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
async def test_build_memory_context_passes_activation_context(monkeypatch, tmp_path):
    """The accountability resolution still gates and labels the memory turn."""
    from app.services import memory_service
    from app.services.agency_charter import build_default_accountability_context

    agent_id = uuid4()
    tenant_id = uuid4()
    owner_id = uuid4()
    viewer_id = uuid4()
    captured = {}

    self_md = tmp_path / str(agent_id) / "memory/self"
    self_md.mkdir(parents=True)
    (self_md / "self.md").write_text("# Self\n\nOwner-labelled resident identity.\n", encoding="utf-8")

    async def fake_resolve_accountability_context(*_args, **_kwargs):
        captured["called"] = True
        return build_default_accountability_context(
            company_id=str(tenant_id),
            company_name="Acme",
            owner_id=str(owner_id),
            owner_name="Alice",
            current_user_id=str(viewer_id),
            current_user_name="Bob",
        )

    monkeypatch.setattr(memory_service, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(memory_service, "_resolve_accountability_context", fake_resolve_accountability_context)

    context = await memory_service.build_memory_context(
        agent_id,
        tenant_id,
        session_id="session-1",
        query="Q3 salary planning for Acme",
        current_user_id=viewer_id,
        current_user_name="Bob",
    )

    assert captured["called"] is True
    assert "Owner-labelled resident identity." in context


@pytest.mark.asyncio
async def test_build_memory_context_omits_pl3_for_non_owner(monkeypatch, tmp_path):
    from app.memory.md_store import ensure_t3_layout
    from app.services import memory_service
    from app.services.agency_charter import build_default_accountability_context
    from tests.memory.authority_fixtures import reference_explicit_memory_authority

    reference_explicit_memory_authority(monkeypatch)

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

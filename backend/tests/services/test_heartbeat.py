from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: self._value or [])


class _FakeSession:
    """Fake async DB session that returns pre-configured values for sequential queries."""

    def __init__(self, execute_values):
        self._execute_values = list(execute_values)
        self.added = []
        self._flush_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        if not self._execute_values:
            return _FakeScalarResult(None)
        return _FakeScalarResult(self._execute_values.pop(0))

    def add(self, obj):
        self.added.append(obj)
        # Give ChatSession a fake id on add
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid4()

    async def flush(self):
        self._flush_count += 1

    async def commit(self):
        return None


# ─── _parse_heartbeat_outcome ───────────────────────────────────


def test_parse_heartbeat_outcome_structured_tags():
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome(
        "I updated focus.md with new priorities.\n\n[OUTCOME:action_taken] [SCORE:7]"
    )
    assert outcome == "action_taken"
    assert score == 7


def test_parse_heartbeat_outcome_accepts_curated_alias():
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome("Curated 3 learnings into feedback.md.\n\n[OUTCOME:curated] [SCORE:7]")
    assert outcome == "action_taken"
    assert score == 7


def test_compose_heartbeat_instruction_adds_strategy_boundary() -> None:
    from app.services.heartbeat import _compose_heartbeat_instruction

    text = _compose_heartbeat_instruction("Base heartbeat")

    assert "Base heartbeat" in text
    assert "evolution/lineage.md stores policy-level learning" in text
    assert "Do NOT turn lineage into a raw task transcript" in text


def test_compact_heartbeat_runtime_messages_trims_single_oversized_message() -> None:
    from app.services.heartbeat import (
        _HEARTBEAT_MESSAGE_MAX_CHARS,
        _compact_heartbeat_runtime_messages,
    )

    huge_payload = "BEGIN-" + ("x" * (_HEARTBEAT_MESSAGE_MAX_CHARS * 3)) + "-END"

    compacted = _compact_heartbeat_runtime_messages([{"role": "user", "content": huge_payload}])

    assert len(compacted) == 1
    content = compacted[0]["content"]
    assert len(content) <= _HEARTBEAT_MESSAGE_MAX_CHARS
    assert "truncated to fit heartbeat context budget" in content
    assert content.startswith("BEGIN-")
    assert content.endswith("-END")


def test_compact_heartbeat_runtime_messages_summarizes_middle_history() -> None:
    from app.services.heartbeat import (
        _HEARTBEAT_CONTEXT_MAX_CHARS,
        _compact_heartbeat_runtime_messages,
    )

    messages = [{"role": "user", "content": "base heartbeat instruction"}]
    messages.extend(
        {"role": "assistant" if idx % 2 else "user", "content": f"old-{idx}-" + ("x" * 7000)} for idx in range(24)
    )
    messages.append({"role": "user", "content": "latest tick payload"})

    compacted = _compact_heartbeat_runtime_messages(messages)
    total_chars = sum(len(msg.get("content") or "") for msg in compacted)

    assert total_chars <= _HEARTBEAT_CONTEXT_MAX_CHARS
    assert compacted[0]["content"].startswith("base heartbeat instruction")
    assert any("Heartbeat context compacted" in (msg.get("content") or "") for msg in compacted)
    assert compacted[-1]["content"] == "latest tick payload"


def test_parse_heartbeat_outcome_noop():
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome("Nothing to do right now. HEARTBEAT_OK\n[OUTCOME:noop] [SCORE:0]")
    assert outcome == "noop"
    assert score == 0


def test_parse_heartbeat_outcome_failure():
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome("Attempted to search but got rate limited.\n[OUTCOME:failure] [SCORE:2]")
    assert outcome == "failure"
    assert score == 2


def test_parse_heartbeat_outcome_fallback_heartbeat_ok():
    """When no structured tags, falls back to HEARTBEAT_OK detection."""
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome("HEARTBEAT_OK")
    assert outcome == "noop"
    assert score == 0


def test_parse_heartbeat_outcome_fallback_action():
    """When no tags and no HEARTBEAT_OK, assume action_taken."""
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome("I successfully fixed the error in ERRORS.md")
    assert outcome == "action_taken"
    assert score == 5


def test_parse_heartbeat_outcome_none_reply():
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome(None)
    assert outcome == "noop"
    assert score is None


def test_format_heartbeat_exception_includes_type_when_message_is_empty():
    import asyncio

    from app.services.heartbeat import _format_heartbeat_exception

    assert _format_heartbeat_exception(asyncio.TimeoutError()) == "TimeoutError"


def test_format_heartbeat_exception_includes_type_and_message():
    from app.services.heartbeat import _format_heartbeat_exception

    assert _format_heartbeat_exception(ValueError("bad state")) == "ValueError: bad state"


def test_parse_heartbeat_outcome_score_capped_at_10():
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome("[OUTCOME:action_taken] [SCORE:99]")
    assert outcome == "action_taken"
    assert score == 10


def test_parse_heartbeat_outcome_case_insensitive():
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, score = _parse_heartbeat_outcome("[outcome:FAILURE] [SCORE:1]")
    assert outcome == "failure"
    assert score == 1


def test_parse_heartbeat_no_false_positive_on_error_word():
    """The old keyword-based detection would flag 'error' in reply as failure.
    The new parser should NOT do this when structured tags are absent."""
    from app.services.heartbeat import _parse_heartbeat_outcome

    outcome, _score = _parse_heartbeat_outcome("I successfully fixed the error in ERRORS.md")
    # Should be action_taken (no structured tags, no HEARTBEAT_OK) — NOT failure
    assert outcome == "action_taken"


def test_heartbeat_lease_is_mutually_exclusive():
    from app.services import heartbeat

    agent_id = uuid4()
    heartbeat._heartbeat_leases.clear()
    try:
        assert heartbeat._try_acquire_heartbeat_lease(agent_id, now=datetime.now(timezone.utc))
        assert heartbeat._try_acquire_heartbeat_lease(agent_id, now=datetime.now(timezone.utc)) is False

        heartbeat._release_heartbeat_lease(agent_id)

        assert heartbeat._try_acquire_heartbeat_lease(agent_id, now=datetime.now(timezone.utc))
    finally:
        heartbeat._heartbeat_leases.clear()


@pytest.mark.asyncio
async def test_heartbeat_distributed_lease_uses_redis(monkeypatch):
    from app.services import heartbeat

    agent_id = uuid4()
    calls: list[tuple] = []

    class FakeRedis:
        async def set(self, key, value, ex=None, nx=False):
            calls.append(("set", key, value, ex, nx))
            return True

        async def delete(self, key):
            calls.append(("delete", key))
            return 1

    async def fake_get_redis():
        return FakeRedis()

    monkeypatch.setattr(heartbeat, "get_redis", fake_get_redis)

    acquired = await heartbeat._try_acquire_heartbeat_lease_async(agent_id)
    await heartbeat._release_heartbeat_lease_async(agent_id)

    assert acquired is True
    assert calls[0][0] == "set"
    assert calls[0][1] == f"heartbeat_lease:{agent_id}"
    assert calls[1] == ("delete", f"heartbeat_lease:{agent_id}")


# ─── _build_evolution_context ───────────────────────────────────


@pytest.mark.asyncio
async def test_build_evolution_context_cold_start_bootstrap():
    """When agent has < 3 non-heartbeat activities, inject bootstrap guidance."""
    from app.services.heartbeat import _build_evolution_context

    agent_id = uuid4()
    # Only 1 heartbeat activity, no real work
    activities = [
        SimpleNamespace(action_type="heartbeat", summary="Heartbeat: OK", detail_json={}),
    ]
    result = await _build_evolution_context(agent_id, activities)
    assert "Bootstrap Mode" in result
    assert "Read soul.md" in result


@pytest.mark.asyncio
async def test_build_evolution_context_not_cold_after_enough_activities():
    """When agent has >= 3 non-heartbeat activities, no bootstrap section."""
    from app.services.heartbeat import _build_evolution_context

    agent_id = uuid4()
    activities = [
        SimpleNamespace(action_type="chat_reply", summary="Hello", detail_json={}),
        SimpleNamespace(action_type="tool_call", summary="read_file", detail_json={"tool": "read_file"}),
        SimpleNamespace(action_type="chat_reply", summary="Done", detail_json={}),
        SimpleNamespace(action_type="heartbeat", summary="OK", detail_json={}),
    ]
    result = await _build_evolution_context(agent_id, activities)
    assert "Bootstrap Mode" not in result


@pytest.mark.asyncio
async def test_build_evolution_context_includes_error_details():
    """Error details from detail_json should be included."""
    from app.services.heartbeat import _build_evolution_context

    agent_id = uuid4()
    activities = [
        SimpleNamespace(
            action_type="error",
            summary="Tool call failed",
            detail_json={"error": "ConnectionTimeout: failed to connect to API after 30s"},
        ),
        SimpleNamespace(action_type="chat_reply", summary="OK", detail_json={}),
        SimpleNamespace(action_type="chat_reply", summary="OK2", detail_json={}),
        SimpleNamespace(action_type="chat_reply", summary="OK3", detail_json={}),
    ]
    result = await _build_evolution_context(agent_id, activities)
    assert "ConnectionTimeout" in result
    assert "Recent error details" in result


@pytest.mark.asyncio
async def test_build_evolution_context_suggests_save_skill_for_repeated_workflow():
    from app.services.heartbeat import _build_evolution_context

    agent_id = uuid4()
    activities = [
        SimpleNamespace(action_type="tool_call", summary="web_search", detail_json={"tool": "web_search"}),
        SimpleNamespace(action_type="tool_call", summary="web_search", detail_json={"tool": "web_search"}),
        SimpleNamespace(action_type="tool_call", summary="web_search", detail_json={"tool": "web_search"}),
        SimpleNamespace(action_type="tool_call", summary="web_fetch", detail_json={"tool": "web_fetch"}),
        SimpleNamespace(action_type="tool_call", summary="web_fetch", detail_json={"tool": "web_fetch"}),
        SimpleNamespace(action_type="tool_call", summary="web_fetch", detail_json={"tool": "web_fetch"}),
        SimpleNamespace(action_type="chat_reply", summary="Summarized findings", detail_json={}),
        SimpleNamespace(action_type="chat_reply", summary="Delivered answer", detail_json={}),
    ]

    result = await _build_evolution_context(agent_id, activities)

    assert "Skill Creation Opportunity" in result
    assert "save_skill" in result
    assert "load_skill" in result
    assert "workflow" in result.lower()


@pytest.mark.asyncio
async def test_build_evolution_context_includes_proactive_steward_plan():
    from app.services.heartbeat import _build_evolution_context

    agent_id = uuid4()
    activities = [
        SimpleNamespace(
            action_type="task_updated",
            summary="Investor memo follow-up is waiting on a local draft.",
            detail_json={"open_loop": True, "objective_id": "objective-1"},
        ),
        SimpleNamespace(action_type="chat_reply", summary="OK", detail_json={}),
        SimpleNamespace(action_type="chat_reply", summary="OK2", detail_json={}),
        SimpleNamespace(action_type="chat_reply", summary="OK3", detail_json={}),
    ]

    result = await _build_evolution_context(agent_id, activities)

    assert "Proactive Steward Context" in result
    assert "Prepare local draft" in result
    assert "Evidence: task_updated" in result


# ─── plaza executor limits ──────────────────────────────────────


@pytest.mark.asyncio
async def test_build_heartbeat_tool_executor_enforces_plaza_limits(monkeypatch):
    from app.services.heartbeat import _build_heartbeat_tool_executor

    agent_id = uuid4()
    creator_id = uuid4()
    calls = []

    async def fake_execute_tool(tool_name, args, _agent_id, _creator_id):
        calls.append((tool_name, args, _agent_id, _creator_id))
        return f"ran:{tool_name}"

    monkeypatch.setattr("app.services.heartbeat.execute_tool", fake_execute_tool)

    executor = _build_heartbeat_tool_executor(agent_id, creator_id)

    first_post = await executor("plaza_create_post", {"content": "post-1"})
    blocked_post = await executor("plaza_create_post", {"content": "post-2"})
    first_comment = await executor("plaza_add_comment", {"content": "comment-1"})
    second_comment = await executor("plaza_add_comment", {"content": "comment-2"})
    blocked_comment = await executor("plaza_add_comment", {"content": "comment-3"})
    generic = await executor("web_search", {"query": "heartbeat"})

    assert first_post == "ran:plaza_create_post"
    assert blocked_post.startswith("[BLOCKED]")
    assert first_comment == "ran:plaza_add_comment"
    assert second_comment == "ran:plaza_add_comment"
    assert blocked_comment.startswith("[BLOCKED]")
    assert generic == "ran:web_search"
    assert calls == [
        ("plaza_create_post", {"content": "post-1"}, agent_id, creator_id),
        ("plaza_add_comment", {"content": "comment-1"}, agent_id, creator_id),
        ("plaza_add_comment", {"content": "comment-2"}, agent_id, creator_id),
        ("web_search", {"query": "heartbeat"}, agent_id, creator_id),
    ]


# ─── _execute_heartbeat integration ────────────────────────────


@pytest.mark.asyncio
async def test_execute_heartbeat_uses_correct_settings(monkeypatch):
    """Verify invoke_agent is called with core_tools_only=False, max_tool_rounds=15,
    and a heartbeat session is created."""
    from app.services.heartbeat import _execute_heartbeat

    agent_id = uuid4()
    creator_id = uuid4()
    model_id = uuid4()
    tenant_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Heartbeat Agent",
        role_description="Watcher",
        primary_model_id=model_id,
        fallback_model_id=None,
        creator_id=creator_id,
        tenant_id=tenant_id,
        last_heartbeat_at=None,
    )
    model = SimpleNamespace(
        id=model_id,
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
        tenant_id=tenant_id,
    )
    participant = SimpleNamespace(id=uuid4(), type="agent", ref_id=agent_id)

    # Sequence: Agent, LLMModel, ActivityLogs, Participant
    fake_session = _FakeSession([agent, model, [], participant])
    captured = {}

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="Did work\n[OUTCOME:action_taken] [SCORE:5]")

    monkeypatch.setattr("app.database.async_session", lambda: fake_session)
    monkeypatch.setattr("app.services.heartbeat.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr("app.services.heartbeat._load_heartbeat_instruction", lambda _id: "HB")

    # Stub activity logger and execution context
    async def _noop_log(*args, **kwargs):
        pass

    monkeypatch.setattr("app.core.execution_context.set_agent_bot_identity", lambda *a, **kw: None)

    await _execute_heartbeat(agent_id)

    request = captured["request"]

    # Core assertions — the critical fixes
    assert request.core_tools_only is False, "Heartbeat should have full tool access"
    assert request.max_tool_rounds == 15, "Heartbeat should stay within the curated 15-round budget"
    assert request.session_context is not None
    assert request.session_context.source == "heartbeat"
    assert request.session_context.session_id is not None, "Heartbeat must have session_id for memory"
    assert request.on_tool_call is not None, "Heartbeat must persist tool calls"
    assert request.execution_identity.identity_type == "agent_bot"


@pytest.mark.asyncio
async def test_execute_heartbeat_marks_runtime_task_skipped_when_no_model(monkeypatch):
    from app.services import heartbeat

    agent_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="No Model Agent",
        primary_model_id=None,
        fallback_model_id=None,
        creator_id=uuid4(),
        tenant_id=uuid4(),
    )
    fake_session = _FakeSession([agent])
    created = []
    updates = []

    async def fake_create_runtime_task_record(**kwargs):
        created.append(kwargs)
        return "heartbeat-task-1"

    async def fake_update_runtime_task_record(task_id, **fields):
        updates.append((task_id, fields))
        return True

    monkeypatch.setattr("app.database.async_session", lambda: fake_session)
    monkeypatch.setattr(heartbeat, "create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr(heartbeat, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.core.execution_context.set_agent_bot_identity", lambda *a, **kw: None)

    await heartbeat._execute_heartbeat(agent_id, lease_acquired=True)

    assert created[0]["task_type"] == "heartbeat"
    assert created[0]["status"] == "running"
    assert created[0]["parent_agent_id"] == agent_id
    assert updates[-1][0] == "heartbeat-task-1"
    assert updates[-1][1]["status"] == "skipped"
    assert updates[-1][1]["metadata_json"]["skip_reason"] == "no_model"


@pytest.mark.asyncio
async def test_execute_heartbeat_does_not_resurrect_session_state_after_reset(monkeypatch):
    from app.services import heartbeat

    agent_id = uuid4()
    creator_id = uuid4()
    model_id = uuid4()
    tenant_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Reset Race Agent",
        role_description="Watcher",
        primary_model_id=model_id,
        fallback_model_id=None,
        creator_id=creator_id,
        tenant_id=tenant_id,
        last_heartbeat_at=None,
    )
    model = SimpleNamespace(
        id=model_id,
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
        tenant_id=tenant_id,
    )
    participant = SimpleNamespace(id=uuid4(), type="agent", ref_id=agent_id)
    fake_session = _FakeSession([agent, model, [], participant])

    async def fake_invoke_agent(_request):
        heartbeat._reset_heartbeat_session(agent_id)
        return SimpleNamespace(content="Nothing changed\n[OUTCOME:noop] [SCORE:1]")

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.database.async_session", lambda: fake_session)
    monkeypatch.setattr(heartbeat, "invoke_agent", fake_invoke_agent)
    monkeypatch.setattr(heartbeat, "_load_heartbeat_instruction", lambda _id: "HB")
    monkeypatch.setattr(heartbeat, "_update_evolution_files", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.core.execution_context.set_agent_bot_identity", lambda *a, **kw: None)
    monkeypatch.setattr("app.services.activity_logger.log_activity", _noop)

    heartbeat._heartbeat_contexts.pop(agent_id, None)
    heartbeat._heartbeat_session_ids.pop(agent_id, None)
    heartbeat._heartbeat_tick_counts.pop(agent_id, None)
    heartbeat._heartbeat_session_ctxs.pop(agent_id, None)

    await heartbeat._execute_heartbeat(agent_id, lease_acquired=True)

    assert agent_id not in heartbeat._heartbeat_contexts
    assert agent_id not in heartbeat._heartbeat_session_ids


@pytest.mark.asyncio
async def test_execute_heartbeat_recovers_from_incomplete_persistent_session(monkeypatch):
    from app.services import heartbeat

    agent_id = uuid4()
    creator_id = uuid4()
    model_id = uuid4()
    tenant_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Recovered Heartbeat Agent",
        role_description="Watcher",
        primary_model_id=model_id,
        fallback_model_id=None,
        creator_id=creator_id,
        tenant_id=tenant_id,
        last_heartbeat_at=None,
    )
    model = SimpleNamespace(
        id=model_id,
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
        tenant_id=tenant_id,
    )
    participant = SimpleNamespace(id=uuid4(), type="agent", ref_id=agent_id)
    fake_session = _FakeSession([agent, model, [], participant])
    captured = {}

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="Recovered\n[OUTCOME:noop] [SCORE:1]")

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.database.async_session", lambda: fake_session)
    monkeypatch.setattr(heartbeat, "invoke_agent", fake_invoke_agent)
    monkeypatch.setattr(heartbeat, "_load_heartbeat_instruction", lambda _id: "HB")
    monkeypatch.setattr(heartbeat, "_update_evolution_files", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.core.execution_context.set_agent_bot_identity", lambda *a, **kw: None)
    monkeypatch.setattr("app.services.activity_logger.log_activity", _noop)

    heartbeat._heartbeat_contexts[agent_id] = [{"role": "user", "content": "stale context"}]
    heartbeat._heartbeat_session_ids.pop(agent_id, None)
    heartbeat._heartbeat_tick_counts[agent_id] = 3
    heartbeat._heartbeat_session_ctxs.pop(agent_id, None)

    await heartbeat._execute_heartbeat(agent_id, lease_acquired=True)

    assert captured["request"].messages[0]["content"].startswith("HB")
    assert agent_id in heartbeat._heartbeat_contexts
    assert agent_id in heartbeat._heartbeat_session_ids

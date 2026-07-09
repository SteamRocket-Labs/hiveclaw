from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent


def _session(*, metadata: dict | None = None) -> ChatSession:
    root_id = uuid4()
    return ChatSession(
        id=uuid4(),
        agent_id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        title="Parity session",
        source_channel="web",
        session_kind="human_chat",
        actor_type="user",
        runtime_source="web_chat",
        visibility_scope="direct_user",
        listed_surface="chat",
        parent_session_id=uuid4(),
        root_session_id=root_id,
        transcript_metadata_json=metadata or {},
    )


def _event(session: ChatSession, event_type: str, sequence: int, content: str) -> ChatTranscriptEvent:
    return ChatTranscriptEvent(
        id=uuid4(),
        sequence=sequence,
        tenant_id=session.tenant_id,
        agent_id=session.agent_id,
        session_id=session.id,
        actor_type="user" if event_type == "user_message" else "assistant",
        event_type=event_type,
        visibility_scope="direct_user",
        listed_surface="chat",
        content=content,
        metadata_json={"checkpoint": event_type == "user_message"},
    )


def test_session_index_captures_thread_metadata_checkpoints_and_dynamic_tools() -> None:
    from app.services.session_index import build_session_index

    session = _session(
        metadata={
            "forked_from_id": "source-thread",
            "dynamic_tools": [{"name": "load_skill"}, {"name": "preview_workflow"}],
            "archived": False,
            "t0_segments": [{"segment_id": "seg-1", "events_path": "segments/seg-1/events.jsonl"}],
        }
    )
    index = build_session_index(
        session=session,
        transcript_events=[
            _event(session, "user_message", 10, "first request"),
            _event(session, "assistant_message", 20, "answer"),
            _event(session, "user_message", 30, "second request"),
        ],
    )

    assert index["thread_id"] == str(session.id)
    assert index["forked_from_id"] == "source-thread"
    assert index["parent_thread_id"] == str(session.parent_session_id)
    assert index["root_thread_id"] == str(session.root_session_id)
    assert index["event_persistence_mode"] == "extended"
    assert index["dynamic_tools"] == [{"name": "load_skill"}, {"name": "preview_workflow"}]
    assert [item["sequence"] for item in index["checkpoints"]] == [10, 30]
    assert index["t0_segments"][0]["events_path"] == "segments/seg-1/events.jsonl"


@pytest.mark.asyncio
async def test_compaction_trace_records_attempt_and_installed_checkpoint_with_stable_id() -> None:
    from app.runtime.compaction_trace import CompactionRequest, CompactionTraceContext

    facts: list[dict] = []

    async def fake_recorder(fact: dict) -> None:
        facts.append(fact)

    ctx = CompactionTraceContext.enabled(
        thread_id="thread-1",
        turn_id="turn-1",
        model="gpt-5",
        provider_name="openai",
        fact_recorder=fake_recorder,
    )
    request = CompactionRequest(
        model="gpt-5",
        input=[{"role": "user", "content": "large history"}],
        instructions="compact without losing open loops",
        tools=[{"name": "read_ledger"}],
        parallel_tool_calls=True,
        prompt_cache_key="cache-key",
    )

    attempt = await ctx.start_attempt(request)
    await attempt.record_completed(output_items=[{"role": "assistant", "content": "summary"}])
    await ctx.record_installed(
        input_history=[{"role": "user", "content": "large history"}],
        replacement_history=[{"role": "assistant", "content": "summary"}],
    )

    assert [fact["fact_type"] for fact in facts] == [
        "compaction_attempt_started",
        "compaction_attempt_completed",
        "compaction_checkpoint_installed",
    ]
    assert facts[0]["compaction_id"] == facts[1]["compaction_id"] == facts[2]["compaction_id"]
    assert facts[0]["request"]["prompt_cache_key"] == "cache-key"
    assert facts[2]["checkpoint"]["replacement_history"][0]["content"] == "summary"


def test_goal_prompt_variants_include_budget_state_and_xml_escape() -> None:
    from app.runtime.prompts.goals import (
        ThreadGoalPromptState,
        budget_limit_prompt,
        continuation_prompt,
        objective_updated_prompt,
    )

    goal = ThreadGoalPromptState(
        objective="Ship <CC parity> & verify",
        tokens_used=25,
        token_budget=100,
        time_used_seconds=7,
    )

    continuation = continuation_prompt(goal)
    budget = budget_limit_prompt(goal)
    updated = objective_updated_prompt(goal)

    assert "Ship &lt;CC parity&gt; &amp; verify" in continuation
    assert "<remaining_tokens>75</remaining_tokens>" in continuation
    assert "<time_used_seconds>7</time_used_seconds>" in budget
    assert "<tokens_used>25</tokens_used>" in updated


def test_permissions_prompt_is_derived_from_effective_policy() -> None:
    from app.runtime.prompts.permissions import PermissionsPromptContext, build_permissions_prompt

    text = build_permissions_prompt(
        PermissionsPromptContext(
            approval_policy="on_request",
            network_access="restricted",
            writable_roots=["/workspace/project"],
            denied_reads=["/workspace/project/.env"],
            allowed_tools=["read_file", "write_file"],
            denied_actions=["raw_subprocess"],
            request_permission_tool_enabled=True,
        )
    )

    assert "approval_policy: on_request" in text
    assert "network_access: restricted" in text
    assert "/workspace/project" in text
    assert "/workspace/project/.env" in text
    assert "raw_subprocess" in text
    assert "request_permission_tool_enabled: true" in text


def test_codex_optimization_ledger_keeps_codex_as_additive_control_plane() -> None:
    from app.runtime import codex_optimization_ledger
    from app.runtime.codex_optimization_ledger import build_codex_optimization_ledger

    ledger = build_codex_optimization_ledger()

    assert ledger["schema"] == "hive.ccplus.codex_optimization_ledger.v1"
    assert ledger["semantic_baseline"] == "freecode_cc"
    assert ledger["codex_role"] == "additive_control_plane"
    assert "/Users/" not in repr(ledger)
    assert all(not source.startswith("/") for source in ledger["source_snapshot"].values())
    capabilities = {entry["capability"] for entry in ledger["adoptable_control_plane"]}
    assert {
        "approval_sandbox_decision_enum",
        "compaction_lifecycle_hooks",
        "turn_thread_telemetry",
        "resume_reconciliation",
        "memory_consolidation_worker",
    } <= capabilities
    assert "skill_progressive_disclosure" in ledger["forbidden_semantic_overrides"]
    assert not hasattr(codex_optimization_ledger, "codex_delta_can_override_semantics")


@pytest.mark.asyncio
async def test_compaction_wrapper_records_attempt_completion_and_checkpoint() -> None:
    from app.kernel.engine import _compress_messages_with_trace
    from app.runtime.compaction_trace import CompactionTraceContext

    facts: list[dict] = []

    async def recorder(fact: dict) -> None:
        facts.append(fact)

    async def fake_compress(messages: list[dict], **_kwargs) -> list[dict]:
        return [{"role": "assistant", "content": "compact summary"}, messages[-1]]

    trace = CompactionTraceContext.enabled(
        thread_id="thread-1",
        turn_id="turn-1",
        model="gpt-test",
        provider_name="openai",
        fact_recorder=recorder,
        compaction_id="cmp_test",
    )

    result = await _compress_messages_with_trace(
        fake_compress,
        [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}],
        trace_context=trace,
        model_provider="openai",
        model_name="gpt-test",
        tenant_id=None,
        tools=[{"type": "function", "function": {"name": "tool_search"}}],
        parallel_tool_calls=True,
        instructions="compress carefully",
    )

    assert result[0]["content"] == "compact summary"
    assert [fact["fact_type"] for fact in facts] == [
        "compaction_attempt_started",
        "compaction_attempt_completed",
        "compaction_checkpoint_installed",
    ]
    assert all(fact["compaction_id"] == "cmp_test" for fact in facts)
    assert facts[0]["request"]["tools"][0]["function"]["name"] == "tool_search"
    assert facts[2]["checkpoint"]["input_history"][0]["content"] == "hello"
    assert facts[2]["checkpoint"]["replacement_history"][0]["content"] == "compact summary"

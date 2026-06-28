from __future__ import annotations

import pytest

from app.runtime.hooks import HookContext, HookEvent, HookRegistry, HookResult


def test_cc_core_lifecycle_hook_events_are_exposed() -> None:
    values = {event.value for event in HookEvent}

    assert {
        "user_prompt_submit",
        "session_start",
        "session_end",
        "stop",
        "stop_failure",
        "subagent_start",
        "subagent_stop",
        "pre_compaction",
        "post_compaction",
    }.issubset(values)


def test_ccplus_broader_hook_catalog_declares_contracts_and_noop_capability() -> None:
    registry = HookRegistry()
    catalog = {item["event"]: item for item in registry.describe_event_catalog()}

    required = {
        "pre_tool_use",
        "post_tool_use",
        "post_tool_failure",
        "notification",
        "permission_request",
        "permission_denied",
        "user_prompt_submit",
        "session_start",
        "session_end",
        "stop",
        "stop_failure",
        "subagent_start",
        "subagent_stop",
        "pre_compaction",
        "post_compaction",
        "setup",
        "config_change",
        "instructions_loaded",
        "task_created",
        "task_completed",
        "teammate_idle",
        "elicitation",
        "elicitation_result",
        "worktree_create",
        "worktree_remove",
        "cwd_changed",
        "file_changed",
    }
    assert required.issubset(catalog)

    for event_name in required:
        entry = catalog[event_name]
        assert entry["standard"] is True
        assert entry["lifecycle_state"] in {"active", "active_observe", "disabled_noop"}
        assert entry["trigger_point"]
        assert isinstance(entry["matcher_fields"], list)
        assert entry["input_schema"]["type"] == "object"
        assert entry["output_schema"]["type"] == "object"
        assert entry["runtime_consumer"]
        assert entry["trust_level"] in {"platform_trusted", "tenant_approved", "agent_scoped", "disabled_noop"}
        assert entry["failure_policy"] in {"fail_closed_if_blocking", "observe_continue", "disabled_noop"}

    assert catalog["worktree_create"]["lifecycle_state"] == "disabled_noop"
    assert catalog["worktree_create"]["runtime_consumer"] == "disabled_noop_audit"
    assert "worktree_path" in catalog["worktree_create"]["output_schema"]["properties"]
    assert "permission_decision" in catalog["permission_request"]["output_schema"]["properties"]
    assert catalog["pre_tool_use"]["runtime_consumer"] == "kernel_pre_tool_use"


def test_permission_denied_reports_live_not_disabled_noop() -> None:
    """B-5 regression guard: PERMISSION_DENIED is live-emitted by tool governance
    (governance.py) and the chat-session permission path (chat_sessions.py), so the
    catalog must NOT classify it as disabled_noop with a noop audit consumer."""
    registry = HookRegistry()
    catalog = {item["event"]: item for item in registry.describe_event_catalog()}

    denied = catalog["permission_denied"]
    assert denied["lifecycle_state"] != "disabled_noop"
    assert denied["lifecycle_state"] == "active_observe"
    assert denied["runtime_consumer"] != "disabled_noop_audit"
    assert denied["runtime_consumer"] == "permission_denied_audit_observer"
    assert denied["standard"] is True


def test_cc_lifecycle_hook_context_carries_standard_payload_fields() -> None:
    ctx = HookContext(
        event=HookEvent.SUBAGENT_STOP,
        session_id="parent-session",
        prompt="Inspect this",
        last_assistant_message="Done",
        stop_hook_active=True,
        agent_type="explorer",
        agent_transcript_path="/tmp/t0/source.md",
        metadata={"runtime_task_id": "rt-1"},
    )

    assert ctx.prompt == "Inspect this"
    assert ctx.last_assistant_message == "Done"
    assert ctx.stop_hook_active is True
    assert ctx.agent_type == "explorer"
    assert ctx.agent_transcript_path == "/tmp/t0/source.md"
    assert ctx.metadata["runtime_task_id"] == "rt-1"


@pytest.mark.asyncio
async def test_stop_hook_can_return_blocking_result() -> None:
    registry = HookRegistry()

    def stop_blocker(ctx: HookContext) -> HookResult:
        return HookResult(block=True, reason="final answer needs verification")

    registry.register(HookEvent.STOP, stop_blocker)

    result = await registry.emit(
        HookContext(
            event=HookEvent.STOP,
            session_id="s1",
            last_assistant_message="draft answer",
            stop_hook_active=False,
        )
    )

    assert result is not None
    assert result.block is True
    assert result.reason == "final answer needs verification"


@pytest.mark.asyncio
async def test_permission_request_hook_returns_decision_payload() -> None:
    registry = HookRegistry()

    def approve_permission(ctx: HookContext) -> HookResult:
        return HookResult(
            permission_request_result={
                "behavior": "allow",
                "updatedInput": {"query": "github trending"},
                "updatedPermissions": [{"tool": "web_search", "behavior": "allow", "scope": "session"}],
            }
        )

    registry.register(HookEvent.PERMISSION_REQUEST, approve_permission)

    result = await registry.emit(
        HookContext(
            event=HookEvent.PERMISSION_REQUEST,
            session_id="s1",
            tool_name="web_search",
            tool_args={"query": "github"},
            metadata={"permission_request": {"tool_name": "web_search"}},
        )
    )

    assert result is not None
    assert result.permission_request_result == {
        "behavior": "allow",
        "updatedInput": {"query": "github trending"},
        "updatedPermissions": [{"tool": "web_search", "behavior": "allow", "scope": "session"}],
    }


@pytest.mark.asyncio
async def test_hook_emit_records_hook_lifecycle_v1_for_modified_args() -> None:
    registry = HookRegistry()

    def rewrite(ctx: HookContext) -> HookResult:
        assert ctx.tool_args == {"query": "github"}
        return HookResult(modified_args={"query": "github trending"})

    registry.register(HookEvent.PRE_TOOL_USE, rewrite, key="skill:session-1:research:pre_tool_use")
    metadata = {"tenant_id": "tenant-1"}

    result = await registry.emit(
        HookContext(
            event=HookEvent.PRE_TOOL_USE,
            agent_id="agent-1",
            session_id="session-1",
            tool_name="web_search",
            tool_args={"query": "github"},
            metadata=metadata,
        )
    )

    assert result is not None
    assert result.modified_args == {"query": "github trending"}
    records = metadata["hook_lifecycle_records"]
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "pre_tool_use"
    assert record["source"] == "skill:session-1:research:pre_tool_use"
    assert record["trust_level"] == "agent_scoped"
    assert record["lifecycle_state"] == "active"
    assert record["decision"] == "rewrite_args"
    assert record["matcher_fields"]["tool_name"] == "web_search"
    assert record["original_hash"] and record["modified_hash"]
    assert record["original_hash"] != record["modified_hash"]


@pytest.mark.asyncio
async def test_stop_handler_failure_emits_stop_failure() -> None:
    registry = HookRegistry()
    calls: list[tuple[str, str]] = []

    def broken(ctx: HookContext) -> None:
        raise RuntimeError("boom")

    def failure(ctx: HookContext) -> None:
        calls.append((ctx.event.value, ctx.error or ""))

    registry.register(HookEvent.STOP, broken)
    registry.register(HookEvent.STOP_FAILURE, failure)

    await registry.emit(HookContext(event=HookEvent.STOP, session_id="s1", last_assistant_message="draft"))

    assert calls == [("stop_failure", "RuntimeError: boom")]

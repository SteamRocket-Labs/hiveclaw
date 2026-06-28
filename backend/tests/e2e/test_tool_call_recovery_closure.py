from __future__ import annotations

import pytest

from app.services.llm_client import LLMMessage


def test_recovery_manifest_preserves_tool_call_closure_state() -> None:
    from app.runtime.recovery_manifest import build_recovery_manifest
    from app.runtime.session import SessionContext

    session = SessionContext(
        session_id="session-tool-closure",
        source="web",
        channel="web",
        active_tool_groups=[{"name": "web_pack", "tools": ["firecrawl_fetch"]}],
        metadata={
            "pending_tool_frame": {
                "tool_call_id": "call_pending",
                "tool_name": "send_email",
                "arguments": {"to": "customer@example.com"},
            },
            "permission_checkpoint": {
                "checkpoint_id": "permission-checkpoint-1",
                "tool_call_id": "call_pending",
                "decision": "allow_once",
            },
            "hook_lifecycle_records": [
                {
                    "hook_run_id": "hook-pre-1",
                    "event": "pre_tool_use",
                    "decision": "rewrite_args",
                }
            ],
            "compaction_lifecycle_records": [
                {
                    "compaction_id": "compact-1",
                    "trigger": "request_preflight",
                    "status": "completed",
                }
            ],
            "permission_profile": {"mode": "request", "allowed_tools": ["send_email"]},
        },
    )
    session.track_discovered_tools(["firecrawl_fetch"])
    session.track_tool_outcome("tool_search", "Discovered firecrawl_fetch for crawled evidence.")

    manifest = build_recovery_manifest(session)

    assert manifest.discovered_tools == ["firecrawl_fetch"]
    assert manifest.pending_tool_frames[0]["tool_call_id"] == "call_pending"
    assert manifest.permission_checkpoints[0]["checkpoint_id"] == "permission-checkpoint-1"
    assert manifest.hook_lifecycle_records[0]["event"] == "pre_tool_use"
    assert manifest.compaction_lifecycle_records[0]["compaction_id"] == "compact-1"
    assert manifest.permission_profile["mode"] == "request"

    restored = manifest.to_restoration_text()
    assert "Discovered Tools" in restored
    assert "Pending Tool Frames" in restored
    assert "Permission Checkpoints" in restored
    assert "Hook Lifecycle Records" in restored
    assert "Compaction Lifecycle Records" in restored


@pytest.mark.asyncio
async def test_request_preflight_compaction_emits_compaction_lifecycle_event() -> None:
    from app.runtime.ccplus_contracts import build_context_policy
    from app.runtime.session_context_controller import prepare_session_context_for_request

    messages = [
        LLMMessage(role="user", content="A" * 120),
        LLMMessage(role="assistant", content="B" * 120),
        LLMMessage(role="user", content="C" * 120),
    ]
    events: list[dict] = []

    async def compress_messages(_messages, **kwargs):
        on_compaction = kwargs.get("on_compaction")
        if on_compaction:
            await on_compaction({"event_type": "compaction_completed", "summary": "summary-ref"})
        return [{"role": "system", "content": "Compacted summary"}]

    result = await prepare_session_context_for_request(
        messages=messages,
        policy=build_context_policy(
            13250,
            overrides={
                "output_reserve": 0,
                "tool_result_inline_limit": 1000,
                "round_tool_result_budget": 1000,
            },
        ),
        estimate_tokens=lambda items: sum(len(item.content or "") for item in items),
        compress_messages=compress_messages,
        cumulative_run_tokens=900,
        session_id="session-compact",
        turn_id="turn-compact",
        runtime_task_id="task-compact",
        on_decision=lambda event: events.append(event),
    )

    lifecycle_events = [event for event in events if event.get("event_type") == "compaction_lifecycle"]

    assert result.compressed is True
    assert lifecycle_events
    lifecycle = lifecycle_events[-1]["compaction_lifecycle"]
    assert lifecycle["session_id"] == "session-compact"
    assert lifecycle["turn_id"] == "turn-compact"
    assert lifecycle["runtime_task_id"] == "task-compact"
    assert lifecycle["trigger"] == "request_preflight"
    assert lifecycle["before_message_count"] == 3
    assert lifecycle["after_message_count"] == 1
    assert lifecycle["before_token_estimate"] >= lifecycle["after_token_estimate"]

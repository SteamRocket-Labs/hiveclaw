from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

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


def test_recovery_manifest_preserves_skill_fork_and_denial_continuation_shape() -> None:
    from app.runtime.recovery_manifest import build_recovery_manifest
    from app.runtime.session import SessionContext

    session = SessionContext(
        session_id="session-crash-matrix",
        metadata={
            "pending_skill_handoffs": [
                {
                    "skill": "Research",
                    "skill_slug": "research",
                    "source": "skills/research/SKILL.md",
                    "execution_tool": "spawn_subagent",
                    "tool_arguments": {
                        "skill_source": "skills/research/SKILL.md",
                        "permission_profile": {"mode": "auto", "allowed_tools": ["web_search", "read_file"]},
                    },
                }
            ],
            "executed_skill_handoffs": [
                {
                    "skill": "Review",
                    "skill_slug": "review",
                    "execution_tool": "spawn_subagent",
                    "tool_call_id": "call-load:skill:review",
                    "result": '{"ok": true, "child_session_id": "child-review"}',
                }
            ],
            "source": "session_permission_denied_resume",
            "resumed_from_permission_request_id": "perm-denied",
            "denied_tool_name": "send_email",
            "denied_tool_call_id": "call-email",
            "resumed_turn_id": "turn-denied",
            "resumed_runtime_task_id": "runtime-denied",
            "round_state": {"round": 3},
            "t0_refs": ["t0://sessions/session-crash-matrix/events/7"],
        },
    )

    manifest = build_recovery_manifest(session)

    assert manifest.pending_skill_handoffs[0]["skill_slug"] == "research"
    assert manifest.executed_skill_handoffs[0]["tool_call_id"] == "call-load:skill:review"
    assert manifest.continuation_records == [
        {
            "source": "session_permission_denied_resume",
            "resumed_from_permission_request_id": "perm-denied",
            "denied_tool_name": "send_email",
            "denied_tool_call_id": "call-email",
            "resumed_turn_id": "turn-denied",
            "resumed_runtime_task_id": "runtime-denied",
            "round_state": {"round": 3},
            "t0_refs": ["t0://sessions/session-crash-matrix/events/7"],
        }
    ]
    restored = manifest.to_restoration_text()
    assert "Pending Skill Handoffs" in restored
    assert "Executed Skill Handoffs" in restored
    assert "Continuation Records" in restored
    assert "session_permission_denied_resume" in restored


def test_persisted_recovery_manifest_restores_full_crash_matrix(tmp_path, monkeypatch) -> None:
    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    manifest_path = tmp_path / str(agent_id) / "runtime_artifacts" / "recovery_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "session_id": "session-crash-matrix",
                "discovered_tools": ["firecrawl_fetch"],
                "pending_tool_frames": [
                    {
                        "permission_request_id": "perm-1",
                        "tool_call_id": "call-send",
                        "tool_name": "send_email",
                        "runtime_task_id": "runtime-1",
                        "turn_id": "turn-1",
                        "round_state": {"round": 2},
                        "t0_refs": ["t0://sessions/session-crash-matrix/events/5"],
                    }
                ],
                "permission_checkpoints": [
                    {
                        "permission_request_id": "perm-1",
                        "decision": "deny",
                        "continuation_runtime_task_id": "runtime-denial-continuation",
                    }
                ],
                "compaction_lifecycle_records": [
                    {"compaction_id": "compact-1", "trigger": "mid_loop_auto", "status": "completed"}
                ],
                "permission_profile": {"mode": "request", "allowed_tools": ["send_email"]},
                "mcp_assignments": [{"server": "docs", "tool": "read_mcp_resource"}],
                "truth_evidence_refs": ["truth://provider-error/web-search"],
                "truth_evidence": [{"evidence_id": "truth://provider-error/web-search", "provider": "searxng"}],
                "pending_skill_handoffs": [
                    {
                        "skill": "Research",
                        "skill_slug": "research",
                        "execution_tool": "spawn_subagent",
                        "tool_arguments": {
                            "skill_source": "skills/research/SKILL.md",
                            "permission_profile": {"mode": "auto", "allowed_tools": ["web_search"]},
                        },
                    }
                ],
                "executed_skill_handoffs": [
                    {
                        "skill": "Review",
                        "skill_slug": "review",
                        "tool_call_id": "call-load:skill:review",
                        "result": '{"child_session_id": "child-review"}',
                    }
                ],
                "continuation_records": [
                    {
                        "source": "session_permission_denied_resume",
                        "resumed_from_permission_request_id": "perm-1",
                        "denied_tool_name": "send_email",
                        "resumed_runtime_task_id": "runtime-denial-continuation",
                        "t0_refs": ["t0://sessions/session-crash-matrix/events/5"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    restored = _build_restoration_context(agent_id, session_context=SessionContext(session_id="session-crash-matrix"))

    assert "### Recovery Manifest" in restored
    assert "Pending Tool Frames" in restored
    assert "runtime-1" in restored
    assert "Permission Checkpoints" in restored
    assert "runtime-denial-continuation" in restored
    assert "Compaction Lifecycle Records" in restored
    assert "MCP Assignments" in restored
    assert "Truth Evidence" in restored
    assert "Pending Skill Handoffs" in restored
    assert "Executed Skill Handoffs" in restored
    assert "Continuation Records" in restored

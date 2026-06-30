from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace


def test_serialize_tool_call_message_includes_parts_and_legacy_fields():
    from app.services.chat_message_parts import serialize_chat_message

    import uuid

    message_id = uuid.uuid4()
    message = SimpleNamespace(
        id=message_id,
        role="tool_call",
        content='{"name":"read_file","args":{"path":"skills/test/SKILL.md"},"status":"done","result":"loaded","reasoning_content":"reasoning"}',
        created_at=datetime.now(timezone.utc),
        thinking=None,
    )

    entry = serialize_chat_message(message)

    assert entry["id"] == str(message_id)
    assert entry["toolName"] == "read_file"
    assert entry["toolArgs"] == {"path": "skills/test/SKILL.md"}
    assert entry["toolStatus"] == "done"
    assert entry["toolResult"] == "loaded"
    assert entry["parts"] == [{
        "type": "tool_call",
        "name": "read_file",
        "args": {"path": "skills/test/SKILL.md"},
        "status": "done",
        "result": "loaded",
        "reasoning": "reasoning",
    }]


def test_serialize_assistant_message_with_thinking_includes_reasoning_part():
    from app.services.chat_message_parts import serialize_chat_message

    message = SimpleNamespace(
        role="assistant",
        content="final answer",
        created_at=datetime.now(timezone.utc),
        thinking="step by step",
    )

    entry = serialize_chat_message(message)

    assert entry["parts"] == [
        {"type": "reasoning", "text": "step by step"},
        {"type": "text", "text": "final answer"},
    ]


def test_runtime_action_started_is_session_native_event():
    from app.services.chat_message_parts import build_session_native_event

    event = build_session_native_event(
        {
            "type": "runtime_action_started",
            "message": "Delegated to Web3 researcher.",
            "status": "running",
            "action_kind": "a2a_delegation",
            "tool_name": "delegate_to_agent",
            "runtime_task_id": "task-1",
            "child_session_id": "child-1",
            "parent_session_id": "parent-1",
            "target_agent_name": "Web3 researcher",
        }
    )

    assert event["type"] == "runtime_action_started"
    assert event["part"] == {
        "type": "event",
        "event_type": "runtime_action_started",
        "title": "Action Started",
        "text": "Delegated to Web3 researcher.",
        "status": "running",
        "tool_name": "delegate_to_agent",
        "runtime_task_id": "task-1",
        "child_session_id": "child-1",
        "parent_session_id": "parent-1",
        "action_kind": "a2a_delegation",
        "target_agent_name": "Web3 researcher",
    }


def test_split_inline_tools_creates_structured_parts():
    from app.services.chat_message_parts import split_inline_tools

    parts = split_inline_tools(
        "Before\n```tool_code\nweb_search\n```\n```json\n{\"query\": \"openai\"}\n```\nAfter"
    )

    assert parts == [
        {
            "role": "assistant",
            "content": "Before",
            "parts": [{"type": "text", "text": "Before"}],
        },
        {
            "role": "tool_call",
            "content": "",
            "toolName": "web_search",
            "toolArgs": {"query": "openai"},
            "toolStatus": "done",
            "toolResult": "",
            "parts": [{
                "type": "tool_call",
                "name": "web_search",
                "args": {"query": "openai"},
                "status": "done",
                "result": "",
            }],
        },
        {
            "role": "assistant",
            "content": "After",
            "parts": [{"type": "text", "text": "After"}],
        },
    ]


def test_stream_event_builders_include_structured_parts():
    from app.services.chat_message_parts import (
        build_chunk_event,
        build_compaction_event,
        build_done_event,
        build_permission_event,
        build_thinking_event,
        build_tool_call_event,
        build_tool_group_activation_event,
    )

    assert build_chunk_event("hello") == {
        "type": "chunk",
        "content": "hello",
        "part": {"type": "text_delta", "text": "hello"},
    }
    assert build_chunk_event("", reset=True) == {
        "type": "chunk",
        "content": "",
        "reset": True,
        "part": {"type": "stream_reset"},
    }
    assert build_thinking_event("plan") == {
        "type": "thinking",
        "content": "plan",
        "part": {"type": "reasoning", "text": "plan"},
    }
    assert build_tool_call_event({
        "name": "read_file",
        "args": {"path": "skills/test/SKILL.md"},
        "status": "done",
        "result": "loaded",
        "reasoning_content": "why",
    }) == {
        "type": "tool_call",
        "name": "read_file",
        "args": {"path": "skills/test/SKILL.md"},
        "status": "done",
        "result": "loaded",
        "reasoning_content": "why",
        "part": {
            "type": "tool_call",
            "name": "read_file",
            "args": {"path": "skills/test/SKILL.md"},
            "status": "done",
            "result": "loaded",
            "reasoning": "why",
        },
    }
    assert build_done_event("final answer", thinking="step by step") == {
        "type": "done",
        "role": "assistant",
        "content": "final answer",
        "parts": [
            {"type": "reasoning", "text": "step by step"},
            {"type": "text", "text": "final answer"},
        ],
        "part": {"type": "reasoning", "text": "step by step"},
    }
    assert build_permission_event({
        "tool_name": "write_file",
        "status": "approval_required",
        "message": "This action requires approval.",
        "approval_id": "approval-123",
        "security_zone": "workspace",
        "capability": "filesystem.write",
        "approval_required": True,
        "reason": "Writes modify repository files.",
        "next_step": "Open Approvals to approve or reject this action.",
    }) == {
        "type": "permission",
        "tool_name": "write_file",
        "status": "approval_required",
        "message": "This action requires approval.",
        "approval_id": "approval-123",
        "security_zone": "workspace",
        "capability": "filesystem.write",
        "approval_required": True,
        "reason": "Writes modify repository files.",
        "next_step": "Open Approvals to approve or reject this action.",
        "part": {
            "type": "event",
            "event_type": "permission",
            "title": "Permission Gate",
            "text": "This action requires approval.",
            "status": "approval_required",
            "tool_name": "write_file",
            "approval_id": "approval-123",
            "security_zone": "workspace",
            "capability": "filesystem.write",
            "approval_required": True,
            "reason": "Writes modify repository files.",
            "next_step": "Open Approvals to approve or reject this action.",
        },
    }
    assert build_compaction_event({
        "summary": "older context compressed",
        "original_message_count": 20,
        "kept_message_count": 8,
    }) == {
        "type": "session_compact",
        "summary": "older context compressed",
        "original_message_count": 20,
        "kept_message_count": 8,
        "part": {
            "type": "event",
            "event_type": "session_compact",
            "title": "Context Compacted",
            "text": "older context compressed",
            "status": "info",
            "original_message_count": 20,
            "kept_message_count": 8,
        },
    }
    assert build_tool_group_activation_event({
        "packs": [{
            "name": "web_pack",
            "summary": "网页搜索与抓取能力",
            "tools": ["web_search", "firecrawl_fetch"],
        }],
        "message": "Activated web_pack",
        "status": "info",
    }) == {
        "type": "tool_group_activation",
        "packs": [{
            "name": "web_pack",
            "summary": "网页搜索与抓取能力",
            "tools": ["web_search", "firecrawl_fetch"],
        }],
        "message": "Activated web_pack",
        "status": "info",
        "part": {
            "type": "event",
            "event_type": "tool_group_activation",
            "title": "Runtime Tool Groups Activated",
            "text": "Activated web_pack",
            "status": "info",
            "packs": [{
                "name": "web_pack",
                "summary": "网页搜索与抓取能力",
                "tools": ["web_search", "firecrawl_fetch"],
            }],
            "tool_groups": [{
                "name": "web_pack",
                "summary": "网页搜索与抓取能力",
                "tools": ["web_search", "firecrawl_fetch"],
            }],
        },
    }


def test_serialize_pack_activation_system_message_as_event():
    from app.services.chat_message_parts import serialize_chat_message

    message = SimpleNamespace(
        role="system",
        content='{"event_type":"pack_activation","message":"Activated web_pack","status":"info","packs":[{"name":"web_pack","summary":"网页搜索与抓取能力","tools":["web_search"]}]}',
        created_at=datetime.now(timezone.utc),
        thinking=None,
    )

    entry = serialize_chat_message(message)

    # Historical reader: a legacy "pack_activation" persisted message must still
    # surface as an event, normalized to the current "tool_group_activation" naming.
    assert entry["role"] == "event"
    assert entry["eventType"] == "tool_group_activation"
    assert entry["parts"] == [{
        "type": "event",
        "event_type": "tool_group_activation",
        "title": "Runtime Tool Groups Activated",
        "text": "Activated web_pack",
        "status": "info",
        "packs": [{
            "name": "web_pack",
            "summary": "网页搜索与抓取能力",
            "tools": ["web_search"],
        }],
        "tool_groups": [{
            "name": "web_pack",
            "summary": "网页搜索与抓取能力",
            "tools": ["web_search"],
        }],
    }]


def test_serialize_session_native_runtime_system_events_as_events():
    from app.services.chat_message_parts import serialize_chat_message

    message = SimpleNamespace(
        role="system",
        content=(
            '{"event_type":"hook_progress","message":"Running PreToolUse hook",'
            '"status":"running","hook_event":"PreToolUse","hook_key":"guard",'
            '"runtime_task_id":"rt-1","turn_id":"turn-1"}'
        ),
        created_at=datetime.now(timezone.utc),
        thinking=None,
    )

    entry = serialize_chat_message(message)

    assert entry["role"] == "event"
    assert entry["eventType"] == "hook_progress"
    assert entry["eventTitle"] == "Hook Progress"
    assert entry["eventStatus"] == "running"
    assert entry["parts"] == [{
        "type": "event",
        "event_type": "hook_progress",
        "title": "Hook Progress",
        "text": "Running PreToolUse hook",
        "status": "running",
        "hook_event": "PreToolUse",
        "hook_key": "guard",
        "runtime_task_id": "rt-1",
        "turn_id": "turn-1",
    }]


def test_build_session_native_event_preserves_generic_metadata():
    from app.services.chat_message_parts import build_session_native_event

    event = build_session_native_event({
        "type": "workflow_step",
        "message": "Running gather-sources",
        "status": "running",
        "runtime_task_id": "rt-1",
        "workflow_run_id": "wf-1",
        "workflow_step_id": "step-2",
    })

    assert event["type"] == "workflow_step"
    assert event["part"] == {
        "type": "event",
        "event_type": "workflow_step",
        "title": "Workflow Step",
        "text": "Running gather-sources",
        "status": "running",
        "runtime_task_id": "rt-1",
        "workflow_run_id": "wf-1",
        "workflow_step_id": "step-2",
    }


def test_build_session_native_event_preserves_task_notification_source():
    from app.services.chat_message_parts import build_session_native_event

    event = build_session_native_event({
        "type": "agent_task_notification",
        "message": "Workflow completed.",
        "status": "completed",
        "notification_source": "workflow",
        "task_id": "run-1",
        "task_type": "workflow",
        "runtime_task_id": "run-1",
    })

    assert event["type"] == "agent_task_notification"
    assert event["part"] == {
        "type": "event",
        "event_type": "agent_task_notification",
        "title": "Task Notification",
        "text": "Workflow completed.",
        "status": "completed",
        "notification_source": "workflow",
        "task_id": "run-1",
        "task_type": "workflow",
        "runtime_task_id": "run-1",
    }


def test_artifact_parts_preserve_revision_metadata():
    from app.services.chat_message_parts import serialize_chat_message

    message = SimpleNamespace(
        role="assistant",
        content="Updated report.",
        created_at=datetime.now(timezone.utc),
        thinking=None,
    )

    entry = serialize_chat_message(
        message,
        artifacts=[
            {
                "artifact_id": "artifact-1",
                "path": "workspace/report.md",
                "name": "report.md",
                "preview_kind": "markdown",
                "source": "workflow",
                "runtime_task_id": "rt-1",
                "revision_id": "rev-2",
                "action": "updated",
                "tool_call_id": "tool-9",
                "diff_summary": "+3 -1",
            }
        ],
    )

    assert entry["artifacts"] == [{
        "type": "artifact",
        "artifact_id": "artifact-1",
        "path": "workspace/report.md",
        "name": "report.md",
        "preview_kind": "markdown",
        "source": "workflow",
        "runtime_task_id": "rt-1",
        "revision_id": "rev-2",
        "action": "updated",
        "tool_call_id": "tool-9",
        "diff_summary": "+3 -1",
    }]


def test_serialize_permission_system_message_preserves_enriched_metadata():
    from app.services.chat_message_parts import serialize_chat_message

    message = SimpleNamespace(
        role="system",
        content=(
            '{"event_type":"permission","message":"Need approval before changing workspace files.",'
            '"status":"approval_required","tool_name":"write_file","approval_id":"approval-456",'
            '"security_zone":"workspace","capability":"filesystem.write","approval_required":true,'
            '"reason":"Repository files will be modified.","next_step":"Open Approvals to continue."}'
        ),
        created_at=datetime.now(timezone.utc),
        thinking=None,
    )

    entry = serialize_chat_message(message)

    assert entry["role"] == "event"
    assert entry["eventType"] == "permission"
    assert entry["eventToolName"] == "write_file"
    assert entry["eventApprovalId"] == "approval-456"
    assert entry["parts"] == [{
        "type": "event",
        "event_type": "permission",
        "title": "Permission Gate",
        "text": "Need approval before changing workspace files.",
        "status": "approval_required",
        "tool_name": "write_file",
        "approval_id": "approval-456",
        "security_zone": "workspace",
        "capability": "filesystem.write",
        "approval_required": True,
        "reason": "Repository files will be modified.",
        "next_step": "Open Approvals to continue.",
    }]

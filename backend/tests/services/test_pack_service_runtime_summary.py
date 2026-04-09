from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from app.services.pack_service import _summarize_chat_messages, get_session_runtime_summary
from app.services.token_tracker import estimate_tokens_from_chars


def _msg(role: str, content: str):
    return SimpleNamespace(
        role=role,
        content=content,
        created_at=datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
    )


def test_summarize_chat_messages_tracks_last_compaction_metadata():
    summary = _summarize_chat_messages(
        [
            _msg(
                "system",
                json.dumps(
                    {
                        "type": "session_compact",
                        "summary": "Compacted the early turns.",
                        "original_message_count": 24,
                        "kept_message_count": 7,
                        "continuity_sections_injected": ["Current State", "Pending Work"],
                    }
                ),
            ),
            _msg(
                "tool_call",
                json.dumps(
                    {
                        "name": "search_query",
                        "args": {"q": "latest market news"},
                        "status": "done",
                        "result": "ok",
                    }
                ),
            ),
        ]
    )

    assert summary["compaction_count"] == 1
    assert summary["used_tools"] == ["search_query"]
    assert summary["last_compaction"] == {
        "summary": "Compacted the early turns.",
        "original_message_count": 24,
        "kept_message_count": 7,
        "continuity_sections_injected": ["Current State", "Pending Work"],
        "created_at": "2026-04-02T12:00:00+00:00",
    }


def test_summarize_chat_messages_tracks_budget_and_retry_reason_metadata():
    summary = _summarize_chat_messages(
        [
            _msg(
                "system",
                json.dumps(
                    {
                        "type": "permission",
                        "tool_name": "write_file",
                        "status": "approval_required",
                        "capability": "filesystem.write",
                        "security_zone": "workspace",
                        "retryable": True,
                        "retry_reason": "auth",
                    }
                ),
            ),
            _msg(
                "system",
                json.dumps(
                    {
                        "type": "session_compact",
                        "summary": "Trimmed stale tool results.",
                        "budget_reason": "pre_compaction_truncation",
                        "tool_name": "web_fetch",
                        "original_message_count": 40,
                        "kept_message_count": 9,
                    }
                ),
            ),
        ]
    )

    assert summary["last_retry_reason"] == "auth"
    assert summary["last_tool_budget_event"] == {
        "reason": "pre_compaction_truncation",
        "tool_name": "web_fetch",
        "created_at": "2026-04-02T12:00:00+00:00",
    }


def test_summarize_chat_messages_tracks_permission_and_team_memory_activity():
    summary = _summarize_chat_messages(
        [
            _msg(
                "system",
                json.dumps(
                    {
                        "type": "permission",
                        "tool_name": "write_file",
                        "status": "approval_required",
                        "capability": "filesystem.write",
                        "security_zone": "workspace",
                    }
                ),
            ),
            _msg(
                "system",
                json.dumps(
                    {
                        "type": "team_memory",
                        "workspace_key": "workspace-alpha",
                        "query": "deploy checklist",
                        "matched_keys": ["deploy-playbook"],
                    }
                ),
            ),
        ]
    )

    assert summary["permission_event_count"] == 1
    assert summary["team_memory_hit_count"] == 1
    assert summary["last_team_memory_hit"] == {
        "workspace_key": "workspace-alpha",
        "query": "deploy checklist",
        "matched_keys": ["deploy-playbook"],
        "created_at": "2026-04-02T12:00:00+00:00",
    }


def test_summarize_chat_messages_tolerates_non_datetime_created_at():
    summary = _summarize_chat_messages(
        [
            SimpleNamespace(
                role="system",
                content=json.dumps(
                    {
                        "type": "team_memory",
                        "workspace_key": "workspace-alpha",
                        "query": "deploy checklist",
                        "matched_keys": ["deploy-playbook"],
                    }
                ),
                created_at="not-a-datetime",
            ),
        ]
    )

    assert summary["team_memory_hit_count"] == 1
    assert summary["last_team_memory_hit"]["created_at"] is None


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Scalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _RowsResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return _Scalars(self._values)


class _QueuedDB:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _stmt):
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_get_session_runtime_summary_includes_model_and_runtime_estimates():
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    model_id = uuid.uuid4()
    session = SimpleNamespace(id=session_id, agent_id=agent_id)
    agent = SimpleNamespace(id=agent_id, primary_model_id=model_id, context_window_size=32000)
    model = SimpleNamespace(
        id=model_id,
        label="GPT-5.4",
        provider="openai",
        model="gpt-5.4",
        supports_vision=True,
        max_input_tokens=128000,
    )
    messages = [
        SimpleNamespace(
            role="user",
            content="Summarize the latest launch blockers.",
            thinking=None,
            created_at=datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
        ),
        SimpleNamespace(
            role="assistant",
            content="Here are the current blockers.",
            thinking="Need to compare with the release checklist.",
            created_at=datetime(2026, 4, 2, 12, 1, tzinfo=timezone.utc),
        ),
    ]
    expected_tokens = estimate_tokens_from_chars(
        sum(len(message.content or "") + len(getattr(message, "thinking", "") or "") for message in messages)
    )
    db = _QueuedDB(
        [
            _ScalarResult(session),
            _ScalarResult(agent),
            _ScalarResult(model),
            _RowsResult(messages),
        ]
    )

    summary = await get_session_runtime_summary(db, session_id)

    assert summary["model"] == {
        "label": "GPT-5.4",
        "provider": "openai",
        "name": "gpt-5.4",
        "supports_vision": True,
        "context_window_tokens": 128000,
    }
    assert summary["runtime"] == {
        "estimated_input_tokens": expected_tokens,
        "remaining_tokens_estimate": 128000 - expected_tokens,
    }

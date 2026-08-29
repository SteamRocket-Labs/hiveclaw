from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest


def _event(**overrides):
    values = {
        "id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "sequence": 7,
        "session_id": uuid.UUID("22222222-2222-2222-2222-222222222222"),
        "run_id": uuid.UUID("33333333-3333-3333-3333-333333333333"),
        "message_id": None,
        "parent_event_id": None,
        "root_session_id": None,
        "parent_session_id": None,
        "schema_version": 1,
        "item_type": "approval_request",
        "item_status": "waiting_user",
        "turn_id": "turn-7",
        "causation_id": None,
        "correlation_id": uuid.UUID("33333333-3333-3333-3333-333333333333"),
        "actor_type": "system",
        "event_type": "permission_request",
        "visibility_scope": "direct_user",
        "listed_surface": "chat",
        "content": "Permission required",
        "parts_json": [],
        "metadata_json": {
            "permission_request_id": "permission-7",
            "tool_name": "write_file",
            "arguments": {"path": "report.md"},
            "permission_mode": "default",
            "risk_class": "controlled_write",
            "expires_at": "2026-07-10T12:00:00+00:00",
            "completed_at": "2026-07-10T12:01:00+00:00",
            "evidence_refs": [{"kind": "invocation_span", "id": "span-7"}],
        },
        "created_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_thread_item_union_exposes_discriminator_and_all_renderer_variants() -> None:
    from app.services.thread_items import THREAD_ITEM_ADAPTER, THREAD_ITEM_TYPES

    schema = THREAD_ITEM_ADAPTER.json_schema()

    assert schema["discriminator"]["propertyName"] == "item_type"
    assert THREAD_ITEM_TYPES == {
        "user_message",
        "agent_message",
        "reasoning",
        "tool_call",
        "tool_result",
        "approval_request",
        "approval_decision",
        "plan",
        "workflow_activity",
        "subagent_activity",
        "agent_team_activity",
        "peer_a2a_activity",
        "context_compaction",
        "artifact",
        "boundary",
        "warning",
        "error",
        "event",
    }


def test_build_thread_item_uses_persisted_discriminant_and_typed_approval_data() -> None:
    from app.services.thread_items import build_thread_item

    item = build_thread_item(_event())

    assert item["schema"] == "hive.thread_item.v1"
    assert item["schema_version"] == 1
    assert item["item_type"] == "approval_request"
    assert item["item_status"] == "waiting_user"
    assert item["turn_id"] == "turn-7"
    assert item["thread_id"] == "22222222-2222-2222-2222-222222222222"
    assert item["completed_at"] == "2026-07-10T12:01:00+00:00"
    assert item["evidence_refs"] == [{"kind": "invocation_span", "id": "span-7"}]
    assert item["item_data"] == {
        "permission_request_id": "permission-7",
        "tool_name": "write_file",
        "arguments": {"path": "report.md"},
        "permission_mode": "default",
        "risk_class": "controlled_write",
        "expires_at": "2026-07-10T12:00:00+00:00",
        "allow_session_allowed": False,
        "destructive": False,
    }


def test_user_thread_projection_contains_actionable_summary_without_operator_evidence() -> None:
    from app.services.thread_items import build_thread_item

    event = _event(
        metadata_json={
            **_event().metadata_json,
            "tool_display_name": "Write final report",
            "arguments": {
                "path": "reports/final.md",
                "api_token": "secret-token-must-not-leak",
                "payload": {"private": "raw"},
            },
            "provider_error_code": "provider_timeout_internal",
        }
    )

    item = build_thread_item(event, audience="user")
    encoded = str(item)

    assert item["audience"] == "user"
    assert item["user_summary"] == "需要你的确认：Write final report"
    assert item["user_action"] == {
        "kind": "resolve_approval",
        "token": "permission-7",
        "label": "确认后继续",
        "expires_at": "2026-07-10T12:00:00+00:00",
        "impact": "可撤销或只读操作",
        "details": [{"label": "path", "value": "reports/final.md"}],
    }
    assert item["item_data"]["arguments"] == {}
    assert item["evidence_refs"] == []
    assert item["metadata"] == {"status": "waiting_user"}
    assert "operator_details" not in item
    assert "secret-token-must-not-leak" not in encoded
    assert "provider_timeout_internal" not in encoded
    assert "span-7" not in encoded


def test_operator_thread_projection_preserves_evidence_only_in_explicit_details() -> None:
    from app.services.thread_items import build_thread_item

    item = build_thread_item(_event(), audience="operator")

    assert item["audience"] == "operator"
    assert item["operator_details"]["item_data"]["arguments"] == {"path": "report.md"}
    assert item["operator_details"]["evidence_refs"] == [{"kind": "invocation_span", "id": "span-7"}]
    assert item["operator_details"]["links"]["run_id"] == "33333333-3333-3333-3333-333333333333"


def test_nonblocking_memory_degradation_has_no_turn_retry_action_or_raw_internal_copy() -> None:
    from app.services.thread_items import build_thread_item

    raw_message = "semantic retrieval is temporarily unavailable"
    item = build_thread_item(
        _event(
            event_type="memory_context_degraded",
            item_type="warning",
            item_status="succeeded",
            content=raw_message,
            metadata_json={"message": raw_message, "retryable": True},
        ),
        audience="user",
    )

    assert item.get("user_action") is None
    assert item["user_summary"] == "部分记忆检索暂时不可用，本轮任务仍在继续。"
    assert raw_message not in str(item)


@pytest.mark.parametrize(
    ("event_type", "item_type", "role"),
    [
        ("thinking", "reasoning", "assistant"),
        ("unknown_future_event", "event", "system"),
    ],
)
def test_user_thread_projection_never_echoes_raw_reasoning_or_unknown_runtime_content(
    event_type: str,
    item_type: str,
    role: str,
) -> None:
    from app.services.thread_items import build_thread_item

    raw_internal_content = "provider request secret: sk-runtime-must-not-leak"
    item = build_thread_item(
        _event(
            event_type=event_type,
            item_type=item_type,
            content=raw_internal_content,
            metadata_json={"provider_error": raw_internal_content},
        ),
        audience="user",
        role=role,
        preserve_user_content=True,
    )

    assert raw_internal_content not in str(item)
    assert item["content"] == item["user_summary"]
    if item_type == "reasoning":
        assert item["user_summary"] == ""
        assert "Agent 正在整理思路" not in str(item)


@pytest.mark.parametrize(
    ("event_type", "role", "expected"),
    [
        ("thinking", "assistant", "reasoning"),
        ("workflow_failed", "system", "workflow_activity"),
        ("subagent_task_started", "system", "subagent_activity"),
        ("team_member", "system", "agent_team_activity"),
        ("delegation_run", "system", "peer_a2a_activity"),
        ("session_compact", "system", "context_compaction"),
        ("artifact_delivery", "system", "artifact"),
        ("run_cancelled", "system", "boundary"),
        ("memory_context_degraded", "system", "warning"),
        ("memory_context_unavailable", "system", "error"),
        ("error", "system", "error"),
        ("unknown_future_event", "system", "event"),
    ],
)
def test_thread_item_classification_is_explicit_and_vendor_neutral(event_type: str, role: str, expected: str) -> None:
    from app.services.thread_items import classify_thread_item

    assert classify_thread_item(event_type=event_type, role=role) == expected


@pytest.mark.parametrize(
    ("item_type", "event_type", "expected"),
    [
        ("error", "denial", "failed"),
        ("tool_result", "tool_failure", "failed"),
        ("agent_team_activity", "member_run_started", "running"),
        ("boundary", "run_cancelled", "cancelled"),
        ("approval_request", "permission_request", "waiting_user"),
        ("warning", "memory_context_degraded", "succeeded"),
    ],
)
def test_thread_item_status_matches_the_historical_backfill(
    item_type: str,
    event_type: str,
    expected: str,
) -> None:
    from app.services.thread_items import classify_thread_item_status

    assert classify_thread_item_status(item_type=item_type, event_type=event_type, metadata={}) == expected


def test_child_session_classification_uses_typed_collaboration_metadata() -> None:
    from app.services.chat_transcript import build_transcript_item_contract

    assert (
        build_transcript_item_contract(
            event_type="child_session",
            role="system",
            metadata={"source": "subagent"},
        )[0]
        == "subagent_activity"
    )
    assert (
        build_transcript_item_contract(
            event_type="child_session",
            role="system",
            metadata={"action_kind": "a2a_delegation", "notification_source": "a2a"},
        )[0]
        == "peer_a2a_activity"
    )


@pytest.mark.asyncio
async def test_live_web_chat_broadcast_carries_same_thread_item_contract(monkeypatch) -> None:
    from app.services import web_chat_runtime

    sent: list[dict] = []

    async def capture(_agent_id, _session_id, payload):
        sent.append(payload)

    monkeypatch.setattr(web_chat_runtime.web_chat_broker, "send_session_message", capture)

    await web_chat_runtime.broadcast_web_chat_event(
        uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        {
            "id": "live-permission-1",
            "type": "permission_request",
            "status": "pending",
            "permission_request_id": "permission-live",
            "tool_name": "write_file",
            "arguments": {"path": "live.md"},
        },
    )

    assert sent[0]["schema"] == "hive.thread_item.v1"
    assert sent[0]["schema_version"] == 1
    assert sent[0]["item_type"] == "approval_request"
    assert sent[0]["item_status"] == "waiting_user"
    assert sent[0]["item_data"]["permission_request_id"] == "permission-live"
    assert sent[0]["audience"] == "user"
    assert sent[0]["item_data"]["arguments"] == {}
    assert "arguments" not in sent[0]
    assert "risk_class" not in sent[0]


@pytest.mark.asyncio
async def test_canonical_session_broadcast_preserves_committed_envelope_bytes(monkeypatch) -> None:
    from app.services import web_chat_runtime

    sent: list[dict] = []

    async def capture(_agent_id, _session_id, payload):
        sent.append(payload)

    monkeypatch.setattr(web_chat_runtime.web_chat_broker, "send_session_message", capture)
    envelope = {
        "schema": "hive.session_event",
        "schema_version": 2,
        "event_id": "canonical-1",
        "sequence": 9,
        "tenant_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "scope": {
            "level": "round",
            "session_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "thread_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "turn_id": "turn-1",
            "run_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "round_id": "round-1",
        },
        "item_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        "item_kind": "assistant_text",
        "kind": "assistant_text.delta",
        "lifecycle": "delta",
        "payload_schema": "hive.session.payload.assistant_text.delta.v2",
        "actor": {"type": "assistant"},
        "visibility": {"audience": "direct_user"},
        "payload": {"phase": "unknown", "content": "literal bytes"},
        "content_hash": "f" * 64,
        "occurred_at": "2026-07-16T00:00:00Z",
        "persisted_at": "2026-07-16T00:00:00Z",
    }

    await web_chat_runtime.broadcast_web_chat_event(
        uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        envelope,
    )

    assert sent == [envelope]


def _v2_tool_event(**overrides):
    values = {
        "id": uuid.UUID("44444444-4444-4444-4444-444444444444"),
        "sequence": 20,
        "session_id": uuid.UUID("22222222-2222-2222-2222-222222222222"),
        "run_id": uuid.UUID("33333333-3333-3333-3333-333333333333"),
        "message_id": None,
        "parent_event_id": None,
        "root_session_id": None,
        "parent_session_id": None,
        "schema_version": 2,
        "item_type": "tool_call",
        "item_status": "started",
        "turn_id": "turn-20",
        "causation_id": None,
        "correlation_id": None,
        "actor_type": "tool",
        "event_type": "tool_call.started",
        "visibility_scope": "direct_user",
        "listed_surface": "chat",
        "content": "",
        "parts_json": [],
        # Production shape (hive_weekend_rc_20260826_full_codex_0701 seq20):
        # tool_name lives in v2_payload; user arguments persist only as a hash.
        "metadata_json": {
            "v2_payload": {
                "tool_name": "load_skill",
                "args_hash": "aae0f17dd82871e51be9d8249ca4b9088cd85e46bf4cee0793387652e08c4bb2",
                "invocation_id": "9193ad53-3e25-51fd-91bb-25050032b8de",
                "effect_state": "prepared_not_started",
            },
            "actor": {"type": "tool"},
        },
        "created_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_v2_tool_call_started_projects_running_with_real_tool_name() -> None:
    from app.services.thread_items import build_thread_item

    item = build_thread_item(_v2_tool_event())

    assert item["item_type"] == "tool_call"
    assert item["item_status"] == "running"
    assert item["item_data"]["tool_name"] == "load_skill"
    assert item["item_data"]["tool_call_id"] == "9193ad53-3e25-51fd-91bb-25050032b8de"
    assert item["item_data"]["arguments"] == {}


def test_v2_tool_call_progress_projects_running() -> None:
    from app.services.thread_items import build_thread_item

    item = build_thread_item(
        _v2_tool_event(
            item_status="progress",
            event_type="tool_call.progress",
            metadata_json={"v2_payload": {"invocation_id": "9193ad53-3e25-51fd-91bb-25050032b8de"}},
        )
    )

    assert item["item_status"] == "running"


def test_v2_tool_call_failed_projects_failed() -> None:
    from app.services.thread_items import build_thread_item

    item = build_thread_item(
        _v2_tool_event(
            item_status="failed",
            event_type="tool_call.failed",
            metadata_json={"v2_payload": {"outcome": "failed", "retryable": False}},
        )
    )

    assert item["item_status"] == "failed"


def test_v2_tool_result_completed_with_failed_outcome_projects_failed_not_succeeded() -> None:
    from app.services.thread_items import build_thread_item

    item = build_thread_item(
        _v2_tool_event(
            item_type="tool_result",
            item_status="completed",
            event_type="tool_result.completed",
            content="❌ Skill not found: deep_research",
            metadata_json={
                "v2_payload": {
                    "outcome": "failed",
                    "content": "❌ Skill not found: deep_research",
                    "invocation_id": "9193ad53-3e25-51fd-91bb-25050032b8de",
                }
            },
        )
    )

    assert item["item_type"] == "tool_result"
    assert item["item_status"] == "failed"
    assert item["item_data"]["success"] is False
    # The real production result payload (seq23) carries no tool_name — the
    # typed call row (seq20) owns it; the result pairs with its call through
    # the invocation id surfaced as tool_call_id.
    assert "tool_name" not in item["item_data"]
    assert item["item_data"]["tool_call_id"] == "9193ad53-3e25-51fd-91bb-25050032b8de"
    assert "未完成" in item["user_summary"]


def test_v2_tool_result_completed_success_projects_truthfully() -> None:
    from app.services.thread_items import build_thread_item

    item = build_thread_item(
        _v2_tool_event(
            item_type="tool_result",
            item_status="completed",
            event_type="tool_result.completed",
            content="Skill loaded.",
            metadata_json={
                "v2_payload": {
                    "outcome": "success",
                    "content": "Skill loaded.",
                    "invocation_id": "9193ad53-3e25-51fd-91bb-25050032b8de",
                }
            },
        )
    )

    assert item["item_status"] == "succeeded"
    assert item["item_data"]["success"] is True


def test_v2_user_projection_keeps_tool_name_but_redacts_internal_payload() -> None:
    from app.services.thread_items import build_thread_item

    item = build_thread_item(_v2_tool_event(), audience="user")

    assert item["metadata"] == {"status": "running"}
    assert not item.get("operator_details")
    assert item["user_summary"] == "正在使用：Load skill"
    serialized = json.dumps(item, ensure_ascii=False)
    assert "args_hash" not in serialized
    assert "invocation_id" not in serialized
    assert "prepared_not_started" not in serialized


@pytest.mark.parametrize(
    ("lifecycle", "expected_status"),
    [
        ("denied", "failed"),
        ("unavailable", "failed"),
        ("waiting", "waiting_user"),
    ],
)
def test_v2_tool_call_denied_unavailable_waiting_lifecycles_project_truthfully(lifecycle, expected_status) -> None:
    from app.services.thread_items import build_thread_item

    item = build_thread_item(
        _v2_tool_event(
            item_status=lifecycle,
            event_type=f"tool_call.{lifecycle}",
            metadata_json={"v2_payload": {"invocation_id": "9193ad53-3e25-51fd-91bb-25050032b8de"}},
        )
    )

    assert item["item_status"] == expected_status


@pytest.mark.parametrize("outcome", ["denied", "unavailable", "blocked"])
def test_v2_tool_result_completed_with_non_success_outcome_projects_failed(outcome) -> None:
    from app.services.thread_items import build_thread_item

    item = build_thread_item(
        _v2_tool_event(
            item_type="tool_result",
            item_status="completed",
            event_type="tool_result.completed",
            content=f"tool {outcome}",
            metadata_json={"v2_payload": {"outcome": outcome, "content": f"tool {outcome}"}},
        )
    )

    assert item["item_status"] == "failed"
    assert item["item_data"]["success"] is False


def test_v2_tool_result_waiting_outcome_projects_waiting_user() -> None:
    from app.services.thread_items import build_thread_item

    item = build_thread_item(
        _v2_tool_event(
            item_type="tool_result",
            item_status="waiting",
            event_type="tool_result.waiting",
            metadata_json={"v2_payload": {"invocation_id": "9193ad53-3e25-51fd-91bb-25050032b8de"}},
        )
    )

    assert item["item_status"] == "waiting_user"


def test_v2_tool_result_absent_outcome_keeps_legacy_fallback() -> None:
    from app.services.thread_items import build_thread_item

    item = build_thread_item(
        _v2_tool_event(
            item_type="tool_result",
            item_status="completed",
            event_type="tool_result.completed",
            content="ok",
            metadata_json={"v2_payload": {"content": "ok"}},
        )
    )

    assert item["item_status"] == "succeeded"
    assert item["item_data"]["success"] is True


@pytest.mark.parametrize(
    ("lifecycle", "expected_status"),
    [
        ("queued", "running"),
        ("needs_reconciliation", "failed"),
        ("reconciled", "succeeded"),
    ],
)
def test_v2_tool_exec_lifecycle_projects_truthfully(lifecycle, expected_status) -> None:
    from app.services.thread_items import build_thread_item

    item = build_thread_item(
        _v2_tool_event(
            item_status=lifecycle,
            event_type=f"tool_call.{lifecycle}",
            metadata_json={"v2_payload": {"invocation_id": "9193ad53-3e25-51fd-91bb-25050032b8de"}},
        )
    )

    assert item["item_status"] == expected_status


def test_v2_outcome_never_overrides_non_tool_item_lifecycle() -> None:
    from app.services.thread_items import build_thread_item

    item = build_thread_item(
        _v2_tool_event(
            item_type="assistant_final",
            item_status="completed",
            event_type="assistant_final.completed",
            content="final answer",
            metadata_json={"v2_payload": {"phase": "final", "outcome": "failed"}},
        )
    )

    assert item["item_status"] == "succeeded"

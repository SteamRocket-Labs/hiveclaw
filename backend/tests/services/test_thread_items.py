from __future__ import annotations

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
        "context_compaction",
        "artifact",
        "boundary",
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


@pytest.mark.parametrize(
    ("event_type", "role", "expected"),
    [
        ("thinking", "assistant", "reasoning"),
        ("workflow_failed", "system", "workflow_activity"),
        ("subagent_task_started", "system", "subagent_activity"),
        ("session_compact", "system", "context_compaction"),
        ("artifact_delivery", "system", "artifact"),
        ("run_cancelled", "system", "boundary"),
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
        ("subagent_activity", "member_run_started", "running"),
        ("boundary", "run_cancelled", "cancelled"),
        ("approval_request", "permission_request", "waiting_user"),
    ],
)
def test_thread_item_status_matches_the_historical_backfill(
    item_type: str,
    event_type: str,
    expected: str,
) -> None:
    from app.services.thread_items import classify_thread_item_status

    assert classify_thread_item_status(item_type=item_type, event_type=event_type, metadata={}) == expected


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

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


def _scope(*, level: str = "round") -> dict[str, str]:
    value = {
        "level": level,
        "session_id": "session-1",
        "thread_id": "session-1",
    }
    if level in {"turn", "run", "round"}:
        value["turn_id"] = "turn-1"
    if level in {"run", "round"}:
        value["run_id"] = "run-1"
    if level == "round":
        value["round_id"] = "round-1"
    return value


def _event(sequence: int = 1, **overrides):
    lifecycle = overrides.pop("lifecycle", "started")
    item_kind = overrides.pop("item_kind", "assistant_text")
    scope = overrides.get("scope", _scope())
    defaults = {
        "schema": "hive.session_event",
        "schema_version": 2,
        "event_id": f"event-{sequence}",
        "sequence": sequence,
        "ordinal": sequence - 1,
        "tenant_id": "tenant-1",
        "scope": scope,
        **({"run_id": scope["run_id"]} if scope.get("level") in {"run", "round"} else {}),
        "item_id": "item-1",
        "item_kind": item_kind,
        "kind": f"{item_kind}.{lifecycle}",
        "lifecycle": lifecycle,
        "payload_schema": f"hive.session.payload.{item_kind}.{lifecycle}.v2",
        "actor": {"type": "assistant"},
        "visibility": {"audience": "direct_user"},
        "payload": {"content": ""},
        "occurred_at": "2026-07-16T00:00:00Z",
        "persisted_at": "2026-07-16T00:00:00Z",
    }
    defaults.update(overrides)
    return defaults


def _legacy_row(**overrides):
    event_id = uuid.uuid4()
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    defaults = {
        "id": event_id,
        "schema_version": 1,
        "sequence": 7,
        "tenant_id": uuid.uuid4(),
        "session_id": session_id,
        "run_id": run_id,
        "turn_id": f"turn-{run_id.hex}",
        "actor_type": "assistant",
        "event_type": "assistant_delta",
        "item_type": "agent_message",
        "item_status": "running",
        "visibility_scope": "direct_user",
        "content": "The literal word failed is ordinary model-authored content.",
        "parts_json": [{"type": "text", "text": "payload"}],
        "metadata_json": {
            "role": "assistant",
            "round_id": f"round-{run_id.hex}-1",
            "provider_private_reasoning": "must not cross the user projection",
            "tool_call_id": "call-1",
            "arguments": {"token": "operator-only"},
            "visibility": {
                "redaction_paths": [
                    "/payload/metadata/provider_private_reasoning",
                    "/payload/metadata/arguments",
                ]
            },
        },
        "created_at": datetime(2026, 7, 16, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_exact_event_matrix_accepts_only_canonical_kind_scope_and_payload_schema() -> None:
    from app.services.session_event_contract import (
        EVENT_KIND_MATRIX,
        SessionEventContractError,
        validate_session_event,
    )

    valid = _event()
    assert validate_session_event(valid) is valid
    assert "result_commit" in EVENT_KIND_MATRIX
    assert "model_result" not in EVENT_KIND_MATRIX

    for invalid in (
        {**valid, "kind": "assistant.final"},
        {**valid, "payload_schema": "hive.session.payload.assistant.final.v2"},
        {**valid, "scope": _scope(level="run")},
        _event(item_kind="tool_result", lifecycle="failed"),
        _event(item_kind="session", lifecycle="completed", scope=_scope(level="session")),
    ):
        with pytest.raises(SessionEventContractError):
            validate_session_event(invalid)


def test_top_level_run_is_an_exact_projection_of_run_or_round_scope() -> None:
    from app.services.session_event_contract import SessionEventContractError, validate_session_event

    assert validate_session_event(_event())["run_id"] == "run-1"
    assert "run_id" not in validate_session_event(
        _event(item_kind="human_input", lifecycle="accepted", scope=_scope(level="session"))
    )

    for invalid in (
        _event(run_id="other-run"),
        _event(run_id=None),
        _event(
            item_kind="human_input",
            lifecycle="accepted",
            scope=_scope(level="session"),
            run_id="secret-top-level-run",
        ),
    ):
        with pytest.raises(SessionEventContractError, match="run_id"):
            validate_session_event(invalid)


def test_hook_boundary_matrix_rejects_cross_boundary_lifecycle_and_scope() -> None:
    from app.services.session_event_contract import SessionEventContractError, validate_session_event

    stop_blocked = _event(
        item_kind="hook",
        lifecycle="blocked",
        scope=_scope(level="run"),
        payload={"boundary": "Stop", "failure_policy": "continue_iteration"},
    )
    assert validate_session_event(stop_blocked) is stop_blocked

    for invalid in (
        _event(
            item_kind="hook",
            lifecycle="denied",
            scope=_scope(level="run"),
            payload={"boundary": "Stop", "failure_policy": "continue"},
        ),
        _event(
            item_kind="hook",
            lifecycle="blocked",
            scope=_scope(level="session"),
            payload={"boundary": "SessionStart", "source": "startup", "failure_policy": "continue"},
        ),
        _event(
            item_kind="hook",
            lifecycle="prevented",
            scope=_scope(level="run"),
            payload={"boundary": "PostCompact", "source": "auto", "failure_policy": "continue"},
        ),
        _event(
            item_kind="hook",
            lifecycle="started",
            scope=_scope(level="run"),
            payload={"boundary": "Stop", "source": "startup", "failure_policy": "continue"},
        ),
    ):
        with pytest.raises(SessionEventContractError):
            validate_session_event(invalid)


def test_checked_in_typescript_contract_is_an_exact_python_generated_artifact() -> None:
    from app.services.session_event_contract import render_session_event_contract_typescript

    generated = (
        Path(__file__).resolve().parents[3]
        / "frontend"
        / "src"
        / "pages"
        / "session-workbench"
        / "sessionEventContract.generated.ts"
    )
    assert generated.read_text(encoding="utf-8") == render_session_event_contract_typescript()


def test_legacy_adapter_is_explicit_byte_faithful_and_never_reads_language_for_state() -> None:
    from app.services.session_event_contract import serialize_session_event

    event = serialize_session_event(_legacy_row(), audience="user")

    assert event["kind"] == "assistant_text.delta"
    assert event["item_kind"] == "assistant_text"
    assert event["lifecycle"] == "delta"
    assert event["scope"]["level"] == "round"
    assert event["payload"]["content"] == "The literal word failed is ordinary model-authored content."
    assert event["payload"]["phase"] == "unknown"
    assert "provider_private_reasoning" not in event["payload"]["metadata"]
    assert event["payload"]["metadata"]["tool_call_id"] == "call-1"
    assert "arguments" not in event["payload"]["metadata"]


@pytest.mark.parametrize(
    ("event_type", "item_status", "envelope", "expected_lifecycle"),
    [
        (
            "tool_call",
            "running",
            {
                "name": "report_progress",
                "args": {"message": "Public progress survives canonical replay."},
                "status": "running",
                "tool_call_id": "progress-call-1",
            },
            "started",
        ),
        (
            "tool_result",
            "succeeded",
            {
                "name": "report_progress",
                "args": {"message": "Public progress survives canonical replay."},
                "status": "done",
                "result": '{"acknowledged":true,"ok":true}',
                "tool_call_id": "progress-call-1",
            },
            "completed",
        ),
    ],
)
def test_legacy_tool_envelope_is_lifted_without_rewriting_model_authored_arguments(
    event_type: str,
    item_status: str,
    envelope: dict[str, object],
    expected_lifecycle: str,
) -> None:
    from app.services.session_event_contract import serialize_session_event

    raw_content = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
    event = serialize_session_event(
        _legacy_row(
            actor_type="tool",
            event_type=event_type,
            item_type=event_type,
            item_status=item_status,
            content=raw_content,
            parts_json=[],
            metadata_json={
                "role": "tool_call",
                "round_id": f"round-{uuid.uuid4().hex}-1",
                "tool_name": "report_progress",
                "tool_call_id": "progress-call-1",
            },
        ),
        audience="user",
    )

    assert event["item_kind"] == event_type
    assert event["lifecycle"] == expected_lifecycle
    assert event["payload"]["content"] == raw_content
    assert event["payload"]["tool_name"] == "report_progress"
    assert event["payload"]["args"] == envelope["args"]
    assert event["payload"]["status"] == envelope["status"]
    assert event["payload"]["tool_call_id"] == "progress-call-1"
    if event_type == "tool_result":
        assert event["payload"]["result"] == envelope["result"]


def test_reducer_keeps_one_item_and_terminal_semantic_outcome_is_monotonic() -> None:
    from app.services.session_event_contract import reduce_session_events

    events = [
        _event(1),
        _event(2, lifecycle="delta", payload={"content": "hello "}),
        _event(3, lifecycle="completed", payload={"content": "world"}),
        _event(4, lifecycle="failed", payload={"content": "ignored"}),
    ]

    state = reduce_session_events(events)

    assert list(state.items) == ["item-1"]
    item = state.items["item-1"]
    assert item.lifecycle == "completed"
    assert item.terminal is True
    assert item.content == "hello world"
    assert item.first_sequence == 1
    assert item.last_sequence == 3
    assert state.ignored_event_ids == ("event-4",)


def test_assistant_text_phase_is_unknown_and_final_is_zero_copy_reference_only() -> None:
    from app.services.session_event_contract import reduce_session_events

    source = _event(
        1,
        lifecycle="completed",
        payload={"content": "original bytes", "phase": "unknown", "block_index": 0},
        content_hash="a" * 64,
    )
    final = _event(
        2,
        item_kind="assistant_final",
        lifecycle="completed",
        item_id="final-1",
        result_id="result-1",
        payload={
            "render_owner_id": "render-1",
            "source_blocks": [{"item_id": "item-1", "block_index": 0, "content_hash": "a" * 64}],
            "result_id": "result-1",
            "result_content_hash": "b" * 64,
        },
    )

    state = reduce_session_events([source, final])

    assert state.items["item-1"].assistant_phase == "unknown"
    assert state.items["item-1"].content == "original bytes"
    assert state.items["final-1"].content == ""
    assert state.items["final-1"].source_blocks[0]["item_id"] == "item-1"


def test_v2_orm_row_round_trips_exact_columns_without_legacy_reclassification() -> None:
    from app.services.session_event_contract import serialize_session_event

    event_id = uuid.uuid4()
    session_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    item_id = uuid.uuid4()
    occurred_at = datetime(2026, 7, 16, 1, 2, 3, tzinfo=timezone.utc)
    row = SimpleNamespace(
        id=event_id,
        schema_version=2,
        sequence=11,
        tenant_id=tenant_id,
        session_id=session_id,
        run_id=None,
        item_id=item_id,
        item_kind="human_input",
        lifecycle="accepted",
        payload_schema="hive.session.payload.human_input.accepted.v2",
        scope_json={"level": "session", "session_id": str(session_id), "thread_id": str(session_id)},
        event_type="human_input.accepted",
        actor_type="user",
        visibility_scope="direct_user",
        content="",
        parts_json=None,
        metadata_json={
            "v2_payload": {"input_id": "input-11", "content_hash": "a" * 64},
            "actor": {"type": "user", "id": "principal-11"},
            "visibility": {"audience": "direct_user"},
            "display": {"summary": "accepted"},
            "evidence_refs": [{"type": "receipt", "id": "receipt-11"}],
            "v2_persisted_at": occurred_at.isoformat(),
        },
        ordinal=None,
        command_id=uuid.uuid4(),
        input_id=uuid.uuid4(),
        result_id=None,
        invocation_id=None,
        provider_tool_use_id=None,
        content_hash="b" * 64,
        parent_item_id=None,
        parent_event_id=None,
        correlation_id=None,
        created_at=occurred_at,
    )

    event = serialize_session_event(row)

    assert event["schema_version"] == 2
    assert event["event_id"] == str(event_id)
    assert event["item_id"] == str(item_id)
    assert event["item_kind"] == "human_input"
    assert event["lifecycle"] == "accepted"
    assert event["scope"] == row.scope_json
    assert event["payload"] == row.metadata_json["v2_payload"]
    assert event["actor"] == row.metadata_json["actor"]
    assert event["display"] == row.metadata_json["display"]
    assert event["evidence_refs"] == row.metadata_json["evidence_refs"]
    assert event["persisted_at"] == occurred_at.isoformat()
    assert "legacy" not in event["payload"]


def test_explicit_json_pointer_redaction_is_identical_for_canonical_dict_and_orm_row() -> None:
    from app.services.session_event_contract import serialize_session_event

    payload = {
        "metadata": {
            "nested": {
                "token": "secret-token",
                "remove_me": "authority-selected",
                "token_count": 17,
                "note": "The words token, secret and credential are ordinary content here.",
            },
            "batches": [
                {
                    "secret": "byte-faithful-key-name",
                    "private": "authority-selected",
                    "summary": "secret is mentioned but this value is safe",
                },
                {"credentials": {"username": "preserve"}, "credential_hint": "keep"},
            ],
        },
        "execution_envelope": {"raw": "preserve", "args": {"token": "preserve"}},
        "content": "Do not scan this sentence for token or credential words.",
    }
    redaction_paths = [
        "/payload/metadata/nested/remove_me",
        "/payload/metadata/batches/0/private",
    ]
    canonical = _event(
        lifecycle="completed",
        payload=payload,
        visibility={"audience": "direct_user", "redaction_paths": redaction_paths},
    )

    operator = serialize_session_event(canonical, audience="operator")
    user = serialize_session_event(canonical, audience="user")

    assert operator["payload"] == payload
    assert user["payload"]["content"] == payload["content"]
    assert user["payload"]["metadata"]["nested"]["token"] == "secret-token"
    assert user["payload"]["metadata"]["nested"]["token_count"] == 17
    assert user["payload"]["metadata"]["nested"]["note"] == payload["metadata"]["nested"]["note"]
    assert "remove_me" not in user["payload"]["metadata"]["nested"]
    assert user["payload"]["metadata"]["batches"][0]["secret"] == "byte-faithful-key-name"
    assert "private" not in user["payload"]["metadata"]["batches"][0]
    assert user["payload"]["metadata"]["batches"][1]["credentials"] == {"username": "preserve"}
    assert user["payload"]["execution_envelope"] == payload["execution_envelope"]
    assert set(user["visibility"]["redacted_fields"]) == {
        "/payload/metadata/batches/0/private",
        "/payload/metadata/nested/remove_me",
    }

    row = SimpleNamespace(
        id=uuid.uuid4(),
        schema_version=2,
        sequence=21,
        tenant_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        item_kind="assistant_text",
        lifecycle="completed",
        payload_schema="hive.session.payload.assistant_text.completed.v2",
        scope_json=_scope(),
        event_type="assistant_text.completed",
        actor_type="assistant",
        visibility_scope="direct_user",
        metadata_json={
            "v2_payload": payload,
            "actor": {"type": "assistant"},
            "visibility": {
                "audience": "direct_user",
                "redaction_paths": redaction_paths,
            },
        },
        ordinal=None,
        command_id=None,
        input_id=None,
        result_id=None,
        invocation_id=None,
        provider_tool_use_id=None,
        content_hash=None,
        parent_item_id=None,
        parent_event_id=None,
        correlation_id=None,
        created_at=datetime(2026, 7, 16, 1, 2, 3, tzinfo=timezone.utc),
    )
    row.scope_json = {
        **row.scope_json,
        "session_id": str(row.session_id),
        "thread_id": str(row.session_id),
        "run_id": str(row.run_id),
    }
    orm_user = serialize_session_event(row, audience="user")
    assert orm_user["payload"] == user["payload"]
    assert set(orm_user["visibility"]["redacted_fields"]) == set(user["visibility"]["redacted_fields"])


@pytest.mark.parametrize(
    "paths",
    [
        "not-an-array",
        ["payload/metadata/private"],
        ["/payload/metadata/bad~2escape"],
        ["/payload/metadata/missing"],
        ["/payload/items/2/private"],
        ["/payload/items/0"],
        ["/payload/metadata", "/payload/metadata/private"],
    ],
)
def test_redaction_pointer_schema_and_bounds_are_rejected(paths) -> None:
    from app.services.session_event_contract import SessionEventContractError, serialize_session_event

    event = _event(
        lifecycle="completed",
        payload={"metadata": {"private": "value"}, "items": [{"private": "value"}]},
        visibility={"audience": "direct_user", "redaction_paths": paths},
    )
    with pytest.raises(SessionEventContractError, match="redaction"):
        serialize_session_event(event, audience="user")


def test_unknown_or_unscoped_legacy_rows_return_compatibility_evidence_not_fake_v2() -> None:
    from app.services.session_event_contract import serialize_session_event

    unknown = serialize_session_event(
        _legacy_row(
            event_type="vendor_magic",
            item_type="vendor_magic",
            item_status="mystery",
            run_id=None,
            turn_id=None,
            metadata_json={},
        )
    )
    assert unknown["schema"] == "hive.session_event_compatibility"
    assert unknown["compatibility_status"] == "needs_reconciliation"
    assert unknown["legacy_kind"] == "legacy_unknown"
    assert "item_kind" not in unknown
    assert "lifecycle" not in unknown
    assert "scope" not in unknown
    assert "legacy-turn-" not in repr(unknown)
    assert "legacy-run-" not in repr(unknown)
    assert "legacy-round-" not in repr(unknown)

    unscoped_assistant = serialize_session_event(
        _legacy_row(run_id=None, turn_id=None, metadata_json={"phase": "final"})
    )
    assert unscoped_assistant["schema"] == "hive.session_event_compatibility"
    assert unscoped_assistant["compatibility_status"] == "needs_reconciliation"
    assert unscoped_assistant["legacy_kind"] == "assistant_final"


def test_legacy_multi_scope_kind_uses_only_the_most_specific_scope_with_real_ids() -> None:
    from app.services.session_event_contract import serialize_session_event

    run_scoped = serialize_session_event(
        _legacy_row(
            event_type="artifact",
            item_type="artifact",
            item_status="completed",
            metadata_json={"lifecycle": "delivered"},
        )
    )
    assert run_scoped["schema"] == "hive.session_event"
    assert run_scoped["scope"]["level"] == "run"
    assert run_scoped["scope"]["turn_id"]
    assert run_scoped["scope"]["run_id"]

    source = _legacy_row(
        event_type="artifact",
        item_type="artifact",
        item_status="completed",
        metadata_json={"lifecycle": "delivered", "round_id": "round-artifact"},
    )
    round_scoped = serialize_session_event(source)
    assert round_scoped["scope"]["level"] == "round"
    assert round_scoped["scope"]["round_id"] == source.metadata_json["round_id"]

    unprovable = serialize_session_event(
        _legacy_row(
            event_type="artifact",
            item_type="artifact",
            item_status="completed",
            run_id=None,
            turn_id=None,
            metadata_json={"lifecycle": "delivered"},
        )
    )
    assert unprovable["schema"] == "hive.session_event_compatibility"
    assert unprovable["reason"] == "insufficient_legacy_scope"


def test_legacy_session_scope_preserves_run_evidence_without_forging_v2_run_authority() -> None:
    from app.services.session_event_contract import serialize_session_event

    source = _legacy_row(
        event_type="user_message",
        item_type="user_message",
        item_status="succeeded",
        actor_type="user",
        metadata_json={},
    )

    event = serialize_session_event(source)

    assert event["schema"] == "hive.session_event"
    assert event["item_kind"] == "human_input"
    assert event["scope"]["level"] == "session"
    assert "run_id" not in event
    assert event["payload"]["legacy_run_id"] == str(source.run_id)

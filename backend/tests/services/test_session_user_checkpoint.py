from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from uuid import uuid4

from app.services.session_user_checkpoint import (
    event_role,
    is_human_input_checkpoint,
    is_human_input_row,
    user_checkpoint_content,
    user_checkpoint_events,
)


def _v2_input_event(
    *,
    sequence: int,
    lifecycle: str = "accepted",
    content_parts: list[dict] | None = None,
    item_id: uuid.UUID | None = None,
):
    parts = content_parts if content_parts is not None else [{"type": "text", "text": f"prompt {sequence}"}]
    resolved_item = item_id or uuid4()
    return SimpleNamespace(
        id=uuid4(),
        sequence=sequence,
        item_id=resolved_item,
        item_kind="human_input",
        lifecycle=lifecycle,
        event_type=f"human_input.{lifecycle}",
        content="",
        metadata_json={
            "v2_payload": {"input_id": str(resolved_item), "content_parts": parts},
            "actor": {"type": "user"},
        },
    )


def _legacy_user_event(*, sequence: int, content: str = "legacy prompt", role: str = "user"):
    return SimpleNamespace(
        id=uuid4(),
        sequence=sequence,
        item_id=None,
        item_kind=None,
        lifecycle=None,
        event_type="user_message",
        content=content,
        metadata_json={"role": role},
    )


def test_single_text_part_renders_exact_bytes() -> None:
    event = _v2_input_event(sequence=1, content_parts=[{"type": "text", "text": "  exact  bytes \n"}])

    assert user_checkpoint_content(event) == "  exact  bytes \n"


def test_multi_part_content_renders_deterministic_canonical_json() -> None:
    parts = [{"type": "text", "text": "a"}, {"type": "attachment_ref", "attachment": {"name": "f.md"}}]
    event = _v2_input_event(sequence=1, content_parts=parts)
    expected = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    assert user_checkpoint_content(event) == expected


def test_lifecycle_state_facts_are_not_user_checkpoints() -> None:
    for lifecycle in ("queued", "bound", "applied", "cancelled", "rejected"):
        event = _v2_input_event(sequence=1, lifecycle=lifecycle)

        assert is_human_input_checkpoint(event) is False
        assert event_role(event) is None


def test_accepted_and_revised_are_user_checkpoints() -> None:
    accepted = _v2_input_event(sequence=1, lifecycle="accepted")
    revised = _v2_input_event(sequence=2, lifecycle="revised")

    assert event_role(accepted) == "user"
    assert event_role(revised) == "user"
    assert is_human_input_checkpoint(accepted) is True
    assert is_human_input_checkpoint(revised) is True


def test_assistant_final_completed_maps_to_assistant_role() -> None:
    event = SimpleNamespace(
        id=uuid4(),
        sequence=9,
        item_id=uuid4(),
        item_kind="assistant_final",
        lifecycle="completed",
        event_type="assistant_final.completed",
        content="",
        metadata_json={"v2_payload": {"phase": "final"}},
    )

    assert event_role(event) == "assistant"


def test_kind_string_rows_without_item_columns_are_detected() -> None:
    event = SimpleNamespace(
        id=uuid4(),
        sequence=2,
        item_id=None,
        item_kind=None,
        lifecycle=None,
        event_type="human_input.revised",
        content="",
        metadata={"v2_payload": {"content_parts": [{"type": "text", "text": "via kind string"}]}},
    )

    assert is_human_input_checkpoint(event) is True
    assert user_checkpoint_content(event) == "via kind string"


def test_any_human_input_lifecycle_is_a_human_input_row() -> None:
    assert is_human_input_row(_v2_input_event(sequence=1, lifecycle="queued")) is True
    assert is_human_input_row(_legacy_user_event(sequence=2)) is False


def test_user_checkpoint_events_dedup_to_latest_revision_per_item() -> None:
    item = uuid4()
    accepted = _v2_input_event(
        sequence=1, lifecycle="accepted", content_parts=[{"type": "text", "text": "first draft"}], item_id=item
    )
    revised = _v2_input_event(
        sequence=5, lifecycle="revised", content_parts=[{"type": "text", "text": "first revised"}], item_id=item
    )
    queued = _v2_input_event(sequence=8, lifecycle="queued", item_id=item)
    other = _v2_input_event(sequence=30, content_parts=[{"type": "text", "text": "second prompt"}])

    checkpoints = user_checkpoint_events([accepted, queued, revised, other])

    assert checkpoints == [revised, other]
    assert user_checkpoint_content(checkpoints[0]) == "first revised"


def test_legacy_user_message_rows_remain_checkpoints_in_order() -> None:
    first = _legacy_user_event(sequence=1, content="one")
    second = _legacy_user_event(sequence=3, content="two")

    assert user_checkpoint_events([second, first]) == [first, second]


def test_empty_content_rows_are_not_checkpoints() -> None:
    empty_legacy = _legacy_user_event(sequence=1, content="   ")
    empty_v2 = _v2_input_event(sequence=2, content_parts=[{"type": "text", "text": ""}])

    assert user_checkpoint_events([empty_legacy, empty_v2]) == []


def _t0_shaped_row(
    *,
    sequence: int,
    event_id: str,
    event_type: str,
    content: str,
    metadata: dict | None = None,
    role: str | None = None,
):
    """T0SessionEvent shape: event_id (no id), no item_id column, top-level role, plain metadata."""
    return SimpleNamespace(
        event_id=event_id,
        sequence=sequence,
        event_type=event_type,
        role=role,
        content=content,
        metadata=metadata or {},
    )


def test_t0_shaped_rows_keep_distinct_checkpoints_with_revision_dedupe() -> None:
    item_one = uuid4()
    item_two = uuid4()
    accepted_one = _t0_shaped_row(
        sequence=1,
        event_id="e1-accepted",
        event_type="human_input.accepted",
        content="",
        metadata={
            "v2_payload": {"input_id": str(item_one), "content_parts": [{"type": "text", "text": "first draft"}]}
        },
    )
    revised_one = _t0_shaped_row(
        sequence=2,
        event_id="e2-revised",
        event_type="human_input.revised",
        content="",
        metadata={
            "v2_payload": {"input_id": str(item_one), "content_parts": [{"type": "text", "text": "first revised"}]}
        },
    )
    accepted_two = _t0_shaped_row(
        sequence=3,
        event_id="e3-accepted",
        event_type="human_input.accepted",
        content="",
        metadata={
            "v2_payload": {"input_id": str(item_two), "content_parts": [{"type": "text", "text": "second prompt"}]}
        },
    )
    legacy_four = _t0_shaped_row(sequence=4, event_id="e4-user", event_type="user_message", content="legacy four")
    legacy_five = _t0_shaped_row(sequence=5, event_id="e5-user", event_type="user_message", content="legacy five")

    checkpoints = user_checkpoint_events([accepted_one, revised_one, accepted_two, legacy_four, legacy_five])

    assert [getattr(event, "event_id") for event in checkpoints] == [
        "e2-revised",
        "e3-accepted",
        "e4-user",
        "e5-user",
    ]
    assert user_checkpoint_content(checkpoints[0]) == "first revised"


def test_v2_state_fact_row_never_leaks_as_checkpoint_even_with_stray_role_metadata() -> None:
    queued_with_role = _v2_input_event(sequence=1, lifecycle="queued")
    queued_with_role.metadata_json["role"] = "user"
    queued_with_role.content = "stray inline content"

    legacy = _legacy_user_event(sequence=2, content="real legacy")

    assert event_role(queued_with_role) is None
    assert user_checkpoint_events([queued_with_role, legacy]) == [legacy]


def test_typed_human_input_checkpoint_wins_over_contradictory_role_metadata() -> None:
    accepted = _v2_input_event(sequence=1, lifecycle="accepted")
    accepted.metadata_json["role"] = "assistant"

    assert event_role(accepted) == "user"


def test_typed_assistant_final_wins_over_contradictory_role_metadata() -> None:
    final = SimpleNamespace(
        id=uuid4(),
        sequence=9,
        item_id=uuid4(),
        item_kind="assistant_final",
        lifecycle="completed",
        event_type="assistant_final.completed",
        content="",
        metadata_json={"v2_payload": {"phase": "final"}, "role": "user"},
    )

    assert event_role(final) == "assistant"


def test_single_part_with_content_key_renders_exact_bytes() -> None:
    event = _v2_input_event(sequence=1, content_parts=[{"type": "text", "content": "exact-api-bytes"}])

    assert user_checkpoint_content(event) == "exact-api-bytes"


def test_text_key_wins_when_both_present_in_single_part() -> None:
    event = _v2_input_event(sequence=1, content_parts=[{"type": "text", "text": "via text", "content": "via content"}])

    assert user_checkpoint_content(event) == "via text"


def test_single_part_without_string_text_or_content_stays_canonical_json() -> None:
    parts = [{"type": "attachment_ref", "attachment": {"name": "f.md"}}]
    event = _v2_input_event(sequence=1, content_parts=parts)

    assert user_checkpoint_content(event) == json.dumps(
        parts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )

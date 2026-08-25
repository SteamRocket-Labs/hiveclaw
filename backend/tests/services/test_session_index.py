from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.services.session_index import build_session_index


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        title="Session",
        source_channel="web",
        runtime_source="web_chat",
        listed_surface="chat",
        visibility_scope="direct_user",
        session_kind="human_chat",
        parent_session_id=None,
        root_session_id=None,
        runtime_task_id=None,
        transcript_metadata_json={},
    )


def _v2_input_row(*, sequence: int, item_id, lifecycle: str = "accepted", text: str):
    return SimpleNamespace(
        id=uuid4(),
        sequence=sequence,
        item_id=item_id,
        item_kind="human_input",
        lifecycle=lifecycle,
        event_type=f"human_input.{lifecycle}",
        content="",
        metadata_json={
            "v2_payload": {"input_id": str(item_id), "content_parts": [{"type": "text", "text": text}]},
            "actor": {"type": "user"},
        },
    )


def _legacy_user_row(*, sequence: int, content: str):
    return SimpleNamespace(
        id=uuid4(),
        sequence=sequence,
        item_id=None,
        item_kind=None,
        lifecycle=None,
        event_type="user_message",
        content=content,
        metadata_json={"role": "user"},
    )


def test_checkpoints_include_v2_human_input_deduped_and_legacy_user_messages() -> None:
    item = uuid4()
    accepted = _v2_input_row(sequence=1, item_id=item, text="draft one")
    revised = _v2_input_row(sequence=2, item_id=item, lifecycle="revised", text="revised one")
    queued = _v2_input_row(sequence=3, item_id=item, lifecycle="queued", text="revised one")
    legacy = _legacy_user_row(sequence=4, content="legacy hello")

    index = build_session_index(
        session=_session(),
        transcript_events=[accepted, revised, queued, legacy],
    )

    assert [checkpoint["checkpoint_event_id"] for checkpoint in index["checkpoints"]] == [
        str(revised.id),
        str(legacy.id),
    ]
    assert [checkpoint["content_preview"] for checkpoint in index["checkpoints"]] == [
        "revised one",
        "legacy hello",
    ]
    assert [checkpoint["sequence"] for checkpoint in index["checkpoints"]] == [2, 4]
    assert index["resume_health"]["has_checkpoints"] is True

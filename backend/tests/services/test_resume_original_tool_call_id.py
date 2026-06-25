"""D-04 read side: resume reuses the ORIGINAL streamed tool_call_id.

On resume, ``conversation_from_history_messages`` rebuilds the provider
conversation that is fed back into the kernel loop
(``web_chat_runtime.py`` and ``api/websocket.py``). The rebuilt
``tool_calls[].id`` MUST equal the id the provider streamed, so the
matching ``role="tool"`` result message pairs correctly. The kernel's
runtime done-payload persists that original id inside the
``content_replacement`` record (``content_replacement.tool_call_id``) even
when no top-level ``tool_call_id`` is present. These tests pin that the read
side recovers the original id from the persisted payload instead of
synthesizing ``call_{msg.id}``.

Revert-sensitive: if the wiring reverts to
``tc_data.get("tool_call_id") or f"call_{msg.id}"`` the
``content_replacement``-only case below loses the original id and these
assertions fail.
"""

import json
from types import SimpleNamespace

from app.services.web_chat_runtime import conversation_from_history_messages


def _tool_call_row(payload: dict, *, msg_id: str) -> SimpleNamespace:
    return SimpleNamespace(role="tool_call", id=msg_id, content=json.dumps(payload))


def test_resume_reuses_original_tool_call_id_from_content_replacement():
    """Production shape: original id lives ONLY inside content_replacement.

    This mirrors the kernel done_payload, which persists the streamed
    tool_call_id nested in the content_replacement record but does NOT
    write a top-level tool_call_id for legacy/runtime rows. The resume read
    side must recover ``toolu_streamed_42`` rather than ``call_<msg.id>``.
    """
    payload = {
        "name": "read_file",
        "args": {"path": "big.txt"},
        "status": "done",
        "result": "R" * 60_000,
        # No top-level "tool_call_id" — exactly what the kernel emits today.
        "content_replacement": {
            "schema": "content_replacement_record.v1",
            "tool_call_id": "toolu_streamed_42",
            "inline_content": "MODEL-SEEN-BYTES",
            "original_chars": 60_000,
            "inline_chars": 16,
        },
    }
    history = [_tool_call_row(payload, msg_id="db-message-id-1")]

    conversation = conversation_from_history_messages(history)

    assistant_msg, tool_msg = conversation
    # Reuses the ORIGINAL streamed id, NOT call_{msg.id}.
    assert assistant_msg["tool_calls"][0]["id"] == "toolu_streamed_42"
    assert tool_msg["tool_call_id"] == "toolu_streamed_42"
    # The assistant tool_call id and its tool result id must match so the
    # provider pairs them on resume.
    assert assistant_msg["tool_calls"][0]["id"] == tool_msg["tool_call_id"]
    # Must not have synthesized the fallback id from the DB message id.
    assert "call_db-message-id-1" not in json.dumps(conversation)
    # The frozen model-seen bytes are still reused as the tool result.
    assert tool_msg["content"] == "MODEL-SEEN-BYTES"


def test_resume_prefers_top_level_original_tool_call_id():
    """When a top-level original id is persisted, it is used directly."""
    payload = {
        "name": "web_search",
        "args": {"query": "ccplus"},
        "status": "done",
        "tool_call_id": "toolu_top_level_7",
        "result": "search result",
    }
    history = [_tool_call_row(payload, msg_id="db-message-id-2")]

    conversation = conversation_from_history_messages(history)

    assistant_msg, tool_msg = conversation
    assert assistant_msg["tool_calls"][0]["id"] == "toolu_top_level_7"
    assert tool_msg["tool_call_id"] == "toolu_top_level_7"


def test_resume_falls_back_to_tagged_synthetic_id_for_legacy_rows():
    """Legacy rows with no original id anywhere fall back to a tagged id.

    The fallback must be clearly marked (``synthetic:`` prefix) so it is not
    mistaken for a real streamed id, while still pairing the assistant
    tool_call with its tool result.
    """
    payload = {
        "name": "list_dir",
        "args": {"path": "."},
        "status": "done",
        "result": "a\nb\nc",
        # No top-level tool_call_id and no content_replacement.tool_call_id.
    }
    history = [_tool_call_row(payload, msg_id="legacy-row-id")]

    conversation = conversation_from_history_messages(history)

    assistant_msg, tool_msg = conversation
    synthetic_id = assistant_msg["tool_calls"][0]["id"]
    assert synthetic_id == "synthetic:call_legacy-row-id"
    # Tagged so callers/debuggers can tell it is not an original streamed id.
    assert synthetic_id.startswith("synthetic:")
    # Assistant tool_call and its result still pair on the same id.
    assert tool_msg["tool_call_id"] == synthetic_id


def test_resume_legacy_content_replacement_without_id_still_falls_back():
    """A content_replacement record missing tool_call_id still falls back."""
    payload = {
        "name": "read_file",
        "args": {"path": "x.txt"},
        "status": "done",
        "result": "R" * 60_000,
        "content_replacement": {
            "schema": "content_replacement_record.v1",
            # No tool_call_id key in the replacement record (legacy).
            "inline_content": "SEEN",
            "original_chars": 60_000,
            "inline_chars": 4,
        },
    }
    history = [_tool_call_row(payload, msg_id="legacy-row-id-2")]

    conversation = conversation_from_history_messages(history)

    assistant_msg, tool_msg = conversation
    assert assistant_msg["tool_calls"][0]["id"] == "synthetic:call_legacy-row-id-2"
    assert tool_msg["tool_call_id"] == "synthetic:call_legacy-row-id-2"
    # Frozen bytes are still reused even on the fallback id path.
    assert tool_msg["content"] == "SEEN"

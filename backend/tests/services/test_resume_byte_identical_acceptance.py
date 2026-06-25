"""CCPlus V1 §7 acceptance — resume_byte_identical (frozen bytes, no re-truncation).

GENUINE acceptance test for the §7 ``resume_byte_identical`` selector. On web
resume, ``conversation_from_history_messages`` rebuilds the provider
conversation that is fed back into the kernel loop. When a persisted tool_call
row carries a ``content_replacement_record.v1``, the rebuilt tool result MUST be
the EXACT frozen ``inline_content`` the model saw at generation time, paired
with the ORIGINAL streamed ``tool_call_id`` (``_resolved_tool_call_id``) — never
a fresh flat re-truncation of the full result.

This is distinct from id-recovery (covered in
``test_resume_original_tool_call_id.py``): here the focus is byte-identity of the
tool *content*. The discriminating case is a long original result whose frozen
inline view is a SHORT eviction preview (e.g. 4K-char preview + a "full output
saved" pointer). Byte-identical resume reuses that exact short preview; the
reverted behavior would re-truncate the full result to a different 50K-char
string the model never actually saw.

Revert-sensitive: if line ~482 reverted from reusing
``content_replacement.inline_content`` to flat-truncating ``result`` (the
``frozen_inline is None`` flat-truncation branch), the resumed bytes would
diverge from the model-seen bytes and these assertions fail.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.services.web_chat_runtime import conversation_from_history_messages


def _tool_call_row(payload: dict, *, msg_id: str) -> SimpleNamespace:
    return SimpleNamespace(role="tool_call", id=msg_id, content=json.dumps(payload))


def test_resume_byte_identical_reuses_short_frozen_preview_not_flat_truncation():
    """A long result with a SHORT frozen preview resumes byte-identically.

    This is the case that separates "reuse frozen bytes" from "re-truncate":
    the original result is 120K chars, but the frozen inline view the model
    actually saw was an eviction preview far shorter than the 50K flat-truncate
    cap. Byte-identical resume must reuse the short preview verbatim.
    """
    # The model saw this exact eviction preview at generation time — a 4K head
    # plus a workspace pointer. It is NOT result[:50000].
    frozen_preview = (
        "R" * 4000 + "\n\n[Full output saved to workspace/tool_results/toolu_big_1.txt — 120000 chars; "
        'reason: result size threshold. Use read_file("workspace/tool_results/toolu_big_1.txt") to retrieve.]'
    )
    full_result = "R" * 120_000

    payload = {
        "name": "read_file",
        "args": {"path": "huge.txt"},
        "status": "done",
        "result": full_result,
        "tool_call_id": "toolu_big_1",
        "content_replacement": {
            "schema": "content_replacement_record.v1",
            "tool_name": "read_file",
            "tool_call_id": "toolu_big_1",
            "reason": "result size threshold",
            "replacement_applied": True,
            "original_chars": len(full_result),
            "inline_chars": len(frozen_preview),
            "inline_content": frozen_preview,
        },
    }
    history = [_tool_call_row(payload, msg_id="db-row-1")]

    conversation = conversation_from_history_messages(history)
    _assistant_msg, tool_msg = conversation

    # Byte-for-byte the frozen preview the model saw — NOT a fresh 50K truncation.
    assert tool_msg["content"] == frozen_preview
    # Definitely not the flat re-truncation the legacy path would produce.
    flat_retruncation = full_result[:50000] + "\n\n[... truncated, full output may be in workspace/tool_results/]"
    assert tool_msg["content"] != flat_retruncation
    assert len(tool_msg["content"]) < 50_000
    # And the result is NOT the raw full output either.
    assert tool_msg["content"] != full_result


def test_resume_byte_identical_pairs_frozen_bytes_with_original_tool_call_id():
    """Frozen bytes resume on the ORIGINAL streamed id, even with no top-level id.

    Production shape: the streamed id lives ONLY inside the content_replacement
    record. ``_resolved_tool_call_id`` recovers it, and the frozen inline bytes
    ride on that same id so the provider pairs the assistant tool_call with its
    tool result.
    """
    frozen_inline = "FROZEN-MODEL-SEEN-BYTES-中文"  # includes non-ASCII to pin exact bytes
    payload = {
        "name": "web_search",
        "args": {"query": "ccplus"},
        "status": "done",
        "result": "X" * 80_000,
        # No top-level tool_call_id — id lives only in the replacement record.
        "content_replacement": {
            "schema": "content_replacement_record.v1",
            "tool_call_id": "toolu_streamed_42",
            "inline_content": frozen_inline,
            "original_chars": 80_000,
            "inline_chars": len(frozen_inline),
        },
    }
    history = [_tool_call_row(payload, msg_id="db-row-2")]

    conversation = conversation_from_history_messages(history)
    assistant_msg, tool_msg = conversation

    # Original streamed id recovered from the record (not call_{msg.id}).
    assert assistant_msg["tool_calls"][0]["id"] == "toolu_streamed_42"
    assert tool_msg["tool_call_id"] == "toolu_streamed_42"
    # Assistant tool_call and its result pair on the SAME id.
    assert assistant_msg["tool_calls"][0]["id"] == tool_msg["tool_call_id"]
    # Frozen bytes are reused byte-for-byte, including non-ASCII.
    assert tool_msg["content"] == frozen_inline
    assert "call_db-row-2" not in json.dumps(conversation)


def test_resume_byte_identical_legacy_row_without_record_does_flat_truncate():
    """Negative control: a legacy row with NO record DOES re-truncate to 50K.

    This pins the boundary — the frozen-bytes path is gated on the presence of
    ``content_replacement``. A row that never persisted one falls back to the
    flat-truncation branch, which is exactly the behavior the frozen path
    replaces for real runtime rows. If the gate inverted, this control would
    break.
    """
    full_result = "L" * 70_000
    payload = {
        "name": "list_dir",
        "args": {"path": "."},
        "status": "done",
        "result": full_result,
        "tool_call_id": "toolu_legacy_9",
        # No content_replacement record at all (legacy row).
    }
    history = [_tool_call_row(payload, msg_id="db-row-3")]

    conversation = conversation_from_history_messages(history)
    _assistant_msg, tool_msg = conversation

    # Legacy path flat-truncates to 50K + marker — NOT byte-identical to source.
    assert tool_msg["content"].startswith("L" * 50_000)
    assert tool_msg["content"].endswith("[... truncated, full output may be in workspace/tool_results/]")
    assert tool_msg["content"] != full_result


def test_resume_byte_identical_preserves_frozen_bytes_longer_than_flat_cap():
    """Frozen inline LONGER than 50K is still reused verbatim (never re-clipped).

    The frozen-bytes path is gated only on the record's presence, not on size:
    if the model genuinely saw a >50K inline view, resume must reuse all of it
    rather than silently clipping it to the legacy 50K cap.
    """
    frozen_inline = "Z" * 55_000  # deliberately above the 50K legacy flat cap
    payload = {
        "name": "read_file",
        "args": {"path": "mid.txt"},
        "status": "done",
        "result": "Z" * 90_000,
        "tool_call_id": "toolu_mid_5",
        "content_replacement": {
            "schema": "content_replacement_record.v1",
            "tool_call_id": "toolu_mid_5",
            "inline_content": frozen_inline,
            "original_chars": 90_000,
            "inline_chars": len(frozen_inline),
        },
    }
    history = [_tool_call_row(payload, msg_id="db-row-4")]

    conversation = conversation_from_history_messages(history)
    _assistant_msg, tool_msg = conversation

    # The full 55K frozen view is reused — not clipped to 50K, no truncation marker.
    assert tool_msg["content"] == frozen_inline
    assert len(tool_msg["content"]) == 55_000
    assert "[... truncated" not in tool_msg["content"]

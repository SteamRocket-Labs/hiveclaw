"""Saved file metadata stays structured outside the human composer."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.session_command_runtime import _checkpoint_payload
from app.services.session_human_input import input_parts_to_runtime_messages
from app.services.session_live_input import shape_human_input_parts
from app.services.session_user_checkpoint import user_checkpoint_content


def test_rewind_draft_omits_file_metadata_without_changing_runtime_bytes():
    parts = [{"type": "text", "text": "继续修改结论"}, {"type": "workspace_file_ref",
        "agent_id": str(uuid.uuid4()), "path": "workspace/private-report.md", "human_saved_hash": "a" * 64}]
    event = SimpleNamespace(id=uuid.uuid4(), sequence=1, item_kind="human_input", lifecycle="accepted",
        event_type="human_input.accepted", created_at=datetime.now(timezone.utc),
        content="", metadata_json={"v2_payload": {"content_parts": parts}})
    assert _checkpoint_payload(event, turn_index=1)["content"] == "继续修改结论"
    assert "workspace/private-report.md" in user_checkpoint_content(event)
    assert parts[1]["human_saved_hash"] == "a" * 64


def test_reference_fields_only_survive_on_workspace_references():
    metadata = {"agent_id": str(uuid.uuid4()), "path": "workspace/report.md", "human_saved_hash": "a" * 64}
    parts = [{"type": kind, **metadata} for kind in ("text", "attachment_ref", "project_ref", "workspace_file_ref")]
    parts[-1]["text"] = "Unneeded reference metadata"
    assert shape_human_input_parts(parts) == [
        {"type": "text"}, {"type": "attachment_ref"}, {"type": "project_ref"},
        {"type": "workspace_file_ref", **metadata}]


def test_malformed_parts_do_not_break_saved_file_detection():
    row = SimpleNamespace(id=uuid.uuid4(), content_parts_json=[None, {"type": "text", "text": "Keep this question"}],
        bound_round_id="round-1")
    messages = input_parts_to_runtime_messages([row])
    assert len(messages) == 1 and "Keep this question" in str(messages[0]["content"])

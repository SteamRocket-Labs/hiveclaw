"""Unreceipted platform sidecars stay unavailable without tainting other work."""

from types import SimpleNamespace

import pytest

from app.services.content_input_receipts import (
    _native_read_denial_matches, authorize_workspace_receipt_read,
)
from app.services.session_tool_runtime import _sha256
from app.tools.result_envelope import ToolContentEnvelope


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,mode", [("read_file", None), ("read_document", None), ("fs_read", "text"), ("fs_read", "document")])
async def test_platform_control_body_is_not_opened_or_back_signed(tool, mode, tmp_path, monkeypatch):
    from pathlib import Path

    relative = "memory/control/session_surfacing/old-session.json"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text("OLD_UNVERIFIED_CONTROL_BODY")
    original = target.read_bytes()
    arguments = {"path": relative, **({"mode": mode} if mode else {})}
    request = SimpleNamespace(tool_name=tool, arguments=arguments, context=SimpleNamespace(workspace=tmp_path))

    def never_open(*args, **kwargs):
        pytest.fail("An unreceipted control body must not be probed or read.")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", never_open)
        result = await authorize_workspace_receipt_read(request)
    assert isinstance(result, ToolContentEnvelope)
    assert "OLD_UNVERIFIED_CONTROL_BODY" not in result
    assert "platform_content_unverified" in result and "read_ledger" in result
    assert target.read_bytes() == original
    observation = result.metadata["native_read_denial"]
    assert _native_read_denial_matches(tool, arguments, observation, content_hash=_sha256(str(result)))
    assert not _native_read_denial_matches(tool, arguments, observation, content_hash="0" * 64)
    assert not _native_read_denial_matches(tool, {"path": "memory/self/self.md"}, observation, content_hash=_sha256(str(result)))
    assert not _native_read_denial_matches(tool, {**arguments, "original_attachment_id": "original"}, observation,
        content_hash=_sha256(str(result)))
    assert not _native_read_denial_matches(tool, arguments, {**observation, "body": "forged"}, content_hash=_sha256(str(result)))


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args", [
    ("read_file", {"path": "skills/current.md"}),
    ("read_file", {"path": "memory/self/self.md"}),
    ("read_file", {"path": "memory/t2/sessions/session/segment.md"}),
    ("read_file", {"path": "memory/control/file.json", "original_attachment_id": "original"}),
    ("glob_search", {"root": "memory/control", "pattern": "*.json"}),
    ("list_files", {"path": "memory/control"}),
    ("fs_read", {"path": "memory/control/file.json", "mode": "image"}),
])
async def test_control_body_failure_does_not_replace_other_source_readers(tool, args):
    request = SimpleNamespace(tool_name=tool, arguments=args, context=SimpleNamespace())
    assert await authorize_workspace_receipt_read(request) is None

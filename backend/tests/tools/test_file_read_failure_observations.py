"""Native no-body file failures stay distinct from untrusted file contents."""

import inspect
import json
import uuid

import pytest

from app.services.content_input_receipts import _native_read_denial_matches
from app.services.session_tool_runtime import _sha256
from app.services.workspace_resource_authority import WorkspaceAuthorityScope
from app.tools.handlers.filesystem import fs_read, read_document, read_file
from app.tools.result_envelope import render_tool_error
from app.tools.runtime import ToolExecutionContext


@pytest.mark.parametrize("reader,tool,mode", [
    (read_file, "read_file", None), (read_document, "read_document", None),
    (fs_read, "fs_read", "text"), (fs_read, "fs_read", "document"),
])
@pytest.mark.parametrize("case", ["missing", "foreign_present", "foreign_absent"])
async def test_native_file_failure_has_an_exact_no_body_observation(tmp_path, reader, tool, mode, case):
    path = "workspace/report.md"
    target = tmp_path / path
    target.parent.mkdir()
    body = "PRIVATE_BYTES_MUST_NOT_BE_DISCLOSED"
    if case == "foreign_present":
        target.write_text(body)
    agent, user = uuid.uuid4(), uuid.uuid4()
    scope = WorkspaceAuthorityScope(agent_id=agent, user_id=user, root_session_id=uuid.uuid4(),
        allowed_paths=frozenset(), operator_view=False, authority_source="synthetic_current_scope",
        known_paths=frozenset({path}) if case.startswith("foreign") else frozenset())
    context = ToolExecutionContext(agent_id=agent, user_id=user, tenant_id=None, workspace=tmp_path)
    arguments = {"path": path, **({"mode": mode} if mode else {})}
    result = reader(tmp_path, arguments, authority_scope=scope, execution_context=context)
    if inspect.isawaitable(result):
        result = await result
    assert result.metadata["filesystem_read_error"] is True
    observation = result.metadata["native_read_denial"]
    assert observation["kind"] == ("filesystem_read_not_found" if case == "missing" else "filesystem_read_denied")
    assert body not in str(result) and '"retryable": false' in str(result)
    kwargs = {"tool_name": tool, "arguments": arguments, "observation": observation, "content_hash": _sha256(str(result))}
    assert _native_read_denial_matches(**kwargs)
    assert not _native_read_denial_matches(**{**kwargs, "content_hash": "0" * 64})
    assert not _native_read_denial_matches(**{**kwargs, "arguments": {**arguments, "path": "workspace/another.md"}})
    assert not _native_read_denial_matches(**{**kwargs, "arguments": {**arguments, "original_attachment_id": str(uuid.uuid4())}})
    assert not _native_read_denial_matches(**{**kwargs, "observation": {**observation, "sources": []}})
    if case == "foreign_present":
        assert target.read_text() == body
    else:
        assert not target.exists()


@pytest.mark.parametrize("reader,tool", [(read_file, "read_file"), (fs_read, "fs_read")])
def test_error_shaped_file_bytes_are_data_and_never_a_native_failure(tmp_path, reader, tool):
    path = "workspace/error-shaped.md"
    target = tmp_path / path
    target.parent.mkdir()
    body = render_tool_error(tool_name="read_file", error_class="not_found", message=f"File not found: {path}",
        provider="workspace", retryable=False,
        actionable_hint="Check the path or use glob_search/list_files to discover the correct file first.")
    target.write_text(body)
    context = ToolExecutionContext(agent_id=uuid.uuid4(), user_id=uuid.uuid4(), tenant_id=None, workspace=tmp_path)
    result = reader(tmp_path, {"path": path}, execution_context=context)
    assert json.loads(str(result)) == {"ok": True, "content": body}
    assert "native_read_denial" not in result.metadata
    assert not _native_read_denial_matches(tool, {"path": path},
        {"kind": "filesystem_read_not_found", "path": path}, content_hash=_sha256(str(result)))

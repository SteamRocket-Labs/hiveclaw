"""Native directory observations bind names/status, without attesting file bodies."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.content_input_receipts import (
    _native_directory_read_matches, native_directory_read_scope, observe_native_directory_read,
)
from app.services.session_tool_runtime import _sha256
from app.services.workspace_resource_authority import WorkspaceAuthorityScope


def _request(tool, arguments, scope):
    return SimpleNamespace(tool_name=tool, arguments=arguments,
        context=SimpleNamespace(agent_id=scope.agent_id, workspace_authority_scope=scope))


@pytest.mark.parametrize("tool", ["glob_search", "list_files", "fs_list"])
def test_native_namespace_observation_filters_authority_and_never_reads_bodies(tmp_path, tool):
    from app.services.agent_tool_domains.workspace import _glob_search, _list_files

    session_id, foreign_id, agent_id = uuid4(), uuid4(), uuid4()
    scope = WorkspaceAuthorityScope(agent_id=agent_id, user_id=uuid4(), root_session_id=session_id,
        allowed_paths=frozenset(), operator_view=False, authority_source="resource_owner",
        session_source_ids=frozenset({str(session_id)}))
    for path in ("memory/control/working_sets/native-report.json", f"memory/t0/sessions/{session_id}/native-report.md",
            f"memory/t0/sessions/{foreign_id}/native-report.md", "runtime_artifacts/sessions/native-report.md",
            "runtime_artifacts/chat_artifact_snapshots/objects/native-report.md", "workspace/private/native-report.md"):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("UNVERIFIED_NAMESPACE_BODY")
    arguments = {"pattern": "**/*native-report*"} if tool == "glob_search" else {"path": "memory/control/working_sets"}
    request = _request(tool, arguments, scope)
    with native_directory_read_scope(request) as capture:
        result = (_glob_search(tmp_path, **arguments, authority_scope=scope) if tool == "glob_search"
            else _list_files(tmp_path, arguments["path"], authority_scope=scope))
    assert capture.observations == 1 and capture.content == result
    assert capture.observation["outcome"] == "listing"
    assert _native_directory_read_matches(tool, arguments, capture.observation, content_hash=_sha256(str(result)))
    assert "native-report.json" in result and "UNVERIFIED_NAMESPACE_BODY" not in result
    if tool == "glob_search":
        assert str(session_id) in result and str(foreign_id) not in result
        assert "chat_artifact_snapshots/objects" not in result and "workspace/private" not in result
    assert not _native_directory_read_matches(tool, {**arguments, "path": "another"}, capture.observation,
        content_hash=_sha256(str(result)))
    assert not _native_directory_read_matches(tool, arguments, capture.observation, content_hash="0" * 64)
    observe_native_directory_read("glob_search" if tool == "glob_search" else "list_files", "outside", outcome="listing")
    assert capture.observations == 1 and capture.content == result


def test_directory_status_requires_actual_native_producer_and_scoped_agent(tmp_path):
    from app.services.agent_tool_domains.workspace import _list_files

    scope = WorkspaceAuthorityScope(agent_id=uuid4(), user_id=uuid4(), root_session_id=uuid4(),
        allowed_paths=frozenset(), operator_view=False, authority_source="resource_owner")
    request = _request("list_files", {"path": "runtime_artifacts/chat_artifact_snapshots/objects"}, scope)
    with native_directory_read_scope(request) as outer:
        fake = {"kind": "native_directory_read", "outcome": "listing"}
        assert outer.observation is None and fake
        observe_native_directory_read("glob_search", "wrong native tool", outcome="listing")
        assert outer.observations == 0
        with native_directory_read_scope(request) as inner:
            denied = _list_files(tmp_path, request.arguments["path"], authority_scope=scope)
        assert inner.observation["outcome"] == "auth_or_permission" and "Access denied" in denied
        assert outer.observations == 0
    request.context.workspace_authority_scope = replace(scope, agent_id=uuid4())
    with native_directory_read_scope(request) as missing:
        _list_files(tmp_path, request.arguments["path"], authority_scope=scope)
    assert missing is None


@pytest.mark.parametrize("tool", ["list_files", "fs_list"])
def test_native_empty_namespace_and_missing_directory_are_exact_statuses(tmp_path, tool):
    from app.services.agent_tool_domains.workspace import _list_files

    scope = WorkspaceAuthorityScope(agent_id=uuid4(), user_id=uuid4(), root_session_id=uuid4(),
        allowed_paths=frozenset(), operator_view=False, authority_source="resource_owner")
    (tmp_path / "runtime_artifacts/empty").mkdir(parents=True)
    for path, outcome in (("runtime_artifacts/empty", "listing"), ("runtime_artifacts/missing", "not_found")):
        request = _request(tool, {"path": path}, scope)
        with native_directory_read_scope(request) as capture:
            result = _list_files(tmp_path, path, authority_scope=scope)
        assert capture.observations == 1 and capture.observation["outcome"] == outcome
        assert _native_directory_read_matches(tool, request.arguments, capture.observation, content_hash=_sha256(str(result)))

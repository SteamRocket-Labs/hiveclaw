from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest


def _scope(known_paths=frozenset()):
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope

    return WorkspaceAuthorityScope(
        agent_id=uuid4(),
        user_id=uuid4(),
        root_session_id=uuid4(),
        allowed_paths=frozenset({"workspace/mine/report.md", "workspace/mine/data.csv"}),
        operator_view=False,
        authority_source="resource_owner",
        known_paths=frozenset(known_paths),
    )


def test_workspace_read_tools_hide_foreign_session_files(tmp_path):
    from app.services.agent_tool_domains.workspace import _glob_search, _grep_search, _list_files, _read_file

    workspace = tmp_path / "agent"
    mine = workspace / "workspace" / "mine" / "report.md"
    foreign = workspace / "workspace" / "foreign" / "private.md"
    mine.parent.mkdir(parents=True)
    foreign.parent.mkdir(parents=True)
    mine.write_text("mine needle", encoding="utf-8")
    foreign.write_text("foreign needle", encoding="utf-8")
    scope = _scope()

    listing = _list_files(workspace, "workspace", authority_scope=scope)
    assert "mine" in listing
    assert "foreign" not in listing
    assert "mine needle" in str(_read_file(workspace, "workspace/mine/report.md", authority_scope=scope))
    assert "auth_or_permission" in str(_read_file(workspace, "workspace/foreign/private.md", authority_scope=scope))
    globbed = _glob_search(workspace, "**/*.md", "workspace", authority_scope=scope)
    assert "mine/report.md" in globbed
    assert "foreign/private.md" not in globbed
    grepped = _grep_search(workspace, "needle", "workspace", authority_scope=scope)
    assert "mine/report.md" in grepped
    assert "foreign/private.md" not in grepped


def test_workspace_tools_never_expose_raw_recovery_manifest_storage(tmp_path):
    from app.services.agent_tool_domains.workspace import _glob_search, _grep_search, _list_files, _read_file

    workspace = tmp_path / "agent"
    manifest = workspace / "runtime_artifacts" / "recovery_manifests" / "session-hash" / "authority-hash.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"pending_items":["PRIVATE_RECOVERY_SENTINEL"]}', encoding="utf-8")
    runtime_legacy = workspace / "runtime_artifacts" / "recovery_manifest.json"
    workspace_legacy = workspace / "workspace" / "recovery_manifest.json"
    runtime_legacy.write_text('{"pending_items":["RUNTIME_LEGACY_SENTINEL"]}', encoding="utf-8")
    workspace_legacy.parent.mkdir(parents=True)
    workspace_legacy.write_text('{"pending_items":["WORKSPACE_LEGACY_SENTINEL"]}', encoding="utf-8")
    scope = _scope()

    listing = _list_files(workspace, "runtime_artifacts", authority_scope=scope)
    workspace_listing = _list_files(workspace, "workspace", authority_scope=scope)
    direct = str(
        _read_file(
            workspace,
            "runtime_artifacts/recovery_manifests/session-hash/authority-hash.json",
            authority_scope=scope,
        )
    )
    globbed = _glob_search(workspace, "**/*.json", "", authority_scope=scope)
    grepped = _grep_search(workspace, "PRIVATE_RECOVERY_SENTINEL", "", authority_scope=scope)

    assert "recovery_manifests" not in listing
    assert "recovery_manifest.json" not in listing
    assert "recovery_manifest.json" not in workspace_listing
    assert "auth_or_permission" in direct
    assert "PRIVATE_RECOVERY_SENTINEL" not in direct
    assert "authority-hash.json" not in globbed
    assert "authority-hash.json" not in grepped
    assert "No matches" in grepped


def test_read_file_decides_known_foreign_ownership_before_existence(tmp_path):
    """The trusted manifest registry decides KNOWN FOREIGN ownership before any
    filesystem probe, so an absent foreign manifest and a present one return the
    same denial (no existence oracle). Unknown new files and owned-but-missing
    files stay honest ``not_found`` (Plan Mode authors plan/target files before
    they exist)."""
    from app.services.agent_tool_domains.workspace import _read_document, _read_file

    workspace = tmp_path / "agent"
    foreign_present = workspace / "workspace" / "foreign" / "present.md"
    foreign_present.parent.mkdir(parents=True)
    foreign_present.write_text("foreign secret", encoding="utf-8")
    # The manifest registry knows both foreign paths; only one exists on disk.
    scope = _scope(known_paths={"workspace/foreign/present.md", "workspace/foreign/absent.md"})

    # Foreign manifest, ABSENT on disk: same denial as a present one.
    absent_foreign = str(_read_file(workspace, "workspace/foreign/absent.md", authority_scope=scope))
    assert "auth_or_permission" in absent_foreign
    assert "not_found" not in absent_foreign

    # Foreign manifest, PRESENT on disk.
    present_foreign = str(_read_file(workspace, "workspace/foreign/present.md", authority_scope=scope))
    assert "auth_or_permission" in present_foreign
    assert "foreign secret" not in present_foreign

    # Unknown NEW file (no manifest, nothing on disk): honest not_found.
    unknown_new = str(_read_file(workspace, "workspace/mine/new-plan.md", authority_scope=scope))
    assert "not_found" in unknown_new
    assert "auth_or_permission" not in unknown_new

    # Owned path that is missing on disk: honest not_found, not a denial.
    owned_missing = str(_read_file(workspace, "workspace/mine/report.md", authority_scope=scope))
    assert "not_found" in owned_missing
    assert "auth_or_permission" not in owned_missing

    # Reserved recovery storage is denied regardless of existence — its
    # existence itself must not be probed or implied.
    absent_reserved = str(
        _read_file(
            workspace,
            "runtime_artifacts/recovery_manifests/session-hash/authority-hash.json",
            authority_scope=scope,
        )
    )
    assert "auth_or_permission" in absent_reserved

    # _read_document keeps the same ordering for the absent foreign manifest.
    absent_foreign_doc = str(
        asyncio.run(_read_document(workspace, "workspace/foreign/absent.md", authority_scope=scope))
    )
    assert "auth_or_permission" in absent_foreign_doc
    assert "not_found" not in absent_foreign_doc
    unknown_new_doc = str(asyncio.run(_read_document(workspace, "workspace/mine/new-plan.md", authority_scope=scope)))
    assert "not_found" in unknown_new_doc
    assert "auth_or_permission" not in unknown_new_doc


def test_write_then_read_roundtrip_for_new_scope_file(tmp_path):
    """The companion write ordering: a scope-authorized new file can be written
    and then read back through the same authority scope."""
    from app.services.agent_tool_domains.workspace import _read_file, _write_file

    workspace = tmp_path / "agent"
    (workspace / "workspace" / "mine").mkdir(parents=True)
    scope = _scope()

    result = _write_file(workspace, "workspace/mine/report.md", "b4 plan body", authority_scope=scope)
    assert "auth_or_permission" not in str(result)
    read_back = str(_read_file(workspace, "workspace/mine/report.md", authority_scope=scope))
    assert "b4 plan body" in read_back


def test_workspace_authority_never_delivers_legacy_recovery_manifest(tmp_path):
    from app.services.workspace_resource_authority import WorkspaceAuthorityError, authorize_workspace_tool_path

    with pytest.raises(WorkspaceAuthorityError) as exc:
        authorize_workspace_tool_path(
            tmp_path,
            _scope(),
            "workspace/recovery_manifest.json",
            action="deliver",
            require_user_workspace=True,
        )

    assert exc.value.code == "recovery_manifest_raw_access_forbidden"


def test_workspace_write_rejects_foreign_manifest_even_when_file_is_absent(tmp_path):
    from app.services.agent_tool_domains.workspace import _write_file
    from app.services.workspace_resource_authority import (
        WorkspaceAuthorityError,
        WorkspaceAuthorityScope,
        authorize_workspace_tool_path,
    )

    reserved = "workspace/foreign/deleted-report.md"
    scope = WorkspaceAuthorityScope(
        agent_id=uuid4(),
        user_id=uuid4(),
        root_session_id=uuid4(),
        allowed_paths=frozenset(),
        operator_view=False,
        authority_source="resource_scope",
        known_paths=frozenset({reserved}),
    )

    with pytest.raises(WorkspaceAuthorityError) as exc:
        authorize_workspace_tool_path(tmp_path, scope, reserved, action="write")

    assert exc.value.code == "workspace_resource_forbidden"
    tool_result = _write_file(tmp_path, reserved, "must not overwrite ownership", authority_scope=scope)
    assert "auth_or_permission" in tool_result
    assert not (tmp_path / reserved).exists()


def test_workspace_write_allows_new_unclaimed_resource_for_current_scope(tmp_path):
    from app.services.agent_tool_domains.workspace import _write_file
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope

    scope = WorkspaceAuthorityScope(
        agent_id=uuid4(),
        user_id=uuid4(),
        root_session_id=uuid4(),
        allowed_paths=frozenset(),
        operator_view=False,
        authority_source="resource_scope",
        known_paths=frozenset(),
    )

    result = _write_file(
        tmp_path,
        "workspace/current-session/new-report.md",
        "new owned output",
        authority_scope=scope,
    )

    assert "Written" in result
    assert (tmp_path / "workspace" / "current-session" / "new-report.md").read_text() == "new owned output"


def test_authorized_code_workspace_merges_new_outputs_without_exposing_or_overwriting_foreign_files(tmp_path):
    from app.services.agent_tool_domains.code_exec import (
        WorkspaceExecutionAuthorityError,
        authorized_execution_workspace,
    )

    workspace = tmp_path / "agent"
    mine = workspace / "workspace" / "mine" / "report.md"
    foreign = workspace / "workspace" / "foreign" / "private.md"
    mine.parent.mkdir(parents=True)
    foreign.parent.mkdir(parents=True)
    mine.write_text("mine", encoding="utf-8")
    foreign.write_text("foreign", encoding="utf-8")
    scope = _scope()

    with authorized_execution_workspace(workspace, scope) as isolated:
        assert (isolated / "workspace" / "mine" / "report.md").read_text() == "mine"
        assert not (isolated / "workspace" / "foreign" / "private.md").exists()
        output = isolated / "workspace" / "mine" / "result.xlsx"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"xlsx")

    assert (workspace / "workspace" / "mine" / "result.xlsx").read_bytes() == b"xlsx"
    assert foreign.read_text(encoding="utf-8") == "foreign"

    try:
        with authorized_execution_workspace(workspace, scope) as isolated:
            collision = isolated / "workspace" / "foreign" / "private.md"
            collision.parent.mkdir(parents=True, exist_ok=True)
            collision.write_text("overwrite", encoding="utf-8")
    except WorkspaceExecutionAuthorityError as exc:
        assert exc.code == "workspace_resource_collision"
    else:  # pragma: no cover - fail loudly if an implementation overwrites it
        raise AssertionError("foreign path collision was not rejected")
    assert foreign.read_text(encoding="utf-8") == "foreign"


def test_authorized_code_workspace_skips_merge_for_mtime_drift_with_identical_bytes(tmp_path):
    import os

    from app.services.agent_tool_domains.code_exec import authorized_execution_workspace

    workspace = tmp_path / "agent"
    mine = workspace / "workspace" / "mine" / "report.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("unchanged content", encoding="utf-8")
    canonical_mtime = mine.stat().st_mtime_ns
    scope = _scope()

    with authorized_execution_workspace(workspace, scope) as isolated:
        isolated_report = isolated / "workspace" / "mine" / "report.md"
        stat = isolated_report.stat()
        os.utime(isolated_report, ns=(stat.st_mtime_ns + 10_000_000_000,) * 2)

    assert mine.read_text(encoding="utf-8") == "unchanged content"
    assert mine.stat().st_mtime_ns == canonical_mtime


def test_authorized_code_workspace_rejects_absent_foreign_manifest_path(tmp_path):
    from app.services.agent_tool_domains.code_exec import (
        WorkspaceExecutionAuthorityError,
        authorized_execution_workspace,
    )
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope

    reserved = "workspace/foreign/deleted-report.xlsx"
    scope = WorkspaceAuthorityScope(
        agent_id=uuid4(),
        user_id=uuid4(),
        root_session_id=uuid4(),
        allowed_paths=frozenset(),
        operator_view=False,
        authority_source="resource_scope",
        known_paths=frozenset({reserved}),
    )

    with pytest.raises(WorkspaceExecutionAuthorityError) as exc:
        with authorized_execution_workspace(tmp_path, scope) as isolated:
            output = isolated / reserved
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"foreign collision")

    assert exc.value.code == "workspace_resource_collision"
    assert not (tmp_path / reserved).exists()


def test_workspace_mutation_extraction_covers_unified_and_code_execution_artifacts():
    from app.services.workspace_resource_authority import workspace_mutations_from_tool
    from app.tools.result_envelope import ToolContentEnvelope

    assert workspace_mutations_from_tool("fs_write", {"mode": "delete", "path": "workspace/old.md"}, "ok") == [
        {"path": "workspace/old.md", "action": "deleted", "content_hash": None}
    ]
    envelope = ToolContentEnvelope(
        text="done",
        artifacts=(
            {"path": "workspace/report.xlsx", "action": "created", "after_state": {"sha256": "a" * 64}},
            {"path": "workspace/old.csv", "action": "deleted"},
        ),
    )
    assert workspace_mutations_from_tool("run_command", {"command": "python report.py"}, envelope) == [
        {"path": "workspace/report.xlsx", "action": "created", "content_hash": "a" * 64},
        {"path": "workspace/old.csv", "action": "deleted", "content_hash": None},
    ]
    assert workspace_mutations_from_tool(
        "office_document_apply",
        {"path": "workspace/source.docx", "output_path": "workspace/result.docx"},
        json.dumps({"ok": True}),
    ) == [{"path": "workspace/result.docx", "action": "written", "content_hash": None}]


@pytest.mark.asyncio
async def test_office_tools_reject_foreign_reads_and_overwrites(tmp_path):
    from app.tools.handlers.office import office_document_apply, office_document_create, office_document_view

    workspace = tmp_path / "agent"
    foreign = workspace / "workspace" / "foreign.docx"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"foreign")
    scope = _scope()

    viewed = json.loads(
        await office_document_view(
            workspace,
            {"path": "workspace/foreign.docx", "mode": "text"},
            authority_scope=scope,
        )
    )
    applied = json.loads(
        await office_document_apply(
            workspace,
            {"path": "workspace/foreign.docx", "operations": []},
            authority_scope=scope,
        )
    )
    created = json.loads(
        await office_document_create(
            workspace,
            {"path": "workspace/foreign.docx", "kind": "docx"},
            authority_scope=scope,
        )
    )
    system_path = json.loads(
        await office_document_create(
            workspace,
            {"path": "soul.docx", "kind": "docx"},
            authority_scope=scope,
        )
    )

    assert viewed["error"] == "workspace_resource_forbidden"
    assert applied["error"] == "workspace_resource_forbidden"
    assert created["error"] == "workspace_resource_forbidden"
    assert system_path["error"] == "workspace_resource_path_required"
    assert foreign.read_bytes() == b"foreign"


@pytest.mark.asyncio
async def test_channel_delivery_and_upload_reject_foreign_workspace_files(tmp_path):
    from app.tools.handlers.communication import send_channel_file, upload_image

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    foreign = workspace / "workspace" / "foreign.png"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"foreign")
    scope = _scope()

    delivered = await send_channel_file(
        agent_id,
        workspace,
        {"file_path": "workspace/foreign.png"},
        authority_scope=scope,
    )
    uploaded = await upload_image(
        agent_id,
        workspace,
        {"file_path": "workspace/foreign.png"},
        authority_scope=scope,
    )

    assert "workspace_resource_forbidden" in delivered
    assert "workspace_resource_forbidden" in uploaded


@pytest.mark.asyncio
async def test_agent_workspace_adapter_forwards_authority_scope_when_supported(tmp_path):
    from app.tools.adapters import adapt_and_call
    from app.tools.decorator import ToolMeta
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    scope = _scope()
    captured = {}

    def handler(agent_id, workspace, arguments, authority_scope=None):
        captured["scope"] = authority_scope
        return "ok"

    request = ToolExecutionRequest(
        tool_name="scoped",
        arguments={},
        context=ToolExecutionContext(
            agent_id=scope.agent_id,
            user_id=scope.user_id,
            tenant_id=str(uuid4()),
            workspace=tmp_path,
            workspace_authority_scope=scope,
        ),
    )

    meta = ToolMeta(
        name="scoped",
        description="scoped test tool",
        parameters={"type": "object", "properties": {}},
        category="test",
        display_name="Scoped",
        adapter="agent_workspace_args",
    )
    assert await adapt_and_call(meta, handler, request) == "ok"
    assert captured["scope"] is scope

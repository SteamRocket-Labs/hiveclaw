from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


@pytest.mark.parametrize("source", ["empty", "receipted", "legacy", "tampered"])
@pytest.mark.parametrize("path", ["workspace/source.txt", "workspace/.hive/source.txt"])
def test_execution_inventory_binds_all_copied_bytes_without_authorizing_legacy_sources(tmp_path, source, path):
    import hashlib
    from app.services.agent_tool_domains.code_exec import authorized_execution_workspace
    from app.services.workspace_resource_authority import (
        WorkspaceAuthorityScope, WorkspaceExecutionInputs, WorkspaceInputRead,
    )

    digest = hashlib.sha256(b"Authorized input").hexdigest()
    receipt_id = uuid4()
    reads = () if source in {"empty", "legacy"} else (
        WorkspaceInputRead(path, receipt_id, 1, digest, "synthetic-dependencies"),)
    if source != "empty":
        (tmp_path / path).parent.mkdir(parents=True)
        (tmp_path / path).write_text("Changed input" if source == "tampered" else "Authorized input")
    capture = WorkspaceExecutionInputs()
    assert capture.snapshot()["observed"] is False
    scope = WorkspaceAuthorityScope(agent_id=uuid4(), user_id=uuid4(), root_session_id=None,
        allowed_paths=frozenset() if source == "empty" else frozenset({path}),
        operator_view=False, authority_source="resource_owner", input_reads=reads, execution_inputs=capture)
    with authorized_execution_workspace(tmp_path, scope) as private:
        assert (private / path).exists() is (source in {"receipted", "legacy"})
    observed = capture.snapshot()
    assert observed["observed"] is True
    assert observed["unbound"] == ([path] if source == "legacy" else [])
    assert observed["sources"] == ([{"source_kind": "workspace_artifact", "source_id": str(receipt_id),
        "source_version": digest}] if source == "receipted" else [])


@pytest.mark.parametrize("operator_view", [False, True])
def test_read_scope_labels_and_inspection_do_not_create_effect_authority(operator_view):
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope

    path = "workspace/borrowed.txt"
    scope = WorkspaceAuthorityScope(agent_id=uuid4(), user_id=uuid4(), root_session_id=None,
        allowed_paths=frozenset({path}), operator_view=operator_view, authority_source="resource_owner")
    assert scope.can_read(path)
    assert all(not scope.can_perform(path, action) for action in ("write", "delete", "create", "deliver"))


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "parent_symlink"])
@pytest.mark.parametrize("write", [False, True])
def test_opened_file_is_checked_before_read_or_truncate(tmp_path, monkeypatch, alias, write):
    import os
    from app.services.workspace_resource_authority import WorkspaceAuthorityError, open_workspace_file

    mine, theirs = tmp_path / "workspace/mine/report.txt", tmp_path / "workspace/theirs/report.txt"
    for path in (mine, theirs):
        path.parent.mkdir(parents=True)
    mine.write_text("Owned bytes")
    theirs.write_text("Private bytes must survive")
    original_open = os.open
    replaced = False

    def replace_at_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if not replaced and path == ("mine" if alias == "parent_symlink" else "report.txt"):
            replaced = True
            mine.unlink()
            if alias == "symlink":
                mine.symlink_to(theirs)
            elif alias == "hardlink":
                os.link(theirs, mine)
            else:
                mine.parent.rmdir()
                mine.parent.symlink_to(theirs.parent, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_at_open)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {replace_at_open})
    with pytest.raises(WorkspaceAuthorityError) as denied:
        with open_workspace_file(tmp_path, "workspace/mine/report.txt", mode="wb" if write else "rb") as handle:
            handle.write(b"Unauthorized replacement") if write else handle.read()
    assert replaced
    assert denied.value.code == "workspace_resource_changed"
    assert theirs.read_text() == "Private bytes must survive"


def test_workspace_file_can_initialize_independent_agent_storage(tmp_path):
    from app.services.workspace_resource_authority import open_workspace_file, write_workspace_file

    root = tmp_path / "new-agent"
    write_workspace_file(root, "workspace/new/report.txt", "Owned UTF-8: 你好".encode())
    with open_workspace_file(root, "workspace/new/report.txt") as source:
        assert source.read().decode() == "Owned UTF-8: 你好"


@pytest.mark.parametrize("write", [False, True])
def test_open_file_keeps_original_inode_and_reports_a_rebound_path(tmp_path, write):
    from app.services.workspace_resource_authority import WorkspaceAuthorityError, open_workspace_file

    mine, theirs = tmp_path / "workspace/mine.txt", tmp_path / "workspace/theirs.txt"
    mine.parent.mkdir()
    mine.write_bytes(b"Owned bytes")
    theirs.write_bytes(b"Private bytes must survive")
    with pytest.raises(WorkspaceAuthorityError) as denied:
        with open_workspace_file(tmp_path, "workspace/mine.txt", mode="wb" if write else "rb") as handle:
            mine.unlink()
            mine.symlink_to(theirs)
            if write:
                handle.write(b"Owned revision")
            else:
                assert handle.read() == b"Owned bytes"
    assert denied.value.code == ("workspace_resource_changed" if write else "workspace_resource_alias_forbidden")
    assert theirs.read_bytes() == b"Private bytes must survive"


def test_workspace_scope_filters_only_workspace_resources_and_preserves_agent_system_paths():
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope

    scope = WorkspaceAuthorityScope(
        agent_id=uuid4(),
        user_id=uuid4(),
        root_session_id=uuid4(),
        allowed_paths=frozenset({"workspace/mine/report.md", "workspace/mine/data.csv"}),
        operator_view=False,
        authority_source="resource_owner",
    )

    assert scope.can_read("soul.md") is True
    assert scope.can_read("skills/review/SKILL.md") is True
    assert scope.can_read("workspace/mine/report.md") is True
    assert scope.can_read("workspace/mine") is True
    assert scope.can_read("workspace/other/private.md") is False
    assert scope.visible_child("workspace", "mine", is_dir=True) is True
    assert scope.visible_child("workspace", "other", is_dir=True) is False


@pytest.mark.asyncio
async def test_workspace_existing_unknown_path_is_quarantined_but_new_path_is_owned(monkeypatch):
    import app.services.workspace_resource_authority as workspace_authority

    agent_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member", department_id=None)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    decisions = []

    async def fake_access(_db, _user, _agent_id):
        return agent, "use"

    async def fake_resource_action(_db, _user, **kwargs):
        decisions.append(kwargs)
        if not kwargs.get("allow_manager_override"):
            raise HTTPException(status_code=403, detail="quarantined")
        return SimpleNamespace(authority_source="resource_owner", operator_view=False)

    monkeypatch.setattr(workspace_authority, "check_agent_access", fake_access)
    monkeypatch.setattr(workspace_authority, "authorize_resource_action", fake_resource_action)

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _DB:
        async def execute(self, _statement):
            return _Result(None)

    with pytest.raises(workspace_authority.WorkspaceAuthorityError) as exc:
        await workspace_authority.authorize_workspace_path(
            _DB(),
            user,
            agent_id=agent_id,
            path="workspace/legacy.md",
            action="write",
            path_exists=True,
        )
    assert exc.value.code == "workspace_resource_quarantined"

    decision = await workspace_authority.authorize_workspace_path(
        _DB(),
        user,
        agent_id=agent_id,
        path="workspace/new.md",
        action="write",
        path_exists=False,
    )
    assert decision.is_new is True
    assert decision.owner_user_id == user_id
    assert decision.authority_source == "new_resource_owner"
    assert len(decisions) == 1
    assert decisions[0]["authority_state"] == "quarantined"


def test_workspace_manifest_upsert_never_reassigns_a_live_path_without_authorized_takeover():
    from app.services.workspace_resource_authority import build_workspace_manifest_upsert

    statement = build_workspace_manifest_upsert(
        tenant_id=uuid4(),
        agent_id=uuid4(),
        path="workspace/report.md",
        owner_user_id=uuid4(),
        root_session_id=uuid4(),
        source="web_chat_artifact",
        content_hash="a" * 64,
        allow_owner_rebind=False,
    )
    compiled = str(statement.compile())

    assert "ON CONFLICT" in compiled
    assert "owner_user_id" not in compiled.split("DO UPDATE SET", 1)[-1]


@pytest.mark.asyncio
async def test_scoped_admin_binds_employee_session_root_without_reassigning_owner(monkeypatch):
    """PDEC-013: an administrator writes through an employee's exact session
    while the manifest keeps its original owner/root provenance."""

    import app.services.workspace_resource_authority as workspace_authority

    agent_id = uuid4()
    tenant_id = uuid4()
    employee_id = uuid4()
    admin_id = uuid4()
    session_id = uuid4()
    root_session_id = uuid4()
    admin = SimpleNamespace(id=admin_id, tenant_id=tenant_id, role="org_admin", department_id=None)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    employee_session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=employee_id, root_session_id=None,
        audience_kind="private")
    manifest = SimpleNamespace(
        agent_id=agent_id,
        path="workspace/report.md",
        owner_user_id=employee_id,
        root_session_id=root_session_id,
        authority_state="owned",
        deleted_at=None,
    )

    async def fake_access(_db, _user, _agent_id):
        return agent, "manage"

    resource_decisions = []

    async def fake_resource_action(_db, user, **kwargs):
        resource_decisions.append(kwargs)
        return SimpleNamespace(authority_source="scoped_business_admin", operator_view=False)

    monkeypatch.setattr(workspace_authority, "check_agent_access", fake_access)
    monkeypatch.setattr(workspace_authority, "authorize_resource_action", fake_resource_action)

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _DB:
        def __init__(self, results):
            self.results = list(results)

        async def execute(self, _statement):
            return _Result(self.results.pop(0))

    decision = await workspace_authority.authorize_workspace_path(
        _DB([manifest, employee_session]),
        admin,
        agent_id=agent_id,
        path="workspace/report.md",
        action="write",
        path_exists=True,
        session_id=session_id,
    )

    # The exact session authority resolved through the employee's session and
    # the manifest owner/root provenance stayed with the employee.
    assert decision.authority_source == "scoped_business_admin"
    assert decision.owner_user_id == employee_id
    assert decision.root_session_id == root_session_id
    assert decision.operator_view is False
    assert resource_decisions[0]["owner_user_id"] == employee_id
    assert resource_decisions[0]["root_session_id"] == root_session_id


@pytest.mark.asyncio
async def test_member_write_through_someone_elses_session_still_fails(monkeypatch):
    import app.services.workspace_resource_authority as workspace_authority

    agent_id = uuid4()
    tenant_id = uuid4()
    employee_id = uuid4()
    member_id = uuid4()
    session_id = uuid4()
    member = SimpleNamespace(id=member_id, tenant_id=tenant_id, role="member", department_id=None)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    employee_session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=employee_id, root_session_id=None,
        audience_kind="private")

    async def fake_access(_db, _user, _agent_id):
        return agent, "manage"

    monkeypatch.setattr(workspace_authority, "check_agent_access", fake_access)

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _DB:
        async def execute(self, _statement):
            return _Result(employee_session)

    with pytest.raises(workspace_authority.WorkspaceAuthorityError) as exc:
        await workspace_authority.authorize_workspace_path(
            _DB(),
            member,
            agent_id=agent_id,
            path="workspace/report.md",
            action="write",
            path_exists=True,
            session_id=session_id,
        )
    assert exc.value.code == "workspace_session_authority_mismatch"

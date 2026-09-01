from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _SessionResult:
    def __init__(self, sessions):
        self._sessions = sessions

    def scalars(self):
        return self

    def all(self):
        return list(self._sessions)


class _FakeDB:
    def __init__(self, sessions, *, fail_commit=False):
        self.sessions = sessions
        self.fail_commit = fail_commit
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0

    async def execute(self, _statement):
        return _SessionResult(self.sessions)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1
        if self.fail_commit:
            raise RuntimeError("database commit failed")

    async def rollback(self):
        self.rollbacks += 1


def _snapshot_session(*, agent_id, tmp_path):
    from app.services.session_workspace_snapshot import capture_workspace_snapshot

    session_id = uuid4()
    checkpoint_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    report = workspace / "report.md"
    report.write_text("checkpoint content", encoding="utf-8")
    snapshot = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=session_id,
        checkpoint_event_id=checkpoint_id,
        data_root=tmp_path,
    )
    return (
        SimpleNamespace(
            id=session_id,
            user_id=uuid4(),
            root_session_id=None,
            transcript_metadata_json={"workspace_snapshots": {str(checkpoint_id): snapshot}},
        ),
        snapshot,
    )


@pytest.mark.asyncio
async def test_operator_only_cannot_restore_even_an_owned_workspace_manifest(monkeypatch):
    import app.api.files as files_api

    agent_id = uuid4()
    user_id = uuid4()

    async def deny_generic_agent_access(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="No access to this agent")

    async def unexpected_version_read(*_args, **_kwargs):
        raise AssertionError("version evidence must not be read before mutation authority")

    monkeypatch.setattr(files_api, "check_agent_access", deny_generic_agent_access)
    monkeypatch.setattr(files_api, "_authorized_workspace_version_sessions", unexpected_version_read)

    with pytest.raises(HTTPException) as exc_info:
        await files_api.restore_file_version(
            agent_id=agent_id,
            version_id="owned-manifest-version",
            path="workspace/report.md",
            data=files_api.FileVersionRestoreRequest(
                expected_current_exists=True,
                expected_current_hash="0" * 64,
            ),
            operator_view=True,
            operator_reason="Incident inspection",
            current_user=SimpleNamespace(id=user_id, tenant_id=uuid4()),
            db=_FakeDB([]),
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_file_versions_api_lists_opaque_authorized_checkpoint_versions(tmp_path, monkeypatch):
    import app.api.files as files_api
    import app.services.workspace_resource_authority as authority

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session, _snapshot = _snapshot_session(agent_id=agent_id, tmp_path=tmp_path)
    current_path = tmp_path / str(agent_id) / "workspace" / "report.md"
    current_path.write_text("current content", encoding="utf-8")
    db = _FakeDB([session])
    calls = []

    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    async def fake_authorize(*_args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            owner_user_id=user_id,
            root_session_id=session.id,
            authority_source="resource_owner",
            operator_view=False,
            manifest=SimpleNamespace(),
        )

    monkeypatch.setattr(files_api, "check_agent_access", fake_access)
    monkeypatch.setattr(authority, "authorize_workspace_path", fake_authorize)

    page = await files_api.list_file_versions(
        agent_id=agent_id,
        path="workspace/report.md",
        offset=0,
        limit=20,
        current_user=SimpleNamespace(id=user_id, tenant_id=tenant_id),
        db=db,
    )

    assert page.path == "workspace/report.md"
    assert page.current.exists is True
    assert page.current.content_hash == hashlib.sha256(b"current content").hexdigest()
    assert len(page.versions) == 1
    assert page.versions[0].state == "available"
    assert len(page.versions[0].version_id) == 40
    serialized = page.model_dump_json()
    assert str(session.id) not in serialized
    assert "checkpoint_event_id" not in serialized
    assert calls[0]["action"] == "read"
    assert calls[0]["path"] == "workspace/report.md"

    content = await files_api.read_file_version(
        agent_id=agent_id,
        version_id=page.versions[0].version_id,
        path="workspace/report.md",
        current_user=SimpleNamespace(id=user_id, tenant_id=tenant_id),
        db=db,
    )
    assert content.content == "checkpoint content"
    assert content.is_binary is False

    download = await files_api.download_file_version(
        agent_id=agent_id,
        version_id=page.versions[0].version_id,
        path="workspace/report.md",
        current_user=SimpleNamespace(id=user_id, tenant_id=tenant_id),
        db=db,
    )
    assert download.body == b"checkpoint content"
    assert "report.md" in download.headers["content-disposition"]


@pytest.mark.asyncio
async def test_file_version_restore_is_atomic_audited_and_stale_safe(tmp_path, monkeypatch):
    import app.api.files as files_api
    import app.services.workspace_resource_authority as authority

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session, _snapshot = _snapshot_session(agent_id=agent_id, tmp_path=tmp_path)
    report = tmp_path / str(agent_id) / "workspace" / "report.md"
    report.write_text("current content", encoding="utf-8")
    current_hash = hashlib.sha256(b"current content").hexdigest()
    db = _FakeDB([session])
    registered = []

    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    async def fake_authorize(*_args, **_kwargs):
        return SimpleNamespace(
            owner_user_id=user_id,
            root_session_id=session.id,
            authority_source="resource_owner",
            operator_view=False,
            manifest=SimpleNamespace(),
        )

    async def fake_register(*_args, **kwargs):
        registered.append(kwargs)

    monkeypatch.setattr(files_api, "check_agent_access", fake_access)
    monkeypatch.setattr(authority, "authorize_workspace_path", fake_authorize)
    monkeypatch.setattr(authority, "register_workspace_path", fake_register)

    page = await files_api.list_file_versions(
        agent_id=agent_id,
        path="workspace/report.md",
        offset=0,
        limit=20,
        current_user=SimpleNamespace(id=user_id, tenant_id=tenant_id),
        db=db,
    )
    version_id = page.versions[0].version_id

    with pytest.raises(HTTPException) as stale:
        await files_api.restore_file_version(
            agent_id=agent_id,
            version_id=version_id,
            path="workspace/report.md",
            data=files_api.FileVersionRestoreRequest(
                expected_current_exists=True,
                expected_current_hash="0" * 64,
            ),
            current_user=SimpleNamespace(id=user_id, tenant_id=tenant_id),
            db=db,
        )
    assert stale.value.status_code == 409
    assert report.read_text(encoding="utf-8") == "current content"
    assert db.commits == 0

    result = await files_api.restore_file_version(
        agent_id=agent_id,
        version_id=version_id,
        path="workspace/report.md",
        data=files_api.FileVersionRestoreRequest(
            expected_current_exists=True,
            expected_current_hash=current_hash,
        ),
        current_user=SimpleNamespace(id=user_id, tenant_id=tenant_id),
        db=db,
    )

    assert result.status == "restored"
    assert report.read_text(encoding="utf-8") == "checkpoint content"
    assert db.commits == 1
    assert db.rollbacks == 0
    assert registered[0]["source"] == "workspace_file_version_restore"
    audit = next(item for item in db.added if item.action == "workspace_file_version_restored")
    assert audit.tenant_id == tenant_id
    assert audit.user_id == user_id
    assert audit.details["previous_content_hash"] == current_hash
    assert audit.details["target_content_hash"] == hashlib.sha256(b"checkpoint content").hexdigest()
    assert audit.details["transaction_id"]


@pytest.mark.asyncio
async def test_file_version_restore_rolls_back_filesystem_when_database_commit_fails(tmp_path, monkeypatch):
    import app.api.files as files_api
    import app.services.workspace_resource_authority as authority

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session, _snapshot = _snapshot_session(agent_id=agent_id, tmp_path=tmp_path)
    report = tmp_path / str(agent_id) / "workspace" / "report.md"
    report.write_text("current content", encoding="utf-8")
    current_hash = hashlib.sha256(b"current content").hexdigest()
    db = _FakeDB([session], fail_commit=True)

    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    async def fake_authorize(*_args, **_kwargs):
        return SimpleNamespace(
            owner_user_id=user_id,
            root_session_id=session.id,
            authority_source="resource_owner",
            operator_view=False,
            manifest=SimpleNamespace(),
        )

    async def fake_register(*_args, **_kwargs):
        return None

    monkeypatch.setattr(files_api, "check_agent_access", fake_access)
    monkeypatch.setattr(authority, "authorize_workspace_path", fake_authorize)
    monkeypatch.setattr(authority, "register_workspace_path", fake_register)

    page = await files_api.list_file_versions(
        agent_id=agent_id,
        path="workspace/report.md",
        offset=0,
        limit=20,
        current_user=SimpleNamespace(id=user_id, tenant_id=tenant_id),
        db=db,
    )

    with pytest.raises(RuntimeError, match="database commit failed"):
        await files_api.restore_file_version(
            agent_id=agent_id,
            version_id=page.versions[0].version_id,
            path="workspace/report.md",
            data=files_api.FileVersionRestoreRequest(
                expected_current_exists=True,
                expected_current_hash=current_hash,
            ),
            current_user=SimpleNamespace(id=user_id, tenant_id=tenant_id),
            db=db,
        )

    assert report.read_text(encoding="utf-8") == "current content"
    assert db.commits == 1
    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_file_version_restore_can_restore_a_deleted_checkpoint(tmp_path, monkeypatch):
    import app.api.files as files_api
    import app.services.workspace_resource_authority as authority
    from app.services.session_workspace_snapshot import capture_workspace_snapshot

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session, _present_snapshot = _snapshot_session(agent_id=agent_id, tmp_path=tmp_path)
    report = tmp_path / str(agent_id) / "workspace" / "report.md"
    report.unlink()
    deleted_checkpoint_id = uuid4()
    deleted_snapshot = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=session.id,
        checkpoint_event_id=deleted_checkpoint_id,
        data_root=tmp_path,
    )
    session.transcript_metadata_json["workspace_snapshots"][str(deleted_checkpoint_id)] = deleted_snapshot
    report.write_text("current content", encoding="utf-8")
    current_hash = hashlib.sha256(b"current content").hexdigest()
    db = _FakeDB([session])
    deleted_paths = []
    mutation_actions = []

    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_access(*_args, **_kwargs):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    async def fake_authorize(*_args, **kwargs):
        mutation_actions.append(kwargs["action"])
        return SimpleNamespace(
            owner_user_id=user_id,
            root_session_id=session.id,
            authority_source="resource_owner",
            operator_view=False,
            manifest=SimpleNamespace(),
        )

    async def fake_mark_deleted(_db, *, agent_id, path):
        deleted_paths.append((agent_id, path))

    monkeypatch.setattr(files_api, "check_agent_access", fake_access)
    monkeypatch.setattr(authority, "authorize_workspace_path", fake_authorize)
    monkeypatch.setattr(authority, "mark_workspace_path_deleted", fake_mark_deleted)

    page = await files_api.list_file_versions(
        agent_id=agent_id,
        path="workspace/report.md",
        offset=0,
        limit=20,
        current_user=SimpleNamespace(id=user_id, tenant_id=tenant_id),
        db=db,
    )
    deleted_version = next(item for item in page.versions if item.state == "deleted")

    result = await files_api.restore_file_version(
        agent_id=agent_id,
        version_id=deleted_version.version_id,
        path="workspace/report.md",
        data=files_api.FileVersionRestoreRequest(
            expected_current_exists=True,
            expected_current_hash=current_hash,
        ),
        current_user=SimpleNamespace(id=user_id, tenant_id=tenant_id),
        db=db,
    )

    assert result.status == "restored"
    assert result.current.exists is False
    assert not report.exists()
    assert deleted_paths == [(agent_id, "workspace/report.md")]
    assert mutation_actions[-1] == "delete"
    audit = next(item for item in db.added if item.action == "workspace_file_version_restored")
    assert audit.details["target_exists"] is False
    assert audit.details["target_content_hash"] is None

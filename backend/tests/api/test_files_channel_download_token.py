from __future__ import annotations

from datetime import timedelta
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException


def _settings(tmp_path):
    return SimpleNamespace(
        AGENT_DATA_DIR=str(tmp_path),
        BASE_URL="https://backend.example.com",
        PUBLIC_BASE_URL="https://backend.example.com",
        JWT_SECRET_KEY="jwt-secret",
        JWT_ALGORITHM="HS256",
    )


@pytest.mark.asyncio
async def test_channel_file_download_token_allows_external_file_download(tmp_path, monkeypatch) -> None:
    import app.api.files as files_api
    import app.services.file_download_tokens as token_service

    settings = _settings(tmp_path)
    monkeypatch.setattr(files_api, "settings", settings)
    monkeypatch.setattr(token_service, "get_settings", lambda: settings)

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    target = workspace / "report.md"
    target.write_text("hello", encoding="utf-8")

    token = token_service.make_channel_file_download_token(
        agent_id=agent_id,
        path="workspace/report.md",
        expires_delta=timedelta(minutes=5),
    )

    response = await files_api.download_file(
        agent_id=agent_id,
        path="workspace/report.md",
        token=token,
        credentials=None,
        db=object(),
    )

    assert response.path == str(target)
    assert response.filename == "report.md"


@pytest.mark.asyncio
async def test_channel_file_download_token_rejects_path_mismatch(tmp_path, monkeypatch) -> None:
    import app.api.files as files_api
    import app.services.file_download_tokens as token_service

    settings = _settings(tmp_path)
    monkeypatch.setattr(files_api, "settings", settings)
    monkeypatch.setattr(token_service, "get_settings", lambda: settings)

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("hello", encoding="utf-8")
    (workspace / "other.md").write_text("other", encoding="utf-8")

    token = token_service.make_channel_file_download_token(
        agent_id=agent_id,
        path="workspace/report.md",
        expires_delta=timedelta(minutes=5),
    )

    with pytest.raises(HTTPException) as exc:
        await files_api.download_file(
            agent_id=agent_id,
            path="workspace/other.md",
            token=token,
            credentials=None,
            db=object(),
        )

    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_channel_file_download_token_rejects_content_changed_after_authorization(
    tmp_path,
    monkeypatch,
) -> None:
    import app.api.files as files_api
    import app.services.file_download_tokens as token_service

    settings = _settings(tmp_path)
    monkeypatch.setattr(files_api, "settings", settings)
    monkeypatch.setattr(token_service, "get_settings", lambda: settings)

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    target = workspace / "report.md"
    target.write_text("safe report", encoding="utf-8")
    token = token_service.make_channel_file_download_token(
        agent_id=agent_id,
        path="workspace/report.md",
        content_sha256=hashlib.sha256(b"safe report").hexdigest(),
        expires_delta=timedelta(minutes=5),
    )
    target.write_text("changed after authorization", encoding="utf-8")

    with pytest.raises(HTTPException) as exc:
        await files_api.download_file(
            agent_id=agent_id,
            path="workspace/report.md",
            token=token,
            credentials=None,
            db=object(),
        )

    assert exc.value.status_code == 409
    assert "changed" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_content_bound_channel_download_serves_the_verified_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    import app.api.files as files_api
    import app.services.file_download_tokens as token_service

    settings = _settings(tmp_path)
    monkeypatch.setattr(files_api, "settings", settings)
    monkeypatch.setattr(token_service, "get_settings", lambda: settings)

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    target = workspace / "report.md"
    target.write_text("safe report", encoding="utf-8")
    token = token_service.make_channel_file_download_token(
        agent_id=agent_id,
        path="workspace/report.md",
        content_sha256=hashlib.sha256(b"safe report").hexdigest(),
        expires_delta=timedelta(minutes=5),
    )

    response = await files_api.download_file(
        agent_id=agent_id,
        path="workspace/report.md",
        token=token,
        credentials=None,
        db=object(),
    )
    served_path = Path(response.path)
    target.write_text("changed after response creation", encoding="utf-8")

    assert served_path != target
    assert served_path.read_text(encoding="utf-8") == "safe report"
    assert response.background is not None
    await response.background()
    assert not served_path.exists()


def test_build_channel_file_download_url_uses_signed_token_and_quoted_path(tmp_path, monkeypatch) -> None:
    import app.services.file_download_tokens as token_service

    settings = _settings(tmp_path)
    monkeypatch.setattr(token_service, "get_settings", lambda: settings)

    agent_id = uuid4()
    url = token_service.build_channel_file_download_url(
        agent_id=agent_id,
        path="workspace/Serenity_投资观点追踪.md",
        expires_delta=timedelta(minutes=5),
    )

    assert url.startswith(f"https://backend.example.com/api/agents/{agent_id}/files/download?")
    assert "path=workspace%2FSerenity_" in url
    assert "token=" in url


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RowResult:
    """Row-shaped result for the canonical user+tenant liveness lookup."""

    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _RlsScopedUserDb:
    def __init__(self, *, tenant_id, user):
        self.tenant_id = tenant_id
        self.user = user
        self.pinned_tenants = []

    async def execute(self, _statement):
        if self.pinned_tenants and self.pinned_tenants[-1] == self.tenant_id:
            return _RowResult((self.user, True))
        return _RowResult(None)


async def _pin_fake_download_tenant(db, tenant_id):
    db.pinned_tenants.append(UUID(str(tenant_id)))


@pytest.mark.asyncio
async def test_workspace_download_query_jwt_pins_token_tenant_before_user_lookup(tmp_path, monkeypatch) -> None:
    import app.api.files as files_api
    import app.core.security as security

    settings = _settings(tmp_path)
    monkeypatch.setattr(files_api, "settings", settings)
    monkeypatch.setattr(files_api, "pin_rls_tenant_context", _pin_fake_download_tenant, raising=False)

    agent_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    target = workspace / "report.md"
    target.write_text("hello", encoding="utf-8")
    user = SimpleNamespace(id=user_id, is_active=True, tenant_id=tenant_id, role="member")
    db = _RlsScopedUserDb(tenant_id=tenant_id, user=user)

    monkeypatch.setattr(
        security,
        "decode_access_token",
        lambda _token: {"sub": str(user_id), "role": "member", "tid": str(tenant_id)},
    )
    monkeypatch.setattr(
        files_api,
        "verify_channel_file_download_token",
        lambda **_kwargs: (_ for _ in ()).throw(files_api.NotChannelFileDownloadToken()),
    )

    async def fake_check_agent_access(check_db, check_user, check_agent_id):
        assert check_db is db
        assert check_user is user
        assert check_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)
    from app.services import workspace_resource_authority

    async def fake_authorize_workspace_path(*_args, **_kwargs):
        return SimpleNamespace(authority_source="resource_owner", operator_view=False)

    monkeypatch.setattr(workspace_resource_authority, "authorize_workspace_path", fake_authorize_workspace_path)

    # The canonical download lane authenticates through
    # ``authenticate_request_user``; a query-JWT request carries no
    # X-Tenant-Id selection, so the token's own tenant is pinned first —
    # exactly what TenantMiddleware does for a Bearer header.
    response = await files_api.download_file(
        agent_id=agent_id,
        path="workspace/report.md",
        token="browser-query-jwt",
        request=SimpleNamespace(headers={}),
        credentials=None,
        db=db,
    )

    assert response.path == str(target)
    assert response.filename == "report.md"
    assert db.pinned_tenants == [tenant_id]


@pytest.mark.asyncio
async def test_artifact_download_query_jwt_pins_token_tenant_before_user_lookup(tmp_path, monkeypatch) -> None:
    import app.api.files as files_api
    import app.core.security as security

    settings = _settings(tmp_path)
    monkeypatch.setattr(files_api, "settings", settings)
    monkeypatch.setattr(files_api, "pin_rls_tenant_context", _pin_fake_download_tenant, raising=False)

    agent_id = uuid4()
    artifact_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    target = workspace / "artifact.md"
    target.write_text("artifact", encoding="utf-8")
    user = SimpleNamespace(id=user_id, is_active=True, tenant_id=tenant_id, role="member")
    db = _RlsScopedUserDb(tenant_id=tenant_id, user=user)

    monkeypatch.setattr(
        security,
        "decode_access_token",
        lambda _token: {"sub": str(user_id), "role": "member", "tid": str(tenant_id)},
    )

    async def fake_check_agent_access(check_db, check_user, check_agent_id):
        assert check_db is db
        assert check_user is user
        assert check_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "manage"

    async def fake_load_artifact_or_404(*, db, agent_id, artifact_id):
        return SimpleNamespace(
            id=artifact_id,
            agent_id=agent_id,
            path="workspace/artifact.md",
            name="artifact.md",
            snapshot_json={},
        )

    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(files_api, "_load_chat_artifact_or_404", fake_load_artifact_or_404)

    async def fake_authorize_resource_action(*_args, **_kwargs):
        return SimpleNamespace(authority_source="resource_owner", operator_view=False)

    monkeypatch.setattr(files_api, "authorize_resource_action", fake_authorize_resource_action)

    # Same canonical lane as the workspace download: no X-Tenant-Id on a
    # query-JWT request, so the token's own tenant is pinned first.
    response = await files_api.download_artifact(
        agent_id=agent_id,
        artifact_id=artifact_id,
        token="browser-query-jwt",
        request=SimpleNamespace(headers={}),
        credentials=None,
        db=db,
    )

    assert response.path == str(target)
    assert response.filename == "artifact.md"
    assert db.pinned_tenants == [tenant_id]


@pytest.mark.asyncio
async def test_artifact_download_does_not_fall_back_when_declared_snapshot_is_missing(tmp_path, monkeypatch) -> None:
    import app.api.files as files_api

    settings = _settings(tmp_path)
    monkeypatch.setattr(files_api, "settings", settings)

    agent_id = uuid4()
    artifact_id = uuid4()
    current = tmp_path / str(agent_id) / "workspace" / "artifact.md"
    current.parent.mkdir(parents=True)
    current.write_text("new current content", encoding="utf-8")
    user = SimpleNamespace(id=uuid4(), is_active=True, tenant_id=uuid4(), role="member")

    async def fake_load_user(**_kwargs):
        return user

    async def fake_check_agent_access(*_args):
        return SimpleNamespace(id=agent_id, tenant_id=user.tenant_id), "manage"

    async def fake_load_artifact_or_404(**_kwargs):
        return SimpleNamespace(
            id=artifact_id,
            agent_id=agent_id,
            path="workspace/artifact.md",
            name="artifact.md",
            snapshot_json={
                "snapshot_storage_path": "runtime_artifacts/chat_artifact_snapshots/missing/artifact.md",
            },
        )

    async def fake_authorize_resource_action(*_args, **_kwargs):
        return SimpleNamespace(authority_source="resource_owner", operator_view=False)

    monkeypatch.setattr(files_api, "_load_download_user_from_jwt", fake_load_user)
    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(files_api, "_load_chat_artifact_or_404", fake_load_artifact_or_404)
    monkeypatch.setattr(files_api, "authorize_resource_action", fake_authorize_resource_action)

    with pytest.raises(HTTPException) as exc:
        await files_api.download_artifact(
            agent_id=agent_id,
            artifact_id=artifact_id,
            token="browser-query-jwt",
            credentials=None,
            db=object(),
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Artifact delivery snapshot is no longer available"

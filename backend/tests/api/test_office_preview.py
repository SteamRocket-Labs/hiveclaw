from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ArtifactDB:
    def __init__(self, artifact):
        self.artifact = artifact

    async def execute(self, _statement):
        return _ScalarResult(self.artifact)


def _preview_result(html="<!DOCTYPE html><html><head></head><body>Preview</body></html>"):
    return SimpleNamespace(
        html=html,
        preview_mode="html",
        source_sha256="a" * 64,
        renderer_version="1.0.88",
        cache_hit=False,
        output_bytes=len(html.encode("utf-8")),
        fallback_reason=None,
    )


@pytest.mark.asyncio
async def test_workspace_office_preview_returns_isolated_html_headers(tmp_path, monkeypatch):
    import app.api.office as office_api

    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    target = tmp_path / str(agent_id) / "workspace" / "report.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"docx")
    monkeypatch.setattr(
        office_api,
        "settings",
        SimpleNamespace(AGENT_DATA_DIR=str(tmp_path), OFFICECLI_PREVIEW_MAX_BYTES=1024 * 1024),
    )

    async def fake_access(*_args):
        return SimpleNamespace(id=agent_id, tenant_id=user.tenant_id), "use"

    captured_authority = {}

    async def fake_authorize(_db, _user, **kwargs):
        captured_authority.update(kwargs)
        return SimpleNamespace(authority_source="resource_owner", operator_view=False)

    spans = []

    async def fake_persist_span(**kwargs):
        spans.append(kwargs)

    monkeypatch.setattr(office_api, "check_agent_access", fake_access)
    monkeypatch.setattr(office_api, "check_agent_operator_reachability", fake_access)
    monkeypatch.setattr(office_api, "authorize_workspace_path", fake_authorize)
    monkeypatch.setattr(office_api, "persist_invocation_span", fake_persist_span, raising=False)
    monkeypatch.setattr(office_api.OfficeDocumentService, "render_preview", lambda *_args, **_kwargs: _preview_result())

    response = await office_api.preview_office_document(
        agent_id=agent_id,
        path="workspace/report.docx",
        operator_view=True,
        operator_reason="Agent session administration",
        current_user=user,
        db=SimpleNamespace(),
    )

    assert response.media_type == "text/html"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-office-preview-mode"] == "html"
    assert response.headers["x-office-source-sha256"] == "a" * 64
    assert response.headers["x-office-preview-trace-id"].startswith("office-preview-")
    assert "sandbox allow-scripts" in response.headers["content-security-policy"]
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert b"Preview" in response.body
    assert captured_authority["allow_manager_override"] is True
    assert captured_authority["manager_override_reason"] == "Agent session administration"
    assert len(spans) == 1
    assert spans[0]["span_type"] == "office_preview"
    assert spans[0]["status"] == "ok"
    assert spans[0]["metadata"]["source_sha256"] == "a" * 64


@pytest.mark.asyncio
async def test_artifact_office_preview_uses_delivery_snapshot_and_resource_authority(tmp_path, monkeypatch):
    import app.api.office as office_api

    agent_id = uuid4()
    artifact_id = uuid4()
    tenant_id = uuid4()
    user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    snapshot_rel = ".chat_artifact_snapshots/session/run/deck.pptx"
    snapshot = tmp_path / str(agent_id) / snapshot_rel
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"delivery-snapshot")
    current = tmp_path / str(agent_id) / "workspace" / "deck.pptx"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"new-current-file")
    artifact = SimpleNamespace(
        id=artifact_id,
        agent_id=agent_id,
        owner_user_id=user.id,
        root_session_id=uuid4(),
        session_id=uuid4(),
        authority_state="owned",
        path="workspace/deck.pptx",
        snapshot_json={"snapshot_storage_path": snapshot_rel},
    )
    monkeypatch.setattr(
        office_api,
        "settings",
        SimpleNamespace(AGENT_DATA_DIR=str(tmp_path), OFFICECLI_PREVIEW_MAX_BYTES=1024 * 1024),
    )
    captured = {}

    async def fake_access(*_args):
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), "use"

    async def fake_authorize_resource(*_args, **kwargs):
        captured["authority"] = kwargs
        return SimpleNamespace(authority_source="resource_owner", operator_view=False)

    def fake_render(_service, target, *, cache_key):
        captured["target"] = target
        captured["cache_key"] = cache_key
        return _preview_result()

    async def fake_persist_span(**_kwargs):
        return None

    monkeypatch.setattr(office_api, "check_agent_access", fake_access)
    monkeypatch.setattr(office_api, "authorize_resource_action", fake_authorize_resource)
    monkeypatch.setattr(office_api, "persist_invocation_span", fake_persist_span)
    monkeypatch.setattr(office_api.OfficeDocumentService, "render_preview_target", fake_render)

    response = await office_api.preview_office_artifact(
        agent_id=agent_id,
        artifact_id=artifact_id,
        operator_view=False,
        operator_reason=None,
        current_user=user,
        db=_ArtifactDB(artifact),
    )

    assert response.headers["x-office-artifact-source"] == "delivery_snapshot"
    assert captured["target"] == snapshot
    assert captured["cache_key"] == f"artifact:{artifact_id}"
    assert captured["authority"]["resource_kind"] == "chat_artifact"
    assert captured["authority"]["resource_id"] == artifact_id


@pytest.mark.asyncio
async def test_workspace_office_preview_preserves_foreign_resource_denial(tmp_path, monkeypatch):
    import app.api.office as office_api
    from app.services.workspace_resource_authority import WorkspaceAuthorityError

    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    target = tmp_path / str(agent_id) / "workspace" / "foreign.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"foreign")
    monkeypatch.setattr(office_api, "settings", SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    async def fake_access(*_args):
        return SimpleNamespace(id=agent_id, tenant_id=user.tenant_id), "use"

    async def deny(*_args, **_kwargs):
        raise WorkspaceAuthorityError("workspace_resource_forbidden", "foreign resource")

    monkeypatch.setattr(office_api, "check_agent_access", fake_access)
    monkeypatch.setattr(office_api, "authorize_workspace_path", deny)

    with pytest.raises(Exception) as exc_info:
        await office_api.preview_office_document(
            agent_id=agent_id,
            path="workspace/foreign.docx",
            operator_view=False,
            operator_reason=None,
            current_user=user,
            db=SimpleNamespace(),
        )

    assert getattr(exc_info.value, "status_code", None) == 403
    assert "workspace_resource_forbidden" in str(getattr(exc_info.value, "detail", ""))


@pytest.mark.asyncio
async def test_artifact_office_preview_preserves_foreign_resource_denial(tmp_path, monkeypatch):
    import app.api.office as office_api

    agent_id = uuid4()
    artifact_id = uuid4()
    user = SimpleNamespace(id=uuid4(), tenant_id=uuid4())
    artifact = SimpleNamespace(
        id=artifact_id,
        agent_id=agent_id,
        owner_user_id=uuid4(),
        root_session_id=uuid4(),
        session_id=uuid4(),
        authority_state="owned",
        path="workspace/foreign.docx",
        snapshot_json={},
    )

    async def fake_access(*_args):
        return SimpleNamespace(id=agent_id, tenant_id=user.tenant_id), "use"

    async def deny(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="This resource belongs to a different principal")

    render_calls = []
    monkeypatch.setattr(office_api, "check_agent_access", fake_access)
    monkeypatch.setattr(office_api, "authorize_resource_action", deny)
    monkeypatch.setattr(
        office_api.OfficeDocumentService,
        "render_preview_target",
        lambda *_args, **_kwargs: render_calls.append(True),
    )

    with pytest.raises(HTTPException) as exc:
        await office_api.preview_office_artifact(
            agent_id=agent_id,
            artifact_id=artifact_id,
            operator_view=False,
            operator_reason=None,
            current_user=user,
            db=_ArtifactDB(artifact),
        )

    assert exc.value.status_code == 403
    assert render_calls == []


def test_office_preview_unavailable_maps_to_typed_retryable_http_error():
    import app.api.office as office_api
    from app.services.office_document_service import OfficePreviewUnavailableError

    error = office_api._preview_http_error(OfficePreviewUnavailableError("OfficeCLI unavailable"))

    assert error.status_code == 503
    assert error.detail == {
        "code": "office_preview_unavailable",
        "message": "OfficeCLI unavailable",
    }

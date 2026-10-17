"""Browser source inspection preserves session privacy and administrator audit."""

from pathlib import Path

import pytest
from sqlalchemy import select

from app.api.files import _load_download_user_from_jwt
from app.core.security import create_access_token
from app.models.chat_session import ChatSession
from app.models.security_audit import SecurityAuditEvent
from app.models.user import User
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m1g40_session_file_actor_postgres import _files

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _get(n, operation, path):
    endpoint = {"read": "/content", "download": "/download", "list": "/"}[operation]
    return await n.client.get(f"/agents/{n.agent}/files{endpoint}", params={"path": path})


@pytest.mark.parametrize("kind", ["t0", "t2", "legacy"])
@pytest.mark.parametrize("operation", ["read", "download", "list"])
async def test_agent_management_never_grants_another_requesters_private_source(
    native_workspace, owner_sessionmaker, kind, operation,  # noqa: F811
):
    n = native_workspace
    own, foreign, paths = await _files(n, owner_sessionmaker, kind)
    if operation == "list":
        root = paths[own].split(f"/{own}/")[0]
        response = await _get(n, operation, root)
        assert response.status_code == 200 and str(own) in response.text, response.text
        assert str(foreign) not in response.text, response.text
        foreign_path = paths[foreign].rsplit("/", 1)[0]
    else:
        allowed = await _get(n, operation, paths[own])
        assert allowed.status_code == 200 and "OWN_PRIVATE_EVIDENCE" in allowed.text, allowed.text
        foreign_path = paths[foreign]
    for path in [foreign_path, foreign_path + ".missing"]:
        denied = await _get(n, operation, path)
        assert denied.status_code == 403 and "OTHER_REQUESTER_PRIVATE_EVIDENCE" not in denied.text, denied.text


@pytest.mark.parametrize("operation", ["read", "download", "list"])
async def test_scoped_admin_source_inspection_keeps_ordinary_access_and_real_actor_audit(
    native_workspace, owner_sessionmaker, operation,  # noqa: F811
):
    n = native_workspace
    own, foreign, paths = await _files(n, owner_sessionmaker, "t0")
    async with owner_sessionmaker() as db:
        (await db.get(User, n.user)).role = "org_admin"
        await db.commit()
    response = await _get(n, operation, paths[foreign] if operation != "list" else paths[foreign].rsplit("/", 1)[0])
    assert response.status_code == 200, response.text
    assert "OTHER_REQUESTER_PRIVATE_EVIDENCE" in response.text if operation != "list" else "source.md" in response.text
    async with owner_sessionmaker() as db:
        rows = (await db.scalars(select(SecurityAuditEvent).where(SecurityAuditEvent.tenant_id == n.tenant,
            SecurityAuditEvent.actor_id == n.user, SecurityAuditEvent.event_type == "session.scoped_business_admin_access"))).all()
        assert rows and any(str(foreign) == str(row.resource_id) for row in rows)
        assert all("PRIVATE_EVIDENCE" not in str(row.details) for row in rows)
    assert own != foreign


@pytest.mark.parametrize("operation", ["read", "download", "list"])
@pytest.mark.parametrize("prefix", ["logs", "memory/../logs", "skills/../logs"])
async def test_normalized_paths_cannot_bypass_the_existing_raw_log_guard(
    native_workspace, operation, prefix,  # noqa: F811
):
    n = native_workspace
    path = n.root / "logs/probe.md"
    path.parent.mkdir(parents=True)
    path.write_text("PRIVATE_LOG_BODY")
    response = await _get(n, operation, prefix if operation == "list" else f"{prefix}/probe.md")
    assert response.status_code == 403 and "PRIVATE_LOG_BODY" not in response.text, response.text
    assert path.read_text() == "PRIVATE_LOG_BODY"


@pytest.mark.parametrize("operation", ["read", "download"])
async def test_source_owner_change_during_file_capture_prevents_release_and_cleans_snapshot(
    native_workspace, owner_sessionmaker, monkeypatch, operation,  # noqa: F811
):
    from app.api import files

    n = native_workspace
    own, _foreign, paths = await _files(n, owner_sessionmaker, "t0")
    original = files.asyncio.to_thread
    snapshots = []

    async def change_source(function, *args, **kwargs):
        result = await original(function, *args, **kwargs)
        if function.__name__ in {"read_content", "snapshot_workspace_file"}:
            if function.__name__ == "snapshot_workspace_file":
                snapshots.append(Path(result[0].name))
            async with owner_sessionmaker() as db:
                (await db.get(ChatSession, own)).user_id = n.foreign
                await db.commit()
        return result

    monkeypatch.setattr(files.asyncio, "to_thread", change_source)
    response = await _get(n, operation, paths[own])
    assert response.status_code == 403 and "OWN_PRIVATE_EVIDENCE" not in response.text, response.text
    assert all(not path.exists() for path in snapshots)


@pytest.mark.parametrize("admin", [False, True])
async def test_unknown_source_files_require_inspection_authority_and_remain_preserved(
    native_workspace, owner_sessionmaker, admin,  # noqa: F811
):
    n = native_workspace
    path = n.root / "memory/t0/sessions/unregistered/legacy.md"
    path.parent.mkdir(parents=True)
    path.write_text("UNREGISTERED_SOURCE_BODY")
    async with owner_sessionmaker() as db:
        (await db.get(User, n.user)).role = "org_admin" if admin else "member"
        await db.commit()
    response = await _get(n, "read", path.relative_to(n.root).as_posix())
    assert response.status_code == (200 if admin else 403), response.text
    assert ("UNREGISTERED_SOURCE_BODY" in response.text) is admin
    assert path.read_text() == "UNREGISTERED_SOURCE_BODY"


@pytest.mark.parametrize("admin", [False, True])
async def test_shared_source_owner_cannot_replace_current_audience_authority_with_a_label(
    native_workspace, owner_sessionmaker, admin,  # noqa: F811
):
    n = native_workspace
    own, _foreign, paths = await _files(n, owner_sessionmaker, "t0")
    async with owner_sessionmaker() as db:
        (await db.get(ChatSession, own)).audience_kind = "group"
        (await db.get(User, n.user)).role = "org_admin" if admin else "member"
        await db.commit()
    response = await _get(n, "read", paths[own])
    assert response.status_code == (200 if admin else 503), response.text
    assert ("OWN_PRIVATE_EVIDENCE" in response.text) is admin


async def test_download_jwt_authentication_reaches_the_same_source_boundary(
    native_workspace, owner_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.api import files

    n = native_workspace
    own, foreign, paths = await _files(n, owner_sessionmaker, "t0")
    monkeypatch.setattr(files, "_load_download_user_from_jwt", _load_download_user_from_jwt)
    token = create_access_token(str(n.user), "member", str(n.tenant))
    for source, expected in [(own, 200), (foreign, 403)]:
        response = await n.client.get(f"/agents/{n.agent}/files/download", params={"path": paths[source]},
            headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == expected, response.text
        assert "OTHER_REQUESTER_PRIVATE_EVIDENCE" not in response.text

"""Logical ownership cannot authorize another requester's physical file."""

import hashlib
import io
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.database import pin_rls_tenant_context
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "parent_symlink"])
@pytest.mark.parametrize(("operation", "replace_after_authorize"), [
    ("read", False), ("write", False), ("upload", False), ("office", False),
    ("read", True), ("write", True), ("upload", True),
    ("download", False), ("download", True),
    ("jwt_send", False), ("channel_send", False), ("bound_send", False),
])
async def test_workspace_alias_cannot_cross_file_ownership(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, alias, operation, replace_after_authorize,
):
    from app.api.files import FileWrite, download_file, read_file, upload_file_to_workspace, write_file
    from app.api.office import _authorize_office_request_path
    from starlette.datastructures import UploadFile
    from app.config import get_settings
    from app.models.agent import AgentPermission
    from app.models.user import User
    from app.services import workspace_resource_authority as authority
    from app.core.security import create_access_token
    from app.services.file_download_tokens import make_channel_file_download_token
    from tests.api.test_files_channel_download_token import _send_download

    tenant, user_id, agent, session, _run = await _seed_session(owner_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    root = tmp_path / str(agent)
    mine, theirs = root / "workspace/mine/report.txt", root / "workspace/theirs/report.txt"
    for file in (mine, theirs):
        file.parent.mkdir(parents=True)
    mine.write_text("My original content")
    theirs.write_text("Other requester's private content")
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        user.role = "member"
        other_id = uuid.uuid4()
        db.add(User(id=other_id, tenant_id=tenant, username=f"alias-{other_id.hex}", email=f"{other_id.hex}@fixture.test",
            password_hash="fixture", display_name="Other requester", role="member"))
        db.add(AgentPermission(tenant_id=tenant, agent_id=agent, scope_type="company", access_level="use"))
        await db.flush()
        for file, owner, source in ((mine, user_id, session), (theirs, other_id, None)):
            await authority.register_workspace_path(db, tenant_id=tenant, agent_id=agent, path=file.relative_to(root).as_posix(),
                owner_user_id=owner, root_session_id=source, source="fixture", content_hash=hashlib.sha256(file.read_bytes()).hexdigest())
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        with pytest.raises(HTTPException) as direct:
            await read_file(agent_id=agent, path="workspace/theirs/report.txt", current_user=user, db=db)
        assert direct.value.status_code == 403

    def replace_path():
        mine.unlink()
        if alias == "symlink":
            mine.symlink_to(theirs)
        elif alias == "hardlink":
            os.link(theirs, mine)
        else:
            mine.parent.rmdir()
            mine.parent.symlink_to(theirs.parent, target_is_directory=True)

    async def download(db, scheme="jwt"):
        if scheme == "jwt":
            token = create_access_token(str(user.id), user.role, str(tenant))
        else:
            token = make_channel_file_download_token(agent_id=agent, path="workspace/mine/report.txt",
                content_sha256=hashlib.sha256(b"My original content").hexdigest() if scheme == "bound" else None)
        return await download_file(agent_id=agent, path="workspace/mine/report.txt", token=token,
            request=SimpleNamespace(headers={}), credentials=None, db=db)

    if replace_after_authorize:
        original = authority.authorize_workspace_path

        async def authorize_then_replace(*args, **kwargs):
            decision = await original(*args, **kwargs)
            replace_path()
            return decision

        monkeypatch.setattr(authority, "authorize_workspace_path", authorize_then_replace)
    elif not operation.endswith("_send"):
        replace_path()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        if operation.endswith("_send"):
            response = await download(db, operation.removesuffix("_send"))
            replace_path()
            status, body = await _send_download(response)
            assert status == 200 and body == b"My original content"
            assert response.filename == "report.txt"
            assert not os.path.exists(response.path)
            assert theirs.read_text() == "Other requester's private content"
            return
        with pytest.raises(HTTPException) as indirect:
            if operation == "read":
                await read_file(agent_id=agent, path="workspace/mine/report.txt", current_user=user, db=db)
            elif operation == "write":
                await write_file(agent_id=agent, path="workspace/mine/report.txt", data=FileWrite(content="Unauthorized replacement"), current_user=user, db=db)
            elif operation == "upload":
                await upload_file_to_workspace(agent_id=agent, path="workspace/mine", file=UploadFile(io.BytesIO(b"Unauthorized replacement"), filename="report.txt"), operator_view=False, operator_reason=None, current_user=user, db=db)
            elif operation == "download":
                response = await download(db)
                _status, body = await _send_download(response)
                assert b"Other requester's private content" not in body
            else:
                await _authorize_office_request_path(db, user, agent_id=agent, path="workspace/mine/report.txt", action="read")
        assert indirect.value.status_code in {403, 409}
    assert theirs.read_text() == "Other requester's private content"


@pytest.mark.parametrize("other_owner", [True, False])
async def test_upload_extraction_requires_its_own_resource_authority(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, other_owner,
):
    from app.api.files import upload_file_to_workspace
    from app.config import get_settings
    from app.models.user import User
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services.workspace_resource_authority import register_workspace_path
    from sqlalchemy import select
    from starlette.datastructures import UploadFile

    tenant, user_id, agent, session, _run = await _seed_session(owner_sessionmaker)
    root = tmp_path / str(agent)
    text = root / "workspace/report.txt"
    text.parent.mkdir(parents=True)
    text.write_text("Existing private text")
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        user.role = "member"
        other_id = uuid.uuid4()
        db.add(User(id=other_id, tenant_id=tenant, username=f"extract-{other_id.hex}", email=f"{other_id.hex}@fixture.test",
            password_hash="fixture", display_name="Other requester", role="member"))
        await db.flush()
        await register_workspace_path(db, tenant_id=tenant, agent_id=agent, path="workspace/report.txt",
            owner_user_id=other_id if other_owner else user_id, root_session_id=None if other_owner else session, source="fixture")
        await db.commit()

    def convert(upload_path, content, filename):
        assert upload_path.read_bytes() == content == b"Original upload bytes"
        result = upload_path.with_suffix(".txt")
        result.write_text("Extracted content")
        return result

    monkeypatch.setattr("app.services.text_extractor.save_extracted_text", convert)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        async def upload():
            return await upload_file_to_workspace(agent_id=agent, path="workspace/",
                file=UploadFile(io.BytesIO(b"Original upload bytes"), filename="report.docx"),
                operator_view=False, operator_reason=None, current_user=user, db=db)
        if other_owner:
            with pytest.raises(HTTPException) as denied:
                await upload()
            assert denied.value.status_code == 403
            assert text.read_text() == "Existing private text"
            assert not (root / "workspace/report.docx").exists()
        else:
            result = await upload()
            assert result["extracted_text_path"] == "workspace/report.txt"
            assert text.read_text() == "Extracted content"
            row = await db.scalar(select(WorkspaceResourceManifest).where(
                WorkspaceResourceManifest.agent_id == agent, WorkspaceResourceManifest.path == "workspace/report.txt"))
            assert row.owner_user_id == user_id and row.root_session_id == session

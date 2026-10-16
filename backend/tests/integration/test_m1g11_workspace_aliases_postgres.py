"""Logical ownership cannot authorize another requester's physical file."""

import hashlib
import io
import os
import uuid

import pytest
from fastapi import HTTPException

from app.database import pin_rls_tenant_context
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
@pytest.mark.parametrize("operation", ["read", "write", "upload", "office"])
async def test_workspace_alias_cannot_cross_file_ownership(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, alias, operation,
):
    from app.api.files import FileWrite, read_file, upload_file_to_workspace, write_file
    from app.api.office import _authorize_office_request_path
    from starlette.datastructures import UploadFile
    from app.config import get_settings
    from app.models.agent import AgentPermission
    from app.models.user import User
    from app.services.workspace_resource_authority import register_workspace_path

    tenant, user_id, agent, session, _run = await _seed_session(owner_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    root = tmp_path / str(agent)
    (root / "workspace").mkdir(parents=True)
    mine, theirs = root / "workspace/mine.txt", root / "workspace/theirs.txt"
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
            await register_workspace_path(db, tenant_id=tenant, agent_id=agent, path=f"workspace/{file.name}",
                owner_user_id=owner, root_session_id=source, source="fixture", content_hash=hashlib.sha256(file.read_bytes()).hexdigest())
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        with pytest.raises(HTTPException) as direct:
            await read_file(agent_id=agent, path="workspace/theirs.txt", current_user=user, db=db)
        assert direct.value.status_code == 403
    mine.unlink()
    if alias == "symlink":
        mine.symlink_to(theirs)
    else:
        os.link(theirs, mine)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        with pytest.raises(HTTPException) as indirect:
            if operation == "read":
                await read_file(agent_id=agent, path="workspace/mine.txt", current_user=user, db=db)
            elif operation == "write":
                await write_file(agent_id=agent, path="workspace/mine.txt", data=FileWrite(content="Unauthorized replacement"), current_user=user, db=db)
            elif operation == "upload":
                await upload_file_to_workspace(agent_id=agent, path="workspace/", file=UploadFile(io.BytesIO(b"Unauthorized replacement"), filename="mine.txt"), operator_view=False, operator_reason=None, current_user=user, db=db)
            else:
                await _authorize_office_request_path(db, user, agent_id=agent, path="workspace/mine.txt", action="read")
        assert indirect.value.status_code in {403, 409}
    assert theirs.read_text() == "Other requester's private content"

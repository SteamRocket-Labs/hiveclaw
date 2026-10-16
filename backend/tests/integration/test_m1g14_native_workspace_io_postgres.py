"""The registered native handlers must not follow a replaced resource after permission checks."""

import base64
import hashlib
import json
import os
import uuid

import pytest

from app.database import pin_rls_tenant_context
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "parent_symlink"])
@pytest.mark.parametrize("operation", ["read_file", "read_image", "write_file", "edit_file"])
async def test_native_file_io_uses_the_authorized_resource(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, alias, operation,
):
    from app.config import get_settings
    from app.models.user import User
    from app.services import workspace_resource_authority as authority
    from app.services.agent_tool_domains import workspace as domain
    from app.tools.adapters import adapt_and_call
    from app.tools.handlers import filesystem
    from app.tools.result_envelope import ToolContentEnvelope
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    tenant, user_id, agent, session, _run = await _seed_session(owner_sessionmaker)
    root = tmp_path / str(agent)
    suffix = ".png" if operation == "read_image" else ".txt"
    mine, theirs = (root / f"workspace/{owner}/report{suffix}" for owner in ("mine", "theirs"))
    for path, content in ((mine, b"Owned content"), (theirs, b"Other requester's private content")):
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        user.role = "member"
        other_id = uuid.uuid4()
        db.add(User(id=other_id, tenant_id=tenant, username=f"native-{other_id.hex}", email=f"{other_id.hex}@fixture.test",
            password_hash="fixture", display_name="Other requester", role="member"))
        await db.flush()
        for path, owner, work in ((mine, user_id, session), (theirs, other_id, None)):
            await authority.register_workspace_path(db, tenant_id=tenant, agent_id=agent, path=path.relative_to(root).as_posix(),
                owner_user_id=owner, root_session_id=work, source="fixture", content_hash=hashlib.sha256(path.read_bytes()).hexdigest())
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        scope = await authority.load_workspace_authority_scope(db, user, agent_id=agent, session_id=session)
    assert scope.can_read(mine.relative_to(root).as_posix())
    assert not scope.can_read(theirs.relative_to(root).as_posix())
    context = ToolExecutionContext(agent_id=agent, user_id=user_id, tenant_id=str(tenant), workspace=root,
        session_id=str(session), workspace_authority_scope=scope)
    read = filesystem.read_file
    denied = await adapt_and_call(read.meta, read, ToolExecutionRequest(tool_name="read_file",
        arguments={"path": theirs.relative_to(root).as_posix()}, context=context))
    assert "auth_or_permission" in str(denied)

    replaced = False
    original = domain._authority_allows_path

    def allow_then_replace(loaded_scope, path, **kwargs):
        nonlocal replaced
        allowed = original(loaded_scope, path, **kwargs)
        if allowed and path == mine.relative_to(root).as_posix() and not replaced:
            replaced = True
            mine.unlink()
            if alias == "symlink":
                mine.symlink_to(theirs)
            elif alias == "hardlink":
                os.link(theirs, mine)
            else:
                mine.parent.rmdir()
                mine.parent.symlink_to(theirs.parent, target_is_directory=True)
        return allowed

    monkeypatch.setattr(domain, "_authority_allows_path", allow_then_replace)
    name = "read_file" if operation == "read_image" else operation
    handler = getattr(filesystem, name)
    arguments = {"path": mine.relative_to(root).as_posix(), "content": "Unauthorized replacement",
        "old_text": "content", "new_text": "Unauthorized replacement"}
    result = await adapt_and_call(handler.meta, handler, ToolExecutionRequest(tool_name=name, arguments=arguments, context=context))
    assert replaced
    if isinstance(result, ToolContentEnvelope):
        assert all(b"Other requester's private content" not in base64.b64decode(block.data) for block in result.blocks)
    assert "<tool_error>" in str(result)
    payload = json.loads(str(result).split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])
    assert payload["error_class"] == "auth_or_permission" and payload["http_status"] == 403
    assert payload["reason_code"] == "workspace_resource_alias_forbidden"
    assert payload["actionable_hint"]
    assert theirs.read_bytes() == b"Other requester's private content"

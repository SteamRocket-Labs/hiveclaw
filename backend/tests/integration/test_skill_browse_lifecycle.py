import pytest
from fastapi import HTTPException


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_browse_new_skill_create_edit_read_delete_uses_loaded_collection(
    monkeypatch, owner_sessionmaker, app_user_sessionmaker
):
    import app.api.skills as api
    from app.database import tenant_scoped_session
    from app.models.user import User
    from tests.services.test_session_input_control_v2 import _seed_session

    tenant_id, user_id, _, _, _ = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        user.role = "platform_admin"
        await db.commit()
    monkeypatch.setattr(
        api, "tenant_scoped_session", lambda tid: tenant_scoped_session(tid, session_factory=app_user_sessionmaker)
    )
    path = "synthetic-browse-skill/SKILL.md"
    assert await api.browse_write(api.BrowseWriteIn(path=path, content="# Synthetic v1"), current_user=user) == {
        "ok": True
    }
    assert await api.browse_read(path, current_user=user) == {"content": "# Synthetic v1"}
    assert await api.browse_write(api.BrowseWriteIn(path=path, content="# Synthetic v2"), current_user=user) == {
        "ok": True
    }
    assert await api.browse_read(path, current_user=user) == {"content": "# Synthetic v2"}
    await api.browse_delete("synthetic-browse-skill", current_user=user)
    with pytest.raises(HTTPException) as exc:
        await api.browse_read(path, current_user=user)
    assert exc.value.status_code == 404

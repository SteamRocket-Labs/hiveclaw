"""Human saves retain their read version and remain readable across sessions."""

import hashlib
import asyncio
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from app.database import pin_rls_tenant_context
from tests.integration.test_m1g15_workspace_actions_postgres import read_granted_file  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def editor(read_granted_file, owner_sessionmaker, app_user_sessionmaker):  # noqa: F811
    from app.api import files
    from app.core.security import get_current_user
    from app.database import get_db
    from app.models.security_audit import ResourcePermission

    tenant, user, agent, _session, root, path, target, _scope, grant_id = read_granted_file
    target.write_bytes("Original 你好\r\nLast line\r\n".encode())
    async with owner_sessionmaker() as db:
        (await db.get(ResourcePermission, grant_id)).actions = ["read", "write"]
        await db.commit()

    async def current_user():
        return user

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    app = FastAPI()
    app.include_router(files.router)
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = scoped_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as client:
        yield tenant, user, agent, root, path, target, client


async def test_read_version_hash_binds_actual_bytes_before_newline_decoding(editor):
    _tenant, _user, agent, _root, path, target, client = editor
    response = await client.get(f"/agents/{agent}/files/content", params={"path": path})
    assert response.status_code == 200, response.text
    assert response.json()["content"] == "Original 你好\nLast line\n"
    assert response.json()["content_hash"] == hashlib.sha256(target.read_bytes()).hexdigest()


async def test_stale_save_does_not_overwrite_the_saved_version(editor):
    _tenant, _user, agent, _root, path, target, client = editor
    base = hashlib.sha256(target.read_bytes()).hexdigest()
    url = f"/agents/{agent}/files/content"
    first = await client.put(url, params={"path": path}, json={"content": "First saved revision", "expected_current_hash": base})
    assert first.status_code == 200, first.text
    stale = await client.put(url, params={"path": path}, json={"content": "Still-visible local draft", "expected_current_hash": base})
    assert target.read_text() == "First saved revision"
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "workspace_content_conflict", stale.text
    assert "current_content_hash" not in stale.json()["detail"]
    assert stale.json()["detail"]["retryable"] is False
    current = await client.get(url, params={"path": path})
    assert current.json()["content"] == "First saved revision"


async def test_saved_revision_is_consumed_from_the_canonical_file_in_another_session(
    editor, owner_sessionmaker, app_user_sessionmaker,
):
    from app.models.chat_session import ChatSession
    from app.services.workspace_resource_authority import load_workspace_authority_scope
    from app.tools.adapters import adapt_and_call
    from app.tools.handlers.filesystem import read_file
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    tenant, user, agent, root, path, target, client = editor
    base = hashlib.sha256(target.read_bytes()).hexdigest()
    saved = "Human saved revision\n决定性尾部"
    response = await client.put(f"/agents/{agent}/files/content", params={"path": path},
        json={"content": saved, "expected_current_hash": base})
    assert response.status_code == 200, response.text
    another_session = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(ChatSession(id=another_session, tenant_id=tenant, user_id=user.id, agent_id=agent, title="Reopen saved result"))
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        scope = await load_workspace_authority_scope(db, user, agent_id=agent, session_id=another_session)
    context = ToolExecutionContext(agent_id=agent, user_id=user.id, tenant_id=str(tenant), workspace=root,
        session_id=str(another_session), workspace_authority_scope=scope)
    body = await adapt_and_call(read_file.meta, read_file, ToolExecutionRequest(tool_name="read_file", arguments={"path": path}, context=context))
    assert str(body).startswith(saved) and target.read_text() == saved
    assert response.json().get("content_hash") == hashlib.sha256(saved.encode()).hexdigest()


async def test_concurrent_saves_have_one_winner(editor):
    _tenant, _user, agent, _root, path, target, client = editor
    base = hashlib.sha256(target.read_bytes()).hexdigest()
    responses = await asyncio.gather(*(
        client.put(f"/agents/{agent}/files/content", params={"path": path},
            json={"content": body, "expected_current_hash": base})
        for body in ("Concurrent A", "Concurrent B")
    ))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(i for i, response in enumerate(responses) if response.status_code == 200)
    assert target.read_text() == ("Concurrent A", "Concurrent B")[winner]


async def test_write_only_conflict_does_not_disclose_current_version(editor, read_granted_file, owner_sessionmaker):  # noqa: F811
    from app.models.security_audit import ResourcePermission

    _tenant, _user, agent, _root, path, target, client = editor
    async with owner_sessionmaker() as db:
        (await db.get(ResourcePermission, read_granted_file[-1])).actions = ["write"]
        await db.commit()
    original = target.read_bytes()
    assert (await client.get(f"/agents/{agent}/files/content", params={"path": path})).status_code == 403
    response = await client.put(f"/agents/{agent}/files/content", params={"path": path},
        json={"content": "Draft", "expected_current_hash": "0" * 64})
    assert response.status_code == 409 and target.read_bytes() == original
    assert set(response.json()["detail"]) == {"code", "message", "outcome", "retryable"}
    assert hashlib.sha256(original).hexdigest() not in response.text and "Original" not in response.text


@pytest.mark.parametrize("failure", ["flush", "registration"])
async def test_save_with_possible_effect_is_unknown_and_not_replayed(editor, monkeypatch, failure):
    import app.services.workspace_resource_authority as authority
    from contextlib import contextmanager

    _tenant, _user, agent, _root, path, target, client = editor
    base = hashlib.sha256(target.read_bytes()).hexdigest()
    if failure == "flush":
        original_open = authority.open_workspace_file

        @contextmanager
        def fail_after_save(*args, **kwargs):
            with original_open(*args, **kwargs) as output:
                yield output
            if kwargs.get("mode") == "r+b":
                raise authority.WorkspaceAuthorityError("workspace_storage_unavailable", "Fixture flush failure")

        monkeypatch.setattr(authority, "open_workspace_file", fail_after_save)
    else:
        async def fail_registration(*args, **kwargs):
            raise RuntimeError("Fixture registration failure")
        monkeypatch.setattr(authority, "register_workspace_path", fail_registration)
    response = await client.put(f"/agents/{agent}/files/content", params={"path": path},
        json={"content": "Possibly saved revision", "expected_current_hash": base})
    assert response.status_code == 503 and response.json()["detail"]["outcome"] == "unknown", response.text
    assert response.json()["detail"]["retryable"] is False
    assert target.read_text() == "Possibly saved revision"

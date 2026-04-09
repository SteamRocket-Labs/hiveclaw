from __future__ import annotations

import io
import sys
import types
import uuid
from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)

    def fetchall(self):
        return self._values


class _QueuedDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.deleted = []
        self.committed = False

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def delete(self, value):
        self.deleted.append(value)

    async def commit(self):
        self.committed = True

    async def refresh(self, value):
        if getattr(value, "id", None) is None:
            value.id = uuid.uuid4()
        if getattr(value, "likes_count", None) is None:
            value.likes_count = 0
        if getattr(value, "comments_count", None) is None:
            value.comments_count = 0
        if getattr(value, "created_at", None) is None:
            value.created_at = datetime.now(timezone.utc)


class _AsyncSessionContext:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_api_routes_do_not_register_duplicate_path_method_pairs():
    from app.main import app

    route_keys = []
    for route in app.routes:
        methods = tuple(sorted(m for m in getattr(route, "methods", set()) if m not in {"HEAD", "OPTIONS"}))
        if methods:
            route_keys.append((route.path, methods))

    duplicates = [key for key, count in Counter(route_keys).items() if count > 1]
    assert duplicates == []


def test_gateway_internal_api_key_routes_are_not_public():
    from app.main import app

    paths = {route.path for route in app.routes}

    assert "/api/gateway/agents/{agent_id}/api-key" not in paths
    assert "/api/v1/gateway/agents/{agent_id}/api-key" not in paths
    assert "/api/gateway/generate-key/{agent_id}" not in paths
    assert "/api/v1/gateway/generate-key/{agent_id}" not in paths


def test_get_skill_route_requires_authentication(monkeypatch):
    import app.api.skills as skills_api

    app = FastAPI()
    app.include_router(skills_api.router)
    monkeypatch.setattr(skills_api, "async_session", lambda: _AsyncSessionContext(_QueuedDB([_ScalarResult(None)])))

    client = TestClient(app)
    response = client.get(f"/skills/{uuid.uuid4()}")

    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_skill_hides_other_tenant_skill():
    import app.api.skills as skills_api

    current_user = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=uuid.uuid4())
    foreign_skill = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="Hidden",
        description="secret",
        category="custom",
        icon="x",
        folder_name="hidden",
        is_builtin=False,
        files=[SimpleNamespace(path="SKILL.md", content="---\nname: Hidden\n---\n")],
    )
    db = _QueuedDB([_ScalarResult(foreign_skill)])

    with pytest.raises(HTTPException) as exc:
        await skills_api.get_skill(skill_id=str(foreign_skill.id), current_user=current_user, db=db)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_upload_requires_agent_access(monkeypatch, tmp_path):
    import app.api.upload as upload_api

    async def fake_check_agent_access(db, current_user, agent_id):
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(upload_api, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(upload_api, "check_agent_access", fake_check_agent_access)

    file = UploadFile(io.BytesIO(b"hello"), filename="notes.txt")

    with pytest.raises(HTTPException) as exc:
        await upload_api.upload_file(
            file=file,
            agent_id=uuid.uuid4(),
            current_user=SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4()),
            db=object(),
        )

    assert exc.value.status_code == 403
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.asyncio
async def test_upload_sanitizes_workspace_filename(monkeypatch, tmp_path):
    import app.api.upload as upload_api

    async def fake_check_agent_access(db, current_user, agent_id):
        return None

    monkeypatch.setattr(upload_api, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(upload_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(upload_api, "extract_text", lambda path, extension: "safe")

    agent_id = uuid.uuid4()
    file = UploadFile(io.BytesIO(b"hello"), filename="../evil.txt")

    result = await upload_api.upload_file(
        file=file,
        agent_id=agent_id,
        current_user=SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4()),
        db=object(),
    )

    uploads_dir = tmp_path / str(agent_id) / "workspace" / "uploads"
    assert (uploads_dir / "evil.txt").read_bytes() == b"hello"
    assert not (tmp_path / str(agent_id) / "workspace" / "evil.txt").exists()
    assert result["workspace_path"] == "workspace/uploads/evil.txt"


def test_extract_text_docx_does_not_shell_out(monkeypatch, tmp_path):
    import app.api.upload as upload_api

    fake_docx = types.ModuleType("docx")
    fake_docx.Document = lambda _path: SimpleNamespace(
        paragraphs=[SimpleNamespace(text="line-1"), SimpleNamespace(text="line-2")]
    )
    monkeypatch.setitem(sys.modules, "docx", fake_docx)

    def fail_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be used for docx extraction")

    import subprocess

    monkeypatch.setattr(subprocess, "run", fail_run)

    file_path = tmp_path / "quoted'.docx"
    file_path.write_bytes(b"placeholder")

    extracted = upload_api.extract_text(file_path, ".docx")

    assert extracted == "line-1\nline-2"


@pytest.mark.asyncio
async def test_list_agent_triggers_checks_agent_access(monkeypatch):
    import app.api.triggers as triggers_api

    agent_id = uuid.uuid4()
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        name="wake-up",
        type="once",
        config={},
        reason="check in",
        focus_ref=None,
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        cooldown_seconds=60,
        last_fired_at=None,
        created_at=datetime.now(timezone.utc),
        expires_at=None,
    )
    db = _QueuedDB([_ListResult([trigger])])
    current_user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member")
    called = {}

    async def fake_check_agent_access(db_arg, user_arg, requested_agent_id):
        called["args"] = (db_arg, user_arg, requested_agent_id)
        return SimpleNamespace(id=requested_agent_id), "manage"

    monkeypatch.setattr(triggers_api, "check_agent_access", fake_check_agent_access)

    result = await triggers_api.list_agent_triggers(agent_id=agent_id, current_user=current_user, db=db)

    assert called["args"] == (db, current_user, agent_id)
    assert result[0].id == str(trigger.id)


@pytest.mark.asyncio
async def test_plaza_create_post_uses_authenticated_identity():
    import app.api.plaza as plaza_api

    current_user = SimpleNamespace(
        id=uuid.uuid4(),
        display_name="Alice",
        tenant_id=uuid.uuid4(),
        role="member",
    )
    db = _QueuedDB([])
    body = plaza_api.PostCreate(
        content="Hello plaza",
        author_id=uuid.uuid4(),
        author_type="agent",
        author_name="Mallory",
        tenant_id=uuid.uuid4(),
    )

    result = await plaza_api.create_post(body=body, current_user=current_user, db=db)

    created_post = db.added[0]
    assert created_post.author_id == current_user.id
    assert created_post.author_type == "human"
    assert created_post.author_name == current_user.display_name
    assert created_post.tenant_id == current_user.tenant_id
    assert result.author_id == current_user.id


@pytest.mark.asyncio
async def test_plaza_comment_uses_authenticated_identity():
    import app.api.plaza as plaza_api

    current_user = SimpleNamespace(
        id=uuid.uuid4(),
        display_name="Alice",
        tenant_id=uuid.uuid4(),
        role="member",
    )
    post = SimpleNamespace(id=uuid.uuid4(), author_id=current_user.id, tenant_id=current_user.tenant_id, comments_count=0)
    db = _QueuedDB([_ScalarResult(post)])
    body = plaza_api.CommentCreate(
        content="Nice work",
        author_id=uuid.uuid4(),
        author_type="agent",
        author_name="Mallory",
    )

    result = await plaza_api.create_comment(
        post_id=post.id,
        body=body,
        current_user=current_user,
        db=db,
    )

    created_comment = db.added[0]
    assert created_comment.author_id == current_user.id
    assert created_comment.author_type == "human"
    assert created_comment.author_name == current_user.display_name
    assert result.author_id == current_user.id


@pytest.mark.asyncio
async def test_plaza_list_posts_rejects_other_tenant_scope():
    import app.api.plaza as plaza_api

    current_user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member")

    with pytest.raises(HTTPException) as exc:
        await plaza_api.list_posts(
            tenant_id=str(uuid.uuid4()),
            current_user=current_user,
            db=_QueuedDB([_ListResult([])]),
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_plaza_get_post_hides_other_tenant_records():
    import app.api.plaza as plaza_api

    current_user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member")
    foreign_post = SimpleNamespace(
        id=uuid.uuid4(),
        author_id=uuid.uuid4(),
        author_type="human",
        author_name="Other",
        content="secret",
        likes_count=0,
        comments_count=0,
        tenant_id=uuid.uuid4(),
        created_at=datetime.now(timezone.utc),
    )
    db = _QueuedDB([_ScalarResult(foreign_post)])

    with pytest.raises(HTTPException) as exc:
        await plaza_api.get_post(post_id=foreign_post.id, current_user=current_user, db=db)

    assert exc.value.status_code == 404

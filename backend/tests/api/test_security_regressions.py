from __future__ import annotations

import io
import sys
import types
import uuid
from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException
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
        if "SET LOCAL app.current_tenant_id" in str(_stmt):
            return _ScalarResult(None)
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


def test_legacy_openclaw_gateway_routes_are_removed():
    from app.main import app

    paths = {route.path for route in app.routes}

    assert not any(path.startswith("/api/gateway") for path in paths)
    assert not any(path.startswith("/api/v1/gateway") for path in paths)
    assert "/api/agents/{agent_id}/api-key" not in paths
    assert "/api/v1/agents/{agent_id}/api-key" not in paths
    assert "/api/agents/{agent_id}/gateway-messages" not in paths
    assert "/api/v1/agents/{agent_id}/gateway-messages" not in paths
    assert "/api/gateway/agents/{agent_id}/api-key" not in paths
    assert "/api/v1/gateway/agents/{agent_id}/api-key" not in paths
    assert "/api/gateway/generate-key/{agent_id}" not in paths
    assert "/api/v1/gateway/generate-key/{agent_id}" not in paths


def test_get_skill_route_requires_authentication(monkeypatch):
    import app.api.skills as skills_api

    app = FastAPI()
    app.include_router(skills_api.router)
    monkeypatch.setattr(
        skills_api, "tenant_scoped_session", lambda *a, **k: _AsyncSessionContext(_QueuedDB([_ScalarResult(None)]))
    )

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
            background_tasks=BackgroundTasks(),
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
        background_tasks=BackgroundTasks(),
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
async def test_agent_files_refuses_direct_memory_writes_and_deletes(monkeypatch, tmp_path):
    import app.api.files as files_api

    agent_id = uuid.uuid4()
    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_check_agent_access(db, current_user, requested_agent_id):
        return SimpleNamespace(id=requested_agent_id, tenant_id=current_user.tenant_id), "manage"

    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)
    current_user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member")

    with pytest.raises(HTTPException) as write_exc:
        await files_api.write_file(
            agent_id=agent_id,
            path="memory/knowledge.md",
            data=files_api.FileWrite(content="raw bypass"),
            current_user=current_user,
            db=object(),
        )
    assert write_exc.value.status_code == 403

    memory_path = tmp_path / str(agent_id) / "memory" / "knowledge.md"
    memory_path.parent.mkdir(parents=True)
    memory_path.write_text("# Knowledge\n\n- [2026-06-05] safe\n", encoding="utf-8")

    with pytest.raises(HTTPException) as delete_exc:
        await files_api.delete_file(
            agent_id=agent_id,
            path="memory/knowledge.md",
            current_user=current_user,
            db=object(),
        )
    assert delete_exc.value.status_code == 403
    assert memory_path.exists()


@pytest.mark.asyncio
async def test_agent_files_refuses_platform_managed_writes_and_deletes(monkeypatch, tmp_path):
    import app.api.files as files_api

    agent_id = uuid.uuid4()
    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_check_agent_access(db, current_user, requested_agent_id):
        return SimpleNamespace(id=requested_agent_id, tenant_id=current_user.tenant_id), "manage"

    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)
    current_user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member")

    for rel in ("logs/session.md", "evolution/skill_usage.json", "runtime_artifacts/session_memory.md"):
        with pytest.raises(HTTPException) as write_exc:
            await files_api.write_file(
                agent_id=agent_id,
                path=rel,
                data=files_api.FileWrite(content="raw bypass"),
                current_user=current_user,
                db=object(),
            )
        assert write_exc.value.status_code == 403

        target = tmp_path / str(agent_id) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("platform owned", encoding="utf-8")

        with pytest.raises(HTTPException) as delete_exc:
            await files_api.delete_file(
                agent_id=agent_id,
                path=rel,
                current_user=current_user,
                db=object(),
            )
        assert delete_exc.value.status_code == 403
        assert target.exists()


@pytest.mark.asyncio
async def test_agent_files_refuses_unscoped_root_writes_and_deletes(monkeypatch, tmp_path):
    import app.api.files as files_api

    agent_id = uuid.uuid4()
    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_check_agent_access(db, current_user, requested_agent_id):
        return SimpleNamespace(id=requested_agent_id, tenant_id=current_user.tenant_id), "manage"

    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)
    current_user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member")

    for rel in ("report.md", "relationships.md", "HEARTBEAT.md", "DREAM.md", "state.json", "tasks.json"):
        with pytest.raises(HTTPException) as write_exc:
            await files_api.write_file(
                agent_id=agent_id,
                path=rel,
                data=files_api.FileWrite(content="raw bypass"),
                current_user=current_user,
                db=object(),
            )
        assert write_exc.value.status_code == 403

        target = tmp_path / str(agent_id) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("platform owned", encoding="utf-8")

        with pytest.raises(HTTPException) as delete_exc:
            await files_api.delete_file(
                agent_id=agent_id,
                path=rel,
                current_user=current_user,
                db=object(),
            )
        assert delete_exc.value.status_code == 403
        assert target.exists()


@pytest.mark.asyncio
async def test_agent_files_refuses_invalid_skill_package_paths(monkeypatch, tmp_path):
    import app.api.files as files_api

    agent_id = uuid.uuid4()
    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_check_agent_access(db, current_user, requested_agent_id):
        return SimpleNamespace(id=requested_agent_id, tenant_id=current_user.tenant_id), "manage"

    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)
    current_user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member")

    for rel in ("skills/MCP_INSTALLER.md", "skills/.usage.json", "skills/flat-skill"):
        with pytest.raises(HTTPException) as write_exc:
            await files_api.write_file(
                agent_id=agent_id,
                path=rel,
                data=files_api.FileWrite(content="raw bypass"),
                current_user=current_user,
                db=object(),
            )
        assert write_exc.value.status_code == 403

    with pytest.raises(HTTPException) as canonical_skill_exc:
        await files_api.write_file(
            agent_id=agent_id,
            path="skills/deploy-checklist/SKILL.md",
            data=files_api.FileWrite(content="---\nname: deploy-checklist\n---\n"),
            current_user=current_user,
            db=object(),
        )
    assert canonical_skill_exc.value.status_code == 403
    assert "Platform Skill Gate" in str(canonical_skill_exc.value.detail)
    assert not (tmp_path / str(agent_id) / "skills" / "deploy-checklist" / "SKILL.md").exists()

    with pytest.raises(HTTPException) as upload_exc:
        await files_api.upload_file_to_workspace(
            agent_id=agent_id,
            file=UploadFile(io.BytesIO(b"bad"), filename="MCP_INSTALLER.md"),
            path="skills/",
            current_user=current_user,
            db=object(),
        )
    assert upload_exc.value.status_code in {400, 403}


def test_agent_files_safe_path_rejects_sibling_prefix_escape(monkeypatch, tmp_path):
    import app.api.files as files_api

    agent_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))

    with pytest.raises(HTTPException) as exc:
        files_api._safe_path(agent_id, f"../{agent_id}-evil/pwn.txt")

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_agent_files_upload_rejects_sibling_prefix_escape(monkeypatch, tmp_path):
    import app.api.files as files_api

    agent_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_check_agent_access(db, current_user, requested_agent_id):
        return SimpleNamespace(id=requested_agent_id, tenant_id=current_user.tenant_id), "manage"

    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)
    current_user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member")

    with pytest.raises(HTTPException) as exc:
        await files_api.upload_file_to_workspace(
            agent_id=agent_id,
            file=UploadFile(io.BytesIO(b"bad"), filename="pwn.txt"),
            path=f"workspace/../../{agent_id}-evil",
            current_user=current_user,
            db=object(),
        )

    assert exc.value.status_code == 403
    assert not (tmp_path / f"{agent_id}-evil" / "pwn.txt").exists()


@pytest.mark.asyncio
async def test_enterprise_kb_paths_reject_sibling_prefix_escape(monkeypatch, tmp_path):
    import app.api.files as files_api

    tenant_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))
    current_user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, role="org_admin")

    info_dir = tmp_path / f"enterprise_info_{tenant_id}"
    info_dir.mkdir()
    sibling = tmp_path / f"enterprise_info_{tenant_id}-evil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret token\n", encoding="utf-8")
    escaped_dir = f"../enterprise_info_{tenant_id}-evil"
    escaped_file = f"{escaped_dir}/secret.txt"

    with pytest.raises(HTTPException) as list_exc:
        await files_api.list_enterprise_kb_files(path=escaped_dir, current_user=current_user)
    assert list_exc.value.status_code == 403

    with pytest.raises(HTTPException) as read_exc:
        await files_api.read_enterprise_file(path=escaped_file, current_user=current_user)
    assert read_exc.value.status_code == 403

    with pytest.raises(HTTPException) as write_exc:
        await files_api.write_enterprise_file(
            path=f"{escaped_dir}/pwn.txt",
            data=files_api.FileWrite(content="bad"),
            current_user=current_user,
        )
    assert write_exc.value.status_code == 403

    with pytest.raises(HTTPException) as delete_exc:
        await files_api.delete_enterprise_file(path=escaped_file, current_user=current_user)
    assert delete_exc.value.status_code == 403

    with pytest.raises(HTTPException) as upload_exc:
        await files_api.upload_enterprise_kb_file(
            file=UploadFile(io.BytesIO(b"bad"), filename="pwn.txt"),
            sub_path=escaped_dir,
            current_user=current_user,
        )
    assert upload_exc.value.status_code == 403

    assert not (sibling / "pwn.txt").exists()
    assert (sibling / "secret.txt").read_text(encoding="utf-8") == "secret token\n"


@pytest.mark.asyncio
async def test_agent_import_skill_cannot_escape_skill_folder(monkeypatch, tmp_path):
    import app.api.files as files_api
    import app.api.skills as skills_api

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(skills_api, "_skill_visible_to_user", lambda *_args, **_kwargs: True)

    async def fake_check_agent_access(db, current_user, requested_agent_id):
        return SimpleNamespace(id=requested_agent_id, tenant_id=current_user.tenant_id), "manage"

    class _FakeDB:
        async def execute(self, _stmt):
            return _ScalarResult(
                SimpleNamespace(
                    id=uuid.uuid4(),
                    name="Unsafe Skill",
                    folder_name="unsafe-skill",
                    files=[
                        SimpleNamespace(path="SKILL.md", content="---\nname: unsafe\n---\n"),
                        SimpleNamespace(path="../evil.md", content="escaped"),
                    ],
                )
            )

    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)
    current_user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, role="member")

    with pytest.raises(HTTPException) as exc:
        await files_api.import_skill_to_agent(
            agent_id=agent_id,
            body=files_api.ImportSkillBody(skill_id=str(uuid.uuid4())),
            current_user=current_user,
            db=_FakeDB(),
        )

    assert exc.value.status_code == 400
    assert "SkillGuard blocked" in str(exc.value.detail)
    assert not (tmp_path / str(agent_id) / "skills" / "unsafe-skill" / "SKILL.md").exists()
    assert not (tmp_path / str(agent_id) / "skills" / "evil.md").exists()
    assert not (tmp_path / str(agent_id) / "evil.md").exists()


@pytest.mark.asyncio
async def test_agent_files_list_hides_dotfile_sidecars(monkeypatch, tmp_path):
    import app.api.files as files_api

    agent_id = uuid.uuid4()
    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_check_agent_access(db, current_user, requested_agent_id):
        return SimpleNamespace(id=requested_agent_id, tenant_id=current_user.tenant_id), "manage"

    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)
    skills_dir = tmp_path / str(agent_id) / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / ".usage.json").write_text("{}", encoding="utf-8")
    (skills_dir / "mcp-installer").mkdir()

    items = await files_api.list_files(
        agent_id=agent_id,
        path="skills",
        current_user=SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member"),
        db=object(),
    )

    assert [item.name for item in items] == ["mcp-installer"]


@pytest.mark.asyncio
async def test_agent_files_use_access_cannot_read_raw_memory(monkeypatch, tmp_path):
    import app.api.files as files_api

    agent_id = uuid.uuid4()
    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))

    async def fake_check_agent_access(db, current_user, requested_agent_id):
        return SimpleNamespace(id=requested_agent_id, tenant_id=current_user.tenant_id), "use"

    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)
    memory_path = tmp_path / str(agent_id) / "memory" / "knowledge.md"
    memory_path.parent.mkdir(parents=True)
    memory_path.write_text("# Knowledge\n\n- [2026-06-05] salary planning is confidential\n", encoding="utf-8")

    with pytest.raises(HTTPException) as exc:
        await files_api.read_file(
            agent_id=agent_id,
            path="memory/knowledge.md",
            current_user=SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="member"),
            db=object(),
        )

    assert exc.value.status_code == 403


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
    post = SimpleNamespace(
        id=uuid.uuid4(), author_id=current_user.id, tenant_id=current_user.tenant_id, comments_count=0
    )
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

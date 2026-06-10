from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _AsyncSessionContext:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _CreateSkillSession:
    def __init__(self, results=None):
        self.added = []
        self.committed = False
        self._results = list(results or [_ScalarResult(None)])

    def add(self, value):
        self.added.append(value)

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    async def flush(self):
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid4()

    async def commit(self):
        self.committed = True


class _QueuedDB:
    def __init__(self, results):
        self._results = list(results)
        self.deleted = []
        self.committed = False

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    async def flush(self):
        return None

    async def delete(self, value):
        self.deleted.append(value)

    async def commit(self):
        self.committed = True


def test_platform_admin_skill_scope_uses_selected_tenant():
    import app.api.skills as skills_api

    selected_tenant_id = uuid4()
    current_user = SimpleNamespace(role="platform_admin", tenant_id=selected_tenant_id)

    clause = skills_api._skill_scope_clause(current_user)

    assert clause is not None
    compiled = str(clause.compile(compile_kwargs={"literal_binds": True}))
    assert "skills.tenant_id IS NULL" in compiled
    assert selected_tenant_id.hex in compiled


def test_tenant_skill_visibility_hides_global_custom_skill():
    import app.api.skills as skills_api

    tenant_id = uuid4()
    current_user = SimpleNamespace(role="member", tenant_id=tenant_id)
    global_builtin = SimpleNamespace(tenant_id=None, is_builtin=True)
    leaked_global_custom = SimpleNamespace(tenant_id=None, is_builtin=False)
    tenant_skill = SimpleNamespace(tenant_id=tenant_id, is_builtin=False)

    assert skills_api._skill_visible_to_user(global_builtin, current_user) is True
    assert skills_api._skill_visible_to_user(tenant_skill, current_user) is True
    assert skills_api._skill_visible_to_user(leaked_global_custom, current_user) is False


@pytest.mark.asyncio
async def test_create_skill_scopes_custom_skill_to_current_tenant(monkeypatch):
    import app.api.skills as skills_api
    from app.models.skill import Skill

    session = _CreateSkillSession()
    tenant_id = uuid4()

    monkeypatch.setattr(skills_api, "tenant_scoped_session", lambda *a, **k: _AsyncSessionContext(session))
    monkeypatch.setattr(skills_api, "_guard_skill_files_or_raise", lambda *_args, **_kwargs: None)

    result = await skills_api.create_skill(
        skills_api.SkillCreateIn(
            name="Tenant Workflow",
            description="Tenant-only workflow",
            category="custom",
            icon="",
            folder_name="tenant-workflow",
            files=[skills_api.SkillFileIn(path="SKILL.md", content="# Tenant Workflow")],
        ),
        current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=tenant_id),
    )

    created_skill = next(value for value in session.added if isinstance(value, Skill))
    assert created_skill.tenant_id == tenant_id
    assert result["name"] == "Tenant Workflow"
    assert session.committed is True


@pytest.mark.asyncio
async def test_create_skill_rejects_builtin_folder_collision(monkeypatch):
    import app.api.skills as skills_api

    tenant_id = uuid4()
    global_builtin = SimpleNamespace(
        id=uuid4(),
        tenant_id=None,
        is_builtin=True,
        folder_name="skill-creator",
        name="Skill Creator",
    )
    session = _CreateSkillSession(results=[_ScalarResult(global_builtin)])

    monkeypatch.setattr(skills_api, "tenant_scoped_session", lambda *a, **k: _AsyncSessionContext(session))
    monkeypatch.setattr(skills_api, "_guard_skill_files_or_raise", lambda *_args, **_kwargs: None)

    with pytest.raises(HTTPException) as exc:
        await skills_api.create_skill(
            skills_api.SkillCreateIn(
                name="Tenant Skill Creator",
                description="Tenant override attempt",
                category="custom",
                icon="",
                folder_name="skill-creator",
                files=[skills_api.SkillFileIn(path="SKILL.md", content="# Tenant Skill Creator")],
            ),
            current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=tenant_id),
        )

    assert exc.value.status_code == 409
    assert session.added == []
    assert session.committed is False


@pytest.mark.asyncio
async def test_update_skill_rejects_foreign_tenant_skill(monkeypatch):
    import app.api.skills as skills_api

    tenant_id = uuid4()
    foreign_skill = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        is_builtin=False,
        name="Foreign",
        description="before",
        category="custom",
        icon="",
        folder_name="foreign",
        files=[],
    )
    session = _QueuedDB([_ScalarResult(foreign_skill)])

    monkeypatch.setattr(skills_api, "tenant_scoped_session", lambda *a, **k: _AsyncSessionContext(session))

    with pytest.raises(HTTPException) as exc:
        await skills_api.update_skill(
            str(foreign_skill.id),
            skills_api.SkillUpdateIn(description="after"),
            current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=tenant_id),
        )

    assert exc.value.status_code == 404
    assert foreign_skill.description == "before"
    assert session.committed is False


@pytest.mark.asyncio
async def test_update_skill_rejects_builtin_skill(monkeypatch):
    import app.api.skills as skills_api

    tenant_id = uuid4()
    builtin_skill = SimpleNamespace(
        id=uuid4(),
        tenant_id=None,
        is_builtin=True,
        name="Skill Creator",
        description="before",
        category="development",
        icon="",
        folder_name="skill-creator",
        files=[],
    )
    session = _QueuedDB([_ScalarResult(builtin_skill)])

    monkeypatch.setattr(skills_api, "tenant_scoped_session", lambda *a, **k: _AsyncSessionContext(session))

    with pytest.raises(HTTPException) as exc:
        await skills_api.update_skill(
            str(builtin_skill.id),
            skills_api.SkillUpdateIn(description="after"),
            current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=tenant_id),
        )

    assert exc.value.status_code == 400
    assert builtin_skill.description == "before"
    assert session.committed is False


@pytest.mark.asyncio
async def test_browse_write_rejects_builtin_skill_file_mutation(monkeypatch):
    import app.api.skills as skills_api

    tenant_id = uuid4()
    skill_file = SimpleNamespace(path="SKILL.md", content="# Skill Creator")
    builtin_skill = SimpleNamespace(
        id=uuid4(),
        tenant_id=None,
        is_builtin=True,
        name="Skill Creator",
        folder_name="skill-creator",
        files=[skill_file],
    )
    session = _QueuedDB([_ScalarResult(builtin_skill)])

    monkeypatch.setattr(skills_api, "tenant_scoped_session", lambda *a, **k: _AsyncSessionContext(session))

    with pytest.raises(HTTPException) as exc:
        await skills_api.browse_write(
            skills_api.BrowseWriteIn(path="skill-creator/SKILL.md", content="# Mutated"),
            current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=tenant_id),
        )

    assert exc.value.status_code == 400
    assert skill_file.content == "# Skill Creator"
    assert session.committed is False


@pytest.mark.asyncio
async def test_browse_delete_rejects_builtin_skill_file_delete(monkeypatch):
    import app.api.skills as skills_api

    tenant_id = uuid4()
    skill_file = SimpleNamespace(path="SKILL.md", content="# Skill Creator")
    builtin_skill = SimpleNamespace(
        id=uuid4(),
        tenant_id=None,
        is_builtin=True,
        name="Skill Creator",
        folder_name="skill-creator",
        files=[skill_file],
    )
    session = _QueuedDB([_ScalarResult(builtin_skill)])

    monkeypatch.setattr(skills_api, "tenant_scoped_session", lambda *a, **k: _AsyncSessionContext(session))

    with pytest.raises(HTTPException) as exc:
        await skills_api.browse_delete(
            path="skill-creator/SKILL.md",
            current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=tenant_id),
        )

    assert exc.value.status_code == 400
    assert session.deleted == []
    assert session.committed is False


@pytest.mark.asyncio
async def test_agent_import_skill_rejects_foreign_tenant_skill(monkeypatch, tmp_path):
    import app.api.files as files_api

    tenant_id = uuid4()
    foreign_tenant_id = uuid4()
    agent_id = uuid4()
    foreign_skill = SimpleNamespace(
        id=uuid4(),
        tenant_id=foreign_tenant_id,
        name="Foreign Skill",
        folder_name="foreign-skill",
        files=[SimpleNamespace(path="SKILL.md", content="# Foreign Skill")],
    )
    db = _QueuedDB([_ScalarResult(foreign_skill)])

    async def fake_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(files_api, "settings", SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(files_api, "check_agent_access", fake_access)

    with pytest.raises(HTTPException) as exc:
        await files_api.import_skill_to_agent(
            agent_id=agent_id,
            body=files_api.ImportSkillBody(skill_id=str(foreign_skill.id)),
            current_user=SimpleNamespace(id=uuid4(), role="member", tenant_id=tenant_id),
            db=db,
        )

    assert exc.value.status_code == 404
    assert not (tmp_path / str(agent_id) / "skills" / "foreign-skill").exists()

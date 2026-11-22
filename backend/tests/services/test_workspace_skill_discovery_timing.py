import asyncio
import os
import time
from types import SimpleNamespace
from uuid import uuid4


async def _run_with_ticker(operation):
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    async with asyncio.TaskGroup() as tasks:
        ticker = tasks.create_task(tick())
        await asyncio.sleep(0)
        try:
            result = await operation
        finally:
            ticker.cancel()
    return result, ticks


def _catalog_request(tmp_path, monkeypatch):
    from app.config import get_settings
    from app.runtime.session import SessionContext
    from app.services import agent_context

    data = tmp_path.resolve() / "data"
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(data))
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", data)
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", tmp_path / "tool")
    agent_id, session_id = uuid4(), str(uuid4())
    workspace = data / str(agent_id)
    skill = workspace / "session_extensions" / session_id / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: A session skill for SW1.\n---\nDemo instructions.\n", encoding="utf-8")

    turn = SimpleNamespace(agent=SimpleNamespace(id=agent_id, tenant_id=None), requester=SimpleNamespace(id=None))
    request = SimpleNamespace(
        private_web_turn=turn,
        tenant_id=None,
        user_id=None,
        session_context=SessionContext(session_id=session_id),
    )
    return request, agent_id, session_id, workspace


async def test_private_catalog_keeps_loop_running_during_discovery(tmp_path, monkeypatch):
    from app.runtime.invoker import _build_native_skill_catalog
    from app.skills.loader import WorkspaceSkillLoader

    request, agent_id, session_id, workspace = _catalog_request(tmp_path, monkeypatch)
    expected = await _build_native_skill_catalog(request, agent_id, session_id=session_id)
    assert "A session skill for SW1." in expected
    discover = WorkspaceSkillLoader._discover_skill_dirs

    def slow_discover(self, root, *, session_id=None):
        if root == workspace:
            time.sleep(0.3)
        return discover(self, root, session_id=session_id)

    monkeypatch.setattr(WorkspaceSkillLoader, "_discover_skill_dirs", slow_discover)
    catalog, ticks = await _run_with_ticker(_build_native_skill_catalog(request, agent_id, session_id=session_id))

    assert ticks >= 10
    assert catalog == expected


async def test_startup_warms_skill_directories_newest_first_off_loop(tmp_path, monkeypatch):
    from app.main import _maintain_skill_workspaces_after_startup, logger
    from app.services import agent_context, skill_seeder
    from app.skills.loader import WorkspaceSkillLoader

    data, tool = tmp_path.resolve() / "data", tmp_path.resolve() / "tool"
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", data)
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", tool)
    newest, unreadable, oldest = tool / str(uuid4()), data / str(uuid4()), tool / str(uuid4())
    demo = newest / "skills" / "demo" / "SKILL.md"
    demo.parent.mkdir(parents=True)
    demo.write_text("---\nname: demo\ndescription: Demo.\n---\n", encoding="utf-8")
    unreadable.mkdir(parents=True)
    oldest.mkdir()
    for root, mtime in ((oldest, 10), (unreadable, 20), (newest, 30)):
        os.utime(root, (mtime, mtime))

    calls = []
    scanned = {}
    discover = WorkspaceSkillLoader._discover_skill_dirs

    async def cleanup():
        calls.append("cleanup")

    async def push_defaults():
        calls.append("push_defaults")

    def slow_discover(self, root, *, session_id=None):
        calls.append((root, session_id))
        if root == newest:
            time.sleep(0.3)
        if root == unreadable:
            raise OSError("unreadable skill directory", root)
        scanned[root] = discover(self, root, session_id=session_id)
        return scanned[root]

    monkeypatch.setattr(skill_seeder, "cleanup_retired_builtin_skills", cleanup)
    monkeypatch.setattr(skill_seeder, "push_default_skills_to_existing_agents", push_defaults)
    monkeypatch.setattr(WorkspaceSkillLoader, "_discover_skill_dirs", slow_discover)
    messages = []
    sink = logger.add(messages.append, format="{message}", filter=lambda record: "Skill workspace warm-up" in record["message"])
    try:
        result, ticks = await _run_with_ticker(_maintain_skill_workspaces_after_startup())
    finally:
        logger.remove(sink)

    assert calls == ["cleanup", "push_defaults", (newest, None), (unreadable, None), (oldest, None)]
    assert scanned == {newest: (newest / "skills",), oldest: ()}
    assert result is None
    assert ticks >= 10
    message, = messages
    assert "2 directories" in message
    assert str(tmp_path.resolve()) not in message

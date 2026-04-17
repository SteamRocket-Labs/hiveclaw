from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


def test_render_relationships_markdown_uses_explicit_relationships_and_current_tool_name():
    from app.services.relationships_file import render_relationships_markdown

    owner = SimpleNamespace(display_name="张三", username="zhangsan", title="产品经理")
    human_relationships = [
        SimpleNamespace(
            relation="collaborator",
            description="负责日常对接",
            member=SimpleNamespace(
                name="李四",
                title="研发经理",
                department_path="研发部/平台组",
                avatar_url=None,
                email="lisi@example.com",
                feishu_open_id="ou_xxx",
            ),
        )
    ]
    agent_relationships = [
        SimpleNamespace(
            relation="assistant",
            description="专门负责代码审查",
            target_agent=SimpleNamespace(
                id=uuid4(),
                name="代码助手",
                role_description="审查代码",
                avatar_url=None,
            ),
        )
    ]

    content = render_relationships_markdown(
        owner=owner,
        human_relationships=human_relationships,
        agent_relationships=agent_relationships,
    )

    assert "## 我的主人" in content
    assert "## 👤 人类同事" in content
    assert "## 🤖 数字员工同事" in content
    assert "send_message_to_agent" in content
    assert "send_agent_message" not in content
    assert "同公司数字员工" not in content


@pytest.mark.asyncio
async def test_write_relationships_file_skips_when_content_unchanged(monkeypatch, tmp_path):
    """Hot path optimization: identical content must not trigger a disk write."""
    from app.services import relationships_file as mod

    agent_id = uuid4()
    monkeypatch.setattr(mod, "_workspace_root", lambda: tmp_path)

    async def fake_load(db, *, agent_id, include_owner):
        return None, [], []

    monkeypatch.setattr(mod, "_load_relationship_context", fake_load)

    written_first = await mod.write_relationships_file(db=cast(AsyncSession, None), agent_id=agent_id)
    assert written_first is True
    target = tmp_path / str(agent_id) / "relationships.md"
    assert target.exists()

    mtime_before = target.stat().st_mtime_ns
    written_second = await mod.write_relationships_file(db=cast(AsyncSession, None), agent_id=agent_id)
    assert written_second is False
    assert target.stat().st_mtime_ns == mtime_before, "file must not be touched on no-op"


@pytest.mark.asyncio
async def test_write_relationships_file_rewrites_when_content_changes(monkeypatch, tmp_path):
    from app.services import relationships_file as mod

    agent_id = uuid4()
    monkeypatch.setattr(mod, "_workspace_root", lambda: tmp_path)

    state: dict[str, SimpleNamespace | None] = {"member": None}

    async def fake_load(db, *, agent_id, include_owner):
        member = state["member"]
        if member is None:
            return None, [], []
        return None, [member], []

    monkeypatch.setattr(mod, "_load_relationship_context", fake_load)

    assert await mod.write_relationships_file(db=cast(AsyncSession, None), agent_id=agent_id) is True

    state["member"] = SimpleNamespace(
        relation="collaborator",
        description="新增同事",
        member=SimpleNamespace(
            name="王五",
            title="工程师",
            department_path="研发部",
            avatar_url=None,
            email=None,
            feishu_open_id=None,
        ),
    )
    assert await mod.write_relationships_file(db=cast(AsyncSession, None), agent_id=agent_id) is True

    target = tmp_path / str(agent_id) / "relationships.md"
    assert "王五" in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_write_relationships_file_creates_dir_when_missing(monkeypatch, tmp_path):
    from app.services import relationships_file as mod

    agent_id = uuid4()
    monkeypatch.setattr(mod, "_workspace_root", lambda: tmp_path)

    async def fake_load(db, *, agent_id, include_owner):
        return None, [], []

    monkeypatch.setattr(mod, "_load_relationship_context", fake_load)

    assert not (tmp_path / str(agent_id)).exists()
    written = await mod.write_relationships_file(db=cast(AsyncSession, None), agent_id=agent_id)
    assert written is True
    assert (tmp_path / str(agent_id) / "relationships.md").exists()


@pytest.mark.asyncio
async def test_write_relationships_file_offloads_io_to_thread(monkeypatch, tmp_path):
    """Regression guard for the event-loop blockage that caused 30s+ nginx outages.

    File I/O must travel through asyncio.to_thread so a slow Volume mount
    cannot freeze the heartbeat loop.
    """
    import asyncio as _asyncio

    from app.services import relationships_file as mod

    agent_id = uuid4()
    monkeypatch.setattr(mod, "_workspace_root", lambda: tmp_path)

    async def fake_load(db, *, agent_id, include_owner):
        return None, [], []

    monkeypatch.setattr(mod, "_load_relationship_context", fake_load)

    seen: list[str] = []
    real_to_thread = _asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        seen.append(getattr(func, "__name__", str(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr("app.services.relationships_file.asyncio.to_thread", spy_to_thread)

    await mod.write_relationships_file(db=cast(AsyncSession, None), agent_id=agent_id)
    assert "_write_if_changed_sync" in seen, "I/O must be offloaded to a worker thread"

"""Shared relationships.md writer used by APIs and background sync."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.models.agent import Agent
from app.models.org import AgentAgentRelationship, AgentRelationship
from app.models.user import User

logger = logging.getLogger(__name__)

RELATION_LABELS = {
    "direct_leader": "直属上级",
    "collaborator": "协作伙伴",
    "stakeholder": "利益相关者",
    "team_member": "团队成员",
    "subordinate": "下属",
    "mentor": "导师",
    "other": "其他",
}

AGENT_RELATION_LABELS = {
    "peer": "同级协作",
    "supervisor": "上级数字员工",
    "assistant": "助手",
    "collaborator": "协作伙伴",
    "other": "其他",
}


def _workspace_root() -> Path:
    return Path(get_settings().AGENT_DATA_DIR)


def render_relationships_markdown(
    *,
    owner: Any | None,
    human_relationships: list[Any] | tuple[Any, ...],
    agent_relationships: list[Any] | tuple[Any, ...],
) -> str:
    """Render relationships.md from explicit relationship records only."""
    lines = ["# 关系网络", ""]

    if owner is not None:
        lines.append("## 我的主人")
        lines.append(f"- 姓名: {getattr(owner, 'display_name', '') or getattr(owner, 'username', '未设置')}")
        username = getattr(owner, "username", "")
        if username:
            lines.append(f"- 用户名: {username}")
        title = getattr(owner, "title", "")
        if title:
            lines.append(f"- 职位: {title}")
        lines.append("")

    if human_relationships:
        lines.append("## 👤 人类同事")
        lines.append("")
        for relation in human_relationships:
            member = getattr(relation, "member", None)
            if member is None:
                continue
            label = RELATION_LABELS.get(getattr(relation, "relation", ""), getattr(relation, "relation", "其他"))
            lines.append(
                f"### {getattr(member, 'name', '未命名成员')} — "
                f"{getattr(member, 'title', '') or '未设置职位'}"
                f"（{getattr(member, 'department_path', '') or '未设置部门'}）"
            )
            lines.append(f"- 关系：{label}")
            if getattr(member, "feishu_open_id", None):
                lines.append(f"- 飞书ID：{member.feishu_open_id}")
            if getattr(member, "email", None):
                lines.append(f"- 邮箱：{member.email}")
            description = getattr(relation, "description", "")
            if description:
                lines.append(f"- {description}")
            lines.append("")

    if agent_relationships:
        lines.append("## 🤖 数字员工同事")
        lines.append("")
        for relation in agent_relationships:
            target_agent = getattr(relation, "target_agent", None)
            if target_agent is None:
                continue
            label = AGENT_RELATION_LABELS.get(getattr(relation, "relation", ""), getattr(relation, "relation", "其他"))
            lines.append(
                f"### {getattr(target_agent, 'name', '未命名数字员工')} — "
                f"{getattr(target_agent, 'role_description', '') or '数字员工'}"
            )
            lines.append(f"- 关系：{label}")
            lines.append(f"- 可以用 send_message_to_agent 工具给 {target_agent.name} 发消息协作")
            description = getattr(relation, "description", "")
            if description:
                lines.append(f"- {description}")
            lines.append("")

    if len(lines) == 2:
        lines.append("_暂无配置的关系。_")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


async def _load_relationship_context(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    include_owner: bool,
) -> tuple[Any | None, list[Any], list[Any]]:
    agent = await db.get(Agent, agent_id)
    if agent is None:
        return None, [], []

    owner = None
    if include_owner and agent.owner_user_id:
        owner = await db.get(User, agent.owner_user_id)

    human_result = await db.execute(
        select(AgentRelationship)
        .where(AgentRelationship.agent_id == agent_id)
        .options(selectinload(AgentRelationship.member))
    )
    human_relationships = list(human_result.scalars().all())

    agent_result = await db.execute(
        select(AgentAgentRelationship)
        .where(AgentAgentRelationship.agent_id == agent_id)
        .options(selectinload(AgentAgentRelationship.target_agent))
    )
    agent_relationships = list(agent_result.scalars().all())
    return owner, human_relationships, agent_relationships


async def write_relationships_file(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    include_owner: bool = True,
) -> bool:
    """Materialize relationships.md for one agent. Returns True if file was written.

    Compares rendered content against existing file and skips the write when
    unchanged — relationships rarely change but this is called from the
    heartbeat hot path on every tenant tick.
    """
    owner, human_relationships, agent_relationships = await _load_relationship_context(
        db,
        agent_id=agent_id,
        include_owner=include_owner,
    )
    workspace_dir = _workspace_root() / str(agent_id)
    path = workspace_dir / "relationships.md"
    content = render_relationships_markdown(
        owner=owner,
        human_relationships=human_relationships,
        agent_relationships=agent_relationships,
    )
    return await asyncio.to_thread(_write_if_changed_sync, path, workspace_dir, content)


def _write_if_changed_sync(path: Path, parent: Path, content: str) -> bool:
    """Synchronous file I/O bundle — runs in a worker thread to avoid blocking the event loop.

    Combines exists + read + mkdir + write so a single thread hop covers the
    full hot-path write rather than four separate context switches.
    """
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except OSError as exc:
            logger.debug("[relationships_file] Could not read %s for diff, will overwrite: %s", path, exc)
    parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True

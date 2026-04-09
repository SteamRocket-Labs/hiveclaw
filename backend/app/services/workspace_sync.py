"""Workspace sync — write DB data to files that agents can read.

This is the bridge between "admin configures in UI" and "agent reads files".
Data flows: DB → markdown files → agent reads via tools.

Files written:
- enterprise_info_{tenant_id}/company_profile.md  ← company name, intro, culture
- enterprise_info_{tenant_id}/org_structure.md    ← departments + members
- {agent_id}/relationships.md                     ← agent owner + peer agents

Optimization: content is compared before writing. If the file already has the
same content, the write is skipped to avoid unnecessary I/O and prompt cache
invalidation in the kernel.
"""

import logging
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.agent import Agent
from app.models.audit import EnterpriseInfo
from app.models.user import User

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = Path(get_settings().AGENT_DATA_DIR)


def _write_if_changed(path: Path, content: str) -> bool:
    """Write file only if content differs. Returns True if written."""
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except Exception as exc:
            logger.debug("[workspace-sync] Could not read %s for comparison, overwriting: %s", path, exc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _enterprise_dir(tenant_id: uuid.UUID) -> Path:
    d = WORKSPACE_ROOT / f"enterprise_info_{tenant_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─── Company Profile ────────────────────────────────────

async def sync_company_profile(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Write company info from DB to company_profile.md."""
    from app.models.tenant import Tenant

    # Get tenant name
    tenant_result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = tenant_result.scalar_one_or_none()
    company_name = tenant.name if tenant else "Unknown"

    # Get company_profile from enterprise_info table
    result = await db.execute(
        select(EnterpriseInfo).where(
            EnterpriseInfo.tenant_id == tenant_id,
            EnterpriseInfo.info_type == "company_profile",
        )
    )
    info = result.scalar_one_or_none()
    profile_text = ""
    if info and info.content:
        profile_text = info.content.get("text", "") or info.content.get("description", "")

    # Write markdown
    path = _enterprise_dir(tenant_id) / "company_profile.md"
    lines = [
        f"# {company_name}",
        "",
    ]
    if profile_text:
        lines.append(profile_text)
    else:
        lines.append("_公司简介尚未填写。请在公司设置-公司信息中编辑。_")

    if _write_if_changed(path, "\n".join(lines)):
        logger.info(f"[workspace-sync] Wrote company_profile.md for tenant {tenant_id}")


# ─── Organization Structure ─────────────────────────────

async def sync_org_structure(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Write org structure from DB to org_structure.md."""
    from app.models.org import OrgDepartment, OrgMember

    # Departments
    dept_result = await db.execute(
        select(OrgDepartment).where(OrgDepartment.tenant_id == tenant_id).order_by(OrgDepartment.path)
    )
    departments = dept_result.scalars().all()

    # Members
    member_result = await db.execute(
        select(OrgMember).where(OrgMember.tenant_id == tenant_id).order_by(OrgMember.name)
    )
    members = member_result.scalars().all()

    # Write markdown
    path = _enterprise_dir(tenant_id) / "org_structure.md"
    lines = ["# 组织架构", ""]

    if departments:
        lines.append("## 部门")
        for dept in departments:
            indent = "  " * dept.path.count("/") if dept.path else ""
            lines.append(f"{indent}- {dept.name}")
        lines.append("")

    if members:
        lines.append("## 成员")
        for m in members:
            dept_info = f" ({m.department_path})" if m.department_path else ""
            title_info = f" - {m.title}" if m.title else ""
            lines.append(f"- {m.name}{title_info}{dept_info}")
        lines.append("")

    if not departments and not members:
        lines.append("_组织架构尚未同步。请在公司设置-组织结构中同步。_")

    if _write_if_changed(path, "\n".join(lines)):
        logger.info(f"[workspace-sync] Wrote org_structure.md for tenant {tenant_id}")


# ─── Agent Relationships ────────────────────────────────

async def sync_agent_relationships(db: AsyncSession, agent_id: uuid.UUID) -> None:
    """Write agent's relationships to its workspace relationships.md."""
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent or not agent.tenant_id:
        return

    ws = WORKSPACE_ROOT / str(agent_id)
    ws.mkdir(parents=True, exist_ok=True)

    lines = ["# 关系", ""]

    # Owner info
    if agent.owner_user_id:
        owner_result = await db.execute(select(User).where(User.id == agent.owner_user_id))
        owner = owner_result.scalar_one_or_none()
        if owner:
            lines.append("## 我的主人")
            lines.append(f"- 姓名: {owner.display_name}")
            lines.append(f"- 用户名: {owner.username}")
            if owner.title:
                lines.append(f"- 职位: {owner.title}")
            lines.append("")

    # Peer agents in same tenant
    peer_result = await db.execute(
        select(Agent).where(
            Agent.tenant_id == agent.tenant_id,
            Agent.id != agent_id,
            Agent.status.in_(["running", "idle", "creating"]),
        )
    )
    peers = peer_result.scalars().all()

    if peers:
        lines.append("## 同事（同公司数字员工）")
        for peer in peers:
            owner_name = ""
            if peer.owner_user_id:
                po = await db.execute(select(User.display_name).where(User.id == peer.owner_user_id))
                owner_name = po.scalar_one_or_none() or ""
            lines.append(f"- **{peer.name}**: {peer.role_description or '无描述'}" + (f" (属于 {owner_name})" if owner_name else ""))
        lines.append("")

    if len(lines) <= 2:
        lines.append("_暂无关系信息。_")

    if _write_if_changed(ws / "relationships.md", "\n".join(lines)):
        logger.info(f"[workspace-sync] Wrote relationships.md for agent {agent.name}")


# ─── Full Sync ──────────────────────────────────────────

async def sync_all_for_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Full sync: company profile + org + all agent relationships."""
    await sync_company_profile(db, tenant_id)
    await sync_org_structure(db, tenant_id)

    # Sync relationships for all agents in this tenant
    result = await db.execute(
        select(Agent).where(Agent.tenant_id == tenant_id)
    )
    agents = result.scalars().all()
    for agent in agents:
        await sync_agent_relationships(db, agent.id)

    return len(agents)

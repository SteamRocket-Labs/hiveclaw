"""Business-team creation, membership, and existing-employee use grants.

A business team is not an AgentTeam, a directory department, a chat group, or a
temporary subagent. Names are labels. Authority comes from an active team, an
active membership with a user id, and an active same-tenant user.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import NoReturn

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import (
    agent_access_allows_execution,
    check_agent_access,
    effective_agent_owner_id,
    is_scoped_business_admin,
    require_agent_owner_or_admin,
)
from app.core.policy import permission_effect_applies, write_audit_event
from app.database import enter_rls_bypass, get_current_tenant_id
from app.models.agent import Agent
from app.models.org import OrgMember
from app.models.organization_identity import BusinessTeam, BusinessTeamMembership, OrgMemberUserLink
from app.models.security_audit import ResourcePermission
from app.models.user import Department, User

TEAM_USE_ACTIONS = ["read", "execute"]
_TEAM_STATUSES = {"active", "suspended", "archived"}
_MEMBER_ROLES = {"member", "lead"}
_MEMBER_STATUSES = {"active", "left", "suspended"}


class BusinessTeamError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _fail(status_code: int, code: str, message: str) -> NoReturn:
    raise BusinessTeamError(status_code, code, message)


def _clean_name(name: str) -> str:
    cleaned = str(name or "").strip()
    if not cleaned or len(cleaned) > 200:
        _fail(422, "invalid_name", "Enter a team name of 1 to 200 characters.")
    return cleaned


async def flush_business_team_write(db: AsyncSession) -> None:
    await db.flush()


def _acting_tenant_id(actor: User) -> uuid.UUID:
    """Company scope for this request.

    An organization user acts in their own company. A platform administrator
    acts only in the company already pinned for the request.
    """

    if getattr(actor, "role", None) == "platform_admin":
        raw = get_current_tenant_id() or getattr(actor, "tenant_id", None)
        try:
            return uuid.UUID(str(raw))
        except (TypeError, ValueError):
            _fail(400, "tenant_required", "Select a company before managing business teams.")
    if actor.tenant_id is None:
        _fail(400, "tenant_required", "Select a company before managing teams.")
    return actor.tenant_id


async def _live_user(db: AsyncSession, user: User, *, fresh: bool = False) -> User:
    row = await db.get(User, user.id, **({"populate_existing": True} if fresh else {}))
    if row is None and getattr(user, "role", None) == "platform_admin":
        # The selected-company pin hides a platform administrator whose home
        # company is different. Reload that one identity through the existing
        # audited bypass, then keep every later read on the pinned company.
        async with enter_rls_bypass(
            db,
            reason="reload platform administrator before business team action",
            actor_id=str(getattr(user, "id", "") or ""),
        ) as bypass_db:
            row = await bypass_db.get(User, user.id, **({"populate_existing": True} if fresh else {}))
    if row is None or not row.is_active:
        _fail(401, "inactive_user", "Sign in with an active company account.")
    _acting_tenant_id(row)
    return row


def _is_admin(user: User, tenant_id: uuid.UUID) -> bool:
    return is_scoped_business_admin(user, resource_tenant_id=tenant_id)


async def _audit(db: AsyncSession, *, actor: User, action: str, team: BusinessTeam, details: dict) -> None:
    await write_audit_event(
        db,
        event_type="business_team.changed",
        severity="info",
        actor_type="user",
        actor_id=actor.id,
        tenant_id=team.tenant_id,
        action=action,
        resource_type="business_team",
        resource_id=team.id,
        details=details,
    )


async def _department_for_caller(db: AsyncSession, caller: User, department_id: uuid.UUID | None) -> uuid.UUID | None:
    if department_id is None:
        return None
    if caller.department_id is None or department_id != caller.department_id:
        _fail(422, "department_mismatch", "A self-service team can only align with your own department.")
    department = await db.get(Department, department_id)
    if department is None or department.tenant_id != caller.tenant_id:
        _fail(422, "department_mismatch", "That department is not your own company department.")
    return department.id


async def _active_company_user(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> User:
    row = await db.get(User, user_id)
    if row is None or row.tenant_id != tenant_id:
        _fail(404, "user_not_found", "That person is not in this company.")
    if not row.is_active:
        _fail(409, "inactive_user", "That person is inactive and cannot receive team authority.")
    return row


async def _org_member(db: AsyncSession, tenant_id: uuid.UUID, org_member_id: uuid.UUID) -> OrgMember:
    row = await db.get(OrgMember, org_member_id)
    if row is None or row.tenant_id != tenant_id:
        _fail(404, "org_member_not_found", "That directory person is not in this company.")
    return row


async def _assert_linked_pair(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    org_member_id: uuid.UUID,
) -> None:
    rows = list(
        (
            await db.execute(
                select(OrgMemberUserLink).where(
                    OrgMemberUserLink.tenant_id == tenant_id,
                    OrgMemberUserLink.status == "active",
                    or_(
                        OrgMemberUserLink.user_id == user_id,
                        OrgMemberUserLink.org_member_id == org_member_id,
                    ),
                )
            )
        ).scalars().all()
    )
    if not rows or any(row.user_id != user_id or row.org_member_id != org_member_id for row in rows):
        _fail(
            409,
            "org_member_user_mismatch",
            "The directory person and the company account are not an active linked pair.",
        )


async def _reject_unlinked_directory_person(db: AsyncSession, tenant_id: uuid.UUID, org_member_id: uuid.UUID) -> None:
    link = (
        await db.execute(
            select(OrgMemberUserLink.id).where(
                OrgMemberUserLink.tenant_id == tenant_id,
                OrgMemberUserLink.org_member_id == org_member_id,
                OrgMemberUserLink.status == "active",
            )
        )
    ).scalar_one_or_none()
    if link is not None:
        _fail(
            409,
            "registered_user_required",
            "This directory person already has a company account. Add that account instead of an unregistered row.",
        )


def _membership(
    *,
    tenant_id: uuid.UUID,
    team_id: uuid.UUID,
    user_id: uuid.UUID | None,
    org_member_id: uuid.UUID | None,
    role: str,
) -> BusinessTeamMembership:
    return BusinessTeamMembership(
        tenant_id=tenant_id,
        business_team_id=team_id,
        user_id=user_id,
        org_member_id=org_member_id,
        role=role,
        status="active",
    )


async def create_self_service_team(
    db: AsyncSession,
    caller: User,
    *,
    name: str,
    department_id: uuid.UUID | None,
) -> dict:
    actor = await _live_user(db, caller)
    tenant_id = _acting_tenant_id(actor)
    if actor.tenant_id != tenant_id:
        _fail(
            422,
            "self_service_home_required",
            "A self-service team uses your own company account. An administrator can install a team for a lead in the selected company.",
        )
    original_role = actor.role
    department = await _department_for_caller(db, actor, department_id)
    team = BusinessTeam(
        tenant_id=tenant_id,
        name=_clean_name(name),
        status="active",
        created_by_user_id=actor.id,
        department_id=department,
    )
    db.add(team)
    await flush_business_team_write(db)
    lead = _membership(
        tenant_id=tenant_id,
        team_id=team.id,
        user_id=actor.id,
        org_member_id=None,
        role="lead",
    )
    db.add(lead)
    await flush_business_team_write(db)
    if actor.role != original_role:
        actor.role = original_role
    await _audit(
        db,
        actor=actor,
        action="business_team.create_self_service",
        team=team,
        details={"first_lead_user_id": str(actor.id), "department_id": str(department) if department else None},
    )
    return await read_team(db, actor, team.id)


async def create_admin_team(
    db: AsyncSession,
    caller: User,
    *,
    name: str,
    first_lead_user_id: uuid.UUID,
) -> dict:
    actor = await _live_user(db, caller)
    tenant_id = _acting_tenant_id(actor)
    if not _is_admin(actor, tenant_id):
        _fail(403, "admin_required", "Only a company administrator can install a team for someone else.")
    original_role = actor.role
    lead_user = await _active_company_user(db, tenant_id, first_lead_user_id)
    lead_role = lead_user.role
    team = BusinessTeam(
        tenant_id=tenant_id,
        name=_clean_name(name),
        status="active",
        created_by_user_id=actor.id,
        department_id=None,
    )
    db.add(team)
    await flush_business_team_write(db)
    lead = _membership(
        tenant_id=tenant_id,
        team_id=team.id,
        user_id=lead_user.id,
        org_member_id=None,
        role="lead",
    )
    db.add(lead)
    await flush_business_team_write(db)
    actor.role = original_role
    lead_user.role = lead_role
    await _audit(
        db,
        actor=actor,
        action="business_team.create_admin",
        team=team,
        details={"first_lead_user_id": str(lead_user.id), "admin_became_member": actor.id == lead_user.id},
    )
    return await read_team(db, actor, team.id)


async def _teams_for(db: AsyncSession, actor: User, tenant_id: uuid.UUID) -> list[BusinessTeam]:
    if _is_admin(actor, tenant_id):
        rows = await db.execute(
            select(BusinessTeam)
            .where(BusinessTeam.tenant_id == tenant_id)
            .order_by(BusinessTeam.name.asc(), BusinessTeam.id.asc())
        )
        return list(rows.scalars().all())
    rows = await db.execute(
        select(BusinessTeam)
        .join(BusinessTeamMembership, BusinessTeamMembership.business_team_id == BusinessTeam.id)
        .where(
            BusinessTeam.tenant_id == tenant_id,
            BusinessTeamMembership.user_id == actor.id,
            BusinessTeamMembership.status == "active",
        )
        .order_by(BusinessTeam.name.asc(), BusinessTeam.id.asc())
    )
    return list(rows.scalars().all())


async def _memberships(db: AsyncSession, team_ids: list[uuid.UUID], *, fresh: bool = False) -> list[BusinessTeamMembership]:
    if not team_ids:
        return []
    rows = await db.execute(
        select(BusinessTeamMembership)
        .where(BusinessTeamMembership.business_team_id.in_(team_ids))
        .order_by(BusinessTeamMembership.joined_at.asc(), BusinessTeamMembership.id.asc())
        .execution_options(populate_existing=fresh)
    )
    return list(rows.scalars().all())


async def _people(db: AsyncSession, memberships: list[BusinessTeamMembership]) -> tuple[dict, dict, dict]:
    user_ids = {row.user_id for row in memberships if row.user_id is not None}
    org_ids = {row.org_member_id for row in memberships if row.org_member_id is not None}
    users = {}
    if user_ids:
        found = await db.execute(select(User).where(User.id.in_(user_ids)))
        users = {row.id: row for row in found.scalars().all()}
    orgs = {}
    if org_ids:
        found = await db.execute(select(OrgMember).where(OrgMember.id.in_(org_ids)))
        orgs = {row.id: row for row in found.scalars().all()}
    return users, orgs, {}


def _person_label(user: User | None, org_member: OrgMember | None) -> str:
    if user is not None and str(user.display_name or "").strip():
        return str(user.display_name).strip()
    if user is not None and str(user.username or "").strip():
        return str(user.username).strip()
    if org_member is not None and str(org_member.name or "").strip():
        return str(org_member.name).strip()
    return "Unregistered directory member"


def _member_payload(
    row: BusinessTeamMembership,
    *,
    team: BusinessTeam,
    user: User | None,
    org_member: OrgMember | None,
) -> dict:
    registered = user is not None
    authority = bool(
        team.status == "active"
        and row.status == "active"
        and registered
        and user.is_active
        and user.tenant_id == team.tenant_id
    )
    return {
        "id": str(row.id),
        "role": row.role,
        "status": row.status,
        "registered": registered,
        "authority": authority,
        "display_name": _person_label(user, org_member),
        "email": user.email if user is not None else None,
        "user_id": str(row.user_id) if row.user_id else None,
        "org_member_id": str(row.org_member_id) if row.org_member_id else None,
        "joined_at": row.joined_at.isoformat() if row.joined_at else None,
        "left_at": row.left_at.isoformat() if row.left_at else None,
    }


def _my_membership(memberships: list[BusinessTeamMembership], user_id: uuid.UUID) -> BusinessTeamMembership | None:
    active = [row for row in memberships if row.user_id == user_id and row.status == "active"]
    return active[0] if active else None


def _can_manage(actor: User, team: BusinessTeam, mine: BusinessTeamMembership | None) -> bool:
    if _is_admin(actor, team.tenant_id):
        return True
    return bool(team.status == "active" and mine is not None and mine.role == "lead" and mine.user_id == actor.id)


async def _department_names(db: AsyncSession, teams: list[BusinessTeam]) -> dict[uuid.UUID, str]:
    ids = {team.department_id for team in teams if team.department_id is not None}
    if not ids:
        return {}
    rows = await db.execute(select(Department).where(Department.id.in_(ids)))
    return {row.id: row.name for row in rows.scalars().all()}


def _summary(
    team: BusinessTeam,
    memberships: list[BusinessTeamMembership],
    users: dict,
    *,
    actor: User,
    department_name: str | None,
    created_by_display_name: str,
) -> dict:
    mine = _my_membership(memberships, actor.id)
    registered = 0
    for row in memberships:
        user = users.get(row.user_id)
        if row.status == "active" and user is not None and user.is_active:
            registered += 1
    return {
        "id": str(team.id),
        "tenant_id": str(team.tenant_id),
        "name": team.name,
        "status": team.status,
        "department_name": department_name,
        "created_by_display_name": created_by_display_name,
        "my_role": mine.role if mine is not None else None,
        "can_manage": _can_manage(actor, team, mine),
        "member_count": len(memberships),
        "active_registered_count": registered,
    }


async def _creator_labels(db: AsyncSession, actor: User, teams: list[BusinessTeam]) -> dict[uuid.UUID, str]:
    ids = {team.created_by_user_id for team in teams}
    labels = {actor.id: _person_label(actor, None)}
    if not ids:
        return labels
    found = await db.execute(select(User).where(User.id.in_(ids)))
    for row in found.scalars().all():
        labels[row.id] = _person_label(row, None)
    return labels


async def list_teams(db: AsyncSession, caller: User) -> dict:
    actor = await _live_user(db, caller)
    tenant_id = _acting_tenant_id(actor)
    teams = await _teams_for(db, actor, tenant_id)
    memberships = await _memberships(db, [team.id for team in teams])
    users, _, _ = await _people(db, memberships)
    departments = await _department_names(db, teams)
    creators = await _creator_labels(db, actor, teams)
    grouped: dict[uuid.UUID, list[BusinessTeamMembership]] = {}
    for row in memberships:
        grouped.setdefault(row.business_team_id, []).append(row)
    department = await db.get(Department, actor.department_id) if actor.department_id else None
    return {
        "tenant_id": str(tenant_id),
        "caller": {
            "id": str(actor.id),
            "display_name": _person_label(actor, None),
            "role": actor.role,
            "tenant_id": str(actor.tenant_id) if actor.tenant_id else None,
            "department_id": str(actor.department_id) if actor.department_id else None,
            "department_name": department.name if department is not None else None,
            "can_create": actor.tenant_id == tenant_id,
            "can_install": _is_admin(actor, tenant_id),
        },
        "teams": [
            _summary(
                team,
                grouped.get(team.id, []),
                users,
                actor=actor,
                department_name=departments.get(team.department_id) if team.department_id else None,
                created_by_display_name=creators.get(team.created_by_user_id, "Unknown creator"),
            )
            for team in teams
        ],
    }


async def _visible_team(db: AsyncSession, actor: User, team_id: uuid.UUID, *, fresh: bool = False) -> tuple[BusinessTeam, BusinessTeamMembership | None]:
    tenant_id = _acting_tenant_id(actor)
    team = await db.get(BusinessTeam, team_id, **({"populate_existing": True} if fresh else {}))
    if team is None or team.tenant_id != tenant_id:
        _fail(404, "team_not_found", "That business team is not available in this company.")
    memberships = await _memberships(db, [team.id], fresh=fresh)
    mine = _my_membership(memberships, actor.id)
    if mine is None and not _is_admin(actor, team.tenant_id):
        _fail(404, "team_not_found", "That business team is not available in this company.")
    return team, mine


async def _caller_can_use(db: AsyncSession, actor: User, agent_id: uuid.UUID) -> bool:
    try:
        _agent, access_level = await check_agent_access(db, actor, agent_id)
    except HTTPException:
        return False
    return agent_access_allows_execution(access_level)


async def _team_after_write(db: AsyncSession, actor: User, team: BusinessTeam, **fields) -> dict:
    """Return the team when it is still visible, otherwise the committed change only.

    A lead can leave or suspend themselves, and an owner can revoke a share
    after that membership no longer grants visibility. Re-reading the full team
    would raise and roll the authorized write back.
    """

    try:
        payload = await read_team(db, actor, team.id)
    except BusinessTeamError as exc:
        if exc.code != "team_not_found":
            raise
        return {
            "tenant_id": str(team.tenant_id),
            "id": str(team.id),
            "visible": False,
            "access": "lost",
            **fields,
        }
    payload.update(fields)
    return payload


async def _affiliation_payloads(db: AsyncSession, team: BusinessTeam, actor: User) -> list[dict]:
    grants = list(
        (
            await db.execute(
                select(ResourcePermission).where(
                    ResourcePermission.tenant_id == team.tenant_id,
                    ResourcePermission.principal_type == "business_team",
                    ResourcePermission.principal_id == team.id,
                    ResourcePermission.resource_type == "agent",
                    ResourcePermission.revoked_at.is_(None),
                )
            )
        ).scalars().all()
    )
    agent_ids = {row.resource_id for row in grants if row.resource_id is not None}
    agents = {}
    if agent_ids:
        found = await db.execute(select(Agent).where(Agent.id.in_(agent_ids), Agent.deleted_at.is_(None)))
        agents = {row.id: row for row in found.scalars().all()}
    owner_ids = {effective_agent_owner_id(row) for row in agents.values() if effective_agent_owner_id(row)}
    owners = {}
    if owner_ids:
        found = await db.execute(select(User).where(User.id.in_(owner_ids)))
        owners = {row.id: row for row in found.scalars().all()}
    payloads = []
    for agent in agents.values():
        rows = [row for row in grants if row.resource_id == agent.id]
        execute_denied = any(
            permission_effect_applies(row, action="execute") and str(row.effect or "allow") == "deny" for row in rows
        )
        allow = next(
            (
                row
                for row in rows
                if str(row.effect or "allow") == "allow"
                and permission_effect_applies(row, action="read")
            ),
            None,
        )
        # A live allow is the share. A leftover execute deny still blocks the
        # next permission check, but it is not an employee this team can use.
        if allow is None:
            continue
        owner = owners.get(effective_agent_owner_id(agent))
        actions = [action for action in TEAM_USE_ACTIONS if allow is not None and action in (allow.actions or [])]
        payloads.append(
            {
                "agent_id": str(agent.id),
                "agent_name": agent.name,
                "owner_display_name": _person_label(owner, None) if owner is not None else "Unknown owner",
                "actions": actions,
                "effect": "allow" if allow is not None else "deny",
                "use_access": "read_and_execute" if set(TEAM_USE_ACTIONS).issubset(actions) else "limited",
                "execution": "blocked" if execute_denied or "execute" not in actions else "allowed",
                "caller_can_use": await _caller_can_use(db, actor, agent.id),
                "manage_granted": False,
                "home_team": agent.home_business_team_id == team.id,
                "owner_unchanged": True,
            }
        )
    payloads.sort(key=lambda item: (item["agent_name"].lower(), item["agent_id"]))
    return payloads


async def read_team(db: AsyncSession, caller: User, team_id: uuid.UUID) -> dict:
    actor = await _live_user(db, caller)
    team, mine = await _visible_team(db, actor, team_id)
    memberships = await _memberships(db, [team.id])
    users, orgs, _ = await _people(db, memberships)
    departments = await _department_names(db, [team])
    creators = await _creator_labels(db, actor, [team])
    summary = _summary(
        team,
        memberships,
        users,
        actor=actor,
        department_name=departments.get(team.department_id) if team.department_id else None,
        created_by_display_name=creators.get(team.created_by_user_id, "Unknown creator"),
    )
    summary["members"] = [
        _member_payload(row, team=team, user=users.get(row.user_id), org_member=orgs.get(row.org_member_id))
        for row in memberships
    ]
    summary["affiliations"] = await _affiliation_payloads(db, team, actor)
    summary["private_sessions"] = True
    summary["my_role"] = mine.role if mine is not None else None
    return summary


async def update_team(
    db: AsyncSession,
    caller: User,
    team_id: uuid.UUID,
    *,
    name: str | None,
    status: str | None,
) -> dict:
    actor = await _live_user(db, caller)
    team, mine = await _visible_team(db, actor, team_id)
    if not _can_manage(actor, team, mine):
        _fail(403, "team_manage_forbidden", "Only this team's lead or a company administrator can update it.")
    if name is None and status is None:
        _fail(422, "empty_update", "Choose a name or a status to update.")
    if status is not None and status not in _TEAM_STATUSES:
        _fail(422, "invalid_status", "Team status must be active, suspended, or archived.")
    if name is not None:
        team.name = _clean_name(name)
    if status is not None:
        team.status = status
    await flush_business_team_write(db)
    await _audit(
        db,
        actor=actor,
        action="business_team.update",
        team=team,
        details={"name": team.name, "status": team.status},
    )
    return await _team_after_write(db, actor, team)


async def _require_manage(db: AsyncSession, caller: User, team_id: uuid.UUID, *, fresh: bool = False) -> tuple[User, BusinessTeam]:
    actor = await _live_user(db, caller, fresh=fresh)
    team, mine = await _visible_team(db, actor, team_id, fresh=fresh)
    if not _can_manage(actor, team, mine):
        _fail(403, "team_manage_forbidden", "Only this team's lead or a company administrator can change its members.")
    return actor, team


async def add_member(
    db: AsyncSession,
    caller: User,
    team_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
    org_member_id: uuid.UUID | None,
    role: str,
) -> tuple[dict, bool]:
    if role not in _MEMBER_ROLES:
        _fail(422, "invalid_role", "A team role is either member or lead.")
    if user_id is None and org_member_id is None:
        _fail(422, "person_required", "Choose a company account or a directory person.")
    if role == "lead" and user_id is None:
        _fail(422, "lead_requires_user", "A lead must be an active company account.")
    actor, team = await _require_manage(db, caller, team_id)
    if user_id is not None:
        await _active_company_user(db, team.tenant_id, user_id)
    if org_member_id is not None:
        await _org_member(db, team.tenant_id, org_member_id)
        if user_id is not None:
            await _assert_linked_pair(db, tenant_id=team.tenant_id, user_id=user_id, org_member_id=org_member_id)
        else:
            await _reject_unlinked_directory_person(db, team.tenant_id, org_member_id)
    identity_filters = []
    if user_id is not None:
        identity_filters.append(BusinessTeamMembership.user_id == user_id)
    if org_member_id is not None:
        identity_filters.append(BusinessTeamMembership.org_member_id == org_member_id)
    existing = list(
        (
            await db.execute(
                select(BusinessTeamMembership).where(
                    BusinessTeamMembership.business_team_id == team.id,
                    BusinessTeamMembership.status == "active",
                    or_(*identity_filters),
                )
            )
        ).scalars().all()
    )
    created = False
    if user_id is not None:
        matched = next((row for row in existing if row.user_id == user_id), None)
        if matched is None:
            directory = next((row for row in existing if row.user_id is None and row.org_member_id == org_member_id), None)
            if directory is not None and org_member_id is not None:
                directory.user_id = user_id
                directory.role = role
                matched = directory
            else:
                matched = _membership(
                    tenant_id=team.tenant_id,
                    team_id=team.id,
                    user_id=user_id,
                    org_member_id=org_member_id,
                    role=role,
                )
                db.add(matched)
                created = True
        elif org_member_id is not None and matched.org_member_id is None:
            matched.org_member_id = org_member_id
    else:
        matched = next((row for row in existing if row.org_member_id == org_member_id), None)
        if matched is None:
            matched = _membership(
                tenant_id=team.tenant_id,
                team_id=team.id,
                user_id=None,
                org_member_id=org_member_id,
                role="member",
            )
            db.add(matched)
            created = True
    try:
        await flush_business_team_write(db)
    except IntegrityError:
        _fail(409, "membership_exists", "That person already has an active membership on this team.")
    await _audit(
        db,
        actor=actor,
        action="business_team.member_add",
        team=team,
        details={
            "membership_id": str(matched.id),
            "user_id": str(user_id) if user_id else None,
            "org_member_id": str(org_member_id) if org_member_id else None,
            "role": matched.role,
            "created": created,
        },
    )
    payload = await _team_after_write(db, actor, team, membership_id=str(matched.id))
    return payload, created


async def update_membership(
    db: AsyncSession,
    caller: User,
    team_id: uuid.UUID,
    membership_id: uuid.UUID,
    *,
    role: str | None,
    status: str | None,
) -> dict:
    if role is None and status is None:
        _fail(422, "empty_update", "Choose a role or a membership status.")
    if role is not None and role not in _MEMBER_ROLES:
        _fail(422, "invalid_role", "A team role is either member or lead.")
    if status is not None and status not in _MEMBER_STATUSES:
        _fail(422, "invalid_status", "Membership status must be active, suspended, or left.")
    actor, team = await _require_manage(db, caller, team_id)
    row = await db.get(BusinessTeamMembership, membership_id)
    if row is None or row.business_team_id != team.id or row.tenant_id != team.tenant_id:
        _fail(404, "membership_not_found", "That membership is not on this team.")
    if role == "lead" and row.user_id is None:
        _fail(422, "lead_requires_user", "An unregistered directory member cannot be a lead.")
    if role is not None:
        row.role = role
    if status is not None:
        row.status = status
        row.left_at = datetime.now(timezone.utc) if status == "left" else None
    await flush_business_team_write(db)
    await _audit(
        db,
        actor=actor,
        action="business_team.member_update",
        team=team,
        details={"membership_id": str(row.id), "role": row.role, "status": row.status},
    )
    return await _team_after_write(
        db,
        actor,
        team,
        membership_id=str(row.id),
        role=row.role,
        status=row.status,
    )


async def list_member_candidates(db: AsyncSession, caller: User, team_id: uuid.UUID) -> dict:
    actor, _team = await _require_manage(db, caller, team_id)
    return await _candidate_payload(db, actor)


async def list_lead_candidates(db: AsyncSession, caller: User) -> dict:
    actor = await _live_user(db, caller)
    tenant_id = _acting_tenant_id(actor)
    if not _is_admin(actor, tenant_id):
        _fail(403, "admin_required", "Only a company administrator can choose the first lead.")
    return await _candidate_payload(db, actor, tenant_id)


async def _candidate_payload(db: AsyncSession, actor: User, tenant_id: uuid.UUID | None = None) -> dict:
    company_id = tenant_id or _acting_tenant_id(actor)
    rows = list(
        (
            await db.execute(
                select(User)
                .where(User.tenant_id == company_id, User.is_active.is_(True))
                .order_by(User.display_name.asc(), User.username.asc())
                .limit(200)
            )
        ).scalars().all()
    )
    department_ids = {row.department_id for row in rows if row.department_id is not None}
    departments = {}
    if department_ids:
        found = await db.execute(select(Department).where(Department.id.in_(department_ids)))
        departments = {row.id: row.name for row in found.scalars().all()}
    return {
        "tenant_id": str(company_id),
        "people": [
            {
                "id": str(row.id),
                "display_name": _person_label(row, None),
                "email": row.email,
                "department_name": departments.get(row.department_id) if row.department_id else None,
            }
            for row in rows
        ],
    }


def _live_team_grant(rows: list[ResourcePermission]) -> ResourcePermission | None:
    allows = [
        row
        for row in rows
        if row.revoked_at is None and str(row.effect or "allow") == "allow" and "read" in (row.actions or [])
    ]
    return allows[0] if allows else None


async def affiliate_agent(db: AsyncSession, caller: User, team_id: uuid.UUID, agent_id: uuid.UUID) -> dict:
    actor = await _live_user(db, caller)
    tenant_id = _acting_tenant_id(actor)
    team = await db.get(BusinessTeam, team_id)
    if team is None or team.tenant_id != tenant_id or team.status != "active":
        _fail(404, "team_not_found", "Choose an active business team in this company.")
    agent = await require_agent_owner_or_admin(db, actor, agent_id)
    if agent.tenant_id != team.tenant_id:
        _fail(404, "agent_not_found", "That employee is not in this company.")
    if not _is_admin(actor, agent.tenant_id):
        mine = _my_membership(await _memberships(db, [team.id]), actor.id)
        if mine is None:
            _fail(403, "team_membership_required", "Share an employee only with a business team you belong to.")
    owner_before = agent.owner_user_id
    creator_before = agent.creator_id
    sponsor_before = agent.sponsor_user_id
    home_before = agent.home_business_team_id
    rows = list(
        (
            await db.execute(
                select(ResourcePermission).where(
                    ResourcePermission.tenant_id == agent.tenant_id,
                    ResourcePermission.principal_type == "business_team",
                    ResourcePermission.principal_id == team.id,
                    ResourcePermission.resource_type == "agent",
                    ResourcePermission.resource_id == agent.id,
                    ResourcePermission.revoked_at.is_(None),
                    ResourcePermission.effect == "allow",
                )
            )
        ).scalars().all()
    )
    grant = _live_team_grant(rows)
    if grant is None:
        grant = ResourcePermission(
            principal_type="business_team",
            principal_id=team.id,
            resource_type="agent",
            resource_id=agent.id,
            actions=list(TEAM_USE_ACTIONS),
            effect="allow",
            tenant_id=agent.tenant_id,
            created_by_user_id=actor.id,
            conditions={},
            purposes=[],
        )
        db.add(grant)
    else:
        grant.actions = list(TEAM_USE_ACTIONS)
        grant.effect = "allow"
    for extra in rows:
        if extra is not grant:
            extra.revoked_at = datetime.now(timezone.utc)
            extra.revoked_by_user_id = actor.id
    if agent.home_business_team_id is None:
        agent.home_business_team_id = team.id
    agent.owner_user_id = owner_before
    agent.creator_id = creator_before
    agent.sponsor_user_id = sponsor_before
    await flush_business_team_write(db)
    await _audit(
        db,
        actor=actor,
        action="business_team.agent_share",
        team=team,
        details={
            "agent_id": str(agent.id),
            "actions": list(TEAM_USE_ACTIONS),
            "effect": "allow",
            "home_business_team_id": str(agent.home_business_team_id) if agent.home_business_team_id else None,
            "home_changed": home_before != agent.home_business_team_id,
            "owner_user_id": str(agent.owner_user_id) if agent.owner_user_id else None,
        },
    )
    return await _team_after_write(db, actor, team, shared_agent_id=str(agent.id))


async def revoke_affiliation(db: AsyncSession, caller: User, team_id: uuid.UUID, agent_id: uuid.UUID) -> dict:
    actor = await _live_user(db, caller)
    tenant_id = _acting_tenant_id(actor)
    team = await db.get(BusinessTeam, team_id)
    if team is None or team.tenant_id != tenant_id:
        _fail(404, "team_not_found", "That business team is not available in this company.")
    agent = await require_agent_owner_or_admin(db, actor, agent_id)
    rows = list(
        (
            await db.execute(
                select(ResourcePermission).where(
                    ResourcePermission.tenant_id == agent.tenant_id,
                    ResourcePermission.principal_type == "business_team",
                    ResourcePermission.principal_id == team.id,
                    ResourcePermission.resource_type == "agent",
                    ResourcePermission.resource_id == agent.id,
                    ResourcePermission.revoked_at.is_(None),
                    ResourcePermission.effect == "allow",
                )
            )
        ).scalars().all()
    )
    now = datetime.now(timezone.utc)
    for row in rows:
        if set(row.actions or []).intersection(TEAM_USE_ACTIONS):
            row.revoked_at = now
            row.revoked_by_user_id = actor.id
    if agent.home_business_team_id == team.id:
        agent.home_business_team_id = None
    await flush_business_team_write(db)
    await _audit(
        db,
        actor=actor,
        action="business_team.agent_unshare",
        team=team,
        details={"agent_id": str(agent.id), "revoked_grant_count": len(rows)},
    )
    return await _team_after_write(db, actor, team, removed_agent_id=str(agent.id))

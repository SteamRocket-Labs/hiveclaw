"""Approval service for capability-gated tool execution."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.audit import ApprovalRequest, AuditLog
from app.models.channel_config import ChannelConfig
from app.models.external_principal import ExternalPrincipal
from app.models.user import User
from app.services.channel_user_service import channel_user_service
from app.services.enterprise_approval_visibility import is_session_tool_approval
from app.services.feishu_service import feishu_service
from app.services.approval_ticket import (
    ApprovalTicketError,
    hash_approval_execution_envelope,
    hash_policy_snapshot,
    hash_tool_input,
    normalize_tool_arguments,
    restore_approval_execution_envelope,
)


def _same_uuid(left: object, right: object) -> bool:
    if left is None or right is None:
        return False
    return str(left) == str(right)


def _can_resolve_agent_approval(agent: Agent, user: User) -> bool:
    """Return whether user can resolve approvals for this agent.

    The current Agent owner and scoped business administrators (PDEC-013) hold
    resolution authority; a delegated capability is never enough. An inactive
    user cannot resolve, including an inactive current owner.
    """
    from app.core.permissions import is_scoped_business_admin

    if getattr(user, "is_active", True) is False:
        return False
    user_id = getattr(user, "id", None)
    owner_user_id = getattr(agent, "owner_user_id", None) or getattr(agent, "creator_id", None)
    if _same_uuid(owner_user_id, user_id):
        return True
    return is_scoped_business_admin(user, resource_tenant_id=getattr(agent, "tenant_id", None))


async def can_resolve_approval(db: AsyncSession, *, agent: Agent, user: User, approval: ApprovalRequest) -> bool:
    from app.core.permissions import effective_agent_owner_id, load_effective_business_team_ids
    from app.core.policy import check_permission
    from app.models.organization_identity import BusinessTeam, BusinessTeamMembership

    if (is_session_tool_approval(approval) or approval.agent_id != agent.id
        or approval.tenant_id not in {None, agent.tenant_id}):
        return False
    if _can_resolve_agent_approval(agent, user):
        return True
    team_id = getattr(agent, "home_business_team_id", None)
    if team_id is None or user.tenant_id != agent.tenant_id:
        return False
    actor = await db.get(User, user.id, populate_existing=True)
    owner_id = effective_agent_owner_id(agent)
    owner = await db.get(User, owner_id, populate_existing=True) if owner_id else None
    if actor is None or not actor.is_active or owner is None or not owner.is_active:
        return False
    membership = await db.scalar(select(BusinessTeamMembership.id).join(
        BusinessTeam, BusinessTeam.id == BusinessTeamMembership.business_team_id).where(
            BusinessTeamMembership.business_team_id == team_id, BusinessTeamMembership.tenant_id == agent.tenant_id,
            BusinessTeamMembership.user_id == actor.id, BusinessTeamMembership.status == "active",
            BusinessTeamMembership.role == "lead", BusinessTeam.id == team_id,
            BusinessTeam.tenant_id == agent.tenant_id, BusinessTeam.status == "active"))
    if membership is None or actor.tenant_id != agent.tenant_id:
        return False
    principals = [("business_team", team) for team in await load_effective_business_team_ids(db, actor, agent.tenant_id)]
    if actor.department_id:
        principals.append(("department", actor.department_id))
    scope = await approval_reviewer_scope(db, agent=agent, approval=approval)
    return await check_permission(db, principal_type="user", principal_id=actor.id,
        additional_principals=principals, resource_type="agent", resource_id=agent.id, action=approval.action_type,
        context={"tenant_id": str(agent.tenant_id), "user_id": str(actor.id), "agent_id": str(agent.id),
            **scope})


async def approval_reviewer_scope(db, *, agent, approval):
    from app.core.permissions import effective_agent_owner_id

    # Canonical handovers are append-only, so transfer-back cannot revive a prior scope.
    handovers = await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.tenant_id == agent.tenant_id,
        AuditLog.agent_id == agent.id, AuditLog.action == "agent:handover"))
    return {"approval_id": str(approval.id), "approval_owner_scope": f"{effective_agent_owner_id(agent)}:{handovers}"}


async def approval_team_reviewers(db, *, agent, approval, user):
    from app.core.policy import permission_effect_applies
    from app.models.organization_identity import BusinessTeam, BusinessTeamMembership
    from app.models.security_audit import ResourcePermission

    if not _can_resolve_agent_approval(agent, user) or is_session_tool_approval(approval):
        return []
    leads = (await db.scalars(select(User).join(BusinessTeamMembership, BusinessTeamMembership.user_id == User.id)
        .join(BusinessTeam, BusinessTeam.id == BusinessTeamMembership.business_team_id).where(
            BusinessTeam.id == agent.home_business_team_id, BusinessTeam.tenant_id == agent.tenant_id,
            BusinessTeam.status == "active", BusinessTeamMembership.tenant_id == agent.tenant_id,
            BusinessTeamMembership.status == "active", BusinessTeamMembership.role == "lead",
            User.tenant_id == agent.tenant_id, User.is_active.is_(True)))).all()
    grants = (await db.scalars(select(ResourcePermission).where(ResourcePermission.tenant_id == agent.tenant_id,
        ResourcePermission.resource_type == "agent", ResourcePermission.resource_id == agent.id,
        ResourcePermission.principal_type == "user", ResourcePermission.effect == "allow",
        ResourcePermission.conditions["approval_id"].as_string() == str(approval.id)))).all()
    context = await approval_reviewer_scope(db, agent=agent, approval=approval)
    return [{"user_id": str(lead.id), "name": lead.display_name or lead.username,
        "grant_id": next((str(grant.id) for grant in grants if grant.principal_id == lead.id
            and permission_effect_applies(grant, action=approval.action_type, context=context)), None)} for lead in leads]


def approval_notification_user_ids(
    *,
    agent: Agent,
    owner: User | None,
    admins: list[User],
    requester: User | None = None,
    include_requester: bool = False,
) -> list[uuid.UUID]:
    """Live approvers for a pending or resolved approval.

    The active current owner is the recipient. When that owner is missing or
    inactive, live scoped administrators of the company are the fallback.
    The historical creator is included only when they are that owner, a live
    administrator in the fallback, or the original requester.
    """

    from app.core.permissions import effective_agent_owner_id, is_scoped_business_admin

    recipient_ids: list[uuid.UUID] = []
    owner_id = effective_agent_owner_id(agent)
    owner_is_live = (
        owner is not None
        and getattr(owner, "is_active", True) is not False
        and _same_uuid(getattr(owner, "id", None), owner_id)
    )
    if owner_is_live and owner is not None:
        recipient_ids.append(owner.id)
    else:
        tenant_id = getattr(agent, "tenant_id", None)
        for admin in admins:
            if getattr(admin, "is_active", True) is False:
                continue
            if not is_scoped_business_admin(admin, resource_tenant_id=tenant_id):
                continue
            if not any(_same_uuid(admin.id, existing) for existing in recipient_ids):
                recipient_ids.append(admin.id)
    if (
        include_requester
        and requester is not None
        and getattr(requester, "is_active", True) is not False
        and not any(_same_uuid(requester.id, existing) for existing in recipient_ids)
    ):
        recipient_ids.append(requester.id)
    return recipient_ids


async def _load_approval_recipients(
    db: AsyncSession,
    agent: Agent,
    *,
    requester_id: uuid.UUID | None = None,
    include_requester: bool = False,
    approval: ApprovalRequest | None = None,
) -> list[User]:
    from app.core.permissions import effective_agent_owner_id

    owner_id = effective_agent_owner_id(agent)
    owner = None
    if owner_id is not None:
        owner = (await db.execute(select(User).where(User.id == owner_id))).scalar_one_or_none()
    admins: list[User] = []
    if owner is None or getattr(owner, "is_active", True) is False:
        admins = list(
            (
                await db.execute(
                    select(User).where(
                        User.tenant_id == agent.tenant_id,
                        User.is_active.is_(True),
                        User.role.in_(("org_admin", "platform_admin")),
                    )
                )
            )
            .scalars()
            .all()
        )
    requester = None
    if include_requester and requester_id is not None:
        requester = (await db.execute(select(User).where(User.id == requester_id))).scalar_one_or_none()
    recipient_ids = approval_notification_user_ids(
        agent=agent,
        owner=owner,
        admins=admins,
        requester=requester,
        include_requester=include_requester,
    )
    by_id = {}
    for user in [owner, requester, *admins]:
        if user is not None and getattr(user, "id", None) is not None:
            by_id[str(user.id)] = user
    if approval is not None and owner is not None and owner.is_active and getattr(agent, "home_business_team_id", None):
        from app.models.organization_identity import BusinessTeamMembership

        leads = (await db.scalars(select(User).join(BusinessTeamMembership, BusinessTeamMembership.user_id == User.id).where(
            BusinessTeamMembership.business_team_id == agent.home_business_team_id,
            BusinessTeamMembership.tenant_id == agent.tenant_id, BusinessTeamMembership.status == "active",
            BusinessTeamMembership.role == "lead", User.tenant_id == agent.tenant_id, User.is_active.is_(True)))).all()
        for lead in leads:
            if lead.id not in recipient_ids and await can_resolve_approval(db, agent=agent, user=lead, approval=approval):
                recipient_ids.append(lead.id)
                by_id[str(lead.id)] = lead
    return [by_id[str(user_id)] for user_id in recipient_ids if str(user_id) in by_id]


class ApprovalService:
    """Manage approval request lifecycle and post-approval execution."""

    async def request_approval(
        self,
        db: AsyncSession,
        agent: Agent,
        *,
        action_type: str,
        details: dict,
    ) -> dict:
        """Create a pending approval request and notify the responsible user."""
        approval_id = uuid.uuid4()
        tool_name = str(details.get("tool") or "").strip() or None
        arguments = normalize_tool_arguments(details.get("args")) if tool_name else None
        requested_by = None
        try:
            requested_by = uuid.UUID(str(details.get("requested_by"))) if details.get("requested_by") else None
        except ValueError:
            requested_by = None
        policy_snapshot = dict(
            details.get("policy_snapshot")
            or {
                "capability": action_type,
                "tenant_id": str(agent.tenant_id) if agent.tenant_id else None,
                "agent_id": str(agent.id),
                "reason": details.get("reason"),
                "origin": details.get("origin"),
            }
        )
        execution_envelope = details.get("execution_envelope")
        execution_envelope_hash = None
        requested_by_external_principal_id = None
        if tool_name:
            if not isinstance(execution_envelope, dict):
                raise ApprovalTicketError("tool approval requires an immutable execution envelope")
            execution_envelope_hash = hash_approval_execution_envelope(execution_envelope)
            restored_envelope = restore_approval_execution_envelope(
                execution_envelope,
                expected_hash=execution_envelope_hash,
            )
            if restored_envelope.agent_id != agent.id or restored_envelope.tenant_id != agent.tenant_id:
                raise ApprovalTicketError("approval execution envelope agent or tenant mismatch")
            if requested_by is None or restored_envelope.requester_user_id != requested_by:
                raise ApprovalTicketError("approval execution envelope requester mismatch")
            execution_identity = restored_envelope.execution_identity
            if execution_identity is not None and execution_identity.identity_type == "external_principal_bound":
                requested_by_external_principal_id = execution_identity.identity_id
                principal = (
                    await db.execute(
                        select(ExternalPrincipal).where(
                            ExternalPrincipal.id == requested_by_external_principal_id,
                            ExternalPrincipal.tenant_id == agent.tenant_id,
                            ExternalPrincipal.linked_user_id == requested_by,
                            ExternalPrincipal.status == "active",
                        )
                    )
                ).scalar_one_or_none()
                if principal is None:
                    raise ApprovalTicketError("external principal binding mismatch")
        db.add(
            AuditLog(
                agent_id=agent.id,
                tenant_id=agent.tenant_id,
                external_principal_id=requested_by_external_principal_id,
                action=f"approval_request:{action_type}",
                details=details,
            )
        )
        approval = ApprovalRequest(
            id=approval_id,
            agent_id=agent.id,
            tenant_id=agent.tenant_id,
            action_type=action_type,
            details=details,
            requested_by=requested_by,
            requested_by_external_principal_id=requested_by_external_principal_id,
            decision_id=str(details.get("decision_id") or f"approval:{approval_id}"),
            tool_name=tool_name,
            normalized_arguments=arguments,
            input_hash=hash_tool_input(tool_name, arguments) if tool_name and arguments is not None else None,
            policy_snapshot=policy_snapshot,
            policy_snapshot_hash=hash_policy_snapshot(policy_snapshot),
            execution_envelope=dict(execution_envelope) if isinstance(execution_envelope, dict) else None,
            execution_envelope_hash=execution_envelope_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            execution_status="pending",
            execution_idempotency_key=f"approval:{approval_id}",
        )
        db.add(approval)
        await db.flush()

        logger.info("Approval requested for %s by agent %s", action_type, agent.name)
        await self._notify_pending_approval(db, agent, approval)

        return {
            "allowed": False,
            "approval_id": str(approval.id),
            "message": "Approval requested from the current responsible approver",
        }

    async def resolve_approval(
        self, db: AsyncSession, approval_id: uuid.UUID, user: User, action: str
    ) -> ApprovalRequest:
        """Approve or reject a pending approval request."""
        result = await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == approval_id).with_for_update())
        approval = result.scalar_one_or_none()
        if not approval:
            raise ValueError("Approval not found")

        if approval.status != "pending":
            raise ValueError("Approval already resolved")
        if is_session_tool_approval(approval):
            raise ValueError("Session tool permissions must be resolved inside the session")

        agent_result = await db.execute(select(Agent).where(Agent.id == approval.agent_id))
        agent = agent_result.scalar_one_or_none()
        if agent and getattr(approval, "tenant_id", None) is None:
            approval.tenant_id = agent.tenant_id
        # The Agent reference is the only authority anchor for this decision:
        # a missing Agent row must fail closed instead of skipping the check.
        if not agent or not await can_resolve_approval(db, agent=agent, user=user, approval=approval):
            raise ValueError("Only the current Agent owner or tenant organization admin, or an authorized team lead, can resolve approvals")

        local_agent_resolution = None

        # M-17: Auto-reject approvals older than 7 days (AFTER authorization check)
        from datetime import timedelta

        if approval.created_at and (datetime.now(timezone.utc) - approval.created_at) > timedelta(days=7):
            approval.status = "rejected"
            approval.execution_status = "rejected"
            approval.resolved_at = datetime.now(timezone.utc)
            logger.info("Approval %s auto-rejected (older than 7 days)", approval.id)
            from app.services.local_agent_channel_service import (
                apply_local_agent_action_approval,
                notify_local_agent_action_resolution,
            )

            local_agent_resolution = await apply_local_agent_action_approval(db, approval=approval)
            await db.flush()
            await db.commit()
            await notify_local_agent_action_resolution(local_agent_resolution)
            return approval

        approval.status = "approved" if action == "approve" else "rejected"
        approval.execution_status = approval.status
        approval.resolved_at = datetime.now(timezone.utc)
        approval.resolved_by = user.id

        execution_task = None
        if approval.status == "approved" and getattr(approval, "tool_name", None):
            if agent is None:
                raise ValueError("Approved tool execution requires an existing Agent")
            if approval.expires_at is not None and approval.expires_at <= datetime.now(timezone.utc):
                approval.execution_status = "needs_reapproval"
                approval.execution_result = "Approval expired before its decision could be executed."
                approval.execution_receipt = {
                    **dict(getattr(approval, "execution_receipt", None) or {}),
                    "status": "needs_reapproval",
                    "side_effect_state": "not_started",
                    "automatic_replay": False,
                    "reason": "approval_expired_before_decision",
                }
            else:
                from app.services.approval_execution_runtime import build_approval_execution_task

                execution_task = build_approval_execution_task(
                    approval,
                    approved_by_user_id=user.id,
                )
                from app.services.session_writer_epoch import assign_runtime_task_writer_generation

                await assign_runtime_task_writer_generation(db, execution_task)
                db.add(execution_task)
                # The scalar FK is intentionally used instead of an ORM
                # relationship; insert the durable job first so PostgreSQL can
                # validate the link within this same transaction.
                await db.flush()
                approval.execution_task_id = execution_task.id
                approval.execution_status = "queued"
                approval.execution_receipt = {
                    **dict(getattr(approval, "execution_receipt", None) or {}),
                    "status": "queued",
                    "side_effect_state": "not_started",
                    "automatic_replay": True,
                    "execution_task_id": str(execution_task.id),
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                }

        from app.services.local_agent_channel_service import apply_local_agent_action_approval

        local_agent_resolution = await apply_local_agent_action_approval(db, approval=approval)

        db.add(
            AuditLog(
                user_id=user.id,
                agent_id=approval.agent_id,
                tenant_id=getattr(approval, "tenant_id", None) or (agent.tenant_id if agent else None),
                action=f"approval_{approval.status}",
                details={
                    "approval_id": str(approval.id),
                    "action_type": approval.action_type,
                    "decision_id": getattr(approval, "decision_id", None),
                },
            )
        )

        try:
            from app.core.policy import write_audit_event

            await write_audit_event(
                db,
                event_type="approval.resolved",
                severity="warn",
                actor_type="user",
                actor_id=user.id,
                tenant_id=agent.tenant_id if agent else uuid.UUID(int=0),
                action=f"approval_{approval.status}",
                resource_type="approval",
                resource_id=approval.id,
                details={
                    "action_type": approval.action_type,
                    "agent_name": agent.name if agent else None,
                    "decision_id": getattr(approval, "decision_id", None),
                },
            )
        except Exception:
            logger.warning("Audit write failed for approval.resolved", exc_info=True)

        if approval.tool_name and approval.execution_status in {"rejected", "needs_reapproval"}:
            from app.services.approval_execution_runtime import _enqueue_origin_continuation

            approval.execution_result = approval.execution_result or "The responsible approver rejected the action. The tool was not executed."
            await _enqueue_origin_continuation(
                db, approval=approval, task=None, approval_status=approval.execution_status,
                execution_result=approval.execution_result,
            )

        # Release the approval/audit transaction before running any approved
        # external action. write_audit_event takes a per-tenant advisory
        # transaction lock; holding that lock while executing tools can block
        # unrelated audit writes for the same tenant.
        await db.flush()
        await db.commit()

        if local_agent_resolution is not None:
            from app.services.local_agent_channel_service import notify_local_agent_action_resolution

            await notify_local_agent_action_resolution(local_agent_resolution)

        if execution_task is not None:
            from app.services.runtime_task_worker import notify_runtime_task_worker

            await notify_runtime_task_worker(
                reason="approval_execution_queued",
                runtime_task_id=execution_task.id,
            )

        if agent:
            from app.services.notification_service import send_notification

            status_label = (
                "blocked"
                if approval.execution_status == "failed"
                else ("approved" if approval.status == "approved" else "rejected")
            )
            body_text = json.dumps(approval.details, ensure_ascii=False)[:200]
            if execution_task is not None:
                body_text = "Approved action queued for durable execution."
            elif approval.execution_status == "needs_reapproval":
                body_text = "Approval expired; submit a new request before execution."
            elif approval.execution_status == "failed" and approval.execution_result:
                body_text = approval.execution_result[:200]
            requester_id = None
            requested_by = approval.details.get("requested_by") if approval.details else None
            if requested_by:
                try:
                    requester_id = uuid.UUID(str(requested_by))
                except (ValueError, AttributeError) as notify_err:
                    logger.debug("Could not parse approval requester %s: %s", requested_by, notify_err)
            approvers = await _load_approval_recipients(db, agent, approval=approval)
            approver_ids = {str(user.id) for user in approvers}
            for recipient in approvers:
                await send_notification(
                    db,
                    user_id=recipient.id,
                    type="approval_resolved",
                    title=f"[{agent.name}] {approval.action_type} — {status_label}",
                    body=body_text if _can_resolve_agent_approval(agent, recipient) else "Review the authorized team action in team approvals.",
                    link=f"/agents/{agent.id}#approvals" if _can_resolve_agent_approval(agent, recipient) else "/team",
                    ref_id=approval.id,
                )
            if requester_id is not None and str(requester_id) not in approver_ids:
                requester_recipients = await _load_approval_recipients(
                    db,
                    agent,
                    requester_id=requester_id,
                    include_requester=True,
                    approval=approval,
                )
                for recipient in requester_recipients:
                    if str(recipient.id) in approver_ids:
                        continue
                    await send_notification(
                        db,
                        user_id=recipient.id,
                        type="approval_resolved",
                        title=f"[{agent.name}] {approval.action_type} — {status_label}",
                        body=body_text,
                        link=f"/agents/{agent.id}#activityLog",
                        ref_id=approval.id,
                    )

        await db.flush()
        return approval

    async def _notify_pending_approval(self, db: AsyncSession, agent: Agent, approval: ApprovalRequest) -> None:
        """Send pending approval notification via web and Feishu when available."""
        from app.services.notification_service import send_notification

        recipients = await _load_approval_recipients(db, agent, approval=approval)
        body = json.dumps(approval.details, ensure_ascii=False)[:200]
        for recipient in recipients:
            await send_notification(
                db,
                user_id=recipient.id,
                type="approval_pending",
                title=f"[{agent.name}] approval required: {approval.action_type}",
                body=body if _can_resolve_agent_approval(agent, recipient) else "Review the authorized team action in team approvals.",
                link=f"/agents/{agent.id}#approvals" if _can_resolve_agent_approval(agent, recipient) else "/team",
                ref_id=approval.id,
            )

        channel_result = await db.execute(select(ChannelConfig).where(ChannelConfig.agent_id == agent.id,
            ChannelConfig.tenant_id == agent.tenant_id, ChannelConfig.channel_type == 'feishu',
            ChannelConfig.is_configured.is_(True)))
        channel = channel_result.scalars().first()
        if channel and channel.app_id and channel.app_secret:
            card_body = json.dumps(approval.details, ensure_ascii=False)[:500]
            for recipient in recipients:
                delivery_target = await channel_user_service.get_feishu_delivery_target(db, user=recipient)
                if not delivery_target:
                    continue
                receive_id, id_type = delivery_target
                await feishu_service.send_approval_card(
                    channel.app_id,
                    channel.app_secret,
                    receive_id,
                    id_type,
                    agent.name,
                    approval.action_type,
                    card_body if _can_resolve_agent_approval(agent, recipient) else "Review the authorized team action in team approvals.",
                    str(approval.id),
                    extra_config=channel.extra_config,
                )


approval_service = ApprovalService()

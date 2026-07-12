"""Approval service for capability-gated tool execution."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.audit import ApprovalRequest, AuditLog, ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.external_principal import ExternalPrincipal
from app.models.runtime_task import RuntimeTask
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
    """Return whether user can resolve approvals for this agent."""
    if _same_uuid(getattr(agent, "creator_id", None), getattr(user, "id", None)):
        return True
    if getattr(user, "role", None) == "platform_admin":
        return True
    if getattr(user, "role", None) == "org_admin":
        return _same_uuid(getattr(agent, "tenant_id", None), getattr(user, "tenant_id", None))
    return False


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
            "message": "Approval requested from creator",
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
        if agent and not _can_resolve_agent_approval(agent, user):
            raise ValueError("Only the agent creator, tenant org admin, or platform admin can resolve approvals")

        # M-17: Auto-reject approvals older than 7 days (AFTER authorization check)
        from datetime import timedelta

        if approval.created_at and (datetime.now(timezone.utc) - approval.created_at) > timedelta(days=7):
            approval.status = "rejected"
            approval.resolved_at = datetime.now(timezone.utc)
            logger.info("Approval %s auto-rejected (older than 7 days)", approval.id)
            await db.flush()
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

        # Release the approval/audit transaction before running any approved
        # external action. write_audit_event takes a per-tenant advisory
        # transaction lock; holding that lock while executing tools can block
        # unrelated audit writes for the same tenant.
        await db.flush()
        await db.commit()

        if execution_task is not None:
            from app.services.runtime_task_worker import notify_runtime_task_worker

            await notify_runtime_task_worker(
                reason="approval_execution_queued",
                runtime_task_id=execution_task.id,
            )

        if agent:
            from app.services.notification_service import send_notification

            status_label = "approved" if approval.status == "approved" else "rejected"
            body_text = json.dumps(approval.details, ensure_ascii=False)[:200]
            if execution_task is not None:
                body_text = "Approved action queued for durable execution."
            elif approval.execution_status == "needs_reapproval":
                body_text = "Approval expired; submit a new request before execution."
            await send_notification(
                db,
                user_id=agent.creator_id,
                type="approval_resolved",
                title=f"[{agent.name}] {approval.action_type} — {status_label}",
                body=body_text,
                link=f"/agents/{agent.id}#approvals",
                ref_id=approval.id,
            )

            requested_by = approval.details.get("requested_by") if approval.details else None
            if requested_by:
                try:
                    requester_id = uuid.UUID(requested_by)
                    if requester_id != agent.creator_id:
                        await send_notification(
                            db,
                            user_id=requester_id,
                            type="approval_resolved",
                            title=f"[{agent.name}] {approval.action_type} — {status_label}",
                            body=body_text,
                            link=f"/agents/{agent.id}#activityLog",
                            ref_id=approval.id,
                        )
                except (ValueError, AttributeError) as notify_err:
                    logger.debug("Could not notify requester %s: %s", requested_by, notify_err)

        await db.flush()
        return approval

    async def _publish_approval_result_to_origin(
        self,
        db: AsyncSession,
        approval: ApprovalRequest,
        *,
        agent: Agent | None,
        approved_by_user_id: uuid.UUID,
        execution_result: str | None,
    ) -> dict | None:
        """Publish a post-approval tool result back to the originating session."""
        details = dict(approval.details or {})
        session_id = str(details.get("session_id") or details.get("conversation_id") or "").strip()
        if not session_id:
            return None

        tool_name = str(details.get("tool") or approval.action_type or "").strip() or "approved_action"
        result_text = "" if execution_result is None else str(execution_result)
        payload = {
            "type": "approval_tool_result",
            "approval_id": str(approval.id),
            "status": approval.status,
            "action_type": approval.action_type,
            "tool_name": tool_name,
            "approved_by_user_id": str(approved_by_user_id),
            "result": result_text,
            "session_id": session_id,
        }

        user_id = approved_by_user_id or (agent.creator_id if agent else None)
        if user_id is not None:
            db.add(
                ChatMessage(
                    agent_id=approval.agent_id,
                    tenant_id=getattr(agent, "tenant_id", None),
                    user_id=user_id,
                    role="tool_call",
                    content=json.dumps(payload, ensure_ascii=False),
                    conversation_id=session_id,
                )
            )

        pending_content = (
            "[Approval result]\n"
            f"Approval {approval.id} was {approval.status}.\n"
            f"Tool: {tool_name}\n"
            f"Result: {result_text[:4000]}\n"
            "Use this as the result of the previously approval-gated tool call and continue the original task."
        )
        try:
            result = await db.execute(
                select(RuntimeTask).where(
                    RuntimeTask.parent_agent_id == approval.agent_id,
                    RuntimeTask.task_type == "web_chat_turn",
                    RuntimeTask.status == "running",
                )
            )
            for task in result.scalars().all():
                metadata = dict(getattr(task, "metadata_json", None) or {})
                if str(metadata.get("session_id") or "") != session_id:
                    continue
                pending = [item for item in metadata.get("pending_user_messages") or [] if isinstance(item, dict)]
                pending.append(
                    {
                        "id": f"approval-{approval.id}",
                        "approval_id": str(approval.id),
                        "content": pending_content,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                metadata["pending_user_messages"] = pending
                metadata["pending_user_message_count"] = len(pending)
                task.metadata_json = metadata
                break
        except Exception:
            logger.warning("Failed to queue approval result for origin session %s", session_id, exc_info=True)
        return payload

    async def _notify_pending_approval(self, db: AsyncSession, agent: Agent, approval: ApprovalRequest) -> None:
        """Send pending approval notification via web and Feishu when available."""
        from app.services.notification_service import send_notification

        await send_notification(
            db,
            user_id=agent.creator_id,
            type="approval_pending",
            title=f"[{agent.name}] approval required: {approval.action_type}",
            body=json.dumps(approval.details, ensure_ascii=False)[:200],
            link=f"/agents/{agent.id}#approvals",
            ref_id=approval.id,
        )

        channel_result = await db.execute(select(ChannelConfig).where(ChannelConfig.agent_id == agent.id))
        channel = channel_result.scalars().first()
        if channel and channel.app_id and channel.app_secret:
            creator_result = await db.execute(select(User).where(User.id == agent.creator_id))
            creator = creator_result.scalar_one_or_none()
            delivery_target = (
                await channel_user_service.get_feishu_delivery_target(db, user=creator) if creator else None
            )
            if creator and delivery_target:
                receive_id, id_type = delivery_target
                await feishu_service.send_approval_card(
                    channel.app_id,
                    channel.app_secret,
                    receive_id,
                    id_type,
                    agent.name,
                    approval.action_type,
                    json.dumps(approval.details, ensure_ascii=False)[:500],
                    str(approval.id),
                    extra_config=channel.extra_config,
                )


approval_service = ApprovalService()

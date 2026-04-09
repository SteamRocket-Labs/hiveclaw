"""Approval service for capability-gated tool execution."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.audit import ApprovalRequest, AuditLog
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.services.feishu_service import feishu_service


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
        db.add(
            AuditLog(
                agent_id=agent.id,
                action=f"approval_request:{action_type}",
                details=details,
            )
        )

        approval = ApprovalRequest(
            agent_id=agent.id,
            action_type=action_type,
            details=details,
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
        result = await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == approval_id))
        approval = result.scalar_one_or_none()
        if not approval:
            raise ValueError("Approval not found")

        if approval.status != "pending":
            raise ValueError("Approval already resolved")

        agent_result = await db.execute(select(Agent).where(Agent.id == approval.agent_id))
        agent = agent_result.scalar_one_or_none()
        if agent and agent.creator_id != user.id and user.role != "platform_admin":
            raise ValueError("Only the agent creator or platform admin can resolve approvals")

        # M-17: Auto-reject approvals older than 7 days (AFTER authorization check)
        from datetime import timedelta
        if approval.created_at and (datetime.now(timezone.utc) - approval.created_at) > timedelta(days=7):
            approval.status = "rejected"
            approval.resolved_at = datetime.now(timezone.utc)
            logger.info("Approval %s auto-rejected (older than 7 days)", approval.id)
            await db.flush()
            return approval

        approval.status = "approved" if action == "approve" else "rejected"
        approval.resolved_at = datetime.now(timezone.utc)
        approval.resolved_by = user.id

        db.add(
            AuditLog(
                user_id=user.id,
                agent_id=approval.agent_id,
                action=f"approval_{approval.status}",
                details={"approval_id": str(approval.id), "action_type": approval.action_type},
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
                details={"action_type": approval.action_type, "agent_name": agent.name if agent else None},
            )
        except Exception:
            logger.warning("Audit write failed for approval.resolved", exc_info=True)

        execution_result = None
        if approval.status == "approved" and approval.details:
            execution_result = await self._execute_approved_action(
                approval.agent_id, approval.action_type, approval.details
            )
            execution_status = "success" if execution_result and "failed" not in str(execution_result).lower() else "failed"
            logger.info(
                "Post-approval execution for %s: status=%s result=%s",
                approval.action_type, execution_status, str(execution_result)[:200],
            )

        if agent:
            from app.services.notification_service import send_notification

            status_label = "approved" if approval.status == "approved" else "rejected"
            body_text = json.dumps(approval.details, ensure_ascii=False)[:200]
            if execution_result:
                body_text = f"Result: {execution_result}"
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

    async def _execute_approved_action(self, agent_id: uuid.UUID, action_type: str, details: dict) -> str | None:
        """Execute the tool action that was approved."""
        tool_name = details.get("tool")
        args_raw = details.get("args", "{}")
        if not tool_name:
            return None

        try:
            import ast

            if isinstance(args_raw, str):
                try:
                    arguments = ast.literal_eval(args_raw)
                except (ValueError, SyntaxError):
                    try:
                        arguments = json.loads(args_raw)
                    except json.JSONDecodeError:
                        arguments = {}
            else:
                arguments = args_raw

            from app.services.agent_tools import _execute_tool_direct

            return await _execute_tool_direct(tool_name, arguments, agent_id)
        except Exception as exc:
            logger.error("Failed to execute approved action %s: %s", tool_name, exc)
            return f"Execution failed: {exc}"

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
            if creator and (creator.feishu_user_id or creator.feishu_open_id):
                receive_id = creator.feishu_user_id or creator.feishu_open_id
                id_type = "user_id" if creator.feishu_user_id else "open_id"
                await feishu_service.send_approval_card(
                    channel.app_id,
                    channel.app_secret,
                    receive_id,
                    id_type,
                    approval.action_type,
                    json.dumps(approval.details, ensure_ascii=False)[:500],
                    str(approval.id),
                )


approval_service = ApprovalService()

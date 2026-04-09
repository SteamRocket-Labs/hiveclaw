"""Email domain — email config loading and tool dispatch."""

import logging
import uuid
from pathlib import Path

from sqlalchemy import select

from app.database import async_session
from app.tools.result_envelope import render_tool_error

logger = logging.getLogger(__name__)


async def _get_email_config(agent_id: uuid.UUID) -> dict:
    """Retrieve per-agent email config from the send_email tool's AgentTool config."""
    from app.models.tool import Tool, AgentTool

    async with async_session() as db:
        # Find the send_email tool
        r = await db.execute(select(Tool).where(Tool.name == "send_email"))
        tool = r.scalar_one_or_none()
        if not tool:
            return {}

        # Get per-agent config
        at_r = await db.execute(
            select(AgentTool).where(
                AgentTool.agent_id == agent_id,
                AgentTool.tool_id == tool.id,
            )
        )
        at = at_r.scalar_one_or_none()
        agent_config = (at.config or {}) if at else {}
        # Merge global + agent override
        return {**(tool.config or {}), **agent_config}


async def _handle_email_tool(tool_name: str, agent_id: uuid.UUID, ws: Path, arguments: dict) -> str:
    """Dispatch email tool calls to the email_service module."""
    from app.services.email_service import (
        send_email,
        read_emails,
        reply_email,
        validate_email_tool_request,
    )

    config = await _get_email_config(agent_id)
    preflight_error = validate_email_tool_request(
        tool_name=tool_name,
        config=config or {"email_provider": "email"},
        arguments=arguments,
        workspace_path=ws,
    )
    if preflight_error:
        return preflight_error

    try:
        if tool_name == "send_email":
            return await send_email(
                config=config,
                to=arguments.get("to", ""),
                subject=arguments.get("subject", ""),
                body=arguments.get("body", ""),
                cc=arguments.get("cc"),
                attachments=arguments.get("attachments"),
                workspace_path=ws,
            )
        elif tool_name == "read_emails":
            return await read_emails(
                config=config,
                limit=arguments.get("limit", 10),
                search=arguments.get("search"),
                folder=arguments.get("folder", "INBOX"),
            )
        elif tool_name == "reply_email":
            return await reply_email(
                config=config,
                message_id=arguments.get("message_id", ""),
                body=arguments.get("body", ""),
            )
        else:
            return render_tool_error(
                tool_name=tool_name,
                error_class="bad_arguments",
                message=f"Unknown email tool: {tool_name}",
                provider="email",
                retryable=False,
                actionable_hint="Use one of send_email, read_emails, or reply_email.",
            )
    except Exception as e:
        provider = str((config or {}).get("email_provider") or "email")
        return render_tool_error(
            tool_name=tool_name,
            error_class="provider_error",
            message=f"Email tool error: {str(e)[:200]}",
            provider=provider,
            retryable=False,
            actionable_hint="Check mailbox configuration and provider response before retrying.",
        )

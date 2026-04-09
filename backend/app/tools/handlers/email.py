"""Email tools — send, read, and reply to emails."""

from __future__ import annotations

from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest


# -- send_email ---------------------------------------------------------------

@tool(ToolMeta(
    name="send_email",
    description="Send an email to one or more recipients. Supports subject, body text, CC, and file attachments from workspace. Use for external inbox delivery or when a message must land in email. For internal recurring notifications, prefer Feishu tools first. Requires email configuration in tool settings.",
    parameters={
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Recipient email address(es), comma-separated for multiple",
            },
            "subject": {
                "type": "string",
                "description": "Email subject line",
            },
            "body": {
                "type": "string",
                "description": "Email body text",
            },
            "cc": {
                "type": "string",
                "description": "CC recipients, comma-separated (optional)",
            },
            "attachments": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of workspace-relative file paths to attach (optional)",
            },
        },
        "required": ["to", "subject", "body"],
    },
    category="email",
    display_name="Send Email",
    icon="\U0001f4e7",
    governance="sensitive",
    adapter="request",
))
async def send_email(request: ToolExecutionRequest) -> str:
    from app.services.agent_tools import _handle_email_tool
    return await _handle_email_tool(
        "send_email",
        request.context.agent_id,
        request.context.workspace,
        request.arguments,
    )


# -- read_emails --------------------------------------------------------------

@tool(ToolMeta(
    name="read_emails",
    description="Read emails from your inbox. Can limit the number returned and search by criteria (e.g. FROM, SUBJECT, SINCE date). Use this before reply_email so you can capture the exact Message-ID. Requires email configuration in tool settings.",
    parameters={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max number of emails to return (default 10, max 30)",
            },
            "search": {
                "type": "string",
                "description": "IMAP search criteria, e.g. 'FROM \"john@example.com\"', 'SUBJECT \"meeting\"', 'SINCE 01-Mar-2026'. Default: all emails.",
            },
            "folder": {
                "type": "string",
                "description": "Mailbox folder, default INBOX",
            },
        },
    },
    category="email",
    display_name="Read Emails",
    icon="\U0001f4ec",
    adapter="request",
))
async def read_emails(request: ToolExecutionRequest) -> str:
    from app.services.agent_tools import _handle_email_tool
    return await _handle_email_tool(
        "read_emails",
        request.context.agent_id,
        request.context.workspace,
        request.arguments,
    )


# -- reply_email --------------------------------------------------------------

@tool(ToolMeta(
    name="reply_email",
    description="Reply to an email by its Message-ID. Maintains the email thread with proper In-Reply-To headers. Requires email configuration in tool settings.",
    parameters={
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "Message-ID of the email to reply to (from read_emails output)",
            },
            "body": {
                "type": "string",
                "description": "Reply body text",
            },
        },
        "required": ["message_id", "body"],
    },
    category="email",
    display_name="Reply Email",
    icon="\u21a9\ufe0f",
    governance="sensitive",
    adapter="request",
))
async def reply_email(request: ToolExecutionRequest) -> str:
    from app.services.agent_tools import _handle_email_tool
    return await _handle_email_tool(
        "reply_email",
        request.context.agent_id,
        request.context.workspace,
        request.arguments,
    )

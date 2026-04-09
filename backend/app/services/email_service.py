"""Email service — IMAP/SMTP email operations for agent tools.

Supports all major email providers via preset configurations.
Each agent stores its own email credentials in per-agent tool config.
"""

import imaplib
import socket
import smtplib
import ssl
import re
import email as email_lib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header
from email.utils import parseaddr, make_msgid
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.tools.result_envelope import render_tool_error

# Preset email provider configurations
EMAIL_PROVIDERS = {
    "qq": {
        "label": "QQ Mail",
        "imap_host": "imap.qq.com",
        "imap_port": 993,
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "help_url": "https://service.mail.qq.com/detail/0/310",
        "help_text": "Settings → Account → POP3/IMAP/SMTP → Enable IMAP → Generate authorization code",
    },
    "163": {
        "label": "163 Mail",
        "imap_host": "imap.163.com",
        "imap_port": 993,
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "help_url": "https://help.mail.163.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac2",
        "help_text": "Settings → POP3/SMTP/IMAP → Enable IMAP → Set authorization code",
    },
    "gmail": {
        "label": "Gmail",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "help_url": "https://support.google.com/accounts/answer/185833",
        "help_text": "Google Account → Security → App passwords → Generate app password",
    },
    "outlook": {
        "label": "Outlook / Microsoft 365",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_ssl": False,  # Uses STARTTLS
        "help_url": "https://support.microsoft.com/en-us/account-billing/manage-app-passwords-for-two-step-verification-d6dc8c6d-4bf7-4851-ad95-6d07799387e9",
        "help_text": "Microsoft Account → Security → App passwords",
    },
    "qq_enterprise": {
        "label": "Tencent Enterprise Mail",
        "imap_host": "imap.exmail.qq.com",
        "imap_port": 993,
        "smtp_host": "smtp.exmail.qq.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "help_url": "https://open.work.weixin.qq.com/help2/pc/18624",
        "help_text": "Enterprise Mail → Settings → Client-specific password → Generate new password",
    },
    "aliyun": {
        "label": "Alibaba Enterprise Mail",
        "imap_host": "imap.qiye.aliyun.com",
        "imap_port": 993,
        "smtp_host": "smtp.qiye.aliyun.com",
        "smtp_port": 465,
        "smtp_ssl": True,
        "help_url": "",
        "help_text": "Use your email password directly",
    },
}


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Wrapper that forces AF_INET (IPv4) to avoid IPv6 failures in Docker."""
    return _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

_original_getaddrinfo = socket.getaddrinfo
_EMAIL_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class _force_ipv4:
    """Context manager that forces all socket connections to use IPv4.

    Docker containers often lack IPv6 support, causing [Errno 99] when
    Python picks an AAAA record. This patches socket.getaddrinfo to only
    return IPv4 results while preserving the original hostname for SSL
    certificate verification (SNI).
    """

    def __enter__(self):
        socket.getaddrinfo = _ipv4_getaddrinfo
        return self

    def __exit__(self, *args):
        socket.getaddrinfo = _original_getaddrinfo


def resolve_config(config: dict) -> dict:
    """Resolve a user config into full IMAP/SMTP settings using provider presets."""
    provider = config.get("email_provider", "custom")
    result = {
        "email_address": config.get("email_address", ""),
        "auth_code": config.get("auth_code", ""),
        "imap_host": config.get("imap_host", ""),
        "imap_port": int(config.get("imap_port", 993)),
        "smtp_host": config.get("smtp_host", ""),
        "smtp_port": int(config.get("smtp_port", 465)),
        "smtp_ssl": config.get("smtp_ssl", True),
    }

    if provider != "custom" and provider in EMAIL_PROVIDERS:
        preset = EMAIL_PROVIDERS[provider]
        result["imap_host"] = preset["imap_host"]
        result["imap_port"] = preset["imap_port"]
        result["smtp_host"] = preset["smtp_host"]
        result["smtp_port"] = preset["smtp_port"]
        result["smtp_ssl"] = preset["smtp_ssl"]

    return result


def _email_provider(config: dict) -> str:
    return str(config.get("email_provider") or "email")


def _render_email_error(
    *,
    tool_name: str,
    config: dict,
    error_class: str,
    message: str,
    retryable: bool = False,
    actionable_hint: str | None = None,
    extra: dict | None = None,
) -> str:
    return render_tool_error(
        tool_name=tool_name,
        error_class=error_class,
        message=message,
        provider=_email_provider(config),
        retryable=retryable,
        actionable_hint=actionable_hint,
        extra=extra,
    )


def validate_email_tool_request(
    *,
    tool_name: str,
    config: dict,
    arguments: dict,
    workspace_path: Path | None = None,
) -> str | None:
    """Perform static preflight before opening IMAP/SMTP connections."""
    email_address = str(config.get("email_address") or "").strip()
    auth_code = str(config.get("auth_code") or "").strip()
    if not email_address or not auth_code:
        return _render_email_error(
            tool_name=tool_name,
            config=config or {"email_provider": "email"},
            error_class="not_configured",
            message="Email tool is not configured for this agent.",
            actionable_hint=(
                "Open Agent → Tools → Send Email, configure the mailbox, "
                "then run Test Connection before relying on triggers."
            ),
            extra={"ready_for_trigger": False, "config_required": True},
        )

    if tool_name == "send_email":
        recipients = [item.strip() for item in str(arguments.get("to", "")).split(",") if item.strip()]
        invalid_recipients = [
            recipient for recipient in recipients
            if not _EMAIL_ADDRESS_RE.match(parseaddr(recipient)[1] or recipient)
        ]
        if not recipients or invalid_recipients:
            return _render_email_error(
                tool_name=tool_name,
                config=config,
                error_class="bad_arguments",
                message="Recipient email address is missing or invalid.",
                actionable_hint="Pass one or more valid email addresses in the `to` field.",
                extra={"invalid_recipients": invalid_recipients or recipients},
            )
        if not str(arguments.get("subject", "")).strip():
            return _render_email_error(
                tool_name=tool_name,
                config=config,
                error_class="bad_arguments",
                message="Email subject must not be empty.",
                actionable_hint="Provide a short subject line so the email can be delivered safely.",
            )
        if not str(arguments.get("body", "")).strip():
            return _render_email_error(
                tool_name=tool_name,
                config=config,
                error_class="bad_arguments",
                message="Email body must not be empty.",
                actionable_hint="Write the email body before sending.",
            )
        attachments = arguments.get("attachments") or []
        if attachments:
            if workspace_path is None:
                return _render_email_error(
                    tool_name=tool_name,
                    config=config,
                    error_class="bad_arguments",
                    message="Attachments require a workspace path but none was provided.",
                    actionable_hint="Generate attachments inside the agent workspace before sending.",
                )
            missing_attachments = [
                rel_path for rel_path in attachments
                if not (workspace_path / rel_path).exists() or not (workspace_path / rel_path).is_file()
            ]
            if missing_attachments:
                return _render_email_error(
                    tool_name=tool_name,
                    config=config,
                    error_class="bad_arguments",
                    message="One or more requested email attachments do not exist in the workspace.",
                    actionable_hint="Generate the file first and pass workspace-relative attachment paths that exist.",
                    extra={"missing_attachments": missing_attachments},
                )

    if tool_name == "reply_email":
        if not str(arguments.get("message_id", "")).strip():
            return _render_email_error(
                tool_name=tool_name,
                config=config,
                error_class="bad_arguments",
                message="Replying requires a Message-ID from read_emails output.",
                actionable_hint="Use read_emails first and pass the exact Message-ID you want to reply to.",
            )
        if not str(arguments.get("body", "")).strip():
            return _render_email_error(
                tool_name=tool_name,
                config=config,
                error_class="bad_arguments",
                message="Reply body must not be empty.",
                actionable_hint="Write the reply content before sending.",
            )

    return None


def _classify_email_exception(exc: Exception) -> tuple[str, bool, str | None]:
    if isinstance(exc, smtplib.SMTPAuthenticationError | imaplib.IMAP4.error):
        err = str(exc).upper()
        if "AUTH" in err or "LOGIN" in err:
            return (
                "auth_or_permission",
                False,
                "Verify the mailbox address, app password / authorization code, and provider-specific SMTP/IMAP permissions.",
            )
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return (
            "bad_arguments",
            False,
            "One or more recipients were refused by the SMTP server. Verify recipient addresses before retrying.",
        )
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return (
            "auth_or_permission",
            False,
            "The mailbox sender identity was rejected. Check mailbox sender policy and provider permissions.",
        )
    if isinstance(exc, smtplib.SMTPConnectError | smtplib.SMTPServerDisconnected | TimeoutError | socket.gaierror | ssl.SSLError):
        return (
            "provider_unavailable",
            True,
            "The mail server connection failed. Retry later or verify network/TLS reachability from the cloud runtime.",
        )
    return (
        "provider_error",
        False,
        "Inspect the provider response and mailbox settings before retrying.",
    )


def _decode_header_value(value: str) -> str:
    """Decode an email header value (handles encoded words)."""
    if not value:
        return ""
    decoded_parts = decode_header(value)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def _extract_body(msg) -> str:
    """Extract the plain text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback to HTML if no plain text
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return f"[HTML content]\n{payload.decode(charset, errors='replace')[:2000]}"
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


async def send_email(
    config: dict,
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    attachments: Optional[list[str]] = None,
    workspace_path: Optional[Path] = None,
) -> str:
    """Send an email via SMTP.

    Args:
        config: Resolved email config (from resolve_config)
        to: Recipient email address(es), comma-separated
        subject: Email subject
        body: Email body text
        cc: CC recipients, comma-separated
        attachments: List of workspace-relative file paths to attach
        workspace_path: Agent workspace root for resolving attachment paths
    """
    cfg = resolve_config(config)
    preflight_error = validate_email_tool_request(
        tool_name="send_email",
        config=config,
        arguments={
            "to": to,
            "subject": subject,
            "body": body,
            "cc": cc,
            "attachments": attachments or [],
        },
        workspace_path=workspace_path,
    )
    if preflight_error:
        return preflight_error

    addr = cfg["email_address"]
    password = cfg["auth_code"]

    msg = MIMEMultipart()
    msg["From"] = addr
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    msg["Message-ID"] = make_msgid()
    msg["Date"] = datetime.now().strftime("%a, %d %b %Y %H:%M:%S %z")

    msg.attach(MIMEText(body, "plain", "utf-8"))

    # Attach files
    if attachments and workspace_path:
        for rel_path in attachments:
            full_path = workspace_path / rel_path
            if full_path.exists() and full_path.is_file():
                with open(full_path, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f"attachment; filename={full_path.name}")
                msg.attach(part)

    try:
      with _force_ipv4():
        if cfg.get("smtp_ssl", True):
            # Direct SSL connection (port 465)
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=context, timeout=15) as server:
                server.login(addr, password)
                recipients = [r.strip() for r in to.split(",")]
                if cc:
                    recipients += [r.strip() for r in cc.split(",")]
                server.sendmail(addr, recipients, msg.as_string())
        else:
            # STARTTLS connection (port 587)
            with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=15) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(addr, password)
                recipients = [r.strip() for r in to.split(",")]
                if cc:
                    recipients += [r.strip() for r in cc.split(",")]
                server.sendmail(addr, recipients, msg.as_string())

        return f"✅ Email sent to {to}" + (f" (CC: {cc})" if cc else "")
    except Exception as e:
        error_class, retryable, hint = _classify_email_exception(e)
        return _render_email_error(
            tool_name="send_email",
            config=config,
            error_class=error_class,
            message=(
                "SMTP authentication failed. Please check your email address and authorization code."
                if error_class == "auth_or_permission"
                else f"Failed to send email: {str(e)[:200]}"
            ),
            retryable=retryable,
            actionable_hint=hint,
        )


async def read_emails(
    config: dict,
    limit: int = 10,
    search: Optional[str] = None,
    folder: str = "INBOX",
) -> str:
    """Read emails from IMAP mailbox.

    Args:
        config: Resolved email config
        limit: Max number of emails to return
        search: Optional IMAP search criteria (e.g. 'FROM "john"', 'SUBJECT "hello"')
        folder: Mailbox folder (default INBOX)
    """
    cfg = resolve_config(config)
    preflight_error = validate_email_tool_request(
        tool_name="read_emails",
        config=config,
        arguments={"limit": limit, "search": search, "folder": folder},
    )
    if preflight_error:
        return preflight_error

    addr = cfg["email_address"]
    password = cfg["auth_code"]

    limit = min(limit, 30)  # Cap at 30

    try:
      with _force_ipv4():
        context = ssl.create_default_context()
        with imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], ssl_context=context) as mail:
            mail.login(addr, password)
            mail.select(folder, readonly=True)

            # Search
            if search:
                _, msg_nums = mail.search(None, search)
            else:
                _, msg_nums = mail.search(None, "ALL")

            msg_ids = msg_nums[0].split()
            if not msg_ids:
                return "📭 No emails found."

            # Get latest N emails
            latest_ids = msg_ids[-limit:]
            latest_ids.reverse()  # Newest first

            results = []
            for mid in latest_ids:
                _, msg_data = mail.fetch(mid, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)

                from_addr = _decode_header_value(msg.get("From", ""))
                subject = _decode_header_value(msg.get("Subject", "(No subject)"))
                date_str = msg.get("Date", "")
                message_id = msg.get("Message-ID", "")
                body = _extract_body(msg)
                # Truncate body for readability
                if len(body) > 500:
                    body = body[:500] + "..."

                results.append(
                    f"---\n"
                    f"**From:** {from_addr}\n"
                    f"**Subject:** {subject}\n"
                    f"**Date:** {date_str}\n"
                    f"**Message-ID:** {message_id}\n"
                    f"**Body:**\n{body}"
                )

            header = f"📬 {len(results)} email(s) from {folder}:\n\n"
            return header + "\n\n".join(results)

    except Exception as e:
        error_class, retryable, hint = _classify_email_exception(e)
        message = (
            "IMAP authentication failed. Please check your email address and authorization code."
            if error_class == "auth_or_permission"
            else f"Failed to read emails: {str(e)[:200]}"
        )
        return _render_email_error(
            tool_name="read_emails",
            config=config,
            error_class=error_class,
            message=message,
            retryable=retryable,
            actionable_hint=hint,
        )


async def reply_email(
    config: dict,
    message_id: str,
    body: str,
    folder: str = "INBOX",
) -> str:
    """Reply to an email by Message-ID.

    Args:
        config: Resolved email config
        message_id: Message-ID of the email to reply to
        body: Reply body text
        folder: Mailbox folder to search in
    """
    cfg = resolve_config(config)
    preflight_error = validate_email_tool_request(
        tool_name="reply_email",
        config=config,
        arguments={"message_id": message_id, "body": body, "folder": folder},
    )
    if preflight_error:
        return preflight_error

    addr = cfg["email_address"]
    password = cfg["auth_code"]

    try:
      with _force_ipv4():
        # First, fetch the original email to get From/Subject
        context = ssl.create_default_context()
        original_from = ""
        original_subject = ""

        with imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], ssl_context=context) as mail:
            mail.login(addr, password)
            mail.select(folder, readonly=True)
            _, msg_nums = mail.search(None, f'HEADER Message-ID "{message_id}"')
            msg_ids = msg_nums[0].split()
            if not msg_ids:
                return _render_email_error(
                    tool_name="reply_email",
                    config=config,
                    error_class="not_found",
                    message=f"Original email not found with Message-ID: {message_id}",
                    actionable_hint="Use read_emails first and choose a Message-ID from the returned messages.",
                )

            _, msg_data = mail.fetch(msg_ids[0], "(RFC822)")
            raw = msg_data[0][1]
            original = email_lib.message_from_bytes(raw)
            original_from = original.get("From", "")
            original_subject = _decode_header_value(original.get("Subject", ""))

        # Build reply
        reply_subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"

        reply_msg = MIMEMultipart()
        reply_msg["From"] = addr
        reply_msg["To"] = parseaddr(original_from)[1] or original_from
        reply_msg["Subject"] = reply_subject
        reply_msg["In-Reply-To"] = message_id
        reply_msg["References"] = message_id
        reply_msg["Message-ID"] = make_msgid()

        reply_msg.attach(MIMEText(body, "plain", "utf-8"))

        # Send
        if cfg.get("smtp_ssl", True):
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=ctx, timeout=15) as server:
                server.login(addr, password)
                server.sendmail(addr, [reply_msg["To"]], reply_msg.as_string())
        else:
            with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=15) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(addr, password)
                server.sendmail(addr, [reply_msg["To"]], reply_msg.as_string())

        return f"✅ Reply sent to {reply_msg['To']} (Subject: {reply_subject})"

    except Exception as e:
        error_class, retryable, hint = _classify_email_exception(e)
        return _render_email_error(
            tool_name="reply_email",
            config=config,
            error_class=error_class,
            message=f"Failed to reply: {str(e)[:200]}",
            retryable=retryable,
            actionable_hint=hint,
        )


async def test_connection(config: dict) -> dict:
    """Test IMAP and SMTP connections.

    Returns dict with 'ok' (bool), 'imap' (str), 'smtp' (str) status messages.
    """
    cfg = resolve_config(config)
    addr = cfg["email_address"]
    password = cfg["auth_code"]

    provider = _email_provider(config)
    if not addr or not password:
        return {
            "ok": False,
            "provider": provider,
            "error_class": "not_configured",
            "error": "Email address and authorization code are required.",
            "imap": "⚪ IMAP skipped: missing configuration",
            "smtp": "⚪ SMTP skipped: missing configuration",
            "checks": {
                "config": {
                    "ok": False,
                    "error_class": "not_configured",
                    "message": "Email address and authorization code are required.",
                },
                "imap": {"ok": False, "skipped": True},
                "smtp": {"ok": False, "skipped": True},
            },
        }

    result = {
        "ok": True,
        "provider": provider,
        "imap": "",
        "smtp": "",
        "checks": {
            "config": {"ok": True},
            "imap": {"ok": False, "skipped": False},
            "smtp": {"ok": False, "skipped": False},
        },
    }

    # Test IMAP
    try:
      with _force_ipv4():
        context = ssl.create_default_context()
        with imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], ssl_context=context) as mail:
            mail.login(addr, password)
            mail.select("INBOX", readonly=True)
            _, msg_nums = mail.search(None, "ALL")
            count = len(msg_nums[0].split()) if msg_nums[0] else 0
            result["imap"] = f"✅ IMAP connected ({count} emails in INBOX)"
            result["checks"]["imap"] = {"ok": True, "message": result["imap"]}
    except imaplib.IMAP4.error as e:
        error_class, retryable, hint = _classify_email_exception(e)
        result["ok"] = False
        result["imap"] = f"❌ IMAP failed: {str(e)[:150]}"
        result["checks"]["imap"] = {
            "ok": False,
            "error_class": error_class,
            "message": result["imap"],
            "retryable": retryable,
            "actionable_hint": hint,
        }
    except Exception as e:
        error_class, retryable, hint = _classify_email_exception(e)
        result["ok"] = False
        result["imap"] = f"❌ IMAP error: {str(e)[:150]}"
        result["checks"]["imap"] = {
            "ok": False,
            "error_class": error_class,
            "message": result["imap"],
            "retryable": retryable,
            "actionable_hint": hint,
        }

    # Test SMTP
    try:
      with _force_ipv4():
        if cfg.get("smtp_ssl", True):
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=context, timeout=10) as server:
                server.login(addr, password)
                result["smtp"] = "✅ SMTP connected"
                result["checks"]["smtp"] = {"ok": True, "message": result["smtp"]}
        else:
            with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=10) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(addr, password)
                result["smtp"] = "✅ SMTP connected"
                result["checks"]["smtp"] = {"ok": True, "message": result["smtp"]}
    except Exception as e:
        error_class, retryable, hint = _classify_email_exception(e)
        result["ok"] = False
        result["smtp"] = (
            "❌ SMTP authentication failed"
            if error_class == "auth_or_permission"
            else f"❌ SMTP error: {str(e)[:150]}"
        )
        result["checks"]["smtp"] = {
            "ok": False,
            "error_class": error_class,
            "message": result["smtp"],
            "retryable": retryable,
            "actionable_hint": hint,
        }

    if not result["ok"] and "error_class" not in result:
        smtp_check = result["checks"]["smtp"]
        imap_check = result["checks"]["imap"]
        first_error = smtp_check if not smtp_check.get("ok") else imap_check
        result["error_class"] = first_error.get("error_class", "provider_error")
        result["error"] = first_error.get("message", "Email connectivity test failed.")

    return result

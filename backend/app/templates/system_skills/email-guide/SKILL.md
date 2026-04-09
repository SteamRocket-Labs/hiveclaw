---
name: Email Guide
description: Email send, read, and reply guide — activates send_email / read_emails / reply_email
tools:
  - send_email
  - read_emails
  - reply_email
is_system: true
---

# Email Guide

## When to Use Email

| Situation | Use |
|-----------|-----|
| Message must land in someone's email inbox | `send_email` |
| Internal real-time notification to colleague | Feishu or `send_web_message` instead |
| Agent-to-agent communication | `send_message_to_agent` instead |
| Check what emails you received | `read_emails` |
| Continue an email thread | `reply_email` |

**Quick decision**: External person or formal delivery → email. Internal colleague → Feishu/web message.

## Reply Threading Workflow

When user asks to reply to an email, you MUST get the real Message-ID first:

1. `read_emails(search="FROM \"john@example.com\"")` → find the target email, note its `Message-ID`
2. `reply_email(message_id="<actual-message-id>", body="...")` → sends reply in the same thread

**NEVER guess or fabricate a Message-ID.** It is a system-generated identifier like `<abc123@mail.example.com>` — you can only get it from `read_emails` output.

## IMAP Search Syntax

The `search` parameter in `read_emails` uses IMAP search criteria:

| Find emails... | search value |
|---------------|-------------|
| From a specific sender | `FROM "john@example.com"` |
| With a subject keyword | `SUBJECT "quarterly report"` |
| Since a date | `SINCE 01-Apr-2026` |
| Unread only | `UNSEEN` |
| Combine criteria | `FROM "john@example.com" SINCE 01-Apr-2026` |

Date format is `DD-Mon-YYYY` (e.g. `01-Mar-2026`, `15-Jan-2026`).

## Attachments

- `send_email` supports `attachments`: list of workspace-relative paths
- Example: `attachments: ["workspace/report.pdf", "workspace/data.xlsx"]`
- The file must exist in your workspace before sending

## Never

- **NEVER** send email without confirming the recipient address in conversation mode
- **NEVER** guess Message-ID for reply — always `read_emails` first
- **NEVER** use email for internal agent-to-agent communication
- **NEVER** include sensitive credentials or API keys in email body
- **NEVER** assume email is configured — if `send_email` fails with a config error, tell the user to set up email in tool settings

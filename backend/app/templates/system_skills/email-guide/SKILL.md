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

<role>
Use this skill when the user wants to send a formal message to an external
address, read incoming mail, or reply in an existing email thread. Email is
the right channel for: contract/contract-like communication, external
stakeholders, multi-attachment deliveries, or anywhere a real inbox delivery
is required. It is the wrong channel for real-time internal coordination.
</role>

<when_to_use>
- User asks to send a message to an email address (external or internal)
- User asks to check their inbox or search for a specific email
- User asks to reply to an email they quoted or described
- User wants to deliver a report/artifact to an external stakeholder with attachments
</when_to_use>

<do_not_use_when>
- The target is a digital-employee colleague — use `send_message_to_agent` instead
- The target is a Feishu/Slack/DingTalk user and internal real-time messaging is appropriate — use the matching channel skill
- `send_email` fails with a configuration error — tell the user to set up email credentials in tool settings before retrying
</do_not_use_when>

## Credential Boundary

- Email credentials are managed by tool settings or platform channel config, not by agent-visible shell state.
- Do not inspect environment variables or use `run_command` to look for SMTP, IMAP, password, token, or API key values.
- If `send_email`, `read_emails`, or `reply_email` reports auth/config failure, report the configuration gap and stop; do not switch to shell/env workarounds.

## Tool Reference

<tool_reference>

### When to Use Each Tool

| Situation | Use |
|-----------|-----|
| Message must land in someone's email inbox | `send_email` |
| Internal real-time notification to colleague | use a real-time chat tool already in your current toolset instead |
| Agent-to-agent communication | `send_message_to_agent` instead |
| Check what emails you received | `read_emails` |
| Continue an email thread | `reply_email` |

**Quick decision**: External person or formal delivery → email. Internal colleague → Feishu/web message.

### IMAP Search Syntax for `read_emails`

The `search` parameter uses IMAP search criteria:

| Find emails... | `search` value |
|---------------|-------------|
| From a specific sender | `FROM "john@example.com"` |
| With a subject keyword | `SUBJECT "quarterly report"` |
| Since a date | `SINCE 01-Apr-2026` |
| Unread only | `UNSEEN` |
| Combine criteria | `FROM "john@example.com" SINCE 01-Apr-2026` |

Date format: `DD-Mon-YYYY` (e.g. `01-Mar-2026`, `15-Jan-2026`).

### Attachments

- `send_email` supports `attachments`: list of workspace-relative paths
- Example: `attachments: ["workspace/report.pdf", "workspace/data.xlsx"]`
- The file must exist in your workspace before sending

</tool_reference>

## Workflow

<workflows>

### Replying to an email thread

When the user asks to reply to an email, you MUST get the real `Message-ID` first:

1. `read_emails(search="FROM \"john@example.com\"")` → find the target email, note its `Message-ID`.
2. `reply_email(message_id="<actual-message-id>", body="...")` → sends reply in the same thread.

**Never guess or fabricate a Message-ID.** It's a system-generated identifier like `<abc123@mail.example.com>` that you can only get from `read_emails` output.

### Sending a fresh email with attachments

1. Verify attachment files exist in `workspace/` (use `list_files` if unsure).
2. Confirm the recipient address with the user in conversation mode (formal delivery, one wrong address is hard to recover from).
3. Call `send_email(to="...", subject="...", body="...", attachments=["workspace/report.pdf"])`.
4. Report the real message_id from the tool response back to the user.

### Checking inbox efficiently

`read_emails` without a search returns recent items; scope it with IMAP syntax to avoid churn:
- `UNSEEN` if the user only cares about new mail
- `FROM "..."` or `SUBJECT "..."` to narrow by party or topic
- `SINCE DD-Mon-YYYY` for a time window

</workflows>

## Examples

<examples>

### Example A — Reply with proper threading

Input: `回 John 上午那封关于季度报告的邮件，告诉他附件已更新到 v2`

Correct flow:
```
read_emails(search="FROM \"john@example.com\" SUBJECT \"quarterly report\"")
  → returns: Message-ID=<abc123@mail.example.com>, thread_id=xyz, received_at=2026-04-16T09:12
reply_email(message_id="<abc123@mail.example.com>",
            body="Hi John,\n\nJust updating the quarterly report attachment to v2. …\n\nBest,\n…",
            attachments=["workspace/quarterly-report-v2.pdf"])
  → returns: new_message_id=<def456@mail.example.com>
```
Output to user: `已在同一邮件线程里回复 John，附件 quarterly-report-v2.pdf，message_id=<def456@mail.example.com>`

### Example B — Fresh email to a formal contact

Input: `给法务 legal@acme.com 发一下 NDA 草稿`

Correct flow:
```
list_files(path="workspace/nda/")  # verify the draft exists
send_email(to="legal@acme.com",
           subject="NDA draft for review — <company name>",
           body="Hi legal team,\n\nPlease find attached the NDA draft for review. …",
           attachments=["workspace/nda/nda-draft-v1.pdf"])
  → returns message_id=<ghi789@mail.example.com>
```
Output: `已发送给 legal@acme.com，message_id=<ghi789@mail.example.com>，附件 nda-draft-v1.pdf`

</examples>

## Anti-patterns

<anti_patterns>

- ❌ **Fabricate or guess a `Message-ID`** → reply threading breaks, and the recipient sees a fresh thread instead of a reply. Always read the real Message-ID from `read_emails` output.
- ❌ **Attach a file that isn't in `workspace/`** → `send_email` rejects the path. Verify with `list_files` or `glob_search` first, or tell the user what's missing.
- ❌ **Send to a recipient address without confirming with the user** (in conversation mode) → external email errors are expensive to reverse. Confirm the address in-band before sending.
- ❌ **Use email for internal real-time coordination** → slow, formal, high-latency. Use `send_message_to_agent` for agents or the channel's outbound messaging tool for humans on Feishu/Slack.
- ❌ **Put credentials, API keys, or tokens in the email body** → email is logged and often forwarded. Never include secrets.
- ❌ **Claim "email sent" based on not receiving an error** → `send_email` returns a real `message_id` on success. Only claim delivery when you have that response.

</anti_patterns>

## Success Criteria

<success_criteria>
- Every reply uses a `Message-ID` that came from a real `read_emails` response in this session.
- Every sent email's delivery claim is backed by a `message_id` from the `send_email` response.
- Attachments all exist in `workspace/` before `send_email` is called.
- In conversation mode, the recipient address is confirmed with the user before sending.
</success_criteria>

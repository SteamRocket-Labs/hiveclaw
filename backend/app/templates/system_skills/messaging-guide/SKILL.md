---
name: Messaging Guide
description: Decision tree for delivering messages, files, and images to humans across channels — how to pick the right tool and avoid cross-channel mistakes.
tools:
  - send_web_message
  - send_channel_message
  - send_channel_file
  - upload_image
  - get_current_time
is_system: true
---

# Messaging Guide

<role>
Use this skill when you need to reach a **human** — send a text, deliver a
file, upload an image, or check the local time before scheduling. This is
the top-level router across your messaging tools. For **agent-to-agent**
collaboration (delegation, consults, async tasks) load the Delegation
Guide instead. For **Feishu-specific** integrations (docs, wiki, base,
tasks) load the Feishu Integration skill.
</role>

<when_to_use>
- You need to reply to a user on the channel where they reached you (Feishu / Telegram / WeCom / WeChat / web)
- You need to push a notification to a web-platform user even when they are not the current requester
- You produced a file (report, PDF, spreadsheet) and the human needs to receive it directly instead of checking the web workspace
- You have an image in your workspace and need to share its public URL
- You are about to schedule a trigger or write an absolute date and need the local current time
</when_to_use>

<do_not_use_when>
- The recipient is another agent — use Delegation Guide (`send_message_to_agent` / `delegate_to_agent`)
- The content is only a short inline reply to the current chat user — just emit the response text, don't call a tool
- You need Feishu-specific features (wiki, docs, base, tasks) — load Feishu Integration skill
- The user is offline, unreachable, or on a channel you don't have configured — don't guess, say "channel unavailable"
- You want to write to long-term memory — use Memory Guide (`save_memory`), not a message
</do_not_use_when>

## Decision Tree

<tool_reference>

### 1. Picking the right messenger

```
Is the recipient a human?
├── No → load Delegation Guide (agent-to-agent tools)
└── Yes
    │
    ├── Did the user message you via an external channel in this session?
    │   (Feishu IM, Telegram, WeCom, WeChat, Slack, Discord, Teams)
    │   └── Yes → send_channel_message  (reply to current requester)
    │
    ├── Do you need to message a specific user by name on Hive web?
    │   └── Yes → send_web_message(username=..., message=...)
    │
    ├── Do you need to reach a specific Feishu user who is NOT the current requester?
    │   └── Yes → load Feishu Integration skill → send_feishu_message
    │
    └── Are you composing purely inline in chat?
        └── Yes → just write the response text, no tool call
```

### 2. Sharing a file or image

| Deliverable | Tool | Channel selection |
|-------------|------|-------------------|
| A file from `workspace/` to current requester | `send_channel_file(file_path, message?)` | Auto — same channel user used to reach you |
| An image from `workspace/` that needs a public URL (embedded in a report, shared externally) | `upload_image(file_path)` → returns URL | CDN — URL persists |
| A public image from the web that needs caching | `upload_image(url=...)` | CDN |
| A Feishu-internal doc (not a file) | Feishu Integration → `feishu_doc_create` / `feishu_doc_append` | Feishu only |

### 3. Time awareness

- `get_current_time(timezone?)` — call this **before** writing absolute dates, scheduling cron triggers, or reasoning about "today" / "now". Your prompt's frozen prefix does not refresh, so without this tool you cannot know the wall-clock time.

### Tool Table

| Tool | What it does | Reach | When to use |
|------|--------------|-------|-------------|
| `send_channel_message` | Text reply to current requester on their active channel | Auto — requester only | Responding to the person who opened the session |
| `send_web_message` | Push a message to a named Hive web user | Web platform users | Notifying a human user who is not the current requester (e.g. coordinator finishing a task for someone else) |
| `send_channel_file` | Deliver a workspace file to current requester | Auto — requester only | Report / PDF / spreadsheet handoff |
| `upload_image` | Upload image → public URL | Anywhere | Need a shareable image link |
| `get_current_time` | Current wall-clock time in a timezone | N/A | Before absolute-date decisions |

Cross-reference for related tools:
- `send_feishu_message` (Feishu-direct, by name or user_id) → Feishu Integration
- `send_message_to_agent` / `delegate_to_agent` / `check_async_task` / `cancel_async_task` / `list_async_tasks` → Delegation Guide
- `set_trigger` (scheduling deliveries, waiting for replies) → Trigger Management Guide
- `save_memory` (persist user preferences, not for runtime messaging) → Memory Guide

</tool_reference>

## Workflows

<workflows>

### A — Reply on the channel the user reached you through

Context: User messaged you via Feishu, asked you to summarize a PDF.

```
1. read_document(path="workspace/report.pdf") → extract content
2. Compose summary (inline in response text)
3. IF the summary is short enough to read inline:
     → emit response text; the runtime delivers it through the active channel
   IF the summary should be saved as a file:
     → write_file(path="workspace/report-summary.md", content=...)
     → send_channel_file(file_path="workspace/report-summary.md",
                          message="Summary ready. See attached.")
```

Key rule: **prefer inline text for short replies, `send_channel_file` for long outputs**. Don't call `send_channel_message` after already emitting an inline response — you'll double-send.

### B — Notify a different web user

Context: coordinator agent finishes a research brief requested by user A but needs to notify user B (the project lead) that it's ready.

```
send_web_message(
  username="b_wang",
  message="Research brief '2026 Q2 competitor landscape' is ready at workspace/brief.md. Requested by a_chen. Reply here with any questions."
)
```

Key rule: `send_web_message` requires a **registered Hive platform username**. Get it from `relationships.md` or prior context — don't guess.

### C — Deliver a generated file

```
1. write_file(path="workspace/weekly-report-2026-04-16.md", content=report_md)
2. send_channel_file(
     file_path="workspace/weekly-report-2026-04-16.md",
     message="本周周报已完成，共涵盖 3 个项目进展与 2 个阻塞项。"
   )
```

### D — Share an image publicly

```
upload_image(file_path="workspace/charts/revenue-2026-q1.png",
             folder="/agents/reports/2026-q1")
→ returns { "url": "https://cdn.hive.io/agents/reports/2026-q1/revenue-2026-q1.png" }
→ embed that URL in a markdown report or an external message.
```

### E — Time-aware scheduling

Context: user says "帮我下周一早上 9 点提醒我发周报"。

```
1. get_current_time(timezone="Asia/Shanghai")
   → returns {"now": "2026-04-16T14:22:08+08:00", "weekday": "Wednesday"}
2. Compute next Monday: 2026-04-21 09:00:00 +08:00
3. set_trigger(type="once",
               config={"at": "2026-04-21T09:00:00+08:00"},
               reason="...")   # see Trigger Management Guide
```

Without step 1, you can be off by days if the session prompt was built hours or a day ago.

</workflows>

## Examples

<examples>

### Example A — Good: picking the right tool

User (on Feishu): "把刚才那份分析做成 markdown 发给我。"

```python
# ✅ Good
write_file(path="workspace/market-analysis.md", content=markdown)
send_channel_file(
  file_path="workspace/market-analysis.md",
  message="分析已整理为 markdown，附件见文件。"
)
```

### Example B — Bad: wrong channel + double-send

```python
# ❌ Bad
# user on Feishu, but agent uses send_web_message
send_web_message(username="li_ming", message="分析完成")
# then also emits inline response text — double delivery risk
# and send_web_message targets Hive web, not Feishu IM
```

Correct: emit inline response OR use `send_channel_message` (single path).

### Example C — Good: upload_image for embedding

```python
upload_image(file_path="workspace/figures/auth-flow.png",
             folder="/agents/diagrams")
# → "https://cdn.hive.io/agents/diagrams/auth-flow.png"
# Embed in report: ![auth flow](https://cdn.hive.io/agents/diagrams/auth-flow.png)
```

### Example D — Bad: hallucinating "now"

```python
# ❌ Bad
# user: "明天下午 3 点再跑一次"
set_trigger(type="once", config={"at": "2026-04-17T15:00:00+08:00"}, ...)
# assumed today is 2026-04-16 — but the session prompt was built yesterday;
# "tomorrow" is actually 2026-04-17 wrt user's current clock, not yours
```

Correct: `get_current_time` first, compute "tomorrow" from the tool result.

</examples>

## Anti-patterns

<anti_patterns>

- ❌ **Using `send_web_message` to reply to the current requester when they are on Feishu** → message goes to web chat history, requester never sees it. Use `send_channel_message` or inline response.
- ❌ **Calling `send_channel_message` after already emitting an inline response** → double delivery; user sees the same content twice.
- ❌ **Treating `send_channel_message` and `send_web_message` as interchangeable** → they target different transports. `send_channel_message` uses the captured reply target (auto-detected from session source). `send_web_message` requires an explicit `username` on the Hive web platform.
- ❌ **Uploading every image via `upload_image` before sending a channel file** → `send_channel_file` already delivers workspace files directly on most channels. Use `upload_image` only when you need a persistent URL (embedding, external sharing).
- ❌ **Assuming "today" without `get_current_time`** → your frozen prompt prefix does not refresh. For any absolute-time decision, check the wall clock first.
- ❌ **Delivering long outputs inline as chat text** → gets truncated on Feishu, trimmed on mobile, loses markdown. Use `send_channel_file` for anything over ~200 lines or with tables / code blocks.
- ❌ **Using messaging tools to "save" things for later** → messages are ephemeral. For persistence, `write_file` to workspace (and `save_memory` for cross-session facts via Memory Guide).

</anti_patterns>

## Success Criteria

<success_criteria>
- Every human-facing reply goes through exactly one delivery path (inline text OR `send_channel_message` OR `send_channel_file`), never two.
- `send_web_message` is only used when the recipient is a named Hive web user who is not the current requester.
- Absolute-date decisions are preceded by a `get_current_time` call.
- Long outputs (reports, multi-section analyses, anything with tables or code blocks) are delivered as workspace files via `send_channel_file`, not as inline chat text.
- `upload_image` is used when and only when a persistent public URL is required — not for every outbound image.
</success_criteria>

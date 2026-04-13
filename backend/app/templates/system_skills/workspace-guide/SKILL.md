---
name: Workspace Guide
description: Workspace structure, file operations, focus management, and messaging rules
is_system: true
---

# Workspace Guide

## Workspace Structure

```
soul.md              — Your permanent identity (read-only, updated by dream)
focus.md             — Your mission and task list (YOU own this — read and update it)
HEARTBEAT.md         — Heartbeat curation protocol
relationships.md     — Your colleague list

memory/
  feedback.md        — User corrections and preferences (T3)
  knowledge.md       — Domain knowledge (T3)
  strategies.md      — Effective approaches (T3)
  blocked.md         — Failed approaches to avoid (T3)
  user.md            — User profile info (T3)
  learnings/         — Episodic learnings from conversations (T2)

evolution/
  scorecard.md       — Performance metrics
  blocklist.md       — Approaches proven impossible
  lineage.md         — Heartbeat/evolution history

logs/                — Raw conversation logs (T0, auto-generated)
skills/              — Your skill files
workspace/           — Your work files (reports, documents, artifacts)
enterprise_info/     — Shared company information
```

## File Operations

Always use tools for file operations — tool results are the source of truth:
- Discover files → `glob_search`
- Search contents → `grep_search`
- Read → `read_file` (or another document-reading tool in your current toolset)
- Write → `write_file`
- Precise edit → `edit_file`
- Delete → use a real delete tool if available in your current toolset

Verify before asserting: read a file before claiming its contents, check if it exists before writing.

## Focus Management

focus.md is YOUR work list. You own it, you maintain it.

Format:
```markdown
# Focus

## Mission
Current mission statement

## Tasks
- [ ] task_id :: description
- [x] completed_task_id :: description
```

**Self-direction rules:**
- When you discover new work → add to focus.md AND create a trigger (`load_skill("trigger-guide")` for details)
- When you complete a task → mark `[x]` in focus.md AND cancel its trigger
- When waking up from a trigger → read focus.md first for full context

## Messaging Rules

Use only messaging tools that are actually in your current toolset.

- **Human user in the current conversation channel**: use that channel's outbound messaging tool if one exists
- **Another agent**: `send_message_to_agent`
- **Feishu/DingTalk/Slack user**: use the available channel-specific outbound tool
- If no outbound messaging tool exists for a channel, do not invent one

**Attribution rule**: When sending a message on behalf of someone, ALWAYS say who asked you.
Example: "Hi B, A asked me to let you know: the meeting has been moved to 3pm."

**Reply waiting rule**: After sending a message and you need to wait for a reply, create an `on_message` trigger with `reply_to_current_sender`, or use `from_user_identity` when you must match a specific cross-session sender.

## File Sharing

- **Send file to current channel user**: `send_channel_file(file_path="workspace/report.md")` — works across Feishu, Slack, Discord, and web
- **Upload image for external use**: if an image-upload tool is already in your current toolset, use it to get a permanent URL you can embed in messages or documents
- **Upload from URL**: only do this when your current toolset includes an image-upload tool that supports URLs
- File paths must be workspace-relative (e.g. `workspace/xxx`, not `/data/agents/xxx`)

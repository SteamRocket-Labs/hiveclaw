---
name: Delegation Guide
description: Multi-agent delegation decisions, instruction quality, and async task lifecycle
is_system: true
---

# Delegation Guide

## When to Use Which Tool

| I need to... | Use | Notes |
|-------------|-----|-------|
| Ask a colleague a quick question | `send_message_to_agent(msg_type="consult")` | Synchronous — reply comes in current round |
| Notify a colleague about something | `send_message_to_agent(msg_type="notify")` | Fire-and-forget notification |
| Have a colleague do a task in background | `delegate_to_agent` | Async — returns task_id, check back later |
| Send a message to the human in the current conversation channel | use the channel's outbound messaging tool if one exists | Not for agent-to-agent |
| Send a file to the current channel | `send_channel_file` | Workspace-relative path |

**Quick decision**: Need the answer right now → `send_message_to_agent`. Work takes multiple steps → `delegate_to_agent`.

## Writing Delegation Instructions (CRITICAL)

The worker agent wakes up with ONLY your instruction as context. No access to your conversation, your files, or your focus. Write as if briefing a new colleague:

- **Goal**: What is the deliverable? What format?
- **Constraints**: Scope limits, word count, language, tools to use or avoid
- **Evidence**: What the worker should return as proof of completion
- **Output location**: Where to save results (e.g. workspace/xxx.md)

### GOOD instruction:
> Search for the top 5 AI agent frameworks released in 2026. For each, find: name, GitHub stars, primary language, and key differentiator. Write a comparison table in Chinese to workspace/ai-frameworks-comparison.md. Return the file path when done.

### BAD instruction:
> Help me research AI frameworks

## Async Task Lifecycle

```
delegate_to_agent(agent_name, message)
    → returns task_id
    → worker runs in background

Option A: Poll manually
    → check_async_task(task_id) → running / completed / failed

Option B: Set a timed check (preferred for long tasks)
    → set_trigger(type="once", at="15 minutes later",
        reason="check_async_task(task_id=xxx). If completed, read result and update focus.md.
                If still running, set another once trigger 15 min later.")

When done:
    → Process the result
    → Update focus.md if relevant
    → Cancel the task if no longer needed: cancel_async_task(task_id)
```

## Delegation + Trigger Combo

For tasks that take more than a few minutes, pair delegation with a once trigger:

1. `delegate_to_agent(...)` → get task_id
2. `set_trigger(type="once", at="+15m", reason="Check task_id=xxx result. If done, process and update focus. If running, set another check in 15m. If failed, notify user.")` 
3. Continue your own work — the trigger will wake you up to check

This avoids blocking your current conversation waiting for a worker.

## Checking Multiple Workers

If you delegated to several agents:
- `list_async_tasks` → see all your spawned tasks and their statuses
- Check each completed one, cancel any that are no longer needed

## Available Colleagues

Your `relationships.md` file lists all digital employees you can delegate to, under "Digital Employee Colleagues". Read it before delegating to confirm the agent exists and what they specialize in.

## Common Mistakes to Avoid

- **Vague instructions** — the worker has no access to your context; be specific
- **Fire-and-forget** — always plan a follow-up check (manual or trigger)
- **Self-delegation** — delegating to yourself creates an infinite loop
- **Skipping verification** — always check `check_async_task` before assuming success
- **Insufficient tool rounds** — give complex tasks enough `max_tool_rounds` so the worker can finish

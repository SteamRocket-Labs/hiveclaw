---
name: Trigger Management Guide
description: Trigger creation, type selection, reason writing, and focus-trigger binding
tools:
  - set_trigger
  - update_trigger
  - cancel_trigger
  - list_triggers
is_system: true
---

# Trigger Management Guide

## Tools

- `set_trigger` — schedule future actions, wait for replies, receive external webhooks
- `update_trigger` — adjust parameters (e.g. change frequency, update reason)
- `cancel_trigger` — remove triggers when tasks are complete
- `list_triggers` — see your active triggers before creating new ones

## When to Use Which Type

| I need to... | Use | Config |
|-------------|-----|--------|
| Do something every day/week at a fixed time | `cron` | `{"expr": "0 9 * * *"}` |
| Do something once at a specific future time | `once` | `{"at": "2026-04-10T09:00:00+08:00"}` |
| Do something repeatedly every N minutes | `interval` | `{"minutes": 30}` |
| Act when a webpage/API changes | `poll` | `{"url": "...", "interval_min": 5, "fire_on": "change"}` |
| Act when a specific person replies | `on_message` | `{"from_agent_name": "Bob"}` or `{"from_user_name": "John"}` |
| Act when an external system sends data | `webhook` | `{"secret": "optional"}` (URL auto-generated) |

**Quick decision**: Repeating on schedule → cron. One-time follow-up → once. Waiting for someone → on_message. Monitoring external change → poll. Receiving external events → webhook.

## Writing `reason` (CRITICAL)

When a trigger fires, you wake up with NO memory of the current conversation. The `reason` is your ONLY context. Write it as a detailed instruction to your future self:

- **Goal**: What is the objective? Who requested it?
- **Action steps**: Exactly what to do (e.g. read focus.md, search web, send message)
- **Edge cases**: What if the person says "wait"? What if the task is already done?
- **Follow-up**: What triggers to create/cancel next?

### GOOD reason:
> Send a Feishu message to Qinrui reminding him to send the movie tickets (requested by Ray). Vary the tone each time.
> After sending, keep this interval trigger active. Also ensure wait_qinrui_reply on_message trigger is still listening.
> If Qinrui replies "wait X minutes" -> cancel this interval, set a once trigger X minutes later, re-create on_message.
> If Qinrui says done -> cancel all related triggers, notify Ray, update focus.md to mark task done.

### BAD reason:
> Remind Qinrui

## Focus-Trigger Binding

**Rule: focus without trigger is a wish. Focus with trigger is a plan.**

1. Before creating a task trigger, add the task to focus.md first
2. Set `focus_ref` to link the trigger to the focus item
3. When the task is done, update focus.md (`- [x] task`) AND cancel the trigger
4. When a trigger produces follow-up work, add it to focus.md AND create a new trigger

Format in focus.md:
```
## Tasks
- Task description (uncompleted)
- [x] Completed task
```

**Exception:** System-level triggers (heartbeat, webhooks for external services) do NOT need a focus item.

## Channel-Aware Delivery

When you create a trigger during a channel conversation (Feishu, Slack, etc.), the system automatically captures the reply channel context. When the trigger fires, your awakening context will include a "Reply Channel" and "Reply To" section telling you WHERE to deliver results.

**CRITICAL**: When you see Reply Channel in your awakening context, you MUST use the specified channel tool to deliver results. The user expects delivery in the channel where they gave the instruction — they will NOT check the web interface.

### GOOD reason (channel-aware):
> Search for AI startup funding news published today. Write a Chinese summary (300 words max) to workspace/ai-news-daily.md. Then send the summary to the requesting user via send_feishu_message. If no significant news found, send a brief "no updates today" message instead.

### BAD reason (no delivery):
> Search for AI startup funding news and save to workspace.

### When Reply Channel is missing
If your awakening context has no Reply Channel (e.g. trigger created via web), deliver results to the workspace and the user will find them there. Do NOT guess which channel to use.

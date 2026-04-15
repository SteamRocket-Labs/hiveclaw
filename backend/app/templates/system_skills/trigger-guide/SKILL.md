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

<role>
Use this skill whenever you need to schedule future action, wait for a
reply, or react to an external event. Triggers are how this agent
self-directs across time — they wake you up later with a `reason` as your
only context, and you execute from there. Write triggers like you're
briefing a future self who has no memory of this conversation.
</role>

<when_to_use>
- User asks to be reminded or followed up with at a specific time
- A task takes more than a few minutes and you want to free up the current conversation
- You want to wait for the same user to reply later in this thread
- You need to poll a URL/API for changes
- You want to receive external webhook events
- You need a recurring cron schedule
</when_to_use>

<do_not_use_when>
- The work can be completed in the current response — just do it
- You already have an active trigger covering the same task (use `list_triggers` to check before creating)
- The user explicitly says "no reminders needed"
</do_not_use_when>

## Tool Reference

<tool_reference>

### Tools

| Tool | Purpose |
|------|---------|
| `set_trigger` | Schedule future actions, wait for replies, receive external webhooks |
| `update_trigger` | Adjust parameters (e.g. change frequency, update reason) |
| `cancel_trigger` | Remove triggers when tasks are complete |
| `list_triggers` | See your active triggers before creating new ones |

### Trigger Type Selection

| I need to... | Use | Config |
|-------------|-----|--------|
| Do something every day/week at a fixed time | `cron` | `{"expr": "0 9 * * *"}` |
| Do something once at a specific future time | `once` | `{"at": "2026-04-10T09:00:00+08:00"}` |
| Do something repeatedly every N minutes | `interval` | `{"minutes": 30}` |
| Act when a webpage/API changes | `poll` | `{"url": "...", "interval_min": 5, "fire_on": "change"}` |
| Act when the same sender replies in the current thread | `on_message` | `{"reply_to_current_sender": true}` |
| Act when a specific external user replies | `on_message` | `{"from_user_identity": "telegram:123456:789"}` |
| Act when a specific agent replies | `on_message` | `{"from_agent_id": "<agent-uuid>"}` |
| Act when an external system sends data | `webhook` | `{"secret": "optional"}` (URL auto-generated) |

**Quick decision**: Repeating on schedule → `cron`. One-time follow-up → `once`. Waiting for someone → `on_message`. Monitoring external change → `poll`. Receiving external events → `webhook`.

</tool_reference>

## Writing the `reason` field

<workflows>

When a trigger fires, you wake up with NO memory of the current
conversation. The `reason` is your ONLY context. Write it as a detailed
instruction to your future self:

- **Goal**: What is the objective? Who requested it?
- **Action steps**: Exactly what to do (e.g. read focus.md, search web, send message)
- **Edge cases**: What if the person says "wait"? What if the task is already done?
- **Follow-up**: What triggers to create/cancel next?

### Focus-Trigger Binding

**Rule: focus without trigger is a wish. Focus with trigger is a plan.**

1. Before creating a task trigger, add the task to `focus.md` first
2. Set `focus_ref` to link the trigger to the focus item
3. When the task is done, update `focus.md` (`- [x] task_id :: task description`) AND `cancel_trigger`
4. When a trigger produces follow-up work, add it to `focus.md` AND create a new trigger

Format in `focus.md`:
```
## Tasks
- [ ] task_id :: description
- [x] completed_task_id :: description
```

**Exception**: System-level triggers (heartbeat, webhooks for external services) do NOT need a focus item.

### Channel-Aware Delivery

When you create a trigger during a channel conversation (Feishu, Slack,
etc.), the system automatically captures the reply channel context. When
the trigger fires, your awakening context will include a "Reply Channel"
and "Reply To" section telling you WHERE to deliver results.

**CRITICAL**: When you see Reply Channel in your awakening context, you
MUST use the specified channel tool to deliver results. The user expects
delivery in the channel where they gave the instruction — they will NOT
check the web interface.

**When Reply Channel is missing**: if your awakening context has no Reply
Channel (e.g. trigger created via web), deliver results to the workspace
and the user will find them there. Do NOT guess which channel to use.

</workflows>

## Examples

<examples>

### Example A — Good `reason` (detailed, recoverable)

Context: User (Ray) asked you to periodically remind colleague Qinrui to send movie tickets.

Call:
```
set_trigger(type="interval",
  config={"minutes": 30},
  reason="Send a Feishu message to Qinrui reminding him to send the movie tickets "
         "(requested by Ray). Vary the tone each time. "
         "After sending, keep this interval trigger active. Also ensure the "
         "wait_qinrui_reply on_message trigger is still listening. "
         "If Qinrui replies 'wait X minutes' -> cancel this interval, set a once "
         "trigger X minutes later, re-create the on_message trigger. "
         "If Qinrui says done -> cancel all related triggers, notify Ray, update "
         "focus.md to mark task done.")
```

### Example B — Bad `reason` (useless when fired)

```
set_trigger(type="interval",
  config={"minutes": 30},
  reason="Remind Qinrui")
```

When the trigger fires, future-you reads "Remind Qinrui" and has no idea
about the movie tickets, the requester, the stop condition, or what to do
if Qinrui responds. Write the full playbook.

### Example C — Channel-aware delivery

Input: `每天早上帮我扫一下 AI 融资新闻，摘要发给我`

Call:
```
# First add to focus.md
write_file(path="focus.md", content="<append>\n- [ ] ai_funding_daily :: Daily AI-funding news brief\n</append>")

set_trigger(type="cron",
  config={"expr": "0 9 * * *", "tz": "Asia/Shanghai"},
  focus_ref="ai_funding_daily",
  reason="Search for AI-startup funding news published in the last 24h. Write a Chinese "
         "summary (≤300 words) to workspace/ai-news-daily-YYYY-MM-DD.md. Then send the "
         "summary to the requesting user via send_feishu_message (Reply Channel in "
         "awakening context will tell you which user/chat). If no significant news found, "
         "send a brief '今日无重点更新' message instead.")
```

</examples>

## Anti-patterns

<anti_patterns>

- ❌ **Write a terse `reason`** like `"Remind Qinrui"` or `"Check task"` → when the trigger fires you have zero context. Always include Goal / Action steps / Edge cases / Follow-up.
- ❌ **Skip `list_triggers` before creating** → you may duplicate an existing trigger and create a double-remind loop.
- ❌ **Create a task trigger without a matching `focus.md` entry** (except heartbeat/webhook system triggers) → the task disappears when the trigger is cancelled; no record in `focus.md` means no audit trail.
- ❌ **Set `cron` expressions without a timezone** → fires in server UTC, drifts from user expectations. Always include `tz` (e.g. `"tz": "Asia/Shanghai"`) or convert to the user's locale explicitly.
- ❌ **Forget to `cancel_trigger` after task completion** → interval/cron keeps firing, user gets repeated useless messages. Always pair completion in `focus.md` with `cancel_trigger`.
- ❌ **Create a trigger that requires channel delivery without referencing the Reply Channel** → when it fires outside the channel you may deliver to the wrong place. Mention Reply Channel in the `reason` so future-you remembers.
- ❌ **Use `on_message` without scoping** (neither `reply_to_current_sender`, `from_user_identity`, nor `from_agent_id`) → fires on any message, causing noise.

</anti_patterns>

## Success Criteria

<success_criteria>
- Every trigger's `reason` contains Goal, Action steps, Edge cases, and Follow-up instructions.
- Every task trigger is preceded by a `focus.md` entry with a matching `focus_ref`.
- Completed tasks in `focus.md` always have their corresponding trigger cancelled.
- Scheduled cron triggers include an explicit timezone (`tz` field).
- `list_triggers` is consulted before creating new triggers to avoid duplicates.
</success_criteria>

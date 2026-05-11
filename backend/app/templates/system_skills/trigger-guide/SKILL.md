---
name: Trigger Management Guide
description: "Use when Codex needs to create, inspect, update, or cancel reminders and recurring triggers while separating wake policy from the underlying objective and completion evidence."
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
reply, or react to an external event. Objective Ledger is the source of truth.
Trigger is wake policy, not the goal itself, and focus.md is a readable projection.
Triggers wake you up later with a `reason` as your immediate context. Write
triggers like you're briefing a future self who has no memory of this conversation.
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
| Act when the same sender replies in the current thread | `on_message` | `{"reply_to_current_sender": true, "max_fires": 1}` |
| Act when a specific external user replies | `on_message` | `{"from_user_identity": "telegram:123456:789", "max_fires": 1}` |
| Act when a specific agent replies | `on_message` | `{"from_agent_id": "<agent-uuid>", "max_fires": 1}` |
| Act when an external system sends data | `webhook` | `{"secret": "optional", "max_fires": 1}` (URL auto-generated) |

**Quick decision**: Repeating on schedule → `cron`. One-time follow-up → `once`. Waiting for someone → `on_message`. Monitoring external change → `poll`. Receiving external events → `webhook`.

</tool_reference>

## Writing the `reason` field

<workflows>

When a trigger fires, you wake up with NO memory of the current
conversation. The `reason` is your ONLY context. Write it as a detailed
instruction to your future self:

- **Goal**: Which objective ledger row or standalone scheduled job is this? Who requested it?
- **Action steps**: Exactly what to do (e.g. list objectives, search web, send message)
- **Edge cases**: What if the person says "wait"? What if the task is already done?
- **Follow-up**: What triggers to create/cancel next?

### Objective-Wake Binding

**Rule: active objective without wake policy is a stalled plan. A trigger without an objective must be explicitly classified as `scheduled_job`, `event_wait`, or `system_maintenance`.**

1. Before creating an objective wake policy, create or confirm the objective with `propose_objective` / `list_objectives`.
2. Bind the trigger using the objective id when available. The focus ref remains a compatibility alias for old projection-key work.
3. When the task is done, call `complete_objective` with concrete evidence; cancel obsolete triggers.
4. When a trigger produces follow-up work, create a new objective candidate with `propose_objective`; active objectives get wake policies through the reconciler.

Legacy projection format in `focus.md`:
```
## Tasks
- [ ] task_id :: description
- [x] completed_task_id :: description
```

**Exception**: System-level triggers and standalone jobs do NOT need objective binding.
Use `trigger_class="scheduled_job"` for standalone recurring jobs that intentionally have no objective.
Use trigger_class="event_wait" for `on_message`, `webhook`, or `poll` waits; always include max_fires or expires_at.
Standalone scheduled jobs can declare context_from, model_id, toolset, excluded_tool_names, and workdir in config.

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
  trigger_class="objective_task",
  focus_ref="movie_ticket_reminder",
  reason="Send a Feishu message to Qinrui reminding him to send the movie tickets "
         "(requested by Ray). Vary the tone each time. "
         "After sending, keep this interval trigger active. Also ensure the "
         "wait_qinrui_reply on_message trigger is still listening. "
         "If Qinrui replies 'wait X minutes' -> cancel this interval, set a once "
         "trigger X minutes later, re-create the on_message trigger. "
         "If Qinrui says done -> cancel all related triggers, notify Ray, update "
         "complete_objective with evidence.")
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
# First create/confirm the objective
propose_objective(
  objective_key="ai_funding_daily",
  description="Daily AI-funding news brief",
  autonomy_class="explicit_user_request",
  risk_level="medium",
  wake_policy={"type": "cron", "config": {"expr": "0 9 * * *"}},
  evidence={"request": "daily brief requested by user"}
)

set_trigger(type="cron",
  config={
    "expr": "0 9 * * *",
    "tz": "Asia/Shanghai",
    "context_from": ["objective:ai_funding_daily"],
    "workdir": "reports/ai-funding"
  },
  trigger_class="objective_task",
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
- ❌ **Create an objective wake policy without a matching objective** → the work disappears when the trigger is cancelled; no objective record means no audit trail.
- ❌ **Set `cron` expressions without a timezone** → fires in server UTC, drifts from user expectations. Always include `tz` (e.g. `"tz": "Asia/Shanghai"`) or convert to the user's locale explicitly.
- ❌ **Forget to `complete_objective` and cancel obsolete triggers after task completion** → interval/cron keeps firing, user gets repeated useless messages.
- ❌ **Create a trigger that requires channel delivery without referencing the Reply Channel** → when it fires outside the channel you may deliver to the wrong place. Mention Reply Channel in the `reason` so future-you remembers.
- ❌ **Use `on_message` without scoping** (neither `reply_to_current_sender`, `from_user_identity`, nor `from_agent_id`) → fires on any message, causing noise.
- ❌ **Use event_wait without max_fires or expires_at** → waits forever and becomes stale operational noise.

</anti_patterns>

## Success Criteria

<success_criteria>
- Every trigger's `reason` contains Goal, Action steps, Edge cases, and Follow-up instructions.
- Every objective wake policy is preceded by an objective ledger row.
- Every objective wake policy uses `trigger_class="objective_task"` and binds the objective id when available.
- Completed objectives always have evidence and obsolete triggers cancelled.
- Scheduled cron triggers include an explicit timezone (`tz` field).
- Event waits include max_fires or expires_at.
- Standalone scheduled jobs use trigger_class="scheduled_job" and declare context_from when they depend on prior context.
- `list_triggers` is consulted before creating new triggers to avoid duplicates.
</success_criteria>

## Bundled Resources

Load resources by need, not by default:

- `references/trigger-design.md`: read only when this request needs its detailed rules, examples, or boundary notes.
- `templates/trigger-reason.md`: use as the output scaffold when creating this artifact type.

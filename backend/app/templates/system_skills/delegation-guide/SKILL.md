---
name: Delegation Guide
description: "Use when you need to delegate bounded work to another agent, monitor asynchronous tasks, decide what must remain local, and reconcile delegated results into the current task."
tools:
  - delegate_to_agent
  - send_message_to_agent
  - check_async_task
  - cancel_async_task
  - list_async_tasks
is_system: true
---


# Delegation Guide

<role>
Use this skill whenever you are about to hand work to another agent —
either synchronously (quick consult) or asynchronously (background task).
The skill covers three decisions: which tool to use, how to write the
instruction so the worker actually succeeds, and how to follow up so the
task doesn't fall off the radar.

A trigger is wake policy, not the goal itself. Classify follow-up triggers as
`scheduled_job` for standalone checks (or `event_wait` for `on_message` /
`poll` / `webhook` waits).
</role>

<when_to_use>
- You want to ask a colleague agent a quick question and wait for the answer
- You want to fire-and-forget a notification to another agent
- You want another agent to do a multi-step task in the background while you continue
- You already delegated and need to check status, wait for result, or cancel
- You need to send a file or a channel message to the human in the current conversation
</when_to_use>

<do_not_use_when>
- The work is entirely within your own capabilities and context — just do it
- You are inside a delegated worker session yourself (delegation tools are disabled for child sessions)
- The target is a human, not an agent — use channel messaging instead
</do_not_use_when>

## Tool Reference

<tool_reference>

### When to use which tool

| I need to... | Use | Notes |
|-------------|-----|-------|
| Ask a colleague a quick question | `send_message_to_agent` with `msg_type="consult"` | Synchronous — reply comes in current round |
| Notify a colleague about something | `send_message_to_agent` with `msg_type="notify"` | Fire-and-forget notification |
| Have a colleague do a task in background | `delegate_to_agent` | Async — returns task_id, check back later |
| Check progress of a delegated task | `check_async_task` | Returns running / completed / failed |
| See all your spawned tasks | `list_async_tasks` | Use before creating new ones to avoid runaway fan-out |
| Cancel a delegation | `cancel_async_task` | Use when the task is no longer needed |
| Schedule a follow-up check | `set_trigger` type=once with `config.at` | See Trigger Management Guide |
| Send a file to the current channel | `send_channel_file` | Workspace-relative path |

**Quick decision**: Need the answer right now → `send_message_to_agent`.
Work takes multiple steps → `delegate_to_agent`.

</tool_reference>

## Writing Delegation Instructions

<workflows>

The worker agent wakes up with ONLY your instruction as context. No access
to your conversation, your files, or your focus. Write as if briefing a new
colleague. The 4 load-bearing pieces:

- **Goal**: What is the deliverable? In what format?
- **Constraints**: Scope limits, word count, language, tools to use or avoid
- **Evidence**: What the worker should return as proof of completion
- **Output location**: Where to save results (e.g. `workspace/xxx.md`)

### Async Task Lifecycle

```
delegate_to_agent(agent_name, message)
    → returns task_id
    → worker runs in background

Option A: Poll manually
    → check_async_task(task_id) → running / completed / failed

Option B: Set a timed check (preferred for long tasks)
    → set_trigger(type="once", config={"at": "<ISO timestamp>"},
        trigger_class="scheduled_job",
        reason="check_async_task(task_id=xxx). If completed, read result and record the outcome with evidence in your work ledger.
                If still running, set another once trigger 15 min later.")

When done:
    → Process the result
    → Record the outcome with evidence in your work ledger
    → cancel_async_task(task_id) if no longer needed
```

### Delegation + Trigger Combo

For tasks that take more than a few minutes, pair delegation with a once trigger:

1. `delegate_to_agent(...)` → get task_id
2. `set_trigger(type="once", config={"at": "<ISO timestamp>"}, trigger_class="scheduled_job", reason="Check task_id=xxx result. If done, process and record the outcome with evidence in your work ledger. If running, set another check in 15m. If failed, notify user.")`
3. Continue your own work — the trigger will wake you up to check

This avoids blocking your current conversation waiting for a worker.

### Checking Multiple Workers

If you delegated to several agents:
- `list_async_tasks` → see all your spawned tasks and their statuses
- Check each completed one, cancel any that are no longer needed

### Available A2A Collaborators

The session's A2A Collaborators context lists governed callable agents.
Same-owner agents and public agents can be delegated to directly. Approved
cross-owner agents appear through active A2A Collaboration Group membership.
Check that context before delegating to confirm the agent exists, what they
specialize in, and whether the relationship is governed.

</workflows>

## Examples

<examples>

### Example A — Good delegation instruction

Input from user: `帮我让"投研助理"看一下最近三天的 AI Infra 融资动态`

Your delegation call:
```
delegate_to_agent(
  agent_name="投研助理",
  message="Goal: Summarize AI-infrastructure funding rounds from 2026-04-13 to 2026-04-15.\n\n"
          "Constraints: Chinese output, ≤400 words, 5 rounds max by deal size.\n\n"
          "For each round include: company, round, amount (USD), lead investor, source URL.\n\n"
          "Evidence: link every data point to a primary source (press release or official funding DB).\n\n"
          "Output: write to workspace/ai-infra-funding-2026-04-13-to-15.md and return the file path when done."
)
```

Then immediately compute an ISO timestamp for 15 minutes later and set the follow-up wake policy:
```
set_trigger(type="once", config={"at": "2026-04-15T10:15:00+08:00"},
  trigger_class="scheduled_job",
  reason="check_async_task(task_id=<returned>). If completed, read workspace/ai-infra-funding-2026-04-13-to-15.md,
          summarize top-3 for user and record the file path as evidence in your work ledger. If running, set another 15m check. If failed, notify user.")
```

### Example B — Quick consult

Input: `问问"法务小助手"这个合同模板里的违约条款合理吗`

Your call:
```
send_message_to_agent(
  agent_name="法务小助手",
  msg_type="consult",
  message="Quick question: please review the breach-of-contract clause in workspace/shared/nda-template.md — "
          "is it enforceable under Chinese contract law? Return: (a) verdict, (b) one-line reasoning, (c) any red flags."
)
```
You get the reply in the current round and relay it to the user.

</examples>

## Anti-patterns

<anti_patterns>

- ❌ **Vague instructions** like `帮我研究一下 AI 框架` → the worker has no access to your context, no deliverable shape, no success criteria. Worker will either return generic overview or ask clarifying questions the parent can't see. Always name Goal/Constraints/Evidence/Output.
- ❌ **Fire-and-forget delegation without any follow-up** → task runs in background and you forget it. Always pair `delegate_to_agent` with either a `set_trigger` once-check or a plan to `check_async_task` before the current conversation ends.
- ❌ **Self-delegation** (delegating to your own `agent_name`) → creates an infinite loop or gets rejected by governance. If no colleague fits, do the work yourself or ask the user to create a specialist.
- ❌ **Skipping `check_async_task` before claiming success** → you'd be asserting based on the task_id alone, not the actual result. The async task may have failed or timed out. Always verify.
- ❌ **Calling `delegate_to_agent` from inside a delegated worker session** → disabled by governance for child sessions. If you need to decompose further, return a Blocker to the parent and let them re-delegate.
- ❌ **Under-budgeting `max_tool_rounds`** for complex tasks → worker runs out of rounds before finishing. Default is usually fine; bump it explicitly if the task clearly needs research + write + verify.

</anti_patterns>

## Success Criteria

<success_criteria>
- Every `delegate_to_agent` call is paired with a follow-up mechanism (manual check or once trigger) before you move on.
- Every delegation instruction names Goal, Constraints, Evidence, Output location.
- You verify with `check_async_task` before claiming the delegated work is done.
- Work ledger is updated when delegated work produces follow-on tasks or completions.
- Board: use `track_todo` to record dispatched todos and pass the ledger_todo_id when calling `delegate_to_agent` or `spawn_subagent` to link the delegation back to the board entry.
</success_criteria>

## Bundled Resources

Load resources by need, not by default:

- `references/delegation-quality.md`: read only when this request needs its detailed rules, examples, or boundary notes.
- `templates/delegation-brief.md`: use as the output scaffold when creating this artifact type.

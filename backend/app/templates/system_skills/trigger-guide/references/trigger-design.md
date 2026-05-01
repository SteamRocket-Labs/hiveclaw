# Trigger Design Reference

Triggers are wake policies, not the source of truth for goals. Use objective
state or workspace artifacts for durable task state, and triggers only to wake
the agent later.

## Type Selection

| Need | Trigger type |
| --- | --- |
| One future action | `once` |
| Repeated fixed schedule | `cron` |
| Repeated interval | `interval` |
| Wait for a reply | `on_message` |
| Poll external URL/API | `poll` |
| Receive external event | `webhook` |

## Reason Quality

A good reason includes the requester, goal, exact action steps, success
criteria, and what to do if the task is obsolete.

## Guardrails

- Check for existing equivalent triggers first.
- Avoid duplicate recurring triggers.
- Cancel triggers when their objective is complete.
- Use absolute timestamps with timezone when scheduling.

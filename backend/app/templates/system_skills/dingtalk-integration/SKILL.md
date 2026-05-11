---
name: DingTalk Integration
description: "Use when Codex needs to handle DingTalk-triggered requests, schedule reminders from DingTalk context, explain outbound limitations, and preserve channel identity boundaries."
---

# DingTalk Channel Behavior

<role>
Use this skill when a user is messaging you from DingTalk and you need to
understand how the channel works. DingTalk is currently an **inbound-only
conversation bridge** — you reply in the current conversation and the
channel handler delivers it back. You do NOT have proactive DingTalk tools
to look up users or send outbound messages to DingTalk by ID.

Objective Ledger is the source of truth. Trigger is wake policy. focus.md is a readable projection.
Simple reminders in the current DingTalk thread are standalone `scheduled_job` wake policies.
</role>

<when_to_use>
- A user is currently talking to you through DingTalk and you are unsure how to reply
- You need to schedule a follow-up with that DingTalk user and need to know which trigger type to use
- You are tempted to "send a DingTalk message" to someone — this skill explains why you cannot
</when_to_use>

<do_not_use_when>
- The user is on Feishu/Slack/web — use the matching channel skill instead
- You need to send a proactive DingTalk message to a user outside the current thread — there is no tool for that; do not invent one
</do_not_use_when>

## Credential Boundary

- DingTalk channel credentials and reply targets are managed by the platform channel config and runtime handler.
- Do not inspect environment variables or use `run_command` to look for DingTalk app, secret, token, webhook, or robot credentials.
- If DingTalk delivery or trigger wakeup reports auth/config failure, report the configuration gap and stop; do not invent outbound tools or shell/env workarounds.

## Tool Reference

<tool_reference>

DingTalk has no dedicated outbound tools in the current runtime. The
relevant tools you WILL use come from other skill packs:

| Task | Tool (from other skills) |
|------|-------------------------|
| Reply in the current DingTalk conversation | (automatic — your normal assistant reply is sent back by the channel handler) |
| Schedule a follow-up later in this conversation | `set_trigger` (from Trigger Management Guide) |
| Wait for the user's next DingTalk message | `set_trigger` with `type="on_message"`, `reply_to_current_sender: true`, `trigger_class="event_wait"`, and `max_fires: 1` |
| List your active triggers | `list_triggers` |
| Contact another digital employee instead | `send_message_to_agent` |

</tool_reference>

## Workflow

<workflows>

### 1. Replying in the current DingTalk conversation
Just reply normally. No tool call needed — the channel handler forwards your assistant message automatically.

### 2. Setting up a follow-up
User asks: "跟我一小时后再提醒一下"
- Get current time, compute the exact ISO timestamp, then call `set_trigger(type="once", config={"at": "<ISO timestamp>"}, trigger_class="scheduled_job", reason="Remind the user about <topic>. Reply in the current DingTalk thread when triggered.")`
- Trust the awakening context's Reply Channel to send the result back through DingTalk.

### 3. Needing to contact someone NOT in this conversation
If the user asks you to "DM someone else on DingTalk" — you cannot do it directly (no outbound tool). Options:
1. Ask the user to introduce or forward the message themselves.
2. If the target is a digital-employee colleague, use `send_message_to_agent`.
3. If the target has an email, use the Email Guide skill.

</workflows>

## Examples

<examples>

### Example A — Scheduled reminder in DingTalk
Input: `帮我 30 分钟后提醒一下开会`
Action: after checking current time, `set_trigger(type="once", config={"at": "<ISO timestamp 30 minutes later>"}, trigger_class="scheduled_job", reason="Remind the user about the meeting. Reply in the current DingTalk thread when triggered.")`
Output to user: `好的，已设置 30 分钟后的提醒，我会在这个 DingTalk 对话里通知你。`

### Example B — User asks to message a third party
Input: `帮我 DingTalk 一下 Alice 说会议改到下周`
Correct response: `我在当前 DingTalk 对话里无法主动发消息给 Alice —— 这个通道没有外发工具。你可以 (1) 自己转发一下，(2) 如果 Alice 是数字员工我可以用 send_message_to_agent 发，或 (3) 如果她有邮箱我可以用邮件发送。`

</examples>

## Anti-patterns

<anti_patterns>
- ❌ **Invent a `dingtalk_send_message` tool or any `dingtalk_*` outbound tool** → it does not exist; the call will fail. Use the escalation options in Example B instead.
- ❌ **Fabricate DingTalk user IDs, mobile numbers, or DingTalk IDs** → there is no way to verify them, and any downstream tool relying on them will break. Only use IDs that appear in tool responses.
- ❌ **Claim a message was "sent to DingTalk" when you only wrote text in the reply** → your reply is auto-forwarded, but that's not "sending outbound". Describe it as "replying in the current thread".
- ❌ **Use `send_channel_file` and assume it will reach a specific DingTalk user outside the thread** → it delivers to the current channel, not arbitrary targets.
</anti_patterns>

## Success Criteria

<success_criteria>
- You only claim delivery when the channel handler has processed your reply (i.e. you replied in the current conversation).
- You never fabricate DingTalk identifiers or tool names.
- Scheduled follow-ups in DingTalk threads use `set_trigger` with `trigger_class="scheduled_job"` and a reason that describes the target channel explicitly.
</success_criteria>

## Bundled Resources

Load resources by need, not by default:

- `references/channel-boundary.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/follow-up-reason.md`: use as the output scaffold when creating this artifact type.

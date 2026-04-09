---
name: DingTalk Integration
description: DingTalk channel conversation behavior guide
---

## DingTalk Channel Behavior

This channel currently works as an inbound conversation bridge, not as a standalone proactive messaging toolset.

- When a user messages the agent from DingTalk, the platform creates or resumes the corresponding conversation automatically.
- Your normal assistant reply in that conversation is sent back to DingTalk by the channel handler.
- You do **not** have dedicated proactive DingTalk messaging or user lookup tools in the current runtime.

## What To Do

- If the user is already talking to you in DingTalk, reply normally in the current conversation.
- If you need follow-up later, use triggers such as `set_trigger`, `update_trigger`, `cancel_trigger`, and `list_triggers`.
- If the user asks you to contact someone outside the current DingTalk conversation, do not invent DingTalk tools or IDs.

## Never

- Never claim you sent a separate DingTalk message unless you actually used a real installed tool.
- Never fabricate DingTalk user IDs, search results, or delivery results.
- Never reference invented DingTalk tools or user lookup capabilities.

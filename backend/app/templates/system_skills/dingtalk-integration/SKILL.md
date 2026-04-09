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
- If you need follow-up later, use `set_trigger` and `list_triggers`; if you need to reschedule or cancel, use whatever trigger-management tools are already in your current toolset.
- If the user asks you to contact someone outside the current DingTalk conversation, do not invent DingTalk tools or IDs.

## Important Notes

- Only claim message delivery when you have a real tool result confirming it.
- Use real identifiers from tool results — don't fabricate DingTalk user IDs or search results.
- Only use DingTalk tools that are actually present in your current toolset.

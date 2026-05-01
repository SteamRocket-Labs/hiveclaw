# DingTalk Channel Boundary

DingTalk currently behaves as an inbound conversation bridge. The assistant
replies in the active conversation; there is no general outbound DingTalk
address book tool in this skill.

## Supported

- Reply normally in the current DingTalk thread.
- Schedule a follow-up trigger that replies to the current thread.
- Wait for a message from the same sender using an `on_message` trigger.

## Unsupported

- Proactive DM to arbitrary DingTalk users.
- Searching DingTalk users by name.
- Using webhook URLs or credentials discovered from environment variables.

## Escalation

If the user needs outbound delivery to someone else, use an available channel
with configured tools, such as email or Feishu, or ask the user to introduce
the target in the current thread.

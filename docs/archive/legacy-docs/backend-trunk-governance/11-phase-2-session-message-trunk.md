# 11 Session Message Trunk

## Current Contract

All runtime entries must preserve a session identity:

```text
web/channel -> ChatSession + SessionContext
A2A         -> agent session + interaction metadata
heartbeat   -> persistent agent heartbeat SessionContext
trigger     -> source_channel=trigger and objective external_conv_id when objective-bound
task         -> source_channel=task
```

Agent-pair conversations must use `app.services.agent_pair_session`:

```text
find_or_create_agent_pair_session -> one durable ChatSession per pair
session_conversation_id           -> ChatMessage.conversation_id
get_or_create_agent_participant_id -> ChatMessage.participant_id
```

Legacy repair scripts:

```text
app.db_legacy_gateway_conversation_migration -> gw_agent_* transcript promotion
app.db_legacy_feishu_session_migration       -> Feishu open_id session rekey to user_id
```

## Rules

- Channel code creates or finds channel sessions, then invokes the shared runtime.
- Channel code must not implement private LLM loops.
- Internal sessions (`trigger`, `task`, `heartbeat`) must stay out of normal human chat recall unless explicitly requested.
- Objective task sessions must be stable across fires.
- Gateway, A2A tool messaging, and supervision reminders must not create private agent-pair session logic.
- Activity chat history must expose canonical `ChatSession.id`; legacy prefixed `conversation_id` is fallback only.
- Teams channel sessions use canonical `microsoft_teams`; legacy `teams` must not appear in chat-session filters.

## Guard Tests

```bash
pytest tests/architecture/test_phase0r_boundaries.py \
       tests/architecture/test_channel_message_contract.py \
       tests/services/test_channel_session.py \
       tests/services/test_web_session_contract.py \
       tests/services/test_session_recall.py \
       tests/api/test_activity_chat_history_sessions.py \
       tests/api/test_gateway_agent_transcript.py \
       tests/api/test_chat_sessions_permissions.py \
       tests/test_db_legacy_gateway_conversation_migration.py \
       tests/test_db_legacy_feishu_session_migration.py
```

## Future Harness H6 Work

- Make channel delivery result write artifacts, not only chat messages.

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

## Rules

- Channel code creates or finds channel sessions, then invokes the shared runtime.
- Channel code must not implement private LLM loops.
- Internal sessions (`trigger`, `task`, `heartbeat`) must stay out of normal human chat recall unless explicitly requested.
- Objective task sessions must be stable across fires.

## Guard Tests

```bash
pytest tests/architecture/test_phase0r_boundaries.py \
       tests/services/test_channel_session.py \
       tests/services/test_web_session_contract.py \
       tests/services/test_session_recall.py
```

## Future Harness H6 Work

- Introduce a canonical SessionKey helper for web/channel/A2A/trigger/task.
- Make channel delivery result write artifacts, not only chat messages.

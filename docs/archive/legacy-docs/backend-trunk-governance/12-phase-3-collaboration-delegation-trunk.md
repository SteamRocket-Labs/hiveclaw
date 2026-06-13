# 12 Collaboration Delegation Trunk

## Current Contract

Collaboration and delegation are separate runtime intents:

```text
agent_message = peer communication
delegation    = worker task execution
```

## Rules

- Delegation must write `RuntimeTask`.
- A2A messages must not be logged as generic user chat when interaction type is known.
- Prompt templates must preserve the distinction between worker mode and peer message mode.
- Recovery must use persisted runtime task metadata where a task exists.

## Guard Tests

```bash
pytest tests/agents/test_orchestrator.py \
       tests/services/test_agent_message_runtime.py \
       tests/services/test_runtime_task_service.py \
       tests/runtime/test_task_eval.py
```

## Future Work

- A unified interaction trace id across A2A, delegation, and artifacts.
- Evaluator hooks for delegated work quality in Harness H5.

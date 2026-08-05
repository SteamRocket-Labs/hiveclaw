# 13 Prompt Memory Trunk

## Current Contract

Prompt construction is split across:

```text
runtime.invoker
runtime.prompt_builder
services.memory_service
services.agent_context
```

## Rules

- Memory is not an objective queue.
- Context injection must identify its source.
- Prompt compaction must preserve references to files, tools, skills, and pending items.
- Future memory/provider refactors must land behind tests before replacing current behavior.

## Guard Tests

```bash
pytest tests/runtime/test_prompt_builder.py \
       tests/runtime/test_memory_section.py \
       tests/services/test_memory_service.py \
       tests/memory
```

## Future Harness H3 Work

- Extract `ContextEngine` protocol.
- Extract `MemoryProvider` protocol.
- Turn compaction summaries into traceable artifacts.
- Fence memory sources by provider and confidence.

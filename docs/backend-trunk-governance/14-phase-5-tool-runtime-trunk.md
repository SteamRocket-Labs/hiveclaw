# 14 Tool Runtime Trunk

## Current Contract

```text
normal tool call
-> ToolRuntimeService.execute
-> governance
-> registry / fallback

approved tool call
-> execute_approved_tool
-> ToolRuntimeService.execute_approved
-> registry / unknown-tool fallback
```

## Rules

- `approval_service` must not import private tool execution functions.
- Approved execution must include approval metadata in activity logs.
- `governance.py` must use the canonical `request_approval` dependency signature.
- Direct fallback must not duplicate first-class tool dispatch.
- Unknown tools may fall through to MCP passthrough.

## Guard Tests

```bash
pytest tests/architecture/test_phase0r_boundaries.py \
       tests/architecture/test_tool_runtime_single_entry.py \
       tests/tools/test_service.py \
       tests/tools/test_governance.py
```

## Future Harness H2 Work

- Introduce `ToolRuntimeBackend` for local/docker/remote execution.
- Add skill guard scanning before tool surface activation.
- Move compatibility facade logic out of `app.services.agent_tools` after tests lock the new surface.

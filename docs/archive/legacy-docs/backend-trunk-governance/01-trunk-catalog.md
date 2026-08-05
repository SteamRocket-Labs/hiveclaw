# 01 Trunk Catalog

## Runtime Trunk

```text
app.runtime.invoker.invoke_agent
-> app.kernel.engine.AgentKernel
-> KernelDependencies callbacks
```

Rules:
- `AgentKernel` must not import DB, ORM models, API routers, or channel services.
- API/channel/task/trigger code must build `AgentInvocationRequest`; it must not run its own LLM loop.
- Session state must travel through `SessionContext`.

## Tool Trunk

```text
agent-facing entrypoint
-> ToolRuntimeService.execute / execute_approved / execute_with_context
-> ToolExecutionRegistry
-> handler/domain implementation
```

Rules:
- Governance runs before normal tool execution.
- Approved tool execution uses `execute_approved`, not private direct calls.
- Direct fallback must not become a second implementation of first-class tools.
- Tool calls must be auditable with agent, user, tenant, tool name, and approval metadata where relevant.

## Objective / Autonomy Trunk

```text
agent_objectives
-> wake policy / AgentTrigger
-> RuntimeTask attempt
-> output artifact / evaluator result
-> focus.md projection
```

Rules:
- `focus.md` is readable projection only.
- Objective-type triggers must bind to an objective id or stable focus key.
- Skipped runs still write attempt metadata with `skip_reason`.

## Session Trunk

```text
SessionContext
ChatSession.source_channel
ChatSession.external_conv_id
RuntimeTask trace metadata
```

Rules:
- Web/channel/A2A/trigger/heartbeat/task sessions must identify source and channel explicitly.
- Objective task triggers use stable objective sessions.
- Scheduled jobs may use isolated sessions and write artifacts.

## Memory / Context Trunk

```text
memory service
-> runtime prompt assembly
-> compaction / recall / persistence
```

Rules:
- Memory does not create business objectives.
- Objective intake does not treat memory markdown as a task queue.
- Context and memory injection need source boundaries and must be testable.

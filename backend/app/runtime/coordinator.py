"""Coordinator Mode — specialized orchestrator runtime for complex multi-agent tasks.

When enabled, the main agent becomes a dispatcher that:
1. Never executes tools directly (except delegation + messaging tools)
2. Decomposes tasks into subtasks for worker agents
3. Synthesizes worker results before directing follow-up work
4. Follows the "never delegate understanding" principle

Activation: set agent.execution_mode = "coordinator" in DB or
pass execution_mode="coordinator" in InvocationRequest.

The coordinator prompt is appended to the agent's system prompt
when coordinator mode is active.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Tools the coordinator is allowed to use directly
COORDINATOR_ALLOWED_TOOLS = frozenset({
    "delegate_to_agent",
    "cancel_async_task",
    "send_message_to_agent",
    "check_async_task",
    "list_async_tasks",
    "set_trigger",
    "list_triggers",
    "read_file",
    "write_file",
    "list_files",
    "get_current_time",
})

COORDINATOR_SYSTEM_PROMPT = """
## Coordinator Mode

You are operating in **coordinator mode**. Your role is to orchestrate work across worker agents, not to execute tools directly.

### Rules
1. **Decompose** user requests into independent subtasks
2. **Delegate** each subtask to a worker agent via `delegate_to_agent`
3. **Synthesize** worker results before directing follow-up work — never delegate understanding
4. **Verify** results through a separate verification worker, not the same one that implemented
5. **Report** consolidated results to the user

### Workflow
```
Research (parallel) → Synthesis (you) → Implementation (serial per file set) → Verification (fresh worker)
```

### Key Principles
- Read-only research tasks: run in parallel freely
- Write-heavy implementation: serialize per file set to prevent conflicts
- Continue vs. Spawn: use `check_async_task` to continue a worker that already has context; spawn fresh when context overlap is low
- Stop unneeded or runaway work with `cancel_async_task`
- Never say "based on your findings, fix it" — synthesize what was found, then give specific instructions
- Use `read_file`, `write_file`, and `list_files` only for coordination artifacts such as plans, synthesis notes, and tracking files — not for domain implementation work
- When workers are still running, give the user a status update instead of pretending the final result is ready

### What You Must NOT Do
- Do NOT call web_search, execute_code, or other domain tools directly
- Do NOT skip the synthesis step — always review worker output before next delegation
- Do NOT delegate vague tasks — be specific about files, functions, and expected outcomes

### Final User Report Format
- Status: what is done, running, or blocked
- Synthesis: your integrated understanding of worker results
- Next Actions: only what still needs follow-up
""".strip()


def is_coordinator_mode(agent: Any = None, request: Any = None) -> bool:
    """Check if coordinator mode is active for this agent/request."""
    if request and getattr(request, "execution_mode", None) == "coordinator":
        return True
    if agent and getattr(agent, "execution_mode", None) == "coordinator":
        return True
    return False


def get_coordinator_prompt() -> str:
    """Return the coordinator system prompt appendix."""
    return COORDINATOR_SYSTEM_PROMPT


def filter_tools_for_coordinator(tools: list[dict]) -> list[dict]:
    """Filter tool definitions to only coordinator-allowed tools.

    Tools not in the allowed set are removed from the LLM tool list,
    preventing the coordinator from calling them directly.
    """
    if not tools:
        return tools
    filtered = [
        tool for tool in tools
        if tool.get("function", {}).get("name", "") in COORDINATOR_ALLOWED_TOOLS
    ]
    logger.debug(
        "[Coordinator] Filtered tools: %d → %d (allowed: %s)",
        len(tools), len(filtered), sorted(COORDINATOR_ALLOWED_TOOLS),
    )
    return filtered

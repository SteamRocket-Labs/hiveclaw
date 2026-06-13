# 02 Dependency And Break Risk Map

## Highest Risk Dependencies

| Area | Risk | Failure Mode | Guardrail |
|---|---|---|---|
| Tool governance | Approval bypass | Tool runs without audit or policy | `test_tool_runtime_single_entry.py`, `test_permission_hardline.py` |
| Trigger/autonomy | Second objective source | focus/trigger diverges from DB objective | `test_phase0r_boundaries.py` |
| Session | Channel-specific session logic | Recall/history splits by channel | `test_session_context_contract.py` |
| Memory/context | Manual memory injection | Stale or untrusted context pollutes prompt | `test_context_memory_boundaries.py` |
| Feature merge | Whole-branch overwrite | Autonomy P0-P6 regresses | master regression plan |

## Current Known Pressure Points

- `app.services.agent_tools` is still a compatibility facade and contains tool surface/runtime wiring. It is allowed for now, but new code should prefer `ToolRuntimeService` boundaries and domain modules.
- `governance.py` must not keep compatibility wrappers that hide the approval call signature.
- `direct_fallback_executor` must stay limited to unknown/MCP passthrough behavior.
- Legacy scheduler/supervision modules may exist in the repository, but autonomous execution must stay on trigger/objective/runtime ledgers.

## Break Detection

Run before and after each governance change:

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/architecture tests/tools/test_governance.py tests/tools/test_service.py
ruff check app tests
```

# Backend Trunk Governance

> Current version: 2026-04-27, aligned with Autonomy P0-P6 and Architecture Phase 0R.

This directory is the executable governance manual for keeping Hive on one backend trunk. It supersedes the imported 2026-04-14 feature-branch notes where they conflict with the current codebase.

## Goal

Hive must stay on a small set of auditable trunks:

```text
Kernel / Invoker
ToolRuntime / Permission
Objective Ledger / Wake Policy / RuntimeTask
SessionContext / Channel Sessions
Memory / Context
Artifacts / Evaluation
```

No feature may create a second kernel, second tool executor, second objective source, second session identity contract, or second memory injection path.

## Reading Order

1. `01-trunk-catalog.md`
2. `02-dependency-and-break-risk-map.md`
3. `03-detection-and-evidence-playbook.md`
4. Phase documents `10-*` through `15-*`
5. `20-master-regression-plan.md`
6. `21-branch-repair-order.md`

## Current Baseline

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest
ruff check app tests
alembic heads

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test
npm run build

cd /Users/rocky243/vc-saas/hiveclaw-main
git diff --check
```

Current expected backend baseline after Phase 0R plus the first executable H1-H6 harness trunk: `1887 passed, 7 skipped, 4 warnings`, ruff clean, Alembic single head `add_agent_objectives_0427`.

## Execution Discipline

- Write architecture tests before deleting or tightening old paths.
- Treat `agent_objectives` as the objective source of truth; `focus.md` is a projection.
- Treat triggers as wake policy, not objective storage.
- Treat `RuntimeTask` and output artifacts as the attempt/result ledger.
- Route all tool execution through `ToolRuntimeService`.
- Keep approved execution auditable through `execute_approved_tool` and `ToolRuntimeService.execute_approved`.
- Do not merge feature branches whole when they predate Autonomy P0-P6. Migrate tests, documents, and narrow implementation slices only.

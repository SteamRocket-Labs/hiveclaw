# Workflow Ops Runbook

Last updated: 2026-06-04

## Feature Flags

Runtime flags are environment-backed `Settings` values:

```bash
WORKFLOW_RUNTIME_ENABLED=true
WORKFLOW_TRIGGER_ENABLED=true
WORKFLOW_DEEP_RESEARCH_ENABLED=false
```

- `WORKFLOW_RUNTIME_ENABLED=false`: **stop-new-starts, not a global kill switch.** It fail-closes `start_run` before any workflow `RuntimeTask` is created; in-flight and suspended runs still resume normally (daemon, signal consumer, explicit resume) so existing work drains instead of stranding. To halt a specific running workflow, use the admin cancel / force-suspend commands below.
- `WORKFLOW_TRIGGER_ENABLED=false`: triggers carrying `workflow_ref` stop before launching workflow runtime.
- `WORKFLOW_DEEP_RESEARCH_ENABLED=false`: Deep Research keeps the legacy `RuntimeTask(task_type="deep_research")` path.

> **⛔ Do NOT enable `WORKFLOW_DEEP_RESEARCH_ENABLED` in production yet.** The
> current `deep_research.v1` workflow is the entry/structure shell only: its
> leaves spawn generic subagents and do NOT yet carry the legacy DR
> capabilities — source ledger, citation neutralisation (RC12), coverage
> enforcement (RC13), and **workspace report materialisation** (the workflow
> path currently returns `workspace_artifact_dir: null`, i.e. no report file
> lands in the agent workspace). Until those leaf capabilities are migrated,
> enabling the flag is a product-quality regression, not a canary. The flag
> exists so the cutover is a config change once leaf parity is reached.

## Metrics

Platform admins can inspect in-process rollout counters:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/api/admin/metrics/workflows"
```

The snapshot includes:

- `runs_started_total`
- `runs_finished_total`
- `steps_total`
- `step_duration_seconds`
- `leaf_calls_total`
- `resume_attempts_total`
- `resume_finished_total`
- `quota_denials_total`
- `hash_mismatches_total`

The durable source of truth remains PostgreSQL: `runtime_tasks`, `workflow_steps`, `workflow_leaf_calls`, and `workflow_quotas`.

## Admin Repair Commands

All commands require `platform_admin` auth and a tenant id. Every destructive
command (cancel / force-suspend / replay) writes a fail-soft audit log entry
(`workflow_admin_cancelled` / `workflow_admin_force_suspended` /
`workflow_admin_replay_from_step`) in addition to run-metadata stamps.

Inspect run:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/api/admin/workflows/$RUN_ID?tenant_id=$TENANT_ID"
```

Export journal:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$BASE_URL/api/admin/workflows/$RUN_ID/journal?tenant_id=$TENANT_ID"
```

Cancel run:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"reason":"operator requested"}' \
  "$BASE_URL/api/admin/workflows/$RUN_ID/cancel?tenant_id=$TENANT_ID"
```

Force suspend:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"reason":"manual reconciliation"}' \
  "$BASE_URL/api/admin/workflows/$RUN_ID/force-suspend?tenant_id=$TENANT_ID"
```

Force-suspend on a RUNNING workflow takes effect at the **next step boundary**
— the engine checks the persisted status before starting each step, so the
leaf currently executing finishes (or fails) first; it is never interrupted
mid-flight. The run then stays `suspended` for reconciliation and is not
auto-resumed (resume it explicitly when done).

Replay from step:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"step_id":"write","reason":"bad downstream artifact"}' \
  "$BASE_URL/api/admin/workflows/$RUN_ID/replay-from-step?tenant_id=$TENANT_ID"
```

Replay precondition: the run must be **quiescent**. The API rejects replay
with `409 Conflict` when `RuntimeTask.status=running` or any
`workflow_steps` / `workflow_leaf_calls` row is still `running`. Use
force-suspend first, wait until the current step boundary has settled, inspect
the journal, then replay. This prevents destructive journal deletion from
racing an in-flight worker or a leaf that is still writing quota/step rows.

Replay also refuses (`409 Conflict`) when the rewound range sweeps any step in
`unknown_requires_reconciliation` — an external-effect step that was in flight
at a crash, so whether its side effect happened is unknown. Deleting that row
would erase the reconciliation anchor while the gate's persisted checkpoint
approval still stands, and the rerun would silently re-fire the external
action (verified by test: the send re-executes without any new approval).
Reconcile first, then replay:

- **Effect verifiably happened** (mail delivered, doc shared): mark the step
  row `done` so resume replays it as completed —
  `UPDATE workflow_steps SET status='done', error=NULL WHERE run_id=$RUN_ID AND step_id='<step>'`.
- **Effect verifiably did NOT happen**: delete that step row so resume
  re-executes it under the existing approval —
  `DELETE FROM workflow_steps WHERE run_id=$RUN_ID AND step_id='<step>' AND status='unknown_requires_reconciliation'`.

Both are manual SQL on purpose: reconciliation is a human judgement, and no
API shortcut should make erasing the anchor easy.

Possible follow-up (not scheduled): a first-class
`POST /admin/workflows/{run_id}/resolve-reconciliation` command taking
`{step_id, resolution: "executed" | "not_executed", reason}` — same audit
trail, same 409 discipline, replacing the manual SQL above. Deliberately
deferred: v1 keeps friction on anchor removal until real operational demand
shows the SQL path is too error-prone.

Replay deletes target/downstream step and leaf journal rows only. It does not execute leaves directly; normal resume/daemon execution performs the rerun under the same runtime governance.

Quota accounting on replay: the deleted **fanout leaf rows'** settled
`token_usage` is refunded to the run quota (floored at 0), so the rerun is not
double-charged. Plain `agent_step` consumption has no row-level metering and
stays charged — if a replayed run wedges on `quota`, raise the run budget or
restart it as a fresh run.

## Railway Rollout Checks

Before enabling workflow Deep Research:

```bash
cd backend
ruff check app tests
pytest
alembic heads
```

Production checks:

```bash
railway status
railway logs --service backend --environment production --tail 200
curl -fsS "$BASE_URL/api/health"
curl -fsS -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/admin/metrics/workflows"
```

Database requirement:

- The application `DATABASE_URL` must use a non-superuser role.
- Workflow tables already use `FORCE ROW LEVEL SECURITY`; a superuser still bypasses RLS by PostgreSQL design.
- Verify cross-tenant isolation with the non-superuser DSN before treating production rollout as complete.

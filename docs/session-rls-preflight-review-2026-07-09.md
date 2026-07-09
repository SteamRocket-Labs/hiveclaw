# Session / RLS Preflight Review - 2026-07-09

## Scope

Review range:

- `HEAD~100..HEAD`
- Count: 100 commits
- Current head at review start: `c2484456d docs: terminal atomic architecture review - three blocks, conflict matrix, purity verdict, agent-native plan`

Covered change families:

- Session runtime: durable stream steps, runtime phases, terminal hooks, session projection, workspace deliverables.
- Runtime budget: provider prompt ledger, cache/cost pressure, breaker dimensions, budget run metadata.
- External capability / plugin / skill marketplace: trust gate, activations, session-scoped trials, materialization, catalog.
- Personal knowledge base: core schema, grants, runtime retrieval, ingestion jobs, graph projections.
- RLS and tenant boundaries: fresh bootstrap RLS, Alembic policy migrations, service-layer BYPASS sites, API session authorization.

## Review Verdict

The reviewed code path had real RLS/session boundary gaps. They were not only local patch issues; they were systemic boundary mismatches across migration, bootstrap, service reuse, and BYPASS runtime loading.

This pass fixes the code-level gaps found in the review. Local confidence is above 95% for the patched code paths because each defect has a regression test and the targeted session/RLS suite passes. Production confidence still requires the read-only SQL gates below, because code review cannot prove the live database has no stale policies or orphaned tenant rows.

## Fixed Breakpoints

### 1. Strict tenant tables had a nullable-tenant RLS bypass

Issue:

- `external_capability_rls_0709.py` allowed `OR tenant_id IS NULL` on external capability and capability factor tables.
- Those tables are tenant-owned runtime tables, not global catalog rows.

Fix:

- Removed the null-tenant branch from the migration predicate.
- Added `external_capability_strict_rls_0709.py` to repair databases where the earlier weak policy may already have been applied.
- The repair migration drops both historical policy names and recreates one strict `tenant_isolation_{table}` policy.

### 2. Fresh bootstrap could reintroduce weak RLS

Issue:

- `bootstrap_database_to_head()` uses `create_all + apply_rls_policies + stamp head`.
- That path skips Alembic migrations, so fixing migrations alone would not fix fresh databases.
- `db_bootstrap.py` used the legacy nullable tenant predicate for all normal tenant tables.

Fix:

- Added `STRICT_TENANT_RLS_TABLES`.
- Added `_strict_tenant_predicate()`.
- Routed external capability, capability factor, and personal KB tables through strict equality-only tenant predicates.

### 3. Runtime budget downgrade could drop columns it did not create

Issue:

- `runtime_budget_breaker_dims_0709.py` downgrade dropped `needs_reconciliation_count` and `failures`.
- The corresponding upgrade did not add those columns.

Fix:

- Removed those invalid downgrade drops.

### 4. External extension activation service trusted caller-side tenant checks

Issue:

- API routes checked access, but `activate_external_extension_for_agent`, `try_external_extension_in_chat`, and deactivation did not enforce agent/session tenant ownership internally.
- Any future service reuse could create cross-tenant or cross-session activations.

Fix:

- Added `_require_agent_in_tenant()`.
- Added `_require_chat_session_in_agent_tenant()`.
- Activation, session trial, and deactivation now fail closed before materialization.

### 5. Session index endpoint skipped session ownership authorization

Issue:

- `GET /agents/{agent_id}/sessions/{session_id}/index` only called `check_agent_access`.
- That was insufficient for same-tenant, same-agent, non-owner access.

Fix:

- Reused `_get_run_session_and_agent()` so the index endpoint follows the same ownership / manage-access gate as session messages and run actions.

### 6. Web-chat runtime BYPASS load did not verify tenant consistency

Issue:

- `_load_runtime_context()` loads `RuntimeTask`, `ChatSession`, `Agent`, and `User` under RLS BYPASS.
- It did not verify task/session/user tenant consistency after loading.

Fix:

- Added `_enforce_runtime_context_tenant_boundary()`.
- Runtime task tenant mismatch, session-agent mismatch, session tenant mismatch, and user tenant mismatch now fail closed.
- Legacy null task/session tenant is backfilled from the loaded agent tenant and marked in metadata.

### 7. Command escalation BYPASS lookup was not tenant scoped

Issue:

- `request_command_escalation()` selected `Agent` under BYPASS by `Agent.id` only.
- The tool entrypoint normally passes the current agent id, but the service contract itself was unsafe for future reuse.

Fix:

- `request_command_escalation()` now requires `tenant_id`.
- The BYPASS query filters `Agent.id`, `Agent.tenant_id`, and `Agent.deleted_at IS NULL`.
- The tool handler passes `request.context.tenant_id` and tests assert the tenant is forwarded.

## Remaining Breakpoints Before Production Release

These are not code gaps left open in this pass; they are production-state gates that must be checked before release.

### A. Live policy state may still contain stale weak policies

Run read-only SQL against production:

```sql
SELECT tablename, policyname, qual, with_check
FROM pg_policies
WHERE schemaname = 'public'
  AND tablename IN (
    'external_capability_reviews',
    'external_capability_snapshots',
    'external_extension_catalog_entries',
    'external_extension_components',
    'external_extension_hook_registrations',
    'external_extension_activations',
    'external_marketplace_sources',
    'external_marketplace_entries',
    'capability_factors',
    'capability_factor_reviews',
    'capability_promotion_proposals',
    'knowledge_documents',
    'knowledge_segments',
    'knowledge_entities',
    'knowledge_assertions',
    'knowledge_links',
    'knowledge_index_jobs',
    'knowledge_grants'
  )
ORDER BY tablename, policyname;
```

Pass condition:

- Exactly one tenant isolation policy per table is acceptable.
- `qual` and `with_check` must not contain `tenant_id IS NULL`.
- Each listed table should have `FORCE ROW LEVEL SECURITY` enabled.

### B. Existing null tenant rows must not exist in strict tenant tables

Run one count per table:

```sql
SELECT 'external_capability_reviews' AS table_name, count(*) FROM external_capability_reviews WHERE tenant_id IS NULL
UNION ALL SELECT 'external_capability_snapshots', count(*) FROM external_capability_snapshots WHERE tenant_id IS NULL
UNION ALL SELECT 'external_extension_catalog_entries', count(*) FROM external_extension_catalog_entries WHERE tenant_id IS NULL
UNION ALL SELECT 'external_extension_components', count(*) FROM external_extension_components WHERE tenant_id IS NULL
UNION ALL SELECT 'external_extension_hook_registrations', count(*) FROM external_extension_hook_registrations WHERE tenant_id IS NULL
UNION ALL SELECT 'external_extension_activations', count(*) FROM external_extension_activations WHERE tenant_id IS NULL
UNION ALL SELECT 'external_marketplace_sources', count(*) FROM external_marketplace_sources WHERE tenant_id IS NULL
UNION ALL SELECT 'external_marketplace_entries', count(*) FROM external_marketplace_entries WHERE tenant_id IS NULL
UNION ALL SELECT 'capability_factors', count(*) FROM capability_factors WHERE tenant_id IS NULL
UNION ALL SELECT 'capability_factor_reviews', count(*) FROM capability_factor_reviews WHERE tenant_id IS NULL
UNION ALL SELECT 'capability_promotion_proposals', count(*) FROM capability_promotion_proposals WHERE tenant_id IS NULL
UNION ALL SELECT 'knowledge_documents', count(*) FROM knowledge_documents WHERE tenant_id IS NULL
UNION ALL SELECT 'knowledge_segments', count(*) FROM knowledge_segments WHERE tenant_id IS NULL
UNION ALL SELECT 'knowledge_entities', count(*) FROM knowledge_entities WHERE tenant_id IS NULL
UNION ALL SELECT 'knowledge_assertions', count(*) FROM knowledge_assertions WHERE tenant_id IS NULL
UNION ALL SELECT 'knowledge_links', count(*) FROM knowledge_links WHERE tenant_id IS NULL
UNION ALL SELECT 'knowledge_index_jobs', count(*) FROM knowledge_index_jobs WHERE tenant_id IS NULL
UNION ALL SELECT 'knowledge_grants', count(*) FROM knowledge_grants WHERE tenant_id IS NULL;
```

Pass condition:

- Every count is `0`.

### C. Runtime task/session/agent tenant mismatches must be zero

Run:

```sql
SELECT rt.id, rt.task_type, rt.tenant_id AS runtime_task_tenant_id, a.tenant_id AS agent_tenant_id
FROM runtime_tasks rt
JOIN agents a ON a.id = rt.parent_agent_id
WHERE rt.tenant_id IS DISTINCT FROM a.tenant_id
  AND rt.task_type IN ('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan');
```

```sql
SELECT cs.id, cs.agent_id, cs.tenant_id AS session_tenant_id, a.tenant_id AS agent_tenant_id
FROM chat_sessions cs
JOIN agents a ON a.id = cs.agent_id
WHERE cs.tenant_id IS DISTINCT FROM a.tenant_id;
```

Pass condition:

- Both queries return zero rows, or only legacy null rows that have an explicit backfill plan before release.

### D. Runtime budget run ownership should be audited

Run:

```sql
SELECT rt.id, rt.task_type, rt.tenant_id AS runtime_task_tenant_id, rb.tenant_id AS budget_run_tenant_id
FROM runtime_tasks rt
JOIN runtime_budget_runs rb ON rb.id = rt.budget_run_id
WHERE rt.budget_run_id IS NOT NULL
  AND rt.tenant_id IS DISTINCT FROM rb.tenant_id;
```

Pass condition:

- Zero rows.

Residual risk:

- Some budget reservations can occur before a child `RuntimeTask` exists. The service now scopes API reads by tenant and the runtime context enforces web-chat task boundaries, but malformed pre-task `budget_run_id` misuse is best caught by the SQL gate plus runtime event audits.

## Verification Run

Commands run locally:

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/migrations/test_external_capability_rls_migration.py \
  tests/migrations/test_runtime_budget_breaker_dims_migration.py \
  tests/test_alembic_bootstrap.py \
  tests/services/test_external_capability_activation.py \
  tests/api/test_external_capability_activation_api.py \
  tests/api/test_chat_sessions_permissions.py \
  tests/services/test_web_chat_runtime.py::test_load_runtime_context_resolves_model_before_rls_bypass_transaction_commit \
  tests/services/test_web_chat_runtime.py::test_load_runtime_context_rejects_runtime_task_agent_tenant_mismatch \
  tests/services/test_runtime_budget_service.py \
  tests/services/test_runtime_budget_llm.py \
  tests/services/test_command_escalation.py \
  tests/services/test_audit_rls_coverage.py \
  -q
```

Result: `105 passed, 4 warnings`.

```bash
cd backend && source .venv/bin/activate && ruff check \
  app/db_bootstrap.py \
  app/api/chat_sessions.py \
  app/services/web_chat_runtime.py \
  app/services/external_capabilities/activation.py \
  app/services/command_escalation_service.py \
  app/tools/handlers/command_parity.py \
  tests/migrations/test_external_capability_rls_migration.py \
  tests/migrations/test_runtime_budget_breaker_dims_migration.py \
  tests/test_alembic_bootstrap.py \
  tests/services/test_external_capability_activation.py \
  tests/api/test_chat_sessions_permissions.py \
  tests/services/test_web_chat_runtime.py \
  tests/services/test_command_escalation.py
```

Result: `All checks passed!`

```bash
cd backend && source .venv/bin/activate && alembic heads
```

Result: `external_capability_strict_rls_0709 (head)`.

## Release Recommendation

Do not release directly from the pre-review state.

Release is acceptable after:

1. This patch is merged.
2. Alembic upgrade applies `external_capability_strict_rls_0709`.
3. The production read-only SQL gates above return clean results.
4. No unrelated dirty workspace artifacts are included in the release commit.

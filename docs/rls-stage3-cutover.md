# RLS Stage-3 Cutover Runbook — flip the app to the non-owner role

> **Status:** ready to execute. This is the irreversible-but-instantly-reversible
> step of the RLS enforcement migration. Everything before it (stages 0–2b) is
> deployed and behaves identically under the current owner connection — RLS
> policies exist but are **inert** because the app connects as the table owner.
> This runbook makes them **bite** by pointing the app at a non-owner role.

## What the flip does

PostgreSQL RLS does not apply to a table's owner unless the table is `FORCE`d.
Production connects as the single `DATABASE_URL` user, which is the table owner —
so every `ENABLE`d policy is silently bypassed. The fix is **not** to `FORCE`
every table; it is to make the *app* connect as a **non-owner** role
(`app_rls`, `NOSUPERUSER NOBYPASSRLS`). For a non-owner, plain `ENABLE` already
binds. One connection-string switch turns isolation on for every policied table;
switching it back turns it off — that is the rollback.

## Prerequisites (already shipped)

- Stages 0–2b deployed: 18 agent-scoped tables carry `tenant_id` + a
  `tenant_isolation_*` policy; ~50 bare-session accessors are pinned to
  `tenant_scoped_session` / `enter_rls_bypass`; every INSERT into a policied
  table stamps `tenant_id`. Shadow exhaustion confirmed no accessor is left
  bare against a policied table.
- Deploy plumbing for the split owner/runtime connection is in place:
  - `SCHEMA_DATABASE_URL` (owner) drives all schema work — `create_all`,
    `alembic upgrade`, RLS policy application, `grant_rls_app_role`. Unset = same
    as `DATABASE_URL` (today's behavior).
  - `entrypoint.sh` runs every schema step against `SCHEMA_URL` and starts
    uvicorn against the runtime `DATABASE_URL`.
  - `app/main.py` lifespan bootstrap uses `schema_engine` (owner).
  - `app.scripts.grant_rls_app_role` (idempotent, runs each deploy) grants the
    role DML on current + future tables; it is a no-op until the role exists.

## Cutover steps (owner-gated)

Run in order. Steps 1–4 are reversible and cause no behavior change; step 5 is
the flip.

### 1. Create the non-owner role (one-time, as the DB owner/superuser)

Connect to the production database as the current `DATABASE_URL` user (Railway:
`railway connect Postgres` or `psql "$DATABASE_URL"`), then:

```sql
CREATE ROLE app_rls LOGIN PASSWORD '<choose-a-strong-password>'
    NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
```

`NOBYPASSRLS` is the load-bearing flag — without it the role would skip policies
just like the owner.

### 2. Set `SCHEMA_DATABASE_URL` (Railway env)

Set `SCHEMA_DATABASE_URL` to the **current** `DATABASE_URL` value (the owner
connection string). After step 5 changes `DATABASE_URL`, schema work still runs
as the owner through this variable.

### 3. Grant the role (automatic on next deploy, or run manually)

`entrypoint.sh` Step 2.6 runs `grant_rls_app_role` every deploy; once the role
exists it grants `SELECT/INSERT/UPDATE/DELETE` on all tables + sequences and sets
default privileges for future tables. To grant immediately without a deploy:

```bash
python -m app.scripts.grant_rls_app_role   # run with DATABASE_URL = owner
```

### 4. Backfill `tenant_id` (irreversible data step — dry-run + confirm)

Run **before** the flip, on the owner connection, so every existing row in the
18 stage-2b tables gets its `tenant_id` (a row left NULL is globally visible
under the `OR tenant_id IS NULL` policy clause):

```bash
python -m app.scripts.backfill_stage2b_tenant_id              # dry-run: review the table
python -m app.scripts.backfill_stage2b_tenant_id --apply --confirm
```

Review the dry-run's `orphan_null` column: those are rows whose owning
agent/task is missing or itself tenant-less. Decide whether to delete or assign
them before the flip — they will be cross-tenant visible otherwise.

### 5. Flip the runtime connection (the cutover)

Change the Railway `DATABASE_URL` to connect as `app_rls`:

```
postgresql+asyncpg://app_rls:<password>@<same-host>:<same-port>/<same-db>
```

(keep host/port/db identical to the owner URL; only user + password change).
Redeploy / restart. On boot: `entrypoint.sh` runs schema steps as the owner
(`SCHEMA_URL`), uvicorn serves as `app_rls`, and RLS now isolates every tenant.

## Verification

- App boots clean (no `permission denied`; schema steps ran as owner).
- Smoke a normal tenant flow (chat, trigger, task) — data appears as before.
- Red-team: as `app_rls` with `app.current_tenant_id` set to tenant A, a query
  for tenant B's `chat_messages` / `agent_triggers` returns nothing
  (`tests/integration/test_stage2b_*` prove this shape against Testcontainers).
- Watch error rate + empty-result metrics for a low-traffic window.

## Rollback (instant)

Set `DATABASE_URL` back to the owner connection string and redeploy. Policies go
inert again immediately (owner-bypass) — no data change, no migration to undo.
`SCHEMA_DATABASE_URL` and the `app_rls` role can stay; they are harmless when
unused.

## Notes

- `resolve_tenant_for_agent` / `resolve_tenant_for_plan` use audited
  `enter_rls_bypass` single-row reads, so the chicken-and-egg "read the agent to
  learn its tenant" works under the non-owner role.
- Daemons (trigger, heartbeat, evolution) that legitimately scan all tenants use
  `enter_rls_bypass(reason=...)`; per-agent daemon work pins
  `tenant_scoped_session(agent.tenant_id)`.
- `app.scripts.audit_rls_coverage` reports ENFORCED/INERT/UNPROTECTED — run it
  after the flip to confirm the stage-2b tables moved to ENFORCED.

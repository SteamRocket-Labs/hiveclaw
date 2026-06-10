# RLS Stage-0 Inventory — Group C (Services 核心 A)

Static enumeration of bare `async_session()` call sites in the runtime/services core.
Scope: what each `with`-block queries/writes, the tenant source, who calls it (request vs daemon),
and whether it fail-closes once the app connects as a **non-owner** role and RLS becomes live.

## Method / ground truth used

- `async_session` = `app/database.py:25` sessionmaker. A bare `async with async_session() as db:`
  **never** runs `SET LOCAL app.current_tenant_id` (only `get_db()` / `tenant_scoped_session()` /
  `enter_rls_bypass()` do). `asyncio.create_task` copies contextvars at creation, but that is
  irrelevant here: bare sessions do not *read* the ContextVar at all.
- Tables **with a policy today** (authoritative source = `app/db_bootstrap.py` `RLS_TENANT_TABLES` +
  `RLS_FORCED_TENANT_TABLES`, mirrored from `alembic/versions/add_row_level_security.py` /
  `add_workflow_tables_0604.py` / `coordination_rls_0604.py`):
  - ENABLE+policy (9): `agents, users, llm_models, skills, tools, plaza_posts, org_departments, org_members, config_revisions`
  - FORCE+policy (7): `workflow_*`, `coordination_*`
- Policy shape = `USING (current_setting('app.current_tenant_id', true)='BYPASS' OR tenant_id::text = current_setting(...) OR tenant_id IS NULL)`.
  **There is no WITH CHECK** — so inserting a `tenant_id IS NULL` row is always permitted, and an
  insert that sets `tenant_id` to the GUC value passes too. The fail-closed risk is on **SELECT/UPDATE/DELETE**
  of *other tenants'* rows (you see only `tenant_id IS NULL` rows under an empty GUC).
- **Stage-2 待补-policy (agent-scoped, agent_id, no tenant_id)** confirmed by reading models:
  `chat_messages` (audit.py), `chat_sessions`, `runtime_tasks`, `gateway_messages`, `agent_tools` (tool.py),
  `pending_reply_contexts`. These have **NO policy today → do NOT fail-closed yet**; they fail-closed
  only after Stage-2 adds tenant_id + policy. Marked `agent-scoped` / fail_closed = **NO (today) / YES (post-Stage-2)**.
- **Tenant-scoped but currently UN-policied** (has tenant_id column, but not in any policy list):
  `agent_plan_requests`, `agent_plan_recommendations`. **No policy today → no fail-closed today.**
  They are natural Stage-2 policy candidates (tenant_id already present). Flagged ⚠️ as a gap below.

> Legend for fail_closed: **YES** = fail-closes the moment the role flips (table already has a policy).
> **NO** = safe today. **NO→YES(S2)** = safe today, will fail-close once Stage-2 policies land.

---

## web_chat_runtime.py  (import: `async_session as _async_session`)

| location | operation | table_class | tenant_source | call_class | fail_closed | migration_target |
|----------|-----------|-------------|---------------|-----------|-------------|------------------|
| web_chat_runtime.py:322 | `_persist_assistant_message` — INSERT chat_messages | agent-scoped (`chat_messages`) | loaded-entity (agent_id/user_id args, both derive from loaded agent) | MIXED (detached run task; initiated by REQUEST web/WS, also Plan-Mode handoff) | NO→YES(S2) | tenant_scoped_session(agent.tenant_id) — thread tenant from `_load_runtime_context` |
| web_chat_runtime.py:347 | `_persist_tool_call` — INSERT chat_messages | agent-scoped (`chat_messages`) | loaded-entity | MIXED (detached run task) | NO→YES(S2) | tenant_scoped_session(agent.tenant_id) |
| web_chat_runtime.py:376 | `_persist_runtime_event` — INSERT chat_messages | agent-scoped (`chat_messages`) | loaded-entity | MIXED (detached run task) | NO→YES(S2) | tenant_scoped_session(agent.tenant_id) |
| web_chat_runtime.py:509 | `_accept_latest_plan_mode_recommendation` → accept_latest_recommendation_for_user — SELECT/UPDATE agent_plan_recommendations | tenant-scoped, **UN-policied today** (`agent_plan_recommendations`) | param (agent_id, user_id, session_id) → loaded recommendation | MIXED (detached run task) | NO→YES(S2) ⚠️ tenant-scoped but no policy yet | tenant_scoped_session(tid) once tenant resolvable — ⚠️needs-review (no tenant_id param at this call; resolvable via agent) |
| web_chat_runtime.py:585 | `_update_runtime_task` — SELECT+UPDATE runtime_tasks (status/result/completed_at) | agent-scoped (`runtime_tasks`) | loaded-entity (run_uuid → task; tenant via parent agent) | MIXED (detached run task) | NO→YES(S2) | tenant_scoped_session(agent.tenant_id) — load tenant alongside RuntimeTask |
| web_chat_runtime.py:605 | `_load_runtime_context` — SELECT runtime_tasks, **agents**, **users**, **llm_models** | mixed (`runtime_tasks` agent-scoped; **`agents`/`users`/`llm_models` HAVE policy**) | self-resolving (looks up agent/user/model by PK; tenant derived from agent.tenant_id) | MIXED (detached run task; bootstrap of the run's tenant) | **YES** (agents/users/llm_models policied today) | ⚠️needs-review — bootstrap lookup: either `enter_rls_bypass(reason="durable web-run tenant bootstrap")` for the PK fetch, OR pass request-side tenant_id into the run metadata and `tenant_scoped_session(tid)`. llm_models query already filters tenant_id but still fail-closes without GUC. |
| web_chat_runtime.py:693 | `_resume_queued_plan_handoffs` — SELECT agent_plan_requests (confirmed+queued handoffs) | tenant-scoped, **UN-policied today** (`agent_plan_requests`) | param (agent_id, session_id) | MIXED (fires from terminal-state hook of a completed run) | NO→YES(S2) ⚠️ | tenant_scoped_session(tid) once policied — ⚠️needs-review (no tenant_id param; resolvable via agent) |
| web_chat_runtime.py:741 | `_deliver_run_result_to_channel` — SELECT chat_sessions (delivery_target_json) + ChannelDeliveryService.send_text | agent-scoped (`chat_sessions`) (+ downstream channel writes) | param (agent_id, session_id) | MIXED (detached run task) | NO→YES(S2) | tenant_scoped_session(agent.tenant_id) |
| web_chat_runtime.py:774 | INSERT gateway_messages (openclaw forward) | agent-scoped (`gateway_messages`) | loaded-entity (agent.id, user.id) | MIXED (detached run task) | NO→YES(S2) | tenant_scoped_session(agent.tenant_id) |
| web_chat_runtime.py:857 | `_claim_pending_reply_suffix_for_session` — SELECT chat_sessions + claim_and_fulfill_pending_replies (UPDATE pending_reply_contexts) | agent-scoped (`chat_sessions`, `pending_reply_contexts`) | param (agent_id, session_id) | MIXED (detached run task) | NO→YES(S2) | tenant_scoped_session(agent.tenant_id) |
| web_chat_runtime.py:875 | `decline_latest_recommendation_for_user` — SELECT/UPDATE agent_plan_recommendations | tenant-scoped, **UN-policied today** (`agent_plan_recommendations`) | param (agent_id, user_id, session_id) | MIXED (detached run task) | NO→YES(S2) ⚠️ | tenant_scoped_session(tid) once policied — ⚠️needs-review |

**Caller proof**: `execute_web_chat_run` is launched at `web_chat_runtime.py:238` via
`asyncio.create_task(...)` inside `start_web_chat_run`, which is invoked from `api/chat_sessions.py:316`
(REQUEST), `api/websocket.py:637` (REQUEST/WS), and `services/plan_mode_session_handoff.py:143`
(Plan-Mode handoff). The durable executor is detached from the request lifecycle → **MIXED**.

---

## plan_mode_service.py  (import: `async_session`)  — every site = `agent_plan_requests`

| location | operation | table_class | tenant_source | call_class | fail_closed | migration_target |
|----------|-----------|-------------|---------------|-----------|-------------|------------------|
| plan_mode_service.py:158 | `create_request` — INSERT agent_plan_requests | tenant-scoped, **UN-policied today** | param (`tenant_id` arg present here) | REQUEST (Plan-Mode API / runtime) | NO→YES(S2) ⚠️ | tenant_scoped_session(tenant_id) — tenant_id already a param |
| plan_mode_service.py:323 | `generate_plan` (move-to-planning) — SELECT+UPDATE agent_plan_requests | tenant-scoped, UN-policied today | self-resolving (`_load` by plan_id; no tenant filter) | REQUEST | NO→YES(S2) ⚠️ | ⚠️needs-review — load tenant from plan row first, then tenant_scoped_session(plan.tenant_id) |
| plan_mode_service.py:345 | `generate_plan` (apply generation) — SELECT+UPDATE agent_plan_requests | tenant-scoped, UN-policied today | self-resolving (`_load` by plan_id) | REQUEST | NO→YES(S2) ⚠️ | ⚠️needs-review — same as :323 |
| plan_mode_service.py:504 | `supersede_to_draft` — SELECT old + INSERT new + UPDATE old | tenant-scoped, UN-policied today | loaded-entity (copies `old.tenant_id` onto new row) | REQUEST | NO→YES(S2) ⚠️ | ⚠️needs-review — load old row tenant, then tenant_scoped_session(old.tenant_id) |
| plan_mode_service.py:585 | `confirm_plan` — SELECT+UPDATE agent_plan_requests | tenant-scoped, UN-policied today | self-resolving (`_load` by plan_id) | REQUEST (user confirmation) | NO→YES(S2) ⚠️ | ⚠️needs-review |
| plan_mode_service.py:670 | `reject_plan` — SELECT+UPDATE agent_plan_requests | tenant-scoped, UN-policied today | self-resolving (`_load` by plan_id) | REQUEST | NO→YES(S2) ⚠️ | ⚠️needs-review |
| plan_mode_service.py:716 | `handoff_confirmed_plan` — SELECT+UPDATE agent_plan_requests (+ handler) | tenant-scoped, UN-policied today | self-resolving (`_load` by plan_id) | MIXED (API confirm path + queued-resume from completed-run hook) | NO→YES(S2) ⚠️ | ⚠️needs-review |
| plan_mode_service.py:791 | `get_plan` — SELECT agent_plan_requests | tenant-scoped, UN-policied today | self-resolving (`_load` by plan_id) | REQUEST (API read) | NO→YES(S2) ⚠️ | ⚠️needs-review |
| plan_mode_service.py:799 | `list_plans_for_agent` — SELECT agent_plan_requests by agent_id | tenant-scoped, UN-policied today | param (agent_id) | REQUEST (API read) | NO→YES(S2) ⚠️ | tenant_scoped_session(tid) — needs agent→tenant resolve or pass tenant_id |

**Note**: `agent_plan_requests` is **not** in any current policy list, so none of these fail-close *today*.
But the table has a tenant_id column and is the obvious Stage-2 policy target. Once policied, the
load-by-plan_id sites (`_load`) all need the row's tenant pinned on the GUC **before** the read —
chicken-and-egg unless the caller passes tenant_id. **Flagged as Stage-2 design dependency.**

---

## resource_discovery.py  (import: `async_session`)

| location | operation | table_class | tenant_source | call_class | fail_closed | migration_target |
|----------|-----------|-------------|---------------|-----------|-------------|------------------|
| resource_discovery.py:17 | `_resolve_agent_tenant_id` — SELECT agents.tenant_id by agent_id | **`agents` HAS policy** | self-resolving (this IS the tenant resolver) | REQUEST (runtime tool: discover/import) | **YES** | ⚠️needs-review — bootstrap: `enter_rls_bypass(reason="agent tenant resolution for MCP discovery")` (it looks up its own tenant by PK) |
| resource_discovery.py:37 | `_get_smithery_api_key` — SELECT agent_tools (by agent_id) + tools (by name) | mixed (`agent_tools` agent-scoped no-policy; **`tools` HAS policy**) | param (agent_id) | REQUEST | **YES** (tools is policied) | tenant_scoped_session(agent_tenant) — resolve tenant first; tools read needs GUC |
| resource_discovery.py:92 | `_get_modelscope_api_token` — SELECT tools by name | **`tools` HAS policy** | N/A (system tool config; rows are tenant_id NULL global) | REQUEST | **YES** for tenant-owned tools; NULL-tenant config rows still visible | ⚠️needs-review — if config rows are global (tenant_id NULL) they stay visible; if any tenant-owned, needs tenant_scoped_session. enter_rls_bypass(reason="global tool config read") is the safe call. |
| resource_discovery.py:289 | (import_mcp_from_smithery) write-back smithery key — SELECT tools + SELECT/UPSERT agent_tools | mixed (`tools` policied; `agent_tools` no-policy) | param (agent_id) → `agent_tenant_id` resolved upstream | REQUEST | **YES** (tools is policied) | tenant_scoped_session(agent_tenant_id) |
| resource_discovery.py:322 | dedup check — SELECT tools (tenant_filter) + SELECT agent_tools | mixed (`tools` policied; `agent_tools` no-policy) | loaded-entity (`agent_tenant_id` already resolved; query filters Tool.tenant_id) | REQUEST | **YES** (tools is policied; manual filter is redundant w/ RLS but still fail-closes w/o GUC) | tenant_scoped_session(agent_tenant_id) |
| resource_discovery.py:453 | import write — INSERT/UPDATE tools + AgentTool links | mixed (`tools` policied; `agent_tools` no-policy) | loaded-entity (`agent_tenant_id`) | REQUEST | **YES** (tools writes; UPDATE of existing tenant rows fail-closes, INSERT of new rows OK b/c no WITH CHECK) | tenant_scoped_session(agent_tenant_id) |
| resource_discovery.py:651 | direct import write — INSERT/UPDATE tools (tenant_filter dedup) + AgentTool | mixed (`tools` policied; `agent_tools` no-policy) | loaded-entity (`agent_tenant_id`) | REQUEST | **YES** (tools) | tenant_scoped_session(agent_tenant_id) |
| resource_discovery.py:800 | `seed_atlassian_rovo_tools` — SELECT + INSERT/UPDATE tools (tenant_id left NULL = global) | **`tools` HAS policy** (writes GLOBAL tenant_id NULL rows) | N/A-global (no tenant; system seeder) | SEEDER (`main.py:364` lifespan startup) | NO for INSERT (tenant_id NULL passes; no WITH CHECK). **YES for UPDATE/SELECT of any tenant-owned Rovo rows** | enter_rls_bypass(reason="global Atlassian Rovo tool seeding") — global write, must see/maintain all rows |
| resource_discovery.py:864 | `refresh_atlassian_rovo_api_key` — UPDATE tools by mcp_server_name (all rows) | **`tools` HAS policy** | N/A-global (updates ALL Rovo rows across tenants) | SEEDER/admin-config (called on API-key update) | **YES** (UPDATE only touches `tenant_id IS NULL` rows under empty GUC → silently misses tenant-owned rows) | enter_rls_bypass(reason="global Atlassian Rovo key refresh") — must update across all tenants |

**Caller proof**: `discover_resources` / `import_mcp_server` are agent tool handlers
(`tools/handlers/search.py:229`, `tools/handlers/mcp.py:208`) → REQUEST/runtime.
`seed_atlassian_rovo_tools` is called at `main.py:364` (lifespan startup) → SEEDER.

---

## runtime_task_service.py  (import: `async_session`)  — every site = `runtime_tasks`

| location | operation | table_class | tenant_source | call_class | fail_closed | migration_target |
|----------|-----------|-------------|---------------|-----------|-------------|------------------|
| runtime_task_service.py:64 | `create_runtime_task_record` — INSERT runtime_tasks | agent-scoped (`runtime_tasks`) | param (parent_agent_id; no tenant) | MIXED (delegation runtime + web-run create) | NO→YES(S2) | ⚠️needs-review — runtime_tasks has no tenant_id; Stage-2 must add column. Then tenant_scoped_session(parent_agent.tenant) or pass tenant. |
| runtime_task_service.py:93 | `update_runtime_task_record` — SELECT+UPDATE runtime_tasks by id | agent-scoped (`runtime_tasks`) | self-resolving (task_id) | MIXED | NO→YES(S2) | ⚠️needs-review — same as :64 |
| runtime_task_service.py:132 | `get_runtime_task_record` — SELECT runtime_tasks by id | agent-scoped (`runtime_tasks`) | self-resolving (task_id) | MIXED | NO→YES(S2) | ⚠️needs-review |
| runtime_task_service.py:149 | `list_runtime_task_records` — SELECT runtime_tasks by parent_agent_id | agent-scoped (`runtime_tasks`) | param (parent_agent_id) | REQUEST (`messaging.py:1225`, agent tool) | NO→YES(S2) | tenant_scoped_session(parent_agent.tenant) once tenant column exists |
| runtime_task_service.py:167 | `list_active_runtime_task_records` — SELECT runtime_tasks WHERE status in (pending,running) **across all** | agent-scoped (`runtime_tasks`) | NONE-daemon (cross-tenant scan) | DAEMON (`orchestrator.py:1318` resume_persisted_async_delegations) | NO→YES(S2) | **enter_rls_bypass(reason="restart-safe async-delegation resume scan")** — genuinely cross-tenant |
| runtime_task_service.py:195 | `reconcile_orphaned_runtime_tasks` — SELECT+UPDATE all running runtime_tasks | agent-scoped (`runtime_tasks`) | NONE-daemon (cross-tenant scan) | DAEMON (`main.py:349` lifespan startup) | NO→YES(S2) | **enter_rls_bypass(reason="startup orphaned runtime-task reconcile")** — genuinely cross-tenant |

**Caller proof**: `reconcile_orphaned_runtime_tasks` ← `main.py:349` (startup DAEMON);
`list_active_runtime_task_records` ← `orchestrator.py:1318` (restart-safe resume, DAEMON);
`list_runtime_task_records` ← `messaging.py:1225` (agent tool, REQUEST). create/update/get are shared
by delegation runtime and the web-run path → MIXED.

---

## invoker.py  (import: `async_session`)  — central runtime tenant bootstrap

| location | operation | table_class | tenant_source | call_class | fail_closed | migration_target |
|----------|-----------|-------------|---------------|-----------|-------------|------------------|
| invoker.py:208 | `_resolve_runtime_config` — SELECT **agents** by id (+ feature_flags read) | **`agents` HAS policy** | self-resolving (THIS derives tenant from agent.tenant_id) | MIXED (every entry point: web/IM/trigger/heartbeat/delegation) | **YES** | ⚠️needs-review — central bootstrap; cleanest fix = pass `request.tenant_id` (already on AgentInvocationRequest, invoker.py:98) and `tenant_scoped_session(request.tenant_id)`; fallback `enter_rls_bypass(reason="runtime agent/tenant resolution")` for the PK lookup |
| invoker.py:256 | `_resolve_current_user_name` — SELECT **users** by user_id | **`users` HAS policy** | self-resolving (user_id by PK) | MIXED (every entry point) | **YES** | ⚠️needs-review — pass tenant from request → tenant_scoped_session(request.tenant_id); user is same-tenant. fallback enter_rls_bypass(reason="runtime current-user display-name lookup") |
| invoker.py:921 | `_resolve_agent_smart_model_routing` — SELECT **agents** by id | **`agents` HAS policy** | self-resolving (agent by PK) | MIXED (every entry point) | **YES** | ⚠️needs-review — same as :208; tenant_scoped_session(request.tenant_id) or enter_rls_bypass |

**Caller proof**: all three are wired as `KernelDependencies` callbacks inside `invoke_agent()`
(invoker.py:877–878, 980) — the single entry for ALL agent execution (web chat, IM channels,
trigger, heartbeat, delegation). `AgentInvocationRequest` already carries `tenant_id`
(invoker.py:98, from `metadata["tenant_id"]`), so the preferred migration is to thread that into
the session GUC rather than bypass.

---

## mcp_server_service.py  (import: `async_session` — local import at call site)

| location | operation | table_class | tenant_source | call_class | fail_closed | migration_target |
|----------|-----------|-------------|---------------|-----------|-------------|------------------|
| mcp_server_service.py:752 | `import_mcp_for_agent_and_register` — SELECT **agents** by id → agent.tenant_id | **`agents` HAS policy** | self-resolving (resolves tenant from agent) | REQUEST (`web_mcp.py:1250/1262`, import_mcp_server agent tool) | **YES** | ⚠️needs-review — bootstrap PK lookup; `enter_rls_bypass(reason="MCP import agent-tenant resolution")` OR caller passes tenant. |
| mcp_server_service.py:767 | (same fn) — SELECT **tools** JOIN agent_tools (post-import server records) | mixed (**`tools` HAS policy**; `agent_tools` no-policy) | loaded-entity (`tenant_id` already resolved at :757) | REQUEST | **YES** (tools is policied) | tenant_scoped_session(tenant_id) — tenant_id already in scope at this point |

**Caller proof**: `import_mcp_for_agent_and_register` ← `services/agent_tool_domains/web_mcp.py:1250,1262`
(the `import_mcp_server` agent tool) → REQUEST/runtime.

---

## Summary counts (verified by deterministic per-site tally)

- **Total bare `async_session()` call sites in Group C: 40**
  - web_chat_runtime.py: 11
  - plan_mode_service.py: 9
  - resource_discovery.py: 9
  - runtime_task_service.py: 6
  - invoker.py: 3
  - mcp_server_service.py: 2 (one fn, two blocks)

  (11+9+9+6+3+2 = 40. An earlier draft of this doc mis-stated 35 — corrected.)

- **call_class distribution (each site counted exactly once; sums to 40):**

  | call_class | count | sites |
  |-----------|-------|-------|
  | **REQUEST** | **18** | plan_mode 158,323,345,504,585,670,791,799 (8) · resource_discovery 17,37,92,289,322,453,651 (7) · runtime_task 149 (1) · mcp_server 752,767 (2) |
  | **MIXED** | **18** | web_chat_runtime 322,347,376,509,585,605,693,741,774,857,875 (11) · invoker 208,256,921 (3) · runtime_task 64,93,132 (3) · plan_mode 716 (1) |
  | **DAEMON** | **2** | runtime_task 167,195 |
  | **SEEDER** | **2** | resource_discovery 800,864 |

  Per-file cross-check (disjoint):

  | file | REQUEST | MIXED | DAEMON | SEEDER | total |
  |------|--------:|------:|-------:|-------:|------:|
  | web_chat_runtime.py | 0 | 11 | 0 | 0 | 11 |
  | plan_mode_service.py | 8 | 1 | 0 | 0 | 9 |
  | resource_discovery.py | 7 | 0 | 0 | 2 | 9 |
  | runtime_task_service.py | 1 | 3 | 2 | 0 | 6 |
  | invoker.py | 0 | 3 | 0 | 0 | 3 |
  | mcp_server_service.py | 2 | 0 | 0 | 0 | 2 |
  | **TOTAL** | **18** | **18** | **2** | **2** | **40** |

---

## fail_closed = YES (fail-closes the instant the role flips — table already policied)

These are the **only sites that break on day one of the role swap** (the rest are NO→YES(S2),
gated on Stage-2 adding policies to agent-scoped / currently-unpolicied tables).

| file:line | table(s) policied today | migration_target |
|-----------|-------------------------|------------------|
| web_chat_runtime.py:605 | `agents`, `users`, `llm_models` (in `_load_runtime_context`) | enter_rls_bypass(reason="durable web-run tenant bootstrap") **or** pass request tenant_id → tenant_scoped_session(tid) |
| resource_discovery.py:17 | `agents` (`_resolve_agent_tenant_id`) | enter_rls_bypass(reason="agent tenant resolution for MCP discovery") |
| resource_discovery.py:37 | `tools` (+ agent_tools no-policy) | tenant_scoped_session(agent_tenant) |
| resource_discovery.py:92 | `tools` | enter_rls_bypass(reason="global tool config read") — global NULL-tenant config |
| resource_discovery.py:289 | `tools` (+ agent_tools) | tenant_scoped_session(agent_tenant_id) |
| resource_discovery.py:322 | `tools` (+ agent_tools) | tenant_scoped_session(agent_tenant_id) |
| resource_discovery.py:453 | `tools` (+ agent_tools) | tenant_scoped_session(agent_tenant_id) |
| resource_discovery.py:651 | `tools` (+ agent_tools) | tenant_scoped_session(agent_tenant_id) |
| resource_discovery.py:800 | `tools` (global NULL writes; tenant-owned UPDATE/SELECT) | enter_rls_bypass(reason="global Atlassian Rovo tool seeding") |
| resource_discovery.py:864 | `tools` (UPDATE all tenants by server name) | enter_rls_bypass(reason="global Atlassian Rovo key refresh") |
| invoker.py:208 | `agents` (`_resolve_runtime_config`) | tenant_scoped_session(request.tenant_id) [preferred] or enter_rls_bypass(reason="runtime agent/tenant resolution") |
| invoker.py:256 | `users` (`_resolve_current_user_name`) | tenant_scoped_session(request.tenant_id) or enter_rls_bypass(reason="runtime current-user display-name lookup") |
| invoker.py:921 | `agents` (`_resolve_agent_smart_model_routing`) | tenant_scoped_session(request.tenant_id) or enter_rls_bypass(reason="runtime smart-model-routing lookup") |
| mcp_server_service.py:752 | `agents` (resolve agent tenant) | enter_rls_bypass(reason="MCP import agent-tenant resolution") or pass tenant |
| mcp_server_service.py:767 | `tools` (+ agent_tools) | tenant_scoped_session(tenant_id) — tenant already resolved at :757 |

**15 day-one fail-closed sites.** Highest-stakes: the **3 invoker.py** sites + **web_chat_runtime.py:605**
— these are the central runtime tenant-bootstrap; if left bare, **all** agent execution fail-closes
(kernel cannot resolve agent/tenant). They MUST be migrated before the role flip.

---

## ⚠️ UNSURE / needs-review / unknown — why uncertain

1. **All `_load`-by-plan_id sites in plan_mode_service.py (323,345,504,585,670,716,791) — chicken-and-egg.**
   `agent_plan_requests` is **not policied today** (no fail-closed now), so they're listed NO→YES(S2).
   The uncertainty is the **Stage-2 migration target**: these resolve the row *by plan_id with no tenant
   filter*, so once the table is policied you can't read the row to learn its tenant without already
   having the GUC set. Resolution requires either (a) callers pass tenant_id down, or (b) a scoped
   bypass for the PK fetch then re-scope. Marked ⚠️needs-review pending the Stage-2 policy decision.

2. **invoker.py:208 / 256 / 921 — bypass vs. request-tenant.** These fail-close day one (policied tables).
   `AgentInvocationRequest` *does* carry `tenant_id` (invoker.py:98), so the clean fix is
   `tenant_scoped_session(request.tenant_id)` — BUT `_resolve_runtime_config` is the function that
   authoritatively *establishes* tenant from `agent.tenant_id`, and its signature only takes `agent_id`.
   If the request's tenant_id is ever absent/untrusted (e.g. heartbeat/trigger paths that build the
   request without metadata tenant), it must fall back to a bypass PK lookup. ⚠️needs-review = decide
   whether request.tenant_id is always present & trusted across all 5 entry points, or wire a bypass.

3. **resource_discovery.py:92 (`_get_modelscope_api_token`) — global vs tenant config rows.** Reads
   `tools` by name for system tool config (`discover_resources`/`import_mcp_server`). If those config
   rows are global (tenant_id NULL — likely, they're platform tools) they remain visible under empty GUC
   (no fail-close). If any are tenant-owned, they fail-close. ⚠️ because the row tenancy isn't proven
   here; safest is `enter_rls_bypass(reason="global tool config read")`.

4. **resource_discovery.py:800/864 (Atlassian Rovo seed/refresh) — INSERT vs UPDATE asymmetry.**
   The policy has **no WITH CHECK**, so INSERTing NULL-tenant global rows is fine. But :864 is a blanket
   `UPDATE tools ... WHERE mcp_server_name = ...` across **all** tenants, and :800 SELECTs existing rows;
   under an empty GUC these only touch `tenant_id IS NULL` rows and **silently skip tenant-owned Rovo
   tools**. That's a correctness bug, not a crash → marked YES with enter_rls_bypass. ⚠️ flag: confirm
   whether Rovo tools are ever tenant-scoped or always global before choosing bypass vs scoped-loop.

5. **runtime_task_service.py:64/93/132/149 + all `runtime_tasks` sites — Stage-2 column dependency.**
   `runtime_tasks` has **no tenant_id column** (only parent_agent_id). It cannot be policied as-is; Stage-2
   must add a tenant_id column (per the plan's "agent-scoped 表加 tenant_id 列"). Until then NO fail-close.
   Migration target is ⚠️needs-review because it depends on how the new column is backfilled (from
   parent_agent.tenant_id) and whether create/update sites get tenant passed or resolve it.

---

## Unexpected findings (isolation gaps / mixed paths / runtime-critical)

1. **🔴 Runtime tenant bootstrap is entirely on bare sessions (invoker.py:208/256/921 + web_chat_runtime.py:605).**
   The single most important finding: the central `invoke_agent()` path resolves agent/user/tenant via
   bare `async_session()` against **policied** tables (`agents`, `users`, `llm_models`). The instant the
   app connects as non-owner, these fail-closed and **no agent can execute** (kernel can't load its own
   agent row). The request already has `tenant_id` in hand (invoker.py:98) but the resolver helpers
   ignore it and re-query by PK. This is the make-or-break set for the role flip.

2. **🟠 Durable web-chat runs are a detached/MIXED execution context masquerading as request-scoped.**
   `execute_web_chat_run` is `asyncio.create_task`'d (web_chat_runtime.py:238) and then does **11** bare-session
   operations. Even though it's initiated by an HTTP/WS request, the durable task is detached — and bare
   sessions wouldn't honor the request GUC anyway. Every persist (`chat_messages`, `gateway_messages`),
   every status update (`runtime_tasks`), and the channel-delivery read (`chat_sessions`) is unscoped.
   All agent-scoped (NO→YES(S2)), but this whole subsystem needs tenant threaded from the loaded agent.

3. **🟠 Two tenant-scoped tables have tenant_id but NO policy — silent Stage-2 gap.**
   `agent_plan_requests` and `agent_plan_recommendations` both carry tenant_id yet are absent from
   `RLS_TENANT_TABLES`/`RLS_FORCED_TENANT_TABLES` and the RLS migration. They do **not** fail-close today,
   which means Plan-Mode data is currently cross-tenant-readable under the (future) non-owner role until
   Stage-2 explicitly adds their policies. They're easy wins (tenant_id already present) and should be on
   the Stage-2 list alongside the agent-scoped tables. **Not in the prompt's enumerated Stage-2 set — flag for the owner.**

4. **🟠 `agent_tools` (agent-scoped, no tenant_id) is read/written alongside policied `tools` in 6 resource_discovery sites + mcp_server:767.** When `tools` fail-closes but `agent_tools` doesn't, the
   import/dedup logic sees a half-visible world (no tool rows, but stale agent_tool links). Stage-2 must
   add tenant_id to `agent_tools` too, or these become subtly inconsistent rather than cleanly fail-closed.

5. **🟡 Global tool seeders (resource_discovery.py:800/864) do blanket cross-tenant UPDATEs.**
   `refresh_atlassian_rovo_api_key` rewrites config on **every** Rovo tool row regardless of tenant. Under
   RLS this becomes a silent partial update (NULL-tenant rows only). This is a pre-existing cross-tenant
   write that only "works" today because of owner-bypass — exactly the kind of thing the migration must
   make explicit via `enter_rls_bypass`.

6. **🟡 plan_mode_service.handoff (716) and web_chat_runtime resume (693) are MIXED via completed-run hooks.**
   These fire from a *terminal-state hook of a finished run*, not the original request — so they run in
   whatever context the run executor had (detached). Tenant must come from the loaded plan/agent row, not
   an ambient request GUC.

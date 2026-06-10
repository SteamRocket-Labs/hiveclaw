# RLS Stage 0 — Inventory Group E (Tools + agent_tool_domains)

Static exhaustive analysis of every `async_session()` / `_async_session()` bare-session
call point in Group E. Read-only; no source modified.

**Total call points: 48** (grep-verified across 17 files).

## Table-class reference (resolved from `app/models/`)

| Model / table | Scope | In named RLS policy set? | fail-closed under bare session? |
|---|---|---|---|
| `agents` | tenant (tenant_id) | YES (ENABLE) | YES |
| `users` | tenant (tenant_id) | YES (ENABLE) | YES |
| `llm_models` | tenant (tenant_id) | YES (ENABLE) | YES |
| `skills` | tenant (tenant_id) | YES (ENABLE) | YES |
| `tools` | tenant (tenant_id) | YES (ENABLE) | YES |
| `plaza_posts` | tenant (tenant_id) | YES (ENABLE) | YES |
| `org_members`, `org_departments` | tenant (tenant_id) | YES (ENABLE) | YES |
| `agent_tools` | **agent-scoped** (agent_id, NO tenant_id) | Stage-2 (to add) | YES (after Stage 2) |
| `chat_sessions`, `chat_messages` | **agent-scoped** (agent_id, NO tenant_id) | Stage-2 | YES (after Stage 2) |
| `tasks`, `task_logs` | **agent-scoped** (agent_id, NO tenant_id) | Stage-2 | YES (after Stage 2) |
| `agent_triggers` | **agent-scoped** (agent_id, NO tenant_id) | Stage-2 | YES (after Stage 2) |
| `channel_configs` | **agent-scoped** (agent_id, NO tenant_id) | Stage-2 | YES (after Stage 2) |
| `tenant_settings`, `tenant_channel_configs`, `capability_policies`, `security_audit_events` | tenant (tenant_id) | NOT in named set | NO unless policy added |
| `system_settings`, `participants`, `mcp_servers*` (separate) | global / not-in-set | n/a | NO |

> NOTE: `mcp_servers` / `mcp_server_tools` carry mandatory tenant_id and are FORCE-eligible, but
> Group E reaches MCP gating only via `resolve_agent_mcp_tool_mode(db, agent_id, tool)` whose
> internal queries live outside these files — flagged where relevant, classed by the *first* table
> the with-block touches (almost always `tools`/`agents`).

---

## Inventory table

| location (file:line) | operation (表+操作) | table_class | tenant_source | call_class | fail_closed | migration_target |
|---|---|---|---|---|---|---|
| tools/governance_resolver.py:45 | `agents` SELECT (security_zone) by agent_id | tenant-scoped | loaded-entity (only agent_id in closure) | REQUEST | YES | set_current_tenant+loop (load agent → set GUC) / ⚠️needs-review |
| tools/governance_resolver.py:63 | `capability_policies` SELECT via `check_capability(tenant_id,agent_id,tool)` | tenant-scoped (not-in-set) | param (tenant_id) | REQUEST | NO (table not in named set) | tenant_scoped_session(tenant_id) (defensive) |
| tools/governance_resolver.py:67 | `security_audit_events`/audit INSERT via write_audit_event | tenant-scoped (not-in-set) | param (kwargs) | REQUEST | NO | tenant_scoped_session(tid) (defensive) |
| tools/governance_resolver.py:80 | `agents` SELECT + `approval_requests` INSERT | mixed (agents tenant + approval_requests agent-scoped) | loaded-entity (agent_id only) | REQUEST | YES (agents) | set_current_tenant+loop / ⚠️needs-review |
| tools/governance_resolver.py:113 | `tools` JOIN `agent_tools` by agent_id; then resolve_agent_mcp_tool_mode | mixed (tools tenant + agent_tools agent) | loaded-entity (agent_id only) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| tools/handlers/mcp.py:36 | `tools` JOIN `agent_tools` by agent_id (list_mcp_resources) | mixed (tools+agent_tools) | loaded-entity (agent_args; agent_id only) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| tools/handlers/mcp.py:137 | `tools` JOIN `agent_tools` by agent_id (read_mcp_resource) | mixed | loaded-entity (agent_id only) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| tools/handlers/mcp.py:307 | `tools` JOIN `agent_tools` by agent_id (call_mcp_tool) + mcp gating | mixed | loaded-entity (agent_id only) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| tools/workspace.py:240 | `agents` SELECT by agent_id (soul.md bootstrap) | tenant-scoped | loaded-entity (agent_id only) | MIXED (REQUEST tool-resolve + DAEMON heartbeat:1747) | YES | set_current_tenant+loop / ⚠️needs-review |
| tools/workspace.py:320 | `tasks` SELECT by agent_id (_sync_tasks_to_file) | agent-scoped (Stage-2) | NONE (agent_id only; no tenant_id col yet) | MIXED (REQUEST + DAEMON) | YES (after Stage 2) | ⚠️needs-review (Stage 2: tenant_id col then set_current_tenant) |
| tools/handlers/tasks.py:36 | `tasks` SELECT by agent_id (_list_tasks_for_agent; REST-only) | agent-scoped (Stage-2) | NONE (agent_id only) | REQUEST (api/tasks.py) | YES (after Stage 2) | ⚠️needs-review (Stage 2) |
| tools/handlers/tasks.py:59 | `tasks`+`task_logs` SELECT by agent_id (_get_task_for_agent; REST-only) | agent-scoped (Stage-2) | NONE (agent_id only) | REQUEST | YES (after Stage 2) | ⚠️needs-review (Stage 2) |
| tools/handlers/subagent.py:52 | `agents` SELECT by agent_id + `llm_models` by agent.tenant_id | tenant-scoped | loaded-entity (tenant derived FROM agent being loaded) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| tools/handlers/subagent.py:85 | `llm_models` SELECT filtered by tenant_id (_resolve_model_override) | tenant-scoped | param (tenant_id arg, may be None) | REQUEST | YES | tenant_scoped_session(tenant_id) / ⚠️needs-review if tenant_id None |
| tools/resolver.py:31 | `agents.tenant_id` SELECT by agent_id (bootstrap of ToolExecutionContext) | tenant-scoped | loaded-entity (agent_id only — this IS the tenant resolver) | REQUEST (every tool exec; also reached on daemon paths) | YES | set_current_tenant+loop / ⚠️needs-review — KEY BOOTSTRAP NODE |
| tools/handlers/hr.py:1167 | big block: `users`,`tenant_settings`,`llm_models`,`agents`,`participants`,`skills`,`tenants`,`agent_permissions` SELECT+INSERT (create_agent) | mixed (most tenant-scoped) | param→loaded (tenant_id arg, falls back to user.tenant_id) | REQUEST (HR tool, has user_id+tenant_id) | YES (users/agents/skills/llm_models) | tenant_scoped_session(tenant_id) — tenant_id arg available up-front |
| services/agent_tools.py:355 | `channel_configs` SELECT by agent_id (_agent_has_feishu) | agent-scoped (Stage-2) | NONE (agent_id only) | REQUEST (tool catalog assembly) | YES (after Stage 2) | ⚠️needs-review (Stage 2) |
| services/agent_tools.py:513 | `agents`+`tools`+`agent_tools` (list_agent_mcp_deferred_tools) | mixed (agents/tools tenant + agent_tools) | loaded-entity (agent_id only; app-code tenant filter via _tool_tenant_predicate) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tools.py:585 | `agents`+`tools`+`agent_tools` (get_agent_tools_for_llm) | mixed | loaded-entity (agent_id only) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tools.py:802 | ChannelDeliveryService.send_file → `channel_configs`,`users`,`chat_sessions`(+INSERT),`agents` | mixed (users/agents tenant + channel_configs/chat_sessions agent) | loaded-entity (agent_id only) | REQUEST (send_channel_file) | YES | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tools.py:855 | ChannelDeliveryService.send_text → same set as :802 | mixed | loaded-entity (agent_id only) | REQUEST (send_channel_message) | YES | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tool_domains/messaging.py:122 | `agents` SELECT (src + target by tenant) + `llm_models` (_resolve_target_agent_runtime) | tenant-scoped | loaded-entity (from_agent → tenant) | REQUEST (A2A delegation) | YES | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tool_domains/messaging.py:219 | `agents`,`org_members`/`agent_relationships`,`channel_configs` (feishu send-to-member) | mixed (agents/org_members tenant + channel_configs agent) | loaded-entity (agent_id only) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tool_domains/messaging.py:667 | `agents.tenant_id`,`users`(tenant-scoped app-side),`chat_sessions`(+INSERT),`chat_messages` (send to user) | mixed (users tenant + chat_sessions/chat_messages agent) | loaded-entity (agent_id → tenant) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tool_domains/messaging.py:773 | `chat_messages` INSERT (_persist_agent_tool_call) | agent-scoped (Stage-2) | NONE (agent_id only) | REQUEST | YES (after Stage 2) | ⚠️needs-review (Stage 2) |
| services/agent_tool_domains/messaging.py:884 | `agents` SELECT (src+target) + pair_session (`chat_sessions` write) + `chat_messages` (A2A message) | mixed (agents tenant + chat_sessions/messages agent) | loaded-entity (agent_id → tenant) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tool_domains/messaging.py:1061 | `chat_messages` INSERT (save A2A target reply) | agent-scoped (Stage-2) | loaded-entity (inside :884 ctx; agent loaded) | REQUEST | YES (after Stage 2) | ⚠️needs-review (Stage 2) |
| services/agent_tool_domains/web_mcp.py:186 | `tools` SELECT by **name only, NO tenant filter** (legacy tool-config fallback) | tenant-scoped | NONE (no agent/tenant at all) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review — see Finding #1 |
| services/agent_tool_domains/web_mcp.py:375 | `system_settings` SELECT (tavily_api_key) | global | N/A-global | REQUEST | NO | none |
| services/agent_tool_domains/web_mcp.py:984 | `tools` SELECT by **name only, NO tenant filter** + `agents` + `agent_tools` + mcp gating (_execute_mcp_tool) | mixed (tools/agents tenant) | loaded-entity (agent_id optional; Python-side tenant filter) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review — see Finding #1 |
| services/agent_tool_domains/web_mcp.py:1080 | `tools` SELECT name=="discover_resources" **NO tenant filter** (Smithery config) | tenant-scoped | NONE | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review — see Finding #1 |
| services/agent_tool_domains/web_mcp.py:1207 | `tools` SELECT by mcp_server_url **NO tenant filter** + `agent_tools` UPDATE by agent_id | mixed (tools tenant + agent_tools agent) | loaded-entity (agent_id) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review — see Finding #1 |
| services/agent_tool_domains/triggers.py:338 | `chat_messages` JOIN `chat_sessions` by agent_id (snapshot latest ts) | agent-scoped (Stage-2) | NONE (agent_id only) | REQUEST (set_trigger) | YES (after Stage 2) | ⚠️needs-review (Stage 2) |
| services/agent_tool_domains/triggers.py:368 | `agents` SELECT + `agent_triggers` count/INSERT by agent_id (set_trigger create) | mixed (agents tenant + agent_triggers agent) | loaded-entity (agent_id only) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tool_domains/triggers.py:526 | `agent_triggers` SELECT+UPDATE by agent_id+name (update_trigger) | agent-scoped (Stage-2) | NONE (agent_id only) | REQUEST | YES (after Stage 2) | ⚠️needs-review (Stage 2) |
| services/agent_tool_domains/triggers.py:633 | `agent_triggers` SELECT+UPDATE by agent_id+name (cancel_trigger) | agent-scoped (Stage-2) | NONE (agent_id only) | REQUEST | YES (after Stage 2) | ⚠️needs-review (Stage 2) |
| services/agent_tool_domains/triggers.py:669 | `agent_triggers` SELECT by agent_id + `agents` SELECT (list_triggers) | mixed (agent_triggers agent + agents tenant) | loaded-entity (agent_id only) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tool_domains/plaza.py:16 | `agents.agent_class` SELECT by id (_is_system_hr) | tenant-scoped | loaded-entity (agent_id only) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tool_domains/plaza.py:49 | `agents` SELECT + `plaza_posts`/`plaza_comments` by tenant_id (_plaza_get_new_posts) | tenant-scoped (agents + plaza_posts) | loaded-entity (agent → tenant) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tool_domains/plaza.py:102 | `agents` SELECT + `plaza_posts` INSERT (tenant_id=agent.tenant_id) (_plaza_create_post) | tenant-scoped | loaded-entity (agent → tenant) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tool_domains/plaza.py:151 | `agents` SELECT + `plaza_posts` SELECT + `plaza_comments` INSERT (_plaza_add_comment) | tenant-scoped | loaded-entity (agent → tenant) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tool_domains/feishu_users.py:115 | `agents.tenant_id` SELECT by id (resolve tenant for directory search) | tenant-scoped | loaded-entity (agent_id only) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tool_domains/feishu_users.py:130 | `org_members` SELECT (tenant filter only if `_tenant_id` truthy) | tenant-scoped | loaded-entity (from :115; may be None) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review — see Finding #2 |
| services/agent_tool_domains/feishu_users.py:164 | `users` SELECT (tenant filter only if `_tenant_id` truthy) | tenant-scoped | loaded-entity (from :115; may be None) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review — see Finding #2 |
| services/agent_tool_domains/tasks.py:36 | `tasks`/`task_logs` CRUD (SELECT+INSERT+UPDATE+DELETE) by agent_id (_manage_tasks; REST-only) | agent-scoped (Stage-2) | NONE (agent_id+user_id; no tenant_id col) | REQUEST (api/tasks.py) | YES (after Stage 2) | ⚠️needs-review (Stage 2) |
| services/agent_tool_domains/image_upload.py:37 | `tools` SELECT name=="upload_image" **NO tenant filter** + `agent_tools` by agent_id | mixed (tools tenant + agent_tools agent) | loaded-entity (agent_id) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review — see Finding #1 |
| services/agent_tool_domains/feishu_helpers.py:33 | `channel_configs` by agent_id + `agents` SELECT + `tenant_channel_configs` by agent.tenant_id (_get_feishu_app_credentials) | mixed (agents tenant + channel_configs agent + tenant_channel_configs not-in-set) | loaded-entity (agent → tenant) | REQUEST (all feishu tools) | YES (agents + channel_configs) | set_current_tenant+loop / ⚠️needs-review |
| services/agent_tool_domains/email.py:19 | `tools` SELECT name=="send_email" **NO tenant filter** + `agent_tools` by agent_id (_get_email_config) | mixed (tools tenant + agent_tools agent) | loaded-entity (agent_id) | REQUEST | YES | set_current_tenant+loop / ⚠️needs-review — see Finding #1 |

---

## Summary

### 1. Total call points: **48**

### 2. call_class distribution
- REQUEST: **46**
- MIXED (REQUEST + DAEMON): **2** — `tools/workspace.py:240`, `tools/workspace.py:320` (both via `ensure_workspace`, called from tool-resolve AND `heartbeat.py:1747`)
- DAEMON-only: 0
- SCRIPT / SEEDER: 0

### 3. fail_closed verdict (grep-verified against the rendered table)

- **fail_closed = YES: 45 of 48**
  - of which **35** fail-closed NOW (block touches a table already in the 9-ENABLE set), and
  - **10** fail-closed ONLY after Stage 2 (block touches ONLY agent-scoped tables).
- **fail_closed = NO: 3** — `web_mcp.py:375` (global `system_settings`); `governance_resolver.py:63` (`capability_policies`, tenant table but NOT in the named RLS set); `governance_resolver.py:67` (audit insert, NOT in named set). The latter two are still recommended `tenant_scoped_session` defensively in case Stage-2 policy coverage expands to those tables.

#### Split by which policy phase makes them fail-closed

**(a) Fail-closed NOW (block touches a 9-ENABLE-set table: agents/users/tools/skills/llm_models/plaza_posts/org_members) — 35 points** (each row tagged `| YES |` and not `(after Stage 2)`; many also touch a Stage-2 agent-scoped table in the same block):
- tools/governance_resolver.py:45 (agents) → set_current_tenant+loop
- tools/governance_resolver.py:80 (agents) → set_current_tenant+loop
- tools/governance_resolver.py:113 (tools+agent_tools) → set_current_tenant+loop
- tools/handlers/mcp.py:36 (tools) → set_current_tenant+loop
- tools/handlers/mcp.py:137 (tools) → set_current_tenant+loop
- tools/handlers/mcp.py:307 (tools) → set_current_tenant+loop
- tools/workspace.py:240 (agents) → set_current_tenant+loop [MIXED req/daemon]
- tools/handlers/subagent.py:52 (agents+llm_models) → set_current_tenant+loop
- tools/handlers/subagent.py:85 (llm_models) → tenant_scoped_session(tenant_id)
- tools/resolver.py:31 (agents) → set_current_tenant+loop — KEY BOOTSTRAP
- tools/handlers/hr.py:1167 (users/agents/skills/llm_models) → tenant_scoped_session(tenant_id)
- services/agent_tools.py:513 (agents/tools) → set_current_tenant+loop
- services/agent_tools.py:585 (agents/tools) → set_current_tenant+loop
- services/agent_tools.py:802 (users/agents + agent-scoped) → set_current_tenant+loop
- services/agent_tools.py:855 (users/agents + agent-scoped) → set_current_tenant+loop
- services/agent_tool_domains/messaging.py:122 (agents/llm_models) → set_current_tenant+loop
- services/agent_tool_domains/messaging.py:219 (agents/org_members) → set_current_tenant+loop
- services/agent_tool_domains/messaging.py:667 (users + agent-scoped) → set_current_tenant+loop
- services/agent_tool_domains/messaging.py:884 (agents + agent-scoped) → set_current_tenant+loop
- services/agent_tool_domains/web_mcp.py:186 (tools) → set_current_tenant+loop [Finding #1]
- services/agent_tool_domains/web_mcp.py:984 (tools/agents) → set_current_tenant+loop [Finding #1]
- services/agent_tool_domains/web_mcp.py:1080 (tools) → set_current_tenant+loop [Finding #1]
- services/agent_tool_domains/web_mcp.py:1207 (tools + agent_tools) → set_current_tenant+loop [Finding #1]
- services/agent_tool_domains/triggers.py:368 (agents + agent_triggers) → set_current_tenant+loop
- services/agent_tool_domains/triggers.py:669 (agents + agent_triggers) → set_current_tenant+loop
- services/agent_tool_domains/plaza.py:16 (agents) → set_current_tenant+loop
- services/agent_tool_domains/plaza.py:49 (agents/plaza_posts) → set_current_tenant+loop
- services/agent_tool_domains/plaza.py:102 (agents/plaza_posts) → set_current_tenant+loop
- services/agent_tool_domains/plaza.py:151 (agents/plaza_posts/plaza_comments) → set_current_tenant+loop
- services/agent_tool_domains/feishu_users.py:115 (agents) → set_current_tenant+loop
- services/agent_tool_domains/feishu_users.py:130 (org_members) → set_current_tenant+loop [Finding #2]
- services/agent_tool_domains/feishu_users.py:164 (users) → set_current_tenant+loop [Finding #2]
- services/agent_tool_domains/image_upload.py:37 (tools + agent_tools) → set_current_tenant+loop [Finding #1]
- services/agent_tool_domains/email.py:19 (tools + agent_tools) → set_current_tenant+loop [Finding #1]
- services/agent_tool_domains/feishu_helpers.py:33 (agents + channel_configs) → set_current_tenant+loop

> (The list immediately above is illustrative of the dominant sites; the authoritative set is "every `| YES |` table row not tagged `(after Stage 2)`" = 35.)

**(b) Fail-closed ONLY after Stage 2 (block touches ONLY agent-scoped tables: tasks/chat_sessions/chat_messages/agent_triggers/channel_configs — no ENABLE-set table in the block) — exactly 10 points (grep-verified):**
- tools/workspace.py:320 (tasks) [MIXED req/daemon]
- tools/handlers/tasks.py:36 (tasks)
- tools/handlers/tasks.py:59 (tasks+task_logs)
- services/agent_tools.py:355 (channel_configs)
- services/agent_tool_domains/messaging.py:773 (chat_messages)
- services/agent_tool_domains/messaging.py:1061 (chat_messages)
- services/agent_tool_domains/triggers.py:338 (chat_messages+chat_sessions)
- services/agent_tool_domains/triggers.py:526 (agent_triggers)
- services/agent_tool_domains/triggers.py:633 (agent_triggers)
- services/agent_tool_domains/tasks.py:36 (tasks CRUD)
> These cannot set a tenant GUC until Stage 2 adds the `tenant_id` column; all marked
> `⚠️needs-review (Stage 2)` — their `tenant_source` is **NONE** today (only agent_id available).

### 4. ⚠️UNSURE / needs-review / unknown

No `⚠️unknown` table_class (every model resolved). The `⚠️needs-review` migration_target falls into three buckets:

**Bucket A — tenant_source resolvable only by loading the agent first (chicken-and-egg)** — the dominant Group E shape: the with-block's FIRST query loads `agents`/`agents.tenant_id` by `agent_id`, and that very query fails-closed. You cannot `SET LOCAL app.current_tenant_id` before you know the tenant, and you can't read the tenant without the GUC. **Resolution pattern for the whole group:** resolve tenant_id via an `enter_rls_bypass`-wrapped narrow lookup (or a dedicated RLS-exempt `agent→tenant` accessor), THEN open the real work session with `set_current_tenant`. Affects every `loaded-entity (agent_id only)` row (≈25 points). This is a *group-wide design decision*, not per-site — flag for the accessor-migration phase (Stage 1).
  - The cleanest single chokepoint is **`tools/resolver.py:31`** — it is the canonical tenant resolver for the entire tool-execution context. If the migration gives `ToolExecutionContext` a GUC-bound session (or threads tenant_id into every domain helper), most downstream Group E helpers stop needing their own bare lookups.

**Bucket B — Stage-2 agent-scoped tables (tenant_source = NONE today)** — 11 points listed in 3(b). Cannot be migrated until the `tenant_id` column lands. needs-review = "blocked on Stage 2 schema."

**Bucket C — tenant_id param may be None** — `subagent.py:85` (`_resolve_model_override(model_name, tenant_id)`): if caller passes `tenant_id=None`, `tenant_scoped_session(None)` is undefined. Needs caller-contract review (is tenant_id ever None on this path?).

### 5. 意外发现 (tenant-isolation holes on the tool/skill resolution path)

**Finding #1 — `Tool` lookups by name with NO DB-level tenant filter (5 sites).** A recurring legacy pattern fetches tool config / MCP tool rows by `Tool.name == ...` (or `mcp_server_url`) with **no `tenant_id` predicate at all**, relying on Python-side `_tool_visible_to_agent_tenant()` (web_mcp) or just taking the first match (image_upload/email/governance legacy):
  - `web_mcp.py:186` (legacy tool-config fallback — `_resolve_tool_config`)
  - `web_mcp.py:984` (`_execute_mcp_tool` — Python-side filter only)
  - `web_mcp.py:1080` (`discover_resources` Smithery config — first match)
  - `web_mcp.py:1207` (re-auth config write — name/url match, AgentTool join scopes the *write* but not the *read*)
  - `image_upload.py:37` (`upload_image`), `email.py:19` (`send_email`) — "global config" by name, first match
  **Pre-cutover risk:** because `tools.name` is only unique per `(name, tenant_id)` (`uq_tools_name_tenant`), a name like `send_email`/`upload_image`/`discover_resources` existing in multiple tenants means these sites can read **another tenant's tool config (credentials/api_key)**. `image_upload.py:42` and `email.py:36` read `tool.config` private_key/api_key directly — a cross-tenant credential read.
  **Post-cutover (non-owner role):** RLS *closes* this as a side effect (bare session sees only `tenant_id IS NULL` rows), but that silently breaks the "global admin tool config" intent for any tenant-owned config row. So the fix is **not** just GUC: these sites need an explicit tenant predicate (or an intentional `tenant_id IS NULL` global-config contract) regardless. Flag to owner — this is a real isolation bug independent of the RLS migration.

**Finding #2 — feishu directory fuzzy-search degrades to cross-tenant when tenant resolution fails (`feishu_users.py:130,164`).** The tenant filter is guarded `if _tenant_id:`. `_tenant_id` comes from the agent lookup at :115; if that lookup returns None (or throws — it's wrapped in `try/except … logger.debug`), `_tenant_id` stays None and the `OrgMember` / `User` queries run **unscoped**, fuzzy-matching names across **all tenants** and returning feishu_user_id / open_id / email. Pre-cutover this is a directory-enumeration leak on a soft failure path. Post-cutover RLS makes the unscoped query return only NULL-tenant rows (fail-closed), masking it — but the silent `except` that produces the None should be hardened to fail-closed explicitly rather than rely on RLS.

**Finding #3 — `governance_resolver.py` silently defaults to `restricted` / proceeds on bare-session failure.** `_resolve_security_zone` (:45) catches all exceptions and returns `"restricted"`; after cutover the `agents` SELECT fails-closed → returns None → defaults to `restricted`. That is fail-*safe* for the security zone (tightest zone), so not a hole — but note the capability gate (`:63`) and approval (`:80`) sit on the same bare sessions: if those fail-close to "no policy / agent not found", `check_capability` behaviour on an empty result and `_request_approval` returning `{"allowed": False, "Agent not found"}` must be verified to fail *closed* (deny), not open. Governance is the one place where a fail-closed DB read must never be interpreted as "allow." Recommend an explicit assertion during Stage 1 migration that these three closures deny-on-empty.

**Finding #4 — `tools/resolver.py:31` swallows tenant-resolution failure → silent tenant_id=None cascade.** The bootstrap tenant resolver wraps its `agents.tenant_id` lookup in `try/except … logger.debug` and leaves `tenant_id = None` on failure. After cutover, if this fails-closed, the *entire* `ToolExecutionContext` runs with `tenant_id=None`, and every downstream governance/tool decision that keys off tenant_id silently degrades (capability gate, pack policies, tool visibility). This single node is the highest-leverage migration target in Group E and must NOT silently default to None post-cutover — it should hard-fail the invocation if tenant can't be resolved.

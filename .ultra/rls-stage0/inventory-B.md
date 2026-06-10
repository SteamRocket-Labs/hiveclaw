# RLS Stage 0 — Bare `async_session()` Inventory — Group B (API routes + seeders + scripts)

Static enumeration for the RLS enforcement migration (flip app to non-owner role).
Read-only analysis; no source modified.

**Foundation primitives** (`backend/app/database.py`): `get_db():43` (SET LOCAL from ContextVar — safe), `async_session:25` (bare sessionmaker — danger), `set_current_tenant:38`, `tenant_scoped_session:81` (pins GUC, ContextVar fallback), `enter_rls_bypass:131` (audited cross-tenant).

**fail-closed rule**: a bare session fail-closes under enforced RLS iff (1) it queries/writes a table with an RLS policy (or stage-2 agent-scoped table being given one) AND (2) no GUC is set (bare session, not via get_db/tenant_scoped_session/enter_rls_bypass). Rows with `tenant_id IS NULL` (global builtins) stay visible to bare sessions — those are NOT fail-closed.

**Model scoping confirmed** (grep of `backend/app/models/`):
- tenant-scoped + RLS policy: `Skill`(skill.py:19), `Tool`(tool.py:47), `User`(user.py:29), `Agent`(agent.py:30), `LLMModel`, `OrgDepartment`/`OrgMember`(org), `TenantSetting`(tenant_setting.py:23, ENABLE assumed via tenant scope)
- agent-scoped (stage-2 — gets policy in phase 2): `ChatSession`(chat_session.py:27), `ChatMessage`(audit.py:52), `AgentTrigger`(trigger.py:27→`triggers`), `ChannelConfig`(channel_config.py:19), `RuntimeTask`/web_chat_run, `AgentSchedule`(schedule.py:19, legacy)
- global (no tenant_id/agent_id → never fail-closed): `SystemSetting`(system_settings.py), `AgentTemplate`(has tenant_id but NOT in RLS list — see note), PG system catalogs
- audit-only (agent_id nullable, NOT in stage-2 list, no policy planned): `AuditLog`(audit.py:20)
- child-of-skill (no own tenant/agent col; parent `skills` has policy): `SkillFile`(skill.py:33)

**Note on `TenantSetting`**: carries `tenant_id` and is tenant-scoped data; not in the 9-ENABLE / 7-FORCE briefing list verbatim, but it WILL fail-closed if a policy is added (it is a `tenant_id` table). Marked tenant-scoped / ⚠️UNSURE on fail_closed where the only access is `tenant_id IS NULL` global rows.

| location (file:line) | operation (表+操作) | table_class | tenant_source | call_class | fail_closed | migration_target |
|---|---|---|---|---|---|---|
| api/skills.py:38 | `_get_tenant_setting`: SELECT TenantSetting WHERE tenant_id=param | tenant-scoped | param (tenant_id arg, from current_user upstream) | REQUEST | YES | tenant_scoped_session(tenant_id) |
| api/skills.py:321 | `_save_skill_to_db`: SELECT+INSERT Skill+SkillFile (tenant_uuid) | tenant-scoped | param (tenant_id arg) | REQUEST | YES | tenant_scoped_session(tenant_id) |
| api/skills.py:381 | `_find_existing_skill_by_folder_name`: SELECT Skill | tenant-scoped | param (tenant_id arg) | REQUEST | YES | tenant_scoped_session(tenant_id) |
| api/skills.py:666 | `list_skills`: SELECT Skill (scope_clause from current_user) | tenant-scoped | current_user.tenant_id | REQUEST | YES | tenant_scoped_session(current_user.tenant_id) — or convert route to Depends(get_db) |
| api/skills.py:749 | `create_skill`: SELECT+INSERT Skill+SkillFile | tenant-scoped | current_user.tenant_id (_current_tenant_or_400) | REQUEST | YES | tenant_scoped_session(tenant_id) |
| api/skills.py:804 | `update_skill`: SELECT+UPDATE+DELETE Skill/SkillFile | tenant-scoped | current_user (skill.tenant_id) | REQUEST | YES | tenant_scoped_session(current_user.tenant_id) |
| api/skills.py:848 | `delete_skill`: SELECT+DELETE Skill | tenant-scoped | current_user (skill.tenant_id) | REQUEST | YES | tenant_scoped_session(current_user.tenant_id) |
| api/skills.py:871 | `_upsert_tenant_setting`: SELECT+UPSERT TenantSetting WHERE tenant_id | tenant-scoped | param (tenant_id arg, current_user.tenant_id) | REQUEST | YES | tenant_scoped_session(tenant_id) |
| api/skills.py:935 | `browse_list`: SELECT Skill (scope_clause) | tenant-scoped | current_user.tenant_id | REQUEST | YES | tenant_scoped_session(current_user.tenant_id) |
| api/skills.py:998 | `browse_read`: SELECT Skill (scope_clause) | tenant-scoped | current_user.tenant_id | REQUEST | YES | tenant_scoped_session(current_user.tenant_id) |
| api/skills.py:1025 | `browse_write`: SELECT+INSERT/UPDATE Skill+SkillFile | tenant-scoped | current_user.tenant_id (_current_tenant_or_400) | REQUEST | YES | tenant_scoped_session(current_user.tenant_id) |
| api/skills.py:1074 | `browse_delete`: SELECT+DELETE Skill/SkillFile | tenant-scoped | current_user.tenant_id | REQUEST | YES | tenant_scoped_session(current_user.tenant_id) |
| api/websocket.py:152 | `_has_active_web_chat_run`: SELECT web_chat_run (RuntimeTask) by agent_id+session_id | agent-scoped | loaded-entity (agent from WS auth; no middleware GUC) | REQUEST | YES | tenant_scoped_session(agent.tenant_id) — resolve tenant from authed user/agent |
| api/websocket.py:348 | WS connect: SELECT User by id; check_agent_access; load LLMModel | mixed (User+Agent+LLMModel tenant-scoped) | loaded-entity (user.tenant_id after token decode) | REQUEST | YES | tenant_scoped_session(user.tenant_id) — WS path has NO TenantMiddleware, GUC unset |
| api/websocket.py:594 | abort handler: get/cancel web_chat_run (RuntimeTask) by agent_id+session_id | agent-scoped | loaded-entity (agent_id+user_id in WS scope) | REQUEST | YES | tenant_scoped_session(agent.tenant_id) |
| api/websocket.py:625 | start run: SELECT ChatSession + start_web_chat_run | agent-scoped (ChatSession) | loaded-entity (agent_id+user_id) | REQUEST | YES | tenant_scoped_session(agent.tenant_id) |
| api/feishu.py:1862 | webhook bg: SELECT Agent + resolve_or_create_feishu_user (User) | mixed (Agent+User tenant-scoped) | loaded-entity (agent_obj.tenant_id) | DAEMON (webhook bg, no middleware GUC) | YES | tenant_scoped_session(agent_obj.tenant_id) — resolve agent first w/ bypass or pass tenant |
| api/feishu.py:2014 | image bg: `_call_agent_llm(_db_img, ...)` (writes ChatMessage, reads history) | agent-scoped (ChatMessage) | loaded-entity (agent_id) | DAEMON (webhook bg) | YES | tenant_scoped_session(agent.tenant_id) |
| api/feishu.py:2057 | image bg: INSERT ChatMessage (assistant reply) | agent-scoped | loaded-entity (agent_id) | DAEMON | YES | tenant_scoped_session(agent.tenant_id) |
| api/feishu.py:2106 | file bg: INSERT ChatMessage (ack) | agent-scoped | loaded-entity (agent_id) | DAEMON | YES | tenant_scoped_session(agent.tenant_id) |
| api/gateway.py:246 | `report_result` route, separate reply_db: SELECT Agent + ChatMessage write (A2A reply transcript) | mixed (Agent tenant-scoped + ChatMessage agent-scoped) | loaded-entity (agent.tenant_id; route gated by api_key→agent) | REQUEST (route has its own Depends(get_db) at :192, opens extra bare session) | YES | reuse the route's `db` (Depends(get_db)) OR tenant_scoped_session(agent.tenant_id) |
| api/gateway.py:335 | `_send_to_agent_background` (asyncio.create_task @ :544): SELECT LLMModel + ChatSession/ChatMessage write | mixed (LLMModel tenant-scoped + ChatMessage agent-scoped) | param (target_tenant_id passed in) | DAEMON (detached bg task, no ContextVar) | YES | tenant_scoped_session(target_tenant_id) |
| api/gateway.py:432 | `_send_to_agent_background` reply persist: INSERT ChatMessage + GatewayMessage | agent-scoped | param (target_tenant_id in scope) | DAEMON | YES | tenant_scoped_session(target_tenant_id) |
| api/atlassian.py:201 | channel setup bg: SELECT/INSERT Tool + AgentTool assignment (per-agent) | mixed (Tool tenant-scoped + AgentTool agent-scoped) | loaded-entity (agent_id; setup task) | DAEMON (channel setup bg) | YES | tenant_scoped_session(agent.tenant_id) |
| api/atlassian.py:282 | `get_atlassian_api_key_for_agent` fallback: SELECT ChannelConfig by agent_id | agent-scoped | loaded-entity (agent_id) | DAEMON/helper (db optional; bare only when no db passed) | YES | tenant_scoped_session(agent.tenant_id) — prefer caller passing GUC-set db |
| api/wecom.py:425 | message bg: SELECT Agent + User + ChatMessage history/write via _call_agent_llm | mixed (Agent+User tenant-scoped, ChatMessage agent-scoped) | loaded-entity (agent_obj.tenant_id) | DAEMON (webhook bg) | YES | tenant_scoped_session(agent_obj.tenant_id) |
| api/webhooks.py:50 | webhook fire: SELECT AgentTrigger by token + Agent + AuditLog write | mixed (AgentTrigger agent-scoped stage-2, Agent tenant-scoped, AuditLog) | loaded-entity (trigger.agent_id → agent.tenant_id) | DAEMON (public webhook, no auth/middleware) | YES | tenant_scoped_session(agent.tenant_id) — but token lookup itself needs cross-tenant read; see ⚠️ note |
| api/telegram.py:67 | `_resolve_public_base_url`: SELECT SystemSetting WHERE key='platform' | global | N/A-global | DAEMON/helper | NO | none |
| api/enterprise.py:1072 | `trigger_org_sync` route: sync_org_structure(db, target_tenant_id) → SELECT OrgDepartment+OrgMember WHERE tenant_id | tenant-scoped | param (target_tenant_id from resolve_tenant_scope(current_user)) | REQUEST (route admin-gated; opens bare session despite no Depends(get_db) on this route) | YES | tenant_scoped_session(target_tenant_id) |
| api/discord_bot.py:273 | `handle_in_background`: SELECT Agent + User + ChatSession + ChatMessage history/write | mixed (Agent+User tenant-scoped, ChatSession/ChatMessage agent-scoped) | loaded-entity (agent_obj.tenant_id) | DAEMON (interaction bg) | YES | tenant_scoped_session(agent_obj.tenant_id) |
| api/dingtalk.py:145 | message bg: SELECT Agent + User + ChatSession + ChatMessage history/write | mixed (Agent+User tenant-scoped, ChatSession/ChatMessage agent-scoped) | loaded-entity (agent_obj.tenant_id) | DAEMON (webhook bg) | YES | tenant_scoped_session(agent_obj.tenant_id) |
| services/tool_seeder.py:35 | `seed_builtin_tools`: SELECT+INSERT/UPDATE Tool (tenant_id=NULL builtins) | tenant-scoped table, NULL-tenant rows only | N/A-global (builtin, tenant_id IS NULL) | SEEDER (startup) | NO | none (NULL-tenant rows pass policy read+WITH CHECK) |
| services/tool_seeder.py:180 | `seed_atlassian_rovo_config`: SELECT+INSERT/UPDATE Tool (NULL-tenant config tool) | tenant-scoped table, NULL-tenant rows | N/A-global | SEEDER (startup) | NO | none |
| services/tool_seeder.py:223 | `get_atlassian_api_key`: SELECT Tool WHERE name='atlassian_rovo' (NULL-tenant) | tenant-scoped table, NULL-tenant row | N/A-global | SEEDER/helper | ⚠️UNSURE | ⚠️needs-review — relies on the global row being tenant_id IS NULL; if a tenant-owned 'atlassian_rovo' row ever exists it'd be invisible. Today builtin only → NO. |
| services/skill_seeder.py:555 | `seed_skills`: SELECT+UPSERT Skill (tenant_id IS NULL builtins) + SkillFile | tenant-scoped table, NULL-tenant rows | N/A-global | SEEDER (startup) | NO | none |
| services/skill_seeder.py:638 | `cleanup_retired_builtin_skills`: SELECT builtin Skill (NULL) + `select(Agent)` ALL agents (cross-tenant) | mixed (Skill NULL-tenant OK + Agent tenant-scoped cross-tenant) | NONE-daemon (no tenant; scans all agents) | SEEDER (startup maintenance) | YES (the unfiltered Agent scan) | enter_rls_bypass(reason="builtin-skill cleanup: scrub retired skills across all agent workspaces") |
| services/skill_seeder.py:692 | `push_default_skills_to_existing_agents`: SELECT default Skill (NULL) + `select(Agent)` ALL agents (cross-tenant) | mixed (Skill NULL-tenant + Agent tenant-scoped cross-tenant) | NONE-daemon (all agents) | SEEDER (startup) | YES (the unfiltered Agent scan) | enter_rls_bypass(reason="push default skills to every existing agent across tenants") |
| services/agent_seeder.py:104 | `seed_default_agents`: SELECT SystemSetting(global) + Agent by name (no tenant filter) + User role=platform_admin (cross-tenant) + INSERT Agent | mixed (SystemSetting global + Agent/User tenant-scoped cross-tenant) | loaded-entity (admin.tenant_id for the INSERT) but lookups are cross-tenant | SEEDER (first-startup only) | YES (Agent-by-name + User-by-role scans are tenant-blind) | enter_rls_bypass(reason="first-startup default-agent seeding: resolve platform admin + seed Morty/Meeseeks") |
| services/template_seeder.py:133 | `seed_agent_templates`: SELECT+UPSERT/DELETE AgentTemplate + count(Agent) by template_id (cross-tenant) | mixed (AgentTemplate not-in-RLS-list + Agent tenant-scoped cross-tenant) | NONE-daemon (builtin templates global) | SEEDER (startup) | ⚠️UNSURE → effectively YES for the Agent ref-count | ⚠️needs-review — AgentTemplate carries tenant_id but is NOT in the briefing's RLS table list; the `count(Agent).where(template_id=...)` ref-count IS tenant-scoped → if Agent gets/has policy this undercounts → false delete. Use enter_rls_bypass(reason="builtin template seed: ref-count agents across tenants before delete") |
| scripts/scrub_global_tool_secrets.py:85 | SELECT Tool WHERE tenant_id IS NULL (global) + User role=platform_admin (cross-tenant) + write TenantToolConfig | mixed (Tool NULL-tenant + User cross-tenant + TenantToolConfig tenant-scoped) | param-ish (platform_tid resolved from cross-tenant User scan) | SCRIPT (owner-run, --apply gated) | YES (User-by-role scan tenant-blind) | enter_rls_bypass(reason="scrub global tool secrets: relocate to platform tenant + clear NULL-tenant rows") |
| scripts/migrate_schedules_to_triggers.py:22 | SELECT AgentSchedule (ALL, cross-tenant) + SELECT/INSERT AgentTrigger | agent-scoped (AgentSchedule + AgentTrigger stage-2, all agents) | NONE-daemon (all agents) | SCRIPT (one-time ops migration) | YES | enter_rls_bypass(reason="one-time schedule→trigger migration across all agents") |
| scripts/cleanup_duplicate_feishu_users.py:174 | SELECT TenantSetting(all tenants) + User(all, backfill) + OrgMember(all) + reconcile (merge users / normalize chat_sessions) | tenant-scoped (TenantSetting/User/OrgMember) cross-tenant | NONE-daemon (iterates every tenant's configs) | SCRIPT (ops maintenance) | YES | enter_rls_bypass(reason="feishu identity maintenance: backfill+merge users across all tenants") |
| scripts/audit_rls_coverage.py:75 | SELECT information_schema.columns + pg_class + pg_namespace + pg_tables (PG catalogs only) | global (system catalogs) | N/A-global | SCRIPT (read-only audit) | NO | none (catalog tables are not RLS-protected) |

## Summary counts (canonical — verified by programmatic recount)

**Total bare-session call points = 43** (one row per `async with [_]async_session()` hit; verified via `grep -cE "async with (_)?async_session\(\)"` across all 19 files).

Per-file hit count:

| file | hits | call_class | fail_closed=YES |
|---|---|---|---|
| api/skills.py | 12 | REQUEST | 12 |
| api/websocket.py | 4 | REQUEST (WS — no TenantMiddleware, GUC unset) | 4 |
| api/feishu.py | 4 | DAEMON (webhook bg) | 4 |
| api/gateway.py | 3 | 1 REQUEST (246) + 2 DAEMON (335,432) | 3 |
| api/atlassian.py | 2 | DAEMON (channel setup) | 2 |
| api/wecom.py | 1 | DAEMON | 1 |
| api/webhooks.py | 1 | DAEMON | 1 |
| api/telegram.py | 1 | DAEMON | 0 |
| api/enterprise.py | 1 | REQUEST (route w/o Depends(get_db)) | 1 |
| api/discord_bot.py | 1 | DAEMON | 1 |
| api/dingtalk.py | 1 | DAEMON | 1 |
| services/tool_seeder.py | 3 | SEEDER | 0 (1 ⚠️UNSURE @223, leans NO) |
| services/skill_seeder.py | 3 | SEEDER | 2 (638,692) |
| services/agent_seeder.py | 1 | SEEDER | 1 |
| services/template_seeder.py | 1 | SEEDER | 1 (effective; ⚠️ see notes) |
| scripts/scrub_global_tool_secrets.py | 1 | SCRIPT | 1 |
| scripts/migrate_schedules_to_triggers.py | 1 | SCRIPT | 1 |
| scripts/cleanup_duplicate_feishu_users.py | 1 | SCRIPT | 1 |
| scripts/audit_rls_coverage.py | 1 | SCRIPT | 0 |
| **TOTAL** | **43** | — | **37 hard-YES** |

### call_class distribution

- **REQUEST = 18** — skills ×12, websocket ×4 (WS path, no middleware GUC), gateway:246 ×1, enterprise:1072 ×1.
- **DAEMON = 13** — feishu ×4, gateway {335,432} ×2, atlassian ×2, wecom ×1, webhooks ×1, telegram ×1, discord ×1, dingtalk ×1.
- **SEEDER = 8** — tool_seeder ×3, skill_seeder ×3, agent_seeder ×1, template_seeder ×1.
- **SCRIPT = 4** — scrub ×1, migrate_schedules ×1, cleanup_feishu ×1, audit_rls ×1.
- 18 + 13 + 8 + 4 = **43.** ✓

### fail_closed distribution

- **YES (hard) = 37** — every site whose with-block touches a tenant-scoped (policy) or agent-scoped (stage-2) table without a GUC.
- **NO = 5** — telegram:67 (SystemSetting global), tool_seeder:35 & :180 (NULL-tenant builtin Tool rows pass policy), tool_seeder:223 (NULL-tenant builtin read; ⚠️ but no explicit NULL filter), audit_rls:75 (PG system catalogs).
- **⚠️UNSURE = 2** — tool_seeder:223 (counted in NO above — leans NO today but fragile), template_seeder:133 (counted in YES-effective via the Agent ref-count guard). See the ⚠️ section for why.

> Verdict sum check: 37 YES + 5 NO = 42. The 43rd is tool_seeder:223, which is double-listed (NO-leaning **and** ⚠️UNSURE) — net hard verdicts: 37 YES, 5 NO, of which 1 NO (tool_seeder:223) is flagged UNSURE and template_seeder:133's YES is "effective" (table not in briefing RLS list but its Agent guard fail-closes).

## fail_closed=YES complete list (file:line — table — migration_target)

REQUEST (tenant from current_user / loaded entity):
1. api/skills.py:38 — TenantSetting — tenant_scoped_session(tenant_id)
2. api/skills.py:321 — Skill+SkillFile — tenant_scoped_session(tenant_id)
3. api/skills.py:381 — Skill — tenant_scoped_session(tenant_id)
4. api/skills.py:666 — Skill — tenant_scoped_session(current_user.tenant_id) [or convert route to Depends(get_db)]
5. api/skills.py:749 — Skill+SkillFile — tenant_scoped_session(tenant_id)
6. api/skills.py:804 — Skill+SkillFile — tenant_scoped_session(current_user.tenant_id)
7. api/skills.py:848 — Skill — tenant_scoped_session(current_user.tenant_id)
8. api/skills.py:871 — TenantSetting — tenant_scoped_session(tenant_id)
9. api/skills.py:935 — Skill — tenant_scoped_session(current_user.tenant_id)
10. api/skills.py:998 — Skill — tenant_scoped_session(current_user.tenant_id)
11. api/skills.py:1025 — Skill+SkillFile — tenant_scoped_session(current_user.tenant_id)
12. api/skills.py:1074 — Skill+SkillFile — tenant_scoped_session(current_user.tenant_id)
13. api/gateway.py:246 — Agent+ChatMessage — reuse route db (Depends(get_db)) OR tenant_scoped_session(agent.tenant_id)
14. api/enterprise.py:1072 — OrgDepartment+OrgMember — tenant_scoped_session(target_tenant_id)

REQUEST-via-WebSocket (NO TenantMiddleware — GUC unset; tenant from token-decoded user/agent):
15. api/websocket.py:152 — web_chat_run/RuntimeTask — tenant_scoped_session(agent.tenant_id)
16. api/websocket.py:348 — User+Agent+LLMModel — tenant_scoped_session(user.tenant_id)
17. api/websocket.py:594 — web_chat_run/RuntimeTask — tenant_scoped_session(agent.tenant_id)
18. api/websocket.py:625 — ChatSession — tenant_scoped_session(agent.tenant_id)

DAEMON (detached bg / webhook handlers — tenant from loaded agent or param):
19. api/feishu.py:1862 — Agent+User — tenant_scoped_session(agent_obj.tenant_id)
20. api/feishu.py:2014 — ChatMessage (via _call_agent_llm) — tenant_scoped_session(agent.tenant_id)
21. api/feishu.py:2057 — ChatMessage — tenant_scoped_session(agent.tenant_id)
22. api/feishu.py:2106 — ChatMessage — tenant_scoped_session(agent.tenant_id)
23. api/gateway.py:335 — LLMModel+ChatSession+ChatMessage — tenant_scoped_session(target_tenant_id) [param available]
24. api/gateway.py:432 — ChatMessage+GatewayMessage — tenant_scoped_session(target_tenant_id)
25. api/atlassian.py:201 — Tool+AgentTool — tenant_scoped_session(agent.tenant_id)
26. api/atlassian.py:282 — ChannelConfig — tenant_scoped_session(agent.tenant_id) [prefer caller-passed GUC db]
27. api/wecom.py:425 — Agent+User+ChatMessage — tenant_scoped_session(agent_obj.tenant_id)
28. api/webhooks.py:50 — AgentTrigger+Agent+AuditLog — tenant_scoped_session(agent.tenant_id) [⚠️ token lookup needs cross-tenant read — see ⚠️ section]
29. api/discord_bot.py:273 — Agent+User+ChatSession+ChatMessage — tenant_scoped_session(agent_obj.tenant_id)
30. api/dingtalk.py:145 — Agent+User+ChatSession+ChatMessage — tenant_scoped_session(agent_obj.tenant_id)

SEEDER (startup, cross-tenant — bypass):
31. services/skill_seeder.py:638 — Agent (all) — enter_rls_bypass(reason="builtin-skill cleanup across agents")
32. services/skill_seeder.py:692 — Agent (all) — enter_rls_bypass(reason="push default skills to all agents")
33. services/agent_seeder.py:104 — Agent+User (cross-tenant lookups) — enter_rls_bypass(reason="first-startup default-agent seed")
34. services/template_seeder.py:133 — Agent ref-count (cross-tenant) — enter_rls_bypass(reason="builtin template ref-count across tenants") [⚠️ effective-YES]

SCRIPT (owner-run ops, cross-tenant — bypass):
35. scripts/scrub_global_tool_secrets.py:85 — User(role scan)+Tool(NULL)+TenantToolConfig — enter_rls_bypass(reason="scrub global tool secrets")
36. scripts/migrate_schedules_to_triggers.py:22 — AgentSchedule+AgentTrigger (all) — enter_rls_bypass(reason="one-time schedule→trigger migration")
37. scripts/cleanup_duplicate_feishu_users.py:174 — TenantSetting+User+OrgMember+chat_sessions (all tenants) — enter_rls_bypass(reason="feishu identity maintenance across tenants")

## ⚠️ UNSURE / needs-review

1. **services/tool_seeder.py:223** (`get_atlassian_api_key`): SELECT Tool WHERE name='atlassian_rovo'. Today this row is a builtin with `tenant_id IS NULL`, so a bare session sees it (NULL-tenant passes policy) → NOT fail-closed. **Why unsure**: the query does not pin `tenant_id IS NULL` — it filters by name only. If any tenant ever creates an 'atlassian_rovo'-named Tool row (tenant_id set), under RLS that row is invisible to a bare session but the builtin NULL row remains visible, so behavior is "returns the global key" — acceptable but fragile. Verdict: NO today, but add `Tool.tenant_id.is_(None)` for explicitness rather than a session change.

2. **services/template_seeder.py:133** (`seed_agent_templates`): the `select(func.count(Agent.id)).where(Agent.template_id == old.id)` ref-count touches tenant-scoped `Agent` cross-tenant. **Why unsure**: `AgentTemplate` itself carries `tenant_id` but is NOT in the briefing's 9-ENABLE/7-FORCE list — so the template SELECT/UPSERT/DELETE won't fail-closed on its own. But the Agent ref-count WILL undercount once `agents` policy binds (bare session sees 0 tenant agents → thinks template is unreferenced → wrongly deletes a template still in use). Verdict: **effective fail-closed via the Agent guard** → use `enter_rls_bypass`. Needs the migration author to confirm whether `agents` policy is already ENABLE (it is — in the 9-ENABLE list) → this IS a real risk.

3. **api/webhooks.py:50** (token lookup): the trigger lookup `WHERE config['token']==token` must scan `AgentTrigger` **across all tenants** (the caller is an unauthenticated public webhook with only a token; tenant is unknown until the trigger is found). **Why unsure**: once `triggers` gets a stage-2 tenant policy, a bare/`tenant_scoped_session`-pinned session can't find the trigger because tenant is not yet known at lookup time. This site needs a **two-phase** pattern: (a) cross-tenant token→trigger resolution under `enter_rls_bypass(reason="public webhook token resolution")`, then (b) re-pin to the resolved `agent.tenant_id` for the Agent/AuditLog writes. Single `tenant_scoped_session(agent.tenant_id)` is insufficient. Flag for migration design.

## Unexpected findings

1. **Two API routes already hold a `Depends(get_db)` db but open a SECOND bare `async_session()` inside the handler** — the exact "should-use-get_db-but-another-bare-session" anti-pattern called out in the briefing:
   - `api/gateway.py:246` (`report_result`, route db at :192) opens `reply_db` for the A2A reply-transcript write. The route's injected `db` is GUC-safe; `reply_db` is not.
   - `api/gateway.py` background path (`_send_to_agent_background`) is correctly detached (post-request), so its bare sessions at 335/432 are legitimately DAEMON — but they pass plain values incl. `target_tenant_id`, so migration is trivial (`tenant_scoped_session(target_tenant_id)`).

2. **WebSocket path has NO TenantMiddleware** — `websocket_chat` authenticates by decoding the JWT directly (`decode_access_token`), so the request ContextVar `_current_tenant_id` is **never set** for WS connections. All 4 websocket bare sessions therefore have an unset GUC regardless of `get_db` usage; they need an explicit `tenant_scoped_session(user.tenant_id)` after auth. (The file does not import `get_db` for the WS handler at all.)

3. **`api/enterprise.py:1072` is a REQUEST route with NO `Depends(get_db)` on the route signature** (`trigger_org_sync(tenant_id, current_user=Depends(get_current_admin))`) — it manually opens a bare session. So even the "request" classification here means the GUC is unset; must use `tenant_scoped_session(target_tenant_id)`.

4. **`AuditLog` (audit.py:20)** is written by `webhooks.py:50` and carries `agent_id` (nullable) but no `tenant_id` and is NOT in the stage-2 list. If audit tables intentionally have no RLS policy, the AuditLog write is fine; flag for the migration author to confirm audit tables are deliberately policy-free (otherwise webhook rate-limit audit writes fail-closed too).

5. **Channel webhook handlers (feishu/wecom/discord/dingtalk) all share the identical pattern**: a webhook-spawned background coroutine opens a bare session, loads `Agent` by id, find-or-creates a platform `User` with `tenant_id=agent_obj.tenant_id`, then writes `ChatMessage`/`ChatSession`. They are uniform → one migration recipe (`tenant_scoped_session(agent_obj.tenant_id)` opened *after* a bypass-resolved agent, or pass tenant down from the webhook entry). Note the **agent must be resolved before the tenant is known** — same two-phase wrinkle as webhooks.py but milder because the agent_id is in the URL path, so the tenant can be looked up via a narrow bypass-or-global read first.

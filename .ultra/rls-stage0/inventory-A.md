# RLS Stage 0 Inventory — Group A (Daemon / Stream, no request context)

**Scope:** Static exhaustive analysis of every bare `async_session()` call site in the
11 Group-A files. Read-only; no source modified.

**Contract recap (from `app/database.py`):**
- `async_session` = bare sessionmaker (line 25). A bare `async with async_session()` **never** runs
  `SET LOCAL app.current_tenant_id` → GUC is empty `''`.
- `get_db()` (43) sets GUC from ContextVar → safe (REQUEST path only).
- `tenant_scoped_session(tid)` (81) pins GUC to `tid` (ContextVar fallback when omitted).
- `set_current_tenant(tid)` (38) sets the ContextVar (daemon loop pattern).
- `enter_rls_bypass(session, reason=)` (131) audited cross-tenant `BYPASS`.

**Policy shape** → bare session (GUC `''`) sees only `tenant_id IS NULL` rows. For tenant/agent-scoped
tables every real row is invisible (select → 0 rows; insert/update → no rows matched / RLS violation)
= **fail-closed**.

**Model→table tenant classification (verified against `app/models/`):**
| Model | Table | tenant_id? | agent_id? | Class | Policy timing |
|-------|-------|-----------|-----------|-------|---------------|
| `Agent` | agents | ✅ | — | tenant-scoped | LIVE (Stage-0) |
| `AgentTrigger` | triggers | ❌ | ✅ | agent-scoped | Stage-2 |
| `ChatSession` | chat_sessions | ❌ | ✅ | agent-scoped | Stage-2 |
| `ChatMessage` | chat_messages (audit.py) | ❌ | ✅ | agent-scoped | Stage-2 |
| `ChannelConfig` | channel_configs | ❌ | ✅ | agent-scoped | Stage-2 |
| `PendingReplyContext` | pending_reply_contexts | ❌ | ✅ | agent-scoped | Stage-2 |
| `Participant` | participants | ❌ | ❌ | global (no scope col) | none |
| `LLMModel` | llm_models | ✅ | — | tenant-scoped | LIVE (Stage-0) |

> Caveat: `chat_sessions / chat_messages / triggers / channel_configs / pending_reply_contexts` have
> **no `tenant_id` column today** → they are the Stage-2 "agent-scoped table" cohort. They are
> **fail-closed only AFTER Stage 2 adds the column + policy**, not at the Stage-3 role flip alone.
> Marked `fail_closed=YES (Stage-2)` below to keep the migration-target columns honest.

---

## Full inventory table

| location (file:line) | operation | table_class | tenant_source | call_class | fail_closed | migration_target |
|----------------------|-----------|-------------|---------------|------------|-------------|------------------|
| trigger_daemon.py:201 | ChatSession select + AgentTrigger update | agent-scoped | param `agent_id` (no tenant loaded) | DAEMON | YES (Stage-2) | ⚠️needs-review — resolve agent.tenant_id → `tenant_scoped_session(tid)` |
| trigger_daemon.py:254 | AgentTrigger select + ChatSession select + AgentTrigger update (backfill) | agent-scoped | NONE (global scan: all enabled triggers, no tenant filter) | DAEMON (startup job) | YES (Stage-2) | enter_rls_bypass("trigger reply_context backfill") — iterates all tenants' triggers |
| trigger_daemon.py:524 | AgentTrigger update (persist `_last_value`) | agent-scoped | loaded-entity `trigger.id` (single trigger, tenant not loaded) | DAEMON | YES (Stage-2) | ⚠️needs-review — resolve trigger.agent→tenant → `tenant_scoped_session(tid)` |
| trigger_daemon.py:587 | Agent + ChatMessage + Participant + User select (new-message match) | mixed (Agent tenant-scoped LIVE; ChatMessage agent-scoped) | loaded-entity (agent loaded at :614, tenant read at :616) | DAEMON | YES (Stage-0 for Agent; Stage-2 for ChatMessage) | ⚠️needs-review — Agent row itself invisible w/o GUC; must `tenant_scoped_session(tid)` but tid only known AFTER loading agent → chicken-egg, see Finding #4 |
| trigger_daemon.py:810 | Agent select + select_trigger_model + evaluate_trigger_preflight | tenant-scoped (Agent) + downstream | param `agent_id` | DAEMON | YES (Stage-0) | ⚠️needs-review — Agent invisible w/o GUC; tid not yet known. Resolve via outer loop or bypass-read agent then re-scope |
| trigger_daemon.py:836 | AgentTrigger select + update (reset failure policy) | agent-scoped | loaded-entity `trigger_id` list (tenant not loaded) | DAEMON | YES (Stage-2) | ⚠️needs-review — `tenant_scoped_session(tid)`; tid must come from caller (agent of these triggers) |
| trigger_daemon.py:851 | AgentTrigger select + update (apply failure policy) | agent-scoped | param `agent_id` | DAEMON | YES (Stage-2) | `tenant_scoped_session(tid)` — resolve agent.tenant_id (agent_id is in scope) |
| trigger_daemon.py:1085 | Agent select + Participant select + ChatSession/ChatMessage insert (wake invocation) | mixed (Agent LIVE + ChatSession/ChatMessage Stage-2 + Participant global) | loaded-entity (Agent loaded at :1087) | DAEMON | YES (Stage-0 Agent + Stage-2 chat) | ⚠️needs-review — same chicken-egg: Agent select itself fail-closed. Pattern: bypass-read or per-loop set_current_tenant before this block |
| trigger_daemon.py:1190 | ChatMessage insert (tool_call persistence callback) | agent-scoped | closure `agent_id` (tenant in outer scope) | DAEMON | YES (Stage-2) | `tenant_scoped_session(tid)` — tid from enclosing wake context |
| trigger_daemon.py:1289 | Participant select + ChatMessage insert (assistant reply) | mixed (Participant global + ChatMessage Stage-2) | closure `agent_id` (agent loaded earlier in fn) | DAEMON | YES (Stage-2) | `tenant_scoped_session(tid)` — tid from enclosing wake context |
| trigger_daemon.py:1457 | AgentTrigger select (ALL enabled, tick scan) | agent-scoped | NONE (global cross-tenant enumeration) | DAEMON | YES (Stage-2) | enter_rls_bypass("trigger daemon tick — enumerate all enabled triggers") |
| trigger_daemon.py:1471 | AgentTrigger select-by-id + update (auto-disable expired) | agent-scoped | loaded-entity `trigger.id` (from global tick list) | DAEMON | YES (Stage-2) | ⚠️needs-review — per-trigger; `tenant_scoped_session(trigger.agent.tenant_id)` or fold into bypass tick |
| trigger_daemon.py:1516 | AgentTrigger select-by-id + update (pre-update fire state) | agent-scoped | loop var `agent_id` (fired_by_group key) | DAEMON | YES (Stage-2) | `tenant_scoped_session(tid)` — resolve agent.tenant_id (agent_id is the loop key) |
| heartbeat.py:1265 | Agent select + update (touch last_heartbeat) | tenant-scoped | param `agent_id` | DAEMON | YES (Stage-0) | ⚠️needs-review — Agent row fail-closed w/o GUC; tid not passed to `_touch_last_heartbeat`. Add tenant param or bypass-read |
| heartbeat.py:1353 | Agent + ChatSession + ChatMessage + LLMModel + Participant (heartbeat session setup) | mixed (Agent+LLMModel LIVE; ChatSession/ChatMessage Stage-2; Participant global) | loaded-entity (Agent loaded at :1354) | DAEMON | YES (Stage-0 + Stage-2) | ⚠️needs-review — chicken-egg: Agent select itself fail-closed. `_heartbeat_tick` knows agent.tenant_id (filters at :1979) → thread tid into `_execute_heartbeat` |
| heartbeat.py:1594 | ChatMessage insert (tool_call callback) | agent-scoped | closure `agent_id` (tenant in outer `_execute_heartbeat` scope) | DAEMON | YES (Stage-2) | `tenant_scoped_session(tid)` — tid from enclosing heartbeat context |
| heartbeat.py:1688 | ChatMessage insert (assistant reply) | agent-scoped | closure `agent_id` | DAEMON | YES (Stage-2) | `tenant_scoped_session(tid)` |
| heartbeat.py:1707 | Agent select + update (last_heartbeat, optimistic lock) | tenant-scoped | closure `agent_id` (agent loaded earlier) | DAEMON | YES (Stage-0) | ⚠️needs-review — Agent fail-closed; `tenant_scoped_session(tid)` w/ tid from enclosing scope |
| heartbeat.py:1909 | Agent select + update (touch last_heartbeat on crash) | tenant-scoped | closure `agent_id` | DAEMON | YES (Stage-0) | ⚠️needs-review — same as :1707; tid from enclosing scope |
| heartbeat.py:1964 | Agent select (ALL running/idle, tick scan) | tenant-scoped | NONE (global cross-tenant enumeration; later filters `tenant_id is None`) | DAEMON | YES (Stage-0) | enter_rls_bypass("heartbeat tick — enumerate all running agents") |
| heartbeat.py:2027 | sync_all_for_tenant(db, tenant_id) | ⚠️unknown (delegates; sync touches per-tenant workspace tables) | param `tenant_id` (explicit) | DAEMON | ⚠️UNSURE | tenant_scoped_session(tenant_id) — tid is the explicit arg; verify what sync_all_for_tenant queries (Finding #5) |
| heartbeat.py:2044 | sync_agent_relationships(db, agent_id) | ⚠️unknown (delegates; relationship render likely reads Agent/org) | param `agent_id` (no tenant) | DAEMON | ⚠️UNSURE | ⚠️needs-review — resolve agent.tenant_id; verify sync_agent_relationships query set |
| heartbeat.py:2073 | Agent.tenant_id select (DISTINCT, ALL active tenants) | tenant-scoped | NONE (global enumeration; filters `tenant_id is_not None`) | DAEMON | YES (Stage-0) | enter_rls_bypass("workspace full sweep — enumerate active tenants") |
| feishu_ws.py:223 | ChannelConfig select + update (persist health) | agent-scoped | param `agent_id` (no tenant) | DAEMON | YES (Stage-2) | ⚠️needs-review — resolve agent.tenant_id → `tenant_scoped_session(tid)` |
| feishu_ws.py:383 | process_feishu_event(agent_id, body, db) — nested ChatSession/ChatMessage/etc. | agent-scoped (deep call chain) | param `agent_id` | DAEMON (WS event) | YES (Stage-2) | ⚠️needs-review — bare db passed into API handler; nested queries fail-closed. Resolve agent.tenant_id → `tenant_scoped_session(tid)` (Finding #6) |
| feishu_ws.py:416 | feishu_card_callback(req, db) — nested queries | agent-scoped (deep call chain) | param `agent_id` (callback derives its own) | DAEMON (WS card action) | ⚠️UNSURE | ⚠️needs-review — card callback resolves agent internally from payload; tenant_source unclear at this layer (Finding #6) |
| feishu_ws.py:558 | ChannelConfig select (ALL feishu, is_configured) | agent-scoped | NONE (global cross-tenant enumeration) | DAEMON (startup `start_all`) | YES (Stage-2) | enter_rls_bypass("feishu start_all — enumerate all configured channels") |
| wecom_stream.py:157 | Agent select (welcome message) | tenant-scoped | param `agent_id` | DAEMON (WS enter_chat) | YES (Stage-0) | ⚠️needs-review — Agent fail-closed; resolve tid (bootstrap problem, see Finding #4) |
| wecom_stream.py:216 | ChannelConfig select (ALL wecom, is_configured) | agent-scoped | NONE (global cross-tenant enumeration) | DAEMON (startup `start_all`) | YES (Stage-2) | enter_rls_bypass("wecom start_all — enumerate all configured channels") |
| wecom_stream.py:269 | Agent + ChatMessage + User + channel session (process message) | mixed (Agent LIVE; ChatMessage Stage-2; User tenant-scoped LIVE) | loaded-entity (Agent loaded at :271) | DAEMON (WS message) | YES (Stage-0 + Stage-2) | ⚠️needs-review — Agent select itself fail-closed; bootstrap tid then re-scope |
| wechat_personal_stream.py:70 | ChannelConfig select (ALL wechat_personal, is_connected) | agent-scoped | NONE (global cross-tenant enumeration) | DAEMON (startup `start_all`) | YES (Stage-2) | enter_rls_bypass("wechat_personal start_all — enumerate all connected channels") |
| wechat_personal_stream.py:410 | ChannelConfig select + update (mark disconnected) | agent-scoped | param `agent_id` (no tenant) | DAEMON | YES (Stage-2) | ⚠️needs-review — resolve agent.tenant_id → `tenant_scoped_session(tid)` |
| wechat_personal_stream.py:449 | Agent + ChatMessage + User + channel session (process message) | mixed (Agent/User LIVE; ChatMessage Stage-2) | loaded-entity (Agent loaded at :451) | DAEMON (poll message) | YES (Stage-0 + Stage-2) | ⚠️needs-review — Agent select fail-closed; bootstrap tid then re-scope |
| dingtalk_stream.py:175 | ChannelConfig select (ALL dingtalk, is_configured) | agent-scoped | NONE (global cross-tenant enumeration) | DAEMON (startup `start_all`) | YES (Stage-2) | enter_rls_bypass("dingtalk start_all — enumerate all configured channels") |
| evolution_daemon.py:46 | cleanup_expired_replies(db) → PendingReplyContext update (ALL, no filter) | agent-scoped | NONE (global cross-tenant update) | DAEMON (loop) | YES (Stage-2) | enter_rls_bypass("pending-reply expiry sweep — all tenants") |
| auto_dream.py:1653 | Agent select (resolve agent name) | tenant-scoped | param `agent_id` (tenant_id also a param to enclosing fn) | DAEMON (dream) | YES (Stage-0) | tenant_scoped_session(tenant_id) — tid is already a param of the enclosing dream fn |
| session_recall.py:593 | ChatSession ⨝ ChatMessage select (history search) | agent-scoped | param `agent_id` (no tenant) | DAEMON / REQUEST-adjacent (called from invocation memory path) | YES (Stage-2) | ⚠️needs-review — `_search_session_history_db(agent_id,...)` has no tid; resolve agent.tenant_id OR rely on ContextVar if call always runs inside request (Finding #7) |
| t0_logger.py:827 | ChatSession + ChatMessage select (backfill T0 logs) | agent-scoped | param `agent_id` (no tenant) | DAEMON (backfill) | YES (Stage-2) | ⚠️needs-review — `backfill_recent_chat_logs(agent_id,...)` has no tid; resolve agent.tenant_id → `tenant_scoped_session(tid)` |
| hooks_setup.py:400 | ChatSession select-by-id + capture_pending_reply (PendingReplyContext insert) | agent-scoped | ctx `agent_id` (hook context; ContextVar tenant may be live) | DAEMON / hook (fire-and-forget POST_TOOL_USE) | YES (Stage-2) | ⚠️needs-review — hook runs in bg task; ContextVar tenant may be lost. Safest: resolve agent.tenant_id → `tenant_scoped_session(tid)` (Finding #8) |

---

## Notes on ⚠️ items (rationale)

- **Stage-2 dependency:** All `agent-scoped` rows are `fail_closed=YES (Stage-2)` because the tables
  have NO `tenant_id` column today — they only break after Stage 2 adds column + policy. The Stage-3
  role flip alone does NOT break them.
- **Chicken-egg (Agent self-read):** Many sites load `Agent` (tenant-scoped, policy LIVE at Stage-0)
  to *discover* the tenant_id — but the Agent select itself is fail-closed once role flips, so you
  can't scope by a tid you haven't read yet. These are tagged ⚠️needs-review and discussed in
  Finding #4. The structural fix is to thread tenant_id down from the global tick scan (which must use
  `enter_rls_bypass`) rather than re-discovering it in each leaf.

---

## Caller-chain resolutions (post-verification)

- **heartbeat.py:2027** (`_sync_one_tenant` → `sync_all_for_tenant`): VERIFIED tenant-scoped. The
  delegate queries `Agent.tenant_id == tenant_id` (:162) and calls `sync_company_profile(tenant_id)` /
  `sync_org_structure(tenant_id)` — all touch tenant-scoped tables filtered by the explicit arg.
  → **fail_closed = YES (Stage-0)**, clean fix `tenant_scoped_session(tenant_id)`. (Upgraded from ⚠️UNSURE.)
- **heartbeat.py:2044** (`_sync_one_agent` → `sync_agent_relationships(agent_id)`): delegate has no
  tenant param and renders relationships (reads Agent/org by agent_id) → Agent read is fail-closed.
  → **fail_closed = YES (Stage-0)**; ⚠️needs-review only because tid must be resolved from agent_id.
- **session_recall.py:593**: inner `_search_session_history_db(agent_id,...)` holds the bare session;
  the public wrapper `search_session_history(agent_id, ..., tenant_id=...)` (:671) DOES carry tid, and
  its sole caller is `tools/handlers/memory.py:356` (a tool handler — runs inside an agent invocation).
  → tid is available at the wrapper layer; thread it into `_search_session_history_db` then
  `tenant_scoped_session(tid)`. ContextVar fallback also viable since it runs in-invocation.
- **t0_logger.py:827** (`backfill_recent_chat_logs(agent_id,...)`): sole caller is `auto_dream.py:1737`
  inside the dream consolidation fn where `tenant_id` is already a param (threaded to
  `_dream_llm_consolidate(agent_id, tenant_id, ...)`). → thread tid into `backfill_recent_chat_logs`,
  then `tenant_scoped_session(tid)`.

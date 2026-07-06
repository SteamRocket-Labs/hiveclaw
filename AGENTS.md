# AGENTS.md

Technical reference for AI coding assistants working with the Hive platform.

## North Star — Highest-Priority Goal (overrides all other guidance)

Hive exists to be **two things, and every line of code must serve one of them**:

1. **A self-evolving agent infrastructure with enterprise-grade access control** — digital employees that genuinely improve over time (memory, reflection, skill acquisition, soul evolution) while every capability, memory write, and external action stays permission-governed and auditable.
2. **A control plane (控制中台)** for operating those agents at company scale — org/permission management, governance, budgeting, coordination, and observability.

**Quality bar:** the per-agent intelligence and self-evolution must be **at least as good as `hermes-agent`** (internal benchmark at `/Users/rocky243/vc-saas/hermes-agent`) — not merely architecturally grander. A system that *feels* weaker than a lean benchmark agent is a failure of Goal 1, not a success.

**Build order:** Goal 1 (the agent's own intelligence + self-evolution) is the **foundational cornerstone** — it is hardened and judged *first*; the control-plane and agent-to-agent layers build on top of it. Current SOTA target entry: `docs/hive-sota-master-goal.md`; CCPlus boundary contract: `docs/ccplus-north-star-contract-2026-06-24.md`; foundation roadmap: `docs/self-evolution-sota-plan.md`.

## Reference Baselines — 对照物顺序

Hive is a **Cloud Code Python evolution**, so implementation comparisons must use the current local source baselines in this order:

1. **FreeCode TS runnable baseline**: `/Users/rocky243/vc-saas/free-code-main` — first reference for answering "what is the essential CC runtime semantics?"
2. **claw-code Python port**: `/Users/rocky243/Context Engineering/claw-code/src` — only for Python-port direction and existing port boundaries; not a full parity baseline.
3. **claw-code Rust runtime**: `/Users/rocky243/Context Engineering/claw-code/rust` — reference for session hygiene, workspace partition, JSONL rotation, resume/fork/compact.
4. **claude-code-org TS source**: `/Users/rocky243/Context Engineering/claude-code-org` — cross-check against FreeCode.
5. **Codex Rust**: `/Users/rocky243/Context Engineering/codex/codex-rs` — Codex delta only; it must not override the CC baseline.

If sources conflict, judge CC parity from FreeCode first, use `claude-code-org` only as cross-check, use `claw-code/rust` for low-level runtime/session lessons, and use Codex Rust only for additive deltas that do not conflict with CC semantics.

## CCPlus Boundary Contract — 本地 CLI 与远程独占能力边界

CC / FreeCode is the semantic baseline. Codex may improve engineering control, observability, approval routing, sandboxing, typed thread/turn surfaces, and workbench ergonomics, but it must not redefine CC capability boundaries.

Do **not** exclude a feature merely because it appears in a local CLI. If a CC local CLI feature is implemented through local process, filesystem, workspace, session, transcript, tool, sandbox, hook, or terminal-state semantics, it is in CCPlus scope and should be mapped into Hive's Web UI, API, RuntimeTask, ChatSession, T0, Session Workbench, or Hive Bridge shape.

The exclusion boundary is provider-hosted or proprietary remote infrastructure: S-Work / CCR / Ant-only remote capabilities, Claude Code on the web execution, UltraPlan's remote planning session, or any inaccessible first-party hosted service. Those are not CC parity requirements. If Hive later builds an equivalent, it is a Hive-native replacement, not hidden CC parity debt.

Use this decision order:

1. CC / FreeCode semantic boundary wins.
2. Codex engineering/control improvement may be adopted only if it preserves that boundary.
3. Hive Memory / Iter / Hermes-style self-evolution and company control plane are deliberate Hive-native deltas.
4. Remote proprietary capabilities are excluded from CC parity and must be documented as such.

## Full Lifecycle Parity — 全生命周期对标

“全面对标” means **agent full-lifecycle parity**, not feature-count parity. Compare the whole agent lifecycle: definition, context assembly, accepted user prompt, transcript write, model loop, tool loop, hook boundaries, compaction, stop, resume, subagent, workflow, skill loading, and session close.

The deliberate non-parity delta is Hive's **Memory / Iter self-evolution system**. Memory stays Hive-native: T0/T2/T3/soul, governed write surfaces, Skill evolution, and Iter are not reduced to CC behavior. Everything else must have an explicit CC/Hive mapping before being called aligned.

Context composition must be mapped one-to-one:

- CC `CLAUDE.md` / project instructions → Hive `soul.md` and governed identity/context sections.
- CC skill progressive disclosure → Hive `Skill` capsule plus `load_skill` / `tool_search` split.
- CC task/todo scaffolding → Hive Work Ledger / Progress Ledger.
- CC transcript/session artifacts → Hive T0 raw evidence plus product read models.
- CC hooks/session boundaries → Hive hook events with equivalent blocking/resume semantics.

Current session-middle parity priorities are **Skill, Sub-agent, Workflow, and Hooks**. Their prompts, tool descriptions, and lifecycle events must be source-checked against FreeCode first and kept vendor-neutral. Anthropic/Claude/Codex names may appear as documented baselines, but runtime prompts must not privilege a model vendor or product identity.

## AI-Native Design Law (最高设计法律 — judges every architectural decision)

Hive is an **AI-native system**. Three layers, in strict priority order:

1. **L1 — Unleash the model first.** Any step that requires intelligence (summarization, planning, extraction, synthesis, judgment) belongs to the LLM at full capability: complete input visibility (no mechanical pruning of what the model sees), sufficient output budget (no starved `max_tokens`), prompts engineered to benchmark (Claude Code) quality — structure, examples, anti-drift constraints. Mechanical/string-based handling of intelligent steps is allowed ONLY as an observable fallback on failure paths, never the primary path. *Case law: compaction once fed the LLM a `[-40:]` truncated slice with a 2500-token output cap and a silent regex fallback — the canonical violation (fixed in `docs/compaction-cc-alignment.md`).*
2. **L2 — Harness constrains, never replaces.** Governance, safety, budgets, audit wrap *above* model capability: they bound what the agent may **do**, not how well it **thinks**. A constraint that degrades model intelligence (instead of scoping authority) is a design bug.
3. **L3 — Hive's identity: a neutral, organization-facing control plane.** Hive is an independent third party with **model equality**: every feature, every constraint, every prompt works equally for every model — no privileged vendor, no model-specific feature gates, no prompt favoritism. On top of that equality Hive adds what no model vendor provides: the company-scoped controllable agent control plane.

**Memory / self-evolution boundary law:** LLM 负责判断、提炼、反思、归纳、候选生成；平台负责证据引用、权限、去重、回滚、审计、最终落盘。Any memory, heartbeat, dream, skill, workflow, or evolution path that replaces model judgment with counters, regexes, truncated summaries, or platform-authored "semantic" text is an AI-native violation. Any path that lets the model bypass governed write surfaces for durable memory/evolution/soul files is a governance violation.

**Memory target form:** Hive memory is an Agent Markdown Wiki / Learning Vault: T0 raw evidence, T2 tagged Markdown Segment Packages under `memory/t2/sessions/**`, a converged T3 semantic layer (`memory/t3/episodes.md`, `user.md`, `worker.md`, `capabilities.md`), source_refs-backed residual evidence verification, and `soul.md`. Skill is a progressive capability capsule grown from T3 capability evidence and eval-backed candidate packages, not a T3 page. `relations`, `contradictions`, graph/vector/search/UI views are derived and rebuildable; no external memory provider may become the T3 source of truth. `memory/indexes/wiki_map.md` is the single generated navigation map, not always-on prompt memory; control sidecars live under `memory/control/`. Current path contract: `docs/memory-vault-path-contract-2026-06-23.md`.

**T0 session truth:** JSONL is the mechanical truth and Markdown is the deterministic projection. Per-segment `memory/t0/sessions/<session_id>/segments/<segment_id>/events.jsonl` is the resume/replay/fork/checkpoint/rollback source of truth; same-segment `source.md` is the human/LLM-readable Markdown/XML projection and legacy fallback, not the mechanical truth.

**Review lens — apply to every subsystem:** ① Is the LLM's input visibility complete? ② Is its output budget sufficient? ③ Is the prompt engineered to benchmark quality? ④ Does mechanical processing appear only as an observable fallback?

## Delivery Discipline — One Complete Pass, No MVP (交付纪律 — 一次改完，零技术债)

**Owner law (2026-06-08, "必须记住"): any revision/rework round ships as ONE complete pass — no MVP, no phased "first implementation," no technical debt deferred.** Before starting a change, define the *complete* scope up front (tests, edge cases, error paths, schema migration, **legacy-data backfill**, production cleanup, observability) and deliver it in one pass. Forbidden: "ship Phase 0 first," default-off flags hiding half-built work, "add tests later," "skip the migration for now."

*Case law: the agent memory system rotted into dirty, drifting files precisely because P0–P10 took the spec-sanctioned "first implementation can encode inline rather than rewriting every existing bullet" shortcut and never paid it down — accumulating ten debts D1–D10 (`docs/agent-memory-purity-spec.md`). MVP's "later" = never.*

**Only exception:** a genuinely irreversible step (production data migration/deletion) uses a dry-run + confirmation gate — that is a safety gate, not an MVP stage; completeness never waives safety.

## Project Overview

Hive is an open-source **multi-agent collaboration platform** — enterprise "digital employees" with persistent identity, long-term memory, private workspaces, autonomous trigger-driven execution, governed self-evolution, and owner/company-aware Memory Gate + Platform Gate governance.

- **Version:** 1.7.0 (tracked in `backend/VERSION` and `frontend/VERSION`)
- **License:** Apache 2.0
- **Stack:** FastAPI (Python 3.12) + React 19 (TypeScript 5) + PostgreSQL 15 + Redis 7
- **Deployment:** Docker / Railway

## Railway Deployment Rule — Production Only, Three Services

For Hive production/product deployments, **always deploy all three Railway services**: `backend`, `backend-api`, and `frontend`. A deploy is incomplete if any one of the three services is still on an older deployment.

The retired Railway eval environment is not a default deploy target. Eval now reads production evidence and keeps deterministic gates in CI; see `docs/eval-system-spec.md`.

Railway source layout differs by service:

- `backend`: service is configured with `rootDirectory=backend`; upload an archive that preserves a top-level `backend/` directory.
- `backend-api`: service is configured with no root directory and `configFile=/railway.json`; upload from the backend package root itself.
- `frontend`: service is configured with `rootDirectory=frontend`; upload an archive that preserves a top-level `frontend/` directory.

Use this exact pattern from the repo root:

```bash
PROJECT_ID=dd959a13-19f9-497a-9704-42c310eae230
tmp_root=$(mktemp -d /tmp/hiveclaw-railway-upload.XXXXXX)
mkdir -p "$tmp_root/backend-root" "$tmp_root/frontend-root"
git archive --format=tar HEAD backend | tar -xf - -C "$tmp_root/backend-root"
git archive --format=tar HEAD frontend | tar -xf - -C "$tmp_root/frontend-root"

cd "$tmp_root/backend-root"
railway up --service backend --environment production --project "$PROJECT_ID" --detach -m "deploy latest backend production archive-root"

cd "$tmp_root/backend-root/backend"
railway up --service backend-api --environment production --project "$PROJECT_ID" --detach -m "deploy latest backend-api production backend-root"

cd "$tmp_root/frontend-root"
railway up --service frontend --environment production --project "$PROJECT_ID" --detach -m "deploy latest frontend production archive-root"
```

After submit, poll production deployment status until all three targets are `SUCCESS`. `backend-api` is not publicly exposed, so the public backend health URL does not prove `backend-api` freshness by itself; Railway deployment status is the required proof for that service.

```bash
railway deployment list --service backend --environment production --project "$PROJECT_ID" --limit 1 --json
railway deployment list --service backend-api --environment production --project "$PROJECT_ID" --limit 1 --json
railway deployment list --service frontend --environment production --project "$PROJECT_ID" --limit 1 --json

curl -fsS https://backend-production-326d.up.railway.app/api/health
curl -I -fsS https://frontend-production-0346.up.railway.app/
```

## Current Engineering Baseline (2026-06-15)

Treat these documents as the current truth surface before making architecture claims:

- `docs/hive-sota-master-goal.md` — canonical SOTA total goal, target matrix, and future loop-comparison ledger.
- `docs/eval-system-spec.md` — current eval direction: no Railway eval clone, no nightly behavior gate, production evidence readout plus deterministic CI gates.
- `docs/ccplus-north-star-contract-2026-06-24.md` — canonical CCPlus boundary contract: CC semantic baseline, Codex engineering delta, Hive-native evolution, local CLI parity, and remote proprietary exclusion.
- `docs/harness-engineering-audit-2026-06-11.md` — harness audit, remediation log, and verification evidence.
- `docs/round2-sota-benchmark-2026.md` — second-round SOTA benchmark, detailed comparison sources, and milestone evidence.
- `docs/memory-clean-loop-refactor-plan-2026-06-17.md` — current memory clean-loop redesign and Agent Markdown Wiki / Learning Vault target.
- `docs/memory-system-flow-map-2026-06-17.md` — end-to-end memory flow map, including source_refs-backed residual evidence verification, T3 semantic layer, and capability candidate lanes.
- `docs/memory-vault-path-contract-2026-06-23.md` — current single-agent memory filesystem contract: canonical T0/T2/T3/session_state/index/control paths plus legacy import quarantine semantics.
- `docs/agent-memory-md-first-spec.md` — MD-first memory truth-source contract and lifecycle spec.
- `docs/self-evolution-sota-plan.md` — canonical self-evolution foundation, now a completed substrate plus ongoing benchmark baseline.
- `docs/agent-memory-purity-spec.md` — memory purity, lifecycle, and hygiene contract.

Current implemented closures that future work must preserve:

- Hard verification and rollback metadata are required for durable self-evolution promotion.
- `RuntimeTask` execution is restart-resumable and web chat disconnects do not cancel runs.
- Plan Mode is a first-class confirmation/planning boundary: substantive plan content must be agent-authored, clarification must be first-class, and unconfirmed autonomous/high-risk work must not execute.
- Workflow is a first-class deterministic orchestration substrate parallel to Plan Mode: `RuntimeTask(task_type="workflow")`, workflow step/leaf journals, run quotas, gate/wait/resume, trigger integration, admin ops, and governed auditability.
- Subagent/delegation is a first-class collaboration capability: lightweight workers, peer delegation, fanout, context isolation, result distillation, governed shared tool execution, and replay-safe resume boundaries must remain distinct from Workflow control flow.
- Agent TodoList / Work Ledger / Progress Ledger is the CC Task/Todo-equivalent agent-authored task board: `track_todo` records todos/dependencies, `record_finding` records findings/failures/replan, and `read_ledger` restores state. Writing a todo is cognitive bookkeeping; it must not start execution.
- Skill is a progressive-disclosure capability capsule, not merely a Markdown prompt. A Skill may package instructions, references, templates, scripts, evals, workflow definitions, and subagent definitions; loading a Skill adds context/guidance only. Executable components still run through their governed runtime (`preview_workflow`/`start_workflow`, `spawn_subagent`/`delegate_to_agent`, or approved sandbox/code execution).
- Memory/Self-Evolution target form is Agent Markdown Wiki / Learning Vault: T0 -> T2 -> T3 -> `soul.md` is the durable gradient; residual verification means T3 curation follows T2 `source_refs` back to T0 evidence; Skill may grow from memory evidence, while Workflow remains a separate execution-control system that memory can only reference or hand off evidence to.
- `invocation_spans` are the canonical DB trace surface; JSONL spans remain compatibility artifacts.
- Provider retry/overload fallback, token budget gates, CJK-aware estimates, canonical prompt-cache anchors, and Anthropic thinking-signature preservation are runtime contracts.
- Agent-controlled code execution is provider based: local/trusted hosts use the shared OS sandbox builder (`bubblewrap` or `sandbox-exec`), while Railway production uses `HIVE_CODE_EXEC_PROVIDER=vercel_sandbox` and Vercel Sandbox credentials. Never fall back to raw subprocesses.
- MCP authz rejects token passthrough and URL userinfo; A2A Agent Cards and `/interoperability/profile` must state unsupported OAuth/JSON-RPC surfaces as `not_exposed`.
- Memory hygiene startup repair retires legacy shadow stores and quarantines dead stubs through a reversible shared path.
- Latest full backend evidence before the current documentation-only update: `cd backend && source .venv/bin/activate && pytest tests -q` -> `4223 passed, 7 skipped, 4 warnings`.

## Commands

```bash
# Setup
bash setup.sh --dev

# Run
bash restart.sh                    # Backend(:8008) + Frontend(:3008)

# Backend (cd backend/)
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8008 --reload
ruff check app/ --fix && ruff format app/
pytest
alembic upgrade head
alembic revision --autogenerate -m "desc"
python -m app.scripts.repair_memory_hygiene      # dry-run fleet memory hygiene
python -m app.scripts.repair_memory_hygiene --apply --confirm

# Frontend (cd frontend/)
npm run dev                        # Vite on :3008
npm run build                      # tsc + vite build

# Docker
docker compose up -d --build       # Full stack → :3008
```

## Backend Architecture (`backend/app/`)

### Codebase Stats

| Layer | Files | LOC | Purpose |
|-------|-------|-----|---------|
| API Routes | 62 | — | FastAPI routers |
| Models | 43 | — | SQLAlchemy ORM (async, RLS) |
| Services | 163 | — | Business logic |
| Tool Domains | 21 | — | Feishu office, messaging, tasks, workspace, email |
| Kernel | 3 | ~2.7K | Core LLM execution engine |
| Tools | 18 handlers | — | Handler implementations; 100+ registered tool definitions |
| Skills | 5 | ~310 | Progressive-disclosure capability capsules |
| Memory | 25 | — | MD-first pyramid (T0/T2/T3/soul) + control plane: write gate, activation, retention, lifecycle, understanding, hygiene |
| Migrations | 79 | — | Alembic schema versions |

### API Routers (62 files)

Core: `agents`, `auth`, `users`, `tenants`, `enterprise`, `admin`
Agent features: `tasks`, `triggers`, `schedules`, `relationships`, `skills`, `files`, `chat_sessions`, `objectives`, `autonomy`
Channels: `feishu`, `slack`, `discord_bot`, `dingtalk`, `wecom`, `wechat_personal`, `teams`, `telegram`, `email_channel`, `tenant_channels`
Platform: `tools`, `packs`, `capabilities`, `plaza`, `notification`, `websocket`, `office`, `interoperability`
Enterprise: `organization`, `memory`, `guard_policies`, `feature_flags`, `config_history`, `role_templates`
Desktop: `desktop_auth`, `desktop_sync`, `desktop_agents`, `desktop_audit`
Other: `upload`, `webhooks`, `gateway`, `llm_proxy`, `oidc`, `onboarding`, `advanced`, `atlassian`

Most routers are mounted under both `/api` and `/api/v1`; public webhooks and `/ws/chat/{agent_id}` are mounted without the API prefix.

### Models (43 files)

Core entities: `User`, `Agent`, `Tenant`, `LLMModel`, `Tool`, `Skill`, `Task`, `RuntimeTask`, `Objective`
Agent config: `AgentTrigger`, `AgentSchedule`, `ChannelConfig`, `AgentPermission`, `AgentTemplate`
Relationships: `AgentRelationship`, `AgentAgentRelationship`, `OrgMember`, `OrgDepartment`, coordination lease/signal/checkpoint models
Audit: `AuditLog`, `SecurityAuditEvent`, `ChatMessage`, `ChatSession`, `AgentActivityLog`, `InvocationSpan`, `SessionFeedbackEvent`
Platform: `CapabilityPolicy`, `CapabilityInstall`, `GuardPolicy`, `FeatureFlag`, `Notification`
Auth: `RefreshToken`, `InvitationCode`, `Participant`, identity provider models
Social: `PlazaPost`, `PlazaComment`, `PlazaLike`

### Services (163 files)

| Category | Services |
|----------|---------|
| Agent lifecycle | `agent_manager`, `agent_seeder`, `auto_dream`, `auto_provision` |
| LLM | `llm_client` (OpenAI/Anthropic/Gemini/compatible), `llm_utils` |
| Execution | `trigger_daemon` (15s loop), `task_executor`, `scheduler`, `heartbeat`, `evolution_daemon`, `web_chat_runtime`, `long_task_runtime`, `invocation_trace` |
| Channels | `feishu_service`, `feishu_ws`, `dingtalk_stream`, `wecom_stream`, `wechat_personal_stream`, `channel_delivery_service` |
| Tools | `agent_tools`, `agent_tool_assignment_service`, `tool_seeder`, `tool_telemetry` |
| Security | `capability_gate`, `approval_service`, `quota_guard`, `secrets_provider`, `audit_logger`, `mcp_authz`, `subprocess_sandbox`, `agent_identity_lifecycle` |
| Memory | `memory_service`, `conversation_summarizer`, `knowledge_inject`, `extract_agent`, `extract_queue`, `agency_charter`, `decision_trace`, `session_feedback` |
| Integration | `mcp_client`, `mcp_registry_service`, `email_service`, `viking_client`, `interoperability` |
| Multi-tenant | `enterprise_sync`, `org_sync_service`, `sync_service` |
| Office / docs | `office_document_service`, `officecli_adapter`, `text_extractor` |
| Other | `pack_service`, `skill_creator_content`, `token_tracker`, `objective_service`, `autonomy_repair_plan` |

### Memory Gate + Platform Gate

Hive keeps the T0/T2/T3/soul Markdown memory pyramid, but runtime behavior is governed by an LLM Memory Gate plus a mechanical Platform Gate. Memory Gate reviews semantic candidates; Platform Gate enforces permissions, evidence refs, dedupe, rollback, audit, and atomic commit.

| Capability | Primary code paths | Runtime invariant |
|------------|--------------------|-------------------|
| Principal + charter context | `services/agency_charter.py`, `services/principal_context.py` | Memory/action decisions must know direct owner, company, creator/current user, and delegation context when available. |
| Write safety | `memory/write_gate.py`, `memory/t2/segment_package.py`, `memory/t3_platform_gate.py`, `memory/explicit_overlay.py`, `tools/handlers/memory.py` | T2 Segment Packages and optional T2 Episode Stitch Packages must be agent-authored candidates reviewed by Memory Gate and committed by Platform Gate before T3 intake; PL4 credentials are rejected. Legacy `memory/t2_store.py` is compatibility/repair only. |
| Memory hygiene | `memory/hygiene.py`, `tools/workspace.py`, `scripts/repair_memory_hygiene.py` | Legacy shadow stores and dead stubs are retired through reversible quarantine/backfill paths; no ad hoc workspace surgery. |
| Dynamic activation | `memory/activation.py`, `memory/retriever.py`, `services/memory_service.py`, `runtime/invoker.py` | Prompt memory is selected by owner/company/goal/open-loop relevance and sensitivity access, not by static file inclusion alone. |
| Decision trace + preflight | `services/action_preflight.py`, `services/decision_trace.py`, `tools/service.py` | External-visible, sensitive, irreversible, or company-conflicting tool calls must pass preflight before execution. |
| Session feedback | `services/session_feedback.py`, `models/session_feedback.py`, `api/chat_sessions.py` | Useful/misleading feedback is persisted with tenant/session/agent context and re-enters memory through governed write paths. |
| Coordination primitives | `agents/coordination.py`, `agents/orchestrator.py`, `tools/service.py` | Cross-agent work uses Lease/Signal; confirm-first actions create Checkpoint; Sentinel can emit Signal or Checkpoint. |
| Proactive steward loop | `services/proactive_employee_loop.py`, `services/heartbeat.py`, `memory/policy_replay.py` | Heartbeat may prepare low-risk work, but external-visible action requires Checkpoint and policy tuning requires replay guard. |

### Web Chat Runtime

Web chat runs are durable background tasks:

- `chat_sessions.py` exposes session history and start/active/cancel run APIs.
- `web_chat_runtime.py` creates and executes `RuntimeTask(task_type="web_chat_turn")`.
- `web_chat_broker.py` broadcasts session-scoped runtime events to WebSocket subscribers.
- `websocket.py` is a subscription and compatibility start path; disconnecting the browser must not cancel the run.
- Active-run uniqueness and persisted `RuntimeTask` scanning prevent duplicate web-chat runs after process restarts.
- Frontend `AgentDetail.tsx` sends a 30s keepalive ping while waiting/streaming; backend replies with `pong`.
- `WS_IDLE_TIMEOUT_SECONDS` defaults to 3600; if an active run exists, idle close is deferred.

### Office Runtime

Office editing is a first-class runtime:

- Backend API: `backend/app/api/office.py`
- Workspace document service: `backend/app/services/office_document_service.py`
- OfficeCLI adapter: `backend/app/services/officecli_adapter.py`
- Frontend workbench: `frontend/src/pages/agent-detail/OfficeWorkbenchSection.tsx`
- Required production env includes `ONLYOFFICE_DOCS_URL`, `ONLYOFFICE_JWT_SECRET`, and public base URL config.
- Agent workspace remains file source of truth; ONLYOFFICE handles browser WYSIWYG editing and signed callbacks.

### Kernel Engine

Stateless LLM loop with dependency injection. Zero DB imports — all I/O goes through `KernelDependencies` callbacks.

- Max 200 tool rounds per invocation (`Agent.max_tool_rounds`); heartbeat overrides to 40
- Semantic loop detection via `LoopGuard` (`kernel/loop_guard.py`, wired in `engine.py`)
- Proactive compaction at 75% utilization (`_MIDLOOP_COMPACT_THRESHOLD`); microcompact pressure at 60%; reactive compaction on prompt-too-long
- Tool result eviction: 50KB/result, 200KB/round
- Parallel-safe tool execution
- Vision support for multimodal models
- Provider-specific cache hints
- DB-backed invocation/generation/tool spans through `record_invocation_span`
- Provider retry/overload fallback, output-cap telemetry, and Anthropic thinking-signature preservation
- Turn-level token budget gates where runtime config provides a budget

### Tool Handlers (18 modules / 100+ registered tool definitions)

| Handler | Tools |
|---------|-------|
| `filesystem` | list_files, read_file, write_file, edit_file, delete_file |
| `search` | web_search, web_fetch, firecrawl_fetch, xcrawl_scrape |
| `communication` | send_feishu_message, send_web_message, send_message_to_agent, delegate_to_agent, async task helpers, channel send/upload |
| `email` | send_email, read_emails, reply_email |
| `feishu` | feishu_wiki_list, feishu_doc_read/append/create/share |
| `office` | office_document_create/view/query/apply/validate/dump |
| `memory` | save_memory and memory-control helpers |
| `plaza` | plaza_get_new_posts, plaza_create_post, plaza_add_comment |
| `skills` | load_skill, tool_search |
| `triggers` | set_trigger, update_trigger, list_triggers, cancel_trigger |
| `hr` | create_digital_employee |
| `mcp` | list_mcp_resources, read_mcp_resource, import_mcp_server, call_mcp_tool |
| `plan_mode` | ask_user_question, exit_plan_mode |
| `subagent` | spawn_subagent |
| `work_ledger` | track_todo, record_finding, read_ledger |
| `workflow` | preview_workflow, start_workflow |

## Frontend Architecture (`frontend/src/`)

### Pages (16 page entries + 40 nested page/section helpers)

| Page | Route | Purpose |
|------|-------|---------|
| Login | `/login` | Authentication |
| CompanySetup | `/setup-company` | Tenant onboarding |
| Dashboard / Workbench | `/enterprise/dashboard` | Company Admin workbench; `/dashboard` redirects here |
| Agent Circle | `/plaza` | Agent social feed; backend route remains `plaza` |
| AgentDetail | `/agents/:id` | Agent management hub |
| EnterpriseSettings | `/enterprise/*` | Workspace admin settings sections |
| PlatformDashboard | `/admin/*` | Platform admin |
| UserManagement | `/enterprise/users` | User/team admin |

### Tech Stack

| Aspect | Choice |
|--------|--------|
| Framework | React 19 |
| Bundler | Vite 6 |
| Routing | React Router 7 (lazy loading) |
| Server state | TanStack React Query 5 |
| Client state | Zustand 5 |
| i18n | i18next (en + zh) |
| Icons | Tabler Icons |
| Charts | Recharts 3 |
| Tests | Vitest 4 (39 frontend test files) |

### API Layer

Core HTTP abstraction in `api/core/request.ts` — `get<T>()`, `post<T>()`, `put<T>()` with JWT auth and tenant header injection.

36 files in `api/domains/` including tests and index, covering agents, enterprise, tools, chat, auth, notifications, files, tasks, skills, relationships, plaza, channels, schedules, admin, activity, users, messages, system, triggers, office, memory, knowledge, plans, workflows, subagents, extensions, autonomy, and evolution.

## Conventions

- **Multi-tenancy:** All entities tenant-scoped. PostgreSQL RLS. `check_agent_access()` required.
- **Kernel invariant:** All LLM calls via `invoke_agent()` → `AgentKernel.handle()`. Never direct.
- **Tool governance:** All tool calls via `ToolRuntimeService.execute()`. Never bypass.
- **Code execution provider:** `execute_code` / `run_command` must go through `services/code_execution/`; local/trusted hosts may use `services/subprocess_sandbox.py`, but Railway production must use the external Vercel Sandbox provider. Never inherit host secrets or launch raw `subprocess` from tool handlers.
- **MCP authz:** MCP imports/execution must go through `services/mcp_authz.py`; URL userinfo, `access_token`, and token passthrough credentials are forbidden.
- **Memory write invariant:** Do not write T2/T3 durable memory directly from tools or extractors; use `prepare_memory_write()` or an existing wrapper that calls it.
- **Memory read invariant:** Prompt memory retrieval must preserve `ActivationContext` and sensitivity stripping when current user/owner/company context is known.
- **Memory hygiene invariant:** Legacy memory artifacts are repaired by `memory/hygiene.py` only, with reversible quarantine/backfill reports.
- **Action boundary invariant:** Do not bypass `ActionPreflightService` for external-visible, sensitive, irreversible, or company-boundary actions.
- **Agent creation invariant:** New employee agents must render first-person accountability plus frozen Company Charter and Owner Agency Charter sections in `soul.md`.
- **Coordination invariant:** Duplicate delegation should acquire a Lease; progress/handoff should use Signal; confirm-first work should create Checkpoint metadata.
- **Trace invariant:** Invocation spans are append-only evidence with tenant, agent, user, runtime task, session, request, trace, and parent span join keys.
- **Interoperability invariant:** A2A/interoperability descriptors are contracts, not marketing; unsupported OAuth delegation and JSON-RPC task surfaces must remain `not_exposed`.
- **i18n:** Both `en.json` and `zh.json` must be updated for any UI text.
- **Migrations:** `alembic heads` must show single head before creating new migration.
- **Ruff:** `target-version = "py311"`, `line-length = 120`.
- **Ports:** Frontend 3008, Backend 8008, PostgreSQL 5432, Redis 6379.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | PostgreSQL async connection |
| `REDIS_URL` | Redis cache/sessions |
| `SECRET_KEY` | Session secret |
| `JWT_SECRET_KEY` | JWT signing |
| `SECRETS_MASTER_KEY` | Encrypt LLM keys and channel credentials |
| `AGENT_DATA_DIR` | Agent workspace root |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | Feishu SSO |
| `ONLYOFFICE_DOCS_URL` / `ONLYOFFICE_INTERNAL_DOCS_URL` / `ONLYOFFICE_JWT_SECRET` | Browser Office editing |
| `WS_IDLE_TIMEOUT_SECONDS` / `WS_IDLE_DREAM_SECONDS` | Web chat WebSocket idle and idle-hook behavior |
| `TAVILY_API_KEY` | Web search |
| `EXA_API_KEY` | Web search |
| `FIRECRAWL_API_KEY` | Web crawling |
| `XCRAWL_API_KEY` | Web crawling |

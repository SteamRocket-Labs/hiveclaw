<div align="center">
  <h1>Hive</h1>
  <h3>Open-source enterprise digital employee OS — self-evolving Agent Runtime + company control plane.</h3>
  <p><strong>English</strong> | <a href="README.zh-CN.md">简体中文</a></p>
</div>

<div align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue.svg" alt="License"></a>
  <a href="#"><img src="https://img.shields.io/badge/python-3.12-blue.svg" alt="Python"></a>
  <a href="#"><img src="https://img.shields.io/badge/react-19-61dafb.svg" alt="React"></a>
  <a href="#"><img src="https://img.shields.io/badge/postgres-15-336791.svg" alt="PostgreSQL"></a>
</div>

<br>

Hive is a self-hosted **enterprise digital employee operating system**. It is not trying to be another chatbot, and it is not just another library for writing agent workflows. Hive is about how a company can hire, authorize, run, audit, correct, and continuously improve a workforce of AI digital employees.

There is a gap in today's agent market. Models are getting stronger, but model vendors do not provide a complete company governance layer. Enterprise SaaS products have permissions and audits, but most agents remain static configurations or offline human-reviewed optimizations. Open-source agent frameworks help developers compose workflows, but they do not give a company an operating surface for long-lived digital employees. Hive sits at that intersection: **runtime self-evolving digital employees + a company-scale control plane**.

## What Hive Is Building

Hive has two first-class goals:

1. **A self-evolving Agent Runtime**: every agent has identity, memory, skills, tools, a private workspace, and long-running task execution, and can improve from real work, user feedback, session outcomes, and failure cases.
2. **A company control plane**: the organization can manage agent identity, owners, permissions, tools, budgets, channels, approvals, audit trails, org relationships, and data boundaries in one place.

That means Hive does not treat an agent as a one-off prompt, and it does not treat governance as an afterthought UI. Intelligence growth, memory writes, skill promotion, external action, and enterprise authority all flow through the same runtime contract.

## Four Product Pillars

**1. Digital employee identity**

Every agent has a `soul.md` identity contract, an isolated workspace, long-term memory, a skill directory, owner/company context, channel configuration, and trigger-backed wake policies. It carries its working identity across sessions, models, and IM channels instead of starting over every time a chat tab opens.

**2. Governed self-evolution**

Hive lets agents learn, but it does not let agents self-certify that they have improved. Response-complete extraction, the fast reflection learning brain, Heartbeat, Dream, session feedback, skill distillation, and patch-first skill candidates can propose improvements; durable promotion must carry source evidence, hard verification, rollback metadata, audit records, and replay/eval gates. Self-evolution is not "the model says it got better"; it is the system proving that the change did not pollute memory, mislead the owner, or bypass company boundaries.

**3. Company-scale control plane**

Hive is built for a company operating many digital employees. Company Admin, Platform Admin, Agent Circle, HR Agent, tool registration, capability policies, approval flows, multi-tenant RLS, audit logs, budgets, org structure, per-agent channel config, MCP authz, A2A-style Agent Cards, and the interoperability profile are all part of the same control surface.

**4. Harness-grade runtime**


## Current Baseline After Two Major Passes

**Round 1: from complex agent app to enterprise-grade Agent Harness.**

The first audit found that the issue was not "missing a few features"; it was weak failure paths and incomplete cross-module closure. Provider overload could kill a run, long tasks could be interrupted by process restarts, browser disconnects could affect active work, tool boundaries were not hard enough, and verification gates risked letting model self-judgment masquerade as hard evidence. After remediation, Hive's runtime baseline is restart-resumable `RuntimeTask` execution, unified provider retry/fallback, DB-backed invocation trace, governed tool execution, sandboxed code execution, safe MCP import, Memory Control Plane write gates, and auditable promotion paths.

**Round 2: from runnable substrate to SOTA digital employee capability.**

The second pass stopped benchmarking only against Claude Code's harness baseline and widened the target to the strongest pieces across Devin, Letta, ACE, Voyager, Temporal, Glean, Microsoft Entra Agent ID, and related SOTA systems. Implemented closures include the `skill_guard` hard verification gate, fast reflection learning brain, patch-first skill repair, ACE-style T3 reinforcement counters, production Session Useful/Misleading feedback, 10-attempt LLM status/network retry, 529 fallback, workflow completion side-effect deduplication, subagent/web-chat restart recovery, Anthropic interleaved-thinking headers, and signed thinking round-trip.

## What You Can Run Today

- **Create digital employees**: the HR Agent creates `soul.md`, initial tasks, operating boundaries, and triggers through a 2-3 round conversation.
- **Connect work channels**: the same agent can live in Web Chat, Feishu/Lark, Slack, Discord, DingTalk, WeChat Work, WeChat Personal, Telegram, Email, and Microsoft Teams.
- **Learn over time**: T0/T2/T3/soul memory separates raw behavior, extracted learnings, semantic memory, and identity; fast reflection and session feedback enter candidate, ledger, and verification paths instead of directly mutating T3.
- **Act autonomously with controls**: cron, interval, webhook, polling, message-event triggers, and workflows let agents initiate work; external-visible, sensitive, irreversible, or company-boundary actions require preflight, approval, or checkpoint gates.
- **Operate at company scale**: Company Admin manages models, employees, org, tools, skills, quotas, approvals, audit, memory, and channels; Platform Admin manages global settings.
- **Work on office documents**: agent workspaces support browser-based DOCX/XLSX/PPTX editing with ONLYOFFICE signed callbacks and revision history.
- **Stay model-neutral and self-hosted**: Hive does not bind you to one model vendor or office suite; it supports Anthropic, OpenAI, Gemini, DeepSeek, Qwen, MiniMax, Azure, OpenRouter, Zhipu, Kimi, vLLM, Ollama, SGLang, and custom OpenAI-compatible endpoints.

> [!NOTE]
> Hive is fully self-hostable. FastAPI + React + PostgreSQL + Redis, ships with Docker Compose, supports 14+ LLM providers (Anthropic, OpenAI, Gemini, DeepSeek, Qwen, MiniMax, Azure, OpenRouter, Zhipu, Kimi, vLLM, Ollama, …).

## Quickstart

**One-shot setup (recommended):**

```bash
git clone https://github.com/rocky2431/hive-agents.git
cd hive-agents
bash setup.sh --dev      # provisions PostgreSQL, venv, frontend deps, seeds the DB
bash restart.sh          # starts backend (:8008) and frontend (:3008)
```

Open http://localhost:3008, register the first user (becomes platform admin), and chat with the HR Agent to create your first employee.

**Or with Docker:**

```bash
cp .env.example .env
docker compose up -d --build    # full stack on http://localhost:3008
```

> [!TIP]
> The HR Agent creates new digital employees through a 2–3 round conversation. Tell it what role you need ("a customer support lead who handles billing escalations"), answer a few clarifying questions, and it generates the soul contract, opening tasks, and starter triggers automatically.

## How it works

```
                   +-----------------------------+
                   |  Frontend (React 19 + Vite) |
                   +--------------+--------------+
                                  |  /api  /ws
                   +--------------v--------------+
                   | Backend (FastAPI + Python)  |
                   +--------------+--------------+
                                  |
       +--------------+-----------+-----------+--------------+
       |              |                       |              |
   PostgreSQL      Redis              Background daemons     Agent FS
   (RLS, async)   (cache, pubsub)    - Trigger (15s tick)    /data/agents/
                                     - Feishu / DingTalk /     {agent_id}/
                                       WeCom / WeChat WS       soul.md
                                     - Heartbeat / Dream       workspace/
                                     - Evolution daemon        memory/
                                                               logs/
                                                               skills/
```

Every agent invocation — whether it comes from a chat message, a webhook, a cron trigger, or another agent delegating — flows through one stateless **kernel**:

```
Entry point  →  invoker.py (resolve deps, build prompt)
             →  kernel/engine.py (multi-round LLM loop, DI-based)
             →  tools/service.py (governed execution)
             →  tools/governance.py (security zone → capability → approval)
```

The kernel has **zero database imports** — all I/O goes through injected callbacks. This means the same kernel runs web chat, Feishu webhooks, scheduled triggers, heartbeat, and agent-to-agent delegation, with identical semantics for context compaction, tool budgets, prompt caching, invocation trace recording, provider retry handling, and governed tool execution.

Web chat is durable: the browser WebSocket subscribes to a background `RuntimeTask(task_type="web_chat_turn")`. If the page is refreshed or temporarily disconnected, the run keeps going and the UI recovers through active-run polling.

## The Memory Pyramid

This is the part that makes Hive feel different from "a chatbot with a vector store."

```
soul.md      ← Dream / Soul Writer     (reviewed soul.md.next, Platform Soul Gate exact commit)
   ↑
T3 memory    ← T3 Consolidator         (LLM pitch + Memory Gate review + Platform Gate exact XML blocks)
   ↑                                     memory/t3/{episodes,user,worker,capabilities}.md
T2 episode   ← Continuity/Episode Agent (synthesis.md / review.md / manifest.json, only for broken/continuing segments)
   ↑
T2 package   ← T0 -> T2 distillers      (summary.md / labels.md / review.md / manifest.json)
   ↑
T0 ledger    ← session ledger           (append-only MD/XML events, segment-sealed resume boundaries)
               raw evidence for chat, tasks, triggers, delegation, heartbeat, and dream
```

| Layer | Where | Written by | What it holds |
|-------|-------|-----------|---------------|
| **T0** | `memory/t0/sessions/<session_id>/segments/<segment_id>/source.md` | web chat, task executor, runtime hooks | Append-only raw MD/XML events — user, assistant, tool, task, trigger, delegation, heartbeat, dream, and segment boundaries |
| **T2 Segment** | `memory/sessions/<session_id>/segments/<t2_segment_id>/{summary.md,labels.md,review.md,manifest.json}` | LLM summary/label agents plus independent Memory Gate review; Platform Gate commits package metadata | One reviewed Segment Package per source session segment, with `source_refs` back to T0 evidence |
| **T2 Episode** | `memory/sessions/<session_id>/episodes/<episode_id>/{synthesis.md,review.md,manifest.json}` | Continuity/Episode Stitcher plus independent Memory Gate review; Platform Gate commits package metadata | Optional stitched episode for adjacent broken/continuing Segment Packages before T3 intake |
| **Explicit overlay** | `memory/explicit/<scope>/...` | `save_memory` for explicit user-commanded memory only | Immediate, scoped memory overlay; later absorbed into T3 only through the same T3 consolidation lane |
| **T3** | `memory/t3/{episodes.md,user.md,worker.md,capabilities.md}` | T3 Consolidator + Memory Gate + Platform Gate exact commit | Curated semantic XML blocks: episodic anchors, user model, worker rules, and capability/SOP seeds |
| **Skill candidates** | `evolution/skill_candidates/<candidate_id>/` | `save_skill`, fast reflection, Skill Distiller | Inactive `SKILL.md.draft` / `candidate_signal.md` packages; active skills require Skill Gate promotion |
| **soul** | `soul.md` | Dream/Soul Writer, through Soul Memory Gate + Platform Soul Gate | Permanent identity — mission, voice, boundaries, and high-stability behavior constitution |

Heartbeat cadence is configuration-backed: `evolution_daemon` dispatches every `HEARTBEAT_TICK_SECONDS` (default 60s), and runnable agents are eligible on the managed `HEARTBEAT_DEFAULT_INTERVAL_MINUTES` cadence (default 120 minutes). Subsequent heartbeat ticks skip when no new T2 entries exist. Full Dream is a slower identity operation: at least 24 hours plus either 3 sessions or 2 productive heartbeats. Soft Dream only does deterministic T3 maintenance and index refresh when T3 is under pressure.

Files are the source of truth for human-readable memory. The only accepted T3 semantic files are the four `memory/t3/*.md` files above; `memory/wiki_map.md` is the single generated navigation read model, not a second memory store and not always-on prompt memory. Legacy `memory/learnings/*.md`, `understandings.md`, root `memory/INDEX.md`, lower-case `memory/index.md`, and `.derived/t3_index.md` are compatibility or retired surfaces, not canonical runtime truth. The legacy learnings extractor is fail-closed by default and only runs with explicit migration env (`HIVE_ENABLE_LEGACY_T2_BACKFILL=1`). No external T3 memory enhancement program is configured by default.

The pyramid is only the storage path. Runtime behavior is governed by the **Memory Control Plane**:

| Layer | What it does |
|-------|--------------|
| Principal stack | Tracks company, direct owner, creator/current user, and delegating agent instead of treating every prompt as the same authority. |
| Privacy + write safety | Classifies memory before persistence; credentials are rejected, PII can be masked, and durable entries carry evidence/lifecycle metadata. |
| Dynamic activation | Retrieves memory by current objective, owner/company relevance, open-loop pressure, retention score, and sensitivity access. |
| Decision trace + feedback | Records why an agent acted, asked, refused, or escalated, then links owner feedback back to the decision that caused it. |
| Session calibration | Stores useful/misleading feedback events and feeds calibrated learnings back through the same T2/T3 write gates. |
| Memory hygiene | Retires legacy shadow stores, quarantines dead stubs, backfills missing lifecycle metadata, and keeps Markdown memory as the canonical source of truth. |
| Coordination runtime | Uses Lease, Signal, Checkpoint, and Sentinel primitives so multi-agent work and confirm-first actions are explicit runtime objects. |
| Proactive steward loop | Heartbeat can prepare useful low-risk artifacts, but external-visible actions require Checkpoint approval and policy changes must pass replay evaluation. |

The design rationale and phase evidence live in [`docs/owner-steward-agent-memory-design.md`](docs/owner-steward-agent-memory-design.md).

## Product surfaces

Hive's app is now split into three surfaces:

| Surface | Routes | Purpose |
|---------|--------|---------|
| App | `/plaza`, `/agents/:id`, `/messages` | Daily agent interaction. `/plaza` is user-facing **Agent Circle**. |
| Company Admin | `/enterprise/*` | Company workbench, model config, memory, HR, tools, skills, quotas, users, org, approvals, audit, invitations. `/dashboard` redirects to `/enterprise/dashboard`. |
| Platform Admin | `/admin/*` | Platform operator settings. |

## Channels

| Channel | Connection | Capabilities |
|---------|-----------|--------------|
| Feishu / Lark | WebSocket + Webhook | Chat, OAuth SSO, Docs, Wiki, Sheets, Base, Tasks, Calendar, approval cards |
| Slack | Bot API | Chat |
| Discord | Bot Gateway | Chat (optional SOCKS5 proxy) |
| DingTalk | Stream SDK | Chat |
| WeChat Work | WebSocket + Webhook | Chat (AES-CBC encrypted) |
| WeChat Personal | Stream bridge | Personal chat bridge |
| Telegram | Bot API | Chat |
| Email | SMTP/IMAP style config | Send, read, reply |
| Microsoft Teams | Bot Framework | Chat |

Channel configs are **per-agent**, so different employees can live in different chat tools simultaneously — sales in Feishu, engineering in Slack, ops in DingTalk — all sharing the same Hive backend and tenant.

## Architecture at a glance

| Layer | Files | Notes |
|-------|-------|-------|
| ORM models | 43 | Tenant-scoped SQLAlchemy models with RLS, runtime tasks, coordination, objectives, identity, invocation spans, session feedback |
| Services | 163 | LLM client, trigger/evolution daemons, channel streamers, memory, office, governance, skills, trace, MCP authz, interoperability |
| Kernel | 1 stateless engine | 200 default max tool rounds · 75% compaction threshold · 50KB tool result eviction · trace spans · thinking signatures |
| Migrations | 79 | Alembic, single-head invariant |
| Frontend pages | 16 page entries + 40 nested page/section helpers | AgentDetail, Agent Circle, Company Admin workbench/settings, Platform Admin |
| Frontend API | 37 domain adapter/test/index files | TanStack Query for server state, Zustand for UI state |

For deeper technical detail, see [`ENGINEERING.md`](ENGINEERING.md) (architecture, invariants, runtime contracts) and [`AGENTS.md`](AGENTS.md) (developer reference for AI coding assistants).

## Tech stack

| Component | Choice |
|-----------|--------|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 async, asyncpg, Pydantic v2 |
| Frontend | React 19, TypeScript 5, Vite 6, React Router 7, TanStack Query 5, Zustand 5 |
| Database | PostgreSQL 15 with row-level security, Redis 7 |
| LLM | Anthropic, OpenAI, Gemini, Azure, DeepSeek, Qwen, MiniMax, OpenRouter, Zhipu, Kimi, vLLM, Ollama, SGLang, custom OpenAI-compatible |
| Migrations | Alembic |
| Lint / format | Ruff (Python), ESLint + Prettier (TypeScript) |
| Tests | pytest (backend), Vitest (frontend) |
| Deployment | Docker Compose, Railway (`backend`, `frontend`, `Postgres`, `Redis`, `onlyoffice-documentserver`) |

## FAQ

### Why should I use Hive instead of LangGraph / AutoGen / CrewAI?

Those are **agent frameworks**. They answer "how does a developer write an agent workflow?" Hive is an **enterprise digital employee operating system**. It answers "how does a company hire, authorize, run, audit, correct, and upgrade a workforce of long-lived digital employees?"

If you only need to compose a few LLM nodes in code, a framework is enough. If you need colleagues to create agents through a UI, connect them to Feishu or Slack, grant tool permissions, set triggers, inspect long-term memory, approve risky actions, audit external behavior, and let the agents improve inside verifiable boundaries, Hive provides the product layer and control plane above the agent runtime.

### What's the deal with `soul.md`?

It's a Markdown file at the root of every agent's workspace describing **who the agent is** — role, primary users, core outputs, operating style, quality standards, boundaries, how it learns. Unlike a system prompt buried in code, the soul is a first-class artifact: editable, versionable, surfaced in the UI. The Dream daemon updates it as the agent grows. The agent's identity literally lives in a file.

### Do I need to be on Feishu / Lark to use this?

No. Feishu has the deepest integration (24 office tools, OAuth SSO, approval cards) because that's where the project started, but every channel is opt-in. You can run Hive entirely with Slack, Discord, or just the built-in web chat at `:3008`.

### Can I run agents fully offline?

Yes. Point the LLM provider at vLLM / Ollama / SGLang or any OpenAI-compatible endpoint. The memory pipeline, hooks, governance, triggers — everything runs locally.

### Is it production-ready?

It runs in production for the maintainers' own teams. The current baseline has gone through two harness/SOTA remediation passes and includes restart-resumable `RuntimeTask` execution, provider retry/fallback, DB tracing, sandboxed code execution, governed memory writes, hard-verification promotion paths, multi-tenant RLS, audit logs, secret encryption, and approval flows.

It is still pre-1.0 in API and schema stability, so upgrades should follow Alembic migrations and release notes. Treat it as a young enterprise system that is built around production closure, not a disposable demo.

### How do I extend it?

Three layers, in increasing order of effort:

1. **Skills** — Progressive-disclosure capability capsules an agent can load on demand. A folder-based Skill can contain instructions, references, templates, scripts, evals, workflow definitions, and subagent definitions; loading it adds context/guidance only, while executable components still run through the governed workflow, subagent/delegation, or sandbox/code runtime.
2. **MCP servers** — Import any [Model Context Protocol](https://modelcontextprotocol.io) server through the UI; tools become discoverable as deferred runtime tool groups.
3. **Native tools** — Add a handler in `backend/app/tools/handlers/`, register it in the runtime, write a governance entry. This is what's needed for tools that touch new credential types or need custom streaming.

## Documentation

- [`AGENTS.md`](AGENTS.md) — Technical reference for AI coding assistants (commands, invariants, conventions)
- [`ENGINEERING.md`](ENGINEERING.md) — Full architecture: kernel, prompt assembly, governance, memory, deployment
- [`CLAUDE.md`](CLAUDE.md) — Project guidance for Claude Code sessions
- [`docs/harness-engineering-audit-2026-06-11.md`](docs/harness-engineering-audit-2026-06-11.md) — Harness audit, remediation log, and verification evidence
- [`docs/round2-sota-benchmark-2026.md`](docs/round2-sota-benchmark-2026.md) — Second-round SOTA benchmark and current improvement roadmap
- [`docs/self-evolution-sota-plan.md`](docs/self-evolution-sota-plan.md) — Canonical self-evolution foundation plan
- [`docs/agent-memory-purity-spec.md`](docs/agent-memory-purity-spec.md) — Memory purity, lifecycle, and hygiene contract

## Acknowledgements

The kernel-with-DI architecture and the memory pipeline are inspired by Claude Code's session lifecycle and the broader agent-harness movement. The Feishu integration drew on first-hand pain from running an agent on lark-cli for several months. Hive keeps durable identity in `soul.md` and operational progress in governed memory, work ledgers, workspace artifacts, and trigger wake policies so the agent's identity does not get rewritten by transient work.

## License

[Apache License 2.0](LICENSE)

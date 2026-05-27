<div align="center">
  <h1>Hive</h1>
  <h3>Open-source digital employees with persistent identity and long-term memory.</h3>
  <p><strong>English</strong> | <a href="README.zh-CN.md">简体中文</a></p>
</div>

<div align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue.svg" alt="License"></a>
  <a href="#"><img src="https://img.shields.io/badge/python-3.12-blue.svg" alt="Python"></a>
  <a href="#"><img src="https://img.shields.io/badge/react-19-61dafb.svg" alt="React"></a>
  <a href="#"><img src="https://img.shields.io/badge/postgres-15-336791.svg" alt="PostgreSQL"></a>
</div>

<br>

Hive is a self-hosted platform for building **digital employees** — AI agents that remember the conversations they have, learn from them autonomously, live inside your team's IM tools, and act on their own when something needs doing. Instead of stateless chatbots that forget everything when the tab closes, Hive agents have an identity contract, a private workspace, a 4-layer memory that consolidates while they "sleep," and a Memory Control Plane that keeps their judgment aligned with their owner and company.

**What makes Hive different:**

- **Persistent identity** — Each agent has a `soul.md` — its role, voice, boundaries, and quality bar. It survives across conversations, sessions, and even model swaps.
- **4-layer memory pyramid + control plane** — Raw logs → learnings → semantic memory → identity, governed by owner/company context, privacy gates, dynamic activation, decision traces, and replay-guarded policy evolution. No manual RAG setup.
- **Heartbeat & Dream** — Background daemons think for the agent while you're away — organizing what it learned, preparing low-risk follow-ups, deciding what's worth keeping, and proposing safe identity/policy evolution.
- **Durable web chat** — Web chat turns run as background `RuntimeTask` jobs. Refreshing or closing the browser disconnects the subscription, not the agent's work.
- **Office workbench** — Agent workspaces now support browser-based DOCX/XLSX/PPTX editing through ONLYOFFICE, with signed callbacks and revision history.
- **Lives in your chat** — First-class connectors for Feishu/Lark, Slack, Discord, DingTalk, WeChat Work, and Microsoft Teams. Same agent, same memory, every channel.
- **Created by conversation** — An HR Agent interviews you in 2–3 rounds and builds a new digital employee for you. No prompt engineering required.
- **Acts on its own** — Cron, interval, webhook, polling, and message-event triggers. Agents wake up to do work, not just answer.
- **Enterprise-ready governance** — Security zones, capability policies, human-in-the-loop approvals, multi-tenant PostgreSQL RLS, full audit trail.
- **60+ tools out of the box** — File I/O, web search, the Feishu office suite, email, OfficeCLI/ONLYOFFICE document workflows, deep research, and MCP server import for anything else.

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
                   |   Backend (FastAPI 3.12)    |
                   +--------------+--------------+
                                  |
       +--------------+-----------+-----------+--------------+
       |              |                       |              |
   PostgreSQL      Redis              Background daemons     Agent FS
   (RLS, async)   (cache, pubsub)    - Trigger (15s tick)    /data/agents/
                                     - Feishu / DingTalk /     {agent_id}/
                                       WeCom / WeChat WS       soul.md
                                     - Heartbeat / Dream       focus.md
                                     - Evolution daemon        workspace/
                                                               memory/
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

The kernel has **zero database imports** — all I/O goes through injected callbacks. This means the same kernel runs web chat, Feishu webhooks, scheduled triggers, heartbeat, and agent-to-agent delegation, with identical semantics for context compaction, tool budgets, and prompt caching.

Web chat is durable: the browser WebSocket subscribes to a background `RuntimeTask(task_type="web_chat_turn")`. If the page is refreshed or temporarily disconnected, the run keeps going and the UI recovers through active-run polling.

## The Memory Pyramid

This is the part that makes Hive feel different from "a chatbot with a vector store."

```
soul.md     ←  Dream         (4h + 3 sessions gate, T3→soul consolidation)
   ↑
T3 memory   ←  Heartbeat     (every 45 min, T2→T3 curation)
   ↑                          feedback / knowledge / strategies / blocked / user
T2 learnings ← Extract Agent (after every response, T0→T2 LLM extraction)
   ↑
T0 raw logs ← t0_logger      (cursor-based, written on session idle/close)
              30-day retention
```

| Layer | Where | Written by | What it holds |
|-------|-------|-----------|---------------|
| **T0** | `logs/YYYY-MM-DD/behavior/` | session hooks | Full conversation MD — every message, tool call, tool result |
| **T2** | `memory/learnings/*.md` | extraction LLM | Atomic learnings: facts, preferences, mistakes, patterns |
| **T3** | `memory/{feedback,knowledge,strategies,blocked,user}.md` | Heartbeat daemon | Curated, deduplicated semantic memory |
| **soul** | `soul.md` | Dream daemon | Permanent identity — role, voice, boundaries |
| **focus** | `focus.md` | agent + heartbeat | Volatile operational priorities |

Files are the source of truth. They're plain Markdown — you can read them, edit them, version them, copy them between deployments. No embeddings to rebuild, no vector store to migrate.

The pyramid is only the storage path. Runtime behavior is governed by the **Memory Control Plane**:

| Layer | What it does |
|-------|--------------|
| Principal stack | Tracks company, direct owner, creator/current user, and delegating agent instead of treating every prompt as the same authority. |
| Privacy + write safety | Classifies memory before persistence; credentials are rejected, PII can be masked, and durable entries carry evidence/lifecycle metadata. |
| Dynamic activation | Retrieves memory by current objective, owner/company relevance, open-loop pressure, retention score, and sensitivity access. |
| Decision trace + feedback | Records why an agent acted, asked, refused, or escalated, then links owner feedback back to the decision that caused it. |
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
| API routers | 55 | Agents, auth, chat sessions, enterprise, channels, admin, Agent Circle/plaza, triggers, office, deep research |
| ORM models | 36 | Tenant-scoped SQLAlchemy models with RLS, runtime tasks, coordination, objectives, identity |
| Services | 130 | LLM client, trigger/evolution daemons, channel streamers, memory, office, governance, skills |
| Tool handlers | 60+ | filesystem · search · communication · email · feishu · office · memory · deep research · plaza · skills · triggers · hr · mcp |
| Kernel | 1 stateless engine | 200 default max tool rounds · 75% compaction threshold · 50KB tool result eviction |
| Migrations | 58 | Alembic, single-head invariant |
| Frontend pages | 16 + 25 sub-sections | AgentDetail, Agent Circle, Company Admin workbench/settings, Platform Admin |
| Frontend API | 25 production domain adapters | TanStack Query for server state, Zustand for UI state |

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

Those are **agent frameworks** — libraries you write code with. Hive is a **multi-agent platform** — a self-hostable product. If you want to give your colleagues a UI to spin up agents, plug them into Feishu, set up cron triggers, review their memory, and approve risky actions — that product layer is what Hive provides on top of an agent runtime.

### What's the deal with `soul.md`?

It's a Markdown file at the root of every agent's workspace describing **who the agent is** — role, primary users, core outputs, operating style, quality standards, boundaries, how it learns. Unlike a system prompt buried in code, the soul is a first-class artifact: editable, versionable, surfaced in the UI. The Dream daemon updates it as the agent grows. The agent's identity literally lives in a file.

### Do I need to be on Feishu / Lark to use this?

No. Feishu has the deepest integration (24 office tools, OAuth SSO, approval cards) because that's where the project started, but every channel is opt-in. You can run Hive entirely with Slack, Discord, or just the built-in web chat at `:3008`.

### Can I run agents fully offline?

Yes. Point the LLM provider at vLLM / Ollama / SGLang or any OpenAI-compatible endpoint. The memory pipeline, hooks, governance, triggers — everything runs locally.

### Is it production-ready?

It runs in production for the maintainers' own teams. It's still pre-1.0 in terms of API stability — expect schema migrations between minor versions (Alembic handles them). Multi-tenant isolation, audit logging, secret encryption, and approval flows are all in place; treat it like a young but earnest enterprise app.

### How do I extend it?

Three layers, in increasing order of effort:

1. **Skills** — Markdown files with frontmatter that an agent can load on demand. Lowest barrier to entry; no code needed.
2. **MCP servers** — Import any [Model Context Protocol](https://modelcontextprotocol.io) server through the UI; tools auto-register as a dynamic pack.
3. **Native tools** — Add a handler in `backend/app/tools/handlers/`, register it in the runtime, write a governance entry. This is what's needed for tools that touch new credential types or need custom streaming.

## Documentation

- [`AGENTS.md`](AGENTS.md) — Technical reference for AI coding assistants (commands, invariants, conventions)
- [`ENGINEERING.md`](ENGINEERING.md) — Full architecture: kernel, prompt assembly, governance, memory, deployment
- [`CLAUDE.md`](CLAUDE.md) — Project guidance for Claude Code sessions

## Acknowledgements

The kernel-with-DI architecture and the 4-layer memory pipeline are inspired by Claude Code's session lifecycle and the broader agent-harness movement. The Feishu integration drew on first-hand pain from running an agent on lark-cli for several months. The `soul.md` / `focus.md` split came from observing what happens when an agent's identity gets edited every time a new tool is installed (it gets confused).

## License

[Apache License 2.0](LICENSE)

<div align="center">
  <h1>Hive</h1>
  <h3>AI-Native Organization OS — Agent-as-a-Service control plane for enterprise digital employees.</h3>
  <p><strong>English</strong> | <a href="README.zh-CN.md">简体中文</a></p>
</div>

<div align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue.svg" alt="License"></a>
  <a href="#"><img src="https://img.shields.io/badge/python-3.12-blue.svg" alt="Python"></a>
  <a href="#"><img src="https://img.shields.io/badge/react-19-61dafb.svg" alt="React"></a>
  <a href="#"><img src="https://img.shields.io/badge/postgres-15-336791.svg" alt="PostgreSQL"></a>
</div>

<br>

Hive is a self-hosted **AI-Native Organization OS**. It gives a company the control plane needed to create, authorize, run, observe, and improve a workforce of long-lived AI digital employees.

The product is not a chatbot wrapper and not just an agent framework. Hive treats each agent as an accountable worker with identity, memory, tools, skills, workspace, runtime state, permissions, audit trails, and evolution paths. The core value is organizational: an enterprise can operate agents as part of the company, not as isolated prompt windows.

## Positioning

Hive can be read in two equivalent ways:

1. **AI-Native Organization SaaS**: a system for companies whose workflows, memory, permissions, and operating rhythm are built around AI workers from day one.
2. **Agent-as-a-Service control plane**: an organization console for creating and governing digital employees that act across tools, channels, files, workflows, and teams.

The north star is simple:

- Build self-evolving agent infrastructure with enterprise-grade access control.
- Build the organization control plane that lets a company safely operate those agents at scale.

## Core Loop

Every product surface eventually feeds the same runtime loop:

```text
User / Trigger / Channel / Agent
        |
        v
ChatSession + RuntimeTask
        |
        v
Context assembly
  identity + company + session + memory + skills + tools + governance
        |
        v
AgentKernel model loop
        |
        v
ToolRuntimeService
  validation + hooks + permission + preflight + execution + audit
        |
        v
Transcript / T0 evidence / artifacts / runtime state
        |
        v
Memory, skill, workflow, and governance feedback loops
```

That is the central design choice. Web chat, channel messages, triggers, workflows, subagents, Agent Team members, and background continuations should not invent separate execution semantics. They become durable session/runtime objects and then flow through the same kernel and governed tool layer.

## What Hive Provides

### 1. Digital Employees

Each agent has:

- A durable identity contract in `soul.md`.
- A private workspace for files and artifacts.
- A long-term memory vault.
- Installed skills and skill candidates.
- Tool and capability policies.
- Owner, tenant, company, and channel context.
- Durable sessions, checkpoints, branches, and runtime tasks.

The goal is not to keep a better chat history. The goal is to make the agent a stable organizational actor.

### 2. Session-Native Runtime

Hive sessions are first-class runtime containers:

- `ChatSession` stores the conversational surface.
- `RuntimeTask` stores the active run handle.
- The WebSocket is only a subscriber; closing the page must not kill the run.
- Checkpoints are navigation anchors; rewind and branch are explicit actions.
- Branch creates another session lineage instead of destructively editing history.
- Rewind projects the current session back to the selected checkpoint state.

The frontend Session Workbench is expected to show this runtime state directly: active run, tools, permissions, compaction, checkpoints, child sessions, Agent Team members, workflows, and background work.

### 3. Governed Tool Calling

All tools go through `ToolRuntimeService.execute()`. The tool layer handles:

- JSON/input validation.
- Pre-tool, post-tool, and failure hooks.
- Session permission profiles.
- Capability and pack policy checks.
- MCP policy checks.
- Action preflight for external-visible or sensitive actions.
- Runtime-owned context injection.
- Timeouts, structured errors, lifecycle frames, and audit records.

Native tools, MCP tools, deferred tools, workflow tools, skill-loading tools, subagent tools, and file/workspace tools all share this governance path.

### 4. Context Assembly

Context is assembled as a layered runtime product, not a single giant prompt:

- Frozen prefix: identity, role, operating contract, `soul.md`, company information, organization structure, and stable prompt sections.
- Dynamic suffix: memory snapshot, memory navigation, retrieval results, skill catalog, runtime metadata, permissions, active tool groups, available deferred tools, and channel/session state.
- User turn envelope: current user input, attachments, selected permission mode, and session metadata.

This split keeps prompt-cacheable identity stable while memory, skills, tools, and runtime state can update every turn.

### 5. Memory and Skill Evolution

Hive separates memory evidence from accepted behavior:

```text
T0 raw session evidence
  -> T2 reviewed segment packages
  -> T3 accepted semantic memory
  -> soul.md and skill candidates
```

Memory write paths must pass the Memory Gate and Platform Gate. Skills are progressive capability capsules: loading a skill adds instructions and references; executable work still goes through governed tools, workflow, subagent, or sandbox runtimes. Active skill changes are promoted through candidate packages and verification gates, not direct self-editing.

### 6. Multi-Agent Work

Hive supports several levels of multi-agent execution:

- `spawn_subagent`: session-local specialist worker with isolated prompt and child-session state.
- Agent Team: a session-local team container; members are created through `spawn_subagent(team_name + name)` and can be entered as addressable sessions.
- Dynamic Workflow: structured workflow runs whose leaves are generally subagent-style workers, with preview, admission, run state, and status projection.
- A2A-style collaboration: relationship and messaging surfaces for cross-agent work when the organization boundary allows it.

These are not the same UI object. Agent Team members can be entered as full sessions; Dynamic Workflow primarily needs run/phase/leaf status unless a leaf exposes a child session.

### 7. Enterprise Governance

Hive is built as a control plane:

- Multi-tenant PostgreSQL with RLS.
- Agent ownership and company context.
- Capability policies and pack policies.
- Permission profiles per session.
- Approval and pending-tool frames.
- MCP import and execution authz.
- Action preflight for sensitive, external, irreversible, or company-boundary actions.
- Invocation spans and transcript events as audit evidence.
- Company Admin and Platform Admin surfaces.

Governance constrains what an agent may do. It should not replace the model's reasoning or starve the agent's context.

## Quickstart

```bash
git clone https://github.com/rocky2431/hive-agents.git
cd hive-agents
bash setup.sh --dev
bash restart.sh
```

Open http://localhost:3008, register the first user, then use the HR / employee creation flow to create your first agent.

Docker:

```bash
cp .env.example .env
docker compose up -d --build
```

Default local ports:

| Service | Port |
|---------|------|
| Frontend | 3008 |
| Backend | 8008 |
| PostgreSQL | 5432 |
| Redis | 6379 |

## Development Commands

Backend:

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8008 --reload
ruff check app/ --fix && ruff format app/
pytest
alembic upgrade head
```

Frontend:

```bash
cd frontend
npm run dev
npm run build
npm test
```

Full local restart:

```bash
bash restart.sh
```

## Architecture Map

| Layer | Primary paths |
|-------|---------------|
| API | `backend/app/api/` |
| Runtime entry | `backend/app/services/web_chat_runtime.py`, `backend/app/runtime/invoker.py` |
| Kernel | `backend/app/kernel/engine.py` |
| Tool governance | `backend/app/tools/service.py`, `backend/app/tools/governance.py` |
| Context assembly | `backend/app/services/agent_context.py`, `backend/app/runtime/prompt_builder.py` |
| Memory | `backend/app/memory/`, `backend/app/services/memory_service.py` |
| Skills | `backend/app/skills/`, `backend/app/services/agent_tool_domains/workspace.py` |
| Workflow | `backend/app/runtime/workflow_*`, `backend/app/tools/handlers/workflow.py` |
| Agent Team | `backend/app/services/agent_team_runtime_service.py`, `backend/app/api/agent_teams.py` |
| Frontend Session UI | `frontend/src/pages/AgentDetail.tsx`, `frontend/src/pages/agent-detail/` |

For the full engineering path, read [`ENGINEERING.md`](ENGINEERING.md). For coding-agent rules, read [`AGENTS.md`](AGENTS.md).

## Tech Stack

| Area | Stack |
|------|-------|
| Backend | Python 3.12, FastAPI, SQLAlchemy async, Pydantic v2 |
| Frontend | React 19, TypeScript 5, Vite 6, React Router 7 |
| State | PostgreSQL 15, Redis 7 |
| Runtime | Durable `RuntimeTask`, session transcript, stateless kernel, governed tools |
| Tests | pytest, Vitest |
| Deployment | Docker Compose, Railway |
| Models | Anthropic, OpenAI, Gemini, DeepSeek, Qwen, MiniMax, Azure, OpenRouter, Zhipu, Kimi, vLLM, Ollama, SGLang, OpenAI-compatible endpoints |

## License

Apache 2.0.

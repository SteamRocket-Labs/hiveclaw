# Local Connector & Unified Workspace Architecture

> Author: Infrastructure Engineer
> Date: 2026-03-20
> Status: Design Proposal
> Depends on: AGENT_NATIVE_EXECUTION_PERMISSION_PROPOSAL.md

---

## 1. Executive Summary

This document designs the technical architecture for two tightly coupled systems:

1. **Local Connector** — a lightweight daemon that runs on the employee's machine, exposing a minimal capability surface to the cloud-hosted Clawith agent runtime.
2. **Unified Workspace Resource** — an abstraction layer that makes local files, cloud-hosted files, Feishu documents, and Git repositories appear as a single, uniform resource namespace to the agent.

**Core principle**: The agent runtime stays in the cloud. The Local Connector is a capability bridge, not a runtime replica.

---

## 2. Local Connector Protocol Design

### 2.1 Transport: WebSocket + JSON-RPC 2.0

**Why WebSocket over HTTP polling:**
- The existing `gateway.py` uses HTTP polling (GET `/poll`). This works for OpenClaw's async model but introduces unacceptable latency for interactive local operations (file reads should be <50ms, not next-poll-cycle).
- WebSocket provides bidirectional streaming, enabling the server to push capability requests to the connector in real-time.
- JSON-RPC 2.0 is chosen over custom protocols because it is the same wire format used by MCP (Model Context Protocol), which Clawith already integrates via `mcp_client.py`. This means the Local Connector can eventually expose itself as an MCP server to external tools.

**Wire format:**

```
Client (Clawith Backend) → Connector (Local Machine)
─────────────────────────────────────────────────────
WebSocket URI: wss://api.clawith.ai/ws/connector/{connector_id}

All frames are JSON-RPC 2.0:

Request (server → connector):
{
  "jsonrpc": "2.0",
  "id": "req-uuid",
  "method": "fs.read",
  "params": {
    "path": "src/main.py",
    "encoding": "utf-8"
  }
}

Response (connector → server):
{
  "jsonrpc": "2.0",
  "id": "req-uuid",
  "result": {
    "content": "import os\n...",
    "size": 1234,
    "mtime": "2026-03-20T10:00:00Z"
  }
}

Error:
{
  "jsonrpc": "2.0",
  "id": "req-uuid",
  "error": {
    "code": -32001,
    "message": "Path outside allowed directory",
    "data": { "path": "/etc/passwd", "allowed": ["/Users/alice/projects/myapp"] }
  }
}
```

### 2.2 Authentication Flow

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│  Clawith UI  │     │  Clawith Backend  │     │  Connector   │
│  (Browser)   │     │  (Control Plane)  │     │  (Local CLI) │
└──────┬───────┘     └────────┬──────────┘     └──────┬───────┘
       │                      │                       │
       │  1. User clicks      │                       │
       │  "Connect Local"     │                       │
       │─────────────────────>│                       │
       │                      │                       │
       │  2. Returns          │                       │
       │  pairing_code +      │                       │
       │  connector_id        │                       │
       │<─────────────────────│                       │
       │                      │                       │
       │  3. User runs:       │                       │
       │  clawith connect     │                       │
       │  --code XXXX-XXXX    │                       │
       │                      │                       │
       │                      │  4. POST /api/connector/pair
       │                      │  { code, machine_fingerprint }
       │                      │<──────────────────────│
       │                      │                       │
       │                      │  5. Returns           │
       │                      │  { connector_token,   │
       │                      │    ws_url,             │
       │                      │    allowed_dirs }      │
       │                      │──────────────────────>│
       │                      │                       │
       │                      │  6. WebSocket connect  │
       │                      │  Authorization: Bearer │
       │                      │  {connector_token}     │
       │                      │<══════════════════════│
       │                      │                       │
```

**Key design decisions:**

- **Pairing code**: 8-character alphanumeric, valid for 5 minutes, single-use. Similar to device pairing flows (Chromecast, VS Code Remote).
- **Connector token**: Long-lived JWT with `connector_id`, `tenant_id`, `user_id`, `allowed_directories[]`. Rotated every 30 days. Stored in `~/.clawith/connector.json`.
- **Machine fingerprint**: SHA-256 of `hostname + platform + username`. Used for audit trail, not security. If the machine changes, re-pairing is required.
- **No shared secrets**: The connector never sees the user's JWT or any agent API keys. It only holds its own connector_token.

### 2.3 Heartbeat & Reconnection

```
Connector → Server: ping every 30s
Server → Connector: pong

If no pong within 10s: reconnect with exponential backoff (1s, 2s, 4s, ..., max 60s)
If connector_token rejected (401): stop, prompt user to re-pair

Server marks connector as offline after 90s of no heartbeat.
Agent tool calls targeting an offline connector return:
  "Connector is offline. Ask the employee to run `clawith connect`."
```

### 2.4 Relationship to Existing `gateway.py`

**Verdict: Extend, not replace.**

| Aspect | OpenClaw Gateway (`gateway.py`) | Local Connector |
|--------|--------------------------------|-----------------|
| Direction | Agent polls server | Server pushes to connector |
| Transport | HTTP polling | WebSocket |
| Auth | API key (per-agent) | Connector token (per-user-machine) |
| Purpose | Remote agent runtime ↔ platform | Platform agent ↔ local filesystem |
| Multiplexing | 1 agent : 1 key | 1 connector : N agents |

**Implementation plan**: Add a new router `backend/app/api/connector.py` alongside `gateway.py`. Both share the same `Agent` model and audit infrastructure. The connector router handles:

- `POST /api/connector/pair` — pairing flow
- `GET /api/connector/status` — list connected connectors for a tenant
- `DELETE /api/connector/{id}` — revoke a connector
- `WS /ws/connector/{connector_id}` — WebSocket endpoint

The WebSocket handler lives in a new file `backend/app/api/connector_ws.py`, co-located with the existing `backend/app/api/websocket.py`.

---

## 3. Minimum Capability Surface API

### 3.1 Method Registry

Every method follows the pattern `{namespace}.{action}`. The connector implements only the methods listed below. Any unlisted method returns JSON-RPC error code `-32601` (Method not found).

```
┌─────────────────────────────────────────────────────────────┐
│                    Capability Methods                        │
├──────────────────┬──────────────────────────────────────────┤
│ fs.read          │ Read file content (text or base64)       │
│ fs.write         │ Write/create file                        │
│ fs.list          │ List directory contents                  │
│ fs.stat          │ Get file metadata (size, mtime, type)    │
│ fs.delete        │ Delete file or empty directory           │
│ fs.search        │ Grep/ripgrep text search                 │
│ fs.watch         │ Subscribe to file change notifications   │
├──────────────────┼──────────────────────────────────────────┤
│ git.status       │ Working tree status                      │
│ git.diff         │ Diff (staged, unstaged, or between refs) │
│ git.log          │ Commit log (limited to 50)               │
│ git.branch       │ List/create/switch branches              │
│ git.add          │ Stage files                              │
│ git.commit       │ Create commit (with message)             │
│ git.pull         │ Pull from remote                         │
│ git.push         │ Push to remote (requires L1 approval)    │
├──────────────────┼──────────────────────────────────────────┤
│ proc.exec        │ Execute shell command                    │
│                  │ ** DEFAULT: DISABLED **                   │
│                  │ Requires explicit tenant-level opt-in     │
│                  │ + per-invocation L3 approval              │
├──────────────────┼──────────────────────────────────────────┤
│ workspace.watch  │ Subscribe to workspace-level events      │
│ workspace.info   │ Return connector metadata + capabilities │
└──────────────────┴──────────────────────────────────────────┘
```

### 3.2 Detailed Method Signatures

```typescript
// fs.read
Request:  { path: string, encoding?: "utf-8" | "base64", offset?: number, limit?: number }
Response: { content: string, size: number, mtime: string, encoding: string, truncated: boolean }

// fs.write
Request:  { path: string, content: string, encoding?: "utf-8" | "base64", create_dirs?: boolean }
Response: { path: string, size: number, mtime: string }

// fs.list
Request:  { path: string, recursive?: boolean, max_depth?: number, glob?: string }
Response: { entries: Array<{ name: string, type: "file"|"dir"|"symlink", size: number, mtime: string }> }

// fs.stat
Request:  { path: string }
Response: { type: "file"|"dir"|"symlink", size: number, mtime: string, permissions: string }

// fs.delete
Request:  { path: string }
Response: { deleted: true }

// fs.search
Request:  { pattern: string, path?: string, glob?: string, max_results?: number, context_lines?: number }
Response: { matches: Array<{ file: string, line: number, content: string, context_before: string[], context_after: string[] }> }

// fs.watch
Request:  { path: string, recursive?: boolean }
Response: (notification, not request-response)
Notification: { method: "fs.changed", params: { path: string, event: "created"|"modified"|"deleted" } }

// git.status
Request:  { path?: string }
Response: { branch: string, ahead: number, behind: number, staged: string[], modified: string[], untracked: string[] }

// git.diff
Request:  { path?: string, staged?: boolean, ref1?: string, ref2?: string }
Response: { diff: string, stats: { insertions: number, deletions: number, files_changed: number } }

// git.log
Request:  { path?: string, limit?: number, since?: string }
Response: { commits: Array<{ hash: string, message: string, author: string, date: string }> }

// git.commit
Request:  { message: string, paths?: string[] }
Response: { hash: string, message: string }

// git.push
Request:  { remote?: string, branch?: string, force?: boolean }
Response: { remote: string, branch: string, status: string }

// proc.exec
Request:  { command: string, args?: string[], cwd?: string, timeout_ms?: number, env?: Record<string,string> }
Response: { exit_code: number, stdout: string, stderr: string, duration_ms: number }
Constraints:
  - timeout_ms max: 300000 (5 min)
  - stdout/stderr max: 1MB each (truncated with warning)
  - No shell expansion unless explicitly `command: "sh"` with `args: ["-c", "..."]`

// workspace.info
Request:  {}
Response: {
  connector_version: string,
  platform: string,
  hostname: string,
  allowed_directories: string[],
  capabilities: string[],   // e.g. ["fs.*", "git.*"] — proc.exec only if enabled
  git_repos: Array<{ path: string, branch: string, remote_url?: string }>
}
```

### 3.3 Capability ↔ Permission Mapping

Every JSON-RPC method maps to a capability key in the proposed capability model:

| Method | Capability Key | Default Approval Level |
|--------|---------------|----------------------|
| `fs.read` | `local.fs.read` | L1 (free) |
| `fs.write` | `local.fs.write` | L1 (free) |
| `fs.list` | `local.fs.read` | L1 (free) |
| `fs.stat` | `local.fs.read` | L1 (free) |
| `fs.delete` | `local.fs.delete` | L2 (approval for non-workspace) |
| `fs.search` | `local.fs.read` | L1 (free) |
| `git.status/diff/log` | `local.git.read` | L1 (free) |
| `git.add/commit/branch` | `local.git.write` | L1 (free) |
| `git.push` | `local.git.push` | L2 (approval required) |
| `git.pull` | `local.git.write` | L1 (free) |
| `proc.exec` | `local.proc.exec` | L3 (explicit approval each time) |

---

## 4. Unified Workspace Resource Abstraction

### 4.1 The Problem

Currently, `agent_tools.py` hardcodes paths relative to `WORKSPACE_ROOT / agent_id`:
- `tasks.json`, `soul.md`, `memory/`, `skills/`, `workspace/`

When Local Connector is added, the agent needs to read files from three different backing stores through the same tool interface:

1. **Hosted workspace** — files on the Clawith server filesystem
2. **Local connector** — files on the employee's machine
3. **External documents** — Feishu docs, Google Docs, Notion pages

### 4.2 Resource URI Scheme

Introduce a URI scheme that the agent uses in tool calls:

```
hosted://workspace/report.md          → server filesystem
hosted://memory/memory.md             → server filesystem (agent memory)
hosted://skills/research.md           → server filesystem (skill file)
local://{connector_id}/src/main.py    → local connector
local://{connector_id}/docs/spec.pdf  → local connector
feishu://doc/{doc_token}              → Feishu document API
git://{connector_id}/repo/.git        → git operations via local connector
```

**Backward compatibility**: If the agent passes a bare path like `workspace/report.md`, it is implicitly treated as `hosted://workspace/report.md`. Zero breaking changes for existing agents.

### 4.3 Resource Router Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                     Agent Runtime (invoker.py)                │
│                                                              │
│  Tool call: read_file(path="local://conn-1/src/main.py")    │
│                          │                                    │
│                          ▼                                    │
│              ┌─────────────────────┐                         │
│              │  Workspace Router    │                         │
│              │  (resource_router.py)│                         │
│              └──────┬──────────────┘                         │
│                     │                                         │
│         ┌───────────┼───────────────┐                        │
│         ▼           ▼               ▼                        │
│   ┌──────────┐ ┌──────────┐  ┌──────────────┐              │
│   │ Hosted   │ │ Local    │  │ External Doc │              │
│   │ Provider │ │ Provider │  │ Provider     │              │
│   └──────────┘ └──────────┘  └──────────────┘              │
│         │           │               │                        │
│         ▼           ▼               ▼                        │
│   Local FS    WebSocket to     Feishu/Notion                │
│   (current)   Connector        REST API                     │
└──────────────────────────────────────────────────────────────┘
```

### 4.4 Provider Interface

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class ResourceEntry:
    name: str
    type: str          # "file" | "dir" | "symlink" | "document"
    size: int | None
    mtime: str | None
    uri: str           # full URI for subsequent operations

@dataclass
class ResourceContent:
    content: str
    encoding: str      # "utf-8" | "base64"
    size: int
    mtime: str | None
    truncated: bool

class WorkspaceProvider(ABC):
    """Base class for all workspace resource providers."""

    @abstractmethod
    async def read(self, path: str, offset: int = 0, limit: int | None = None) -> ResourceContent: ...

    @abstractmethod
    async def write(self, path: str, content: str, encoding: str = "utf-8") -> ResourceEntry: ...

    @abstractmethod
    async def list(self, path: str, recursive: bool = False) -> list[ResourceEntry]: ...

    @abstractmethod
    async def stat(self, path: str) -> ResourceEntry: ...

    @abstractmethod
    async def delete(self, path: str) -> bool: ...

    @abstractmethod
    async def search(self, pattern: str, path: str = "", max_results: int = 50) -> list[dict]: ...
```

**Implementations:**

| Provider | Backing Store | Location |
|----------|-------------|----------|
| `HostedWorkspaceProvider` | Local filesystem (existing) | `backend/app/services/workspace/hosted.py` |
| `LocalConnectorProvider` | WebSocket → connector | `backend/app/services/workspace/local.py` |
| `FeishuDocProvider` | Feishu Open API | `backend/app/services/workspace/feishu_doc.py` |
| `GitProvider` | Wraps connector's git.* methods | `backend/app/services/workspace/git.py` |

### 4.5 Integration with `agent_tools.py`

The existing `execute_tool` function dispatches by tool name. With the resource router, the change is surgical:

```python
# Current (agent_tools.py):
async def _read_file(args, agent_id, ...):
    path = _resolve_path(agent_id, args["path"])
    content = path.read_text()
    return content

# New (with resource router):
async def _read_file(args, agent_id, ...):
    uri = args["path"]
    router = get_workspace_router(agent_id)
    result = await router.read(uri)
    return result.content
```

The `get_workspace_router(agent_id)` factory checks which connectors are online for this agent's tenant and builds the appropriate provider chain.

---

## 5. Security Boundaries

### 5.1 Directory Allowlist

The connector enforces a strict directory allowlist. This is the **primary security boundary**.

```
Configuration (set during pairing, stored in connector token):
{
  "allowed_directories": [
    "/Users/alice/projects/myapp",
    "/Users/alice/Documents/reports"
  ]
}

Rules:
1. ALL path parameters are resolved to absolute paths and checked against allowlist
2. Symlinks are resolved BEFORE checking (no symlink escape)
3. Dotfiles (.git, .env) are accessible within allowed dirs
4. Parent traversal (../) is resolved and checked
5. Paths outside allowlist return error code -32001
```

### 5.2 Sensitive File Filtering

Even within allowed directories, certain files are blocked by default:

```
Blocked patterns (configurable per-tenant):
- **/.env*           → environment files with secrets
- **/*secret*        → files with "secret" in name
- **/*.pem           → private keys
- **/*.key           → private keys
- **/id_rsa*         → SSH keys
- **/.git/config     → may contain credentials
- **/credentials*    → credential files
- **/*.p12           → certificate stores
```

The connector checks these patterns locally. The server never sees the content of blocked files.

### 5.3 `proc.exec` Sandboxing

When `proc.exec` is enabled (opt-in per tenant):

```
Restrictions:
1. Command allowlist (configurable):
   Default allowed: ["ls", "cat", "grep", "find", "wc", "head", "tail",
                     "git", "npm", "pnpm", "yarn", "node", "python", "pip",
                     "cargo", "go", "make", "ruff", "pytest", "vitest"]
   Default blocked: ["rm -rf", "sudo", "chmod", "chown", "kill", "pkill",
                     "curl | sh", "wget | sh", "dd", "mkfs", "fdisk"]

2. Working directory: MUST be within allowed_directories
3. Timeout: Max 5 minutes per invocation
4. Output: Max 1MB stdout + 1MB stderr
5. No background processes (no &, nohup, disown)
6. No network listeners (no binding to ports)
```

### 5.4 Rate Limiting

```
Per-connector limits:
- fs.read:    100 req/s (burst 200)
- fs.write:   20 req/s  (burst 50)
- fs.search:  10 req/s  (burst 20)
- git.*:      30 req/s  (burst 60)
- proc.exec:  2 req/s   (burst 5)
- Total:      200 req/s  (burst 400)
```

### 5.5 Audit Trail

Every capability invocation through the connector is logged:

```python
# Logged to ExecutionRecord (proposed in AGENT_NATIVE_EXECUTION_PERMISSION_PROPOSAL.md)
{
    "trace_id": "uuid",
    "agent_id": "uuid",
    "connector_id": "uuid",
    "user_id": "uuid",           # owner of the connector
    "capability_key": "local.fs.read",
    "method": "fs.read",
    "params": {"path": "src/main.py"},
    "result_summary": "ok, 1234 bytes",  # never log full content
    "duration_ms": 12,
    "timestamp": "2026-03-20T10:00:00Z"
}
```

---

## 6. Deployment Form Factor Recommendation

### 6.1 Analysis of Options

| Form Factor | Pros | Cons | Verdict |
|-------------|------|------|---------|
| **CLI binary** | Simplest, cross-platform, scriptable, easy to update | No GUI, needs terminal | **Primary** |
| **Electron app** | GUI, tray icon, auto-start | 100MB+ download, maintenance burden | Phase 2 |
| **Docker sidecar** | Isolation, reproducible | Overkill for file access, Docker required | Not recommended |
| **VS Code extension** | Integrated into dev workflow | Limited to VS Code users | Nice-to-have |
| **System service** | Always-on, auto-start | OS-specific install, harder to debug | Phase 2 |

### 6.2 Recommended: Single-binary CLI (Phase 1)

```bash
# Install
curl -fsSL https://get.clawith.ai/connector | sh
# or
brew install clawith-connector
# or
pip install clawith-connector

# Pair with platform
clawith connect --code XXXX-XXXX --dir ~/projects/myapp --dir ~/Documents/reports

# Run (foreground)
clawith connector start

# Run (background with auto-restart)
clawith connector start --daemon

# Status
clawith connector status

# Revoke
clawith connector disconnect
```

**Technology choice**: Single static binary built with Go or Rust.

- Go is preferred because: the Clawith ecosystem already has Go experience (OpenViking uses Go for AGFS), goroutines map naturally to concurrent WebSocket + filesystem operations, cross-compilation is trivial.
- The binary includes: WebSocket client, JSON-RPC handler, filesystem watcher (via fsnotify), git CLI wrapper, process executor.
- Target size: <15MB single binary, no runtime dependencies.

### 6.3 Phase 2: Electron/Tauri Wrapper

Wrap the CLI binary in a Tauri shell (Rust + webview, ~5MB overhead):
- System tray icon showing connection status
- Auto-start on login
- GUI for managing allowed directories
- Notification when agent accesses files

---

## 7. Integration with Existing Runtime (`invoker.py`)

### 7.1 Current Flow

```
invoker.py → get_agent_tools_for_llm() → AGENT_TOOLS list → execute_tool()
                                                                    │
                                                              agent_tools.py
                                                              (local filesystem only)
```

### 7.2 New Flow

```
invoker.py → get_agent_tools_for_llm() → AGENT_TOOLS + connector_tools → execute_tool()
                                                                               │
                                                                    ┌──────────┴──────────┐
                                                                    │                      │
                                                              agent_tools.py        resource_router.py
                                                              (hosted workspace)    (local/external)
                                                                                          │
                                                                                 ┌────────┼────────┐
                                                                                 │        │        │
                                                                            connector  feishu   git
                                                                            WebSocket  API      via connector
```

### 7.3 Integration Points

**1. Tool discovery** (`agent_tools.py: get_agent_tools_for_llm`)

When an agent has connected Local Connectors, additional tools are dynamically added:

```python
async def get_agent_tools_for_llm(agent_id, core_only=True):
    tools = list(AGENT_TOOLS)  # existing hosted workspace tools

    # Check for online connectors
    connectors = await get_online_connectors(agent_id)
    if connectors:
        tools.extend(build_connector_tools(connectors))

    return tools
```

The connector tools reuse the same `read_file`, `write_file` names but accept `local://` URIs. The agent sees a unified interface.

**2. Tool execution** (`agent_tools.py: execute_tool`)

```python
async def execute_tool(tool_name, args, agent_id, user_id, ...):
    path = args.get("path", "")

    if path.startswith("local://"):
        # Route to connector
        return await resource_router.execute(agent_id, tool_name, args)
    elif path.startswith("feishu://"):
        # Route to Feishu document provider
        return await resource_router.execute(agent_id, tool_name, args)
    else:
        # Existing hosted workspace logic (unchanged)
        return await _execute_hosted(tool_name, args, agent_id, user_id)
```

**3. Capability check** (new, before execution)

```python
async def execute_tool(tool_name, args, agent_id, user_id, ...):
    # NEW: capability check before execution
    capability_key = resolve_capability_key(tool_name, args)
    approval = await check_capability(agent_id, capability_key)
    if approval.requires_human:
        raise ApprovalRequired(capability_key, approval.reason)

    # ... existing dispatch logic
```

This hooks into the proposed Capability Policy from the proposal document (Section 6.3).

**4. Memory and context** (`invoker.py`)

No changes needed. The `invoke_agent` function is agnostic to where the tool reads/writes. It only sees tool call results as strings, which the unified workspace abstraction provides.

---

## 8. Connector Lifecycle

### 8.1 State Machine

```
                    ┌─────────┐
         pair code  │         │  token expired / revoked
        ──────────> │ PAIRED  │ ────────────────────────────┐
                    │         │                              │
                    └────┬────┘                              ▼
                         │                            ┌───────────┐
                    WS connect                        │ REVOKED   │
                         │                            └───────────┘
                         ▼
                    ┌─────────┐
                    │ ONLINE  │ <────── reconnect
                    │         │
                    └────┬────┘
                         │
                    90s no heartbeat
                         │
                         ▼
                    ┌─────────┐
                    │ OFFLINE │ ────── user restarts ──> ONLINE
                    │         │
                    └─────────┘
```

### 8.2 Database Model

```python
class Connector(Base):
    __tablename__ = "connectors"

    id = Column(UUID, primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID, ForeignKey("tenants.id"), nullable=False)
    user_id = Column(UUID, ForeignKey("users.id"), nullable=False)
    name = Column(String, nullable=False)               # e.g. "Alice's MacBook"
    machine_fingerprint = Column(String, nullable=False)
    allowed_directories = Column(JSONB, nullable=False)  # ["/Users/alice/projects"]
    capabilities = Column(JSONB, nullable=False)         # ["fs.*", "git.*"]
    token_hash = Column(String, nullable=False)
    status = Column(String, default="paired")            # paired/online/offline/revoked
    last_seen_at = Column(DateTime(timezone=True))
    version = Column(String)                             # connector binary version
    platform = Column(String)                            # "darwin-arm64", "linux-x64"
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
```

### 8.3 Multi-Agent Access

One connector serves all agents in the same tenant. The connector authenticates as a user, not an agent. When the agent runtime needs to read a local file:

1. Runtime resolves which connector(s) belong to the file owner
2. Runtime checks the agent's capability grants for `local.fs.read`
3. Runtime sends the JSON-RPC request through the connector's WebSocket
4. Connector executes and returns result

This means the connector is **tenant-scoped, user-owned**, not agent-scoped. An agent that needs local file access must have a CapabilityGrant for `local.fs.read` scoped to a specific connector or user.

---

## 9. File Structure

```
backend/app/
├── api/
│   ├── connector.py          # REST endpoints: pair, status, revoke
│   └── connector_ws.py       # WebSocket handler for connector communication
├── models/
│   └── connector.py          # Connector SQLAlchemy model
├── services/
│   └── workspace/
│       ├── __init__.py        # WorkspaceProvider ABC + ResourceRouter
│       ├── hosted.py          # HostedWorkspaceProvider (current filesystem logic)
│       ├── local.py           # LocalConnectorProvider (WebSocket → connector)
│       ├── feishu_doc.py      # FeishuDocProvider (future)
│       └── git.py             # GitProvider (wraps connector git.* methods)
└── schemas/
    └── connector.py           # Pydantic schemas for connector API

connector-cli/                 # Separate repo or subdirectory
├── cmd/
│   ├── connect.go             # Pairing flow
│   ├── start.go               # Main daemon loop
│   ├── status.go              # Show status
│   └── disconnect.go          # Revoke and clean up
├── internal/
│   ├── ws/                    # WebSocket client
│   ├── rpc/                   # JSON-RPC handler + method registry
│   ├── fs/                    # Filesystem operations with allowlist check
│   ├── git/                   # Git CLI wrapper
│   ├── proc/                  # Process execution with sandboxing
│   └── watch/                 # Filesystem watcher (fsnotify)
├── go.mod
└── Makefile
```

---

## 10. Key Conclusions

1. **Protocol**: WebSocket + JSON-RPC 2.0, not HTTP polling. Aligns with MCP wire format for future interop.

2. **Relationship to gateway.py**: Local Connector is a **new, parallel system** alongside the OpenClaw gateway. They share infrastructure (Agent model, audit, tenant scoping) but serve different purposes: gateway is for remote agent runtimes, connector is for local capability bridging.

3. **Minimum capability surface**: `fs.*` and `git.*` enabled by default; `proc.exec` disabled by default and requires tenant opt-in + per-invocation approval. This matches the proposal's recommendation.

4. **Unified Workspace Resource**: URI-based routing (`hosted://`, `local://`, `feishu://`) with a common `WorkspaceProvider` interface. Backward compatible — bare paths default to `hosted://`.

5. **Security**: Directory allowlist is the primary boundary, enforced at the connector (not the server). Sensitive file patterns blocked by default. All operations audited.

6. **Deployment**: Go single-binary CLI for Phase 1. Tauri GUI wrapper for Phase 2. Docker sidecar is not recommended (adds complexity without isolation benefit for filesystem access).

7. **Integration with runtime**: Surgical changes to `agent_tools.py` — URI prefix routing in `execute_tool()`. No changes to `invoker.py`. Dynamic tool list expansion when connectors are online.

8. **Connector is user-scoped, not agent-scoped**: One connector per user-machine pair, serving all agents in the tenant. Agent access controlled by CapabilityGrant.

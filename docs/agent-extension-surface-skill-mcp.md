# Agent Extension Surface: Skill + MCP

| Field | Value |
|------|-------|
| Status | Design draft |
| Date | 2026-06-02 |
| Scope | Agent/user extension surface, company admin extension surface, MCP server management, pack/package removal from user surface, MCP server identity migration |
| Non-goal | Implementing the migration in this document |

## 0. Decision

Hive's user-facing agent extension surface should expose only two module types:

1. **Skill**: reusable agent behavior, workflow, prompt instructions, references, templates, scripts, examples, and evals.
2. **MCP Server**: external tool/resource connector installed and governed as one server-level integration.

Everything else is internal:

- `package`, `pack`, `capability pack`, `web_pack`, `office_pack`, `deep_research_pack`, `plan_mode_pack`, and similar tool-group names must not be shown as installable user modules.
- `capability` remains a governance/policy concept, not an agent extension module.
- `Tool` remains an execution primitive, not the primary user-facing unit for MCP installation.

The product model should be:

```text
Agent Extensions
  - Skills
  - MCP Servers

Internal Runtime
  - tools
  - runtime tool groups
  - capability policies
  - active tool group events
```

## 1. Why This Matters

The current codebase has already chosen Skill + MCP as the install model. `backend/app/services/capability_install_service.py` builds install records for `platform_skill`, `clawhub_skill`, `mcp_server`, and external skill URLs. It does not define `pack` as an install kind.

That makes pack a legacy runtime grouping concept, not a clean product module. The main problem is not that every user literally sees raw names like `web_pack` everywhere. Some frontend places translate internal names into labels such as "Office" or "Deep Research". The real problem is that the codebase still maintains two parallel product concepts:

- Skill/MCP as the install surface.
- Pack/package as an exposed API/runtime surface.

The current pack leakage is concentrated in these places:

- `backend/app/tools/packs.py` defines static tool groups such as `web_pack`, `feishu_pack`, `mcp_admin_pack`, `office_pack`, `deep_research_pack`, and `plan_mode_pack`.
- `backend/app/api/packs.py` exposes `/packs`, `/agents/{agent_id}/packs`, `/enterprise/packs/policies`, `/agents/{agent_id}/capability-summary`, and `/chat/sessions/{session_id}/runtime-summary`. The same file also hosts the `/enterprise/mcp-servers` route family, so the pack routes that get removed and the MCP routes that get reshaped share one module (see §7.3).
- `backend/app/services/pack_service.py` returns fields such as `available_packs`, `channel_backed_packs`, `skill_declared_packs`, and `activated_packs`.
- `backend/app/services/chat_message_parts.py` serializes `pack_activation` events with the title "Capability Packs Activated", but the events are actually emitted upstream in `backend/app/runtime/invoker.py` (three sites) and `backend/app/kernel/engine.py`. The rename must cover the emit sources, not just the serializer.
- `backend/app/services/mcp_registry_service.py` derives MCP `server_key` and `pack_name` from `make_mcp_server_pack_name()`, so MCP servers do not yet have stable first-class identity. Note `make_mcp_server_pack_name()` is defined twice — in `mcp_registry_service.py` and in `backend/app/tools/packs.py` — so removing it from the product path means retiring both definitions.
- `frontend/src/pages/agent-detail/ToolsManager.tsx` fetches `/agents/{agent_id}/packs` to organize visible tools.
- `frontend/src/pages/agent-detail/AgentChatSection.tsx` can render activated pack names from runtime event metadata.

This creates the wrong product mental model. Users should not manage runtime tool groups. They should decide:

- Which skills does this agent know how to use?
- Which MCP servers is this agent connected to?
- Which permissions does the company allow, deny, or require approval for?

The target framing is therefore:

```text
Eliminate pack/package as a product concept.
Give MCP servers their own stable identity.
Keep runtime tool groups internal.
```

## 2. Terms

| Term | User-visible? | Meaning |
|------|---------------|---------|
| Skill | Yes | Agent behavior package: instructions plus optional resources. |
| MCP Server | Yes | External integration installed as one connector. It may expose many tools/resources internally. |
| Tool | Mostly no | Executable function surface used by the runtime. Tool-level controls may appear only in advanced settings. |
| Capability | Admin governance only | Permission category such as file write, external web read, MCP call, Feishu send. |
| Pack / Package | No | Legacy/internal runtime grouping of tools. Not an installable agent module. |
| Runtime Tool Group | Internal/debug | Replacement naming for internal packs. Used for lazy tool loading, prompt continuity, and diagnostics. |

## 3. Target Product Surfaces

### 3.1 Agent Detail

Agent detail should show:

1. **Skills**
   - Agent workspace skills.
   - Imported preset skills.
   - Imported ClawHub skills.
   - Imported GitHub/external skill URLs.

2. **MCP Servers**
   - Server name.
   - Connection status.
   - Auth status.
   - Tool/resource count.
   - Enabled/disabled state for this agent.
   - Optional "advanced tool controls" drawer.

3. **Permissions**
   - Coarse capability policies.
   - Approval requirements.
   - Security zone.

Agent detail should not show:

- `available_packs`
- `channel_backed_packs`
- `skill_declared_packs`
- `web_pack`
- `office_pack`
- `deep_research_pack`
- `mcp_admin_pack`
- `plan_mode_pack`

### 3.2 Company Admin

Company admin should show:

1. **Skill Registry**
   - Company-wide preset skills.
   - Skill import settings.
   - Skill lifecycle/evals where relevant.

2. **MCP Servers**
   - Tenant-managed MCP servers.
   - Server-level install/delete/config/auth.
   - Which agents can use each server.
   - Optional per-server advanced tool allowlist.

3. **Governance / Permissions**
   - Capability policies.
   - Approval modes.
   - Audit events.

Company admin should not show "pack policies" as a product UI. If operators still need runtime-tool-group diagnostics, expose that only through an explicit internal/operator route, not company admin navigation.

## 4. MCP Management Model

### 4.1 Install One MCP Server, Manage One MCP Server

Installing an MCP integration should create one tenant-visible `MCPServer` object.

Example:

```text
Install GitHub MCP
  -> MCP Server: GitHub
     status: connected
     tools: 18
     agents: Researcher, Engineer
```

The default management unit is the server, not every individual tool.

This prevents the current UX problem:

```text
Bad:
  User installs GitHub MCP and immediately sees 18 unrelated tool toggles.

Good:
  User installs GitHub MCP and sees one GitHub integration.
  If they need precision, they open "Advanced tool access" and select tool-level overrides.
```

### 4.2 Default Tool Exposure

MCP tools should follow this default:

```text
Server enabled for agent = runtime may use server tools when relevant.
Server disabled for agent = runtime cannot use any tool from that server.
```

Tool-level control is optional and advanced:

```text
Server: GitHub MCP
  Enabled for agent: yes
  Tool policy: default allow
  Advanced overrides:
    - create_issue: approval required
    - delete_repository: denied
    - list_repositories: auto
```

The UI control for precise management should be a dropdown/drawer under the server:

```text
[GitHub MCP]  Connected  18 tools  [Enabled]
  Advanced tool controls
    Tool dropdown: [create_issue v]
    Mode: [Auto | Approval | Deny]
```

### 4.3 Server Identity

MCP server identity should be stable and visible:

```text
MCPServer {
  id: uuid
  tenant_id: uuid
  name: "GitHub"
  server_key: "github"
  transport: "sse" | "stdio" | "streamable_http"
  server_url: string | null
  registry_source: "smithery" | "direct" | "manual" | "system"
  status: "connected" | "needs_auth" | "failed" | "disabled"
  auth_status: "none" | "configured" | "expired" | "error"
  tool_count: int
}
```

The existing `mcp_server_name` and `mcp_server_url` on `Tool` can remain as compatibility fields, but UI should group by a server record, not by raw tool rows.

Current implementation note:

- `/enterprise/mcp-servers` already exists.
- It currently groups `Tool(type="mcp")` rows by `mcp_server_name` and `mcp_server_url`.
- Its delete route uses `/enterprise/mcp-servers/{server_key}`.
- `server_key` currently comes from `make_mcp_server_pack_name()`, for example `mcp_server:github`.
- The registry response still returns `pack_name`.

So the target work is not "add MCP endpoints from nothing". The target work is to migrate from dirty pack-derived server identity to stable MCP server records, then reshape the existing endpoints around those records.

### 4.4 Agent Assignment

MCP server assignment should be explicit:

```text
AgentMCPServerAssignment {
  tenant_id: uuid
  agent_id: uuid
  mcp_server_id: uuid
  enabled: bool
  default_tool_mode: "auto" | "approval" | "deny"
}
```

Tool-level overrides are optional:

```text
AgentMCPToolOverride {
  tenant_id: uuid
  agent_id: uuid
  mcp_server_id: uuid
  tool_name: string
  mode: "auto" | "approval" | "deny"
}
```

This makes "install one MCP, manage one MCP" the default, while preserving precise control.

### 4.5 Self-Service Install Path

Agents can install MCP servers themselves through the tool group currently named `mcp_admin_pack`. That tool group is internalized as a runtime tool group like every other pack (§7.1), but its product effect must be wired to the new model: when an agent or admin installs an MCP through these tools, the install creates one `MCPServer` record plus the relevant `AgentMCPServerAssignment`, exactly like a UI-driven install. The install path is a runtime entry point; the durable result is still a first-class MCP server record, never a `mcp_server:*` pack name. `capability_install_service` already treats `mcp_server` as the install unit, so this path must feed the same record creation, not a parallel one.

## 5. Skill Model

Skills are the normal way to expand an agent's behavior. A skill may declare tools or runtime tool groups internally, but users should not manage those groups directly.

Allowed user-facing skill actions:

- create/edit/delete agent-local skill files
- import preset skill
- import ClawHub skill
- import GitHub/external skill
- pin/unpin skill
- inspect skill resources

Internal skill behavior:

- `load_skill` can still trigger lazy tool expansion.
- `save_skill` can still validate declared tool groups for safety.
- Skill loaders may read existing `hive.pack` or `declared_packs` as legacy input, but new skill authoring should describe required tools/permissions without naming user-visible packs.

But the user-facing wording should say "this skill may use these tools" or "requires these permissions", not "this skill installs a package".

## 6. Single-Release Cutover Rule

This migration should not become a long-running "hide first, rename later, clean up later" compatibility project. That would likely become permanent debt because pack state already touches runtime, prompt context, chat events, API responses, and frontend grouping.

A single release does not mean a single undifferentiated change. Code rollback and database rollback are different problems. Once a migration creates new MCP server rows, backfills assignments, or new code writes to new tables, "roll back the release" is not enough unless a tested down migration or forward-fix path exists.

The correct plan is **one release with two hard ordering constraints inside it**:

```text
One release changes product surface, APIs, runtime naming, frontend UI, chat events,
and data migration together.

Inside that release the work is ordered, not shipped as two product releases:
  First:  MCP data foundation — add server tables, backfill, validate parity.
  Then:   Product/runtime cutover — switch APIs, runtime naming, frontend, and chat
          events to Skill + MCP only.
```

These two ordering constraints are non-negotiable even in a single release:

1. The MCP data migration runs first and ships with a tested down migration or forward-fix path. Code rollback alone cannot undo backfilled MCP server rows.
2. Parity is validated — new server records reproduce the existing MCP registry result — before the same release flips product/API/runtime to MCP-only. A parity failure blocks the cutover within the release.

First in the release — data foundation (must pass parity before cutover):

- Add `MCPServer`, `MCPServerTool`, `AgentMCPServerAssignment`, and `AgentMCPToolOverride`.
- Backfill existing `Tool(type="mcp")` rows into MCP server records.
- Backfill existing `AgentTool` MCP rows into server assignments and precise overrides.
- Add read-side parity tests proving the new server records reproduce the existing MCP registry result.
- Ship a tested rollback or forward-fix path for the migration itself.

Then in the same release — product/runtime cutover:

- Make `/agents/{agent_id}/extensions` the normal Agent Detail source of truth.
- Reshape `/enterprise/mcp-servers` around stable MCP server records.
- Add agent MCP assignment APIs.
- Remove normal product exposure of pack APIs and pack DTO fields.
- Rename runtime pack naming to runtime-tool-group naming.
- Stop new writes of `pack_activation` and `active_packs`.
- Update frontend and i18n to Skill + MCP wording only.

Allowed compatibility:

- Old persisted chat messages may contain `pack_activation`; readers may map them to `tool_group_activation`.
- Old persisted session context may contain `active_packs`; loaders may map it to `active_tool_groups`.
- Old tenant settings may contain pack policy keys. They live in `SystemSetting` under `tenant:{tenant_id}:policies` (written by `set_tenant_pack_policy`), so they are real persisted governance state, not transient config. The migration classifies each key per pack: a pack that maps to a surviving runtime tool group moves to an internal runtime-tool-group policy key; a pack with no surviving governance meaning is deleted. "Move or delete" is a per-key decision, not a blanket drop.
- Existing `Tool.mcp_server_name` and `Tool.mcp_server_url` may remain as compatibility fields until all MCP imports write server records.

Disallowed compatibility after this release:

- No new runtime event may emit `pack_activation`.
- No new prompt section may say `Active Capability Packs`.
- No normal frontend route may call `/packs`, `/agents/{agent_id}/packs`, or `/enterprise/packs/policies`.
- No normal frontend route may consume `/agents/{agent_id}/capability-summary` or `/chat/sessions/{session_id}/runtime-summary` fields named `available_packs`, `skill_declared_packs`, `channel_backed_packs`, or `activated_packs`.
- No company admin page may expose pack/package/capability-pack management.
- No MCP server response shown to the UI may present `pack_name` as product state.
- No MCP server identity may depend on `make_mcp_server_pack_name()`.
- No dual product surface where both "packs" and "MCP/Skill extensions" are visible.

## 7. Complete Target Architecture

### 7.1 Internal Runtime Tool Groups

Replace the pack naming layer completely inside runtime code.

| Current | Target |
|---------|--------|
| `backend/app/tools/packs.py` | `backend/app/tools/runtime_tool_groups.py` |
| `ToolPackSpec` | `RuntimeToolGroupSpec` |
| `TOOL_PACKS` | `RUNTIME_TOOL_GROUPS` |
| `pack_for_name()` | `runtime_tool_group_for_name()` |
| `infer_static_pack_names()` | `infer_static_runtime_tool_group_names()` |
| `make_mcp_server_pack_name()` (defined in both `packs.py` and `mcp_registry_service.py`) | removed from product path in both modules; server identity comes from MCP server records |
| `active_packs` | `active_tool_groups` |
| `pack_activation` | `tool_group_activation` |
| `build_active_packs_section()` | `build_active_tool_groups_section()` |
| `Active Capability Packs` prompt heading | `Active Runtime Tool Groups` |

Runtime tool groups are still useful for:

- lazy tool expansion after `load_skill`
- skill-declared tool expansion
- prompt continuity after compaction
- operator diagnostics
- mapping a loaded skill to concrete tools

They are not:

- installable user modules
- marketplace products
- company-admin toggles
- MCP server identities

### 7.2 Public Agent Extension API

Add the replacement endpoint and make it the only normal frontend source of extension state:

```http
GET /agents/{agent_id}/extensions
```

```json
{
  "skills": [
    {
      "id": "skill-id",
      "name": "market-research",
      "source": "workspace",
      "status": "available"
    }
  ],
  "mcp_servers": [
    {
      "id": "server-id",
      "name": "GitHub",
      "status": "connected",
      "enabled": true,
      "tool_count": 18,
      "default_tool_mode": "auto"
    }
  ]
}
```

Response contract:

- `skills` contains agent-local and imported skills.
- `mcp_servers` contains server-level integration objects.
- It must not return `available_packs`, `channel_backed_packs`, `skill_declared_packs`, `pack_name`, or internal runtime tool groups.

### 7.3 Company MCP Servers

These routes partially exist today:

- `GET /enterprise/mcp-servers`
- `POST /enterprise/mcp-servers/import`
- `DELETE /enterprise/mcp-servers/{server_key}`

The target is to refactor this existing route family, not create it from scratch. The DELETE identity should move from pack-derived `{server_key}` to stable `{server_id}` after the MCP server migration is in place.

These routes currently live in `backend/app/api/packs.py`, the same module as the pack routes this release removes (§7.5). Extract them into a dedicated module (for example `backend/app/api/mcp_servers.py`) so the pack module can be deleted without taking the MCP routes with it. Do not delete `api/packs.py` wholesale — split first, then remove only the pack routes.

```http
GET /enterprise/mcp-servers
POST /enterprise/mcp-servers/import
DELETE /enterprise/mcp-servers/{server_id}
PUT /enterprise/mcp-servers/{server_id}
```

The endpoint response must be server-first:

```json
[
  {
    "id": "server-id",
    "name": "GitHub",
    "server_key": "github",
    "status": "connected",
    "auth_status": "configured",
    "transport": "sse",
    "tool_count": 18,
    "agent_count": 4,
    "agents": [
      { "id": "agent-id", "name": "Engineer", "enabled": true }
    ]
  }
]
```

It must not expose `pack_name` except in a backend-only historical/debug field that is never consumed by normal frontend code.

### 7.4 Agent MCP Assignment

```http
GET /agents/{agent_id}/mcp-servers
PUT /agents/{agent_id}/mcp-servers/{server_id}
GET /agents/{agent_id}/mcp-servers/{server_id}/tools
PUT /agents/{agent_id}/mcp-servers/{server_id}/tools/{tool_name}/policy
```

Default UI should call only the server-level endpoints. Tool-level endpoints are for advanced controls.

### 7.5 Removed Or Reshaped Public Pack Surfaces

These endpoints should be removed from normal product routing or reshaped so no pack fields remain:

```http
GET /packs
GET /agents/{agent_id}/packs
GET /enterprise/packs/policies
PUT /enterprise/packs/policies/{pack_name}
GET /agents/{agent_id}/capability-summary
GET /chat/sessions/{session_id}/runtime-summary
```

For the two summary endpoints, removal is not mandatory if the endpoint still serves a real runtime/governance purpose. The mandatory rule is that normal frontend consumers must not receive or render pack-shaped fields such as `available_packs`, `channel_backed_packs`, `skill_declared_packs`, or `activated_packs`.

If operators still need runtime-tool-group diagnostics, add an explicit internal route instead:

```http
GET /_internal/runtime-tool-groups
GET /_internal/agents/{agent_id}/runtime-tool-groups
```

Internal route requirements:

- platform-admin/operator only
- not linked from company admin navigation
- not used by normal agent detail pages
- no install/delete semantics
- read-only unless a separate operator config system is explicitly designed

## 8. Single-Release Implementation Scope

This is one release. Inside it, the MCP data foundation lands and passes parity first, then the product/runtime cutover follows in the same release. The release is complete only if all items below are handled.

### 8.1 Backend Runtime

Required changes:

- Rename pack modules, types, helper functions, session fields, prompt sections, recovery manifest fields, and tool expansion payloads to runtime-tool-group naming.
- Emit only `tool_group_activation` for new runtime events.
- Convert old `pack_activation` only in chat-message readers and serializers for historical messages.
- Convert old `active_packs` only in session/recovery loaders for historical persisted state.
- Remove new writes of `active_packs` and `pack_activation`.
- Update compaction reinjection to use `active_tool_groups`.
- Update tests that assert prompt cache or runtime context changes to use runtime-tool-group naming.

Historical reader shape:

```python
def normalize_runtime_event(event: dict) -> dict:
    if event.get("event_type") == "pack_activation":
        event = {**event, "event_type": "tool_group_activation"}
    if "packs" in event and "tool_groups" not in event:
        event = {**event, "tool_groups": event["packs"]}
    return event
```

The historical adapter is a reader shim, not a dual-write path.

### 8.2 Backend API

Required changes:

- Add `/agents/{agent_id}/extensions`.
- Add agent MCP server assignment endpoints.
- Refactor the existing `/enterprise/mcp-servers` endpoints to use server-first records and stable server identity.
- Remove normal product exposure of `/packs`, `/agents/{agent_id}/packs`, and `/enterprise/packs/policies`.
- Remove or reshape pack fields from `/agents/{agent_id}/capability-summary`.
- Remove or reshape pack fields from `/chat/sessions/{session_id}/runtime-summary`.
- Remove `pack_name` from MCP server DTOs consumed by normal UI.
- Keep capability policy APIs as governance APIs, not extension APIs.

### 8.3 Data Model

Introduce explicit MCP server records instead of treating grouped MCP tools as pseudo-packs.

All four tables are tenant-scoped, and `tenant_id` is mandatory on every one of them. Hive enforces cross-tenant isolation with PostgreSQL row-level security (`backend/alembic/versions/add_row_level_security.py` enables RLS and creates a `tenant_isolation_{table}` policy per table; `backend/app/database.py` sets `SET LOCAL app.current_tenant_id` per session, with `BYPASS` for platform admin). An RLS policy can only filter on a `tenant_id` column that physically exists on the row, so a child table that reaches tenant only by foreign key (for example `AgentMCPToolOverride -> mcp_server_id -> MCPServer.tenant_id`) cannot be covered and would leak across tenants. Every table below therefore stores `tenant_id` directly, even when a join could derive it.

```text
MCPServer
  id
  tenant_id            FK tenants.id, NOT NULL, ON DELETE CASCADE, indexed
  name
  server_key
  transport
  server_url
  registry_source
  status
  auth_status
  config_json
  created_at
  updated_at
  UNIQUE (tenant_id, server_key)

MCPServerTool
  id
  tenant_id            FK tenants.id, NOT NULL, ON DELETE CASCADE, indexed
  mcp_server_id        FK mcp_servers.id, ON DELETE CASCADE
  tool_id
  mcp_tool_name
  display_name
  schema_hash
  UNIQUE (tenant_id, mcp_server_id, mcp_tool_name)

AgentMCPServerAssignment
  id
  tenant_id            FK tenants.id, NOT NULL, ON DELETE CASCADE, indexed
  agent_id
  mcp_server_id        FK mcp_servers.id, ON DELETE CASCADE
  enabled
  default_tool_mode
  UNIQUE (tenant_id, agent_id, mcp_server_id)

AgentMCPToolOverride
  id
  tenant_id            FK tenants.id, NOT NULL, ON DELETE CASCADE, indexed
  agent_id
  mcp_server_id        FK mcp_servers.id, ON DELETE CASCADE
  tool_name
  mode
  UNIQUE (tenant_id, agent_id, mcp_server_id, tool_name)
```

Tenant isolation (non-negotiable, mirrors the existing `CoordinationLease` table):

- Every table stores `tenant_id` as `ForeignKey("tenants.id", ondelete="CASCADE")`, `nullable=False`, `index=True`.
- Every uniqueness rule is tenant-scoped, never global.
- The same migration adds all four tables to row-level security exactly like `add_row_level_security.py`: `ALTER TABLE {table} ENABLE ROW LEVEL SECURITY` plus a `tenant_isolation_{table}` policy of the form `current_setting('app.current_tenant_id', true) = 'BYPASS' OR tenant_id::text = current_setting('app.current_tenant_id', true)`. A new table shipped without this policy is a tenant-isolation regression and blocks the release.

Migration rules:

- Group existing `Tool(type="mcp")` rows by `(tenant_id, mcp_server_name, mcp_server_url)`.
- Create one `MCPServer` per group, carrying the group's `tenant_id`.
- Stamp `tenant_id` onto every `MCPServerTool`, `AgentMCPServerAssignment`, and `AgentMCPToolOverride` row at creation; never leave it null and never depend on a join to recover it.
- Create stable `MCPServer.id` values and `(tenant_id, server_key)`-unique `server_key` values that do not use the `mcp_server:*` pack namespace.
- Treat old `mcp_server:*` keys as legacy lookup aliases only during migration validation.
- Create `MCPServerTool` rows linking each existing MCP tool to its server.
- Convert existing `AgentTool` rows for MCP tools into `AgentMCPServerAssignment` rows.
- Preserve disabled per-tool rows as `AgentMCPToolOverride(mode="deny")` only when they differ from the server default.
- Remove `pack_name` from the tenant-visible MCP registry output.
- Enable RLS and create the `tenant_isolation_*` policy for all four tables in the same migration, then prove isolation with a two-tenant read test before cutover.
- Include a tested rollback or forward-fix path; code rollback alone is not sufficient once new MCP server rows have been written.

### 8.4 Frontend

Required changes:

- Agent detail uses `/agents/{agent_id}/extensions`, not `/agents/{agent_id}/packs`.
- Agent detail has two extension modules: Skills and MCP Servers.
- Tools page groups MCP tools under MCP server cards.
- Individual MCP tools are only shown inside an advanced drawer/dropdown.
- Company admin shows MCP servers and Skill Registry, not pack policies.
- Chat runtime displays `tool_group_activation` only as internal/runtime status; it must not render raw names like `web_pack`.
- i18n keys must avoid `pack`, `package`, and `capability pack` for user-facing extension labels.

### 8.5 Tool Availability

`get_agent_tools_for_llm()` should treat MCP server assignment as the gating layer:

```text
Tool is type=mcp
  -> find MCPServerTool
  -> find AgentMCPServerAssignment
  -> if assignment missing or disabled: exclude tool
  -> if tool override deny: exclude tool
  -> if tool override approval/default approval: expose tool but capability gate/preflight enforces approval
```

Static built-in tools still use existing runtime/governance logic.

This gating runs on the runtime hot path — once per agent invocation — and it is fail-closed: a missing `AgentMCPServerAssignment` silently removes the tool. That makes backfill completeness a release-blocking precondition. If the migration misses even one agent's existing MCP assignment, that agent loses its MCP tools on cutover day, and registry parity (§10) will not catch it, because registry parity compares server grouping, not per-agent tool reachability. The release must therefore validate tool-availability parity: for every agent, every MCP tool reachable before the migration is still reachable through the new gating path after it. Because the lookups run per invocation, batch or cache the assignment/override resolution instead of issuing per-tool queries.

### 8.6 Product Copy

Allowed labels:

- Extensions
- Skills
- MCP Servers
- Tools
- Permissions
- Governance
- Advanced tool controls
- Runtime tool groups, only in internal/operator surfaces

Disallowed user-facing labels:

- Pack
- Package
- Capability Pack
- Active Packs
- Available Packs
- Skill Declared Packs

## 9. Permission Model

Capability policies remain important, but they are not the extension catalog.

Recommended layering:

```text
Skill/MCP installed
  -> determines what the agent can discover or attempt

Capability policy
  -> determines whether an attempted action runs, needs approval, or is denied

Action preflight
  -> applies external-visible/sensitive/irreversible/company-boundary checks
```

MCP-specific capability buckets should stay coarse:

- `agent.mcp.read`
- `agent.mcp.call`
- `agent.tool.install`

Precise MCP tool behavior should be server/tool policy overrides, not hundreds of top-level capability toggles.

## 10. Execution Plan

This is one release with ordered work inside it. The data foundation runs first to reduce database risk; it is not shipped as a separate product release and does not keep two product models alive.

### First in the release: MCP data foundation

1. Backend red tests
   - Existing MCP tools can be grouped into stable server records.
   - Backfilled server records preserve current `/enterprise/mcp-servers` registry semantics except for the legacy pack-derived identity.
   - Existing `AgentTool` MCP assignments can be represented as `AgentMCPServerAssignment` plus precise overrides.
   - `capability_install_service` continues to use `mcp_server` as the install unit and never introduces a pack install kind.

2. Data model and migration
   - Add MCP server tables, each with a mandatory `tenant_id` and tenant-scoped unique constraints.
   - Enable row-level security on all four tables and create their `tenant_isolation_*` policies in the same migration.
   - Backfill existing MCP tools into server records.
   - Backfill agent/server assignments.
   - Convert exceptional per-tool state into overrides.
   - Migrate `SystemSetting` pack policy keys (`tenant:{tenant_id}:policies`, written by `set_tenant_pack_policy`): map each enabled/disabled pack to its surviving runtime-tool-group policy key, or delete keys whose pack has no surviving governance meaning. Record the mapping so the decision is auditable.
   - Keep old `Tool.mcp_server_name` and `Tool.mcp_server_url` fields as compatibility during validation.

3. Validation
   - Compare old registry grouping with new MCP server records for representative tenants.
   - Verify tool-availability parity: every agent's pre-migration set of reachable MCP tools is exactly reproduced through the new `AgentMCPServerAssignment` gating path. This is stronger than registry parity and is the real cutover-day risk.
   - Verify two-tenant RLS isolation on all four new tables: with `app.current_tenant_id` set to tenant A, none of tenant B's MCP servers, tools, assignments, or overrides are visible.
   - Verify duplicate/collision handling for normalized server names.
   - Verify direct URL imports and Smithery imports both produce one server object.
   - Verify migration rollback or forward-fix path.

This step does not change the normal product UI. It prepares the data foundation and proves parity before the cutover step runs.

### Then in the same release: product and runtime cutover

1. Backend red tests
   - New extension API does not include packs.
   - `/enterprise/mcp-servers` returns server-first records without `pack_name`.
   - `/agents/{agent_id}/capability-summary` does not return pack fields to normal frontend consumers.
   - `/chat/sessions/{session_id}/runtime-summary` does not return `activated_packs` to normal frontend consumers.
   - Runtime emits `tool_group_activation`, never `pack_activation`, for new events.
   - Historical `pack_activation` messages render as tool-group events.
   - `/packs`, `/agents/{agent_id}/packs`, and `/enterprise/packs/policies` are unavailable to normal users.

2. Backend implementation
   - Rename runtime pack code to runtime-tool-group code.
   - Add extension and MCP assignment APIs.
   - Refactor existing MCP endpoints to use stable MCP server records.
   - Remove pack APIs from normal product routing.
   - Update runtime context, prompt builder, recovery manifest, and event serializers.

3. Frontend red tests
   - Agent extension UI renders Skills and MCP Servers only.
   - Company admin renders MCP server management, not pack policy management.
   - MCP server card hides individual tools by default.
   - Advanced tool dropdown supports precision controls.
   - Chat event UI does not render raw pack names.

4. Frontend implementation
   - Replace `agentApi.getPacks()` usage.
   - Add server-first MCP UI.
   - Remove user-facing pack labels and i18n keys.
   - Update chat runtime event parsing.

5. Verification
   - Backend targeted tests.
   - Frontend targeted tests.
   - Full backend/frontend smoke where feasible.
   - Manual browser check for Agent Detail and Company Admin.

Cutover rule:

```text
Do not ship the release as a half-state where the frontend hides packs but the backend
still emits or exposes pack product APIs to normal users. The data migration must run and
pass parity before the same release flips product/API/runtime to MCP-only.
```

## 11. Test Plan

Documentation-only changes do not require TDD. Implementation phases should use TDD.

Backend tests to add later:

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/services/test_mcp_server_migration.py \
  tests/services/test_mcp_registry_service.py \
  tests/services/test_capability_install_service.py \
  tests/runtime/test_active_tool_groups_section.py \
  tests/runtime/test_prompt_builder.py \
  tests/runtime/test_recovery_manifest_persistence.py \
  tests/services/test_chat_message_parts.py \
  tests/api/test_agent_extensions_api.py \
  tests/api/test_agent_mcp_server_assignments_api.py \
  tests/api/test_mcp_servers_api.py \
  tests/api/test_pack_api_removed_from_user_surface.py
```

Frontend tests to add later:

```bash
cd frontend
npm test -- AgentDetailSections.test.tsx WorkspaceRemainingSections.test.tsx chatRuntime.test.ts
```

Acceptance criteria:

- A normal user sees only Skills and MCP Servers as extension modules.
- Company admin sees MCP servers as server-level objects, not a flat list of every MCP tool.
- Installing one MCP creates one server-level management object.
- Existing MCP tools are backfilled into stable MCP server records before product/API cutover.
- Every agent's set of reachable MCP tools is identical before and after the migration (tool-availability parity, not just registry parity).
- The four new MCP tables are tenant-scoped and covered by `tenant_isolation_*` RLS policies; a two-tenant test proves no cross-tenant read.
- Tool-level MCP control exists only as advanced precision control.
- No user-facing UI labels say `pack`, `package`, or `capability pack`.
- New runtime events never emit `pack_activation`.
- New session context never writes `active_packs`.
- Historical messages with `pack_activation` still render through the normalized `tool_group_activation` path.
- Runtime still lazily expands tools from skills and MCP without breaking existing sessions.
- `/agents/{agent_id}/capability-summary` and `/chat/sessions/{session_id}/runtime-summary` do not expose pack fields to normal frontend consumers.

## 12. Cutover Checklist

The release is incomplete unless all boxes are true:

- [ ] MCP server tables exist and existing MCP tools are backfilled.
- [ ] All four new MCP tables store `tenant_id` and are covered by `tenant_isolation_*` RLS policies (two-tenant read test passes).
- [ ] Every agent's reachable MCP tool set is identical before and after migration (tool-availability parity verified).
- [ ] `SystemSetting` pack policy keys (`tenant:{tenant_id}:policies`) are migrated to runtime-tool-group keys or deleted, with the mapping recorded.
- [ ] MCP server migration has a tested rollback or forward-fix path.
- [ ] No user-facing route calls `/packs`.
- [ ] No user-facing route calls `/agents/{agent_id}/packs`.
- [ ] No user-facing route calls `/enterprise/packs/policies`.
- [ ] `/agents/{agent_id}/capability-summary` exposes governance state without `available_packs`, `channel_backed_packs`, or `skill_declared_packs`.
- [ ] `/chat/sessions/{session_id}/runtime-summary` exposes runtime state without `activated_packs`.
- [ ] `/agents/{agent_id}/extensions` is the Agent Detail source of truth.
- [ ] `/enterprise/mcp-servers` returns server-first objects.
- [ ] `/enterprise/mcp-servers` does not expose `pack_name` to normal UI consumers.
- [ ] MCP server identity does not depend on `make_mcp_server_pack_name()`.
- [ ] MCP installation creates one visible server object.
- [ ] Individual MCP tools appear only under advanced controls.
- [ ] Runtime emits `tool_group_activation`.
- [ ] Historical `pack_activation` is read-normalized only.
- [ ] Prompt heading says `Active Runtime Tool Groups`.
- [ ] User-facing copy contains no `pack/package/capability pack` labels.
- [ ] Backend and frontend targeted tests pass.

## 13. Final Defaults

1. Internal runtime tool groups should be operator-only, not company-admin UI.
2. Tenant-level MCP import may default to all agents only when imported from company admin; agent-level import should default to the current agent only.
3. Use a dedicated MCP override table for per-tool precision; keep `CapabilityPolicy` coarse and readable.
4. Within the single release, land the MCP server data migration and pass parity validation before the product/API/runtime cutover step.
5. Do not keep an event compatibility window for new writes after this release. New writes use `tool_group_activation`; old writes are reader-normalized only.
6. Treat `SKILLS_AND_PACKS_V2.md` as an older implementation checkpoint for pack catalog work, not the final user-facing product model.

## 14. Implementation Progress

Single-release cutover, implemented in ordered parts on `main`. Each part is committed separately with tests green before the next begins. Status flips from `Design draft` to `Implemented` when Part 7 closes.

### Part 1 — MCP server data model + RLS migration ✅

Data foundation, first half: the four tenant-scoped tables that give MCP servers stable first-class identity.

- `backend/app/models/mcp_server.py` — `MCPServer`, `MCPServerTool`, `AgentMCPServerAssignment`, `AgentMCPToolOverride`. Every table: `tenant_id` NOT NULL, FK `tenants.id` ON DELETE CASCADE, indexed, plus a tenant-scoped `UniqueConstraint`.
- `backend/alembic/versions/add_mcp_server_records_0602.py` — creates the four tables, enables RLS, and creates a `tenant_isolation_{table}` policy for each in a loop (verbatim from the production-verified `add_row_level_security.py`); downgrade drops policies and tables. `down_revision` = prior head `add_agent_work_ledgers_0601`.
- Registered in `alembic/env.py` and the `app/main.py` create_all path.

Evidence: `pytest tests/services/test_mcp_server_records.py` → **7 passed** (model structure: mandatory `tenant_id`, tenant-scoped uniqueness; migration DDL contract: RLS enabled + policy per table, downgrade cleanup). Existing `test_mcp_registry_service.py` still passes. `ruff check`/`format` clean.

RLS verification honesty: Hive's suite is unit-first with no DB fixture, so isolation is proven structurally — (a) `tenant_id` exists and is NOT NULL on every table, (b) the policy DDL is created verbatim from the template already validated in production. A live two-tenant read test belongs in an integration environment with Postgres and is not run here.

### Part 2 — MCP backfill + parity (in progress)

**Part 2a — backfill functional core + parity proofs ✅**

Data foundation, second half (transform). The legacy-to-records transform is a pure functional core so both parity properties are proven without a DB.

- `backend/app/services/mcp_backfill.py` — `group_mcp_tools` (group by `(name, url)`, tenant-unique `server_key` with a numeric collision suffix, never the `mcp_server:*` namespace); `reachable_before` (persisted half of `get_agent_tools_for_llm`: `AgentTool.enabled` if a row exists, else `is_default`); `resolve_reachable_tools` (the gating contract Part 5 reuses at runtime); `plan_agent_assignment` (one assignment + `deny` overrides that reproduce pre-migration reachability exactly — when a server is enabled, every tool not reachable before is denied).
- Evidence: `pytest tests/services/test_mcp_backfill.py` → **12 passed**, including registry parity (vs `build_mcp_server_registry`) and tool-availability parity across mixed / all-enabled / all-disabled / unrelated-agent cases. ruff clean.

**Part 2b — async DB backfill shell ✅**

- `backend/app/services/mcp_backfill_service.py` — `backfill_tenant_mcp_servers(db, tenant_id)` reads legacy `Tool(type="mcp")` + `AgentTool` rows (joined via agent association, matching `list_tenant_mcp_servers`), delegates the transform to the Part 2a functional core, and writes `MCPServer` / `MCPServerTool` / `AgentMCPServerAssignment` / `AgentMCPToolOverride`. Idempotent per tenant (skips a tenant that already has server rows) — the forward-fix path.
- Evidence: `pytest tests/services/test_mcp_backfill_service.py` → **3 passed** (writes records + parity deny override for a pre-disabled tool; idempotent skip; empty tenant). Combined backfill suite **15 passed**. ruff clean.
- Trigger wiring: `backfill_tenant_mcp_servers` is invoked by the admin endpoint added in Part 4; until then it is callable but not yet routed (tracked, not orphaned).

### Part 3 — Runtime pack→runtime_tool_group rename ✅

Cutover, runtime layer. The legacy "pack" vocabulary is gone from runtime code; "runtime tool group" is the internal name. 18 app files + 11 test files.

- `app/tools/packs.py` → `app/tools/runtime_tool_groups.py`; `ToolPackSpec`→`RuntimeToolGroupSpec`, `TOOL_PACKS`→`RUNTIME_TOOL_GROUPS`, `pack_for_name`→`runtime_tool_group_for_name`, `infer_static_pack_names`→`infer_static_runtime_tool_group_names`, `iter_tool_packs`→`iter_runtime_tool_groups`, `static_pack_names_for_tool`→`static_runtime_tool_group_names_for_tool`, `build_active_packs_section`→`build_active_tool_groups_section`.
- `app/runtime/prompt_sections/active_packs.py` → `active_tool_groups.py`; `SessionContext.active_packs` → `active_tool_groups` (+ `RecoveryManifest`, `ToolExpansionResult`, `ContextBudget.active_tool_groups_budget_chars`, prompt builder). Heading `Active Capability Packs` → `Active Runtime Tool Groups`.
- New runtime events emit `tool_group_activation` (kernel/engine.py + invoker.py ×3), carrying both `tool_groups` and legacy `packs` keys, title `Runtime Tool Groups Activated`.
- Historical compatibility: `chat_message_parts.py` + `web_chat_runtime.py` still read old persisted `pack_activation` and normalize to `tool_group_activation` on read (reader shim, not dual-write). `pack_service.py` summary reader accepts both event types so new activations are not lost before Part 4.
- Kept for Part 4: `make_mcp_server_pack_name()` and `api/packs.py` routes untouched.

Evidence: `import app.main` OK; `ContextBudget` field present; ruff clean; targeted suite (prompt_builder, recovery_manifest, engine, chat_message_parts) **78 passed**; subagent's broader run **528 passed**. Grep confirms no residual `ToolPackSpec` / `TOOL_PACKS` / `active_packs` / `pack_for_name` in `app/`. (Executed via an isolated subagent; reviewed by re-running import + targeted tests + grep here — a stale mid-edit diagnostic that flagged two symbols was disproven by the final-state grep and green tests.)

### Part 4 — Backend API: server-first MCP extension API ✅ (additive)

Purely additive — new server-first API on the Part 1 tables; existing pack routes untouched (removed in Part 6 after the frontend switches). No `pack`/`pack_name` in any DTO.

- `app/services/mcp_server_service.py` — `list_tenant_servers`, `get_agent_mcp_servers`, `get_agent_extensions`, `set_agent_mcp_assignment`, `trigger_tenant_backfill`. Every query tenant-scoped.
- `app/api/mcp_servers.py` (registered as `mcp_servers_router`) — `GET /agents/{id}/extensions` (Agent Detail source of truth, `{skills, mcp_servers}`); `GET /enterprise/mcp-servers/records` (server-first, stable id); `GET /agents/{id}/mcp-servers`; `PUT /agents/{id}/mcp-servers/{server_id}` (assignment); `POST /enterprise/mcp-servers/backfill` (the Part 2 backfill trigger).
- Skills in extensions reuse `WorkspaceSkillLoader` (same source as legacy `get_agent_packs`). The `/records` route uses a distinct path to avoid colliding with the legacy `/enterprise/mcp-servers`; Part 6 reconciles them to the canonical path per §7.3.
- Evidence: `import app.main` OK; `pytest test_mcp_server_service.py test_mcp_servers_api.py` → **14 passed**; ruff clean. Reviewed: router-set comparison confirms main.py only ADDED `mcp_servers_router` (no router dropped by the format reflow); DTOs verified free of pack fields.
- Backfill is now wired (`POST /enterprise/mcp-servers/backfill`), closing the Part 2b orphan note.

### Part 5 — Tool availability gating via MCP assignment ✅

`get_agent_tools_for_llm()` gates MCP tools through the new assignment tables, reusing the Part 2a `resolve_reachable_tools` contract — with a fallback that stops un-backfilled tenants from losing tools.

- `_resolve_agent_mcp_gating(db, agent_id)` — 3 batch queries (assignments, server tools, overrides; no per-tool N+1 on the hot path) → `{tool_id: reachable}`, or `None` when the agent has no new-table data.
- Wire (surgical, 2 insertions): `if t.type == "mcp" and mcp_gating is not None and tid in mcp_gating: enabled = mcp_gating[tid]`. Non-mcp tools unchanged; un-backfilled agents (`mcp_gating is None`) keep the legacy `AgentTool.enabled`/`is_default` decision — **not fail-closed**, so manually-triggered backfill can't strip tools from un-migrated tenants. The tenant pack-policy gate stays orthogonal.
- Evidence: `import app.main` OK; `pytest test_agent_mcp_gating.py test_agent_tools.py` → **13 passed** (parity with backfilled data; fallback for un-backfilled; disabled assignment excludes; deny override excludes); broader run 25 passed. ruff clean. Reviewed the hot-path diff: fallback condition confirmed correct.

### Part 6 — Frontend + i18n: Skills + MCP Servers surface ✅ (frontend)

- New typed adapter `frontend/src/api/domains/extensions.ts` (`extensionsApi`: getAgentExtensions, getAgentMcpServers, setAgentMcpAssignment, listEnterpriseMcpServers, backfillEnterpriseMcpServers). DTOs free of pack/pack_name.
- `ToolsManager.tsx` rewritten: drives off `GET /agents/{id}/extensions`; two modules — Skills + MCP Servers. Each MCP server is one card (name, status, tool_count, enabled toggle → PUT assignment); individual tools live in a collapsed "Advanced tool controls" drawer per card, hidden by default.
- Company admin `WorkspaceToolsSection.tsx`: server-first MCP Servers view from `/enterprise/mcp-servers/records`; no pack-policy product UI. Skill Registry stays in its own tab.
- Chat runtime: recognizes `tool_group_activation` (+ historical `pack_activation` read-normalized); shows a generic count, never raw internal names like `web_pack`.
- i18n: `agent.packs.*` → `agent.extensions.*` in both en.json + zh.json (lockstep); user-facing pack/package/capability-pack labels removed. Internal category keys office_pack/deep_research_pack kept (they key raw backend `tool.category` and display clean "Office"/"Deep Research").
- Removed the orphaned `getPacks` adapter (no consumers).
- Evidence: `npx tsc --noEmit` exit 0; `npm run build` exit 0; targeted tests (chatRuntime, AgentDetailSections, WorkspaceRemainingSections, AgentDetail, AgentDetail.query-gating) **47 passed**. Reviewed: tsc re-run confirmed green; removed one unused React default import.

Note: backend legacy pack routes (`/packs`, `/agents/{id}/packs`, `/enterprise/packs/policies`) are now orphaned (no frontend consumer) but still registered; their physical removal + reconciling `/enterprise/mcp-servers/records` to the canonical path is finished in Part 7.

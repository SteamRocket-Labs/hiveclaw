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
- `backend/app/api/packs.py` exposes `/packs`, `/agents/{agent_id}/packs`, `/enterprise/packs/policies`, `/agents/{agent_id}/capability-summary`, and `/chat/sessions/{session_id}/runtime-summary`.
- `backend/app/services/pack_service.py` returns fields such as `available_packs`, `channel_backed_packs`, `skill_declared_packs`, and `activated_packs`.
- `backend/app/services/chat_message_parts.py` emits and serializes `pack_activation` events with the title "Capability Packs Activated".
- `backend/app/services/mcp_registry_service.py` derives MCP `server_key` and `pack_name` from `make_mcp_server_pack_name()`, so MCP servers do not yet have stable first-class identity.
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
  agent_id: uuid
  mcp_server_id: uuid
  enabled: bool
  default_tool_mode: "auto" | "approval" | "deny"
}
```

Tool-level overrides are optional:

```text
AgentMCPToolOverride {
  agent_id: uuid
  mcp_server_id: uuid
  tool_name: string
  mode: "auto" | "approval" | "deny"
}
```

This makes "install one MCP, manage one MCP" the default, while preserving precise control.

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
- Old tenant settings may contain pack policy keys; a migration must either move them to internal runtime-tool-group policy keys or delete them when no longer product-relevant.
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
| `make_mcp_server_pack_name()` | removed from product path; server identity comes from MCP server records |
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

```text
MCPServer
  id
  tenant_id
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

MCPServerTool
  id
  mcp_server_id
  tool_id
  mcp_tool_name
  display_name
  schema_hash

AgentMCPServerAssignment
  id
  agent_id
  mcp_server_id
  enabled
  default_tool_mode

AgentMCPToolOverride
  id
  agent_id
  mcp_server_id
  tool_name
  mode
```

Migration rules:

- Group existing `Tool(type="mcp")` rows by `(tenant_id, mcp_server_name, mcp_server_url)`.
- Create one `MCPServer` per group.
- Create stable `MCPServer.id` values and tenant-unique `server_key` values that do not use the `mcp_server:*` pack namespace.
- Treat old `mcp_server:*` keys as legacy lookup aliases only during migration validation.
- Create `MCPServerTool` rows linking each existing MCP tool to its server.
- Convert existing `AgentTool` rows for MCP tools into `AgentMCPServerAssignment` rows.
- Preserve disabled per-tool rows as `AgentMCPToolOverride(mode="deny")` only when they differ from the server default.
- Remove `pack_name` from the tenant-visible MCP registry output.
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
   - Add MCP server tables.
   - Backfill existing MCP tools into server records.
   - Backfill agent/server assignments.
   - Convert exceptional per-tool state into overrides.
   - Keep old `Tool.mcp_server_name` and `Tool.mcp_server_url` fields as compatibility during validation.

3. Validation
   - Compare old registry grouping with new MCP server records for representative tenants.
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

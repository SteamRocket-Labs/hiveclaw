# Agent Extension Surface: Skill + MCP

| Field | Value |
|------|-------|
| Status | Design draft |
| Date | 2026-06-02 |
| Scope | Agent/user extension surface, company admin extension surface, MCP server management, one-shot pack/package removal from user surface |
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

The current codebase has a useful runtime concept called packs, but the same concept leaks into several product surfaces:

- `backend/app/tools/packs.py` defines static tool groups such as `web_pack`, `feishu_pack`, `mcp_admin_pack`, `office_pack`, `deep_research_pack`, and `plan_mode_pack`.
- `backend/app/api/packs.py` exposes `/packs`, `/agents/{agent_id}/packs`, and `/enterprise/packs/policies`.
- `backend/app/services/mcp_registry_service.py` still returns `pack_name` for MCP servers.
- `frontend/src/pages/agent-detail/ToolsManager.tsx` fetches `/agents/{agent_id}/packs` to organize visible tools.

This creates the wrong product mental model. Users should not decide whether an agent has `web_pack` or `office_pack`. They should decide:

- Which skills does this agent know how to use?
- Which MCP servers is this agent connected to?
- Which permissions does the company allow, deny, or require approval for?

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

## 6. One-Shot Cutover Rule

This migration should not be staged as a long-running compatibility project. A gradual "hide first, rename later, clean up later" plan will likely become permanent debt because pack state already touches runtime, prompt context, chat events, API responses, and frontend grouping.

The correct plan is a **single cutover release**:

```text
One release changes the product surface, backend APIs, runtime naming, frontend UI,
tests, and data normalization together.
```

Allowed compatibility is limited to **historical-read adapters**:

- Old persisted chat messages may contain `pack_activation`; readers may map them to `tool_group_activation`.
- Old persisted session context may contain `active_packs`; loaders may map it to `active_tool_groups`.
- Old tenant settings may contain pack policy keys; a migration must either move them to internal runtime-tool-group policy keys or delete them when no longer product-relevant.

Disallowed compatibility:

- No new runtime event may emit `pack_activation`.
- No new prompt section may say `Active Capability Packs`.
- No normal frontend route may call `/packs` or `/agents/{agent_id}/packs`.
- No company admin page may expose pack/package/capability-pack management.
- No MCP server response shown to the UI may present `pack_name` as product state.
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

### 7.5 Removed Public Pack APIs

These endpoints should be removed from normal product routing in the same cutover:

```http
GET /packs
GET /agents/{agent_id}/packs
GET /enterprise/packs/policies
PUT /enterprise/packs/policies/{pack_name}
```

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

## 8. One-Shot Implementation Scope

The cutover is complete only if all items below land together.

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
- Change `/enterprise/mcp-servers` response to server-first shape.
- Remove normal product exposure of `/packs`, `/agents/{agent_id}/packs`, and `/enterprise/packs/policies`.
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
- Create `MCPServerTool` rows linking each existing MCP tool to its server.
- Convert existing `AgentTool` rows for MCP tools into `AgentMCPServerAssignment` rows.
- Preserve disabled per-tool rows as `AgentMCPToolOverride(mode="deny")` only when they differ from the server default.
- Remove `pack_name` from the tenant-visible MCP registry output.

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

## 10. Single-Release Execution Plan

This is not a staged rollout. It is one implementation package with ordered work inside the PR/release.

1. Backend red tests
   - New extension API does not include packs.
   - MCP registry groups tools by server.
   - Runtime emits `tool_group_activation`, never `pack_activation`.
   - Historical `pack_activation` messages render as tool-group events.
   - `/packs` is not available to normal users.

2. Data migration
   - Add MCP server tables.
   - Backfill existing MCP tools into server records.
   - Backfill agent/server assignments.
   - Convert exceptional per-tool state into overrides.

3. Backend implementation
   - Rename runtime pack code to runtime-tool-group code.
   - Add extension and MCP assignment APIs.
   - Remove pack APIs from normal product routing.
   - Update runtime context, prompt builder, recovery manifest, and event serializers.

4. Frontend red tests
   - Agent extension UI renders Skills and MCP Servers only.
   - Company admin renders MCP server management, not pack policy management.
   - MCP server card hides individual tools by default.
   - Advanced tool dropdown supports precision controls.
   - Chat event UI does not render raw pack names.

5. Frontend implementation
   - Replace `agentApi.getPacks()` usage.
   - Add server-first MCP UI.
   - Remove user-facing pack labels and i18n keys.
   - Update chat runtime event parsing.

6. Verification
   - Backend targeted tests.
   - Frontend targeted tests.
   - Full backend/frontend smoke where feasible.
   - Manual browser check for Agent Detail and Company Admin.

Rollback rule:

```text
If the cutover fails, roll back the release. Do not ship a half-state where
frontend hides packs but backend still emits or exposes pack product APIs.
```

## 11. Test Plan

Documentation-only changes do not require TDD. Implementation phases should use TDD.

Backend tests to add later:

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/services/test_mcp_registry_service.py \
  tests/services/test_capability_install_service.py \
  tests/runtime/test_active_tool_groups_section.py \
  tests/runtime/test_prompt_builder.py \
  tests/runtime/test_recovery_manifest_persistence.py \
  tests/services/test_chat_message_parts.py \
  tests/api/test_agent_extensions_api.py \
  tests/api/test_agent_mcp_server_assignments_api.py \
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
- Tool-level MCP control exists only as advanced precision control.
- No user-facing UI labels say `pack`, `package`, or `capability pack`.
- New runtime events never emit `pack_activation`.
- New session context never writes `active_packs`.
- Historical messages with `pack_activation` still render through the normalized `tool_group_activation` path.
- Runtime still lazily expands tools from skills and MCP without breaking existing sessions.

## 12. Cutover Checklist

The release is incomplete unless all boxes are true:

- [ ] No user-facing route calls `/packs`.
- [ ] No user-facing route calls `/agents/{agent_id}/packs`.
- [ ] No user-facing route calls `/enterprise/packs/policies`.
- [ ] `/agents/{agent_id}/extensions` is the Agent Detail source of truth.
- [ ] `/enterprise/mcp-servers` returns server-first objects.
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
4. Do not keep an event compatibility window for new writes. New writes use `tool_group_activation`; old writes are reader-normalized only.
5. Treat `SKILLS_AND_PACKS_V2.md` as an older implementation checkpoint for pack catalog work, not the final user-facing product model.

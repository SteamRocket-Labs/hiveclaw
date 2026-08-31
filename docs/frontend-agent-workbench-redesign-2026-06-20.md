# Frontend Agent Workbench Redesign

状态：Part 2 已有代码级闭环；2026-06-23 按 CC Design 截图进入 IA reset / design-contract 更新，本轮尚未开始实现
日期：2026-06-20；Part 2 更新：2026-06-23；闭环更新：2026-06-23；IA reset：2026-06-23
输入原型：`claude-design-for-hiveclaw/`
目标前端：`frontend/src/`
参考细节：CC Design prototype、Codex Desktop chat/workbench UI、Hive 当前真实 runtime/API

## 0. Execution Gate

This document is **Phase 2** of the frontend redesign.

Phase 1 is `docs/frontend-session-workbench-cc-codex-parity-gap-2026-06-23.md`: session 内体验先成为 Codex Desktop / CC-grade thread。Part 2 只在这个基础上扩展到整个前端工作台，不回头把会话重新做成普通 tab 或拼接式页面。

Blocking rule:

1. Session Workbench contract remains locked: `ThreadTimeline`, `ActiveRunCell`, Codex-like composer, slash menu, right inspector, session-native Goal/Plan/Team/Checkpoint controls.
2. Part 2 may move where the session workbench sits in the app, but must not degrade the session timeline, composer, runtime disclosure, or artifact inspector.
3. Part 2 must preserve every real front/backend capability that users need to know. Redesign is IA and productization work, not a visual-only shell.
4. Any visible UI affordance must map to a real route/API/runtime event, or be explicitly shown as unavailable/needs setup. No prototype mock state may enter product code.

## 1. Bottom Line

Hive 前端要从“管理 tab 集合”升级为 **agent workbench + company control plane**。

The target is **CC Design as product structure, Codex Desktop as interaction quality, Hive runtime as truth**:

1. **CC Design prototype is the structural baseline.** It defines the employee workspace, control plane split, Notion-tree IA, digital employee lifecycle, task flow, and governance surfaces.
2. **Codex Desktop is the interaction-quality overlay.** It defines composer density, message rhythm, command menu, low-noise status, artifact inspection, and side panel behavior.

Part 2 is not a second chat polish pass. It is the broader frontend transformation after session-internal parity:

1. Global shell and navigation become workspace/control-plane aware.
2. User-facing feature surfaces become discoverable, not hidden under engineering tabs.
3. Agent Workbench becomes a daily operating surface, not a diagnostics page.
4. Control Plane becomes a real company operations console, not a long settings tab list.
5. The UI stays calm, dense, durable, and long-session friendly.

### 1.1 Current Code Closure Snapshot

As of the 2026-06-23 final closure pass, the implementation has moved beyond route-only shell work:

1. `/agents/new` is HR Agent-only. It calls the HR Agent entrypoint and exposes no blank/template/direct-create fork.
2. `PlanCard` no longer uses browser-native `window.prompt`; revise/reject are inline, session-native composers inside the plan card.
3. `WorkspaceFeatureHub` is no longer only a link hub. It reads real adapters for cross-agent plans, approvals, memory overview, workspace files, workflow definitions, and workflow run evidence, then routes users back to the owning employee/control-plane surface for action.
4. `WorkspaceFeatureHub kind="team"` now gives A2A / Team a real front-end surface over session-local team, employee relationships, agent subagents, company subagent library, and user-scoped Local Agent Channel.
5. `/enterprise/dashboard` and every `/enterprise/*` section now render through `ControlPlane`, a new company operations console. The old workspace sections are embedded as implemented capability bodies, not exposed as a long legacy settings tab strip.
6. Global quick-open/search is route-backed and can include routes/control-plane pages/local agents plus loaded employee filtering, but it is not a permanent workspace-search row in the sidebar.
7. `DigitalEmployees` now exposes ownership, shared, recommended, running, attention, coordinator, and local-runtime filters; cards deep-link to chat, memory, workflow, team/relationships, detail, and Local Agent where applicable.
8. `AppDialogs` provides global in-app toast and confirmation. Frontend product code no longer uses browser-native `alert()`, `confirm()`, or `prompt()`; the only remaining `confirm` string is the `planApi.confirm` API method test.
9. `AgentDetail` session-path interruptions have been reduced: session delete uses `ConfirmModal`, and create/branch/upload failures use non-blocking in-app toast instead of `alert()`.
10. Vite build now uses manual vendor chunks for React, React Query, i18n, charts, icons, and shared vendor code to reduce main-entry pressure.

Code-level verification completed:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm test -- --run
npm run build
git diff --check
rg -n "alert\(|confirm\(|prompt\(" frontend/src --glob '*.tsx' --glob '*.ts'
```

Still not claimed as live-closed until a real login/backend/WebSocket pass is run:

1. Long running session with tool calls and slash command interaction.
2. HR Agent creation flow in a real tenant.
3. Mobile/tablet visual verification.
4. Production/eval deployment verification.

### 1.2 2026-06-23 CC Design IA Reset

This section supersedes the earlier broad-sidebar interpretation for Part 2.

The previous implementation pass made most capabilities reachable, but the user-facing IA is still too much like a feature inventory. The CC Design screenshots and `claude-design-for-hiveclaw/` sources show a stricter product structure:

1. The app shell is not a long navigation catalog. It is a compact workspace rail plus an object-focused main area.
2. Home is one workspace homepage, not a mixed admin dashboard.
3. Agent Circle, Automation/Tasks, Bridge, session conversations, and Digital Employees must fit into one coherent Codex-like left rail.
4. Agent Detail should look like the CC Design employee workspace, while using Hive's real modules and data.
5. Session is the daily working object. Management surfaces exist, but they should not dominate the first read.

#### 1.2.1 Source Facts Read This Round

CC Design source files reviewed:

1. `claude-design-for-hiveclaw/design-tokens.css`
   - Warm paper background `--bg`, white `--surface`, sunken `--bg-sunk`, honey as the single brand accent, small radii, low shadows, mono eyebrow labels.
2. `claude-design-for-hiveclaw/ui.jsx`
   - Shared compact primitives: hex avatar, chip, button, card, tabs, page head, empty state, icon-first controls, and mock agent capability fields.
3. `claude-design-for-hiveclaw/app-shell.jsx`
   - Workspace switcher, global search, notification popover, user footer, left tree navigation, employee/control-plane split.
4. `claude-design-for-hiveclaw/emp-core.jsx`
   - Home and Digital Employees list: greeting, quick actions, needs-you, in-progress, this-month usage, activity, employee cards.
5. `claude-design-for-hiveclaw/emp-workspace.jsx`
   - Agent/employee detail: hero card, tabs, work record, active task, monthly overview, about, capabilities, source files.
6. `claude-design-for-hiveclaw/chat-task.jsx`
   - Conversation/task flow: task composer, plan confirmation, progress, A2A handoff, artifacts, right rail.
7. `claude-design-for-hiveclaw/emp-more.jsx`
   - Automations, memory/knowledge, documents/research, approvals.
8. `claude-design-for-hiveclaw/admin.jsx`
   - Control Plane reference only. It should not leak into the normal workspace homepage.
9. `claude-design-for-hiveclaw/create-flow.jsx`
   - Creation form reference only. Product decision remains HR Agent-only creation.
10. `claude-design-for-hiveclaw/auth-flow.jsx`
   - Auth and workspace picker design language already used by the login/setup pass.
11. `claude-design-for-hiveclaw/shells.js`
   - IA exploration reference, not implementation source.

Current Hive code facts:

1. `frontend/src/pages/layout/AppSidebar.tsx` currently exposes many top-level workspace routes: Digital Employees, Conversations & Tasks, Plan Review, Automations, Memory & Knowledge, Documents & Research, A2A / Team, Local Agent Channel, Agent Circle. This is discoverable but too wide for the CC/Codex target.
2. `frontend/src/pages/Dashboard.tsx` is still closer to a management dashboard than the CC Design workspace home.
3. `frontend/src/pages/DigitalEmployees.tsx` already has ownership/shared/local filters, but it is a separate directory page instead of being integrated into the left project/employee rail.
4. `frontend/src/pages/AgentDetail.tsx` already groups detail modules into workbench areas, but the first impression is still a management page with tabs, not the CC Design employee detail.
5. `frontend/src/pages/agent-detail/AgentChatSection.tsx` already has `sessions`, `allSessions`, `chatScope`, source-channel labels, branch lineage, read-only mode, and session workbench header.
6. `backend/app/models/chat_session.py` and `backend/app/api/chat_sessions.py` already support durable session metadata: `source_channel`, `session_kind`, `runtime_source`, `visibility_scope`, `listed_surface`, `runtime_task_id`, `peer_agent_id`, `parent_session_id`, and `root_session_id`.
7. `frontend/src/api/domains/chat.ts` currently under-declares `ChatSession`; it omits fields already returned by `SessionOut` such as `user_id`, `username`, `source_channel`, `session_kind`, `runtime_source`, `visibility_scope`, `listed_surface`, `message_count`, `peer_agent_id`, `peer_agent_name`, and `participant_type`.

#### 1.2.2 New Target Shell

The left rail should become the annotated Codex-style object tree:

```text
Workspace
  我的工作区 / current workspace

Top Actions
  Home
  Agent Circle
  Automation / Tasks
  Bridge

Digital Employees
  ▸ owned employee A
      private/recent session 1 [任务]
      private/recent session 2 [IM]
  ▸ owned employee B
  ▸ shared employee C [公共]
      current user's session with C [A2A]
  ▸ public employee D [公共]
  ▸ local employee E [本地]
      local session 1 [本地]
  + 新建数字员工
```

Rules:

1. The workspace block is required at the top of the sidebar. It shows the current workspace and owns workspace switching/account context; it is not a search row.
2. There are no `My Employees` / `Company Employees` group labels in the primary sidebar.
3. A digital employee row is the Codex Project equivalent. The agent itself represents the project/work object.
4. Ownership/source is shown inline by the small icon before the agent and the badge after the agent name.
5. If an agent has no ownership badge, it is treated as the current user's own agent. A `我的` badge may be used only where extra clarity is needed.
6. Public/non-owned agents use a `公共` badge. Local/Bridge agents use a `本地` badge.
7. The agent row's disclosure/dropdown expands that agent's sessions. Sessions carry source badges such as `任务`, `IM`, `A2A`, `本地`, `工作流`, and `分叉`.
8. Clicking the agent row enters `/agents/:id`; clicking the disclosure opens/closes its session list.
9. `+ 新建数字员工` is a persistent action at the bottom of the `Digital Employees` list and routes through the HR Agent creation path. A Home quick action may duplicate it, but cannot replace this sidebar entry.
10. The old Codex "Pinned" area is not part of the Hive IA. Do not preserve a primary pinned section in the new shell.
11. The sidebar should not show every feature module as a top-level item. Feature modules move into Home cards, Agent Detail tabs, Control Plane, or command/search surfaces.
12. Existing routes can remain for compatibility, but the primary visible shell must follow this compact IA.

This intentionally borrows Codex Desktop's object hierarchy:

```text
Codex:
  top commands
  local projects
  project sessions

Hive:
  workspace identity / switcher
  top actions
  agents as projects
  agent dropdowns / sessions with source badges
```

##### 1.2.2.1 Corrected Homepage Shell Wireframe

This wireframe reflects the annotated screenshot from 2026-06-23. It is the current homepage shell target before implementing the broader Agent Detail redesign.

```text
┌──────────────────────────┬──────────────────────────────────────────────────────────────┐
│ ┌ 工作区 ───────────────┐ │ Home / 我的工作区                                             │
│ │ example-owner的实验室         │ │                                                              │
│ └──────────────────────┘ │  ┌──────────────────────────────────────────────────────────┐ │
│                          │  │ greeting / needs-you / active-work summary                │ │
│ Home                     │  └──────────────────────────────────────────────────────────┘ │
│ Agent Circle             │                                                              │
│ Tasks / Automation       │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────┐ │
│ Bridge                   │  │ Create via HR │ │ Assign task  │ │ Automation   │ │Assets│ │
│                          │  └──────────────┘ └──────────────┘ └──────────────┘ └──────┘ │
│                          │                                                              │
│                          │  Needs you                                      This month   │
│                          │  ┌──────────────────────────────────┐       ┌─────────────┐ │
│ Digital Employees        │  │ Ledger plan pending      [任务]    │       │ Real usage   │ │
│   ▾ Atlas                │  │ Requires confirmation              │       │ or empty     │ │
│       Q2 report [任务]   │  └──────────────────────────────────┘       └─────────────┘ │
│       Feishu QA [IM]     │  In progress                                  Activity      │
│   ▸ Ledger               │  ┌──────────────────────────────────┐       ┌─────────────┐ │
│                          │  │ Atlas Q2 research       [任务]    │       │ Real logs    │ │
│   ▸ Relay        [公共]  │  │ Relay delegation        [A2A]     │       │ artifacts    │ │
│   ▸ Warden       [公共]  │  └──────────────────────────────────┘       └─────────────┘ │
│                          │  首页不保留右侧空白 inspector，主内容吃满可用宽度             │
│   ▾ Local Bot    [本地]  │                                                              │
│       Local run [本地]   │                                                              │
│   + 新建数字员工         │                                                              │
│                          │                                                              │
│ User / Settings          │                                                              │
└──────────────────────────┴──────────────────────────────────────────────────────────────┘
```

Homepage shell rules from the annotated screenshot:

1. The workspace block stays above navigation and displays the active workspace, for example `我的工作区 / example-owner的实验室`.
2. The top action block maps to Hive actions: Home, Agent Circle, Tasks/Automation, Bridge.
3. The Codex pinned section is ignored for Hive. Do not allocate a primary pinned band.
4. The Codex Projects section becomes Digital Employees, and each Agent is the project-equivalent row.
5. Do not add group labels such as `My Employees` or `Company/Public` in the primary sidebar.
6. Agent rows use a small leading icon plus an optional trailing badge to distinguish own, public/shared, and local/bridge. No badge means the current user's own agent by default.
7. The agent disclosure/dropdown expands sessions under that agent. Session rows carry source badges such as `任务`, `IM`, `A2A`, `本地`, and `分叉`.
8. `+ 新建数字员工` belongs at the bottom of the `Digital Employees` tree because it creates the same object represented by that tree. It must use the governed HR Agent creation flow.
9. Home should not render a permanent right-side environment/inspector panel. That panel is for session/workbench contexts, not the homepage.
10. The homepage content should expand into the available width and not leave the right side visually empty.
11. Do not add a separate "workspace search" row to this shell. If global search is needed later, it belongs to command/quick-open behavior, not this primary left-rail structure.

#### 1.2.3 Home Page Contract

Home is the single workspace homepage. It should follow the first screenshot and `emp-core.jsx`.

Required blocks:

1. Workspace identity and greeting.
2. Quick actions:
   - Create digital employee, routed only to the HR Agent creation path.
   - Assign new task, opening an agent/session picker or task composer.
   - Save successful task as workflow, only when real candidate evidence exists.
   - Browse company assets, routed to Control Plane/assets or read-only asset browser depending permission.
3. Needs you:
   - Pending plan confirmations.
   - AskUserQuestion cards.
   - Approval requests.
   - Failed runs requiring intervention.
4. In progress:
   - Active `RuntimeTask` / web-chat / workflow / channel sessions.
   - A2A/team handoffs with clear source tags.
5. This month:
   - Usage/budget only if backed by real API.
   - Otherwise show a setup/empty state, not fake counts.
6. Recent activity:
   - Real activity logs and recent artifacts.

Non-goals:

1. Do not show full Control Plane controls on Home.
2. Do not show a long feature grid.
3. Do not invent usage/activity data.
4. Do not leave a blank right rail / environment inspector on Home.

#### 1.2.4 Agent Circle Contract

Agent Circle is the company/public employee discovery surface. It replaces the old feel of a social feed as the primary IA.

Required behavior:

1. Show public/company-recommended employees and optionally internal posts/updates.
2. Use the same CC Design card language as Digital Employees.
3. Mark whether an employee is owned by me, public, company-standard, or shared by another owner.
4. Clicking an employee goes to its detail page.
5. Creation still routes through HR Agent; no alternate direct create path.

#### 1.2.5 Automation / Tasks Contract

Automation / Tasks is a workspace-level task/session entry, not a low-level workflow admin page.

Required behavior:

1. Show scheduled tasks, trigger runs, workflow runs, and long-running runtime tasks as session-like rows.
2. Every row must bind to or open a `ChatSession` replay surface when available.
3. Sessions created by scheduled/triggered work must carry a `任务` label in the session UI.
4. Successful tasks that can become workflows should surface as "save as workflow" candidates only with real evidence.
5. Admin-only workflow definition controls stay behind Control Plane or manage permissions.

#### 1.2.6 Session Source Labels

Session labels are not styling guesses. They are a frontend projection of durable session metadata.

Canonical label mapping:

| Backend fields | UI label | Meaning |
| --- | --- | --- |
| `source_channel = web`, `runtime_source = web_chat` | no label or `对话` | Normal user web chat |
| `source_channel` in `feishu`, `telegram`, `slack`, `discord`, `dingtalk`, `wecom`, `microsoft_teams`, `wechat_personal` | `IM` plus channel name | IM/channel-originated conversation |
| `source_channel = trigger` or `runtime_source = trigger` or `session_kind = trigger_run` | `任务` | Scheduled/triggered task session |
| `source_channel = agent` or `participant_type = agent` | `A2A` | Agent-to-agent conversation |
| `runtime_source = workflow` or workflow-linked `runtime_task_id` metadata | `工作流` | Workflow run session |
| local/openclaw metadata | `本地` | Local Agent / bridge session |
| `parent_session_id` or branch metadata | `分叉` | Fork/edit/rewind branch |

Implementation implication:

1. Update frontend `ChatSession` TypeScript interface to match backend `SessionOut`.
2. Add a single `getSessionSourceBadges(session)` helper and tests.
3. Use the same badge helper in sidebar session lists, Agent Detail session dropdown, owner all-user view, and workspace Automation / Tasks.

#### 1.2.7 Agent Detail Contract

Agent Detail should follow screenshots 3 and 4 and `emp-workspace.jsx`, but populated by real Hive modules.

Page structure:

1. Left rail remains global and compact.
2. Main content starts with a profile hero:
   - hex/avatar, name, status chip, short id, role/scope/owner metadata.
   - primary action: Open Conversation.
   - secondary actions: Edit and more menu, gated by permission.
3. Below hero, use CC Design-style top tabs:
   - Overview
   - Memory & Knowledge
   - Workspace
   - Skills
   - Connectors
   - Workflows
   - Expert Roles
   - A2A
   - IM
   - Permissions
   - Settings
4. Overview content maps current modules into product sections:
   - Work record from runtime/activity/session data.
   - Active task from active run/workflow/task.
   - This month from usage/budget if available.
   - About from soul/profile/role configuration.
   - Capabilities from tools, skills, workflows, MCP/connectors, A2A.
   - Source files from governed identity/memory/source documents.

Current module mapping:

| Target tab/section | Existing source |
| --- | --- |
| Overview | `AgentStatusSection`, `AgentActivityLogSection`, active sessions/runs |
| Memory & Knowledge | `AgentKnowledgeSection`, `AgentMindSection`, memory read models |
| Workspace | `AgentWorkspaceSection`, `OfficeWorkbenchSection`, files/artifacts |
| Skills | `AgentSkillsSection`, skill lifecycle summaries |
| Connectors | `ToolsManager`, MCP/tool/channel capability data |
| Workflows | `AgentWorkflowsSection` |
| Expert Roles | `AgentSubagentsSection` |
| A2A | `RelationshipEditor`, delegation/team traces |
| IM | channel sessions filtered by IM `source_channel` |
| Permissions | approvals, visibility, access level, expiry |
| Settings | advanced settings only |

#### 1.2.8 Agent Detail Session Model

The Agent Detail page needs a Codex-like session selector, but it must respect privacy.

Required behavior:

1. The default session dropdown/list shows only sessions related to the current user.
2. Session rows show source labels: `任务`, `IM`, `A2A`, `工作流`, `本地`, `分叉`.
3. Opening a session enters the Session Workbench, preserving Phase 1 timeline/composer/inspector behavior.
4. Owner/admin all-user viewing is not mixed into the private left rail.
5. Public-agent owner review appears as a separate window/panel/tab, e.g. `Public Conversations` or `All User Conversations`.
6. The owner review panel uses read-only session rendering by default.
7. The owner review panel must show participant/user/channel labels and should be auditable.

Current code partially supports this:

1. `chatApi.listSessions(agentId, 'mine' | 'all')` exists.
2. Backend `scope=all` already requires admin/creator/manage access.
3. `AgentChatSection` already has `chatScope`, `allSessions`, all-user filter, and read-only rendering.

Required cleanup:

1. Rename the UI from a raw scope toggle to a product concept: private sessions vs owner review.
2. Do not place all-user conversations in the main sidebar.
3. Ensure public-agent owner access is policy-backed, not just creator/admin convenience.
4. Add visible audit/permission copy only where it clarifies, not as noisy internal text.

#### 1.2.9 Open Decisions Before Implementation

Recommended decisions:

1. Normal users land on Home after login.
2. Clicking an employee opens Agent Detail Overview by default; Open Conversation enters the session workbench.
3. The Agent Detail header session dropdown lists current-user sessions only.
4. Owner review is a separate read-only panel/tab and must not pollute private session navigation.
5. Home left rail should include the required workspace block, then Home, Agent Circle, Automation/Tasks, Bridge, and Digital Employees as primary concepts.
6. The visible `+ 新建数字员工` action lives under Digital Employees and enters `/agents/new` through the HR Agent creation flow.
7. Control Plane remains accessible for org/admin users but no longer occupies normal workspace IA.
8. `/messages`, `/plans`, `/memory`, `/documents`, `/team`, and broad workspace hubs may remain as compatibility routes, but should not be primary left-rail items after the IA reset.

Potential backend/API follow-up:

1. Confirm every scheduled/trigger/workflow/channel runtime creates or binds a `ChatSession`.
2. Confirm `runtime_source` and `session_kind` are consistently set by workflow runs, scheduled tasks, trigger runs, IM channels, A2A, and local agent sessions.
3. Add tests for session source badge derivation and owner/public visibility.
4. Extend frontend `ChatSession` type to match backend `SessionOut`.

## 2. TasteSkill Calibration

Relevant TasteSkill resources were applied with scope control:

1. `design-taste-frontend`: use anti-slop, density, layout, motion, and verification rules; do not apply landing-page/hero/Awwwards patterns to this enterprise workbench.
2. `redesign-existing-projects`: preserve existing stack, routes, API contracts, and behavior; redesign by diagnosis and targeted migration, not framework rewrite.
3. `gpt-taste`: use only as a warning against generic AI-looking visuals. Do not import its GSAP-heavy, campaign-site style into dense product UI.

Design dials for Hive Part 2:

| Dial | Setting | Meaning |
| --- | --- | --- |
| Visual density | 8/10 | More like Codex/CC operational UI than a marketing dashboard. |
| Motion intensity | 2/10 | Transform/opacity only; no ornamental motion. |
| Brand expression | 4/10 | Hive identity visible through tokens, avatars, structure; not decorative honeycomb everywhere. |
| Governance clarity | 9/10 | Permissions, approval, model/budget, memory sensitivity, and audit must be visible where decisions happen. |
| Design variance | 4/10 | Use prototype IA and existing app stack; avoid radical style detours. |

Taste rules:

1. No landing-page hero in authenticated app surfaces.
2. No fake screenshots, fake metrics, fake Git/environment state, or mock agent arrays.
3. No decorative gradient/orb/bokeh treatment.
4. No card-inside-card information pileups.
5. Use Tabler Icons or existing icon stack, not copied inline SVG icon systems.
6. Warm neutral/honey prototype tokens must be adapted so the product does not become a one-note beige/honey app. Keep honey as accent; balance with neutral white/gray surfaces and semantic status colors.
7. Motion must be functional: rail open/close, slash menu, disclosure, upload progress, task status transitions.
8. Every dense panel must have stable dimensions and responsive collapse behavior.

## 3. Prototype Inventory

| Prototype file | Product intent to keep | What must not be copied |
| --- | --- | --- |
| `design-tokens.css` | Warm neutral base, honey accent, small radii, low-noise shadows, mono labels | Direct beige domination, remote Google font dependency without product decision, tokens that break accessibility |
| `ui.jsx` | Button/chip/tab/page-head/empty-state primitives and compact icon-first controls | `window.HiveUI`, inline SVG icon registry, mock data coupling |
| `app-shell.jsx` | Workspace switcher, employee/control-plane split, Notion-tree navigation, global search, notifications, user footer | Local-only fake navigation and global mock state |
| `auth-flow.jsx` | Login/onboarding visual direction, workspace picker, workspace creation flow | Replacing real auth, SSO, tenant setup, guards, or backend onboarding |
| `emp-core.jsx` | Home, digital employee list, quick actions, running/needs-attention aggregation | Hardcoded dates, fake usage counts, mock agent arrays |
| `emp-workspace.jsx` | Agent Workbench areas: overview, capabilities, memory, A2A, permissions, settings | Fake capability toggles and non-real permission changes |
| `chat-task.jsx` | Task lifecycle: delegate -> plan -> execute -> A2A -> artifacts | Demo timers, fake plan/artifact data, standalone local state |
| `emp-more.jsx` | Automations, Memory & Knowledge, Documents & Research, Approvals | Static fake records and unbacked actions |
| `create-flow.jsx` | Explicit digital employee creation wizard and save-as-workflow modal | Simulated creation timeout or modal-only asset persistence |
| `admin.jsx` | Control Plane overview, members/org, governance, capabilities, approvals, assets, memory, channels, budget | Prototype-only metrics and fake admin lists |
| `shells.js` | IA exploration for employee/admin shells | Vanilla HTML shell implementation |

## 4. Current Real Frontend Inventory

### 4.1 Route Surfaces

| Current path | Current source | Part 2 disposition |
| --- | --- | --- |
| `/plaza` | `frontend/src/pages/Plaza.tsx` | Keep as Agent Circle / social surface; move into workspace IA with clearer label. |
| `/local-agents` | `frontend/src/pages/LocalAgents.tsx` | Keep as user-scoped Local Agent Channel; surface under workspace connections/local runtime. |
| `/agents/new` | `frontend/src/pages/AgentCreate.tsx` | HR Agent-only employee creation entry. It may show a single governed HR Agent entry card/loading/error state, but must not expose blank/template/direct-create paths. |
| `/agents/:id` | `frontend/src/pages/AgentDetail.tsx` | Convert into Agent Workbench with grouped product areas. |
| `/agents/:id/chat` | `frontend/src/pages/Chat.tsx` redirect | Preserve redirect to session workbench. |
| `/messages` | `frontend/src/pages/Messages.tsx` | Keep or fold into conversations/tasks if still user-visible. |
| `/enterprise/dashboard` | `frontend/src/pages/Dashboard.tsx` | Become workspace home / control-plane overview depending role and shell mode. |
| `/enterprise/*` | `EnterpriseSettings` + workspace sections | Reframe from settings tabs into Control Plane sections. |
| `/admin/platform-settings` | `AdminCompanies.tsx` | Keep platform-admin-only surface; do not mix with tenant Control Plane. |

### 4.2 Agent Detail Modules

Current `AGENT_DETAIL_TABS`:

```text
status, aware, knowledge, evolution, tools, skills, subagents,
relationships, workspace, workflows, office, chat, activityLog,
approvals, settings
```

These are not deleted. They are folded into product areas:

| Target area | Current modules |
| --- | --- |
| Overview | `status`, runtime summary, recent `activityLog`, pending approvals, active sessions |
| Capabilities | `tools`, `skills`, `workflows`, MCP/extension status, Office capability, channels capability |
| Memory & Knowledge | `knowledge`, `evolution`, memory events, soul candidates, skill/workflow candidate links |
| A2A / Team | `subagents`, `relationships`, delegation traces, peer/team memory summaries |
| Documents & Workspace | `workspace`, `office`, generated files, artifact browser |
| Permissions & Settings | `approvals`, access/sharing, expiry, guard policies, advanced `settings` |

### 4.3 Workspace / Control Plane Modules

Current workspace sections:

```text
dashboard, info, llm, eval_ci, memory, hr, tools, skills, subagents,
quotas, users, org, approvals, audit, invites
```

These become productized Control Plane areas:

| Target control-plane area | Current modules |
| --- | --- |
| Overview | `dashboard`, runtime health, tool failures, attention queue |
| Members & Org | `users`, `org`, `invites` |
| Agent Governance | agent list, HR Agent, owner/status/scope/risk, `hr` |
| Models & Budget | `llm`, `quotas`, eval/runtime status, budget usage |
| Capabilities & Tools | `tools`, `skills`, MCP/extensions/plugins, shared skill registry |
| Team / Delegation | `subagents`, org delegation policies |
| Memory Governance | `memory`, company knowledge, sensitivity, retention |
| Channels & Integrations | Feishu/Slack/Dingtalk/WeCom/Teams/Telegram/email/channel config surfaces where available |
| Approval Center | `approvals` |
| Audit Log | `audit` |
| Assets & Automation | workflow definitions, skill candidates, templates, reusable assets |

## 5. Target Information Architecture

### 5.1 Global Shell

The normal workspace shell should expose one compact Codex-style object rail plus a role-gated Control Plane entry. Deep feature modules remain accessible from Home cards, Agent Detail tabs, command/search, or Control Plane surfaces; they should not all become permanent left-rail items.

```text
My Workspace
  Workspace
    我的工作区 / current workspace

  Home
  Agent Circle
  Tasks / Automation
  Bridge

  Digital Employees
    ▸ owned employee
        session [任务]
        session [IM]
    ▸ public/shared employee [公共]
    ▸ local employee [本地]
        session [本地]
    + 新建数字员工

  User / Settings
  Company Admin / Control Plane

Control Plane
  Overview
  Members & Org
  Agent Governance
  Models & Budget
  Capabilities & Tools
  Team & Delegation
  Memory Governance
  Channels & Integrations
  Approval Center
  Audit Log
  Assets & Automation
```

Rules:

1. The shell mode is route-backed, not a frontend-only pseudo-router.
2. Workspace switcher uses real tenant/user context.
3. Search is a command/quick-open surface, not a permanent workspace-search row in the left rail.
4. Notifications use existing notification center data, not fake counters.
5. The global sidebar should not duplicate the local agent/session rail inside the conversation workbench.
6. `+ 新建数字员工` is placed under Digital Employees and remains the only visible create entry into the HR Agent creation flow.

### 5.2 My Workspace Surfaces

| Page | Purpose | Real sources |
| --- | --- | --- |
| Home | Needs attention, running work, quick actions, recent artifacts/activity, usage summary | `Dashboard`, agents API, tasks/activity APIs, notifications |
| Digital Employees | Sidebar object tree plus an independent directory page when users need broader browsing/filtering | agents API, access level, agent status |
| Conversations & Tasks | Cross-agent sessions/tasks entry from Home or command/search; current agent session workbench is the detailed view | chat sessions, RuntimeTask, Work Ledger, plans |
| Plan Review | Pending plan confirmations and ask-user questions surfaced from Home attention blocks, Agent Detail, or command/search | plans API, chat runtime state, approval metadata |
| Automations | Workflows, scheduled/triggered runs, save successful task as workflow from Home or Agent Detail | workflows API, schedules/triggers, skill/workflow promotion |
| Memory & Knowledge | Per-agent memory/knowledge tabs plus workspace-wide entry from Home/Control Plane | knowledge API, memory API, workspace memory config |
| Local Agent Channel | Local bridge connection, local workspace files, local transcript under Bridge and Agent/session context | localBridge API, existing `LocalAgents` page |
| Approvals | User-facing approvals from Home needs-you, Agent Detail, and Control Plane | agent/workspace approvals APIs |

### 5.3 Agent Workbench

Target areas:

1. Overview
2. Conversation & Tasks
3. Capabilities
4. Memory & Knowledge
5. A2A / Team
6. Documents & Workspace
7. Permissions & Settings

Agent Workbench header:

1. Agent identity, role, owner, access level, status.
2. Primary action: talk / continue current session.
3. Secondary actions: new task, plan review, open artifacts, configure.
4. Compact governance chips: visibility, capability policy, approval state, expiry.

Agent Workbench body:

1. Daily work defaults to Conversation & Tasks for normal users.
2. Admin/manage mode can default to Overview.
3. Advanced controls are still reachable, but not exposed as the primary page rhythm.
4. Local Agent Channel stays user-scoped and must not reappear as an agent detail tab unless the backend makes it agent-scoped.

### 5.4 Control Plane

Control Plane must read as an operating console:

1. Overview: health, usage, pending approvals, tool/runtime failures, active employees, budget risk.
2. Members & Org: users, departments/groups, invitations.
3. Agent Governance: employee ownership, visibility, status, risk posture, lifecycle state.
4. Models & Budget: model configuration, quotas, eval CI, budget utilization.
5. Capabilities & Tools: enterprise tools, MCP servers, plugins, skill registry, capability openness.
6. Team & Delegation: subagents, team policies, delegation controls.
7. Memory Governance: company memory settings, shared knowledge, sensitivity/retention.
8. Channels & Integrations: Feishu, Slack, Dingtalk, WeCom, WeChat, Teams, Telegram, email, local bridge policies where applicable.
9. Approval Center: approvals by risk/source/action type.
10. Audit Log: action and config history.
11. Assets & Automation: workflow assets, candidate packages, templates, reusable skills.

## 6. Page-Level Design Contract

### 6.1 Shell

Required changes:

1. Convert sidebar from agent-list-first into workspace/control-plane tree with a scoped agent/session list where appropriate.
2. Keep agent search/pin, tenant switch, notifications, account menu, theme toggle.
3. Add global search entry, but route-backed first.
4. Preserve role guards: normal user, org admin, platform admin.
5. Do not collapse all enterprise functions behind one `Company Admin` item.

### 6.2 Home

Home should answer:

1. What needs me now?
2. Which agents are working?
3. What was produced recently?
4. Which quick action should I take?
5. Are usage/budget/approval risks visible?

Prototype quick actions are valid, but actions must map to real routes:

| Quick action | Real route/action |
| --- | --- |
| Create digital employee | `/agents/new` HR Agent creation entry |
| Delegate new task | open agent/session picker or default agent session |
| Save successful work as automation | workflow promotion surface |
| Browse company assets | Control Plane Assets & Automation |

### 6.3 Digital Employees

Required:

1. Sidebar rows are the primary object tree; an independent employee directory page remains available for broader browsing/filtering.
2. Do not add `My Employees` / `Company Employees` group headers in the primary sidebar. Directory filters may still expose all/mine/shared/status/capability/visibility.
3. Cards show status, role, owner/access, last activity, pending attention.
4. Empty state uses the real HR Agent creation route.
5. Clicking an employee opens Agent Workbench, with Conversation & Tasks available immediately.
6. The persistent sidebar create action is `+ 新建数字员工` at the bottom of the Digital Employees tree.

### 6.4 Create Digital Employee

`/agents/new` is intentionally **HR Agent-only**.

Hive's product feature is that employee creation is handled by the HR Agent as a governed creation role. The user should not see multiple creation paths such as blank employee, template employee, or direct form-based creation.

Required behavior:

1. The only exposed path is entering the HR Agent creation session.
2. The primary shell placement is the `+ 新建数字员工` row under Digital Employees. Home may also show a quick action card, but it is secondary.
3. The page may show a single HR Agent entry card, loading state, and error recovery.
4. The HR Agent clarifies role, visibility, capability packs, memory boundaries, approval needs, and the first work session.
5. Final employee creation still happens through the governed backend, but that direct API is not exposed as a separate frontend path.
6. If future templates exist, they are HR Agent prompts/presets inside the HR Agent flow, not separate top-level creation options.

### 6.5 Conversation & Tasks

Part 1 contracts apply here. Part 2 only embeds this workbench into the broader product shell.

Required desktop layout:

```text
Agent/session local rail | unified timeline stream | artifact/task inspector
```

Required behavior:

1. Assistant output is one continuous timeline, not stitched cards.
2. User prompts stay stable during tool calls and active run transitions.
3. Commands live in slash menu/composer controls, not as persistent command bars.
4. Plan, ask-user, Work Ledger, runtime disclosure, artifacts, and files stay in the unified session model.
5. Right inspector owns previews and detailed task context.

### 6.6 Capabilities

Capabilities page consolidates:

1. Tools and MCP/plugin status.
2. Skills and skill files.
3. Workflows and workflow runs.
4. Office/document capability.
5. Channel capabilities and approval requirements.
6. Optional coding pack status as non-default/explicitly activated when product policy says so.

The page must show governance states:

1. enabled
2. needs admin
3. needs approval
4. locked by company policy
5. installed but not assigned
6. runtime unavailable

### 6.7 Memory & Knowledge

Memory & Knowledge must reflect the current Memory system:

1. T0 JSONL is mechanical truth; Markdown projections are readable views.
2. T2/T3/soul/skill paths are LLM-driven governed write surfaces.
3. The UI should expose current memory/knowledge, source refs, candidates, and held curations without implying the user edits raw T0.
4. Save Memory should show that writes go through the governed memory path, not a platform-authored direct edit.
5. Skill evolution candidates should link from memory evidence to Skills, not become hidden background behavior.

### 6.8 A2A / Team

Required:

1. Show subagents, relationships, delegation traces, peer/team summaries.
2. Distinguish session-local team/window behavior from org-level A2A/delegation.
3. Make delegation state inspectable without turning it into workflow control flow.
4. Team chat/window behavior should be treated as a higher-interaction sibling of subagent, not as a replacement for long-term org delegation.

### 6.9 Documents & Workspace

Required:

1. Generated artifacts list and preview.
2. Office document editing route/surface.
4. File download/open/continue actions.
5. Source and sensitivity disclosure where applicable.

Right rail previews should handle lightweight inspection; full editing can open a dedicated workspace/document surface.

### 6.10 Approvals

Employee-side approvals:

1. User approvals for high-risk actions, channel sends, external visibility, budget-sensitive work.
2. Approve, reject, request changes.
3. Link back to source session/task.

Control-plane approvals:

1. Capability opening.
2. Channel connection.
3. Memory/company knowledge changes.
4. Asset promotion.
5. Budget/model changes.

### 6.11 Local Agent Channel

Local Agent Channel remains user-scoped:

1. Keep `/local-agents`.
2. Show local bridge status, connection presence, local workspace uploads/files, transcript.
3. Do not put Local Agent into `AgentDetail` tabs unless backend ownership changes.
4. In the global shell, surface it as local runtime/channel infrastructure.

## 7. Frontend/Backend Alignment Law

Part 2 must not create a front-back split.

Rules:

1. Every user-visible module has an owner source: API domain client, websocket event, local state derived from real query data, or explicit disabled setup state.
2. If backend support is missing, the document/API contract must be written and the UI must remain disabled or route to setup. No fake persistence.
3. No UI action can bypass existing governance: Plan Mode, approvals, ToolRuntimeService, Memory Gate/Platform Gate, capability policies, tenant guards.
4. Feature discovery is allowed; fake success is not.
5. Advanced/debug capability may be moved under advanced sections, but must not disappear if users need it.

Capability ownership matrix:

| Capability | Frontend owner | Data/runtime owner |
| --- | --- | --- |
| Session transcript and resume | Session Workbench | `chatApi`, websocket broker, runtime events |
| Plans and ask-user | Conversation & Tasks | `plans.ts`, chat tool envelopes |
| Work Ledger | Conversation & Tasks / right inspector | `autonomyApi.getSessionWorkLedger()` |
| Artifacts/files | Documents & Workspace / inspector | `files.ts`, chat artifact parts |
| Office | Documents & Workspace | `office.ts`, Office service |
| Skills | Capabilities / Control Plane assets | `skills.ts`, skill registry/files |
| Workflows | Automations / Capabilities | `workflows.ts`, schedules/triggers |
| Tools/MCP/plugins | Capabilities & Tools | `tools.ts`, `extensions.ts`, enterprise policy |
| Memory/knowledge | Memory & Knowledge | `memory.ts`, `knowledge.ts`, memory governance |
| Subagents/A2A/team | A2A / Team | `subagents.ts`, relationships, delegation traces |
| Approvals | Approvals / Approval Center | agent/workspace approvals APIs |
| Audit/activity | Overview / Audit Log | activity/admin/enterprise APIs |
| Local agents | Local Agent Channel | `localBridge.ts` |

## 8. Visual System Contract

### 8.1 Tokens

Adopt the prototype's restraint, not its exact palette:

1. Background: neutral app canvas, not strong cream.
2. Panels: white/near-white with subtle borders.
3. Accent: honey for primary Hive actions and attention, used sparingly.
4. Status: semantic ok/warn/danger/info/purple with low saturation.
5. Radii: 6-10px for dense app controls; larger only for modals/empty states.
6. Shadows: minimal; prefer borders and spacing.
7. Typography: existing app stack unless a deliberate product-font decision is made.

### 8.2 Layout

1. Desktop authenticated app uses stable shell + content area.
2. Agent session view uses three columns.
3. Control Plane pages use dense table/list/detail surfaces, not marketing cards.
4. Repeated entities may use cards; page sections should not all be floating cards.
5. Text must not overflow on mobile/tablet; controls need stable dimensions.

### 8.3 Motion

Allowed:

1. Slash menu open/close.
2. Rail drawer open/close.
3. Disclosure expand/collapse.
4. Upload/progress/running indicators.
5. Row hover/focus.

Not allowed:

1. Decorative page-load choreography.
2. Width/height/top/left animation that causes layout thrash.
3. Rebuilding UI state on tool-call flashes.
4. Hiding latency with fake progress.

## 9. Complete Implementation Tracks

These are complete bounded tracks, not MVP stages. Each logic change must start with failing tests.

### Track A: Shell And Navigation

Scope:

1. Workspace/control-plane mode in global shell.
2. Route-backed nav tree.
3. Global search entry.
4. Preserve agent search/pin, notifications, tenant/user controls.
5. Keep `/plaza`, `/local-agents`, `/agents/new`, `/agents/:id`, `/enterprise/*`, `/admin/*` reachable.

Primary files:

1. `frontend/src/pages/Layout.tsx`
2. `frontend/src/pages/layout/AppSidebar.tsx`
3. `frontend/src/surfaces/shared/SurfaceLayout.tsx`
4. `frontend/src/surfaces/workspace/sections.ts`
5. `frontend/src/App.tsx`
6. `frontend/src/i18n/en.json`
7. `frontend/src/i18n/zh.json`

### Track B: Home, Digital Employees, Create Flow

Scope:

1. Home attention/running/recent/activity/usage surface.
2. Independent digital employee list.
3. `/agents/new` as a single HR Agent creation entry.
4. Persistent `+ 新建数字员工` row under the Digital Employees sidebar tree.
5. No blank/template/direct-create frontend path.
6. No mock-created agent state.

Primary files:

1. `frontend/src/pages/Dashboard.tsx`
2. `frontend/src/pages/AgentCreate.tsx`
3. New or refactored employee directory components under `frontend/src/pages/workspace/` or `frontend/src/pages/agents/`
4. `frontend/src/api/domains/agents.ts`

### Track C: Agent Workbench Consolidation

Scope:

1. Reduce visible AgentDetail tabs into product areas.
2. Preserve all existing modules.
3. Make Conversation & Tasks first-class.
4. Route/hash compatibility for legacy tabs.
5. Overview aggregates status, attention, activity, artifacts, capabilities.

Primary files:

1. `frontend/src/pages/AgentDetail.tsx`
2. `frontend/src/pages/agent-detail/*`
3. `frontend/src/pages/session-workbench/*`

### Track D: Workspace Feature Surfaces

Scope:

1. Automations page over workflows/schedules/triggers.
2. Memory & Knowledge cross-agent entry.
3. Documents & Research artifact browser.
4. User approvals page.
5. Local Agent Channel IA placement.

Primary files:

1. `frontend/src/pages/LocalAgents.tsx`
2. `frontend/src/pages/agent-detail/AgentWorkflowsSection.tsx`
3. `frontend/src/pages/agent-detail/AgentKnowledgeSection.tsx`
4. `frontend/src/pages/agent-detail/OfficeWorkbenchSection.tsx`
5. `frontend/src/pages/workspace/WorkspaceApprovalsSection.tsx`
6. `frontend/src/api/domains/*`

### Track E: Control Plane Reframe

Scope:

1. Control Plane overview.
2. Members & Org.
3. Agent Governance.
4. Models & Budget.
5. Capabilities & Tools.
6. Team & Delegation.
7. Memory Governance.
8. Channels & Integrations.
9. Approval Center.
10. Audit Log.
11. Assets & Automation.

Primary files:

1. `frontend/src/pages/EnterpriseSettings.tsx`
2. `frontend/src/surfaces/workspace/WorkspaceLayout.tsx`
3. `frontend/src/surfaces/workspace/sections.ts`
4. `frontend/src/pages/workspace/*`

### Track F: Visual System, Accessibility, Verification

Scope:

1. Token consolidation.
2. Shared app primitives where useful.
3. Responsive behavior.
4. Keyboard navigation.
5. i18n.
6. Reduced motion.
7. Visual/browser verification.

Primary files:

1. `frontend/src/index.css`
2. shared component folders already used by the frontend
3. relevant test files per track

## 10. TDD And Verification Plan

Docs-only updates do not require TDD. Implementation changes do.

Red tests before implementation:

1. Shell renders workspace/control-plane navigation and preserves existing route links.
2. Sidebar keeps agent search/pin and Local Agent entry.
3. `/agents/new` renders only the HR Agent creation path.
4. Agent Workbench maps legacy tab hashes to product areas without losing modules.
5. Conversation & Tasks still renders `session-composer`, unified timeline, active run, slash menu, and right inspector.
6. Capabilities page exposes tools, skills, workflows, governance states.
7. Memory & Knowledge links evidence/candidates to existing APIs without raw T0 editing.
8. Control Plane sections map to current workspace sections and keep role guards.
9. Local Agent Channel remains user-scoped.
10. i18n keys exist in both English and Chinese.
11. Plan revision/reject controls render inside `PlanCard`, not as browser-native prompts.
12. Workspace feature hubs render real cross-agent read models for plan, memory, approval, document, and workflow states.

Target commands:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm test -- --run \
  src/pages/layout/LayoutSections.test.tsx \
  src/pages/AgentDetail.test.tsx \
  src/pages/AgentDetail.query-gating.test.tsx \
  src/pages/agent-detail/AgentDetailSections.test.tsx \
  src/pages/agent-detail/ChatWorkLedgerDock.test.tsx \
  src/pages/agent-detail/CommandPalette.test.tsx \
  src/pages/agent-detail/SlashCommandMenu.test.tsx \
  src/pages/Dashboard.test.tsx \
  src/pages/LocalAgents.test.tsx \
  src/pages/workspace/WorkspaceRemainingSections.test.tsx \
  src/pages/workspace/WorkspaceToolsSection.test.tsx \
  src/pages/workspace/WorkspaceSubagentsSection.test.tsx
npm run build
```

Latest code-level closure commands:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm test -- --run src/pages/AgentDetail.query-gating.test.tsx src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/WorkspaceFeatureHub.test.tsx
npm run build
```

Visual verification:

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm run dev -- --host 0.0.0.0 --port 3008
```

Inspect:

1. `/agents/:id#chat` desktop, tablet, mobile.
2. `/agents/:id` default user mode and manage mode.
3. `/agents/new` HR Agent-only creation entry.
4. employee directory.
5. workspace home.
6. automations, memory, documents, approvals surfaces.
7. control-plane sections.
8. `/local-agents`.

## 11. Open Product Decisions

Resolved:

1. CC Design is the product/IA baseline.
2. Codex Desktop is the interaction-detail overlay.
3. Hive runtime/API remains truth.
4. Session-internal experience is the foundation, not a normal tab.
5. All user-visible capabilities must have front-end representation.

Open:

1. Should normal users land on Home or Digital Employees after login?
   - Recommendation: Home, because it aggregates attention/running work.
2. Should selecting an agent default to Conversation & Tasks or Overview?
   - Recommendation: Conversation & Tasks for normal users; Overview for manage/admin mode.
3. Should templates or direct forms become separate `/agents/new` creation paths?
   - Resolved: no. `/agents/new` is HR Agent-only. Templates or presets may exist inside the HR Agent conversation, but no separate frontend creation path is exposed.
4. Should Control Plane live under `/enterprise/*` or a clearer `/control-plane/*` alias?
   - Recommendation: keep `/enterprise/*` for compatibility; optionally add alias later.
5. Should global search initially search only routes, or agents/tasks too?
   - Resolved: route + loaded agent search first. Task/doc search waits for an explicit API/search contract.

## 12. Acceptance Criteria

Part 2 is accepted when:

1. The app feels like a coherent agent workbench, not a collection of admin/debug tabs.
2. Session experience remains continuous and Codex/CC-grade.
3. Every user-visible Hive capability has a clear entry point or clearly disabled setup state.
4. Agent Workbench exposes daily work, capabilities, memory, team/delegation, documents, approvals, and settings without hiding real functions.
5. Control Plane exposes company-scale governance, budget/model, capability, memory, channel, approval, audit, and asset operations.
6. `/agents/new` exposes a single HR Agent creation model, with no blank/template/direct-create fork.
7. Prototype visual language is adapted, not copied; no fake state, no mock metrics, no decorative-only controls.
8. Existing routes, guards, API clients, query keys, websocket/runtime semantics, and governance boundaries are preserved.
9. Tests and build pass for every implementation track.
10. Browser verification confirms desktop/tablet/mobile layouts, no text overflow, no composer/session regression, and no visible feature dead ends.

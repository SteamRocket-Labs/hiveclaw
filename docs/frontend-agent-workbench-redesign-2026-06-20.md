# Frontend Agent Workbench Redesign

状态：Part 2 前端代码级闭环完成；live 登录态/生产体验验证仍需单独执行
日期：2026-06-20；Part 2 更新：2026-06-23；闭环更新：2026-06-23
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
3. **Hive runtime is the source of truth.** Existing routes, guards, React Query clients, websocket runtime, Plan Mode, Work Ledger, Office/files/deep-research, memory, workflows, skills, subagents, approvals, audit, local agents, and enterprise settings must be preserved.

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
6. Sidebar search is now a workspace-wide quick opener over routes/control-plane pages/local agents plus loaded employee filtering.
7. `DigitalEmployees` now exposes ownership, shared, recommended, running, attention, coordinator, and local-runtime filters; cards deep-link to chat, memory, workflow, team/relationships, detail, and Local Agent where applicable.
8. `AppDialogs` provides global in-app toast and confirmation. Frontend product code no longer uses browser-native `alert()`, `confirm()`, or `prompt()`; the only remaining `confirm` string is the `planApi.confirm` API method test.
9. `AgentDetail` session-path interruptions have been reduced: session delete uses `ConfirmModal`, and create/branch/upload failures use non-blocking in-app toast instead of `alert()`.
10. Vite build now uses manual vendor chunks for React, React Query, i18n, charts, icons, and shared vendor code to reduce main-entry pressure.

Code-level verification completed:

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
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
| Conversation & Tasks | `chat`, `plans`, Work Ledger, `PlanCard`, `PlanModeRequestCard`, Deep Research stream, artifacts |
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

The shell should expose two first-class modes:

```text
My Workspace
  Home
  Digital Employees
  Conversations & Tasks
  Plan Review
  Automations
  Memory & Knowledge
  Documents & Research
  Local Agent Channel
  Approvals

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
3. Global search must search route entries and, later, real agents/tasks/docs; until then it can be route-search only with clear scope.
4. Notifications use existing notification center data, not fake counters.
5. The global sidebar should not duplicate the local agent/session rail inside the conversation workbench.

### 5.2 My Workspace Pages

| Page | Purpose | Real sources |
| --- | --- | --- |
| Home | Needs attention, running work, quick actions, recent artifacts/activity, usage summary | `Dashboard`, agents API, tasks/activity APIs, notifications |
| Digital Employees | Search/filter employee cards, ownership/shared/recommended grouping, status and permission chips | agents API, access level, agent status |
| Conversations & Tasks | Cross-agent sessions/tasks entry; current agent session workbench is the detailed view | chat sessions, RuntimeTask, Work Ledger, plans |
| Plan Review | Pending plan confirmations and ask-user questions across sessions | plans API, chat runtime state, approval metadata |
| Automations | Workflows, scheduled/triggered runs, save successful task as workflow | workflows API, schedules/triggers, skill/workflow promotion |
| Memory & Knowledge | Cross-agent memory/knowledge entry and per-agent drilldown | knowledge API, memory API, workspace memory config |
| Documents & Research | Generated artifacts, Office docs, deep research outputs | files API, Office API, deep research, artifacts |
| Local Agent Channel | Local bridge connection, local workspace files, local transcript | localBridge API, existing `LocalAgents` page |
| Approvals | User-facing approvals for high-risk actions | agent/workspace approvals APIs |

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

1. Independent employee directory page, not only sidebar rows.
2. Tabs/filters: all, mine, shared, recommended, status, capability, visibility.
3. Cards show status, role, owner/access, last activity, pending attention.
4. Empty state uses real create route.
5. Clicking an employee opens Agent Workbench, with Conversation & Tasks available immediately.

### 6.4 Create Digital Employee

`/agents/new` is intentionally **HR Agent-only**.

Hive's product feature is that employee creation is handled by the HR Agent as a governed creation role. The user should not see multiple creation paths such as blank employee, template employee, or direct form-based creation.

Required behavior:

1. The only exposed path is entering the HR Agent creation session.
2. The page may show a single HR Agent entry card, loading state, and error recovery.
3. The HR Agent clarifies role, visibility, capability packs, memory boundaries, approval needs, and the first work session.
4. Final employee creation still happens through the governed backend, but that direct API is not exposed as a separate frontend path.
5. If future templates exist, they are HR Agent prompts/presets inside the HR Agent flow, not separate top-level creation options.

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
3. Deep Research output browsing.
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
| Deep Research | Documents & Research / session stream | `deepResearch.ts`, runtime panels |
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
4. No blank/template/direct-create frontend path.
5. No mock-created agent state.

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
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
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
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run src/pages/AgentDetail.query-gating.test.tsx src/pages/agent-detail/AgentDetailSections.test.tsx src/pages/WorkspaceFeatureHub.test.tsx
npm run build
```

Visual verification:

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
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

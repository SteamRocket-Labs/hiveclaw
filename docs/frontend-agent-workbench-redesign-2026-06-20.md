# Frontend Agent Workbench Redesign

状态：实施前产品与工程对齐文档
日期：2026-06-20
输入原型：`claude-design-for-hiveclaw/`
目标前端：`frontend/src/`
参考细节：Codex Desktop chat/workbench UI

## 0. Execution Gate

This document is **Phase 2** of the frontend redesign.

Phase 1 is `frontend-session-workbench-cc-codex-parity-gap-2026-06-23.md`: finish every in-session detail first. A session must already behave like a Codex Desktop / CC-grade thread before the broader workbench IA migration starts.

Blocking rule:

1. Do not start broad shell/sidebar/AgentDetail IA migration until Session Workbench is stable.
2. Do not rewrite conversation UX during Phase 2. Reuse the Phase 1 contract: `ThreadTimeline`, `ActiveRunCell`, Codex-like composer, slash menu, right inspector, session-native Goal/Plan/Team/Checkpoint controls.
3. Phase 2 may reorganize where the Session Workbench lives in the app, but it must not turn it back into a normal Agent management tab.

## 1. Decision

Hive 前端要重建为 agent workbench，而不是继续把真实能力堆在一个管理后台式 `AgentDetail` tab 集合里。

决策：

1. 整体 shell、信息架构、页面组织和产品流沿用 `claude-design-for-hiveclaw/` 的方向。
2. 对话输入区、消息阅读流、代码块、文件卡、产物 rail 的细节参考 Codex Desktop。
3. 不直接迁移 prototype mock state。真实状态继续来自 `frontend/src/` 已接线的 API、React Query、web chat runtime、Plan Mode、Work Ledger、Office/files/deep-research 能力。
4. 本次重建的第一目标是“对话与任务工作台”：左侧 agent/会话/任务导航，中间对话与任务流，右侧文件/产物/运行上下文。
5. 这是一轮完整页面形态重建，不是 composer-only 换皮，也不是默认关闭的半成品功能。

Reference priority:

1. **CC Design prototype is the structural baseline.** It defines the agent workbench information architecture, the three-column task surface, and the task lifecycle: delegate -> plan -> execute -> inspect artifacts.
2. **Codex Desktop is the interaction-quality overlay.** It improves the concrete feel of composer controls, message rhythm, code/file cards, low-noise status, and contextual side panels. It must not replace the CC Design IA.
3. **Hive runtime is the source of truth.** Any visible state must map back to real Hive APIs or runtime events. Prototype state and Codex-only environment affordances cannot enter product code as decorative UI.

原因：

1. 现有 `AgentDetail.tsx` 暴露 15 个工程 tabs，适合调试和管理，不适合作为数字员工的日常工作界面。
2. `claude-design-for-hiveclaw/chat-task.jsx` 已经表达了正确的产品流：交办任务 -> 计划确认 -> 执行进度 -> A2A -> 产物。
3. Codex Desktop 的强项是工作流密度、输入 composer、消息阅读节奏、文件/环境侧栏的低噪音表达；这些正好可以补齐 prototype 细节。
4. Hive 的真实能力已经存在于 runtime 和前端模块里，重建应重组 shell/IA/UX，而不是重写 runtime。

## 2. Product Thesis

Visual thesis：Hive agent workbench 应该像一个安静、可长时间工作的桌面操作台：浅色中性表面、清晰三栏、中央阅读流、右侧产物上下文、底部高质量 composer。

The three-column layout is accepted as the target desktop shape. Future implementation discussion should focus on component boundaries, responsive behavior, and data mapping, not on whether the workbench is one-column, two-column, or three-column on desktop.

Interaction thesis：

1. 用户在中间交办任务，agent 以文档流方式回应；只有用户消息保留气泡感。
2. Plan Mode、附件、权限、停止、发送等操作集中在底部 composer，使用图标和 tooltip，而不是堆文字按钮。
3. 文件、报告、表格、图片、Office 文档和 deep research 产物优先进入右侧 rail 预览；聊天流只保留紧凑引用卡。
4. Work Ledger、compaction、tool/runtime 细节默认克制显示，必要时展开，不把内部调试文本放进主阅读流。

Primary user action：用户进入某个数字员工后，应能立即交办任务、确认计划、观察执行、查看产物并继续修改。

## 3. Source Inventory

### 3.1 Prototype Inputs

| Prototype file | Keep | Do not keep |
| --- | --- | --- |
| `claude-design-for-hiveclaw/app-shell.jsx` | workspace switcher、Notion-tree sidebar、employee/control-plane split、global search pattern | mock agent arrays、window globals、inline fake navigation |
| `claude-design-for-hiveclaw/chat-task.jsx` | task lifecycle, plan card, progress timeline, right task rail, artifact cards, composer shape | local state demo timers, fake plan/artifact data |
| `claude-design-for-hiveclaw/emp-workspace.jsx` | 6-tab employee workbench framing: overview, capabilities, memory, A2A, permissions, settings | fake capability state and non-real permission changes |
| `claude-design-for-hiveclaw/design-tokens.css` | warm neutral palette, restrained honey accent, low-contrast surfaces | any token that breaks existing accessibility or product states |

### 3.2 Current Real Frontend Inputs

| Current path | Existing real capability to preserve |
| --- | --- |
| `frontend/src/App.tsx` | real routes and guards |
| `frontend/src/pages/Layout.tsx` and `frontend/src/pages/layout/AppSidebar.tsx` | current app shell, account menu, notifications, tenant context |
| `frontend/src/pages/AgentDetail.tsx` | agent data loading, tab routing, websocket/session state, upload state, Plan Mode toggles |
| `frontend/src/pages/agent-detail/AgentChatSection.tsx` | active session list, all-users view, message rendering, attachments, artifact preview, runtime events |
| `frontend/src/pages/agent-detail/PlanCard.tsx` | real plan confirmation card and plan refetching |
| `frontend/src/pages/agent-detail/PlanModeRequestCard.tsx` | interactive Plan Mode entry request |
| `frontend/src/pages/agent-detail/ChatWorkLedgerDock.tsx` | real Work Ledger/Todo data and collapse semantics |
| `frontend/src/pages/agent-detail/DeepResearchStreamPanel.tsx` | live deep research stream |
| `frontend/src/pages/agent-detail/OfficeWorkbenchSection.tsx` | real Office/document editing surface |
| `frontend/src/api/domains/chat.ts`, `files.ts`, `plans.ts`, `autonomy.ts`, `office.ts` | data contracts |

## 4. Target Information Architecture

### 4.1 Global Shell

The global shell should move toward the CC Design prototype:

```text
My Workspace
  Home
  Digital Employees
  Conversations & Tasks
  Plan Review
  Automations
  Memory & Knowledge
  Documents & Research
  Approvals

Control Plane
  Overview
  Members & Org
  Agent Governance
  Models & Budget
  Capabilities & Tools
  Memory Governance
  Channels
  Approval Center
  Audit Log
  Assets & Automation
```

This should map to real routes. Do not introduce a pseudo-router that only exists in frontend memory.

### 4.2 Agent Workbench

The agent-facing workbench should reduce the current 15 tabs into product areas:

1. Overview
2. Conversation & Tasks
3. Capabilities
4. Memory & Knowledge
5. A2A Collaboration
6. Permissions & Sharing
7. Settings / Advanced

The current advanced modules are not deleted. They are folded into these product areas:

| Current module | Target area |
| --- | --- |
| status, activityLog | Overview |
| chat, plans, runtime summary | Conversation & Tasks |
| tools, skills, workflows, office capability settings | Capabilities |
| knowledge, evolution, memory views | Memory & Knowledge |
| subagents, relationships, delegation traces | A2A Collaboration |
| approvals, sharing, expiry, guard policies | Permissions & Sharing |
| settings and debug-only controls | Settings / Advanced |

## 5. Conversation & Task Workbench

The conversation/task workbench is the first concrete page to rebuild.

This page should be implemented as **CC Design structure plus Codex interaction details**:

1. CC Design supplies the three columns and task lifecycle.
2. Codex supplies composer density, assistant/user message treatment, compact status rows, file cards, and right-rail inspection behavior.
3. Hive supplies the real data and governance boundaries.

### 5.1 Layout

```text
┌────────────────────────┬──────────────────────────────────────┬──────────────────────────┐
│ Left rail              │ Main stream                          │ Right rail               │
│                        │                                      │                          │
│ Agent switcher         │ Compact task/session header           │ Artifacts                │
│ Conversation sessions  │ Assistant document stream             │ File/document preview    │
│ Tasks / Plan Review    │ User message bubbles                  │ Work Ledger              │
│ Automations shortcut   │ Plan cards / progress timeline        │ Runtime context          │
│ Documents shortcut     │ Floating Codex-like composer          │ Sources / permissions    │
└────────────────────────┴──────────────────────────────────────┴──────────────────────────┘
```

Recommended desktop dimensions:

1. Left rail: 248-280px.
2. Main stream: flexible, centered content max 760-880px.
3. Right rail: 320-420px, resizable later if needed.
4. Composer: sticky/floating at bottom of main stream, max width aligned to message column.

Responsive behavior:

1. Below tablet width, right rail becomes a drawer.
2. Below mobile width, left rail collapses to a top session/agent picker.
3. Composer remains visible and keyboard-safe; no toolbar text should overflow.

### 5.2 Left Rail

Left rail jobs:

1. Select agent.
2. Select or create conversation/session.
3. Jump to task/plan/document surfaces.
4. Show minimal live state: active run, waiting approval, unread/needs attention.

Do not duplicate the full global sidebar inside the page. If the global shell already carries workspace navigation, the local left rail should be scoped to the current employee and task/session context.

### 5.3 Main Stream

The main stream should behave more like Codex Desktop than generic IM:

1. Assistant messages render as document content, not heavy chat bubbles.
2. User messages render as right-aligned soft bubbles.
3. Runtime events render as compact status rows or low-noise cards.
4. Plan cards remain explicit and actionable.
5. Progress timeline appears as a structured task object, not raw log spam.
6. Code blocks use a calm neutral surface with copy affordance.
7. Tool output is summarized by default; raw output only behind disclosure.
8. Long artifacts are referenced in-stream and opened in the right rail.

### 5.4 Right Rail

Right rail modes:

1. Artifacts: generated files, reports, spreadsheets, PDFs, images, downloads.
2. Preview: inline Markdown/text/PDF/image/Office preview where supported.
3. Task: current objective, plan status, Work Ledger, progress, blockers.
4. Sources: files, web sources, connector references, permissions/sensitivity.
5. Runtime: websocket state, active run, Plan Mode, model/permission state.

Default priority:

1. If a generated artifact is selected, show preview.
2. Else if a task is active, show task/progress.
3. Else show session context and recent artifacts.

Right rail should not imitate Codex's Git environment literally unless Hive has real data for it. Git branch, local change counts, or commit controls must not be shown as decorative mock UI.

Codex-inspired right-rail details that should be adopted:

1. Compact header with current context title and small icon actions.
2. Lightweight grouped rows for task state, artifacts, sources, and runtime status.
3. One selected artifact preview at a time, with open/download/continue actions close to the preview.
4. Subtle dividers over nested cards; avoid card-inside-card layouts.
5. Status labels should be short: running, waiting, complete, blocked, needs approval.

## 6. Codex-Aligned Composer Details

The composer is the highest-priority micro-UI.

Target anatomy:

```text
┌──────────────────────────────────────────────────────────┐
│ textarea: ask / delegate / continue work                 │
│                                                          │
│ +  attachment  create  plan-mode  plugin     model  send │
└──────────────────────────────────────────────────────────┘
```

Required controls:

1. Add file/image.
2. Plan Mode toggle.
3. Optional create/action menu for task/document/workflow actions.
4. Permission/access indicator.
5. Upload progress/cancel.
6. Stop generation while waiting/streaming.
7. Send button as icon-first control.

Rules:

1. Use icons for common actions; text labels go in tooltips.
2. Textarea auto-grows up to a stable max height.
3. Attachment chips live inside the composer or immediately above it.
4. Disabled states must be explicit when websocket is disconnected, uploading, waiting, streaming, or agent is expired.
5. Enter sends, Shift+Enter inserts newline, IME composition is preserved.
6. Composer should not resize the entire page when toolbars, labels, or upload progress appear.

Codex details to explicitly copy:

1. Input sits in a floating rounded container aligned to the message column.
2. Primary send/stop controls are icon-first and visually stable.
3. Secondary actions live in the composer footer, not as scattered page buttons.
4. File attachments appear as compact chips with remove affordances.
5. Permission/access/model indicators are compact controls, not explanatory banners.
6. The composer remains useful while a run is active: stop is prominent, disabled send state is clear, and the text area does not jump.

## 7. State Model And Data Mapping

| UI concept | Real source |
| --- | --- |
| Conversation sessions | `chatApi.listSessions()` via `AgentDetail.tsx` |
| Transcript replay | `chatApi.getSessionTranscript()` and `chatRuntime.ts` |
| Active run | `chatApi.getActiveSessionRun()` and websocket runtime events |
| Plan confirmation | `PlanCard`, `PlanModeRequestCard`, `plans.ts` |
| Work Ledger | `ChatWorkLedgerDock`, `autonomyApi.getSessionWorkLedger()` |
| Attachments | `/chat/upload`, `uploadFileWithProgress()` |
| Generated artifacts | `ChatArtifactPart`, `fileApi.downloadUrl()`, `fileApi.read()` |
| Deep Research | `DeepResearchStreamPanel` |
| Office/document editing | `OfficeWorkbenchSection`, `office.ts` |
| Runtime notices | `getTransportNotice()`, `getRuntimeEventMessage()` |
| Compaction details | `getCompactionDisplayContent()` with details behind disclosure |

## 8. Design Boundaries

### 8.1 Keep

1. Existing auth, routing, query keys, API domain clients.
2. Durable web chat run semantics.
3. Active-run recovery and websocket keepalive.
4. Plan Mode confirmation boundary.
5. Work Ledger collapse semantics.
6. Artifact download and preview behavior.
7. Office and Deep Research feature surfaces.
8. i18n in both `en.json` and `zh.json` for new visible text.

### 8.2 Change

1. Current tab-heavy `AgentDetail` presentation.
2. IM-style assistant bubbles.
3. Bottom composer layout and visual treatment.
4. Artifact preview placement.
5. Runtime/status chrome density.
6. Shell/sidebar hierarchy.

### 8.3 Do Not Do

1. Do not copy prototype fake data.
2. Do not show Git/environment controls unless backed by real Hive data.
3. Do not hide half-built work behind default-off flags.
4. Do not ship composer-only styling as the workbench redesign.
5. Do not expose raw compaction summaries or debug text by default.
6. Do not bypass existing tool/runtime governance for UI convenience.

## 9. Complete Implementation Passes

These are complete bounded passes, not MVP stages. Each pass must preserve existing production behavior and ship with tests.

### Pass A: Conversation & Task Workbench

Complete scope:

1. Rebuild chat page as three-column workbench.
2. Preserve sessions, admin all-users view, new/delete session, read-only sessions.
3. Preserve upload, paste image/file, upload progress, cancel upload.
4. Preserve sending, Plan Mode toggle, stop generation, websocket disconnected/expired states.
5. Preserve plan cards, clarification cards, tool result cards, Deep Research, Work Ledger.
6. Move artifact preview/right context into the right rail.
7. Implement responsive collapse for right rail and left local rail.
8. Add i18n and accessibility labels.
9. Add tests and build verification.

Primary files:

1. `frontend/src/pages/agent-detail/AgentChatSection.tsx`
2. `frontend/src/pages/agent-detail/ChatWorkLedgerDock.tsx`
3. `frontend/src/pages/agent-detail/chatRuntime.ts`
4. `frontend/src/pages/AgentDetail.tsx`
5. `frontend/src/index.css`
6. `frontend/src/i18n/en.json`
7. `frontend/src/i18n/zh.json`
8. `frontend/src/pages/agent-detail/AgentDetailSections.test.tsx`
9. `frontend/src/pages/agent-detail/ChatWorkLedgerDock.test.tsx`
10. `frontend/src/pages/agent-detail/chatRuntime.test.ts`

### Pass B: Shell And IA Migration

Complete scope:

1. Move app shell toward prototype workspace/control-plane navigation.
2. Keep real routes and guards.
3. Preserve tenant switch, notifications, account menu, global search entry.
4. Map employee/control-plane navigation to real pages.
5. Add tests for visible navigation and route preservation.

Primary files:

1. `frontend/src/pages/Layout.tsx`
2. `frontend/src/pages/layout/AppSidebar.tsx`
3. `frontend/src/surfaces/shared/SurfaceLayout.tsx`
4. `frontend/src/surfaces/workspace/sections.ts`
5. `frontend/src/App.tsx`
6. `frontend/src/index.css`
7. `frontend/src/i18n/en.json`
8. `frontend/src/i18n/zh.json`

### Pass C: Agent Workbench Consolidation

Complete scope:

1. Reduce current 15 visible tabs into product areas.
2. Fold advanced/debug modules under product sections without deleting capability.
3. Make Conversation & Tasks the first-class daily work surface.
4. Preserve direct deep links for legacy hashes where practical.
5. Add tests for hash routing and module availability.

Primary files:

1. `frontend/src/pages/AgentDetail.tsx`
2. `frontend/src/pages/agent-detail/*`
3. `frontend/src/pages/AgentDetail.test.tsx`
4. `frontend/src/pages/AgentDetail.query-gating.test.tsx`

## 10. Test Plan

Docs-only changes do not require TDD. Implementation changes do.

Red tests before code changes:

1. `AgentChatSection` renders the new workbench shell with left rail, main stream, right rail, and composer.
2. Existing session switching and all-users rendering remain accessible.
3. Composer exposes file upload, Plan Mode, stop/send controls with accessible labels.
4. User messages and assistant messages use distinct layout semantics.
5. Artifact cards can open in right rail preview.
6. Work Ledger collapse still avoids rendering task rows while collapsed.
7. Compaction/runtime details remain behind disclosure.
8. Mobile/responsive classes or state expose rail collapse behavior.

Verification commands:

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm test -- --run \
  src/pages/agent-detail/AgentDetailSections.test.tsx \
  src/pages/agent-detail/ChatWorkLedgerDock.test.tsx \
  src/pages/agent-detail/chatRuntime.test.ts \
  src/pages/AgentDetail.test.tsx \
  src/pages/AgentDetail.query-gating.test.tsx
npm run build
```

Visual verification:

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run dev -- --host 0.0.0.0 --port 3008
```

Then inspect:

1. `/agents/:id#chat` desktop width.
2. Tablet width with right rail collapsed/drawer.
3. Mobile width with local rail collapsed.
4. Empty session.
5. Active run with streaming/stop.
6. Artifact preview.
7. Plan confirmation.
8. Work Ledger live and collapsed.

## 11. Open Decisions

Resolved:

1. The desktop workbench target is a three-column layout.
2. CC Design remains the product/IA baseline.
3. Codex Desktop is used as an interaction-detail overlay, especially for composer, message stream, file cards, and low-noise side-panel treatment.

Open:

1. Should `/agents/:id#chat` become the default route after selecting an agent, or should Overview remain default?
   - Recommendation: default to Conversation & Tasks for normal users; keep Overview for admin/manage mode.
2. Should the right rail be resizable in the first implementation pass?
   - Recommendation: not initially. Make fixed width plus collapse first; add resize later only if real usage demands it.
3. Should Office editing open inside the right rail or a dedicated document workbench?
   - Recommendation: preview/read in right rail; full editing in Office workbench/modal route.
4. Should global shell and local left rail both be visible?
   - Recommendation: avoid double navigation. Global shell handles workspace-level navigation; local left rail handles current agent/session/task context.
5. Should Codex-like model selector appear in composer?
   - Recommendation: only show it if the current user can actually switch model for the agent/session. Otherwise show a compact read-only model/status label or omit it.

## 12. Acceptance Criteria

The redesign is accepted when:

1. A user can open an agent and immediately understand where to talk, where to inspect files, and where to track work.
2. The page visually reads as a workbench, not a settings page with chat embedded.
3. The composer feels close to Codex Desktop in density, affordance, and stability.
4. Assistant output is readable as work product, not just chat transcript.
5. Generated artifacts are visible and useful without leaving the task context.
6. Runtime details are available but not noisy by default.
7. Existing chat/runtime behavior is preserved by tests and build verification.
8. No prototype mock data or decorative fake environment state enters product code.

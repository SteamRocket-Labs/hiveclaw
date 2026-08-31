# Frontend Session Workbench CC/Codex Parity Refactor

日期：2026-06-23

状态：当前前端会话体验重构的决策文档。本文替代“继续修 `AgentDetail` chat tab”的思路，作为后续代码重构的产品与工程准绳。

## Execution Order — 两阶段前端改造顺序

当前前端改造分两阶段，顺序不能反过来：

1. **Phase 1：Session 内体验先追平 Codex Desktop / CC。**
   - 一个 session 对应一条 timeline。
   - 一个 assistant turn 对应一个连续演进的 active run cell。
   - thinking、tool call、hook、permission、AskUserQuestion、Plan、Work Ledger、compaction、checkpoint、final answer 都必须在同一条 thread 语法下表达。
   - composer、slash command、attachment、stop/send、Plan Mode、queued prompt、tool progress、artifact chips、right inspector 的交互细节都按 Codex Desktop 标准做完。
   - 这一阶段是当前最高优先级。对话是主体，先把对话内所有细节做完，再谈全局页面改造。

2. **Phase 2：执行 `frontend-agent-workbench-redesign-2026-06-20.md` 的整体现代化。**
   - 在 Phase 1 稳定后，再重构全局 shell、agent workbench IA、左侧导航、右侧产物 rail、Agent 管理页整合、全局视觉 token 和 responsive behavior。
   - Phase 2 可以吸收 `claude-design-for-hiveclaw/` 的结构方向，但不能破坏 Phase 1 已完成的 Session Workbench contract。

执行原则：

- Phase 1 是阻塞门。Session 内体验没有达到 Codex Desktop 标准前，不启动全局 shell/IA 的大面积迁移。
- Phase 2 必须以 Phase 1 的 `ThreadTimeline` / `ActiveRunCell` / `Composer` / `Inspector` 为基础，不重新发明聊天体验。
- 任何前端任务如果同时触及 session 体验和全局 workbench，先按 Phase 1 判定，再按 Phase 2 扩展。

## 0. 结论

Hive 的单 Agent 会话能力已经在 runtime/API 层长出很多 CC/Codex parity 能力，但当前前端仍把它们塞进 `AgentDetail` 管理页的一个 tab 里。这个组织方式天然会显得“拼接”，无法获得 CC / Codex 那种 transcript-first、run-cell-first 的一体感。

### 0.1 2026-06-23 当前判断口径

本文件讨论的是 **Session Workbench 前端会话体验**，不是重新判定整个后端/session runtime 的完成度。

上一轮口头判断里的“架构端约 70% 对齐”只指 **前端 session presentation architecture**：

- 已有 `ThreadTimeline` view model、`active-run-cell`、session header、session inspector、session-native controls、slash menu 等正确骨架。
- 但 waiting state、artifact preview、branch compose、attachment strip、composer 状态、run disclosure 展开策略、right rail 响应式等仍有组件各自为政的问题。
- 所以它是“前端会话呈现架构已经从散乱组件进化到统一骨架，但还没有完全收口”的 70%，不是“整体 CC/Codex session 基座只有 70%”。

整体单 Agent session/runtime 的当前口径应沿用 `docs/cc-codex-gap-ledger-2026-06-22.md`：

- CC / FreeCode 单 Agent 生命周期底座在本轮已经达到 **code-level closed for this pass**。
- Session JSONL truth、resume、checkpoint、rollback、branch、command dispatcher、Goal、Team、Task、Skill、Workflow、MCP、Plan Mode、Sub-agent、Work Ledger、Hooks event surface 都已有代码路径和定向测试覆盖。
- 剩余 release gates 是全量测试、live browser pass、killed-process resume smoke、Team/Goal/Hook 等生产式验收；这些是 release evidence，不等价于“后端 session 仍然碎片化”。

因此当前风险分层必须写清楚：

| Layer | 当前判断 | 是否属于“70%” |
| --- | --- | --- |
| Backend/session runtime | Code-level closed for this pass；仍需 release evidence | 否 |
| API/read model/session commands | 基础已接上；仍需 live/browser 验证 command/team/goal/hook 可读性 | 否 |
| Frontend session presentation architecture | 有统一骨架，但仍有外围状态绕开 `ThreadTimeline` / composer / inspector contract | 是 |
| Frontend interaction quality | 仍明显落后 Codex Desktop，尤其是 composer 稳定性、active cell 连续感、artifact rail、run 折叠策略 | 否；这是体验层约 50% |

北极星口径：Hive 的目标仍是 **CC 基底 + Codex 体验/压缩/提示词优势 + Hive Memory/治理增强**。前端 Phase 1 的任务不是重做后端基座，而是把已经接好的单 session 能力投影成一个 CC/Codex-grade 的连续会话工作台。

### 0.2 2026-06-23 Session 内体验收口结果

本轮代码收口后，`0.1` 的 70%/50% 判断已经过期。新的判断口径：

| Layer | 当前状态 | 完成度口径 |
| --- | --- | --- |
| Backend/session runtime | 继续沿用 code-level closed for this pass；本轮没有重做后端 | 不低于本轮后端基座目标 |
| Frontend session presentation architecture | `ThreadTimeline` 现在承载 active waiting run；旧 waiting bubble 移除；active run cell 成为运行状态唯一主表达 | Code-level 95%+ |
| Frontend interaction quality | composer 变成唯一输入容器；slash menu、attachment、branch compose 归入 composer；artifact preview 移到 inspector；completed run 默认收敛，running/blocked 默认展开 | Code-level 90-95%，需浏览器视觉验收到 95%+ |
| Performance/layout stability | session composer 和 AskUserQuestion 进度状态去掉 `transition: width`，改用 transform/opacity 类更新 | Code-level closed |

本轮实际代码落点：

- `frontend/src/pages/session-workbench/timelineModel.ts`：为 waiting/streaming/active run 生成 synthetic `active_run` cell，并避免重复运行 cell。
- `frontend/src/pages/agent-detail/AgentChatSection.tsx`：聊天区消费统一 `threadTimelineModel`，移除旧 waiting bubble；slash menu、附件、branch compose、输入行统一进 `session-composer`；artifact preview 进入 `SessionWorkbenchInspector`。
- `frontend/src/pages/agent-detail/RunDisclosureBlock.tsx`：completed run 默认折叠成 compact step summary；running/blocked/failed 默认展开；移除 75% 宽卡片感。
- `frontend/src/pages/agent-detail/SlashCommandMenu.tsx`：slash menu 改成 composer 内浮层，而不是参与主布局的页面模块。
- `frontend/src/pages/agent-detail/AskUserQuestionCard.tsx`：进度点不再使用 width transition。

验证结果：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm test -- --run
# 61 passed, 297 tests passed

npm run build
# tsc + vite build passed

npx impeccable --json src/pages/agent-detail/AgentChatSection.tsx src/pages/agent-detail/RunDisclosureBlock.tsx src/pages/agent-detail/AskUserQuestionCard.tsx src/pages/session-workbench
# []
```

剩余不能在代码静态层直接宣称 100% 的部分：

1. 真实登录态 browser pass：running tool call、AskUserQuestion、Plan Mode、artifact preview、slash menu、附件上传、branch compose、Team/Goal/Checkpoint 控件。
2. 窄屏/移动 viewport 视觉验收：right inspector 目前仍是桌面 rail；是否要升级为 drawer 属于下一刀视觉验收。
3. 真实后端流式事件联调：确认 WebSocket/runtime polling 混合更新不会再导致 composer prompt 丢失或 active run 抖动。

这不是单个组件的样式问题，也不是命令条、卡片、loading 状态某一处没调好。根因是展示模型错误：

```text
当前模型：
Agent Detail 管理页
  多个管理 tabs
  Chat tab
    session list
    transcript message list
    run disclosure card
    work ledger dock
    command/slash menu
    attachment row
    composer

目标模型：
Session Workbench
  session 是主对象
  transcript 是主画布
  active run 是一个连续演进的 typed cell
  tools / thinking / hooks / plan / question / ledger / compaction 都是同一个 run cell 的 parts
  advanced/admin 控制进入 inspector 或 command escape hatch
```

后续重构必须围绕 **Session Workbench** 进行，而不是继续在 `AgentChatSection` 里堆功能。

## 1. 管理观点

### 1.1 Chat 不是 Agent 管理页的附属功能

`AgentDetail` 适合承载配置、权限、知识、技能、审计、Office、工作流等管理面。但用户进入一个数字员工后的日常主动作是“交办、观察、确认、追问、查看产物、继续推进”。这应是一级工作台，而不是管理页里的一个普通 tab。

当前代码事实：

- `frontend/src/pages/AgentDetail.tsx:75` 定义了 15 个 agent detail tabs。
- `frontend/src/pages/AgentDetail.tsx:1854` 以后才在 `activeTab === 'chat'` 时挂载 `AgentChatSection`。
- 这导致 conversation 被管理面包住，用户第一眼看到的是“Agent 控制台”，不是“正在工作的 session”。

管理判断：

- Agent 管理面保留，但不再决定聊天体验的主信息架构。
- `Conversation & Tasks / Session Workbench` 应成为普通用户的默认工作面。
- 高级管理 tab 可以继续存在，但必须退到 manage/advanced 区域。

### 1.2 Session 是主对象，不是消息数组

CC/Codex 的体验核心不是“更漂亮的消息列表”，而是“一个可恢复、可分叉、可压缩、可审计的 thread/session”。Hive 当前已经有 transcript、T0、checkpoint、fork、resume health 等基础，但前端没有把 session 作为主对象呈现。

目标心智：

- 用户正在操作一个 session。
- session 有 title、run state、resume health、checkpoint、branch ancestry、active goal/team。
- 用户可以从任意关键边界继续、分叉、回滚、进入 team member session。

前端必须把这些从后端事实升级为可见产品事实。

### 1.3 Transcript 是主画布，runtime event 不是外挂卡片

当前 `AgentChatSection` 已经有 `RunDisclosureBlock` 和 `chatDisclosureReducer`：

- `frontend/src/pages/agent-detail/chatDisclosureReducer.ts`
- `frontend/src/pages/agent-detail/RunDisclosureBlock.tsx:133`
- `frontend/src/pages/agent-detail/AgentChatSection.tsx:1393`

这是正确底座，但当前 projection 仍像“聊天消息 + disclosure 卡片”。Codex 的模型更接近：

```text
UserTurnCell
ActiveRunCell
  reasoning summary
  tool step
  command execution
  hook/checkpoint/compact marker
  ask-user / plan approval
  final answer
AssistantFinalCell / finalized history cell
```

Codex 本地源码的测试里反复验证 `InsertHistoryCell`、`display_lines`、`active_cell_transcript_lines`、`CommandExecution` 等统一 history/cell 表达。参考：

- `/Users/example-owner/Context Engineering/codex/codex-rs/tui/src/chatwidget/tests/status_and_layout.rs:2738`
- `/Users/example-owner/Context Engineering/codex/codex-rs/tui/src/chatwidget/tests/status_and_layout.rs:3355`
- `/Users/example-owner/Context Engineering/codex/codex-rs/tui/src/chatwidget/tests/status_and_layout.rs:3504`
- `/Users/example-owner/Context Engineering/codex/codex-rs/tui/src/chatwidget/tests/status_and_layout.rs:3537`

管理判断：

- runtime event 不应该各自拥有独立视觉主权。
- thinking/tool/permission/question/plan/compact/ledger 都应先进入统一 `ThreadTimeline` view model，再由一个 run cell 渲染。
- raw/debug detail 只进入 disclosure 或 inspector。

### 1.4 命令存在，但不能抢主界面

当前已经完成第一刀：普通聊天界面不再常驻 raw command bar，输入 `/` 才显示 slash command menu。这个方向是对的。

Codex slash 命令本质是 composer 行为，不是页面模块。参考：

- `/Users/example-owner/Context Engineering/codex/codex-rs/tui/src/slash_command.rs:7`
- `/Users/example-owner/Context Engineering/codex/codex-rs/tui/src/slash_command.rs:249`

管理判断：

- `CommandPalette` 保留为 advanced/debug escape hatch。
- 常用 session-native 能力不能要求用户写 JSON。
- Goal、Plan、Checkpoint、Team、Fork、Resume 需要产品化控件或 slash guided flow。

### 1.5 Work Ledger 是 run/session 的状态，不是底部外挂文档

当前 `ChatWorkLedgerDock` 已经比早期好：可折叠、能显示 session/runtime ledger。代码位置：

- `frontend/src/pages/agent-detail/AgentChatSection.tsx:2058`

但它仍是 transcript 下方的 dock。用户会感到“这是一块拼上去的工作日志”。

目标：

- active run 内的 todo/finding 变化进入 run cell summary。
- session 级 Work Ledger 进入右侧 inspector / session context。
- 只有用户主动展开时，才显示完整条目。

### 1.6 Team 是可进入的成员会话，不是高级版 subagent 卡片

产品判断已经明确：

- Sub-agent 偏一次性委派，用户不能自然进入其中持续对话。
- Team member 是独立 session，可以进入、追问、切换、最后 consolidate 回主 session。

因此 Team UX 不是 command result，而是 session topology：

```text
Lead Session
  Team panel
    Member A running  [Enter]
    Member B waiting  [Enter]
    Member C done     [Enter]

Member Session
  Header: Team / Member / Role
  用户可以直接对话
  Back to lead session

Close Team
  Consolidation preview
  Merge summary into lead transcript
```

这条线属于单 Agent session workbench，不进入跨组织 A2A 控制面。

## 2. 为什么现在会显得拼接

### 2.1 一个 turn 被拆到了太多组件

当前同一次 agent turn 可能被拆到这些位置：

- transcript message list
- `RunDisclosureBlock`
- `StructuredToolResultBody`
- `ChatWorkLedgerDock`
- `BranchLineagePanel`
- `BranchComposePanel`
- attachment strip
- slash command menu
- active-run polling state
- runtime summary query
- artifact preview

代码事实：

- `AgentChatSection.tsx:1539` 渲染 `BranchLineagePanel`。
- `AgentChatSection.tsx:2058` 渲染 `ChatWorkLedgerDock`。
- `AgentChatSection.tsx:2066` 渲染 `SlashCommandMenu`。
- `AgentChatSection.tsx:1393` 由 `renderConversationMessages(...)` 把 disclosure message group 成 `RunDisclosureBlock`。

这些组件都合理，但缺少统一 presentation model，所以最终视觉像组合件。

### 2.2 多条数据流同时驱动同一块 UI

当前会话体验同时依赖：

- WebSocket runtime event
- transcript replay
- legacy message fallback
- active run polling
- runtime summary polling
- work ledger polling
- branch/session lineage API
- artifact/file preview API

这解释了“工具调用时一闪、prompt 消失、状态跳动”的体感。上一轮已经修了 pending user message 和 stale active-run 误清理，但只解决了具体 bug；一体感问题仍然需要把这些数据流 reduce 成一个 `ThreadTimeline`。

### 2.3 管理后台视觉语言压过会话语言

当前页面有大 header、多 tab、左侧 agent list、chat 内 session list、底部 dock。它适合“管理一个员工”，不适合“和一个员工连续工作”。

CC/Codex 的会话主界面更克制：

- 主视觉只有 thread。
- 运行过程是 thread 内的状态。
- command 是 composer 行为。
- session metadata 是 header/inspector。
- debug/admin 信息默认不抢主界面。

## 3. 目标信息架构

### 3.1 新增 Session Workbench Shell

建议后续代码中引入独立边界：

```text
frontend/src/pages/session-workbench/
  SessionWorkbench.tsx
  SessionHeader.tsx
  SessionSidebar.tsx
  ThreadTimeline.tsx
  ThreadCells.tsx
  ActiveRunCell.tsx
  Composer.tsx
  SessionInspector.tsx
  TeamSessionPanel.tsx
  timelineModel.ts
  timelineModel.test.ts
```

`AgentDetail` 可以继续提供入口，但不应继续承载主要 conversation layout。

推荐路由：

```text
/agents/:agentId/chat/:sessionId?
/sessions/:sessionId
```

短期可以先从 `/agents/:id#chat` 挂载新 shell，保留旧 URL 兼容；中期应把 session route 升为一等路由。

### 3.2 三个主要区域

```text
┌──────────────────────┬──────────────────────────────────────┬──────────────────────┐
│ Session sidebar      │ Thread timeline                      │ Session inspector    │
│                      │                                      │                      │
│ sessions             │ UserTurnCell                         │ resume health        │
│ active badges        │ ActiveRunCell                        │ checkpoints          │
│ branch lineage       │ AssistantFinalCell                   │ work ledger          │
│ team members         │ compact/checkpoint separators        │ artifacts/sources    │
└──────────────────────┴──────────────────────────────────────┴──────────────────────┘
```

底部只有一个 composer。slash menu、attachment chips、stop/send、Plan Mode toggle 都属于 composer，不是页面额外条带。

### 3.3 Session Header 成为控制中心

Header 最低需要展示：

- session title
- run state
- model/provider
- resume health
- checkpoint count / latest checkpoint
- branch ancestry
- active goal
- active team
- compact/context status

这些不是装饰项，是用户理解 session 是否可靠、是否可恢复、是否正在继续工作的关键状态。

### 3.4 Session Inspector 承载高级细节

右侧 inspector 承载：

- Work Ledger 完整条目
- checkpoint list / rollback preview
- T0 segment / transcript replay health
- branch tree
- artifacts / files / sources
- tool raw args/result
- debug trace id

默认主线程只展示人能读懂的摘要，不展示 raw JSON。

## 4. 统一展示模型

### 4.1 ThreadTimeline

所有前端数据先进入一个稳定模型：

```ts
type ThreadTimelineCell =
  | UserTurnCell
  | ActiveRunCell
  | AssistantFinalCell
  | BoundaryCell
  | QuestionCell
  | PlanApprovalCell
  | ArtifactCell;
```

输入源：

- transcript events
- active run snapshot
- pending user messages
- runtime events
- work ledger summary
- checkpoint/session index
- branch/team topology

输出：

- 一个连续 timeline。
- 一个 active run cell。
- 一个 session inspector state。

### 4.2 ActiveRunCell

`ActiveRunCell` 是一体感的核心。

它应该包含：

- processing / processed / waiting header
- reasoning summary
- tool steps
- permission/approval steps
- question/plan blocking step
- compaction/checkpoint markers
- ledger summary
- artifact output chips
- final answer handoff

显示策略：

- running / blocked 默认展开。
- completed 默认折叠成 compact summary。
- error 立即显著展示。
- raw args/result 只在展开或 inspector 中出现。

### 4.3 Composer

Composer 是唯一输入面：

- textarea
- attachment
- Plan Mode toggle
- slash command menu
- access/model compact control
- stop/send

行为要求：

- 输入 `/` 才出现 command menu。
- tool call / active run 更新不得清空正在输入的 prompt。
- active run 时 stop 明确，send/queue 行为明确。
- IME composition、Shift+Enter、upload progress 都稳定。

## 5. 实现轨道

### Track A — 文档与架构冻结

本文档是 Track A 的第一产物。后续实现前需要避免再分散出多份互相打架的前端会话文档。

验收：

- `docs/README.md` 指向本文档。
- 后续 PR / commit 引用本文档作为 Session Workbench truth surface。

### Track B — Timeline Model First

先做纯函数和测试，再动视觉组件。

新增：

- `timelineModel.ts`
- `timelineModel.test.ts`

测试用例：

- 普通 user -> assistant final。
- running tool call 原地更新 active run cell。
- failed tool call 进入 error step。
- AskUserQuestion 是 blocking step。
- Plan approval 是 blocking step。
- compaction 是 boundary/step，不是 assistant raw message。
- Work Ledger summary 进入 active run cell，完整 ledger 进入 inspector。
- pending user message 不被 active-run refetch 清掉。

### Track C — Session Workbench Shell

新增 shell，并从旧 `AgentChatSection` 迁移能力：

- session sidebar
- session header
- thread timeline
- composer
- inspector

旧组件可被包裹/复用，但不能继续决定页面结构。

### Track D — Session-Native Controls

从 raw command/JSON 迁出：

- Goal
- Plan Mode / Advanced Plan
- Checkpoint / rollback
- Fork / branch
- Team create / enter / close / consolidate

Command Palette 只保留 advanced/debug。

### Track E — Team Conversation UX

实现：

- lead session team panel
- member session entry
- member header/breadcrumb
- close team consolidation preview
- consolidate result 写回 lead transcript

### Track F — Visual And Performance Polish

最后才做视觉打磨：

- 去掉 card stack 观感。
- 减少嵌套边框。
- 固定 composer 高度和状态区域。
- run cell 原地更新，减少整块重排。
- inspector/drawer 响应式。

## 6. 验收标准

只有全部满足，才能称为 CC/Codex-grade 会话体验：

1. 用户进入会话后看到的是 session/thread，不是管理后台 tab。
2. 一个 agent turn 的 thinking/tool/plan/question/final answer 是一个连续 active run cell。
3. 工具调用不会造成 prompt 闪烁或输入丢失。
4. slash command 是 composer 行为，不是常驻页面模块。
5. Goal、Plan、Checkpoint、Team 不要求普通用户写 JSON。
6. Work Ledger 默认不抢主界面，但可在 run summary 或 inspector 中可靠查看。
7. Team member session 可以进入、直接对话、返回、关闭并 consolidate。
8. resume health、checkpoint、fork lineage 是 session 可见状态。
9. 刷新页面后 transcript replay 能恢复同一可见 timeline。
10. debug/raw/provider payload 不默认暴露。
11. desktop/tablet/mobile 都没有明显布局断点。
12. 现有 runtime 能力和治理边界不被绕开。

## 7. 验证命令

文档改动本身只需要格式校验：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main
git diff --check -- docs/frontend-session-workbench-cc-codex-parity-gap-2026-06-23.md docs/README.md
```

后续实现必须按 TDD：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm test -- --run \
  src/pages/session-workbench/timelineModel.test.ts \
  src/pages/agent-detail/chatRuntime.test.ts \
  src/pages/agent-detail/chatDisclosureReducer.test.ts \
  src/pages/agent-detail/AgentDetailSections.test.tsx \
  src/api/domains/chat.test.ts \
  src/api/domains/ccParity.test.ts
npm run build
```

浏览器验收场景：

- empty session
- completed assistant turn
- running tool call
- failed tool call
- Plan Mode request/confirmation
- AskUserQuestion blocking card
- compaction boundary
- checkpoint/rollback list
- branch/fork lineage
- active goal
- team member session switch
- active run 中输入 prompt 不丢失
- narrow/mobile viewport

## 8. 不做什么

- 不把 Codex 的 Git/worktree 环境控件搬进默认 UI，除非 Hive 有真实数据和使用场景。
- 不把所有高级能力塞进常驻按钮区。
- 不把 Work Ledger、Team、Goal 当作 command result 卡片长期挂在 transcript 外。
- 不用默认关闭 feature flag 掩盖半成品。
- 不用 mock state 或 prototype fake data 伪造能力。
- 不让前端为了好看绕过 runtime、tool、memory、approval governance。

## 9. 下一步

下一轮代码重构的第一刀必须是 **Timeline Model First**：

1. 新增 `session-workbench/timelineModel.ts`。
2. 写 `timelineModel.test.ts` 覆盖 transcript、active run、tool、question、plan、compaction、ledger、pending prompt。
3. 让旧 `AgentChatSection` 可以先消费这个 model 的一部分。
4. 再替换 shell、header、timeline、composer、inspector。

如果继续先改视觉组件，会再次把问题做成局部补丁，仍然达不到 CC/Codex 的一体感。

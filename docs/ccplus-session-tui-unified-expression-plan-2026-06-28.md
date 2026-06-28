# CCPlus Session TUI 统一表达方案

日期：2026-06-28

状态：前端 / Web TUI 升级方案。本文承接 `ccplus-session-checkpoint-branch-ui-upgrade-plan-2026-06-27.md`、`ccplus-unclosed-gap-register-2026-06-27.md`、`ccplus-freecode-00-08-terminal-audit-2026-06-24.md`、`ccplus-tool-call-closure-audit-2026-06-28.md`、`ccplus-governance-truth-search-repair-plan-2026-06-28.md`。

## 0. 结论

不能把两件事混在一起说：

1. **后端工具 / governance / runtime 闭环**：当前已有大量已关闭项和测试证据。`/compact`、`/rewind` next-turn context consumption、typed session command result、hidden command 裁决、workspace rewind snapshot、Hook external runner、SkillTool/frontmatter hooks、Sub-agent / Agent Team、Dynamic Workflow、Background completion wake 等已经有 code-level 闭环。
2. **Web 端 TUI 统一表达**：尚未完工。当前前端已有 `SessionWorkbenchHeader`、`SessionWorkbenchInspector`、slash command menu、permission mode menu、基础 checkpoint panel、branch lineage、native controls、compaction inline event、workflow card，但它们仍是分散表达，不是一个完整的 Session-native TUI shell。

因此，准确说法是：

```text
底层能力基本完成并可观察；但用户端 Session TUI 还没有统一。
下一步不是补一个弹窗，也不是重做左侧导航栏，而是把所有 session 状态统一收敛到现有左侧导航右边的 Session Shell。
```

## 1. CC / Codex / Hive 表达差异

### 1.1 CC / FreeCode 决定语义

CC 的价值不在视觉样式，而在 session/runtime 语义：

| 语义 | CC 表达 | Hive 应 follow |
| --- | --- | --- |
| slash command | `/command args`，命令由 registry 管理 | Web composer 继续采用 `/command `，但结果必须进入 session event/control，不输出 raw JSON |
| compact | compact boundary message，`compactMetadata` 记录压缩边界 | UI 显示 compact marker、context replacement、token/manifest 信息 |
| status line | model、permission、cwd、usage 等状态靠近输入区 | Web 应把 model/run/permission/context 放入 Session Header + composer footer |
| permission | 权限模式和工具审批是 session runtime 的一部分 | “Full access” 只能表示 bypass session prompts，仍受 enterprise rules |
| rewind / branch | rewind 是当前 session projection；branch/fork 是新工作线 | Web 需要明确区分 active projection 与 new session line |
| commands / agents / hooks | 命令、agent、hooks 都进入同一 runtime lifecycle | Web 不能把 Team/Subagent/Workflow/Hooks 分散成孤岛页 |

参考证据：

- `/Users/rocky243/vc-saas/free-code-main/src/commands.ts`
- `/Users/rocky243/vc-saas/free-code-main/src/components/messages/CompactBoundaryMessage.tsx`
- `/Users/rocky243/vc-saas/free-code-main/src/components/StatusLine.tsx`

### 1.2 Codex 值得吸收的是 TUI 工程表达

Codex 的 TUI 表达比 CC 更完整，尤其适合 Web 借鉴：

| Codex TUI / protocol | 表达价值 | Hive Web 对应 |
| --- | --- | --- |
| bottom pane command popup | `/` 命令是 composer-native overlay | 保留 slash menu，但命令结果进入 center timeline / 右侧 Runtime Tables |
| approval overlay | 权限请求不是普通消息，是 blocking overlay | 权限请求进入右侧 Governance tab + inline card，可 allow once/session/deny |
| footer active agent label | 当前 active agent/thread 在输入区附近可见 | Session lane tabs / composer footer 显示 active lane |
| app backtrack / rollback | 回退是 thread/session 操作，不是普通助手回复 | Checkpoint rail + branch graph 一体化 |
| ThreadFork / ThreadRollback / Resume protocol | thread 形状是可分叉、可回退、可 resume 的对象 | Hive `ChatSession` family graph 需要可视化 |
| CollabAgentTool | spawn/send/resume/wait/close 是协作工具生命周期 | Agent Team/Subagent lanes 需要显示状态和 enter/resume/close |
| permission profile response | approval scope 与 thread/turn/tool call 绑定 | Hive permission card 必须带 session/turn/tool/evidence refs |
| hooks browser | hook 生命周期可枚举、可诊断 | 右侧 Governance tab 需要 Hooks section |

参考证据：

- `/Users/rocky243/Context Engineering/codex/codex-rs/tui/src/bottom_pane/snapshots/codex_tui__bottom_pane__command_popup__tests__command_popup_default_items.snap`
- `/Users/rocky243/Context Engineering/codex/codex-rs/tui/src/bottom_pane/snapshots/codex_tui__bottom_pane__approval_overlay__tests__approval_overlay_permissions_prompt.snap`
- `/Users/rocky243/Context Engineering/codex/codex-rs/tui/src/bottom_pane/snapshots/codex_tui__bottom_pane__footer__tests__footer_active_agent_label.snap`
- `/Users/rocky243/Context Engineering/codex/codex-rs/app-server-protocol/schema/typescript/v2/ThreadForkResponse.ts`
- `/Users/rocky243/Context Engineering/codex/codex-rs/app-server-protocol/schema/typescript/v2/ThreadRollbackResponse.ts`
- `/Users/rocky243/Context Engineering/codex/codex-rs/app-server-protocol/schema/typescript/v2/CollabAgentTool.ts`
- `/Users/rocky243/Context Engineering/codex/codex-rs/app-server-protocol/schema/typescript/v2/PermissionsRequestApprovalParams.ts`

### 1.3 Hive 当前前端状态

当前前端已经有基础材料：

| 当前能力 | 代码入口 | 当前问题 |
| --- | --- | --- |
| Header chips | `frontend/src/pages/session-workbench/SessionWorkbenchChrome.tsx` | 有 resume/checkpoint/branch/compaction/run，但缺 permission/governance/context projection |
| Right inspector | `SessionWorkbenchInspector` | 是固定窄列，不是 Workspace Documents + Runtime Tables 上下分区 |
| Native controls | `SessionNativeControls` | Hook/team/goal/plan/export/checkpoint 被堆在一个长列表里 |
| Slash menu | `frontend/src/pages/agent-detail/SlashCommandMenu.tsx` + `slashCommand.ts` | 有 `/command ` 输入，但命令类别、结果落点和右侧 Runtime Tables 未统一 |
| Permission modes | `AgentChatSection.tsx` `SESSION_PERMISSION_MODE_OPTIONS` | 已有 Full access/Ask first/Approve for me，但只在 composer menu，不在 Session governance header |
| Checkpoint panel | `SessionCommandControlPanel` | 点击 checkpoint 直接 rewind，没有操作菜单/branch/context/files |
| Branch lineage | `BranchLineagePanel` | 还不是 checkpoint graph |
| Compaction event | `session_compact` inline render | 有事件卡，但没有右侧 Context/Governance detail 中的 active projection 详情 |
| Team enter | `SessionNativeControls.enterMember` | 能 enter child session，但不是 session lane/tabs |
| Workflow card | chat inline dynamic workflow proposal | 可观察 proposal，但运行图和 journal 应进右侧 Workflow tab |

## 2. 目标布局：左侧导航栏冻结，只重构中间和右侧

Web 端不需要像终端一样逐像素复刻，但需要复刻 TUI 的信息架构。

```text
┌────────────────────────┬──────────────────────────────────────────┬──────────────────────────────┐
│ Existing Left Nav      │ Center Session Timeline                  │ Right Workspace / Runtime    │
│                        │                                          │                              │
│ KEEP AS IS             │ Session Header                           │ Workspace Documents          │
│ agent/session list     │ Git-line checkpoint conversation flow     │ document status / preview     │
│ current product nav    │ Transcript / Run cells                   ├──────────────────────────────┤
│ no structural changes  │ Agent Team / Subagent lane tabs          │ Runtime Tables                │
│                        │ Composer + slash menu + footer status    │ tasks / background / workflow │
│                        │                                          │ notifications / commands      │
└────────────────────────┴──────────────────────────────────────────┴──────────────────────────────┘
```

### 2.1 左侧导航栏：冻结

左侧导航栏保持当前产品结构，不纳入本轮 TUI 重构范围。

明确不做：

- 不重做 workspace / agent / session 列表。
- 不把 branch graph、Agent Team、Subagent、Background Agent 挪到左侧。
- 不修改左侧导航的信息架构、宽度策略、折叠策略。
- 不把新的 TUI 状态塞进左侧列表。

左侧只承担已有入口职责：选择 agent、选择 session、进入当前产品导航。新的 session 内状态全部放在中间主区和右侧面板。

### 2.2 中间：Session Timeline

中栏是主工作面。

结构：

1. `SessionWorkbenchHeader`
   - title
   - model/provider
   - run status
   - permission mode
   - context state
   - checkpoint count
   - branch count
   - compaction count
   - active projection state

2. Session lane tabs，放在对话流底部 / 输入框上方
   - `Main`
   - `Team: <name>`
   - `Subagent: <name>`
   - `Workflow: <run>`
   - `Background: <count>`

3. Git-line checkpoint conversation flow
   - 每个 user prompt 是 primary checkpoint node。
   - compact / permission / plan approval / branch anchor 是 marker。
   - node 状态：`past`、`current_head`、`rewound_tail`、`new_tail`、`branch_anchor`、`compacted_scope`。
   - 这条线长在对话流本身，不放到左侧导航，也不作为右侧列表。

4. Transcript / run cells
   - 用户消息。
   - assistant final。
   - active run cell。
   - tool call group summary。
   - permission required card。
   - compact marker。
   - workflow proposal/run marker。
   - subagent/team event marker。

5. Composer
   - `/` command popup。
   - permission mode footer。
   - active lane label。
   - attachment/artifact chips。

### 2.3 右侧：Workspace / Runtime 上下分区

右侧不做泛化万能面板，而是分成上下两个固定区域。桌面下固定显示；窄屏时可以折叠成 overlay。

上半区：Workspace Documents。

| 内容 | 说明 |
| --- | --- |
| 文档列表 | 当前 workspace 中的文档、文件、artifact |
| 文档状态 | 是否生成中、已更新、可预览、需要确认 |
| 文档预览入口 | 点击文档后在右侧区域或浮层打开预览，不占用左侧导航 |

下半区：Runtime Tables。

| Tab | 内容 |
| --- | --- |
| Tasks | 当前 todo / work ledger / task table |
| Background | background agent、long task、completion wake |
| Workflow | dynamic proposal、definition、step graph、journal、gate/wait/resume/repair/promote |
| Notifications | 后台任务完成、失败、需要用户处理的通知 |
| Commands | 独立运行命令、最近 command result、command schema |
| Runs | active RuntimeTask、tool rounds、tool calls、terminal status、killed/cancelled/failure reason |
| Governance | L0/L1/L2/L3、permission profile、pending approvals、Truth Search evidence、decision trace |
| Raw | T0/source refs/InvocationSpan/export JSON，仅 debug/admin 默认展开 |

## 3. 治理表达

### 3.1 后台治理如何表达

后台治理应继续按 L0-L3 + hooks + preflight + Truth Search evidence 表达：

```text
L0 platform hard guard
  -> L1 enterprise / company policy
  -> L2 extensions and add-ons visibility
  -> L3 session permission
  -> hooks / preflight / sandbox / provider
  -> tool execution
  -> T0 / InvocationSpan / DecisionTrace
```

Truth Search 的位置：

- 不是 instruction。
- 不是权限 authority。
- 是 source-bound、ACL-filtered、可审计 evidence。
- 可以进入 preflight、permission prompt、decision trace、右侧 Context/Governance detail。

### 3.2 前端治理如何表达

Header：

- `Permission: Full access | Ask first | Approve for me`
- `Governance: clean | waiting | blocked | denied`
- `Context: normal | compacting | compacted | rewound`

右侧 Governance tab：

- 当前 permission profile。
- 本 turn 的 L0/L1/L2/L3 判定。
- pending approval cards。
- hook events：before/after、blocking/rewrite、failure。
- Truth Search evidence refs。
- decision trace / invocation span refs。

关键文案：

```text
Full access = Bypass session prompts, still obey enterprise rules.
```

不能把 Full access 表达成“平台治理完全关闭”。

## 4. 命令表达

### 4.1 用户如何输入命令

采用 Codex/CC 共同的低摩擦形式：

```text
/command natural language args
```

例子：

```text
/compact 保留当前实现结论和未完成事项
/rewind
/branch 从第 6 个 checkpoint 开一条新线
/plan 重构右侧 Workspace / Runtime 面板，不改后端 contract
/workflow triage inbound leads
/agent critic: review this plan
/skill frontend-session-tui
```

### 4.2 命令类别

| 类别 | 用户可见命令 | 落点 |
| --- | --- | --- |
| Session | `/compact` `/rewind` `/branch` `/clear` `/resume` | 中间 timeline marker + 右侧 Commands / Runs tab |
| Planning | `/plan` `/goal` | inline plan card + 右侧 Runs / Commands tab |
| Collaboration | `/agent` `/task delegate` `/team` | 输入框上方 lane tabs + 右侧 Background / Runs tab |
| Automation | `/workflow` `/schedule` | 中间 workflow marker + 右侧 Workflow tab |
| Capability | `/skill` `/tools` | 中间 event marker + 右侧 Commands / Governance tab |
| Debug/Admin | `/export` `/status` `/hooks` | 右侧 Raw / Governance tab，默认不进普通消息流 |

### 4.3 命令结果如何呈现

原则：

- 不输出 assistant raw JSON。
- 不只 toast。
- 不只弹窗。
- 每个命令结果都变成 session event/control cell，并同步到右侧 Workspace / Runtime 面板。

结果形态：

| `ui_action` | 中间主区 | 右侧面板 |
| --- | --- | --- |
| `open_checkpoint_selector` | checkpoint rail 高亮，节点菜单打开 | Context / Commands tab 显示 checkpoint list |
| `install_compacted_context` | compact marker | Context tab 显示 summary/token/manifest |
| `install_active_projection` | rewind marker + tail dimmed | Context tab 显示 active head |
| `switch_session` | 切换 session line | 保持现有左侧导航行为，不新增左侧结构 |
| `open_permissions_menu` | permission card | Governance tab |
| `open_context_panel` | context marker | Context tab |
| `open_usage_panel` | usage chip | Context / Runs tab |

## 5. Checkpoint / Rewind / Branch / Compact

### 5.1 Checkpoint

Checkpoint 是 user prompt node，不是每个工具调用 node。

点击 checkpoint 后打开菜单：

- 回到这里。
- 从这里创建分支。
- 查看此处上下文。
- 查看此处文件变化 / workspace snapshot。
- 复制该轮输入。

当前 `SessionCommandControlPanel` 直接触发 rewind，需要升级为 checkpoint node menu。

### 5.2 Rewind

Rewind 不创建新 session。它更新当前 session 的 active projection。

UI 必须显示：

- active head 指向哪个 checkpoint。
- 哪些旧 tail 已被排除出当前 context。
- rewind 后的新 tail 从哪里开始。

### 5.3 Branch

Branch 创建新 `ChatSession.id`。

UI 必须显示：

- branch graph。
- anchor checkpoint。
- parent session。
- 当前 branch。
- 切换 branch 等价于切换 session。

### 5.4 Compact

Compact 不创建新 session。

UI 必须显示：

- manual compact / auto compact 的 marker。
- compact summary。
- compact 前后 token/context。
- active projection 是否生效。
- compact 后保留的 tail。

Codex 在压缩状态表达上更好，Hive 应采用更显式的右侧 Context/Governance detail，而不是只在消息里显示 “Context was compressed”。

## 6. Agent Team / Sub-agent / Background Agent

### 6.1 统一为 Session Lanes

不要把 Agent Team、Sub-agent、Background Agent 分散到独立页面。它们在当前 session 内应表现为 lanes：

```text
Main | Team: Review crew | Subagent: Critic | Workflow: Release checks | Background: 3
```

每个 lane 有状态：

- running
- waiting_for_permission
- waiting_for_user
- blocked_by_hook
- completed
- failed
- cancelled

### 6.2 Agent Team

位置：

- 输入框上方的 session lane tabs 显示 team/member 标签。
- 中栏点击 lane tab 切换到 team/member session 或 filtered channel。
- 右侧 Runtime Tables 显示 team/member graph、mailbox、events、enter/resume/wait/close。

当前 `SessionNativeControls.enterMember` 已能进入 member session，下一步要把它升级为 session lane 交互，而不是藏在右侧长列表。

### 6.3 Sub-agent

Sub-agent 应显示：

- subagent type / role。
- child session id。
- tool profile / allowed tools。
- running/completed/failed。
- completion wake。
- parent timeline 投影。

用户点击 subagent marker：

- 中栏切换 child lane 或打开 filtered transcript。
- 右侧 Runtime Tables 的 Background / Runs tab 展示详情。

### 6.4 Background Agent

Background completion wake 已进入 Workbench read model。UI 应表达为：

- Header / lane tab 显示 pending/running/completed/failed count。
- 输入框上方 lane tab 显示 background count。
- 右侧 Runtime Tables 的 Runs / Background tab 显示 run detail。
- 完成后在 parent session timeline 插入 completion marker。

## 7. Dynamic Workflow

Dynamic Workflow 不应该只是一张 inline card。

推荐表达：

- 中栏只显示 proposal/start/step-complete/final marker。
- 右侧 Runtime Tables 的 Workflow tab 显示：
  - proposal candidate
  - exact artifact/hash
  - step graph
  - journal
  - gate/wait/resume
  - repair action
  - promote suggestion

点击 workflow marker 默认打开右侧 Runtime Tables 的 Workflow tab。

## 8. 思考、工具调用与交付物暴露边界

### 8.1 显示什么

默认显示：

- 用户输入。
- assistant final。
- run summary，例如 “13 tool calls, 7 messages”。
- tool group summary。
- user-relevant artifacts。
- permission/governance/blocker。
- compact/rewind/branch/session state。
- workflow/team/subagent status。

### 8.2 不显示什么

默认不显示：

- raw hidden chain-of-thought。
- provider raw payload。
- raw tool JSON。
- long logs。
- raw T0 / InvocationSpan。

这些只能进入 Raw / Debug tab，并受权限控制。

### 8.3 交付物如何给到用户

Workspace Documents 是统一交付面：

- 生成文件。
- 当前文档。
- workspace snapshot。
- diff/changes。
- export JSON。
- workflow artifact。
- evidence/source refs。

当前文档不应再以 modal 为主。桌面下应该在右侧 Workspace Documents 展示；窄屏可用 overlay panel。

## 9. 需要改的前端代码面

### 9.1 新增 / 扩展模型

目标文件：

- `frontend/src/pages/session-workbench/timelineModel.ts`

新增模型：

```ts
type SessionLaneKind = 'main' | 'team_member' | 'subagent' | 'workflow' | 'background';

interface SessionLaneModel {
  id: string;
  kind: SessionLaneKind;
  label: string;
  status: 'running' | 'waiting' | 'blocked' | 'completed' | 'failed' | 'cancelled';
  sessionId?: string;
  runtimeTaskId?: string;
}

interface CheckpointTimelineNode {
  id: string;
  sequence: number;
  label: string;
  state: 'past' | 'current_head' | 'rewound_tail' | 'new_tail' | 'branch_anchor' | 'compacted_scope';
  checkpointEventId: string;
  branchSessionIds: string[];
  compacted?: boolean;
}

interface SessionRightPanelModel {
  workspaceDocuments: unknown;
  runtimeTables: unknown;
  context: unknown;
  governance: unknown;
  commands: unknown;
  runs: unknown;
  team: unknown;
  subagents: unknown;
  workflow: unknown;
  artifacts: unknown;
  raw: unknown;
}
```

### 9.2 升级组件

| 文件 | 改动 |
| --- | --- |
| `SessionWorkbenchChrome.tsx` | Header 增加 permission/context/governance chips；右侧改成 Workspace Documents + Runtime Tables 上下分区 |
| `SessionNativeControls.tsx` | 从“控件长列表”拆成 Runtime Tables tab content，不再全量堆叠 |
| `AgentChatSection.tsx` | 中栏引入 lane tabs、git-line checkpoint flow、right panel tab triggers；composer footer 显示 active lane + permission |
| `SlashCommandMenu.tsx` | 按命令类别分组；隐藏 internal-only；支持 command detail preview |
| `sessionCommandResult.ts` | 所有 `ui_action` 映射到 right panel action + timeline marker |
| `chatDisclosureReducer.ts` | 将 tool/team/workflow/compaction/subagent summaries 与 right panel detail 建立稳定 key |
| `AgentDetail.tsx` | session switch、branch switch、team member enter、right panel state 统一管理 |

### 9.3 测试

先写测试，再实现：

```bash
cd frontend
npm test -- --run \
  src/pages/session-workbench/timelineModel.test.ts \
  src/pages/agent-detail/AgentDetailSections.test.tsx \
  src/pages/agent-detail/SlashCommandMenu.test.tsx \
  src/pages/agent-detail/sessionCommandResult.test.ts \
  src/pages/agent-detail/chatDisclosureReducer.test.ts
```

新增测试点：

- Header 显示 permission/context/governance chips。
- Checkpoint node 点击先打开菜单，不直接 rewind。
- Rewind 后旧 tail 标为 excluded from context。
- Compact marker 可打开右侧 Context/Governance detail。
- `/command ` 选择后进入 composer，执行结果不追加 raw JSON。
- Agent Team member 显示为输入框上方 lane，点击 enter 切换 session。
- Subagent / background wake 显示在 lane 和 Runtime Tables。
- Workflow marker 打开右侧 Runtime Tables 的 Workflow tab。
- Governance tab 显示 permission profile / pending request / Truth Search evidence refs。
- Raw tab 默认不展开 provider/tool JSON。

构建验证：

```bash
cd frontend && npm run build
```

## 10. 落地顺序

一次性目标不是“全部视觉重做”，而是所有能力进入同一个 Session Shell，不再散落。

1. **模型层**：扩展 `timelineModel.ts`，形成 lanes、checkpoint timeline、right panel model。
2. **布局层**：冻结左侧导航栏，只重构中间 Session Timeline 与右侧 Workspace / Runtime 上下分区。
3. **命令层**：slash command category + typed `ui_action` -> timeline marker + right panel action。
4. **治理层**：permission mode、pending approvals、L0-L3、Truth Search evidence 进入右侧 Governance tab。
5. **状态层**：compact/rewind/branch/clear/resume 进入 git-line checkpoint flow + 右侧 Context / Runs tab。
6. **协作层**：Agent Team/Subagent/Background/Workflow 统一成 session lanes。
7. **交付层**：Workspace Documents 替代文档/文件弹窗主路径。
8. **验收层**：前端单测 + build；后端不改 contract 时不跑全量 backend，若补 API contract 再按对应后端测试补齐。

## 11. 完成口径

只有满足以下条件，才能说 Web/TUI 统一表达完成：

- 用户能在一个 Session 内看到当前工作线、分支、checkpoint、active head、compact 状态。
- 用户能用同一个 composer 输入 `/command args`，并看到命令结果进入 session 状态，而不是 raw JSON。
- Permission / Full access / Ask first / Approve for me 在 Header、composer footer、右侧 Governance tab 三处一致。
- Agent Team、Sub-agent、Background Agent、Dynamic Workflow 都是 session lanes，不再只藏在独立 tab 或弹窗里。
- 工具调用默认显示 summary；Raw 细节只在 Raw tab。
- 当前文档、文件、snapshot、artifact 在右侧 Workspace Documents 可查看，不依赖 modal 作为主交互。
- Rewind 和 Branch 视觉上明确不同：rewind 改当前 head，branch 产生新 session line。
- Compact 显示为 context lifecycle，不只是普通系统消息。
- 所有 UI 状态都能从 `SessionWorkbenchV1` / session index / runtime events 重建，可 replay、可 export、可测试。

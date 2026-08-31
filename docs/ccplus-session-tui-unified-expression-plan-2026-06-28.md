# CCPlus Session TUI 统一表达方案

日期：2026-06-28

状态：前端 / Web TUI 升级唯一主方案。本文承接 `ccplus-session-checkpoint-branch-ui-upgrade-plan-2026-06-27.md`、`ccplus-unclosed-gap-register-2026-06-27.md`、`ccplus-freecode-00-08-terminal-audit-2026-06-24.md`、`ccplus-tool-call-closure-audit-2026-06-28.md`、`ccplus-governance-truth-search-repair-plan-2026-06-28.md`。后续 Session TUI、checkpoint、rewind、branch、Agent Team、Dynamic Workflow 的产品裁决必须收敛到本文；旧 checkpoint/branch 文档只作为历史细节附录，不再承载并行方案。

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

- `/Users/example-owner/vc-saas/free-code-main/src/commands.ts`
- `/Users/example-owner/vc-saas/free-code-main/src/components/messages/CompactBoundaryMessage.tsx`
- `/Users/example-owner/vc-saas/free-code-main/src/components/StatusLine.tsx`

### 1.1.1 CC checkpoint / rewind / branch 源码核验结论

本节以本机 FreeCode/CC 源码为准，前端不得按 UI 直觉重新定义后端语义。

| CC 入口 | 后端 / 运行时语义 | Web TUI 对齐 |
| --- | --- | --- |
| `/branch` slash command | 创建新的 fork session id，读取当前 transcript，复制所有 main conversation messages，重写 `sessionId` 和 `parentUuid` 链，写入 `forkedFrom`，复制 `content-replacement` 记录，然后 resume 到新 branch。这个入口是当前 head 分支，不是 checkpoint selector。 | `/branch` 无 checkpoint 参数时按 current-head branch；创建新 `ChatSession.id`，UI 切换到新 session line。 |
| MessageSelector rewind | 选择一条 user message，但恢复点是这条 user message 发送之前。实现使用 `messages.slice(0, messageIndex)`，再把这条 prompt 放回 composer；旧 tail 不物理删除。 | checkpoint 上的 `Rewind here` 必须是 before-boundary active projection；点击 checkpoint 本身只选中/定位，不执行 rewind。 |
| code rewind / `--rewind-files` | 只恢复文件快照。目标必须是 user message；可以 dry-run；standalone 模式不能同时带 prompt。它不是 session branch，也不是聊天上下文恢复。 | 文件/workspace restore 必须作为独立 workspace snapshot 操作展示，不和 session rewind 混成一个按钮。 |
| `--resume-session-at` | headless resume 截断到指定 message，代码使用 `slice(0, index + 1)`；CLI help 说明是 up to and including assistant message。它不是交互式 checkpoint click。 | Web checkpoint click 不等于 resume-at，也不等于 rewind；只是导航和 selected node。 |

源码证据：

- `/Users/example-owner/vc-saas/free-code-main/src/commands/branch/branch.ts:61`
- `/Users/example-owner/vc-saas/free-code-main/src/commands/branch/branch.ts:122`
- `/Users/example-owner/vc-saas/free-code-main/src/commands/branch/branch.ts:274`
- `/Users/example-owner/vc-saas/free-code-main/src/components/MessageSelector.tsx:328`
- `/Users/example-owner/vc-saas/free-code-main/src/screens/REPL.tsx:3659`
- `/Users/example-owner/vc-saas/free-code-main/src/screens/REPL.tsx:3674`
- `/Users/example-owner/vc-saas/free-code-main/src/screens/REPL.tsx:3715`
- `/Users/example-owner/vc-saas/free-code-main/src/cli/print.ts:573`
- `/Users/example-owner/vc-saas/free-code-main/src/cli/print.ts:736`
- `/Users/example-owner/vc-saas/free-code-main/src/cli/print.ts:4520`
- `/Users/example-owner/vc-saas/free-code-main/src/utils/fileHistory.ts:347`
- `/Users/example-owner/vc-saas/free-code-main/src/main.tsx:991`
- `/Users/example-owner/vc-saas/free-code-main/src/cli/print.ts:5105`

### 1.2 Codex 值得吸收的是 TUI 工程表达

Codex 的 TUI 表达比 CC 更完整，尤其适合 Web 借鉴：

| Codex TUI / protocol | 表达价值 | Hive Web 对应 |
| --- | --- | --- |
| bottom pane command popup | `/` 命令是 composer-native overlay | 保留 slash menu，但命令结果进入 center timeline / 右侧 Runtime Tables |
| approval overlay | 权限请求不是普通消息，是 blocking overlay | 权限请求进入右侧 Governance tab + inline card，可 allow once/session/deny |
| footer active agent label | 当前 active agent/thread 在输入区附近可见 | 右侧 Runtime Agents 切换 session；中间 active tab / composer footer 显示 active session |
| app backtrack / rollback | 回退是 thread/session 操作，不是普通助手回复 | Checkpoint rail + branch graph 一体化 |
| ThreadFork / ThreadRollback / Resume protocol | thread 形状是可分叉、可回退、可 resume 的对象 | Hive `ChatSession` family graph 需要可视化 |
| CollabAgentTool | spawn/send/resume/wait/close 是协作工具生命周期 | Agent Team/Subagent session windows 需要显示状态和 enter/resume/close |
| permission profile response | approval scope 与 thread/turn/tool call 绑定 | Hive permission card 必须带 session/turn/tool/evidence refs |
| hooks browser | hook 生命周期可枚举、可诊断 | 右侧 Governance tab 需要 Hooks section |

参考证据：

- `/Users/example-owner/Context Engineering/codex/codex-rs/tui/src/bottom_pane/snapshots/codex_tui__bottom_pane__command_popup__tests__command_popup_default_items.snap`
- `/Users/example-owner/Context Engineering/codex/codex-rs/tui/src/bottom_pane/snapshots/codex_tui__bottom_pane__approval_overlay__tests__approval_overlay_permissions_prompt.snap`
- `/Users/example-owner/Context Engineering/codex/codex-rs/tui/src/bottom_pane/snapshots/codex_tui__bottom_pane__footer__tests__footer_active_agent_label.snap`
- `/Users/example-owner/Context Engineering/codex/codex-rs/app-server-protocol/schema/typescript/v2/ThreadForkResponse.ts`
- `/Users/example-owner/Context Engineering/codex/codex-rs/app-server-protocol/schema/typescript/v2/ThreadRollbackResponse.ts`
- `/Users/example-owner/Context Engineering/codex/codex-rs/app-server-protocol/schema/typescript/v2/CollabAgentTool.ts`
- `/Users/example-owner/Context Engineering/codex/codex-rs/app-server-protocol/schema/typescript/v2/PermissionsRequestApprovalParams.ts`

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
| Team enter | `SessionNativeControls.enterMember` | 能 enter child session，但还没有形成右侧 Runtime Agents row -> 中间 session window 的统一表达 |
| Workflow card | chat inline dynamic workflow proposal | 可观察 proposal，但运行图和 journal 应进右侧 Workflow tab |

## 2. 目标布局：固定三栏，左右可缩放，中间自适应

Web 端不需要像终端一样逐像素复刻，但需要复刻 TUI 的信息架构。

```text
┌────────────────────────┬──────────────────────────────────────────┬──────────────────────────────┐
│ Existing Left Nav      │ Center Session Timeline                  │ Right Workspace / Runtime    │
│                        │                                          │                              │
│ KEEP AS IS             │ Session Header                           │ Workspace Documents          │
│ agent/session list     │ Git-line checkpoint conversation flow     │ document status / preview     │
│ current product nav    │ Transcript / Run cells                   ├──────────────────────────────┤
│ no structural changes  │ Selected session page / breadcrumb       │ Runtime Tables                │
│                        │ Composer + slash menu + footer status    │ agents / tasks / workflow     │
│                        │                                          │ notifications / commands      │
└────────────────────────┴──────────────────────────────────────────┴──────────────────────────────┘
```

### 2.0 三栏 shell 不再摇摆

当前最终布局固定为三栏：

- 左栏：现有产品导航 / agent-session 入口。
- 中栏：当前 active session 的 chat / timeline。
- 右栏：Workspace Documents + Runtime Tables。

三栏规则：

1. 左右两个侧栏都支持水平拖拽缩放。
2. 左右栏有最小宽度和最大宽度，不能挤压到不可用，也不能把中栏挤没。
3. 中间 chat 区域必须随左右栏宽度变化自适应，使用 `min-width: 0`、flex/grid track、overflow containment，而不是固定 `calc(100vw - x)`。
4. 拖拽侧栏时，中间 timeline、message bubble、git-line checkpoint、composer footer 都要稳定重排，不出现横向溢出或文本遮挡。
5. 左侧导航的内容和信息架构不改；本轮只允许在三栏 shell 层增加 resize 行为。
6. 右侧删除的是旧 `SessionWorkbenchInspector` 杂项列，不是取消右栏。右栏必须以新的 Workspace/Runtime 分区重建。

桌面布局建议：

```text
grid-template-columns:
  clamp(260px, var(--left-sidebar-width), 420px)
  minmax(520px, 1fr)
  clamp(320px, var(--right-sidebar-width), 560px)
```

窄屏规则：

- 左栏保持现有移动端/折叠行为。
- 右栏可折叠成 overlay / drawer。
- 中栏 chat 永远是主阅读与输入区，不被右栏覆盖。

### 2.1 左侧导航栏：冻结

左侧导航栏保持当前产品结构，不纳入本轮 TUI 重构范围。

明确不做：

- 不重做 workspace / agent / session 列表。
- 不把 branch graph、Agent Team、Subagent、Background Agent 挪到左侧。
- 不修改左侧导航的信息架构、层级、列表内容、折叠语义。
- 不把新的 TUI 状态塞进左侧列表。

左侧只承担已有入口职责：选择 agent、选择 session、进入当前产品导航。新的 session 内状态全部放在中间主区和右侧面板。左栏宽度可以在三栏 shell 层拖拽调整，但这不等于重做左侧导航。

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

2. Selected session header / breadcrumb
   - `Main`
   - `Main > Agent: <name>`
   - `Main > Subagent: <name>`
   - `Main > Workflow: <run>`
   - 中间区域必须明确当前展示的是哪一个 session window。
   - 主要切换入口在右侧下栏 `Agents` / `Background` / `Workflow`，不是输入框上方主 tab。
   - 切换后，中间 session window 仍要显示一个 active session tab / label，类似 CC/Codex TUI 的 cyan label。
   - active tab 的颜色随 session 状态变化：running 用高亮色，waiting/permission 用 amber，blocked/failed 用 red，completed 用 muted green/gray。

3. Git-line checkpoint conversation flow
   - 每个 user prompt 是 primary checkpoint node。
   - compact / permission / plan approval / branch anchor 是 marker。
   - node 状态：`past`、`current_head`、`rewound_tail`、`new_tail`、`branch_anchor`、`compacted_scope`。
   - 这条线长在对话流本身，不放到左侧导航，也不作为右侧列表。
   - 这不是完整 Git 实现，而是借用 Git timeline 的视觉语言：线、节点、分叉、hover preview、当前 head 高亮。

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
   - active session label。
   - attachment/artifact chips。
   - 输入栏整体边框不改；当前输入上下文跟随中间 active session window。
   - Composer 必须贴近 session shell 底部，底部留白只保留安全间距，不允许出现大块空白。
   - chat history 区域用 flex 占满剩余高度；composer 是底部固定的 flow child，不通过额外 spacer 把输入框顶高。
   - 当前实现里的硬编码高度和固定底部 padding 需要收敛为三栏 shell 的 flex 高度模型。

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
| Agents | 运行中 / 已完成的 Agent Team member、Sub-agent、Background Agent session；点击切换中间 session window |
| Tasks | 当前 todo / work ledger / task table |
| Background | background agent、long task、completion wake |
| Workflow | dynamic proposal、definition、step graph、journal、gate/wait/resume/repair/promote |
| Notifications | 后台任务完成、失败、需要用户处理的通知 |
| Commands | 独立运行命令、最近 command result、command schema |
| Runs | active RuntimeTask、tool rounds、tool calls、terminal status、killed/cancelled/failure reason |
| Governance | L0/L1/L2/L3、permission profile、pending approvals、Truth Search evidence、decision trace |
| Raw | T0/source refs/InvocationSpan/export JSON，仅 debug/admin 默认展开 |

### 2.4 Codex-like 视觉密度与样式基线

当前页面“不好看”的根因不是某一个按钮颜色，而是 Session 区没有统一的 workbench density。当前代码里同一个 Chat 页面混用了大量 inline `10/11/12/13/15/16/28px` 字号、随机 padding、过圆 bubble、彩色选中态和局部背景色。下一轮前端不能继续在各组件里局部调样式，必须先建立 Session TUI 的视觉 token。

目标参考 Codex / CC TUI 的克制风格：

- 信息密度高，但不拥挤。
- 低饱和灰阶为主，颜色只表达状态。
- 细线、轻底色、小状态点，而不是大面积彩色块。
- 选中态清楚但安静，不抢正文。
- 文本阅读优先，装饰退后。

Session TUI 字号基线：

| 用途 | 字号 | 行高 | 说明 |
| --- | --- | --- | --- |
| 主正文 / assistant answer | 13px | 1.55-1.65 | Chat 主阅读字号。 |
| 用户消息 / compact text | 13px | 1.5 | 不要放大成卡片标题。 |
| section title / row primary | 12px | 1.35-1.45 | Runtime table、file row、checkpoint label。 |
| metadata / timestamp / status | 11px | 1.3-1.4 | token、elapsed、source、secondary hint。 |
| tiny badge / count | 10px | 1.2 | 只用于很短的 counter，不用于长句。 |
| page title / active session title | 15px | 1.3 | 仅 header 使用，避免 hero 化。 |

Spacing 基线：

| 层级 | 值 | 用途 |
| --- | --- | --- |
| row gap | 4-6px | 同组 metadata / icon / status。 |
| compact row padding | 6px 8px | runtime table、checkpoint row、small controls。 |
| message inner padding | 8px 12px | 普通消息 bubble / tool summary。 |
| panel padding | 10px 12px | 右侧 Runtime / Workspace panel。 |
| section gap | 12-16px | 明确不同模块之间的间距。 |
| page gutter | 16-20px | 中间 timeline 左右留白，不能过大。 |

选中态与状态色：

- selected：低饱和灰底 + 1px border + 左侧 2px hairline 或小圆点；不要使用大面积绿色/青色底。
- hover：只提升背景一档或边框一档，不加阴影。
- running：小状态点 / thin accent line；不要整行染色。
- waiting/permission：amber 小点或 tiny chip。
- failed/blocked：red 小点 + 文案，不整块红底。
- completed：muted green/gray，降低视觉权重。
- active session tab 可以借鉴 Codex 的 cyan label，但只用于当前 session window 的小标签，不扩展成整块 header 背景。

视觉禁用项：

- 不再在 Session TUI 中随意新增 `borderRadius: 12px` 的大卡片；普通 row 用 6px，面板最多 8px。
- 不在消息气泡、checkpoint、runtime row 上使用高饱和彩色背景。
- 不用 emoji 作为主要结构图标；状态使用图标库或小点/线。
- 不把每个小模块都包成 card；同一区域内用分隔线、缩进、密度层级表达。
- 不用随机 inline style 定义字号和间距；新实现先提取 `sessionTuiTokens` 或 CSS class。

当前代码断点：

- `frontend/src/pages/agent-detail/AgentChatSection.tsx` 内存在大量局部 inline 字号和 padding。
- `SessionCommandControlPanel` 的 checkpoint rail 当前是 10px x 18px 方块按钮，不像 Codex 的细线/节点。
- 用户消息当前使用 `rgba(16,185,129,0.1/0.15)` 的绿色气泡/头像底，这会让选中态和消息角色色都显得廉价。
- `details/thinking/tool` 多处使用紫色或随机状态背景，和 Codex-like workbench 风格不统一。

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
| Collaboration | `/agent` `/task delegate` `/team` | 右侧 Runtime Agents row + 中间 session window + 右侧 Background / Runs tab |
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

### 5.1.1 Git-like checkpoint rail

这里要借鉴 CC / Codex 的“细线 + 节点 + hover 卡片”表达，而不是实现真实 Git。

目标效果不是完整 Git DAG，而是“主线优先 + 分支提示 + 必要时短支线展开”。

默认形态：

```text
│
●  checkpoint 1  用户发起任务
│
·  tool calls / search / todo / workflow marker
│
●  checkpoint 2  阶段性结果        [2 branches]
│
●  checkpoint 3  main 继续
│
╳  rewound old tail  被回溯排除，不再进入 active context
│
●  new tail after rewind
```

展开某个 checkpoint 的 branch stack：

```text
│
●  checkpoint 2  阶段性结果
├─ branch A · completed · 12 msgs
└─ branch B · running   · 4 tool calls
│
●  checkpoint 3  main 继续
```

交互规则：

- Hover checkpoint 节点：显示该 checkpoint 的缩略状态卡片。
- 缩略状态卡片只展示状态，不承载改变类动作：用户输入摘要、时间、工具调用数、文件变化数、是否有 compact / branch / workspace snapshot、当时运行状态。
- Click checkpoint 节点：执行 session 内位置回滚 / 定位，把中间 session timeline 滚动并聚焦到该 checkpoint 对应的对话锚点。
- Click checkpoint 不是弹卡片，不是执行 `/rewind`，也不是创建 branch；它只改变当前 UI viewport / focused anchor。
- 改变类操作只有两个：`Rewind here`、`Branch here`，它们出现在被定位到的 checkpoint 对话锚点操作区或右侧 inspector 动作区，而不是 hover 缩略卡片里。
- 只读操作可以有：`查看上下文`、`查看文件变化 / workspace snapshot`。
- `复制输入` 或 prompt prefill 是 composer helper，不是第三种 session mutation，不命名为 `Clone`。
- 当前定位节点时，rail 上的线段应有轻微高亮或流动动效，让用户看到“当前视口回到了哪一个 checkpoint / 哪一段工作线”。
- branch 默认折叠成 branch chip，例如 `[2 branches]`，不要默认画多条并行线。
- 只有 hover / selected checkpoint / branch chip click 时，才展开短支线列表。
- 展开的 branch stack 最多缩进 16-20px，使用 1px hairline，不画大面积彩色线。
- branch 行只显示 branch title、status、message/tool count、elapsed；点击 branch 行切换到对应 `ChatSession.id`。
- rewind 后的旧 tail 不删除，只灰掉 / 降低透明度，并明确标注“不在当前上下文”。
- compact marker 是线上的压缩标记，不是普通 assistant 消息。

GitLine 视觉规格：

| 元素 | 规格 |
| --- | --- |
| 主线 | 1px neutral hairline，低对比度；当前 active segment 提升一档对比度。 |
| checkpoint node | 6-7px 圆点；current head 可用 8px ring，不用大方块。 |
| tool/compact marker | 3-4px 小点或短横，不抢 checkpoint。 |
| branch chip | 10px 字号、4px radius、灰底/灰边，不用彩色胶囊。 |
| selected checkpoint | node ring + row 背景轻微提升；不要整行强色。 |
| rewound tail | opacity 0.35-0.45 + dashed line / crossed marker。 |
| branch expanded line | 1px elbow line，最多两级；深层 branch 折叠成 count。 |

分支不丑的关键原则：

- 主线永远是主视觉，branch 只是“可进入的另一路径”。
- 不做横向铺开的树；横向 DAG 会和 chat 阅读宽度冲突。
- 多个 branch 不逐条常驻展开，默认只显示 count。
- 选中 branch 后，中间切到该 branch session，GitLine 只高亮该 branch path，不同时展示所有路径。
- branch path 使用短线和小标签，不使用大面积背景色区分。

### 5.1.2 Branch 切换视觉契约

branch 切换必须让用户看懂两件事：

1. 我现在在哪条 session line。
2. 这条 line 是从哪个 checkpoint 分出去的。

不要用“每个 branch 一个颜色”来解决这个问题。branch 多了以后颜色会失控，也会和 running/waiting/failed 这些状态色冲突。最终规则是：**颜色表达状态，不表达 branch 身份；branch 身份用结构、标签和 active path 表达。**

状态模型：

| 状态 | UI 表达 |
| --- | --- |
| common ancestor checkpoint | neutral node + normal line；如果它是分叉点，右侧显示 branch count chip。 |
| 当前 active session line | active path 用一档更深的 neutral/cyan hairline + current head ring。 |
| inactive sibling branch | 只显示 branch chip / branch row，灰色文本，不常驻画完整路径。 |
| selected branch row | 轻灰背景 + 1px border + 左侧 2px hairline；不整行染色。 |
| running branch | branch row 内显示小 running dot / elapsed / tool count，不改变 branch identity 色。 |
| failed branch | branch row 内显示 red dot + failed 文案，不把整条 branch path 染红。 |

切换流程：

```text
1. click checkpoint C2
   -> 中间 timeline 定位到 C2
   -> C2 旁显示 [2 branches]

2. click [2 branches]
   -> 展开 C2 下的 branch stack
      ├─ main · current · 20 msgs
      ├─ branch A · completed · 12 msgs
      └─ branch B · running · 4 tool calls

3. click branch A
   -> switch_session(branchA.chat_session_id)
   -> 中间 Chat 切到 branch A 的 transcript
   -> composer 当前输入上下文切到 branch A
   -> GitLine active path 从 C2 高亮到 branch A head
   -> header/breadcrumb 显示 Main > branch A
```

branch 后续 checkpoint 规则：

- branch 创建点仍是共同祖先 checkpoint。
- branch 进入后，后续用户输入生成 branch-local checkpoint。
- branch-local checkpoint 只属于当前 branch session line，不回写 main。
- 切回 main 时，只高亮 main path；branch-local checkpoint 折叠回 branch chip。
- 切回 branch A 时，重新显示 `C2 -> A1 -> A2 -> branch A head` 这条 active path。
- 如果 branch A 上继续创建 branch，默认仍折叠成 branch chip，不展开成全量 DAG。

视觉示意：

```text
main view:
● C1
│
● C2  [2 branches]
│
● C3 main head

branch A active view:
● C1
│
● C2  fork anchor
└─● A1
  │
  ● A2 branch A head
```

让它克制又看得懂的关键：

- 不靠颜色区分 branch；只用一个 active path 高亮。
- 不同时画所有 branch path；只展开当前 selected checkpoint 下的 branch stack。
- 中间 header / active session label 必须显示当前 line 名称，例如 `Main > branch A`。
- 右侧 Runtime Agents / Branches tab 同步选中同一个 branch row。
- GitLine、Chat transcript、composer context 必须同源切换，不能只换视觉不换 session。

动效边界：

- 可以有节点 pulse、线段 draw-in、选中态平滑过渡、hover card fade/slide。
- 不做花哨的全屏动画，不改变 transcript 的阅读稳定性。
- 动效必须服务于“我当前在哪个 checkpoint / 哪条线 / 哪些内容已被排除”。

checkpoint 节点的 hover 状态卡片：

- 输入摘要。
- 运行状态。
- tool call / token / 文件变化摘要。
- compact / branch / workspace snapshot 标记。

点击 checkpoint 后：

- 中间 session timeline 回滚 / 定位到该 checkpoint 对应的对话锚点。
- 对话锚点处显示 action bar：`Rewind here`、`Branch here`、`查看上下文`、`查看文件变化 / workspace snapshot`、`复制该轮输入`。
- `复制该轮输入` 只能影响 composer/clipboard，不能改变 `active_projection`、`ChatSession.id` 或 runtime head。

当前 `SessionCommandControlPanel` 直接触发 rewind，需要升级为 hover 状态卡片 + click 定位 + 锚点 action bar。

### 5.2 Rewind

Rewind 不创建新 session。它更新当前 session 的 active projection。

语义边界：

- `Rewind here` 的 anchor 是所选 user checkpoint。
- 实际恢复点是该 user prompt 输入前，即 provider conversation 必须使用 `sequence < checkpoint.sequence`。
- T0/transcript append-only，不物理删除 checkpoint 之后的旧消息。
- selected prompt 如果需要回填输入框，只能作为 composer prefill 副作用，不进入下一轮 provider context。

UI 必须显示：

- active head 指向哪个 checkpoint。
- 哪些旧 tail 已被排除出当前 context。
- rewind 后的新 tail 从哪里开始。

真实断点：

- 当前 `backend/app/services/web_chat_runtime.py::_rewind_projected_history` 仍按 `sequence <= anchor.sequence` 取 prefix，会把所选 user prompt 本身带入下一轮上下文；这与 CC MessageSelector 的 before-boundary 不一致。
- 当前 `frontend/src/pages/agent-detail/AgentChatSection.tsx::SessionCommandControlPanel` 的 checkpoint row 点击会直接触发 `/rewind`；这必须改为只选中/定位。

### 5.3 Branch

Branch 创建新 `ChatSession.id`。

语义边界：

- `/branch` slash command：对齐 CC current-head fork，按当前有效 head 创建新 session line。
- checkpoint 上的 `Branch here`：对齐 CC MessageSelector 的 before-boundary，从所选 user checkpoint 输入前复制 prefix 创建新 session line。
- checkpoint `Branch here` 不能把被选 user prompt 本身复制进新分支；如果 UI 需要让用户继续编辑该 prompt，应使用 composer prefill，而不是把 prompt 写入新 session transcript。

UI 必须显示：

- branch graph。
- anchor checkpoint。
- parent session。
- 当前 branch。
- 切换 branch 等价于切换 session。

真实断点：

- 当前 `/branch` command 内部使用 `create_conversation_branch(mode="branch")`，适合 current-head branch；如果 selected checkpoint UI 复用该路径，会因为 `mode="branch"` 的 prefix 规则包含 anchor 而复制 selected prompt。
- 当前 `POST /sessions/{session_id}/branches` schema 接受 `mode="rewind"`，且 branch service 已有 before-boundary 能力；checkpoint `Branch here` 应走该模式，而不是复用普通 `/branch` command。

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

### 6.1 统一为 Session Windows

不要把 Agent Team、Sub-agent、Background Agent 分散到全局导航或独立页面。它们应是当前 parent session 下的 child session windows，由右侧 Runtime Agents panel 列出，中间区域负责显示当前选中的 session window：

```text
Right Runtime / Agents
  ● main
  ● general-purpose  排查 Phase 0+1 Contract与Taxonomy
  ○ general-purpose  排查 Phase 2+3 边界与 L1L3

Center
  [排查 Phase 0+1 Contract与Taxonomy]  <- active session tab / colored label
  Main > general-purpose / 排查 Phase 0+1 Contract与Taxonomy
  child session transcript
```

每个 child session 有状态：

- running
- waiting_for_permission
- waiting_for_user
- blocked_by_hook
- completed
- failed
- cancelled

### 6.2 Agent Team

位置：

- 右侧下栏 `Runtime Tables > Agents` 显示 team/member session 列表。
- 点击右侧 member row 后，中间区域切换到该 member 的独立 session window。
- 中间 session window 顶部显示 breadcrumb / title，强调当前对应的是哪一个 session。
- 中间 session window 顶部或分隔线上显示 active session tab / colored label；颜色与右侧 row 的状态同步。
- 底部输入栏边框不改；进入 child session window 后，输入上下文就是该 child session。
- 右侧 Runtime Tables 继续显示 team/member graph、mailbox、events、enter/resume/wait/close 的详情和审计。

当前 `SessionNativeControls.enterMember` 已能进入 member session，下一步要把它升级为右侧 Runtime Agents row -> 中间 session window 的直接切换。

### 6.2.1 Runtime Agents Panel 表达

Agent Team 的主表达参考 CC/Codex 的 TUI，但 Web 端放在右侧下栏：不是普通 tab，而是一组可点击的 running session rows。

目标结构：

```text
Runtime / Agents
Running 2 agents

› ● general-purpose
  排查 Phase 0+1 Contract与Taxonomy
  31s · 8 tool uses · 77.3k tokens
  Bash: Check which contract classes appear in tests

  ○ general-purpose
  排查 Phase 2+3 边界与 L1L3
  8s · 6 tool uses · 62.1k tokens
  Reading 4 files...
```

显示规则：

- `main` 永远存在，表示父 session 主线。
- 每个 team member / subagent / background worker 是一行 session row。
- 当前正在查看的 session row 用 `›` + 实心点 / 高亮背景表示。
- 运行中显示 elapsed time、token estimate、tool use count、last activity。
- 完成显示 completed / failed / cancelled，并保留可查看入口。
- pending permission / blocked hook / waiting user 用状态徽标显示。
- 右侧下栏顶部显示 `Running N agents` / `Completed N` / `Blocked N` summary。

交互规则：

- 点击 row：切换中间区域到该 member 的 child session window。
- 切换完成后，中间顶部 active tab 文案和颜色同步变化，用户能立即知道当前窗口对应哪个 session。
- 点击 `main` row：回到父 session 主线。
- child session window 不是只读预览；它是当前可交互的 session 表达面。
- row 右侧可以有停止单个 agent 的 icon button；批量停止放在 Runtime Agents panel 菜单，不放左侧导航。
- 如果一个 team 有多个成员，Runtime Agents panel 默认展开；完成后可折叠成一行 summary。
- parent transcript 里只保留 “Running N agents...” / “Agent completed” 这类 marker，不把每个 child agent 的全部细节灌进主线。

与右侧 Runtime Tables 的分工：

- Runtime Agents panel：快速切换当前正在操作/查看的 agent session。
- Runtime detail tabs：显示完整 member graph、mailbox、event log、tool calls、permission/gate、stop/resume 审计。

### 6.3 Sub-agent

Sub-agent 应显示：

- subagent type / role。
- child session id。
- tool profile / allowed tools。
- running/completed/failed。
- completion wake。
- parent timeline 投影。

用户点击 subagent marker：

- 中栏切换 child session window 或打开 filtered transcript。
- 右侧 Runtime Tables 的 Background / Runs tab 展示详情。

### 6.4 Background Agent

Background completion wake 已进入 Workbench read model。UI 应表达为：

- Header / right Runtime summary 显示 pending/running/completed/failed count。
- 右侧 Runtime Agents / Background tab 显示 background count。
- 右侧 Runtime Tables 的 Runs / Background tab 显示 run detail。
- 完成后在 parent session timeline 插入 completion marker。

## 7. Dynamic Workflow

Dynamic Workflow 不应该只是一张 inline card，也不应该被误表达成 Agent Team 的完整 member session。

真实实现路径：

- Dynamic Workflow root 是 `RuntimeTask(task_type="workflow")`。
- 它的进度由 `workflow_steps` 和 `workflow_leaf_calls` 组成。
- workflow leaf 通过 `build_subagent_leaf_executor()` 调用真实 `spawn_subagent(ctx, spec, task)`。
- 该调用是同步 leaf worker，返回 conclusion-only `SubagentResult`。
- 当前 `GET /agents/{agent_id}/workflows/runs/{run_id}` 的 `leaf_calls` 只返回 `step_id`、`leaf_id`、`status`、`error`、`token_usage`，没有 `child_session_id`。
- 因此 Dynamic Workflow leaf 当前不是 enterable ChatSession，不能像 Agent Team member 一样进入完整第二 / 第三 session 通道。

证据：

- `backend/app/services/workflow_launch.py`：`build_subagent_leaf_executor()` 将 leaf 绑定到真实 `spawn_subagent`。
- `backend/app/agents/subagent.py`：同步 `spawn_subagent(run_in_background=False)` 返回 resolved `SubagentHandle.result`，而 `SubagentResult` 是 conclusion-only。
- `backend/app/api/workflows.py`：workflow detail API 的 `leaf_calls` 没有 `child_session_id`。
- `frontend/src/pages/agent-detail/AgentWorkflowsSection.tsx`：当前 UI 只把 `leaf_calls` 渲染成普通状态列表。

### 7.1 UI 形态

Dynamic Workflow 的 UI/UX 应比 Agent Team 简单。它的主要目标不是进入每个 leaf 的完整对话，而是清楚表达 workflow run 的整体状态、阶段进度、leaf 状态、整合节点和最终产物。

右侧入口：

- 右侧下栏仍然提供统一入口。
- `Runtime Agents` 可以显示 workflow root row，例如 `Workflow: ccplus-closure-audit · 21/24 agents done`。
- workflow root row 用于提示“有一个 workflow 正在跑”，但不把每个 leaf 冒充为可进入的 agent session。
- 点击 workflow root row：中间切到 `Workflow Run Window`。
- 点击 workflow marker：默认打开右侧 `Workflow` tab。

中间 `Workflow Run Window`：

```text
[Workflow: ccplus-closure-audit]  running
Main > Workflow / ccplus-closure-audit

Phase / Step
▾ 1 Audit                21/24 leaves done
  ✓ find:D0-contracts    115.3k tok · done
  ✓ find:D1-taxonomy     108k tok · done
  ○ verify:D7-truth      running
▸ 2 Synthesize

Selected leaf detail
  prompt summary
  status / tokens / tools
  result summary
  sources / error
```

右侧 `Workflow` tab：

- proposal candidate
- exact artifact/hash
- phase / step tree
- leaf status table
- selected leaf detail
- journal
- gate/wait/resume
- repair action
- promote suggestion

交互规则：

- workflow root 可切换中间 `Workflow Run Window`，但它不是 ChatSession。
- step row 展开 / 折叠，不切换 session。
- leaf row 展开 detail，不切换 session。
- 只有 leaf 或 background subagent 显式带 `child_session_id` 时，才允许显示 “Enter session”。
- 当前 Dynamic Workflow leaf 没有 `child_session_id`，所以只显示 `View leaf detail`。
- parent transcript 只保留 proposal/start/progress/final marker，不把每个 leaf transcript 灌进主线。

### 7.2 与 Agent Team 的边界

| 项 | Agent Team | Dynamic Workflow |
| --- | --- | --- |
| 运行本体 | `AgentTeam` + `AgentTeamMember` | `RuntimeTask(workflow)` |
| worker 形态 | teammate child `ChatSession` | workflow step / leaf call |
| 是否 enterable | 是，member 有 `chat_session_id` | 当前否，leaf 无 `child_session_id` |
| 中间点击行为 | 切换到 member session window | 切换到 workflow run window / 展开 leaf detail |
| 右侧表达 | Runtime Agents row 为主 | Workflow tab 的 phase / step / leaf tree 为主 |

### 7.3 当前核验结论

当前存在一个已关闭项和一个需要保持边界的 UI 约束：

1. **Agent Team 前端创建路径已改为 container-only。**
   - 当前 `frontend/src/api/domains/ccParity.ts::CreateAgentTeamInput` 只包含 `parent_session_id` 和 `name`，不再包含 `members`。
   - 当前 `frontend/src/pages/session-workbench/SessionNativeControls.tsx::createTeam` 只向 `ccParityApi.createTeam()` 传 `parent_session_id` 和 `name`。
   - UI 文案已经说明 teammate 通过 `spawn_subagent` 加入 team。
   - `enterMember` 继续通过 `ccParityApi.enterTeamMember()` 切换到 member 的 `chat_session_id`。
   - 后端 `create_agent_team_runtime_result()` 已明确 `TeamCreate creates the Team container only; spawn teammates with spawn_subagent team_name + name`。
   - 结论：这个断点当前按代码已关闭；后续工作不是再修 create API，而是把 team/member rows 从 `SessionNativeControls` 的控件列表升级到右侧 Runtime Agents panel。

2. **Dynamic Workflow leaf 当前没有 enterable session contract。**
   - 当前 `WorkflowLeafCall` 和 workflow detail API 不携带 `child_session_id`。
   - 这不是 bug，而是正确的 UI 边界：workflow leaf 是同步 subagent worker / leaf call，不是 Agent Team member session。
   - 修复：UI 不提供 leaf session enter；只提供 leaf detail。未来如果 workflow leaf 改为 background subagent 并持久化 `child_session_id`，再按显式字段开启 enter。

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

### 8.4 页面级拆分：Session Page 与 Agent Detail

当前前端已经在产品入口上露出两条路径：

- 左侧 Agent 行的加号：创建一个新的 Session。
- 左侧 Agent 行的详情按钮：进入 Agent Detail / 管理页。

但当前实现仍是“入口看似分离，运行时仍塞回 AgentDetail”：

- `frontend/src/App.tsx` 已有 `agents/:id/chat` 路由，但 `frontend/src/pages/Chat.tsx` 只是重定向到 `/agents/:id#chat`。
- 左侧创建 Session 后仍跳转到 `/agents/:id?session_id=...#chat`。
- `frontend/src/pages/AgentDetail.tsx::isSessionWorkbenchRoute()` 用 `activeTab === "chat" && session_id && !manage` 判断 session-only 模式。
- `AgentDetail` 通过 `agent-detail-session-only` 隐藏 header/tab，把 Chat 伪装成独立页面。
- `AgentChatSection` 已有 `sessionOnly` prop，但它仍由 `AgentDetail` 持有全部 session state、WebSocket、run、branch、permission、upload、composer 状态。

结论：当前是半拆分状态。下一轮不应该把 Chat 区域作为一个整体从 `AgentDetail` 里搬走，而应该按 scope 拆开三个职责：

1. **我的对话 / My Conversations**：应该在 Agent Detail 外面，作为用户自己的 Session 入口和个人工作流入口。左侧 Agent 下的 session list / 新建 session / 我的会话列表都属于这一层。
2. **所有用户 / All Users**：仍保留在 Agent Detail 内。它是管理/审计视角，用来查看这个 Agent 面向所有用户产生的对话记录、归属、删除/管理入口。这里不是普通个人工作台。
3. **当前对话 / Active Session Workbench**：从 Agent Detail 里拆出来，回到独立 Session Page。它承载当前 session 的 timeline、checkpoint、right runtime、composer、Agent Team/Workflow 状态。

也就是说，Agent Detail 不是只剩一个“打开 Session”的按钮；它仍保留 `All Users` 这一类对话管理/审计区域。但 `My Conversations` 和当前个人 Session workbench 不应该继续藏在 Agent Detail 里面。

目标页面职责：

| 页面 | 路由建议 | 职责 |
| --- | --- | --- |
| Agent Detail | `/agents/:agentId` 或后续 `/agents/:agentId/details` | 管理 Agent：概览、能力、记忆、A2A、workspace、workflow、office、governance、settings；同时保留 `All Users` 对话审计/管理区域，用于查看所有用户与该 Agent 的会话记录。 |
| My Conversations / Session Entry | 左侧 Agent 下 session list，或后续 `/agents/:agentId/sessions` | 用户自己的对话入口：新建 session、查看自己的 session、进入当前 session。它在 Agent Detail 外部。 |
| Agent Session Page | `/agents/:agentId/sessions/:sessionId` | 承载“当前对话 / Active Session Workbench”：中间 timeline、checkpoint git line、right Workspace/Runtime、Agent Team/Workflow 状态、composer。不再重复显示 `All Users` 管理列表。 |
| Agent Chat Redirect | `/agents/:agentId/chat` | 兼容入口：进入最近 session，或创建新 session 后跳到 `/agents/:agentId/sessions/:sessionId`。 |
| Legacy Session URL | `/agents/:agentId?session_id=...#chat` | 兼容旧链接：redirect 到 `/agents/:agentId/sessions/:sessionId`。 |

迁移策略：

1. 先把当前 `AgentChatSection` 拆成两个概念组件：
   - `AgentUserConversationAudit`：`All Users` 对话列表、归属用户、只读查看/删除/管理、session metadata。继续用于 Agent Detail。
   - `MyConversationEntry`：`My Conversations` 对话列表、新建 session、进入 session。移动到 Agent Detail 外部，优先复用左侧 Agent session list；如果需要页面级入口，再做 `/agents/:agentId/sessions`。
   - `ActiveSessionWorkbench`：当前对话 timeline、right runtime、composer、checkpoint、permission、workflow/team。用于独立 Session Page。
2. 抽出 `useAgentSessionController(agentId, sessionId)`，只管理当前 active session 的消息、WebSocket、run、permission、upload、branch、composer 状态。
3. 新建 `AgentSessionPage.tsx`，使用 `useAgentSessionController` + `ActiveSessionWorkbench`。
4. `AppSidebar` 的 session row 和创建 session 后跳转统一改到 `/agents/:agentId/sessions/:sessionId`。
5. `AgentDetail` 的 Chat/Conversations 区域只保留 `All Users` 管理/审计视角；`My Conversations` 不再作为 Agent Detail 内部 tab 出现。
6. 在 `All Users` 里点击其他用户会话时，默认是 Agent Detail 内的管理查看/审计视图；只有进入自己的 session 或显式“打开为 Session”时，才跳到独立 Session Page。
7. 等新页面稳定后，再移除 `?session_id=...#chat` 的 session-only 伪页面逻辑。

这个拆分是 TUI 落地的前置条件。否则 `My Conversations`、`All Users` 和 `Active Session Workbench` 会继续混在 `AgentDetail` 的同一组件里，三栏 Session Shell、右侧 Runtime Tables、checkpoint git line、Agent Team session window 也会继续挤在管理页里。

### 8.5 消息底部 Action Bar 边界

当前消息底部操作区过重，且动作语义放错了位置。

当前代码状态：

- `frontend/src/pages/agent-detail/AgentChatSection.tsx::MessageBranchActions` 当前暴露 `Fork`、`Edit`、`Insert before`、`Insert after`、`Reply`、`Regenerate`。
- 这些动作都挂在单条消息底部，导致 Session 级操作、消息反馈、文本复制、分支编辑混在同一排。
- 从当前实现看，这个位置不适合承载 `Enter`、Agent Team enter、Workflow enter、Hook 管理、复杂 branch editor 或多种 insert/reply/regenerate 模式。

目标边界：

| 动作类别 | 是否保留在消息底部 | 说明 |
| --- | --- | --- |
| Copy | 保留 | 轻量、局部、无 session mutation。 |
| Like / Dislike | 保留 | 作为 session feedback / self-evolution 输入；必须进入可审计 feedback event。 |
| Branch / Batch（待术语确认） | 暂定保留一个入口 | 如果指 Branch：只保留一个“从这里分支”的主入口，不展示 Fork/Edit/Insert/Reply/Regenerate 多模式；具体编辑在进入 branch flow 后处理。如果指 Batch approval：它不应放在消息底部，而应放在 permission/governance UI。 |
| Rewind | 保留 | 替代当前类似 Hook/anchor 的位置，用 Rewind 图标表示“从此处回到前一轮边界”。 |
| Hook | 不保留 | Hook 是治理/运行时生命周期，不是消息局部动作；进入右侧 Governance/Runtime tab。 |
| Enter | 不保留 | Enter 只适用于 Agent Team member / child session / workflow run window，由右侧 Runtime Agents/Workflow 控制，不挂在普通消息底部。 |
| Insert before / Insert after / Reply / Regenerate | 不直接保留 | 这些是 branch/edit workflow 的子模式，不应在普通消息底部铺开。需要时进入 Branch flow 或高级菜单。 |

推荐最终消息 action bar：

```text
Copy · Like · Dislike · Branch/Batch? · Rewind
```

实装前需要确认一件事：

- 用户口头说的 `Batch` 是不是指 `Branch/分支`。如果是，UI 文案统一用 `Branch` / `分支`；如果它指批量批准，则它必须移动到 permission/governance 区，不能放在消息底部。

测试验收：

- 普通 user/assistant 消息底部不再渲染 `Fork/Edit/Insert before/Insert after/Reply/Regenerate` 这一组按钮。
- 消息底部保留 Copy、Like、Dislike。
- Rewind 图标点击不立即删除历史；它只触发明确的 rewind flow，并遵守 before-boundary 语义。
- Branch 入口如果保留，只打开单一 branch flow；不在 action bar 上展开所有 branch modes。
- Hook / Enter 不出现在普通消息底部。

### 8.6 本轮 UI 裁决：三栏、右侧列、Workspace 共享记忆、Skills 清单

本轮截图暴露了多个前端表达断点，下一轮实装必须一起处理。左侧导航栏的信息架构不属于本轮 scope，保持不动；但三栏 shell 本身已经固定，左右栏都要可缩放。

#### 8.6.0 固定三栏与底部 composer 下沉

裁决：

- Web Session Shell 固定为三栏：左侧现有导航 / 中间 chat timeline / 右侧 Workspace + Runtime。
- 左右两个侧栏都可以拖拽缩放。
- 中间 chat 区域必须跟随左右栏缩放自适应。
- 底部输入栏的边框不改，但位置要更接近 session shell 底部，去掉当前过大的底部留白。
- composer 下沉不等于遮挡内容；history 区域应该通过 `padding-bottom` / scroll anchoring 保证最后一条消息不会被输入框盖住。

当前代码证据：

- `frontend/src/pages/agent-detail/AgentChatSection.tsx:2207` 仍在非 session-only 模式使用 `height: calc(100vh - 206px)`。
- `frontend/src/pages/agent-detail/AgentChatSection.tsx:2301` 中间 chat 容器是 flex column，但整体仍被外层硬编码高度限制。
- `frontend/src/pages/agent-detail/AgentChatSection.tsx:2468` composer 作为底部 flow child 渲染，当前 padding 是 `14px 16px 16px`，外层高度模型容易造成下方空白。

下一轮实装动作：

1. 新建或改造 `SessionShell`，使用 CSS grid/flex 三栏布局。
2. 左右栏宽度进入 UI state / local storage，提供 resize handle 和 min/max width。
3. 中间 chat 使用 `minmax(520px, 1fr)` / `min-width: 0` / `overflow: hidden`，消息容器内部滚动。
4. 移除 `height: calc(100vh - 206px)` 这类硬编码高度，改用父级 shell 的 `height: 100dvh` 或明确的 viewport layout contract。
5. composer 区域贴近底部，底部安全间距控制在视觉必要范围内；不要保留大块空白。

#### 8.6.1 删除当前右侧固定 inspector 列

当前 Chat / Agent Detail 右侧红框列不是最终 TUI 的 Runtime 表达，它只是旧的 `SessionWorkbenchInspector + SessionNativeControls` 固定列，里面混放了：

- 会话上下文统计。
- JSON 导出。
- Hook 管理。
- 启动目标。
- 高级计划。

裁决：

- 这一列从当前 Agent Detail / Chat 位置删除。
- 不要把它原样迁移到新的 Session Page。
- Hook / goal / advanced plan / JSON export 如果后续仍需要，必须进入明确的 Runtime / Governance / Debug / Export 入口，而不是常驻在当前对话右侧。
- 未来独立 Session Page 的右侧区域只允许放真正服务当前 session 的内容，例如 Workspace Documents、Agent Team 状态、Dynamic Workflow 状态、后台任务、permission/governance pending items；不能复用当前这列杂项面板。

当前代码证据：

- `frontend/src/pages/agent-detail/AgentChatSection.tsx` 在 `!sessionOnly && activeSession` 时渲染 `SessionWorkbenchInspector`。
- `SessionWorkbenchInspector` 内部继续渲染 `SessionNativeControls`。
- `frontend/src/pages/session-workbench/SessionWorkbenchChrome.tsx` 仍定义固定 inspector 面板。

下一轮实装动作：

1. 从 Agent Detail / 当前 Chat 位置移除 `SessionWorkbenchInspector` 和 `SessionNativeControls`。
2. 删除或改写依赖该固定右列的测试。
3. 后续独立 Session Page 若需要右侧面板，重新按 Runtime Tables / Workspace Documents 建模，不复用旧 inspector。

#### 8.6.2 删除 Workspace 里的“团队共享记忆”

Workspace 页里的“团队共享记忆”目前没有实际使用价值，也不应该占据文档与工作区页面的首屏。

裁决：

- 从前端 Workspace tab 删除 `TeamMemorySummaryCard`。
- Workspace tab 回到文件/文档/Office/workspace artifact 的实际工作区表达。
- 后端记忆系统是否保留不在本轮前端 scope；本轮只删除这个无效前端入口。

当前代码证据：

- `frontend/src/pages/agent-detail/AgentWorkspaceSection.tsx` import `TeamMemorySummaryCard`。
- 同文件在 `FileBrowser` 之前渲染 `<TeamMemorySummaryCard agentId={agentId} section="workspace" />`。

下一轮实装动作：

1. 删除 `AgentWorkspaceSection.tsx` 里的 `TeamMemorySummaryCard` import 和渲染。
2. 更新 `AgentDetailSections.test.tsx` 中对 shared memory / team memory 的断言。
3. 若 `TeamMemorySummaryCard` 只剩 Aware 页或无真实入口，再单独判断是否继续保留组件；不要因为 Workspace 删除而顺手清理未核实入口。

#### 8.6.3 恢复 Skills 页的已安装清单

Skills 页现在只剩导入入口和格式说明，这是错误的。用户进入 Skills 页首先应该看到“这个 Agent 当前已经安装/可用的 Skill 与 MCP Skill 是什么”，导入按钮只能是辅助操作。

裁决：

- Skills 页必须恢复 installed inventory。
- 内部 / 平台内置 skill 必须列出。
- Agent workspace/imported skill 必须列出。
- ClawHub / URL 导入的 skill 必须列出。
- MCP skill / MCP-backed capability 必须列出，至少能看到 MCP server 名称、启用状态、tool count、default tool policy、always-load 状态。
- 导入入口保留，但不能替代已安装清单。

当前代码证据：

- `frontend/src/pages/agent-detail/AgentSkillsSection.tsx` 当前只渲染标题、说明、`Import from URL`、`Browse ClawHub`、`Import from Presets` 和 governed notice。
- `AgentSkillsSection` 里的 `skillApi.list()` 只在 `showImportSkillModal` 时查询，实际用于 preset import，不是 agent installed skills list。
- `frontend/src/api/domains/extensions.ts` 明确写了 `/agents/{id}/extensions` 是 Agent Detail extension state 的 single source of truth，并返回 `skills`、`mcp_servers`、`plugins`。
- `frontend/src/pages/agent-detail/ToolsManager.tsx` 已经使用 `extensionsApi.getAgentExtensions(agentId)` 展示 MCP servers / plugins，但 Skills 页没有消费这个 source of truth。

下一轮实装动作：

1. `AgentSkillsSection` 增加 `extensionsApi.getAgentExtensions(agentId)` 查询。
2. 页面顶部渲染 installed inventory，而不是先渲染导入说明。
3. installed inventory 至少分组：
   - Internal / Platform Skills。
   - Agent Skills（workspace/imported/ClawHub/URL）。
   - MCP Skills / MCP-backed Capabilities。
   - Plugins / external skill capsules（如果后端返回）。
4. 每项显示 `name`、`source`、`status`；MCP 项额外显示 `enabled`、`tool_count`、`default_tool_mode`、`always_load`。
5. 空态必须区分“真的没有安装”和“加载失败”；不能再出现 Skills 页空白但其实已有 MCP/内部 skill 的情况。

### 8.7 当前前端核验记录（2026-06-28）

本节记录下一轮前端实装前的当前 checkout 状态。当前未改前端代码，只做代码阅读和方案落账。

| 优先级 | 结论 | 当前证据 | 下一轮前端动作 |
| --- | --- | --- | --- |
| P0 | Session Shell 固定三栏：左右栏可缩放，中间 chat 自适应。 | 当前文档已定三栏；当前代码仍以 `AgentChatSection` 内联 flex + `height: calc(100vh - 206px)` 承载 session 区。 | 建 `SessionShell` grid/flex contract；左右栏 resize handle + min/max；中间 `min-width:0` 自适应；左侧导航内容不改。 |
| P0 | 当前 chat 底部留白过大，composer 需要下沉到 shell 底部。 | `AgentChatSection.tsx` composer 作为 flow child 渲染，但外层硬编码高度和 padding 会形成多余下方空间。 | 移除硬编码高度；history flex 占满剩余高度；composer sticky/flow bottom；底部只留安全间距，保证最后一条消息不被遮挡。 |
| P0 | Session TUI 需要统一 Codex-like 字号、间距、选中态 token。 | `AgentChatSection.tsx` 和 `SessionWorkbenchChrome.tsx` 混用大量 inline `10/11/12/13/15/16/28px`、随机 padding、彩色气泡/状态背景。 | 提取 Session TUI typography/density/selection tokens；主正文 13px，metadata 11px，row padding 6-8px；选中态用低饱和灰底 + 细线/小点，不用大面积彩色块。 |
| P0 | GitLine 分支默认折叠为 branch chip，展开时只画短支线。 | 当前 `SessionCommandControlPanel` checkpoint rail 是方块按钮 + row list，不具备 Codex-like 细线/节点/branch stack。 | 重写 checkpoint rail：1px 主线、6-7px 圆点、branch count chip、selected 才展开短支线；避免横向 DAG 和大面积彩色分叉。 |
| P0 | Branch 切换用 active path + label 表达，不用每个 branch 一个颜色。 | 如果用颜色区分 branch，会和 running/waiting/failed 状态色冲突；当前文档已定 branch 是 `ChatSession.id` 切换。 | branch row click 执行 `switch_session(branch.chat_session_id)`；GitLine 高亮当前 active path；header 显示 `Main > branch A`；inactive sibling branches 折叠为灰色 chip/row。 |
| P0 | 当前 Agent Detail / Chat 右侧固定 inspector 列必须从这个位置删除。 | `frontend/src/pages/agent-detail/AgentChatSection.tsx` 渲染 `SessionWorkbenchInspector`，内部挂 `SessionNativeControls`；截图红框列正是这组旧控件。 | 移除当前位置的 `SessionWorkbenchInspector` / `SessionNativeControls`；Hook/goal/export/plan 不再常驻右列；未来 Session Page 右侧区域重新按 Runtime Tables / Workspace Documents 建模。 |
| P0 | Workspace tab 的“团队共享记忆”前端入口删除。 | `frontend/src/pages/agent-detail/AgentWorkspaceSection.tsx` 在 `FileBrowser` 前渲染 `TeamMemorySummaryCard`。 | 删除 Workspace 里的 `TeamMemorySummaryCard` import/render；更新 shared memory 相关前端测试；只保留真实 workspace 文件/文档/Office 表达。 |
| P0 | Skills 页必须恢复已安装清单，内部 skill 和 MCP skill 都要列出。 | `AgentSkillsSection` 当前只剩导入入口；`skillApi.list()` 只服务 preset modal；`extensionsApi.getAgentExtensions(agentId)` 才返回 agent 的 `skills/mcp_servers/plugins`。 | `AgentSkillsSection` 查询 `extensionsApi.getAgentExtensions(agentId)`，按 Internal/Agent/MCP/Plugin 分组展示 installed inventory；导入按钮降级为辅助入口。 |
| P0 | Chat 区域需要按 scope 拆：`My Conversations` 外置；`All Users` 留在 Agent Detail；当前对话成为独立 Session Page。 | `frontend/src/pages/Chat.tsx` 目前只是 redirect 到 `/agents/:id#chat`；`AgentDetail.tsx` 用 `sessionOnly` 伪装独立 session；`AgentChatSection` 同时承载 My/All tabs 和 active session workbench。 | 拆出 `AgentUserConversationAudit` 留在 Agent Detail；`MyConversationEntry` 移到外部 session 入口；新建 `AgentSessionPage` + `ActiveSessionWorkbench` + `useAgentSessionController`；session row / new session 跳到 `/agents/:agentId/sessions/:sessionId`。 |
| P0 | 消息底部 action bar 过重，当前暴露 `Fork/Edit/Insert before/Insert after/Reply/Regenerate`。 | `frontend/src/pages/agent-detail/AgentChatSection.tsx::MessageBranchActions`。 | 收敛为 Copy、Like、Dislike、Branch/Batch?、Rewind；Hook/Enter/insert/reply/regenerate 从普通消息底部移走。 |
| P0 | checkpoint 交互仍不符合最终 TUI 语义。当前 rail node 和 row 的 click 都直接调用 `/rewind`。 | `frontend/src/pages/agent-detail/AgentChatSection.tsx:651`, `frontend/src/pages/agent-detail/AgentChatSection.tsx:674` | 写红测：hover 显示缩略状态卡；click 只定位到 checkpoint 锚点；`Rewind here` 才调用 `/rewind`；`Branch here` 走 before-boundary branch API。 |
| P0 | checkpoint click 必须是 session 内位置回滚 / 定位，不是弹卡片，也不是 mutation。 | 当前 `SessionCommandControlPanel` 没有 focused checkpoint / anchor action bar 状态模型。 | 给 `SessionCommandControlPanel` 增加 focused checkpoint state、hover preview、anchor action bar；保留底部输入框边框不动。 |
| P1 | Agent Team 创建路径已对齐 container-only，不再是当前断点。 | `frontend/src/api/domains/ccParity.ts::CreateAgentTeamInput` 只有 `parent_session_id/name`；`frontend/src/pages/session-workbench/SessionNativeControls.tsx::createTeam` 只传这两个字段。 | 后续把 team/member rows 从控件列表升级为右侧 Runtime Agents panel；不要再回退到 `members` 创建。 |
| P1 | Dynamic Workflow 当前仍应作为 workflow run window + leaf detail，不进入 leaf session。 | 当前 workflow detail leaf 没有 `child_session_id`；现有 UI 在 workflows section 展示 leaf status。 | 下一轮先不做 leaf enter；只在 Runtime Workflow tab / Workflow Run Window 展示 phase/step/leaf/detail。 |

本轮讨论后的 checkpoint UI 规则必须作为下一轮前端测试的第一条验收：

```text
hover checkpoint -> show compact status preview
click checkpoint -> scroll/focus the session timeline to that checkpoint anchor
click Rewind here -> install active rewind projection
click Branch here -> create before-boundary branch session
```

## 9. 需要改的前端代码面

### 9.1 新增 / 扩展模型

目标文件：

- `frontend/src/pages/session-workbench/timelineModel.ts`

新增模型：

```ts
type SessionWindowKind = 'main' | 'team_member' | 'subagent' | 'workflow' | 'background';

interface SessionWindowModel {
  id: string;
  kind: SessionWindowKind;
  label: string;
  status: 'running' | 'waiting' | 'blocked' | 'completed' | 'failed' | 'cancelled';
  selected?: boolean;
  activeTabLabel?: string;
  tabTone?: 'neutral' | 'running' | 'waiting' | 'blocked' | 'completed' | 'failed';
  accentColor?: string;
  preset?: string;
  teamId?: string;
  memberId?: string;
  sessionId?: string;
  runtimeTaskId?: string;
  elapsedSeconds?: number;
  tokenCount?: number;
  toolUseCount?: number;
  lastActivityLabel?: string;
  canStop?: boolean;
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
| `AgentChatSection.tsx` | 中栏引入 selected session window/header、active session tab、git-line checkpoint flow、right panel tab triggers；composer footer 保持现有边框并显示 active session context + permission |
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
- Checkpoint node hover 显示缩略状态卡片；hover 卡片不包含 `Rewind here` / `Branch here` 改变类动作。
- Checkpoint node 点击只执行 session 内位置回滚 / 定位到对应对话锚点，不直接调用 `/rewind`，不改变 `active_projection`。
- 定位后的 checkpoint 对话锚点 action bar 的 `Rewind here` 才调用 rewind command，并传 selected `checkpoint_event_id`。
- 定位后的 checkpoint 对话锚点 action bar 的 `Branch here` 走 branch API before-boundary 路径，不复用 current-head `/branch` command。
- Rewind 后旧 tail 标为 excluded from context。
- Compact marker 可打开右侧 Context/Governance detail。
- `/command ` 选择后进入 composer，执行结果不追加 raw JSON。
- Agent Team member 显示在右侧 Runtime Agents row，点击 row 切换中间 session window。
- Runtime Agents panel 渲染 `main` row、selected row、elapsed time、token count、tool use count、running/completed/failed/waiting 状态。
- 点击右侧 Runtime Agents row 后，中间 session window 的 active tab label 和颜色同步更新。
- active tab 颜色按 `tabTone` 表达 running/waiting/blocked/completed/failed，不改变底部输入框边框。
- Runtime Agents panel 的 stop single / stop all 动作不写入左侧导航。
- Subagent / background wake 显示在右侧 Runtime Agents 和 Runtime Tables。
- Workflow marker 打开右侧 Runtime Tables 的 Workflow tab。
- Dynamic Workflow root row 切换中间 `Workflow Run Window`，leaf row 只展开 detail，不进入 session。
- Dynamic Workflow leaf 没有 `child_session_id` 时不显示 “Enter session”。
- Agent Team create 前端不得再向 container-only create API 传非空 `members`。
- Governance tab 显示 permission profile / pending request / Truth Search evidence refs。
- Raw tab 默认不展开 provider/tool JSON。

构建验证：

```bash
cd frontend && npm run build
```

若本轮同时修后端 session boundary，必须先补后端红测再实现：

```bash
cd backend
source .venv/bin/activate
pytest \
  tests/services/test_web_chat_runtime.py::test_active_rewind_projection_truncates_history_to_checkpoint_and_keeps_later_tail \
  tests/services/test_conversation_branch_service.py::test_rewind_branch_copies_prefix_before_user_checkpoint \
  tests/services/test_session_command_runtime.py::test_branch_command_is_non_destructive_session_fork \
  -q
```

后端验收点：

- active rewind projection 排除 selected user checkpoint 本身。
- checkpoint `Branch here` 创建新 `ChatSession.id`，但复制边界是 selected prompt 输入前。
- `/branch` 无 checkpoint 参数仍保留 current-head fork 语义。

## 10. 落地顺序

执行顺序需要调整。当前最大风险不是缺一个功能控件，而是先在丑的、不稳定的布局和随机 inline style 上继续叠功能，后面会整体返工。因此落地必须先修视觉和布局地基，再做 session/branch/runtime 能力。

注意：下面是工程执行顺序，不是分阶段交付借口。每一步完成后都必须保持 build/test 通过；最后统一验收后才算完成。

### 10.1 Step 1：视觉地基先落地

先解决导致页面“丑”的共因。

范围：

- 建立 Session TUI typography / spacing / radius / selected-state token。
- 将 Chat / Session 相关核心组件从随机 inline `fontSize/padding/borderRadius/background` 收敛到 CSS class 或 `sessionTuiTokens`。
- 统一 Codex-like density：正文 13px、metadata 11px、row primary 12px、row padding 6-8px。
- 统一选中态：低饱和灰底 + 1px border + 小状态点/细线，不用大面积彩色块。
- 清掉绿色用户气泡、紫色 thinking 随机背景、过圆 card 等和 Session TUI 不一致的样式。

验收：

- Chat/Session 页面看起来先接近 Codex 的克制工作台密度。
- 之后 GitLine、Runtime panel、Branch row 都复用同一套 token，不再各自长出一套样式。

实施证据（2026-06-28 / Step 1）：

- 已在 `frontend/src/index.css` 建立 Session TUI density token：正文 13px、row 12px、metadata 11px、row radius 6px、panel radius 8px、低饱和 selected/hover/message 背景。
- 已将 `frontend/src/pages/agent-detail/AgentChatSection.tsx` 的核心消息行、头像、气泡、thinking 块、checkpoint command panel 收敛到 `session-tui-*` class；移除用户消息绿色 `rgba(16,185,129,...)` 与 thinking 紫色 `rgba(147,130,220,...)` 的直接表达。
- 红测：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx -t "Session TUI density"` 在实现前失败，失败点是缺少 `session-tui-message-row ...` class 且输出仍包含旧绿色/紫色 inline style。
- 绿测：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx -t "Session TUI density"` -> 1 passed / 69 skipped。
- 回归：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx` -> 70 passed。

### 10.2 Step 2：固定三栏 Shell 与底部 Composer

视觉地基稳定后，再做页面骨架。

范围：

- 新建或改造 `SessionShell`。
- 固定三栏：左侧现有导航 / 中间 Chat Timeline / 右侧 Workspace + Runtime。
- 左右栏可拖拽缩放，宽度持久化到 UI state / local storage。
- 中间 Chat 使用 `min-width: 0`、内部滚动、自适应左右栏。
- 移除 `height: calc(100vh - 206px)` 这类硬编码高度。
- composer 下沉到 shell 底部，只保留安全间距，不再出现大块底部留白。

验收：

- 拖动左右栏时，中间消息、GitLine、composer 不溢出、不遮挡。
- 窄屏下右栏可折叠，Chat 仍是主区。

实施证据（2026-06-28 / Step 2）：

- 已将 `frontend/src/pages/agent-detail/AgentChatSection.tsx` 的 session outer shell 从旧 inline flex/height 改为 `session-tui-shell` class contract；普通模式使用 `session-tui-shell-managed`，session-only 使用 `session-tui-shell-session-only`。
- 已将中间区域、history scroll 和 composer 分别收敛到 `session-tui-center`、`session-tui-history`、`session-tui-composer`；移除旧 `height: calc(100vh - 206px)` inline 高度和 composer `padding: 14px 16px 16px`。
- 已在 `frontend/src/index.css` 建立 managed shell 高度、内部滚动、composer 下沉和 detail session browser 横向 resize 的 CSS contract；输入框 shell/border 未改。
- 红测：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx -t "Session TUI shell"` 在实现前失败，失败点是缺少 `session-tui-shell` class 且输出仍包含旧 inline height/padding。
- 绿测：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx -t "Session TUI shell"` -> 1 passed / 69 skipped。
- 回归：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx` -> 70 passed。

### 10.3 Step 3：页面职责拆分

Shell 稳定后，再切页面职责，否则会继续把 Session Workbench 塞在 Agent Detail 里。

范围：

- `My Conversations` 外置到个人 session 入口。
- `All Users` 保留在 Agent Detail，作为管理/审计视角。
- `Active Session Workbench` 独立为 Session Page。
- 抽 `useAgentSessionController(agentId, sessionId)`。
- 创建 `AgentSessionPage` + `ActiveSessionWorkbench`。
- 移除 Agent Detail / Chat 里的旧 `SessionWorkbenchInspector + SessionNativeControls` 固定右列。

验收：

- Agent Detail 不再伪装 session-only page。
- 当前对话有独立 Session Page，且左侧导航内容不被重做。

实施证据（2026-06-28 / Step 3）：

- 已从 `frontend/src/pages/agent-detail/AgentChatSection.tsx` 移除旧 `SessionWorkbenchInspector + SessionNativeControls` 固定右列；Hook/goal/export/team/create/checkpoint 不再作为杂项控件常驻 Chat 右侧。
- 已将 Agent Detail 内部会话列收窄为 `All Users` 审计入口：不再展示 `My Conversations`、不再提供新建会话按钮；个人会话入口回到外部 session route / 左侧已有会话入口。
- 已在 `frontend/src/pages/AgentDetail.tsx` 中让 `manage=true#chat` 管理视角自动拉取 `all` sessions，避免移除 scope tab 后 All Users 列没有数据来源。
- 已在 `frontend/src/pages/Chat.tsx` 中保留 legacy `/agents/:id/chat?session_id=...` 的 query，跳转到 `/agents/:id?session_id=...#chat`，保证 session-only workbench 不丢当前 session。
- 红测 1：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx -t "session-workbench-inspector|persistent work ledger dock|Session page"` 在实现前失败，失败点是仍渲染 `data-testid="session-workbench-inspector"` / `data-testid="session-native-controls"`。
- 红测 2：`cd frontend && npm test -- --run src/pages/Chat.test.tsx -t "preserves an explicit session query"` 在实现前失败，失败点是 legacy redirect 输出 `/agents/agent-1#chat` 而不是 `/agents/agent-1?session_id=session-1#chat`。
- 红测 3：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx -t "All Users conversation audit"` 在实现前失败，失败点是 Agent Detail 会话列仍显示 `My Conversations` 和 `New Conversation`。
- 绿测：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx -t "All Users conversation audit|session-workbench-inspector|persistent work ledger dock|Session page"` -> 2 passed / 68 skipped。
- 绿测：`cd frontend && npm test -- --run src/pages/Chat.test.tsx` -> 2 passed。
- 回归：`cd frontend && npm test -- --run src/pages/AgentDetail.test.tsx src/pages/AgentDetail.query-gating.test.tsx` -> 2 files passed / 6 tests passed。
- 回归：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx` -> 70 passed。

### 10.4 Step 4：Session Timeline / GitLine / Rewind / Branch

页面和样式都稳定后，再做最容易混乱的 checkpoint/branch 交互。

范围：

- 扩展 `timelineModel.ts`：session windows、checkpoint nodes、branch entries、active path。
- GitLine 改成 1px 主线 + 6-7px node + branch count chip。
- checkpoint hover 只显示缩略状态卡。
- checkpoint click 只定位到对话锚点，不执行 rewind。
- GitLine 只做导航：已有 checkpoint / branch 节点可以 hover 预览、click 定位或切换已有 branch session；不得在 GitLine 上新建 branch、执行 rewind、放右键菜单或替代命令面板。
- 对话边界 action bar 放 `Rewind here` / `Branch here`，位置是下一轮 prompt 开始前的上一轮末尾。
- `Rewind here` 和 checkpoint `Branch here` 都采用 before-boundary 语义。
- branch row click 切换 `ChatSession.id`，GitLine/Chat/composer context 同步切换。
- branch 身份不用颜色区分；用 active path + header label 表达。
- Task/Todo strip 放在 composer 上方的小条：非 hover 只显示任务范围/进度（例如“第 2-6 个任务”），hover/focus 才展开具体 todo 列表；不显示 `files changed`。

验收：

- Rewind 与 Branch 视觉和语义分开。
- branch 后续 checkpoint 是 branch-local，不回写 main。
- 切回 main / branch 时 active path、Chat、composer context 一致。
- Task/Todo 与 GitLine 同属 session 结构导航层，但职责不同：GitLine 定位 checkpoint/branch，Task/Todo 展示当前工作账本，不承载 rewind/branch 操作。

实施证据（2026-06-28 / Step 4）：

- 已将 `frontend/src/pages/agent-detail/AgentChatSection.tsx::SessionCommandControlPanel` 的 checkpoint rail node / row click 从直接执行 `rewind` 改为 `focus-checkpoint`：点击 checkpoint 只定位/选中，不再直接回滚。
- 已在选中 checkpoint 的 action bar 中提供显式 `Rewind here` / `Branch here`：`Rewind here` 调用 `/rewind` 并传 `checkpoint_event_id`；`Branch here` 调用 `/branch` 并传 `anchor_event_id`，对齐后端 `session_command_runtime.py` 当前 command 语义。
- 已默认聚焦最新 checkpoint，并在 checkpoint 列表变化时保持 active checkpoint 有效；GitLine node/row 使用 `aria-pressed` 和 `is-focused` class 表达当前定位点。
- 已在 `frontend/src/index.css` 增加 focused checkpoint node、row、action bar 的克制 TUI 样式，复用 Session TUI token，不引入第二套视觉体系。
- 红测：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx -t "checkpoint selector"` 在实现前失败，失败点是 checkpoint selector 只输出 row/node，没有 `focus-checkpoint`、`Rewind here`、`Branch here`。
- 绿测：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx -t "checkpoint selector|branch lineage"` -> 2 passed / 68 skipped。
- 回归：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx` -> 70 passed。
- 构建：`cd frontend && npm run build` -> `tsc && vite build` exit 0。

补充实施证据（2026-06-28 / Step 4b）：

- 已在 `frontend/src/pages/agent-detail/AgentChatSection.tsx` 增加主 Session GitLine：`SessionGitLine` 放在 `session-tui-history-frame` 左侧轨道，和 chat history 同滚动区域对齐，不进入右栏，也不改左侧导航。
- GitLine checkpoint 节点只输出 `data-session-action="navigate-checkpoint"`，点击只调用 `navigateGitCheckpoint()` 定位/滚动到对应消息锚点；branch 节点只输出 `data-session-action="navigate-branch"` 并调用既有 `onSelectBranchSession()` 切换已有 `ChatSession.id`。
- 测试已钉死 GitLine 不提供命令入口：`AgentDetailSections.test.tsx` 断言存在 `navigate-checkpoint` / `navigate-branch`，同时不存在 `data-session-command="branch"`。
- 已在 `frontend/src/pages/agent-detail/ChatWorkLedgerDock.tsx` 将 Task/Todo 改成 composer 上方悬停条：默认只显示任务进度范围，hover/focus 展开 `agent-task-list`；不再显示文件变更数量。
- 测试已钉死 Task/Todo 位置和文案：`ChatWorkLedgerDock.test.tsx` 断言默认摘要为 `Task 2-3 of 3`，存在 hover popover，且不出现 `files changed`。
- 验证：`cd frontend && npm test -- --run src/pages/agent-detail/ChatWorkLedgerDock.test.tsx` -> 7 passed。
- 验证：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx` -> 70 passed。
- 验证：`cd frontend && npm run build` -> `tsc && vite build` exit 0。

### 10.5 Step 5：右侧 Runtime / Workspace 收敛

核心 session timeline 成型后，再把分散状态收进右侧。

范围：

- 右侧上栏：Workspace Documents。
- 右侧下栏：Runtime Tables。
- Governance / Context / Runs / Agents / Workflow / Background / Commands / Raw 按 tab 收敛。
- Agent Team、Sub-agent、Background Agent 进入 Runtime Agents，可点击切换中间 session window。
- Dynamic Workflow 进入 Workflow run window + leaf detail，不做 leaf session enter。
- slash command typed `ui_action` 映射到 timeline marker + right panel action。

验收：

- 不再用杂项 inspector 常驻右侧。
- 工具、治理、命令、workflow、agent team 状态都有统一落点。

实施证据（2026-06-28 / Step 5）：

- 已在 `frontend/src/pages/agent-detail/AgentChatSection.tsx` 新增 `SessionRuntimePanel`，并接入现有 `chat-session-workbench` 数据源；没有恢复旧 `SessionWorkbenchInspector` / `SessionNativeControls`。
- 右侧上栏 `Workspace Documents` 从当前 session timeline artifacts 收集可打开文档；右侧下栏 `Runtime` 以 tab header + runtime cards 统一承载 Agents、Workflow、Tasks、Governance、Runs。
- Agent Team / Sub-agent 由 `sessionWorkbench.teams[*].members[*].chat_session_id` 驱动：有 `chat_session_id` 时点击 member row 走现有 `onSelectBranchSession` 切换中间 session window；Dynamic Workflow 只显示在 Runtime / Workflow 状态和 active runtime 概览里，不伪装成完整可进入 Session。
- 已在 `frontend/src/index.css` 为 `session-runtime-panel` 建立可缩放右栏、上下分区、runtime tab、team row、governance metric 的统一 Session TUI 样式；`session-only` shell 也改为 row layout，以便独立 Session Page 保持中间 Chat + 右侧 Runtime。
- 已补 `frontend/src/i18n/en.json` / `frontend/src/i18n/zh.json` 的 `sessionWorkbench.rightPanel.*` 文案，避免新右栏中文界面退回英文。
- 已为右侧 Runtime panel 增加显式 collapse toggle：默认展开为 Workspace Documents + Runtime 状态区，点击后收缩为窄 rail，中间 chat 自动吃回宽度。
- 已修正 `session-only` 页面外层 padding / composer spacing：Session 页面不再像被上下裁切，composer 更贴近底部，历史区和输入区保持同一个三栏 shell。
- 红测：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx -t "workspace documents and runtime tables"` 在实现前失败，失败点是缺少 `data-testid="session-runtime-panel"`。
- 绿测：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx -t "workspace documents and runtime tables"` -> 1 passed / 69 skipped。
- 回归：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx` -> 70 passed。
- 构建：`cd frontend && npm run build` -> `tsc && vite build` exit 0。

### 10.6 Step 6：周边页面清理与能力清单恢复

主 Session Shell 稳定后，清理旧表面和断点。

范围：

- Workspace tab 删除“团队共享记忆”前端入口。
- Skills 页恢复 installed inventory：internal/platform、agent/imported、ClawHub/URL、MCP-backed capabilities、plugins。
- 消息底部 action bar 收敛为 Copy / Like / Dislike / Branch / Rewind。
- Hook / Enter / insert / reply / regenerate 移出普通消息底部。
- 清理不再使用的旧组件、旧测试和旧兼容路径。

验收：

- 不保留两套 UI 体系。
- 用户能看到已安装 skill / MCP skill，而不是只看到导入按钮。

实施证据（2026-06-28 / Step 6）：

- `frontend/src/pages/agent-detail/AgentWorkspaceSection.tsx` 已删除 `TeamMemorySummaryCard` import/render，Workspace tab 只保留 `FileBrowser` 工作区入口；`TeamMemorySummaryCard` 仍只在 `AgentAwareSection` 被引用，没有误删仍有入口的组件。
- `frontend/src/pages/agent-detail/AgentSkillsSection.tsx` 已接入 `extensionsApi.getAgentExtensions(agentId)`，并在导入按钮下方恢复 installed inventory：Installed skills、MCP-backed capabilities、Plugins；内部/导入/URL/ClawHub skill、MCP server、plugin 均从 `/agents/{id}/extensions` 同一 truth source 渲染。
- `frontend/src/pages/agent-detail/AgentChatSection.tsx` 的普通消息底部 action bar 已收敛为图标化 Like / Dislike / Branch / Rewind；`Fork/Edit/Insert before/Insert after/Reply/Regenerate` 不再在普通消息底部渲染。
- `BranchComposePanel`、`BranchComposeDraft`、`branchDraft` / `branchBusy` / `submitBranchDraft` 已从 `AgentChatSection` 删除，避免保留旧 edit/insert/reply/regenerate 分叉路径；Branch 按钮仍调用现有 `onBranchMessage(message, 'fork')`，用户端表达为 Branch，后端语义不重写。
- `frontend/src/api/domains/chat.ts` 新增 `recordSessionFeedback()`，Like / Dislike 通过 `POST /agents/{agentId}/sessions/{sessionId}/feedback` 写入 session feedback；UUID message 写 `message_id`，非 UUID transcript/event anchor 写 `decision_id`。
- `frontend/src/i18n/en.json` / `frontend/src/i18n/zh.json` 已补 Skills installed inventory、消息动作、feedback 状态文案。
- 红测 1：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx -t "transcript-anchored conversation branch actions|standalone skills module|standalone workspace module"` 在实现前失败，失败点分别是缺少 `message-action-like`、缺少 `Installed skills`、Workspace 仍出现 `Deploy Playbook` / shared memory 文案。
- 红测 2：`cd frontend && npm test -- --run src/api/adapter-cleanup.test.ts -t "session feedback"` 在实现前失败，失败点是 `chatApi.recordSessionFeedback is not a function`。
- 绿测 1：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx -t "transcript-anchored conversation branch actions|standalone skills module|standalone workspace module"` -> 3 passed / 67 skipped。
- 绿测 2：`cd frontend && npm test -- --run src/api/adapter-cleanup.test.ts -t "session feedback"` -> 1 passed / 22 skipped。
- 回归 1：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx` -> 70 passed。
- 回归 2：`cd frontend && npm test -- --run src/api/adapter-cleanup.test.ts` -> 23 passed。
- 构建：`cd frontend && npm run build` -> `tsc && vite build` exit 0。

### 10.7 Step 7：总体验收与视觉回归

最后做一次完整闭环验收。

范围：

- 前端单测。
- frontend build。
- 核验 desktop / narrow viewport。
- 截图检查三栏、resize、composer 下沉、GitLine、branch stack、Runtime panel、Skills list、Workspace 清理。
- 如果修了后端 before-boundary contract，再补后端 focused pytest。

验收：

- 功能闭环：session / checkpoint / rewind / branch / compact / runtime / workflow / agent team / skills / workspace 全部可达。
- 视觉闭环：字体、间距、选中态、GitLine、branch stack 都服从同一套 Codex-like density。
- 清理闭环：旧 inspector、无效 TeamMemorySummaryCard、旧 action bar、重复入口不再残留。

实施证据（2026-06-28 / Step 7）：

- 前端总回归：`cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx src/api/adapter-cleanup.test.ts src/pages/Chat.test.tsx src/pages/AgentDetail.test.tsx src/pages/AgentDetail.query-gating.test.tsx` -> 5 files passed / 102 tests passed。
- 构建：`cd frontend && npm run build` -> `tsc && vite build` exit 0。
- Browser smoke：临时启动 `cd frontend && npm run dev -- --host 127.0.0.1 --port 3018`，用 Playwright 打开真实 Vite app + route-mocked `/api`：
  - `/agents/:agentId?session_id=:sessionId#chat` 成功挂载 `.session-tui-shell`、`data-testid="session-runtime-panel"`、`message-action-like`、`message-action-branch`、`message-action-rewind`。
  - 桌面视口 `1440x900` 下测得 `shellWidth=1076`、`runtimePanelWidth=346`，三栏 shell 和右侧 runtime panel 均可见。
  - `/agents/:agentId#skills` 成功显示 `Installed skills`、`web-research`、`filesystem-mcp`、`paperclip`。
  - `/agents/:agentId#workspace` body text 不再包含 `Shared Team Memory` / `团队共享记忆`。
  - 该 smoke 在无真实后端场景下出现 Vite WS proxy `ECONNREFUSED` 日志，属于 mock 环境没有后端 websocket 的预期副作用；DOM 验收通过。

## 11. 完成口径

只有满足以下条件，才能说 Web/TUI 统一表达完成：

- 用户能在一个 Session 内看到当前工作线、分支、checkpoint、active head、compact 状态。
- 用户能用同一个 composer 输入 `/command args`，并看到命令结果进入 session 状态，而不是 raw JSON。
- Permission / Full access / Ask first / Approve for me 在 Header、composer footer、右侧 Governance tab 三处一致。
- Agent Team、Sub-agent、Background Agent 是 enterable session windows；Dynamic Workflow 是 workflow run window，不再只藏在独立 tab 或弹窗里。
- 工具调用默认显示 summary；Raw 细节只在 Raw tab。
- 当前文档、文件、snapshot、artifact 在右侧 Workspace Documents 可查看，不依赖 modal 作为主交互。
- Rewind 和 Branch 视觉上明确不同：rewind 改当前 head，branch 产生新 session line。
- Compact 显示为 context lifecycle，不只是普通系统消息。
- 所有 UI 状态都能从 `SessionWorkbenchV1` / session index / runtime events 重建，可 replay、可 export、可测试。

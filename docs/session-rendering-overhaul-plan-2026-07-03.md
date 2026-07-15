# Session 流式呈现「达标 + 超越 CC/Codex」实施方案（2026-07-03）

> 承接诊断文档 `docs/session-rendering-streaming-cc-codex-gap-analysis-2026-07-03.md`。
> 本文 = 深度根因升级 + 超越设计 + 完整反模式清单 + 隔离实验载体评估 + 分级实施路线。
> **状态：纯纸面设计，未改动任何产品代码。所有实施须经 owner 逐阶段授权，遵循「先研究、先测量、成熟一个部署一个、不影响正常功能」纪律。**
> 证据来源：5 路调研（codex-tui / cc-tui / hive-frontend / hive-backend / web-sota）+ 主 session 亲手核实。

---

## 0. 相对诊断文档的三处升级

1. **头号根因升级**：从"O(N)/chunk 重算"升级为 **remount 风暴风险**（关键组件 type 在 token 热路径中可能变化）——它比单纯 O(N) 更致命，是"效果非常差"的直接候选根因，需用 Profiler/测试量化实际范围。
2. **拿到超越武器**：`useSyncExternalStore` + 细粒度 selector（我们已有的 Zustand 底层即此）——Web 里最适合把流式更新降到叶子订阅粒度的武器之一，有机会在 DOM 更新粒度上低于 CC/Codex 的 cell-diff / stable-tail。
3. **载体评估完成并纠错**：`LocalAgentChatSection` 不适合当实验台（不含病灶）；最干净的第一刀是**原地稳定化**，不是另建实验页。

---

## 1. 头号根因：remount 风暴风险（每 chunk 可能让消息子树重挂载）

### 1.1 一个 chunk 的生命周期（逐跳，file:line）
- **跳 1**：`ws.onmessage` → `d.type==='chunk'`（`AgentDetail.tsx:1806`）。
- **跳 2**：同一个 WS handler 内的状态更新在 React 19 下会批处理；热路径核心变化是 `setChatMessages(prev => applyStreamingChunkEvent(prev, d))`（`:1807`），`setIsStreaming`/`setIsWaiting` 只在状态翻转时真正改变。
- **跳 3**：`AgentDetail`（3107 行）整体 re-render。当前只有少量局部 `useMemo`（例如 `runtimeSummary`），但关键 handler（`selectSession`、`handleRunSessionCommandFromUi`、`sendChatMsg`、`resolveSessionPermission`、`handleBranchMessage` 等）不是系统性 `useCallback`，JSX inline arrow prop 也会每帧重建。
- **跳 4**：`<AgentChatSection>`（未 memo，`:2766`）随父重渲 + 收到全新 prop 引用。
- **跳 5**：`AgentChatSection`（~1825 行 body）重跑——`rewindFromMessage`/`startBranchAction` 等 useCallback 依赖每帧失效。
- **跳 6（灾难点）**：`ChatMessageItem = useMemo(()=>memo(...), [.., rewindFromMessage↑, startBranchAction↑])`（`:3103-3105`）→ 若依赖因父层 callback/prop 不稳定而变化，就会算出**全新组件 type** → React 视为不同组件 → 对应消息子树 remount（DOM 销毁重建，非 diff）。实际 remount 范围必须用 Profiler/测试验证。
- **跳 7**：remount 会导致组件 state 丢失风险——`ThinkingDisclosure.expanded`、`RunDisclosureBlock.expanded`、`useLiveElapsed` 的 interval、展开的 `<details>`、文本选区都可能被重置。

### 1.2 为什么它比 O(N) 重算更致命
O(N) 是 CPU 重算；remount 是 DOM teardown + 重建 + 子组件 state 归零。二者叠加时，remount 往往比重算更容易造成体感崩坏。本文把它列为首要候选根因，但实施前要先补 Profiler/测试证据。

### 1.3 为什么它让所有下游优化作废（关键）
`React.memo` 只在**同类型协调**时短路；组件 type 变了 = 该子树 teardown，memo 没有机会生效。因此 `MarkdownRenderer` 的 memo、`StreamingMarkdown` 的 newline-gated 抗闪（`StreamingMarkdown.tsx:29`，本身写对了）、C 组局部 memo 都可能被上层 type 变化绕开。修复必须**自顶向下**：先止住会导致 type 变化的热路径，再做下游细粒度优化。

---

## 2. 完整反模式清单（可照着逐条修）

> 🔴致命 / 🟠重 / 🟡轻；难度 易/中/难。

### A 组 — AgentDetail.tsx（放大器，B 组的根源）
| # | 反模式 | file:line | 影响 | 难度 |
|---|---|---|---|---|
| A1 🔴 | chunk→setChatMessages 零节流/零合帧 | `1806-1807` | 渲染频率==后端 chunk 频率 | 易 |
| A2 🔴 | 父层关键 handler 未系统性 `useCallback`（`handleBranchMessage:2196`、`handleRunSessionCommandFromUi:1180`、`sendChatMsg:1946`、`resolveSessionPermission:2171`、`selectSession:735` 等每帧重建） | 毒化下游 memo，是跳 6 remount 的上游根因 | 中 |
| A3 🟠 | JSX 内联箭头 prop 每帧新引用（`onTogglePlanMode:2811`、`onAbortGeneration:2840` 等） | 破坏子 memo | 易-中 |
| A4 🟠 | `<AgentChatSection>` 未 `React.memo`（`2766`） | 父任何 setState 都整体重渲 | 易（须先 A2/A3） |
| A5 🟡 | `useMemo` 覆盖不足；当前已有 `runtimeSummary` 局部 memo，但事件处理仍有 `getRuntimeEventMessage({...d})` 等每事件对象复制（`1769`） | 每 chunk/事件额外建对象 | 易 |

### B 组 — AgentChatSection.tsx（震中）
| # | 反模式 | file:line | 影响 | 难度 |
|---|---|---|---|---|
| B1 🔴 | render 内 `useMemo(()=>React.memo(...))` 造组件，依赖含不稳 callback | `3103-3105` | **跳 6 元凶：依赖变化时消息子树 remount**。单条最致命 | 中（提模块顶层，callbacks/agentId 走 props） |
| B2 🔴 | `buildThreadTimeline` 裸调用未 useMemo | `3637` | O(全消息×步骤)/chunk 重算 | 易（须先稳定 `sessionWorkbench`/`branchLineage` 引用） |
| B3 🟠 | `buildRunTimelineFromMessages` 对全部消息 `.map` 重建全部步骤 | `chatDisclosureReducer.ts:417-443` | B2 内层成本，会话越长越贵 | 中（分片 memo/增量） |
| B4 🟠 | ~15 渲染闭包每帧重建（`renderConversationMessage:3333`、`renderConversationMessages:3355`、`renderInlinePlanToolCall:3296` 等） | node 数组每帧重建 | 中（拆组件） |
| B5 🟠 | `RunDisclosureBlock`+每个 `RunStepRow` 未 memo，每帧收全新 `timeline` | `3382`；`RunDisclosureBlock.tsx:190,228-233` | 改一步全步骤行重渲；若上层 remount，秒表 interval 也会拆建 | 易-中 |
| B6 🟡 | 164 处内联 `style={{}}` 每帧新对象 | GC 压力 + 破坏 memo + 违反设计法律 | 中（迁 CSS） |
| B7 🟡 | index 作 key（`3151`、`3346`、`3040`） | 删中间消息→错配/remount | 易（换 msg.id） |
| B8 🟠 | `previousUserCheckpointForMessage` 每条消息渲染时 O(index) 前向扫描 | `2414-2420`（调用 `3350`） | **O(N²)/帧** | 易-中（每帧预算一次复用） |
| B9 🟡 | `visibleTimeline`/`visibleChatMessages` 裸 const 喂给未 memo 的 B2 | `3425/3429` | B2 的输入 | 易（并入 B2 memo） |

### C 组 — MarkdownRenderer.tsx（次生，非主因）
| # | 反模式 | file:line | 影响 | 难度 |
|---|---|---|---|---|
| C1 🟡 | `markdownToHtml` 正则全量重解析整个 committed 前缀 | `197-198` | 长答案每换行重解析 O(总字符)；配 B1 则每 chunk 重解析 | 难（增量解析）或接受 |
| C2 🟡 | `dangerouslySetInnerHTML` committed 变一次整块 DOM 重建 | `200-204` | committed 块 DOM 整替 | 难 |

---

## 3. 如何做到「强于」CC/Codex（不只是追平）

### 3.1 对手的天花板
CC（cell-diff）和 Codex（stable-tail）无论多优化，每帧仍要**扫描 / diff 一个字符网格**（CC 是 damage 区整数 diff，Codex 是行队列）。这是终端的物理上限。

### 3.2 我们的超越武器：单 DOM 节点级更新粒度
**`useSyncExternalStore` + 细粒度 selector**（外部 store）：把流式文本移出大 React state 树，放进按 session/message id 分片的 store；每个 token 只通知订阅该 message id 的叶子组件，其余组件不进入 render。目标是"一个 token 只 patch 活跃叶子"——这在 Web DOM 中有机会比 TUI 每帧扫字符网格更细，但必须用 render count / Profiler 验证。
- 我们**已在用 Zustand**（`useAuthStore`/`useAppStore`），其 v5 订阅机制基于 React external store 能力，**零新依赖**即可扩一个 streaming store。
- React 官方 `useDeferredValue` 也用于流式（CC 自己就用了 `useDeferredValue(messages)`，官方认证用法）。

### 3.3 S1–S9 超越点（TUI 物理做不到）
| # | 能力 | TUI 为何做不到 | 适用 |
|---|---|---|---|
| S1 | 单 DOM 节点级更新粒度 | 每帧必扫字符网格 | 流式正文/思考逐 token |
| S2 | GPU 合成动画（transform/opacity 60/120fps 不占主线程） | 无 GPU 合成 | 思考呼吸/shimmer、工具卡展开、光标——比 Codex 字符 shimmer 更顺 |
| S3 | `content-visibility:auto`+`contain` 跳过屏外 layout/paint | 无"屏外"概念 | 长会话历史 |
| S4 | 真正虚拟化 + 可搜索/可选中 DOM | scrollback 不可编程虚拟化 | 长会话性能 |
| S5 | 富内联交互（可点 diff、可折叠工具树、内联图表/表格/图片、语法高亮、复制按钮） | 只有字符 + 有限 ANSI | 工具结果/diff/代码/数据可视化 |
| S6 | 并发渲染可中断（transition/deferred）保证流式时输入零延迟 | 单线程逐帧无优先级 | 流式中用户继续操作 |
| S7 | Web Worker/OffscreenCanvas 卸载重活（大 diff、语法高亮、MD 解析） | 无 worker 模型 | 超长工具输出/diff |
| S8 | CSS 声明式状态过渡（不占 JS） | 状态变化要逐帧重画 | running→done 颜色/高度过渡、stall 变红 |
| S9 | 可访问性开箱（prefers-reduced-motion、ARIA live 播报流式） | 无标准 a11y 层 | 全局 |

### 3.4 目标形态（融合 CC/Codex 之长 + Web 超越）
- **冻结历史**：已完成消息 `React.memo` + 稳定 id key，selector 永不变 → 永不重渲（= CC `shouldRenderStatically`+memo 短路 / Codex stable 区）。
- **活跃区细粒度订阅**：只有正在流式的消息走 streaming store 的 selector 订阅（= CC 流式独立兄弟节点 / Codex 可变尾，但粒度更细到单 DOM 节点）。
- **rAF 合帧**：token 进 store buffer，`requestAnimationFrame` 每帧 flush 一次（= Codex 8.33ms commit tick / CC 16ms throttle，但 rAF 原生对齐 vsync + 后台标签自动降频）。
- **虚拟化 + content-visibility**：长会话历史（TUI 做不到）。
- **GPU 动画 + 并发**：S2/S6 拉开身位。

---

## 4. 隔离实验载体评估（纸面）

### 4.1 结论纠错：LocalAgentChatSection 不适合当实验台
实测 `LocalAgentChatSection.tsx`（738 行）**不复用** `buildThreadTimeline`：走自己的 `localAgentChannelEventsToChatMessages`（`:115-180`）、朴素 `messages.map(bubble)` + 稳定 `message.id` key（`:536-542`）、`useMemo` 缓存（`:417,421`）。它**架构正确但不含三类交互机制**（无 thinking/tool 披露、无 `RunDisclosureBlock`、朴素 `MarkdownRenderer`、5s 轮询非逐 token）。**在它上面优化 = 在健康人身上试药，证明不了 AgentChatSection 热路径的任何东西。** 价值仅为"干净组件长什么样"的参照系。

### 4.2 现成 feature-flag / A-B：不存在
全仓聊天路径 0 个 `import.meta.env`/`VITE_`/localStorage 旗标；`sessionWorkbenchMode`（`AgentDetail.tsx:2494`）只是路由派生的**布局开关**，两模式渲染**同一个** `<AgentChatSection>`（`sessionOnly` prop `:2839`），不是灰度。`AgentChatSection` 只有 AgentDetail 一个挂载点。

### 4.3 唯一 swap 点
`AgentChatSection.tsx:3947` `renderConversationMessages(...)` —— 可写会话流式消息列表的唯一出口。

### 4.4 推荐载体路径
- **方案①（推荐，第一刀）：原地稳定化 A2/A3/B1，不建新组件、不加 flag。** 因为 remount 风暴根在"父传下不稳 prop + render 内造组件"，**任何**新组件挂到 3947 都会被同样的不稳 prop 二次毒化——不先修父层，新载体照样每 chunk 重挂载。A2/A3/B1 是**纯稳定化改写、语义零变化、无需 flag、React DevTools「highlight re-renders」可直接验**，天然满足"成熟一个部署一个"。本地 Agent 聊天不经此路径；只读会话共享 `AgentChatSection`，需纳入回归但风险较低。
- **方案②（深隔离后手）：在 3947 用自建 flag（`?thread=v2`/`localStorage`）切到新 `<SessionThread>` 隔离组件。** 优点：swap 点单一、flag 关则老路径逐字节不变→秒回滚。前置硬依赖：**仍须先完成方案①的 A2/A3**。flag 基础设施当前需新建，属"要动手搭"部分。

---

## 5. 分级实施路线（成熟一个部署一个，每阶段独立可验可回滚）

> 每阶段均为独立 PR，验收通过才进下一阶段。全部经 owner 授权后再动手。

| 阶段 | 内容 | 反模式项 | 达到/超越 | 验收方式 | 影响面 |
|---|---|---|---|---|---|
| **S0 基线与红灯测试** | 加 render/remount 观测测试或轻量 Profiler harness，构造长会话 + streaming chunk 场景，确认旧消息展开态/选区/组件 mount count 当前会受 chunk 影响 | 验证 B1/B2/B8 | 先证明症状 | `npm run test -- ...` + Playwright/Profiler 记录；拿到当前 render count 基线 | 只测不改产品行为 |
| **S1 止 remount（第一刀）** | A2 useCallback 稳定化 + A3 内联 prop 收敛 + B1 组件提模块顶层（callbacks/agentId 走 props）+ A4 memo AgentChatSection | A2/A3/A4/B1 | 追平 CC/Codex 的"历史冻结"地基 | S0 红灯变绿；DevTools highlight：旧消息行不再随 chunk 重挂；展开态/选区不再丢 | AgentChatSection 可写+只读共享路径；LocalAgentChatSection 不受影响 |
| **S2 去重算** | B2 buildThreadTimeline useMemo + B9 输入并入 + B8 checkpoint 扫描每帧预算一次 | B2/B8/B9 | 消灭 O(N)+O(N²)/chunk | 微基准：每 chunk 重算耗时随会话长度不再线性/平方增长 | 同上 |
| **S3 合帧（先低风险）** | A1 前端 delta 微批 + rAF flush，仍写回现有 `chatMessages` contract，不引入新 store | A1 | 与后端 chunk 节奏解耦到 ≤60fps | 流式 render 次数 ≤ frame count；输入框 typing latency 不上升 | 活跃消息渲染域，历史不动 |
| **S4 细粒度订阅（超越支点）** | 新建 Zustand streaming store，活跃消息走 message-id selector；冻结历史继续走普通 React props | 新 store + active tail | **超越候选**：活跃叶子订阅粒度 | DevTools：一个 token 只触发活跃叶子 render；Profiler 与 S3 对比下降 | 仅活跃 streaming tail |
| **S5 组件拆分 + 步骤域** | 拆静态历史列表 / 活跃 run / 右栏 runtime；B5 RunStepRow memo + B7 id key + B4 闭包组件化 + B6 内联样式迁 CSS | B4/B5/B6/B7 | 追平 + 为虚拟化铺路 | 长会话滚动流畅；DOM mount 稳定；现有 AgentDetail tests 全绿 | 渲染层重构，逐组件灰度 |
| **S6 增量 + 富交互超越** | B3 增量时间线（Codex stable/tail）+ C1 增量 markdown + 虚拟化/GPU 动画/富内联/CSS 过渡 | B3/C1 + S2/S4/S5/S8 | **强于**：富动画 + 富内联交互 | 截图/录屏对照 Codex；a11y 检查 | 增量增强，非破坏 |
| **后端配合（并行于 S3 之后）** | 正文/思考 delta runtime 侧微批（~16-33ms flush）降 per-delta 3 次 Redis 往返；长工具进度事件；`<think>` 标签思考 re-emit | backend | 消投递背压 + 补可见性 | 打点：delta 间隔在高负载不被 Redis 拉大；前端仍看到同等语义事件 | 后端投递层，粒度语义不变 |

**关键纪律**：S0/S1 是唯一"必须先做"的地基（不先证明并止住 remount，S3-S6 的一切细粒度/memo 都可能被上层 type 变化冲掉）。S1 本身应保持语义不变、可 DevTools/测试验证，是最安全的"成熟一个部署一个"起点。

---

## 6. 后端能力评估（承接诊断 L4）

后端 thinking/正文/tool **已逐 delta 推、非瓶颈**（诊断已坐实）。仅两类配合项，均**不改粒度、只改投递/可见性**：
1. **投递微批**（S3 并行）：`web_chat_stream_bus.py:60-69` 的 per-delta `INCR`+`XADD`+`PUBLISH` 三连，在 runtime 侧微批合并后再进 Redis，降一个数量级往返，消 Redis 抖动→burst。
2. **可见性补齐**（独立小项）：长工具执行期加进度/心跳事件；`<think>` 标签内联型 provider 思考剥标签后 re-emit 到 `on_thinking`（`llm_client.py:764-766`）。

---

## 附：证据映射
| 结论 | 来源 |
|---|---|
| remount 风暴风险（头号候选根因）+ 反模式清单 A/B/C + 载体评估 | hive-frontend 深化 + 主 session 核实 3637/3103 |
| CC cell-diff + 三重冻结 + 平滑三层 | cc-tui |
| Codex stable-tail + 换行门控 + 两档变速 + reasoning 状态栏 | codex-tui |
| 后端逐 delta 非瓶颈 + Redis 背压 + 可见性缺口 | hive-backend |
| useSyncExternalStore 细粒度订阅 + S1-S9 超越点 + 技术栈 | web-sota，需以 render count / Profiler 验证 |

**一句话**：先用 S0/S1 证明并止住 remount 风险（追平"历史冻结"地基），再用 S2/S3 去掉全量重算和无节制 chunk 渲染，随后用 `useSyncExternalStore` 细粒度订阅把更新粒度压到活跃叶子，最后用组件拆分、虚拟化、GPU 动画、富内联、并发逐步拉开身位。达标靠 S1-S3，超越靠 S4 起；全程语义可控、逐阶段可验可回滚。

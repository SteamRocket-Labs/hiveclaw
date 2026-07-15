# Session 流式呈现对标 CC / Codex 的差距分析（2026-07-03）

> 调研范围：一个 Session 内 agent 三类交互（思考 thinking / 工具调用 tool·exec / 模型输出 output）的**流式实时呈现**。
> 对照基线：Claude Code（`free-code-main` 为首要基线 + `claude-code-org` 交叉验证）与 Codex TUI（`codex-rs/tui`）。
> 我方代码：`frontend/` 前端渲染链路 + `backend/` 流式事件链路。
> 方法：4 路并行 subagent 逐行读源 + 主 session 交叉综合。所有 `file:line` 均按 2026-07-03 当前 checkout 核实；后续代码变动后应按路径重新校准行号。

---

## 0. 结论先行（TL;DR）

**反转结论一：后端不是瓶颈。** 我方后端在 thinking / 正文 / tool 三类上**全部逐 delta 实时推送、零批处理**，粒度完全足以支撑 CC/Codex 式的逐块流式与实时状态机。"后端整块给" 的假设被证伪。

**反转结论二：不是缺动画零件。** 我方 shimmer 扫光、旋转 spinner、每秒秒表、闪烁光标**四种动画都真实存在**；输出正文的 `newline-gated` 抗闪甚至**真正对齐了 Codex**。零件在，问题不在零件。

**真正的根因是前端渲染架构的方向反了：**

| | CC / Codex | 我们 |
|---|---|---|
| 已完成内容 | **冻结**，永不重绘（scrollback / stable 区） | 每 chunk 参与全量重建 |
| 每个 delta 的工作量 | 只重画"活跃的一小块" | 把**整条时间线从零重算** |
| 输出量正比于 | **变化量** | **全部历史量**（O(消息数×步骤数)/chunk） |
| 会话越长 | 每帧成本不变 | 每帧越来越贵 |

一句话：**CC/Codex 让"输出字节 ≈ 真正变化的字符数"；我们让"每个 token 都重算全部历史"。** 结果不是闪烁，而是主线程 CPU 被吃满 → 快速流式 / 长会话下卡顿、掉帧、跟不上手。

差距分四层，按修复优先级：**L1 渲染架构（主因）> L2 流式节奏控制（几乎完全缺失）> L3 思考呈现哲学（需 owner 拍板）> L4 后端投递与可见性（次要但真实）**。

---

## 1. Claude Code 是怎么做的（对照基线 1）

CC **弃用了 ink 经典 `<Static>`**，改用自研 `ink/` cell-diff 渲染器 + "终端 scrollback 即历史" 模型。

### 1.1 手感的物理根因：cell 级 diff，只吐变化字符
- 整棵树每帧渲染进一个**打包 Int32Array 的虚拟 cell 网格**（每 cell 2 个 Int32：charId / styleId），char/style/hyperlink 全 interning 成整数 id（`ink/screen.ts:332-370`）。
- diff **只扫 damage 区**（`next.damage ∪ prev.damage`），未写入区根本不进扫描；逐 cell 整数比对，全等跳过（`ink/screen.ts:1169-1178, 1213-1224`）。
- 流式追加（growing）路径**只渲染新增的行**，跳过所有旧行（`ink/log-update.ts:310, 404-412`）。
- 整帧补丁 `optimize()` 合并后**一次 `stdout.write`**；空 diff 直接 return 零写入（`ink/terminal.ts:196-198, 247`）。
- 未变子树走 **blit 快路径**：`TypedArray.set` 整块拷贝、跳过分词/字形/样式计算（`ink/render-node-to-output.ts:454-482`）。
- ~60fps 节流：`FRAME_INTERVAL_MS=16`，`scheduleRender=throttle(deferredRender,16)`（`ink/constants.ts:2`；`ink/ink.tsx:213-216`）。

### 1.2 三重"冻结历史"护栏（React 层防抖动）
- `shouldRenderStatically`：消息一旦 tool_use 全 resolved 且非 streaming，即判 static（`Messages.tsx:779, 794-810`）。
- static 行经 `React.memo` **短路**：`if(prev.isStatic && next.isStatic) return true`，永不再 render（`Message.tsx:622`）。
- `Ratchet` 单调高度锁：`maxHeight` ref 只增不减，内容滚出视口即锁高，scrollback 不因 React 重算回流跳动（`Ratchet.tsx:38, 48-51`；`MessageResponse.tsx:50`）。

### 1.3 流式内容 = 独立兄弟节点
流式文本/思考渲染在 memo 化历史列表**之外**，每个 delta 只更新这一小块，不触碰已提交历史（`Messages.tsx:703-719`）。

### 1.4 三类交互
- **思考**：`∴ Thinking` 符号 + **默认折叠一行** dim 斜体 headline，Ctrl+O 展开成段灰字（`AssistantThinkingMessage.tsx:44, 62-69`）；spinner 里 thinking 状态每态**最少显示 2s** 防抖（`Spinner.tsx:125-159`）。
- **工具**：`●` 前缀（活动态动画 loader）+ `⎿` 缩进挂结果（`MessageResponse.tsx:22`）；多工具 `groupToolUses` 合并、连续 Read/Search 折叠成一行摘要（`groupToolUses.ts:54`；`utils/collapseReadSearch.ts`）；`StreamingToolExecutor` 安全工具并行、非安全独占，`inProgressToolUseIDs` Set 驱动 spinner 出现/消失（`StreamingToolExecutor.ts:129-135, 267-269, 521-530`）。
- **输出**：流式 `streamingText` + `●` 前缀 + `StreamingMarkdown`（`Messages.tsx:703-712`）。

### 1.5 流式平滑三层（明确无逐字打字机）
1. **行边界门控**：`visibleStreamingText = streamingText.substring(0, lastIndexOf('\n')+1)` —— 隐藏正在增长的最后一行，按 `\n` 逐行流出（`REPL.tsx:1476`）。
2. **Ink 16ms 节流**：每个 delta 直接 setState，靠 Ink 渲染节流批处理（`REPL.tsx:1461-1463`）。
3. **StreamingMarkdown 尾块增量解析**：稳定前缀 memoized 永不重解析，仅增长中的尾块每 delta 重 lex（`Markdown.tsx:211-234`；LRU `tokenCache` 500）。

### 1.6 动画
- 190 个谐趣动词 spinner（`spinnerVerbs.ts:16-204`），50ms 帧时钟**下沉到叶子** `SpinnerAnimationRow`（约 25 次/轮 vs 383 次/轮），父组件隔离在热路径外（`SpinnerAnimationRow.tsx:103`）。
- shimmer：3 字符亮窗滑过，requesting 时 50ms 否则 200ms（`ShimmerChar.tsx:21-24`）。
- **stall 变红**：`timeSinceLastToken > 3000 && !hasActiveTools` → 2s 渐入染 `ERROR_RED`（`useStalledAnimation.ts:42-45`；`SpinnerGlyph.tsx:50-56`）。
- `prefersReducedMotion` 传 `null` 停动画（无障碍）。

---

## 2. Codex 是怎么做的（对照基线 2）

Codex 与 CC 殊途同归（稳定区冻结 + 活跃尾部重画），但把**流式节奏做成了四层解耦的状态机**，且 reasoning 处理更激进。

### 2.1 双区域流式模型（Stable / Tail 二分）—— 最根本
每条流切成**稳定区**（已提交进 scrollback、永不再变）+ **可变尾部**（active-cell、每 delta 自由重渲）。尾部从 `enqueued_stable_len` 开始，是"还允许变形而不破坏滚动顺序"的部分（`chatwidget/streaming/controller.rs:1-37, 206-217`）。表格增行会重排列宽——尾部机制让表格作为可变尾一直挂到 finalize。

### 2.2 换行门控 + 源码回渲（绝不渲染半成品）
markdown 收集器只在遇换行时提交"最后换行之前"的完整源码，残缺行留 buffer 等下个 delta（`markdown_stream.rs:87-96`）——用户永远不会瞥见半成品 `##` 标题。流结束整段合并成**存原始 markdown、按当前宽度重渲**的 `AgentMarkdownCell`（`history_cell/messages.rs:343-396`），resize 时表格边框重画对。

### 2.3 自适应两档变速 + 迟滞（节奏感来源）
`Smooth` 档每 commit tick 吐 1 行（打字机感）；队列压力上来切 `CatchUp` 档一次吐光积压（`chunking.rs:118-125, 200-210`）。**迟滞防抖**：进入阈值（≥8 行 或 最老行 ≥120ms）与退出阈值（≤2 行 且 ≤40ms + 维持 250ms）不对称，避免在阈值边界反复卡顿档（`chunking.rs:85-116, 216-262`）。commit 动画时钟 = 8.33ms / 120Hz（`app.rs:395`）。

### 2.4 时间锚定动画（墙钟经过时间，非计数器）
所有动画从墙钟 elapsed 算当前帧：shimmer 同步进程启动的 2 秒扫光（`shimmer.rs:16-31`）；ascii `idx=(elapsed_ms/tick_ms)%frames.len()` 且对齐 tick 边界排下一帧（`ascii_animation.rs:44-77`）。掉帧不影响动画时间正确性、永不漂移。

### 2.5 合并 + 120fps 限流的帧调度（请求帧，而非直接画）
组件"请求一帧"，专门 actor 把大量请求合并成一次绘制、钳到 120fps（`frame_requester.rs:96-127`；`frame_rate_limiter.rs:13` = 8.33ms）。

### 2.6 思考 = 提炼为状态栏旁白，不落历史
reasoning delta **不进历史流**；而是字节级扫当前思考块第一个粗体 `**...**` 作为**闪烁状态栏标题**实时显示，没闭合就等更多 delta 绝不显示半个标题（`chatwidget.rs:2044-2070`；`streaming.rs:200-225`）。思考结束只留一条 `dim().italic()` 的 `ReasoningSummaryCell`，无粗体标题则标记 `transcript_only`（Ctrl+T 才见）（`history_cell/messages.rs:478-510`）。

### 2.7 工具·exec：时态状态机 + 颜色 + 探索分组
- 数据模型 `ExecCell` 的 `output: None` 即"运行中"，`complete_call` 填 output+duration（`exec_cell/model.rs:23-33, 82-117`）。
- **时态标签翻转编码进度**：`Running→Ran`、探索组 `Exploring→Explored`（`exec_cell/render.rs:377-385`）。
- bullet 颜色状态机：运行中=动画 `•`，成功=绿，失败=红（`render.rs:370-375`）。
- 连续 Read/List/Search 折叠进同一 group cell（`model.rs:119-121, 154-165`）。
- 输出 head+tail 中间省略，agent 命令 5 行、用户 shell 50 行，附 `ctrl+t` 逃生口（`render.rs:32-33, 103-184`）；按**屏幕行**（wrap 后）而非逻辑行截断（`render.rs:523-630`）。

### 2.8 可访问性是编译期约束
所有 shimmer/spinner 必须走 `motion.rs`（内建 `Reduced` 降级）；一个测试扫描全源码，任何文件直接调 `shimmer_spans(`/`spinner(` 就让**构建失败**（`motion.rs:121-167`）。

**Codex 一句话本质**：把"何时能安全显示（换行门控/表格 holdback）、以什么节奏显示（两档迟滞）、用什么时间基准动画（墙钟锚定）、画多少次（合并限流）"**四层解耦**，每层都是状态机而非即兴逻辑。

---

## 3. 我们的现状

### 3.1 数据流全景
```
ws.onmessage (AgentDetail.tsx:1806) ── 每 chunk 无节流 ──▶ setChatMessages
  └─ applyStreamingChunkEvent (chatRuntime.ts:466)          ← 数据层：干净的 APPEND
       └─ chatMessages 传给未 memo 的 <AgentChatSection> (AgentDetail.tsx:2766)
            └─ buildThreadTimeline (AgentChatSection.tsx:3637)   ← 视图层：每帧全量重算
                 └─ buildRunTimelineFromMessages (chatDisclosureReducer.ts:417) ← 对全部消息 .map 重建
                      └─ RunDisclosureBlock / StreamingMarkdown / ThinkingDisclosure
```
**核心矛盾**：数据层是干净的"追加"，但视图模型层"每个 chunk 把整条时间线从零重建"，且跑在一个 4547 行、未 memo、无节流的巨型组件里。

### 3.2 三类交互现状
- **思考**：后端 thinking 逐块追加进 `msg.thinking`（`AgentDetail.tsx:1781`；`chatRuntime.ts:828`）。渲染 `ThinkingDisclosure` 只显示**一行** headline——流式中取"最后一个非空行"，完成 settle 到第一行（`ThinkingDisclosure.tsx:27-30`），带 shimmer。**看不到成段推理流动**；无换行长段退化成 140 字符截断 blob 像卡死（`:11`）。同一 thinking 还在 `RunDisclosureBlock` 里有第二身份（`kind:'reasoning'` 折叠步骤，`chatDisclosureReducer.ts:334-345`）——两套并存表示。
- **工具**：running/done 按 toolCallId 合并、原地替换（`chatRuntime.ts:503-539`）。状态机其实**做得不差**（`RunDisclosureBlock.tsx`）：running=IconLoader2 spin + "Working" shimmer + **活秒表**每秒 tick（`:35-56, 221`）；done=塌缩一行 compact chips（`:169-188`）；command 类结构化 exec、head/tail 各裁 5 行、exit code 着色（`:96-121`）。**死代码** `renderToolCall`（`AgentChatSection.tsx:3218`）零调用点未删。
- **输出**：逐 chunk 追加（`chatRuntime.ts:479`）。渲染 `StreamingMarkdown` **newline-gated**（Codex parity）：只把到最后换行的前缀交给 memo 的 `MarkdownRenderer`，尾行纯文本 + 闪烁光标 `▍`（`StreamingMarkdown.tsx:29-37`）。**这块是全场做得最好、真对齐 Codex 的一处。**

### 3.3 现状小结
| 维度 | 现状 |
|---|---|
| 文本粒度 | 逐 chunk 追加（非整条替换）✅ |
| 平滑/节流/pacing | **完全没有**：无 typewriter、无节流、无 rAF 合帧，chunk 来一个 setState 一次 ❌ |
| markdown 抗闪 | 有（newline-gated + memo）✅ |
| 工具/run 状态机 | 完整三态 ✅ |
| 动画 | shimmer/spinner/秒表/光标四种都在 ✅ |
| 思考流式 | 只一行 headline 跳变，无成段流动 ❌ |

---

## 4. 分层差距（核心）

### L1 — 渲染架构（主因，对应 frontend P1）🔴
三问题叠成一条 O(N)/chunk 热路径：
1. 每个 WS chunk 无条件 `setChatMessages`，零批处理（`AgentDetail.tsx:1806-1807`）。
2. `<AgentChatSection>` **未被 memo**，3107 行的 `AgentDetail` + 1825 行组件体每 chunk 整体重跑（`AgentDetail.tsx:2766`）。
3. `buildThreadTimeline` 渲染体里**裸调用未 useMemo**（`AgentChatSection.tsx:3637`），内部对**全部历史消息 `.map` 重建所有步骤**（`chatDisclosureReducer.ts:417-443`）。

**对照**：CC 已完成消息 `React.memo` 短路永不重渲 + scrollback 冻结；Codex 稳定区永不再变。我们反着来——每 token 重算全历史。memo 过的叶子挡住了 DOM 重绘，所以**不是闪烁风暴，而是主线程 CPU 打满 → 卡顿掉帧跟不上手，会话越长越贵**。

**结构性障碍**：`AgentChatSection.tsx` 184KB / 4547 行（默认导出组件体独占 ~1825 行），~15 个渲染闭包定义在函数体内每次重建、无法稳定引用/单独 memo；164 处内联 `style={{`（违反项目设计法律）。**它不是 P1 的直接触发点，但让 P1 的外科级 memo 优化很脆弱**。第一刀不应先做大拆分，而应先做语义零变化的稳定化：父层 callback/inline prop 稳定、`ChatMessageItem` 提到模块顶层、`AgentChatSection` memo。拆成静态历史列表 / 活跃流式 run / 右栏运行时三块，是后续结构治理。

### L2 — 流式节奏控制（几乎完全缺失，对应 frontend P3 + backend 投递）🟠
- 前端零 pacing：全局 grep 无 throttle/typewriter，唯一 rAF 是滚动到底非合帧（`AgentDetail.tsx:1806, 1916`）。流畅度 **100% 外包给后端 chunk 节奏**。
- 后端投递层有隐患：每个 delta 在消费 provider 流的**同一协程里**串行 await 本进程投递 + `INCR`+`XADD`+`PUBLISH`（3 次 Redis 往返）+ 双跳 forwarder（`web_chat_stream_bus.py:60-69`；`web_chat_runtime.py:1066-1071`）。一条 500-token 回复 = 上千次串行 Redis 往返落在关键路径；Redis 抖动/跨区时**平滑生成被投递成一顿一顿的 burst**。

**对照**：Codex 四层（换行门控 + 两档迟滞变速 + 时间锚定动画 + 合并限流帧）把"生成节奏"和"显示节奏"解耦；CC 行门控 + 16ms 节流。我们直接**把生成节奏当显示节奏**，且后端还可能把它打成 burst。

### L3 — 思考呈现哲学（需 owner 拍板，对应 frontend P2）🟠
我们 `ThinkingDisclosure` 学了 Codex "提炼成一行" 的**形**，但：
- Codex 是**语义抽粗体标题**做现场旁白，我们是**机械截最后一行**跳变；
- 无换行长段退化成截断 blob 像卡死；
- 同一 thinking 有 headline + reasoning step 两套并存表示。

**这是产品哲学冲突，需 owner 定方向**：① 保 Codex 摘要式（则至少把机械截行升级成语义抽粗体标题、修 blob）；② 回到 CC 成段灰字流（`∴` 折叠 + 展开）；③ 两者可切。若 owner 心理基准是 CC 的成段灰字，现状会被直接判"做坏了"。

### L4 — 后端投递与可见性（次要但真实，对应 backend）🟡
- **长工具执行期无进度/心跳事件**：running → 静默 → done，30s 网搜/代码执行期间界面"冻住"（`engine.py` 全内核无 `tool_progress`）。CC/Codex 也转圈，但我们连转圈驱动源在长静默时都可能停。
- **部分 provider 思考被静默丢弃**：`<think>...</think>` 内联型 provider，`_filter_think_tags` 把它从正文剥掉但**不 re-emit 到 on_thinking**（`llm_client.py:764-766, 776-817`），这类 provider 思考对前端**完全不可见**。
- **工具参数不流式**：`input_json_delta` 只累加不逐块 emit（`llm_client.py:2151-2154`），看不到参数逐字生成（CC 同样，非硬伤）。

---

## 5. 修复方向与优先级

> 仅为方向建议，未实施。L1 是主因；实施顺序应先止住 remount/全量重算，再拆巨型组件。

**P0 — 重构流式渲染架构（对齐 CC/Codex 的"冻结历史 + 活跃区重画"）**
1. 先做原地稳定化：父层关键 handler `useCallback` 化，收敛 JSX inline prop，`ChatMessageItem` 从 render 内 `useMemo(()=>React.memo(...))` 提到模块顶层，`AgentChatSection` 包 `React.memo`。
2. `buildThreadTimeline` 用 `useMemo`，并把 checkpoint 前向扫描等 O(N²) 热点预计算一次。
3. 静态历史整体 memo + **稳定 key 用消息 id 而非 index**（修 frontend P5 remount），已完成消息不随 token delta 重渲（对齐 CC `shouldRenderStatically` + `React.memo` 短路）。
4. 再把"正在流式的活跃消息/run"作为**独立高频兄弟节点**渲染，与冻结历史解耦（对齐 CC "流式内容独立兄弟节点"）。
5. 最后拆 `AgentChatSection.tsx`：静态历史列表 / 活跃流式 run / 右栏运行时 三块各自 `memo`，并逐步迁走内联 style。

**P1 — 加客户端流式节奏层**
1. 前端 delta 微批合并 + rAF 合帧（16ms），可选两档迟滞变速（对齐 Codex chunking + CC 16ms 节流）。
2. 后端 runtime 侧 delta **微批 flush**（~16–33ms 或 N 字符），把 per-delta 3 次 Redis 往返降一个数量级——**降低投递开销而非降低粒度**，既保流畅又消背压。

**P2 — 思考呈现拍板（owner 决策）**
定方向后：若保摘要式，机械截行升级为语义抽粗体标题 + 修长段 blob；若回 CC 成段流，改造 ThinkingDisclosure 为可展开的成段灰字。消除 headline/reasoning-step 两套并存表示。

**P3 — 补齐可见性与状态 + 清理**
1. 长工具执行期加进度/心跳事件。
2. 修 `<think>` 标签型 provider 思考被丢弃（剥标签后 re-emit 到 on_thinking）。
3. 清理：死代码 `renderToolCall`、`_streaming` 裸 `(msg as any)` 字段类型化、164 处内联 style 迁 tokens。

---

## 附：四路调研证据索引

| 路 | subagent | 对象 | 关键结论 |
|---|---|---|---|
| A | codex-tui | `codex-rs/tui` | 双区域流式 + 换行门控 + 两档迟滞变速 + 时间锚定动画 + reasoning 提炼状态栏 + 合并限流帧 |
| B | cc-tui | `free-code-main` + `claude-code-org` | cell 级 diff（只吐变化字符）+ 三重冻结护栏 + 流式独立兄弟节点 + 平滑三层 + stall 变红 |
| C | hive-frontend | `frontend/` | P1 无节流+未memo+每帧全量重建（主因）/ P2 思考压一行 / P3 零客户端平滑 / 4547 行巨型组件是结构性障碍 |
| D | hive-backend | `backend/` | 后端逐 delta 无聚合、非瓶颈；投递层 per-delta Redis 背压 + 长工具无进度 + `<think>` 思考丢弃 |

**核心洞察**：CC 与 Codex 殊途同归——已完成内容冻结、只重画活跃区，让输出正比于变化量。我们相反地让每个 token 重算全部历史。修复的第一性问题不是"加动画/加节流"，而是**把渲染架构从"每帧全量重建"翻转成"冻结历史 + 活跃区增量"**，而这被 4547 行巨型组件挡着——所以拆组件是一切的前置。

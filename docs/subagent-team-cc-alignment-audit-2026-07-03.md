# Sub-Agent / Agent Team — CC 全面对齐审计与差异清单（2026-07-03）

**基线**：FreeCode TS runnable baseline `/Users/example-owner/vc-saas/free-code-main`（第一参考），claude-code-org 去混淆源码交叉验证（行号一致，同代码库）。
**对照物**：Hive `backend/app`（工作区当前内容，含未提交改动）。
**方法**：5 个并行研究 agent 全文精读 CC `src/tools/AgentTool/**` + `src/tasks/**` + Hive `agents/subagent.py` / `services/subagent_run_service.py` / `services/runtime_task_worker.py` 等，主 session 第一手复核关键文件与全部进报告的数字。所有论断带 `file:line`，区分 **Fact**（代码直证）/ **Inference**（据证据推断）。

---

## 0. 摘要 — owner 三判断的核实结论

| owner 判断 | 核实 | 铁证 |
|---|---|---|
| "恢复只有两个 Sub-Agent 支持" | **Fact 坐实** | `SUBAGENT_RESTART_REPLAY_SAFE_TYPES = frozenset({explorer, critic})`（`subagent_run_service.py:41`）。且这两个的"恢复"是**从零重跑（replay）**，不是断点续跑。默认类型 `general-purpose` 有写副作用 → 重启即 `needs_reconciliation`。 |
| "Worker 层实现有问题" | **原始 Fact 坐实；当前 P0 已修复** | 原始问题：`subagent` 不在 worker 的 `SUPPORTED_RUNTIME_TASK_TYPES` 且后台 subagent 跑在发起进程内。当前 checkout 已改为 `RuntimeTask(task_type="subagent")` 入队 + runtime worker claim/dispatch；dispatch 会重建 `SubagentSpawnContext` 的 model resolver / memory store / memory distiller / fork parent messages，并接入 cancel event。 |
| "Sub-Agent 基本不可用" | **原始判断部分成立；当前可用性已恢复到 P0 水平** | 前台同步 spawn 健全；后台 spawn 现在走 durable worker 且支持 explicit TaskStop cancel。仍未完成的是 transcript resume、前台同步 subagent 可续话地址、真实 worktree isolation、旧 `needs_reconciliation` 积压清理，以及 `DEFAULT_SUBAGENT_TOOL_ROUNDS = 8` 带来的复杂任务轮数偏低。 |

**一句话定性**：CC 的 sub-agent 是"一套统一 `runAgent()` 内核 + 两层生命周期外壳（subagent 跑完落盘按需 resume / teammate 常驻 mailbox 保活）"，**恢复是普适基础设施**。Hive 原始实现把语义拆成前台同步直跑 / 后台进程内 asyncio / Agent Team durable worker 三套；当前 P0 已把普通后台 subagent 归入 durable worker，但 **CC transcript resume / worktree isolation / foreground continuation** 仍是待补的语义差距。

### 0.1 当前 checkout 复核（2026-07-03 修复后）

本节是对本文档原始审计结论的 post-fix 校准。结论不要混淆：

- **已修复的是兼容、可见性与 P0 worker/cancel 载体层**：CC agent.md frontmatter 兼容、`subagent_type=<custom agent name>` 命名查找、background run 的完整 `SubagentSpec` 快照、普通 background subagent 入队为 `RuntimeTask(task_type="subagent")` 并由 runtime worker claim/dispatch、worker dispatch 恢复 `SubagentSpawnContext` 的 model resolver / memory store / memory distiller / fork parent messages、`model: inherit` 继承父模型、explicit TaskStop 通过 runtime control bus 设置 subagent cancel event、startup resume pump 将可恢复 subagent 改为 `resumable` 并唤醒 worker、mutating `general-purpose/worker` 在缺 transcript resume 前 fail-closed 到 `needs_reconciliation`、Agent Team member turn 完成后回到 `idle` 可续聊、Team task-list/lifecycle payload、Team member 子 session 可看到 Team workspace + parent Work Ledger shared tasks、Dynamic Workflow JSON Schema args 与 `{item:[...]}` array normalize、Session Runtime waiters UI。
- **仍未完成的是本文 P1/P2 语义层**：历史 `needs_reconciliation` 积压需要单独清理；restart 后仍是 replay/重跑式恢复，不是 CC 的 transcript resume；前台同步 subagent 仍不返回可续话地址；`worktree` isolation 仍只是 metadata/compatibility；`DEFAULT_SUBAGENT_TOOL_ROUNDS = 8` 仍低于 CC general-purpose 的实用上限。
- **D2 的局部变化**：新建 background subagent RuntimeTask 现在会保存完整 `subagent_spec` snapshot，resume 时不再只剩 `name/type/max_tool_rounds`；worker dispatch 也会恢复 spec 之外的 ctx 运行时依赖（model resolver / memory / fork parent messages）。但这只是修复“重跑时配置和 ctx 丢失”，不是修复“重跑 vs transcript 续跑”的根本差异。
- **D7 的局部变化**：定义文件 parser 已能接受 CC frontmatter 的 `isolation: worktree` 并保留到 `SubagentSpec`，但工具 schema 与运行时仍未提供真实 worktree 文件系统隔离；当前只能视为 metadata/compatibility，不可宣称 worktree isolation 已实现。
- **D13 的变化**：Agent Team 侧进一步补齐 CC teammate 语义，Team payload 暴露 shared task-list 与 lifecycle，member 单轮完成后记录 `last_turn_status` 并投影回 `idle`，可继续经 `send_agent_session_message` 投递下一轮。

所以本文的 P1/P2 路线仍然有效；本轮修复关闭了 tool/schema/UI、ctx state-loss、explicit cancel，以及“普通 background subagent 不经 worker”的 P0 架构 bug，但还没有替代 transcript resume、foreground continuation 和 worktree isolation 的后续返工。

### 0.2 Post-fix residuals（2026-07-03 落地补充）

1. **`general-purpose` fail-closed 的短期 retry 出口已补；长期仍是 P1 transcript resume**：移除 `spawn_intent_recorded` 对 mutating subagent 的自动放行是正确的安全修复。当前 checkout 已让 `build_restart_reconciliation_metadata()` 在 `task_type="subagent"` 且 blocker 为 `non_idempotent_subagent_type` 时写入 `reconciliation_retry_allowed=true` 与 `runtime_reconciliation_retry_contract.v1`；admin retry 写入 `reconciliation_status=retry_requested` 后，`dispatch_persisted_subagent_run()` 会验证并消费该 contract，允许整段 `restart_from_prompt` 重启。因此后续默认 `general-purpose` / legacy `worker` restart fail-closed item 不再只能 archive；`child_pending_tool_frame_not_replay_safe` 仍不会被打开 retry。剩余工作是 P1 transcript resume 和历史 `needs_reconciliation` 积压清理/回填。
2. **explicit cancel early-registration race 已补**：`apply_remote_subagent_cancel()` 对尚未注册本进程 `asyncio.Event` 的 run 记录 pending cancel latch，并在 `_subagent_cancel_event_for_run()`/test registration 时补触发；release/unregister 会清掉 pending，避免 stale cancel 影响后续同 key 测试或重入。
3. **cleanup 已补**：`_restart_replay_has_spawn_intent()` 已删除；fail-closed 判据不再保留 spawn-intent 放行旁路。

---

## 第一部分 — CC Sub-Agent 机制完整解剖

### 1.1 模式全集（问题 a）

`Agent` 工具（legacy 别名 `Task`）`isReadOnly=true`、`isConcurrencySafe=true`（`AgentTool.tsx:1264,1273`）。`call()` 把每次调用翻译成**三条互斥大路**（`AgentTool.tsx:239`）：

| 路径 | 触发条件 | 载体 | 证据 |
|---|---|---|---|
| **① 同进程 subagent** | 默认 | sync 前台 / async 后台，同一个 `runAgent()`→`query()` | `AgentTool.tsx:686`(async)/`:765`(sync) |
| **② Teammate spawn** | `team_name && name` 同时提供 | tmux / iTerm2 pane / in-process 三后端，长循环 mailbox | `AgentTool.tsx:284` |
| **③ Remote CCR** | `isolation === 'remote'`（仅 `USER_TYPE=ant`） | 远程环境，恒后台 | `AgentTool.tsx:435` |

**subagent 的 sync vs async**（`shouldRunAsync`，`AgentTool.tsx:567`）= `run_in_background===true || selectedAgent.background===true || isCoordinator || forceAsync(FORK 开) || assistantForceAsync(KAIROS) || proactiveActive`，且 `!isBackgroundTasksDisabled`。

**五个正交维度**（都是**参数或 agent 定义**，可自由组合）：
1. **前台/后台**：`run_in_background` 布尔。
2. **isolation（文件系统隔离）**：`worktree`（临时 git worktree 隔离副本，无改动自动删、有改动保留并回传 path/branch，`AgentTool.tsx:590,667-681`）/ `remote`（CCR，ant-only）/ 无（共享父 cwd）。`cwd` 参数可显式指定，与 worktree 互斥。
3. **subagent_type**：选专门 agent；省略 → general-purpose（fork 实验开则 → fork 路，`AgentTool.tsx:322`）。
4. **model / effort**：`model` 参数 > agent 定义 model > 父继承；`effort` 由 agent 定义 frontmatter 定。
5. **并行**：单条 assistant 消息放多个 Agent tool_use block（`prompt.ts:248,271`），concurrency-safe 被并发调度。

**中途甩后台**（关键，`AgentTool.tsx:873-950`）：sync agent 跑过 2s 显 BackgroundHint，用户可随时转后台——`Promise.race([nextMessage, backgroundSignal])`（`:886`），转后台则迭代器 `.return()` 优雅关闭切 async lifecycle 继续。**同步与后台不是 spawn 时定死的二选一，是可动态转换的连续态。**

**fork（隐式继承路径，`forkSubagent.ts`）**：feature-gate `FORK_SUBAGENT`；省略 `subagent_type` → 隐式 fork。`FORK_AGENT = {tools:['*'], maxTurns:200, model:'inherit', permissionMode:'bubble'}`（`forkSubagent.ts:60`）。**继承父完整对话上下文 + 父 system prompt 字节 + 父精确工具池**（全部为 prompt cache 共享设计）；禁递归 fork；全部强制后台统一 task-notification。

### 1.2 创建 + 运行 + 交互（问题 b）

**创建原子步骤**（`runAgent.ts:248`）：
model 二次解析 → 生成 agentId → **`initialMessages = [...contextMessages, ...promptMessages]`，非 fork 的 `contextMessages=[]`（`:370`，零对话上下文）** → userContext(CLAUDE.md)/systemContext(gitStatus)（read-only agent 省 CLAUDE.md + gitStatus 省 token，`:390,404`）→ 覆盖 permissionMode/effort/allowedTools 的闭包 → resolveAgentTools → agentSystemPrompt → abortController（**async 独立新建 / sync 共享父**，`:524`）→ **SubagentStart hooks 注入 context（`:532`）** → frontmatter hooks 注册 → skills 预加载（`:578`）→ **agent 专属 MCP servers additive（`:648`，结束清理）** → thinkingConfig（**普通 subagent 强制关思考省 token**，fork 继承父，`:682`）→ record sidechain transcript → `query()` 循环，`maxTurns = 传入 ?? agentDefinition.maxTurns`（`:756`）。

**subagent system prompt 构成**（CC replace 语义，非叠加）= `agentDefinition.getSystemPrompt()`（built-in 常量 / custom·plugin 的 `.md` 正文）经 `enhanceSystemPromptWithEnvDetails`（cwd/platform/date/model）；有 memory 则尾附 memory prompt。**不含父 system prompt。**

**上下文传递对比（这是最核心的设计边界）**：

| 上下文 | 普通 subagent | fork |
|---|---|---|
| 父对话历史 | **不传** | **全传** |
| 父 system prompt | 不传 | 传（字节复刻，`AgentTool.tsx:496`） |
| CLAUDE.md | 传（除 omitClaudeMd） | 传 |
| readFileState | 新建空 | 克隆父 |
| 父工具池 | 不传（自己 assembleToolPool） | 传（exact，`useExactTools`） |
| prompt | user message | fork directive + 父 assistant 克隆 + placeholder tool_results |

**普通 subagent = 零对话上下文的新同事**（`prompt.ts:103`），故 prompt 描述反复要求写足背景。**fork 才是继承路径。**

**运行中与主 agent 交互**：
- **sync progress**：每 yield 一条消息，`onProgress` 转发 tool_use/tool_result 为 `agent_progress` 事件（`AgentTool.tsx:1104`）；token 累加父 spinner；父 LLM 循环**阻塞等子完成**。
- **async progress**：写 AppState task，父**不实时看中间输出**（`prompt.ts:263` 明令 do NOT sleep/poll），可选读 output_file（symlink 到 transcript）。

### 1.3 返回 + 恢复 + 续话（问题 c）

**同步 tool result 结构**（`finalizeAgentTool`，`agentToolUtils.ts:276-355`）= `{agentId, agentType, content, totalDurationMs, totalTokens, totalToolUseCount, usage}`。`content` = **最后一条 assistant message 的 text blocks**（末条纯 tool_use 则回退到最近含 text 的），即 **result 是 final text 非结构化**。给 LLM 的三段式：`子文本 + "agentId: X (use SendMessage with to:'X' to continue this agent)" + <usage>`（`AgentTool.tsx:1343`）。**one-shot 内建（Explore/Plan）例外**：`ONE_SHOT_BUILTIN_AGENT_TYPES`（`constants.ts:9-12`）砍掉 agentId/usage 尾巴（省 token，父永不续它们，`AgentTool.tsx:1356`）。

**异步返回** = `{agentId, description, prompt, outputFile, canReadOutputFile}`（`AgentTool.tsx:754`），文案含 "Use SendMessage with to:'X' to continue... You will be notified automatically when it completes."

**后台完成通知**（`enqueueAgentNotification`，`agentToolUtils.ts:624`）：构造 XML `<task-notification>`（task_id/output_file/status/summary/`<result>`finalMessage/`<usage>`/`<worktree>`），入全局单例队列，父 loop 把它当作 **user-role 消息** drain（用 `<task-notification>` 开标签区分）。completed/killed/failed 三态均发。`notified` flag 原子 check-and-set 去重。

**恢复（最关键，`resumeAgent.ts:42` + `SendMessageTool.ts:800`）**：

CC 的 resume **零 per-type 门控、零 capability flag**（通读 `resumeAgent.ts` 无任何类型白名单）。统一入口 = `SendMessage(to: name/id)`，三段式路由：
- **running** → `queuePendingMessage`（内存续话，下一 tool-round 边界注入，`SendMessageTool.ts:809`）。
- **stopped/killed** → `resumeAgentBackground`（`:822`）。
- **已从 state 逐出（evicted）** → 从**磁盘 transcript** resume（`:846`）。

`resumeAgentBackground` 全过程：读 `subagents/agent-<id>.jsonl` transcript + `.meta.json` → 清洗（去未决 tool_use / 孤儿 thinking / 空白 assistant）→ 重建 content-replacement 预算 → 恢复 worktree cwd（bump mtime 防清理，外部已删回退父 cwd）→ agent 类型路由（**找不到降级 general-purpose**）→ **`promptMessages = [...resumedMessages, createUserMessage(prompt)]`**（`:168`，旧转录重建 + 追加新 user 消息）→ 全新 `runAgent()` 循环（**总是后台异步**）。

**"不可续"在 CC 里不是 agent 内在终态属性，而是外部条件**：① transcript 文件可达（`/clear` 换 sessionId 或删文件 → 不可达）；② agentId 已知（Explore/Plan 故意不吐 ID → 父无从续）；③ teammate 裸名过不了 `toAgentId` 格式校验。**只要文件在、ID 知道，killed 的都能复活。** 进程重启后 transcript 在磁盘存活，`--resume/--continue` 恢复 sessionId 后仍可续。

**agentMemory（与 resume 正交的另一层，`agentMemory.ts`）**：按 **agentType** 键（同类型所有实例共享），三 scope（user/project/local），spawn 时注入 system prompt。这是"这类 agent 学到的东西"，resume 是"这个 agentId 对话的历史"——**两层必须分清**：Hive 的 T3/记忆.md ≈ agentMemory 层；会话续接 ≈ transcript resume 层。

### 1.4 工具逻辑（问题 d）

worker 工具池用**自己的 permissionMode**（agent 定义 > 默认 acceptEdits）从头 `assembleToolPool`，**不受父限制**（`AgentTool.tsx:573`）。经 `resolveAgentTools`（`agentToolUtils.ts:122`）三层过滤：
1. **`filterToolsForAgent`**：MCP 全放行 / `ALL_AGENT_DISALLOWED_TOOLS` 剔除 / 非 built-in 再剔 CUSTOM / **async 只留 `ASYNC_AGENT_ALLOWED_TOOLS` 白名单**。
2. agent 定义 `disallowedTools` 剔除。
3. wildcard（`undefined`/`['*']`）放全部，否则白名单逐个解析。

**默认排除**（`constants/tools.ts:36`）：TaskOutput / ExitPlanMode / EnterPlanMode / AskUserQuestion / TaskStop / Workflow **+ Agent 工具本身（防 subagent 再 spawn Agent 递归，仅 `USER_TYPE=ant` 放行嵌套）**。
**async 白名单**（`:55`）：Read/WebSearch/TodoWrite/Grep/WebFetch/Glob/Shell/Edit/Write/NotebookEdit/Skill/SyntheticOutput/ToolSearch/EnterWorktree/ExitWorktree。
**in-process teammate 特例**（`:77`）：额外放行 TaskCreate/Get/List/Update + SendMessage + cron，且可 spawn **同步** subagent。

**model 解析**（`model/agent.ts:37`）：`env CLAUDE_CODE_SUBAGENT_MODEL > 工具 model 入参 > agent 定义 model > 默认 inherit`。inherit → 父主循环模型；**bare alias 匹配父 tier 时继承父精确模型串防降级**（`:110`）。

**并发**：`CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY || 10`（同 turn 最多 10 个 sync 并跑）。**async spawn 瞬返不占槽 → 后台运行数不受 10 限制，且无全局后台 agent 硬上限**（LocalAgentTask 无 semaphore）。**maxTurns**：general-purpose **无上限**、fork=200、custom 可 frontmatter 配。**cancel**：sync 共享父 controller（ESC 连带取消）；async 独立 unlinked controller，ESC 不杀，只能 `chat:killAgents`/TaskStop 显式杀。

---

## 第二部分 — CC Agent Team 与容器化（问题 2）

### 2.1 "容器"的本质 = Task

**容器不是进程，是 `Task`**——受 `AppState.tasks` 管的执行上下文。`Task.ts:72-76` 基类只有 `{name, type, kill()}`；状态全在 `TaskStateBase`。7 类 TaskType（`Task.ts:6-13`）：`local_bash / local_agent / remote_agent / in_process_teammate / local_workflow / monitor_mcp / dream`，前缀 `b/a/r/t/w/m/d`。差异三轴 = 进程内 vs 独立进程 vs 远程 / 一次性 vs 保活 / 输出是否回流。

**关键区分**：`local_agent`（一次性 subagent，跑完终止，可 resume）≠ `in_process_teammate`（长生命周期 teammate，idle 循环 + mailbox 保活，不 resume）。

### 2.2 Team ≠ 容器，是协调壳

**TeamCreate 只建壳**（`TeamCreateTool.ts:128-237`）：单团约束 → 写 `~/.claude/teams/{team}/config.json`（注册表）→ 建 `~/.claude/tasks/{team}/`（**Team 与 TaskList 1:1**）→ leader 登记进 `AppState.teamContext`。**teammate 不是 TeamCreate spawn 的，是 Agent tool 带 `team_name`+`name` spawn 的**；agentId = `name@teamName`。

### 2.3 "进入容器"三后端（`spawnMultiAgent.ts:1040-1093`）

| 后端 | 本质 | 身份 | 初始 prompt |
|---|---|---|---|
| **in-process**（默认，非交互强制） | 主 Node 进程内，AsyncLocalStorage 隔离，剥空父对话 | 内存 | 直传（不走邮箱） |
| **tmux / iTerm2 pane** | 真 spawn 独立 CC 进程 | CLI 参数 `--agent-id` | 经文件邮箱投递 |

探测优先级：在 tmux 内→tmux；iTerm2+it2→iterm2；否则 tmux external；**非交互(-p)强制 in-process**。注：tmux teammate 也登记为 `in_process_teammate` 类型 task，靠 `backendType` 区分——类型名是"teammate task"统称。

### 2.4 切换与交互

**焦点切换 = 纯 UI**（`enterTeammateView` 设 `viewingAgentTaskId`），**不影响后台执行**。

**消息 → LLM turn 两条路**：
- **(A) leader/tmux**：`useInboxPoller` 1s 轮询文件邮箱（in-process 跳过因共享 AppState）——对方 **IDLE 立即成新 turn**，**BUSY 入队闲时投递**，**投递成功才标已读防丢**。
- **(B) in-process teammate**：`waitForNextPromptOrShutdown` 500ms 循环，优先级：`pendingUserMessages > shutdown > team-lead 消息 > FIFO > 认领共享任务`。

**一轮循环用与 subagent 相同的 `runAgent()`**，双 abortController（Escape 停本轮 vs lifecycle 杀整个 teammate）。

**两个硬边界**：① **teammate 输出不自动回流 leader**（`inProcessRunner.ts:1328`，防上下文被噪声淹没，必须显式 SendMessage）；② `in_process_teammate` **不在 `getAllTasks()`**（`tasks.ts:22`）→ 通用 TaskStop 停不了 teammate，只能走 shutdown 双向握手协议或 `InProcessBackend.kill`。

**约束**：teammate 不能生 teammate（花名册扁平）；in-process teammate 不能生后台 agent（只能生同步 subagent）。

### 2.5 Team vs 普通后台 Agent 的本质区别

同一入口 `AgentTool.tsx:284`：`teamName && name` → teammate，否则 subagent。**判据 = 是否需保活 + 相互协调（非任务大小）**：
- **background agent**：一次性 / 结果回父 / 可 resume / 可递归。
- **teammate**：长循环保活 / 文件邮箱自动成 turn / 输出不回流 / peer 互通 / 禁递归。

---

## 第三部分 — Hive 现状 vs CC 差异清单（核心交付）

> 差异按维度编号 D1-D16。每项：**CC 语义 / Hive 现状 / 差距性质 / 影响**。
> 注意 CCPlus Boundary Contract：CC 是单进程 CLI，其"进程内 asyncio task"实现在 CC 里合理；Hive 是双进程服务端，对齐目标是 **CC 的语义边界 + Hive 自己的 durable worker 载体**，不是照搬 CC 的进程内实现。

### A 组 — 恢复 / 续话（owner 命门①）

**D1｜恢复能力被做成类型内在属性（应为普适基础设施）**
- CC：`resumeAgentBackground` 零 per-type 门控，凡有 `agent-<id>.jsonl` transcript 皆可续（`resumeAgent.ts` 通读无白名单）。
- Hive：`SUBAGENT_RESTART_REPLAY_SAFE_TYPES = frozenset({explorer, critic})`（`subagent_run_service.py:41`）——只有两个只读类型。默认 `general-purpose`（mutating）重启即 `needs_reconciliation`。
- 性质：**根本语义倒置**。CC 把"可恢复性"做成普适底座（transcript 持久化 + SendMessage 统一入口），gating 只在"是否 surface agentId"层面；Hive 把它做成了类型能力开关。
- 影响：绝大多数 subagent（默认类型）永远不可恢复。这正是 owner 说的"只有两个"。

**D2｜Hive 的"恢复"是从零重跑（replay），不是续跑（resume-from-transcript）**
- CC：读回磁盘 transcript 重建完整消息数组 + append 新 user 消息 + 全新 query loop（`resumeAgent.ts:168`）。**子代的全部历史进上下文。**
- Hive：原审计时 resume 用 `SubagentSpec(name, type, max_tool_rounds)`（无 system_prompt、无 memory）+ 原始 `record["prompt"]`，`run_in_background=True` **从头重跑整个任务**（`subagent_run_service.py:765-798`）。当前 checkout 已补 `subagent_spec` 完整快照，新记录 resume 不再丢 system prompt/tools/model/frontmatter metadata；但执行语义仍是 **replay/重跑**，不是 transcript resume。**子代 T0/进度仍不重建为上下文。**
- 性质：**命名误导 + 能力缺失**。"restart_replay" 命名即实情——对只读 explorer 幂等安全但浪费；对 mutating 会重复副作用故被拦。
- 影响：即便"可恢复"的两个类型，也只是带完整 spec 的重跑，不是续接。Hive 仍缺 CC 的"transcript 重放式续话"这一整块基础设施。

**D3｜续话入口语义不同**
- CC：`SendMessage(to: name/id)` 统一入口，三段式自动路由（running 排队 / stopped resume / evicted 磁盘 resume），调用方无需区分状态。
- Hive：`send_agent_session_message`（`subagent.py:742`）走 mailbox continuation（`continue_agent_session_from_mailbox`），依赖 `child_session_id`，且后台 spawn 明确 `interrupt_supported: false`（`subagent.py:634`）。前台同步 spawn 返回**不带任何续话地址**（`subagent.py:647-661`）——同步 subagent 完全不可续。
- 性质：**部分接线**。Hive 有 mailbox 续话骨架，但前台 subagent 无续话、后台不支持 interrupt、无"凡有 transcript 皆可续"的统一语义。

### B 组 — Worker 层 / 执行载体（owner 命门②）

**D4｜后台 subagent durable worker 载体（P0 已修复）**
- CC：单进程 CLI，后台 subagent = 进程内 AppState task（对 CC 合理，无跨进程问题）。
- 原始 Hive：**双进程服务端**，后台 subagent 跑 `asyncio.create_task(_run_and_signal())` 在发起进程内，被 `_BACKGROUND_TASKS` set 强引用。`subagent` 不在 `SUPPORTED_RUNTIME_TASK_TYPES`，worker 根本不 claim。RuntimeTask 记录纯粹是"崩溃→对账"台账，不是执行调度单元。
- 当前 checkout：`subagent` 已加入 runtime worker supported task types；`spawn_subagent(run_in_background=true)` 工具路径只创建 `pending` RuntimeTask 并唤醒 worker；worker claim 后调用 `dispatch_persisted_subagent_run()` 恢复完整 `SubagentSpec` snapshot 与 `SubagentSpawnContext` runtime deps（model resolver / memory store / memory distiller / fork parent messages）并执行；startup resume pump 将安全记录改为 `resumable`，不再在 startup 进程里直接 spawn。
- 性质：**P0 架构层已修复**。普通 background subagent 与 `delegation` / `team_member` 进入同一 durable worker claim 面。剩余问题是 transcript resume 与 worktree isolation，不再是“执行载体选错层”。
- 影响：新建 background subagent 不再因发起 API 进程退出而必然丢执行载体；旧 `needs_reconciliation` 积压仍需单独清理/重试策略。

**D5｜explicit cancel 全链（P0 已修复）**
- CC：async agent 独立 abortController，`chat:killAgents`/TaskStop 有真实 cancel 通道。
- 原始 Hive：`_BACKGROUND_TASKS` 只被 add/discard，全仓无任何 `.cancel()` 调用；DB 驱动的 killed 状态无通道触达运行中的 subagent。
- 当前 checkout：`task_stop` 对 `task_type="subagent"` 的 RuntimeTask 置 `killed` 后发布 `subagent_cancel` runtime-control event；worker dispatch 为每个 subagent run 注册 `asyncio.Event` 并传入 `AgentInvocationRequest.cancel_event`，kernel/tool loop 能收到 explicit cancel；完成路径若 event 已置位则保持 `killed`，不会被 completer 覆盖成 `completed`。若 control bus cancel 在本进程 event 注册前到达，会先进入 pending cancel latch，并在 dispatch 注册 event 时补触发。
- 性质：**P0 explicit cancel 已修复**。这对齐 CC 的 TaskStop/killAgents 显式停后台 agent；父 turn ESC/浏览器断开是否联动后台 subagent 仍应按 CC async 独立 controller 语义另行定义，不应误杀后台任务。
- 影响：后台 subagent 不再只能等自然结束；跨进程 stop 有 runtime control bus 入口，且 early-registration race 已由 pending latch 覆盖。

**D6｜needs_reconciliation 死胡同 → 514 积压单调累积**
- CC：无对应物（靠磁盘 transcript resume，不需要 reconciliation 层）。
- 原始 Hive 机理链：① 进程重启丢 in-flight 后台 subagent，durable RuntimeTask 仍 `running`；② 启动 `resume_persisted_subagent_runs(limit=50)`：explorer/critic 重跑，mutating（**默认类型**）→ `needs_reconciliation`；③ 溢出 limit 的被 `reconcile_orphaned_runtime_tasks` 兜（subagent 不在 `_RESTART_RESUMABLE_TASK_TYPES`）→ 全进 needs_reconciliation；④ **`reconciliation_retry_allowed` 字段全仓从不被任何代码 set**（`runtime_reconciliation.py:59,192` 只读不写）→ admin retry 永抛 `RuntimeReconciliationConflict`，只能 archive；⑤ 即便 retry 成功设回 pending，worker 也不 claim subagent 类型 → 死循环。
- 当前 checkout：新建/可安全 replay 的 background subagent 会经专用 `resume_persisted_subagent_runs()` 进入 `resumable` 并唤醒 runtime worker；mutating `general-purpose/worker` 在没有 transcript resume 前明确 fail-closed 到 `needs_reconciliation`，不再靠只读 last frame 误判放行。对 `non_idempotent_subagent_type` blocker，metadata builder 会写入 audited retry contract，admin retry 后 dispatch 会验证并消费该 contract 再执行；对正在运行的 mutating child tool frame blocker，retry 仍 fail-closed。通用 orphan reconcile 仍会把未被专用 pump 接住的 subagent 置 `needs_reconciliation`，历史积压仍需单独清理/回填。
- 性质：**新任务 P0 出口已补；历史积压和 transcript-resume 缺口仍在。** fail-closed 回归是正确安全选择，短期 retry writer + dispatch consumer 已避免后续默认类型 item 只能 archive。
- 影响：后台 subagent 不再因为“worker 不 claim”形成新死循环；默认 `general-purpose` / legacy `worker` restart 仍会先进入 `needs_reconciliation`，但后续新 item 可经管理员审计 retry 回到 `pending` 并由 worker 消费 retry contract 后执行。旧 514 类积压不会自动消失，除非做一次专门 backfill/清理。

### C 组 — 模式 / 创建 / 上下文边界

**D7｜`isolation` 参数语义被劫持（两个正交概念合并）**
- CC：`isolation` = **文件系统隔离**（`worktree`/`remote`）；上下文继承是独立的 fork 路径（省略 subagent_type）。
- Hive：工具 schema 层 `isolation ∈ {none, all}`（`subagent.py:201-209`）= **上下文继承**（`all` = fork 父会话，`none` = 全新）。当前 checkout 的 definition parser 已接受 `isolation: worktree` 作为 CC frontmatter 兼容 metadata，但低层运行时没有创建 worktree，也没有文件系统隔离。Hive 的 isolation 仍承担了 CC 的 fork 语义，而 CC 的 worktree 文件隔离**在 Hive 缺失**。
- 性质：**概念合并 + 能力缺失**。Hive 用一个参数名装了 CC 的 fork 概念，同时丢了 CC 的 worktree 文件隔离（对服务端多租户尤其有价值——并行 mutating subagent 无冲突副本）。
- 影响：命名混淆；无文件系统隔离 → 并行写 subagent 会互相踩（Hive 目前靠"输出只回 digest"缓解，但工作区仍共享）。

**D8｜maxTurns 默认 8，低一个数量级**
- CC：general-purpose **无上限**，fork=200，主循环 200。
- Hive：`DEFAULT_SUBAGENT_TOOL_ROUNDS = 8`（`agents/subagent.py:134`），handler 默认亦 8（`subagent.py:219`）。对比 Hive 主 agent `max_tool_rounds` 默认 200。
- 性质：**参数取值失衡**。
- 影响：subagent 8 轮工具就被截断，稍复杂的探索/编辑任务干不完 → "基本不可用"的直接体感来源之一。

**D9｜无 sync→async 中途甩后台**
- CC：`Promise.race([nextMessage, backgroundSignal])` + BackgroundHint，sync agent 可随时转后台。
- Hive：spawn 时 `run_in_background` 定死，无中途转换。
- 性质：**能力缺失**（非致命，但影响长任务体感）。

**D10｜默认上下文策略与 CC 相反的风险**
- CC：**默认 subagent 零对话上下文**（刻意省 token + 独立判断），fork 才继承。
- Hive：`isolation` 默认逻辑（`subagent.py:270`）——前台无显式类型时默认 `all`（继承父），后台默认 `none`。
- 性质：**部分对齐但默认值偏离**。前台默认灌父上下文与 CC 的"零上下文新同事"哲学相反。需确认这是有意的 Hive delta 还是漂移。

### D 组 — 工具 / 模型（已基本对齐，列出供核对）

**D11｜工具三层过滤 — 方向对齐**
- Hive 有 `DELEGATED_WORKER_BASE_EXCLUDED_TOOLS`（禁递归 spawn/delegation）+ 类型 preset + 空 allow-list 回落（`resolve_subagent_tools:427`）+ 深度上限 `DEFAULT_MAX_SUBAGENT_DEPTH=2`。方向与 CC 一致。**待核对**：Hive 的后台白名单是否与 CC `ASYNC_AGENT_ALLOWED_TOOLS` 一致；permission_profile.allowed_tools 硬限是否等价 CC 的 allowedTools 替换语义。

**D12｜model inherit 防降级 — 需对标**
- CC：bare-alias 匹配父 tier 时继承父精确模型串防降级（`model/agent.ts:110`）。
- Hive：`_resolve_child_model`（`agents/subagent.py:448`）直接用 `ctx.model`（父模型）或 resolver 解析 override。**Hive 曾出跨租户模型引用事故**（memory: web3 研究员 outage）——CC 的"精确串继承"防降级值得对标。

### E 组 — Agent Team（Hive 反而更成熟，列出供全局判断）

**D13｜Team teammate 走 durable worker（比普通 subagent 成熟）**
- Hive：`spawn_subagent` 的 `team_name+name` 分支走 `spawn_agent_team_member_from_tool_request`，teammate 是 `team_member` 任务类型——**在 SUPPORTED 且在 RESTART_RESUMABLE**。即 Agent Team teammate 是 durable、可 worker claim、可重启恢复的一等公民。
- 当前 checkout 追加修复：Team payload 已暴露 shared task-list/lifecycle，member turn 完成后记录 `last_turn_status` 并回到 `idle`；Team member 子 session 现在也能解析回所属 Team，并读取 parent session Work Ledger 里的 open todos，以 `owner=member_name` 作为 shared task-list 可见性契约。
- 反常识点：Hive 的 Team 路径仍是 durable worker 成熟路径；普通 subagent 本轮已复用该 worker 基建补齐 P0 执行载体差距。

**D14｜CC 三容器后端 vs Hive**
- CC：tmux / iTerm2 / in-process 三后端（终端 CLI 特性）。
- Hive：服务端无终端 pane 概念，对标物是 RuntimeTask（durable 执行上下文）。这是合理的 Hive-native 映射（CCPlus：本地 CLI 的 pane 语义映射到 Hive 的 RuntimeTask/Session），**非缺陷**。

**D15｜teammate 输出不回流 — 已对齐**
- CC：teammate 输出不自动回流 leader（防噪声淹没）。
- Hive：delegation/subagent result distillation 保持了这个边界（`agents/subagent.py:376-381` 只回 digest）。**已对齐。**

**D16｜消息→turn 自动唤起 — 部分对齐但有失控**
- CC：双消费路径（IDLE 立即成 turn / BUSY 排队闲时投递，投递成功才标已读防丢）+ 单团约束 + isActive/idle 控频。
- Hive：mailbox 唤起对标此，但已知 **716 web_chat_turn failed（无模型 agent 被 task_notification/mailbox 持续唤起）**——缺 CC 的"无模型熔断 / idle 控频"护栏。

---

## 第四部分 — 死代码 / 结构性问题（审计副产品）

1. **死代码**：`consume_subagent_signals`（`agents/subagent.py:1123`）无任何调用者（真消费走独立的 `subagent_wake_consumer.py` 读 DB）。切口④旧设计残留。
2. **双唤醒路径信号滞留**：后台完成有直接唤醒（`_wake_parent_session_from_subagent_completion`）+ DB Signal daemon 两条路。正常路径下直接唤醒先建父 active run，daemon 的 `_parent_has_active_run` 门会跳过，**留未消费 CoordinationSignal 悬在表里**。
3. **并发预算配置断链**：`_background_subagent_semaphore`（`agents/subagent.py:1088`）想读 `RUNTIME_TASK_WORKER_TASK_TYPE_LIMITS` 的 `subagent=`，但 `_parse_task_type_limits` 因 `subagent not in SUPPORTED_RUNTIME_TASK_TYPES` **跳过该配置**，只能回落默认 8。
4. **Gateway 模式依赖**：Signal 唤醒正确性依赖 gateway 处于 Postgres 模式（默认是，`coordination_wiring.py:13`）；若 in-memory gateway 被启用则后台完成唤醒静默失效，无护栏。

---

## 第五部分 — 对齐路线建议（供 owner 决策，未实施）

> 完整交付纪律：任何返工一次改完、禁 MVP、零债。以下是**建议的完整范围**，待 owner 拍板后一次性落地。

**P0 — Worker 层归位（D4/D5 已闭合；D6 仍需历史清理）**
把 `subagent` 提升为 RuntimeTask worker 一等公民，复用 `team_member`/`delegation` 已跑通的 durable 基建：
- 加入 `SUPPORTED_RUNTIME_TASK_TYPES` + `_dispatch_claimed_task` 分支 + `_RESTART_RESUMABLE_TASK_TYPES`。
- 后台 spawn 从 `asyncio.create_task` 改为 worker claim（durable、跨进程、可 cancel）。**已完成。**
- 接通 cancel 通道（DB killed status → runtime control bus → subagent cancel_event，对标 delegation/web chat cancel）。**已完成。**
- 补 explicit cancel 的 early-dispatch race：为 miss 的 control-bus cancel 记录 pending-cancel latch，注册 event 时补触发。**已完成。**
- 修 `reconciliation_retry_allowed` 死胡同：`non_idempotent_subagent_type` subagent reconciliation 写入 audited retry contract，admin retry 后由 worker dispatch 验证并消费；仍需清理/回填 514 类历史积压。

**P1 — 恢复语义归位（解 D1/D2/D3）**
- 去掉 `SUBAGENT_RESTART_REPLAY_SAFE_TYPES` 类型白名单，改为"凡有 T0 transcript 皆可续"的普适能力。
- resume 从"从零重跑"改为"transcript 重放式续话"（读回子 T0 → 重建消息 + append 新 prompt），对标 CC `resumeAgent.ts:168`。gating 移到"是否 surface 续话地址"层面（对标 CC one-shot 不吐 agentId）。
- 前台同步 subagent 也返回续话地址（当前完全不可续）。

**P2 — 参数 / 概念归位（解 D7/D8/D9/D10）**
- `max_tool_rounds` 默认从 8 提到与任务复杂度匹配的值（对标 CC fork=200 / 无上限）。
- 拆分 `isolation`：恢复 CC 语义的 `worktree` 文件隔离（服务端多租户价值高）；上下文继承用独立的 fork 概念表达，不复用 isolation 参数名。
- 补 sync→async 中途甩后台。
- 确认默认上下文策略（前台默认 fork 父上下文是否有意 delta）。

**P3 — 护栏 / 清理（解 D16 + 死代码）**
- mailbox 唤起加无模型熔断 + idle 控频（对标 CC）。
- 删 `consume_subagent_signals` 死代码；修双唤醒信号滞留；修并发预算配置断链。`_restart_replay_has_spawn_intent()` 已删除。

---

## 附：证据来源

- FreeCode 第一手：`Task.ts` / `tasks/types.ts` / `resumeAgent.ts` / `forkSubagent.ts` / `InProcessTeammateTask/types.ts` / `agentToolUtils.ts` / `constants/tools.ts` / `AgentTool.tsx`(关键段) / `runAgent.ts`(核心循环)。
- 5 研究 agent 报告：cc-agenttool（AgentTool 核心）、cc-resume（返回/恢复/fork）、cc-team（Team+Task 容器）、cc-org-xcheck（claude-code-org 交叉验证，行号一致）、hive-audit（Hive 现状）。
- Hive 第一手复核：`agents/subagent.py:55-58,133-134` / `services/subagent_run_service.py:41` / `services/runtime_task_worker.py:21-29` / `services/runtime_task_service.py:16-22`。

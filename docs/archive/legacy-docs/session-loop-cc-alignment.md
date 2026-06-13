# Session Loop 全流程梳理 × CC 对标 × 病灶定位

> 状态：**诊断梳理稿（不含方案）**。目的是把"一次 agent 执行从开始到结束的完整 Loop"按 Claude Code(CC) 基线逐环节摊开，标出哪里已对齐、哪里是机械逻辑/双管道，为下一步方案讨论提供事实地基。
>
> 方法：4 路并行代码调研 + 关键链路第一手验证。每条断言带 `file:line`。
>
> 一句话结论：**底座 Loop（新开 Session → 循环 → 工具 → 输出）已统一、且基本对齐 CC 的 ReAct——这是好消息，地基是稳的。所有"没法用"的体感都集中在底座之上的"模式叠加层"：需要智能的环节（要不要规划、怎么管 Task、计划怎么交接给执行）被正则/模板/结构化字段机械化了，违反 AI-Native L1；同一件事存在语义悬殊的双管道。**

---

## 0. 用户的 Loop 心智模型（本文件的组织骨架）

```
1. Session 开始 → 上下文组装（Memory 范畴：soul + memory + skills）
2. 把用户的 Prompt 塞进去
3. 由 LLM 判断是否进入 Plan Mode（或用户显式要求进入）
4. 调用工具 → Running → 输出结果 → 形成反馈
   └─ Task = 模型判断需要 multi-agent / 长任务时的"看板"（working memory）
```

核心原则（最高法律）：**MD-first / AI-Native L1** —— 需要智能的步骤交给 LLM 全能力发挥；机械/字符串处理只能是失败兜底，绝不能是主路径。

下面逐环节对标。

---

## 1. 底座 Loop（最简单的 Session 流程）—— ✅ 已统一，基本对齐 CC

### 1.1 一次普通执行的完整链路

| 步骤 | 函数 | file:line |
|------|------|-----------|
| 入口 | `invoke_agent()` | `runtime/invoker.py:951` |
| 组 prompt | `_build_system_prompt` / `_resolve_memory_context` | `invoker.py:295 / :350` |
| 取工具 | `get_tools` | `invoker.py:798` |
| 进核心 | `AgentKernel.handle()` | `kernel/engine.py:1374` |
| **主循环** | `for round_i in range(max_rounds)` | `engine.py:1807` |
| LLM 流式 | `client.stream(...)` | `engine.py:1884` |
| 工具执行 | `_execute_tool_with_hooks` | `engine.py:380` |
| 结果回灌 | `api_messages.append(tool_result)` | `engine.py:2448 / :2656` |
| **退出** | `if not response.tool_calls: return` | `engine.py:2190` |
| 超预算 | `round_i >= max_rounds → [Error]` | `engine.py:2841` |

剥离所有模式后，这就是一个**纯 ReAct 循环**，和 CC 的 agent loop 同构。`max_tool_rounds` 默认 200、heartbeat 40。这一层没有发现机械化或双管道。

### 1.2 所有触发方式如何"新开 Session"接入底座 —— ✅ 统一

| 入口 | 走 `invoke_agent`? | source | file:line |
|------|:--:|--------|-----------|
| Web Chat | ✅ | `web` | `api/websocket.py:260`（`call_llm` 包装） |
| Feishu/Slack/钉钉/企微/Teams | ✅ | `feishu`… | `api/feishu.py:2122` 共享 `_call_agent_llm` |
| 定时任务 Trigger | ✅ | `trigger` | `services/trigger_daemon.py:1193` |
| Heartbeat | ✅ | `heartbeat` | `services/heartbeat.py:1619` |
| Delegation（agent→agent） | ✅ | `agent` | `agents/subagent.py:678` |
| Deep Research | ⚠️ 独立循环，仅 synthesis 调 `invoke_agent(disable_tools=True)` | `tool` | `tools/handlers/deep_research.py` |
| Workflow | ⚠️ 确定性引擎，每步 `spawn_subagent` → kernel | `workflow` | `runtime/workflow_engine.py:542` |

**"新开 Session" 的代码含义**：`SessionContext(session_id=uuid, source, channel)`；续跑则复用 `session_id` + `metadata`（命中 prompt 前缀缓存、保留 active_tool_groups/skills）。

> **结论：用户的命题"本质都是新开 Session → 循环 → 工具 → 输出"在代码里成立。** 10+ 入口干净地收敛到 `invoke_agent → kernel.handle`。Deep Research / Workflow 是两条有意的特化分支（不是债）。**底座不需要返工。**

---

## 2. 上下文组装（Loop 第 1 步，Memory 范畴）—— ✅ 基本是 LLM-friendly 的

- `prompt_builder.py` 组装：soul → knowledge → memory(T3) → active packs → skill catalog。
- 记忆体系是 **MD-first**（soul.md / memory/*.md / focus.md 为 source of truth）。
- `standalone_system_prompt` 非空时绕过整套记忆组装（CC subagent 隔离语义，`invoker.py:304/360/453`）。

这一层方向正确。**唯一要在方案阶段回答的问题**：Loop 第 3 步"要不要进 Plan Mode"的判断，本该读这套组装好的上下文由 LLM 决定——但现在它发生在 LLM 之前、用正则做（见 §3 病灶 A）。

---

## 3. 病灶层（模式叠加，全部集中在这里）

### 病灶 A 🟠 — Plan Mode entry：`auto` 路径自动触发，侵占用户决定权

**设计基准（用户 2026-06-08 拍板）**：Plan Mode 进入权归用户（与 CC 一致——用户显式切换）。Agent 可以根据提示词主观判断"要不要规划"并**建议**，但 agent 的判断**永不触发**进入；最终决定权在用户。

按此基准复核 `classify_plan_mode_entry`（`plan_mode_core.py:164-224`）+ `_maybe_handle_plan_mode_entry`（`web_chat_runtime.py:562-628`）的四条出口：

| mode | 触发条件 | 行为 | 对齐基准? |
|------|----------|------|:--:|
| `explicit` | UI 信号 `plan_mode_requested` 或文本含"计划模式/plan mode" | 激活 Plan Mode | ✅ 用户显式 = CC |
| `recommend` | 调度关键词（每天/定时/监控） | **只发建议消息、不激活**（`web_chat_runtime.py:593`） | ✅ **已经是"建议不触发"的理想形态** |
| `declined` | 文本含"不用计划模式" | 标记退出 | ✅ 中性 |
| `auto` | 长任务关键词（深度研究/出报告，`_LONG_TASK_RE`） | **直接激活、不问用户**（`plan_mode_core.py:214-222` → `web_chat_runtime.py:621`） | ❌ **唯一违例：正则判断 → 自动触发** |

**真正的病灶不是"用了正则"，而是 `auto` 这一条把"判断"与"触发"耦合了**——它靠关键词替用户做了进入决定。`recommend` 已经证明系统有"建议不触发"的现成机制。

**落地方向（用户 2026-06-08 定稿：一切皆 suggest、零强加）**：
- **唯一进入路径 = 用户显式反馈**（`explicit`）。删除一切"强加"——`auto` 自动触发必须砍。硬不变量："除用户显式外，无任何代码路径能激活 Plan Mode。"
- **判断主体从 pre-LLM 正则挪进 agent 提示词**：agent 在正常 ReAct 循环里，依 system prompt 引导判断"要不要提醒/建议用户进入 Plan Mode"，以普通文本输出一句建议；判断归模型（L1）、触发归用户。
- **CC 实证的落地形态**（`/Users/rocky243/Context Engineering/claude-code-org`）：CC 用一个 AI 可调的 `EnterPlanMode` 工具实现——AI 自主判断何时该规划并主动调用（prompt 教它 7 种该用/不该用情况，`prompt.ts`），但工具语义 = 请求许可，**用户审批确认才真正进入**（`prompt.ts:95` "REQUIRES user approval"；子代理内禁用 `EnterPlanModeTool.ts:78`）。这是"AI 判断 + 用户决定 + 零强加"的标准答案，比"输出一句建议文本"更结构化；Hive 可用 `ask_user_question` / PlanCard 承接"用户批准"。
- 架构含义：`classify_plan_mode_entry` 的 `recommend`/`auto` 两条 pre-LLM 正则分类整体让位（`recommend` 的"建议不触发"语义保留，判断改由 agent 提示词产生）。这是简化，不是加层。

> 修正：本病灶早期定性为"entry 该改成 LLM 自动判断进入"是误读——用户的设计是进入归用户、判断归模型、agent 只 suggest 零强加。

### 病灶 B 🟠 — 确认/接受/拒绝意图识别是正则

`is_plan_mode_acceptance_reply` / `extract_plan_confirmation_request`（`plan_mode_core.py:140-161`）用正则匹配"确认上一个计划""不用计划模式"。用户自然语言说"嗯这个可以，开搞吧"若不匹配死板模式就不被识别为确认。

### 病灶 C 🟠 — 计划的结构化字段靠字典/模板拼装

- capability 标签**字典翻译覆写** LLM 输出（`plan_mode_core.py:354-381`，把 LLM 写的能力描述替换成固定中文词条——这是主路径覆写，不是兜底，属 L1 违例）。
- wake_policy 拼装 / risk 归一化 / handoff target 静态映射表（`:490 / :783 / :384`）。

> 澄清（与"全机械"的印象相反）：交互式 web chat 路径上**计划正文 `plan_markdown` 现在确实是 LLM 亲自写的**（`reminder_scheduler.py:61-66` 要求写文章、`plan_mode.py:215-231` 拒空、`PlanCard.tsx:404-415` markdown 渲染）。机械化残留在 entry/确认/字段，以及下面的 D。

### 病灶 D 🔴 — Deep Research 整篇 plan 是硬编码模板

`deep_research/plan_mode.py:57-198` `build_deep_research_plan_fill`：steps / success_criteria / stop_conditions / motivation 全是 Python 字符串常量，**没有一个字是 LLM 写的**，且绕过 agent 主循环授权（`plan_mode_service.py:192-243`）。DR 这个高频场景的"计划"100% 机械生成。

### 病灶 E 🔴 — **plan 正文 ↔ 定时执行脱节（第一手验证，命中你最关心的场景）**

你的理想："Plan Mode 准备好计划 → 按时把**这个计划**丢进新 Session 执行。"
代码现实是**两条 handoff 路径对 plan 正文的处理完全相反**：

| handoff target | 用途 | 是否用 `plan_markdown` | 证据 |
|----------------|------|:--:|------|
| `continue_current_session`（live chat 默认） | 同会话续跑 | ✅ 注入执行 prompt | `plan_mode_session_handoff.py:69-86` `_plan_execution_prompt` |
| `objective_trigger`（**定时任务**） | 建 objective+trigger | ❌ **丢弃** | `plan_mode_handoff.py:98-110` 只提取 `wake_policy`；全文件 0 处引用 `plan_markdown` |

也就是说：定时任务路径上，你精心准备的计划正文**在建完 trigger 后被丢弃**——plan 只用于"授权创建 trigger 这个动作"+ 提取 cron 参数；trigger 按时触发时，丢进新 Session 的是 `trigger_context = focus.md + trigger.reason`（`trigger_daemon.py` 构造），**不是那份计划**。

**这是命中你最关心场景的双管道活标本**，也是"plan → 定时执行"心智模型与现实最大的一处断裂。

### 病灶 F — Multi-agent 派发主链（集大成）：派发已基本智能，但看板↔派发脱节、无认领、spawn/delegate 不对称

**对标修正**：Task 对标的是 CC 现役的 **Task 模式（multi-agent 作业看板）**，不是已淘汰的 `TodoWrite`（V1）。用户图景是一条主链：
`主 Agent 派发子任务 → Task 看板 → Sub-agent 认领 → 新 Session 独立完成 → 返回结果 → 主/专门 Agent 汇总写报告`。
**关键智能化判据（用户点名）**：派发给 sub-agent 的 prompt 必须由主 Agent 调 LLM 生成，不能是代码预设模板。

逐环节代码验证：

| 环节 | 现状 | 判定 |
|------|------|:--:|
| **派发 prompt（最要害）** | `spawn_subagent` 的 `task` 是主 Agent LLM 写的自由文本，逐字成为 sub-agent 首条 user message（`subagent.py:595` `content=task`，零改写） | ✅ **纯智能** |
| 同上 | `delegate_to_agent` 的 `message` 是主 Agent 自由文本，但被 `_build_delegation_brief` 裹进固定三段式模板（`orchestrator.py:533-543,844,900`） | ⚠️ **智能内核+机械信封，且与 spawn 不对称** |
| **新 Session** | 两条路径都真·独立新 Session + `invoke_agent`（spawn `subagent.py:691-715`；delegate `orchestrator.py:903-927`）。spawn=无身份 worker（角色 baseline，replace 语义）；delegate=有身份同事（叠 soul + suffix） | ✅ |
| **结果返回** | spawn 同步（工具返回值进 kernel loop）+ 异步 Signal/wake；delegate 异步 RuntimeTask + `check_async_task` | ✅ |
| **认领** | **无 claim 机制**。全是 push 指派（主 Agent 直接 spawn 指定目标）；只有 `assign_todo_owner` 盖章，没有 sub-agent 拉取 | ❌ 与"认领"心智有差距 |
| **看板↔派发** | track_todo 仅通过 `ledger_todo_id` 镜像 owner 状态（看板 note-only、不驱动调度）；manage_tasks 的 Task 表与派发**零接线** | ❌ **看板不是调度中枢** |
| **汇总写报告** | 通用路径=主 Agent 自己续写 / Coordinator Mode 强制 synthesis（`coordinator.py:164-183`）；DR=专门 synthesizer | ✅ |

**核心结论**：用户最担心的"派发 prompt 机械化"——`spawn_subagent` 已是 CC Task 工具形态（主 Agent 智能生成），**主链大部分已成立且智能**。三处真实差距：
1. **spawn/delegate 不对称 + delegate 机械信封**：同是派发，spawn 逐字透传、delegate 加模板套。
2. **无"认领"机制**：当前 push 指派，非 sub-agent 从看板 claim。
3. **看板↔派发脱节（最大结构差距）**：用户图景核心"Task 看板作为调度中枢"当前不成立——看板（还分 `track_todo`/`manage_tasks` 两套）与派发是两套系统，看板不驱动派发。

**Deep Research 定性（印证用户）**：DR = 固定 Workflow 实例——角色 prompt 是代码常量（planner/explorer/critic/synthesizer，`leaf_presets.py`）、worker 拆解由主 Agent 的 `worker_topics` 生成、主 Agent = 调 `deep_research_start` 的全功能对话 agent、synthesizer 专门写报告。**DR ≠ Plan Mode，是 Workflow 的产物**。✅

> 看板自身的 MD-first / 双轨（`manage_tasks` DB+tasks.json create-即-execute vs `track_todo` ledger JSON 写不触发执行、两套 status 枚举 `done`/`completed`）仍是待收敛债，但已从属于"看板该不该成为调度中枢"这个更大的结构决策（见 §5.3）。

### 病灶 G 🟠 — 模式选择没有统一 arbiter（你的"何时用 Workflow/Plan Mode/其他"困惑）

"决定用哪种模式"的逻辑**散落 5 处、无单一 dispatcher**：

| 决策 | 机制 | file:line |
|------|------|-----------|
| 要不要规划 | 正则 | `plan_mode_core.py:164` |
| 自治动作要不要 plan gate | 6 个工具硬表 | `plan_gate_registry.py:37-46` |
| workflow 要不要 plan | risk 阈值 | `workflow_launch.py:51` |
| trigger 走 workflow 还是 ReAct | `config.workflow_ref` 字段 | `trigger_daemon.py:904-936` |
| `execution_mode` | **其实只是 prompt cache hint**，名字误导 | `invoker.py:292`，`engine.py:1876` |

且：
- **LLM 从不"选择"模式**——只能靠读 4 个工具描述（`spawn_subagent`/`delegate_to_agent`/`start_workflow`/plan）自行判断，无代码仲裁。
- **Plan Mode 与 Workflow 无直接 handoff**：5 个 handoff target 里没有 `workflow`（`plan_mode_registry.py:24-33`）——**确认的计划无法"变成"一个 workflow**。关系是"Plan Mode 门控 Workflow 启动"，不是"Plan 产出 Workflow"。
- **`long_task` 一词三义**：plan 的 intent / 高风险 workflow 确认时的 intent / 已废弃的 handoff target 别名（`plan_mode_session_handoff.py:34`）。

**CC 源码基线（实证 `/Users/rocky243/Context Engineering/claude-code-org`）——根本没有"模式调度器"**：CC 把这些分到两条互不相干的轴：
- **权限/模式轴**：Plan Mode 是一个 *permission mode* 枚举值（`PermissionMode.ts:52`），叠在主 loop 上的状态修饰。四入口：`--permission-mode plan` / `/plan` / Shift+Tab cycle（用户）+ `EnterPlanMode` 工具（AI 发起、需用户批准）。
- **工具轴**：Sub-agent（`AgentTool`，旧名 Task）和 Workflow（`WorkflowTool`，ant-only、公开版编译期剔除）都是模型工具池里的**普通工具**，AI 在 ReAct loop 里自主调用、prompt 教它何时用。
- **普通 ReAct** = `query.ts` 的 `while(true)` 底座，永远在跑。
- CC **无 `--workflow` flag、无一等 Workflow（公开版）、无统一 mode dispatcher**。选择天然分散在 permission-mode 状态机（Plan）+ 模型工具调用决策（Sub-agent/Workflow）。

**对齐方向（拆掉不该存在的调度器）**：
- Plan Mode → 回归"模式开关"：用户控制 + AI 用"请求进入计划模式"工具发起（对标 `EnterPlanMode`，AI 判断、用户批准、零强加）。砍 `auto` 正则自动触发（病灶 A）。
- Sub-agent / Workflow → 纯工具（Hive 的 `spawn_subagent`/`start_workflow` 已是工具，方向对）：靠 prompt 教模型何时用，删散落的代码模式特判、修 `execution_mode` 误导命名、消除 `long_task` 一词三义。
- 企业治理（高风险 workflow 要确认）= Hive 的 L2 delta，应**复用 Plan Mode 的"用户批准"机制**，而非另起 risk 特判 + handoff 断链。
- **不建"统一模式调度器"**——那是反 CC 的。

### 病灶 H 🟡 — handoff 断链 / 死桩残留

- `tool_action` target（`external_action`/`state_change` 注入，`plan_mode_core.py:384-390`）**无 handler** → 确认后 `skipped` 断链。
- `detached_runtime_task` 是永久 fail-closed 桩（`plan_mode_detached_handoff.py:41-47`）。
- `long_task` + `continue_current_session` 双 target 共用一个 handler。

### 病灶 I — 系统提示词：主力段已到位，三处短板（用户："系统提示词不到位也会出问题"）

**CC 基线**：`getSystemPrompt()` 分段函数组装（`constants/prompts.ts:444,560-576`，**非一大段静态文本**）：身份/Intro → `# System` → `# Doing tasks` → `# Executing actions with care` → `# Using your tools` → `# Tone and style` → `# Output efficiency` → `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` → 动态段。CLAUDE.md **不是 system 段**，走 `isMeta:true` user message + `<system-reminder>`（`messages.ts:3700`）。

**Hive 现状**：两层装配（`agent_context.py:396` 合成 identity/executing_actions/tone_style → `prompt_builder.py:165` frozen + `:365` dynamic）；缓存边界 `__PROMPT_DYNAMIC_BOUNDARY__`（`prompt_cache.py:34`）✅。主力段**到位**——`executing_actions`/`system`/`scenario`/`tone_style` 均有 XML 结构 + BAD/GOOD + anti-drift，且有 `prompt_eval.py` 回归守护 ✅。

**三处短板**：
1. `tasks.py` / `tools.py` 两段偏薄——纯散文、无示例、无 XML，相比 executing_actions 掉档。
2. `prompt_sections/__init__.py` 文档漂移——"14 段一处装配"与实际两层装配不符。
3. frozen prefix 16K hard cap（`prompt_builder.py:48`）对长 soul + 大 catalog 可能触发尾裁 → soul/公司信息静默降智。

### 病灶 J — 工具暴露：Skill ✅ 对齐 CC；MCP ⚠️ 不走 tool_search（最大差距）

**CC 基线**：工具 schema 在 API `tools[]`（不在 prompt 文本）；分 always-loaded vs deferred。`isDeferredTool`（`ToolSearchTool/prompt.ts:62`）：**MCP 永远 deferred（`isMcp:true`）走 ToolSearch；Skill/ToolSearch/core 常驻**。
- **Skill = 独立常驻工具** + `skill_listing` system-reminder catalog，**不在 ToolSearch**。
- **MCP = deferred**，名经 `deferred_tools_delta` system-reminder 露出、schema 经 ToolSearch 拉，**在 ToolSearch**。

**Hive 现状**：`tool_search` 是真实 CC 风格 deferred 机制（`handlers/skills.py:157` + 会话持久化 `session.py:219` + 跨 invocation 恢复 `invoker.py:823` + mid-loop 注入 `engine.py:2503`）✅；CORE ~40 常驻 + `RUNTIME_TOOL_GROUPS` deferred；live chat `core_tools_only=True` ✅。
- **Skill ✅ 对齐**：独立机制（catalog 进 prompt + `load_skill` 常驻），不在 tool_search；Hive 更明确——`load_skill` 只给方法、`tool_search` 才解锁能力（`system.py:36`）。**用户记忆正确。**
- **MCP ⚠️ 不对齐（最大差距）**：MCP 工具是 DB `Tool(type="mcp")` 行，按 assignment/`is_default` 门控（`agent_tools.py:507-575`）；`tool_search` 只扫 `RUNTIME_TOOL_GROUPS` + skills（`invoker.py:629-647`），**发现不到 MCP**。MCP 自成 admin 链路（`discover_resources`→`import_mcp_server`→`call_mcp_tool`）。→ 与 CC "MCP 走统一 ToolSearch" 不一致，造成"web/feishu 用 tool_search、MCP 用另一套"的认知割裂。

---

## 4. 用户心智模型 vs 代码现实 —— 三大核心差距

| 用户理想 | 代码现实 | 差距 | 病灶 |
|----------|----------|------|------|
| 进入权归用户；agent 判断只建议不触发（=CC） | `explicit`/`recommend` 已对齐；但 `auto` 靠关键词正则**自动激活、不问用户** | `auto` 侵占用户决定权 | A |
| 定时任务：把**这份计划**按时丢进新 Session 执行 | 计划正文建完 trigger 即丢弃；触发跑 `focus.md+reason` | plan 与执行脱节 | E |
| Multi-agent 主链：看板驱动派发、sub-agent 认领、智能派发 prompt（=CC Task） | 派发 prompt 基本智能(spawn✅/delegate⚠️信封)；但**看板↔派发脱节**、无认领、看板仍两套 | 看板非调度中枢 | F |
| 一个清晰的"何时用哪种模式" | 选择逻辑散 5 处、无仲裁、模式间无 handoff | 边界混乱 | G、H |
| 系统提示词全段 benchmark 质量 | 主力段到位 + 回归守护；tasks/tools 偏薄、文档漂移、16K cap | 三处短板 | I |
| Skill/MCP 暴露对齐 CC（Skill 常驻、MCP 走 tool_search） | Skill✅；**MCP 走 DB 独立链路、tool_search 发现不到** | MCP 不对齐 | J |

---

## 5. 下一步方案讨论需要先拍板的根本决策点（本稿不预设答案）

1. **【方向已定稿】Plan Mode entry = 一切皆 suggest、零强加**（用户 2026-06-08）：唯一进入 = 用户显式反馈；agent 在提示词内判断"要不要建议进入"、只 suggest 永不触发；砍掉 `auto`；entry 判断从 pre-LLM 正则挪进 agent 提示词。方案阶段细化：system prompt 这段建议引导怎么写、`explicit` 用户信号走哪个 UI/通道、pre-LLM 分类器删到什么程度。

2. **plan → 执行 的统一交接**：定时任务路径要不要也把 `plan_markdown` 作为"按时丢进新 Session 的指令"（消除病灶 E 的双管道）？即 `objective_trigger` 触发时携带计划正文，而非只携带 cron+focus。

3. **Multi-agent 主链（集大成，对标 CC Task 模式）**——底层零件 spawn 智能派发 / 新 Session / 返回 / 汇总已到位；待定：
   - (a) **看板定位（核心方向）**：A = 看板成为真正的调度中枢（单一 Task 看板 + sub-agent 认领 `claim` + 看板驱动派发，对齐 CC Task V2 共享作业表，**大工程**）；B = 维持 push 指派、看板只做状态镜像（现状微调）。用户图景（"sub-agent 认领"）偏 A。
   - (b) 统一 spawn/delegate 派发形态、去掉 delegate 机械信封（消除不对称）。
   - (c) 看板单一化：收敛 `track_todo`/`manage_tasks` 两套、归一 status 枚举、定 MD-first 落地形态。

4. **模式边界——对齐 CC"两轴、无调度器"心智**（CC 源码实证）：Plan Mode 回归权限模式式开关（用户控制 + AI 用"请求进入"工具，对标 `EnterPlanMode`）；Sub-agent / Workflow 保持纯工具（prompt 教何时用）；删散落模式特判、修 `execution_mode` 命名、消除 `long_task` 一词三义；企业治理（高风险 workflow 确认）复用 Plan Mode 用户批准而非 risk 特判 + 断链。**不建统一模式调度器**。待定：Hive 是否补一等 Workflow（CC 公开版无、ant-only），还是维持 `start_workflow` 工具够用。

5. **系统提示词补强**：`tasks.py`/`tools.py` 两段补到 benchmark 质量（结构/示例/anti-drift）；修 `prompt_sections/__init__.py` 文档漂移；评估 frozen 16K cap 对长 soul/大 catalog 是否够、裁剪是否需更智能（防 soul/公司信息静默降智）。

6. **MCP 并入 tool_search（对齐 CC）**：让 MCP 工具进入统一 deferred/tool_search 面（对齐 CC `isMcp→deferred`），模型用 `tool_search` 即可按需发现已导入的 MCP 工具，消除"MCP 独立 admin 链路 vs tool_search"的割裂；企业治理（assignment/approval）作为 L2 叠加保留。

> **梳理状态（2026-06-08）**：整个 Session Loop + 系统提示词 + 工具暴露 全环节现状与 CC 基线已对齐认知——底座✅、上下文组装✅、entry 定稿（A）、plan→定时执行脱节（E）、multi-agent 主链（F）、模式边界 CC 两轴（G/H）、系统提示词三短板（I）、MCP 不走 tool_search（J）。
>
> 下一步进入**方案设计**。按仓库 CLAUDE.md「交付纪律」，每个决策点拍定后须**一次完整交付（禁 MVP / 禁分期首版 / 零技术债）**：先在本文件落定完整设计（含测试、边界、迁移、legacy 回填、可观测），再动代码（foundation-first）。

# Agent Task 认知脚手架 —— 对标 CC V2 Task，给 Work Ledger 补 agent 主动入口

> 本文补充 `docs/plan-mode-agent-work-ledger.md`、`docs/plan-mode-runtime-paradigm.md`、
> `docs/plan-mode-design.md`，**不重写**它们的范式、治理、恢复设计。
>
> 一句话定位：Hive 已经有 Work Ledger 的"数据结构 + 治理 + 恢复"骨架（甚至比 CC 丰富），
> 但缺 CC task 的灵魂——**agent 主动维护的工具入口 + 全会话可用 + 创建与执行解耦**。
> 本文设计如何补齐这一层，保留 Hive 全部治理加强。这是 Hive=CC 加强版定位下的标准动作：
> **先对标 CC 基线，再叠 Hive delta**。

---

## 0. TL;DR

**核心命题**：

> 在 ReAct 骨架上，把 agent 的轻量认知脚手架（task/todo/进度）从"执行触发"和"重治理"里解耦出来，
> 让 agent 能像 CC 那样在**任意会话**、**零摩擦**地主动维护它。治理与执行**包裹**这层认知，而非**取代**它。

**为什么现在做**：北极星 Goal 1 要求 per-agent 智能 ≥ hermes / ≥ CC。CC 的 task 系统是现代 agent
处理复杂多步任务、保持条理、不丢步骤、不跑飞的核心机制。Hive 当前**两套 task 机制都不是这个东西**：

| | `manage_tasks` | Work Ledger | **CC V2 Task（基线）** |
|---|---|---|---|
| agent 主动工具入口 | ✅ 有 | ❌ **无** | ✅ 有 |
| 创建 ≠ 执行 | ❌ 一写就 `execute_task` | —（agent 写不了） | ✅ 创建只声明 |
| 全会话可用 | ⚠️ 要过 plan gate（`sensitive`） | ❌ 仅 long_task / plan planner | ✅ 任意会话 |
| 数据结构 | 弱（title/status/priority） | ✅ 强（phase/todo/findings/failures/verification） | 中（+owner/blocks/blockedBy） |
| 治理 / 恢复 / 审计 | 部分 | ✅ **强（护城河）** | 弱 |
| 多 agent 派发 | ❌ | ❌ | ✅ owner + swarm |

**结论**：缺的不是"又一套 task 系统"，而是把 **Work Ledger 升级为 agent-authored 认知脚手架**——
给它装上 agent 工具入口、扩到全会话、解开"创建即执行"耦合，并为 multi-agent 预留 owner/依赖。

---

## 1. 背景与定位

### 1.1 Hive = Claude Code 加强版（先决条件）

Hive 的产品定位是 Claude Code 的加强版（superset）：CC 能做的 Hive 都要能做，且在其上叠加企业级
治理（多租户 / RLS / 审计 / 权限 / governed write gate）、self-evolution、控制中台。**任何 agent 能力
的设计第一步是全面对标 CC 基线，再谈 Hive 的 delta。** 本文严格遵循 "CC 基线 + Hive delta" 结构。

### 1.2 本文在文档群里的位置

plan-mode 文档群已经把**规划期**讲透：

| 文档 | 管的事 | 本文是否重写 |
|---|---|---|
| `plan-mode-design.md` | Plan Mode 总框架、触发、状态机、安全不变量、handoff 契约 | 否，引用 §8/§13 |
| `plan-mode-runtime-paradigm.md` | 规划的"工位"从隔离 RPC 搬回主循环；plan artifact vs work ledger 分离 | 否，引用 §10 |
| `plan-mode-agent-authored-planning.md` | 计划内容由 agent 撰写而非 skeleton 填表 | 否，引用其原则 |
| `plan-mode-agent-work-ledger.md` | **执行期** Work Ledger 的数据模型、治理边界、恢复、completion check | 否，**本文是它的延续** |

**本文聚焦它们都没覆盖的空白**：Work Ledger 当前是 runtime 自动填的，agent 没有主动入口、且只在
长任务/规划场景启用。本文设计如何让它成为 agent 在**任意会话**主动用的认知工具。

### 1.3 一条贯穿原则（呼应 `feedback_plan_from_agent_system_governs`）

> **轻量认知动作（想、规划、记进度）归 agent；治理、持久化、确认、审计归系统。
> 治理要包裹 agent 的认知脚手架，不能挤掉它。**

Plan 填表反模式、task "创建即执行"耦合、Work Ledger 无 agent 入口——是同一个病的三个症状：
本属 agent 认知层的轻量自主动作，被做成了重的系统流程，结果 agent 失去了"自己组织工作"的脚手架。

---

## 2. CC 基线：V2 Task 机制是什么

（证据：CC 源码 `/Users/rocky243/Context Engineering/claude-code-org`）

CC 早期的 `TodoWrite`（V1，纯内存认知清单）已淘汰，交互式默认启用 **V2 task 系统**
（`TaskCreate` / `TaskUpdate` / `TaskList` / `TaskGet` / `TaskOutput` / `TaskStop`）。其本质是
**认知脚手架 + 持久化作业调度的双层融合**，关键特征：

1. **agent 100% 主动**：task 完全由 LLM 通过 `TaskCreate`/`TaskUpdate` tool_call 创建/更新，
   系统代码从不塞内容（仅有 `TaskCreated`/`TaskCompleted` hooks 做校验，不写内容）。
2. **创建 ≠ 执行**：`TaskCreate` 只是**声明"要做什么"**。谁做（owner）、何时做由后续决定。
   写 task 不触发任何执行——这是它能当"思考草稿"用的前提。
3. **全会话、零摩擦、强鼓励**：system prompt 明确 *"Use this tool proactively... Plan mode — create a
   task list to track the work"*；3+ 步任务建议必用；不过任何治理 gate。
4. **数据结构**：`id / subject / description / activeForm / owner / status / blocks / blockedBy / metadata`。
   `blocks/blockedBy` 构成依赖 DAG；`owner` + swarm mailbox 让 leader 建、teammate（subagent）认领执行。
5. **执行范式**：CC 底层是**纯 ReAct**（`src/query.ts` queryLoop `while(true)`）。task 是循环里
   **可随时增改的工作记忆**，不是前置冻结计划。CC 的 plan mode 也是 ReAct + 人类审批 gate，
   **不是 Plan-and-Solve**。→ 推论：对标 CC 不该造 PS；目标是 ReAct + 好的 task 工作记忆 + plan gate。

**CC 设计哲学一句话**：task 是 agent 给自己的、零摩擦的、可派给同伴的工作记忆。

---

## 3. Hive 现状核实：两套机制都不是认知脚手架

### 3.1 `manage_tasks` = 重治理作业（创建即执行）

- `backend/app/tools/handlers/tasks.py:142-196`：`manage_tasks` 是 LLM 工具，但
  `governance="sensitive"` + `plan_gate_action_kind="start_long_task"`（:183-184）——创建 task 被当作
  "启动长任务"，要过 plan gate 确认。
- `backend/app/services/agent_tool_domains/tasks.py:52-57`：创建 `todo` 类型 task 立即
  `asyncio.create_task(execute_task(task.id, agent_id))`，返回 *"auto-execution started"*。
- `backend/app/runtime/prompt_sections/tasks.py`：prompt 只有通用工作原则，**不鼓励**主动建 task 追踪进度。

> **后果**：agent 不能用 `manage_tasks` 来"规划/记进度"，因为**一写就开火执行**。它是作业实体，不是草稿。

### 3.2 Work Ledger = runtime 自动填的执行日志（无 agent 入口、限场景）

（核实证据，2026-06-03）

- **无 agent 工具入口**：`grep "work_ledger\|add_todo\|update_todo\|record_finding" app/tools/` → **零匹配**。
  agent 工具表里没有任何写 ledger 的工具。
- **方法全是系统层**：`backend/app/services/agent_work_ledger.py` 暴露的是
  `initialize_agent_work_ledger_artifact` / `append_agent_work_ledger_progress` /
  `load_agent_work_ledger` / `validate_agent_work_ledger_completion` 等——由 runtime 调用，
  无 "agent 主动 add todo" 语义。
- **仅 3 处启用**：`long_task_runtime.py`（长任务自动建 + progress 同步）、
  `long_task_validation.py`（terminal check）、`plan_mode_service.py`（planner 阶段）。
  **普通 web chat 会话不接 ledger**（`invoker.py` / `engine.py` 无 ledger 接线）。
- **数据结构其实很好**：`agent_work_ledgers` 表有
  `current_phase / todo_items_json / findings_json / progress_json / failures_json /
  verification_json / open_questions_json / evidence_refs_json`；service 里已有 `_task_active_form`、
  `_normalize_task_status`——**早就借鉴了 CC 的 activeForm/status 字段**。

> **后果**：在 Work Ledger 这层，"todo 是系统生成、非 agent 自主"完全成立。agent 是被 runtime
> 记录的对象，不是执笔者；且只在长任务/规划场景存在，日常会话裸奔。

### 3.3 合起来的 gap

Hive 把"task"这件事劈成了两半，每半都缺一块：

```
manage_tasks   = [agent 工具] + [重治理] + [创建即执行]   ← 有手，但一动就开火
Work Ledger    = [丰富结构]   + [强治理] + [系统自动填]   ← 有台账，但 agent 没笔
CC V2 Task     = [agent 工具] + [轻治理] + [创建≠执行] + [全会话] + [owner/依赖]  ← 完整
```

**没有任何一套 = "agent 在任意会话零摩擦主动维护的认知脚手架"。** 这就是要补的 delta。

---

## 4. 诊断：缺失的那一层，为什么重要

1. **直接损害 Goal 1**：缺认知脚手架的 agent，在复杂多步任务里更容易丢步骤、重复失败、跑飞、
   提前宣称完成。这是 per-agent 智能落后 CC/hermes 的直接来源之一。
2. **精确化了用户的原始担忧**：不是"系统强行塞了 todo"，而是"**该属于 agent 的笔，系统拿走了
   （Work Ledger）或做得太重 agent 不敢拿（manage_tasks）**"。
3. **挡住了 multi-agent 演进**：CC V2 的 owner/依赖是 subagent fan-out 的作业层底座
   （见 `subagent-source-capability.md`）。Hive 两套都没有 owner/依赖，subagent 协作缺作业层。

---

## 5. 设计：把 Work Ledger 升级为 agent-authored 认知脚手架

**不新建系统**。复用已实装的 `agent_work_ledgers` 表与 `agent_work_ledger.py` service，做四件事。

### 5.1 核心原则

> Work Ledger 的**结构、治理、恢复、completion check 全部保留**（`plan-mode-agent-work-ledger.md`
> 的设计不动）。本文只改"**谁来写、什么时候能写、写了会不会触发执行**"。

### 5.2 Delta-1：给 agent 装主动工具入口（对标 `TaskCreate`/`TaskUpdate`）

新增一组**轻量 agent 工具**，写入**已有的** governed work ledger：

| 工具（建议名） | 对标 CC | 行为 | 治理 |
|---|---|---|---|
| `track_todo` | `TaskCreate` + `TaskUpdate` | agent 主动 add / update todo item（title, status, activeForm, evidence_refs） | **非 sensitive，不过 plan gate** |
| `record_finding` | （Work Ledger 独有） | 记一条已验证发现 / open question / 失败+下次策略 | 非 sensitive |
| `read_ledger` | `TaskList` / `TaskGet` | 读回当前 phase / todo / findings / failures（恢复用） | 只读 |

关键：这些工具走 `append_agent_work_ledger_progress` 等**已有 service 方法**，只是把"调用者"
从 runtime 改成 agent。**写 ledger 是认知动作，不是治理动作**，因此**不挂 `plan_gate_action_kind`、
不设 `governance="sensitive"`**——这是和 `manage_tasks` 最根本的区别。

### 5.3 Delta-2：扩到全会话（解除 long_task/plan 限制）

- 在 `runtime/invoker.py` / `kernel/engine.py` 的通用 invocation 路径接入 ledger：按
  `plan-mode-agent-work-ledger.md §8` 的启用阈值（`expected_tool_calls ≥ 5` / 多文件 / 外部副作用 /
  未来自主），**在普通 web chat 复杂会话里也按需 lazy-create ledger**，不再局限于 long_task/plan。
- 简单问答不建 ledger（成本 > 收益），与 CC "skip trivial task" 一致。
- 复用已有的**每轮 reminder + compaction 保活**机制（`plan-mode-runtime-paradigm.md §6.2/§10`
  已为规划期做了，执行期 ledger 享受同等待遇）。

### 5.4 Delta-3：解开"创建即执行"耦合

- `track_todo` **只写认知状态，绝不触发执行**。这是它能当思考草稿的前提。
- `manage_tasks` 的"创建 todo 即 `execute_task`"语义**保留但收窄**：它继续作为"启动一个受治理的
  自主作业"的重动作（过 plan gate 合理）。**认知追踪走 `track_todo`，启动作业走 `manage_tasks`——
  两个动作彻底分开。**
- prompt（`prompt_sections/tasks.py` 或 executing_actions）补一段对标 CC 的引导：
  *复杂多步任务主动用 `track_todo` 拆解+追踪进度；开始一步前标 in_progress、做完标 complete。*

### 5.5 Delta-4：为 multi-agent 预留 owner / 依赖（合流 subagent 源能力）

- ledger todo item 增加可选 `owner`（对标 CC）+ `blocks/blockedBy`（依赖 DAG）。
- 当 `delegate_to_agent` / subagent spawn 发生时，父 ledger 的 todo 可标 `owner=<child>`；
  子 agent 完成后回写 status。**与 `subagent-source-capability.md` 的作业层对接**（本文只定义
  ledger 侧字段契约，spawn/回收机制以那份文档为准）。

### 5.6 治理边界：什么过 gate、什么不过

| 动作 | 过 plan gate? | 理由 |
|---|---|---|
| `track_todo` / `record_finding` / `read_ledger` | ❌ 否 | 纯认知，无外部副作用 |
| `manage_tasks`（启动受治理作业） | ✅ 是 | 启动自主执行，是治理动作 |
| 外部可见 / 不可逆 / 敏感动作 | ✅ 是 | 走既有 `action_preflight` / plan gate |

**不变量**：写 ledger 永远不放大 confirmed plan 边界（`plan-mode-agent-work-ledger.md §10.1` 不变量沿用）；
ledger 的 `findings` 不能覆盖 confirmed plan；untrusted 内容注入仍按 §10.2 delimiter 隔离。

---

## 6. 与已有系统的边界（引用，不重写）

| 对象 | canonical owner | 本文关系 |
|---|---|---|
| `agent_plan_requests`（规划确认契约） | Plan Mode | 不动；ledger todo 可记 `source_plan_id` 做审计回链 |
| Plan artifact（规划期产物） | `plan-mode-runtime-paradigm.md` | 不动；与执行期 ledger 分离原则沿用其 §10 |
| `agent_work_ledgers`（执行认知账本） | `plan-mode-agent-work-ledger.md` | **本文延续**：补 agent 工具入口 + 全会话 + 解耦 |
| `manage_tasks` / `Task` 表（受治理作业） | `tools/handlers/tasks.py` | 收窄为"启动作业"，认知追踪剥离到 ledger |
| Memory Control Plane | memory/* | 不动；ledger → 长期 memory 仍过 write gate |

---

## 7. 落地切口（runtime-first，memory 最后）

1. **切口①（先做，最关键）✅ 已实装**：新增 `track_todo` / `record_finding` / `read_ledger` 三个 agent 工具，
   复用现有 ledger service 方法；**非 sensitive、不过 gate**。补 prompt 引导。
   → 验收：agent 能在普通会话里主动写/读 ledger，写 todo 不触发任何执行。

   **实装记录（2026-06-03）**：
   - **存储模型确认**：Work Ledger 的 source of truth 是 **JSON 文件**（`agent_work_ledger.py` 的
     `_ledger_path` → `AGENT_DATA_DIR/<agent_id>/runtime_artifacts/work_ledger.json`，通用路径无
     plan_id/runtime_task_id）。`models/work_ledger.py` 的 `AgentWorkLedger` 表存在但**尚未接线**
     （模型 docstring 自述为 "future canonical index"，全仓仅自身模块 import，service 零 DB 写）。
     三工具走文件路径，与现有 3 个 caller（`long_task_runtime` / `long_task_validation` /
     `api/autonomy`）一致。
   - **改了哪些文件**：
     - `app/services/agent_work_ledger.py`：新增三个公共 service 写/读原语
       `upsert_agent_work_ledger_todo`（load→改 todo→`_write_ledger`，update 原地不新增、保留
       `created_at`）、`append_agent_work_ledger_finding`（按 type 路由到 `findings` /
       `open_questions` / `failures`，不扰动其他列）；私有 `_load_or_bootstrap_ledger`（缺则
       lazy-create，`source="agent_authored"`）。复用既有 `_normalize_work_item` /
       `_normalize_finding` / `_normalize_failure` 归一化，保持 ledger 契约不破。
     - `app/tools/handlers/work_ledger.py`（**新建**）：三工具薄壳，从 `request.context` 取
       agent_id；写工具不触发任何执行（无 `asyncio.create_task` / 无 Task 实体）。
     - `app/tools/collector.py`：`HANDLER_MODULES` 加 `app.tools.handlers.work_ledger`。
     - `app/runtime/prompt_sections/executing_actions.py`："Complex multi-step tasks" 段补对标
       CC 的 task 鼓励语（主动 `track_todo` 拆解 + in_progress/completed 标记 + `read_ledger`
       恢复 + 明示"写笔记不触发执行"）。
     - `tests/tools/test_bridge_equivalence.py`：canonical 工具面集合补三工具名（+3 工具的必然更新）。
   - **三工具名**：`track_todo`（category `work_ledger`, governance `""`, adapter `request`）、
     `record_finding`（同上）、`read_ledger`（governance `safe`, read_only, parallel_safe, adapter
     `request`）。
   - **非 sensitive / 不过 gate 证据（grep 实证）**：
     - `grep "plan_gate_action_kind\|governance=\"sensitive\"" app/tools/handlers/work_ledger.py` →
       **零匹配**（三工具均无 plan gate tag，写工具 governance 省略为 `""`）。
     - 测试 `tests/tools/test_work_ledger_governance.py` 断言：三工具不在
       `collected.sensitive_tools`、不在 `plan_gated_tool_action_kinds()`、
       `hard_gated_action_kind(name)` / `hard_gated_action_kind(name, {"action":"add"})` 均为
       `None`；`read_ledger` 在 `safe_tools`/`read_only_names`/`parallel_safe_names`。
   - **tenant scope 如何保证**：service 按 `agent_id` 物理隔离文件目录
     （`AGENT_DATA_DIR/<agent_id>/`），handler 仅从 `ToolExecutionContext` 取调用方 agent_id，
     agent 无法读写他人 ledger（测试 `test_two_agents_ledgers_are_isolated` 实证）。tenant 层在
     governance 上游兜底：`run_tool_governance` 对非 safe 工具在 `tenant_id` 缺失时 fail-closed、
     对每个工具过 capability gate，并对 `public` 安全区只放行 SAFE_TOOLS（故 `read_ledger` 在
     public 区可读、两个写工具被治理外壳挡住——符合 §5.6"治理在外面包着"）。
   - **测试证据**：新增 3 个测试文件共 23 用例全绿
     （`tests/services/test_agent_work_ledger_agent_writes.py` 4 +
     `tests/tools/handlers/test_work_ledger_handler.py` 12 +
     `tests/tools/test_work_ledger_governance.py` 7）；`tests/tools/` + `tests/runtime/` +
     既有 ledger service 测试合计 **661 passed**。
   - **§8 不变量核对**：①认知≠治理（无 gate/sensitive，写不触发执行，实测 `create_task` 未被调用）；
     ②写 ledger 不放大/不覆盖 confirmed plan（仅改 todo/findings 列，不碰 plan 边界）；
     ③untrusted summary/title 以 JSON 字符串值落库=data 非 instruction（service 从不解释其内容）；
     ⑤多租户 scope 见上；`manage_tasks` 一行未动。
2. **切口② ✅ 已实装**：通用 invocation 路径（invoker/engine）按阈值启用 ledger 认知脚手架 + 每轮
   reminder 注入 + compaction 保活（复用规划期机制）。→ 验收：复杂 web chat 会话 compaction 后 agent
   能从 ledger 恢复。

   **实装记录（2026-06-03）**：
   - **接线点（3 处，复用规划期机制而非重造）**：
     - `app/runtime/invoker.py` `_resolve_effective_turn_route`：每轮路由解析后调
       `should_enable_work_ledger(...)`，把决策落到 `session_context.metadata["work_ledger_enabled"]`
       （kernel 读它的方式与既有 `context_budget` / `turn_route` 完全一致——invoker 算、塞 metadata、
       kernel 读，kernel 保持 zero-DB 纯核）。
     - `app/kernel/engine.py` 主轮循环（紧邻 plan-mode reminder 注入点，约 `:1767`）：每轮调
       `_work_ledger_reminder_content(session_context)`，complex turn 注入一条
       `role="system"` 的 `_WORK_LEDGER_REMINDER`（与 round-pressure / plan-mode reminder 同一
       `api_messages.append(LLMMessage(...))` 机制，paradigm-runtime §6.2）。
     - `app/kernel/engine.py` `_build_restoration_context`（post-compaction 恢复，约 `:1125`）：
       complex turn 且 ledger 文件存在时，直接读 `runtime_artifacts/work_ledger.json`（与读 soul.md /
       focus.md 同一 workspace 解析路径），经 pure `build_agent_work_ledger_resume_summary` +
       `render_work_ledger_resume_block` 注入 **5-question reboot**，置于 soul/focus 之后高优先位。
   - **阈值判断逻辑**（pure，住 `agent_work_ledger.py::should_enable_work_ledger`，与 §8 阈值对齐）：
     §8 的 `expected_tool_calls >= 5 / 多文件 / 外部副作用` 在 runtime 无单一一等信号，最接近的现成
     proxy 是 turn 的 `TaskProfile`（`context_budget.infer_task_profile`）。规则=
     `is_simple_turn_candidate`（general+low+短、无 code/url/file，既有 `_is_simple_turn_candidate`
     探测器）→ **不启用**（简单问答零开销，对齐 CC "skip trivial task"）；`complexity ∈ {medium, high}`
     → 启用；专才 profile（coding / research / operations / self_evolution，天然多步）即便 low 也启用；
     其余（纯 general low 非 simple-shape）→ 不启用。**这是粗粒度启用门，非硬契约**——启用只多注入一条
     nudge + compaction reboot，从不强写 ledger（lazy-create 仍由切口① 的 `track_todo`/`record_finding`
     首次写时发生，不污染空 ledger 文件）。
   - **compaction 恢复如何验证**：`tests/kernel/test_work_ledger_scaffold.py` 用**真实 service +
     临时 data_root**（不 mock service）写一个 in-progress ledger（已完成/in_progress/pending todo +
     verified finding + failure），monkeypatch `AGENT_DATA_DIR` 指向 tmp，跑真实
     `_build_restoration_context`，断言 5-question reboot 全覆盖：current phase / 开放 todo（completed
     的不出现在"开放"）/ verified finding / failure（do-NOT-repeat）/ pending verification；
     另两例断言简单 turn 不读 ledger（零开销）、complex 但无 ledger 文件时不注空块。
   - **两套 reminder 并存不冲突（§8 不变量）**：`_work_ledger_reminder_content` 在 plan_mode.active 时
     返回 None——规划期是只读/无执行相，"track 你的执行 todo" nudge 会与 plan-mode reminder 矛盾，故
     每轮至多一条 reminder 触发；plan-mode 的 reminder/compaction 一行未动（回归
     `tests/kernel/test_plan_mode_reminder.py` 全绿）。
   - **切口① governance 缺口顺手补齐（commit 必须全绿的前置）**：切口①（07d8b4a）加了三工具但**漏把它们
     登记进 `CAPABILITY_MAP`**，而 `STRICT_CAPABILITY_MAPPING` 默认 True → 三工具在有 tenant 的真实
     invocation 里被 capability gate **拒绝**（切口①/切口② 在生产其实都跑不动）。本切口补：
     `track_todo`/`record_finding` → 新 capability `agent.task.track`（认知记账，区别于
     `manage_tasks` 的 `agent.task.modify` 作业执行，呼应 §5.6"认知≠治理"）；`read_ledger` →
     `agent.task.read` + 进 `_CAPABILITY_GATE_EXEMPT_TOOLS` + `_STATIC_SAFE_TOOLS`（只读自有 ledger，
     与其它只读 context 工具一致、public 区可读）。**未改切口① 三工具/service 写原语一行**——只补登记。
   - **测试证据**：新增/扩展 3 文件 17 用例全绿（engine `test_work_ledger_scaffold.py` 7 +
     service `test_agent_work_ledger_agent_writes.py` 新增 6 + invoker `test_invoker.py` 新增 2 +
     plan-mode reminder 回归 9 复跑）；`tests/services/test_capability_gate_policy_surface.py` 8 绿。
     全量 `pytest tests/ -q`：**3413 passed, 7 skipped**，2 个 `test_feishu_identity_resolution`
     failures 经 stash 验证为 HEAD 既有、与本切口无关（Feishu user_id backfill，另一 WIP 分支）。
3. **切口③ ✅ 已实装**：ledger todo 增加 `owner` / `blocks` / `blockedBy` 字段契约，对接 subagent 源能力。
   → 验收：delegation 时父 ledger todo 能标 owner、子 agent 回写 status。

   **实装记录（2026-06-03）**：
   - **字段契约（service 侧，§5.5 Delta-4）**：`agent_work_ledger.py` 的 `_normalize_work_item`
     早已归一化 `blocks`/`blockedBy`（切口① 侦察确认），本切口补 `owner` —— 归一化器与
     `_display_item`（读路径）均改为**仅当 owner 非空才落键**（CC parity：owner 可选、不强加）。
     `upsert_agent_work_ledger_todo` 新增 `owner` / `blocks` / `blocked_by` 三个可选关键字参数，
     add/update 两分支都贯穿（update 为**部分更新**：未传的字段保留原值）。**owner 是纯认知字段——
     标 owner 永不 spawn/delegate 任何东西**（§8 不变量①认知≠治理）。
   - **track_todo 工具**：`tools/handlers/work_ledger.py` 的 `track_todo` schema 加 `owner` 参数
     （描述明示"设 owner 只是注记，不启动/派发任何工作"），透传到 service。**未动 record_finding /
     read_ledger 一行**。
   - **delegation 契约原语（ledger 侧两个新 service 函数）**：
     - `assign_todo_owner(agent_id, item_id, owner, status="in_progress")` —— 父在 delegate 时给一条
       todo 盖上子 agent 为 owner 并（默认）翻 in_progress；**不 spawn 子**（启动是 delegation runtime
       的活）。
     - `record_delegated_todo_status(agent_id, item_id, status, expected_owner=None)` —— 子完成后把
       终态写回父 todo；给 `expected_owner` 时**fail-closed 校验**当前 owner 不符则 `PermissionError`
       （防越权 flip 他人 todo），owner 经写回保留。
   - **delegation 自动回写接线点（本切口未做，标注待对接）**：核实 `agents/orchestrator.py:_delegate`
     的 `DELEGATION_END` hook metadata 仅含 `from_agent`/`to_agent`/`status`/`task`，**无 `ledger_todo_id`**
     —— `AgentDelegationRequest` 没有"哪条父 todo 触发了本次 delegation"的概念（delegation 工具
     `communication.py:delegate_to_agent` 由 LLM 以自由文本 task 调用，非 todo id）。要做**自动**回写
     需把 `ledger_todo_id` 串过 `AgentDelegationRequest` → `_delegate` → `DELEGATION_END` metadata +
     新增一个 `DELEGATION_END` 处理器读 `from_agent` 的 ledger 调 `record_delegated_todo_status`。
     **这超出"ledger 侧字段契约"范围、要改 delegation 请求契约/coordination 邻接核**，按 §5.5
     "spawn/回收机制以 subagent-source-capability.md 为准 / 本文只定义 ledger 侧字段契约" + 任务
     brief 的边界裁定，**留作 subagent 源能力对接点**。本切口交付的 `assign_todo_owner` /
     `record_delegated_todo_status` 正是那一层将调用的 ledger 侧契约原语。
   - **测试证据**：service 6 用例（owner+依赖字段往返 / owner 可选省略 / assign_todo_owner 标
     in_progress / assign 要求 owner / 回写 completion 保留 owner / 回写拒绝错误 owner 且不改 todo）+
     handler 1 用例（track_todo owner 透传），**全用真实 service + 临时 data_root（不 mock）**。
     `tests/services/test_agent_work_ledger_agent_writes.py` + `test_work_ledger_handler.py` +
     `test_agent_work_ledger.py` 合计 33 passed；`tests/tools/` + kernel scaffold 252 passed。
     全量 `pytest tests/ -q`：**3420 passed, 7 skipped**（较切口② 的 3413 恰 +7），2 个
     `test_feishu_identity_resolution` failures 为 HEAD 既有 unstaged WIP（stash 验证），与本切口无关。
   - **§8 不变量核对**：①认知≠治理（owner/依赖字段无 gate、标 owner 不触发执行）；②不放大/覆盖
     confirmed plan（只改 todo 列）；③owner/title untrusted 文本以 JSON 字符串值落库=data；⑤多租户
     scope 沿用切口① 的 `AGENT_DATA_DIR/<agent_id>/` 物理隔离 + `record_delegated_todo_status` 的
     `expected_owner` fail-closed 校验。
4. **切口④ ✅ 已实装（最后，过 write gate）**：完成后哪些 ledger 内容沉淀为长期 memory，走 Memory
   Control Plane。

   **实装记录（2026-06-03）**：
   - **侦察结论（write gate 复用点）**：`memory/t2_store.py::append_t2_entries` **早已**对每条
     extraction 调 `memory/write_gate.py::prepare_memory_write`（→ `PrivacyLayer.classify_and_mask`），
     `decision.rejected` 的（PL4 凭据）`continue` 跳过、PL2/PL3 分级 + lifecycle/evidence metadata
     由 `_base_metadata` 盖戳。`extract_agent.py::_append_to_learnings(agent_id, extractions, source=)`
     是喂进这条管道的 canonical 写入口（RESPONSE_COMPLETE / backfill 都走它）。→ **切口④ 正是 brief
     预判的"小接线"：把 ledger findings 整形成同一个 extractions 列表喂 `_append_to_learnings`，gate
     原样复用、零绕过、零重造。**
   - **改了哪些（两个新 service 函数，住 `extract_agent.py`）**：
     - `ledger_findings_to_extractions(ledger)`（**pure，无 IO**）：把 ledger 的**已验证** findings
       （`trust=="verified"`）映射成 `category="reference"`（concept `how-it-works`，`ev=tool_verified`，
       `source_refs`→`refs`）；**未验证 findings 留作 ledger scratch 不沉淀**（§8 认知≠持久化）；带
       `next_strategy` 的**未 resolved** failures 映射成 `category="blocked_pattern"`（"error — next
       time: strategy"），让下个会话不重蹈死路；裸 error 无教训=噪声跳过。输出形状即
       `append_t2_entries` 消费的 extraction dict，**本函数从不碰隐私/PL4，由 gate 逐条裁决**。
     - `consolidate_ledger_findings_to_t2(agent_id, ...)`（薄壳 orchestrator）：load scoped ledger →
       map → 交给 `_append_to_learnings`（**每条过 `prepare_memory_write`**）。返回实际写入数（gate
       拒绝 + 去重的不计）。
   - **触发点（SESSION_CLOSE，任务完成边界）**：`runtime/hooks_setup.py::_t0_session_close` 在
     extractor `drain` 之后调 `consolidate_ledger_findings_to_t2(agent_id, source="work_ledger")`，
     best-effort（consolidation 失败不阻断后续 T0 写）。SESSION_CLOSE 是会话"任务做完"的自然落点，
     且与切口② compaction reboot 读的同一个 unscoped `runtime_artifacts/work_ledger.json` 对齐。
   - **ledger→memory 如何过 gate（PL4 拒绝实证）**：测试 `test_pl4_credential_in_finding_is_rejected_
     by_gate` 用**真实 gate**（不 mock）—— 一条 `trust=verified` 但 summary 含 OpenAI 式 `sk-` 凭据
     （运行时拼接、源码无字面密钥，规避 SEC 守卫）的 finding，经 `consolidate_ledger_findings_to_t2`
     →`prepare_memory_write` 判 PL4 `rejected`→**written==0**，且断言**原始密钥串从未落进任何 T2 md
     文件**；`test_pl3_sensitive_finding_is_classified_when_settled` 证一条含 "salary review" 的
     verified finding 仍沉淀但被 gate 标 `sensitivity=PL3_sensitive`（敏感分级生效、非整条毙）；
     `test_verified_finding_settles_into_t2_through_gate` 证普通 verified finding 落 T2 且被盖
     `entry_id`/`PL1_public` lifecycle metadata。
   - **测试证据**：新增 `tests/services/test_ledger_to_memory_gate.py` 9 用例全绿（3 pure mapper +
     6 orchestrator/gate/SESSION_CLOSE，**全用真实 gate + 真实 ledger service + 临时 data_root，不 mock
     gate**）；含 SESSION_CLOSE hook E2E（`test_session_close_hook_settles_ledger_findings_to_t2` 跑真实
     `_t0_session_close` 证 verified finding 经 hook 落 T2）+ 幂等去重（二次 consolidation written==0）。
     `tests/services/test_extract_agent.py` + `tests/runtime/` + `tests/memory/` 729 passed；全量
     `pytest tests/ -q`：**3429 passed, 7 skipped**（较切口③ 的 3420 恰 +9），2 个
     `test_feishu_identity_resolution` failures 为 HEAD 既有 WIP，与本切口无关。
   - **§8 不变量核对**：④ledger→长期 memory 必过 write gate（PL4 拒绝 + 敏感分级，**复用现有 gate 零
     绕过**，实证见上）；①认知≠治理（沉淀是系统在 SESSION_CLOSE 做的持久化动作，非 agent 认知工具）；
     ③untrusted finding 文本作为 memory content 仍过 gate 的隐私/form 校验（PL4 凭据被中和）；⑤多租户
     scope 沿用 `AGENT_DATA_DIR/<agent_id>/` 物理隔离。

每个切口都是 thin E2E（工具 → service → DB → prompt 注入 → 测试），可独立交付、独立验证。

---

## 8. 安全不变量

保留 `plan-mode-agent-work-ledger.md §10` 全部不变量，新增/强调：

1. **认知 ≠ 治理**：`track_todo` 等认知工具不得挂 plan gate / sensitive；反过来，启动自主作业 /
   外部动作必须过 gate——两者不可混淆。
2. 写 ledger 永不放大 confirmed plan 边界；ledger findings 不覆盖 confirmed plan。
3. untrusted（web/file/tool output）内容注入 ledger 仍按 delimiter 隔离，永远是 data 非 instruction。
4. ledger → 长期 memory 必须过 Memory Control Plane write gate（PL4 拒绝，敏感分级）。
5. 多租户：ledger 读写按 tenant/agent scope，RLS 隔离（Hive delta，CC 无此层）。

---

## 9. 验收标准

1. agent 在**普通 web chat 复杂会话**里能用 `track_todo` 主动维护 todo，**写 todo 不触发执行**。
2. ledger 在通用 invocation 路径按阈值启用，简单问答不启用。
3. context compaction / resume 后，agent 能从 ledger 判断下一步（5-question reboot），不重复失败动作。
4. `track_todo` 不过 plan gate；`manage_tasks` 启动作业仍过 gate——治理边界清晰可测。
5. ledger todo 支持 `owner`/依赖字段，delegation 场景可派发 + 回写。
6. 全程多租户 scope + RLS；ledger → memory 过 write gate。
7. 对照 CC：同一个"3+ 步复杂任务"，Hive agent 的条理性 / 不丢步 / 失败去重能力 ≥ CC（Goal 1 bar）。

---

## 10. 原则总结

> CC 的 task 是 agent 给自己的、零摩擦的、可派给同伴的工作记忆。
> Hive 已经有了更丰富的台账（Work Ledger）和更强的治理，**只差把笔交回给 agent**。
> 把笔交回去——让 agent 在任意会话主动记、治理在外面包着——Hive 才真正成为 CC 的加强版，而不是
> 一个把 agent 认知能力治理没了的"更重的 CC"。

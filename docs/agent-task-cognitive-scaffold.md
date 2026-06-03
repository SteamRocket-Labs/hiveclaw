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
2. **切口②**：通用 invocation 路径（invoker/engine）按阈值 lazy-create ledger + 每轮 reminder 注入 +
   compaction 保活（复用规划期机制）。→ 验收：复杂 web chat 会话 compaction 后 agent 能从 ledger 恢复。
3. **切口③**：ledger todo 增加 `owner` / `blocks` / `blockedBy` 字段契约，对接 subagent 源能力。
   → 验收：delegation 时父 ledger todo 能标 owner、子 agent 回写 status。
4. **切口④（最后，过 write gate）**：完成后哪些 ledger 内容沉淀为长期 memory，走 Memory Control Plane。

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

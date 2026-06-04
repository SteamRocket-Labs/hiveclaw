# Workflow 源能力设计：Hive 确定性编排层（轴 2）

> **定位**：聚焦**单一核心**的源能力设计文档——补齐 Hive 的 **Workflow 体系**（代码拥有控制流的确定性编排）。轴 1（subagent）的姊妹篇。
>
> **状态**：**v0 草稿（讨论中，未拍板）**。前置调研见 `docs/workflow-vs-skill-a2a-discussion.md`（v0.1，三方辩论 + 多租户底座 8 blocker）；本文在**三件套落地后**的新架构上重新设计。
>
> **方法论（先决条件）**：Hive = CC superset——**先精确对标 CC 基线，再叠 Hive delta（治理/多租户/进化），绝不做减法、不自创范式**。
>
> **次序铁律（用户拍板）**：轴 2 先讨论 → 设计 → 实装；**deep research 接入是最后一步**，所有条件齐全后才做。

---

## 0. 一页纸

**为什么现在重写而不是沿用讨论稿 v0.1**：讨论稿写于 2026-05-29，当时轴 1 未实装、Plan Mode 三路并存、task 创建即执行、`FinanceWorkflowRunner` 还在。**2026-06-03 三件套全部落地**后，格局变了：

| CC 基线栈 | Hive 现状（2026-06-03） |
|---|---|
| ReAct 主循环 | kernel ✅ |
| Plan Mode（人类 gate） | ✅ 唯一规划路径（`a6dc5e8` 删 RPC planner）；`action_kind` 接法标准化；`author_type="workflow"` 缝已存在 |
| Task（工作记忆 + owner/依赖 + reminder + compaction 恢复） | ✅ Work Ledger（`07d8b4a..1159197`）；`runtime_task_id` scope 天然是 run 级；delegation 契约原语已交付 |
| Agent 工具（spawn/fanout） | ✅ subagent 源能力（`1155f46..97bac1c`）：治理/隔离/budget/Signal 齐 |
| **Workflow（代码控制流）** | **❌ 唯一空格 = 本文标的** |

**三件套把讨论稿 §6 设想要新建的东西做掉了一大半**（叶子多租户化、安全门、认知层）。真正要新建的只剩四件：**持久 workflow 定义、薄控制流引擎、run journal（kill-resume）、run 级预算信封**。

---

## 1. 术语边界：两种编排形态，本文只钉形态 B

| 形态 | 控制流归属 | 组成 | 状态 |
|---|---|---|---|
| **A：即兴 swarm** | **LLM**（agent 在 ReAct 里自己协调） | Work Ledger todo + `owner` 标记 + `spawn_subagent`/`fanout` | ✅ 三件套已齐；**只差一根线**：`ledger_todo_id` 串进 spawn/delegation → `DELEGATION_END` 自动回写（work-ledger 切口③ 显式留缝，见 §4.2） |
| **B：确定性管线** | **代码**（引擎保证步序，LLM 只填叶子） | 本文的 Workflow 引擎 | ❌ 空白，本文标的 |

CC 自己也是两形态并存（Task+Agent 的 swarm vs Workflow 脚本）——**这个切分就是 CC 基线**，不是 Hive 发明。

**铁律**：
- 轴 2 **不承担形态 A 的任何职责**（A 已被三件套覆盖）。
- Workflow ≠ Coordinator Mode（散文 SOP，LLM 控制流）≠ `deep_research/controller.py`（DR 专属预算循环 + `EvidenceLedger`，将来被本引擎吸收，非现在）。
- **范围纪律**（讨论稿 §4，三方辩论共识）：只用于确定性关键管线（deep research / 进化 eval / 定时治理）；对话型 / 开放探索 agent 不碰——那是形态 A 的地盘。

---

## 2. CC Workflow 基线精确拆解（第一手）

> 证据等级：**Fact** —— 以下是 Claude Code Workflow 工具的运行时契约（作者第一手使用），非转述。

剥开实现，CC Workflow = **7 要素**：

1. **脚本 = 控制流载体**：一段 JS，`export const meta = {name, description, phases}`（纯字面量）+ 脚本体；原语 `phase(title)` / `log(msg)` / `args`（参数化输入）。代码决定每一步，LLM 不参与控制流。
2. **`agent(prompt, opts)` = 隔离叶子**：spawn 一个 subagent，**只回结论**（文本或 `schema` 强制的结构化对象）；中间 tool 过程不进编排者上下文；单叶失败 → `null`（失败隔离）。`opts`: label/phase/schema/model/isolation/agentType。
3. **结构化并发原语**：`pipeline(items, stage1, stage2...)`（无 barrier 流水，**默认**）/ `parallel(thunks)`（barrier，仅当真正需要全量结果）/ 组合模式（loop-until-dry、judge panel、adversarial verify）。并发 cap `min(16, cpu-2)` 排队；总 1000 agent backstop。
4. **`budget` 硬顶池**：`{total, spent(), remaining()}`，跨整个 run 的**所有** agent 共享；耗尽后 `agent()` 直接 throw——是硬约束不是建议。
5. **确定性 → journal → resume**：脚本内**禁** `Date.now`/`Math.random`/`new Date()`（破坏重放）；引擎 journal 每个 `agent()` 调用（prompt+opts→result）；`resumeFromRunId` = 未改动前缀**缓存命中秒回**，从第一个改动点起才真跑。kill/edit/resume 是一等公民。
6. **异步 + 完成重入**：后台跑，立即返回 task id；完成时以 task-notification **重入**编排者主循环（不轮询）。
7. **入口纪律**：用户**显式 opt-in** 才能跑（明说/ultracode/skill 指令）——agent 不能因为"任务适合"就自发起；**named workflow**（`.claude/workflows/` 持久文件，按名调用 + `args`）与 inline script 双路径；`workflow()` 嵌套限一层。

> **CC 中 Workflow journal 与 Task 系统是分开的**：journal 是引擎私有执行账本（驱动 resume）；Task 是 swarm 协作的共享认知 ledger。两者职责不同、互不驱动。Hive 同构（§5 D3）。

---

## 3. Hive 现状：三件套留给 Workflow 的接缝（file:line 实测）

> 证据等级：本轮 file-grounded 调查（2026-06-03），关键签名经报告核对。

### 3.1 Plan Mode（发起门，现成）

| 接缝 | 位置 | 用法 |
|---|---|---|
| Gate 检查 | `plan_mode_gate.py:81` `PlanModeGate.check(db, agent_id, action_kind, confirmed_plan_id, plan_version, plan_hash)` | workflow run 用 `action_kind="start_long_task"`（`plan_mode_core.py:45-51` ACTION_KINDS 现члены） |
| 工具层自动接入 | `ToolMeta.plan_gate_action_kind="start_long_task"` → `plan_gate_registry.py:134-151` | workflow 暴露为工具时一个字段接入 |
| 静态 fail-closed | 无 confirmed plan → `PlanGateDecision(allowed=False, needs_plan_payload=...)` | 引擎必须 `if needs_plan: 停止并上抛 payload` |
| 无人值守拦截 | `session.py:260` + `invoker.py:172`（source ∈ {trigger, heartbeat}） | 定时 workflow 自动走主循环 Plan Mode，authored plan 落 `awaiting_confirmation` |
| plan 生成 launcher | `plan_mode_system_run.py:112` `launch_system_plan_run(plan, seed_context)` | 无 live 消息流的外部入口起 plan |
| 一等 caller 标记 | `plan_mode_service.py:338` `author_type="workflow"` | **缝已存在**：workflow 类 caller 提交 plan 的身份标记 |

### 3.2 Work Ledger（认知镜像，现成）

| 接缝 | 位置 | 用法 |
|---|---|---|
| run 级 ledger | `agent_work_ledger.py:75-91`（scope 路径）+ `:206` `initialize_agent_work_ledger_artifact` | `runtime_task_id=workflow_run_id` → `runtime_artifacts/long_tasks/<run>/work_ledger.json` |
| owner 标记 | `:559` `assign_todo_owner(agent_id, item_id, owner, ...)` | 纯 ledger write，**不 spawn**（认知≠治理） |
| 回写 | `:595` `record_delegated_todo_status(..., expected_owner=)` | fail-closed owner 校验（不匹配 → PermissionError） |
| 留缝（未实装） | `DELEGATION_END` metadata 无 `ledger_todo_id`；`AgentDelegationRequest` 无此概念 | 形态 A 的最后一根线（§4.2），**非本文范围但建议前置** |
| §8 不变量① | 认知≠治理：track_todo 永不挂 gate/触发执行 | **= 编排层对接协议**：引擎只能单向写 ledger 镜像，绝不读 ledger 驱动控制流 |

### 3.3 Subagent（执行叶子，现成）

`spawn_subagent` / `fanout_subagents` / `SubagentBudget`（rounds/timeout/sources/output 全 enforce）/ fail-closed 工具面 / `consume_signals`（read-once 完成通知）/ 持久 `定义.md` + governed `记忆.md`。**讨论稿 §6.3 "把 delegate_async 叶子多租户化"的工作，轴 1 已做掉大半。**

### 3.4 其他现成件

`action_preflight.evaluate`（外向/不可逆步的门）、`CoordinationCheckpoint`（human-in-the-loop）、coordination postgres backend（`COORDINATION_BACKEND=postgres`）、Redis Streams `event_bus.py`（跨 worker seam，未接）、`quota_guard`（仅入口预检——run 级复检要新建）。

### 3.5 仍然成立的底座债（讨论稿 §3，轴 1 又欠了一笔）

- 🔴 **§7 RLS 安全点**：background asyncio 子任务靠 ContextVar 传 tenant、不走 TenantMiddleware → 可能越界（`调查`级未验证）。**轴 1 的 `run_in_background`/`_BACKGROUND_TASKS`（模块全局）走同一条路** → 此风险从"未来 workflow 才碰"变成"已上线带着"。**先验证（集成测试），若属实先修——优先级高于本文一切。**
- 🔴 无 per-tenant admission/公平；无 run 级跨任务树预算池；fan-out 可绕 user 级配额（`quota_guard.py:24-66` 只入口预检）。
- 🔴 单 worker；重启丢 in-flight；`_async_tasks`/`_BACKGROUND_TASKS` 跨 worker 不可见。
- 引擎设计必须**第一天 tenant 正确**（显式传 tenant_id、journal 带 tenant_id、admission 硬拒），跨 worker 持久化可后置（单 worker 下未爆，但 DB journal 从第一天就为它铺路）。

---

## 4. 取舍：CC 基线 + Hive delta（叠加，非减法）

### 4.1 Delta 决策表

| # | CC 基线 | Hive delta | 理由 |
|---|---|---|---|
| **D1** | 用户显式 opt-in 才跑 workflow | **发起门 = Plan Mode gate**（`action_kind="start_long_task"`，`author_type="workflow"`） | CC 的 opt-in 在企业语境的对应物**就是**已统一的 plan 确认；接法标准化后几乎免费。无人值守（定时 workflow）自动被拦截走 plan → `awaiting_confirmation`，闭环现成 |
| **D2** | named workflow + inline ad-hoc script 双路径 | **只开 registered workflow 路径**：持久具名实体（tenant-scoped，同 subagent `定义.md` 哲学），平台内置起步；**inline ad-hoc 脚本不开**（多租户不能跑租户任意代码） | 不是把脚本降级成 DSL——引擎原语保持 CC 形（phase/pipeline/parallel/agent/budget），收紧的只是"谁能注册控制流代码"。创作治理（租户自定义 workflow 的审核流）是后续独立题 |
| **D3** | journal（引擎私有）≠ Task（协作 ledger），分开 | 同构：**WorkflowRun journal ≠ Work Ledger**；引擎**单向**写 ledger 做进度镜像（todo per step，owner=step），绝不读 ledger 驱动控制流 | 守住 §8 不变量①（认知≠治理）+ 守住确定性（控制流只由 journal+定义决定） |
| **D4** | 单一 budget 池（一个人的额度） | **per-(tenant, run) 预算信封**：`allocated/consumed`，**spawn 时扣**非完成时扣，advisory lock 跨任务树原子；耗尽 → 步失败/降级，admission 硬拒非 WARN | 多租户公平 + 堵 fan-out 绕过 user 配额（§3.5） |
| **D5** | journal = 本地文件 | **journal 进 DB**（tenant_id 列，RLS），done 步幂等跳过 | 跨 worker/重启可恢复的前提；审计（控制中台）免费获得 |
| **D6** | 完成 task-notification 重入 | 复用轴 1 Signal 通道（`subagent_completed` 同款 → `workflow_completed`）；真正调度重入与轴 1 ④ 共享同一后续（wake-consumer） | 不另起炉灶 |

### 4.2 前置（非本文范围，但建议先做）

1. **§7 RLS 验证**：一个集成测试——请求结束后触发 background 子任务（delegate_async + 轴 1 `run_in_background` 两条路都测），断言 DB 会话 `app.current_tenant_id` 仍正确。若漏 → 先修（显式传 tenant + `set_current_tenant`），轴 1、轴 2 都受益。
2. **`ledger_todo_id` 串线**：扩 spawn/delegation 请求契约带可选 `ledger_todo_id` → `DELEGATION_END`/spawn 完成处调 `record_delegated_todo_status`。补完形态 A 最后一根线，几十行，与轴 2 无依赖。

---

## 5. 核心设计

### 5.1 四个新建件

```
┌─ WorkflowDefinition（持久具名实体，tenant-scoped 注册表）
│    name / description / phases / steps（代码控制流，平台侧 Python）/ 默认预算
│    MVP：代码库内置注册（deep research 将是第一个）；租户可见性可配置
│
├─ WorkflowEngine（runtime/workflow_engine.py，薄解释层）
│    步序由定义决定；step 类型：
│      agent_step   → spawn_subagent（轴1 叶子）
│      fanout_step  → fanout_subagents（轴1 并行叶子，per_agent_budget 透传）
│      gate_step    → action_preflight / CoordinationCheckpoint（外向/不可逆步）
│    原语对标 CC：phase / pipeline（默认，无 barrier）/ parallel（barrier）/ loop
│    确定性：控制流里禁时钟/随机；叶子的不确定性被 journal 缓存吸收（CC 同款）
│
├─ WorkflowRun + WorkflowStep journal（DB 表，tenant_id + RLS）
│    run: id/tenant_id/agent_id/definition_name/args_hash/status/budget
│    step: run_id/step_id/phase/input_hash/status/result_ref/started/finished
│    resume = done 且 input_hash 相同的步直接返回 result_ref（CC 前缀缓存的 DB 形）
│    exactly-once 缺口（诚实）：step 落盘与工具副作用不同事务——对外步一律 gate_step
│    （Checkpoint/preflight），只有可逆步允许自动重试（用 preflight 的可逆性分级判定）
│
└─ Run 预算信封（workflow_quotas：tenant_id/run_id/allocated/consumed）
     spawn 时预扣；超额 admission 硬拒；与叶子级 SubagentBudget 两级配额
```

### 5.2 生命周期（全部走现成件）

```
发起（agent 工具 start_workflow / API / trigger 定时）
  → ToolMeta.plan_gate_action_kind="start_long_task" → PlanModeGate.check
     （needs_plan → fail-closed 上抛；无人值守自动转主循环 Plan Mode）
  → 创建 WorkflowRun(journal) + initialize_agent_work_ledger_artifact(runtime_task_id=run_id)
执行
  → 引擎逐 phase/step；agent/fanout 步 = 轴1 叶子（治理/隔离/budget 全继承）
  → 每步前后写 journal；进度单向镜像 ledger（todo: owner=step, status 流转）
  → 对外步过 gate_step（preflight / Checkpoint 人审）
完成/失败
  → run 终态落 journal + RuntimeTask；投递 workflow_completed Signal（轴1 ④ 通道）
恢复
  → kill/重启/编辑定义后 resume(run_id)：done+同 hash 步秒过，断点续跑
```

### 5.3 与 deep research 的关系（最后一步，写明白防漂移）

DR 接入 = **第一个 registered WorkflowDefinition**：`plan → fanout(explorer×N) → synthesize(worker) → critic(只读)`。届时 `_run_worker_fanout`、`controller.py` 预算循环、RC13 的 COVERAGE 散文全部被引擎吸收（`for dim: assert` 落进 step 断言）。**但按既定次序：轴 2 切口全部落地并验证后才动 DR，本文不含任何 DR 改造。**

---

## 6. 增量切口（草案，待拍板）

0. **（前置，独立）**§7 RLS 集成测试验证（+若漏则修）；`ledger_todo_id` 串 spawn（形态 A 收尾）。
1. **Walking skeleton**：WorkflowRun/Step 表 + 最小引擎（顺序 phase/step，叶子=`spawn_subagent`）+ plan gate 发起 + **kill-resume 集成测试（Testcontainers 真 Postgres）**——一次立住"代码控制流 + journal 真持久 + 分步 resume"三件事。
2. **并发 + 预算**：fanout_step（接 `fanout_subagents`）+ pipeline/parallel 原语 + run 预算信封（spawn 时扣 + admission 硬拒）。
3. **Registered 定义 + 镜像**：WorkflowDefinition 注册表（tenant-scoped）+ ledger 进度镜像 + `workflow_completed` Signal。
4. **gate_step**：action_preflight 接入 + Checkpoint 人审步 + 可逆性分级重试策略。
5. **跨 worker 持久**（后续）：Redis Streams/DB 驱动 + lifespan 优雅 drain。
6. **（最后）DR 接入**：第一个 registered workflow（独立阶段，另行拍板）。

---

## 7. 非目标 / 不变量

- **非目标**：形态 A（已被三件套覆盖，只做 §4.2 串线）；租户自定义 workflow 创作流（后续治理题）；改 Coordinator Mode / 对话 agent；本阶段不动 deep research。
- **不变量**：① 叶子必须是轴 1 原语（治理/隔离/budget 继承，不开第二条执行路）；② journal ≠ ledger，单向镜像，引擎绝不读 ledger 驱动控制流（§8 不变量①）；③ 发起必过 plan gate（needs_plan fail-closed）；④ 对外/不可逆步必过 gate_step，不自动重试；⑤ journal/quota 第一天带 tenant_id；⑥ 增量演进，每切口独立可回滚。

---

## 8. 待决问题（本轮讨论要拍的）

1. **D2 收口**：只开 registered workflow、不开 inline ad-hoc 脚本——这刀认不认？（认 = 多租户安全优先；代价 = agent 不能即兴写 workflow，即兴归形态 A）
2. **切口 0 时机**：§7 验证 + `ledger_todo_id` 串线，现在先做掉，还是与切口 1 并行？
3. **WorkflowDefinition 的 MVP 载体**：代码库内置 Python 注册表（部署发布，最简最安全）vs DB 行级注册（运行时可管理，复杂）？我倾向前者起步。
4. **journal 粒度**：step 级（本文设计）vs CC 的 agent() 调用级（fanout 内每叶一条）？fanout 步建议落叶级子记录，否则 8 叶跑 7 断 1 的 resume 会整步重跑。
5. **预算信封单位**：只 token，还是 token + 叶子数 + 墙钟时长三维？

---

> **状态**：v0 草稿（2026-06-03，三件套落地后重写）。CC 基线 7 要素为第一手 Fact；Hive 接缝 file:line 为本轮调查；§3.5 底座债沿讨论稿 v0.1 并更新轴 1 现状。待 §8 五问拍板后升 v1。

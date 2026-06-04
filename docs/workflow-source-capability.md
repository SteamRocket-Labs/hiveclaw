# Workflow 源能力设计：Hive 确定性执行编排底座（轴 2）

> **定位**：Workflow 是 Hive 的 **runtime 基础能力**，与 Plan Mode **并列**的底座——不是 subagent / deep research / office / Work Ledger 的附属方案。
>
> **状态**：**v0.4 草稿（v1 决策已按 review 收紧，待实现设计）**。v0 的两处主干被推翻重写（见 §0.2）；前置调研 `docs/workflow-vs-skill-a2a-discussion.md`（v0.1）。
>
> **方法论**：Hive = CC superset——先对标 CC 基线，再叠 Hive delta，**绝不做减法、不自创范式**。
>
> **次序铁律（用户拍板）**：轴 2 讨论 → 设计 → 实装；**deep research 接入是最后一步**。

---

## 0. 主旨（用户拍板，2026-06-03）

1. **Workflow 是 Hive 的确定性执行编排基础能力，与 Plan Mode 并列。**
2. **Plan Mode 管确认边界**（能不能开始执行、是否需要人确认、计划怎么被确认）；**Workflow 管执行控制流**（执行开始后，控制流如何确定性推进、暂停、恢复、审计）。两者可组合，**不能混成一个概念**——Workflow run 可以被 plan gate 拦住，但 Workflow 本身不是 Plan Mode 的一个 action type。
3. **Workflow 不属于 subagent、deep research、office 或 Work Ledger**；这些只是它的**调用方、叶子执行器或观察面**。
4. **同一套 runtime engine，支持 ephemeral 与 registered 两种 definition 来源**——不是两条 runtime。

### 0.2 对 v0 的修正记录（诚实留痕）

- ~~v0-D1"发起门 = Plan Mode gate"~~ → **降级为 integration（§6.1）**：那种写法把 Workflow 矮化成 plan gate 下的 action type，违反主旨 2。
- ~~v0-D2"只开 registered、不开 inline ad-hoc"~~ → **推翻**：CC 基线本来就是 inline script（临时）+ named workflow（注册）**双路径**，砍 inline 是减法、违反 superset 方法论。正确的 delta 是**换载体不砍路径**：CC 的"任意 JS"在多租户下换成**结构化 definition 数据**（§3.2），ephemeral 与 registered 都保住。
- ~~v0-§8 问 3"definition 载体 = 代码库内置 Python 注册表"~~ → **连带改判**：Python 模块当载体则 ephemeral / promote / fork 全部无法成立；**definition 必须是可序列化数据**。
- ~~v0"DR = 第一个 registered workflow"放核心~~ → 降级为 integration example（§6.5），且按次序铁律最后做。

---

## 1. 职责分界：两个并列底座 + 两种编排形态

### 1.1 Plan Mode × Workflow（并列，组合不混淆）

| 底座 | 回答的问题 | 不回答的问题 |
|---|---|---|
| **Plan Mode** | 能不能开始？要不要人确认？计划怎么被确认？ | 开始之后怎么跑 |
| **Workflow** | 开始之后控制流如何**确定性**推进 / 暂停 / 恢复 / 审计？ | 要不要开始、谁来批 |

组合关系（§6.1）：重型 / 无人值守的 workflow run 发起**可以**被 plan gate 拦；ephemeral definition 的"确认后运行"**可以**挂在 plan 确认面上。但这些都是集成，不进定义。

### 1.2 两种编排形态（沿 v0，本文只管 B）

| 形态 | 控制流归属 | 组成 | 状态 |
|---|---|---|---|
| **A：即兴 swarm** | LLM | Work Ledger todo + owner + spawn/fanout | ✅ 三件套已齐，差 `ledger_todo_id` 一根线（§7 前置） |
| **B：确定性管线** | **代码（引擎）** | 本文 | ❌ 空白，本文标的 |

范围纪律：Workflow 只用于确定性关键管线；对话型 / 开放探索归形态 A，不碰。

---

## 2. CC 基线（第一手 Fact，8 要素）

1. **definition = 控制流载体**：`meta {name, description, phases}` + 控制流体；原语 `phase / log / args`。代码决定每一步，LLM 不参与控制流。
2. **`agent()` = 隔离叶子**：spawn worker，只回结论（文本或 schema 结构化）；单叶失败 → null（失败隔离）。
3. **结构化并发**：`pipeline`（无 barrier，默认）/ `parallel`（barrier）/ 组合模式（loop-until-dry、judge panel）。
4. **`budget` 硬顶池**：跨 run 全部 agent 共享，耗尽即 throw。
5. **确定性 → journal → resume**：禁时钟/随机；journal 每个叶子调用；resume = 未改前缀缓存秒回。
6. **异步 + 完成重入**：后台跑，完成通知重入主循环。
7. **双 definition 来源**：**inline script（临时）+ named workflow（持久文件，按名 + args 调用）** —— ephemeral / registered 的雏形就在 CC 基线里。
8. **入口纪律**：用户显式 opt-in，agent 不能自发起规模化 run。

---

## 3. 核心设计

### 3.1 一套引擎，两种 definition 来源

```
WorkflowEngine（唯一 runtime）
  ├─ EphemeralWorkflowDefinition    # 临时：agent 按本次任务意图生成，确认后运行
  │     一次性；随 run 存档；可 replay；可 promote
  └─ RegisteredWorkflowDefinition   # 固定：平台/组织/agent 持久注册，可复用
        有名字、有版本、有 owner、有权限策略；可被反复调用；可 fork
```

**两种来源共享同一套（绝不分叉）**：

- run journal / step journal
- budget envelope
- resume
- gate step
- tenant boundary
- audit

**差异只在 definition lifecycle**：

```
ephemeral ──稳定复用（如连续 3 次同类）──▶ promote ──▶ registered
registered ──本次需要微调──▶ fork ──▶ ephemeral run
```

### 3.2 definition 载体 = 结构化数据（ephemeral 安全存在的前提）

- definition 是**可序列化的结构化数据**（非任意代码）：phases / steps（类型：agent_step、fanout_step、gate_step）/ 叶子引用（SubagentSpec 或持久 `定义.md` 名）/ 默认预算 / args schema。
- 它**只能编排平台原语 + 受治理的叶子**——没有任意代码执行面，所以 agent 在运行时可以临时生成一份 definition（生成的是数据，执行的是引擎）。
- 安全不来自"JSON 本身"，而来自 **compile/admission 校验**：step type allowlist、leaf capability binding、tenant/agent visibility check、预算预检、fanout cap、以及对外/不可逆步骤强制 `gate_step`。未通过校验的 definition 不能进入 run。
- Hive delta 的本质：CC 用"任意 JS"换灵活性（单机单用户付得起）；Hive 用"结构化数据 + 引擎解释"换多租户安全与可审计，**两条路径（临时/注册）一条不少**。
- 表达力边界：v1 = sequence + bounded fanout/map + structured condition + gate + wait_until/time suspend；不开放任意 loop / 任意代码 / 动态生成新 step；`wait_signal` 留到 persistent signal-resume consumer 建成后再进 v2（详见 §9）。

### 3.3 run / journal（融合既有执行账本，不另开账本）

- **WorkflowRun = `RuntimeTask(task_type="workflow")` + WorkflowStep journal 子表**（run_id / step_id / phase / input_hash / status / result_ref / tenant_id）。不平行造第五种后台执行记账（现状已有 4 种变体：trigger 执行 / delegate_async / execute_task / DR+subagent background——见盘点）。
- **resume** = done 且 input_hash 相同的步直接返回 result_ref（CC 前缀缓存的 DB 形）；startup 扫 journal 续跑（对标 `resume_persisted_async_delegations` 先例）。
- run 状态机：`created → running → suspended（gate / budget 耗尽 / 等外部）→ completed | failed`；`sleep_until` / `delay_until` 这类时间挂起可用 once trigger 恢复（§6.2），gate approval / budget refill 分别由 Checkpoint 审批、quota/admission 恢复。`wait_signal` 不进 v1：现有 Signal 可 read-once，但缺少"Signal 到达 → 恢复 suspended WorkflowRun"的持久 consumer。
- **exactly-once 缺口（诚实）**：step 落盘与工具副作用不同事务——对外/不可逆步一律 gate_step，仅可逆步允许自动重试（用 action_preflight 可逆性分级判定）。
- 完成通知：`workflow_completed` Signal（复用轴 1 通道）。

### 3.4 四个新建件

1. **definition schema + 解析校验**（结构化数据，§3.2）
2. **WorkflowEngine**（`runtime/workflow_engine.py`，薄解释层：phase / sequence / bounded map-fanout / structured condition / gate / wait_until；叶子 = `spawn_subagent` / `fanout_subagents`）
3. **WorkflowStep journal 子表**（挂 RuntimeTask 子类型下）
4. **run 预算信封**（`workflow_quotas(tenant_id, run_id, allocated, consumed)`，spawn 时预扣，admission 硬拒；与叶子级 SubagentBudget 两级配额）

---

## 4. 办公场景的产品路径（一个心智模型，三个阶段）

用户**不应该**看到"临时 workflow / 固定 workflow"两个入口。产品上是一个东西：**自动化流程 / 工作流**。内部是同一条成熟路径的三个阶段：

```
一次性编排 ──▶ 可复用模板 ──▶ 定时/触发自动化
(ephemeral)    (promote→registered)   (registered + trigger)
```

- "帮我处理这批合同：先 OCR，再提取条款，再生成风险表" → **ephemeral**（agent 生成 definition，确认后运行一次）。
- "以后每周一自动汇总销售周报" → **registered + cron trigger**。
- 用户连续 3 次跑同类临时流程 → 系统建议"保存为模板/自动化"（**promote 建议**）。
- promote 建议的感知层种子已在代码里：`skill_distiller.WorkflowSignature` + `fast_reflection_service` 的 `repeated_workflow_signature` metadata 通道（重复任务模式识别）——接 North Star 自我进化（方向，见 §6.6，非本期）。

---

## 5. 多租户底座（硬约束，沿 v0.1 调研并更新）

- 🔴 **§7 RLS 安全点先验证**（background asyncio 子任务 ContextVar tenant 可能越界；轴 1 `run_in_background` 已上线带着同款风险）——**优先级高于本文一切**（§7 前置）。
- 引擎**第一天 tenant 正确**：journal / quota 带 tenant_id（RLS）；显式传 tenant 进叶子；per-(tenant, run) admission **硬拒**非 WARN；fan-out 不得绕 user 级配额。
- 跨 worker 持久化可后置（单 worker 未爆），但 DB journal 从第一天为它铺路；Redis Streams `event_bus` 是现成 seam。

---

## 6. 集成面（全部是 integration / example，不进核心定义）

| # | 集成对象 | 关系 | 接线（现有 seam 标 file:line；拟新增 schema 明确标出） |
|---|---|---|---|
| 6.1 | **Plan Mode** | 组合：确认边界 × 执行控制流 | 重型/无人值守 run 发起被 gate（`PlanModeGate.check`，`plan_mode_gate.py:81`；工具层 `ToolMeta.plan_gate_action_kind`）；ephemeral"确认后运行"可挂 plan 确认面（definition 预览随 authored plan；`author_type="workflow"` 缝已在 `plan_mode_service.py:338`）；无人值守自动转主循环 Plan Mode（`session.py:260` / `invoker.py:172`） |
| 6.2 | **触发层（6 类，一个不加）** | trigger 是 workflow 的一个**调用方** | 时间组 cron/once/interval + 事件组 webhook/poll/on_message 全覆盖"何时开始"；拟新增融合点 = fire 后的 payload 分支：`trigger.config.workflow_ref={definition_name, definition_version, definition_hash, args}` → 引擎 start/resume，无 ref → 现状散文 ReAct（`_invoke_agent_for_triggers` 一个分支）；创建带 ref 的 enabled trigger 走既有 `create_enabled_trigger` gate，**授权绑定 creation-time 的 definition_version + definition_hash**；fire 时必须校验 version/hash，mismatch → suspend / needs re-confirmation，不能静默运行新版 definition；once 只作为 `sleep_until` / `delay_until` 的时间恢复点（`fire_count==0`），不承载 approval/budget/signal 恢复；webhook `_webhook_payload` → run args |
| 6.3 | **Subagent（轴 1）** | **叶子执行器** | agent_step → `spawn_subagent`；fanout_step → `fanout_subagents`（per_agent_budget 透传）；治理/隔离/budget/Signal 全继承，不开第二条执行路 |
| 6.4 | **Work Ledger** | **观察面**（单向） | run 创建时 `initialize_agent_work_ledger_artifact(runtime_task_id=run_id)`（`agent_work_ledger.py:206`）；step 进度镜像 todo（`assign_todo_owner:559` / `record_delegated_todo_status:595`）；**引擎绝不读 ledger 驱动控制流**（cognitive-scaffold §8 不变量①认知≠治理） |
| 6.5 | **office / deep research** | **调用方 example** | office = §4 三阶段的主场景；DR = 未来的一个 registered definition（plan→fanout(explorer×N)→synthesize→critic，吸收 `_run_worker_fanout` / `controller.py` 预算循环 / RC13 散文）——**最后做，另行拍板** |
| 6.6 | **自我进化** | promote 建议机制 | "N 次同类 ephemeral → 建议 promote"接 `skill_distiller.WorkflowSignature` 感知 + `fast_reflection_service` 的 `repeated_workflow_signature` 通道 + evolution_ledger 审计——**方向，非本期** |

---

## 7. 前置（独立于本文，先做）

1. **§7 RLS 验证**：集成测试——请求结束后触发 background 子任务（delegate_async + 轴 1 `run_in_background` 两条路），断言 DB 会话 tenant 作用域正确；漏则先修。
2. **`ledger_todo_id` 串线**：spawn/delegation 请求带可选 ledger_todo_id → 完成回写（形态 A 收尾，几十行）。

---

## 8. 增量切口（草案 v0.4，按 §9 决策更新）

0. **（前置）**§7 RLS 验证（+修）；`ledger_todo_id` 串线。
1. **Walking skeleton**：definition 数据 schema（sequence + structured condition 的最小结构）+ 最小引擎 + `RuntimeTask(task_type="workflow")` run + Step journal + **kill-resume 集成测试（Testcontainers 真 Postgres）**——一次立住"数据 definition + 代码控制流 + journal 真持久 + 分步 resume"。skeleton 跑的 definition 既可临时构造也可文件加载——**两种来源天然同时打通**（同一引擎的必然结果）。`gate_step` / `wait_until` 不在 skeleton 里做 schema 假承诺，随切口 4 一起落行为。
2. **并发 + 预算**：bounded fanout/map + leaf-level journal + run 预算信封（预扣 + admission 硬拒）。
3. **definition lifecycle + 权限**：ephemeral 随 run 存档 / replay；registered 注册表（name/version/hash/owner/visibility_scope/call_policy/status，tenant-scoped）；**promote / fork 原语**；promote 只允许 agent 建议 + owner/admin 审批。
4. **gate_step + wait_until + 集成接线**：action_preflight / Checkpoint 人审步；`wait_until` / `delay_until` 经 once trigger 恢复；plan 组合（6.1）；trigger payload 分支 + once 恢复点（6.2）；ledger 镜像（6.4）；`workflow_completed` Signal。`wait_signal` 仍不承诺自动恢复。
5. **跨 worker 持久**（后续）：Redis Streams / DB 驱动 + lifespan 优雅 drain。
6. **（最后）DR 接入**：独立阶段，另行拍板。

（办公产品面——模板建议 UI / 自动化管理页——属前端/产品文档，不在本 backend 文档切口内。）

---

## 9. 非目标 / 不变量 / v1 决策

**非目标**：形态 A（已被三件套覆盖）；改 Coordinator Mode / 对话 agent；本阶段不动 deep research；租户图灵完备脚本（载体已定数据）。

**不变量**：① definition = 结构化数据，**无任意代码执行面**；② 叶子必须是轴 1 原语（治理/隔离/budget 继承）；③ ephemeral 与 registered **共享同一引擎/journal/budget/resume/gate/tenant/audit，绝不分叉两条 runtime**；④ journal ≠ ledger，单向镜像；⑤ 对外/不可逆步必过 gate_step，不自动重试；⑥ journal/quota 第一天带 tenant_id；⑦ 增量演进，每切口独立可回滚。

**v1 决策（2026-06-03）**：
1. **definition 表达力边界**：v1 支持 `sequence`、bounded `fanout/map`、structured `condition`、`gate`、`wait_until`/time suspend。允许 `retry(max_attempts=...)`，但只允许可逆步骤重试；不开放 `wait_signal`、任意 loop、任意 Python/JS 表达式、动态生成新 step。
2. **condition 谓词形态**：谓词是结构化比较 AST，不是字符串表达式。原子形态为 `{field, op, value}`，`field` 只能指向 `args` 或 structured step output，`op ∈ {eq, ne, gt, lt, gte, lte, contains, exists, in}`；布尔组合仅 `and/or/not`，且嵌套深度有上限。禁止 `eval`、Jinja、Python/JS 表达式、模板求值或任意解释器。
3. **ephemeral 的"确认后运行"分级**：按风险分级，不按 ephemeral/registered 分级。低风险 ephemeral 可走对话内 definition preview + 用户确认；外部可见、不可逆/敏感、创建/启用 trigger、高预算/高 fanout/长时运行、跨 agent/org/company 资源、或 promote 成 registered 时，必须走 Plan Mode 确认面。预算、fanout、时长阈值进入配置，不硬编码。
4. **promote 权限**：agent 只能建议 promote，不能自行 promote。流程是 repeated ephemeral evidence → promote proposal → 用户/owner/admin 审批 → compile/admission/capability check → registered workflow version/hash → audit log。
5. **registered 权限模型**：拆成可见性与可执行性。`visibility_scope = agent | org | tenant | platform`；`call_policy = allowed_agents / allowed_roles / allowed_orgs`；`owner_type = user | agent | org | tenant | platform`；`status = draft | active | deprecated | revoked`；`definition_version` + `definition_hash` immutable。registered workflow 可见不等于可执行，leaf capability 仍必须逐 run 校验。`platform` scope 只能是出厂策展只读模板，不能由租户产物聚合生成。
6. **journal 粒度**：fanout 必须 leaf-level journal。结构为 `RuntimeTask(task_type="workflow") -> WorkflowStep -> WorkflowLeafCall`；`WorkflowLeafCall` 至少记录 `run_id`、`step_id`、`leaf_id`、`input_hash`、`definition_hash`、`status`、`result_ref`、`token_usage`、`error`、`started_at`、`finished_at`、`idempotency_key`，避免 8 叶跑完 7 叶后整步重跑。

---

> **状态**：v0.4 草稿（2026-06-03 重定位：与 Plan Mode 并列底座 + ephemeral/registered 双来源同引擎 + 集成面全部降级 + §9 v1 决策已收紧）。§0.2 留痕 v0 被推翻处。下一步是按 §8 切口进入前置验证与 walking skeleton 设计。

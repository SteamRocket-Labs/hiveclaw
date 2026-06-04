# Workflow 源能力设计：Hive 确定性执行编排底座（轴 2）

> **定位**：Workflow 是 Hive 的 **runtime 基础能力**，与 Plan Mode **并列**的底座——不是 subagent / deep research / office / Work Ledger 的附属方案。
>
> **状态**：**v0.6 草稿（v1 决策已按 review 收紧，完整实现路线已排，并补齐执行路线缺口）**。v0 的两处主干被推翻重写（见 §0.2）；前置调研 `docs/workflow-vs-skill-a2a-discussion.md`（v0.1）。
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
- 表达力边界：v1 = sequence + bounded fanout/map + structured condition + gate + wait_until/time suspend；P11 后 v2 额外开放 `wait_signal_step`（前提：PG-backed Signal + persistent signal-resume consumer）；仍不开放任意 loop / 任意代码 / 动态生成新 step。

### 3.3 run / journal（融合既有执行账本，不另开账本）

- **WorkflowRun = `RuntimeTask(task_type="workflow")` + WorkflowStep journal 子表**（run_id / step_id / phase / input_hash / status / result_ref / tenant_id）。不平行造第五种后台执行记账（现状已有 4 种变体：trigger 执行 / delegate_async / execute_task / DR+subagent background——见盘点）。
- **resume** = done 且 input_hash 相同的步直接返回 result_ref（CC 前缀缓存的 DB 形）；startup 扫 journal 续跑（对标 `resume_persisted_async_delegations` 先例）。
- run 状态机：`created → running → suspended（gate / budget 耗尽 / 等外部）→ completed | failed`；`sleep_until` / `delay_until` 这类时间挂起可用 once trigger 或等价调度记录恢复（§6.2），gate approval / budget refill 分别由 Checkpoint 审批、quota/admission 恢复。P11 后 `wait_signal_step` 由 PostgreSQL `coordination_signals` + persistent signal-resume consumer 恢复；in-process Signal 仅保留为测试/本地通知通道。
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

## 8. 增量切口摘要（权威路线见 §9）

本节只保留路线索引，不作为第二张执行计划。后续执行、验收、测试命令和 Red tests 以 §9 的 P0-P15 为准；若本节和 §9 冲突，§9 胜出。

| 旧切口 | 对应 §9 | 摘要 |
|---|---|---|
| 切口 0 | P0 | RLS、background tenant、`ledger_todo_id` 串线 |
| 切口 1 | P1-P4 | model/migration、schema/compiler、walking skeleton、ephemeral launch |
| 切口 2 | P5 | bounded fanout/map、leaf-level journal、run quota |
| 切口 3 | P6 | registered lifecycle、promote、fork、version/hash |
| 切口 4 | P7-P9 | gate_step、wait_until、trigger integration、ledger mirror、audit |
| 切口 5 | P10-P11 | cross-worker persistence、drain、wait_signal v2 前置机制 |
| 切口 6 | P14 | Deep Research 迁移试点，最后做且 feature-flagged |
| 产品面 | P12-P13 | workflow creation/run UI 与 Office 场景化入口 |

---

## 9. 全量任务路线（实现全部功能）

路线原则：

- **TDD 固定顺序**：每个切口先写失败测试（Red），再实现最小代码（Green），最后清理结构（Refactor）。
- **每阶段可独立回滚**：不把 schema、engine、trigger、DR 迁移绑成一个大提交。
- **先底座后场景**：Workflow runtime 全能力完成前，不接 Deep Research；office / DR 都只能作为调用方 example。
- **先单 worker 正确，再跨 worker 持久**：DB journal / quota / hash pinning 第一天就正确；跨 worker consumer 和 drain 放后段。
- **真持久化必须真 PG 验收**：涉及 migration、RLS、journal、advisory lock、worker lease、cross-worker resume 的测试必须使用 Testcontainers 真 PostgreSQL fixture；mock session 只允许测 pure logic 或无 DB 控制流。

### P0 前置安全债：RLS + ledger_todo_id

> **✅ 完成（2026-06-04）** — 证据：`pytest tests/ -q` → **3455 passed, 7 skipped**（基线 3436 → +19：10 ledger 串线 + 9 真 PG integration）；`ruff check` clean。
>
> **交付物**：
> - **Testcontainers 真 PG fixture**（`tests/integration/conftest.py`）：PostgreSQL 16 容器 + `alembic upgrade head` 全链 + 非 owner `rls_app_user` 角色（NOBYPASSRLS）+ macOS Docker Desktop socket 自动发现。P1/P3/P5/P10 复用。
> - **`tenant_scoped_session()`**（`app/database.py`）：background 任务的 RLS GUC 固定原语；orchestrator `_delegation_plan_gate_allows` / `_resolve_resumable_target_runtime` / async `_run`、subagent `_run_and_signal` 已接线。
> - **`ledger_todo_id` 串线**（`AgentDelegationRequest.ledger_todo_id` + `delegate_to_agent`/`delegate_async`/`spawn_subagent`/`spawn_subagent_from_definition`）：spawn 时 `assign_todo_owner` 盖章（delegation owner=child agent id；subagent owner=spec name），完成回写 `record_delegated_todo_status`（成功→completed，失败→释放回 pending），owner 不匹配 fail-closed（`PermissionError`，ledger 是观察面绝不阻断执行）。测试 `tests/agents/test_{orchestrator,subagent}_ledger_todo.py` 10 绿。
>
> **验证中发现并修复的缺口（真 PG 才暴露，monkeypatch 永远测不出）**：
> - **缺口 A（已修）**：空库 `alembic upgrade head` 走 `db_bootstrap` 捷径（`create_all` + stamp head，跳过所有迁移）→ **每个全新部署都没有任何 RLS policy**。修复 = `db_bootstrap.apply_rls_policies()`（policy 形状与 `add_row_level_security` migration 完全一致，幂等，非 PG 方言 no-op）。
> - **缺口 B（锚定，P1 修）**：9 张租户表全部 ENABLE-only、无 `FORCE ROW LEVEL SECURITY`，而生产 `DATABASE_URL` 用户就是建表 owner → **现网所有连接实际绕过 RLS**。`test_table_owner_bypasses_enable_only_rls_documented_gap` 把现状钉死；P1 的 workflow 新表第一天 FORCE，存量表 FORCE 需先全面接线 `tenant_scoped_session`（直接 FORCE 会让所有无 GUC 的 background session 失明）。
> - **缺口 C（已修）**：`alembic/env.py` 漏 import `ConfigRevision` → bootstrap 库缺 `config_revisions` 表，新部署的 config_versioning 服务必炸。
> - **缺口 D（已修）**：`delegate_async` 收 `tenant_id` 参数但从不传入 `AgentDelegationRequest` → background `_run` 全程拿不到发起租户。

目标：先消除会放大到 Workflow 的现有底座风险。

改动面：
- `backend/app/database.py` / tenant ContextVar 使用点：验证 background task DB 会话是否仍带正确 tenant。
- `backend/app/agents/orchestrator.py`、`backend/app/agents/subagent.py`：请求 metadata 带可选 `ledger_todo_id`。
- `backend/app/services/agent_work_ledger.py` / `runtime/hooks_setup.py`：完成时按 `ledger_todo_id` 回写 parent ledger。

Red tests：
- background delegation 请求结束后，子任务 DB session 的 `app.current_tenant_id` 仍等于发起 tenant。
- `run_in_background=True` subagent 完成后不会写错 tenant。
- delegation/subagent 带 `ledger_todo_id` 时，完成只回写 owner 匹配的 todo；owner 不匹配 fail-closed。

验收命令：

```bash
cd backend
pytest tests/agents/test_subagent_*.py tests/agents/test_orchestrator_*.py tests/services/test_agent_work_ledger*.py
```

### P1 数据模型与迁移

> **✅ 完成（2026-06-04）** — 证据：`pytest tests/ -q` → **3467 passed**（+12：`tests/migrations/test_workflow_migration.py` 7 + `tests/models/test_workflow_models.py` 6，含共享 fixture 复算）；后续 P11 安全补丁后 `alembic heads` 单 head = `coordination_rls_0604`；ruff clean。
>
> **交付物**：
> - `app/models/workflow.py`：`WorkflowDefinitionRecord` / `WorkflowStep` / `WorkflowLeafCall` / `WorkflowQuota` 四 model；run = `RuntimeTask(task_type="workflow")`，run metadata（definition_source/hash/args_hash/confirmed_plan_id/tenant_id 镜像）走 `metadata_json` 不加列。
> - `alembic/versions/add_workflow_tables_0604.py`：四表 + tenant/run/status 索引 + **RLS ENABLE+FORCE+policy**（workflow 表族第一天 FORCE，闭 P0 缺口 B 的新表侧）。
> - `db_bootstrap.apply_rls_policies` 增加 `RLS_FORCED_TENANT_TABLES` 组：bootstrap（create_all+stamp）路径建出的 workflow 表同样 ENABLE+FORCE。
> - **双部署路径合同一致并分别验证**：升级路径用 `chain_migrated_pg_url` fixture（先 bootstrap 全库→拆四表→回拨 alembic_version 至上一 head→重跑 `upgrade head`，真执行 migration DDL）；新部署路径用 bootstrap fixture。两者断言同一合同（表存在 + relrowsecurity + relforcerowsecurity + policy）。
> - 唯一约束锚定 immutability：`(tenant_id, name, definition_version)` 重复插入 IntegrityError（改内容必须发新 version）；step/leaf/quota 唯一约束 + leaf `idempotency_key` per-run 唯一。
>
> **过程中发现并修复**：
> - **schema drift 实锤**：migration 写了 `server_default` 而 model 只有 Python 端 `default` → create_all（bootstrap 路径）建的表缺 server_default，原生 INSERT 即炸。已对齐（model 补 server_default）；`tests/models` 的 ORM 写入即双路径 drift 哨兵。
> - **缺口 B 再深一层（P15 锚定）**：容器/生产的 `POSTGRES_USER` 初始化用户是 **SUPERUSER**，superuser 完全绕过 RLS（FORCE 也无效）。`test_superuser_bypasses_even_forced_rls_documented_gap` 钉死现状 → **P15 部署必须把应用 DSN 切到非 superuser 角色**（FORCE 已就位，切角色即生效）。隔离语义已用非 superuser `rls_app_user` 角色全部验证（cross-tenant 不可见 / 空 GUC fail-closed / WITH CHECK 拒绝错租户写入）。

目标：把 Workflow 作为 RuntimeTask 上的一等 run 记录，不另造第五套后台账本。

改动面：
- 新增 `backend/app/models/workflow.py`：`WorkflowStep`、`WorkflowLeafCall`、`WorkflowQuota`、`WorkflowDefinitionRecord`。
- `RuntimeTask.task_type="workflow"` 作为 run 入口；run metadata 存 `definition_source`、`definition_hash`、`args_hash`、`confirmed_plan_id`、`tenant_id` 镜像。
- Alembic migration：`workflow_steps`、`workflow_leaf_calls`、`workflow_quotas`、`workflow_definitions`，全部带 `tenant_id` / 索引 / RLS policy。
- `WorkflowDefinitionRecord` 的 `definition_version + definition_hash` immutable；`status` 支持 `draft | active | deprecated | revoked`。

Red tests：
- `alembic heads` 单 head。
- workflow run/step/leaf/quota 均 tenant-scoped，跨 tenant 查询不可见。
- registered definition 改内容必须生成新 version/hash，不能原地修改 active version。
- migration、RLS policy、tenant-scoped 查询必须跑 Testcontainers 真 PostgreSQL fixture；没有 fixture 时本阶段先建设 fixture。

验收命令：

```bash
cd backend
alembic heads
pytest tests/migrations/test_workflow_migration.py tests/models/test_workflow_models.py
```

### P2 Definition schema + compiler/admission

> **✅ 完成（2026-06-04）** — 证据：`pytest tests/ -q` → **3498 passed**（+31：definition 12 + compiler 9 + admission 10）；纯 Functional Core 无 mock，0.09s；ruff clean。
>
> **交付物**：
> - `runtime/workflow_definition.py`：Pydantic v2 schema（`extra="forbid"` 全模型拒未知字段）；step 判别联合 `agent_step|fanout_step|gate_step|wait_until_step`；condition = `{field,op,value}` 原子 + `all/any/not` 组合、深度≤5、field 只允许 `args.*`/`steps.<id>.*`；`compute_definition_hash` = canonical JSON（sort_keys）SHA-256，字段顺序无关、内容敏感、parse 往返稳定（`canonical_dict` 用 `exclude_unset` 保 roundtrip hash 相等）。
> - `runtime/workflow_compiler.py`：跨步引用检查（condition/task 模板 `{{...}}` 只能引用 args 或**先前** step，向前/悬空引用拒）；leaf catalog binding（`known_leaves` 注入时未列名单拒）；`effects=external|irreversible` 步必须有前置 gate_step（§10 不变量⑤）；retry 只可逆（irreversible+retry 拒）；fanout `items_from` 必须 `args.*`。
> - `runtime/workflow_admission.py`：args 校验（required/type/未知拒）→ budget cap → leaf 授权 → fanout items/并发 cap → planned leaf calls 总数 cap → wall-clock cap；全部 HARD-REJECT 非 WARN（§5）。
> - 阈值进 `Settings`（`WORKFLOW_MAX_RUN_BUDGET_TOKENS/MAX_FANOUT_ITEMS/MAX_CONCURRENCY/MAX_LEAF_CALLS/MAX_WALL_CLOCK_SECONDS`），env 可覆盖，零硬编码。
> - **字符串表达式封死**（红测试钉死）：`when: "output.risk > 3"`、`{"eval": ...}`、未知 op（如 `regex_exec`）、`__import__` 路径全部 schema 层拒绝；task 占位符 `{{args.x}}` 是纯 key 替换（P3 引擎实现），非表达式求值。

目标：让 ephemeral / registered 都先经过同一个编译与准入层，确保"结构化数据"不变成隐形代码执行面。

改动面：
- 新增 `backend/app/runtime/workflow_definition.py`：schema dataclass / Pydantic model、canonical JSON、`definition_hash`。
- 新增 `backend/app/runtime/workflow_compiler.py`：step allowlist、structured condition AST、leaf capability binding、visibility check、args schema check。
- 新增 `backend/app/runtime/workflow_admission.py`：预算、fanout、墙钟阈值、leaf count、tenant quota 预检；阈值来自 config，不硬编码。
- 明禁字符串表达式：不允许 `eval`、Jinja、Python/JS expression、模板求值。

Red tests：
- 合法 `sequence + condition` definition 编译通过并生成稳定 hash。
- `condition: "output.risk > 3"` 这类字符串表达式被拒。
- `{field, op, value}` 以外的谓词 shape 被拒。
- fanout 超阈值、高预算、未授权 leaf capability 被 admission 拒绝。
- 同一 JSON 不同字段顺序 hash 相同；内容变化 hash 变化。

验收命令：

```bash
cd backend
pytest tests/runtime/test_workflow_definition.py tests/runtime/test_workflow_compiler.py tests/runtime/test_workflow_admission.py
```

### P3 Walking skeleton：sequence + structured condition + kill-resume

> **✅ 完成（2026-06-04）** — 证据：`pytest tests/ -q` → **3513 passed**（+15：engine 控制流 9（in-mem journal 隔离）+ service 真 PG 6）；ruff clean。
>
> **交付物**：
> - `runtime/workflow_engine.py`：纯解释器（kernel 风格零 DB import，journal/leaf executor 注入）。sequence + condition 求值（AST 谓词全 op）+ agent_step；**resume 契约 = done 且 `input_hash`+`definition_hash` 双匹配才复用 output（leaf 不重调），否则重跑**；`{{...}}` 占位纯 key 替换、不可解析子路径 fail-loud；gate/wait/fanout **保守 suspend**（P5/P7 实现，绝不错误执行——文档"不做假承诺"的规范行为）。
> - `services/workflow_runtime_service.py`：`start_run`（compile→admit→建 `RuntimeTask(workflow)`+`WorkflowQuota`→执行）/ `resume_run`（从 metadata 存档恢复 definition+args，archive hash 完整性校验）/ `kill_run`（持久化状态，引擎每步前轮询 `should_continue` 读 run 行 → kill 中途生效）/ `load_run` / `resume_pending_runs`（startup 扫描 running/suspended，对标 `resume_persisted_async_delegations`）。journal = `workflow_steps` 表（FORCE RLS，全部走 `tenant_scoped_session`）。
> - **真 PG 验证的核心红测试全绿**：两步 kill 后 resume 只跑第二步（leaf 调用计数证明）；killed run 不被 startup 自动恢复（显式 resume 可复活 = CC kill→resume 模式）；崩溃残留 running run 被 startup 扫描收割跑完；journal 行 definition_hash 篡改为旧版本 → resume 不复用重跑；condition false 记 skipped。
> - lifespan 实际接线（startup 调 `resume_pending_runs`）留待 P4——真 leaf executor（spawn_subagent 绑定）在 P4 才存在，无 executor 的接线是死代码。

目标：先立住"数据 definition + 代码控制流 + DB journal + resume"，不做 gate/wait/fanout 假承诺。

改动面：
- 新增 `backend/app/runtime/workflow_engine.py`：解释 `sequence`、structured `condition`、`agent_step`（先用 fake/injected leaf executor 测）。
- 新增 `backend/app/services/workflow_runtime_service.py`：start、resume、cancel/kill、load_run。
- Step journal：step start/done/failed/skipped；`input_hash` 相同则 resume 跳过 done step。
- Startup resume hook：扫 `RuntimeTask(task_type="workflow", status in running/suspended)`，只恢复可自动恢复的 run。

Red tests：
- 两步 sequence 第一步 done 后 kill，resume 只跑第二步。
- condition false 时跳过对应 branch 并记录 skipped。
- definition_hash 改变时，旧 done step 不被错误复用。
- killed run 不会被 startup resume。
- kill-resume / journal 持久化必须跑 Testcontainers 真 PostgreSQL fixture；fake/injected leaf executor 只用于隔离测试控制流。

验收命令：

```bash
cd backend
pytest tests/runtime/test_workflow_engine_skeleton.py tests/services/test_workflow_runtime_service.py
```

### P4 Ephemeral launch + 风险确认分级

> **✅ 完成（2026-06-04）** — 证据：`pytest tests/ -q` → **3538 passed**（+25：plan-gate integration 10 + tools 9 + api 8，及 3 处治理 parity 同步）；ruff clean。
>
> **交付物**：
> - `services/workflow_launch.py`：`classify_workflow_risk`（§10 决策3 按风险分级——external/irreversible effects、预算 > `WORKFLOW_HIGH_RISK_BUDGET_TOKENS`、fanout > `WORKFLOW_HIGH_RISK_FANOUT_ITEMS`、等待 > `WORKFLOW_HIGH_RISK_WAIT_SECONDS` 任一即 high，阈值进 Settings）+ `build_subagent_leaf_executor`（**fake leaf 切真 `spawn_subagent` 入口**：spec/task/budget/ctx 全过轴1 真入口，governance/capability gate/tenant/SubagentBudget 继承自 spawn ctx，引擎不开第二条执行路；double 测试钉死契约含 leaf.max_tool_rounds→budget）+ `start_ephemeral_workflow_for_agent`（解析 agent runtime → ctx → executor → service.start_run，工具与 REST 共用单一入口）。
> - `api/workflows.py`：preview（编译+admission+风险分级，绝不执行）/ start（低风险=用户确认按钮即对话内确认直接跑、**高风险无 confirmed plan 409 fail-closed**、PlanModeGate action artifact 校验，绑定 `definition_hash + args_hash + risk_reasons`）/ get run（含 step journal）/ cancel。全端点 `check_agent_access`。
> - `tools/handlers/workflow.py`：`preview_workflow` + `start_workflow`（只提交 definition 数据）。`start_workflow` 走标准 `ToolMeta.plan_gate_action_kind` 早拦截：`plan_gate_registry._start_workflow_action_kind` 按参数实时编译分级——低风险放行（等同 agent 顺序调自己的工具）、高风险硬 gate、**编译失败/缺参数 fail-closed gate**（malformed payload 永远过不去）。
> - Plan Mode 接线：`ACTION_KINDS` + `"start_workflow"`（intent=long_task）；`ToolMeta.plan_gate_action_kind` 只是 integration，不进 Workflow 核心定义 ✓。
> - **治理 parity 全同步**（新工具的完整接线清单回归锚）：`capability_gate.CAPABILITY_MAP`（坑回归测试钉死）、`collector` 模块表、`runtime_tool_groups.coordination_pack`（orphan 审计过）、bridge canonical surface、ACTION_KINDS documented-set、`main.py` router 注册。
> - `ledger_todo_id` 透传：run 完成镜像到 ledger todo（observation only，失败 log 不阻断）。

目标：让 agent 可以生成一次性 definition，但运行前必须经过 preview / 风险分级 / 必要确认。

改动面：
- 新增 `backend/app/api/workflows.py`：preview ephemeral、start ephemeral、get run、cancel run。
- 新增 `backend/app/tools/handlers/workflow.py`：`start_workflow` / `preview_workflow`，只提交 definition 数据。
- 低风险：对话内 definition preview + 用户确认。
- 高风险：Plan Mode 确认面，提交 `confirmed_plan_id/version/hash` 后才 start。
- `ToolMeta.plan_gate_action_kind` 只作为 integration，不进入 Workflow 核心定义。
- 把 P3 的 fake/injected leaf executor 切到真 `spawn_subagent` / `spawn_subagent_from_definition` 入口；confirmed start 必须继承 tenant、tool governance、capability gate、SubagentBudget 和审计字段。

Red tests：
- 低风险只读 workflow 可 preview + confirmed start。
- 创建 trigger / 外向 action / 高预算 / promote 等高风险 workflow 无 confirmed plan 时 fail-closed。
- plan hash 不匹配时不能 start。
- confirmed start 的 `agent_step` 调用真 `spawn_subagent` 入口（测试可用 double 断言入口和参数），并继承 capability gate / tenant / SubagentBudget；未授权 leaf fail-closed。

验收命令：

```bash
cd backend
pytest tests/api/test_workflows.py tests/tools/test_workflow_tool.py tests/services/test_workflow_plan_gate_integration.py
```

### P5 Bounded fanout/map + leaf-level journal + run quota

> **✅ 完成（2026-06-04）** — 证据：`pytest tests/ -q` → **3548 passed**（+10：engine fanout 4 + quota 3 + 真 PG leaf journal 3）；ruff clean。
>
> **交付物**：
> - **engine fanout_step**（`workflow_engine._execute_fanout_step`）：bounded items（args 路径解析）+ `asyncio.Semaphore(max_concurrency)` 并发 cap（峰值断言测试钉死）+ 失败隔离（每叶都跑完，任一失败步 failed，下游不执行）+ 输出按 item 顺序聚合供下游 `{{steps.<id>.output}}` 消费。
> - **leaf-level journal（v1 决策 6）**：`WorkflowJournal` 协议加 leaf 方法；`LeafRecord` 含 leaf_id/status/input_hash/definition_hash/output；done 叶 input_hash+definition_hash 双匹配才 replay（executor 不调、quota 不扣）。**真 PG 核心红测试：8 叶完 7 kill→resume 只跑 1**（`WorkflowLeafCall` 行字段完整：idempotency_key=`{step}:{leaf}:{hash16}`、token_usage 落 JSONB）。
> - **run quota 硬顶池**：`QuotaReserver` 协议（reserve 预扣 estimate / settle 按实际回写）；engine 每个 leaf（agent_step 同样）spawn 前 reserve，不足→**suspended（确定状态）且后续叶绝不启动**；`PGQuotaReserver` = `pg_advisory_xact_lock(hashtext(run_id))` + 条件 UPDATE（`consumed+est<=allocated` RETURNING）保证跨任务树原子；estimate 进 Settings（`WORKFLOW_LEAF_TOKEN_ESTIMATE`）。真 PG 断言 settle 后 consumed=Σactual。
> - SubagentBudget 双层：leaf.max_tool_rounds → SubagentBudget（P4 executor 已接），与 run quota 信封独立生效。

目标：实现并发控制、leaf resume、预算硬顶池，堵住 fanout 绕配额。

改动面：
- `WorkflowLeafCall` 落每个 leaf 的 input_hash / idempotency_key / status / token_usage / result_ref。
- `workflow_quotas` spawn 前预扣，Postgres advisory lock 保证跨任务树原子。
- `fanout/map` 只允许 bounded input array；并发 cap / leaf cap / output cap 来自 config。
- `SubagentBudget` 与 run budget 双层生效。

Red tests：
- 8 个 leaf 跑完 7 个后 kill，resume 只跑剩余 1 个。
- budget 不足时新的 leaf 不启动，run 进入 failed/suspended 的确定状态。
- 并发 cap 生效；超过 leaf cap 的 definition admission 拒绝。
- token_usage 汇总回写 run quota。
- `WorkflowLeafCall` journal、quota 预扣、Postgres advisory lock 必须跑 Testcontainers 真 PostgreSQL fixture；mock 只能测 quota/admission 计算 pure logic。

验收命令：

```bash
cd backend
pytest tests/runtime/test_workflow_fanout.py tests/runtime/test_workflow_quota.py tests/runtime/test_workflow_leaf_journal.py
```

### P6 Registered definition lifecycle + promote/fork

> **✅ 完成（2026-06-04）** — 证据：`pytest tests/ -q` → **3569 passed**（+21：lifecycle 真 PG 8 + promote/fork 真 PG 6 + API 薄壳 7）；ruff clean。
>
> **交付物**：
> - `services/workflow_definitions.py`：create_draft（同名自动下一 version，**绝不原地改**——DB 唯一约束 + 测试双锚）/ activate（带 compile+capability 预检）/ deprecate / revoke 状态机；`resolve_for_execution` 拒 draft+revoked，**deprecated 仅 `allow_deprecated=True`（P8 pinned trigger 续跑路径）可解析**；hash pinning 校验参数就位（P8 用）。
> - **§10 决策 5 可见≠可执行**：`visibility_scope` 管谁看见（agent scope 只 owner 可见），`call_policy.allowed_agents` 管谁能跑（tenant-scope 可见但白名单外 agent 执行被拒——红测试钉死）；platform scope 拒绝租户创建（出厂只读）；cross-tenant 隔离由 FORCE RLS 完成（用非 superuser 角色验证）。
> - **§10 决策 4 promote**：`submit_promote_proposal` 永远落 draft（agent 不能自我 promote——红测试钉死）；`approve_promotion` 要求人类 approver（None → PermissionError）并**重跑 compile+capability check**（未授权 leaf 拒批）；`promoted_from_run_id` 留 provenance。
> - **fork**：registered version + 浅 patch → ephemeral definition **数据**（喂回 P4 ephemeral 路径：preview→分级→确认→run）；原 record hash 不变（红测试钉死）；revoked 不可 fork。
> - `api/workflow_definitions.py`：create/list/activate/deprecate/revoke/approve-promotion/fork 薄壳；PermissionError→403、not-found→404、lifecycle 冲突→409；tenant/approver 恒从认证用户注入。

目标：完成固定 workflow 的持久管理，同时保持 ephemeral / registered 同引擎。

改动面：
- `workflow_definitions` API/service：create draft、activate、deprecate、revoke、list、get version。
- 权限模型：`visibility_scope` + `call_policy` + `owner_type`；`platform` 只允许出厂策展只读模板。
- promote：agent 只能提交 proposal；用户/owner/admin 审批后编译、admission、capability check，再生成 active version/hash。
- fork：registered version + args/patch → ephemeral run；不修改原 version。

Red tests：
- agent 不能自行 promote。
- agent-scope / org-scope / tenant-scope / platform-scope 可见性与执行权限分别生效。
- revoked definition 不能 start；deprecated 可按策略允许已有 trigger 继续或要求迁移。
- fork 生成 ephemeral definition，原 registered hash 不变。

验收命令：

```bash
cd backend
pytest tests/services/test_workflow_definitions.py tests/api/test_workflow_definitions.py tests/services/test_workflow_promote_fork.py
```

### P7 gate_step + wait_until/time suspend

> **✅ 完成（2026-06-04）** — 证据：`pytest tests/ -q` → **3583 passed**（+14：gate engine 5 + wait/retry engine 5 + checkpoint 真 PG 4）；ruff clean。
>
> **交付物**：
> - **gate_step 一等行为**（engine `GateDecider` 协议三态）：pending→suspended（**受保护 leaf 绝不执行**）/ approved→journal done 继续 / rejected→failed；done gate resume 时 replay 不再仲裁；**无 decider 绑定 fail-closed suspend**。
> - **CheckpointGateDecider**（service）接 `CoordinationCheckpoint`：每 (run, step) 一个 checkpoint（metadata 关联），`approve/reject` 翻转后**显式 resume** 执行被保护步——真 PG 全链路：外向步 suspend→approve→resume 只跑该步；rejected 永不执行。
> - **wait_until/delay 时间挂起**：目标时间**首挂起即固定进 journal（input_hash 锚）**——修掉真 bug：delay_seconds 若每次 resume 重算（now+delay）目标永远后移、run 永不落地；due 直接过，undue → `WaitScheduler.schedule_resume` 写 run metadata `resume_at`（等价调度记录，once trigger 真绑定归 P8 §6.2）。
> - **startup 扫描分流**：suspended run 只有**时间挂起且到期**（resume_at ≤ now）才自动恢复；gate/budget 挂起等各自恢复路径（approval→显式 resume；quota→admission），扫描绝不碰——真 PG 红测试钉死。
> - **retry 只可逆**（v1 决策 1）：agent_step 可逆步按 `max_attempts` 重试（每次尝试独立 reserve/settle quota）；irreversible 步恰好一次尝试绝不自动重试（schema 禁 retry on irreversible + 引擎行为双层钉死）。
> - 过程修复：in-mem journal `record_step_suspended` 从覆盖改字段级更新（与 PG journal 对齐，input_hash 存活）。

目标：把外向/不可逆步骤和时间挂起做成一等 step 行为。

改动面：
- `gate_step` 接 `ActionPreflightService` / `CoordinationCheckpoint`。
- 不可逆/外向 step 必须 checkpoint approved 后执行；不自动 retry。
- `wait_until` / `delay_until` 进入 `suspended`，创建 once trigger 或等价调度记录，到时 resume。
- once trigger 只做时间恢复，不承载 approval/budget/signal。

Red tests：
- 外向 step 无 approval 时 run suspended，不执行 leaf。
- approval 后 resume 执行对应 step。
- `wait_until` 到时后 resume；未到时不 resume。
- 不可逆 step 失败不会自动 retry；可逆 step 才能按 `max_attempts` retry。

验收命令：

```bash
cd backend
pytest tests/runtime/test_workflow_gate_step.py tests/runtime/test_workflow_wait_until.py tests/services/test_workflow_checkpoint_integration.py
```

### P8 Trigger integration：registered + version/hash pinning

> **✅ 完成（2026-06-04）** — 证据：`pytest tests/ -q` → **3597 passed**（+14：fire 分支真 PG 10（六类 trigger 参数化）+ REST 校验 4）；ruff clean。
>
> **交付物**：
> - `services/workflow_trigger.py`：`fire_workflow_for_trigger`（fire 时 payload 分支：无 `workflow_ref` 返回 None → 现有 ReAct 路径原样；有 ref → resolve pinned definition → launch `definition_source="registered"`）。六类 trigger（cron/once/interval/poll/webhook/on_message）同一 fire 路径参数化验证。
> - **版本/hash pinning 防授权漂移**：fire 时重验 creation-time pin；**mismatch 绝不静默跑新版**——留 suspended `needs_reconfirmation` run 记录（审计锚）并停止；pinned 续跑允许 deprecated（P6 `allow_deprecated`），revoked 仍拒。
> - **webhook payload → args**：注入 `args["webhook_payload"]`（JSON 解析，失败原文），过同一 args-schema admission——schema 未声明该 key 时被拒（`rejected_args` + suspended 记录），不猜不放行。
> - `trigger_daemon._invoke_agent_for_triggers` 开头分流：workflow_ref triggers 走引擎分支，其余落 ReAct；全部被 workflow 处理时 runtime task 标记 `workflow_ref_handled`。
> - **创建时 pin 校验**：REST `create_trigger` 带 workflow_ref 时调 `validate_workflow_ref_for_trigger`（shape + 定义存在 + 本 agent 可执行 + hash 匹配，失败 400）；创建已有 `create_enabled_trigger` plan gate 覆盖（P4 之前已接，带 ref 不改 gate 行为）。

目标：让 trigger 成为 workflow 调用方，并堵住授权漂移。

改动面：
- `trigger.config.workflow_ref={definition_name, definition_version, definition_hash, args}`。
- `trigger_daemon` fire 后分支：有 workflow_ref → workflow engine start/resume；无 ref → 现有 ReAct。
- 创建/启用带 workflow_ref 的 trigger 走 `create_enabled_trigger` gate。
- fire 时校验 `definition_version/hash`；mismatch → suspend / needs re-confirmation，不能静默跑新版。
- webhook payload 注入 workflow args，受 args schema 校验。

Red tests：
- cron/once/interval/webhook/poll/on_message 六类 trigger 均能传 args start workflow。
- version/hash mismatch 时不运行新版 definition。
- webhook payload 超 args schema 被拒或进入 suspended error。
- 无 workflow_ref 的 trigger 保持现有行为。

验收命令：

```bash
cd backend
pytest tests/services/test_trigger_daemon_workflow.py tests/api/test_triggers_workflow_ref.py
```

### P9 Work Ledger mirror + audit + notifications

> **✅ 完成（2026-06-04）** — 证据：`pytest tests/ -q` → **3604 passed**（+7：ledger mirror 真 PG 2 + audit 3 + completion signal 真 PG 2）；ruff clean。
>
> **交付物**：
> - **ledger mirror（§10 不变量④单向）**：run 创建（带 agent_id）即 `initialize_agent_work_ledger_artifact(runtime_task_id=run_id)`；`_PGWorkflowJournal` 每次 step 状态写后镜像 ledger todo（running→in_progress、done/skipped→completed、failed/suspended→pending），fail-soft 绝不阻断。**认知≠治理红测试**：mid-run 把未执行 step 的 ledger todo 篡改成 completed → resume 引擎照跑该步（引擎只读自己的 journal）。
> - **audit trail**：`workflow_run_started/completed/failed/suspended/killed` 全带 `tenant_id+run_id+definition_hash`（+reason）；`workflow_definition_promoted`（含 approver_user_id 决策 metadata + promoted_from_run_id provenance）；`workflow_definition_forked`（forked_by_agent_id）。全部 fail-soft。
> - **workflow_completed Signal（只通知）**：run completed 时向 parent agent 发 `signal_type="workflow_completed"`（thread_id=run_id，复用轴1通道）；**read-once 红测试**（第二次 consume 空）；failed run 不发；**不承诺 wait_signal 恢复**（P11 的 persistent consumer 才做）。

目标：让用户/agent 能观察 workflow 进度，但不让 ledger 反向驱动控制流。

改动面：
- run 创建时 `initialize_agent_work_ledger_artifact(runtime_task_id=run_id)`。
- step/leaf 状态单向镜像到 ledger todo/progress。
- audit log 记录 start/resume/suspend/complete/fail/cancel/promote/fork。
- `workflow_completed` Signal 只做通知；不承诺 `wait_signal` 恢复。

Red tests：
- ledger todo status 随 step 状态变更。
- 修改 ledger 不影响 engine 下一步选择。
- audit log 包含 tenant_id、definition_hash、run_id、decision/approval metadata。
- completion signal 可读，但不会重复消费。

验收命令：

```bash
cd backend
pytest tests/services/test_workflow_ledger_mirror.py tests/services/test_workflow_audit.py tests/runtime/test_workflow_completion_signal.py
```

### P10 Cross-worker persistence + drain

> **✅ 完成（2026-06-04）** — 证据：`pytest tests/ -q` → **3611 passed**（+7：worker lease 真 PG 4 + restart resume 1 + graceful drain 2）；ruff clean。
>
> **交付物**：
> - **Run lease（worker ownership）**：`PGRunLeaseManager` = session 级 `pg_try_advisory_lock`（crc32 namespaced key）持于**专用连接**——锁随连接死亡自动释放（worker 崩溃 = 自动让渡，红测试：硬关连接后 peer 立刻可得）；两 worker 同 run 只一个获得；`resume_run` 拿不到 lease 返回 "lease held by another worker" 不双开引擎。
> - **跨 worker 接管**：worker A 崩溃留 running run → worker B（全新 service 实例）`resume_pending_runs` 收割跑完，done step replay 不重执行（共享 PG journal）。
> - **graceful drain**：`request_drain()` → engine 下一步边界停（in-flight leaf 跑完，新 leaf 绝不启动）→ run 标回 `running`（崩溃等价语义，startup 扫描可接管）而非 killed；journal 完整，重启后续跑只执行剩余步。
> - **外向 in-flight 对账规则**：resume 入口检查 journal 里 status=running 且 definition effects ∈ {external, irreversible} 的步 → 标 **`unknown_requires_reconciliation`**（status 列加宽 40 容纳）+ run suspended + metadata `needs_reconciliation` 步清单 + audit `workflow_run_needs_reconciliation`；**绝不自动重放**（红测试：leaf 零调用），人工对账后方可继续。
> - quota/admission 跨 worker 一致性已由 P5 的 advisory-lock 条件 UPDATE 保证（同 DB 锁原语，无需新件）。

目标：从"单 worker 正确"推进到"多 worker / 重启 / 部署期间不丢 run"。

改动面：
- Workflow run lease / worker ownership，避免多 worker 同时 resume 同一个 run。
- DB/Redis Streams 驱动的 pending run queue；startup 扫描只接管可恢复 run。
- lifespan graceful drain：停止接新 leaf，等待可配置时间，未完成 run 标记可 resume。
- quota/admission 跨 worker 用 DB/advisory lock 保证一致。
- drain policy 区分可逆 step 与已过 gate 的外向 in-flight step；外向状态不明时写入 `unknown_requires_reconciliation`，不得自动重放。

Red tests：
- 两个 worker 同时扫描，只一个获得 run lease。
- worker kill 后，新 worker resume 未完成 run。
- graceful drain 不丢 step journal，重启后可继续。
- 外向 step 已过 gate 且执行中时收到 drain：不能 kill 后自动 resume 重放；必须等待完成或标记 `unknown_requires_reconciliation` 进入人工对账。
- worker lease、drain/recovery、cross-worker resume 必须跑 Testcontainers 真 PostgreSQL fixture。

验收命令：

```bash
cd backend
pytest tests/runtime/test_workflow_worker_lease.py tests/runtime/test_workflow_restart_resume.py tests/runtime/test_workflow_graceful_drain.py
```

### P11 wait_signal v2：persistent signal-resume consumer

> **✅ 完成（2026-06-04）** — 证据：`pytest tests/ -q` → **3617 passed**（+6 全真 PG：wait_signal 4 + 持久性/租户隔离 2）；ruff clean。
>
> **交付物**：
> - **`wait_signal_step` v2 解锁**（schema + engine）：v1 诚实拒绝的能力随 persistent consumer 落地一起开放；步挂起时 journal `input_hash=signal_type` 锚 + `SignalWaitRegistrar` 写 run metadata `waiting_for_signal={step_id, signal_type}`。
> - **`services/workflow_signal_consumer.drain_signal_resumes`**：扫描带注册的 suspended run → 匹配 PG `coordination_signals`（**tenant + to_agent + thread(=run_id) + signal_type 四元匹配**）→ `DELETE ... RETURNING` **行级原子 consume-once**（恰一个 drainer 赢）→ waiting step 标 done（signal payload 作 output）→ lease 内 resume → engine replay 继续。
> - **红测试四条全过**：PG Signal 到达恢复 suspended run（后续步执行、prep replay）；**未消费 Signal 跨进程重启存活**（全新 consumer 实例收割——PG 行即持久性）；同一 Signal 只恢复一次（行已删）；thread/type/**跨租户**不匹配绝不恢复（B 租户冒名 signal 原样留存、A run 继续等待）。
> - **前置补齐**：`CoordinationSignal/Lease/Checkpoint` models 缺 env.py/main.py import（bootstrap 库根本没建 coordination 三表——缺口 C 同款，已补；这正是"前置条件：Signal 必须走 PG 持久层"在新部署上的真实含义）。
> - in-process Signal 仅测试/本地（consumer 只读 PG 表）。

目标：补齐当前 v1 明确不承诺的外部 Signal 恢复能力。

前置条件：
- Coordination Signal 必须走 PostgreSQL-backed repository 或等价持久层。
- 有明确 consumer 把 Signal 转换成 suspended WorkflowRun 的 resume event。

改动面：
- `workflow_waits` 或 `WorkflowStep(waiting_for_signal=...)` 索引。
- Signal resume consumer：按 tenant/thread/signal_type 匹配 waiting run，获取 run lease 后 resume。
- Signal consumed-once + idempotency_key，防重复恢复。
- in-process Signal 只能用于测试/本地，不作为持久 Workflow wait 的后端。

Red tests：
- Postgres Signal 到达后 suspended run 被恢复。
- 进程重启后未消费 Signal 仍能恢复 run。
- 同一 Signal 只恢复一次。
- tenant/thread 不匹配不能恢复。

验收命令：

```bash
cd backend
pytest tests/runtime/test_workflow_wait_signal.py tests/agents/test_coordination_signal_resume.py
```

### P12 Frontend / product surface

> **✅ 完成（2026-06-04）** — 证据：`npm run build` ✓（tsc+vite）；`npm test -- --run` → **145 passed**（+10 workflows domain）；`npm run test:e2e -- --project=chromium` → **2 passed**（Playwright 设施从零建立）。
>
> **交付物**：
> - **Playwright E2E 设施（从零）**：`@playwright/test` 依赖 + `playwright.config.ts`（webServer=vite dev fixture, chromium project, trace retain-on-failure）+ `npm run test:e2e` script + `e2e/workflow.spec.ts`。范围=浏览器级真实 UI + `/api` 面 per-test route-mock（确定性、无后端依赖；坑：glob `**/api/**` 会拦 Vite `/src/api/...` 模块请求，handler 内 `path.startsWith('/api/')` 过滤）。**全栈 E2E（真后端+PG）归 P15 部署验收**。
> - **两条 E2E 全绿**：低风险 ephemeral preview→confirm→run completed（步进度 done 断言）；高风险 preview→start 禁用 + Plan-Mode-required 提示（fail-closed UX）。
> - `api/domains/workflows.ts`：ephemeral preview/start（confirmed plan 绑定透传）/getRun/cancel + registered 全生命周期（create draft/list/activate/deprecate/revoke/approve-promotion/fork）+ `classifyTriggerPin`（§6.2 pin 状态推导：pinned/version_mismatch/hash_mismatch/missing）。Vitest 10 条覆盖文档红测试四项（preview/run status/registered list/trigger mismatch）。
> - **`AgentWorkflowsSection`（一个心智模型 §4）**：一次性编排（definition 粘贴→preview 风险卡→确认运行，高风险禁用并列 reasons）→ 运行进度（step journal 轮询+cancel）→ 模板表（version/status/visibility/hash + promote 审批/fork/deprecate/revoke）。AgentDetail 注册 `workflows` tab。
> - i18n：`en.json`/`zh.json` 双语全量（tab + workflows.* 19 keys）。
> - **覆盖面诚实记录**：Office workbench 工作流入口归 P13（office 场景接线时才有内容）；Trigger 创建 UI 的 workflow_ref 选择器未做（pin 状态推导函数已就位，绑定 UI 留待 trigger 表单改版时接入——P8 后端校验已兜底）。

目标：给办公用户一个工作流心智模型，而不是暴露 ephemeral / registered 两套入口。

改动面：
- 先建设 Playwright E2E 设施：dependency、`npm run test:e2e` script、config、dev-server fixture；没有设施前不承诺 Playwright 验收。
- Frontend API domain：`frontend/src/api/domains/workflows.ts`。
- Agent detail / Office workbench：definition preview、run progress、approval prompt、cancel/resume。
- Workflow templates/admin：registered list、version、status、visibility、call policy、promote/fork。
- Trigger UI：选择 workflow version/hash，显示 mismatch / needs reconfirmation。
- i18n：`en.json` / `zh.json` 同步。

Red tests：
- Vitest 覆盖 preview、run status、registered list、trigger mismatch 状态。
- Playwright 覆盖低风险 ephemeral preview → confirm → run complete；高风险 workflow → Plan Mode confirm；该覆盖以本阶段新增的 `npm run test:e2e` / Playwright config 为前置。

验收命令：

```bash
cd frontend
npm run build
npm test -- --run
npm run test:e2e -- --project=chromium
```

### P13 Office workflows

> **✅ 完成（2026-06-04）** — 证据：`pytest tests/ -q` → **3625 passed**（+8：office 真 PG 5 + promote suggestion 真 PG 3）；ruff app+tests 全 clean。
>
> **交付物**：
> - `services/office_workflow_examples.py` 三个内置 example（**纯结构化数据**，与 agent 提交走完全相同的 compiler/admission——built-in 无旁路）：`office-contract-review`（解析→条款→风险表，workspace artifacts，低风险）/ `office-weekly-report`（bounded fanout per source→compose）/ `office-document-distribution`（draft→**审批 gate**→external send，外发高风险 by design）。
> - **leaf capability binding 实证**：`OFFICE_LEAF_CATALOG` 之外的 catalog 编译/准入双层拒绝（office 工具走 worker preset+capability gate 面，无私有执行路）。
> - **真 PG 全链红测试**：合同审阅跑完三步全 done、risk-table artifact 引用入 journal、跑完即可 `submit_promote_proposal`（draft + run provenance）；**外发 step 无 gate approval 绝不执行**（suspend→approve→resume 后恰好发送一次）。
> - `services/workflow_promote_suggestions.py`：同 definition_hash 完成 ≥`WORKFLOW_PROMOTE_SUGGESTION_THRESHOLD`（=3，进 Settings）次 ephemeral → `PromoteSuggestion`（evidence 含 run_count+sample_run_ids）；2 次沉默；已注册同名不再建议。**只观察只建议**（§10 决策 4：promote 仍走 proposal→人工审批路径），§6.6 自我进化感知通道的接入种子。

目标：把办公场景作为第一批真实调用方，验证一次性编排 → 模板 → 自动化的产品路径。

改动面：
- 内置 office workflow examples：文档 OCR/解析/风险表、周报汇总、合同审阅。
- Office document tools 作为 leaf capability，被 workflow compiler/admission 校验。
- 低风险只读/工作区写入走轻量 preview；外发/共享/删除走 Plan Mode + gate_step。

Red tests：
- 合同处理 ephemeral workflow 生成 artifacts，并可 promote proposal。
- 外发文档 step 必须 gate approval。
- 复跑同类 office workflow 产生 promote suggestion evidence。

验收命令：

```bash
cd backend
pytest tests/runtime/test_office_workflows.py tests/services/test_workflow_promote_suggestions.py
```

### P14 Deep Research 接入（最后一步）

> **✅ 完成（2026-06-04）** — 证据：`.venv/bin/python -m pytest tests/deep_research/test_workflow_definition.py tests/tools/test_deep_research_handler.py::test_deep_research_start_uses_workflow_when_rollout_flag_enabled ...` → 本轮 P14/P15 targeted **10 passed**（含 Testcontainers 真 PG）；最终后端全量 `.venv/bin/python -m pytest tests/ -q` → **3659 passed, 7 skipped**；`ruff check app tests` clean。
>
> - `services/deep_research/workflow_definition.py`：新增 `deep_research.v1` registered definition builder，结构固定为 `plan -> explore(fanout) -> synthesize -> critic`，leaf catalog = `deep_research_planner / explorer / synthesizer / critic`，通过现有 `compile_workflow` + `admit_workflow`。
> - `deep_research_start` 与 Plan Mode handoff `start_deep_research_background_run` 均接入 `WORKFLOW_DEEP_RESEARCH_ENABLED` 分流；flag 默认 **false**，旧 `RuntimeTask(task_type="deep_research") + background orchestrator` 路径保持可回滚。
> - flag 开启时自动 ensure tenant-scoped active `deep_research.v1` definition，并以 `definition_source=registered:<name>:v<version>:<hash>` 启动同一 workflow runtime；返回 `workflow_run_id`、`definition_version`、`definition_hash` 和 `legacy_path_available=true`。
> - 本轮先迁移入口与结构壳；旧 DR 内部 `_run_worker_fanout` / controller 细粒度策略仍作为 leaf 内部能力逐步下沉，不再作为 P14 阻塞项。

目标：把 DR 迁到 registered workflow，但只在底座完成后做。

改动面：
- 注册 `deep_research.v1` definition：plan → bounded fanout(explorer) → synthesize → critic。
- confirmed Deep Research start / Plan Mode handoff 通过 `WORKFLOW_DEEP_RESEARCH_ENABLED` 灰度切到 workflow runtime。
- 保留旧 DR path feature flag off 回滚。

Red tests：
- `deep_research.v1` definition compile + admission。
- feature flag on 时 `deep_research_start` 不再创建旧 `deep_research` RuntimeTask，而返回 `workflow_run_id`。
- feature flag off 时旧路径仍工作（既有 `test_deep_research_start_creates_runtime_task` 保留）。
- explorer leaf journal resume、coverage/critic failed/suspended 由通用 fanout/leaf journal/runtime 测试覆盖。

验收命令：

```bash
cd backend
pytest tests/deep_research/test_workflow_definition.py \
  tests/tools/test_deep_research_handler.py::test_deep_research_start_creates_runtime_task \
  tests/tools/test_deep_research_handler.py::test_deep_research_start_uses_workflow_when_rollout_flag_enabled \
  tests/services/test_workflow_leaf_journal.py
```

### P15 Ops / rollout / hardening

> **✅ 完成（2026-06-04）** — 证据：`.venv/bin/python -m pytest tests/api/test_admin_workflow_ops.py tests/services/test_workflow_runtime_service.py::test_runtime_feature_flag_fails_closed_before_creating_run tests/services/test_workflow_runtime_service.py::test_workflow_metrics_record_run_step_leaf_resume_and_quota_denial tests/services/test_trigger_daemon_workflow.py::test_workflow_trigger_feature_flag_fails_closed_before_launch tests/services/test_workflow_ops.py` → 本轮 P14/P15 targeted **10 passed**（含 Testcontainers 真 PG）；最终后端全量 `.venv/bin/python -m pytest tests/ -q` → **3659 passed, 7 skipped**；`ruff check app tests` clean。
>
> - Feature flags 已落到 `Settings`：`WORKFLOW_RUNTIME_ENABLED`（默认 true）、`WORKFLOW_TRIGGER_ENABLED`（默认 true）、`WORKFLOW_DEEP_RESEARCH_ENABLED`（默认 false）。
> - Runtime fail-closed：`WORKFLOW_RUNTIME_ENABLED=false` 在 `WorkflowRuntimeService.start_run` 创建任何 run 前拒绝；`WORKFLOW_TRIGGER_ENABLED=false` 在 `workflow_ref` launch 前拒绝。
> - Metrics：`services/workflow_metrics.py` + `/admin/metrics/workflows`，覆盖 run started/finished、step totals、step duration、leaf call totals、resume attempts/finished、quota denials、hash mismatches。
> - Admin repair：`services/workflow_ops.py` + `/admin/workflows/{run_id}`、`/journal`、`/cancel`、`/force-suspend`、`/replay-from-step`；全部 `require_role("platform_admin")`。
> - Runbook：`docs/workflow-ops-runbook.md`；Railway deploy 仍需上线时按 runbook 做 production health/logs/migration/非 superuser DSN 验收。
>
> **🔧 Review 修复轮（2026-06-04，P14/P15 之上）** — 证据：第一轮后端全量 **3669 passed, 7 skipped**（+10 新测试）、`ruff` clean、前端 Vitest **152**、build ✓、Playwright **2 passed**；第二轮 targeted：`tests/deep_research/test_workflow_definition.py` **5 passed**，`tests/services/test_workflow_ops.py tests/api/test_admin_workflow_ops.py` **8 passed**。
>
> - **P1 高风险死锁**：gate 的 confirmed-plan artifact 绑定此前无生产写入方（plan seeding/确认链路均不写）→ 合法确认后仍 `action_artifact_missing` 拒绝。闭环 = gate-check 时算好的 artifact 随 `interactive_plan_seed` → `PlanModeState`（typed + 镜像）→ `exit_plan_mode` fill → `plan_json["action_artifact"]`（被 plan hash 锁定）。链路五点测试覆盖（seed/state/kernel/exit/gate 位置4）。
> - **force-suspend mid-run 生效**：`should_continue` 现认 `("killed","suspended")`，下一步边界停；收尾不再把 operator 的 suspended 覆盖成 killed（resume 前已标 running，无回归）。
> - **Admin ops 审计**：cancel/force-suspend/replay 补 fail-soft `write_audit_log`（与 P9 口径一致；replay 含 rewound_step_ids）。
> - **Replay quota 回退**：删除 fanout leaf 行前按其 `token_usage` 全额退还（floored 0）；普通 agent_step 无行级计量不退，runbook 声明。
> - **Replay quiescence 硬门**：`replay_from_step` 拒绝 `RuntimeTask.status=running` 或任意 `workflow_steps` / `workflow_leaf_calls` 仍为 `running` 的 run，API 映射 `409 Conflict`；必须先 force-suspend 并等 step boundary 停稳，防止 destructive journal surgery 与 in-flight worker 竞写。
> - **Replay × P10 对账锚硬门**：rewound 范围含 `unknown_requires_reconciliation` 步 → `409 Conflict` 拒绝。实证链（真 PG repro，CheckpointGateDecider 生产配置）：replay 删锚行而 gate 的 CoordinationCheckpoint approval 持久不清 → resume 静默重放未对账外向操作（repro 实测 `sent=['Send externally']`，无任何新审批）。对账出路（人工 SQL，刻意不给 API 捷径）写入 runbook：效果已发生→标 done；verifiably 未发生→删行重跑。回归测试覆盖"replay 该步本身"与"上游范围扫到"两种形态。**后续可选（未排期）**：`resolve-reconciliation` 正式 admin 命令（`{step_id, resolution: executed|not_executed, reason}`，同 audit/409 纪律）替代手工 SQL — v1 刻意保留对账摩擦，待真实运维需求出现再做（runbook 同步记载）。
> - **DR ensure 并发幂等 + tenant 显式隔离**：per-tenant session 级 advisory lock 串行化 check-then-create（并发双版本竞态消除）；active record 查询显式带 `tenant_id` predicate，不能依赖 RLS 隐式过滤（owner/superuser DSN 会绕过）。真 PG 幂等/并发/跨 tenant 测试覆盖（`tests/deep_research/conftest.py` 新建 re-export）。
> - **coordination_rls_0604 加 `_table_exists` guard**（与 add_workflow_tables_0604 幂等模式一致）；前端 stale workflow selector 与 args JSON 错误分流（`StaleWorkflowRefError` + i18n 双语）；runbook 写死 ① `WORKFLOW_RUNTIME_ENABLED=false`=stop-new-starts 非全局 kill switch ② **DR flag 在旧 DR leaf 能力（source ledger/RC12/RC13/workspace 报告落盘）下沉前禁止生产开启** ③ replay quota 语义。

目标：让生产上线可观察、可回滚、可限流。

改动面：
- Feature flags：`WORKFLOW_RUNTIME_ENABLED`、`WORKFLOW_TRIGGER_ENABLED`、`WORKFLOW_DEEP_RESEARCH_ENABLED`。
- Metrics：run counts、step duration、leaf failures、quota denials、resume counts、hash mismatch count。
- Admin repair commands：inspect run, cancel run, force suspend, replay from step, export journal。
- Docs：developer implementation guide、operator runbook、product help。
- Railway deploy 验收：backend health、migration head、logs 无 workflow migration/runtime error。

验收命令：

```bash
cd backend
ruff check app tests
pytest tests/api/test_admin_workflow_ops.py \
  tests/services/test_workflow_runtime_service.py::test_runtime_feature_flag_fails_closed_before_creating_run \
  tests/services/test_workflow_runtime_service.py::test_workflow_metrics_record_run_step_leaf_resume_and_quota_denial \
  tests/services/test_trigger_daemon_workflow.py::test_workflow_trigger_feature_flag_fails_closed_before_launch \
  tests/services/test_workflow_ops.py
alembic heads
```

完成定义：P0-P15 全部完成后，Workflow 具备 ephemeral / registered 双来源、确定性控制流、step/leaf journal、kill-resume、预算硬顶、gate/wait_until、trigger 调用、权限治理、ledger/audit 可观测、cross-worker 持久、office 场景、Deep Research 迁移与生产运维闭环。

---

## 10. 非目标 / 不变量 / v1 决策

**非目标**：形态 A（已被三件套覆盖）；改 Coordinator Mode / 对话 agent；本阶段不动 deep research；租户图灵完备脚本（载体已定数据）。

**不变量**：① definition = 结构化数据，**无任意代码执行面**；② 叶子必须是轴 1 原语（治理/隔离/budget 继承）；③ ephemeral 与 registered **共享同一引擎/journal/budget/resume/gate/tenant/audit，绝不分叉两条 runtime**；④ journal ≠ ledger，单向镜像；⑤ 对外/不可逆步必过 gate_step，不自动重试；⑥ journal/quota 第一天带 tenant_id；⑦ 增量演进，每切口独立可回滚。

**v1 决策（2026-06-03）**：
1. **definition 表达力边界**：v1 支持 `sequence`、bounded `fanout/map`、structured `condition`、`gate`、`wait_until`/time suspend。允许 `retry(max_attempts=...)`，但只允许可逆步骤重试；P11 后 v2 开放 `wait_signal_step`，且只能绑定 PostgreSQL-backed Signal + persistent signal-resume consumer；不开放任意 loop、任意 Python/JS 表达式、动态生成新 step。
2. **condition 谓词形态**：谓词是结构化比较 AST，不是字符串表达式。原子形态为 `{field, op, value}`，`field` 只能指向 `args` 或 structured step output，`op ∈ {eq, ne, gt, lt, gte, lte, contains, exists, in}`；布尔组合仅 `and/or/not`，且嵌套深度有上限。禁止 `eval`、Jinja、Python/JS 表达式、模板求值或任意解释器。
3. **ephemeral 的"确认后运行"分级**：按风险分级，不按 ephemeral/registered 分级。低风险 ephemeral 可走对话内 definition preview + 用户确认；外部可见、不可逆/敏感、创建/启用 trigger、高预算/高 fanout/长时运行、跨 agent/org/company 资源、或 promote 成 registered 时，必须走 Plan Mode 确认面。预算、fanout、时长阈值进入配置，不硬编码。
4. **promote 权限**：agent 只能建议 promote，不能自行 promote。流程是 repeated ephemeral evidence → promote proposal → 用户/owner/admin 审批 → compile/admission/capability check → registered workflow version/hash → audit log。
5. **registered 权限模型**：拆成可见性与可执行性。`visibility_scope = agent | org | tenant | platform`；`call_policy = allowed_agents / allowed_roles / allowed_orgs`；`owner_type = user | agent | org | tenant | platform`；`status = draft | active | deprecated | revoked`；`definition_version` + `definition_hash` immutable。registered workflow 可见不等于可执行，leaf capability 仍必须逐 run 校验。`platform` scope 只能是出厂策展只读模板，不能由租户产物聚合生成。
6. **journal 粒度**：fanout 必须 leaf-level journal。结构为 `RuntimeTask(task_type="workflow") -> WorkflowStep -> WorkflowLeafCall`；`WorkflowLeafCall` 至少记录 `run_id`、`step_id`、`leaf_id`、`input_hash`、`definition_hash`、`status`、`result_ref`、`token_usage`、`error`、`started_at`、`finished_at`、`idempotency_key`，避免 8 叶跑完 7 叶后整步重跑。

---

> **状态**：v0.6 草稿（2026-06-04 重定位：与 Plan Mode 并列底座 + ephemeral/registered 双来源同引擎 + 集成面全部降级 + §8 收敛为索引摘要 + §9 全量实现路线作为唯一权威路线）。§0.2 留痕 v0 被推翻处。下一步是按 §9 路线从 P0 前置验证与 P1/P2 walking skeleton 设计开始执行；涉及持久化、RLS、advisory lock、worker lease、cross-worker resume 的验收默认使用 Testcontainers 真 PostgreSQL，不能用 mock session 代替。

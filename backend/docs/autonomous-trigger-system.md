# Hive 自主触发与自我进化系统

## 背景

Hive 当前已经具备 heartbeat、trigger、focus.md、evolution files、auto-dream、memory distillation 等组件，但这些组件还没有形成强闭环。核心问题不是“触发器没有运行”，而是目标、唤醒、执行、结果之间缺少同一个可审计账本。

当前代码事实：

- `focus.md` 只解析标准格式 `- [ ] task_id :: description`，非标准目标不会进入 objective ledger。
  参考：`backend/app/services/focus_state.py`
- 普通 scheduled/job trigger 每次触发仍创建新的 `source_channel="trigger"` Reflection Session；`objective_task` trigger 已改为复用稳定 objective session。
  参考：`backend/app/services/trigger_daemon.py`
- trigger fire 会更新 `fire_count` 和 `last_fired_at`，并写入 `RuntimeTask(task_type="trigger")` 作为执行账本。
  参考：`backend/app/services/trigger_daemon.py`
- heartbeat 已有持久 session 语义，并写入 `RuntimeTask(task_type="heartbeat")`。
  参考：`backend/app/services/heartbeat.py`
- completed focus 自动关闭 trigger 已从 heartbeat 末尾逻辑升级为独立 reconciler，并由 trigger daemon tick 调用。
  参考：`backend/app/services/trigger_reconciler.py`

## 对照结论

Claude Code 和 Hermes 都说明了一点：可靠的自主 agent 不能只靠 prompt 约定。

Claude Code 的关键模式：

- local cron 有 durable/session-only 区分、文件账本、锁、防重复触发、missed handling。
- remote scheduled agent 是隔离 session，prompt 必须自包含。
- task/todo 有状态机和 hook，完成不是靠自然语言自觉声明。
- assistant background task 的结果会回流主 agent，而不是散落在不可追踪 session 里。

Hermes 的关键模式：

- cron job 有 JSON 账本。
- 每次 run 有独立输出 artifact。
- 支持 `wakeAgent=false` 的预执行门控。
- 支持 `context_from`、script output、toolset、workdir、model pinning。
- cron run 默认隔离 session，不污染长期记忆。

Hive 应采用的原则：

```text
Objective 是目标事实源
Trigger 是唤醒策略
RuntimeTask / Attempt 是执行账本
Session 是上下文容器
focus.md 是可读投影，不是事实源
```

## 目标架构

未来系统分三层：

```text
Objective Ledger
- 记录 agent 的真实目标、状态、成功标准、优先级、阻塞原因、完成时间

Wake Policy
- 记录什么时候唤醒 agent
- trigger 不再代表目标本身，只代表唤醒条件

Attempt Ledger
- 记录每次 heartbeat / trigger / objective run 是否真的发生
- skipped 也要记录原因
```

目标型 trigger 必须绑定目标。没有目标绑定的 trigger 只能属于以下类型：

```text
scheduled_job
event_wait
system_maintenance
```

推荐的 trigger 分类：

```text
objective_task
- 必须有 objective_id / focus_ref / success_criteria
- 用稳定 objective session

scheduled_job
- 自包含 prompt
- 每次 run 可以独立 session
- 可生成 output artifact

event_wait
- on_message / webhook / poll
- 必须有 ttl 或 max_fires
- 必须定义命中后的动作

system_maintenance
- heartbeat / dream / distill / reconciler
- 不代表用户业务目标
```

## Session 策略

```text
web / channel conversation
- 保持当前对话 session

A2A delegation
- 保持当前 delegation/runtime task 模式

heartbeat
- 每 agent 一个长期 session
- 负责观察、反思、蒸馏、生成目标
- 不直接承担所有任务执行

objective_task trigger
- 每 objective 一个稳定 session
- external_conv_id = objective:{objective_id}
- 当前兼容 focus_ref：external_conv_id = objective:focus:{normalized_focus_ref}

scheduled_job
- 每次 run 独立 session
- 结果写 artifact
- 需要长期上下文时显式使用 context_from

event_wait
- 事件命中后挂到 objective session
- 没有 objective 时必须显式创建一次性 job 或新 objective
```

## P0 审计目标

P0 不改变线上行为，不做迁移，不自动修复。只建立一个只读审计层，把当前系统里的断点暴露出来。

P0 识别以下问题：

```text
orphan_focus_task
- focus.md 里有未完成标准任务，但没有 enabled trigger 绑定 focus_ref

noncanonical_focus_item
- focus.md 里看起来像任务，但不符合 canonical 格式，系统不会解析

trigger_focus_ref_missing
- trigger.focus_ref 指向的任务不存在于 focus.md

completed_focus_trigger_active
- focus.md 任务已完成，但对应 trigger 仍 enabled

scheduled_trigger_without_focus_ref
- cron / once / interval trigger 没有 focus_ref，先作为 warning

agent_no_model_blocking_autonomy
- agent 有 heartbeat 或 enabled trigger，但 primary_model_id 为空

trigger_runtime_gap
- lookback 窗口内存在 trigger session，但没有 RuntimeTask(task_type="trigger")

heartbeat_runtime_gap
- lookback 窗口内存在 heartbeat session，但没有 RuntimeTask(task_type="heartbeat")
```

P0 endpoint：

```text
GET /api/admin/autonomous-audit
```

查询参数：

```text
tenant_id?: UUID
agent_id?: UUID
lookback_hours: int = 24
```

返回 finding 结构：

```json
{
  "severity": "error | warning | info",
  "category": "orphan_focus_task",
  "agent_id": "uuid",
  "trigger_id": "uuid|null",
  "focus_ref": "string|null",
  "message": "human-readable summary",
  "evidence": {},
  "recommendation": "next action"
}
```

线上验证命令形态：

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "https://<railway-domain>/api/admin/autonomous-audit?lookback_hours=24"
```

## P1 已实现内容

P1 完成了以下闭环增强：

- trigger fired batch 会创建 `RuntimeTask(task_type="trigger")`。
- heartbeat execution 会创建 `RuntimeTask(task_type="heartbeat")`。
- 成功执行写 `status="completed"`，异常写 `status="failed"`。
- 无 agent、agent 不可运行、无模型、模型不存在、重复 heartbeat lease 等路径写 `status="skipped"` 和 `metadata_json.skip_reason`。
- `RuntimeTask.status="skipped"` 会设置 `completed_at`，避免跳过任务长期停留在 active 状态。
- `set_trigger` / `update_trigger` 支持 `trigger_class`。
- `trigger_class="objective_task"` 必须绑定 `focus_ref` 或 `config.objective_id`。
- 若提供了 `focus_ref` 或 `objective_id` 但未显式传 `trigger_class`，后台默认按 `objective_task` 处理，兼容旧 focus-bound trigger。
- `list_triggers` 会附带 Trigger Diagnostics，暴露 orphan、blocked、stale 类问题。
- heartbeat skill opportunity prompt 明确要求先 `tool_search` + `load_skill`，再决定是否 `save_skill`。

P1 仍然复用现有 `runtime_tasks.metadata_json`，不新增表、不做 migration。

## P2 已实现内容

P2 完成了以下触发连续性增强：

- `trigger_class="objective_task"` 的 trigger 会生成稳定 objective session key。
- 若存在 `config.objective_id`，session key 为 `objective:{objective_id}`。
- 若暂时只有 `focus_ref`，session key 为 `objective:focus:{normalized_focus_ref}`。
- 历史 trigger 即使没有 `trigger_class`，只要有 `focus_ref` 也会按 objective session 复用。
- trigger daemon 按 `(agent_id, objective_session_key)` 分组触发；不同 objective 不再混入同一次 LLM invocation。
- objective trigger 会复用 `chat_sessions.external_conv_id` 对应 session，并把最近 session 历史作为 `memory_messages` 注入。
- 新增 `trigger_reconciler`，独立负责把 `focus.md` 中已完成任务对应的 enabled trigger 关闭。
- trigger daemon 每个 tick 先运行 completed-focus reconciler；heartbeat 末尾保留兼容 wrapper。
- trigger RuntimeTask metadata 会记录 `objective_session_key`，便于审计。

P2 仍然不新增 `agent_objectives` 表，不改变 `focus.md` 的事实源地位；它只是把当前 focus/objective trigger 的执行连续性补齐。

## P3 已实现内容

P3 完成了目标事实源切换：

- 新增 `agent_objectives` 表。
- `focus.md` 改为 `agent_objectives` 的可读投影，投影头包含 `AUTO-GENERATED FROM agent_objectives`。
- 启动时会读取现有 agent workspace 的 canonical `focus.md` task 行，写入 objective ledger，再重新渲染投影。
- 新 agent 初始化时会把 blueprint/default focus task 同步到 objective ledger。
- `set_trigger` / `update_trigger` 对 objective task 会创建或绑定对应 objective，并把 `config.objective_id` 写回 trigger config。
- trigger daemon 在执行前会把 legacy focus 文件中的 canonical task 同步到 objective ledger，兼容 agent 通过 `write_file/edit_file` 更新 focus 的旧路径。
- completed focus 自动关闭 trigger 的 reconciler 改为优先读取 objective ledger；文件解析只作为兼容 fallback。
- autonomous audit 优先使用 objective ledger 渲染出的 focus projection，而不是把文件本身当事实源。
- 当前 P3 不新增前端 objective UI，不移除 `focus_ref`，也不阻断旧 agent 写 `focus.md`；这些旧入口会被同步进 DB。

## P4 之后方向

P4：

- 增加 wake gate、output artifact、context_from、per-job model/toolset/workdir。
- 增加 backoff、stale SLA、失败蒸馏。

## P0 边界

P0 做：

- 新增只读审计服务。
- 新增 platform admin endpoint。
- 汇总 focus、trigger、runtime task、heartbeat/trigger session 四类事实。
- 报告当前自主系统断点。

P0 不做：

- 不新增 `agent_objectives` 表。
- 不改 trigger firing 行为。
- 不自动 cancel / repair。
- 不改 heartbeat 运行逻辑。
- 不要求前端 UI 立刻接入。
- 不把 `focus.md` 改成 DB 投影。

## 测试与验收

验证命令：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate

pytest tests/services/test_focus_state.py \
       tests/services/test_autonomous_audit.py \
       tests/api/test_admin_autonomous_audit.py \
       tests/services/test_runtime_task_service.py \
       tests/services/test_trigger_daemon.py \
       tests/services/test_trigger_reconciler.py
```

P0 视为完成，当且仅当：

- 审计 endpoint 能在 Railway 上返回当前自主系统断点。
- 报告至少覆盖 focus、trigger、runtime task、heartbeat/trigger session 四类事实。
- 报告是只读的，不改变任何线上行为。
- 所有新增测试通过。
- 文档明确说明当前架构问题、P0 范围、P1-P4 路线。
- 当前已知问题能被报告出来：无模型阻塞、trigger/heartbeat runtime gap、focus/trigger 绑定缺失或失效。

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

系统分三层：

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

P3 之后必须再补一层：`Objective Intake`。P3 解决的是目标事实源，尚未解决“目标如何可靠地产生”。如果没有独立的目标摄取和调度闭环，agent 仍然只能靠 HR 初始化、会话里手写 `focus.md`、trigger reason、heartbeat prompt 自觉行为来产生目标，这不是可审计的 24 小时自主进化系统。

```text
Objective Intake
- 从 HR blueprint、用户会话、runtime attempt、heartbeat/dream signal 中发现目标候选
- 对候选目标做来源标记、置信度评分、去重合并、审批门控
- 只把通过 gate 的目标写入 active objective ledger
```

最终原则：

```text
Objective Intake 负责发现目标
Objective Ledger 负责保存目标事实
Wake Reconciler 负责把 active objective 变成可执行唤醒策略
Trigger Daemon 只负责到点唤醒
RuntimeTask / Attempt 负责记录实际执行
Evaluator 负责用证据推进 objective 状态
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

## 自主目标设定策略

目标来源分四类，不能混为一谈：

```text
HR Blueprint
- 创建 agent 时由用户确认
- first_tasks / first mission 直接生成 active objectives
- 适合初始化目标，不适合长期自主目标发现

User Conversation
- 从用户会话中提取承诺、待办、长期跟踪、定时交付
- 显式请求可进入 active
- 模糊意图只能进入 proposed，等待澄清或下次确认

Runtime Reflection
- 从 trigger/attempt 的失败、阻塞、未完成下一步中生成目标候选
- 主要用于恢复目标、拆解下一步、记录能力缺口
- 默认 proposed；低风险内部修复可自动 active

Self-Evolution Signal
- 从 heartbeat/dream/evolution files 中发现内部改进目标
- 例如重复失败、重复工作流、技能缺口、质量标准漂移
- 只能生成内部目标，不直接触发外部消息或业务承诺
```

目标状态机：

```text
proposed -> active -> running -> completed
                  \-> blocked
                  \-> snoozed
proposed -> rejected
```

目标 gate 规则：

```text
explicit_user_request
- 用户明确要求 agent 后续执行或持续跟踪
- 可以 active

confirmed_hr_blueprint
- HR preview + 用户确认后的 first_tasks / scheduled work
- 可以 active

internal_self_improvement
- 不触达外部系统、不承诺用户业务结果
- 可以 active，但必须限制优先级和频率

implicit_inference
- agent 从对话中推断出来，但用户没有明确委托
- 只能 proposed

external_side_effect
- 会发送消息、创建外部任务、改外部文档、调用付费或敏感系统
- 必须 proposed 或 requires_approval，除非原始用户委托已经明确授权
```

判定例子：

```text
"帮我每天发日报"
-> active objective + scheduled wake policy

"之后关注一下这个方向"
-> proposed objective，等待确认或下一次会话澄清

"我觉得你应该更严谨"
-> memory/feedback，不是 objective

"你这次失败是因为没检查权限"
-> internal self-improvement objective 或 blocked_pattern

"创建后先做这三件事"
-> HR confirmed active objectives
```

heartbeat 的职责边界：

```text
heartbeat
- 负责记忆蒸馏、自我观察、内部改进 proposal
- 不作为万能执行器
- 不直接承担业务目标执行

objective_task trigger
- 负责唤醒具体 active objective
- 使用 objective stable session
- 每次执行写 RuntimeTask / Attempt
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

## P4 已实现内容：自主目标生成与唤醒闭环

P4 把 `Objective Intake`、`Objective Wake Reconciler`、`Objective Evaluation` 和 agent-facing objective tools 合成一个闭环，避免只做目标生成或只做 trigger 调度形成能力孤岛。

实现后的闭环：

```text
Conversation / HR / Runtime Signal
-> Objective Intake
-> Objective Gate
-> AgentObjective ledger
-> Wake Reconciler
-> objective_task trigger
-> RuntimeTask / stable objective session
-> Objective Evaluator
-> completed / blocked / running objective state
```

P4A — Objective Intake：

```text
- 新增 objective intake 服务
- 从用户会话 session close hook 中提取显式后续任务、模糊意图、低风险内部改进候选
- HR 创建 agent 时把 first_tasks / scheduled work 写入 confirmed active objectives
- 每条 proposal 必须包含 source、confidence、risk_level、autonomy_class、suggested_status、evidence
- 不直接调用 LLM 执行业务任务，只负责目标发现和结构化
```

P4B — Objective Wake Reconciler：

```text
- 扫描 active objectives
- 为缺少 wake policy 的目标自动创建 objective_task trigger
- 已有 enabled objective wake 不重复创建
- HR boot trigger 改为绑定第一个 objective_id
```

P4C — Objective Gate：

```text
- 增加 autonomy gate：explicit_user_request、confirmed_hr_blueprint、internal_self_improvement、implicit_inference、external_side_effect
- implicit / external-side-effect 目标默认 proposed，不自动 active
- 内部自我改进目标可自动 active，但必须是低风险、不触达外部系统
```

P4D — Objective Evaluation：

```text
- objective completed 必须带 evidence
- RuntimeTask / trigger result 进入 evaluator
- 失败或阻塞写 blocked_reason 和 failure metadata
- focus-key session lookup 按 agent_id 过滤，避免跨 agent objective key 碰撞
```

P4 新增文件：

```text
backend/app/services/objective_intake.py
backend/app/services/objective_wake_reconciler.py
backend/app/services/objective_evaluator.py
backend/app/tools/handlers/objectives.py
backend/app/services/agent_tool_domains/objectives.py
```

P4 新增测试：

```text
backend/tests/services/test_objective_intake.py
backend/tests/services/test_objective_wake_reconciler.py
backend/tests/services/test_objective_evaluator.py
backend/tests/tools/test_objective_tools.py
backend/tests/api/test_objectives_api.py
```

P4 连带统一改造：

```text
- runtime hooks：session close 后自动走 objective intake，排除 trigger/heartbeat/dream/delegation/agent 内部 session
- trigger daemon：每 tick 先运行 objective wake reconciler，trigger 完成后走 objective evaluator
- HR handler：创建 agent 的初始任务不再只落 focus/trigger，而是进入 objective ledger 并绑定 trigger
- tool registry：list/propose/update/complete objective 进入核心工具面
- prompts/skills：heartbeat、trigger-guide、workspace-guide、delegation-guide 改为 objective ledger 优先
- filesystem guidance：focus.md 是 projection，不再指导 agent 把它当事实源
- autonomous audit：新增 proposed/active-without-wake/blocked/stale objective findings
- API：新增 objective list/propose/update endpoint，completion 必须提供 evidence
```

P4 验收标准：

```text
- HR 创建 agent 后，first_tasks 生成 active objectives，且 boot wake policy 绑定第一个 objective_id
- 用户会话中的显式后续任务能生成 active objective
- 用户会话中的模糊意图只能生成 proposed objective
- active objective 缺少 trigger 时，wake reconciler 自动补齐 objective_task trigger
- objective_task trigger 执行后写 RuntimeTask，并把 evidence 回写 objective 状态
- completed objective 必须有 evidence，不能只靠自然语言声明完成
- 外部副作用目标在未授权时不能自动 active
- autonomous audit 能报告 proposed/active/orphan/stale/blocked objective 状态
```

## P5 之后方向

原 P4 中的 wake gate、output artifact、`context_from`、per-job model/toolset/workdir、backoff、stale SLA、失败蒸馏应下沉到 P5。

原因是这些能力很重要，但它们优化的是“已有自主目标如何更可靠地执行”。在 P4 完成前，系统还缺少目标生成闭环；先做 artifact/context/model pinning 会让执行层更强，但不会解决“agent 自己到底应该做什么”的核心问题。

P5：

- 增加 wake gate 和 preflight check。
- 增加 output artifact。
- 增加 `context_from`。
- 增加 per-job model/toolset/workdir。
- 增加 backoff、stale SLA、失败蒸馏。
- scheduled_job 与 objective_task 分离得更彻底。

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

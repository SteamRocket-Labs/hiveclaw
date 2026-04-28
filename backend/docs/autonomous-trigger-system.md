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

P2 当时仍未新增 `agent_objectives` 表；它只补齐 focus/objective trigger 的执行连续性。P3 之后，目标事实源已经切换到 `agent_objectives`，`focus.md` 只保留为 projection。

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

## P5 已实现内容：执行可靠性与运营闭环

P5 补齐的是“已有自主目标如何可靠执行”。P4 让 agent 知道自己该做什么，P5 让每一次唤醒都可门控、可恢复、可产出、可审计。

P5 闭环：

```text
Wake Policy / Trigger
-> preflight / wake gate
-> RuntimeTask attempt ledger
-> stable session or standalone scheduled_job session
-> output artifact
-> objective completion / blocked state
-> failure backoff / recovery objective
-> audit finding
```

P5 已实现：

```text
- wake gate / preflight：trigger fire_count 更新前检查 agent/model/objective approval/backoff/event_wait lifecycle。
- output artifact：trigger RuntimeTask 完成后写 runtime_artifacts/triggers/{runtime_task_id}.json。
- context_from：scheduled/objective trigger 可显式加载 objective/session 上下文。
- per-job model/toolset/workdir：trigger config 支持 model_id、toolset、excluded_tool_names、workdir。
- backoff：trigger 失败后写 failure_count、last_failure、backoff_until，成功后清理失败状态。
- stale SLA：objective metadata.stale_after_hours 超时后标记 blocked/stale。
- 失败蒸馏：重复 trigger failure 后生成 internal self-improvement recovery objective。
- scheduled_job / event_wait 默认分类：无 objective 的 cron/once/interval 默认 scheduled_job；poll/on_message/webhook 默认 event_wait。
- event_wait 生命周期：event_wait 必须有 max_fires 或 expires_at。
- approval API：proposed objective 可 approve/reject，approval 会清理 requires_approval 并激活目标。
- legacy cleanup：auto-dream 不再直接修改 objective ledger 生成的 focus.md projection。
```

P5 后仍属于产品运营层的增强：

```text
- 前端 objective approval 队列。
- blocked/stale objective 运营处理台。
- 长期 SLA dashboard 和趋势报表。
```

## P6 已实现与验收：Trigger 前端控制台增强适配

P6 的目标不是给现有触发器列表加更多字段，而是把前端页面升级为“自主系统运营控制台”。页面必须展示同一条闭环里的事实：

```text
Objective Ledger
-> Wake Policy / Trigger
-> RuntimeTask Attempt
-> Session / Artifact
-> Audit / Lifecycle Finding
```

前端不能把 `focus.md`、trigger reason、activity log 文本解析成第二套目标系统。所有目标状态以 `agent_objectives` 为准；所有唤醒策略以 `agent_triggers` 为准；所有执行结果以 `runtime_tasks` 与 artifact 为准。

### UI 暴露原则

P6 的关键不是“把后端能力都暴露出来”，而是把用户需要做判断和操作的内容默认展示，把排障信息放进诊断层，把内部实现细节留在后台。

```text
Default Operator UI
- 让用户知道 agent 当前承诺了什么、是否会自主执行、执行是否成功、哪里需要人工处理。
- 面向业务/运营用户，展示意图和状态，不展示内部字段名。

Advanced Diagnostics
- 让管理员或调试者定位为什么没有触发、为什么跳过、为什么失败。
- 可展开，可复制 ID/code，但不作为普通用户的主路径。

Internal Policy
- 由后台策略、reconciler、preflight、daemon 自动处理。
- 不在普通 UI 暴露成开关，避免用户误以为每个字段都需要手工配置。
```

默认 UI 只暴露这四件事：

```text
1. Objective
- 目标是什么
- 当前状态：待审批 / 运行中 / 阻塞 / 已完成 / 已拒绝
- 成功标准和完成证据

2. Wake
- 会不会自动醒
- 大概什么时候醒：一次性 / 每天 / 每 N 分钟 / 等待事件
- 如果不会醒，原因是什么：缺模型 / 目标待审批 / 已暂停 / 已过期 / 失败退避中

3. Result
- 最近一次是否执行
- 成功、失败、跳过还是仍在运行
- 用户可读的失败/跳过原因

4. Action
- 审批 / 拒绝 proposed objective
- 暂停 / 恢复 wake policy
- 查看结果 artifact
- 提供 completion evidence
- 修复阻塞项，例如配置模型或重新授权能力
```

默认 UI 不应该暴露为主控件的内容：

```text
- raw trigger_class：默认显示为“目标任务 / 定时任务 / 等待事件 / 系统维护”。
- objective_id / focus_ref：默认显示目标标题；ID 只放诊断抽屉。
- raw config JSON：只放诊断抽屉。
- context_from：默认由模板和后台策略管理；高级定时任务才允许配置。
- model_id per-job pinning：默认继承 agent 模型；只在高级设置显示。
- toolset / excluded_tool_names：默认由 capability/policy 管理；只在高级设置显示。
- workdir：默认后台选择；只在高级设置显示。
- cooldown_seconds：默认隐藏；只有高频 poll/event_wait 高级设置显示。
- backoff_until / failure_count：默认合成为“失败退避中，预计 X 后重试”；原始值放诊断抽屉。
- runtime_task_id / session id / artifact path：默认显示“最近执行/结果文件”；原始 ID 和路径放诊断抽屉。
- metadata_json 任意字段：永远不作为前端直接 contract。
```

需要默认暴露的“能力未打开/阻塞”项：

```text
- agent 没有 primary model，导致 heartbeat/trigger 无法运行。
- proposed objective 等待审批，导致自主唤醒被 gate 拦截。
- objective active 但没有 wake policy。
- event_wait 没有 max_fires/expires_at，导致无限等待风险。
- scheduled_job 没有自包含 prompt/reason，导致每次独立 session 不可靠。
- 最近 attempt 连续 skipped/failed，且用户需要处理授权、模型、能力、外部连接问题。
```

### 当前代码事实

```text
Backend trigger model
- agent_triggers 已有 name/type/config/reason/focus_ref/is_enabled/fire_count/max_fires/cooldown_seconds/expires_at。
- 高级能力主要放在 config：trigger_class、objective_id、context_from、model_id、toolset、excluded_tool_names、workdir、backoff_until、failure_count 等。

Backend trigger API
- GET /api/agents/{agent_id}/triggers 返回基础 TriggerResponse。
- PATCH /api/agents/{agent_id}/triggers/{trigger_id} 只支持 config/reason/is_enabled/max_fires/cooldown_seconds/expires_at。
- 当前 REST API 没有 create trigger endpoint；创建主要走 agent tool set_trigger。
- 当前 REST API 没有 trigger run history、preflight diagnostic、artifact read endpoint。

Backend objective API
- GET /api/agents/{agent_id}/objectives 已存在。
- POST /api/agents/{agent_id}/objectives/proposals 已存在。
- PATCH /api/agents/{agent_id}/objectives/{objective_id} 已存在。
- POST approve/reject 已存在。

Frontend current state
- AgentDetail Aware tab 调用 triggerApi.list(id)。
- AgentAwareSection 仍从 focusContent 解析 checklist，再按 trigger.focus_ref 分组。
- 页面只支持启停、删除、简单 fired count、简单 reflection session 展开。
- frontend/src/api/domains/triggers.ts 只有 list/update/delete。
- frontend 还没有 objectives domain adapter，也没有 autonomous audit adapter。
```

### 前端信息架构

Agent Detail 的 Aware tab 应拆成四个工作区，而不是继续把所有东西塞进 focus markdown 视图：

```text
Objectives
- active/running/proposed/blocked/stale/completed 分组
- proposed 目标可 approve/reject
- blocked/stale 目标显示原因、最后 attempt、建议动作
- completed 目标显示 completion evidence

Wake Policies
- 每个 objective 显示绑定的 objective_task trigger
- standalone scheduled_job 单独分组
- event_wait 单独分组，强制显示 max_fires/expires_at
- system_maintenance 只读展示，不与业务目标混淆

Attempts
- RuntimeTask history，默认按目标、触发类型、状态过滤
- objective_id / trigger_id / trigger_class 只作为 diagnostics filter
- skipped / failed / completed / running 都要可见
- preflight skip_reason 要映射成用户可读原因，不让用户只看到“没运行”

Artifacts
- scheduled_job 与 trigger attempt 的 output artifact 列表
- artifact 默认展开显示 summary、final_reply、created_at
- trigger metadata、runtime_task_id、artifact path 只放 diagnostics
- artifact 是执行产物，不是新目标事实源
```

### 后端 BFF/API 需要补齐

直接让前端解析 `trigger.config` 和 `runtime_tasks.metadata_json` 会快速形成能力孤岛。P6 应先补一层 agent-scoped BFF，让前端消费稳定结构：

```text
GET /api/agents/{agent_id}/autonomy/overview
- 返回 objectives、triggers、recent_attempts、findings、totals
- tenant 用户可访问，只限自己有权限的 agent
- 复用 autonomous_audit 的纯审计逻辑，但不要求 platform_admin

GET /api/agents/{agent_id}/triggers
- response 增加 normalized 字段：
  display_kind
  display_title
  display_schedule
  attention_state
  attention_reason
  next_action
  linked_objective
  last_attempt
  last_artifact
- diagnostics 字段可选返回：
  trigger_class
  objective_id
  lifecycle_state
  blocked_reason
  backoff_until
  failure_count
  runtime_options_summary

POST /api/agents/{agent_id}/triggers
- 前端创建 trigger 不再绕 agent tool
- 使用同一套 validation：trigger_class、objective binding、event_wait lifecycle、scheduled_job 自包含 prompt
- objective_task 必须绑定 objective_id 或 focus_ref

GET /api/agents/{agent_id}/runtime-tasks
- 支持 task_type=trigger|heartbeat|delegation
- 支持 trigger_id/objective_id/status/limit
- 返回 display_summary/status/created_at/completed_at/artifact/diagnostics
- metadata_json 只由后端映射成稳定字段，不直接给 UI 依赖

GET /api/agents/{agent_id}/runtime-artifacts/{runtime_task_id}
- 只允许读取当前 agent workspace 下 runtime_artifacts/triggers/*.json
- 默认返回 title、created_at、summary、final_reply
- diagnostics 可返回 schema、runtime_task_id、trigger ids、相对路径

GET /api/agents/{agent_id}/autonomy/diagnostics
- agent-scoped audit findings
- platform admin 的 /api/admin/autonomous-audit 保留全局视角
```

### Trigger 表单策略

创建/编辑 trigger 应使用模式化表单，而不是一个 JSON config 文本框。

```text
objective_task
- 选择 active/proposed objective
- proposed objective 需要先 approve 才允许自动 wake
- 可配置 cadence：once / interval / cron
- 默认不显示 model/toolset/workdir/context_from
- 高级设置才允许 per-job model、toolset、workdir、context_from

scheduled_job
- 必须填写自包含 prompt/reason
- cron/once/interval
- 默认继承 agent 模型和能力
- 高级设置才允许 per-job model、toolset、workdir、context_from
- 不显示为业务目标进度

event_wait
- poll/on_message/webhook
- 必须 max_fires 或 expires_at
- 必须选择命中后的动作：绑定 objective、创建一次性 scheduled_job、或创建 proposed objective
- 没有 lifecycle 不允许保存

system_maintenance
- 只读展示 heartbeat/dream/distill/reconciler 类运行状态
- 不允许普通用户误创建业务 trigger
```

### 页面状态与交互

```text
状态 badge
- active / paused / expired / max_fires_reached
- waiting_approval / missing_model / no_wake_policy
- blocked / stale / failed_recently / no_recent_attempt
- backoff_active 只显示为“失败后等待重试”，不默认展示 raw backoff_until

安全操作
- pause/resume trigger
- approve/reject proposed objective
- retry failed trigger：默认显示“请求重试”，是否清 backoff 或立即触发由后端权限决定
- mark objective completed 必须提供 evidence
- delete trigger 前展示是否会留下 active objective without wake

可观测性
- 每个 trigger 行默认显示：会不会醒、何时醒、最近结果、是否需要处理
- 每个 objective 行默认显示：目标状态、wake 是否存在、最近 attempt、completion evidence
- scheduled_job 行默认显示：最近 artifact 摘要
- event_wait 行默认显示：等待什么事件、剩余次数或过期时间
- 原始 skip_reason、trigger_id、runtime_task_id、config、metadata 放诊断抽屉
```

### UI 设计约束

```text
- 这是运营工具，不做营销式大卡片 hero。
- 采用 dense dashboard：左侧 objective 列表，中间 wake policy/attempt，右侧详情抽屉。
- 表格/列表优先，卡片只用于单个 objective/attempt detail。
- 状态色不使用单一紫蓝主题；错误、阻塞、等待、完成要有清晰区分。
- 所有文案进入 en.json / zh.json。
- 图标按钮使用现有图标库，危险动作必须有确认。
```

### P6 分阶段实施

```text
P6A — Read-only Autonomy Overview
- 新增 frontend objectives/autonomy API adapter。
- 新增 agent-scoped autonomy overview endpoint。
- Aware tab 从 focus.md 解析切换到 objective ledger 展示。
- trigger 仍可启停删除，但默认列表只显示 display_kind/objective/status/next_action。
- 原始 trigger_class/backoff/skip_reason 只进入 diagnostics drawer。
- 新增测试覆盖 overview empty/error/blocked/proposed/scheduled_job/event_wait。

P6B — Attempts & Artifacts
- 新增 runtime task list endpoint。
- 新增 artifact read endpoint。
- 前端增加 attempt timeline 与 artifact drawer。
- skipped/failed/completed 都可见，用户能看到没唤醒的原因。

P6C — Objective Approval Console
- 前端接入 objective approve/reject/update。
- proposed 目标不再隐藏在审计报告里。
- completed objective 必须提交 evidence。
- active objective without wake 显示修复入口。

P6D — Trigger Create/Edit Wizard
- REST create trigger endpoint 与现有 set_trigger tool 共用 validation helper。
- 表单按 objective_task/scheduled_job/event_wait/system_maintenance 分流。
- event_wait 没有 max_fires/expires_at 时前后端都拒绝。
- scheduled_job 必须显示 artifact 策略。

P6E — Realtime & Ops Polish
- WebSocket trigger_notification 增加 runtime_task_id/objective_id/trigger_id。
- Aware tab 收到事件后局部刷新 attempts/objectives/triggers。
- 加入筛选：status、trigger_class、objective、last 24h failed/skipped。
```

### P6 验收标准

```text
- 用户能从一个页面看清：目标是什么、为什么会醒、醒了没有、结果在哪里。
- proposed objective 能被 approve/reject，不再只能靠后台 API。
- 目标任务、定时任务、等待事件、系统维护在 UI 中是一等分类，但不要求用户理解 raw trigger_class。
- event_wait lifecycle、scheduled_job artifact 在 UI 中是 first-class。
- skipped/failed trigger 不再只表现为“没有反应”，而是显示用户可读的原因和下一步动作。
- 页面不再把 focus.md 当事实源，只作为可选 raw projection。
- 前端没有直接依赖未定义的 metadata_json 私有字段；复杂字段由 BFF normalized 后返回。
- 普通 UI 不默认暴露 raw config、ID、metadata、workdir、toolset、context_from、cooldown、backoff 原始值。
- 所有新增 UI 文案完成 en/zh i18n。
- API、服务层、前端组件均有测试覆盖。
```

## Autonomy P0-P6 验收状态

Autonomy P0-P6 是本文件内部的自主目标/触发/执行/UI 实施阶段，和架构对齐文档里的 Architecture Phase 0R-5 不是同一套编号，不能混算。本文件不定义任何后续 P 编号；后续长期 harness 能力在架构计划中单独命名为 Harness H1-H6。

| 阶段 | 目标 | 当前验收状态 |
|------|------|-------------|
| P0 | 只读审计层，暴露 focus/trigger/runtime/session 断点 | 已完成：`/api/admin/autonomous-audit` 与服务/API 测试覆盖 |
| P1 | trigger/heartbeat 写 RuntimeTask，skipped 也入账 | 已完成：trigger、heartbeat、skip reason、diagnostics 进入执行账本 |
| P2 | objective_task 稳定 session 与 completed-focus reconciler | 已完成：objective session key、分组触发、独立 reconciler 已落地 |
| P3 | `agent_objectives` 成为目标事实源，`focus.md` 变投影 | 已完成：migration、同步、projection、legacy fallback 已落地 |
| P4 | Objective Intake / Gate / Wake / Evaluation 闭环 | 已完成：会话/HR/runtime signal 生成目标，wake reconciler 与 evaluator 已接入 |
| P5 | wake gate、artifact、context_from、backoff、approval、event lifecycle | 已完成：执行可靠性与运营闭环已落地 |
| P6 | Autonomy/Aware UI 与 BFF，前端不解析 raw metadata | 已完成：agent autonomy API、overview service、attempt/artifact endpoint、trigger P6 API、Aware UI 与 i18n/tests 已落地 |

当前验收基线：

```text
backend pytest     1887 passed,7 skipped,4 warnings
backend ruff       All checks passed
frontend test      18 files,70 tests passed
frontend build     passed
alembic heads      add_agent_objectives_0427 (head)
git diff --check   clean
```

Autonomy P0-P6 完成后的剩余问题不是“自主闭环没做完”，而是：

```text
- 需要补 architecture tests 防止 Autonomy P0-P6 主干回退。
- 需要把 feature/agent-session-feishu 中 session/Feishu/tool runtime 治理资产选择性迁移。
- 需要进入独立的 Harness H1-H6，把 autonomy trunk 升级成长期 harness trunk。
```

2026-04-27 Phase 0R 已补第一层护栏：

```text
backend/tests/architecture/test_phase0r_boundaries.py
- kernel 不直接导入 DB/model/API 层。
- approved tool execution 必须走公开的 approved boundary。
- objective ledger 是 focus projection 的事实源。
- trigger/heartbeat 执行必须进入 RuntimeTask attempt ledger。
- objective session / heartbeat session / runtime session_context 必须保持显式边界。
- memory layer 不创建或修改 objective ledger。
```

本轮同时把 post-approval 工具执行从私有 `_execute_tool_direct` 调整为 `execute_approved_tool` / `ToolRuntimeService.execute_approved`，审计记录包含 `approved_by_user_id` 与 `approval_id`。这属于架构护栏修正，不改变 trigger firing 或 objective lifecycle 行为。

2026-04-27 H1-H6 第一版可执行 harness trunk 已补齐：

```text
backend/tests/architecture/test_tool_runtime_single_entry.py
- direct fallback 不再复制第一类工具分发，只保留 unknown/MCP passthrough。
- ToolExecutionRegistry 请求只由 ToolRuntimeService 组织。

backend/tests/architecture/test_permission_hardline.py
- governance 移除 approval compat wrapper，直接调用 canonical request_approval。
- secret exfiltration shell 命令必须升级 approval。

backend/tests/architecture/test_context_memory_boundaries.py
- websocket 不再把外部 memory_context 直灌 runtime prompt。
- memory/objective 继续保持分层。

backend/tests/architecture/test_session_context_contract.py
- trigger/task/heartbeat 等内部 session 不进入普通聊天列表或普通 recall。

backend/tests/architecture/test_legacy_schedule_trunk.py
- legacy schedules API 只作为 AgentTrigger cron facade。
- manual schedule run 排队 one-shot trigger，不再直接调用旧 scheduler runtime。

backend/tests/architecture/test_h2_harness_hardening.py
- ToolRuntimeBackend contract 已存在，默认 local，docker backend 未启用时 fail-closed。
- 外部 / agent workspace / HR / save_skill skill 导入路径必须经过 SkillGuard。

backend/tests/architecture/test_h3_context_engine_contract.py
- ContextEngine / MemoryProvider contract 已存在。
- memory snapshot、memory recall、runtime hints、knowledge injection 都通过 source/fence/context_artifacts 记录。

backend/tests/architecture/test_h4_long_task_runtime_contract.py
- long task plan/progress/resume artifact 建立在 RuntimeTask metadata 之上。
- long task validation report 会检查 plan、progress、terminal status、完成证据和中断原因。

backend/tests/architecture/test_h5_evolution_ledger_contract.py
- skill distiller 自动 promote 路径写入 evolution candidate、eval run、promotion decision。
- promotion decision 包含 rollback_ref 和 critical regression gate。
- evolution validation report 会检查 candidate source、eval reward/trace、promotion rollback_ref 和 critical regression。

backend/tests/architecture/test_h6_session_key_contract.py
- SessionKey 统一 objective、runtime task、external conversation 的 stable_id 映射。
- invoker 对所有 entrypoint 的 SessionContext 自动补 session_key metadata。

backend/tests/architecture/test_harness_validation_contract.py
- `/admin/harness-validation` 只读聚合 H4/H5 证据。
- Harness validation 复用 `validate_long_task_run(write_report=False)` 与 `validate_evolution_ledger(write_report=False)`。
- 不写 RuntimeTask，不生成 evolution candidate，不自动修复 artifact。
```

2026-04-28 生产修复 dry-run 层已补齐：

```text
backend/app/services/autonomy_repair_plan.py
- 输入：/admin/autonomous-audit 的 findings + 当前 Agent / Trigger / Objective / tenant default model 快照。
- 输出：只读 dry-run repair actions，不修改 DB，不写 focus.md，不触发 agent。
- 所有 action 都带 action_id、action_type、risk、auto_apply、proposed_change、preconditions、manual_steps。
- 自动可应用只表示“证据足够、未来 apply endpoint 可以执行”，dry-run endpoint 本身永远不执行修复。

GET /api/admin/autonomy-repair-plan
- platform_admin only。
- query：tenant_id?、agent_id?、lookback_hours=24。
- 用于把 audit findings 转成可审阅的生产修复计划。
```

当前 dry-run action 分类：

```text
create_objective_wake_policy
- active objective / orphan focus 已有 objective ledger 证据时，预测创建 objective_task trigger。
- 复用 objective_wake_reconciler.build_objective_trigger_payload。

classify_scheduled_trigger
- cron / once / interval 无 focus_ref 且未标 trigger_class 时，预测标记 trigger_class=scheduled_job。

assign_default_primary_model
- agent 有自主唤醒路径但无 primary_model_id，且 tenant 有 enabled default model 时，预测补 primary_model_id。

repair_trigger_focus_ref_from_objective
- trigger.focus_ref 缺失/过期，但 trigger.config.objective_id 仍绑定有效 objective 时，预测把 focus_ref 修回 objective_key。

disable_completed_focus_trigger
- trigger 仍绑定 completed focus/objective 时，预测 disable trigger。

review_missing_focus_ref / configure_primary_model / review_orphan_focus_task
- 证据不足或业务意图不确定，必须人工确认，不自动修复。
```

生产验证命令：

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "https://<railway-domain>/api/admin/autonomy-repair-plan?lookback_hours=1" \
  | jq '.totals'
```

受控应用入口：

```text
POST /api/admin/autonomy-repair-plan/apply
- platform_admin only。
- body.action_ids 可选；不传表示应用当前计划中 max_risk 允许的所有 auto_apply=true action。
- body.max_risk 默认 medium；合法值 low / medium / high。
- 每条 action 应用前重新检查 precondition；不满足则 skipped，不会强行写。
- 当前支持的 apply action：
  - classify_scheduled_trigger
  - create_objective_wake_policy
  - assign_default_primary_model
  - repair_trigger_focus_ref_from_objective
  - disable_completed_focus_trigger
  - create_objective_for_existing_trigger
  - disable_model_blocked_heartbeat
```

`create_objective_for_existing_trigger` 的边界：

```text
它只把已经 enabled 的 trigger 纳入 objective ledger，不新增唤醒频率。
如果同 agent + focus_ref 已有 objective，则复用 objective 并补 trigger.config.objective_id。
如果没有 objective，则创建 source=trigger_repair 的 open objective，然后把 trigger 标为 objective_task。
```

`disable_model_blocked_heartbeat` 的边界：

```text
只在 agent 无 primary_model、无 enabled trigger、且 heartbeat 是唯一 autonomous wake path 时自动关闭 heartbeat。
如果 agent 有 enabled trigger 但无模型，仍要求人工先配置模型或显式停用 trigger。
```

2026-04-28 Harness canary 写入入口已补齐：

```text
POST /api/admin/harness-canary/run
- platform_admin only。
- 目的：证明 H4/H5 harness 生产通路可写、可恢复、可审计。
- 默认目标：有 heartbeat 或 enabled trigger 的 autonomous agents。
- 默认幂等：已有 H4 long-task artifact 或 H5 evolution ledger 的 agent 会 skipped。
- body:
  - tenant_id?
  - agent_id?
  - include_h4=true
  - include_h5=true
  - max_agents=50
  - force=false
```

H4 canary 写入：

```text
- RuntimeTask(task_type="harness_canary", status="completed")
- runtime_artifacts/long_tasks/<runtime_task_id>/plan.json
- runtime_artifacts/long_tasks/<runtime_task_id>/progress.jsonl
- runtime_artifacts/long_tasks/<runtime_task_id>/validation_report.json
```

H5 canary 写入：

```text
- evolution/evolution_ledger.jsonl
  - evolution_candidate.v1
  - evolution_eval_run.v1
  - evolution_promotion_decision.v1(decision="hold")
- evolution/evolution_validation_report.json
```

Canary 边界：

```text
- 不创建或修改 objective。
- 不创建、启用或停用 trigger。
- 不修改 skill / prompt / model / permission。
- 不把 canary 当成自然业务目标完成或真实行为 promotion。
- 所有写入 metadata 都显式标记 harness_canary=true 和 no_behavior_change=true。
```

生产验证命令：

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  "https://<railway-domain>/api/admin/harness-canary/run" \
  -d '{"include_h4":true,"include_h5":true,"max_agents":50}' \
  | jq '.totals'

curl -H "Authorization: Bearer $TOKEN" \
  "https://<railway-domain>/api/admin/harness-validation?lookback_hours=1" \
  | jq '.totals'
```

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
- 文档明确说明当前架构问题、P0 范围、P1-P6 路线。
- 当前已知问题能被报告出来：无模型阻塞、trigger/heartbeat runtime gap、focus/trigger 绑定缺失或失效。

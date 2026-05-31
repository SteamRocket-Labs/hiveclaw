# Hive Plan Mode 设计文档

> 本文定义 Hive 的完整 Plan Mode。它不是单个 skill、不是 Deep Research 的专用参数、也不是
> `Agent.execution_mode` 的一种取值。Plan Mode 是 Hive runtime 的一等阶段:
>
> **先规划 -> 用户确认 -> 再执行 / 落地自主行为。**
>
> 最高不变量:
>
> **未经真实用户确认的计划,不得产生任何可执行的自主行为。**

---

## 1. 一句话定义

Plan Mode 是:

> 当用户诉求会让 agent 在当前对话之外继续自主执行,或会触发外部可见 / 高风险副作用时,
> 系统必须先创建一份可审阅、可修改、可确认的结构化计划。只有用户在后续独立动作中确认
> 该计划版本后,系统才允许把它交给执行层。

它覆盖的不只是定时任务。所有这些都属于 Plan Mode 的管辖范围:

- 创建或修改未来会自动触发的 wake policy / trigger。
- 创建 objective 后让 agent 自主推进。
- 启动 long-running task,尤其是会持续查找、写文件、调用工具、产生报告或后续动作的任务。
- delegate / A2A,也就是把工作交给另一个 agent 后异步推进。
- 外部可见动作的准备链路,例如未来自动发 Feishu / email / Slack / plaza 消息。
- 高风险状态变更,例如批量写文件、删除/覆盖已有文件、修改生产配置、花钱调用外部系统。

Plan Mode 不等于“永远不执行”。它的目标是让执行有明确边界:

```text
用户诉求
  -> PlanRequest(draft/planning)
  -> plan.md + plan_json(awaiting_confirmation)
  -> 用户确认具体 plan_version
  -> confirmed handoff
  -> Objective / Trigger / RuntimeTask / Tool execution
```

---

## 2. 当前代码事实

### 2.1 Hive 目前没有完整 Plan Mode

当前仓库里有几块相似能力,但都不是完整 Plan Mode:

| 现有机制 | 当前作用 | 缺口 |
|---|---|---|
| Deep Research plan gate | `plan_confirmed=false` 时返回 `needs_plan`,不执行研究 | 只覆盖 deep research 工具,不是全局 runtime gate |
| Complex Task Executor skill | 让 agent 对复杂任务写 `workspace/<task>/plan.md` | 是 prompt/skill 习惯,不是安全边界,不能阻止工具执行 |
| long task artifact | 写 `runtime_artifacts/long_tasks/<id>/plan.json` 和 progress | 是恢复/审计 artifact,不是用户确认闸门 |
| objective proposed gate | `objective_task` 绑定 proposed/requires_approval objective 时 trigger preflight 会 skip | 只覆盖 objective_task,不覆盖普通 scheduled_job、REST trigger、agent set_trigger |
| capability approval | 工具权限审批 | 审批的是单个 action/tool,不是整段工作计划 |

### 2.2 现在的自主执行入口会绕过规划

前端创建 trigger:

```text
backend/app/api/triggers.py
  create_trigger()
  -> reason 只校验非空
  -> AgentTrigger(..., is_enabled=True)
```

Agent 工具创建 trigger:

```text
backend/app/services/agent_tool_domains/triggers.py
  _handle_set_trigger()
  -> 校验 name/type/config/reason
  -> AgentTrigger(...)
  -> enabled trigger 直接进入 daemon
```

Trigger 到点执行:

```text
backend/app/services/trigger_daemon.py
  _invoke_agent_for_triggers()
  -> "Trigger: <name>\nReason: <reason>"
  -> 作为 user message 喂给 LLM
```

所以今天的主要问题不是“计划模式没触发”,而是:

> 上游没有一个统一的 PlanRequest 账本和硬闸门。`reason` 被当成未来执行说明,但它没有经过
> 目标、步骤、成功标准、副作用、成本、确认版本这些计划约束。

### 2.3 下游 objective gate 可以复用,但不能当完整方案

`trigger_preflight.py` 已经能拦住一类情况:

```text
if trigger_class == "objective_task":
    objective = load bound objective
    if objective.metadata.requires_approval or objective.status == "proposed":
        skip "objective_requires_approval"
```

这个门控有价值,但边界很窄:

- 只对 `objective_task` 生效。
- 必须能绑定到 objective。
- 不拦普通 `scheduled_job` / `event_wait` / `system_maintenance`。
- 不阻止 REST API 或 agent tool 直接创建 enabled trigger。

因此完整 Plan Mode 必须在上游入口和工具 runtime 处 fail-closed,不能只依赖 trigger daemon 的局部 preflight。

---

## 3. 设计目标

Plan Mode 要解决四件事:

1. **先想清楚再排活**
   任何自主执行都必须先明确目标、步骤、成功标准、唤醒策略、所需能力、外部副作用、成本和停止条件。

2. **确认对象稳定**
   用户确认的是某个 `plan_version` + `plan_hash`,不是一段可被 agent 后续悄悄改写的聊天文本。

3. **执行层拿不到未确认计划**
   未确认计划不创建 enabled trigger,不启动自主 RuntimeTask,不执行外部副作用工具。

4. **计划可追踪、可恢复、可审计**
   计划要能在 chat、Aware/Autonomy 页面、审批/审计记录、runtime artifact 中被稳定引用。

---

## 4. 非目标

这些不是 Plan Mode 第一阶段要解决的问题:

- 不重写整个 Objective 系统。
- 不把所有普通对话都变成确认流程。
- 不要求只读查询、文件读取、web search、解释代码等低风险同步动作都先写计划。
- 不用 Plan Mode 替代 capability approval。Plan Mode 管“整段工作是否应开始”,approval 管“某个敏感动作是否可执行”。
- 不把 `Agent.execution_mode` 扩展成 `plan`。Plan Mode 是 turn/workflow phase,不是 agent 的长期人格/调度模式。

---

## 5. 触发规则

### 5.1 进入策略: 显式、自动、推荐、兜底

Plan Mode 是底层能力,但入口不能一律"强制计划"。正确分层是:

| 入口 | 行为 | 示例 |
|---|---|---|
| 用户显式选择/表达 Plan Mode | 立即创建 PlanRequest | 前端 Plan Mode toggle、"先做计划"、"进入计划模式" |
| 长任务创建 | 自动进入 Plan Mode | "完整调研这个行业并出报告" |
| 定时/监控类任务创建 | 推荐进入 Plan Mode,询问是否继续 | "每天 9 点帮我整理新闻"、"盯一下这个网站" |
| 工具/REST 兜底发现即将开启未来自主行为 | hard gate,返回 `needs_plan` 或要求显式 opt-out | agent 直接调 `set_trigger`、legacy schedule API |

定时/监控的默认 UX 是**推荐**,不是硬强制:agent 应先说明需要确认频率、范围、成本、停止条件和通知方式,询问用户是否进入 Plan Mode。用户明确拒绝推荐后,可以继续创建定时/监控任务,但必须写入审计豁免 `plan_exempt_reason=user_declined_plan_mode`,并在工具参数或 REST body 里带 `plan_mode_decision=declined`。

### 5.2 必须创建 PlanRequest

满足任一条件,必须创建 PlanRequest,不能直接执行:

| 类型 | 示例 | 原因 |
|---|---|---|
| Objective 自主推进 | “以后持续跟进这个客户” | 需要明确目标、成功标准和停止条件 |
| long-running autonomous task | “完整调研这个行业,出报告,后面自动跟进” | 多轮、长耗时、高 token/工具成本 |
| delegation / A2A | “让另一个 agent 去做并持续汇报” | 当前 agent 把执行权交出去 |
| 外部可见 future action | “明天自动发给群里” | 对外沟通必须先确认 |
| 高风险状态变更 | “定期改生产配置 / 批量删除旧文件” | 不可逆或影响面大 |

### 5.3 可以不进入 Plan Mode

这些可以直接执行,但仍受普通工具治理和 approval gate 约束:

- 只读问题: “解释这个文件”,“查一下现在状态”。
- 当前轮低风险同步动作: “读取日志并总结”,“跑测试并告诉我结果”。
- 用户明确要求立即执行且动作低风险、可逆、只在当前对话内完成。
- 用户已明确拒绝"定时/监控任务建议进入 Plan Mode"的推荐,且本次动作只是在创建/更新该定时或监控任务。
- 已确认 PlanRequest 的 handoff 执行。

### 5.4 模糊情况先推荐或先计划

如果请求中出现这些语义,UX 层先识别并分流:

```text
每天 / 每周 / 定时 / 以后 / 持续 / 自动 / 监控 / 盯着 / 到时候 /
提醒我 / 等回复 / 有变化就 / 帮我长期 / 自己跟进 / 派给 / 让某个 agent 去
```

长任务、委派、高风险 future action 的失败方向必须是“不执行,先计划”。定时/监控任务的常规方向是“先推荐 Plan Mode,等用户确认是否进入”,而不是静默创建 trigger。

但**关键词匹配 / 意图识别只是 UX 层预判**(让 chat 能提前推荐或弹 plan card),不是安全边界,判错不致命。真正的 fail-closed 安全闸门锚在工具/REST/执行兜底层(§9.2):无论意图识别是否命中,agent 一旦真的去调会开启未来自主行为的工具,必须有 confirmed plan、显式用户 opt-out,或其他受支持的审计豁免。**意图识别负责"体验",兜底层负责"安全",两者解耦。**

---

## 6. 核心数据模型

### 6.1 PlanRequest 是 canonical source of truth

`plan.md` 不是唯一事实源。Markdown 适合人读,但权限、版本、确认、并发、审计必须有 DB 记录。

新增表建议:

```text
agent_plan_requests
```

字段:

```text
id                         uuid pk
tenant_id                  uuid nullable/index
agent_id                   uuid not null/index
session_id                 text nullable/index
runtime_task_id            uuid nullable/index
requested_by_user_id       uuid nullable/index
source                     text  # web_chat | trigger_api | tool_runtime | objective | channel | system
intent_type                text  # autonomous_wake | long_task | delegation | external_action | state_change
original_request           text
status                     text  # draft | planning | planning_failed | awaiting_confirmation | confirmed | rejected | superseded | expired
plan_version               integer
plan_hash                  text
plan_markdown_path         text nullable
plan_json                  jsonb
handoff_payload            jsonb nullable
handoff_status             text nullable  # not_started | completed | failed | skipped
confirmed_by_user_id       uuid nullable
confirmed_at               timestamptz nullable
rejected_by_user_id        uuid nullable
rejected_at                timestamptz nullable
superseded_by_plan_id      uuid nullable
expires_at                 timestamptz nullable
created_at                 timestamptz
updated_at                 timestamptz
metadata_json              jsonb
```

索引:

```text
(agent_id, status)
(tenant_id, status)
(session_id, created_at)
(runtime_task_id)
```

### 6.2 Markdown 是 user-facing artifact

同时写:

```text
{AGENT_DATA_DIR}/{agent_id}/plans/{plan_id}.md
```

Markdown frontmatter 必须镜像 DB 里的关键字段,但不能取代 DB:

```markdown
---
schema: hive_plan.v1
plan_id: <uuid>
agent_id: <uuid>
tenant_id: <uuid|null>
status: awaiting_confirmation
plan_version: 1
plan_hash: <sha256>
intent_type: autonomous_wake
created_at: <iso8601>
confirmed_by: null
confirmed_at: null
---
```

### 6.3 plan_json 是执行契约

`plan_json` 必须结构化,用于 handoff、测试和 UI 渲染:

```json
{
  "schema": "hive_plan.v1",
  "title": "Daily industry news brief",
  "intent_type": "autonomous_wake",
  "objective": "Produce a useful daily industry brief for the user.",
  "motivation": "User asked for a recurring morning industry news summary.",
  "steps": [
    {
      "order": 1,
      "description": "Collect official and high-signal news sources.",
      "expected_output": "Source list with timestamps."
    }
  ],
  "success_criteria": [
    "Brief includes 5-10 material updates with source links.",
    "Brief is sent only after checking for duplicates."
  ],
  "wake_policy": {
    "type": "cron",
    "timezone": "Asia/Shanghai",
    "expr": "0 9 * * 1-5"
  },
  "required_capabilities": ["web_search", "web_fetch", "send_feishu_message"],
  "external_side_effects": [
    {
      "kind": "message",
      "channel": "feishu",
      "audience": "requesting user",
      "requires_confirmation": true
    }
  ],
  "risk_assessment": {
    "level": "medium",
    "reasons": ["recurring autonomous wake", "external message"]
  },
  "estimated_cost": {
    "tokens_per_run": "medium",
    "expected_duration": "1-3 minutes"
  },
  "stop_conditions": [
    "User cancels the plan.",
    "Three consecutive failed runs occur.",
    "No relevant news for 5 consecutive runs."
  ],
  "handoff": {
    "target": "objective_trigger",
    "create_objective": true,
    "create_trigger": true
  }
}
```

---

## 7. 状态机

```text
DRAFT
  -> PLANNING
  -> AWAITING_CONFIRMATION
       -> CONFIRMED
            -> handoff_status: completed / failed (status 仍为 confirmed)
       -> PLANNING       # 用户要求修改
       -> REJECTED
       -> EXPIRED
       -> SUPERSEDED
```

状态含义:

| 状态 | 含义 | 是否允许执行 |
|---|---|---|
| `draft` | 捕获诉求,尚未生成计划 | 否 |
| `planning` | 规划子流程正在生成/修订计划 | 否 |
| `planning_failed` | 计划生成失败(schema 不合格等) | 否,需补充信息或重试 |
| `awaiting_confirmation` | 计划可见,等待用户确认 | 否 |
| `confirmed` | 用户确认了具体版本(handoff 成败记 `handoff_status`,不另设 status) | 允许 handoff |
| `rejected` | 用户拒绝 | 否,终态 |
| `superseded` | 被新版本/新计划替代 | 否,终态 |
| `expired` | 超时未确认 | 否,终态 |

唯一能进入执行层的路径:

```text
awaiting_confirmation --真实用户确认同一 plan_version/plan_hash--> confirmed
```

禁止路径:

```text
planning -> confirmed                 # agent 自己确认
awaiting_confirmation -> trigger       # 未确认直接建 trigger
awaiting_confirmation -> runtime task  # 未确认直接启动自主任务
confirmed(version=1) + plan edited -> execute version=2
```

---

## 8. 安全不变量

1. **禁止自我确认**
   agent 不得在产出计划的同一轮里确认。确认必须来自后续独立用户动作。

2. **确认绑定版本和 hash**
   确认请求必须带 `plan_id`, `plan_version`, `plan_hash`。任一不匹配则 409。

3. **确认后不可变**
   confirmed 计划不得原地编辑。要修改必须创建新版本或新 PlanRequest,旧计划 `superseded`。

4. **fail-closed**
   PlanRequest 缺失、状态异常、hash 不匹配、DB/file 不一致时,不得 handoff。

5. **真实用户动作**
   偏好记忆、历史授权、agent 自述、system summary 都只能预填计划,不能确认计划。

6. **工具层二次防线**
   即使 prompt 忘了要求计划,`ToolRuntimeService` 和 trigger API 也必须拦住未确认自主动作。

7. **外部副作用仍走 ActionPreflight / approval**
   Plan confirmed 不等于所有工具免审。它只表示“这段工作可以开始”。具体外部动作仍要经过现有 preflight/approval。

---

## 9. Runtime 接入点

### 9.0 安全架构:PlanModeGate 服务 + 纵深防御(核心)

Plan Mode 的安全保证**不是单点锚**(早期草案一度以为是"工具层")。Hive 有多条"开启自主行为"的路径不经过 agent tool,逐个堵既漏又脆。正确形态是一个系统级 `PlanModeGate` 服务,以**纵深防御**接入所有收口点。

**完整自主入口清单(已逐条核实):**

| 入口 | 过 agent tool? | 风险 |
|---|---|---|
| `set_trigger`/`update_trigger`(`execute_approved` 可能绕过普通 gate) | 是 | 建/重启 enabled trigger |
| REST `POST /agents/{id}/triggers` | 否 | 默认 `is_enabled=True` |
| legacy `POST /agents/{id}/schedules` | 否 | 直建 enabled cron/once trigger |
| objective intake + wake reconciler(hook 每 tick 跑) | 否 | active objective 自动补 enabled trigger |
| objective REST(写 wake_policy/active) | 否 | 间接自动 wake |
| HR `create_digital_employee`(内部直写 first mission/boot trigger) | 半 | 走 HR blueprint 确认;产物带 `confirmed_hr_blueprint` 豁免,不进 Plan Mode hard gate |
| web/Feishu task auto-sync(regex → `execute_task`) | 否 | 聊天命中即后台执行 |
| tasks tool/REST `todo` | 否/是 | 后台 `execute_task` |
| REST `/collaborate/delegate` | 否 | 直接 async delegation |
| existing enabled triggers / webhook / on_message | 否 | cutover 遗留,daemon 直跑 |

**两层防御:**

```text
早拦层(体验:尽早提示用户去确认计划)
  tool(tagged):set_trigger / update_trigger / delegate_to_agent / manage_tasks(todo create) / deep_research_start
  REST:triggers / schedules / objectives 激活 / collaborate-delegate / tasks 自动执行
  hook:objective intake 不再把 wake_policy 意图直接判 active

兜底层(安全保证:确定性 fail-closed,少数收口覆盖所有上游)
  trigger daemon 执行前 preflight:无 confirmed plan 且无 cutover 豁免的 autonomous trigger → skip/quarantine
  wake reconciler:只为 confirmed plan / 豁免 objective 创建 enabled trigger
  task_executor:execute_task 启动前校验
  delegation:启动前校验
```

**关键洞察:** 所有自主路径最终都收敛到 **(a) 创建/启用 enabled trigger** 或 **(b) 启动 `execute_task`/delegation**。兜底层守住这两个动作,即使某个上游入口漏了早拦,fail-closed 仍挡得住——**这是"原能力"该有的安全闭包,不留洞**。早拦层负责体验(早提示),兜底层负责安全(最终保证),职责分离。

**cutover(存量 enabled trigger):** 严格模式 quarantine/disable;兼容模式打 `metadata.plan_exempt_reason="preexisting_before_cutover"`,daemon 只放行有明确豁免标记的旧 trigger。

### 9.1 Web chat turn

`start_web_chat_run()` 当前每轮会创建 `RuntimeTask(task_type="web_chat_turn")`,再调用 `invoke_agent()`。

Plan Mode 接入方式:

1. 在 web chat run 创建后,先做 intent classification。
2. 如果请求命中 Plan Mode:
   - 显式 Plan Mode 或长任务:创建 `agent_plan_requests` row。
   - 定时/监控:先推荐进入 Plan Mode 并等待用户选择;不立即创建 PlanRequest。
3. 创建 PlanRequest 后:
   - 启动受限 planning invocation,或用 deterministic builder 生成 plan draft。
   - 写 `plans/{plan_id}.md`。
   - assistant 返回 plan card / markdown preview。
   - 当前 web_chat_turn 完成,不继续执行原请求。
4. 用户点击 Confirm / Revise / Reject,走 plan API。

Planning invocation 必须禁用高风险工具,且**复用现有工具收窄机制,不新增 `execution_mode` 字段**(与 §4 非目标一致):

```text
# 复用 InvocationRequest 现有字段 + deep research RC11 已加的 disable_tools
allowed_tool_names=("list_files", "read_file", "web_search", "web_fetch", "list_triggers", "list_objectives")
excluded_tool_names=("set_trigger", "update_trigger", "send_*", "delete_file", "write_file")
max_tool_rounds=8
```

planning 期"只读取、不落地"的约束由上面的 allow/exclude 列表 + `disable_tools` 表达,是 invocation 级临时参数,不写入 `Agent` 持久字段。

### 9.2 ToolRuntimeService(早拦层之一)

工具层是早拦层里最大的一块(覆盖 agent 主动调的所有自主工具),但它是**纵深防御的一层,不是唯一锚点**——真正的安全保证在 §9.0 的兜底层。

实现要点:

- **打标用代码级 `ToolMeta` 字段,不用 `Tool.config`**:`Tool.config` 是 DB seed,seeder 不覆盖已有非空 config,做不了治理事实源。ToolMeta 已有现成的 `governance: "" | "safe" | "sensitive"`,扩展它(如加 `"autonomous"`)或新增 `plan_gate` 字段即可——import 期注册的代码级 registry。
- **tagged 工具必须在所有执行入口统一检查**:`execute()` / `execute_direct()` / `execute_approved()`,尤其 `execute_approved` 不能成为绕过点。不对所有工具查 DB,只对 tagged 工具查。

第一批 tagged(真正"开启自主行为"的)工具:

```text
set_trigger                       # 启用未来自主 wake;用户拒绝推荐后可带 plan_mode_decision=declined
update_trigger                    # 只有显式替换为 autonomous wake config 时 hard gate;改 reason/name 等不 gate
delegate_to_agent                 # 交出执行权,异步推进
manage_tasks (action=create→run)  # 会后台 execute_task
deep_research_start               # 已有 plan_confirmed(RC11-RC15 刚修好);MVP 只桥接登记 PlanRequest,不重构
```

**不要**把 `send_feishu_message` / `write_file` / `edit_file` / `delete_file` / `create_digital_employee` 放进第一批强拦——当前轮明确授权的低风险同步动作和 HR blueprint 已确认的创建动作不该进 Plan Mode,它们继续走现有 ActionPreflight / capability approval / HR blueprint 确认。只有"未来自动发送、批量覆盖、改生产配置"这类才升级到 plan。

工具层返回应类似 Deep Research:

```json
{
  "ok": false,
  "status": "needs_plan",
  "plan_id": "...",
  "plan_version": 1,
  "summary": "Confirm the plan before creating this autonomous wake policy.",
  "plan_preview": {...},
  "next_action": "Show this plan to the user and wait for explicit confirmation."
}
```

### 9.3 Trigger REST API

`POST /agents/{agent_id}/triggers` 不能继续默认 `is_enabled=True` 创建自主 trigger。

改法:

- 新增可选 `confirmed_plan_id`, `confirmed_plan_version`, `confirmed_plan_hash`。
- 如果请求创建 enabled autonomous trigger 且没有 confirmed plan,返回 409/422:

```json
{
  "error": "plan_required",
  "message": "Create and confirm a plan before enabling this wake policy."
}
```

前端 New wake 入口应先创建 PlanRequest,不是直接 create trigger。

### 9.4 Objective approval

现有 objective `proposed` / `requires_approval` 可以保留,但 Plan Mode 不应直接复用它当主账本。

推荐关系:

```text
PlanRequest.confirmed
  -> create/update AgentObjective(status="open" or "active", metadata.plan_id=...)
  -> create AgentTrigger(is_enabled=True, config.objective_id=...)
```

如果 PlanRequest 尚未确认:

```text
可以创建 proposed objective 作为预览
但不得创建 enabled trigger
或只能创建 disabled trigger draft
```

### 9.5 Long task

已有 `long_task_runtime.write_long_task_plan_artifact()` 可复用为执行期 artifact。

但 Plan Mode 的用户确认计划必须先于 long task execution:

```text
PlanRequest.awaiting_confirmation
  -> 用户确认
  -> RuntimeTask(task_type="long_task"/"deep_research"/...)
  -> runtime_artifacts/long_tasks/<id>/plan.json
```

### 9.6 Channel sessions

Feishu / Slack / Telegram / WeCom 这类非 web 入口也要遵守 Plan Mode。

最小方案:

- 非 web channel 共用 `_call_agent_llm()` 的入口分类:显式 Plan Mode/长任务创建 PlanRequest;定时/监控先推荐是否进入 Plan Mode。
- 用户明确拒绝推荐后,下一轮普通 agent 执行可通过 `plan_mode_decision=declined` 创建定时/监控任务并写审计豁免。
- 非 web channel 真正进入 Plan Mode 时,agent 回复计划摘要和确认按钮/确认文字。
- 如果 channel 无按钮能力,要求用户明确回复确认短语。
- 确认仍必须落到 plan API / service,不能让 agent 在普通回复里自行置 confirmed。

---

## 10. Plan generation 策略

### 10.1 不靠单纯 prompt 自律

Complex Task Executor 的 `plan.md` 是有用起点,但只能当模板来源。Plan Mode 的核心必须由 service 强制:

```text
PlanModeService.create_plan_request()
PlanModeService.generate_plan()
PlanModeService.revise_plan()
PlanModeService.confirm_plan()
PlanModeService.reject_plan()
PlanModeService.handoff_confirmed_plan()
```

### 10.2 初版推荐 deterministic skeleton + LLM fill

第一版不要让 LLM 自由决定 schema。流程:

1. Service 根据 intent_type 生成 plan_json skeleton。
2. 受限 planning invocation 只填字段和解释,不执行工具。
3. Service 校验 JSON schema。
4. Service 生成 Markdown。
5. Service 计算 hash 并落库。

如果 LLM 输出不合格:

```text
status = planning_failed
不执行
返回用户: 计划生成失败,需要补充信息或重试
```

---

## 11. API 草案

```text
POST /api/agents/{agent_id}/plans
GET  /api/agents/{agent_id}/plans
GET  /api/agents/{agent_id}/plans/{plan_id}
POST /api/agents/{agent_id}/plans/{plan_id}/revise
POST /api/agents/{agent_id}/plans/{plan_id}/confirm
POST /api/agents/{agent_id}/plans/{plan_id}/reject
POST /api/agents/{agent_id}/plans/{plan_id}/handoff
```

确认请求:

```json
{
  "plan_version": 1,
  "plan_hash": "sha256:...",
  "reason": "Looks good"
}
```

确认响应:

```json
{
  "ok": true,
  "status": "confirmed",
  "plan_id": "...",
  "handoff_status": "not_started"
}
```

---

## 12. UI 设计方向

UI 不需要第一版很复杂,但必须做到“确认对象清楚”。

### 12.1 Chat inline plan card

当用户在 chat 里触发 Plan Mode:

- assistant message 展示 plan card。
- card 展示:
  - 目标
  - 执行步骤
  - 成功标准
  - 唤醒策略
  - 外部副作用
  - 预估成本
  - 风险等级
- 操作:
  - Confirm
  - Request changes
  - Reject

### 12.2 Aware / Autonomy plan queue

在 Agent Aware/Autonomy 页面增加计划队列:

- Awaiting confirmation
- Confirmed / handoff pending
- Rejected / expired
- Handoff failed

现有 Objectives 和 Wake Policies 继续展示执行后的世界; Plan Queue 展示执行前的世界。

### 12.3 Workspace approvals 不是主入口

现有 approval UI 可以显示 pending plan,但不应把 Plan Mode 伪装成普通 approval。

原因:

- approval 是 action-level。
- Plan Mode 是 workflow-level。
- 两者可以共享通知、审计、按钮样式,但数据模型和语义不能混。

---

## 13. Handoff 契约

Confirmed plan 的 handoff 必须显式、可重试、可审计。

第一批 handoff target:

| target | 产物 |
|---|---|
| `objective_trigger` | `AgentObjective` + enabled `AgentTrigger` |
| `long_task` | `RuntimeTask` + long_task plan artifact |
| `tool_action` | one-shot tool execution,仍走 preflight/approval |
| `delegation` | child RuntimeTask / A2A task |

handoff 必须记录:

```json
{
  "handoff_status": "completed",
  "created_objective_id": "...",
  "created_trigger_id": "...",
  "created_runtime_task_id": null,
  "completed_at": "..."
}
```

如果 handoff 失败:

```text
PlanRequest.status 保持 confirmed,失败写 handoff_status="failed"
(不新建 handoff_failed status,避免破坏 confirmed 的审计语义)
不得部分静默成功
必须把已创建的 disabled/draft artifacts 或 rollback 状态写入 metadata
```

---

## 14. MVP 实施顺序

### Phase 0: 文档和测试夹具

- 定稿本文。
- 新增测试 fixtures: autonomous wake request、scheduled job request、low-risk read-only request、external message request。

### Phase 1: PlanRequest service + storage

- migration: `agent_plan_requests`。
- `PlanModeService`:
  - create
  - render markdown
  - confirm/reject
  - version/hash validation
- 文件 artifact: `plans/{plan_id}.md`。

验收:

```bash
pytest tests/services/test_plan_mode_service.py -q
```

### Phase 2: trigger 硬闸门

- `set_trigger` 没有 confirmed plan 时返回 `needs_plan`。
- REST `create_trigger` 没有 confirmed plan 时拒绝创建 enabled autonomous trigger。
- 已 confirmed plan handoff 可以创建 trigger。

验收:

```bash
pytest tests/tools/test_plan_mode_trigger_gate.py tests/api/test_plan_mode_trigger_api.py -q
```

### Phase 3: web chat plan turn

- web chat run 先识别 Plan Mode intent。
- 命中后返回 plan card,不继续执行原请求。
- 用户 Confirm 后再 handoff。

验收:

```bash
pytest tests/services/test_web_chat_plan_mode.py -q
```

### Phase 4: Objective + Trigger handoff

- confirmed `objective_trigger` plan 创建 objective + enabled trigger。
- objective metadata 写入 `plan_id`, `plan_version`, `plan_hash`。
- trigger config 写入 `plan_id`。

验收:

```bash
pytest tests/services/test_plan_mode_handoff.py tests/services/test_trigger_p5_policies.py -q
```

### Phase 5: UI

- chat inline plan card。
- Aware/Autonomy plan queue。
- i18n: `en.json` + `zh.json`。

验收:

```bash
cd frontend
npm run build
npm test -- planMode
```

---

## 15. 测试矩阵

必须覆盖:

| 场景 | 期望 |
|---|---|
| 用户说“每天 9 点提醒我” | 推荐进入 Plan Mode,不直接创建 enabled trigger |
| 用户回复“不用计划模式,直接创建” | 不再重新进入 Plan Mode;后续 `set_trigger` 或 REST trigger/schedule body 可带 `plan_mode_decision=declined` 创建并记录豁免 |
| agent 调用 `set_trigger` 但无 confirmed plan | 返回 `needs_plan`,不落库 trigger |
| REST create trigger 无 confirmed plan | 4xx `plan_required` |
| 用户确认 plan_version=1/hash 匹配 | status -> confirmed |
| 用户确认旧 version | 409 |
| plan confirmed 后被修改 | hash 改变,旧确认不能执行 |
| confirmed handoff 成功 | 创建 objective/trigger,记录 IDs |
| rejected plan | 不创建 objective/trigger/runtime task |
| deep research 未确认 | 仍返回 needs_plan,并登记/兼容 PlanRequest |
| low-risk read-only chat | 不进入 Plan Mode |
| external action plan confirmed | 开始执行,具体 send 仍走 ActionPreflight/approval |

---

## 16. 需要改掉的旧假设

原设计里有几条需要替换:

1. **“只覆盖定时任务”不够**
   Plan Mode 必须覆盖所有 future/autonomous/high-risk workflow。

2. **“plan.md 是唯一交付物/事实源”不够**
   必须有 DB canonical PlanRequest。Markdown 是用户可读 artifact。

3. **“下游未确认不执行门控天然存在”不完整**
   只有 objective_task 局部门控存在。完整门控必须加在 trigger API、set_trigger、web chat 和 ToolRuntimeService。

4. **“Objective 接入是范围外”只能在文档阶段成立**
   实施 MVP 至少需要一个 handoff contract,否则 confirmed plan 没有可验证出口。

5. **“前端确认界面是后续工作”只能短暂成立**
   没有用户确认 UI,Plan Mode 无法闭环。MVP 可以很薄,但必须有。

---

## 17. 最终验收标准

Plan Mode 视为完成,当且仅当:

```text
□ 命中 Plan Mode 的请求无法直接创建 enabled trigger / autonomous RuntimeTask / delegation。
□ 系统创建 canonical PlanRequest,并写出可读 plan.md。
□ plan_json 含 objective、steps、success_criteria、wake_policy、capabilities、side_effects、cost、stop_conditions。
□ 用户能在 chat 或 Aware/Autonomy UI 看到计划并确认/修改/拒绝。
□ 确认绑定 plan_version + plan_hash。
□ agent 不能 self-confirm。
□ 旧版本确认不能执行新版本计划。
□ confirmed plan 能 handoff 到目标执行层。
□ rejected/expired/superseded plan 不残留 enabled autonomous artifacts。
□ 所有外部副作用仍经过 ActionPreflight / approval。
□ 测试覆盖 service、tool gate、REST API、web chat、handoff、UI 基本路径。
```

---

## 18. 推荐第一版边界

第一版不追求覆盖所有 handoff target,但**安全边界必须一次堵全**——Plan Mode 是原能力,留一个绕过口就等于没有。Codex 评审证实:只堵 `set_trigger` + 主 triggers REST,仍能从 legacy `/schedules` 和 objective intake 两条路"随手建长期 trigger"。

最小但**无洞**的切口:

1. `agent_plan_requests` + `PlanModeService` + `PlanModeGate`。
2. **早拦层**:
   - tool gate(代码级 ToolMeta 打标):`set_trigger`/`update_trigger`/`delegate_to_agent`/`manage_tasks(todo create)`/`deep_research_start`,在 `execute`/`execute_direct`/`execute_approved` 全入口生效。
   - REST gate:`triggers` + legacy `schedules` + `objectives` 激活/wake_policy + `collaborate/delegate` + `tasks` 自动执行。
3. **兜底层(安全保证)**:
   - `wake_reconciler`:只为 confirmed plan / 豁免 objective 建 enabled trigger。
   - `trigger_daemon` 执行前 preflight:cutover 后无 confirmed plan 的 autonomous trigger 不执行。
   - `task_executor` / delegation 启动前校验。
4. Web chat plan UX:对 autonomous intent 弹 plan card(早拦,判错由兜底兜)。
5. Chat card + Aware plan queue:确认 / 拒绝 / 请求修改。
6. Confirmed plan 第一版只支持 `objective_trigger` handoff;存量 trigger 按 §9.0 cutover 处理。

这才真正解决最痛的问题:

> 任何路径(agent 工具、REST、legacy schedules、对话意图、HR 创建)都不能再靠一句随手 `reason` 创建会长期运行的自主 trigger。

后续再扩展 long task、channel confirmation、risk-tiered confirmation。

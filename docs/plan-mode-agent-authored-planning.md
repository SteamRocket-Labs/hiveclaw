# Plan Mode Agent-authored Planning 设计

> 本文是 `docs/plan-mode-design.md` 的原则性修正和下一阶段落地方案。
>
> 核心判断: 需要分析和规划的任务,必须接入 agent 的真实规划能力。系统可以做治理、持久化、确认和审计,但不能用 deterministic skeleton + tool args 冒充 agent 计划。

---

## 1. 问题定义

当前 Plan Mode 已经有几个正确的治理部件:

- `agent_plan_requests` 作为计划账本。
- `PlanModeGate` / `ToolRuntimeService` 作为执行前闸门。
- `plan_version` / `plan_hash` / `awaiting_confirmation` 作为用户确认边界。
- frontend plan card 作为确认、修改、拒绝入口。

但当前 plan 内容生成路径存在原则性错误:

```text
user request / intercepted tool args
  -> classifier chooses intent/action_kind
  -> deterministic skeleton
  -> tool_args_to_plan_fill()
  -> awaiting_confirmation
```

这条链路能产出结构化字段,但不能代表 agent 的理解、分析、取舍和计划能力。对用户来说,Plan Mode 的价值不是"系统拦住执行并填一张表",而是"agent 先认真想清楚,给出可审阅的工作方案"。

因此新的不变量是:

> Plan Mode 的 substantive plan content 必须由 agent authored planner 产生。系统只负责创建 planning envelope、限制 planner 权限、校验 schema、保存版本、展示确认、执行 handoff。

---

## 2. 目标和非目标

### 2.1 目标

1. **agent 参与分析**
   计划正文、步骤、风险、成本、停止条件、执行策略必须来自 agent planner invocation。

2. **保留治理硬边界**
   agent 可以写计划,但不能确认计划,也不能在 planning 阶段执行高风险或未来自主动作。

3. **保留可验证结构**
   planner 输出必须落到 `plan_json` schema,并生成可读 `plan.md`。

4. **上下文连续**
   planner 应继承当前 agent identity、role、soul、相关 memory、用户原始请求、会话上下文和被拦截 tool args。

5. **失败方向正确**
   planner 失败、schema 不合格、工具越权、上下文不足时,计划进入 `planning_failed` 或返回澄清问题,不得继续执行原动作。

### 2.2 非目标

- 不把 Plan Mode 变成 `Agent.execution_mode` 的持久人格模式。
- 不允许 agent 自己确认计划。
- 不让 deterministic builder 继续充当最终计划生成器。
- 不把所有低风险同步动作都升级成 Plan Mode。
- 不绕过 capability approval、ActionPreflight、Memory Control Plane。

---

## 3. 目标架构

```text
User request
  -> UX/runtime classifier
  -> create AgentPlanRequest(status=draft)
  -> transition planning
  -> invoke same agent in planner sandbox
       inputs:
         - original user request
         - current conversation slice
         - agent identity / role / memory context
         - intended action_kind / intent_type
         - intercepted tool args if any
       allowed tools:
         - read-only context tools only
       forbidden:
         - set_trigger / update_trigger
         - write_file / edit_file / delete_file
         - send_* external messages
         - execute_task / delegate_to_agent
         - any handoff or confirmation tool
  -> planner returns structured plan_json + markdown summary
  -> schema validation + policy validation
  -> status=awaiting_confirmation
  -> plan card
  -> user confirms exact version/hash
  -> confirmed handoff creates trigger/objective/task/delegation
```

关键点:

- classifier 只决定"是否需要计划"和初始 `intent_type`。
- deterministic skeleton 只允许作为 schema envelope 和 validation default,不能作为最终 substantive plan。
- tool intercept 只提供 planning input,不能直接变成 plan。
- `awaiting_confirmation` 的计划必须标记 `author_type=agent` 或等价 metadata,并保存 planner run evidence。

---

## 4. Planner Invocation

### 4.1 输入

Planner invocation 至少需要这些输入:

```json
{
  "plan_id": "uuid",
  "intent_type": "autonomous_wake | long_task | delegation | external_action | state_change",
  "action_kind": "create_enabled_trigger | start_long_task | ...",
  "source": "web_chat | tool_runtime | trigger_api | channel",
  "original_request": "raw user request",
  "conversation_context": ["recent turns"],
  "agent_context": "same agent identity/role/memory summary",
  "intercepted_tool": {
    "tool_name": "set_trigger",
    "arguments": {}
  },
  "planning_constraints": {
    "must_not_execute": true,
    "must_wait_for_user_confirmation": true,
    "allowed_tool_names": ["read_file", "list_files", "web_search", "web_fetch", "list_triggers", "list_objectives"],
    "max_tool_rounds": 8
  }
}
```

### 4.2 Planner prompt template

这是产品级 prompt 草案,不是最终实现代码:

```text
You are {agent_name}, planning before execution.

The user is asking for work that may continue beyond the current chat turn,
create future autonomous behavior, delegate work, or cause external/high-risk
side effects.

Your job in this invocation is to author a plan for the user to review.
You must not execute the requested work. You must not create triggers, tasks,
delegations, files, external messages, or other side effects. The runtime will
block those actions during planning.

Use available read-only tools only when needed to understand current state.
If the request is underspecified, state concrete assumptions and make the plan
reviewable. Ask for clarification only when no safe plan can be drafted.

Return a JSON object that conforms to the Plan Mode schema and a concise
markdown preview. The plan must include:

- objective
- motivation
- concrete steps
- success criteria
- wake policy or execution cadence if applicable
- required capabilities
- external side effects
- risk assessment
- estimated cost and duration
- stop conditions
- handoff target
- assumptions and open questions

The user must confirm the exact plan version before any execution can happen.
Do not claim that execution has started.
```

### 4.3 输出

Planner output 应该是严格结构化结果:

```json
{
  "plan_json": {
    "schema": "hive.plan.v1",
    "title": "...",
    "intent_type": "autonomous_wake",
    "objective": "...",
    "motivation": "...",
    "steps": [],
    "success_criteria": [],
    "wake_policy": {},
    "required_capabilities": [],
    "external_side_effects": [],
    "risk_assessment": {
      "level": "low | medium | high",
      "reasons": []
    },
    "estimated_cost": {
      "tokens_per_run": "...",
      "expected_duration": "..."
    },
    "stop_conditions": [],
    "handoff": {},
    "assumptions": [],
    "open_questions": []
  },
  "plan_markdown": "..."
}
```

---

## 5. 与现有系统的职责边界

| 模块 | 应做 | 不应做 |
|---|---|---|
| `classify_plan_mode_entry()` | 判断是否推荐/进入 Plan Mode,给出初始 intent | 生成最终计划内容 |
| `PlanModeService` | 创建 PlanRequest、调用 planner、校验、保存、版本化 | 用 skeleton 代替 agent planner |
| `ToolRuntimeService` | 拦截 gated tool,把 tool args 作为 planner input | 把 tool args 直接映射成最终 plan |
| `PlanModeGate` | 校验 confirmed plan/version/hash | 判断计划质量 |
| frontend plan card | 展示、编辑、确认、拒绝 | 静默确认或自动执行 |
| agent planner | 分析需求并产出计划 | 确认计划或执行计划 |

---

## 6. 入口改造

### 6.1 Web Chat 显式 Plan Mode / 长任务

当前逻辑:

```text
_maybe_handle_plan_mode_entry()
  -> ensure_awaiting_plan()
  -> deterministic plan
```

目标逻辑:

```text
_maybe_handle_plan_mode_entry()
  -> create PlanRequest(status=draft)
  -> start planner invocation
  -> await planner result
  -> status=awaiting_confirmation
  -> assistant message includes plan_id and card metadata
```

如果 planner 需要较长时间,web chat run 应持续 streaming planning progress,而不是立即返回空壳计划。

### 6.2 定时/监控推荐后的接受

当前风险:

```text
User: 每天下午 13:00 自动执行这个任务
Assistant: 建议进入计划模式
User: 进入计划模式
```

如果只按第二句话重新 classify,可能丢失上一轮的 schedule intent,误走 long_task。正确做法:

```text
User accepts latest recommendation
  -> load agent_plan_recommendations(latest pending for same user/session)
  -> use original_request + action_kind + intent_type
  -> create PlanRequest
  -> planner invocation based on original request
```

也就是说,"进入计划模式"应该绑定上一条 recommendation ledger,不是重新靠关键词猜。

### 6.3 Tool Runtime 拦截

当前逻辑:

```text
agent calls set_trigger(args)
  -> gate blocks
  -> tool_args_to_plan_fill(args)
  -> awaiting plan
```

目标逻辑:

```text
agent calls set_trigger(args)
  -> gate blocks
  -> create PlanRequest(source=tool_runtime)
  -> planner invocation receives:
       original user request
       attempted tool name
       attempted args
       reason the action is gated
  -> agent authors a user-reviewable plan
  -> return needs_plan with plan_id/plan_json/preview
```

这里 planner 可以利用 tool args,但必须把它们当 evidence/input,不能机械照抄成计划。

---

## 7. 数据模型补充

`agent_plan_requests.metadata_json` 至少应保存:

```json
{
  "author_type": "agent",
  "planner_runtime_task_id": "uuid",
  "planner_model_id": "uuid",
  "planner_prompt_version": "agent_plan_v1",
  "planner_allowed_tools": ["..."],
  "planner_source": "web_chat | tool_runtime",
  "intercept_signature": "...",
  "recommendation_id": "uuid",
  "quality_checks": {
    "schema_valid": true,
    "has_steps": true,
    "has_stop_conditions": true,
    "has_success_criteria": true
  }
}
```

后续如果要做计划质量审计,可以把 planner transcript 或摘要存入 runtime artifacts,但不要把完整敏感上下文暴露给前端。

---

## 8. 计划质量门槛

进入 `awaiting_confirmation` 前,至少校验:

- `objective` 非空且不是原请求的简单复制。
- `steps` 至少 2 条,每条包含可执行动作。
- `success_criteria` 至少 1 条。
- `stop_conditions` 至少 1 条。
- 定时/监控类必须有 `wake_policy.type`、timezone、频率或触发条件。
- 外部可见或高风险动作必须列入 `external_side_effects` 和 `risk_assessment.reasons`。
- 如果 planner 声明缺信息,状态应是 `planning_failed` 或 `needs_user_clarification`,而不是确认态空计划。

---

## 9. 落地阶段

### Phase 1: 文档和测试先行

- 新增 planner service 的单元测试,先定义:
  - web chat 显式 Plan Mode 会调用 agent planner。
  - recommendation accept 复用上一条 recommendation 的 original request。
  - tool intercept 不再使用 `tool_args_to_plan_fill()` 作为最终 plan。
  - planner schema 不合格时进入 `planning_failed`。

### Phase 2: Agent planner service

- 新增 `AgentPlanPlannerService` 或等价模块。
- 封装 planner invocation:
  - 复用 `invoke_agent()`。
  - 注入 planner prompt suffix。
  - 限制工具 allowlist / blocklist。
  - 设置较小 tool round 上限。
  - 要求 structured JSON output。

### Phase 3: 替换生成路径

- `PlanModeService.generate_plan()` 不再直接 `_apply_generation(skeleton + fill)`。
- 改为:
  - 创建 envelope。
  - 调 planner。
  - validate planner output。
  - persist plan JSON/markdown/hash。

保留 deterministic skeleton 作为:

- schema default。
- validation helper。
- planner 失败时的内部 debug artifact。

不能把它展示成可确认计划。

### Phase 4: Recommendation accept 修正

- 增加"接受最近 recommendation"路径。
- `进入计划模式` 不重新猜 intent,而是绑定 pending recommendation。
- rejected/declined recommendation 不可被复用。

### Phase 5: Frontend 状态

- plan card 支持:
  - `planning` loading state。
  - `planning_failed` state。
  - planner assumptions/open questions。
  - revise request 后重新进入 planner,生成新 version。

---

## 10. 验收标准

1. 对分析型任务,数据库里的 `agent_plan_requests.metadata_json.author_type` 是 `agent`。
2. plan 内容包含 agent 产出的步骤、风险、停止条件和成功标准。
3. 旧的 skeleton-only plan 不会进入 `awaiting_confirmation`。
4. 用户只回复"进入计划模式"时,系统能绑定上一条 recommendation 的原始请求。
5. planner 不能调用创建 trigger、发消息、写文件、委派、启动任务等执行型工具。
6. 用户确认前,任何 gated handoff 都被 fail-closed。
7. 用户确认后,执行层只消费同一 `plan_id + plan_version + plan_hash`。

---

## 11. 原则总结

Plan Mode 的核心产品承诺不是"先弹一张确认卡",而是:

> agent 先用自己的能力完成分析和计划,系统再把这个计划变成可确认、可审计、可执行的治理对象。

如果计划不是 agent authored,Plan Mode 就只剩流程控制,失去了用户期待的智能规划价值。

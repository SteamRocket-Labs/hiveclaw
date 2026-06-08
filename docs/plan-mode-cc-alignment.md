# Plan Mode CC Alignment

> 状态: **v0.2 文档优先修订（2026-06-08）——待实现**。
> 触发: Web3 研究员 RWA 周报生产实跑暴露出 plan "用不了"：它像结构化审批表，不像 agent 认真规划后的方案。
> 范围: Hive runtime 的 Plan Mode 语义、澄清机制、`exit_plan_mode` contract、PlanCard 展示、Plan Mode prompt。本文不动 read-only gate、plan hash、confirmation、audit、handoff 这些治理硬边界。

## 0. 结论

Hive 应该继承 Claude Code Plan Mode 的**机制**，不继承 Claude Code 的**coding-only 产品范围**。

要继承的机制:

- Agent 的规划由主循环智能产出，作为给人读的方案，而不是被填表式 schema 拖成结构化拼接。（注：RPC planner 已删，plan 当前**已经**由 agent 在主循环 `exit_plan_mode` authored——真问题不是"系统代写"，而是 schema 形态诱导填表，见 §1/§3 P0。）
- Plan 主体是一篇用户可读的 markdown 工作方案，而不是 13 个字段拼出来的表。
- 有关键未决问题时，agent 必须先问用户；问题解决前不应提交可确认计划。
- `exit_plan_mode` 是审批出口，不是规划能力本身。
- read-only 约束的是执行副作用，不应剥夺 agent 澄清需求、组织思路、维护私有 planning state 的能力。

不应继承的范围:

- Claude Code 说 `ExitPlanMode` 只用于需要写代码的 implementation steps，这是 Claude Code 作为 coding agent 的产品边界，不是 Hive 的设计法律。
- Hive 是综合 co-work/control-plane 平台。Plan Mode 可用于 coding、研究、内容、运营、销售、财务、自动化、协作委派等任务。
- Hive 的触发标准不是"是否写代码"，而是"是否需要先对齐计划/授权边界后再执行"。

## 1. 生产实证

用户请求:

```text
我需要做一个rwa的周报 进入计划模式计划一下
```

生产侧事实:

- Agent: `ec03ec3e-c4e8-417d-95f7-f84215e7b9c3`（Web3研究员）
- Session: `d02cd199-37e0-4a9b-b6d1-3b0ef81b5962`
- Plan: `2d416566-58eb-4b40-a0d6-86d6497cf128`
- Plan status: `awaiting_confirmation`
- Plan metadata: `author_type="workflow"`, `planner_prompt_version="structured_fill.v1"`
- Matching `agent_work_ledgers`: `0`

对话链路:

1. Agent 先用只读工具读了 `list_objectives`、`list_triggers`、`focus.md`。
2. Agent 识别出 4 个真正应该先问用户的问题：范围、周期、重点赛道、数据深度。
3. Agent 尝试调用 `send_channel_message` 澄清。
4. Runtime 返回 `plan_mode_readonly_violation`。
5. Agent 被迫把问题转成 `assumptions/open_questions`，然后调用 `exit_plan_mode`。
6. 最终 PlanCard 出现"（执行阶段，本步不实施）"、`wake_policy=none`、`estimated_cost=unknown` 等字段。

这证明问题不是"没有进 Plan Mode"，而是 Plan Mode 的交互和输出契约把 agent 推向了结构化填表。

**关于 `author_type="workflow"` 的澄清（源码核实）**：这是个**误导性标签**，不代表 plan 被 workflow 代写。`plan_mode_service.py:290-312` 注释明确——RPC planner（DefaultAgentPlanPlanner）已删，plan 实为 **agent 在主循环 `exit_plan_mode` authored**，`generate_plan` 只做 schema 落地/校验/hash；`author_type="workflow"` 是这个落地路径留下的烂标签。所以 P0 根因是 **exit_plan_mode 的 13 字段 schema 诱导 agent 填表**，不是"系统代写"；provenance 标签应改为 `agent_authored`（见 §7）。

## 2. Claude Code 对照

Claude Code 的 Plan Mode 有三类机制值得借鉴:

| 机制 | CC 形态 | Hive 应继承什么 |
|---|---|---|
| 主循环规划 | agent 在同一对话/工具循环里探索和规划 | planning 必须由 agent 的主循环智能完成 |
| 澄清问题 | `AskUserQuestionTool` 可在 plan mode 中问用户 | Hive 需要 first-class `ask_user_question`，不能靠 blocked messaging tool |
| 审批出口 | `ExitPlanMode` 发起审批，不负责思考本身 | `exit_plan_mode` 只提交已经形成的计划和治理摘要 |
| Plan 主体 | plan 写在文件/markdown 中 | Hive 第一屏应以 `plan_markdown` 为主体 |
| 反模式 | 不用 AskUserQuestion 问"这个计划行不行" | approval 仍只能走 `exit_plan_mode` |

Claude Code 的 coding-only 限制只说明它自己的任务域，不是 Hive 的触发标准。Hive 的 Plan Mode 是 domain-neutral planning and authorization phase。

## 3. 当前偏差

### P0: 计划不像计划，而像结构化拼接

当前 `exit_plan_mode` 要求 agent 一次性提供:

```text
title / objective / plan_markdown / steps / success_criteria /
stop_conditions / assumptions / open_questions / risk_assessment /
estimated_cost / wake_policy / handoff_target / handoff_payload
```

这会把模型注意力从"想清楚怎么做"转移到"把字段填完整"。结果是:

- `plan_markdown` 沦为字段之一，而不是计划主体。
- `steps` 混入 Plan Mode 自身的元步骤，例如"提交 Plan Mode 卡片"。
- `open_questions` 变成免责清单，而不是真正暂停执行的问题。
- `estimated_cost=unknown`、`wake_policy=none` 这种系统空值被展示成用户内容。

目标改法:

- Agent 首先写一篇用户可读的 `plan_markdown`。
- 结构化字段是系统治理层从计划中抽取/校验/补全的结果，不是 agent 的主要写作目标。
- `steps` 只能描述用户确认后真正要执行的步骤。
- 如果存在 blocking open question，不能进入 `awaiting_confirmation`。

### P1: 缺少 first-class ask-user 工具

Plan Mode 需要一个明确的交互出口:

```text
ask_user_question
```

它不是普通外发消息，也不是 approval。它的语义是:

- 只向当前用户/当前会话提问。
- 不产生外部副作用。
- 可在 Plan Mode read-only 状态下调用。
- 调用后 PlanRequest/RuntimeTask 进入 `awaiting_user_clarification`。
- 用户回答后恢复同一个 Plan Mode state，继续规划。

限制:

- 不允许用它问"这个计划可以吗"。
- 不允许绕过 `exit_plan_mode` 进行审批。
- 不允许把问题发给外部联系人或其他 channel recipient。

### P2: Prompt 不能再往 coding 方向引导

Hive 是综合 co-work 平台，不是 coding-only assistant。Plan Mode prompt 应避免这些倾向:

- 不把 implementation 默认理解成代码实现。
- 不把 tests/CI/deploy 当成每类任务都必须有的计划结构。
- 不把 research/content 类任务降级为"不该 Plan Mode"。
- 不用 repository/files/code 作为默认语境。

通用 Plan Mode 应覆盖:

- 调研报告、周报、财务分析、运营活动、客户沟通、招聘流程、销售计划、数据分析、文档生产、代码修改、自动化触发器、agent 委派等。

触发标准应是:

- 用户显式要求先计划。
- 工作多步骤、长耗时、多来源或高成本。
- 输出格式、受众、范围、数据源、频率、交付方式存在关键不确定性。
- 会产生外部可见动作、长期自动化、文件交付、委派、预算消耗或不可逆/敏感动作。
- 需要用户确认边界后才能安全执行。

### P3: PlanCard 暴露 plumbing

`wake_policy`、`handoff_target`、`handoff_payload`、`risk_assessment`、`estimated_cost` 是 Hive 控制中台的合理治理数据，但不应平铺成第一屏内容。

PlanCard 第一屏应该是:

1. 标题。
2. `plan_markdown` 主体。
3. 用户需要确认/回答的关键事项。
4. 主要动作: 确认、要求修改、拒绝、回答问题。

高级/审计区域才显示:

- 风险等级。
- 外部副作用。
- 成本/时间估计。
- stop conditions。
- wake policy。
- handoff target。
- plan hash / version。

空值不展示:

- `none`
- `unknown`
- empty arrays
- empty payloads

### P4: Work Ledger 缺席导致过程不可见

生产实跑里 session work-ledger API 返回 404，DB 也没有 matching ledger。结果是用户只能看到最终 PlanCard，看不到 agent 如何拆解、验证、取舍。

Plan Mode 需要 private planning ledger，但它不能替代 PlanCard:

- Work Ledger = agent 的私有规划/执行认知状态。
- PlanCard = 用户确认的治理合同。

Plan Mode 期间至少应允许:

```text
track_todo
record_finding
read_ledger
```

这些工具仍是 read-only planning aids，不启动执行。

## 4. 目标语义

Plan Mode 是 Hive 的 domain-neutral co-work planning phase:

```text
User asks for work that needs planning/authorization
  -> Plan Mode active
  -> agent explores read-only context
  -> agent asks blocking questions if needed
  -> agent writes substantive plan_markdown
  -> runtime extracts/checks governance fields
  -> exit_plan_mode submits confirmable plan
  -> user confirms exact version/hash
  -> only then handoff/execution starts
```

关键不变量:

- Agent 负责 substantive plan。
- Runtime 负责 classification、read-only constraints、persistence、hash、confirmation、audit、handoff。
- 用户确认的是计划边界，不是系统字段表。
- 有 blocking question 时不允许确认。
- Plan Mode 不按 coding/research 分类；按规划和授权需求分类。

## 5. 新 prompt 草案

这段应替换当前 Plan Mode reminder/launcher 中偏 coding 或偏填表的语义:

```text
Plan Mode is active. The user has not approved execution.

Do not perform the requested work yet. Do not create or enable automations,
write deliverable files, send external messages, delegate work, modify memory,
or trigger long-running execution. Use only read-only context tools and planning
tools.

Your job is to produce a useful, domain-appropriate work plan for the requested
outcome. This may be coding, research, writing, analysis, operations, sales,
finance, recruiting, customer communication, automation, or any other co-work
task. Do not assume the task is a software implementation unless the user's
request actually says so.

First understand the user's real outcome, constraints, audience, delivery
format, risks, cost, timing, and external side effects. Inspect current state
only when it matters for the plan.

If a missing decision materially changes scope, risk, cost, recipient, cadence,
data source, deliverable format, or irreversible behavior, ask the user a brief
clarifying question with ask_user_question. Do not submit a confirmable plan
while a blocking question is unresolved.

When the plan is ready, write plan_markdown as the main user-facing plan. It
should explain the approach, sequencing, tradeoffs, verification, stopping
conditions, and what will happen after approval in natural user language.

Then call exit_plan_mode. exit_plan_mode is the approval request. Do not use
ask_user_question or prose to ask "is this plan OK?"
```

## 6. `ask_user_question` contract

Minimal schema:

```json
{
  "question": "string",
  "reason": "string",
  "options": [
    {"label": "string", "description": "string"}
  ],
  "allow_free_text": true,
  "blocking": true
}
```

Runtime behavior:

- Allowed in Plan Mode read-only policy.
- Persists the question in plan/session metadata.
- Emits a chat-visible question card.
- Pauses the current Plan Mode run.
- Stores the user answer.
- Resumes Plan Mode with the answer injected as confirmed user input.

Non-goals:

- It is not a general external messaging tool.
- It is not an approval tool.
- It does not confirm a plan.

## 7. `exit_plan_mode` contract changes

Keep `exit_plan_mode` as the only approval exit, but change its expectations:

Required:

- `title`
- `plan_markdown`
- `execution_summary` or equivalent short summary

Derived or secondary:

- `steps`
- `success_criteria`
- `stop_conditions`
- `risk_assessment`
- `estimated_cost`
- `wake_policy`
- `handoff`
- `assumptions`

Validation rules:

- Reject if `plan_markdown` is missing or too thin.
- Reject if `steps` include Plan Mode meta-work such as "submit plan card", "call exit_plan_mode", "in Plan Mode inspect context".
- Reject or pause if blocking `open_questions` exist.
- Do not render empty governance fields.
- Metadata should mark real provenance as `agent_main_loop` / `agent_authored`, not `workflow`, when the plan came from the main agent loop.

## 8. Implementation Order

### Phase A: Document + prompt correction

- Rewrite this document and related Plan Mode docs around domain-neutral co-work.
- Remove coding-only framing from Plan Mode prompt.
- Make "blocking question first, plan after" explicit.

### Phase B: Ask-user tool

- Add `ask_user_question`.
- Add Plan Mode allowlist entry.
- Add state transition for `awaiting_user_clarification`.
- Resume Plan Mode from user answer.

### Phase C: Plan quality contract

- Make `plan_markdown` the primary plan artifact.
- Add `exit_plan_mode` validation for meta-steps and blocking questions.
- Correct provenance metadata.
- Allow planning ledger tools during Plan Mode.

### Phase D: PlanCard surface

- Render `plan_markdown` first.
- Hide empty plumbing fields.
- Fold governance/audit fields into advanced details.
- Show blocking questions as questions, not as confirmable assumptions.

## 9. Acceptance Criteria

- RWA 周报这类非 coding 任务可以进入 Plan Mode，但会先澄清关键范围/交付/数据深度问题，或产出真正可执行的研究计划。
- Plan 第一屏读起来像 agent 写给用户的工作方案，而不是 schema 表。
- `open_questions` 不再是免责清单；blocking questions 会暂停确认。
- Plan Mode prompt 不默认 coding，不要求所有任务都出现 tests/CI/deploy。
- `exit_plan_mode` 不再接受包含 Plan Mode 元步骤的 execution steps。
- PlanCard 不展示 `none/unknown` 空 plumbing。
- 用户确认后，执行者能直接从计划理解要做什么、为什么、怎么验收、何时停止。

## 10. North Star

Plan Mode 的价值不是"先弹一张确认卡"，而是:

> agent 在执行前认真理解、澄清、取舍、规划；系统只负责把这份计划变成可确认、可审计、可治理的执行边界。

Hive 相对 Claude Code 的 superset 不是更复杂的表单，而是更通用的 co-work runtime 加企业级治理。Plan Mode 必须服务这个定位。

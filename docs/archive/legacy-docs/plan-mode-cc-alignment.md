# Plan Mode CC/Codex Alignment — 一次性闭环方案

> 状态: **v1.0 文档优先闭环方案（2026-06-08）— 待实现**。
>
> 触发: Web3 研究员生产对话暴露两个同源问题：
>
> 1. Plan 看起来像字段堆砌，不像 agent 真正规划后的方案。
> 2. 用户确认后没有在当前对话继续执行，反而走到 `long_task` handoff；生产中该 target 没有 handler，最终 `handoff_status=skipped`，agent 又错误地说还没确认。
>
> 本文目标: 一次性修掉 Plan Mode 的**规划质量、确认语义、执行续跑、UI 状态、异步边界、测试验收**。不能只补 prompt，不能只注册一个 `long_task` handler，也不能只修 PlanCard。

---

## 0. 总结论

Hive 要对齐的是 Claude Code / Codex 的 **Plan Mode 运行语义**，不是它们的 coding-only 产品范围。

一次性闭环后的核心语义：

```text
当前会话中触发 Plan Mode
  -> agent 在当前会话主循环里只读探索、澄清、形成观点
  -> agent 输出以 plan_markdown 为主体的可确认计划
  -> 用户确认 exact plan_version + plan_hash
  -> 默认在同一个 chat session 继续执行并流式输出结果
  -> 只有用户明确要求后台/离线/定时/委派/无人值守时，才进入 detached handoff
```

关键判断：

- Plan Mode 不是表单生成器。runtime 可以持久化、hash、审计、拦截和确认，但 substantive plan 必须是 agent-authored。
- `agent_plan_requests` 仍是治理账本；PlanCard 是确认界面；Work Ledger 是 agent 执行认知状态。三者不能混用。
- Web Chat 本身已经是 durable async run，所以“异步能力”不等于“后台 detached task”。默认应保持 current-session continuation。
- `long_task` 不能再作为 live chat Plan Mode 的默认 target。它要么被显式重命名为 `detached_runtime_task`，要么只作为 backward-compatible alias，且必须有 handler 和用户可见 task id。

---

## 1. 源码基线：Claude Code / Codex 真实语义

### 1.1 Claude Code

源码位置：`/Users/example-owner/Context Engineering/claude-code-org`

| 事实 | 源码锚点 | 结论 |
|---|---|---|
| `/plan` 把当前 session 权限切到 `plan` | `src/commands/plan/plan.tsx:72-82` | Plan Mode 是当前会话的 runtime mode，不是后台任务 |
| EnterPlanMode 要求探索、理解架构、比较方案、必要时问用户 | `src/tools/EnterPlanModeTool/prompt.ts:4-12`, `:108-124` | 计划质量来自 agent 主循环判断，不是 schema 拼装 |
| ExitPlanMode 是用户确认出口 | `src/tools/ExitPlanModeTool/ExitPlanModeV2Tool.ts:147-193` | 审批出口和规划能力分离 |
| 用户批准后，tool result 明确告诉模型可以开始执行 | `src/tools/ExitPlanModeTool/ExitPlanModeV2Tool.ts:481-489` | 默认语义是批准后继续执行 |
| clear-context/fresh query 是选项，不是默认 | `src/components/permissions/ExitPlanModePermissionRequest/ExitPlanModePermissionRequest.tsx:332-395`, `:425-452` | detached/fresh context 是可选执行形态 |

Claude Code 的 coding-only 限制不适合照搬到 Hive。Hive 是 co-work/control-plane 平台，Plan Mode 应覆盖研究、报告、运营、销售、财务、自动化、委派和代码任务。

### 1.2 Codex

源码位置：`/Users/example-owner/Context Engineering/codex/codex-rs`

| 事实 | 源码锚点 | 结论 |
|---|---|---|
| Plan Mode 是 conversational collaboration mode | `collaboration-mode-templates/templates/plan.md:1-15` | 规划应在对话中逐步形成 |
| Plan 必须 decision complete | `collaboration-mode-templates/templates/plan.md:1-4`, `:92-128` | 计划要让执行者无需再做关键决策 |
| Plan Mode 允许非突变探索、禁止执行性突变 | `collaboration-mode-templates/templates/plan.md:17-40` | read-only 是副作用边界，不是思考能力降级 |
| `request_user_input` 是 Plan Mode 澄清工具 | `core/src/tools/handlers/request_user_input.rs:54-75` | 澄清是 Plan Mode 一等能力 |
| `update_plan` 是执行 TODO 工具，Plan Mode 中反而禁用 | `core/src/tools/handlers/plan.rs:79-82` | TODO/checklist 不等于 Plan Mode plan |
| 确认后默认切回 Default 并执行；fresh context 是可选项 | `tui/src/chatwidget/plan_implementation.rs:9-18`, `:33-64`, `:77-104` | 默认 continuation，非默认 detached |

Codex 对 Hive 的启示更直接：`<proposed_plan>` 是模型输出的正式计划，UI 特殊渲染它；`update_plan` 只是后续执行中的进度清单。

---

## 2. Hive 当前断点（源码核实）

### 2.1 计划质量断点

当前 `exit_plan_mode` 要求 agent 一次性填：

```text
title / objective / plan_markdown / steps / success_criteria /
stop_conditions / assumptions / open_questions / risk_assessment /
estimated_cost / wake_policy / handoff_target / handoff_payload
```

源码：`backend/app/tools/handlers/plan_mode.py:119-148`, `:178-195`。

这会诱导模型把注意力放在“字段完整”而不是“方案有观点”。结果就是：

- `plan_markdown` 沦为字段之一，而不是计划主体。
- `steps` 混入 Plan Mode 自身元步骤，例如“提交计划卡片”。
- `open_questions` 变成免责清单，而不是阻断确认的澄清问题。
- `estimated_cost=unknown`、`wake_policy=none` 这类 plumbing 被展示成计划内容。

### 2.2 执行语义断点

当前 live Web Chat 的 Plan Mode 默认 target：

```python
elif decision.action_kind == "create_enabled_trigger":
    handoff_target = "objective_trigger"
else:
    handoff_target = "long_task"
```

源码：`backend/app/services/web_chat_runtime.py:396-402`。

但 handoff registry 只注册：

```python
objective_trigger
delegation
```

源码：`backend/app/services/plan_mode_registry.py:22-26`。

没有 handler 时，`PlanModeService` 会写：

```json
{"reason": "no_handler_registered", "target": "long_task"}
```

源码：`backend/app/services/plan_mode_service.py:713-728`。

所以生产里的真实情况不是“已经后台跑了但用户看不到”，而是：

```text
用户确认成功
  -> handoff target = long_task
  -> no handler
  -> handoff_status = skipped
  -> 没有 RuntimeTask / task_id
  -> agent 无法开始，还错误地说用户没确认
```

### 2.3 UI 状态断点

`AgentChatSection` 有两种 PlanCard：

- `InlinePlanCard` 会通过 `planApi.get(agentId, planId)` 拉真实 plan，并 10 秒 refetch。源码：`frontend/src/pages/agent-detail/AgentChatSection.tsx:120-146`。
- `plan_needs_confirmation` 工具结果路径会合成一个 `PlanRequest`，硬编码 `status: 'awaiting_confirmation'`。源码：`frontend/src/pages/agent-detail/AgentChatSection.tsx:226-258`。

这会造成用户已确认后，旧卡片仍显示确认按钮；再次点击就会命中后端正确的 409：

```text
plan in status 'confirmed' cannot be confirmed
```

源码：`backend/app/services/plan_mode_core.py:913-960`。

---

## 3. 目标语义：Plan Mode 状态机

### 3.1 统一状态机

```text
planning
  -> awaiting_user_clarification  # 有 blocking question
  -> planning                     # 用户回答后恢复
  -> awaiting_confirmation        # agent 提交 decision-complete plan
  -> confirmed                    # 用户确认 exact version/hash
  -> executing_current_session    # 默认 live chat continuation
  -> completed | failed | cancelled
```

Detached / autonomous 分支：

```text
confirmed
  -> handed_off_objective_trigger
  -> handed_off_delegation
  -> handed_off_detached_runtime_task
```

不变量：

- `awaiting_confirmation` 前不能有 unresolved blocking questions。
- `confirmed` 不代表执行已经开始；执行开始必须体现在 `handoff_status` 或 session run 状态。
- live chat 默认 `executing_current_session`，不是 detached `long_task`。

### 3.2 执行 target 枚举

废弃当前模糊的 `long_task` 默认语义，改为清晰枚举：

| target | 何时使用 | 用户体验 |
|---|---|---|
| `continue_current_session` | live web chat / 当前对话里确认的普通工作、报告、研究、代码任务 | 当前对话继续流式执行并给结果 |
| `objective_trigger` | 确认后要创建/启用 objective、trigger、schedule | 当前会话显示已创建对象和后续唤醒策略 |
| `delegation` | 确认后把工作交给另一个 agent | 当前会话显示 delegation id、接收方、回收方式 |
| `detached_runtime_task` | 用户明确要求后台跑、离线跑、跑完通知我，或无人值守确认后执行 | 必须返回 task id、ledger、取消入口 |

兼容策略：

- 读旧 plan 时如果 `handoff.target == "long_task"`：
  - 有 `session_id` 且 source 是 live chat：视为 `continue_current_session`。
  - 没有 live session 或用户明确后台：视为 `detached_runtime_task`。
  - 无法判定：fail closed，显示“需要选择执行方式”，不 silent skipped。
- 新 plan 不再写 `long_task`。

---

## 4. 一次性闭环改造方案

这不是分阶段上线的“先修一点”。可以按内部切口开发，但必须作为一个闭环 release 验收，不允许只发其中一半。

### 4.1 Backend：Plan 质量 contract

修改面：

- `backend/app/services/plan_mode_system_run.py`
- `backend/app/kernel/reminder_scheduler.py`
- `backend/app/tools/handlers/plan_mode.py`
- `backend/app/services/plan_mode_core.py`

要求：

1. Plan Mode prompt/domain 彻底 domain-neutral：
   - 不默认 coding。
   - 不默认 tests/CI/deploy。
   - 研究、报告、运营、财务、销售、委派、自动化都按同一 Plan Mode 原则处理。

2. `plan_markdown` 是计划主体：
   - `exit_plan_mode` 仍可收 structured fields，但 prompt 明确先写 plan。
   - `steps` 只能是确认后真正执行的步骤。
   - 禁止“调用 exit_plan_mode / 提交计划卡片 / 等待确认”这类 meta-step。

3. blocking question 不能进入确认：
   - 新增或复用 `ask_user_question`/`request_user_input` 语义。
   - `open_questions` 分成 `blocking_questions` 和 `non_blocking_assumptions`。
   - 有 blocking question 时 PlanRequest 进入 `awaiting_user_clarification`，不显示确认按钮。

4. provenance 修正：
   - main-loop authored plan 标记 `author_type="agent_main_loop"` 或 `agent_authored`。
   - 不再用误导性的 `author_type="workflow"` 表示 agent 写的 plan。

### 4.2 Backend：current-session continuation handoff

新增或改造：

- `backend/app/services/plan_mode_registry.py`
- `backend/app/services/plan_mode_service.py`
- `backend/app/services/web_chat_runtime.py`
- 建议新增：`backend/app/services/plan_mode_session_handoff.py`

设计：

```text
confirm plan
  -> handoff_confirmed_plan(plan_id)
  -> target = continue_current_session
  -> append a session-scoped user/system message:
       "Implement the approved plan <plan_id>..."
  -> call/start the existing durable web_chat_turn runtime
  -> stream chunks/tool calls/done events to same session
  -> set handoff_status=completed only after continuation run is created
  -> handoff_payload.runtime_task_id = <web_chat_turn task id>
```

实现注意：

- 复用 `start_web_chat_run(...)` 的 durable `RuntimeTask(task_type="web_chat_turn")` 机制；源码入口：`backend/app/services/web_chat_runtime.py:161-238`。
- 不能把 continuation 写成普通用户自由输入，必须带 confirmed plan metadata：
  - `approved_plan_id`
  - `approved_plan_version`
  - `approved_plan_hash`
  - `approved_plan_markdown`
  - `source="plan_mode_handoff"`
- 如果当前 session 已有 active run：
  - 不丢任务。
  - 返回 `handoff_status="queued"` 或 fail with typed `active_run_exists`，前端显示“当前有运行中任务，计划已确认，待排队执行”。
  - 不得显示“还没确认”。
- 如果没有 `session_id`：
  - 不能用 `continue_current_session`。
  - 转入 `needs_execution_target` 或 fallback 到 `detached_runtime_task`，但必须用户可见。

### 4.3 Backend：detached runtime task 只作为显式后台

修改面：

- `backend/app/services/web_chat_runtime.py`
- `backend/app/services/plan_mode_core.py`
- `backend/app/services/plan_mode_registry.py`
- `backend/app/services/long_task_runtime.py`

要求：

- 用户说“后台跑、离线跑、跑完通知我、我先关闭页面也继续”等，才写 `handoff.target="detached_runtime_task"`。
- 该 target 必须有 handler，创建 `RuntimeTask` 并初始化 Work Ledger。
- handler 必须返回：
  - `runtime_task_id`
  - `work_ledger_path` 或 API ref
  - cancellation endpoint/ref
  - delivery channel / completion notification policy
- 旧 `long_task` target 只能作为兼容 alias，不能作为新 plan 默认值。



```text
  -> same session emits progress/status
  -> result artifact appears in same chat
  -> if delivery file is produced, same chat reports attachment/ref
```


### 4.5 Frontend：PlanCard 全部读真实状态

修改面：

- `frontend/src/pages/agent-detail/AgentChatSection.tsx`
- `frontend/src/pages/agent-detail/PlanCard.tsx`
- `frontend/src/api/domains/plans.ts`

要求：

- 删除或降级 synthetic `PlanRequest`。
- `toolMeta.kind === 'plan_needs_confirmation'` 时优先用 `planId` 渲染 `InlinePlanCard`，通过 API 拉真实状态。
- `PlanCard` 根据真实状态显示：
  - `awaiting_confirmation`: 确认 / 请求修改 / 拒绝
  - `confirmed + handoff_status=null`: 已确认，准备启动
  - `confirmed + handoff_status=queued`: 已确认，等待当前 run 完成
  - `confirmed + handoff_status=completed`: 已开始，显示 runtime task / continuation status
  - `confirmed + handoff_status=skipped`: 红色错误，不再显示确认按钮
  - `awaiting_user_clarification`: 显示问题输入，不显示确认按钮
- 空 plumbing 不展示：
  - `none`
  - `unknown`
  - empty array/object
  - empty handoff payload

### 4.6 Agent 回答修正：确认后必须查 ledger

当用户问“现在开始了吗？”时，agent 不能凭上下文猜。

必须读取：

- latest plan by session / plan_id
- `status`
- `handoff_status`
- `handoff_payload`
- active session run

回答规则：

| 状态 | 回答 |
|---|---|
| `awaiting_confirmation` | 还没确认，给确认入口 |
| `confirmed` + no handoff | 已确认，正在准备启动 / 可重试 handoff |
| `confirmed` + `skipped` | 已确认，但启动失败，说明 reason |
| `confirmed` + `queued` | 已确认，排队等待当前 run 完成 |
| `confirmed` + `completed` + runtime id | 已开始，给当前进度 |
| active web chat run exists | 正在当前对话执行 |

---

## 5. 所有场景覆盖矩阵

| 场景 | Plan 生成 | 默认执行 | 不允许的行为 |
|---|---|---|---|
| 用户在 Web Chat 显式“进入计划模式”做报告/研究/代码 | main-loop Plan Mode | `continue_current_session` | 默认 `long_task` detached |
| 用户在 Web Chat 要“后台跑完通知我” | main-loop Plan Mode | `detached_runtime_task` | 无 task id / 无取消入口 |
| 用户创建定时/监控 | recommend -> accepted -> Plan Mode | `objective_trigger` | 不经确认直接启用 |
| 用户拒绝定时/监控 Plan Mode 推荐 | trusted opt-out | 可继续低风险 schedule | agent 自填 opt-out |
| 用户委派给另一个 agent | Plan Mode hard gate | `delegation` | 当前 agent 直接移交执行权 |
| Feishu/IM 有人在场 | channel session Plan Mode | channel-bound continuation / confirmation | regex 直接造 plan |
| Trigger/heartbeat 无人值守命中 gate | system plan run -> awaiting_confirmation | 用户确认后按 target 执行 | 未确认自动执行 |
| REST/API legacy create plan | 创建 draft 或 system plan run | 等确认后执行 | API 直接写 confirmed plan |
| 用户重复点确认 | 后端 409；前端隐藏按钮 | 显示真实状态 | 旧 synthetic 卡片继续确认 |
| 页面断开 | web_chat_turn durable run 继续 | 回来看到同 session 状态 | detached 语义混入普通执行 |

---

## 6. 测试计划（实现必须先红测）

文档改动本身不需要 TDD；后续代码实现必须按下面红测先行。

### 6.1 Backend tests

新增/修改建议：

- `backend/tests/services/test_plan_mode_session_handoff.py`
- `backend/tests/services/test_web_chat_runtime.py`
- `backend/tests/services/test_plan_mode_service.py`
- `backend/tests/tools/test_exit_plan_mode_tool.py`
- `backend/tests/api/test_plan_mode_api.py`

必测：

1. live web chat explicit Plan Mode 默认 target 是 `continue_current_session`，不是 `long_task`。
2. confirmed `continue_current_session` plan 会创建/排队同 session `web_chat_turn` run。
3. created run metadata 带 `approved_plan_id/version/hash`。
4. active run 存在时不 silent fail；返回 queued 或 typed conflict。
5. `long_task` legacy target 有兼容处理，不再 `no_handler_registered`。
6. blocking question 存在时不能进入 `awaiting_confirmation`。
7. `exit_plan_mode` 拒绝 meta steps。
8. `author_type` 对 main-loop authored plan 不再是 `workflow`。
10. no session id + `continue_current_session` 必须 fail closed。

命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_plan_mode_session_handoff.py \
  tests/services/test_web_chat_runtime.py \
  tests/services/test_plan_mode_service.py \
  tests/tools/test_exit_plan_mode_tool.py \
  tests/api/test_plan_mode_api.py -q
```

### 6.2 Frontend tests

新增/修改建议：

- `frontend/src/pages/agent-detail/AgentChatSection.test.tsx`
- `frontend/src/pages/agent-detail/PlanCard.test.tsx`
- `frontend/src/api/domains/plans.test.ts`

必测：

1. `plan_needs_confirmation` 使用 `InlinePlanCard` / API 真实 plan，不再硬编码 awaiting。
2. confirmed plan 不显示确认按钮。
3. skipped handoff 显示错误和 reason。
4. queued/current-session execution 显示“已确认，等待/执行中”。
5. awaiting clarification 显示问题输入，不显示确认。
6. `none/unknown/empty` plumbing 不渲染。
7. i18n en/zh 都有新增 copy。

命令：

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm run test -- AgentChatSection.test.tsx PlanCard.test.tsx plans.test.ts
npm run build
```

### 6.3 Integration / production acceptance

必须覆盖两个真实回归用例：

1. **DeFi 新玩法报告**
   - 用户：`进入计划模式，做一个关于 DeFi 新玩法的报告`
   - 期望：计划有观点、范围、信息源策略、章节结构、验证/交付方式。
   - 点击确认后：同一 chat session 开始执行，能看到进度和最终报告/附件。
   - 不允许：`handoff_status=skipped`、`target=long_task no_handler_registered`、agent 说“还没确认”。

2. **RWA 周报**
   - 用户：`我需要做一个 RWA 的周报，进入计划模式计划一下`
   - 期望：若周期/受众/深度不清，先问 blocking question；如果信息足够，则给出可执行计划。
   - 不允许：把问题塞进 `open_questions` 后仍显示确认按钮。

生产查询验收：

```sql
select id, status, handoff_status, handoff_payload, plan_json->'handoff' as handoff, metadata_json
from agent_plan_requests
where agent_id = '<agent_id>'
order by created_at desc
limit 5;
```

通过条件：

- live chat 普通任务新 plan 不再写 `handoff.target = "long_task"`。
- confirmed 后能看到 `handoff_status in ('completed','queued')`，且 payload 有 session/run id。
- UI 不再允许重复确认已 confirmed plan。

---

## 7. 非目标和保留边界

不改：

- `plan_hash` / `plan_version` 绑定。
- 真实用户确认 requirement。
- `PlanModeGate` fail-closed。
- ActionPreflight / capability approval / Memory Control Plane。
- PlanCard 的确认/拒绝/请求修改基本治理动作。

不做：

- 不把所有普通对话都强制 Plan Mode。
- 不把 schedule/monitor recommendation 改成一律 hard gate。
- 不把 Work Ledger 当成用户确认 plan。
- 不用 prompt-only 修复替代运行时状态和 handoff 修复。

---

## 8. 一次性发布门槛

本次修复必须作为一个闭环 release 验收。任一条不满足，不算完成：

1. Plan 读起来像 agent-authored work plan，而不是字段表。
2. blocking question 真的阻断确认。
3. live chat 确认后默认 current-session continuation。
4. `long_task` 不再是新 plan 默认 target。
5. 所有 target 都有 handler 或显式 fail-closed UX。
6. PlanCard 全部基于真实 plan 状态，不再 synthetic stale confirmation。
7. 用户问“开始了吗”时 agent 能从 ledger/run 状态回答。
8. backend + frontend targeted tests 通过。
9. Railway production 验收两个真实用例通过。

---

## 9. North Star

Plan Mode 的价值不是“多一步确认”，而是：

> agent 在执行前认真理解、澄清、取舍、规划；用户确认的是稳定边界；runtime 把这份边界变成可恢复、可审计、可治理、可继续执行的工作流。

Hive 相对 Claude Code / Codex 的 superset，不是更复杂的表单，也不是更早把任务丢后台，而是：

```text
current-session agent planning quality
  + enterprise-grade confirmation/audit
  + durable async runtime
  + explicit detached execution only when it is truly needed
```

---

## 10. Review 修正（实现采用 — 2026-06-08）

主负责人 review 了本方案，独立核实了所有承重断言（`web_chat_runtime.py:402` 默认 long_task ✅、`plan_mode_registry.py:24-26` 缺 handler ✅、`plan_mode_service.py:717-728` skipped ✅、`AgentChatSection.tsx:237` 合成卡片写死 awaiting ✅），并验证了最高风险假设：`start_web_chat_run`（`web_chat_runtime.py:161-238`）只需 `db/agent/user/session/content`、与 WS 解耦（靠 `broadcast_web_chat_event` 推流）、已自带 `ActiveWebChatRunExists` 守卫 → **`continue_current_session` 续跑机制可行、低风险**。方案批准，落地采用以下 4 处修正（覆盖 §3 / §4 对应条目）：

1. **不新增 plan 状态。** 不引入 `executing_current_session` / `awaiting_user_clarification` 两个 §3.1 状态。执行态用现成的 `confirmed + handoff_status(queued|completed) + handoff_payload.runtime_task_id` 表示（§4.6 回答表本就这么判）。blocking question 不靠新状态：live chat 澄清发生在 plan row 存在之前（`ask_user_question` 是 pre-plan、结束本轮的循环），所以靠 ① Phase A reminder 引导 blocking→`ask_user_question`，② `exit_plan_mode` 拒收空 `plan_markdown` / meta-step。避免动 `_TRANSITIONS`、前端 `PlanStatus` union、i18n、所有 status switch。

2. **`detached_runtime_task` 先上 fail-closed 桩，不建完整基建。** 两个验收用例（DeFi 报告 / RWA 周报）都是 `continue_current_session`，detached **不在 §6.3 验收门内**。满足 §8.5「所有 target 有 handler 或显式 fail-closed」只需注册一个桩 handler（明确返回「后台执行暂未开放，将在当前会话执行」）。RuntimeTask+Work Ledger+取消端点+通知策略整套基建，等用户真要「后台跑完通知我」时做快速跟进，不绑进本次闭环。

3. **provenance 用现成的 `"agent"`。** §4.1.4 提的 `agent_main_loop`/`agent_authored` 不引入第三种术语；`plan_mode_service.py:423` 现成默认就是 `"agent"`，改 `:338` 的 `"workflow"`→`"agent"` 即可。

4. **续跑把计划注入 prompt，不只塞 metadata。** `start_web_chat_run` 的 `content` 硬编码 role=user，所以续跑时 `content=完整计划执行指令`（agent 的 marching orders，含 approved plan_markdown）、`display_content="✅ 计划已确认，开始执行"`（UI 干净）。metadata 仍带 `approved_plan_id/version/hash` 供审计。


落地序：§4.1 计划质量 contract（吸收先前 Phase A/B/D 的 plan_markdown 正文 + author_type + meta-step + ledger 工具）→ §4.2 `continue_current_session` handler → §4.5 前端 `InlinePlanCard` 取代合成卡片 → §6.3 两个验收用例。

### 10.1 Review 后闭环补丁（2026-06-08）

二次 review 发现两个残余缺口：Web 卡片仍是浏览器端 `confirm` 后再调 `handoff` 两段 HTTP，且 `handoff_status="queued"` 只是展示状态、没有恢复执行机制。最终补丁采用：

1. **Web 端确认走后端单接口。** 新增 `POST /agents/{agent_id}/plans/{plan_id}/confirm-and-handoff`，服务层提供 `PlanModeService.confirm_and_handoff_plan()`，Web `PlanCard` 的确认按钮只调这一条接口；旧 `confirm` / `handoff` 保留给 Feishu 等需要分层语义的入口。
2. **`queued` 必须可恢复执行。** `continue_current_session` 遇到 active run 时仍返回 `queued`，但 `web_chat_runtime.execute_web_chat_run()` 在 terminal cleanup 中调用 queued resume hook：查找同 agent/session 最早的 `confirmed + handoff_status=queued` plan，再调用 `handoff_confirmed_plan()`。每次完成只恢复 1 条，下一条由续跑完成后继续拉起，避免并发挤爆当前 session。
3. **验收钉子。** 后端测试覆盖 service 单操作、REST 单接口、queued resume helper、run terminal cleanup hook；前端测试覆盖 API adapter 与 `confirmAndHandoffPlan()` 不再拆成两次请求。

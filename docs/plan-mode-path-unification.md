# Plan 路径统一 —— 消除 Path B,所有规划回归 agent 主循环

> 本文**修订** `docs/plan-mode-runtime-paradigm.md` Phase 5 的"RPC planner 降级为无人值守
> fallback"决定。该决定接受了两条 plan 生成路径(live 走主循环 / 无人值守走隔离 RPC)。
> 用户 2026-06-03 拍板硬约束:**绝不能有两个 plan 模式路径。** 故本文设计如何消除 Path B,
> 把所有 plan 生成收敛到唯一一条 agent 主循环范式。
>
> 治理层(`plan_mode_core.py` hash / `validate_confirmation` / `PlanModeGate` fail-closed)**一行不动。**

---

## 0. TL;DR

**核心命题(唯一不变量)**:

> **所有 plan 生成,无一例外,经 agent 主循环规划(plan mode:只读 policy + 每轮 reminder +
> `exit_plan_mode` 提交)。消除独立的 `DefaultAgentPlanPlanner`(隔离 RPC 填表)与 chat/IM 的
> regex/classification 代理路径。区别只剩"触发源"与"确认时机",不再是"两套实现"。**

收敛后只有一条范式,两种触发场景:

| | 有人在场(live chat / Feishu / 显式 plan mode) | 无人值守(trigger / heartbeat / API) |
|---|---|---|
| 规划在哪 | agent 主循环(实时流式可见) | agent 主循环(系统发起的 plan run,无实时用户流) |
| 确认时机 | 当场 / 异步 plan queue | 异步 plan queue(用户下次在场) |
| 实现 | **同一套 plan mode runtime** | **同一套 plan mode runtime** |

---

## 1. 背景与定位

### 1.1 硬约束 + 修订原决定

`plan-mode-runtime-paradigm.md` 的范式收敛(Phase 1-6,已上生产)把**有人在场的 live chat** 路径
收敛到了主循环(Path A,对标 CC)。但它的 Phase 5 **故意保留** RPC planner 作"无人值守 fallback"
(只把它从 8 轮放宽到 20 轮、去 token 钳制,没换形态)。所以两路径并存**不是遗漏,是设计选择**。

本文推翻这个选择。理由:CC superset 定位下(见 `project_hive_cc_superset`),**对标 CC 是先决条件**,
而 **CC 只有一条 plan 路径**——不存在"无人值守隔离 planner"这种东西。Hive 多出来的无人值守需求
(CC 没有)不该用"另起一套范式"满足,而应**用同一套范式的不同入口**满足。

### 1.2 一条贯穿原则

> 规划是 agent 的认知动作,必须由 agent 在主循环用完整能力完成(`feedback_plan_from_agent_system_governs`)。
> 治理/确认/审计是系统的职责,**包裹**规划而非**代替**规划。
> regex 猜意图 + RPC 代笔 = 系统代替了 agent 的认知 = 反模式之最。

---

## 2. CC 基线:只有一条 plan 路径

(证据:`/Users/rocky243/Context Engineering/claude-code-org`)

- CC 的 plan mode = `permissionMode='plan'`,agent 在**主 ReAct 循环**里只读探索 + 规划 →
  `ExitPlanMode` 工具 → 人类审批 → 批准后继续同一循环执行。
- **没有独立的 planner 子进程**:CC 不存在"隔离沙箱里一次性生成 plan_json"的东西。
- **没有 regex 意图代理**:CC 从不用关键词匹配绕过 agent 去造 plan;一切由 agent 在主循环判断。
- CC **有**无人值守自主(cron 公开;RemoteTrigger/DreamTask/Swarm feature-gated),且全部**共用同一条
  主循环**(`query()`/`queryLoop`;cron 唤醒 = prompt 入队进同一循环,无独立后台/plan 引擎)。但 CC 对
  无人值守高风险动作**不做"先规划+审批"**——直接执行,靠 permission mode + 提前授权(CLAUDE.md
  "authorized to push to main") + hooks 兜底。**所以"无人值守先规划+异步审批"是 Hive 主动加的治理
  delta(CC 没有)**:执行范式对标 CC 的主循环,但这层治理叠加无 CC 可照搬。补法是**扩展同一条主循环的
  入口 + 叠加异步审批**,不是新增一条隔离路径。

---

## 3. Hive 现状核实(2026-06-03 亲自读码)

### 3.1 分叉点

`plan_mode_service.generate_plan(use_agent_planner: bool = True)`(`:393`):

- `use_agent_planner=False`(`:465-475`):直接用 caller 的 `fill` 作 plan_json,不调 LLM = **Path A**
- `use_agent_planner=True`(默认,`:429`→`_run_planner`):`DefaultAgentPlanPlanner.plan()`
  (`agent_plan_planner.py:257` 隔离 `invoke_agent(source="plan_mode", channel="internal",
  max_tool_rounds=20, PLANNER_ALLOWED_TOOLS)` → 强制 JSON coerce)= **Path B**

### 3.2 Path B 全部触发源(分两类)

| # | 触发源 | 代码 | 类别 | 有用户? |
|---|---|---|---|---|
| 1 | web chat regex"创建任务"auto-sync | `web_chat_runtime.py:499` | **regex 代理** | 有(绕过主循环) |
| 2 | Feishu regex"创建任务"auto-sync | `feishu.py:55` | **regex 代理** | 有(绕过主循环) |
| 3 | Feishu plan_entry classification 入口 | `feishu.py:2291` | **classification 代理** | 有(绕过主循环) |
| 4 | tool 拦截(非 live / flag off) | `service.py:646 _attach_intercepted_plan` | 无人值守 / 降级 | 可能无 |
| 5 | REST API create / regenerate | `plans.py:327 / :404` | 外部直接造 | 看调用方 |
| 6 | supersede | `plan_mode_service.py:740` | plan 重生成 | - |

注:trigger/heartbeat daemon **不直接**调 PlanModeService(grep 证实);无人值守的 plan 是 agent 在
trigger/heartbeat run 里调 gated tool → #4 tool 拦截进 Path B。

### 3.3 两层分裂诊断

1. **路径分裂(A/B)**:同一个"生成 plan"有两套实现(主循环 fill / 隔离 RPC)。
2. **更深:regex/classification 代理(#1/#2/#3)**:chat/IM 渠道有用户在场,却用关键词匹配绕过 agent
   主循环,由系统直接造 plan。agent 没参与规划。**这是"系统代替 agent 认知"的最严重形态。**

---

## 4. 统一目标

**单一路径定义(可测)**:`DefaultAgentPlanPlanner` 不再被任何"有 plan_json 产出"的路径调用;
所有 awaiting plan 的 `plan_json` 都来自 agent 在 plan mode 主循环里 `exit_plan_mode` 提交的 fill
(`author_type` 恒为 agent-authored,不再有 `author_type="workflow"` 的填表产物或 RPC coerce 产物)。

---

## 5. 统一设计

### 5.1 原则

> 保留 `generate_plan(use_agent_planner=False)` 这**一条**落地通道(caller 提供 agent 已规划好的
> fill)。删除 `use_agent_planner=True` 这条分支及其 `DefaultAgentPlanPlanner`。所有触发源改为
> "进入 agent 主循环 plan mode → agent 规划 → `exit_plan_mode` → `ensure_awaiting_plan_from_fill`"。

### 5.2 层次一:拆 chat/IM 的 regex/classification 代理(#1/#2/#3)

这三处**有用户在场**,完全可以走主循环,不需要任何新机制:

- 删除 `web_chat_runtime.py:499` / `feishu.py:55` 的 regex auto-sync 旁路。
- 删除 `feishu.py:2291` 的 classification 直造 plan 旁路。
- 改为:这些渠道的用户消息正常进 agent 主循环;agent 若判断要创建任务/触发器,**自己调 `manage_tasks`/
  `set_trigger`** → tool gate(`service.py:646`)拦截 → 因为是 live chat,`defer_to_interactive` 为真
  → 翻转进 interactive plan mode(Path A)。
- **安全等价**:原 regex 路径的价值是"拦住'创建任务'文字 → 不静默后台执行"。统一后这个语义由
  **tool gate** 保证(agent 调 manage_tasks 必过 gate),不依赖 regex。
- **风险(§9 详述)**:regex 是"agent 没调工具、但回复文字里提了创建任务"的兜底。拆掉后若 agent 只
  "说"不"调工具",不会触发 plan。缓解:prompt 引导 + 真实意图本就该由 agent 调工具表达,而非文字。

### 5.3 层次二:无人值守 plan run(#4 的非 live 部分)

这是唯一需要**新机制**的地方,也是本文核心。

无人值守(trigger/heartbeat 经 #4 拦截)**没有实时用户消息流**驱动主循环。设计一个
**系统发起的 plan-mode agent run**:

```
gated tool 在无人值守 run 里被拦截
  → 不静态返回 needs_plan,也不调 DefaultAgentPlanPlanner
  → 启动一个 plan-mode agent run(复用同一 agent kernel):
      · plan mode 激活:PLAN_MODE_READONLY_TOOLS 只读 policy + 每轮 reminder(与 Path A 同一套)
      · 初始 prompt = "你刚才要执行 <被拦截的 action>,现在在 plan mode 下为它规划"
      · agent 在主循环探索/规划(完整能力,非 8/20 轮钳制的填表)
      · agent 调 exit_plan_mode 提交 fill
  → ensure_awaiting_plan_from_fill(use_agent_planner=False)
  → plan 落 awaiting_confirmation
  → 用户下次在场时在 plan queue 异步审批
```

**关键**:这个 run 用的是**和 Path A 完全相同**的 plan mode runtime(reminder + 只读 policy +
exit_plan_mode),只是**入口是系统触发、产出异步审批**。它取代 `DefaultAgentPlanPlanner` 的独立
prompt(`_planner_system_prompt`)、独立工具集(`PLANNER_ALLOWED_TOOLS`)、强制 JSON coerce。

### 5.4 REST API(#5)与 supersede(#6)

- `plans.py:327 create_plan`:核查调用方。若是前端"手动建 plan",改为引导用户进 chat 让 agent 规划
  (或前端 create 仅建 draft,实际 plan_json 由 §5.3 的无人值守 run 产出)。
- `regenerate`/`supersede`:改为复用 §5.3 的无人值守 plan run 重新规划,不调 RPC planner。

### 5.5 治理不变量(一行不动)

`plan_hash` / `plan_version` / `validate_confirmation` / `PlanModeGate` fail-closed / 禁 agent
自我确认 / handoff 三元组校验——全部保留。统一只改"plan_json 从哪来",不改"plan 如何被确认/执行"。

### 5.6 成本权衡(诚实标注)

无人值守启动完整 plan-mode 主循环 run(多轮探索)比现在的 RPC planner(一次性)**更贵**(token + 时延)。
这是 superset 的代价:换来单一范式 + 规划质量。可用 `max_tool_rounds` 上限 + 启用阈值(仅
"够复杂/有外部副作用"的无人值守动作才起 plan run)控制成本,与 work ledger 启用阈值一致。

---

## 6. 与已有文档边界

| 文档 | 关系 |
|---|---|
| `plan-mode-runtime-paradigm.md` | **本文修订其 Phase 5**(取消保留 Path B);其 Phase 1-4/6 范式收敛成果(主循环 + reminder + 只读 policy + exit_plan_mode)是本文复用的基础 |
| `plan-mode-design.md` | 治理框架/handoff 契约不动,引用 |
| `plan-mode-agent-authored-planning.md` | "计划由 agent 撰写"原则,本文是它的彻底化(连无人值守也 agent 撰写) |
| `agent-task-cognitive-scaffold.md` | task 那块押后;但无人值守 plan run 的"主循环复用"机制与之同源 |

---

## 7. 落地切口

1. **切口①(先做,纯删除,零新机制)**:拆层次一的三个 regex/classification 代理(#1/#2/#3),让
   chat/IM 意图回归 agent 主循环 + tool gate。验收:grep 无 `web_chat_runtime.py:499`/`feishu.py:55,2291`
   的 ensure_awaiting_plan 旁路;live chat 说"创建任务"→ agent 调工具 → interactive plan。
2. **切口②(核心新机制)**:无人值守 plan run(§5.3)。把 #4 非 live 分支从 `_attach_intercepted_plan`
   → RPC,改为启动 plan-mode 主循环 run。验收:无人值守 gated tool → plan run → awaiting plan,
   plan_json author 为 agent-authored。
3. **切口③**:REST API / supersede(#5/#6)改走切口②的 run。
4. **切口④(收尾)**:删除 `DefaultAgentPlanPlanner` + `PLANNER_ALLOWED_TOOLS` +
   `use_agent_planner` 参数。验收:`grep DefaultAgentPlanPlanner` 零调用;`use_agent_planner` 参数移除。

每个切口 thin E2E(触发 → plan run → ensure_awaiting_plan_from_fill → 测试),独立交付、独立验证。

---

## 8. 验收标准(单一路径可测)

1. `grep -rn "DefaultAgentPlanPlanner\|use_agent_planner=True\|PLANNER_ALLOWED_TOOLS"` → 零业务调用。
2. 所有 awaiting plan 的 `plan_json` 来自 agent `exit_plan_mode` fill(`author_type` 单一)。
3. live chat / Feishu 说"创建任务/触发器"→ agent 主循环调工具 → tool gate → interactive plan(无 regex 旁路)。
4. 无人值守 gated tool → 系统发起 plan-mode run → agent 规划 → awaiting plan → 用户异步确认 → handoff 执行。
5. 治理逐字节不变:`plan_hash`/`validate_confirmation`/`PlanModeGate` 测试全绿。
6. 全程多租户 scope + RLS。

---

## 9. 风险

1. **regex 兜底丢失(切口①)**:agent 只"说"创建任务不"调工具"则不触发 plan。缓解:prompt 引导
   "意图用工具表达";真实创建意图本就该走工具。需 eval 盯 chat 场景召回。
2. **无人值守 plan run 成本(切口②)**:见 §5.6,用轮数上限 + 启用阈值控。
3. **无人值守 run 失败处理**:plan run 失败 → plan 进 `planning_failed`(沿用现有),fail-closed 不执行。
4. **灰度**:切口②上线先 flag 控,生产默认旧 RPC,验收后切换;切口④(删 RPC)最后做。

---

## 10. 原则总结

> CC 只有一条 plan 路径,因为规划是 agent 在主循环里的认知动作——没有第二种"造 plan"的方式。
> Hive 多出的无人值守需求,该用**同一条路径的新入口**满足,而不是新增一条隔离 RPC 路径,
> 更不该用 regex 猜意图绕过 agent。统一之后:**plan 只有一种来源——agent 想出来的。**

---

## 11. 实装进度(逐切口)

| 切口 | 状态 | 证据 |
|---|---|---|
| ① A 拆 chat/Feishu regex auto-sync 代理 | ✅ 已实装 | commit `2eee9e8`(删 405 行;web_chat 无旁路) |
| ② B 无人值守 plan run | ✅ 已实装 | 见下 §11.1 |
| ③ C REST/supersede + Feishu classification | ✅ 已实装 | 见下 §11.2 |
| ④ D 删 `DefaultAgentPlanPlanner` + 移除全部 flag(终态:plan mode/launcher 唯一路径) | ✅ 已实装 | 见下 §11.3 |

### 11.1 切口② 实装记录

> **历史中间态说明**：本节记录切口②当时的灰度实现。当前生产语义以 §11.3/§12.7 为准：
> `PLAN_MODE_UNATTENDED_RUN`、`PLAN_MODE_SYSTEM_RUN`、`PLAN_MODE_TOOL_INTERCEPT_INTERACTIVE` 和 RPC fallback
> 都已删除；eligible source 无条件进入主循环 Plan Mode，非 eligible source 静态 `needs_plan` fail-closed。

**机制定型(比原 §5.3 设想更简洁)**:无人值守(trigger/heartbeat)本就是多轮 kernel run
(`AgentKernel.handle()`,heartbeat 40 轮 / 其他 200 轮),不是"一次性调用"。因此**无需启动新 run**——
只需让无人值守 run 内被拦截的 gated tool 也能像 live chat 那样**在当前 run 内激活主循环 Plan Mode**:
agent 在后续轮只读规划 → `exit_plan_mode` 落 awaiting → run 自然结束 → 用户下次在 plan queue 异步确认。
live 与无人值守**共用同一套 Plan Mode runtime**(每轮 reminder + 只读 policy + `exit_plan_mode`),
区别仅在 `PlanModeState.source`(`tool_intercept` vs `tool_intercept_unattended`)与确认时机。

**改动(5 文件)**:

- 历史中间态曾在 `app/config.py` 新增 `PLAN_MODE_UNATTENDED_RUN`(默认 **off**,灰度;§9.4)。
- `app/runtime/session.py`:新增 `is_unattended_plan_eligible`(白名单 `{trigger, heartbeat}`)+
  `_UNATTENDED_PLAN_RUN_SOURCES`,与 `is_interactive_plan_eligible` 并列、互斥。
- `app/kernel/engine.py`:`_maybe_activate_interactive_plan_from_tool_result` 激活条件从"仅 live chat"
  扩展为 `live(interactive flag) OR unattended(unattended flag)`,并据此打 source label。
- `app/tools/service.py`:`_plan_mode_gate_block` 新增 `plan_mode_unattended_available` 参数;defer 决策
  `defer = defer_to_interactive or defer_to_unattended`,defer 时跳过 `_attach_intercepted_plan`(RPC)、
  改挂 activation signal(`_maybe_attach_interactive_signal(enabled=defer)`);新增 `_tool_intercept_unattended_enabled`。
- `app/runtime/invoker.py`:新增 `_plan_mode_unattended_available`,在两处工具调用透传。

**治理不变量**:`_attach_intercepted_plan` fail-closed、`plan_hash`/`validate_confirmation`/`PlanModeGate`
逐字节未动(§5.5)。flag off 时行为与改动前**完全一致**(回退 RPC planner)。

**测试证据**:

- `tests/runtime/test_plan_mode_state.py`:`is_unattended_plan_eligible` 白名单 + 与 interactive 互斥(disjoint)。
- `tests/kernel/test_plan_mode_tool_intercept_activation.py`:trigger/heartbeat + flag on → 激活
  (source=`tool_intercept_unattended`、ContextVar armed);flag off(即使 interactive on)→ 不激活(回退 RPC);
  两 flag 同开时 live 优先保持 `tool_intercept`。
- `tests/tools/test_service.py`:unattended flag on → defer(`ensure_awaiting_plan` calls==0、挂 signal、无 plan_id);
  flag off → 回退 RPC(`ensure_awaiting_plan` calls==1、embed plan_id、无 signal)。
- 全域回归:**plan-mode 相关 301 passed**;3 个改动测试文件 ruff 全绿。

**已收口**:该中间态已被切口④替换;`PLAN_MODE_UNATTENDED_RUN` 与 RPC fallback 均已删除,无人值守 eligible source
无条件进入主循环 Plan Mode,非 eligible source 静态 `needs_plan` fail-closed。

### 11.2 切口③ 实装记录(③a + ③b)

> **历史中间态说明**：本节记录 launcher 上线前的 flag-on/flag-off 灰度状态。当前运行态不再有
> `PLAN_MODE_SYSTEM_RUN` 或 RPC 回退；REST/Feishu classification 无条件走 `system_plan_run` launcher。

**机制定型(完全照 §12 规格)**:无 agent run 的纯外部入口(REST create/regenerate/revise + Feishu
classification)通过**启动一个 system_plan_run**(预激活 Plan Mode + 带 draft `plan_id`)让 agent 在主循环
只读规划 → `exit_plan_mode` 填充该 draft → 落 `awaiting_confirmation`。与切口② 的唯一差别:切口② 在
已运行的 kernel loop **内**被拦截后激活(plan_id 空 → 新建);launcher 在 run **前**预激活且带 plan_id
(→ 填充已有 draft)。两者复用同一套 Plan Mode runtime(每轮 reminder + 只读 policy + `exit_plan_mode`)。

**历史 flag 中间态（已删除）**:切口③曾引入 `PLAN_MODE_SYSTEM_RUN: bool = False` 做 launcher 灰度。
切口④后该 flag 与 off 回退均已删除;REST/Feishu classification 无条件走 launcher。

**改动(8 文件)**:

- 历史中间态曾在 `app/config.py` 新增 `PLAN_MODE_SYSTEM_RUN`(默认 off,灰度)。
- `app/runtime/session.py`:`PlanModeState.to_metadata()` 仅当 `plan_id` 非空时输出 `plan_id`(否则 live chat /
  无人值守 tool-intercept 的 mirror 逐字节不变 → `exit_plan_mode` 仍走"新建"分支)。
- `app/tools/handlers/plan_mode.py`(③a 核心):`exit_plan_mode` 读 metadata 的 `plan_id`(`_plan_uuid` 解析,
  malformed → None 退回新建)→ **有 plan_id** 调 `generate_plan(plan_id, fill, use_agent_planner=False)`
  填充已有 draft(plan_id 稳定);**无 plan_id** 维持 `ensure_awaiting_plan_from_fill` 新建。
- `app/services/plan_mode_system_run.py`(③b 核心,**新模块**):`launch_system_plan_run(plan, seed_context)` —
  解析 agent/model(tenant-scoped,复制自 RPC planner 的 `_resolve_agent_models`,不依赖将删的
  `DefaultAgentPlanPlanner`)→ 预激活 `SessionContext(source="system_plan_run")` +
  `PlanModeState(active=True, plan_id=str(plan.id), …)` + arm ContextVar → `invoke_agent`(标准工具,被只读
  policy 限制;`max_tool_rounds=20`)→ `finally` reset ContextVar。fail-closed:`invoke_agent` 抛错被吞,
  plan 留非确认态,**绝不执行**。
- `app/services/plan_mode_service.py`:抽出 `supersede_to_draft(plan_id)`(只 supersede 返回 draft,不 generate);
  `revise_plan` 改为 `supersede_to_draft` + `generate_plan`(flag-off 行为逐字节不变)。
- `app/api/plans.py`:新增 `_system_plan_run_enabled()` + `_author_draft_plan(service, plan, fill, plan_id)`
  (flag-on→launcher+reload / flag-off→`generate_plan`);create/regenerate 经 `_author_draft_plan`,revise
  flag-on 经 `supersede_to_draft`+launcher。
- `app/api/feishu.py`:classification 块 flag-on→`create_plan_request`(draft)+`launch_system_plan_run`
  (intent 用 `action_kind_to_intent_signature` 与 RPC 路径一致)+reload;flag-off→旧 `ensure_awaiting_plan`。

**治理不变量(逐字节未动)**:`_apply_generation` / `compute_plan_hash` / `validate_plan_json` / `PlanModeGate` /
`validate_confirmation` / 禁自我确认。多租户 scope + RLS 全程保持(launcher 经 `invoke_agent` 标准治理路径)。
未动切口② 的 trigger/heartbeat 激活;未动 deep_research 的 `ensure_awaiting_plan_from_fill` 路径。

**subtle**:(1)launcher run `source="system_plan_run"` 已 active,不会被
`_maybe_activate_interactive_plan_from_tool_result` 二次激活(`engine:1006` short-circuit);其 gated 写工具被
`execute_with_context` 首检的只读 gate(`service:513`)挡住,不会进 `_plan_mode_gate_block` 触发嵌套 RPC。
(2)③a 的 `generate_plan(use_agent_planner=False)` 与 live chat 的 `ensure_awaiting_plan_from_fill` 共用同一
结构化 fill 落地分支,`author_type` 一致(均 `workflow` 标签——这是结构化 fill 分支既有语义,非 ③ 引入;
切口④ 收口 `generate_plan` 为纯 fill 落地时统一为 agent-authored)。

**测试证据**:

- `tests/runtime/test_plan_mode_state.py`:`plan_id` 未设时不入 mirror(byte-compat)/ armed 时 round-trip。
- `tests/tools/test_exit_plan_mode_tool.py`:plan_id armed → `generate_plan(use_agent_planner=False)` 填充
  (id 稳定、`ensure_awaiting_plan_from_fill` calls==0);无 plan_id → 新建(`generate_plan` calls==0);
  malformed plan_id → 退回新建。
- `tests/services/test_plan_mode_system_run.py`:launcher 预激活(run 内 ContextVar armed 带 plan_id、
  source=system_plan_run、typed state.active)+ run 后 reset;seed_context 入 prompt;fail-closed
  (`invoke_agent` 抛错 → 不传播、ContextVar 复位);agent/model 缺失 → 不 run。
- `tests/services/test_plan_mode_service.py`:`supersede_to_draft` 产 bare draft 不调 planner / 未知 plan 抛错。
- `tests/api/test_plan_mode_plans_api.py`:flag-on create/regenerate/revise → launcher 被调、RPC `generate_plan`
  /`revise_plan` 不被调(`AssertionError` 守卫)、返回 authored 结果;flag-off 旧测试全保留绿。
- `tests/api/test_channel_plan_mode_entry.py`:flag-on Feishu classification → launcher 被调、`ensure_awaiting_plan`
  不被调、intent/original_request/seed_context 正确;flag-off 旧测试保留绿。
- 全域回归:**plan-mode 相关 316 passed**;runtime/kernel/services/tools/api **2579 passed**;
  agents/architecture/core **213 passed**;8 个改动文件 ruff format + check 全绿。

**已收口**:切口④ 已删除 RPC planner
(`DefaultAgentPlanPlanner` / `PLANNER_ALLOWED_TOOLS` / `_run_planner` / `_get_planner` / `use_agent_planner` 参数),
`generate_plan` 已退化为纯 fill 落地,两 flag(`PLAN_MODE_UNATTENDED_RUN` / `PLAN_MODE_SYSTEM_RUN`)和 flag-off
回退消费者均已删除。

### 11.3 切口④ 实装记录(删 RPC + 移除全部 flag → plan mode/launcher 唯一路径)

**用户拍板(方案 B,终态)**:删 RPC + 移除所有灰度 flag,plan mode/launcher 成为生产**唯一**规划路径
(详见 §12.7)。这是彻底收口,符合硬约束"绝不能有两个 plan 路径"。

**删除**:

- `app/services/agent_plan_planner.py` **整个文件删除**(`DefaultAgentPlanPlanner` / `PLANNER_ALLOWED_TOOLS`
  / `PLANNER_EXCLUDED_TOOLS` / `PLANNER_PROMPT_VERSION` / `_planner_system_prompt` / `_build_planner_user_prompt`
  / `_coerce_planner_result` / `AgentPlanPlannerInput` / `AgentPlanPlannerResult` / `PlanPlanningError`)——grep 确认
  零生产引用后删整文件。
- `plan_mode_service.py`:删 `_get_planner` / `_run_planner` / `ensure_awaiting_plan`(intercept-then-create,零
  生产消费者)/ `_mark_generation_failed_by_id`(随 RPC 失败分支死)/ 构造器 `planner=` 参数 + `_planner` 字段 /
  `agent_work_ledger` import(随 RPC ledger 段死)。`generate_plan` **删 `use_agent_planner` 参数 + `=True` 分支**
  (含 planner_work_ledger 初始化/进度段),退化为**纯 fill 落地**(原 `=False` 行为):validate→hash→markdown→awaiting。
  `_planner_prompt_version()` 改返回常量 `"structured_fill.v1"`(不再 import 已删模块),保 `_apply_generation` 逐字节不动。
- `config.py`:**移除三 flag** `PLAN_MODE_TOOL_INTERCEPT_INTERACTIVE` / `PLAN_MODE_UNATTENDED_RUN` / `PLAN_MODE_SYSTEM_RUN`。
- `tools/service.py`:删 `_tool_intercept_interactive_enabled` / `_tool_intercept_unattended_enabled` / `_attach_intercepted_plan`
  (RPC 回退,无条件 defer 后死);defer 改为 `defer = bool(plan_mode_interactive_available or plan_mode_unattended_available)`
  (去 flag 的 and);`_maybe_attach_interactive_signal` 去 flag 检查、`enabled` 参数默认 True(由 gate defer 决策驱动)。
  `plan_mode_interactive_available` / `plan_mode_unattended_available` 参数**保留**(仍判 eligible)。`plan_mode_service` DI
  字段保留(测试构造 + 未来 intake 用,注释更新)。
- `kernel/engine.py` `_maybe_activate_interactive_plan_from_tool_result`:删 flag 检查 → `live = _is_live_interactive_chat(sc)`
  / `unattended = is_unattended_plan_eligible(sc)`**无条件激活**(只看 eligible);source label 不变。
- `api/plans.py`:删 `_system_plan_run_enabled` + flag 分支;`_author_draft_plan` / revise **无条件**走 launcher;删 flag-off
  回退(`generate_plan True` / `revise_plan`)。`api/feishu.py`:删 flag 分支,classification **无条件**走 launcher。

**保留(非 RPC,合法复用)**:`plan_mode_service.revise_plan`(`supersede_to_draft` + `generate_plan` 组合,REST 改内联
launcher 后无业务调用但仍是合法 ledger API)、`tool_args_to_plan_fill` / `action_kind_to_intent_signature`(pure core,
Feishu launcher 仍用 `action_kind_to_intent_signature` 派生 intent)。

**`ensure_awaiting_plan` 命运**:**删除**。grep 证实切口④ 后零生产消费者(原消费者:`_attach_intercepted_plan` 已删、
Feishu flag-off 分支已删)。其内部依赖 `generate_plan(use_agent_planner=True)` 的 RPC 路径,随参数删除而失效;`_find_awaiting_by_signature`
**保留**(`ensure_awaiting_plan_from_fill` 仍用)。

**非 eligible source 静态降级(§12.7 / 关键 fail-closed edge)**:delegation(`source="agent"`)/ runtime / 已 active 的
system_plan_run 的 gated tool 被拦截 → `defer=False`(两 available 均假)→ 不挂 activation signal → payload **保持静态 needs_plan**
→ agent **不规划、不执行被拦截动作**(fail-closed)。已 active 的 system_plan_run 不被 `engine:1006` short-circuit 二次激活。
新增测试 `test_non_eligible_source_intercept_returns_static_needs_plan_fail_closed`(service)+ `test_blocked_tool_non_eligible_source_returns_static_needs_plan`(gate)逐条验证:`status==needs_plan` 且 `activate_interactive_plan` 不在 payload、
`plan_id` 不在 payload、`registry.calls==[]`(工具未执行)。

**治理不变量(逐字节未动)**:`_apply_generation` / `compute_plan_hash` / `validate_plan_json` / `PlanModeGate` /
`validate_confirmation` / 禁自我确认;多租户 scope + RLS(launcher 经 `invoke_agent` 标准路径);切口②③ 的 launcher /
`exit_plan_mode` 双态 / `is_interactive_plan_eligible` / `is_unattended_plan_eligible` 不动。

**测试收口**:删 `tests/services/test_agent_plan_planner.py`(整文件,模块已删);删 RPC-intercept-then-create 测试
(`test_ensure_awaiting_plan_*` 4 个,方法已删)+ flag-off 回退测试(`test_unattended_intercept_falls_back_to_rpc...`、
`test_activation_noop_when_flag_off/_unattended_flag_off`);"flag off 回退 RPC" 语义**改写**为"无条件走 plan mode/launcher"或
"非 eligible → 静态 needs_plan";`_EchoAgentPlanner` 删除,`generate_plan` 测试改直接传 fill;flag-patch 全清。新增非 eligible
fail-closed 测试。**全量回归:3375 passed / 7 skipped / 0 failed**(切口③ 时为 3303 passed);改动文件 ruff format + check 全绿。

**grep 验收**:`DefaultAgentPlanPlanner` / `use_agent_planner` / `PLAN_MODE_UNATTENDED_RUN` / `PLAN_MODE_SYSTEM_RUN` /
`PLAN_MODE_TOOL_INTERCEPT_INTERACTIVE` / `_attach_intercepted_plan` / `_run_planner` / `.ensure_awaiting_plan` 在 `app/`
**零代码引用**(仅 `plan_mode_service.py` 一条历史注释提及 `DefaultAgentPlanPlanner` 说明其被删)。

---

## 12. 切口③/④ 实现设计(launcher 收口 RPC planner)

> 切口② 已让"**有 agent run**"的无人值守拦截走主循环 plan mode。切口③/④ 收口剩下的
> "**无 agent run** 的纯外部入口"(2026-06-03 读码 + Explore 侦察确认)。

### 12.1 现状(侦察证实)

| 入口 | 有 run? | 当前 plan_json 来源 |
|---|---|---|
| REST `create_plan`(`plans.py:351`) | ❌ 无 | `generate_plan(use_agent_planner=True)` → RPC |
| REST `regenerate`(`plans.py:421`) | ❌ 无 | 同上 |
| REST `revise`/supersede(`plans.py:400`→`revise_plan:687-740`) | ❌ 无 | 同上 |
| Feishu classify auto/explicit(`feishu.py:2225+`) | ❌ 无 | `ensure_awaiting_plan:251` → `generate_plan(True)` → RPC |
| tool-intercept(`ensure_awaiting_plan`) | ✅ 有 | 切口② 已覆盖(flag off 回退 RPC) |

枢纽:`ensure_awaiting_plan`(`:251`)与 REST/supersede 都汇入 `generate_plan(use_agent_planner=True)` →
`DefaultAgentPlanPlanner`。这就是要消除的 Path B。

### 12.2 机制:system plan run launcher

无 run 入口要让 agent 主循环规划,必须**启动一个 run**。新增 `launch_system_plan_run(plan)`:

1. 入口先 `create_plan_request` → draft plan(稳定 `plan_id`)。
2. `launch_system_plan_run(plan)`:**预激活** plan mode —— `SessionContext(source="system_plan_run")` +
   `PlanModeState(active=True, plan_id=str(plan.id), intent_type=…, original_request=…)` + arm ContextVar;
   再 `invoke_agent`(标准工具,被 plan mode 只读 policy 限制)。
3. agent 在主循环只读探索/规划 → 调 `exit_plan_mode` 提交 fill。
4. 入口返回该 plan(已 `awaiting_confirmation`)。

与切口② 唯一差别:切口② 是 run **内**被拦截后激活(plan_id 此时为空 → 新建);launcher 是 run **前**预激活
且**带 plan_id**(→ 填充已有 draft)。两者复用同一 plan mode runtime(reminder + 只读 + exit_plan_mode)。

### 12.3 `exit_plan_mode` 双态(关键改造)

`exit_plan_mode` 读 plan mode metadata 的 `plan_id`:
- **有 `plan_id`**(launcher / system plan run):`generate_plan(plan_id, fill, use_agent_planner=False)`
  **填充已有 draft** —— plan_id 稳定,前端已持有的 id 不变。
- **无 `plan_id`**(live chat / 无人值守 tool-intercept,切口②):`ensure_awaiting_plan_from_fill` **新建**(保持现状)。

### 12.4 generate_plan 收口 + 删 RPC(切口④)

- `generate_plan` 删 `use_agent_planner` 参数,退化为**纯 fill 落地**(原 `=False` 行为):验证 → hash →
  markdown → awaiting。`_apply_generation` 逐字节不动(RPC 与 structured-fill 本就共用)。
- 删 `DefaultAgentPlanPlanner` / `PLANNER_ALLOWED_TOOLS` / `_run_planner` / `_get_planner`。
- `ensure_awaiting_plan`(:251 调 True)的消费者(Feishu / tool-intercept)改 launcher 或已被切口② 覆盖。

### 12.5 切口拆分(每步 thin E2E + 测试 + commit)

- **③a**:`exit_plan_mode` 双态(plan_id 填充 / 新建)+ 测试。
- **③b**:`launch_system_plan_run` + REST create/regenerate/revise + Feishu classification 改走 launcher + 测试。
- **④**:删 `DefaultAgentPlanPlanner` + `use_agent_planner` 参数 + 测试收口。

### 12.6 不变量 + 成本

- 治理:`plan_hash`/`validate_confirmation`/`PlanModeGate`/fail-closed 不动;多租户 scope + RLS。
- 成本:每次 REST/Feishu 规划启动一个完整 agent run(比 RPC 一次性更贵)——单一范式的代价(§5.6),
  用 `max_tool_rounds` 上限控(切口④ 后无 flag 灰度;launcher 默认 `SYSTEM_PLAN_RUN_MAX_ROUNDS=20`)。
- subtle:launcher run 的 `source="system_plan_run"` 已 active,不会被
  `_maybe_activate_interactive_plan_from_tool_result` 二次激活(`engine:1006` short-circuit)。

### 12.7 用户拍板:方案 B(删 RPC + 移除 flag + plan mode 唯一)+ 非 eligible 静态降级

> **2026-06-03 用户拍板(切口④ 终态)**:删 RPC planner + 移除所有灰度 flag(`PLAN_MODE_TOOL_INTERCEPT_INTERACTIVE`
> / `PLAN_MODE_UNATTENDED_RUN` / `PLAN_MODE_SYSTEM_RUN`),让 **plan mode / system_plan_run launcher 成为生产唯一规划路径**。
> 不再保留任何"flag off 回退 RPC"的灰度后门——这是彻底收口,直接兑现硬约束"**绝不能有两个 plan 路径**"。

收敛后**唯一**路径(无分叉、无 flag):

| 触发场景 | 激活方式 | source |
|---|---|---|
| live chat(eligible) tool-intercept | run **内**被拦截 → 主循环 Plan Mode(无条件) | `tool_intercept` |
| 无人值守 trigger/heartbeat(eligible) tool-intercept | run **内**被拦截 → 主循环 Plan Mode(无条件) | `tool_intercept_unattended` |
| REST create/regenerate/revise · Feishu classification(无 run) | launcher run **前**预激活 + draft plan_id | `system_plan_run` |
| **非 eligible**(delegation `agent` / runtime / 已 active run) | **不激活** → 静态 needs_plan | —(fail-closed) |

**非 eligible source 静态降级(关键安全语义)**:删 `_attach_intercepted_plan`(RPC 回退)后,`_plan_mode_gate_block` 对
`confirmed_plan_id is None` 的拦截只在 `defer`(eligible)时挂 activation signal;**非 eligible → `defer=False` → payload 原样
是静态 `needs_plan` 块**。agent 收到 needs_plan 既不进 Plan Mode 规划、也不执行被拦截动作 → **fail-closed**。这是删 RPC 后非
eligible source 的正确终态(而非"无路可走报错"):工具被治理硬门挡住,需要 confirmed plan 才能跑;无人为其规划时它就停在 blocked。
已 active 的 system_plan_run 自身被 `engine:1006` short-circuit 不二次激活,不变。

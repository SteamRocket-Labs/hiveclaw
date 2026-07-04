# Runtime Budget Control Plane 方案

日期：2026-07-03
状态：已定稿；2026-07-04 补充主 Agent 终止契约、Subagent/Workflow/Agent Team 计量边界和默认 profile 数值后进入实现验收
范围：Hive runtime admission、预算预占、执行计量、自动运行熔断，以及企业控制中台里的预算治理。

## 1. 问题定义

Hive 现在已经有 tenant、user、agent 三个层级的 token quota counter，但这套机制还不足以保护自主运行场景。

现有 quota 能回答：

- 这个 tenant / user / agent 今天或这个月是否已经超过 token 上限？
- 已经记录了多少 token？

它不能回答：

- 这次 runtime run 现在还能不能继续 enqueue 一个 background subagent？
- 一个 trigger run 在几千个 child task failed 或进入 `needs_reconciliation` 后，还应不应该继续跑？
- 这个 root run 总共预留了多少 tokens、LLM calls、background tasks、cache-miss tokens、wall-clock seconds？
- parent wake 之后应该继续 spawn，切到 summary-only，还是直接停止？
- workflow、trigger、heartbeat、web chat、subagent、task notification 怎么共享同一个执行预算？

这个缺口危险的原因是：Hive 有 Claude Code 本地 CLI 没有以同样形式暴露的长期 durable execution surface，包括 scheduled trigger、heartbeat、durable `RuntimeTask`、background subagent、workflow run、parent continuation wake、restart recovery。

因此这不是单个“众筹雷达”问题，而是 runtime 控制中台缺少一层统一预算治理。

### 1.1 本轮根因拆分

常春藤 / 众筹雷达事故不是单一 bug，而是两个问题叠在一起：

1. **主 Agent 没有足够明确的终止/失败判断。**
   定时任务本质上是平台在固定时间往一个 session chain 里投递 wake prompt。主 Agent 仍然负责判断任务是否完成、是否失败、是否还需要更多 subagent。但如果它把“子任务完成/失败”理解成“继续探索”，就会在 parent wake 后继续 spawn。

2. **平台没有足够硬的 root-run 保护。**
   即使主 Agent 判断失误，平台也不能允许一次 trigger fire 在 durable `RuntimeTask`、subagent completion signal、parent continuation wake 之间无限放大。模型可以判断，平台必须设边界。

因此方案必须同时落两层：

| 层级 | 目标 | 失败时谁兜底 |
| --- | --- | --- |
| 主 Agent 行为层 | 让模型知道什么时候继续、什么时候总结、什么时候失败并停止。 | runtime prompt / parent wake contract / tool result contract。 |
| 平台硬保护层 | 就算模型没停，也不能无限创建 subagent、workflow leaf、delegation、parent wake 或 provider call。 | Runtime Budget Control Plane。 |

只做主 Agent prompt 会变成“相信模型一定会停”；只做平台预算会让模型体验像突然撞墙。两层必须一起做。

## 2. 北极星

Runtime Budget Control Plane 是每一次 autonomous/background runtime action 的硬执行许可证。

每个 runtime run 必须具备：

- root run identity
- policy snapshot
- reservation ledger
- settlement ledger
- circuit-breaker state
- auditable terminal reason

如果一个 background 或 autonomous action 无法绑定有效 runtime budget，它必须在 enqueue 工作或调用 LLM/provider 之前 fail closed。

## 3. 与 Claude Code 的对齐边界

Hive 应保留 Claude Code 的 AgentTool 语义：

- `spawn_subagent` 继续是 session-local worker primitive。
- 支持 foreground、background、parallelism、child completion notification、fresh child context。
- 不应把它简单砍掉，也不应强行退化成 deterministic workflow-only abstraction。
- AgentTool-style worker 仍然适合探索、验证、独立判断和临时研究。

Hive 也应保留 Claude Code 风格的预算闭环：

- `maxTurns` 对应 runtime tool-round / continuation limit。
- `maxBudgetUsd` 对应 run-level spend/token budget。
- provider rate limit 对应 provider error handling 和 backoff。
- `TaskStop` 对应显式 runtime cancellation。
- usage accounting 对应 token/cost telemetry。

但 Hive 必须增加 Hive-native enterprise guard。原因是 Hive 会在非交互、长期、durable 的 runtime 里执行自动任务，这已经超出 Claude Code 本地 CLI 的控制形态。新增 guard 不是替代 AgentTool 语义，而是在 AgentTool 外面加企业控制中台 envelope。

### 3.1 为什么 Claude Code 的预算挡不住这类事故

Claude Code 的 `maxTurns` 是单个 `query()` 调用里的 turn gate。Hive 的事故链路不是一个 parent invocation 内部跑太多 turn，而是 child completion 触发 durable parent wake，然后 parent 重新进入一次新的 invocation。每次 wake 都会重置单次 invocation 的 turn counter，所以只对齐 `maxTurns` 不足以覆盖这个 horizontal loop。

Claude Code 的 subagent/AgentTool 主要是 session-local、process-local worker；Hive 把它扩展成 durable `RuntimeTask`、跨 worker claim、completion signal、parent continuation wake、restart reconciliation。这个 Hive-native surface 是产品能力，也是风险来源。因此预算 envelope 必须绑定 root runtime run，而不是只绑定一次 LLM invocation。

`maxBudgetUsd` 也只能覆盖当前 query / engine 看到的累计 cost；它不是跨 durable task、跨 parent wake、跨 restart 的企业级 run ledger。Hive 需要把 spend/fanout/wake/reconciliation 都放进同一个 budget run。

## 4. 当前真实状态

现有 live surface：

- `backend/app/services/quota_guard.py`：在 invocation 前检查 tenant、agent、user 的 daily/monthly token counter。
- `backend/app/services/token_tracker.py`：模型调用后记录 usage。
- `backend/app/models/runtime_task.py`：持久化 runtime task。
- `backend/app/services/runtime_task_worker.py`：按 task type 控 worker 并发。
- `backend/app/services/subagent_run_service.py`：创建 durable background subagent task。
- `backend/app/tools/handlers/subagent.py`：暴露 `spawn_subagent(run_in_background=true)`。
- `backend/app/runtime/workflow_admission.py`：已有 workflow 的 static admission，覆盖 budget、fanout、concurrency、leaf calls、wall-clock。
- `backend/app/runtime/invoker.py`：从 `max_tool_rounds` 推导 per-invocation `turn_token_budget`。

常春藤事故的代码级闭环：

```text
daily_scan trigger
→ 众筹雷达 parent invocation
→ LLM loop 反复调用 spawn_subagent(run_in_background=true)
→ start_subagent_run() 无 root-run cap 地创建 durable RuntimeTask
→ child completed / failed / needs_reconciliation
→ completion signal 或 task notification wake parent
→ parent 作为新的 invocation 续跑
→ 再次 spawn background subagent
→ 跨 tick / 跨 invocation 无总量上界
```

现有防线都是瞬时节流，不是总量熔断：

- `runtime_task_worker` 的 task type limit 只限制同时 claim/dispatch 的数量，不限制一天内为同一 root/trigger 创建多少 durable work。
- `drain_subagent_completion_wakes(max_wakes=10)` 只限制单次 drain tick 的 wake 数；下一个 tick 仍会继续消费剩余 signal。
- `DEFAULT_MAX_SUBAGENT_DEPTH=2` 只限制 child 再 spawn grandchild 的垂直递归；parent 被 wake 后再 spawn 是水平循环，parent 仍然是 root/depth 0。
- `quota_guard._raise_if_limit_reached()` 是 `if limit and used >= limit`；tenant/user/agent 没配限额或限额过大时，这层不会提供任何保护。

当前缺口：

- quota 是 counter-based，不是 reservation-based。
- runtime task 没有共享 root run budget。
- worker concurrency 不等于 total work admission。
- `spawn_subagent` background enqueue 没有 per-root-run fanout cap。
- `delegate_to_agent` 也是 work-amplifying surface，但当前方案不能只在 summary-only 禁用它，必须在正常运行时也计量。
- parent wake continuation 没有 circuit breaker。
- cache-miss spend 不是一等预算维度。
- workflow 有 static admission，但 subagent 和 parent wake 没有复用同一套 admission service。
- quota 可以不设置；但即使客户没设置 quota，runtime 也必须有默认 guardrail。

## 5. 设计原则

1. **先 admission，再创建工作。**
   在创建 queued `RuntimeTask` 或 provider call 之前拒绝不安全工作。

2. **先 reservation，再执行昂贵操作。**
   runtime run 在 provider call 或 background enqueue 前预占预算。

3. **按真实 usage settlement。**
   provider 返回真实 usage 后，结算 reservation 并写审计事件。

4. **默认 runtime policy 永远生效。**
   tenant/user/agent quota 可以为空，但 runtime budget 不可以不存在。

5. **root-run budget 向下继承。**
   child subagent、task notification、workflow leaf、continuation wake 默认消费同一个 root run budget，除非显式创建新的已批准 root。

6. **circuit breaker 是 runtime 语义，不只是监控。**
   监控报警是第二层；runtime 自身必须能停止或降级。

7. **workflow 与 subagent 共用预算底座。**
   workflow admission 保留，但预算检查要复用 subagent/background 同一套 service。

8. **约束模型造成的行动，不替代模型思考。**
   LLM 仍然负责拆解和判断；平台负责限制它能触发多少工作。

9. **reservation 必须原子化。**
   所有预算预占必须是数据库内的 conditional update / row lock / advisory lock；禁止 read-then-write。

10. **预算系统故障要有明确安全语义。**
    background、scheduled、heartbeat/evolution maintenance、workflow、work-amplifying tools 默认 fail-closed；直接真人 interactive 回复可以 fail-open 但必须降级并告警。

11. **budget run 绑定一次活跃续跑链，不绑定整个 session。**
    同一个用户长会话里的多条独立消息不能永久累积到同一个 budget；但一条消息或一次 trigger fire 引发的 subagent、wake、delegation、workflow leaf 必须共享同一个 budget。

12. **estimate 必须来自真实成本，宁高勿低。**
    reservation 的估值如果长期低于真实消耗，budget 只是纸面护栏；default reservation 要用生产观测校准。

13. **hard stop 必须处理存量 work。**
    熔断不仅禁止新建工作，还要处理同一 budget 下已经入队但尚未执行的 work item。

14. **主 Agent 必须显式做继续/完成/失败判断。**
    parent wake 不是“继续原样运行”的空白授权。每次 wake 都必须要求主 Agent 在继续、完成、失败/阻塞并停止之间做选择，并把选择写入 runtime event / session timeline。

15. **Agent Team 不计入 Subagent。**
    普通 Mix Sub Agent 和 workflow 内 Mix Sub Agent 都消费 `subagents`；Agent Team member 是完整 child session / teammate lane，应消费单独的 `team_sessions`，否则会把两个语义不同的执行单元混在一起。

16. **cache-miss 按 root run 计量，按 agent/display 归因。**
    非缓存 token 是本次事故的主要成本放大面。预算 admission 必须按 root run 统计，不能按单个 agent 分散重置；UI 可以展示每个 subagent / team member / provider call 的归因，但不能改变 admission root。

17. **记忆蒸馏不进入本轮控制分类。**
    Summary Agent、Learning Brain、Memory Gate Agent、T3/Heartbeat Curator、Dream/Soul Writer、Skill Distiller 这类蒸馏/自进化 actor 不是 `spawn_subagent` worker，也不是 Agent Team member。它们是 direct LLM call / curation actor，没有普通 agent tool surface，不能自己调用 `spawn_subagent`。本轮 Runtime Budget 分类不为蒸馏新增额度、不假设蒸馏 token 很小、不把它放进公司后台这组三类控制项。唯一必须守住的边界是：蒸馏 actor 自身不消费 `subagents`。

## 6. 术语

| 术语 | 含义 |
| --- | --- |
| Account quota | tenant/user/agent 的 daily/monthly hard cap。 |
| Runtime budget policy | 某类 runtime work 的预算规则。 |
| Runtime budget run | 绑定到一次活跃续跑链的具体预算实例。 |
| Active continuation chain | 一次 trigger fire、一次 heartbeat tick、一次 workflow run，或一条用户消息及其引发的所有 subagent/delegation/wake 续跑。 |
| Root runtime task | 拥有预算的顶层 `RuntimeTask`；interactive turn 可没有独立 root task，但必须有 root turn/run key。 |
| Reservation | 执行前对某个预算维度的预占。 |
| Settlement | 执行完成后按真实 usage 结算。 |
| Circuit breaker | 强制 stop、summary-only 或 require-confirmation 的 runtime 状态。 |
| Summary-only mode | parent 只能总结当前结果，不能再 enqueue 新工作。 |
| Run Control Contract | 每次 autonomous root run / parent wake 给主 Agent 的继续、完成、失败判断契约。 |
| Mix Sub Agent | 通过 `spawn_subagent` 创建的 session-local worker；包括普通 subagent 和 workflow leaf 中调用的同一 worker primitive。 |
| Agent Team session | Team teammate / member session，是可进入的完整 child session；不计入 `subagents`，计入 `team_sessions`。 |
| Distillation actor | 蒸馏/自进化维护 actor，例如 Summary Agent、Learning Brain、Memory Gate Agent、T3/Heartbeat Curator、Dream/Soul Writer、Skill Distiller；本轮不进入控制分类，也不计入 `subagents`。 |
| Cache-miss tokens | 本次 root run 内未命中 provider prompt cache 的输入 token 风险；provider 不返回该指标时按 prompt estimate 计入 admission。 |

## 7. 数据模型

### 7.1 `runtime_budget_policies`

用途：可复用预算策略定义。

字段：

```text
id
tenant_id
scope_type: global | tenant | user | agent | trigger | task_type | source
scope_id
source: web_chat | trigger | heartbeat | workflow | subagent | task_notification | agent_session_mailbox | delegation | agent_team
profile: interactive | scheduled | heartbeat | workflow | agent_team | background_worker
is_enabled
enforcement_mode: observe | enforce
priority
max_tokens
max_cache_miss_tokens
max_llm_calls
max_subagents
max_team_sessions
max_delegations
max_background_tasks
max_concurrency
max_tool_rounds
max_continuation_wakes
max_parent_invocations
max_wall_clock_seconds
max_failures
max_needs_reconciliation
max_child_failure_ratio
default_child_token_reservation
default_llm_call_token_reservation
fail_mode: hard_stop | summary_only | require_confirmation
created_at
updated_at
```

policy resolution 顺序：

```text
trigger-specific
agent-specific
user-specific
tenant-specific
source/task-type profile default
global default
```

每次 run 选中的 policy 都要复制成 snapshot。后续 policy 修改不能改写历史 run 的执行语义。

### 7.2 `runtime_budget_runs`

用途：一次 root run 的实时和历史预算状态。

字段：

```text
id
tenant_id
user_id
agent_id
root_runtime_task_id
root_session_id
parent_session_id
root_run_kind: interactive_turn | trigger_fire | heartbeat_tick | workflow_run | agent_team_run | delegation_run | system_repair
root_run_key
origin_invocation_id
trigger_id
trigger_fire_id
source
profile
policy_id
policy_snapshot
enforcement_mode_snapshot: observe | enforce
status: active | summary_only | exhausted | cancelled | completed | failed | expired
terminal_reason
reserved_tokens
used_tokens
reserved_cache_miss_tokens
used_cache_miss_tokens
reserved_llm_calls
used_llm_calls
reserved_subagents
used_subagents
reserved_team_sessions
used_team_sessions
reserved_delegations
used_delegations
reserved_background_tasks
used_background_tasks
continuation_wakes
parent_invocations
failures
needs_reconciliation_count
started_at
expires_at
last_activity_at
completed_at
created_at
updated_at
```

Root key 规则：

```text
trigger fire:
  root_run_kind = trigger_fire
  root_run_key = trigger:<trigger_id>:<fire_id_or_runtime_task_id>

interactive user message:
  root_run_kind = interactive_turn
  root_run_key = session:<session_id>:turn:<user_message_id_or_runtime_task_id>

parent wake / task notification continuation:
  inherit existing budget_run_id from child RuntimeTask / completion signal metadata
  do not create a fresh budget_run

next independent user message in the same chat session:
  create a new budget_run

workflow run:
  inherit caller budget_run_id when launched from an active chain
  otherwise root_run_kind = workflow_run and root_run_key = workflow:<runtime_task_id_or_workflow_run_id>

delegation run:
  inherit caller budget_run_id when launched from an active chain
  otherwise root_run_kind = delegation_run and root_run_key = delegation:<runtime_task_id_or_delegation_session_id>

heartbeat:
  root_run_kind = heartbeat_tick
  root_run_key = heartbeat:<agent_id>:<runtime_task_id_or_tick_id>
  profile = heartbeat
  note: existing independent heartbeat/evolution budget; not one of the three company control categories in §9

explicit Agent Team root:
  root_run_kind = agent_team_run
  root_run_key = agent_team:<session_id_or_runtime_task_id>
  profile = agent_team
```

`root_session_id` 是 transcript / UI lineage，不是 budget lifetime boundary。budget lifetime boundary 由 `root_run_kind + root_run_key` 决定。

### 7.3 `runtime_budget_events`

用途：append-only evidence ledger。

字段：

```text
id
tenant_id
budget_run_id
runtime_task_id
agent_id
user_id
event_type: create | reserve | settle | release | deny | would_deny | circuit_break | complete | expire | cancel | parent_decision
dimension: tokens | cache_miss_tokens | llm_calls | subagents | team_sessions | delegations | background_tasks | wall_clock | failures | continuation_wakes | parent_invocations
amount
source
reason
reservation_key
metadata_json
created_at
```

### 7.4 `runtime_tasks` 增量字段

给 `runtime_tasks` 增加轻量外键和终止元数据：

```text
budget_run_id
root_runtime_task_id
budget_snapshot_json
budget_terminal_reason
```

完整可变预算状态仍保存在 `runtime_budget_runs`，不要复制到每个 task 上。

### 7.5 Reservation 原子性

预算预占不能实现成：

```text
select current counters
if current < max:
  update counters
```

这会在多个 worker 同时 reserve 同一个 `budget_run_id` 时发生 TOCTOU：两个 worker 都读到 `reserved_subagents=9` 且 `max_subagents=10`，然后都放行，最后变成 11。

必须使用单个事务里的原子 conditional update。形态可以是：

```sql
update runtime_budget_runs
set
  reserved_subagents = reserved_subagents + :subagents,
  reserved_background_tasks = reserved_background_tasks + :background_tasks,
  reserved_tokens = reserved_tokens + :tokens,
  updated_at = now()
where id = :budget_run_id
  and status = 'active'
  and reserved_subagents + used_subagents + :subagents <= :max_subagents
  and reserved_background_tasks + used_background_tasks + :background_tasks <= :max_background_tasks
  and reserved_tokens + used_tokens + :tokens <= :max_tokens
returning id, status;
```

如果一次 admission 需要同时 reserve 多个维度，必须在同一个 update 条件里一起判断并一起增加，不能拆成多个非原子步骤。

可选实现：

- `UPDATE ... WHERE ... RETURNING` 作为默认方案。
- `SELECT ... FOR UPDATE` 包住整行预算状态，适合需要复杂 policy snapshot 计算的路径。
- Postgres advisory lock 只作为补充，不能替代最终的条件更新。

`runtime_budget_events.reservation_key` 用于幂等：同一个 tool call / provider call / wake continuation 重试时不能重复扣 reservation。

### 7.6 Reservation estimate contract

reservation 估值是安全系统的一部分，不是 UI 默认值。

基本规则：

- `estimated_tokens` 必须大于等于该 source/profile 最近生产真实成本的 p50，scheduled/background/heavy research 默认应使用 p75 或更高。
- child work 的 start reservation 至少取 `max(policy.default_child_token_reservation, assembled_prompt_estimate, profile_observed_floor)`。
- provider call 的 reservation 至少取 `max(prompt_token_estimate + output_reservation, policy.default_llm_call_token_reservation, profile_observed_floor)`。
- `cache_miss_tokens` 对 background subagent 默认按 fresh context 估算；不能假设 prompt cache 命中。
- 每次 settlement 都要把 actual usage 写入 calibration surface，用于周期性更新 default reservation。

初始 default 不能拍脑袋。scheduled profile 的 `default_child_token_reservation`、`default_llm_call_token_reservation`、`max_cache_miss_tokens`、`max_subagents`、`max_continuation_wakes` 必须用常春藤事故和后续正常 trigger 的真实 Railway/DB 计量反推。

如果某个 provider 不返回 cache-miss metrics，cache-miss reservation 仍应按 prompt estimate 计入风险预算；settlement 时标记 `cache_miss_usage_observed=false`，但不能跳过 admission。

### 7.7 Lifecycle、过期与悬挂 reservation 回收

`runtime_budget_runs.expires_at` 必须被 runtime daemon/reaper 实际消费：

- trigger/scheduled：`expires_at = started_at + max_wall_clock_seconds`。
- heartbeat / distillation：沿用已有 heartbeat / evolution 预算或内部节流；本轮不新增公司后台控制分类。
- interactive turn：使用 active run idle timeout；下一条独立用户消息新建 budget run，不复用旧 run。
- workflow：使用 workflow profile wall-clock cap，wait/suspend 状态按 workflow policy 决定是否延长。

reaper 职责：

```text
scan runtime_budget_runs where status in ('active', 'summary_only') and expires_at < now
for each expired run:
  cancel or release in-flight reservations with no live owner
  mark pending/unclaimed RuntimeTask under this budget according to policy
  write runtime_budget_events(event_type='expire'/'release'/'cancel')
  set status='expired' or terminal policy status
```

悬挂 reservation 必须可回收：

- enqueue 失败：立即 release。
- RuntimeTask cancelled before claim：release reservation and mark task terminal。
- worker claimed but process crashed before settlement：restart reconciliation 检查 claim lease / owner heartbeat，确认无 live owner 后 release 或 settle estimated floor。
- provider call reserved but no response：按 request timeout / invocation failure path settle as failed and release unused output reservation。

没有 reaper 的 reservation system 会把 run 卡成假 exhausted；这属于实现 blocker，不是后续优化。

## 8. Runtime 执行流

### 8.1 Root run 创建

web chat、trigger、heartbeat、workflow、Agent Team、delegation 等 executable runtime root：

```text
resolve root_run_kind/root_run_key
create root RuntimeTask when the source has a durable root task
resolve runtime budget policy
create runtime_budget_run with policy_snapshot
attach budget_run_id to root RuntimeTask and runtime/session metadata
dispatch or execute
```

除了显式标记的 system repair job，任何 active continuation chain 都不应在没有 budget run 的情况下进入执行。

Interactive web chat 的边界：

```text
one user message + all work it causes = one budget_run
parent wake/task notification continuation caused by that message = same budget_run
next independent user message in same chat session = new budget_run
```

Trigger 的边界：

```text
one trigger fire = one budget_run
all subagents/delegations/wakes/workflow leaves caused by that fire = same budget_run
next cron fire = new budget_run
```

### 8.2 Background subagent 入队

在 `start_subagent_run()` 创建 durable subagent task 前：

```text
resolve inherited budget_run_id from session/runtime context
atomically reserve dimensions:
  subagents += 1
  background_tasks += 1
  tokens += estimated_child_start_cost
if reservation denied:
  return tool result with budget denial
  do not create RuntimeTask
if admitted:
  create RuntimeTask(task_type="subagent", budget_run_id=...)
  persist full subagent_spec
```

关键点：被拒绝时不能创建 `RuntimeTask`，否则仍会污染 runtime queue 和后续 wake/reconciliation。

### 8.3 Delegation 入队

`delegate_to_agent` 是 To Employee 的 work-amplifying surface。它不等同于 session-local `spawn_subagent`，但同样会创建后台运行、跨 agent continuation、后续消息和可能的 A→B→A 循环。因此它必须进入同一个 runtime budget system。

在 `delegate_to_agent` 调用 `delegate_async()` 或 local-agent channel enqueue 前：

```text
resolve inherited budget_run_id from runtime/session context
atomically reserve dimensions:
  delegations += 1
  background_tasks += 1
  tokens += estimated_delegation_start_cost
if reservation denied:
  return tool result with budget denial
  do not create delegation RuntimeTask / local channel work item
if admitted:
  create RuntimeTask(task_type="delegation", budget_run_id=...)
  or enqueue local-agent work item with budget_run_id
```

`send_agent_session_message` 如果只是给已存在 child session 追加交互，应至少 reserve `continuation_wakes` 或 `llm_calls/tokens`；如果它会唤醒/排队后台执行，也必须消费 `background_tasks`。

### 8.4 LLM/provider call

provider request 前：

```text
estimate prompt/output/cache-miss risk
atomically reserve dimensions:
  llm_calls += 1
  tokens += estimated_tokens
  cache_miss_tokens += estimated_uncached_tokens
if denied:
  return Runtime Limit result before provider call
```

provider response 后：

```text
extract actual usage
settle tokens
settle cache_miss_tokens
record token_usage_event
record invocation span
evaluate circuit breaker
```

### 8.5 Parent wake continuation

`subagent_wake_consumer` 调用 parent continuation 前：

```text
load budget_run
atomically reserve continuation_wakes += 1
if exhausted or child failure ratio exceeded:
  append circuit_break event
  wake parent in summary-only mode, or stop without wake according to policy
else:
  continue parent normally
```

parent invocation 必须拿到 runtime metadata：

```text
budget_mode: normal | summary_only | hard_stop
```

如果是 `summary_only`，prompt/tool layer 必须禁止：

```text
spawn_subagent
start_workflow
delegate_to_agent
其他 work-amplifying tools
```

### 8.6 Workflow admission

`workflow_admission.py` 保留 static compiler/admission 职责，但预算部分应调用 shared budget service：

- planned leaf calls
- planned fanout
- requested token budget
- requested wall-clock budget
- run-level reservation

这样可以避免 workflow 与 subagent 各自拥有不同预算语义。

### 8.7 Budget service 降级语义

budget service 自身故障时必须安全可预期：

| Runtime source | Budget unavailable / policy invalid / DB reservation error |
| --- | --- |
| `trigger` / `scheduled` | fail-closed；不创建 work item，不调用 provider。 |
| `heartbeat` / evolution maintenance | 保持现有独立预算/节流语义；不进入本轮 sub-agent/team/workflow 分类。 |
| `workflow` | fail-closed；不启动 leaf execution。 |
| `spawn_subagent(run_in_background=true)` | fail-closed；不创建 `RuntimeTask`。 |
| foreground `spawn_subagent` | fail-closed；避免无预算 work amplification。 |
| `delegate_to_agent` / local-agent delegation | fail-closed；不创建 delegation task / local work item。 |
| parent wake continuation | fail-closed 或 summary-only；禁止再次触发 work-amplifying tools。 |
| direct interactive web chat LLM response | fail-open，但标记 `budget_observability_degraded=true`、发告警、禁用 background/work-amplifying tools。 |

这条不是调优项，而是安全语义。实现前必须写入测试，避免 breaker 只在系统正常时生效。

交互体验边界：budget service 故障时，真人在线会话仍可继续 direct LLM response，但 foreground `spawn_subagent` 仍然 fail-closed。这是刻意的安全取舍：服务故障时不阻断真人继续沟通，但也不允许模型放大工作量。产品表现应是“预算系统降级，子任务暂不可用”，而不是“模型失败”或“工具 bug”。

### 8.8 Run Control Contract：主 Agent 终止/失败判断

每个 autonomous root run 和 parent wake continuation 都必须给主 Agent 注入一段明确的运行契约。它不是用户可见文案，而是 runtime 对模型的执行边界：

```text
Run objective:
  <本次 trigger / workflow / user message 要达成什么>

Success criteria:
  <满足哪些事实时必须结束并总结>

Failure / stop criteria:
  <连续失败、无新增证据、预算耗尽、权限缺失、外部依赖不可用时如何停止>

Remaining runtime guard:
  subagents: used/reserved/max
  team_sessions: used/reserved/max
  continuation_wakes: used/max
  provider_calls: used/reserved/max
  cache_miss_tokens: used/reserved/max

Decision required on every parent wake:
  continue_with_new_evidence | complete_and_summarize | fail_or_block_and_stop
```

主 Agent 的行为规则：

- 只有在“新的子任务会带来新的证据或新的行动路径”时才能继续 spawn。
- 如果最近一批 subagent 没有新增证据、重复失败、进入 `needs_reconciliation`，主 Agent 应停止扩散，汇总失败原因。
- 如果成功条件已经满足，必须完成并总结，不能因为还有剩余额度就继续探索。
- 如果失败条件满足，必须记录原因并停止，不能用新的 subagent 掩盖失败。
- 如果需要人类或管理员决策，必须进入 blocked / approval path，不得自建 trigger 或继续 fanout。

这层解决“主 Agent 会不会停”的问题；但它不是唯一安全边界。平台硬保护仍然必须独立生效。

### 8.9 重复 spawn 与无新增证据 breaker

除了数值预算，runtime 还应记录 parent 在同一 root run 内的 subagent intent 指纹：

```text
fingerprint = hash(parent_agent_id, root_budget_run_id, subagent_type, normalized_prompt_goal, definition_name)
```

规则：

- 相同 fingerprint 连续出现，且上一批结果没有新增 artifact / finding / evidence，应触发 `repeated_subagent_intent` warning。
- 超过 policy 阈值后，parent wake 必须进入 summary-only 或 blocked；禁止继续创建相同 intent 的 subagent。
- workflow leaf fanout 不用这个语义 breaker 误杀，因为 workflow definition 已声明 fanout；但 workflow leaf 仍消费 `subagents` 和 token/cache-miss 预算。
- Agent Team member 不进入 `subagents` fingerprint；Team 使用 `team_sessions` 和 team mailbox / session lineage 计量。

这个 breaker 用来补足纯数值 cap 的盲区：即使还没达到 `max_subagents`，重复无收益的探索也应该停止。

### 8.10 记忆蒸馏 / Heartbeat / Skill Distiller 的边界

Heartbeat 不是 Subagent。Heartbeat 是平台维护 tick / session 续跑机制，用来触发记忆整理、自进化维护、T2/T3 intake、reflection learning、skill candidate 检查等工作。

蒸馏 actor 也不是 Subagent：

```text
Summary Agent
Learning Brain
Memory Gate Agent
T3 / Heartbeat Curator
Dream / Soul Writer
Skill Distiller
```

这些 actor 是平台维护型 LLM actor / curator。它们可以有 agent-like prompt、可以调用 LLM、可以写候选包或审查候选包，但它们不是通过 `spawn_subagent` 创建的业务 worker，也不是用户可进入的 Agent Team member session。当前代码里的 SkillDistiller、Skill Referee、T2 phase agent、Dream 都是 `create_llm_client_from_config(...).complete/stream(...)` 的 direct LLM call，不传 `tool_executor`，因此不能调用 `spawn_subagent`。

本轮决策：

- 不新增蒸馏专用 profile。
- 不新增蒸馏专用预算维度。
- 不把蒸馏/heartbeat/skill distiller 放进 Company Admin 的本轮三类控制项。
- 不假设蒸馏 token 消耗很小；大底座文本下，蒸馏可能是高 token 工作。
- 如果蒸馏线已有独立预算或内部节流，保持它在自己的 lane 中治理，不并入本轮 sub-agent/team/workflow 分类。
- 唯一必须接入本轮语义的是：蒸馏 actor 自身不消费 `subagents`，也不能被实现成普通 agent tool-loop 后再暴露 `spawn_subagent`。
- Heartbeat 需要单独表述：当前 heartbeat 已退化为平台维护 tick + direct T3 core，不再通过完整 `invoke_agent` tool-loop 执行，也不暴露 `spawn_subagent`。它可以调用 direct LLM core 做 T3 artifact drafting / Memory Gate review，但蒸馏 actor 自身不消费 `subagents`，也不进入 Company Admin 本轮三类控制项。

产品表达：

- 公司后台本轮不要展示蒸馏相关配置入口。
- 普通用户只看到记忆整理/自进化的结果状态；不需要看到 Summary Agent、T3 Curator、reservation、root_run_kind 等内部细节。

## 9. 默认 Policy Profile

默认 profile 是平台内置保护，不是用户手动开启的功能。Company Admin 可以覆盖数值和 observe/enforce，但没有公司策略时平台默认策略必须生效。

公司后台本轮只放三类控制项：

1. **Sub-agent 线**：覆盖普通 `spawn_subagent`，包括 interactive / scheduled root 下的 Mix Sub Agent。
2. **Agent Team 线**：覆盖显式进入的 teammate / member session；Team member 本身不算 subagent。
3. **Dynamic Workflow 线**：覆盖显式 workflow runtime；大 fanout 只能通过这个线或它的 policy override 承载。

数值原则：

- **按 root run 算，不按单 agent 算。** 一次 trigger fire、一次 workflow run、一次 Agent Team run，或一条用户消息引发的所有 provider call / wake / child work 都共享同一个预算。
- **Subagent、Agent Team、Workflow 分开。** 普通 `spawn_subagent` 和 workflow leaf 中的 Mix Sub Agent 消费 `subagents`；Agent Team member 消费 `team_sessions`；Workflow 通过 workflow profile 管 declared fanout。
- **蒸馏不进入本轮分类。** 蒸馏 actor 不消费 `subagents`，也不在本表假设 token 很小或新增默认 token cap。
- **cache-miss 是独立硬维度。** `total_tokens` 可以较高，但 `cache_miss_tokens` 必须更严格，因为它代表真实新输入成本。provider 不返回 cache 指标时按 prompt estimate 计入风险。
- **scheduled 默认不承载 100-200 worker fanout。** 如果一次任务预期启动 100-200 个 subagent，它应走 Dynamic Workflow runtime，并由 workflow policy 或公司 override 提高该 run 的预算，而不是落到普通 daily trigger profile。
- **公司策略优于默认设置。** Override 只影响新 run；已开始的 run 使用启动时 policy snapshot。

### 9.1 初始内置默认值

这些值是初始安全默认，用于避免裸奔。上线前可以用 production readout 校准，但不能把默认保护删掉或改成无限。

| Profile | 控制线 | 典型入口 | max_subagents | max_team_sessions | max_delegations | max_continuation_wakes | max_parent_invocations | max_provider_calls | max_tokens | max_cache_miss_tokens | default work reservation | 默认 fail mode |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `interactive` | Sub-agent | 真人 web chat 一条消息 | 24 | 4 | 16 | 64 | 16 | 300 | 50,000,000 | 10,000,000 | 200,000 | require_confirmation |
| `scheduled` | Sub-agent | cron / daily_scan / interval trigger | 32 | 0 | 12 | 64 | 16 | 240 | 40,000,000 | 8,000,000 | 250,000 | summary_only |
| `workflow` | Dynamic Workflow | 已声明 fanout 的 Dynamic Workflow run | 256 | 0 | 64 | 512 | 64 | 2,000 | 250,000,000 | 80,000,000 | 300,000 | hard_stop |
| `agent_team` | Agent Team | 显式 Agent Team / teammate run | 16 | 4 | 16 | 96 | 24 | 500 | 80,000,000 | 16,000,000 | 250,000 | require_confirmation |

解释：

- `scheduled` 允许几十个 subagent，足够覆盖常规扫描、检索、验证，但默认不会允许 100-200 个 worker 的大规模 fanout。需要这种规模时，应该把任务建成 Dynamic Workflow 或由管理员给特定 trigger 覆盖策略。
- `workflow` 默认允许 100-200 个 subagent，因为 WorkflowDefinition 预先声明 fanout、barrier、leaf call 和结果汇总；但它同时有更高的 token/cache-miss/provider-call 上限和 hard_stop。
- `agent_team` 允许 3-4 个 teammate session。Team member 本身消费 `team_sessions`；如果 teammate session 内部又调用普通 `spawn_subagent`，那个 worker 才消费 `subagents`。
- `interactive` 允许少量 Agent Team，因为 Team 常见规模是 3-4 个可进入 teammate session；这不应挤占 `subagents`。
- 蒸馏/heartbeat/skill distiller 不在本表。它们如果已有独立预算或内部节流，继续沿用独立 lane；不要为了本轮控制台把它们压进 Sub-agent / Agent Team / Workflow。
- 大规模 workflow 不需要第二种 Workflow runtime。它仍然是同一条 `propose_dynamic_workflow` / `preview_workflow` / `start_workflow` 路径，只是这次 run 绑定了更高的 workflow policy 或公司 override。

### 9.1.1 大规模 Workflow 不是第二种 Workflow

“高预算 Workflow”不能作为产品概念或 runtime type。Hive 只有一个 Workflow runtime：

```text
propose_dynamic_workflow
→ lowered WorkflowDefinition
→ preview_workflow
→ start_workflow
→ RuntimeTask(task_type="workflow")
```

所谓“大规模 workflow”只表示 **同一个 Workflow runtime 使用了更高的 runtime budget policy**。例如：

| Policy label | 适用场景 | max_subagents | max_provider_calls | max_tokens | max_cache_miss_tokens |
| --- | --- | ---: | ---: | ---: | ---: |
| workflow default | 常规 workflow fanout | 256 | 2,000 | 250,000,000 | 80,000,000 |
| workflow elevated override | 管理员批准的大规模审计 / 研究 | 1,000 | 8,000 | 1,000,000,000 | 250,000,000 |

这里的 `workflow elevated override` 是 policy label，不是 profile、不是新工具、不是第二套 UI。它可以通过公司策略、特定 workflow definition、特定 trigger，或一次性 approval 绑定到某个 root run。

### 9.2 Cache-miss 的计量方式

`cache_miss_tokens` 不按单 agent 单独封顶，而是按 root run 聚合：

```text
root_budget_run.cache_miss_tokens =
  sum(provider_call.cache_miss_tokens or provider_call.prompt_token_estimate_when_unknown)
```

展示层可以按下面维度归因：

```text
by parent agent
by subagent run
by team member session
by workflow leaf
by provider/model
```

但 admission 只能看 root run 总量。否则一个 parent 只要不断创建 fresh subagent / fresh Team member，就会把 cache-miss 风险分散成很多“小账本”，重新打开绕过口。

### 9.3 Company override 执行语义

公司策略可以覆盖平台默认值，覆盖粒度从粗到细：

```text
tenant default
source/profile default
agent-specific
trigger-specific
agent + trigger specific
workflow-specific override / one-run approval
```

执行规则：

- 更具体的 enabled policy 胜出。
- 同一粒度内 priority 高者胜出。
- policy 在 root run 创建时 snapshot；运行中的 root run 不因后台修改 policy 改变 admission 语义。
- override 不能把安全系统关掉，只能在 `observe` / `enforce` 和数值阈值上调整；平台级 emergency kill-switch 可以把 tenant 临时降到 observe，但必须记录事件。

### 9.4 Profile 选择规则

| 场景 | Profile |
| --- | --- |
| 用户在 web chat 发起一条普通消息 | `interactive` |
| cron / interval / once / poll 触发器直接唤醒 agent | `scheduled` |
| trigger 携带 workflow_ref，或 agent 调用 `start_workflow` | `workflow` |
| 明确批准的大规模研究 workflow | `workflow` + elevated policy override |
| heartbeat / T2->T3 / soul / skill distillation 等自进化维护 | 不进入本轮三类控制；沿用独立 heartbeat/evolution lane |
| 显式 Agent Team root run | `agent_team` |
| subagent / delegation / team member 由已有 root run 创建 | 不创建新 profile，继承 parent root budget |

如果主 Agent 在 `scheduled` profile 下尝试启动 100-200 个 subagent，默认应被 budget admission 拦住，并提示它改为总结当前进展、请求管理员批准、或把任务转成同一套 Workflow runtime。管理员可以给这个 workflow run 或这个 trigger 绑定 elevated policy，但这不产生第二种 Workflow。

## 10. Circuit Breaker

circuit breaker 应在每次 settlement 后、每次 continuation wake 前检查。

触发条件：

```text
used_tokens + reserved_tokens >= max_tokens
used_cache_miss_tokens + reserved_cache_miss_tokens >= max_cache_miss_tokens
used_llm_calls >= max_llm_calls
used_subagents >= max_subagents
used_team_sessions >= max_team_sessions
used_delegations >= max_delegations
used_background_tasks >= max_background_tasks
continuation_wakes >= max_continuation_wakes
parent_invocations >= max_parent_invocations
failures >= max_failures
needs_reconciliation_count >= max_needs_reconciliation
child_failure_ratio >= max_child_failure_ratio
wall_clock exceeded
```

动作：

```text
hard_stop:
  no more provider calls or RuntimeTask creation
  cancel pending/unclaimed RuntimeTask under the same budget_run
  running work follows policy: drain current provider call, cancel at next safe checkpoint, or immediate cancel for replay-safe work

summary_only:
  one final parent invocation may summarize current state
  work-amplifying tools are disabled

require_confirmation:
  pause and create a checkpoint/approval request
```

hard_stop 的止血目标是同时处理增量和存量：

- 新 work：admission 立即 deny。
- pending/unclaimed work：同一 `budget_run_id` 下的 `pending` / `resumable` / unclaimed queued tasks 立即标记 `killed` 或 `cancelled`，写 terminal reason。
- claimed/running work：按 policy 决定 drain 或 cancel；无论哪种，都必须禁止 running work 再创建新 work。
- completion signal：同一 `budget_run_id` 的后续 wake 进入 summary-only 或直接丢弃并写 event。

## 11. 强制接入点

| Surface | Enforcement |
| --- | --- |
| Runtime root creation | 创建 budget run |
| `spawn_subagent(run_in_background=true)` | enqueue 前 reserve subagent/background/tokens |
| foreground `spawn_subagent` | 执行前 reserve subagent/tokens |
| Agent Team member spawn | 创建 teammate session 前 reserve team_sessions/background/tokens |
| `delegate_to_agent` | enqueue 前 reserve delegation/background/tokens |
| local-agent delegation enqueue | enqueue 前 reserve delegation/background/tokens |
| `send_agent_session_message` | 唤醒/继续 child work 前 reserve continuation/tokens；如会排队后台执行则 reserve background_tasks |
| `AgentKernel` provider call | request 前 reserve LLM call/tokens/cache-miss |
| provider response | settle actual usage |
| `subagent_wake_consumer` | parent wake 前检查 breaker |
| `continue_parent_session_with_task_notification` | 遵守 `summary_only` mode |
| parent wake runtime prompt | 注入 Run Control Contract，要求 continue/complete/fail 决策 |
| `workflow_admission` | 使用 shared budget admission |
| `runtime_task_worker` claim | 不 claim exhausted/cancelled budget 的 task |
| restart reconciliation | 保留 budget state 和 terminal reason |

## 12. 可观测性与 Admin UI

Runtime budget 应成为控制中台的一等 surface。

Ownership split：

- **Runtime / Agent Framework core**：enforcement、default policy、atomic reservation、fail-mode、reaper、hard_stop 必须是平台框架内置兜底能力。它不能依赖前端页面、tenant 是否配置过 quota、或 admin 是否打开过某个开关。没有 UI 配置时也必须按内置 default policy 保护 tenant。
- **Company Admin / Enterprise Settings**：这是 tenant admin 的主要配置面。它负责查看和调整 runtime budget policy、profile 默认值、agent/trigger overrides、approve-overrun、以及当前 tenant 的 budget run 可见性。它应该进入公司后台，而不是藏在平台后台。
- **Platform Admin / Operator Console**：这是平台方运维面。它负责跨 tenant observe/enforce rollout、tenant-level emergency kill-switch、异常租户榜单、reaper health、budget service degradation、以及事故处置。
- **Agent Detail / Session Timeline**：只做当前 agent / session / run 范围内的用户可理解状态说明和局部操作，不作为 tenant-wide policy 的主要配置入口。普通用户默认只看到“现在是不是正常、影响了什么、为什么停了、下一步怎么办”。内部 budget 维度、policy match、reservation/settlement、runtime ids 等排障信息只进入 admin/support 的技术详情，不放在默认主界面。它们不能编辑 tenant default policy、profile defaults、全局 observe/enforce、或 kill-switch。

因此它不是单纯的 GuardPolicy，也不是单纯的 quota UI。GuardPolicy 管“能不能做某类动作”，Runtime Budget 管“允许一次 autonomous chain 放大到多少”。两者都属于治理，但 Runtime Budget 的 enforcement 边界属于 runtime core。

Backend APIs：

```text
GET /api/runtime-budgets/policies
POST /api/runtime-budgets/policies
PATCH /api/runtime-budgets/policies/{id}
GET /api/runtime-budgets/runs
GET /api/runtime-budgets/runs/{id}
GET /api/runtime-budgets/runs/{id}/events
POST /api/runtime-budgets/runs/{id}/cancel
POST /api/runtime-budgets/runs/{id}/approve-overrun
```

UI surfaces：

- Enterprise settings / Company Admin：新增 Runtime Budgets section，位置靠近现有 Quotas / Approvals / Audit，而不是塞进 Tools。它管理 tenant runtime policies、profile defaults、agent/trigger overrides、observe/enforce 状态、approve-overrun 入口。
- Agent detail runtime panel：当前运行状态、影响范围、用户可理解原因、下一步操作、管理员处理入口；默认不展示 raw budget dimensions / runtime ids。
- Platform Admin dashboard：top budget denials、cache-miss spikes、runaway fanout、pending approvals、tenant kill-switch、observe/enforce rollout、reaper health。
- Session timeline：用用户语言展示本次运行的关键状态变化，例如“已开始”“已暂停以避免继续消耗额度”“需要管理员批准后继续”“已停止未启动的后续任务”。

Company Admin 可调项应按用户能理解的策略线表达，不直接暴露内部 root kind：

| UI 分组 | 对应 profile | 公司可调项 | 默认说明 |
| --- | --- | --- | --- |
| 日常运行保护 | `interactive` / `scheduled` | 子任务上限、续跑上限、调用次数、总 token、非缓存 token、失败后处理方式 | 默认已开启；用于防止普通对话和定时任务扩散。 |
| Dynamic Workflow | `workflow` | workflow 子任务上限、调用次数、总 token、非缓存 token、是否允许更高预算 override | 显式 workflow 才进入；大 fanout 走这里。 |
| Agent Team | `agent_team` | team member 上限、team run 调用次数、总 token、非缓存 token、是否需要确认 | 显式 Agent Team 才进入；team member 不算子代理。 |

覆盖规则在 UI 上要表达成“公司策略优于平台默认”，而不是“手动开启保护”。保存 tenant policy 只是创建覆盖配置；没有覆盖配置时，平台内置默认仍然按 `enforce` 生效。

Agent Detail 的“局部”默认面向小白用户，只围绕当前 agent 回答四个问题：

- **现在状态**：正常运行、正在处理、已暂停、需要管理员处理、已停止、已完成部分结果。
- **影响范围**：影响的是本次对话、某个定时触发器、某次后台扫描，还是这个 agent 的后续自动运行。
- **原因**：运行额度已达上限、后台任务异常增多、连续失败过多、触发器运行过于频繁、管理员暂停、平台保护机制已介入。
- **下一步**：等待额度恢复、请求管理员批准继续、降低触发频率、查看已完成结果、联系管理员处理。

Session Timeline 的“局部”默认是用户可读的运行日志，不是内部 budget ledger：

- `09:30` 定时扫描开始。
- 系统发现本次运行消耗异常，已暂停继续扫描。
- 已保留当前完成结果，未启动的后续任务已停止。
- 需要管理员批准后才能继续。
- 管理员已批准继续一次，系统恢复运行。

Admin/support 技术详情可以展开，但不能作为普通用户默认视图：

- `budget_run_id` / `runtime_task_id` / trigger / workflow / subagent lineage。
- tokens、cache-miss tokens、subagents、delegations、wake continuations、background tasks 等内部维度。
- matched policy、reserve / settle、would-deny、hard_stop、pending-work cancellation 等事件原文。

不属于 Agent Detail / Session Timeline 的内容：

- tenant default policy、profile defaults、agent/trigger override 的完整编辑。
- tenant-wide observe/enforce 切换。
- tenant emergency kill-switch。
- 跨 agent / 跨 tenant 的异常榜单、reaper health、budget service degradation。

Alerts：

- cache-miss spike
- subagent fanout spike
- reconciliation spike
- trigger run exceeds normal profile
- budget deny rate exceeds threshold

## 13. 迁移计划

这是 runtime safety system，应该一次性设计完整，但上线要可控。

### Step 1：Schema 与 service skeleton

- 新增 budget tables 和 model。
- 新增 `RuntimeBudgetService`。
- 新增 default policy resolver。
- 新增 append-only event writer。
- 固化 `root_run_kind/root_run_key`，并测试“同一用户消息续跑共享预算、下一条用户消息新建预算”。
- 固化 reservation estimate contract，初始值由生产计量反推，不允许小常数占位。
- 新增 budget reaper/reconciliation skeleton，过期 run 和悬挂 reservation 必须可回收。
- policy 支持 `enforcement_mode: observe | enforce`。
- reservation 必须使用 atomic conditional update / row lock / advisory lock，不允许 read-then-write。
- 固化 budget service 降级语义：autonomous/background fail-closed，interactive direct response fail-open + alert + 禁用 work-amplifying tools。
- 补 policy resolution、atomic reservation、settlement、fail-mode 单元测试。

### Step 2：Root run attachment

- web chat、trigger、heartbeat、workflow、Agent Team、subagent root 都挂 budget run。
- 在 runtime context metadata 中传递 `budget_run_id`。
- 给所有 tenant 安装 default runtime policies；没有手动 quota 的 tenant 也必须被默认 policy 保护。
- 安装内置 profile 默认值：`interactive` / `scheduled` / `workflow` / `agent_team`。
- 固化 profile 选择规则：100-200 subagent fanout 默认只能走 `workflow` + elevated policy override，不能落到普通 `scheduled`。
- 测试每个 executable root 都有 budget。

### Step 3：LLM call reservation

- 在 `AgentKernel` 或 invoker boundary 加 provider-call preflight。
- provider call 前 reserve estimated call budget。
- provider response 后 settle actual usage。
- 保留现有 `token_usage_events`。

### Step 4：Subagent admission

- background subagent enqueue 前 admission。
- foreground subagent execution 前 admission。
- Agent Team member session 创建前 admission，消费 `team_sessions`，不得消费 `subagents`。
- `delegate_to_agent` / local-agent delegation enqueue 前 admission。
- `send_agent_session_message` 对 continuation / wake / queued background work 做 budget admission。
- 持久化完整 `subagent_spec`。
- 补 fanout cap、team session cap、delegation cap、spec preservation 测试。

### Step 5：Parent wake circuit breaker

- 在 `subagent_wake_consumer` 加 budget check。
- parent wake 注入 Run Control Contract，要求主 Agent 明确选择继续、完成、失败/阻塞并停止。
- 记录 parent decision event，用于 timeline 和 admin/support 排障。
- 增加 repeated subagent intent / no-new-evidence breaker。
- 增加 `summary_only` continuation mode。
- summary-only 下禁用 work-amplifying tools。
- 补 failure/reconciliation breaker 测试。
- **复发防护验收点：到 Step 5 结束时，scheduled trigger + background subagent fanout + parent wake loop + default policy 已经具备 hard guard。**
- 这个验收点不等待 UI、backfill、admin visibility 完成；后续步骤增强统一化和可观测性，但不能成为护栏延迟生效的理由。

### Step 6：Workflow unification

- workflow admission 预算检查接入 shared budget service。
- 保留现有 workflow static checks。
- workflow leaf 中调用的 Mix Sub Agent 消费 `subagents`；Agent Team lane 消费 `team_sessions`。
- 补 workflow 与 subagent/team session 共享 budget root、但维度分开的测试。

### Step 7：UI 与 ops visibility

- 增加 budget run APIs。
- 增加 Enterprise Settings / Company Admin 的 Runtime Budgets section：tenant policies、profile defaults、agent/trigger overrides、observe/enforce、approve-overrun。
- 增加 Platform Admin runtime budget ops panel：tenant kill-switch、observe/enforce rollout、异常租户榜单、reaper health、budget service degradation。
- 增加 Agent Detail runtime panel：只解释当前 agent 的运行保护状态、影响、原因和下一步；默认不展示 raw budget dimensions、runtime ids、policy attribution、reservation/settlement。
- 增加 Session Timeline 用户事件：用自然语言展示“已开始”“已暂停以避免继续消耗额度”“需要管理员批准后继续”“已停止未启动的后续任务”；内部 reserve / would-deny / settlement 只进入 admin/support 技术详情。
- 增加 alert queries。

### Step 8：Production backfill 与 rollout verify

- 对近期 runtime task 做 best-effort budget run grouping，仅用于可观测性。
- 保留 legacy quota 作为 account-level caps。
- 先以 `enforcement_mode=observe` 上线：照常 reserve/settle/record deny，但不阻断正常 work。
- 用 production readout 验证 observe 模式不会误杀正常 trigger/workflow，再逐 tenant 切 `enforce`。
- 增加 tenant-level emergency kill-switch：一键把该 tenant runtime budget policy 降回 observe，供误杀时应急。
- hard enforcement 前置条件：observe 数据稳定、reaper 生效、hard_stop 能取消 pending/unclaimed work、kill-switch 已验证。

## 14. 测试计划

新增测试文件：

```text
backend/tests/services/test_runtime_budget_service.py
backend/tests/services/test_runtime_budget_policy.py
backend/tests/services/test_runtime_budget_atomic_reservation.py
backend/tests/services/test_runtime_budget_root_run_boundary.py
backend/tests/services/test_runtime_budget_estimates.py
backend/tests/services/test_runtime_budget_reaper.py
backend/tests/services/test_runtime_budget_fail_modes.py
backend/tests/services/test_subagent_budget_admission.py
backend/tests/services/test_agent_team_budget_admission.py
backend/tests/services/test_delegation_budget_admission.py
backend/tests/services/test_subagent_wake_budget_breaker.py
backend/tests/services/test_run_control_contract.py
backend/tests/runtime/test_runtime_budget_context.py
backend/tests/runtime/test_workflow_budget_unification.py
backend/tests/kernel/test_runtime_budget_enforcement.py
backend/tests/api/test_runtime_budget_api.py
```

必测用例：

```text
policy resolution chooses most-specific enabled policy
one interactive user message and its parent wakes share one budget_run
next independent user message in the same session creates a new budget_run
one trigger fire and all caused work share one budget_run
reservation fails when no budget remains
reservation estimate uses observed floor and is not a tiny constant
concurrent reservations cannot overspend the same budget_run
multi-dimension reservation is all-or-nothing
reservation retry with the same reservation_key is idempotent
reservation is released on enqueue failure
expired active budget_run releases or cancels dangling reservations
hard_stop cancels pending/unclaimed RuntimeTask under the same budget_run
observe mode records would-deny but allows work
enforce mode denies work
tenant kill-switch downgrades enforcement to observe
settlement applies actual usage and appends event
budget service unavailable fail-closes scheduled/background/heartbeat/workflow work
budget service unavailable fail-opens direct interactive LLM response with degraded flag and alert
provider call is blocked before LLM request when budget is exhausted
background subagent is not enqueued without budget context
subagent cap rejects excess fanout
Agent Team member session consumes team_sessions and does not consume subagents
team_sessions cap rejects excess teammate sessions
distillation direct LLM calls are not counted as subagents
distillation direct LLM calls do not receive spawn_subagent tool schemas
delegate_to_agent is not enqueued without budget context
delegation cap rejects excess A2A fanout
workflow leaf Mix Sub Agent consumes subagents under the workflow root budget
scheduled profile rejects 100-200 subagent fanout unless workflow + elevated policy override is used
parent wake enters summary-only after child failure threshold
parent wake requires a continue/complete/fail decision event
repeated subagent intent with no new evidence trips summary-only or blocked mode
summary-only mode disables work-amplifying tools
workflow fanout and subagent fanout consume the same root budget while preserving distinct dimensions
cache-miss tokens are aggregated at root_run even when attributed by child run
tenant/user/agent quota still blocks even when runtime budget remains
runtime budget still blocks even when account quota is unset
restart reconciliation preserves budget terminal reason
```

验证命令：

```bash
cd backend
source .venv/bin/activate
pytest tests/services/test_runtime_budget_service.py \
       tests/services/test_runtime_budget_policy.py \
       tests/services/test_runtime_budget_atomic_reservation.py \
       tests/services/test_runtime_budget_root_run_boundary.py \
       tests/services/test_runtime_budget_estimates.py \
       tests/services/test_runtime_budget_reaper.py \
       tests/services/test_runtime_budget_fail_modes.py \
       tests/services/test_subagent_budget_admission.py \
       tests/services/test_agent_team_budget_admission.py \
       tests/services/test_delegation_budget_admission.py \
       tests/services/test_subagent_wake_budget_breaker.py \
       tests/services/test_run_control_contract.py \
       tests/runtime/test_runtime_budget_context.py \
       tests/runtime/test_workflow_budget_unification.py \
       tests/kernel/test_runtime_budget_enforcement.py \
       tests/api/test_runtime_budget_api.py -q
```

相关回归：

```bash
cd backend
source .venv/bin/activate
pytest tests/services/test_subagent_run_service.py \
       tests/runtime/test_workflow_admission.py \
       tests/runtime/test_invoker.py \
       tests/kernel/test_engine.py \
       tests/services/test_runtime_task_worker.py -q
```

## 15. 产品语义

预算停止执行时，不应该对用户说模型“失败”，也不应该默认展示内部事件名。普通用户主界面只表达结果、原因和下一步：

- **结果**：这次运行已暂停 / 已停止 / 已完成部分结果 / 需要管理员处理。
- **原因**：运行消耗超过本次保护额度 / 连续失败较多 / 后台任务异常增多 / 定时任务触发过于频繁 / 管理员已暂停。
- **影响**：已完成的结果会保留；未开始的后续任务不会继续消耗额度。
- **下一步**：等待额度恢复、让管理员批准继续、降低触发频率、或查看已完成结果。

默认用户文案示例：

```text
这次运行已暂停，以避免继续消耗额度。
已完成的结果已保留，未开始的后续任务已停止。
如果需要继续，请联系管理员批准本次运行。
```

内部状态到用户文案的映射：

| Internal state | User-facing wording |
| --- | --- |
| `budget_exhausted` | 运行额度已达上限 |
| `summary_only` | 已暂停继续执行，只保留结果总结 |
| `hard_stop` | 系统已停止未开始的后续任务 |
| `would_deny` in observe mode | 系统检测到这次运行接近保护阈值 |
| `denied` | 这次运行未启动，因为缺少可用运行额度 |
| `needs_admin_approval` | 需要管理员批准后继续 |
| `budget_service_unavailable` | 运行保护系统暂时不可用，自动任务已暂停 |

`budget_run_id`、`runtime_task_id`、reservation、settlement、cache-miss、subagent fanout 等术语只进入 admin/support 技术详情和日志，不进入普通用户默认文案。

给 LLM 的 runtime message 应该精确：

```text
[Runtime Budget] This run has reached its background worker budget.
You may summarize completed work, but you may not call spawn_subagent, start_workflow, delegate_to_agent, or other work-amplifying tools unless the user approves more budget.
```

## 16. 待讨论决策

已拍板、不可降级为实现细节的决策：

- **两层治理模型**：主 Agent 行为层负责继续/完成/失败判断；平台硬保护层负责 root-run fanout/token/cache-miss/wake/provider-call 上限。两层都必须实现，不能用 prompt 替代 hard guard，也不能用 hard guard 替代主 Agent 的终止契约。
- **Run Control Contract**：autonomous root run 和 parent wake 必须注入明确的 objective、success criteria、failure/stop criteria、remaining guard 和 decision required。parent wake 必须产出 continue / complete / fail_or_block decision event。
- **Subagent / Workflow / Agent Team / 记忆蒸馏边界**：普通 Mix Sub Agent 和 workflow 内 Mix Sub Agent 都消费 `subagents`；Agent Team member 是完整 teammate session，消费 `team_sessions`；Summary Agent、Learning Brain、Memory Gate Agent、T3/Heartbeat Curator、Dream/Soul Writer、Skill Distiller 这类蒸馏 actor 是 direct LLM call / curator，不消费 `subagents`，也不进入本轮公司后台控制分类；三者不能混算。
- **Cache-miss root-run 聚合**：cache-miss budget 按 root run admission，不能按每个 agent / subagent / team member 单独重置。UI 可做归因展示，但 admission root 不变。
- **默认 profile 数值已作为初始内置保护拍板**：`interactive`、`scheduled`、`workflow`、`agent_team` 使用 §9.1 初始默认值；production data 只负责校准这些默认，不得让默认保护退化成空值或无限。Heartbeat / distillation 保持现有独立 lane，不进入本轮三类公司控制项。大规模 workflow 通过 elevated policy override 表达，不是新 profile。
- **跨 agent delegation 的 budget root ownership**：`delegate_to_agent`、local-agent delegation、以及后续 `send_agent_session_message` 唤醒/排队后台执行时，必须继承发起方当前 root `budget_run_id`，不得为目标 agent 创建 fresh root budget。唯一例外是没有发起方 active chain 的 external/standalone delegation ingress，此时它自己就是 root，使用 `root_run_kind=delegation_run`。目标 agent 仍保留自己的 identity、permission、audit 和 transcript 归属，但 work-amplification 预算归属于发起 run。原因：如果 B 作为独立 digital employee 自动另起 root budget，A→B→A 或 A→B→C 循环会在每次 delegation 时重置预算，重新打开无限 fanout / continuation 绕过口。若未来产品需要展示 B 的独立成本，可在同一个 root budget 下增加 target-agent cost attribution，不得改变 admission root。
- **interactive 降级时 foreground subagent 仍 fail-closed**：budget service unavailable 不是预算耗尽，而是控制中台不可判定安全性。direct LLM response 可以 fail-open 以保证真人会话连续性；但 foreground/background `spawn_subagent`、`delegate_to_agent`、workflow start 等 work-amplifying action 必须 fail-closed。这个边界是预期行为，不应在事故排查时被当成可自动修复的工具失败。
- **root run boundary**：`budget_run` 绑定一次 active continuation chain。一次 trigger fire 一个 budget_run；一条用户消息及其引发的 subagent/delegation/wake/workflow leaf 一个 budget_run；同一 chat session 的下一条独立用户消息新建 budget_run。parent wake 不因新 invocation 创建 fresh budget。
- **reservation estimate contract**：estimate 是安全边界，必须用真实成本观测初始化和校准；不能用小常数占位。scheduled/background 默认 reservation 至少使用生产真实成本 p50，风险较高 profile 使用 p75 或更高。
- **budget lifecycle / reaper**：`expires_at` 必须被 daemon/reaper 消费；过期 active run、无 owner in-flight reservation、crashed worker claim、cancelled-before-claim task 都必须释放或终止，不能让 reserved budget 永久挂起。
- **hard_stop handles existing queued work**：hard_stop 不只禁止新增；同一 `budget_run_id` 下 pending/unclaimed work 必须被取消或标 terminal，running work 按 policy drain/cancel 且不得继续放大。
- **rollout safety**：budget policy 必须支持 `enforcement_mode: observe | enforce`；上线先 observe 记录 would-deny，再逐 tenant enforce；必须有 tenant-level emergency kill-switch 降回 observe。
- **frontend ownership**：Runtime Budget enforcement 是 Agent Framework / runtime core 的内置兜底能力，不依赖前端配置；Company Admin 必须提供 tenant-facing Runtime Budgets 配置页；Platform Admin 只承载平台方 ops/kill-switch/rollout；Agent Detail 和 Session Timeline 只做局部可见性。

实现前仍必须用生产数据校准：

1. §9.1 默认值是否需要按真实 production p50/p75 上调或下调。
2. 特定 agent / trigger 是否需要公司级 override，而不是修改平台默认。
3. `default_child_token_reservation`、`default_llm_call_token_reservation`、`max_cache_miss_tokens` 的 calibration window 和更新频率。

可延后到对应 Step 前收敛的工程细节：

1. `send_agent_session_message` 精确消费哪些维度：Step 4 前定。
2. workflow 的 agent step 动态 spawn subagent 如何继承 `budget_run_id`：Step 6 前定。
3. `budget_run_id` 在 runtime context 中的具体载体：`SessionContext` / `InvocationRequest` / `RuntimeTask.metadata_json` 的组合在 Step 2 前定。
4. cache-miss budget 对不暴露 cache metrics 的 provider 如何展示 uncertainty。
5. reservation 与 actual usage 之间允许多少 overrun tolerance。
6. owner/admin 是否能从 chat、admin UI 或两者批准 overrun。
7. 大规模研究在 fanout plan 已知时是否默认必须走 Dynamic Workflow。
8. session timeline 里如何展示 budget stop，避免普通用户误以为 agent crash。
9. 现有 `quota_tokens_per_day/month` UI 继续放在 User Management，还是迁到统一 Budget Settings。

## 17. 验收标准

实现完成必须同时满足：

- 每个 executable root runtime 都有 budget run。
- budget run 边界是 active continuation chain；同一用户消息的 wake 共享预算，同一 session 下一条独立消息新建预算。
- `root_session_id` 不再被误用为 budget lifetime boundary；预算边界由 `root_run_kind/root_run_key` 决定。
- autonomous root run 和 parent wake 都注入 Run Control Contract。
- parent wake 必须记录 continue / complete / fail_or_block decision event。
- 每次 provider call 都在 request 前 reserve budget。
- 每次 provider response 都 settle actual usage。
- reservation estimate 由生产成本观测初始化和校准，不能用脱离真实成本的小常数。
- reservation 是并发安全的 atomic conditional update，不能通过并发 worker 超卖任何预算维度。
- 过期 budget run 和悬挂 reservation 会被 reaper/reconciliation 回收。
- budget service 故障时，autonomous/background work fail-closed；direct interactive response 的 fail-open 必须可观测并禁用 work-amplifying tools。
- background subagent enqueue 在没有可用 run budget 时被阻止。
- Agent Team member session 消费 `team_sessions`，不会挤占或绕过 `subagents`。
- distillation actor 是 direct LLM call / curator，不消费 `subagents`，也不接收 `spawn_subagent` tool schema。
- workflow leaf 中的 Mix Sub Agent 消费 `subagents`，并继承 workflow/root budget。
- scheduled profile 默认会阻止 100-200 subagent fanout；这类任务必须使用 workflow + elevated policy override 或公司 override。
- cache-miss tokens 作为 root-run 硬预算聚合，不能按 child agent 重置。
- `delegate_to_agent` / local-agent delegation enqueue 在没有可用 run budget 时被阻止。
- parent wake 在 child failure / reconciliation spike 后不能无限 continuation。
- repeated subagent intent / no-new-evidence loop 会进入 summary-only 或 blocked。
- hard_stop 会取消同一 budget 下 pending/unclaimed 的存量 RuntimeTask，不只是不再创建新 task。
- workflow 与 subagent 使用同一个 budget service。
- tenant/user/agent quota 继续作为 account-level outer cap 生效。
- 即使没有手动配置 quota，runtime default policy 也能保护 tenant。
- runtime budget core 在没有任何 UI 配置时仍按 default policy enforce/observe；前端不能成为安全边界。
- Company Admin 有 tenant-facing Runtime Budgets 配置面；Platform Admin 有 operator-facing rollout/kill-switch/incident 面；Agent Detail 和 Session Timeline 能解释具体 run 的预算状态。
- Step 5 完成时，scheduled trigger + background subagent fanout + parent wake loop 的复发防护已经生效，不等待 UI/backfill。
- enforce 上线前必须经过 observe 模式 production readout；tenant-level kill-switch 必须验证可用。
- budget denial 和 circuit breaker 在 API、timeline、admin UI 中可见。
- production 能回答：哪次 run 花了 tokens、为什么被允许、什么时候停止、是哪条 policy allow/deny。

# 执行 / 自动化全面对标 CC — 砍自创杂物、收敛回基线、明确保留有意 delta

> **单一核心**:Hive 在「执行 / 自动化」这条线上反复自创了 CC(Claude Code)根本没有的概念(`trigger_type` / `objective` / `focus.md` / `long_task` / `intent 分类` / 第二套 task 系统 …)。每个单看「有点道理」,合起来是层层不必要抽象。本文把这条线整体拉回 CC 基线:**自动化任务的本质只有一个 call**(一次 `invoke_agent` 的 agent loop),触发 / plan 确认 / task / subagent 都只是「何时、用什么 prompt、谁来起这个 call」。
>
> **范围**:本文从触发系统起步(v0 只覆盖 trigger),现扩为**执行 / 自动化全线审计**:① 触发系统 ② Plan Mode ③ Task ④ Subagent。
>
> **状态**:v1 草案,待 owner 在文档上拍板后实现。对标基线 [[project_hive_cc_superset]](Hive = CC 加强版,先对标 CC 基线再谈 delta);遵循 [[feedback_foundation_first_doc]](先文档后叠加)、[[feedback_no_mvp_finish_completely]](单次完整、禁 MVP)。
>
> **证据**:本轮三个调研 agent 全源码实证(Hive 后端 `backend/app/` + CC 源码 `/Users/rocky243/Context Engineering/claude-code-org/src/`),所有论断带 `file:line`。

---

## 0. 结论先行 + 审计框架

Owner 判断(2026-06-08):**「Plan Mode、Task、Subagent 全部过一遍,特别是执行任务、自动化任务,全部对标 CC。」** 核对 CC 源码后,这条判断在四块上全部成立 —— CC 把「执行 / 自动化」做得极简,Hive 一路加厚。

### 0.1 审计框架:对标 CC ≠「CC 没有的全砍」

关键纪律 —— 不能把 North Star 能力当杂物砍。每个「CC 没有」的概念落进三类之一:

| 类别 | 判据 | 处置 |
|------|------|------|
| **砍(自创杂物)** | CC 无 + **不服务** [[project_hive_north_star]] Goal 1/2 + 是「把简单的事复杂化」的层层抽象 | 删除 / 退役 |
| **收敛(同概念塌缩 / 归位)** | CC 有对应物,但 Hive 实现发散、重复、或多套并行 | 塌缩成一套,对齐 CC 语义 |
| **保留(有意 delta)** | CC 无,但**明确服务 North Star** Goal 1(self-evolution)/ Goal 2(控制中台),且符合 AI-Native L2(只 scope 权限 / 加治理,不降智) | 保留,但文档**明确标为 Hive delta 而非「CC 对齐」** |

> 这个三分是本文的灵魂。owner 的「砍自创」直觉针对的是**第一类**(trigger/objective/focus/long_task/intent taxonomy 这些把「给 agent 一个 prompt」复杂化的抽象);**第三类**(plan 治理信封、record_finding、subagent 进化闭环、peer delegation)是 Hive 存在的理由本身,对标 CC 时要**保留并诚实标注距离**,不能误砍。

### 0.2 四块一句话结论

| 块 | CC 基线 | Hive 现状 | 主要动作 |
|----|---------|-----------|----------|
| **① 触发** | 触发 = 往 agent 输入流喂一个 prompt;无类型系统(`trigger_type` 零命中) | 5 类型平铺 + objective 自带唤醒 + focus.md + reconciler | **砍** objective/focus/reconciler;**收敛** 5 类型→三桶 |
| **② Plan Mode** | plan 确认 = 权限模式切换 + 同 loop 继续;零 intent/action 分类(全零命中) | INTENT_TYPES(5)+ ACTION_KINDS(6)+ 状态机 + 信封 | **砍** 孤儿 intent + long_task;**收敛** intent taxonomy→continue-same-loop;**保留** 治理信封 |
| **③ Task** | TodoWrite + Task 板,create≠execute,owner/blocks/blockedBy | **两套**:Work Ledger(已对齐✅)+ 业务 DB Task(第二套) | **砍** supervision type;**收敛** 业务 DB Task 第二套;**保留** record_finding |
| **④ Subagent** | Task/Agent 工具,body 替换,memory 自写 + 通用 nightly,**从不改 definition** | spawn/delegate 两轴,12 项已对齐✅ + 进化闭环 | **收敛** fanout/ForkLevel;**保留** 进化闭环 = Goal 1 self-evolution(owner 已拍) |

---

## 0.3 实施进度(在 main 上逐 commit 推进,2026-06-08)

> owner 拍板:直接在 main 改、每部分一个 commit、每步更新本文带证据。main 本地推进,未 push。long_task 区分两层:plan intent(本线收敛)vs `long_task_runtime.py`(deep research 异步基建,**不动**)。

| # | 内容 | 关键改动 | 证据 |
|---|------|---------|------|
| 0 | 设计文档 v1 落地 | 四块审计 + 砍/收敛/保留三分框架 | `cd50356d` |
| 1 | 砍 `external_action` / `state_change` 孤儿 plan intent | `INTENT_TYPES` 5→3;`_INTENT_HANDOFF_TARGET` 去 2 条;`plan_request` 注释 | red-first(`test_intent_types` 先红)→535 plan/intent/handoff/dr 测试绿 |
| 2 | **重命名归位** plan intent `long_task` → `in_session_execution`(owner 拍 A) | `INTENT_TYPES`/`_ACTION_INTENT`/`_INTENT_HANDOFF_TARGET` + handlers/deep_research/web_chat;alembic data migration backfill 存量;**保留** legacy target word + `long_task_runtime` + `start_long_task` action_kind 名 | 全量 4057 绿;migration head 测试更新;保留 intent-match Goal-2 治理 |
| 3 | **plan handoff 解开 objective**:`objective_trigger` → `scheduled_trigger` 直连 | `plan_mode_handoff.py` 整重写=从 plan `wake_policy` **直接建 `scheduled_job` cron trigger**(盖 `config.plan_id` backstop,零 AgentObjective);`autonomous_wake` intent 重指 `scheduled_trigger`;**detached 桩补成真 once**(§7 step1:`force_once` 复用同机器);registry/web_chat/handlers/plan_mode `create_objective` 字段去除;plan_mode_handoff 自此**脱离 objective 爆炸半径** | red-first(handoff/e2e 重写)→ 130 plan-mode 测试绿 + web_chat/gate 84 绿;e2e 断言 `session.objectives == []` |

**C3 净效果**:plan 确认后只剩三条干净 handoff —— `continue_current_session`(当前会话)/ `scheduled_trigger`(周期 cron,从 plan 直建)/ `detached→once`(后台一次),全部不经 objective。下一步 objective 子系统可整体退役(已无 plan 客户)。

| # | 内容 | 关键改动 | 证据 |
|---|------|---------|------|
| 4 | **objective 子系统退役 + focus.md 投射停**(§7 step2 合并;二者技术不可分=focus.md 是 objective 投射) | 整删 11 源文件(model/service/wake_reconciler/intake/evaluator/approval/lifecycle/api/agent_tool_domains/handlers/autonomy_repair_plan)+11 测试文件;**抽出** `trigger_failure_policy.py`(trigger backoff 从 objective_lifecycle 剥离,无 objective 依赖);`focus_state.py` 瘦成纯 slug normalizer;`autonomy_overview/audit` reduce 到 trigger+runtime-only;trigger_daemon/preflight 去 objective_task 类+session keying+reconcile 调用;memory `retriever._retrieve_working`(focus.md→prompt 主投射)+assembler WORKING 段删除;kernel restoration/reminder/prompt_sections/coordinator/hr/extract/dream/summarizer/distiller 去 objective+focus 文本;migration `retire_agent_objectives_0608`(drop RLS+table,幂等);**真 bug 修**=plan 定时执行指令原叫死工具 `complete_objective/update_objective`→改 `track_todo/record_finding`;砍死 action_kind `activate_objective_wake`(保留 live 的 `enable_autonomous_wake`) | **全量 3978 passed / 0 failed / 7 skip**;`grep AgentObjective app/`=CLEAN(仅 plan handoff 文档串);ruff 全过;3990→3978 测试(删 11 objective 测试文件) |

**C4 净效果**:objective 概念在后端**彻底消失**——无 model/表/工具/API/prompt 投射/trigger 耦合。agent 的"当前在做什么"状态记录归位到 CC 对齐的 Work Ledger(track_todo/record_finding)。focus.md 降为普通 workspace scratch(不再自动投射进 prompt);`focus_ref` 列保留为 trigger 被动字段(不 drop,无迁移)。前端 objective 面已配对退役(`d4c9f226`:删 objectiveApi+AgentAware objectives 段+设置死工具开关,保留 PlanCard plan objective 字段;build+vitest 195 绿)。

| # | 内容 | 关键改动 | 证据 |
|---|------|---------|------|
| 5 | **触发三桶收口 + 事件驱动 v1** | `trigger_bucket()` 分类器=三桶 source of truth(cron+interval→`cron`/once→`once`/poll·on_message·webhook→`event_driven`);daemon context 抽成纯函数 `_build_trigger_context`(可单测)+三桶框架:**事件驱动 trigger 显式"Event from trigger"+喂事件 payload**(scheduled 则"Scheduled trigger");**真缺口补**=poll 检测到的变化(`_last_event` old→new)现在喂给 agent(此前只 on_message/webhook 喂,poll 静默);删 trigger context 里 retired `focus_ref`「Related Focus」注入;v1=直接 invoke_agent(prompt=事件,§8 开放点2 owner 倾向),非 v2 Signal 持久层 | 全量 3983 绿;6 新红测先行(bucket 分类/event 框架/poll 注入/无 focus_ref) |

**C5 净效果**:6 类 trigger 收口为三桶语义(cron 无条件周期/once 后台一次/event_driven 检测→喂事件→agent)。检测(`_evaluate_trigger`)与执行(`_invoke_agent_for_triggers`)已分离(C4 去 objective 耦合后天然成立),事件驱动现在把**真实事件**(poll 变化/消息/webhook payload)喂给 agent 而非"某 trigger 触发了"。CC 对齐:trigger 只是"何时/用什么 prompt 起一个 call"。

| # | 内容 | 关键改动 | 证据 |
|---|------|---------|------|
| 6 | **Task 单板收敛 = supervision 退役**(唯一真砍;§4 其余项核实后无需改) | 删孤儿 `supervision_reminder.py`(387 行,`start_supervision_reminder` **main.py 从不启动**=死代码);`Task.type` enum `(todo,supervision)`→`(todo)`;drop 4 列 supervision_target_user_id/name/channel/remind_schedule;task_executor 去 supervision 分支(execute_task 只剩 todo:doing→invoke_agent→done);api/tasks 去 supervision 跳过(所有 task create/trigger 现统一 plan-gated todo);agent_tool_domains/tasks 去 supervision 子分支;migration `retire_task_supervision_0608`(drop 4 列,幂等) | 全量绿(head pin 更新后);supervision residue app+tests=0 |

**C6 净效果 + §4 处置核实**(审计 §4 三分按真实代码校准):
- **T-D2 supervision = 砍**(已做):提醒/唤醒概念塞进 task 类型枚举,且 runner 是孤儿死代码。归位:周期提醒用 §2 trigger 桶。
- **T-D1 业务 DB Task 第二套 = 保留(Goal-2 delta,非砍)**:审计原列"收敛",但 [[project_hive_north_star]] Goal 2 明确把"任务管理"列为控制中台能力——业务 Task 是**人给 agent 派活**(human/REST-facing),与 Work Ledger(agent 私有工作记忆,agent-facing)是**不同层非重复**;按优先级 rule 0(Goal-Alignment First)约束冲突目标时保目标。收敛**已在历史完成**:manage_tasks 退役(agent 不双写)+execute_task 漏斗进统一 invoke_agent runtime。
- **T-D7 should_enable_work_ledger = 无需改**(核实):docstring+map 实证它**只 gate nudge 注入时机**(简单回合零开销),**不 gate 工具可用性**(track_todo/record_finding 始终注册、lazy-create)。审计"机械门决定可用性"前提不准;这其实是 CC 对齐的(工具常驻+prompt 教用法+简单回合不 nag)。
- **T-D6 required 完成门 = 无需改**(核实):map 实证 validate_completion 产出 audit finding 不 hard-block=已 advisory(守 sensor-vs-blocker 法)。
- **T-D4 record_finding / T-D5 RuntimeTask.task_type = 保留**(Goal-1 认知脚手架 / 执行机器账本)。

| # | 内容 | 关键改动 | 证据 |
|---|------|---------|------|
| 7 | **Subagent 收敛**(§5.2):ForkLevel 砍 `brief`→二元 | `ForkLevel` `(none,brief,all)`→`(none,all)`(CC 二元 fresh/full-fork);删 `_build_brief_from_messages`+`_BRIEF_MAX_*` 常量;`_build_subagent_messages` 去 brief 压缩分支(all=父消息逐字 or 显式 context_brief);`_VALID_ISOLATION`→`(none,all)`+`_coerce_isolation` 旧 brief→all 向后兼容;**S-D2 fanout_subagents 核实非 LLM 工具=保留 DR 内部**(无需改) | 全量 3974 绿;`brief` 无 producer(spawn 工具不暴露 fork 参数,印证审计) |

**C7 净效果**:subagent fork 回归 CC 二元(none=fresh 干净 worker / all=full-context fork),自创的 brief 中间档(压缩父消息)消除。fanout_subagents 维持 DR 内部细节(不进工具目录、不提升一等)。**进化闭环按 owner 2026-06-08 拍板保留全套(Goal-1 delta,不动)**。Subagent 轴对齐 CC 完成。

| # | 内容 | 关键改动 | 证据 |
|---|------|---------|------|
| 8 | **存量生产数据 dry-run 脚本 + 收尾**(§7 step5-6) | `exec_align_legacy_data_dryrun.py`:只读清单(objective_task 类 trigger + supervision Task 行 + 检测 agent_objectives 表残留)+`--apply` 门控(默认关、先写 JSON 备份、交互确认、禁用 trigger 可逆/删 supervision 行);**不擅自动生产数据**=交付纪律唯一例外(dry-run+确认门),owner 在 Railway 跑+审+确认 | 最终全量 3974 绿;`py_compile` OK;`import app.main` OK |

**C8 净效果 + 存量数据安全性**:schema 残留(objective 表+supervision 列)由迁移在 deploy 时清;数据残留(旧 objective_task trigger / supervision Task 行)**已 fail-closed 惰性**(objective_task trigger 无 plan_id 被 plan-gate 挡=blocked 不跑;supervision Task 无 executor 分支=不执行),清理是卫生非安全。dry-run 脚本供 owner 审后清。

---

## 🏁 全部完成(2026-06-09,8 commits 在 main 逐步推进,未 push)

| commit | 块 | 一句话 |
|--------|----|----|
| `240aa239` `9b9c5439` `6bd3dee7` | Plan Mode intent(#1-3) | 砍 2 孤儿 intent · long_task 重命名归位(治理锚) · handoff 直连 trigger 解开 objective |
| `3f26d90a` `d4c9f226` | objective+focus 退役(#4) | 后端整删 11 源+11 测试+迁移 · 前端删 objectiveApi/UI · 真 bug 修(死工具指令) |
| `4dbbc680` | trigger 三桶+事件驱动 v1(#5) | `trigger_bucket()` 三桶 · 事件驱动喂真实事件(补 poll 缺口) |
| `e95009fc` | Task 单板(#6) | supervision 退役(孤儿死代码) · 业务 Task 核实为 Goal-2 保留 · should_enable/required 已对齐 |
| `8f0200c1` | Subagent(#7) | ForkLevel 砍 brief 回 CC 二元 · fanout 保 DR 内部 · 进化闭环保留 |
| (本提交) | 存量 dry-run+收尾(#8) | 只读清单脚本+`--apply` 门控 · owner 审后清生产数据 |

**总账**:四块(Plan/触发/Task/Subagent)全部对标 CC 过一遍。砍掉的自创杂物=objective 子系统(整子系统)+focus.md 投射+2 孤儿 plan intent+supervision 类型+ForkLevel brief 档+死 action_kind。保留的有意 delta=plan 治理信封/状态机/hash(Goal-2)+业务 Task 人派活(Goal-2)+record_finding(Goal-1)+subagent 进化闭环(Goal-1)+spawn 治理(Goal-2)。每块逐 commit 红测先行+全量绿(终 3974)+文档带证据。CC 范式落地:执行/自动化 = 一个 `invoke_agent` call,触发只是"何时/用什么 prompt 发起它"。

## 1. 病灶:CC 范式 vs Hive 跑偏(evidence)

### CC 怎么做(源码实证)

CC 把「执行 / 自动化」全部收进**一个 agent loop**,周边只是输入源与权限态:

- **定时** = `useScheduledTasks.ts:32-39`:到点把 prompt 塞进当前 session 的命令队列(later 优先级),REPL 在 turns 间 drain。brief 模式才 fork background subagent。
- **plan 确认** = `ExitPlanModeV2Tool.ts:361,399`:唯一改的状态是权限模式(`plan` → `prePlanMode`),然后同 loop 继续(tool_result「You can now start coding」)。
- **task** = `TodoWriteTool.ts:65-103` / `tasks.ts:284-308`:写 todo / 建 task 只落盘,**不 spawn**;执行是另一个工具(`AgentTool`)。
- **subagent** = `runAgent.ts:906`:body 即整个 system prompt(替换不叠加);memory 靠 agent 自写 + 通用 nightly 蒸馏(`memdir.ts:348`)。

**全源码 grep 零命中**(均在 `…/claude-code-org/src` 内):`trigger_type` / `objective_wake` / `focus.md` / `AgentTrigger` / `long_task` / `intent_type` / `action_kind` / `wake_policy` / `objective_trigger` / `supervision`(task 路径)。CC 没有触发类型系统、没有 objective、没有 focus.md、没有 intent 分类、没有第二套 task 系统。

### Hive 跑偏成了什么(四块)

- **触发**:`models/trigger.py:31` 5 类型平铺;`trigger_daemon.py:970` 每 fire 新建 ChatSession + 独立 `invoke_agent`;`objective_wake_reconciler.py` 为 objective 自动派生 trigger;`focus.md` 把 objective 投射成「当前焦点」(与 task 重叠)。
- **Plan Mode**:`plan_mode_core.py:34-40` 自创 INTENT_TYPES(5)给每个 plan 贴意图标签 + 派生不同 handoff;其中两个 intent 没有任何 producer(死词汇)。
- **Task**:`models/task.py:13-62` 业务层 DB `Task` 与 agent 面的 Work Ledger **两套并行**;`Task.type=supervision` 把提醒概念塞进 task 类型枚举。
- **Subagent**:`subagent.py:506,531` 每次 spawn 自动蒸馏 run-log → 提名 → 平台 LLM 改写 subagent definition body(CC 完全没有此机制)。

**一句话**:Hive 把「在某个时刻 / 某个条件下,给 agent 一个 prompt 让它干活」这件最简单的事,在四个地方各自长出了一套子系统。

---

## 2. 触发系统 → 三桶语义,一个 call

### 2.1 三桶(按驱动语义,非时间写法)

| 桶 | 语义 | 触发产物 | 现状(要改的) |
|----|------|---------|-------------|
| **cron** | 完全自动化任务:无条件 · 时间驱动 · 周期 | 到点 → 一个完整 call | 保留,简化 |
| **once** | background 任务:一次性 · 延迟 | 到时 → 一个完整 call(只一次) | 保留,概念归位为 background |
| **interval / 事件驱动** | 简化自动化:条件 / 事件驱动 | 轻量检测 / 接收事件 → **把事件喂给主 agent** → agent 自己起 call | **最大改动**:不再自带完整 session/loop;poll · webhook · on_message 全并入 |

**核心变化**:把「检测」与「执行」分离 —— 检测轻量(daemon 评估),执行只在事件达成时才起 call。cron/once 是「到点直接起 call、无检测」;事件驱动是「探测 / 收事件 → emit → 主 agent 在自己 loop 里消化」。

### 2.2 边界判据(避免滑回平铺类型)

| 问题 | 判据 |
|------|------|
| cron vs interval? | **看语义不看时间写法**。「每天 9 点就是要跑一遍」→ cron(无条件执行);「每 30 分钟探一下,有变化才叫醒」→ interval(有条件检测)。 |
| poll/webhook/on_message? | 全是事件驱动桶的事件源,**保留能力**,产物统一成「喂事件给主 agent」。 |
| 周期能力砍了吗? | **没砍**。cron 承载无条件周期执行,interval 承载有条件周期检测。 |
| agent 长期目标放哪? | 用已有 **task 系统**(track_todo / RuntimeTask),不再需要 objective + focus.md。 |

### 2.3 退役 objective + focus.md + reconciler(砍)

- **`AgentObjective` 自带唤醒**:删。objective 不再自动派生 trigger。
- **`focus.md` 投射**:删。agent「当前焦点」由 task 系统 active tasks 表达。
- **`objective_wake_reconciler.py`**:整文件删(8 函数 100% 服务 objective,零非 objective 职责)。
- **`AgentObjective` 实体本身**:待拍板(§8)—— 整删 vs 降级成纯「目标备注」。

reconciler 的 4 个客户先安置后删:

| 依赖方 | 现用途 | 重新安置 |
|--------|--------|---------|
| `trigger_daemon.py:1488` | 每 tick reconcile 派生 objective trigger | **直接删调用** |
| `plan_mode_handoff.py:46` | plan 确认后派生 objective trigger | 改走三桶(见 §3.5) |
| `autonomy_repair_plan.py:20` | 自主修复建 objective trigger | 改按需建 cron/once |
| `hr.py:1393` | HR 建 agent 建 objective trigger payload | 改不建;初始目标用 task |

---

## 3. Plan Mode → intent/action/gate 收敛(证据:PlanModeAudit)

### 3.1 CC 基线:plan 确认 = 一个权限模式,零分类

- **模式枚举**(`types/permissions.ts:16-29`):`plan` 是 7 个权限模式值之一,无意图 / 动作分类。
- **进入**(`EnterPlanModeTool.ts:88-99`):`call()` 只做一件事 —— `setMode 'plan'`;入参 schema 空(`z.strictObject({})`)。
- **退出**(`ExitPlanModeV2Tool.ts:361,399`):唯一改的状态是权限模式(`plan` → `prePlanMode`);入参只有可选 `allowedPrompts`,**无 intent_type / action_kind / wake_policy / handoff target**;批准后同 loop 继续。
- **plan 内容** = markdown 文件 `{slug}.md`(`utils/plans.ts:38`),无 schema、无必填字段、无状态机。
- **grep 零命中**:`intent_type` / `action_kind` / `long_task` / `wake_policy` / `objective_trigger` / `autonomous_wake` / `external_action`(`state_change` 仅命中无关的 SDK 事件 `sessionState.ts:130`)。

**CC 证明:plan 不需要任何意图 / 动作标签 —— 确认是权限翻转,loop 继续。**

### 3.2 Hive INTENT_TYPES(5)— `plan_mode_core.py:34-40`

| intent | 谁映射到它 | handoff target | CC |
|--------|-----------|----------------|----|
| `autonomous_wake` | 3 个 action_kind | `objective_trigger` | 无 |
| `long_task` | start_long_task + **start_workflow** | `continue_current_session` | 无 |
| `delegation` | start_delegation | `delegation` | 无 |
| `external_action` | **无 action_kind 映射** | continue_current_session | 无 |
| `state_change` | **无 action_kind 映射** | continue_current_session | 无 |

**关键发现**:`external_action` 与 `state_change` 是**孤儿 intent** —— `_ACTION_INTENT`(`core.py:57-66`)没有任何 action_kind 映射到它们,grep 无 producer。gate 永远不会给 plan 贴这两个标签。死词汇。

### 3.3 Hive ACTION_KINDS(6)— `plan_mode_core.py:45-52`

`create_enabled_trigger` / `enable_autonomous_wake` / `start_long_task` / `start_delegation` / `activate_objective_wake` / `start_workflow`。其中:
- `start_workflow` → **`long_task`**(`core.py:65`):**杂物桶铁证** —— workflow 启动没有诚实的 intent,被塞进 long_task。(它的确认风险来自「确定性执行」,不是「长」。)
- `activate_objective_wake` + `enable_autonomous_wake`:REST-only,骑在 Hive-only 的 objective/trigger 概念上。

### 3.4 砍 / 收敛 / 保留

| 项 | loc | 处置 | 理由 |
|----|-----|------|------|
| `external_action` intent | core.py:38 | **✅ 砍 (#1)** | 孤儿,无 producer,死词汇 |
| `state_change` intent | core.py:39 | **✅ 砍 (#1)** | 孤儿,无 producer,死词汇 |
| `long_task` intent | core.py:36 | **✅ 重命名归位 (#2)** | 深入代码修正:非死标签,是 start_workflow/start_long_task 的 intent-match 治理锚 + handoff 锚;owner 拍 A=重命名 `in_session_execution`(诚实语义,保留 intent-match Goal-2 delta),非纯砍 |
| `start_workflow → long_task` | core.py:65 | **✅ 随 #2 重命名** | `_ACTION_INTENT` → `in_session_execution`;gate 仍按 risk grade(`plan_gate_registry.py:127-151`) |
| `activate_objective_wake` / `enable_autonomous_wake` | core.py:49-50 | **砍** | 随 objective/trigger 退役;后者近似重复 `create_enabled_trigger` |
| `objective_trigger` handoff | core.py:385 | **砍** | 随 objective 退役;CC 只翻权限继续 loop |
| `wake_policy`(嵌在 plan_json) | core.py:438-441 | **收敛** | schedule 归 trigger,不归 plan 信封 |
| **INTENT_TYPES 整个 taxonomy** | core.py:34-40 | **收敛** | 5 个里 4 个 handoff 已塌缩到 `continue_current_session`;塌缩成「plan → 同 loop 继续」(CC 基线) |
| **`plan_json` 信封 + `REQUIRED_PLAN_FIELDS`** | core.py:31,313-328 | **保留(delta)** | CC plan 是裸 markdown;Hive 结构化信封是企业治理超集(hash 覆盖确认)。**标为 delta**;注意 `plan_markdown`(core.py:451)已恢复 CC 式正文为真,结构字段应居次 |
| **`PLAN_STATUSES` + 状态机 + `validate_plan_handoff`** | core.py:277-306 | **保留(delta)** | 版本 / hash 确认绑定 = 真正的治理价值;`governs what may execute, not how the agent thinks`(L2 合规) |

**净结论**:Plan Mode 真正 CC-发散、该砍的核心是 **intent taxonomy**(`INTENT_TYPES` + `_ACTION_INTENT` + `_INTENT_HANDOFF_TARGET`)。CC 证明 plan 无需意图 / 动作标签。结构化信封 + 状态机 + hash 绑定是合法 Hive 治理 delta,保留。

### 3.5 plan handoff 执行链(砍 objective_trigger 不断链)

Hive 现有三条 handoff,主路径不依赖 objective:

| target | 干什么 | 评 |
|--------|--------|----|
| `continue_current_session`(live-chat 默认) | plan 确认 → 同 session 直接续跑 | ✅ **就是 CC 模型,主路径** |
| `long_task`(legacy) | 已 seed 到 continue_current_session | ✅ 已归并 |
| `objective_trigger` | plan → AgentObjective + 派生 trigger | ❌ **旁路,砍** |
| detached/background | 仍是桩 | ⏳ 补成 once(后台) |

砍 objective **不断 plan 链** —— `objective_trigger` 只是本不该有的旁路。确认后只剩两条干净路径:`continue_current_session`(当前会话) / `once`(后台),对齐 CC。

---

## 4. Task → 两套系统收敛(证据:TaskAudit)

### 4.1 Hive 有两套独立 task 系统(中心事实)

| 系统 | 入口 | 存储 | LLM 可调? | 对标 CC |
|------|------|------|-----------|---------|
| **A. Work Ledger** | `track_todo` / `record_finding` / `read_ledger`(`work_ledger.py:54-298`) | JSON 文件(`agent_work_ledger.py:75-91`) | ✅ 是 | **已逐字段对齐 CC** |
| **B. 业务 DB Task** | REST `api/tasks.py` | DB `Task`(`models/task.py:13-62`)+ `RuntimeTask` | ❌ 否(REST-only) | **第二套,要收敛** |

### 4.2 CC 基线 + 对齐面(Work Ledger 已对齐,勿动)

CC:`TodoWrite`(工作记忆,字段仅 content/status/activeForm)+ `Task` 板(`tasks.ts:76-89`,有 owner/blocks/blockedBy);**create≠execute**(`tasks.ts:284-308` 只落盘),执行是另一个 `AgentTool` + `claimTask`(`tasks.ts:541-606`,claim 只盖 owner 不 spawn)。

Work Ledger 已对齐的 7 点:① create≠execute(`work_ledger.py:13` 不变式「writing the ledger never triggers execution」)② 状态机 `pending/in_progress/completed` 相同 ③ activeForm 双形 ④ owner/blocks/blockedBy + assign-owner 不 spawn(`agent_work_ledger.py:559-592`)⑤ 执行是单独工具 ⑥ reminder 形状 `#id [status] title` ⑦ proactive 提示。**这一套是 CC-aligned 基线,不动。**

### 4.3 砍 / 收敛 / 保留

| # | Hive 概念 | loc | 处置 | 理由 |
|---|-----------|-----|------|------|
| T-D2 | `Task.type=supervision` + `supervision_target_*` + `remind_schedule` | task.py:22-26,48-51 | **砍** | 提醒 / 唤醒概念塞进 Task 类型枚举;CC 无;归 §2 trigger 桶 |
| T-D1 | 业务 DB `Task` 第二套(type/priority/assignee/due_date/TaskLog) | task.py:13-75 | **收敛** | 违反 CC 单板模型 + 自家「F-2 单看板收敛」;Work Ledger 才是 CC 面;DB Task 主要为托管 `execute_task` |
| T-D3 | `Task.type=todo` 带 `execute_task` 自动跑 | api/tasks.py:84-121 | **收敛** | create-即-execute 现已是 REST-only + plan-gated(好),但仍养着一条 `execute_task` lane 与 delegate/workflow 重复;漏斗进同一 runtime |
| T-D7 | `should_enable_work_ledger` 粗复杂度门 | agent_work_ledger.py:772-812 | **收敛** | CC 把「用不用」交给模型 prompt;Hive 加了 pre-LLM 机械门决定**可用性** = 同 Plan Mode 反模式(判断挪出 agent)。改为常驻可用 + prompt 驱动 |
| T-D6 | `required` flag + `validate_..._completion` 完成门 | agent_work_ledger.py:1293-1359 | **收敛** | 确认是 advisory(sensor)非 hard blocker;守 sensor-vs-blocker 法 |
| T-D4 | `record_finding` + trust/source_refs/verified/failures | work_ledger.py:186-262 | **保留(delta)** | additive 认知脚手架,不降智不触发执行;CC 功能等价物是 verification-agent nudge。**标为 Hive 超集**,非 CC 对齐项 |
| T-D5 | `RuntimeTask.task_type` 枚举(delegation/heartbeat/trigger/coordinator_worker/workflow) | runtime_task.py:29-30 | **保留 but watch** | 执行机器账本(合法,CC 也追踪 spawned agent);但枚举里 heartbeat/trigger/workflow = 别处在收敛的自创概念,等 §2 落地后再审 |

**关键纠正**:create≠execute 在 **agent 路径上已验证为真**(2026-06-03 四切口属实,`manage_tasks` LLM 工具已退役)。耦合只剩 REST 端点 + plan gate。所以 Task 的活不是「解耦 create/execute」(已解),而是**收掉第二套业务 DB Task 系统**回归单板。

---

## 5. Subagent → 已对齐 + 进化闭环 delta 张力(证据:SubagentAudit)

### 5.1 已对齐(12 项,勿动)

Hive 两轴:`spawn_subagent`(worker)+ `delegate_to_agent`(peer delegation)。已逐项对齐 CC 的有 12 处,核心:
- **C2 body 替换语义**(精确对标):`subagent.py:459` `_build_standalone_system_prompt`(定义 body 即整个 prompt,不叠加宿主 soul/memory)↔ CC `runAgent.ts:906`。
- C1 复用统一 kernel;C4 whenToUse 必填;C5 自有 memory 追加在 body 后;C6 结论-only 返回;C7 后台 + 通知取代 busy-poll;C10 critic「只验不改」近移植 CC `verificationAgent`;C11 递归 guard;C12 并行多 worker。

### 5.2 收敛

| # | Hive 概念 | loc | 处置 | 理由 |
|---|-----------|-----|------|------|
| S-D2 | `fanout_subagents` 独立原语(Semaphore + `SubagentBudget`) | subagent.py:963 | **收敛** | CC 靠模型在一条 message 发多个 Agent call 并行,无 fan-out 函数。当前不暴露为 LLM 工具(对的);**保留为 deep-research 内部细节,不提升一等、不进工具目录** |
| S-D3 | `ForkLevel` 三档 `none/brief/all` | subagent.py:138 | **收敛(砍 brief)** | CC 刻意二元:fresh(`subagent_type` 在场)vs full-fork(省略)。`brief`(压缩父消息)是自创中间态,且 LLM spawn 工具根本不暴露 fork 参数。收敛为 `none`+`all` |

### 5.3 保留:进化闭环 = North Star Goal 1(✅ owner 2026-06-08 已拍 —— 保留全套)

**owner 拍板:「这个要的。」** 进化闭环**保留为 Goal-1 self-evolution delta**(第三类),不进砍 / 收敛清单。我此前不盲从 subagent 的「砍」建议是对的 —— 它是 Hive 存在的理由本身,不是杂物。下面保留张力分析作为定性依据:

**事实(CC 完全没有)**:
- Hive `subagent.py:506` 每次 spawn 成功 → 自动 LLM 蒸馏 run-log(How-not-What)→ `subagent.py:531` `maybe_nominate` 自动提名 → 平台 LLM 起草修订 definition body(`subagent_evolution.py:41`)→ `apply_proposal`(`:233`)写回定义。
- CC 对照:`runAgent.ts:735` `recordSidechainTranscript` **只把 run transcript 落盘**供 resume/观测,**从不蒸馏进 memory、从不改 agent definition**;CC 的 agent 改进 = agent 自写 `MEMORY.md` + 通用 nightly `/dream` 蒸馏(`memdir.ts:348`),**没有平台自动改写 subagent 身份**。

**张力**:
- 一方面:这是 subagent 轴上唯一一整套 CC 没有的自动子系统(3 文件 + auto-nominate + LLM draft),形式上正是 owner 要揪的「自创」。
- 另一方面:它**是 North Star Goal 1(self-evolution:skill acquisition / soul evolution)的直接实现**,是 [[project_hive_north_star]] 里「Hive 存在的理由」本身,**不是 trigger/long_task 那种把简单事复杂化的杂物**。CLAUDE.md 明确「Goal 1 是 foundational cornerstone」。

**定性(owner 已拍)**:
- **保留全套**,含 auto-nominate(`maybe_nominate` 自动提名)。self-evolution 的价值正在于「自动」—— agent 自己进化,而非等人手动提名;关掉 auto-nominate 会把 Goal-1 退化成人工定义编辑。
- **L2 合规已满足**:`apply_proposal` 是**人审 / API 审批 + base_sha 409 stale 锁**(`api/agent_subagents.py`)—— 平台**提议**、人**批准**才改身份,治理在上层、不自动越权改身份。auto/manual 本就有开关(Agent 列),默认行为由 owner 控。
- **文档定性**:**Hive Goal-1 delta**,不是「CC 对齐项」,也不是「该砍的自创杂物」—— 第三类。本次审计不动它。

**其他保留 delta**:S-D5 `spawn` 的 `governance=sensitive` + capability gate(L2 治理,`tools/handlers/subagent.py:178`);S-D6 `delegate_to_agent` peer delegation profile 五档(peer 轴,CC 无「员工互派」概念,**本次 spawn 审计范围外**,留后续 peer-轴审计)。

---

## 6. 统一收敛总表(四块 × 三类)

### 砍(自创杂物,CC 无 + 不服务 North Star)

| 块 | 项 | loc |
|----|----|----|
| 触发 | `AgentObjective` 自带唤醒 / `focus.md` 投射 / `objective_wake_reconciler`(整文件) | objective_wake_reconciler.py |
| Plan | `external_action` + `state_change` 孤儿 intent | core.py:38-39 |
| Plan | `long_task` intent + `activate_objective_wake`/`enable_autonomous_wake` action_kind + `objective_trigger` handoff | core.py:36,49-50,385 |
| Task | `Task.type=supervision` + `supervision_target_*` + `remind_schedule`(归 trigger 桶) | task.py:22-26,48-51 |
| Subagent | `ForkLevel.brief` 中间档 | subagent.py:138 |

### 收敛(同概念塌缩 / 归位,对齐 CC)

| 块 | 项 | 目标 |
|----|----|------|
| 触发 | 5 类型平铺 → 三桶(cron/once/interval);poll·webhook·on_message 并入事件驱动 | 三桶语义,检测 / 执行分离 |
| Plan | INTENT_TYPES taxonomy + `_ACTION_INTENT` + `_INTENT_HANDOFF_TARGET` | 塌缩成「plan → continue-same-loop」 |
| Plan | `start_workflow→long_task` 映射 / `wake_policy` 嵌 plan | 按风险直接 gate / schedule 归 trigger |
| Task | 业务 DB `Task` 第二套 + `execute_task` lane | 回归 Work Ledger 单板 + 统一 runtime |
| Task | `should_enable_work_ledger` 机械门 / `required` 完成门 | 常驻可用 + prompt 驱动 / advisory 非 blocker |
| Subagent | `fanout_subagents` / `SubagentBudget` | 降为 deep-research 内部细节,不提升一等 |

### 保留(有意 Hive delta,标为非 CC 对齐项)

| 块 | 项 | 服务 | L2 合规 |
|----|----|------|---------|
| Plan | `plan_json` 信封 + `PLAN_STATUSES` 状态机 + `validate_plan_handoff` hash 绑定 | Goal 2 控制中台(治理 / 审计) | ✅ governs what may execute |
| Task | `record_finding` + trust/source_refs/verified | Goal 1 认知脚手架 | ✅ 不降智不触发执行 |
| Subagent | **进化闭环**(蒸馏 → 提名 → 人审 apply 改 definition) | **Goal 1 self-evolution** | ✅ apply 人审 409 锁合规;auto-nominate **owner 已拍保留**(2026-06-08) |
| Subagent | `spawn` sensitive 治理 + capability gate | Goal 2 企业权限 | ✅ 只 scope 权限 |
| Subagent | `delegate_to_agent` peer delegation(范围外) | Goal 2 控制中台 | 留后续 peer-轴审计 |

---

## 7. 迁移路径(单次完整交付,禁 MVP — [[feedback_no_mvp_finish_completely]])

> 内部依赖执行序,非可中途上线的阶段。生产数据(存量 objective trigger / AgentObjective / focus.md / 业务 DB Task)触碰数据卷 → dry-run + owner 确认(安全门,非 MVP 阶段)。

1. **Plan Mode 收敛**:砍 `external_action`/`state_change`/`long_task` intent + `start_workflow` 按风险重归类 + `INTENT_TYPES` 塌缩到 continue-same-loop;删 `plan_mode_handoff` 的 objective_trigger 旁路 + 取消注册;detached 桩补成 once。**保留** plan_json/状态机/hash 绑定不动。
2. **触发三桶 + 退役 objective/focus**:安置 reconciler 4 客户(§2.3)→ 删 reconciler → 停 focus.md 投射 → `AgentObjective` 按 §8 整删 / 降级;5 类型按三桶组织,poll·webhook·on_message 并入事件驱动,产物改「emit 事件 → 主 agent 起 call」。
3. **Task 单板收敛**:砍 `Task.type=supervision`(归 trigger);业务 DB `Task` 第二套收敛 / 隔离到非 agent 面;`execute_task` lane 漏斗进统一 runtime;`should_enable`/`required` 改 prompt 驱动 / advisory。**保留** record_finding。
4. **Subagent 收敛**:`ForkLevel` 砍 brief(二元);`fanout` 降为 DR 内部不进工具目录。**进化闭环按 §8 owner 决策**处理 auto-nominate。
5. **存量生产数据**(dry-run + 确认):objective trigger(`objective_*` 命名)/ AgentObjective / focus.md / 业务 DB Task 行 → railway dry-run 列出 → 确认 → 禁用 / 删除 + 备份。
6. **红测先行 + 生产实证验收**([[feedback_green_tests_dont_mean_done]]):plan 确认后续跑真接通;事件驱动不再新建孤立 session;objective/focus 退役后无残留唤醒;Task 单板下 agent 写 todo 不触发执行;subagent body 替换语义不回归。

---

## 8. 待 owner 拍板的开放点

| # | 块 | 开放点 | 我的倾向 |
|---|----|--------|----------|
| 1 | 触发 | `AgentObjective` 实体:整删 vs 降级成纯「目标备注」(无唤醒、仅记录) | **整删**,长期目标交 task,避免再留半死实体 |
| 2 | 触发 | 事件驱动「喂事件给主 agent」落地:v1 直接 `invoke_agent`(prompt=事件)vs 一步到位上 `Signal` 持久层 + consumer | **v1 起步**(Signal 与 [[project_workflow_determinism_hive]] wait_signal 同题,合并做) |
| 3 | 触发 | `once` 在 UI/工具层呈现为「后台任务」还是仅内部归类 | 内部归类先行,UI 措辞次之 |
| 4 | **Subagent** | ~~进化闭环 auto-nominate 处置~~ | ✅ **owner 2026-06-08 已拍:保留全套**(含 auto-nominate);定性 Goal-1 delta;apply 人审 409 锁守 L2;本次审计不动 |
| 5 | 全局 | 存量生产数据盘点(objective trigger / AgentObjective / focus.md / 业务 DB Task)railway dry-run + 退役方式(禁用 vs 删除 + 备份) | dry-run 先列清单再决 |

---

## 附:不碰的范围(明确保留)

- **`invoke_agent` 统一入口** —— 所有 call 仍走它,本文不动 kernel。
- **web search 工具** —— agent 搜索能力,与 trigger/objective 无关,完全不动。
- **webhook / on_message / poll** —— 作为事件源全保留,仅并入事件驱动桶、改产物形态。
- **cron / once 评估逻辑** —— 保留,仅做语义归位与简化。
- **Work Ledger**(`track_todo`/`record_finding`/`read_ledger`)—— 已对齐 CC 的 agent 面 task 系统,不动(除 §4.3 收敛项)。
- **Subagent body 替换语义 + 12 项对齐面**(C1-C12)—— 已精确对标 CC,不动。
- **`delegate_to_agent` peer delegation** —— 本次 spawn 审计范围外,留后续 peer-轴审计。
- **plan 治理信封 / 状态机 / hash 绑定** —— 合法 Goal-2 delta,保留。

# 运行时动态引导体系：机制 × 提示词（对标 CC attachment/reminder 层）

> 状态:**v0.7 完成稿**(2026-06-07)。T-G1/T-G2/T-G3.1 已落地。T-G3.1 修正 `28f236fc` 的 catalog 命名空间错误:旧 catalog 把 CC native attachment type 与 Hive native runtime guidance 通道混在同一个 `cc_type`,导致"覆盖 45 个 CC attachment/reminder 类型"声明失真。按本地 CC 源码 `claude-code-org/src/utils/attachments.ts` 的 Attachment union 区间抽取,CC native attachment type 当前为 **60 项**;工程权威源已改成双表:`CC_NATIVE_ATTACHMENT_CATALOG` 覆盖 60/60 真名(have 30/planned 19/n-a 11),`HIVE_NATIVE_GUIDANCE_CATALOG` 登记 35 项 Hive 自有通道(have 26/planned 1/n-a 8)。测试冻结 CC 真名清单并断言差集为空,同时钉住 Hive transient prompt 条目的唯一入口仍是 `ReminderScheduler`。
> 前版:**v0.1 审计定稿**(2026-06-05)。源码实证:CC `utils/attachments.ts` / `utils/messages.ts` / `constants/prompts.ts`;Hive `kernel/engine.py`。
> 关系:`docs/execution-mode-spectrum.md` 管 CC 引导体系的前两层——暴露架构 + 静态引导(§5 七原语决策序列、工具描述互指,T2 ✅ 已落地);**本文档管第三层:运行中的动态引导(reminder 注入)**。三层一起构成完整的"关键节点给模型判断信息"的体系。
> 流程(用户拍板,2026-06-05):**机制对齐 → 提示词对齐 → 全面 CC 对标**。本文档按此分 §5 三阶段路线。

---

## 0. 主旨与边界

CC 的引导体系是三层结构:

| 层 | CC 做法 | Hive 状态 |
|---|---|---|
| 静态系统提示 | 刻意做轻:`# Using your tools` 四条(dedicated-over-Bash 映射、任务管理一句话、并行引导、Agent 一句 when-to-use),选择哲学下放 | ✅ T2 落地(决策序列比 CC 总纲重,有意 delta,见 §4 P5) |
| 工具描述 | when-to-use / when-NOT / 互指网 | ✅ T2 对齐 |
| **动态 reminder** | **40+ attachment 类型,事件驱动,节流注入** | ✅ T-G1/T-G2/T-G3.1 已完成:机制、文案/快照、双命名空间 catalog |

本文档回答三问:① Hive 动态引导**机制自身**有什么缺陷(§3,不依赖 CC 视角);② 提示词与 CC 的差距(§4);③ 对齐路线(§5)。

**边界**:`runtime/hooks.py` 的 15 事件总线是内部管线(记忆蒸馏/治理),不是 prompt 注入通道,不混入本议题;CC 的 file-watch/diagnostics 类 attachment 依赖 IDE/LSP 设施,在 §5 T-G3 登记但不在前两阶段。

---

## 1. CC 基线(源码实证)

### 1.1 attachment 管线

- **40+ attachment 类型**(`attachments.ts:296-611`),关键类:`todo_reminder`/`task_reminder`(任务工具)、`plan_mode`/`plan_mode_reentry`/`plan_mode_exit`、`critical_system_reminder`、`task_status`(任务状态变化)、`skill_listing`/`skill_discovery`、`nested_memory`/`relevant_memories`、`diagnostics`(lint/类型错误)、`edited_text_file`(外部文件变更)、hook 系×9。
- **注入形态**:attachment 渲染为 isMeta user message 包在 `<system-reminder>` 里(`wrapMessagesInSystemReminder`),**进入消息历史持久存在**——只追加不重排,天然 cache 安全;历史可回看,节流计数靠扫描历史实现。

### 1.2 task_reminder 解剖(本议题对齐锚点)

`attachments.ts:254,3212-3320` + `messages.ts:3685-3698`:

1. **双冷却节流**:`TURNS_SINCE_WRITE=10` + `TURNS_BETWEEN_REMINDERS=10`——最近 10 个 assistant 轮没用任务工具才提醒,两次提醒之间再隔 ≥10 轮。**不是每轮注入**。
2. **行为推断 gate**:倒扫消息历史数"距上次 TodoWrite 几轮"——从模型行为倒推要不要提醒,**不预判任务复杂度**。
3. **可用性/冲突让位**:任务工具不在 tools 数组 → 不提醒;Brief(更高优先通信通道)在场 → 整个 nag 让位,避免引导冲突(`#20467`)。
4. **状态快照**:reminder 自带当前任务清单(`#id. [status] subject` 逐行),模型零额外调用看到现状。
5. **防护句×2**:"This is just a **gentle reminder - ignore if not applicable**" + "Make sure that you **NEVER mention this reminder to the user**"。语气全程建议式(consider / if relevant)。
6. plan_mode attachment 节流:`TURNS_BETWEEN_ATTACHMENTS=5`。

### 1.3 哲学总结

CC 把动态 reminder 当**低频、事件驱动、带状态、可忽略**的旁路信号——核心引导住在工具描述里,reminder 只是"你好像忘了"的轻推。频率失控的重复文本会变成壁纸(模型学会忽略),所以节流不是省 token 的小优化,是**提醒有效性**的前提。

---

## 2. Hive 现状全图(Fact,2026-06-05 盘点)

### 2.1 动态引导通道清单(T-G1 落地后新现状,2026-06-06)

全部 runtime reminder 经 **`kernel/reminder_scheduler.py`** 统一注册(`build_default_reminder_specs`),engine 每 invocation 建一个 `ReminderScheduler`:

| 通道 | spec | 触发(eligibility) | 频率(行为节流) | 状态 |
|---|---|---|---|---|
| Plan Mode FULL | `plan_mode_full` | plan 激活 | fire-once;compaction `reset()` re-arm(M8 ✓);file hint 随附 | ✅ |
| Plan Mode SPARSE | `plan_mode_sparse` | plan 激活 | 与 FULL 同 mutex 组,组级冷却 **5 轮**(对齐 CC plan throttle) | ✅ 原"每轮"已废 |
| Work Ledger | `work_ledger` | `work_ledger_enabled` flag + plan 互斥(M7:flag 只管参赛资格) | **idle 10 + 冷却 10**(对齐 CC 10+10):engine 逐轮 `observe(tool_names)`,用过 ledger 工具即重置 idle;触发时从 persisted ledger 渲染 `#id [status] title` 快照 | ✅ T-G2 快照+gentle guard |
| Round-pressure | `round_pressure` | 阈值轮(80%/final-2) | content fn 内判,带真实数据(B2 不变),带 internal/never-mention guard | ✅ T-G2 文案 |
| Loop guard warning | `enqueue()` 事件通道 | 语义循环命中(A4 先软后硬) | 事件驱动,下一轮 collect 排空一次;warning 带 internal/never-mention guard | ✅ T-G2 文案 |
| DR routing reminder | `engine.py:774-808` | deep research 场景 | 条件 | 未迁(DR 整体冻结待重做,不动) |
| 工具结果 next_action | 各 handler | 工具返回时 | 随结果 | Hive 特色保留(对位 CC hook_additional_context) |

### 2.2 注入机制(T-G1 后)

**Transient**:`scheduler.collect()` 的产出只拼进本轮 `stream_messages = _clone_api_messages(api_messages) + reminders`(PTL retry 自带)——**永不进入 `api_messages`**,故不堆积(M1 ✓)、不进 persist(M2 ✓,正常+abnormal 两路径)。可观测:每次注入 emit `reminder_injected` 事件(round/count/chars,M6 ✓)。compaction 重建处调 `scheduler.reset()` 重启调度时钟;已排队的事件型 warning 保留到下一次 collect,避免 loop_guard 软警告在 compaction 同轮丢失(M8 ✓)。

---

## 3. 机制层缺陷(自身审视,按严重度排序)

| # | 缺陷 | 实锤(修复前) | 状态(T-G1 后) |
|---|---|---|---|
| **M1** | **reminder 逐轮堆积** | append 在循环内,数组跨轮持有,无 pop/去重——40 轮 run 堆 40 条相同文本,壁纸效应+token 浪费 | **✅ 已修**:transient 注入,reminder 永不进 `api_messages`(集成钉:最后一轮请求中 FULL/SPARSE 各 ≤1) |
| **M2** | **堆积泄漏进记忆管线** | `_build_persisted_memory_messages` 只 skip `[0]`,reminder 全进 persist(两调用点)→ 污染 T0/T2 蒸馏输入 | **✅ 已修**:transient 根治,persist 钉×3(plan/ledger/pressure 零泄漏+真实对话照常) |
| M3 | **无节流基础设施** | 无状态纯函数,唯一状态 `reminded_full` 一 bit | **✅ 已修**:scheduler 统一 idle/cooldown/fire-once 计数,`observe(tool_names)` 逐轮喂入 |
| M4 | **无统一调度层** | 互斥硬编码,新增 reminder O(n²) 核对 | **✅ 已修**:specs 注册式,mutex_group 组级冷却,plan×ledger 互斥迁 eligibility |
| M5 | **无状态快照能力** | 字符串常量,无法带任务清单 | **✅ 已修**:engine 传 persisted ledger snapshot provider;`render_work_ledger_reminder_snapshot()` 渲染 `#id [status] title` |
| M6 | **零可观测** | 注入无 metric 无事件 | **✅ 已修**:每次注入 emit `reminder_injected`(round/count/chars) |
| M7 | **复杂度预判 gate 的 L1 张力** | 预判+每轮组合放大 M1 | **✅ 已修(拍板方向)**:flag 收窄为 eligibility(参赛资格),频率交行为推断(idle 10+冷却 10) |
| M8 | **冷却×compaction 语义未设计** | 仅 plan 有 re-arm | **✅ 已修**:`scheduler.reset()` 统一 re-arm(fire-once 重发+全部时钟清零),且保留 queued event warning |

## 4. 提示词层差距(对 CC,依赖 §3 机制修复的标注在列)

| # | 差距 | CC | Hive | 依赖 |
|---|---|---|---|---|
| P1 | 防护句缺失 | "gentle reminder - ignore if not applicable" + "NEVER mention this reminder to the user" | **✅ 已修**:ledger/round-pressure/loop_guard 均带 internal/never-mention;ledger 带 ignore-if-not-applicable | T-G2 |
| P2 | 语气 | 建议式(consider / if relevant) | **✅ 已修**:ledger reminder 从指令式改为 gentle + consider | T-G2 |
| P3 | 状态快照 | 带任务清单 `#id [status] subject` | **✅ 已修**:ledger reminder 读取 persisted ledger view 并渲染 `#id [status] title` | T-G2 |
| P4 | 频率 | 任务 10+10,plan 5 | **✅ 已修**:ledger idle10+cooldown10,plan SPARSE 组冷却5 | T-G1 |
| P5 | (有意 delta,非差距)静态层决策序列比 CC 总纲重 | 一句话+下放 | 七原语整段(executing_actions) | Hive 工具面宽(41 core+pack),一张决策地图必要——**保留,记录在案** |

## 5. 对齐路线(机制 → 提示词 → 全面对标)

### T-G1 机制对齐(地基,先行)

> **✅ 完成(2026-06-06)** — 红测先行(用户定序):新增 `tests/kernel/test_runtime_reminder_scheduler.py`(14 钉:ledger idle10/冷却10/工具使用重置/eligibility 硬门/plan 互斥不变性/FULL-once+SPARSE 组冷却5/reset re-arm/file hint 迁移钉/round-pressure 阈值+数据/enqueue 单次排空/reset 保留 queued event/kernel 集成不堆积+第一轮可见)+ `test_memory_persist_filters.py`(3 钉:plan/ledger+pressure 不进 persist + 真实对话照常持久)→ GREEN:新模块 `kernel/reminder_scheduler.py`(纯 Functional Core,specs 注册式:eligibility/cooldown/idle+observed_tools/fire_once/mutex_group 组级冷却;文本常量与 `_build_round_pressure_warning` 单一住所随迁);engine 接线=循环内三段 append 整删 → `collect()`+transient 拼接(`stream_messages` clone 后)+`observe(tool_names)` 逐轮喂入+loop guard `enqueue()`+compaction `reset()`+`reminder_injected` 事件;**路径统一删除**:`_plan_mode_reminder_content`/`_reset_plan_reminder`/`_work_ledger_reminder_content` 三纯函数 + `PlanModeState.reminded_full`/`entered_round` 死字段(session.py);既有测试同步 5 文件(plan_mode_reminder 重写留文本钉+B3/work_ledger_scaffold gating→eligibility 钉/round_pressure import/plan_mode_state 字段断言/test_engine PTL 事件按类型过滤——M6 新事件所致)。证据:`pytest -q` → **3902 passed, 7 skipped, 0 failed**(全量零排除,T-G1 原始验收);`ruff check`+`format` clean。M1/M2/M3/M4/M6/M7/M8 全闭(M5 快照装配能力为 T-G2 供能项,specs 已支持 callable content,T-G2 实装快照内容)。

1. **堆积归零(M1)+持久化过滤(M2)**:reminder 改为**transient 注入**——每轮发送给 LLM 的请求中包含,但不滞留 `api_messages` 数组、不进 persist。T-G1 拍板的设计分叉已执行:Hive 不采用 CC 的"持久进历史+扫历史节流",而采用 per-invocation scheduler 计数,因为 Hive 有记忆蒸馏管线,reminder 进入 persist 是污染。
2. **ReminderScheduler(M3/M4)**:注册式——每个 reminder 声明 `(触发条件, 冷却轮数, 优先级/互斥组)`,调度器统一计数与裁决;plan×ledger 互斥从硬编码迁入互斥组;round-pressure/loop-guard 保持事件驱动但纳入同一注册表(冷却=0)。
3. **快照装配(M5)**:reminder 支持 callable content(运行时读 ledger/objective 状态拼装),为 T-G2① 供能。
4. **可观测(M6)**:每次注入 emit 事件(type/round/字节数)进既有事件流;独立 metric 计数。
5. **compaction 语义(M8)**:scheduler 计数随 compaction reset(复用 `_reset_plan_reminder` 模式,全 reminder 统一);queued loop_guard event 不随 reset 丢弃。

### T-G2 提示词对齐(站在 G1 上)

> **✅ 完成(2026-06-06)** — 红测先行:service 钉 `render_work_ledger_reminder_snapshot()` 从真实 persisted ledger view 输出 `Current Work Ledger snapshot` + `#id [status] title` + dependency metadata;scheduler 钉 ledger reminder 触发时才调用 snapshot provider,并带 `gentle reminder` / `ignore it if it does not apply` / `Do not mention this reminder to the user`;kernel 集成钉 11 轮工具循环从 session ledger 注入 persisted snapshot;round-pressure 与 loop_guard warning 钉 internal/never-mention guard;persist 钉更新为新文案并用 distinct tool args 确保 ledger/pressure reminder 真出现后仍不进 persist。证据:`pytest tests/kernel/test_runtime_reminder_scheduler.py tests/kernel/test_loop_guard.py tests/kernel/test_memory_persist_filters.py tests/services/test_agent_work_ledger_agent_writes.py -q` → **45 passed**。

1. **ledger reminder**:双冷却(对齐 CC 10+10,数值可配)+ ledger 快照(当前 todos `[status] title` 清单)+ 防护句×2 + gentle 化改写;
2. **plan SPARSE 降频**(对齐 CC plan 节流 5 轮,FULL/re-arm 语义不动);
3. **round-pressure / loop-guard 文案**补防护句(数据部分已对齐,不动);
4. **M7 演进**:`work_ledger_enabled` 已降级为"是否参赛"(eligibility),频率全交行为推断(N 轮没用 ledger 工具才提醒)——gate 不删(T1.2 拍板边界),语义已随 T-G1 收窄。

### T-G3 全面 CC 对标(40+ attachment 逐项 gap 盘点)

> **✅ T-G3.1 完成(2026-06-07)** — 工程权威源为 `backend/app/kernel/runtime_guidance_catalog.py`。`28f236fc` 的 mixed catalog 已拆成双命名空间:① `CC_NATIVE_ATTACHMENT_CATALOG` 只登记 CC native attachment type,测试冻结本地 CC `Attachment` union 抽取出的 60 个真名并断言差集为空;② `HIVE_NATIVE_GUIDANCE_CATALOG` 只登记 Hive 自有 runtime guidance 通道,不再使用 `cc_type`;③ 兼容 alias `ATTACHMENT_ALIGNMENT_CATALOG` 指向 CC native catalog,不能再混入 Hive extra;④ `runtime_transient_prompt_entries()` 只返回 Hive native entries,并继续钉唯一入口 `kernel/reminder_scheduler.py::ReminderScheduler`。证据:红测 5 项先失败(缺新表/命名空间/强覆盖)→ GREEN:`pytest tests/kernel/test_runtime_guidance_catalog.py -q` → **5 passed**。

旧 catalog 的有价值部分已保留为 Hive native 通道登记表,但不再冒充 CC native attachment。双表状态汇总:

| catalog | 总数 | `have` | `planned` | `n/a` | 含义 |
|---|---:|---:|---:|---:|---|
| `CC_NATIVE_ATTACHMENT_CATALOG` | 60 | 30 | 19 | 11 | 对 CC `Attachment` union 60 个真名逐项裁决,覆盖差集必须为空 |
| `HIVE_NATIVE_GUIDANCE_CATALOG` | 35 | 26 | 1 | 8 | Hive 自有通道,例如 `work_ledger_reminder`/`round_pressure`/`loop_guard_warning`/治理 hook |

代表性裁决:

| CC native 类型/家族 | T-G3.1 结论 | Hive 唯一路径 |
|---|---|---|
| `agent_listing_delta` / `deferred_tools_delta` / `mcp_instructions_delta` | `planned` | 未来 T3 deferred-loading 增量宣告,不混进当前 small-cut |
| `todo_reminder` / `task_reminder` | `have` | Hive native `work_ledger_reminder`,经 `ReminderScheduler` transient 注入 |
| `plan_mode` / `plan_mode_reentry` / `plan_mode_exit` | `have` | `plan_mode_full`/`plan_mode_sparse` scheduler + `exit_plan_mode` 工具结果 |
| `token_usage` / `budget_usd` / `output_token_usage` / `context_efficiency` | `planned` | 当前只有 Hive `round_pressure`;精确 token/budget telemetry 需独立切口 |
| `dynamic_skill` / `invoked_skills` / `skill_listing` / `skill_discovery` | `have` | `tool_search`/`load_skill` 工具结果路径 |
| `teammate_mailbox` / `team_context` / `teammate_shutdown_batch` | `planned` | A2A primitive 已有,但 prompt-facing team context/mailbox 需单独 contract |
| file/IDE 系(`file`,`directory`,`selected_lines_in_ide`,`edited_*`) | `have`/`planned` 分开 | workspace 工具已有;IDE selected/opened/edited watcher 依赖未来 IDE bridge |
| hook 系真名(`hook_blocking_error`,`hook_success` 等) | `have`/`n/a` 分开 | tool result/governance event 可映射;内部 hook/cancel/system message 不注入 prompt |

### 红测样例(T-G1/G2)

| # | 红测 |
|---|---|
| 1 | N 轮 run 后 `api_messages` 中 reminder 类消息 ≤ 调度器允许的注入次数(堆积归零) |
| 2 | persist 消息中不含任何 reminder 文本(M2 过滤) |
| 3 | 冷却:连续轮内同一 reminder 不重复注入;冷却到期后恰好一次 |
| 4 | ledger reminder 含当前 todos 快照;清单变化后快照跟随 |
| 5 | 防护句存在(ignore-if-not-applicable + never-mention) |
| 6 | plan×ledger 互斥经互斥组生效(行为不变性) |
| 7 | compaction 后 plan FULL re-arm 不回归 + scheduler 计数 reset |
| 8 | 注入事件可观测(emit 一次/注入) |

**DoD**:① M1/M2 归零有红测钉死;② reminder 全部经 scheduler 注册(无旁路 append);③ 提示词三要素(冷却/快照/防护句)落地;④ 全量绿;⑤ 本文档 §2/§3 更新为落地后新现状(证据闭环)。

## 6. 非目标

- 不删 reminder gate(`should_enable_work_ledger`,T1.2 拍板)——T-G2④ 只收窄其语义,且单独拍板;
- T-G3 的 `planned` 项只登记独立切口,不在本文档内抢跑 LSP/file-watch/web-quality 等新基础设施;
- 不动静态层(execution-mode-spectrum T2 已收口;P5 是记录在案的有意 delta);
- 不把 `runtime/hooks.py` 事件总线改造为 prompt 注入通道(职责不同);
- 工具结果内嵌 next_action 引导(Hive 特色)保留现状,不强行 CC 化。

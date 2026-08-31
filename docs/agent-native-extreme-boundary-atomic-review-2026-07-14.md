# CCPlus 极端边界原子化复审与最终整改方案 — 2026-07-14

> **历史账本提示（2026-07-14）**：本报告保留第一次极端边界复审的 94-leaf 证据，但它把少量“单个 root Session 的 100 个 child”表述成了“100 Agent”，且尚未纳入 fleet scheduler/trigger 与 Session V2 的独立 seam。当前分母、四平面口径和最终施工顺序以 `docs/agent-native-unified-atomic-review-2026-07-14.md` 为准；本文的 94 与 35 个场景仅代表当时快照。
>
> 本报告依据当时修订的 `docs/reusable-agent-native-atomic-review-prompt.md`，综合 `docs/agent-native-atomic-review-2026-07-14.md`、`docs/agent-native-atomic-review-501db655.md` 与当时工作树的新增源码复核。它曾取代前两份报告的断点分母与施工顺序，但现已由上述统一报告接续；旧报告仍不应删除或改写成“当时就得出了后续结论”。
>
> 审查快照：`main@501db6555dae374e5fcf43a6fdcfe8a3dd89343e` + 2026-07-14 当前未提交工作树。工作树包含其它并发工作的业务代码与测试改动，本轮没有回滚、覆盖、暂存或归属这些改动。`docs/` 当前被 `.gitignore` 忽略，因此本报告和提示词在显式 `git add -f` 或收窄 ignore 规则前，不是 Git-tracked truth。

## 1. 最终结论

Hive 当前不是“边界太多”或“边界太少”二选一的问题，而是四种本应正交的事实被混进了一套 `max_* / exception / fixed prose / task failed` 机制：

1. 物理/协议不可能；
2. 权威与外部效果禁止；
3. 用户或组织显式购买的成本、截止时间与取消；
4. 平台自己的运行水位与调度偏好。

前三类可以形成 hard outcome，但必须只阻断其作用域；第四类默认只能产生 pressure、backpressure、queue、checkpoint、partial result 或 typed pause。当前实现多处把第四类直接升级为整个 turn/task/session 的失败，并且不把“为什么失败、保留了什么、如何恢复”交还模型。这正是极端情况下形成死循环、垃圾回答和不可恢复 Session 的共同根因。

本轮统一后的**当前工作账本为 94 个 canonical breakpoint：P0 1、P1 32、P2 32、P3 29**。另有 **5 个已知缺失**、**6 类关键 coverage gap**，均不计入 94。计算式是：

```text
旧工作账本 69
- 1  B-04 / A-09 同一 seam 的重复计数
+ 2  D-KB1 + D-KB2 从 2 个合并项拆成 4 个独立 leaf
+ 1  P1-F5 从“审计表可变 + tenant=None 静默丢弃”拆成 2 个 leaf
+ 1  G-01 从“平台冒充 assistant + NL expired 判状态”拆成 2 个 leaf
+ 8  context / capability / Memory / output / result / MCP / observation 新 leaf
+ 11 concurrency / A2A / Workflow / queue 新 leaf
+ 3  budget-root fail-open / foreground approval intent / mailbox lost-update 新 leaf
= 94
```

严重度重算：旧账校正后为 `P0 1 + P1 11 + P2 31 + P3 29 = 72`；本轮新增为 `P1 21 + P2 1 = 22`；合计 `P0 1 + P1 32 + P2 32 + P3 29 = 94`。

这个数字是绑定当前快照与当前分类规则的工作分母，不是“世界上再无第 95 个 bug”的承诺。本轮没有安全地实跑 100 个真实付费 Agent、200 个真实 MCP 进程或钉钉/飞书/Slack 的真实跨渠道风暴；这些规模结论由当前生产代码的无界数据路径静态坐实，实际容量曲线仍明确标为未验证。任何后续证据都必须通过 `added / merged / split / refuted / reclassified / closed` 改账，禁止为了维持 94 而凑数。

## 2. 北极星裁决

修复方向不违背北极星，前提是严格采用下面的边界顺序：

| 边界 | 默认裁决 | 允许结束什么 | 必须保留什么 |
|---|---|---|---|
| provider context/request、协议帧、真实进程容量 | hard physical fact | 当前 request/attempt | durable evidence、partial output、coverage、可重建引用 |
| tenant/RLS/ACL/delegation/sensitivity、credential、sandbox、外发/付款/删除审批 | hard authority/effect fact | 对应 ingress/effect/hop | 无关推理与工具、已有结果、typed deny receipt |
| 用户/组织显式 cost/deadline/cancel/workflow contract | hard economic/lifecycle fact | 当前 task 的授权执行范围 | checkpoint、remaining work、恢复/续费/重新授权入口 |
| Prompt 目标占用率、tool rounds、fan-out、batch、semaphore、queue、retry、result size、内部 depth | soft runtime target | 当前 attempt 或自动 retry | pressure state、progress certificate、defer/queue/partial/replan |

因此，正确目标不是“去掉所有限制”，而是 **effect hard、thinking elastic、failure visible、state durable、recovery reachable**。这与 AGENTS.md 的 Model Agency Boundary 完全一致：平台约束行动和机械事实，模型保有语义判断、综合与最终表达。

## 3. 提示词已经完成的修订

`docs/reusable-agent-native-atomic-review-prompt.md` 已加入以下强制门：

- effective context contract：记录真实 provider/model/version、可用 window、output reserve、各类输入开销、估算误差、实际 request/usage、`finish_reason` 与 413/overflow receipt；
- attempt、turn、task、session、workflow 与 delivery 终态分离；
- `progress certificate`：`blocked_on`、`resume_condition`、`resume_owner`、`not_before`、lease、progress marker、retry fingerprint、checkpoint/coverage refs；
- parent/child `reserve → durable admission/enqueue → commit/release`，未 admission 的 child 不进入 expected set；
- no-hold-and-wait、full barrier 的 partial/late/failed/cancel policy、retry 单调进展；
- 单 Session、单模型、无备用 provider 时的 externalize、compact、分批、多 turn、pause/resume；
- 100-way completion 与 streaming event storm、400 Skill、200 MCP、`10^3→10^6` Memory、1 万节点 Workflow、跨钉钉/飞书/Slack/Web A2A；
- 35 个 `X-*` 场景、`LB-1` 至 `LB-10` 活性门、1/10/25/50/100 capacity curve、canonical leaf 计数规则；
- CC/FreeCode 语义下限、Codex 工程增量、Hive Native 超越点必须收敛成同一个 lifecycle/principal/evidence/result contract，并做同模型同 fixture paired replay。

提示词不再把“任意 cycle/depth 都可 hard stop”写成通行证：只有可证明的真实拓扑环、调用栈/协议上限或显式 Workflow contract 才是 hard fact；应用自设 depth 默认只是分解或暂停水位。

## 4. CC、Codex 与 Hive Native 的真实组合

### 4.1 CC / FreeCode 是语义下限，不是无条件照抄

当前可读源码事实表明：FreeCode 主要围绕物理 context window 做 AutoCompact，保留 output/recovery 空间，provider 413 后可 reactive compact，并用 circuit breaker 与 stop hook 避免 compact 自循环。Skill 预算不足时退化描述，但保留全部名称可发现。这是 Hive 应保持的“模型先看见、超窗再恢复”语义下限。源码落点：`/Users/example-owner/vc-saas/free-code-main/src/services/compact/autoCompact.ts:30-91,241-351`、`src/query.ts:592-647,1065-1182,1258-1265,1292-1297`、`src/tools/SkillTool/prompt.ts:20-40,70-171`。

但 CC/FreeCode 自身也有极端缺口：concurrency-safe Agent tool 没有数值 admission；成功 child conclusion 可全量 fan-in；aggregate spill 的保护受默认关闭 flag 控制；ToolSearch 结果数也缺稳定上界。源码落点：`src/services/tools/StreamingToolExecutor.ts:129-150`、`src/tools/AgentTool/agentToolUtils.ts:276-356`、`src/constants/toolLimits.ts:6-49`、`src/utils/toolResultStorage.ts:447-462`。因此 CC 是普通单 Agent 行为下限，不是 Hive 的 100-Agent 容量答案。

对照限制：`claude-code-org` 本地 HEAD 是 README 明示的 2026-03-31 sourcemap snapshot，本轮核对的核心文件与 FreeCode 当前 HEAD byte-identical；本机 CC `2.1.209` 是闭源二进制。未做 current CC paired replay，不能把 snapshot 事实冒充 current binary 行为。

### 4.2 Codex 是工程参考，也不能照抄

Codex 值得吸收的是 typed `AgentLimitReached`、durable AgentJob 遇 cap 保持 pending、stream retry 与 transport fallback、typed thread/turn 状态，证据位于 `/Users/example-owner/Context Engineering/codex/codex-rs/core/src/config/mod.rs:203-211,1428-1441`、`core/src/agent/control/execution.rs:59-72`、`core/src/tools/handlers/agent_jobs.rs:185-227`、`core/src/responses_retry.rs:20-79`。不能照抄的是固定 4/6 thread cap 作为产品语义、超预算 omit skills、compaction blind drop/truncate，以及 completion mailbox 全量 drain；对应 `core-skills/src/render.rs:376-412`、`core/src/compact.rs:284-292`、`core/src/compact_remote.rs:368-459`、`core/src/session/input_queue.rs:72-102,197-224`。

### 4.3 Hermes 是 Goal 1 行为基准，不是边界事实源

本轮补读 `/Users/example-owner/vc-saas/hermes-agent@18e840469ffe9f8235331c787e34ebbe908564b8`。其 `tools/delegate_tool.py:590-598,1624-1714,2649-2656` 已按 parent 剩余 headroom 给 batch summary 分配预算，并把完整超长结果 spill 到文件后返指针；这是 Hive 当前 full child result 直接回灌路径至少应达到的 lean benchmark。它在 `agent/chat_completion_helpers.py:1500-1509,1656-1719` 命中 max iterations 后再次请求模型总结，也比平台固定 prose 更接近 Model Agency。

但 Hermes `run_agent.py:3756-3784` 会静默截掉超出的 `delegate_task` calls，默认并发/summary ceiling 仍是应用常量；这些不是 CCPlus 可照抄的治理契约。Hive 应保留 Hermes 的 lossless spill 与 model-authored summary，同时用 durable `not_admitted/deferred` 取代静默 truncation，并用 progress certificate 取代“总结后把 task 当完结”。

### 4.4 CCPlus 的唯一正确合成

```text
CC / FreeCode：模型优先、完整本地生命周期、overflow recovery 语义下限
        +
Codex：typed state、durable backpressure、approval/sandbox/observability 工程增量
        +
Hive Native：principal/RLS、Memory/Knowledge、artifact refs、A2A/Workflow、企业治理
        =
CCPlus：普通单 Agent 不弱于 CC，极端规模和企业协同明显强于 CC，且不以盲删能力换稳定
```

三个“不允许”的捷径：复制 CC 的无界 fan-out；复制 Codex 的盲截断/omit；保留 Hive 当前 pre-model Prompt hard cap。

## 5. 极端场景的源码裁决

### 5.1 256K、400 Skill、200 MCP 与巨大 Memory

1. `backend/app/runtime/context_budget.py:8-34` 把 system Prompt 固定为 context 的 20%，并 clamp 到 15K–350K chars；各 section 虽已 advisory，`backend/app/runtime/prompt_builder.py:754-817` 最终仍会在模型调用前 hard raise。它既不是 provider 物理事实，也没有 externalize/compact/resume。
2. `backend/app/skills/registry.py:93-129` 和 `backend/app/runtime/prompt_builder.py:410-426` 枚举并 inline 完整 Skill/deferred tool catalog；`backend/app/services/agent_tools.py:596-680,796-862` 的发现面没有 cursor/wave，部分路径删除 limit 后全量返回。400 Skill + 200 MCP 的问题不是“模型一定看不懂”，而是系统先在组装阶段失败或线性膨胀。
3. MCP transport 是 lazy 的，反证了“启动时同步连接 200 个 MCP”这一假设；但 `backend/app/tools/handlers/mcp.py:315-429` 与 `backend/app/services/mcp_client.py:268-358` 在执行时消费缓存 schema/URL，缺 execution-time schema/version fresh-check，schema drift 只退化为泛化 retryable error。
4. `backend/app/memory/retriever.py:38-88,628-640` 会把授权候选与历史 summary 全量取入内存；selector 缺失/失败返回全部候选；`backend/app/memory/assembler.py:47-83` 的 budget 是 advisory。这保住了“不能机械 top-k 丢证据”的语义，却没有 iterative retrieval、coverage page 和 resident-memory 上界，规模从 `10^3` 到 `10^6` 会线性放大进程与 Prompt 压力。

结论：正确修复不是重新引入 first-N，而是“全量可发现、分层描述、schema/content 按需加载、coverage 诚实、原文 durable”。

### 5.2 单模型输出、tool rounds 与失败感知

1. `backend/app/kernel/engine.py:102-111,3461-3504` 的 output-cap continuation 固定最多 3 次；第 4 次 `finish_reason=length` 可在 `backend/app/kernel/turn_orchestrator.py:1750-1760` 落成正常 final，`backend/app/kernel/llm_client.py:518-540` 还会加入平台 marker。partial output 没有形成通用 durable continuation state。
2. `backend/app/kernel/turn_orchestrator.py:951-966,2521-2530` 的 `max_tool_rounds` 是直接 cliff，平台写固定终止文案；`backend/app/services/web_chat_run_orchestrator.py:783-818` 仍依赖 prose/prefix 分类，tool budget 命中可被持久化成 completed assistant，而不是模型可见的 typed pressure。
3. `turn_token_budget` 在 `backend/app/runtime/invoker.py:96-108,465-470` 被计算和存储，但没有 live enforcement consumer；这说明“配置存在”与“真实边界”已经漂移。
4. `backend/app/kernel/turn_orchestrator.py:498-555,831-876` 的 `collected_parts`、streamed chunks/thinking 无统一上界；pressure event 多为 debug-only，没有 material pressure epoch 的模型可见聚合。

结论：output cap、tool round、retry cap 只能结束 attempt 或自动 retry。任务应进入 `pressure/backpressured/paused`，携带 checkpoint、coverage、remaining work 与下一步，而不是让平台代写“我做不到”。

### 5.3 单个 root Session 的 100 个 child 返回爆炸

1. child/delegation/workflow 完整原始结果会沿 `RuntimeTask.result_summary → completion outbox.summary → parent continuation Prompt` 传播；active parent 时又逐条追加到 `RuntimeTask.metadata_json.pending_user_messages`，下一轮在 `backend/app/services/web_chat_runtime.py:1104-1153` 一次性全量 drain。100×1 MiB 会同时放大 JSONB、Python resident memory 和 Prompt。
2. `backend/app/kernel/turn_orchestrator.py:1864-2025,2246-2306` 虽能为大文本工具结果落 hash/artifact pointer，但 full raw result 仍保留在 callback、`done_payload.result` 与 `collected_parts`；media/base64 路径没有同等 eviction。
3. 每个 child terminal 都产生独立 continuation delivery intent；`backend/app/services/runtime_notification_outbox.py:245-286,530-680` 全局 FIFO、每批 20、串行 delivery。parent 活跃时 intent 会进入同一 run 的 mid-run mailbox，因此不必然形成 100 次父模型调用；但系统没有 parent/root integration epoch、bounded claim page 或 result manifest，同秒完成仍会形成 notification backlog、mailbox burst，并可能在 parent idle/active 切换时产生多次 continuation。
4. active parent 的 `pending_user_messages` 使用 JSON list 的 read-copy-write + commit；多 worker 同时追加没有 row lock/CAS，存在 lost update。即使不 overflow，也可能静默丢 child result。

结论：semaphore 只限制“同时跑几个”，不能证明系统有界。必须同时有 admission ledger、durable queue、result manifest、per-parent pressure epoch、coalesced wake、partial coverage、weighted fairness 和 drain convergence。

### 5.4 Admission、ghost child、cycle 与 Workflow

1. async delegation 可在 durable RuntimeTask 创建失败后仍发 queued event 并返回 handle，形成 ghost delegation；background subagent 先 commit child ChatSession、后建 RuntimeTask，后者失败留下 ghost child session。
2. foreground subagent 的 budget approval 在 `backend/app/tools/handlers/subagent.py:794-841` 只返回 `waiting_budget_approval`，批准前没有持久化 exact child intent/RuntimeTask；“批准后自动继续”没有对应 durable resume object。
3. web chat root budget 在 `backend/app/services/web_chat_runtime.py:124-189` 创建失败时吞异常并返回 `None`；随后 RuntimeTask/LLM/tool 路径以无 `budget_run_id` 继续。`decide_budget_service_failure()` 要求交互回复可继续但 work-amplifying tools 必须禁用，这个决定没有在该入口被消费，形成预算治理 fail-open。
4. durable `delegation_chain` 被写入但不参与拒环；真正 cycle guard 是 process-local visited set，finally 即移除，跨 async queue/restart 的 A→B→A 只靠固定 depth 停止。
5. generic RuntimeTask terminal update 没有单调 CAS；远端 cancel/kill 后的 late completion 可覆盖 terminal 并再次发 outbox。web chat 私有路径的 preserve-killed 不能证明 A2A/subagent 通用路径成立。
6. Workflow 固定 fanout 128、concurrency 128、leaf 512、wallclock 86400、token 16M 在 preflight 直接 admission error；这些默认值不是 provider/authority fact，也没有 split/defer/replan。fanout 会一次创建全量 coroutine/raw results，任一 failure 可把 step 标 failed，成功 leaf 只留 journal，不进入完整 parent outcome/coverage。
7. runtime budget breaker 用固定 failures/reconciliation/failure-ratio 机械 hard stop，pending tasks 被 cancel，没有模型 summary/recovery lane。
8. Agent Team 顺序逐 member 启动；中途异常保留已启动前半，后半没有 requested/admitted/deferred ledger，父返回也只有瞬时 status。

### 5.5 跨钉钉、飞书、Slack、Web 的 A2A

Hive 已有各渠道 ingress、per-session `delivery_target_json`、delivery outbox、Agent session 与 A2A primitives，但当前没有一个 root task 下的跨渠道 causal DAG、逐 hop principal/delegation/policy frame、统一 result manifest 和多 destination delivery ledger。`send_channel_message` 主要依赖当前 ContextVar `channel_delivery_target`，它适合“回复当前会话”，不足以表达 A 去钉钉、B 去飞书、C 去 Slack、D 在 Web 汇总的显式 route plan。

因此这里必须分账：

- 已存在生产路径但断裂的部分计 breakpoint：E-1 requester/creator 漂移、P1-004 execution frame 丢失、channel queue 无公平、A2A cycle/terminal/admission、fan-in/wake；
- “同一 root task 的多渠道编排与统一 causal/delivery contract”当前没有完整实现，记 `MISS-XCHANNEL-A2A-001`，不拿 Missing 凑断点数。

Agent work 与 channel delivery 必须是两个正交状态机：Agent 可以完成但某渠道 delivery 失败；某渠道成功也不能把 root task 标 complete。每次 hop 都要 fresh-check requester、tenant、delegation、sensitivity/residency 与 credential reference，不能用 same owner 自动放权。

## 6. 本轮新增的 22 个 canonical leaf

| ID | P | 独立断裂 seam | 主要场景 | 完整修复方向 |
|---|---:|---|---|---|
| XCB-CTX-001 | P1 | 20% system Prompt pre-model hard cap | X-CTX-01/02/03、X-CAP-01 | 删除方便性 hard raise；建立 effective context ledger、物理窗 preflight、externalize/model-led compact |
| XCB-CAP-001 | P1 | Skill/MCP catalog 全量 inline/无 wave/cursor | X-CAP-01/02、X-DISC-01 | 全量可发现的 tiered catalog；名称/namespace 常驻，描述/schema 按需 load，coverage ledger |
| XCB-MEM-001 | P1 | Memory 全量候选/resident 聚合，budget 不约束 | X-MEM-01 | iterative paged retrieval + source refs + conflict coverage；不机械 top-k 冒充完整 |
| XCB-OUT-001 | P1 | 固定 3 次续写后截断可被当 final | X-OUT-01、X-ONE-01 | durable ordered partial output、continuation cursor、same-model resume、typed pause |
| XCB-LIM-001 | P1 | tool-round cliff + platform final；token budget 假接线 | X-LIM-01、X-BUD-01 | attempt/task 分离；typed pressure；移除平台语义终答；清理或真实接线 dead budget |
| XCB-RESULT-001 | P1 | raw tool/media result 多份驻留，artifact pointer 未替换内部副本 | X-RESULT-01、X-FAN-01 | durable blob/ref 后释放 raw resident copy；media 同契约；按范围读取 |
| XCB-MCP-001 | P2 | MCP schema/credential execution-time fresh-check 缺失 | X-MCP-01、X-DISC-01 | lazy transport 保留；调用前核 schema/version/auth；typed drift/reauth |
| XCB-OBS-001 | P1 | stream/parts 无界，pressure 不对模型形成有界 epoch | X-FAN-05、X-OBS-01/02 | raw event durable，material transition 聚合，Prompt 只进 bounded epoch manifest |
| CONC-FANIN-001 | P1 | child/delegation/workflow full result 直接进 parent Prompt | X-FAN-01/02 | result manifest + expected/received/late/duplicate coverage + LLM batch integrator |
| CONC-WAKE-002 | P1 | one terminal = one independent delivery intent；active parent 仅偶发汇入同一 mailbox，无 root integration epoch | X-FAN-02/05、X-QUEUE-01 | per-root pressure epoch/coalesced manifest wake；runtime fairness 另账；drain proof |
| A2A-ADMISSION-001 | P1 | delegation 无 RuntimeTask 仍返回 queued handle | X-LIVE-01、X-A2A-01 | reserve + durable enqueue 原子提交后才宣告 admitted；失败记 not_admitted |
| SUBAGENT-ADMISSION-001 | P1 | child session 先 commit，RuntimeTask 失败留 ghost | X-FAN-02、X-LIVE-01 | session+task+budget 同事务或 compensation tombstone；expected set 后置 |
| A2A-CYCLE-001 | P1 | process-local visited set 无法阻止 durable/restart 环 | X-FAN-03、X-LOOP-01 | durable ancestor/path edge + 最小成环边拒绝；depth 仅作软调度水位 |
| A2A-TERMINAL-001 | P1 | kill/cancel 可被 late completion 覆盖 | X-FAN-04、X-A2A-02 | terminal monotonic CAS、late-result ledger、outbox idempotency |
| CHANNEL-FAIRNESS-001 | P1 | ingress/delivery 全局 FIFO，无 tenant/channel 公平 | X-A2A-02、X-QUEUE-01 | per-tenant/channel admission + weighted fair queue + control-plane reserve |
| TEAM-FANOUT-001 | P1 | Team 顺序半启动，无全量 admission/coverage ledger | X-FAN-02/03 | 先登记 requested set，再逐项 admitted/deferred；partial start 可恢复 |
| WF-HARDLIMIT-001 | P1 | 固定 fanout/leaf/wallclock/token 直接 admission error | X-WF-01、X-LIM-01 | 内部值降为 soft watermarks；显式 contract 才 hard；自动 split/defer/checkpoint |
| WF-PARTIAL-001 | P1 | 全量 coroutine/raw results；部分成功不进入父 outcome | X-WF-01、X-FAN-02 | durable leaf journal + partial join policy + coverage/result refs + resumable scheduler |
| BUD-BREAKER-001 | P1 | 固定失败计数/比例机械终止并 cancel pending | X-BUD-02、X-LIVE-01 | breaker 只停自动 admission/retry；保留 task，调用模型 recovery/summary lane |
| BUD-ROOT-001 | P1 | root budget service failure 后无 budget 继续 work-amplifying path | X-SAFE-01、X-LIVE-01 | 消费 `decide_budget_service_failure`；允许直接回答但禁用扩增工具，typed unavailable + resume |
| SUBAGENT-APPROVAL-001 | P1 | foreground approval 没有 durable exact intent/resume object | X-LIVE-01、X-REC-01 | approval 前持久化 requested child、reservation、arguments/hash；批准后幂等 admission |
| CONC-MAILBOX-001 | P1 | pending JSON list 并发 read-copy-write 可丢 child message | X-FAN-01/02/05 | 独立 mailbox row + unique idempotency key + claim/lease/CAS；分批 drain |

## 7. 全部 94 个 leaf 的机器可重算账本

以下每一行只代表一个 canonical leaf。`inherited-recheck` 表示它来自同日旧报告，本轮没有重新执行其全部验收；它仍是当前工作账本，但对应施工组开工前必须按当前 checkout 重验。family、alias、场景、Missing 和 coverage gap 不在这里计数。

<!-- canonical-ledger-start -->
- P0 | P0-F1 | current-confirmed | agent-controlled `web_fetch` SSRF
- P1 | P0-F2 | inherited-current-evidence | Alembic 增量迁移失败仍启动
- P1 | E-1 | inherited-current-evidence | durable subagent requester 被 creator 顶替
- P1 | P1-004 | inherited-current-evidence | A2A inner effect 丢 outer execution frame
- P1 | P1-F4 | inherited-current-evidence | RecoveryManifest 缺恢复授权绑定
- P1 | C-BP1 | inherited-current-evidence | terminal hook 同步 T2 LLM 阻塞完成
- P1 | P1-008 | inherited-current-evidence | Memory dependency failure 冻结无关 effect
- P1 | P1-017 | inherited-dirty-fix-unaccepted | transcript commit 与 T0 wake 可见性
- P1 | G-01A | split-from-G-01 | 平台 failure prose 冒充 assistant/final author
- P1 | KB-AUTH-001 | split-from-D-KB1-D-KB2 | cross-principal PKB requester/grant/ceiling
- P1 | KB-EXTRACT-001 | split-from-D-KB1 | sensitivity canonical enum 与 extraction blocklist 漂移
- P1 | KB-PROP-001 | split-from-D-KB1 | sensitivity/provenance 未贯穿 transcript/T0/T2/outbound
- P2 | A-01 | inherited-current-evidence | 模型正文前缀机械判失败
- P2 | A-03 | inherited-current-evidence | compaction active projection/replay 边界漂移
- P2 | A-04 | inherited-current-evidence | Redis 降级取消不可观测/phase 漂移
- P2 | C-BP2 | inherited-current-evidence | CORE_DAEMON 默认关闭隐藏自进化车道
- P2 | C-BP3 | inherited-current-evidence | T2 retry 耗尽后永久 held
- P2 | C-BP4 | inherited-current-evidence | T3 profile 锁外直写
- P2 | C-BP5 | inherited-current-evidence | T0 hash chain 只写不验
- P2 | C-BP6 | inherited-current-evidence | capability 三表无真实回读消费者
- P2 | F-OBS1 | inherited-current-evidence | T0 health 保留陈旧 last_error
- P2 | B-02 | inherited-current-evidence | unavailable 与 denied 在证据层合并
- P2 | B-03 | inherited-current-evidence | governance outcome 从平台文本反推
- P2 | E-2 | inherited-current-evidence | Hive Connect local A2A 不 wake parent
- P2 | AUDIT-IMM-001 | split-from-P1-F5 | 审计表数据库层可修改
- P2 | AUDIT-TENANT-001 | split-from-P1-F5 | tenant=None 安全审计静默丢弃
- P2 | F-PLAINTEXT | inherited-current-evidence | agent tool config 明文 MCP credential
- P2 | P2-F8 | inherited-current-evidence | `rg` 参数缺 `--` 可 flag injection
- P2 | P2-F6 | inherited-current-evidence | model config 写入缺 cross-tenant reference 校验
- P2 | KB-CONTRACT-001 | split-from-D-KB1 | Knowledge tool description/spec/implementation 不一致
- P2 | G-02 | inherited-current-evidence | production i18n key 缺失
- P2 | H-404a | inherited-current-evidence | Messages read-state UI/backend 404 契约
- P2 | H-404b | inherited-current-evidence | channel test UI/backend 404 契约
- P2 | P2-018 | inherited-current-evidence | canonical 文档引用不存在测试路径
- P2 | G-03 | inherited-recheck | 旧报告 UI leaf G-03
- P2 | G-04 | inherited-recheck | 旧报告 UI leaf G-04
- P2 | G-05 | inherited-recheck | 旧报告 UI leaf G-05
- P2 | G-06 | inherited-recheck | 旧报告 UI leaf G-06
- P2 | G-07 | inherited-recheck | 旧报告 UI leaf G-07
- P2 | G-08 | inherited-recheck | 旧报告 UI leaf G-08
- P2 | G-09 | inherited-recheck | 旧报告 UI leaf G-09
- P2 | G-10 | inherited-recheck | 旧报告 UI leaf G-10
- P2 | G-01B | split-from-G-01 | UI 以 `includes('expired')` 决定 hard state
- P3 | B-01 | inherited-recheck | HR 受信固定业务体绕统一 tool throat
- P3 | A-05 | inherited-recheck | 旧报告单 Agent leaf A-05
- P3 | A-06 | inherited-recheck | 旧报告单 Agent leaf A-06
- P3 | A-07 | inherited-recheck | 旧报告单 Agent leaf A-07
- P3 | A-08 | inherited-recheck | 旧报告单 Agent leaf A-08
- P3 | B-04 | merged-alias-A-09 | 结果自然语言 failure 词仅驱动 warn/counter
- P3 | E-3 | inherited-recheck | 旧报告 Hive Native/死治理 leaf E-3
- P3 | E-4 | inherited-recheck | 旧报告 Hive Native/死治理 leaf E-4
- P3 | E-5 | inherited-recheck | 旧报告 Hive Native/死治理 leaf E-5
- P3 | E-6 | inherited-recheck | 旧报告 Hive Native/死治理 leaf E-6
- P3 | E-7 | inherited-recheck | 旧报告 Hive Native/死治理 leaf E-7
- P3 | C-BP8 | inherited-recheck | 旧报告 Hive Native leaf C-BP8
- P3 | C-BP9 | inherited-recheck | 旧报告 Hive Native leaf C-BP9
- P3 | C-BP10 | inherited-recheck | 旧报告 Hive Native leaf C-BP10
- P3 | C-BP11 | inherited-recheck | 旧报告 Hive Native leaf C-BP11
- P3 | C-BP12 | inherited-recheck | 旧报告 Hive Native leaf C-BP12
- P3 | B-05 | inherited-recheck | 旧报告治理 leaf B-05
- P3 | B-06 | inherited-recheck | 旧报告治理 leaf B-06
- P3 | B-07 | inherited-recheck | 旧报告治理 leaf B-07
- P3 | G-11 | inherited-recheck | 旧报告 UI leaf G-11
- P3 | G-12 | inherited-recheck | 旧报告 UI leaf G-12
- P3 | G-13 | inherited-recheck | 旧报告 UI leaf G-13
- P3 | G-14 | inherited-recheck | 旧报告 UI leaf G-14
- P3 | G-15 | inherited-recheck | 旧报告 UI leaf G-15
- P3 | G-16 | inherited-recheck | 旧报告 UI leaf G-16
- P3 | G-17 | inherited-recheck | 旧报告 UI leaf G-17
- P3 | G-18 | inherited-recheck | 旧报告 UI leaf G-18
- P3 | D-KB3 | inherited-recheck | 旧报告 Knowledge leaf D-KB3
- P3 | D-KB4 | inherited-current-evidence | Knowledge handler 把 not-found/denied 合为自由文本 warning
- P1 | XCB-CTX-001 | added-current-confirmed | pre-model 20% Prompt hard cap
- P1 | XCB-CAP-001 | added-current-confirmed | capability catalog 无 progressive wave/cursor
- P1 | XCB-MEM-001 | added-current-confirmed | Memory 全量候选与 resident 聚合
- P1 | XCB-OUT-001 | added-current-confirmed | output continuation 固定三次后假 final
- P1 | XCB-LIM-001 | added-current-confirmed | tool-round cliff/平台终答/预算假接线
- P1 | XCB-RESULT-001 | added-current-confirmed | raw tool/media result 多副本驻留
- P2 | XCB-MCP-001 | added-current-confirmed | MCP execution-time schema/auth fresh-check 缺失
- P1 | XCB-OBS-001 | added-current-confirmed | stream/parts 无界且 pressure observation 缺失
- P1 | CONC-FANIN-001 | added-current-confirmed | full child result 直接进入 parent context
- P1 | CONC-WAKE-002 | added-current-confirmed | terminal wake storm 无 coalescing/fairness
- P1 | A2A-ADMISSION-001 | added-current-confirmed | queued ghost delegation
- P1 | SUBAGENT-ADMISSION-001 | added-current-confirmed | ghost child session
- P1 | A2A-CYCLE-001 | added-current-confirmed | durable/restart cycle guard 缺失
- P1 | A2A-TERMINAL-001 | added-current-confirmed | late completion 可覆盖 cancel/kill
- P1 | CHANNEL-FAIRNESS-001 | added-current-confirmed | channel ingress/delivery 全局 FIFO
- P1 | TEAM-FANOUT-001 | added-current-confirmed | Agent Team 半启动无 coverage ledger
- P1 | WF-HARDLIMIT-001 | added-current-confirmed | Workflow 固定方便性上限 hard fail
- P1 | WF-PARTIAL-001 | added-current-confirmed | Workflow partial join/result contract 缺失
- P1 | BUD-BREAKER-001 | added-current-confirmed | runtime breaker 机械终止/cancel
- P1 | BUD-ROOT-001 | added-current-confirmed | budget root failure work-amplification fail-open
- P1 | SUBAGENT-APPROVAL-001 | added-current-confirmed | foreground approval 无 durable intent
- P1 | CONC-MAILBOX-001 | added-current-confirmed | parent mailbox JSON lost-update race
<!-- canonical-ledger-end -->

## 8. 已知缺失、排除与 coverage gap

### 8.1 已知缺失，不计入 94

1. `MISS-EVAL-001`：真实行为级 self-evolution eval、version-pinned baseline、LLM referee、behavior receipt 消费；旧 alias `C-BP7`。
2. `MISS-EK-001`：Enterprise Knowledge 的 organization authority、ACL/RLS、retention、legal hold、version/deletion propagation。
3. `MISS-AIASSET-001`：AI Asset 当前未覆盖类型的完整治理面。
4. `MISS-RETENTION-001`：跨 Memory/Knowledge/Artifact/Audit 的 retention/deletion/export/legal hold 产品闭环。
5. `MISS-XCHANNEL-A2A-001`：同一 root task 下显式多渠道 route plan、causal DAG、统一 result/delivery ledger。

这些 Missing 不阻塞 P0/P1 修复独立发布，但 `MISS-EVAL-001` 未闭环时禁止宣称 Goal 1 / North Star 已完成；Goal 2 Missing 未闭环时禁止宣称产品总目标完成。

### 8.2 本轮未证实范围

1. codebase-memory MCP 多次返回 `Transport closed`；按项目规则记录工具缺口后改用当前源码的 `rg`/定点读取，调用链置信度因此下调。
2. 单个 root Session 的 100 个真实 child、100×1 MiB 同秒 completion、1 万条 streaming event 的真实容量曲线未执行；静态无界路径已确认，吞吐/时延数值未确认。平台 fleet 中已注册 Agent 的总量是另一条规模轴。
3. 400 个真实 Skill、200 个真实 MCP server、`10^6` Memory 的生成 fixture 未落地；现有定向测试只证明当前“全量保留/硬 raise”契约。
4. 钉钉/飞书/Slack/Web 真实 credential、rate limit、auth revoke、duplicate webhook 与 ack-loss 故障注入未执行。
5. CC current binary、Codex、Hive 的同模型同 fixture paired replay 未执行；本轮只有源码对照。
6. 本轮未访问 production queue/DB/provider/channel telemetry，也未做三服务部署；不能把源码结论冒充生产负载证据。

## 9. 最终一次性整改顺序

下面是依赖顺序，不是把 94 项绑成一个不可发布的大列车。每个开始施工的 leaf 或同根家族都必须在同一轮交付 Red→Green、边界/故障测试、migration/backfill、observability、recovery/rollback、真实消费、文档回填和发布验收；P0/P1 自身闭环后立即独立发布，不等待 P3/UI/Missing。

### Group 0 — 最小事实冻结，不拖住 P0

- 记录每个 leaf 开工时 HEAD、相关文件 hash/status/diff owner、原始失败、权威事实源与独立 commit 边界；不得 reset 或覆盖当前脏工作树。
- 将提示词、本报告和 machine ledger 显式纳入 Git truth；CI 校验 canonical ID 唯一、severity count 与 ledger delta。
- 建立 synthetic provider/channel/MCP、virtual clock、fault injector 与 1/10/25/50/100 capacity harness；真实付费/外发压测单独 approval。

### Group 1 — 立即封安全与 fail-open

1. `P0-F1` 统一 governed egress transport：URL normalization、全部 DNS result、actual peer、每个 redirect、proxy、DNS rebinding、credential stripping、timeout/hop/response byte/decompression limit、typed deny receipt。完成后立即发布。
2. `P0-F2` migration fail-closed：Alembic 增量失败不得启动；migration owner 与 runtime role 分离；隔离库 fault injection、回滚与再升级。
3. `BUD-ROOT-001`：预算 plane unavailable 时交互 Agent 可以给直接回答，但所有 work-amplifying tools 必须从权威 capability intersection 中移除，并向模型/UI 暴露 typed unavailable 与恢复条件。
4. `E-1 → P1-004 → KB-AUTH-001/KB-PROP-001 → P1-F4`：先建立唯一 requester/principal/delegation frame，再贯穿 subagent/A2A/Knowledge/recovery；不能用 creator、same owner 或客户端字段补身份。

### Group 2 — 建立统一 pressure/terminal contract

- 一个最小 `ExecutionPressureEnvelope`（或复用现有 typed contract）承载 hard/soft source、scope、used/reserved/remaining、task/attempt、preserved refs、retryability、next actions 与 progress certificate。
- 只有 provider/authority/effect/explicit policy fact 可产生 hard outcome；每个 hard outcome 指向 authoritative receipt。
- `XCB-LIM-001`、`XCB-OUT-001`、`BUD-BREAKER-001`、`WF-HARDLIMIT-001` 全部改为 attempt/automatic-retry 边界；平台不再写语义 final。
- failure/pressure 在当前轮能继续时交给模型；已无法再调用 provider 时先给 UI/operator typed state，并保证下次 resume 第一轮进入模型。

### Group 3 — Admission、预算守恒和终态单调

- parent/child budget 使用 reserve→durable enqueue/admit→commit/release；守恒式为 `requested = admitted + deferred/not_admitted`、`reserved = committed + released + live_reserved`。
- 修复 A2A/subagent ghost、foreground approval intent、Agent Team 半启动；未 admission 的 child 永不进入 expected set。
- 等 approval/channel/budget/fan-in 时释放 worker/DB/connector permit；lease 与 sweeper 可恢复。
- durable ancestor/path cycle detection 只拒最小成环边；terminal CAS 保证 killed/cancelled 不被 late result 覆盖，late result 单独保留。

### Group 4 — Durable result plane 与 fan-in backpressure

- 所有 tool/child/workflow result 先完整落 blob/artifact，返回 `result_ref + sha256 + bytes/tokens + source refs + coverage + range reader`；落盘后释放进程内 raw copy。
- 建立 parent integration epoch：同一 root/parent 的 terminal/progress 先聚合成一份 manifest，再唤醒一次；父 LLM 分批综合，平台不写摘要结论。
- 把 `pending_user_messages` 从 RuntimeTask JSON list 拆成有唯一 idempotency key、sequence、claim/lease 的 mailbox row；分批 drain，不一次灌入 Prompt。
- ingress、runtime、completion、delivery queue 使用 per-tenant/channel weighted fairness 与 control-plane headroom；验证 load stop 后 drain convergence。

### Group 5 — Context capacity 与 capability discovery

- 移除 20% convenience hard cap；以 provider effective context contract 做真实 preflight。目标占用率只触发整理，不终态化 task。
- Prompt ledger 分别统计 frozen instructions、history、tool schema、catalog、Memory refs、media、result refs、compaction summary、output reserve；provider success/failure 都留 receipt。
- Skill/MCP/Agent/Workflow 使用 namespace-searchable hierarchical catalog；所有名称可发现，description/schema/reference 逐层加载；cache key 绑定 authority/version/model/context。
- Memory 使用 iterative retrieval pages、source refs、coverage/conflict ledger；selector failure 返回可恢复 page/manifest，不在进程中 gather 全库，也不机械 top-k 宣称完整。
- output partial、stream replay、413、single-model unavailable 使用 durable cursor/checkpoint；禁止秘密换模。

### Group 6 — 跨渠道 A2A

- 建立 root execution frame：root task/requester/tenant、delegation chain、policy/sensitivity ceiling、budget、causation/correlation、result manifest、route plan。
- 每个 channel lane 有独立 `AgentWorkState` 与 `DeliveryState`；channel credential 只以平台 reference 使用，不进入模型/跨 hop payload。
- webhook authenticity/nonce/dedupe、sequence/out-of-order、ack loss、rate limit、auth expiry/revoke、residency/sensitivity 在每次 ingress/effect 前 fresh-check。
- root final destination 显式、可重选；禁止按昵称/owner 猜 requester，也禁止成功后向所有渠道广播。

### Group 7 — Memory、Evidence 与恢复

- `C-BP1` 改为 terminal commit 后持久化 T2 queued job/outbox，再 broadcast done；worker+sweeper 执行 LLM 打包，禁止裸 `asyncio.create_task`。
- 完成 P1-017 clean/full/deployed 验收、RecoveryManifest 授权、T0 hash replay、audit immutable/tenantless fallback、dependency-specific Memory availability。
- 所有机械 fallback 只能 hold/quarantine/retry/request review，保留原证据并重新进入 LLM-primary path。

### Group 8 — 产品消费与 UI

- pressure/partial/approval/unavailable/denied/late/delivery failure 使用 typed ThreadItem，不伪装成 assistant。
- parent/child/channel/workflow 页面展示 expected/received/failed/late、保留结果、remaining budget、resume owner/condition 和可执行 recovery action；raw IDs/payload 渐进披露。
- Workspace/Artifact/result manifest、Messages read state、channel test、i18n 与旧 G 系列逐项闭环。

### Group 9 — Goal 1 行为门与剩余账本

- `MISS-EVAL-001` 在同 model/config/authorized tools/budget/task corpus 上真实执行 candidate 与 baseline；LLM referee 依据 versioned rubric 和完整 evidence 生成 verdict/source refs，平台只验 schema/binding/runner facts/rollback。
- reviewer unavailable 或 coverage 不足只能 hold；promotion 必须消费 behavior receipt，并经 provisional/rollback fault injection。
- 对 inherited P2/P3 逐项重验、合并或删除；数字服从证据。Goal 1 完成后，才把主建设方向转向 Goal 2/UI/KISS。

## 10. 极端测试与验收门

修订后的提示词包含 35 个场景：`X-FAN-01..05`、`X-CAP-01..02`、`X-DISC-01`、`X-MCP-01`、`X-MEM-01`、`X-CTX-01..03`、`X-OUT-01`、`X-ONE-01`、`X-RESULT-01`、`X-BUD-01..02`、`X-LIM-01`、`X-LIVE-01`、`X-QUEUE-01`、`X-SAFE-01`、`X-A2A-01..05`、`X-LOOP-01`、`X-INJ-01`、`X-OBS-01..02`、`X-REC-01`、`X-CACHE-01`、`X-WF-01`、`X-CCP-01`。

本轮定向测试没有证明系统已经通过这些场景；它们证明当前代码确实保留了本报告指出的 hard raise、全量 catalog/Memory、固定 continuation 与 limit 行为：

```bash
cd backend
source .venv/bin/activate
pytest -q \
  tests/runtime/test_prompt_builder.py::test_final_system_prompt_budget_never_blind_trims_frozen_contract \
  tests/runtime/test_prompt_builder.py::test_skills_catalog_section_preserves_complete_discovery_index \
  tests/runtime/test_prompt_builder.py::test_agent_skill_catalog_preserves_complete_ranked_descriptions \
  tests/runtime/test_context_budget.py::test_compute_context_budget_256k_research_is_more_aggressive \
  tests/runtime/test_context_budget.py::test_compute_context_budget_1m_model_uses_long_context_capacity \
  tests/runtime/test_provider_prompt_ledger.py \
  tests/kernel/test_engine.py::test_kernel_continues_streaming_output_after_output_cap \
  tests/kernel/test_engine.py::test_large_tool_result_evicted_in_kernel_loop \
  tests/kernel/test_engine.py::test_persist_memory_called_on_max_rounds_exceeded \
  tests/kernel/test_engine.py::test_turn_token_budget_does_not_preempt_tool_followup \
  tests/services/test_agent_tools.py::test_available_deferred_tool_names_never_applies_platform_top_n
# 中断后当前工作树重跑：12 passed in 0.56s

pytest -q \
  tests/memory/test_retrieval_pipeline.py::test_model_selector_failure_returns_every_authorized_candidate_observably \
  tests/memory/test_retrieval_pipeline.py::test_missing_selector_model_returns_all_candidates_instead_of_mechanical_top_k \
  tests/memory/test_assembler.py::TestAssembleBudgetAdvisory \
  tests/runtime/test_session_context_controller.py
# 中断后当前工作树重跑：13 passed in 0.32s
```

施工后的验收必须满足：

1. 1/10/25/50/100 capacity curve 中 resident memory、DB connections、queue depth、Prompt bytes 与 wake count 均有明确 envelope；100-way fan-in 不把 raw N×M bytes inline 给 parent。
2. `requested = admitted + deferred/not_admitted`；`expected = received + terminal_missing + live`；无 ghost、lost wake、lost mailbox、重复 effect。
3. 负载停止或依赖恢复后可在虚拟时钟/可测 SLA 内 drain；nonterminal state 的 progress certificate 覆盖率 100%。
4. threshold−1/=/+1 与 0.5×/1×/2× 变异只改变 pressure/batch/admission，不产生无依据 task semantic cliff。
5. 决定性 evidence 位于最后一页/冷 Memory/late child 时仍可发现；所有外置内容有 sha256、source refs、range read 与 coverage。
6. 单模型无 fallback、provider unavailable、output length、413、restart、cancel、late callback 下可保留和恢复。
7. 跨渠道 duplicate/out-of-order/ack loss/auth revoke/partial delivery 不漂移 principal，不重复外发，不把 delivery 冒充 task completion。
8. 模型 final bytes 除精确 unauthorized secret redaction 外保持 byte-faithful；平台状态永不写成 assistant 结论。
9. `LB-1..LB-10` 全绿；任何 UNVERIFIED 都阻止对应能力的“极端规模闭环”声明，但不阻塞已完成 P0/P1 单项发布。

## 11. 迁移、回填、可观测性与回滚

- mailbox/result/coverage/admission 新表先 dual-read，不 dual-author；backfill 从 RuntimeTask/outbox/child sessions 生成 refs/sequence，无法证明的 pending 项进 quarantine，不猜 completed。
- 历史 large result 只生成 ref/hash，不重写原 transcript/T0；历史 inline bytes 保留到 retention policy 安全删除。
- 历史 budget-less RuntimeTask 标 `legacy_budget_unbound`；恢复时从当前 policy 重建，无法绑定则禁 work-amplification 并请求 operator，不默认放行。
- 历史 approval 无 exact intent 的记录不能自动执行；保留证据并要求重新确认。
- terminal status migration 增加 CAS/version 与 late-result 表；回滚不得恢复“late completion 覆盖 killed”。
- capability/Memory index 可重建；canonical source 始终是授权原文/Memory vault，不把向量索引变 truth。
- 新指标至少包含：pressure state/epoch、admission/deferred、queue age/tenant fairness、fan-in coverage/bytes、wake coalesce ratio、context component tokens、externalization bytes、resume latency、ghost/lost-update detector、delivery task-state cross product。
- rollback 只能停新 consumer/切回兼容读，并保留 durable job/ref/coverage；不得丢 queued work、删除 evidence 或恢复平台伪造 final。

## 12. 完成声明的四层口径

| 层级 | 完成条件 | 不代表什么 |
|---|---|---|
| 单 leaf/家族闭环 | 七原子、Red→Green、migration/backfill、fault injection、observability、rollback、消费与发布全过 | 不代表 94 清零 |
| 程序账本完成 | 冻结快照上 inventory 重认证，open breakpoint=0，所有 delta 有证据 | 不代表 Missing 已建设 |
| Goal 1 / North Star 完成 | Goal 1 断点与 `MISS-EVAL-001` 闭环，真实行为对 CC/lean benchmark 非劣 | 不代表 Goal 2 完成 |
| 产品总目标完成 | Goal 1 + Goal 2、Enterprise Knowledge、AI Asset、retention/legal hold、跨渠道产品闭环 | 不能由 94/94 或单个 eval 代替 |

## 13. 置信度与最终裁决

- hard/soft limit 与 context/capability 静态链：高；有当前源码与 25 个定向测试，但真实规模曲线未跑。
- concurrency/fan-in/A2A/Workflow 静态链：中高；生产数据路径已追到 durable task/outbox/parent consumption，真实 100-way/channel fault injection 未跑。
- CC/FreeCode/Codex 源码对照：中高；源码 snapshot 可核，但 current CC binary 与 paired replay 缺失。
- 旧 P0/P1 账本：高到中高；同日当前源码证据较完整。
- inherited P2/P3：中；旧报告没有为所有范围项保留逐 leaf 展开，本报告明确要求开工前重验，未把它们伪装成新一轮全量证明。
- 当前分母：中高的工作账本，不是永久认证。

最终工程裁决：Hive 已经有超越 CC 所需要的原材料——durable RuntimeTask/outbox、artifact refs、Memory/Knowledge、principal/RLS、Workflow/A2A——但还没有把它们收敛成一个可在压力下保持活性的执行契约。下一步不是再加一个更大的 `max_*`，也不是删除安全边界，而是先建立 typed pressure + durable admission/result/coverage + attempt/task 分离，再让 context discovery、fan-in 和跨渠道协同全部消费这套契约。只有这样，Hive 才是在 CC 之上做 CCPlus，而不是在 CC 外面堆一层会把模型卡死的控制面。

## 14. 审查产物与文档验收

- Prompt SHA-256：`c66363d02fd3a7f2b58aa21b95515739c736955e9ccd24f04ad394b3ae4d6be9`；35 个 `X-*` ID 唯一，外层四反引号 `text` fence 成对。
- Context/budget 证据：`.ultra/reviews/20260714-222552-main-extreme-boundary-iter2/review-context-budget.json`；8 个新增 leaf，JSON parse exit 0。
- Concurrency/A2A 证据：`.ultra/reviews/20260714-222552-main-extreme-boundary-iter2/review-concurrency-a2a.json`；14 个 P1 leaf（并发审查 11 个 + 主审补证 3 个），JSON parse、ID uniqueness 与声明计数校验通过。
- Harness baseline 证据：`.ultra/reviews/20260714-222552-main-extreme-boundary-iter2/review-harness-baselines.json`；30 source facts、12 inferences、8 CCPlus requirements、10 个 paired replay specification（全部明确 `unverified`），JSON parse exit 0。
- canonical ledger 校验：94 行、94 个唯一 ID，机械重算为 `P0=1/P1=32/P2=32/P3=29`。
- 中断后复核：133 条文件级源码引用全部解析到当前文件且引用行号未越界，另有 2 条目录级 `rg` 扫描证据单独验证；已把 concurrency artifact 中误写为 `services/workflow_*` 的两个路径校正为当前真实的 `runtime/workflow_*`，断点语义、ID 和计数不变。
- 四份 Markdown 无 trailing whitespace，`git diff --no-index --check` 无 whitespace error；报告引用的关键 Hive/FreeCode/Codex/Hermes 源文件均存在。
- 本轮只修改 Markdown 审查文档并新增 `.ultra/reviews/...` 只读证据 artifact；未修改业务代码、数据库、部署配置或生产数据。由于 docs 被 ignore，交付 owner 审阅后仍需显式纳入 Git truth。

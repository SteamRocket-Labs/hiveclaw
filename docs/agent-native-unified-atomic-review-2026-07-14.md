# CCPlus 统一原子化复审：Fleet、单根 Session、Context 与 Session Truth

> 状态：当前工作账本、Group 0–10 施工入口与后续修复证据总报告；不是实现完成声明
>
> 审查快照：`main@501db6555dae374e5fcf43a6fdcfe8a3dd89343e` + 2026-07-14 当前未提交工作树
>
> 总账编排更新：2026-07-15；本次只补全 Group 路由、规范交叉表、owner map 与证据回填合同，没有把旧源码证据冒充为 2026-07-15 重新全量认证。
>
> 组合输入：`agent-native-atomic-review-2026-07-14.md`、`agent-native-extreme-boundary-atomic-review-2026-07-14.md`、`unified-context-assembly-and-progressive-disclosure-2026-07-14.md`、`session-v2-cc-codex-alignment-contract-2026-07-14.md`、修订后的 `reusable-agent-native-atomic-review-prompt.md`，以及当前 Hive / FreeCode / Codex 本地源码。
>
> 本地基线快照：FreeCode `7dc15d6c8fb0c40c7fcc02ce9b58204324252632`；claw-code Python/Rust `d229a9b022d4845d28a728677e6a6b7c22ec5a2e`；claude-code-org `a99de1bb3c0c301b83b784abbcdb7a3674b2cd45`；Codex `5c19155cbd93bfa099016e7487259f61669823ff`。四个对照仓库的 tracked source 均无 diff；各自仅有未跟踪的本地索引、审查文档或 assistant 配置，不作为源码证据。
>
> 本轮只修改审查与设计文档，不修改业务代码、数据库、部署配置或生产数据。当前工作树包含其它 session 的大量未提交业务改动；本轮没有 reset、覆盖、暂存或归属这些改动。本文的数量和行号绑定上述快照，不是永久 KPI。

## 0. 如何使用这份终极修复报告

本文是后续全量修复的唯一总入口，但不是把所有设计全文复制到一个文件。使用顺序固定为：

1. 打开本文，定位当前 Group、owner leaf、依赖 Group 与 `@文档路由`。
2. 按“必须先读”的顺序读取被 `@` 的规范文档；支持文档只在触及对应子域时读取，历史报告只用于找原始证据，不得覆盖当前源码。
3. 先锁当前 HEAD、工作树、相关文件 hash、运行环境和现有失败；再用 codebase graph 与当前源码重验 leaf，写出 Red。
4. 按 leaf/同根家族一次完成实现、migration/backfill、observability、failure/recovery、真实 consumer/UI 和 rollback；不得只完成 Group 标题中的一部分。
5. 将命令、零失败结果、数据库/事件/trace/截图或生产 canary、commit/deploy、回滚证据写回本文的 Group 证据区与 §12 canonical ledger。
6. 只有 owner leaf 全部满足退出门，才可把 Group 标为 `closed`；Group 顺序表示依赖，不表示必须等待整个 103/103 才发布已独立闭环的 P0/P1。

### 0.1 文档权威层级

| 层级 | 文档 | 作用 |
|---|---|---|
| L0 | `@AGENTS.md`、`@docs/hive-sota-master-goal.md`、`@docs/ccplus-north-star-contract-2026-06-24.md`、`@docs/runtime-model-agency-constraint-audit-2026-07-13.md` | 北极星、Model Agency、CC/Codex/Hive 裁决；任何 Group 不得覆盖 |
| L1 | 本文、`@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`、`@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md` | 施工顺序、Context Resource Plane、Session Event/Item/Reducer 的当前设计权威 |
| L2 | 各 Group 的“必须先读/按需读取”文档 | 子系统合同、迁移、UI、运行与验收细节；只能细化 L0/L1 |
| L3 | `@docs/agent-native-atomic-review-2026-07-14.md`、`@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md`、`@docs/agent-native-atomic-review-501db655.md` 及 archive | 历史证据、旧编号与反例；不得作为当前完成声明 |

冲突裁决固定为：当前源码/运行事实 → L0 → L1 → L2 → L3。文档写着“完成”但当前消费路径或测试不成立时，状态必须回退。

### 0.2 修复证据写回合同

每个 leaf 的证据记录必须包含：

- `leaf_ids`、owner Group、依赖 Group、读取过的 `@docs` 与当前源码路径；
- 修复前 HEAD/worktree/hash、原始症状、Red 命令与正确失败原因；
- 权威事实源、状态机/数据模型/唯一写入口/唯一 consumer 的变化；
- migration、dry-run、backfill、legacy quarantine/cleanup 与 rollback；
- Green、扩展回归、fault injection、性能/容量曲线、observability 与 UI/E2E；
- commit、三服务部署（如适用）、生产 canary/health、证据链接与残余风险；
- `status = open | in_progress | blocked | closed | refuted | missing`、更新时间和证据 owner。

禁止只写“代码已改”“测试通过”或链接一个 commit。缺少任一适用原子时仍是 open/partial，不得把 Group 标绿。

### 0.3 跨仓 `@文档` 快照合同

跨仓设计文档不能绑定某个开发者的绝对文件路径。本文使用 `@hive-connect:<repo-relative-path>` 作为可移植逻辑地址；权威快照是 remote + commit + file SHA-256，当前本机 checkout 只用于读取和复核，不是文档身份。

- remote：`https://github.com/rocky2431/hive-connect.git`
- snapshot commit：`6cf0b591c037c52ab6b0542c1756006023c7f218`
- 当前本机 convenience root：`/Users/rocky243/vc-saas/hive-connect`（不进入 `@` 路由，不作为 CI 必需路径）

<!-- external-doc-registry-start -->
- hive-connect | 6cf0b591c037c52ab6b0542c1756006023c7f218 | AGENTS.md | 6ccb105ee0c6d79e7015e4aec5b66c94bb49f214ce6359d365c1c6befc4dd1fc
- hive-connect | 6cf0b591c037c52ab6b0542c1756006023c7f218 | docs/bridge-protocol.zh-CN.md | 4e407f1b6f5ccb768a19d6627b51e5ac05875ac1910811f0a2f50d0d4f9eb4de
- hive-connect | 6cf0b591c037c52ab6b0542c1756006023c7f218 | docs/plans/2026-03-13-session-resilience-design.md | a0f7665b1c47c69328da2db42e40420df13a76c4504216271d118d4463010709
- hive-connect | 6cf0b591c037c52ab6b0542c1756006023c7f218 | docs/plans/2026-03-12-multi-workspace-design.md | e48540da0bf7d347807f311d42e14de707206ca457c17fd89133be2e4b9c12f9
- hive-connect | 6cf0b591c037c52ab6b0542c1756006023c7f218 | docs/dingtalk.md | 8e2bf50198c8c857947928b9d54622ea72ddd0e68b3f746d7fee567ba5829fda
- hive-connect | 6cf0b591c037c52ab6b0542c1756006023c7f218 | docs/feishu.md | 4c3e6ff1aa33dea96584a0706f927a8cc4e795f1d35affafcf335201ff86f985
- hive-connect | 6cf0b591c037c52ab6b0542c1756006023c7f218 | docs/slack.md | f798a0167943d2f8cdd2f4f61e6de99cb5085de379b8659c7e9a9a3476c38217
- hive-connect | 6cf0b591c037c52ab6b0542c1756006023c7f218 | docs/management-api.zh-CN.md | 0ee3cf6ea97e127f9d9a29fcb7ca16db0f67ed59ad5f38fb5374e370aeb287b4
<!-- external-doc-registry-end -->

进入 Group 7 时必须 checkout/获取该 commit 并复核 SHA-256；如果上游版本变化，先记录 registry delta 和设计影响，再更新快照。禁止把“本机文件还在”当成跨仓规范未漂移的证据。

### 0.4 Group commit 与脏工作树 ownership

Group commit 只允许包含该 Group 已完成验收且已写回证据的路径。Group 0 冻结时，HEAD 为 `501db6555dae374e5fcf43a6fdcfe8a3dd89343e`；除本 Group 的 6 个 staged 路径外，仍有 66 个 tracked unstaged 路径与 8 个 untracked 路径，全部视为进入本轮前已经存在的外部改动，不因文件名或测试变绿自动归本轮所有。

<!-- group0-owned-paths-start -->
- backend/tests/architecture/test_agent_native_repair_ledger.py
- docs/README.md
- docs/agent-native-unified-atomic-review-2026-07-14.md
- docs/reusable-agent-native-atomic-review-prompt.md
- docs/session-v2-cc-codex-alignment-contract-2026-07-14.md
- docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md
<!-- group0-owned-paths-end -->

现有外部改动的后续处理规则：

- `.ultra/**` debug/session/review artifact：默认排除所有 Group commit，除非用户单独要求纳管。
- Session、ThreadItem、web chat、invocation trace 与对应 frontend diff：进入 Group 2/9 前逐 hunk 重验，不能直接继承“已完成”。
- Sub-agent、Agent Team、Workflow、channel diff：进入 Group 3/4/7 前逐 hunk 重验。
- database/config/migration/hook/Memory diff：进入 Group 1/5/6/8 前按 authority、事务和 migration owner 拆分。
- 每次 commit 前必须比较 `git diff --cached --name-status` 与当前 Group owned-path manifest；任何跨 Group path 都先 unstaged 或拆 hunk，禁止顺手提交。

## 1. 最终裁决

用户本次纠偏是成立的。必须同时区分四个平面：

1. **Fleet plane**：平台常态存在两三千、上万甚至更多已注册、可路由、可被 trigger 唤醒的数字员工。这些 Agent 首先是持久化定义，不等于同数量模型进程，也不进入同一个 Prompt。
2. **Root execution tree**：一个 root Agent 的一个 Session/Turn/Task 一次请求 100 个 child execution。child 可以来自 direct Sub-agent、Agent Team 或 Workflow；本轮“100 个返回爆炸”只指这个平面。
3. **Capability plane**：这个单 Agent 同时拥有 300–400 Skill、200 MCP、大量 Sub-agent/Workflow definition 与巨大 Memory。
4. **Channel plane**：同一 root task 横跨钉钉、飞书、Slack、Web 的 Agent work 与 delivery。

旧极端报告把 root fan-in 主体抓对了，但标题和少量推论把“100 个 child”写成了“100 Agent”，并把“每个 child terminal 有独立通知意图”近似成“每个 terminal 必然触发一次父模型 wake”。当前源码证明：父 run 活跃时，后续通知会进入该 run 的 mid-run mailbox，因此不保证 100 次父模型调用；但系统依然没有确定性的 root integration epoch，mailbox 又会在 claim 时一次性全量注入，所以根因没有消失，只是必须改写为更精确的事实。

本轮统一后的当前工作账本是：

```text
原极端报告工作账本                       94
  P0 1 / P1 32 / P2 32 / P3 29

本轮新增 current-confirmed canonical leaf  9
  P1 5 / P2 4

当前统一工作账本                         103
  P0 1 / P1 37 / P2 36 / P3 29
```

断点没有因纠偏减少，也没有旧项被 refute。变化是：

- **1 个旧 leaf 事实口径漂移但编号和严重度不变**：`CONC-WAKE-002`；
- **2 个旧 leaf 重新归位但不改数量**：`CHANNEL-FAIRNESS-001` 只属于 channel plane；`CONC-FANIN-001` 只属于单根 Session 的 parent context；
- **新增 3 个规模连接断点**：统一 root execution tree、fleet worker fairness、fleet trigger scan；
- **新增 6 个 Session truth 断点**：accepted input、stable item lifecycle、非破坏 projection、平台 reasoning 文案、persist/publish envelope、前端 canonical reducer。

因此，正确回答不是“94 仍完全不变”，也不是“以前全错”。更准确的回答是：**核心 fan-in 结论保留，压力单位从模糊的 100 Agent 校正为单个 root Session 的 100 个 child；加入 fleet 与 Session truth 两条此前缺失的轴后，工作分母从 94 增至 103。**

## 2. 纠偏后的真实运行拓扑

### 2.1 Fleet Agent 不是常驻模型进程

当前 `Agent` 是数据库实体（`backend/app/models/agent.py:13`），实际执行通过 `RuntimeTask`（`backend/app/models/runtime_task.py:35`）进入 worker。worker 当前全局默认 `batch_size=8`、`max_concurrent=16`（`backend/app/config.py:82-83`），可执行任务由 `runtime_task_claim_service.build_runtime_task_claim_statement()` 领取。

因此：

- 10,000 个 Agent definition 可以长期存在而没有 10,000 个模型调用；
- fleet 风险主要在 registry、trigger enumeration、queue fairness、worker capacity、tenant isolation 与 control-plane headroom；
- root context 风险只在某个 Session 实际建立执行树并把 child evidence 汇回父模型时发生。

### 2.2 单根 Session 的真实 100-way 路径

```text
root Agent / root Session / root RuntimeTask
  ├─ direct Sub-agent RuntimeTask × N
  ├─ Agent Team member task/session × M
  └─ Workflow fanout leaf × K

N + M + K = requested child executions
```

当前三条路径都有局部实现，但没有统一的 root requested/admitted/expected/result/integration ledger：

- direct Sub-agent 使用 `RuntimeTask.root_runtime_task_id`、child session、completion outbox；
- Agent Team 逐 member 启动，并为 member terminal 写独立 notification；
- Workflow fanout 创建全部 coroutine，并把 raw result list 留在 step output；
- `root_runtime_task_id` 当前主要用于身份/预算关联，不是 root coverage 或 result manifest。

这意味着“每条路径能跑”不等于“一个 root 能诚实知道 100 个 child 中哪些 requested、哪些 admitted、哪些 deferred、哪些 terminal、哪些 late、哪些已经集成”。

### 2.3 当前默认预算不会自动解决 100-way

`runtime_budget_service.py` 的内置 profile 当前分别给出：

| profile | `max_subagents` | `max_continuation_wakes` | fail mode |
|---|---:|---:|---|
| interactive | 24 | 64 | require confirmation |
| scheduled | 32 | 64 | summary only |
| workflow | 256 | 512 | hard stop |
| agent_team | 16 | 96 | require confirmation |

所以修正后的 100-way 场景会出现两种坏路径：

1. 普通 profile 在 100 个 child 真正 admission 前先撞固定内部 cliff，进入 approval/summary-only/exhausted；它没有 durable wave plan，不是弹性 backpressure。
2. workflow 或放宽 profile 可以真实 admission 100 个 child，此时 full result、notification、mailbox 与 parent Prompt 的爆炸仍然存在。

这不会新增一个预算 leaf；它已经由 `BUD-BREAKER-001`、`WF-HARDLIMIT-001`、`SUBAGENT-APPROVAL-001` 与 `CONC-FANIN-001` 覆盖。纠偏只改变场景解释，不重复计数。

## 3. 当前源码确认的关键事实

### 3.1 Root fan-in 与 mailbox

1. `subagent_run_service.make_run_completer()` 把 `result.content` 全量写成 `result_summary`。
2. `update_subagent_child_session_state_for_run()` 把完整 summary 写入 child event 和 parent child-session event。
3. `_wake_parent_session_from_subagent_completion()` 为每个 terminal child 创建独立 completion outbox item。
4. `RuntimeNotificationOutboxService._deliver()` 逐 item 调用 `continue_parent_session_with_task_notification()`。
5. `continue_agent_session_from_mailbox()` 在 parent 活跃时把通知加入当前 run；parent idle 时才创建新的 continuation turn。
6. `_queue_saved_mid_run_user_message()` 对 `RuntimeTask.metadata_json.pending_user_messages` 做 read-copy-write，没有 row lock/CAS。
7. `_claim_pending_mid_run_user_messages()` 一次取出全部 pending entries、全部 materialize、清空 list，再作为同一轮输入返回。

因此应把旧表述改成：

> 每个 child terminal 产生一个独立 delivery intent；活跃 parent 可能把多个 intent 暂存在同一 run，但系统没有 root integration epoch、没有 bounded claim page、没有 result manifest，且 JSON mailbox 存在 lost-update 与一次性上下文 burst。

### 3.2 Workflow 与 Agent Team

- `backend/app/runtime/workflow_engine.py` 的 `_execute_fanout_step()` 为全部 item 创建 coroutine，semaphore 只限制 active leaf；`results` 保留全部 raw result，`asyncio.gather` 等待整个集合，任一失败使 step failed，成功 leaf 没有完整进入 parent partial outcome。
- `agent_team_runtime_service.message_agent_team_members_runtime()` 按 member 顺序启动；中途失败时已启动前半继续存在，后半没有 requested/admitted/deferred ledger。
- `_wake_parent_session_from_team_member_completion()` 仍按 member 写独立 outbox，并保留 creator fallback 的 identity 漂移风险。

### 3.3 Fleet 调度

- `runtime_task_claim_service.build_runtime_task_claim_statement()` 只按 `priority DESC, created_at ASC` 全局排序，使用 `SKIP LOCKED`；没有 tenant/root fair share、per-root active share 或 control-plane reserve。
- `runtime_task_worker._claim_batch_size_for_available_slots()` 只用全局 active count 计算可领取槽位。
- 因此一个 root 先入队 100 个同优先级 child 时，可以长期占据全局 worker 槽与 claim 前排；这与 channel queue fairness 是独立 seam。
- `trigger_daemon._tick()` 每次通过 `.scalars().all()` 载入所有 enabled trigger，逐条评估，再按 agent 串行 preflight/create task；没有 keyset page、shard、durable scan cursor 或 due partition。

静态事实足以确认 O(N) 与公平性缺口；但本文没有实跑 10,000-agent production curve，具体饱和点仍是 coverage gap，不能伪造成已测容量数字。

### 3.4 Context resource plane

`unified-context-assembly-and-progressive-disclosure-2026-07-14.md` 的八个主要断点已经被原 94 账本中的 `XCB-CTX/CAP/MEM/OUT/LIM/RESULT/MCP/OBS` 覆盖，本轮不重复计数：

- `SkillRegistry.render_catalog()` 忽略 budget 并渲染全部可见 description；
- `gather_subagent_candidates()` 的 limit 不形成真实分页；
- `MemoryRetriever.retrieve()` selector 失败返回全部授权候选；
- `MemoryAssembler.assemble()` 忽略 budget 并渲染全部 selected body；
- Prompt 最终仍存在基于方便性字符预算的 pre-model hard raise；
- Tool/MCP discovery 缺稳定 cursor/coverage；
- large result 外置后仍有 raw resident copies；
- output/tool-round/pressure 状态没有 durable continuation contract。

该设计稿不是“又一套 Context Manager”。它应成为 root result、Session replay、Skill/MCP/Memory progressive disclosure 共用的资源平面。

### 3.5 Session truth 降级链

当前 `ChatTranscriptEvent` 已有 session sequence、run/message/parent/root refs、item type/status、turn/causation/correlation 等强资产，且 `append_session_event()` 在 caller transaction 内写事件并通过 after-commit bridge 投影 T0。问题不是“完全没有事件”，而是事件到用户消费之间仍被连续降级：

1. `start_web_chat_run()` 同事务先写 `RuntimeTask + ChatMessage`，但 canonical user transcript event 由 worker 的 `_materialize_initial_user_turn_for_worker()` 之后补写；pending task 若未 claim 就终止，accepted input 可只留在兼容模型/metadata。
2. `_persist_stream_step_event()` 为每段 thinking/chunk 创建独立 event ID，没有跨 started/delta/completed 的 stable item ID 与 ordinal；`_finalize_invocation_result()` 又把全部 `thinking_content` 拼成最终 assistant message 的 `thinking` 附件。
3. `thread_items._user_summary()` 把 reasoning 显示成固定“Agent 正在整理思路。”；这是平台展示文案冒充真实模型过程。
4. `_user_item_data()` 清除 tool/workflow/subagent/compaction 的关联 ID；`build_live_thread_item()` 在 live 入口直接走 user projection。
5. stream event 先在独立事务持久化，再直接 broadcast；`web_chat_stream_bus.publish_web_chat_stream_event()` 使用另一套 run-local Redis sequence，且没有与 transcript event 同事务的 durable delivery outbox。
6. 前端 `threadItemToAgentChatMessage()` 把 typed item 降级成旧 message；`timelineModel.buildCells()` 再依据 message/thinking/相邻关系重建 process 与 final。

这六条不是一个“UI 样式问题”，也不能只以 `G-01A` 或 `XCB-OBS-001` 代替。它们有独立复现、独立 migration/rollback 与独立验收，因此新增六个 canonical leaf。

## 4. 本轮新增的 9 个 canonical leaf

| ID | P | 独立 seam | 七原子断裂 | 当前源码锚点 | 完整修复 |
|---|---:|---|---|---|---|
| `ROOT-TREE-001` | P1 | direct Sub-agent、Agent Team、Workflow 没有统一 root execution ledger/integration epoch | 权威→证据→恢复→消费 | `RuntimeTask.root_runtime_task_id`、`backend/app/services/subagent_run_service.py`、`backend/app/services/agent_team_runtime_service.py`、`backend/app/runtime/workflow_engine.py` | 建 root requested/admitted/expected/result ledger；三类执行投影同一 coverage/result contract；integration 分页、幂等、可恢复 |
| `FLEET-SCHED-001` | P1 | RuntimeTask 全局 priority/FIFO，无 tenant/root 公平或 control reserve | 执行→恢复→验收 | `backend/app/services/runtime_task_claim_service.py:31-67`、`backend/app/services/runtime_task_worker.py:157-162` | scheduler key=`tenant + root`；weighted fairness、per-root active share、queue age、control-plane reserve；超额 child deferred 而非 failed |
| `FLEET-TRIGGER-001` | P2 | daemon 每 tick 全量载入并串行扫描所有 enabled triggers | 执行→恢复→验收 | `backend/app/services/trigger_daemon.py:2429-2547` | due-index/keyset page、shard lease、durable cursor、restart resume；不把 Agent definition 当常驻进程 |
| `SES-ACCEPT-001` | P2 | accepted input 先落 ChatMessage/RuntimeTask，canonical transcript event 延迟到 worker | 输入→权威→恢复 | `backend/app/services/web_chat_runtime.py:1707-1921,3736-3811` | 在接受请求的同一事务写 `user_message.accepted` event；RuntimeTask/ChatMessage 只引用其 event ID；幂等 request ID |
| `SES-ITEM-001` | P1 | stream delta 无 stable item lifecycle，过程又聚合进 final thinking | 证据→恢复→消费 | `backend/app/services/web_chat_runtime.py:1058-1101`、`backend/app/services/web_chat_run_orchestrator.py:398-432,783-818` | 事件具 stable item_id、ordinal、started/delta/completed；commentary/reasoning/final 分离；final byte-faithful |
| `SES-PROJECTION-001` | P1 | user projection 删除 tool/workflow/subagent/compaction 关联 ID，live 过早破坏事实 | 权威→证据→恢复 | `backend/app/services/thread_items.py:688-733,879-924` | visibility 只 redact exact sensitive fields；保留 event/item/status/correlation identity；live/history 同 envelope |
| `SES-PROSE-001` | P2 | 固定平台文案替代 reasoning 的用户表达 | Model Agency→消费 | `backend/app/services/thread_items.py:596-646` | 无模型公开 commentary 时显示 typed runtime state；有 commentary 时保留模型字节；平台文案不得冒充模型过程 |
| `SES-TRANSPORT-001` | P2 | transcript 持久化与 live publish 非同一 outbox/envelope/sequence | 证据→恢复→消费 | `backend/app/services/web_chat_runtime.py:1058-1101`、`backend/app/services/web_chat_stream_bus.py:35-73` | event+outbox 同事务；至少一次投递；consumer event-id 幂等、session-sequence gap recovery；Redis/WS 仅 transport |
| `SES-CONSUMER-001` | P1 | typed item 降级成 AgentChatMessage，再由启发式 timeline 重建事实 | 消费→恢复→验收 | `frontend/src/pages/session-workbench/threadItemReducer.ts:345+`、`frontend/src/pages/session-workbench/timelineModel.ts:1642+` | live/history/reconnect/reload/resume 走同一 typed reducer；timeline/right rail/deliverables 都是同 store projection |

### 4.1 为什么没有再多算

- 100-way full raw result 入 parent 仍是 `CONC-FANIN-001`，不因 direct/team/workflow 三种来源重复计三次。
- JSON mailbox lost update 仍是 `CONC-MAILBOX-001`；root ledger 不能替代 mailbox row 的 CAS/lease 修复。
- Agent Team 半启动仍是 `TEAM-FANOUT-001`；Workflow partial join 仍是 `WF-PARTIAL-001`。
- channel ingress/delivery fairness 仍是 `CHANNEL-FAIRNESS-001`；RuntimeTask worker fairness 新增 `FLEET-SCHED-001`。
- T0 after-commit race 仍是 `P1-017`；Session live publish/outbox 新增 `SES-TRANSPORT-001`，二者 consumer 与事务边界不同。
- failure prose 冒充 assistant 仍是 `G-01A`；reasoning 固定展示新增 `SES-PROSE-001`，两者输出来源和验收不同。

## 5. 旧断点的纠偏与漂移

### 5.1 `CONC-WAKE-002` 保留，但重写

旧描述：`one terminal = one wake`。

当前准确描述：

> `one terminal = one independent completion delivery intent`。parent idle 时该 intent 可以启动 continuation；parent active 时会被排入该 run 的 mailbox，因此模型 invocation 数不必等于 child 数。但 outbox 没有 root/run integration epoch，mailbox 没有 bounded claim/CAS，结果仍会形成 notification backlog、lost update 或一次性 Prompt burst。

修复不应只是“多等 100 ms 再 wake”，而是：terminal 先进入 root result ledger，按 material epoch 更新一份 manifest；同一 root 只对新 coverage page 产生幂等 resume intent。

### 5.2 `CHANNEL-FAIRNESS-001` 不再承担 fleet 结论

它只证明 channel ingress/delivery 队列缺 tenant/channel fairness。平台 worker 的 tenant/root 公平性由新增 `FLEET-SCHED-001` 单独承担；两者不能互相作为验收证据。

### 5.3 100-way 数量是 soft admission 目标，不是无界并发许可

用户要求系统能承受一次请求 100 个 child，不等于必须同时运行 100 个模型调用。允许 active concurrency 受真实进程/provider capacity 约束，但超额 child 必须：

- durable `deferred/not_admitted`；
- 不进入 expected set；
- 保留 exact intent 与 authority；
- 有可达 resume condition；
- 不把 root task 终态化；
- 不使其它 tenant/root 饥饿。

这同时保留模型 agency 与平台安全边界。

## 6. CC、Codex 与 Hive Native 的统一合成

### 6.1 CC / FreeCode：语义下限

FreeCode 当前本地源码证明它保有完整 model/tool loop、permission/hook、compaction、Skill load、AgentTool fork/fresh/resume/background 等生命周期语义。它的核心价值是：模型先拥有真实能力与证据，工具结果回到模型，物理超窗后再 compact/recover。

但它不是 100-way capacity 答案：

- concurrency-safe Agent tool 没有数值 admission；
- child final text 可以完整回到 parent tool result；
- aggregate spill 保护依赖默认关闭的 feature flag；
- Tool/Agent catalog 仍没有统一 authority/page/coverage contract。

因此 Hive 不能以“CC 也没有”为理由保留 fan-in 爆炸，也不能为了稳定而删掉 CC 的 Sub-agent/Skill/Tool 语义。

`claude-code-org` 的 `/Users/rocky243/Context Engineering/claude-code-org/src/tools/AgentTool/**`、`/Users/rocky243/Context Engineering/claude-code-org/src/utils/forkedAgent.ts`、`/Users/rocky243/Context Engineering/claude-code-org/src/utils/sessionStorage.ts` 对 fork/resume/background/transcript 语义给出同向交叉证据。claw-code Python port 只用于识别现有移植边界；其中按固定 turn 数保留尾部的简化实现不能反向定义 CC 语义。claw-code Rust 的 JSONL session、fork、resume、compact 与 health-probe 只作为低层 session hygiene 参考。发生冲突时仍以 FreeCode 为 CC semantic floor。

### 6.2 Codex：工程增量

Codex 当前源码提供两个本轮最有价值的参考：

1. typed Thread/Turn/Item 与 started/delta/completed 事实，适合补 Hive Session truth；
2. Agent Job：把批量 item 持久化到 state DB，active worker 并发有界，遇 slot cap 保持 pending，worker 用 `report_agent_job_result` 提交结构化结果，最终只向 root 返回 job status、计数与 output CSV 路径，而不是把全部 row result 注入 parent Prompt。

源码锚点：

- `/Users/rocky243/Context Engineering/codex/codex-rs/core/src/tools/handlers/agent_jobs.rs`
- `/Users/rocky243/Context Engineering/codex/codex-rs/core/src/tools/handlers/agent_jobs/spawn_agents_on_csv.rs`
- `/Users/rocky243/Context Engineering/codex/codex-rs/core/src/tools/handlers/agent_jobs/report_agent_job_result.rs`
- `/Users/rocky243/Context Engineering/codex/codex-rs/protocol/src/protocol.rs`

不能照抄固定 4/6/64 thread cap、omit skills、blind truncate 或仅本地 CSV 假设。Hive 应吸收 typed/durable/bounded/result-ref 结构，并用 tenant/RLS、root execution、Workspace Artifact、Memory source refs 与跨渠道治理扩展它。

### 6.3 Hive Native：超越点

Hive 已有形成 CCPlus 的原材料：

- tenant/principal/RLS/delegation 与 approval；
- durable RuntimeTask、lease/fence、outbox、sweeper；
- Workflow journal、Agent Team、A2A、channel delivery；
- Workspace/Artifact 与 large-result ref；
- T0/T2/T3/soul、Knowledge、Skill evolution；
- ChatTranscriptEvent、invocation spans、Session Workbench。

当前缺的不是再增加一个 `max_*`，而是把这些资产收敛为同一个机械契约：

```text
CC semantic loop
  + Codex typed/durable control envelope
  + Hive Context Resource Plane
  + Hive Root Execution / Result Plane
  + Hive Session Event / Item Plane
  + Hive Enterprise Authority / Channel Delivery Plane
= CCPlus
```

## 7. 目标架构

### 7.1 Fleet plane

- Agent definition 保持轻量、持久化、按租户/组织索引；不常驻模型进程。
- Trigger 用 `next_evaluate_at + keyset page + shard lease + durable cursor`，只扫描 due page。
- RuntimeTask scheduler 以 `(tenant_id, root_runtime_task_id)` 为 fairness key；priority/SLA 只能来自显式 policy。
- 保留 cancel/approval/checkpoint/outbox 的 control-plane reserve，不能被 child work 吃满。

### 7.2 Root execution plane

最小事实模型：

```text
RootExecution
  root_execution_id
  root_session_id
  root_runtime_task_id
  principal/delegation/policy/budget refs
  requested/admitted/deferred/not_admitted counts
  expected/received/failed/late/duplicate counts
  current_integration_epoch
  result_manifest_ref
  resume_cursor

RootExecutionItem
  stable_item_id
  execution_kind = subagent | team_member | workflow_leaf | a2a
  requested_intent_hash
  admission_state
  child_runtime_task/session/workflow refs
  terminal_state
  result_ref/hash/bytes/source_refs
  integrated_epoch
```

可优先复用 `RuntimeTask.root_runtime_task_id`、现有 outbox、Workspace Artifact 和 budget run；只新增无法由现有事实可靠推导的 item/epoch/mailbox 状态，避免建第二套 execution engine。

### 7.3 Durable result 与 integration plane

1. child 完整结果先进入 Workspace/object/blob 既有 durable artifact，生成 hash、bytes/tokens、source refs、range reader。
2. root item 事务性提交 terminal + result ref；raw bytes 不再复制到 `RuntimeTask.result_summary`、outbox summary、mailbox 与 Prompt 四处。
3. outbox 只携 root/item/epoch ref；相同 root 的 material transition 合并为幂等 integration intent。
4. parent LLM 每次读取 bounded manifest page 与按需 result page，生成模型 authored synthesis。
5. coverage 未完整时允许模型给 partial judgment，但平台/UI 必须显示 partial，不能冒充 complete。

### 7.4 Context Resource Plane

采用 `unified-context-assembly-and-progressive-disclosure-2026-07-14.md` 的 descriptor/page/packet/ledger：

- resident kernel 相对资源总量 O(1)；
- 400 Skill、200 MCP、巨大 Memory 全量可发现，不全量 inline；
- Skill/MCP/Sub-agent/Workflow/Memory/Knowledge 保持不同 public semantics，共享内部 page/hash/coverage/recovery；
- Personal KB 继续 tool-only；
- output reserve 按模型与任务决定；内部百分比只是 pressure 水位；
- index/cache 不是 truth，授权原文、registry、Memory Vault、Workflow journal、Artifact 才是 truth。

### 7.5 Session Event / Item Plane

采用 `session-v2-cc-codex-alignment-contract-2026-07-14.md`，但不新建平行真相：

- 继续演进 `ChatTranscriptEvent`；
- accepted user input 与 RuntimeTask 在同一事务产生 canonical event；
- 每个 work item 有 stable item ID、ordinal、lifecycle；
- event 与 publish outbox 同事务，Redis/WS 只做 transport；
- user projection 可 redact bytes，不可删除 identity/status/existence；
- live/history/reconnect/reload/resume 走同一 reducer；
- `ChatMessage` 是兼容读模型，不能反向成为过程权威；
- commentary/reasoning/final 分离，平台不写模型语义。

### 7.6 Channel plane

- root execution/result terminal 与每个 destination delivery terminal 正交；
- 每 hop fresh-check principal/delegation/sensitivity/residency/credential ref；
- channel payload 只携 result/artifact refs 与允许的 bytes；
- duplicate/out-of-order/ack loss/auth revoke 由 delivery ledger 恢复；
- root final destination 显式，不按 owner/昵称猜，也不默认群发。

## 8. Hard 与 Soft 的最终边界

| 类型 | 可以 hard 的事实 | 命中后的合法结果 |
|---|---|---|
| 物理/协议 | provider context/request、真实进程/连接/帧容量 | 结束当前 request/attempt；externalize、queue、checkpoint、resume |
| 权威/effect | tenant/RLS/ACL/delegation、credential、sandbox、付款/删除/外发 approval | 只拒对应 ingress/effect/hop；保留无关推理、证据与工具 |
| 显式经济/生命周期 | 用户/组织可信 policy 的 cost/deadline/cancel/workflow contract | durable paused/stopped；保留 progress、remaining、re-authorize path |
| 内部运行目标 | fan-out、active concurrency、tool rounds、retry、Prompt target、result page、queue batch | pressure/defer/batch/backpressure；不得无依据终态化 task |

并发槽本身可以是物理 hard capacity；**“最多同时跑多少”可以 hard，超出的 work 是否消失或 task 是否失败不可以由该数字决定。**

### 8.1 两份关键设计文档是不可降级的规范输入

Group 摘要不能替代以下两份文档：

- `@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`：Context Resource Plane 全文为 Group 6 主规范，Group 4 消费 durable result 合同，Group 10 做最终重认证，并被 Group 1/2/3/7/8/9 按资源域消费。
- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`：Session Event/Item/Reducer 全文为 Group 2 主规范，并被 Group 3/4/6/7/8/9/10 复用。

若 Group 条目与两份文档冲突，以北极星裁决后的两份设计合同为准，并回写本文修正 Group；不得以“Group 没写”删除设计能力。

#### Context 文档章节交叉表

| Context 文档范围 | 主 owner Group | 必须被消费的其它 Group | 不可丢失的合同 |
|---|---:|---|---|
| §0–§6、§8–§9、§17–§20 | 6 | 0、2、3、4 | hard/soft 精确定义、五层披露、token authority、capacity ledger、descriptor/page/packet/cursor/hash/coverage |
| §7.1 Memory | 6 | 8 | body 默认不常驻、warm descriptor、source refs、selector unavailable typed degrade |
| §7.2 Skill、§7.3 Tool/MCP | 6 | 1、9 | registered/discoverable/active/executable 四态、schema lazy-load、execution-time auth fresh-check |
| §7.4 Sub-agent/Agent Team/A2A | 6 | 3、4、7 | definition 可发现、child intent/admission/result ref、parent bounded consumption |
| §7.5 Workflow | 6 | 3、4 | DAG/leaf 可发现，执行仍由 Workflow authority；partial/result 不全量塞 Prompt |
| §7.6 Personal/Enterprise Knowledge | 6 | 1、8 | Personal KB tool-only；Enterprise authority/retention 不得由 Personal/legacy 冒充 |
| §7.7 Hooks | 6 | 2、8 | Hook 不能绕 context ledger；机械 fallback 不制造语义 |
| §7.8 Session history/Tool Result | 6 | 2、4、9 | full bytes 外置、coverage 诚实、Session replay 可恢复 |
| §10–§12 | 0 | 2–9 | 高压矩阵、TDD Red、七原子与故障恢复必须进入各 Group 验收 |
| §13–§15 | 0 | 1–10 | CC/FreeCode 主基线、Codex additive delta、精确代码触点和禁止模式 |
| §16 决策 A–F | 6 | 1、8、9 | 六项产品决策全部按下表执行，不得重新退回局部 35K/65K patch |

#### Context 六项决策 owner map

<!-- context-decision-map-start -->
- CTX-A | Group 6 | T2/T3 Memory body 默认不允许自动 0-hop；显式 task-local pin 除外
- CTX-B | Group 6 | 8% 仅为 256K resident review center，不是硬配额或填充目标
- CTX-C | Group 6 | 暂不新增统一 public context_search/context_load；统一内部合同，保留领域工具
- CTX-D | Group 6 | 后台 Memory 只可生成 bounded warm descriptor，不可自动注入 body
- CTX-E | Group 6 | provider-native Tool Search 仅为 adapter，不成为唯一标准
- CTX-F | Group 6 | tool_search 只发现 executable schema；Memory/Skill/Workflow/Agent/Knowledge 保留领域入口
<!-- context-decision-map-end -->

#### Session 文档章节交叉表

| Session 文档范围 | 主 owner Group | 必须被消费的其它 Group | 不可丢失的合同 |
|---|---:|---|---|
| §0–§8 | 2 | 0、6、10 | CC 完整生命周期底线、Codex typed delta、Hive-native 一等 Session 类型 |
| §9–§11 | 2 | 3、4、8、9 | `ChatTranscriptEvent` 演进为唯一 event truth；stable item/lifecycle/ordinal；同一 reducer |
| §12 Item Family | 2 | 3、4、6、7、8、9 | Session/assistant/tool/file/context/memory/collaboration/hook/error 全部是一等 typed item |
| §13–§14 | 2 | 1、6、9 | commentary/reasoning/final 分离；Tool/Hook/File/Artifact 保留真实 phase 与 receipt |
| §15 Compaction | 6 | 2、9 | compaction 只改变 model context projection，不删除 UI/T0/audit 历史 |
| §16 Collaboration | 3 | 4、7、9 | Sub-agent 不扁平化；A2A authority/receipt；Workflow 与协作语义分离 |
| §17–§19 | 9 | 2 | 主时间线/right rail 同一 store；live/reconnect/replay/reload/resume 同构；redaction 不删除 identity |
| §20 Migration | 9 | 2、4、8 | backfill 只用机械证据；未知保持 `legacy_unknown`；禁止永久双事实源 |
| §21–§22 | 0 | 1–10 | 七原子和禁止模式是所有 Group 的共同门 |
| §23 G1–G13 | 0 | 2–9 | 全部黄金轨迹必须变成自动化验收，不得挑选 happy path |
| §24–§25 | 0 | 2–9 | unit/contract/integration/browser/byte snapshot/production gate 与精确文件边界 |
| §26–§29 | 2 | 0、9、10 | S-01–S-12 ADR、最终体验、当前状态和源码参考必须随修复证据更新 |

#### Session S-01–S-12 owner map

<!-- session-decision-map-start -->
- S-01 | Group 2 | CC 有序完整生命周期是语义底线
- S-02 | Group 2 | Codex typed Thread/Turn/Item 是工程增量
- S-03 | Group 2 | commentary、reasoning summary/private、final 分离
- S-04 | Group 2 | Session event 是唯一运行事实
- S-05 | Group 2 | Session item 是 reducer 读模型
- S-06 | Group 2 | persist-before-publish，Outbox 至少一次
- S-07 | Group 9 | 主时间线与 right rail 消费同一 store
- S-08 | Group 6 | Compaction 只改变 context projection
- S-09 | Group 2 | Hive-native 能力全部是一等 Item
- S-10 | Group 2 | user projection 可 redaction，不可删除 identity
- S-11 | Group 9 | 历史未知内容保持 unknown，不用 heuristic 造事实
- S-12 | Group 0 | 旧 Session 完成声明撤销，按当前源码重新验收
<!-- session-decision-map-end -->

#### Session G1–G13 黄金轨迹 owner map

<!-- session-golden-map-start -->
- SESSION-G1 | Group 2 | 基础模型—工具循环
- SESSION-G2 | Group 2 | 多次工具与动态压缩
- SESSION-G3 | Group 2 | 无 commentary Provider
- SESSION-G4 | Group 2 | 受限 reasoning
- SESSION-G5 | Group 9 | 断线、重连与重复投递
- SESSION-G6 | Group 2 | Tool denied/unavailable/approval-required/retryable 分态
- SESSION-G7 | Group 2 | Hook approval 与用户问题
- SESSION-G8 | Group 7 | 嵌套 Sub-agent/A2A
- SESSION-G9 | Group 3 | Workflow gate/wait/resume
- SESSION-G10 | Group 4 | 文件已提交但 final 前崩溃
- SESSION-G11 | Group 6 | 多次 Compaction + resume/fork
- SESSION-G12 | Group 9 | 历史 backfill
- SESSION-G13 | Group 6 | 高压长 Session 与资源爆炸
<!-- session-golden-map-end -->

## 9. 最终一次性修复顺序

下面是依赖顺序，不是把 103 个 leaf 绑成一个发布列车。每个开工 leaf/同根家族必须一次完成 Red→Green、migration/backfill、fault injection、observability、recovery/rollback、真实消费与发布验收。P0/P1 自身闭环后立即独立发布。

| Group | owner canonical leaf | owner Missing | 当前状态 |
|---:|---:|---:|---|
| 0 | 0（全局门） | 0 | closed：`EVID-G0-002`，Git truth、机器账本、跨仓快照与 harness 基座已闭环 |
| 1 | 16 | 0 | open |
| 2 | 14 | 0 | open |
| 3 | 7 | 0 | open |
| 4 | 6 | 0 | open |
| 5 | 2 | 0 | open |
| 6 | 10 | 0 | open |
| 7 | 1 | 1 | open |
| 8 | 9 | 2 | open |
| 9 | 19 | 1 | open |
| 10 | 19 | 1 | open |
| **总计** | **103** | **5** | 未完成 |

### Group 0：证据、文档路由与工作树隔离

**Owner 范围**：无业务 leaf；它是所有 Group 的前置门和证据基础设施。

**依赖 Group**：无。所有后续 Group 开工前必须通过本 Group 的冻结快照、owner 唯一性、文档路径与证据写回门；这不表示 Group 0 可以替代各业务 Group 的 Red/Green。

**@必须先读（顺序）**：

1. `@AGENTS.md`
2. `@docs/hive-sota-master-goal.md`
3. `@docs/ccplus-north-star-contract-2026-06-24.md`
4. `@docs/runtime-model-agency-constraint-audit-2026-07-13.md`
5. `@docs/reusable-agent-native-atomic-review-prompt.md`
6. `@docs/agent-native-unified-atomic-review-2026-07-14.md`
7. `@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`
8. `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`

**@历史证据（只取证，不继承完成状态）**：

- `@docs/agent-native-atomic-review-2026-07-14.md`
- `@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md`
- `@docs/agent-native-atomic-review-501db655.md`
- `@docs/harness-engineering-audit-2026-06-11.md`
- `@docs/round2-sota-benchmark-2026.md`
- `@docs/final-atomic-review-2026-07-09.md`

**执行**：记录 HEAD、工作树、文件 hash/diff owner、原始 Red、权限事实源、migration/backfill/rollback 和 commit 边界；将 Prompt、本文及两份关键设计显式纳入 Git truth；建立 synthetic provider/channel/MCP、真实测试 DB/Redis、virtual clock、1/10/25/50/100 root fanout 与 2k/10k/50k fleet harness。

**首个 Red**：让机器校验在删除任一 owner 行、复制任一 owner、写入不存在的 `@docs`、漏掉 CTX/S/SESSION-G 映射或让 Group 计数与 ledger 不一致时确定失败；并证明并发脏工作树的文件 ownership 未定义时不能形成完成声明。

**退出门**：§12 owner map 证明 103/103 唯一归属；5/5 Missing 唯一归属；CTX-A–F、S-01–S-12、SESSION-G1–G13 无遗漏；文档路径存在；CI 可复算；任何 Group 的证据能按 §0.2 回填。证据写入 `EVID-G0-*`。

### Group 1：真实安全、principal、authority 与 fail-open

**Owner leaf（16）**：`P0-F1`、`P0-F2`、`E-1`、`P1-004`、`P1-F4`、`KB-AUTH-001`、`KB-EXTRACT-001`、`KB-PROP-001`、`AUDIT-IMM-001`、`AUDIT-TENANT-001`、`F-PLAINTEXT`、`P2-F8`、`P2-F6`、`KB-CONTRACT-001`、`B-01`、`BUD-ROOT-001`。

**依赖 Group**：Group 0。P0/P1 家族自身闭环后立即发布，不等待 Group 2–10。

**@必须先读**：

- `@docs/runtime-model-agency-constraint-audit-2026-07-13.md`
- `@docs/agent-permission-governance-spec-2026-07-07.md`
- `@docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md`
- `@docs/ccplus-governance-layer-architecture-2026-06-28.md`
- `@docs/ccplus-tool-call-governance-closure-landing-plan-2026-06-28.md`
- `@docs/session-rls-preflight-review-2026-07-09.md`
- `@docs/rls-enforcement-migration-plan.md`
- `@docs/personal-company-knowledge-tool-boundary-2026-07-10.md`
- `@docs/personal-knowledge-base-completion-contract-2026-07-08.md`
- `@docs/runtime-budget-conformance-audit-2026-07-09.md`

**@按需读取**：`@docs/personal-knowledge-base-spec.md`、`@docs/personal-knowledge-base-capability-rebaseline-2026-07-09.md`、`@docs/ccplus-governance-code-repair-plan-2026-06-28.md`、`@docs/ccplus-governance-truth-search-repair-plan-2026-06-28.md`。

**源码入口**：先用 graph 查 egress/web fetch、database startup/migration/RLS、principal/delegation frame、tool governance、runtime budget、Personal KB access/proposal/extraction；再读 exact live path。

**首个 Red**：分别复现 SSRF/redirect/DNS rebinding、缺失迁移仍启动、creator/requester 置换、cross-principal PKB 无 grant、audit 可改/静默丢弃、credential 明文与 budget authority fail-open；禁止用一个大测试掩盖多个独立安全 seam。

**退出门**：SSRF/redirect/DNS rebinding 与 sandbox egress 为零泄漏；schema/RLS fail-closed；唯一 requester/principal/delegation贯穿 inner effect、RecoveryManifest、PKB、audit 和 receipt；credential 不明文；budget service failure 只能缩小 work-amplification，不能伪造授权或冻结无关 direct answer。证据写入 `EVID-G1-*`。

### Group 2：Session 机械事实语言

**Owner leaf（14）**：`G-01A`、`A-01`、`A-04`、`B-02`、`B-03`、`G-01B`、`B-04`、`D-KB4`、`SES-ACCEPT-001`、`SES-ITEM-001`、`SES-PROJECTION-001`、`SES-PROSE-001`、`SES-TRANSPORT-001`、`SES-CONSUMER-001`。

**依赖 Group**：Group 0、Group 1。Session envelope 必须携带 Group 1 收敛后的 principal/authority，不得先建一个无可信身份的第二事实语言。

**@必须先读**：

- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`（全文，尤其 §9–§14、§18–§24、S-01–S-12、G1–G13）
- `@docs/runtime-model-agency-constraint-audit-2026-07-13.md`
- `@docs/t0-append-only-session-ledger-redesign-2026-06-18.md`
- `@docs/session-timeline-projection-contract-2026-07-04.md`
- `@docs/session-rendering-streaming-cc-codex-gap-analysis-2026-07-03.md`
- `@docs/ccplus-session-tui-unified-expression-plan-2026-06-28.md`
- `@docs/ccplus-session-full-landfall-2026-07-09.md`

**@按需读取**：`@docs/session-rendering-overhaul-plan-2026-07-03.md`、`@docs/session-rendering-s6-completion-plan-2026-07-04.md`、`@docs/ccplus-session-ux-contract-2026-06-26.md`、`@docs/hook-goal-session-expression-plan-2026-07-09.md`、`@docs/ccplus-session-control-command-alignment-2026-06-27.md`。

**源码入口**：`ChatTranscriptEvent`/append path、web chat accept/stream/finalize、thread item projection、stream outbox/bus、frontend typed reducer/timeline/right rail。

**首个 Red**：用同一固定 Session fixture 分别走 live/history/reconnect/reload/resume，注入 interleaved commentary/tool/final、duplicate/out-of-order/gap/publish failure，证明当前 item identity、phase、author 或 snapshot 不同构。

**退出门**：accepted input 同事务成为 canonical event；stable item/lifecycle/ordinal；typed denied/unavailable/approval/retryable；persist-before-publish；live/history/reconnect/reload/resume 同 reducer；平台不以 assistant prose 冒充模型；final 除 exact secret redaction 外 byte-faithful。必须通过 SESSION-G1/G3/G4/G5/G6/G7。证据写入 `EVID-G2-*`。

### Group 3：Root admission、预算与终态

**Owner leaf（7）**：`A2A-ADMISSION-001`、`SUBAGENT-ADMISSION-001`、`A2A-CYCLE-001`、`A2A-TERMINAL-001`、`TEAM-FANOUT-001`、`SUBAGENT-APPROVAL-001`、`ROOT-TREE-001`。

**依赖 Group**：Group 0–2。root ledger、approval 与 terminal 必须复用 Group 2 的 canonical event/item 和 Group 1 的 authority frame。

**@必须先读**：

- `@docs/ccplus-subagent-team-skill-mcp-hooks-parity-audit-2026-06-27.md`
- `@docs/subagent-agent-team-cc-parity-audit-2026-07-03.md`
- `@docs/subagent-team-cc-alignment-audit-2026-07-03.md`
- `@docs/ccplus-v1-subagent-resume-ruling-2026-06-24.md`
- `@docs/agent-team-session-workbench-root-cause-and-repair-plan-2026-07-02.md`
- `@docs/a2a-session-substrate-design-2026-06-24.md`
- `@docs/dynamic-workflow-harness-semantics-2026-06-24.md`
- `@docs/dynamic-workflow-cc-alignment-redesign-2026-06-23.md`
- `@docs/runtime-budget-control-plane-plan-2026-07-03.md`
- `@docs/runtime-budget-conformance-audit-2026-07-09.md`

**@按需读取**：`@docs/plan-subagent-workflow-prompt-parity-audit-2026-06-21.md`、`@docs/a2a-workflow-orchestration-design-2026-06-24.md`、`@docs/subagent-source-capability.md`、`@docs/workflow-source-capability.md`。

**源码入口**：RuntimeTask/root refs、subagent start/resume、Agent Team fanout、Workflow leaf journal、runtime budget reserve/settle、approval continuation。

**首个 Red**：同一 root 混合 direct/team/workflow 请求 100 child，注入 admission 中途崩溃、approval pause、cancel 与 late completion，证明 requested/admitted/expected 不守恒、ghost child、cycle 或 terminal 回退。

**退出门**：`requested = admitted + deferred/not_admitted`；reserve+durable enqueue commit 先于 expected；direct/team/workflow 进入同一 root item ledger；cycle/path durable；terminal monotonic CAS；late result 不覆盖 cancel/kill；approval intent durable。必须通过 SESSION-G9 与 1/10/25/50/100 mixed fanout。证据写入 `EVID-G3-*`。

### Group 4：Durable Result、mailbox 与 fan-in

**Owner leaf（6）**：`E-2`、`XCB-RESULT-001`、`CONC-FANIN-001`、`CONC-WAKE-002`、`WF-PARTIAL-001`、`CONC-MAILBOX-001`。

**依赖 Group**：Group 0–3。只有 admitted child、稳定 root/item identity 与 typed terminal 才能进入 result manifest、mailbox 和 integration epoch。

**@必须先读**：

- `@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`（§7.4、§7.5、§7.8、§9、§12、§18）
- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`（§12.4、§12.6、§16、G8–G10）
- `@docs/agent-team-session-workbench-root-cause-and-repair-plan-2026-07-02.md`
- `@docs/session-tui-collaboration-provenance-root-cause-and-repair-plan-2026-07-02.md`
- `@docs/chat-artifact-delivery-redesign-2026-06-20.md`
- `@docs/a2a-session-substrate-design-2026-06-24.md`
- `@docs/dynamic-workflow-harness-semantics-2026-06-24.md`

**@按需读取**：`@docs/session-workspace-hr-atomic-closure-2026-07-10.md`、`@docs/a2a-workflow-orchestration-design-2026-06-24.md`、`@docs/runtime-budget-control-plane-plan-2026-07-03.md`。

**源码入口**：subagent/team completion outbox、RuntimeNotificationOutbox、parent continuation/mailbox、Workflow fanout result、Workspace/Artifact/blob result refs、frontend parent coverage。

**首个 Red**：让 100 个 child 同秒返回 512 KiB–1 MiB 结果并混入 duplicate/out-of-order/partial/late；并发写 parent mailbox、在文件已 commit 但 final 前崩溃，证明 raw bytes、lost update、重复 integration 或永久等待。

**退出门**：完整 bytes 只在 durable result truth；outbox/mailbox 只携 ref；mailbox row 有 idempotency/sequence/claim/lease；root integration 以 epoch/page 幂等；partial/late/duplicate 可重算；100×1 MiB raw result 不线性进入 parent Prompt。必须通过 SESSION-G8/G10。证据写入 `EVID-G4-*`。

### Group 5：Fleet 公平与 Trigger 扫描

**Owner leaf（2）**：`FLEET-SCHED-001`、`FLEET-TRIGGER-001`。

**依赖 Group**：Group 0、Group 2、Group 3。需要 canonical task/pressure 状态与 root fairness key；不硬依赖 Group 4 的 result 实现，闭环后可独立发布。

**@必须先读**：

- `@docs/trigger-cc-alignment.md`
- `@docs/runtime-budget-control-plane-plan-2026-07-03.md`
- `@docs/runtime-budget-conformance-audit-2026-07-09.md`
- `@docs/harness-engineering-audit-2026-06-11.md`
- `@docs/eval-system-spec.md`

**@按需读取**：`@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md`、`@docs/round2-sota-benchmark-2026.md`。

**源码入口**：RuntimeTask claim SQL/worker capacity、trigger daemon/query/index/lease、tenant/root queue metrics、control-plane reserve。

**首个 Red**：一个 noisy root 先排入 100 child，同时加入 1,000 个其它 root 的交互任务与 cancel/approval/checkpoint；再对 2k/10k/50k trigger definitions 测单 tick、crash/restart 和 cursor，量化饥饿与 O(N) 扫描。

**退出门**：2k/10k/50k definitions 不等于模型进程；trigger due-index/keyset/shard/cursor 可 crash-resume；scheduler 按 tenant+root 公平并保留 cancel/approval/checkpoint 槽；noisy root 不永久饿死其它 root。证据写入 `EVID-G5-*`。

### Group 6：Context、Capability、Compaction 与输出恢复

**Owner leaf（10）**：`A-03`、`XCB-CTX-001`、`XCB-CAP-001`、`XCB-MEM-001`、`XCB-OUT-001`、`XCB-LIM-001`、`XCB-MCP-001`、`XCB-OBS-001`、`WF-HARDLIMIT-001`、`BUD-BREAKER-001`。

**依赖 Group**：Group 0–2、Group 4。Context Resource Plane 复用 authority、typed pressure/session state 与 durable result refs；Group 3/5 的 admission/fairness 是极端验收输入，但不是删除非法 Prompt hard cap 的发布阻塞项。

**@必须先读**：

- `@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`（全文；CTX-A–F 全部强制）
- `@docs/ccplus-session-runtime-token-compaction-alignment-2026-06-27.md`
- `@docs/runtime-model-agency-constraint-audit-2026-07-13.md`
- `@docs/agent-memory-md-first-spec.md`
- `@docs/memory-system-flow-map-2026-06-17.md`
- `@docs/memory-vault-path-contract-2026-06-23.md`
- `@docs/ccplus-subagent-team-skill-mcp-hooks-parity-audit-2026-06-27.md`
- `@docs/subagent-source-capability.md`
- `@docs/workflow-source-capability.md`
- `@docs/personal-company-knowledge-tool-boundary-2026-07-10.md`

**@按需读取**：`@docs/agent-memory-purity-spec.md`、`@docs/knowledge-container-boundaries.md`、`@docs/personal-knowledge-base-capability-rebaseline-2026-07-09.md`、`@docs/dynamic-workflow-harness-semantics-2026-06-24.md`。

**源码入口**：provider prompt ledger/context budget/prompt builder、Memory retriever/assembler、Skill registry、Tool Search/MCP registry、subagent/workflow directory、session context controller、kernel compaction/output continuation。

**首个 Red**：配置 400 Skill、200 MCP、大量 Sub-agent/Workflow 与大 Memory，让决定性证据位于最后 page/chunk；依次触发 selector unavailable、provider window pressure、output exhaustion、多次 compaction、resume/fork 和更小模型恢复，证明固定 cap、静默丢弃、不可发现或假 final。

**退出门**：resident kernel 对资源总量 O(1)；400 Skill/200 MCP/巨大 Memory/大量 definitions 全量可发现；directory/cursor/hash/coverage 完整；token-native preflight；internal threshold 只触发 pressure/defer；same-model output resume；compaction 不删证据；所有 soft/hard 状态模型可见。必须通过 CTX-A–F、SESSION-G2/G11/G13。证据写入 `EVID-G6-*`。

### Group 7：跨渠道 A2A 与 Delivery Plane

**Owner leaf（1）**：`CHANNEL-FAIRNESS-001`。**Owner Missing（1）**：`MISS-XCHANNEL-A2A-001`。

**依赖 Group**：Group 0–4。跨渠道必须建立在唯一 principal、canonical Session、root admission 与 durable result 上；与 Group 5 的 fleet fairness 分账验收，不互相冒充闭环。

**@必须先读**：

- `@docs/a2a-integrated-implementation-plan-2026-06-27.md`
- `@docs/a2a-session-substrate-design-2026-06-24.md`
- `@docs/a2a-workflow-orchestration-design-2026-06-24.md`
- `@docs/ccplus-round2-v2-company-control-plane-a2a-permission-design-2026-06-24.md`
- `@docs/a2a-relationship-group-collaboration-plan-2026-06-20.md`
- `@docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md`
- `@hive-connect:AGENTS.md`
- `@hive-connect:docs/bridge-protocol.zh-CN.md`
- `@hive-connect:docs/plans/2026-03-13-session-resilience-design.md`
- `@hive-connect:docs/plans/2026-03-12-multi-workspace-design.md`

**@渠道适配按需读取**：`@hive-connect:docs/dingtalk.md`、`@hive-connect:docs/feishu.md`、`@hive-connect:docs/slack.md`、`@hive-connect:docs/management-api.zh-CN.md`、`@docs/a2a-relationship-retirement-plan-2026-06-27.md`。

**源码入口**：Hive A2A execution frame、channel ingress/outbox/delivery、identity binding、Hive Connect bridge/session/workspace/channel adapters。

**首个 Red**：让同 owner 的 A/B/C/D Agent 在钉钉、飞书、Slack、Web 交错协作，注入 duplicate/reorder/ack loss/rate limit/auth expiry、delegation revoke 和部分渠道失败，证明 Agent terminal、channel sent、delivered、read 或 parent consumed 被错误合并。

**退出门**：每 hop fresh-check principal/delegation/sensitivity/residency；Agent work/result 与每 destination delivery 正交；route/delivery ledger durable；duplicate/reorder/ack loss/auth revoke idempotent；final destination 显式；channel fairness 独立于 fleet fairness。必须通过 SESSION-G8 与真实/沙箱钉钉、飞书、Slack、Web fault matrix。证据写入 `EVID-G7-*`。

### Group 8：Memory、Knowledge、证据完整性与恢复

**Owner leaf（9）**：`C-BP1`、`P1-008`、`P1-017`、`C-BP2`、`C-BP3`、`C-BP4`、`C-BP5`、`C-BP6`、`F-OBS1`。**Owner Missing（2）**：`MISS-EK-001`、`MISS-RETENTION-001`。

**依赖 Group**：Group 0–2、Group 6。durable Memory/Knowledge intelligence 必须使用可信 authority、canonical evidence 与 Context Resource Plane；与 Group 7 共享 retention/delivery 验收时仍分别保留 owner。

**@必须先读**：

- `@docs/memory-clean-loop-refactor-plan-2026-06-17.md`
- `@docs/memory-system-flow-map-2026-06-17.md`
- `@docs/memory-vault-path-contract-2026-06-23.md`
- `@docs/agent-memory-md-first-spec.md`
- `@docs/agent-memory-purity-spec.md`
- `@docs/self-evolution-sota-plan.md`
- `@docs/t0-append-only-session-ledger-redesign-2026-06-18.md`
- `@docs/company-knowledge-base-spec-2026-07-07.md`
- `@docs/knowledge-pyramid-agent-person-org-2026-07-03.md`
- `@docs/personal-company-knowledge-tool-boundary-2026-07-10.md`
- `@docs/knowledge-substrate-plugin-architecture-2026-07-09.md`

**@按需读取**：`@docs/personal-knowledge-base-spec.md`、`@docs/personal-knowledge-base-implementation-plan-2026-07-07.md`、`@docs/personal-knowledge-base-completion-contract-2026-07-08.md`、`@docs/subagent-evolution-loop.md`、`@docs/eval-system-spec.md`。

**源码入口**：terminal hook/T2 job/outbox、T0 projection/hash verifier、T2/T3 write authority/locks、capability factor consumers、Memory availability gates、Knowledge ACL/index/retention/audit。

**首个 Red**：在 terminal commit 后注入 T2 provider outage、worker crash/restart、dead-letter/requeue、T0 hash tamper、并发 T3 write、Knowledge ACL revoke 与 retention/legal hold；证明 terminal 被阻塞、证据不可验、锁外写、永久 held 或跨资产删除不守恒。

**退出门**：terminal commit 与 T2 intelligence 分离；T0→T2→T3→soul source refs 可验证；retry/dead-letter/admin requeue；无锁外语义写；Memory failure 只降级相关能力；Enterprise Knowledge organization authority 与 retention/legal hold 真实闭环；跨 Memory/Knowledge/Artifact/Audit deletion/export 可追踪。证据写入 `EVID-G8-*`。

### Group 9：产品消费、UI、迁移与旧路径退出

**Owner leaf（19）**：`G-02`、`H-404a`、`H-404b`、`G-03`–`G-10`、`G-11`–`G-18`。**Owner Missing（1）**：`MISS-AIASSET-001`。

**依赖 Group**：Group 0、Group 2、Group 4、Group 6–8。UI/Workspace/Artifact 只能消费已建立的 typed truth/ref；不能用前端 heuristic 提前模拟尚不存在的 backend contract。

**@必须先读**：

- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`（§17–§20、§23–§28）
- `@docs/frontend-design-refinement-2026-07-03.md`
- `@docs/session-timeline-projection-contract-2026-07-04.md`
- `@docs/session-rendering-overhaul-plan-2026-07-03.md`
- `@docs/session-rendering-streaming-cc-codex-gap-analysis-2026-07-03.md`
- `@docs/session-rendering-s6-completion-plan-2026-07-04.md`
- `@docs/session-right-rail-runtime-console-design-2026-07-03.md`
- `@docs/ccplus-session-ux-contract-2026-06-26.md`
- `@docs/chat-artifact-delivery-redesign-2026-06-20.md`
- `@docs/org-agent-asset-rights-model.md`
- `@docs/agent-team-session-workbench-root-cause-and-repair-plan-2026-07-02.md`

**@按需读取**：`@docs/ccplus-session-full-landfall-2026-07-09.md`、`@docs/ccplus-session-checkpoint-branch-ui-upgrade-plan-2026-06-27.md`、`@docs/ccplus-session-tui-unified-expression-plan-2026-06-28.md`、`@docs/session-workspace-hr-atomic-closure-2026-07-10.md`。

**源码入口**：Session Workbench typed store/reducer/renderers/right rail、Messages read receipts、channel test contract、i18n catalogs/CI、Artifact/Workspace/AI Asset projections、legacy readers/backfill.

**首个 Red**：对同一 typed fixture 比较主时间线/right rail/live/history/reload，复现 Messages/channel test 404、缺失 i18n key、Artifact 已生成但主 Agent/Workspace 不可见、legacy backfill 误猜 identity 与未覆盖 AI Asset。

**退出门**：主时间线/right rail/Artifact/parent coverage/channel delivery 消费同一 typed store/ref；historical backfill 可复算且 unknown 不猜；V1 heuristic reader/writer 删除；Messages/channel test/i18n 真实闭环；AI Asset coverage 明确；SESSION-G5/G12 与浏览器 E2E、byte/structure snapshots、production acceptance 全过。证据写入 `EVID-G9-*`。

### Group 10：Goal 1 行为门、残余重认证与总账清零

**Owner leaf（19）**：`P2-018`、`A-05`–`A-08`、`E-3`–`E-7`、`C-BP8`–`C-BP12`、`B-05`–`B-07`、`D-KB3`。这些 inherited P3/P2 在施工前必须恢复具体语义与当前源码证据；不得以旧标题直接修。**Owner Missing（1）**：`MISS-EVAL-001`。

**依赖 Group**：Group 0–9 的对应行为证据。它是 Goal 1 非劣、residual leaf 重认证和程序总账清零门，不反向阻塞已经独立闭环的 Group 1 安全发布。

**@必须先读**：

- `@docs/hive-sota-master-goal.md`
- `@docs/self-evolution-sota-plan.md`
- `@docs/eval-system-spec.md`
- `@docs/round2-sota-benchmark-2026.md`
- `@docs/harness-engineering-audit-2026-06-11.md`
- `@docs/single-agent-framework-atomic-review-2026-07-02.md`
- `@docs/final-atomic-review-2026-07-09.md`
- `@docs/runtime-model-agency-constraint-audit-2026-07-13.md`
- `@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`
- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`

**@历史恢复依据**：`@docs/agent-native-atomic-review-2026-07-14.md`、`@docs/agent-native-atomic-review-501db655.md`、`@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md`。先恢复 leaf 的输入/权威/执行/证据/恢复/消费/验收，再决定 closed/refuted/merge/rewrite。

**源码入口**：行为 eval runner/evidence/referee/promotion/rollback，及 owner leaf 重认证后确认的 live entry/consumer；不得按旧文件行号盲改。

**首个 Red**：先对 19 个 inherited leaf 做 refute-first 当前源码重认证；随后用同 model/provider/tool fixture/corpus 运行 baseline/candidate paired replay，证明当前 eval 缺少真实 execution、LLM referee、behavior receipt、provisional promotion 或 rollback，且不能从结构测试推出 CCPlus 非劣。

**退出门**：真实 candidate/baseline 同模型同 fixture 执行；LLM referee + behavior receipt + provisional/rollback；Goal 1 对 CC/Hermes 非劣；19 个 residual leaf 全部以当前证据 closed/refuted/重新定级；103 open=0，5 Missing 均 closed 或有 owner 明确裁决；所有 Group 证据完整、文档与生产 truth 一致。Goal 1 未完成前不能以 UI/KISS 数量宣称 CCPlus 完成，但不反向阻塞已闭环安全修复发布。证据写入 `EVID-G10-*`。

## 10. Migration、Backfill 与 Rollback

### 10.1 Session

- 在 `ChatTranscriptEvent` 上增加/收敛 stable item/lifecycle/ordinal 与 event outbox，不建第二张 session truth 表。
- backfill 来源优先级：原 transcript/provider blocks → spans/tool receipts/workflow/subagent journals → T0 → ThreadItem → ChatMessage/thinking。
- 无法证明 phase/identity 的记录标 `legacy_unknown`，保留原 bytes 与 coverage gap；不得用文本相似度猜。
- dry-run 输出确定/模糊/缺失计数，apply 幂等、可回滚；V2 consumer 完成后删除旧 heuristic 写/读入口。

### 10.2 Root/result/mailbox

- 从 `RuntimeTask.root_runtime_task_id`、child session、workflow journal、team membership 与 outbox 回填 root item。
- 无法证明是否 admitted/terminal 的历史项进入 quarantine/reconciliation，不猜 complete。
- 历史 large result 生成 ref/hash，但不重写 transcript/T0 原 bytes。
- mailbox dual-read 期间只有新 row 是 author；旧 JSON 只导入一次并打 idempotency key，禁止双写双权威。

### 10.3 Fleet

- 为现有 pending RuntimeTask 计算 fairness key；不改变 task identity/priority policy。
- trigger backfill `next_evaluate_at` 与 shard key；初始 cursor 从最老 due item 开始。
- rollback 可以切回兼容 claim/read，但必须保留新 queue/root/result/session evidence，不能丢 queued work。

## 11. 极端测试与验收

可复用 Prompt 当前共有 40 个 `X-*` 强制极端场景；其中本次纠偏新增/改写并在统一报告中突出的是：

- `X-ROOT-01`：同一 root 混合 direct/team/workflow 的 100 child；
- `X-FLEET-01`：2k/10k/50k Agent/trigger definition 控制面曲线；
- `X-FLEET-02`：一个 noisy root 与 1,000 个其它 root 的 fairness；
- `X-SES-01`：live/history/reconnect/reload/resume 同 reducer；
- `X-SES-02`：interleaved lifecycle、gap/out-of-order/duplicate/publish failure。

下面的 primary owner 只负责该场景的 harness、主断言和证据汇总；场景触及其它 Group 时仍必须消费对方合同，但不得复制测试 ID 或制造第二 owner。

<!-- extreme-scenario-owner-map-start -->
- X-FAN-01 | Group 4 | 100-way large-result durable commit、manifest 与 bounded fan-in
- X-FAN-02 | Group 4 | mixed terminal、partial/late/duplicate coverage
- X-FAN-03 | Group 3 | nested budget 与真实拓扑 cycle
- X-FAN-04 | Group 3 | cancel/restart/lease expiry 与 monotonic terminal
- X-FAN-05 | Group 4 | streaming event storm、coalescing 与 parent bounded consumption
- X-ROOT-01 | Group 3 | direct/team/workflow 统一 root requested/admitted/expected ledger
- X-FLEET-01 | Group 5 | 2k/10k/50k definition/trigger 分页与 crash-resume
- X-FLEET-02 | Group 5 | noisy-root、tenant/root fairness 与 control-plane reserve
- X-CAP-01 | Group 6 | 400 Skill/200 MCP/大资源目录的 O(1) resident kernel
- X-CAP-02 | Group 6 | namespace/version/untrusted descriptor/auth freshness
- X-DISC-01 | Group 6 | 尾页能力 discover/load 与撤权 fresh-check
- X-MCP-01 | Group 6 | 200 MCP transport/schema/auth 故障隔离
- X-MEM-01 | Group 6 | 10^3→10^6 Memory 可发现、coverage 与 authority
- X-CTX-01 | Group 6 | soft waterline、尾部证据与 provider physical window
- X-CTX-02 | Group 6 | model-led compaction failure、coverage 与恢复
- X-CTX-03 | Group 6 | 大小模型切换前 compatibility preflight
- X-OUT-01 | Group 6 | max_output/stream replay 与 same-model continuation
- X-ONE-01 | Group 6 | 单 Session/单模型 overflow/unavailable 恢复
- X-RESULT-01 | Group 4 | 超大/压缩结果 artifact/ref、hash 与 UI/parent consumption
- X-BUD-01 | Group 6 | soft budget 与真实 context/cost/cancel hard fact 分态
- X-BUD-02 | Group 3 | parent/child reserve/commit/release 与幂等重试
- X-LIM-01 | Group 6 | threshold mutation 与无语义 cliff
- X-LIVE-01 | Group 3 | timeout/retry/approval/queue/breaker wait-for 收敛
- X-QUEUE-01 | Group 5 | durable queue saturation、fairness 与 restart drain
- X-SAFE-01 | Group 1 | 单 effect denial 不冻结无关 Agent 能力
- X-A2A-01 | Group 7 | 四 Agent 四渠道 root authority/result/delivery
- X-A2A-02 | Group 7 | rate limit/auth/duplicate/reorder/ack-loss
- X-A2A-03 | Group 7 | cross-owner/tenant、delegation revoke 与 sensitivity ceiling
- X-A2A-04 | Group 7 | identity race、causal ordering 与 final destination
- X-A2A-05 | Group 7 | webhook authenticity、replay、size 与 residency
- X-LOOP-01 | Group 3 | wait-for cycle 与最小边恢复
- X-INJ-01 | Group 1 | untrusted child/tool/channel result 与 schema repair
- X-OBS-01 | Group 6 | typed pressure/hard stop observation 与恢复入口
- X-OBS-02 | Group 6 | 10k 重复 observation 聚合且保留 material transition
- X-SES-01 | Group 2 | live/history/reconnect/reload/resume 同 reducer
- X-SES-02 | Group 2 | interleaved lifecycle、gap/out-of-order/duplicate/publish failure
- X-REC-01 | Group 4 | result/notification/fan-in kill-point transactional recovery
- X-CACHE-01 | Group 6 | stable catalog prefix、dynamic auth suffix 与 cache evidence
- X-WF-01 | Group 3 | 1万节点 DAG、动态展开、环与 partial join
- X-CCP-01 | Group 10 | 同 model/provider/fixture/corpus 的 CCPlus paired replay
<!-- extreme-scenario-owner-map-end -->

<!-- liveness-gate-owner-map-start -->
- LB-1 | Group 6 | hard fact authority 与内部常量不得终态化
- LB-2 | Group 2 | attempt/task/session delivery 分离
- LB-3 | Group 2 | progress certificate、resume edge 与 owner
- LB-4 | Group 3 | reserve + durable admission 先于 expected
- LB-5 | Group 3 | no-hold-and-wait、资源全序与 lease 回收
- LB-6 | Group 3 | retry fingerprint 与单调进展
- LB-7 | Group 4 | full barrier 的 partial/late/failure/cancel policy
- LB-8 | Group 4 | durable result/checkpoint first 与 control-plane headroom
- LB-9 | Group 6 | material observation 可见且重复聚合
- LB-10 | Group 6 | 单模型/provider unavailable/restart/late callback 恢复
<!-- liveness-gate-owner-map-end -->

最小验收不变量：

1. `requested = admitted + deferred + not_admitted`；只有 admitted 进入 expected。
2. `expected = live + terminal_received + terminal_missing`；late/duplicate 单独可重算。
3. 100-way raw result bytes 不随 N 线性进入 parent Prompt；完整 bytes 仍可按 ref/range 读取。
4. parent integration invocation 数随 material epoch/page 有界，不等于 child terminal 数。
5. noisy root 下其它已 admission 交互任务不永久饥饿；control-plane cancel/approval/checkpoint 始终有槽。
6. 10k Agent definitions 不产生 10k 模型进程；trigger scan 可分页、重启续扫。
7. live/history/reload/resume 的 `SessionItem[]` snapshot 同构；visibility 只允许 exact redaction 差异。
8. duplicate/out-of-order/gap 不复制 item、不丢 terminal；publish failure 可由 outbox/history 补齐。
9. 400 Skill、200 MCP、巨大 Memory 的首轮 Prompt 相对总资源量有界，尾部授权资源可搜索/load。
10. 模型 final bytes 除 exact unauthorized-secret redaction 外保持 byte-faithful。

本轮没有安全实跑真实 100 个付费模型、50,000 Agent production fleet 或真实 IM storm。对应实现能力仍不能标“极端规模闭环”；本文交付的是源码确证、可执行场景与最终施工契约。

## 12. 全部 103 个 canonical leaf

### 12.1 唯一 owner Group 映射

Group 0 是全局证据门，不拥有业务 leaf。下面 103 行必须与 canonical ledger 一一同构：每个 leaf 恰好一个 owner Group；跨组依赖写在 Group runbook 和证据记录中，不复制 owner。Group 10 的 19 个 inherited leaf 是显式 owner 清单，不再用“剩余账本”兜底。

<!-- group-owner-map-start -->
- Group 1 | P0-F1
- Group 1 | P0-F2
- Group 1 | E-1
- Group 1 | P1-004
- Group 1 | P1-F4
- Group 1 | KB-AUTH-001
- Group 1 | KB-EXTRACT-001
- Group 1 | KB-PROP-001
- Group 1 | AUDIT-IMM-001
- Group 1 | AUDIT-TENANT-001
- Group 1 | F-PLAINTEXT
- Group 1 | P2-F8
- Group 1 | P2-F6
- Group 1 | KB-CONTRACT-001
- Group 1 | B-01
- Group 1 | BUD-ROOT-001
- Group 2 | G-01A
- Group 2 | A-01
- Group 2 | A-04
- Group 2 | B-02
- Group 2 | B-03
- Group 2 | G-01B
- Group 2 | B-04
- Group 2 | D-KB4
- Group 2 | SES-ACCEPT-001
- Group 2 | SES-ITEM-001
- Group 2 | SES-PROJECTION-001
- Group 2 | SES-PROSE-001
- Group 2 | SES-TRANSPORT-001
- Group 2 | SES-CONSUMER-001
- Group 3 | A2A-ADMISSION-001
- Group 3 | SUBAGENT-ADMISSION-001
- Group 3 | A2A-CYCLE-001
- Group 3 | A2A-TERMINAL-001
- Group 3 | TEAM-FANOUT-001
- Group 3 | SUBAGENT-APPROVAL-001
- Group 3 | ROOT-TREE-001
- Group 4 | E-2
- Group 4 | XCB-RESULT-001
- Group 4 | CONC-FANIN-001
- Group 4 | CONC-WAKE-002
- Group 4 | WF-PARTIAL-001
- Group 4 | CONC-MAILBOX-001
- Group 5 | FLEET-SCHED-001
- Group 5 | FLEET-TRIGGER-001
- Group 6 | A-03
- Group 6 | XCB-CTX-001
- Group 6 | XCB-CAP-001
- Group 6 | XCB-MEM-001
- Group 6 | XCB-OUT-001
- Group 6 | XCB-LIM-001
- Group 6 | XCB-MCP-001
- Group 6 | XCB-OBS-001
- Group 6 | WF-HARDLIMIT-001
- Group 6 | BUD-BREAKER-001
- Group 7 | CHANNEL-FAIRNESS-001
- Group 8 | C-BP1
- Group 8 | P1-008
- Group 8 | P1-017
- Group 8 | C-BP2
- Group 8 | C-BP3
- Group 8 | C-BP4
- Group 8 | C-BP5
- Group 8 | C-BP6
- Group 8 | F-OBS1
- Group 9 | G-02
- Group 9 | H-404a
- Group 9 | H-404b
- Group 9 | G-03
- Group 9 | G-04
- Group 9 | G-05
- Group 9 | G-06
- Group 9 | G-07
- Group 9 | G-08
- Group 9 | G-09
- Group 9 | G-10
- Group 9 | G-11
- Group 9 | G-12
- Group 9 | G-13
- Group 9 | G-14
- Group 9 | G-15
- Group 9 | G-16
- Group 9 | G-17
- Group 9 | G-18
- Group 10 | P2-018
- Group 10 | A-05
- Group 10 | A-06
- Group 10 | A-07
- Group 10 | A-08
- Group 10 | E-3
- Group 10 | E-4
- Group 10 | E-5
- Group 10 | E-6
- Group 10 | E-7
- Group 10 | C-BP8
- Group 10 | C-BP9
- Group 10 | C-BP10
- Group 10 | C-BP11
- Group 10 | C-BP12
- Group 10 | B-05
- Group 10 | B-06
- Group 10 | B-07
- Group 10 | D-KB3
<!-- group-owner-map-end -->

### 12.2 Canonical 证据账本

`inherited-recheck` 表示来自前一工作账本，本轮未重新执行该 leaf 的全部验收；它仍在当前 ledger，但开工前必须按当前 checkout 重验。`current-confirmed` 表示本轮重新读取了直接源码。family、alias、scenario、coverage gap、Missing 不计数。

<!-- canonical-ledger-start -->
- P0 | P0-F1 | in_progress-local-green:EVID-G1-001 | agent-controlled `web_fetch` / URL-import / remote-fetch-forwarding SSRF family；production canary 待执行
- P1 | P0-F2 | in_progress-local-green:EVID-G1-002 | migration/RLS readiness 已本地 fail-closed；独立 commit、deploy 与 production startup canary 待执行
- P1 | E-1 | inherited-current-evidence | durable subagent requester 被 creator 顶替
- P1 | P1-004 | inherited-current-evidence | A2A inner effect 丢 outer execution frame
- P1 | P1-F4 | inherited-current-evidence | RecoveryManifest 缺恢复授权绑定
- P1 | C-BP1 | inherited-current-evidence | terminal hook 同步 T2 LLM 阻塞完成
- P1 | P1-008 | inherited-current-evidence | Memory dependency failure 冻结无关 effect
- P1 | P1-017 | inherited-dirty-fix-unaccepted | transcript commit 与 T0 wake 可见性
- P1 | G-01A | inherited-split | 平台 failure prose 冒充 assistant/final author
- P1 | KB-AUTH-001 | inherited-split | cross-principal PKB requester/grant/ceiling
- P1 | KB-EXTRACT-001 | inherited-split | sensitivity canonical enum 与 extraction blocklist 漂移
- P1 | KB-PROP-001 | inherited-split | sensitivity/provenance 未贯穿 transcript/T0/T2/outbound
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
- P2 | AUDIT-IMM-001 | inherited-split | 审计表数据库层可修改
- P2 | AUDIT-TENANT-001 | inherited-split | tenant=None 安全审计静默丢弃
- P2 | F-PLAINTEXT | inherited-current-evidence | agent tool config 明文 MCP credential
- P2 | P2-F8 | inherited-current-evidence | `rg` 参数缺 `--` 可 flag injection
- P2 | P2-F6 | inherited-current-evidence | model config 写入缺 cross-tenant reference 校验
- P2 | KB-CONTRACT-001 | inherited-split | Knowledge tool description/spec/implementation 不一致
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
- P2 | G-01B | inherited-split | UI 以 `includes('expired')` 决定 hard state
- P3 | B-01 | inherited-recheck | HR 受信固定业务体绕统一 tool throat
- P3 | A-05 | inherited-recheck | 旧报告单 Agent leaf A-05
- P3 | A-06 | inherited-recheck | 旧报告单 Agent leaf A-06
- P3 | A-07 | inherited-recheck | 旧报告单 Agent leaf A-07
- P3 | A-08 | inherited-recheck | 旧报告单 Agent leaf A-08
- P3 | B-04 | inherited-merged-alias-A-09 | 结果自然语言 failure 词仅驱动 warn/counter
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
- P1 | XCB-CTX-001 | inherited-current-evidence | pre-model 20% Prompt hard cap
- P1 | XCB-CAP-001 | inherited-current-evidence | capability catalog 无 progressive wave/cursor
- P1 | XCB-MEM-001 | inherited-current-evidence | Memory 全量候选与 resident 聚合
- P1 | XCB-OUT-001 | inherited-current-evidence | output continuation 固定三次后假 final
- P1 | XCB-LIM-001 | inherited-current-evidence | tool-round cliff/平台终答/预算假接线
- P1 | XCB-RESULT-001 | inherited-current-evidence | raw tool/media result 多副本驻留
- P2 | XCB-MCP-001 | inherited-current-evidence | MCP execution-time schema/auth fresh-check 缺失
- P1 | XCB-OBS-001 | inherited-current-evidence | stream/parts 无界且 pressure observation 缺失
- P1 | CONC-FANIN-001 | inherited-current-evidence | full child result 直接进入 parent context
- P1 | CONC-WAKE-002 | reworded-current-confirmed | per-child delivery intent 无 root integration epoch/coalesced manifest
- P1 | A2A-ADMISSION-001 | inherited-current-evidence | queued ghost delegation
- P1 | SUBAGENT-ADMISSION-001 | inherited-current-evidence | ghost child session
- P1 | A2A-CYCLE-001 | inherited-current-evidence | durable/restart cycle guard 缺失
- P1 | A2A-TERMINAL-001 | inherited-current-evidence | late completion 可覆盖 cancel/kill
- P1 | CHANNEL-FAIRNESS-001 | reclassified-plane-current | channel ingress/delivery 全局 FIFO
- P1 | TEAM-FANOUT-001 | inherited-current-evidence | Agent Team 半启动无 coverage ledger
- P1 | WF-HARDLIMIT-001 | inherited-current-evidence | Workflow 固定方便性上限 hard fail
- P1 | WF-PARTIAL-001 | inherited-current-evidence | Workflow partial join/result contract 缺失
- P1 | BUD-BREAKER-001 | inherited-current-evidence | runtime breaker 机械终止/cancel
- P1 | BUD-ROOT-001 | inherited-current-evidence | budget root failure work-amplification fail-open
- P1 | SUBAGENT-APPROVAL-001 | inherited-current-evidence | foreground approval 无 durable intent
- P1 | CONC-MAILBOX-001 | inherited-current-evidence | parent mailbox JSON lost-update race
- P1 | ROOT-TREE-001 | added-current-confirmed | direct/team/workflow 无统一 root coverage/result/integration ledger
- P1 | FLEET-SCHED-001 | added-current-confirmed | RuntimeTask 全局 priority/FIFO 无 tenant/root fairness
- P2 | FLEET-TRIGGER-001 | added-current-confirmed | trigger daemon 全量 O(N) scan 无 page/shard/cursor
- P2 | SES-ACCEPT-001 | added-current-confirmed | accepted input canonical event 延迟到 worker
- P1 | SES-ITEM-001 | added-current-confirmed | stream 无 stable item lifecycle 且 thinking 聚合进 final 附件
- P1 | SES-PROJECTION-001 | added-current-confirmed | user/live projection 删除关联 identity
- P2 | SES-PROSE-001 | added-current-confirmed | 平台固定 reasoning 文案冒充模型过程
- P2 | SES-TRANSPORT-001 | added-current-confirmed | transcript 与 live publish 非同一 outbox/envelope/sequence
- P1 | SES-CONSUMER-001 | added-current-confirmed | typed item 降级后由启发式 timeline 重建
<!-- canonical-ledger-end -->

### 12.3 Group 修复证据索引

本节是后续施工证据的唯一目录，不是测试结果占位符。每次修复必须先创建稳定的 `EVID-G<group>-<序号>` 记录，再把同一证据 ID 回填到对应 canonical leaf 或 Missing；一个证据可以覆盖同根家族的多个 leaf，但不能因此合并它们的独立状态。Group 标绿前，索引、leaf 状态、测试结果、迁移状态、部署状态与实际 consumer 必须一致。

<!-- group-evidence-index-start -->
| Group | 证据前缀 | Owner 范围 | 当前证据状态 | 下一次写入要求 |
|---:|---|---|---|---|
| 0 | `EVID-G0-*` | 0 leaf / 0 Missing | `closed`：`EVID-G0-001/002`；文档 Git truth、owner/path/decision/scenario CI、跨仓快照与现有 fake-provider/PG/Redis harness 基座成立 | 后续仅在 ledger/路径/场景变化时追加 delta；业务场景 Green 由 owner Group 负责 |
| 1 | `EVID-G1-*` | 16 leaf / 0 Missing | `in_progress`：`EVID-G1-001` 已完成 P0-F1 本地 Red→Green 与仓级回归；`EVID-G1-002` 已完成 P0-F2 本地 Red→Green、真实 PG rollback/re-upgrade 与 production read-only catalog preflight；两项 deploy/canary 均 open，其余 14 leaf open | 先形成 P0-F2 独立 commit；再按 P0/P1 可独立发布家族继续写 production canary 与 authority/credential/budget 闭环 |
| 2 | `EVID-G2-*` | 14 leaf / 0 Missing | `open` | 写 Session event/item/reducer、persist-before-publish、projection/backfill 与 SESSION-G 结果 |
| 3 | `EVID-G3-*` | 7 leaf / 0 Missing | `open` | 写 root admission、reserve/commit/release、terminal CAS、approval resume 与 fanout 曲线 |
| 4 | `EVID-G4-*` | 6 leaf / 0 Missing | `open` | 写 result ref、mailbox lease/CAS、integration epoch、partial/late/duplicate 与 100-way return storm |
| 5 | `EVID-G5-*` | 2 leaf / 0 Missing | `open` | 写 fleet scheduler/trigger benchmark、公平性、分页续扫与 control-plane reserve |
| 6 | `EVID-G6-*` | 10 leaf / 0 Missing | `open` | 写 CTX-A–F、capacity ledger、progressive disclosure、compaction/output recovery 与尾部证据覆盖 |
| 7 | `EVID-G7-*` | 1 leaf / 1 Missing | `open` | 写跨渠道 execution/delivery ledger、逐 hop authority、fault matrix 与真实/沙箱 channel 分层证据 |
| 8 | `EVID-G8-*` | 9 leaf / 2 Missing | `open` | 写 T0→T2→T3→soul、durable intelligence job、Enterprise Knowledge、retention/legal hold 与恢复证据 |
| 9 | `EVID-G9-*` | 19 leaf / 1 Missing | `open` | 写 canonical UI consumer、legacy 退出、historical backfill、Artifact/AI Asset 与浏览器/生产验收 |
| 10 | `EVID-G10-*` | 19 leaf / 1 Missing | `open` | 写 inherited leaf 重认证、真实 behavior eval、paired replay、Goal 1 非劣与总账清零证据 |
<!-- group-evidence-index-end -->

### 12.4 单 leaf / 同根家族证据记录模板

后续证据直接追加在本节之后，禁止只在 commit、PR、外部聊天或临时测试日志中留存。证据较大时可以落到稳定 artifact/报告，但本文必须保留可验证 ref、hash、命令、结果摘要和当前状态。

```markdown
#### EVID-G<group>-<序号>：<修复家族或 leaf 名称>

- `leaf_ids`：
- `missing_ids`（如适用）：
- owner Group / 依赖 Group：
- 当前状态：`open | in_progress | blocked | closed | refuted | missing`
- 证据 owner / 更新时间：
- 冻结事实：HEAD、worktree、相关文件 hash、环境、部署 ID：
- 已完整读取的 `@必须先读` 文档及版本/hash：
- 按需读取文档与选用理由：
- 当前 live entry / authority source / unique writer / consumer：
- Red：命令、退出码、正确失败原因、原始症状：
- 实现：状态机、数据模型、权限点、model-agency 裁决、删除的旧路径：
- migration / dry-run / backfill / cleanup / rollback：
- Green：精确命令、零失败结果、扩展回归：
- fault / capacity / concurrency / security / observability：
- UI / Artifact / parent / Memory / Knowledge 等真实消费：
- commit / deploy / production canary（如适用）：
- 七原子结论：Input / Authority / Execution / Evidence / Recovery / Consumption / Acceptance：
- 残余风险、coverage gap、下一可达动作：
- 对应 §12.2 canonical 行状态更新：
- 对应 §13.1 Missing 行状态更新（如适用）：
```

证据写入后必须同步执行三项更新：

1. 把 §12.2 对应 leaf 的状态改成 `in_progress`、`closed`、`refuted` 或新的当前证据状态，并附 `EVID-*`；不得只改 Group 汇总。
2. 更新 §12.3 对应 Group 的证据状态；只有 owner leaf 与 owner Missing 全部满足退出门时才可标 `closed`。
3. 若证据改变分母，先记录 `added / merged / split / refuted / reclassified / closed` delta，再同步 §1 数量、§9 Group owner、§12.1 owner map、§12.2 ledger 与 §13.1 Missing；禁止局部改数字。

#### EVID-G0-001：终极修复文档路由与账本编排

- `leaf_ids`：无；Group 0 不拥有业务 leaf。
- `missing_ids`：无；本证据只验证 5 个 Missing 的唯一 Group 归属，不宣称能力已经实现。
- owner Group / 依赖 Group：Group 0 / 无。
- 当前状态：`partial`。
- 证据 owner / 更新时间：Codex docs compilation / 2026-07-15。
- 冻结事实：HEAD `501db6555dae374e5fcf43a6fdcfe8a3dd89343e`；工作树存在其它 session 改动；本轮只编辑四份 Markdown，不运行实现迁移或部署。
- Red 观察：修订前 Group 章节不能机械证明 103/103 唯一 owner，5 个 Missing 也没有完整建设归属；两份关键设计文档没有完整的决策/黄金轨迹 → Group 交叉表和同文档证据回填合同。
- 实现：新增 §0 使用/权威/写回合同、§8.1 两份关键设计交叉表、§9 Group 0–10 文档路由/依赖/Red/退出门、§12.1 owner map、§12.3 证据索引、§12.4 模板与 §13.1 Missing owner map；同步 Prompt 与两份关键设计文档的施工消费合同。
- 静态验证命令：本轮终端执行 read-only `python - <<'PY'` marker/path/fence validator，以及 `git diff --check -- docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`、`git diff --cached --check -- docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`。
- 静态验证结果：canonical `103/103 unique`；severity `P0=1 / P1=37 / P2=36 / P3=29`；owner `103/103 unique`；Group counts `0/16/14/7/6/2/10/1/9/19/19`；Missing `5/5 unique`；evidence index `11/11`；`CTX=6`、`S=12`、`SESSION-G=13`；`@docs`/外部 Markdown path `85/85 exists`；四份文档 fence、尾随空白、NUL、末尾换行检查通过；两个 Git diff check 均 exit 0。
- 非自引用文档 SHA-256：Prompt `3745a103d78993a094eb5993fbd9ae66e907a841cb5a082badb467e26af3c186`；Context `c83a1f94b206af7de8bc44f7f4de35746c65d255574a347cbfd80ce0cc3075b7`；Session `52a13072ef51ec1ad8f22be5f484b274880c4b7aea801104bd4ca5cdc27c0ac4`。总报告自身不内嵌自引用 hash；在 Git 纳管/commit 时记录 blob/commit ID。
- Git truth：`docs/session-v2-cc-codex-alignment-contract-2026-07-14.md` 当前为既有 staged new file + 本轮 unstaged 集成说明（`AM`）；总报告、Prompt、Context 文档命中 `.gitignore:36:docs/`，本轮没有越权 `git add -f`、覆盖或接管其它 session 的 staged 内容。
- 七原子结论：文档 Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 路由已编排；业务 leaf 七原子未因此闭环。
- 残余 gate：把 owner/path/decision/evidence 的正负断言持久化到 CI；为 1/10/25/50/100 fanout、2k/10k/50k fleet、400 Skill/200 MCP、跨渠道 fault 建立可执行 harness；明确四份文档的 Git ownership 后，Group 0 才能从 `partial` 转 `closed`。

#### EVID-G0-002：Git truth、可移植文档路由与持久验证门

- `leaf_ids` / `missing_ids`：无；只关闭 Group 0 全局证据门，不改变 103 + 5 业务分母。
- owner Group / 依赖 Group：Group 0 / 无。
- 当前状态：`closed`。
- 证据 owner / 更新时间：主 Agent / 2026-07-15。
- 冻结事实：HEAD `501db6555dae374e5fcf43a6fdcfe8a3dd89343e`；本 Group staged manifest 恰好为 §0.4 的 6 个路径；66 个 tracked unstaged 与 8 个 untracked 外部路径未纳入本 Group。
- Red 1：`cd backend && source .venv/bin/activate && pytest tests/architecture/test_agent_native_repair_ledger.py -q` → `2 failed, 5 passed`；正确失败为总报告/Prompt/Context 未进入 Git truth，以及 Hive Connect `@` 路由依赖 `/Users/...` 绝对路径。
- Red 2：增加 portability 修复与场景 owner 断言后，同命令 → `2 failed, 6 passed`；正确失败为文档仍未 Git-tracked，以及 40 个 `X-*` / 10 个 LB 门缺机器 owner map。
- 实现：四份真相文档与 `docs/README.md` 强制纳入 Git；新增 103 leaf/5 Missing/11 Group/6 CTX/12 Session/13 golden/40 extreme/10 LB 的机器守恒测试；Hive Connect 8 份文档改为 logical ref，并绑定 remote、commit 与逐文件 SHA-256；新增 §0.4 commit ownership 规则。
- Harness 基座：复用 `backend/tests/journeys/fake_external_provider.py` 的受控 model/channel/sandbox/local-bridge provider；`.github/workflows/harness-ci.yml` 已在 hermetic full pytest 之外提供真实 PostgreSQL 15 + Redis 7 的 atomic journey job；40 个极端场景和 10 个活性门已唯一分派，具体行为 Green 由其 owner Group 实现，Group 0 不伪造通过结果。
- Green 1：`cd backend && source .venv/bin/activate && pytest tests/architecture/test_agent_native_repair_ledger.py -q` → `8 passed in 0.23s`。
- Green 2：`ruff check tests/architecture/test_agent_native_repair_ledger.py` → `All checks passed!`。
- Green 3：`pytest tests/architecture/test_model_agency_no_semantic_truncation.py tests/evals/test_harness_ci_workflow.py tests/architecture/test_harness_validation_contract.py -q` → 初次发现 timeless 旧文案含“第一轮”，改为“恢复后的首次模型调用”后 `40 passed in 1.39s`。
- Green 4：`pytest tests/architecture -q` → `170 passed in 11.98s`。
- Green 5：`git diff --cached --check` 与本 Group 路径的 `git diff --check` → exit 0。
- 当前非自引用 SHA-256：Prompt `dad2b37a75a9fdeb7d23135bb606b96d11fa4a37bc5eebb2428e4bd50477b02e`；Context `c83a1f94b206af7de8bc44f7f4de35746c65d255574a347cbfd80ce0cc3075b7`；Session `52a13072ef51ec1ad8f22be5f484b274880c4b7aea801104bd4ca5cdc27c0ac4`；validator `c03d1d2db79a3de02377c4ed6c9e1a02610ea0ece7cecf4e35290c5f49dd90fa`；docs index `aa17eff0b7c7ae16ff23945fff9b842c1dd69bff9aad232e7739309cfbfa25e1`。
- commit / deploy：包含本记录的 Group 0 commit 是 Git 机械事实源，不在自身内容中嵌入自引用 hash；本 Group 无业务 runtime、migration 或生产部署。
- 七原子：Input=总报告/Prompt/两份设计；Authority=AGENTS/L0/L1；Execution=pytest + CI；Evidence=marker map/hash/Git index；Recovery=external snapshot 与 delta 规则；Consumption=所有 Group runbook/CI；Acceptance=8 + 40 + 170 tests、ruff、diff check。
- 残余风险：业务 leaf 与极端行为仍按 owner Group 保持 open；这不是 Group 0 未闭环，也不能被误读成系统能力已闭环。

#### EVID-G1-001：P0-F1 governed public HTTP egress

- `leaf_ids`：`P0-F1`；同根范围包含 Agent `web_fetch`/advanced fetch、Personal KB URL import、`upload_image(url=...)` 的远端 URL 转交；未把固定 provider API、显式 Custom API connector 或受权内网连接器偷换成“任意公网 fetch”。
- owner Group / 依赖 Group：Group 1 / Group 0。
- 当前状态：`in_progress`；当前 checkout 的实现、fault matrix、相关回归与 backend 仓级 suite 已绿，但 commit 后三服务部署与 production canary 尚未执行，故不得标 `closed`。
- 证据 owner / 更新时间：主 Agent / 2026-07-15。
- 冻结事实：开工 HEAD `770a64189eecb291655e727cb04ffb5fd5cd27d1`；Group 0 之外仍有共享脏工作树。本家族只拥有 `backend/app/services/governed_egress.py`、`backend/app/services/agent_tool_domains/web_mcp.py`、`backend/app/services/agent_tool_domains/image_upload.py`、`backend/app/services/personal_knowledge_service.py`、`backend/tests/services/test_governed_egress.py`、`backend/tests/services/test_web_mcp_resilience.py`、`backend/tests/services/test_web_mcp_conversion.py` 与本文证据 hunk；没有接管其它 dirty path。
- `@docs` 当前快照：Group 1 的 10 份 must-read 文档均在开工 checkout 存在并记录 SHA-256；P0 直接裁决消费 `@docs/runtime-model-agency-constraint-audit-2026-07-13.md` 的 hard-constraint allowlist / Model Agency、`@docs/ccplus-governance-layer-architecture-2026-06-28.md` 的 L0 call-time boundary，以及本文 §9/§12 的 P0-F1 逐跳验收。完整 hash 清单由本轮 `shasum -a 256` 输出保留，后续 Group 1 leaf 继续按各自路由读取，不能以本证据代替 Knowledge/RLS/Budget 全文裁决。
- 当前 live entry：`web_mcp._web_fetch` 是 Hive 本机直取；`_advanced_web_fetch`、AnySearch/Tavily/Exa/Firecrawl/XCrawl 在把 URL 交给远端 extractor 前重新执行相同 public-target gate；`PersonalKnowledgeService.ingest_url` 不再自行 `follow_redirects=True`；`_upload_image(url=...)` 不再把私网/metadata URL转交 ImageKit。
- 权威事实源：URL parser、`ipaddress` 网络属性、resolver 的全部 A/AAAA、pinned socket peer、redirect Location/origin、单调 redirect 计数、wall-clock timeout 与 wire/decoded byte 计数。平台没有检查页面关键词、意图、正确性或内容意义。
- Red 1：`pytest tests/services/test_governed_egress.py -q` → collection error `ModuleNotFoundError: app.services.governed_egress`，证明 governed transport 缺失。
- Red 2：建立网络事实层测试后同命令 → `2 failed, 26 passed`；私网 `web_fetch` 仍返回 `provider_error`，AnySearch 仍收到 `127.0.0.1`。
- Red 3：扩展 Personal KB / ImageKit seam 后同命令 → `2 failed, 28 passed`；Personal KB 仍构造直接 HTTP client，ImageKit 仍收到私网 URL。
- Red 4：增加端口与 resolver fail-closed 后同命令 → `2 failed, 31 passed`；port 0 与 unexpected resolver error 尚未 fail-closed。增加 durable typed exception 文本后先得到 `17 failed, 16 passed`，证明原 exception string 未携机械 error code。
- 实现：新增严格 `http/https` URL normalization；拒绝 userinfo、控制字符、反斜杠、非法/零端口、single-label/混淆 IP、IPv6 zone、mapped/6to4/Teredo/NAT64 表示；所有 A/AAAA 必须全为公网。`PinnedPublicNetworkBackend` 只向验证 IP 建连并核对实际 peer，`trust_env=False` 禁止未经治理代理；redirect 逐跳重新解析/解析 DNS/注册 pins，HTTPS→HTTP 拒绝、跨 origin 清除 Authorization/Cookie/Proxy-Authorization；响应以流式 wire/decoded ceilings 和总 wall-clock timeout 约束。
- Model Agency：上限只约束网络资源和未授权 ingress；超过上限返回 typed infrastructure failure，不生成部分摘要、不机械裁剪后冒充完整页面、不判断页面语义。合法响应 bytes 原样交给既有 document conversion/模型消费；`max_chars` 仍只在模型显式请求时使用。
- migration / backfill / cleanup / rollback：无 schema/data migration。旧 `trigger_daemon._is_private_url` 暂未删除，因为其 poll path 仍独立消费；后续统一时必须保持 trigger 行为测试。代码 rollback 是回退本独立 commit；无不可逆数据动作。
- Green（定向）：`pytest tests/services/test_governed_egress.py -q` → `33 passed`；`pytest tests/services/test_governed_egress.py tests/services/test_web_mcp_resilience.py tests/services/test_web_mcp_conversion.py -q` 的上一个稳定点为 `78 passed`；Personal KB/API/Web 合并回归为 `107 passed`；Model Agency + tool definition 为 `46 passed`；scoped `ruff check` → `All checks passed!`。
- Green（仓级，最终提交前复跑）：`cd backend && source .venv/bin/activate && pytest tests -q` → `6987 passed, 2 skipped in 227.99s`，exit `0`；该结果已经覆盖 typed exception code 与本记录所述最终代码状态。
- fault/security：覆盖 metadata、IPv4/IPv6 private/loopback/link-local/unspecified、mapped/zone/十进制/八进制/十六进制混淆、多 DNS 答案含一个私网、同 host redirect 后 DNS rebinding、302→metadata、HTTPS downgrade、redirect loop、跨 origin credential、compression bomb、总超时、socket peer 与 pin 不一致；测试未访问真实 metadata/localhost/内网。
- 本机 live probe：尝试 `fetch_public_http('https://example.com')` 时，本机受控 DNS 返回保留的 `198.18.0.27`，validator 按设计 fail-closed；这证明 proxy/fake-IP 不会静默绕过，但不是公网成功 canary，也不能冒充 production evidence。
- Evidence / Recovery / Consumption：deny 经 `render_tool_error(error_class=network_target_denied)` 进入既有 ToolResult/span/transcript；Personal KB queued job 的 exception string 现在携带 code；timeout/too-large/redirect deny 保留不同 typed code，可由模型解释并换源/重试。成功内容继续由 Web conversion、PKB ingestion 或 ImageKit consumer 消费。
- commit / deploy / production canary：包含本记录与 7 个 owned code/test path 的独立 P0 commit 是 Git 机械事实源，本文不内嵌自身 commit 的自引用 hash；尚未部署，Railway 三服务 freshness、生产 DNS/redirect/metadata deny 与 public allow canary 均 open。
- 七原子：Input=Agent/user URL；Authority=public-target L0 policy；Execution=pinned transport/remote target gate；Evidence=typed code + tool/job receipts；Recovery=retry/换源且无 partial semantic fallback；Consumption=Web/PKB/ImageKit live path；Acceptance=本地已绿、production 未验。因此 canonical 行保持 `in_progress`，不是 `closed`。
- 残余边界：显式 Custom API、MCP、HTTP Hook 和企业内网连接器拥有不同的管理员配置/approval/network scope，不能被本 P0 public-fetch policy 粗暴删除；它们在 Group 1 的 authority/B-01 与后续 governance recheck 中必须证明 allowlist/pinning/credential/receipt，而不是默认继承“public fetch 已安全”。

#### EVID-G1-002：P0-F2 migration 与 RLS catalog fail-closed

- `leaf_ids`：`P0-F2`；只负责 deployment schema truth、migration owner/runtime separation 与启动前 catalog readiness，不把业务数据语义、一般 runtime availability 或模型判断扩张成 schema hard gate。
- owner Group / 依赖 Group：Group 1 / Group 0；production 已执行但未入 Git 的 `memory_context_warning_0714` 及其 warning consumer 是本项不可跳过的部署前置，不改变 `SES-ITEM-001` 的 Group 2 owner 归属。
- 当前状态：`in_progress`；本地 Red→Green、隔离 PostgreSQL fault/rollback/re-upgrade、production read-only preflight 和 deployed-source reconciliation 已完成；P0-F2 独立 commit、三服务部署和 production startup canary 尚未完成，故不得标 `closed`。
- 证据 owner / 更新时间：主 Agent / 2026-07-15。
- `@docs` 裁决：消费 `@docs/session-rls-preflight-review-2026-07-09.md`、`@docs/rls-enforcement-migration-plan.md`、`@docs/ccplus-governance-layer-architecture-2026-06-28.md` 与本文 §9/§12；hard invariant 是 Alembic head、table/column catalog、RLS ENABLE/FORCE、policy presence、schema-owner/runtime URL separation，事实源分别是 Git migration graph、`alembic_version`、`pg_class`、`pg_policy`、`pg_attribute` 和 server-side deployment env。
- 历史 refute-first：`069ff5e88` 在 2026-07-13 15:54 +08:00 曾加入 fail-closed，`42f6b6081` 在 20 分钟后整体 revert。Railway 最近 40 条 backend deployment 中，首个相关部署是 08:25Z 的 `42f6b6081`，没有 `069ff5e88` 部署记录；因此“旧修复已经因 production legacy data 失败”没有证据，不能继续当事实。
- 实际漂移根因：production `alembic_version=memory_context_warning_0714`，而开工时 `git ls-files --error-unmatch backend/alembic/versions/memory_context_warning_0714.py` exit `1`。Railway 最新源码消息绑定 tree `fcd7a0d55424`，其中 migration blob 为 `6287725dca6b7992e459af08195d2b24f81bfc92`；工作树文件 hash-object 完全一致。`f7902ab7b` 已把该 immutable revision、degraded-warning status、backend typed item、frontend renderer/retry 与回滚测试一起纳入 Git，禁止 clean deploy 再以 unknown revision 依赖 fail-open 存活。
- Red（启动/事实源）：`pytest tests/deploy/test_schema_startup_gate.py tests/scripts/test_verify_schema_readiness.py -q` → `14 failed`：production head 未被 Git 跟踪；Alembic/grant/readiness 非零仍到 uvicorn；API role 无 read-only gate；readiness 模块不存在。
- Red（真实 PG 对抗）：首轮 `pytest tests/integration/test_schema_readiness.py -q` → `1 failed`，暴露 `RLS_FORCED_TENANT_TABLES` 含已退役兼容表 `identities`。若把兼容表“必须存在”写成 hard gate，fresh DB 会永久拒绝启动；实现据此改为 live `Base.metadata` 表必须存在、兼容表 absent 可接受但 present 必须通过 RLS catalog。
- 实现：新增 `app.scripts.verify_schema_readiness`。它以 Alembic `ScriptDirectory` 计算 expected heads，以 owner connection 一次读取 actual heads 与 catalog；live model table 必须存在，所有存在的 expected RLS table 必须 ENABLE+FORCE 且至少一条 policy，strict tenant table 必须有 `tenant_id NOT NULL`。输出仅含 typed issue code/object/retryable，不读取 row payload、不判断业务语义。
- 启动顺序：runtime role 走 `create_all/safety patch → alembic upgrade head(owner) → data migration(owner) → grant app_rls(owner) → readiness(owner) → uvicorn(runtime URL)`；任一 migration/grant/readiness 非零均 exit，不接流量。API role 不做 DDL/grant，但必须通过同一只读 readiness。旧 `RLS_BACKFILL_ON_DEPLOY` 后台 convenience writer 已删除，避免 audit 通过后仍有锁外 schema/data mutation。
- Green（启动/纯函数）：新 Red 集合 → `14 passed in 4.32s`；相关既有 startup/Alembic/tenant tests 与真实 PG 合并 → `44 passed in 9.33s`；`bash -n entrypoint.sh`、可用时 `shellcheck entrypoint.sh`、scoped `ruff check` 均 exit `0`。
- Green（真实 PG）：隔离数据库先 `upgrade head` 并 readiness green；注入 `ALTER TABLE runtime_tasks NO FORCE ROW LEVEL SECURITY` 后得到 `rls_not_forced`；恢复 FORCE 后 green；`downgrade session_permission_semantics_0713` 得到 `alembic_head_mismatch`；再次 `upgrade head` 后 green。`pytest tests/integration/test_schema_readiness.py -q` → `1 passed in 5.36s`。
- Green（仓级最终复跑）：`cd backend && source .venv/bin/activate && pytest tests -q` → `7002 passed, 2 skipped in 238.24s`，exit `0`；覆盖本项最终 entrypoint/readiness、已纳管 production revision 和共享脏工作树当前状态。
- deployed warning prerequisite：backend warning/migration 定向 `33 passed`；frontend warning reducer/renderer/chat runtime `3 files / 82 tests passed`；`npm run build` exit `0`，AgentDetail `290185/380000` bytes、gzip `82018/115000`，vendor `591449/620000`、gzip `186474/200000`。
- production read-only preflight：Railway tunnel/`psql` 显示 schema user `postgres`、PostgreSQL `18.3`、DB head `memory_context_warning_0714`；115 个带 `tenant_id` 表均 ENABLE+FORCE。按当前 `RLS_FORCED_TENANT_TABLES` 分 4 个 catalog chunk 检查 missing/disabled/unforced/no-policy 均 `0 rows`；按 `STRICT_TENANT_RLS_TABLES` 分 3 个 chunk 检查 missing tenant column/nullable 均 `0 rows`；strict NULL 动态查询 `0 rows`。
- production NULL 解释：全 tenant-column 扫描只见 `users=6`、`audit_logs=1844`（明确 operator-nullable），`skills=9`、`tools=165`（明确 platform-shared），以及 retired compatibility table `retired_trigger_focus_refs_0613=1`；它们不是 strict tenant leak，不能用错误 hard gate 阻断启动。
- 失败证据诚实性：一次本机 `railway run` owner URL 探针在 SQL 前因 TLS/connection lost 失败，只记录为 transport failure；后续 Railway DB tunnel 查询才是 production catalog 证据。没有把连接失败冒充 migration/data failure。
- migration / backfill / rollback：本项不新增 schema revision；只恢复 production 已执行 revision 的 Git truth。隔离库 downgrade/re-upgrade 已验；production migration/deploy 属外部状态变更，尚未执行。代码 rollback 是回退 P0-F2 独立 commit，但不得删掉已执行的历史 revision 文件；如 readiness 拒绝，typed issue 保留且容器可重启重验。
- Model Agency / 北极星：该 hard gate逐项命中 machine contracts、authority、execution isolation 与 evidence/recovery allowlist；不检查 Prompt、模型输出、任务意义或自然语言，不裁剪 context，也不把 catalog failure伪装成模型结论。API/runtime 只能在机械 schema 不可消费时 fail-closed，不能因此禁用无关模型能力。
- commit / deploy / production canary：deployed-source prerequisite commit=`f7902ab7b`；包含本记录的 P0-F2 独立 commit 尚待创建并作为 Git 事实源，不在自身内容中嵌入自引用 hash。三服务未部署；production fail/allow startup、health、runtime `app_rls` 跨 tenant 行为与 rollback drill 均 open。
- 七原子：Input=container env + migration graph；Authority=schema owner vs runtime role；Execution=single entrypoint gate；Evidence=typed JSON/exit/catalog/Git blob；Recovery=restart/rollback/re-upgrade；Consumption=runtime/API 只在 ready 后启动；Acceptance=本地与 production read-only 已绿、deploy/canary 未验，因此 canonical 仍为 `in_progress`。

## 13. Missing、Coverage Gap 与完成口径

### 13.1 已知缺失，不计入 103

Missing 不进入 103 个 breakpoint 分母，但进入产品总目标；每项仍必须有唯一施工 Group、独立证据和明确完成裁决。

<!-- missing-owner-map-start -->
| Owner Group | Missing ID | 当前状态 | 缺失能力 | 证据前缀 |
|---:|---|---|---|---|
| 7 | `MISS-XCHANNEL-A2A-001` | `missing` | 同一 root task 的完整多渠道 route/result/delivery 产品合同 | `EVID-G7-*` |
| 8 | `MISS-EK-001` | `missing` | Enterprise Knowledge 完整 organization authority/retention/legal hold | `EVID-G8-*` |
| 8 | `MISS-RETENTION-001` | `missing` | 跨 Memory/Knowledge/Artifact/Audit 的 retention/deletion/export/legal hold | `EVID-G8-*` |
| 9 | `MISS-AIASSET-001` | `missing` | AI Asset 未覆盖类型 | `EVID-G9-*` |
| 10 | `MISS-EVAL-001` | `missing` | 真实行为级 self-evolution eval | `EVID-G10-*` |
<!-- missing-owner-map-end -->

Missing 开工后按 §12.4 写证据；只有实现、迁移/回填、真实 consumer、故障恢复和验收全部成立才可从 `missing` 改为 `closed`。如果产品明确排除，必须记录北极星裁决、authority 和替代路径，不能从表中直接删除。

### 13.2 本轮未证实

- 真实 100 个付费 child 同秒 completion 的容量曲线；
- 2k/10k/50k Agent/trigger definition 的真实 DB/queue benchmark；
- 400 个真实 Skill、200 个真实 MCP server、百万 Memory fixture；
- 钉钉/飞书/Slack/Web credential、rate limit、auth revoke、duplicate/ack-loss fault injection；
- current closed-source CC binary 与 Hive 的同模型 paired replay；
- 当前 dirty worktree 的全量 frontend vitest 与三服务生产验收；backend 全量 suite 和 frontend production build 已有 `EVID-G1-001/002` 当前证据；
- inherited P2/P3 的逐 leaf 当前源码重认证。

### 13.3 四层完成口径

| 层级 | 完成条件 | 不代表什么 |
|---|---|---|
| 单 leaf/家族闭环 | 七原子、Red→Green、migration/backfill、fault、observability、rollback、消费、发布全过 | 不代表 103 清零 |
| 程序账本完成 | 冻结快照重认证，open breakpoint=0，delta 全有证据 | 不代表 Missing 已建设 |
| Goal 1 / North Star 完成 | Goal 1 断点 + `MISS-EVAL-001` 闭环，真实行为对 CC/Hermes 非劣 | 不代表 Goal 2 完成 |
| 产品总目标完成 | Goal 1 + Goal 2 Missing + 跨渠道/企业治理产品闭环 | 不能由 103/103 或单个 eval 代替 |

P0/P1 安全与正确性修复永远不等待 103/103、UI/P3 或 `MISS-EVAL-001`；one-pass 约束的是每个已经开工的完整 leaf/同根家族，不是把所有 leaf 绑成一次部署。

## 14. 置信度

| 范围 | 置信度 | 说明 |
|---|---:|---|
| 四平面纠偏 | 高 | 当前数据模型、worker、root task 与 channel 路径直证 |
| 单根 Session 100-way 静态链 | 高 | result→outbox→mailbox→parent 与 Workflow/Team 已追全 |
| fleet fairness/trigger 静态链 | 高 | claim SQL 与 trigger `.all()` 当前源码直证 |
| Session truth 六 seam | 高 | backend persistence/projection/publish 与 frontend consumer 直证 |
| Context/Capability 八 seam | 高 | 两份报告与当前关键函数复核一致 |
| 103 分母 | 中高 | 94 旧账含 inherited-recheck；新增 9 个已逐 seam 去重，仍绑定 dirty snapshot |
| 真实容量曲线 | 未验证 | 未实跑 100 paid child、10k production fleet、真实 channel storm |
| CCPlus 行为非劣 | 中 | 源码对照成立，paired replay 未执行 |

最终工程判断：Hive 的差距不是“少一个更大的上限”，而是缺少把 fleet、root execution、context resource、Session truth 与 channel delivery 正交连接起来的统一机械合同。修复后，平台可以硬守 authority/effect/physical facts，同时把内部容量压力变成 durable queue、manifest、page、checkpoint 和模型可见恢复；这才是 CC 语义底座 + Codex 工程增量 + Hive Native 超越，而不是用控制面把 Agent 卡死。

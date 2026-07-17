# 本轮 P0 止血执行入口：Session 呈现、Memory 不爆、A2A 三层（2026-07-17）

> 状态：本轮施工的**唯一执行入口与滚动证据页**。它不取代 `agent-native-unified-atomic-review-2026-07-14.md`（下称 AA 总账）的 Group 定义、103 leaf 与证据回填合同；它只做三件事——**(A) 把执行序从"依赖分层序"重排为"用户痛点垂直切片序"**，**(B) 修正 A2A 的层级定义（两层 → 三层）**，**(C) 记录三个切片的本地实现、提交与生产验收状态**。
>
> 冲突裁决：源码/运行事实 → L0（北极星/Model Agency）→ 三份源头设计合同（本文 §1）→ AA 总账 → 历史报告。AA 总账 §9 的依赖表仍是"正确性依赖"，本文 §4 是"施工先后"，二者不矛盾：依赖表说"谁不能先于谁闭环"，本文说"本轮先交付哪几个用户可见的垂直切片"。

## 0. 为什么需要这份文档：偏移诊断

Session V2 重构（AA 总账 Group 1–4，229 文件 / ~4 万行）**方向正确、施工序错误**。

- **已部署的是后端事实底座，不是永不回归的完成声明**：Group 1（安全/principal/authority）、Group 2（Session 机械事实语言）、Group 3（root admission/coverage）、Group 4（durable result/fan-in）已经形成并上生产；但本轮 live-entry/path 复核重新打开了 Group 1/2/3 的五个协作 leaf，必须按当前源码修复，不能用旧 deployment 证据掩盖。
- **用户真正痛的垂直切片跨越多个 Group**：Session 前端呈现主要在 Group 9；Memory 披露在 Group 6；当前 Peer A2A 故障落在 Group 1/2/3 的 authority、consumer 与 terminal/Team seam。Group 7 继续拥有跨渠道 route/result/delivery Missing，但不是当前单渠道 A2A 瘫痪的兜底桶。
- **两个平面因此断裂**：写路径换成了 v2 事实语言，但把 v2 接到用户眼睛上的读路径/前端收口（Group 9）没做；先上了后端底座却没有产品验收门兜底，断裂直接暴露给用户。

这就是 owner 观察到的"用户需要的东西和正在修的东西处于两个不同平面"的机制性根因：**水平分层序（先把所有底层做完再往上）取代了垂直切片（每次交付一个用户可见的端到端闭环）**，违反北极星第 1 条（Goal 1 产品先建先判）、第 7 条（产品消费是操作面）与"垂直切片/走通骨架优先"。

铁证：Group 2 表格自称"frontend canonical consumer 闭环"，但生产首屏 `GET /sessions/{id}/transcript` 默认仍走 v1 序列化，返回的 v2 事件名前端归约不认 → 整批丢弃 → 白屏。原因是"首屏 REST canonical 化 + V1 writer 退出"是 Group 9 的活，没做。

## 1. 三个大问题 × 源头文档 × 当前状态

本轮的用户痛点来自三份**分别讨论过的源头设计合同**，它们本身没有偏，偏的是被揉进 AA 总账施工序时的排序：

| 大问题 | 源头文档 | AA 总账 Group | 当前状态 | 用户痛点 |
|---|---|---|---|---|
| ① 模型自主性 / 别把 native 改挂 | `runtime-model-agency-constraint-audit-2026-07-13.md` | Group 1 | 已闭环（C-01~C-20 上生产，删掉了 post-hoc final-answer rewriter） | 老根因已修；本轮作为**回归门**贯穿所有切片 |
| ② 上下文组装 / Memory 不爆 | `unified-context-assembly-and-progressive-disclosure-2026-07-14.md` | Group 6 | **P0 Memory 自动披露切片本地 Green；完整 Group 6 仍未完成，待生产验收** | **Memory 爆炸** |
| ③ Session V2 CC/Codex 对齐 | `session-v2-cc-codex-alignment-contract-2026-07-14.md` | Group 1/2/3 当前回归 + Group 7/9 后续验收 | 既有 Session/A2A substrate 保留；本轮补 server read-only、三类 typed consumer、terminal root、Team model/hidden surface 与历史 backfill，当前 local Green、待生产验收 | Session 呈现、Desktop 交付、A2A 层级 |

## 2. A2A 的正确层级：三层，不是两层

源头文档 `session-v2-cc-codex-alignment-contract` §16 原本只写了"Sub-agent / A2A(peer employee) / Workflow"，**遗漏了中间的 agent team 层**。基于源码核实，正确是三层 agent 协作 + Workflow：

| 层 | 用户视角 | 代码入口 | 运行身份 | `SessionContext.source` | 治理 |
|---|---|---|---|---|---|
| **① sub-agent** | 主 agent 内部临时开一个匿名 worker 干一件事 | `spawn_subagent`（`app/agents/subagent.py:1460`）| 主 agent 内部匿名 worker | `"subagent"` | 轻量 |
| **② agent team** | 当前环境瞬间新开多个"临时对话"，仍用主 agent 这套系统 | `spawn_subagent(team_name+name)` → `spawn_agent_team_member_runtime`（`app/services/agent_team_runtime_service.py:588`，`command="spawn_subagent"`）| **同一个 lead agent**（member 挂 `lead_agent_id`），多个具名可寻址 child session（enterable，`runtime_task_type="team_member"`）| `"subagent"` | 建在 ① 之上 + team 容器 |
| **③ A2A 到 employee** | 委派给**另一位不同的数字员工** | `delegate_to_agent`（`app/tools/handlers/communication.py:318`）→ `orchestrator.delegate_async`（`app/agents/orchestrator.py:3287`）| **另一个 agent（不同 agent_id）** | `"agent"` | 完整 A2A：principal / depth / cycle / budget / `delegation_run` read-only |

关键区分（对齐 CC/FreeCode：`AgentTool.tsx:284` 靠 `name` 参数二选一 teammate vs sub-agent，in-process teammate 复用同一个 `runAgent()`/`query()` 用主 agent system）：

- **② 和 ③ 不是同一套代码**，只在最底层 `invoke_agent()` → `AgentKernel.handle()` 汇合（Hive 铁律：所有执行都过内核）。② 走 `spawn_subagent` 机制（同一 lead agent 的具名 teammate），③ 走 `orchestrator.delegate_async`（跨 agent，独立治理）。**"底层内核共用，编排层是两条独立的路"**。
- **术语**："agent team" 专指 lead agent 自己的具名 teammate 群（同一 agent）；跨到不同数字员工是 **A2A delegation**，不是"另一种 team"。
- 因此 A2A 修复必须拆成**三个独立验收对象**，禁止把 ② 和 ③ 一锅端。

## 3. 三个 P0 垂直切片

每个切片是一个**端到端闭环**（写路径 → 读路径 → 前端呈现一次打通），一次完成 Red→Green、migration/backfill、observability、recovery、真实消费与发布，并挂一个 **native 回归门**。

### P0-1 · Memory 不爆（落在 Group 6）

**症状**：Memory 越攒越多，动态注入把 prompt 挤爆，撞硬上限后 runtime 直接 error。

**历史根因（当前补丁前已核实）**：

- `invoker._resolve_memory_context()` 每轮把 resident 与召回正文作为 dynamic suffix 注入；explicit overlay resident plane 又把所有 active body 全量拼入，资源量直接传导到 prompt。
- `MemoryRetriever` 的 selector 过去读取全部候选正文；模型不可用或失败时再返回**全部候选正文**，所以正常路径和失败路径都可能爆。
- `MemoryAssembler` 过去执行 `del budget_chars`，已选正文不受表示预算约束；上游膨胀最终只能撞 `assemble_runtime_prompt()` 的硬错误闸。
- 这不是 Session V2 的根因，而是 Group 6 的 Memory disclosure 断点。

**当前已实装的 CC 对齐合同**（FreeCode `7dc15d6c8` 当前源码）：

1. `MEMORY.md` 式常驻索引，而不是常驻正文：最多 200 行、25,000 bytes；Hive 保留完整身份 profile，把 explicit overlay 改为 bounded ID/preview/load index。
2. selector 只读取候选的 name/description/load ref manifest，不读取所有候选正文；每轮最多选择 5 条。
3. 入选正文每条最多 4,096 UTF-8 bytes / 200 行；连同 section/ref 开销，每轮总量最多 20KiB。
4. 单 Session 自动披露累计最多 60KiB；账本以 durable turn identity 幂等、跨进程加锁，不复制语义正文。
5. 完整授权 evidence 始终可经 `search_memory` / `load_memory` 读取；selector/ledger/assembler 不可用时产生 typed degraded state，不把整个 Session 判失败，也不冻结仍有正常 authority 的无关 effect。
6. selector failure 只保留 candidate ID/hash/coverage receipt，绝不恢复“全部正文兜底”。

**当前状态**：P0 切片已完成 Red→Green、Memory/runtime 定向 `104 passed`、architecture `198 passed`、backend 全量 `7543 passed, 2 skipped`、frontend 对应 checkout `693 passed` 与 production build/bundle budget；当前 `HEAD b9852f37f` 已随 backend=`a64092a1-395b-48c2-9853-83ff9b45c2ae`、backend-api=`ab14d317-3c29-4b74-9d31-341e778f92b7`、frontend=`3ff852aa-e078-464c-80c7-7568b1272a2a` 同源发布。完整结果写入本文提交的 commit body 与 AA `EVID-G6-001`。当前仍不得写“生产闭环”，因为真实长 Session canary 与 production prompt-pressure/actual-token 指标尚未执行。

**验收门**：`unified-context-assembly` §0/§1.2/§10 与 §18.10；memory 数量增长时 resident/automatic bytes 有界；selector prompt 不含全量 body；selector failure 无 body；同 Session 多 turn 不超过 60KiB；预算耗尽后 conversation/search/load 可继续；无 `prompt too long` 整轮失败。
**native 回归门**：模型仍能通过 ref/search/load 读取全部授权 memory 证据，不因"不爆"而删除决定性尾部、冲突或 provenance；4KiB 自动 excerpt 是 recoverable preview，不是事实源替代。

### P0-2 · Session 能看见（落在 Group 9 前端收口）

**症状**：一进入 Session 就"重新连接中"；执行过程看不到、一直 Waiting；完整回答闪一下后消失/变 `[LLM Error]`；"加载更早消息"把可见内容挤没了。

**本轮修复前的根因**（`session-v2-alignment` §8.1/§8.4/§8.5 已核实断点 + 07-17 诊断）：
- 直播发 v1 事件名（`chunk`/`thinking`），落库存 v2 信封名（`assistant_text.delta` 等）；首屏 REST 默认 v1 序列化 → v2 delta 行前端归约不认 → 整批丢弃 → 白屏。
- `chatTransportRecovery.ts::latestTranscriptSequence` 取 max-seen cursor 而非 highest-contiguous，会永久跳过 sequence gap。
- `useSessionTransportController.ts` 初始即 `reconnecting`；live `done` 后 REST hydration 整数组替换刚显示的 live 消息。
- "加载更早"窗口按原始事件行数切（`agentDetailPolicy.ts` `TRANSCRIPT_INITIAL_WINDOW=25`），一轮几十条 delta 行把可见内容挤到 load-earlier 后。CC/Codex 无此物——**分页不该做可见性边界**。

**当前 checkout 已实装什么**（`session-v2-alignment` §8.5 闭环映射 + §17 UI 投影 + §18 同构）：
- 首屏 REST 走 `schema_version=2` canonical；前端只经 `SessionEventStore` 一次归约，不再交给 legacy message reducer 二次归类。
- highest-contiguous cursor + `session.ready` 握手分界（首次连接 ≠ 重连）；terminal final identity 冻结，hydration 不整数组替换 live。
- 删掉 load-earlier 的可见性边界；分页只做性能窗口，不做可见/隐藏决策。
- 前端固定 Codex 呈现序：**Thinking → Text → Tool（含 tool_search）→ 最终输出**；Desktop 克制交付（thinking 展示是产品决策，不塞原始 token）。

**验收门**：`session-v2-alignment` §8.5 那张"用户问题 → 永久修复 → 验收"表逐条对上；live / reconnect / replay / reload / resume 五路同构（§18）。
**native 回归门**：native 直播能看到的，刷新后必须还能看到（final regression 指标恒 0，false-provider-error 恒 0）。

**当前状态**：`06f340c4c` 已实装 newest-first canonical hydration、自动 backfill、live/backfill merge、typed retry 与 optimistic input identity；`7b6798933` 修复 legacy session-scoped event replay，上述代码已包含在 `b9852f37f` 三服务 production archive。仍待 live / reconnect / reload / resume browser canary，不能仅凭部署成功宣称生产闭环。

### P0-3 · A2A 三层各自跑通（当前回归落在 Group 1/2/3；跨渠道 Missing 留在 Group 7）

**症状**：大 A2A 委派（`delegate_to_agent` 给另一位数字员工）失败/卡住；父会话看不到对方执行（read-only 语义"没了"）。

**本轮修复前的当前根因（live-entry/path proof，而不是沿用已修历史根因）**：

1. `delegation_run` 只在 DTO/UI 声明 read-only，server mutation 仍能 start/steer/rename/delete/Team/Workflow/Plan，另一个数字员工的 task-scoped Session 仍可被接管。
2. backend 把 `task_type=delegation` 归到 `subagents`，frontend 也没有 `peer_a2a` typed section；右栏因此把 Peer A2A、内部 worker 与 terminal state 混成 Child Session/Workers/Working。
3. `SessionRunOutcome` 完成 `RuntimeTask` 后没有同事务关闭对应 `RuntimeRootItem`，provider 已失败/任务已结束时 root coverage 仍可长期显示 Working。
4. `AgentTeamMember.model_id` 已保存但没有进入 worker model loader，具名 teammate 实际仍可能使用 lead Agent primary model。
5. Team member implementation Session 使用 `listed_surface=chat`，进入普通 Session 列表，违背“父上下文内具名 teammate”的产品身份。

**当前 checkout 的实装与剩余验收**（按 §2 三层拆成三个独立验收对象）：

- ① Sub-agent：保持独立 spawn/return/progress，backend `subagents` 与 canonical `subagent_activity` 只承载轻量内部 worker；不制造普通用户可导航 Session。
- ② Agent Team：保持同一 lead Agent 的具名 teammate、roster 与回收合同；member model 在 spawn 前按 tenant + enabled + exact UUID/label/model 校验，resolved ID 进入 RuntimeTask 并由 worker 真正消费；member Session 改为 `listed_surface=parent`。
- ③ Peer A2A：保持跨 `agent_id` 的 governed delegation；`delegation_run` 由 server exact `session_kind` 强制 read-only，owner/manager 均不可 mutation，read transcript/workbench/export 仍可用；backend `peer_a2a`、canonical `peer_a2a_activity` 与 frontend A2A segment 消费独立 typed truth。
- 共同 terminal/recovery：`SessionRunOutcome`、`RuntimeTask`、`RuntimeRootItem` 同事务单调收敛；additive migration `collaboration_runtime_closure_0717` 回填历史 Team surface、exact task-bound Peer A2A Session、root terminal drift 与 collaboration ThreadItem，downgrade 不重新暴露或重开已修真相。

**验收门**：修正后的 §16 三层 + §8.5；三层各自有独立 golden 轨迹；cycle/depth/budget/principal 治理不变。
**native 回归门**：三层在改动前能 spawn/委派/回传的，改动后必须仍能，且治理（principal/cycle/budget）不被削弱。

**当前状态**：既有 `06f340c4c` / `2b3e05011` / `7b6798933` substrate 保持；本轮五个 regression seam 已完成代码、typed consumer、additive migration/backfill 与 Red→Green。协作 backend family=`382 passed`，backend full=`7567 passed, 2 skipped`，真实 PostgreSQL migration suite=`214 passed`，frontend typed consumer=`51+1 passed`，frontend full=`120 files / 709 tests`，production build/bundle budget 全绿。commit `b9852f37f` 已三服务同源部署，production migration head=`collaboration_runtime_closure_0717`，readiness 与 health 全绿；真实跨员工委派、authenticated read-only deny、Team model route、terminal/root reconciliation 与 browser canary 未执行，因此 Group 1/2/3 对应 leaf 为 `in_progress-deployed-pending-canary`，不得写成 production closed。

## 4. 排序与并行性

本轮不再回到 Group 5→10 的水平工程序。发布门按用户痛点垂直切片收口：

1. **P0-1 Memory**（生产硬 error，用户发一条消息就爆，最痛）。
2. **P0-2 Session**（体验断裂，能跑完但看不见）。
3. **P0-3 A2A**（影响跨员工委派这一条路径）。

代码可独立提交；本轮 backend 全量、frontend 全量/build、migration readiness 与三服务 exact-source deploy 已完成，剩余发布验收是三个用户路径各自的 authenticated/browser/production canary。任一切片本地闭环后不等待其它 103 leaf；任一切片未通过自己的七原子，也不得被另两个切片的 Green 或 deployment success 掩盖。

## 5. 暂缓项

AA 总账 Group 5（Fleet 公平/Trigger 扫描）、Group 10（Goal 1 行为门/总重认证）等**无对应当前用户痛点的纯工程深化**，本轮不占带宽，按 AA 依赖表在 P0 止血后各自 owner 推进。Group 8 的 Memory/Knowledge durable evidence、retention 与 P0-1 的"上下文披露"不同域：P0-1 只解"注入撑爆 prompt"，Group 8 解"T2/T3/retention/证据完整性"，二者不合并。

## 6. 与 AA 总账的关系

- AA 总账 §9 的依赖表、Group 定义、leaf、`EVID-*` 证据合同**全部保留有效**。
- 本文只在 AA 总账 §9 前插入"P0 止血序"引用本文，并把三类协作产品身份写成共同验收合同；它不改变 canonical owner。
- 本文三个 P0 切片闭环后，其证据仍按 AA 总账 §0.2 回填到对应 Group：P0-1→Group 6，P0-2→Group 9，P0-3 当前回归→Group 1/2/3；真正的跨渠道 route/result/delivery Missing→Group 7。不得另造第二套证据账本。

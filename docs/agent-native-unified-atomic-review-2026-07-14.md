# CCPlus 统一原子化复审：Fleet、单根 Session、Context 与 Session Truth

> 状态：当前工作账本、Group 0–10 施工入口与后续修复证据总报告；不是实现完成声明
>
> 原始审查冻结快照：`main@501db6555dae374e5fcf43a6fdcfe8a3dd89343e` + 2026-07-14 当时未提交工作树；后续施工不改写这个审查锚点，每条 `EVID-*` 另记自己的开工 HEAD、工作树与生产快照
>
> 修复账本滚动更新：2026-07-15；Group 0 已用 `EVID-G0-005` 把 11 个上下文包总索引、逐 Group 详细路由和证据回流做成机器门并保持关闭；Group 1 的同源三服务部署已经成功，`P0-F1/P0-F2/KB-EXTRACT-001` 以 `EVID-G1-001/002/006` 完成 production canary 并关闭，`KB-AUTH-001` 已通过 `EVID-G1-007` 的本地与 detached clean-snapshot 全量验收但尚未 commit/deploy/canary，`E-1/P1-004/P1-F4` 仍因 live authority/recovery canary 与历史数据处置保持 open；不得把 3 个 leaf 的关闭或 1 个本地 Green 冒充 Group 1 或 103 项完成。
>
> 组合输入：`agent-native-atomic-review-2026-07-14.md`、`agent-native-extreme-boundary-atomic-review-2026-07-14.md`、`unified-context-assembly-and-progressive-disclosure-2026-07-14.md`、`session-v2-cc-codex-alignment-contract-2026-07-14.md`、修订后的 `reusable-agent-native-atomic-review-prompt.md`，以及当前 Hive / FreeCode / Codex 本地源码。
>
> 本地基线快照：FreeCode `7dc15d6c8fb0c40c7fcc02ce9b58204324252632`；claw-code Python/Rust `d229a9b022d4845d28a728677e6a6b7c22ec5a2e`；claude-code-org `a99de1bb3c0c301b83b784abbcdb7a3674b2cd45`；Codex `5c19155cbd93bfa099016e7487259f61669823ff`。四个对照仓库的 tracked source 均无 diff；各自仅有未跟踪的本地索引、审查文档或 assistant 配置，不作为源码证据。
>
> 原始统一审查阶段只修改审查与设计文档；进入施工后，业务代码、测试、只读生产预检和文档证据按 `EVID-*` 独立推进。当前工作树仍包含其它 session 的大量未提交业务改动；每条证据只拥有其显式 path manifest，未 reset、覆盖或归属其它改动。本文的数量和旧行号绑定原始快照，不是永久 KPI。

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

### 0.5 `AA → 上下文包 → 施工 → 证据` 闭环合同

后续把本文简称为 `AA`。`AA` 是导航、owner、状态和证据总账；被 `@` 的文档保存完整设计、历史取证或子系统施工细节。两者不能互相替代。每次开工固定按下列顺序执行：

1. **从 AA 定位**：读取当前 Group 的 Owner leaf/Missing、依赖 Group、`§12.1` owner 行、`§12.2` canonical 行和 `§12.3` 当前证据状态；没有稳定 leaf ID 不得开工。
2. **继承全局上下文**：每个 Group 自动继承 `§0.1` 的 L0/L1 权威，不因本 Group 未重复列出而失效；尤其不得跳过两份关键 Context/Session 合同中该 Group 的 owner/consumer 章节。
3. **读取 Group 上下文包**：先读 `@原始断点证据` 恢复“为什么是断点”，再按顺序读 `@必须先读` 确认“应该怎么做”；`@按需读取` 只在 leaf 触及相应子域时展开。历史报告只提供输入和反例，不能继承其中的完成状态或旧行号。
4. **重验当前事实**：记录文档版本/章节、当前 HEAD/worktree/hash，并用 codebase graph、当前源码和运行事实重建 live entry、authority、effect、evidence、recovery 与 consumer。当前事实与文档冲突时，先把冲突写入证据，再按 `§0.1` 裁决并修正文档。
5. **按完整 leaf 施工**：先 Red，再完成实现、migration/backfill、observability、failure/recovery、真实 consumer/UI、rollback、独立 commit 和适用的 production gate；禁止把设计文档中的长方案复制成无消费代码。
6. **把证据收回 AA**：先在 `§12.4` 创建或更新稳定 `EVID-G<group>-<序号>`，再把同一 ID 回填 `§12.2` canonical leaf/Missing 和 `§12.3` Group 索引；Group 状态、commit/deploy/canary 与实际证据必须同步。

`@` 路由必须使用仓库相对路径和稳定章节标题，不使用会随编辑漂移的行号。相关内容过长时留在被 `@` 的设计/运行文档，AA 只保留：裁决、读取目的、不可丢失合同、owner、退出门和可验证证据 ref。若一个外部方案没有被任何 Group `@`、没有 owner、或没有证据回填位置，它不属于可执行修复方案。

每条 `EVID-*` 必须包含以下 Context Read Receipt；这是证明“读过正确上下文并按正确裁决施工”的机械记录，不是形式化打勾：

```yaml
context_read_receipt:
  aa_entry: "§9 Group <n> + §12.1/§12.2 owner rows"
  leaf_ids: ["<canonical leaf id>"]
  documents:
    - ref: "@docs/<file>.md §<stable heading>"
      role: "authority | design | original_evidence | migration | acceptance"
      decision_consumed: "<本次实现实际采用的合同>"
  source_baselines:
    hive_head: "<commit>"
    freecode_head: "<commit or not-applicable with reason>"
    codex_head: "<commit or not-applicable with reason>"
  conflicts_or_deltas: ["<none or explicit delta>"]
  evidence_sink: "EVID-G<group>-<序号>"
```

“读过”只有在 `decision_consumed` 能对应到实现、测试或明确的 refute 证据时才成立。不得把整份文档名堆进 receipt，却不说明本 leaf 消费了什么。

### 0.6 Group 上下文包总索引

本索引解决“打开 AA 后下一步去哪里”的问题；`§9` 对应 Group 的 `@原始断点证据`、`@必须先读`、`@按需读取`/专项读取清单才是该 Group 的**完整文档集合**，本节只给最快的主合同入口，不复制长清单形成第二事实源。执行者必须先从本节跳到主合同，再回到 `§9 Group n` 展开完整上下文包，最后把实际消费的章节和裁决写入 `Context Read Receipt`。

文档角色固定如下：

- `primary`：该 Group 最先打开的设计/运行主合同，用于找到目标状态和具体施工语义；它不替代 L0/L1。
- `detail`：该 Group 的完整 `@` 路由，依次恢复原始证据、强制合同和触及子域时的专项文档。
- `purpose`：从该上下文包必须带回实现与测试的核心决策；若实际 leaf 不消费该决策，必须在 receipt 中解释差异。
- `sink`：完成 Red、实现、迁移/回填、验证、发布和 rollback 后唯一回填位置；只在外部聊天或 commit 留证据不算完成。

<!-- group-context-package-map-start -->
- Group 0 | primary=@docs/reusable-agent-native-atomic-review-prompt.md; @docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md; @docs/session-v2-cc-codex-alignment-contract-2026-07-14.md | purpose=冻结北极星、审查口径、owner/路由/证据机器真相 | detail=§9 Group 0 @原始断点证据/@必须先读/@按需读取 | sink=§12.4 EVID-G0-*
- Group 1 | primary=@docs/agent-permission-governance-spec-2026-07-07.md; @docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md; @docs/personal-company-knowledge-tool-boundary-2026-07-10.md | purpose=收敛 authenticated principal、逐 effect 权限、安全事实源和 fail-closed 恢复 | detail=§9 Group 1 @原始断点证据/@必须先读/@按需读取 | sink=§12.4 EVID-G1-*
- Group 2 | primary=@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md; @docs/session-timeline-projection-contract-2026-07-04.md | purpose=建立唯一 Session Event/Item/Reducer、typed outcome 与 persist-before-publish | detail=§9 Group 2 @原始断点证据/@必须先读/@按需读取 | sink=§12.4 EVID-G2-*
- Group 3 | primary=@docs/ccplus-subagent-team-skill-mcp-hooks-parity-audit-2026-06-27.md; @docs/dynamic-workflow-harness-semantics-2026-06-24.md; @docs/runtime-budget-control-plane-plan-2026-07-03.md | purpose=统一 direct/team/workflow root admission、预算预留、approval 与单调终态 | detail=§9 Group 3 @原始断点证据/@必须先读/@按需读取 | sink=§12.4 EVID-G3-*
- Group 4 | primary=@docs/agent-team-session-workbench-root-cause-and-repair-plan-2026-07-02.md; @docs/chat-artifact-delivery-redesign-2026-06-20.md; @docs/a2a-session-substrate-design-2026-06-24.md | purpose=把 child result、mailbox、fan-in 与 parent integration 变成 durable ref/epoch | detail=§9 Group 4 @原始断点证据/@必须先读/@按需读取 | sink=§12.4 EVID-G4-*
- Group 5 | primary=@docs/trigger-cc-alignment.md; @docs/runtime-budget-control-plane-plan-2026-07-03.md; @docs/harness-engineering-audit-2026-06-11.md | purpose=建立 fleet/root 公平调度、Trigger 分页续扫和 control-plane reserve | detail=§9 Group 5 @原始断点证据/@必须先读/@按需读取 | sink=§12.4 EVID-G5-*
- Group 6 | primary=@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md; @docs/ccplus-session-runtime-token-compaction-alignment-2026-06-27.md; @docs/runtime-model-agency-constraint-audit-2026-07-13.md | purpose=实现 lossless progressive disclosure、soft pressure、compaction/output 恢复和模型语义主权 | detail=§9 Group 6 @原始断点证据/@必须先读/@按需读取 | sink=§12.4 EVID-G6-*
- Group 7 | primary=@docs/a2a-integrated-implementation-plan-2026-06-27.md; @docs/a2a-session-substrate-design-2026-06-24.md; @hive-connect:docs/bridge-protocol.zh-CN.md | purpose=分离逐 hop execution/result 与每 destination delivery，并绑定跨渠道 authority | detail=§9 Group 7 @原始断点证据/@必须先读/@按需读取 | sink=§12.4 EVID-G7-*
- Group 8 | primary=@docs/memory-clean-loop-refactor-plan-2026-06-17.md; @docs/memory-system-flow-map-2026-06-17.md; @docs/company-knowledge-base-spec-2026-07-07.md | purpose=闭环 T0→T2→T3→soul、durable intelligence job、Enterprise Knowledge 与 retention | detail=§9 Group 8 @原始断点证据/@必须先读/@按需读取 | sink=§12.4 EVID-G8-*
- Group 9 | primary=@docs/frontend-design-refinement-2026-07-03.md; @docs/session-timeline-projection-contract-2026-07-04.md; @docs/chat-artifact-delivery-redesign-2026-06-20.md | purpose=让 UI/Workspace/Artifact 消费同一 typed truth，完成 backfill 与旧路径退出 | detail=§9 Group 9 @原始断点证据/@必须先读/@按需读取 | sink=§12.4 EVID-G9-*
- Group 10 | primary=@docs/hive-sota-master-goal.md; @docs/eval-system-spec.md; @docs/self-evolution-sota-plan.md | purpose=逐 leaf refute-first 重认证、真实 paired replay、Goal 1 非劣与总账清零 | detail=§9 Group 10 @原始断点证据/@必须先读/@按需读取 | sink=§12.4 EVID-G10-*
<!-- group-context-package-map-end -->

如果施工中发现一份真正影响目标状态、迁移、运行或验收的文档未出现在当前 Group 的完整 `@` 路由中，必须先更新本 Group 路由、`Context Read Receipt` 和 architecture validator，再写业务代码；禁止把未登记文档当隐藏权威。反之，纯历史叙述、已被新合同覆盖的旧方案或无 live consumer 的文档只留在 `@原始断点证据`/archive，不得塞进 `@必须先读` 增加上下文噪声。

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
| 0 | 0（全局门） | 0 | closed：`EVID-G0-002/003/004/005`，Git truth、机器账本、11 个上下文包/总索引、跨仓快照与 clean-checkout harness 已闭环 |
| 1 | 16 | 0 | in_progress：`P0-F1/P0-F2/KB-EXTRACT-001` 已关闭；`E-1/P1-004/P1-F4` 已部署但 live authority/recovery gate 仍 open；其余 10 个 leaf 待施工 |
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

**AA 开工入口**：本文 `§0`（权威、写回、跨仓快照、ownership、上下文包）、`§8.1`（两份关键设计交叉表）、`§9`（Group 顺序）、`§11`（极端验收）、`§12`（owner/ledger/evidence）与 `§13`（Missing/完成口径）。

**@原始断点证据**：`@docs/agent-native-atomic-review-2026-07-14.md` §13–§22、`@docs/agent-native-atomic-review-501db655.md` §13–§22、`@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md` §5–§12。本 Group 只建立可重算账本和取证入口，不把三份报告的旧状态批量标成当前状态。

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

**证据回填**：更新 `§12.1` owner map、`§12.2` canonical ledger、`§12.3 EVID-G0-*` 索引和 `§12.4 EVID-G0-*` 记录；任何路由增删都同步 `@docs/README.md` 与 architecture ledger test。

**退出门**：§12 owner map 证明 103/103 唯一归属；5/5 Missing 唯一归属；CTX-A–F、S-01–S-12、SESSION-G1–G13 无遗漏；文档路径存在；CI 可复算；任何 Group 的证据能按 §0.2 回填。证据写入 `EVID-G0-*`。

### Group 1：真实安全、principal、authority 与 fail-open

**Owner leaf（16）**：`P0-F1`、`P0-F2`、`E-1`、`P1-004`、`P1-F4`、`KB-AUTH-001`、`KB-EXTRACT-001`、`KB-PROP-001`、`AUDIT-IMM-001`、`AUDIT-TENANT-001`、`F-PLAINTEXT`、`P2-F8`、`P2-F6`、`KB-CONTRACT-001`、`B-01`、`BUD-ROOT-001`。

**依赖 Group**：Group 0。P0/P1 家族自身闭环后立即发布，不等待 Group 2–10。

**AA 开工入口**：本文 `§12.1` 的 16 个 Group 1 owner 行、`§12.2` 对应 canonical 行、`§12.3 EVID-G1-*` 和已有 `§12.4 EVID-G1-001/002/003/004/005/006`；每次只开一个 leaf/同根安全家族，不把 Group 1 当成单个巨型改动。

**@原始断点证据**：

- `@docs/agent-native-atomic-review-2026-07-14.md` §5.1、§11、§13–§16、§19–§22：F-ID、安全 P0、authority/Knowledge、双事实源、治理冲突与原施工门。
- `@docs/agent-native-atomic-review-501db655.md` §13 [P0-001]/[P0-002]/[P1-003]/[P1-004]/[P1-005]/[P1-011]、§14–§16、§20–§22：SSRF、PKB contract、durable requester、A2A frame、RecoveryManifest、migration/RLS 的逐断点证据与 fault matrix。
- `@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md` §5.2、§6–§7、§9 Group 1、§10–§11：budget/root fail-open 与极端资源边界证据。

**@必须先读**：

- `@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`（§1、§7.3、§7.6、§8–§12、§13–§15、§18.4、§18.7–§18.8）
- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`（§9–§14、§16.2、§19、§21–§24）
- `@docs/runtime-model-agency-constraint-audit-2026-07-13.md`（§5–§11、§13–§15）
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

**证据回填**：为当前 leaf 创建/更新 `§12.4 EVID-G1-*`，在 `§12.2` 只更新被该证据覆盖的 canonical 行，并同步 `§12.3` 的 local/commit/deploy/canary 状态；P0/P1 独立发布证据不能等到整组完成后补写。

**退出门**：SSRF/redirect/DNS rebinding 与 sandbox egress 为零泄漏；schema/RLS fail-closed；唯一 requester/principal/delegation贯穿 inner effect、RecoveryManifest、PKB、audit 和 receipt；credential 不明文；budget service failure 只能缩小 work-amplification，不能伪造授权或冻结无关 direct answer。证据写入 `EVID-G1-*`。

### Group 2：Session 机械事实语言

**Owner leaf（14）**：`G-01A`、`A-01`、`A-04`、`B-02`、`B-03`、`G-01B`、`B-04`、`D-KB4`、`SES-ACCEPT-001`、`SES-ITEM-001`、`SES-PROJECTION-001`、`SES-PROSE-001`、`SES-TRANSPORT-001`、`SES-CONSUMER-001`。

**依赖 Group**：Group 0、Group 1。Session envelope 必须携带 Group 1 收敛后的 principal/authority，不得先建一个无可信身份的第二事实语言。

**AA 开工入口**：本文 `§3.5` Session truth 降级链、`§8.1` Session S/G owner map、`§12.1` 的 14 个 Group 2 owner 行、`§12.2` canonical 行与 `§12.3 EVID-G2-*`。

**@原始断点证据**：

- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md` §8：当前 Session 六个 seam 与旧完成声明失效原因，是本 Group 的直接问题定义。
- `@docs/agent-native-atomic-review-2026-07-14.md` §10、§13、§15–§18、§22：平台 prose、typed outcome、consumer 与历史 UI/Knowledge 断点。
- `@docs/agent-native-atomic-review-501db655.md` §13 [P1-007]/[P2-013]、§15–§18、§22：failure prose 与字符串反推 machine outcome 的原始证据。
- `@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md` §5.2、§6–§7、§10：pressure/terminal 与输出失败感知证据。

**@必须先读**：

- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`（全文，尤其 §9–§14、§18–§24、S-01–S-12、G1–G13）
- `@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`（§7.7–§7.8、§8–§12、§14.4、§14.6、§18）
- `@docs/runtime-model-agency-constraint-audit-2026-07-13.md`（§3–§6、§8.1、§8.6、§9–§11、§13–§15）
- `@docs/t0-append-only-session-ledger-redesign-2026-06-18.md`
- `@docs/session-timeline-projection-contract-2026-07-04.md`
- `@docs/session-rendering-streaming-cc-codex-gap-analysis-2026-07-03.md`
- `@docs/ccplus-session-tui-unified-expression-plan-2026-06-28.md`
- `@docs/ccplus-session-full-landfall-2026-07-09.md`

**@按需读取**：`@docs/session-rendering-overhaul-plan-2026-07-03.md`、`@docs/session-rendering-s6-completion-plan-2026-07-04.md`、`@docs/ccplus-session-ux-contract-2026-06-26.md`、`@docs/hook-goal-session-expression-plan-2026-07-09.md`、`@docs/ccplus-session-control-command-alignment-2026-06-27.md`。

**源码入口**：`ChatTranscriptEvent`/append path、web chat accept/stream/finalize、thread item projection、stream outbox/bus、frontend typed reducer/timeline/right rail。

**首个 Red**：用同一固定 Session fixture 分别走 live/history/reconnect/reload/resume，注入 interleaved commentary/tool/final、duplicate/out-of-order/gap/publish failure，证明当前 item identity、phase、author 或 snapshot 不同构。

**证据回填**：每个 Session seam 的 `EVID-G2-*` 必须附同一 fixture 的 live/history/reconnect/reload/resume 对照、event/item schema 与 reducer version；回填 `§12.2` 对应 14 行、`§12.3` 及 Session S/G owner map 的实际验收状态。

**退出门**：accepted input 同事务成为 canonical event；stable item/lifecycle/ordinal；typed denied/unavailable/approval/retryable；persist-before-publish；live/history/reconnect/reload/resume 同 reducer；平台不以 assistant prose 冒充模型；final 除 exact secret redaction 外 byte-faithful。必须通过 SESSION-G1/G3/G4/G5/G6/G7。证据写入 `EVID-G2-*`。

### Group 3：Root admission、预算与终态

**Owner leaf（7）**：`A2A-ADMISSION-001`、`SUBAGENT-ADMISSION-001`、`A2A-CYCLE-001`、`A2A-TERMINAL-001`、`TEAM-FANOUT-001`、`SUBAGENT-APPROVAL-001`、`ROOT-TREE-001`。

**依赖 Group**：Group 0–2。root ledger、approval 与 terminal 必须复用 Group 2 的 canonical event/item 和 Group 1 的 authority frame。

**AA 开工入口**：本文 `§2.2` 单 root 100-way、`§3.1–§3.2` fan-in/Team/Workflow 事实、`§5.3` soft admission、`§7.2` root plane、`§12.1–§12.3` Group 3 行。

**@原始断点证据**：`@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md` §5.3–§5.4、§6–§7、§9 Group 3、§10–§11 是 100-way admission/ghost/cycle/terminal 的主要来源；`@docs/agent-native-atomic-review-2026-07-14.md` §13、§15、§20–§22 与 `@docs/agent-native-atomic-review-501db655.md` §13 [P1-003]/[P1-004]/[P1-005]、§20–§22 提供 identity/recovery/approval 的相邻 seam，不能直接继承旧修复顺序。

**@必须先读**：

- `@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`（§7.4–§7.5、§8–§12、§14.5、§18）
- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`（§9–§12、§16、§18、§21–§24，尤其 G8/G9）
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

**证据回填**：`EVID-G3-*` 必须按 root fixture 记录 requested/admitted/deferred/expected/terminal ledger、authority/approval receipt、1/10/25/50/100 曲线及 crash-resume 结果，再同步 `§12.2/§12.3` 与 SESSION-G8/G9 owner 状态。

**退出门**：`requested = admitted + deferred/not_admitted`；reserve+durable enqueue commit 先于 expected；direct/team/workflow 进入同一 root item ledger；cycle/path durable；terminal monotonic CAS；late result 不覆盖 cancel/kill；approval intent durable。必须通过 SESSION-G9 与 1/10/25/50/100 mixed fanout。证据写入 `EVID-G3-*`。

### Group 4：Durable Result、mailbox 与 fan-in

**Owner leaf（6）**：`E-2`、`XCB-RESULT-001`、`CONC-FANIN-001`、`CONC-WAKE-002`、`WF-PARTIAL-001`、`CONC-MAILBOX-001`。

**依赖 Group**：Group 0–3。只有 admitted child、稳定 root/item identity 与 typed terminal 才能进入 result manifest、mailbox 和 integration epoch。

**AA 开工入口**：本文 `§2.2–§3.2`、`§7.2–§7.3`、`§8.1` 中 Context/Session collaboration 映射，以及 `§12.1–§12.3` Group 4 行。

**@原始断点证据**：`@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md` §5.3、§6–§7、§9 Group 4、§10–§11 提供 result/fan-in 爆炸证据；`@docs/agent-native-atomic-review-2026-07-14.md` §13 [E-2]、§15、§17、§20–§22 与 `@docs/agent-native-atomic-review-501db655.md` §15、§17、§20–§22 提供 parent wake、无消费与 recovery 原始证据。

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

**证据回填**：`EVID-G4-*` 必须保存 result object/hash/ref、outbox/mailbox sequence、lease/CAS、integration epoch、parent coverage、Prompt resident bytes 曲线和 SESSION-G8/G10 crash evidence，并同步 `§12.2/§12.3`。

**退出门**：完整 bytes 只在 durable result truth；outbox/mailbox 只携 ref；mailbox row 有 idempotency/sequence/claim/lease；root integration 以 epoch/page 幂等；partial/late/duplicate 可重算；100×1 MiB raw result 不线性进入 parent Prompt。必须通过 SESSION-G8/G10。证据写入 `EVID-G4-*`。

### Group 5：Fleet 公平与 Trigger 扫描

**Owner leaf（2）**：`FLEET-SCHED-001`、`FLEET-TRIGGER-001`。

**依赖 Group**：Group 0、Group 2、Group 3。需要 canonical task/pressure 状态与 root fairness key；不硬依赖 Group 4 的 result 实现，闭环后可独立发布。

**AA 开工入口**：本文 `§2.1` fleet 拓扑、`§3.3` 当前调度事实、`§5.2` 纠偏、`§7.1` fleet plane、`§11` 2k/10k/50k matrix 和 `§12.1–§12.3` Group 5 行。

**@原始断点证据**：本轮 fleet 纠偏以本文 `§2.1、§3.3、§4、§5.2、§12.2 [FLEET-SCHED-001/FLEET-TRIGGER-001]` 为直接证据；`@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md` §5、§9–§11 只保留极端测试输入，不能把旧的“100 个平台 Agent”解释带回实现。

**@必须先读**：

- `@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`（§1.2–§1.3、§6.3、§9.1、§10–§12、§18.7–§18.8）
- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`（§9–§12.1、§18、§21、§23 G5/G13、§24）
- `@docs/trigger-cc-alignment.md`
- `@docs/runtime-budget-control-plane-plan-2026-07-03.md`
- `@docs/runtime-budget-conformance-audit-2026-07-09.md`
- `@docs/harness-engineering-audit-2026-06-11.md`
- `@docs/eval-system-spec.md`

**@按需读取**：`@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md`、`@docs/round2-sota-benchmark-2026.md`。

**源码入口**：RuntimeTask claim SQL/worker capacity、trigger daemon/query/index/lease、tenant/root queue metrics、control-plane reserve。

**首个 Red**：一个 noisy root 先排入 100 child，同时加入 1,000 个其它 root 的交互任务与 cancel/approval/checkpoint；再对 2k/10k/50k trigger definitions 测单 tick、crash/restart 和 cursor，量化饥饿与 O(N) 扫描。

**证据回填**：`EVID-G5-*` 分别保存 scheduler fairness 和 trigger scan 两套 benchmark、query plan/index、cursor/checkpoint、crash-resume 与 control-plane reserve；回填 `§12.2` 两行及 `§12.3`，不得用其中一条 Green 关闭另一条。

**退出门**：2k/10k/50k definitions 不等于模型进程；trigger due-index/keyset/shard/cursor 可 crash-resume；scheduler 按 tenant+root 公平并保留 cancel/approval/checkpoint 槽；noisy root 不永久饿死其它 root。证据写入 `EVID-G5-*`。

### Group 6：Context、Capability、Compaction 与输出恢复

**Owner leaf（10）**：`A-03`、`XCB-CTX-001`、`XCB-CAP-001`、`XCB-MEM-001`、`XCB-OUT-001`、`XCB-LIM-001`、`XCB-MCP-001`、`XCB-OBS-001`、`WF-HARDLIMIT-001`、`BUD-BREAKER-001`。

**依赖 Group**：Group 0–2、Group 4。Context Resource Plane 复用 authority、typed pressure/session state 与 durable result refs；Group 3/5 的 admission/fairness 是极端验收输入，但不是删除非法 Prompt hard cap 的发布阻塞项。

**AA 开工入口**：本文 `§2.3`、`§3.4`、`§6`、`§7.4`、`§8–§8.1` 中 CTX-A–F、`§11` context stress matrix 与 `§12.1–§12.3` Group 6 行。

**@原始断点证据**：`@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md` §5.1–§5.2、§6–§7、§9 Group 5、§10–§11 是 400 Skill/200 MCP/Memory/output/limit 的直接来源；`@docs/agent-native-atomic-review-2026-07-14.md` §10、§13 [A-03]、§20–§22 与 `@docs/runtime-model-agency-constraint-audit-2026-07-13.md` §6 [C-06/C-13/C-16/C-18/C-19] 提供静默裁剪、输入饥饿和 compaction 反例。

**@必须先读**：

- `@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`（全文；CTX-A–F 全部强制）
- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`（§5–§12.5、§15、§18、§21–§25，尤其 G2/G11/G13）
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

**证据回填**：`EVID-G6-*` 必须逐项记录 CTX-A–F 的 `decision_consumed`、capacity ledger/page/hash/coverage、provider token preflight、resident Prompt 曲线、compaction/output continuation 和 SESSION-G2/G11/G13；同步 `§8.1` 决策 map、`§12.2/§12.3`。

**退出门**：resident kernel 对资源总量 O(1)；400 Skill/200 MCP/巨大 Memory/大量 definitions 全量可发现；directory/cursor/hash/coverage 完整；token-native preflight；internal threshold 只触发 pressure/defer；same-model output resume；compaction 不删证据；所有 soft/hard 状态模型可见。必须通过 CTX-A–F、SESSION-G2/G11/G13。证据写入 `EVID-G6-*`。

### Group 7：跨渠道 A2A 与 Delivery Plane

**Owner leaf（1）**：`CHANNEL-FAIRNESS-001`。**Owner Missing（1）**：`MISS-XCHANNEL-A2A-001`。

**依赖 Group**：Group 0–4。跨渠道必须建立在唯一 principal、canonical Session、root admission 与 durable result 上；与 Group 5 的 fleet fairness 分账验收，不互相冒充闭环。

**AA 开工入口**：本文 `§2.2`、`§5.2`、`§7.3/§7.6`、`§8.1` collaboration 映射、`§11` 跨渠道 matrix、`§12.1–§12.3` Group 7 行及 `§13.1 MISS-XCHANNEL-A2A-001`。

**@原始断点证据**：`@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md` §5.5、§6–§10 是多渠道 fault 场景与 Missing 的主要来源；`@docs/agent-native-atomic-review-2026-07-14.md` §13 [E-1/E-2]、§15–§17、§20–§22 与 `@docs/agent-native-atomic-review-501db655.md` §13 [P1-003]/[P1-004]、§15–§16、§20–§22 提供逐 hop identity/effect/result 的邻接证据。

**@必须先读**：

- `@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`（§7.4、§7.8、§8–§12、§14.5–§14.6、§18）
- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`（§9–§12.6、§16、§18–§19、§21–§24，尤其 G8）
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

**证据回填**：`EVID-G7-*` 必须附跨仓文档 snapshot/hash receipt、每 hop authority、execution/result/destination delivery ledger、真实或明确标注 sandbox 的 channel fault 证据；同步 `§12.2 [CHANNEL-FAIRNESS-001]`、`§13.1 [MISS-XCHANNEL-A2A-001]` 和 `§12.3`。

**退出门**：每 hop fresh-check principal/delegation/sensitivity/residency；Agent work/result 与每 destination delivery 正交；route/delivery ledger durable；duplicate/reorder/ack loss/auth revoke idempotent；final destination 显式；channel fairness 独立于 fleet fairness。必须通过 SESSION-G8 与真实/沙箱钉钉、飞书、Slack、Web fault matrix。证据写入 `EVID-G7-*`。

### Group 8：Memory、Knowledge、证据完整性与恢复

**Owner leaf（9）**：`C-BP1`、`P1-008`、`P1-017`、`C-BP2`、`C-BP3`、`C-BP4`、`C-BP5`、`C-BP6`、`F-OBS1`。**Owner Missing（2）**：`MISS-EK-001`、`MISS-RETENTION-001`。

**依赖 Group**：Group 0–2、Group 6。durable Memory/Knowledge intelligence 必须使用可信 authority、canonical evidence 与 Context Resource Plane；与 Group 7 共享 retention/delivery 验收时仍分别保留 owner。

**AA 开工入口**：本文 `§3.4`、`§6.3`、`§7.4`、`§8.1` 的 Memory/Knowledge/Session 映射、`§11` fault matrix、`§12.1–§12.3` Group 8 行及 `§13.1 MISS-EK-001/MISS-RETENTION-001`。

**@原始断点证据**：

- `@docs/agent-native-atomic-review-2026-07-14.md` §13 [F-MEM/C-BP1–C-BP6/F-OBS1/P1-017]、§15–§17、§20–§22：terminal/T2、T0/T3、capability consumer 与 evidence/recovery 原始问题。
- `@docs/agent-native-atomic-review-501db655.md` §13 [P1-006]/[P1-008]/[P2-014]/[P2-015]/[P2-016]/[P1-017]、§15–§22：同步 LLM、dependency freeze、hash verify、并发写、AI Asset 与 commit visibility 的逐项证据。
- `@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md` §5.1、§6–§11：Memory 大规模披露、durable recovery 和极端 fault 输入。

**@必须先读**：

- `@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`（§7.1、§7.6–§7.7、§8–§12、§14.2、§18.5–§18.8）
- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`（§9–§12.5、§12.7、§15、§18–§21、§23–§25）
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

**证据回填**：`EVID-G8-*` 必须保存 T0/T2/T3/soul source refs/hash/lock/outbox/job/retry/dead-letter/requeue、Knowledge ACL/retention/legal-hold 与跨资产 deletion/export ledger；同步 `§12.2` 九行、`§13.1` 两个 Missing 和 `§12.3`。

**退出门**：terminal commit 与 T2 intelligence 分离；T0→T2→T3→soul source refs 可验证；retry/dead-letter/admin requeue；无锁外语义写；Memory failure 只降级相关能力；Enterprise Knowledge organization authority 与 retention/legal hold 真实闭环；跨 Memory/Knowledge/Artifact/Audit deletion/export 可追踪。证据写入 `EVID-G8-*`。

### Group 9：产品消费、UI、迁移与旧路径退出

**Owner leaf（19）**：`G-02`、`H-404a`、`H-404b`、`G-03`–`G-10`、`G-11`–`G-18`。**Owner Missing（1）**：`MISS-AIASSET-001`。

**依赖 Group**：Group 0、Group 2、Group 4、Group 6–8。UI/Workspace/Artifact 只能消费已建立的 typed truth/ref；不能用前端 heuristic 提前模拟尚不存在的 backend contract。

**AA 开工入口**：本文 `§7.3–§7.6`、`§8.1` 中 Session UI/migration owner、`§11` UI/reconnect/backfill matrix、`§12.1–§12.3` Group 9 行及 `§13.1 MISS-AIASSET-001`。

**@原始断点证据**：`@docs/agent-native-atomic-review-2026-07-14.md` §9、§13 [G/H families]、§17–§18、§20–§22 与 `@docs/agent-native-atomic-review-501db655.md` §9、§13 [P2-009]/[P2-010]/[P2-016]、§17–§22 提供 UI/API/i18n/consumer/AI Asset 原始证据；`@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md` §8、§28 是当前 Session 完成状态的纠偏入口。

**@必须先读**：

- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`（§17–§20、§23–§28）
- `@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`（§7.8、§9–§12、§14.6、§17–§18）
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

**证据回填**：`EVID-G9-*` 必须保存 backend contract、同一 typed store/reducer 的 byte/structure snapshot、browser E2E、migration/backfill unknown 统计、legacy reader/writer 删除、i18n CI、Artifact/Workspace/AI Asset consumer 与 production acceptance；同步 `§12.2` 十九行、`§13.1` Missing 和 `§12.3`。

**退出门**：主时间线/right rail/Artifact/parent coverage/channel delivery 消费同一 typed store/ref；historical backfill 可复算且 unknown 不猜；V1 heuristic reader/writer 删除；Messages/channel test/i18n 真实闭环；AI Asset coverage 明确；SESSION-G5/G12 与浏览器 E2E、byte/structure snapshots、production acceptance 全过。证据写入 `EVID-G9-*`。

### Group 10：Goal 1 行为门、残余重认证与总账清零

**Owner leaf（19）**：`P2-018`、`A-05`–`A-08`、`E-3`–`E-7`、`C-BP8`–`C-BP12`、`B-05`–`B-07`、`D-KB3`。这些 inherited P3/P2 在施工前必须恢复具体语义与当前源码证据；不得以旧标题直接修。**Owner Missing（1）**：`MISS-EVAL-001`。

**依赖 Group**：Group 0–9 的对应行为证据。它是 Goal 1 非劣、residual leaf 重认证和程序总账清零门，不反向阻塞已经独立闭环的 Group 1 安全发布。

**AA 开工入口**：本文 `§1` 最终裁决、`§6` CC/Codex/Hive 合成、`§8.1` 全部决策/黄金轨迹 owner、`§12.1–§12.3` Group 10 行、`§13` Missing/完成口径和 `§14` 当前置信度。

**@原始断点证据**：除下方三份历史恢复依据外，还必须读取每个 residual ID 在 `@docs/agent-native-third-round-atomic-audit-2026-07-11.md`、`@docs/agent-native-ultimate-atomic-architecture-report-2026-07-10.md` 与 `@docs/final-atomic-review-2026-07-09.md` 中的最初语义；只要当前源码无法重现，就先标 `refuted/rewritten`，不得为了清零强造修复。

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

**证据回填**：每个 residual leaf 都要有独立或明确共享的 `EVID-G10-*` refute/Green 证据；`MISS-EVAL-001` 另记 paired replay corpus/version、同 model/provider/tool fixture、LLM referee、receipt、promotion/rollback。最后才更新 `§12.2/§12.3`、`§13.1/§13.3` 和 `§14`，禁止先改总数再补证据。

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
- P0 | P0-F1 | closed:EVID-G1-001 | governed egress commit `10b74360a` 已随 `1b822eb766` 三服务同源部署；public/redirect allow 与 metadata/downgrade typed deny production canary 已绿
- P1 | P0-F2 | closed:EVID-G1-002 | deployed-source reconciliation `f7902ab7b` 与 fail-closed readiness `5ad6ff3c6` 已随 `1b822eb766` 部署；schema/startup/runtime-role/cross-tenant production canary 已绿
- P1 | E-1 | in_progress-production-deployed:EVID-G1-003 | durable requester authority commit `3b3b281543bc` 已部署；creator≠requester live canary 与 1,499 条 legacy held drift 的 operator disposition 待完成
- P1 | P1-004 | in_progress-production-deployed:EVID-G1-004 | typed A2A authority frame、persisted receipt/restart drift hold 与 effect-boundary validation 已部署；sync/async/nested live canary 待执行
- P1 | P1-F4 | in_progress-production-dry-run:EVID-G1-005 | signed authority frame与所有恢复 consumer 已部署；fleet dry-run=`54 would_quarantine`，apply/disposition 与 direct/A2A/restart canary 待完成
- P1 | C-BP1 | inherited-current-evidence | terminal hook 同步 T2 LLM 阻塞完成
- P1 | P1-008 | inherited-current-evidence | Memory dependency failure 冻结无关 effect
- P1 | P1-017 | inherited-dirty-fix-unaccepted | transcript commit 与 T0 wake 可见性
- P1 | G-01A | inherited-split | 平台 failure prose 冒充 assistant/final author
- P1 | KB-AUTH-001 | in_progress-local-green:EVID-G1-007 | typed requester/session/purpose/delegation grant、sensitivity ceiling、PL4 reference-only 与可逆 legacy quarantine 已通过 detached clean-snapshot 全量；commit/deploy/live canary 待完成
- P1 | KB-EXTRACT-001 | closed:EVID-G1-006 | canonical sensitivity enum、全部写边界、PL3/PL4 extraction gate、可逆 backfill、DB constraint 与 production canary 已绿
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
| 0 | `EVID-G0-*` | 0 leaf / 0 Missing | `closed`：`EVID-G0-001/002/003/004/005`；文档 Git truth、owner/path/decision/scenario CI、11 个 Group 上下文包与机器总索引、59 份本地 `@docs` clean-checkout 快照、跨仓快照与 fake-provider/PG/Redis harness 基座成立 | 后续任何新增本地 `@docs` 必须先进入 Git 并同步上下文包索引；业务场景 Green 仍由 owner Group 负责 |
| 1 | `EVID-G1-*` | 16 leaf / 0 Missing | `in_progress`：六个独立 code commit；`P0-F1/P0-F2/KB-EXTRACT-001` production canary 已绿并关闭；`KB-AUTH-001` 已完成本地 typed authority、migration/real-PG、API/UI/tool 与 detached clean-snapshot 全量回归，仍等待独立 commit、三服务部署与 live canary；`E-1/P1-004/P1-F4` 已部署但 live authority/recovery gate 仍 open，P1-F4 最新 dry-run=`54 would_quarantine`；其余 9 leaf 未施工 | 先把 `EVID-G1-007` 的 production grant inventory/quarantine、owner/shared/A2A/PL4/revoke canary 与安全 rollback 闭合；并行完成 E-1 legacy disposition、P1-004 A2A canary、P1-F4 apply/恢复 canary，不能用本地 Green、shared deploy 或 dry-run 冒充 leaf closed |
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
- Context Read Receipt：AA 入口、实际读取的 `@文档`/章节/角色/消费裁决、Hive/FreeCode/Codex snapshot、冲突/delta、evidence sink：
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

#### EVID-G0-003：11 个 Group 上下文包与证据回流合同

- `leaf_ids` / `missing_ids`：无；本记录只增强 Group 0 导航与证据基础设施，不改变 103 + 5 分母，也不替任何业务 Group 宣称 Green。
- owner Group / 依赖 Group：Group 0 / 无。
- 当前状态：`closed`。
- 证据 owner / 更新时间：主 Agent / 2026-07-15。
- 冻结事实：开工 HEAD `3b3b281543bc5d63423d5a7fa3d7660b95ae3a48`；owned manifest 仅为本文与 `backend/tests/architecture/test_agent_native_repair_ledger.py`，其余 66 个 tracked dirty 与 4 个 untracked path 保持 unstaged/unowned（本记录两条 owned path 使当前总 tracked dirty 为 68）。
- Context Read Receipt：

```yaml
context_read_receipt:
  aa_entry: "§0 + §8.1 + §9 Group 0–10 + §12.1/§12.2/§12.3"
  leaf_ids: []
  documents:
    - ref: "@docs/agent-native-atomic-review-2026-07-14.md §13–§22"
      role: "original_evidence"
      decision_consumed: "恢复安全、authority、Memory、UI 与残余 leaf 的原始家族和施工门"
    - ref: "@docs/agent-native-atomic-review-501db655.md §13–§22"
      role: "original_evidence"
      decision_consumed: "为逐项 P0/P1/P2 断点提供精确证据入口和 fault matrix"
    - ref: "@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md §5–§12"
      role: "original_evidence"
      decision_consumed: "保留 100-way、400 Skill/200 MCP、跨渠道与 hard/soft 极端场景，但不继承旧 fleet 误读"
    - ref: "@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md §0–§20"
      role: "design"
      decision_consumed: "所有 Group 明确消费其 Context Resource Plane owner/consumer 章节与 CTX-A–F"
    - ref: "@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md §0–§29"
      role: "design"
      decision_consumed: "所有 Group 明确消费其 Session Event/Item/Reducer、S-01–S-12 与 G1–G13 章节"
  source_baselines:
    hive_head: "3b3b281543bc5d63423d5a7fa3d7660b95ae3a48"
    freecode_head: "7dc15d6c8fb0c40c7fcc02ce9b58204324252632"
    codex_head: "5c19155cbd93bfa099016e7487259f61669823ff"
  conflicts_or_deltas:
    - "旧 AA 只有裸文档列表；缺 AA 开工入口、原始证据角色、两份关键设计逐 Group 消费和证据回填合同"
  evidence_sink: "EVID-G0-003"
```

- Red：`git show HEAD:docs/agent-native-unified-atomic-review-2026-07-14.md | awk ...` → `HEAD route markers: aa=0, original_evidence=0, evidence_sink=0`；同时旧 architecture validator 对这些标记无断言，因此删掉任一上下文入口仍可能假绿。
- 实现：新增 `§0.5 AA → 上下文包 → 施工 → 证据` 六步合同和 Context Read Receipt；Group 0–10 各补 `AA 开工入口`、`@原始断点证据`、两份关键 Context/Session 合同的精确消费章节与 `证据回填`；长设计继续保留在被 `@` 文档，AA 只保存 owner、裁决、路由、退出门和证据 ref。
- 机器门：architecture test 现在要求 11/11 Group 同时含依赖、AA 入口、原始证据、Context/Session 路由、源码/执行入口、Red、证据回填、退出门与 `EVID-Gn-*`；另断言 Context Read Receipt 和 §12.4/§12.2/§12.3 回流合同存在。
- Green 1：当前 worktree marker 复算 → `aa=11, original_evidence=11, evidence_sink=11`。
- Green 2：`cd backend && source .venv/bin/activate && pytest tests/architecture/test_agent_native_repair_ledger.py -q` → `9 passed in 0.25s`。
- Green 3：`cd backend && source .venv/bin/activate && ruff check tests/architecture/test_agent_native_repair_ledger.py` → `All checks passed!`；owned paths `git diff --check` → exit `0`；validator SHA-256=`dcd48426580d9155dcca1c85f2fe126431368740488bc1995506735d08d151fc`。
- 扩展套件隔离：同一工作树 `pytest tests/architecture -q` → `1 failed, 171 passed`；唯一失败为当前未提交 P1-004 改动使 `app/tools/service.py` 的 high-risk root 超过 60 行，失败路径不在本记录两条 owned path。该结果不计作 Group 0 Green，也不允许被本文改动掩盖；由 `EVID-G1-004/P1-004` 在其完整 Red→Green 中收口。
- commit / deploy：Group 上下文包与 validator 的独立 commit=`b07de7811`；本证据同步不改 runtime/schema，无 production deploy。
- migration / rollback：纯文档导航与 architecture validator 变更；无 schema/data migration。rollback 为回退 `b07de7811`，不涉及生产状态。
- 七原子：Input=AA Group/leaf；Authority=L0/L1 + Group docs；Execution=Red→实现→验收；Evidence=Context Read Receipt + EVID；Recovery=稳定章节/跨仓 snapshot/delta；Consumption=11 个 Group runbook；Acceptance=11/11 marker 和 validator Green。
- 残余风险：文档路由只能防止漏读和证据失联，不能证明业务 leaf 已实现；各 Group 仍必须用当前源码与真实运行重验。后续任何新增/删除/拆分 leaf 或规范文档必须同时更新对应上下文包和本机门。

#### EVID-G0-004：本地 `@docs` clean-checkout 上下文包

- `leaf_ids` / `missing_ids`：无；本记录修复 Group 0 的 Git-truth 验收漏门，不改变 103 个 breakpoint 或 5 个 Missing 的 owner/status。
- owner Group / 依赖 Group：Group 0 / 无。
- 当前状态：`closed`；独立 Git-index 快照已证明 AA 的全部本仓路由可在 clean checkout 读取，Group 0 在发现缺口后重新关闭。
- 证据 owner / 更新时间：主 Agent / 2026-07-15。
- 冻结事实：缺口在验证 P1-004 staged snapshot 时暴露；P1-004 code commit=`585581319`，本修复开工 HEAD=`58558131918a5b706c7438f52ea76ec9d8f560c7`。提交前当前开发工作树仍含其它 Session 的未暂存实现，不进入本证据；本项 owned manifest 是 59 份既有本地 `@docs`、`docs/README.md` 与 route validator，共 61 个 path。
- Context Read Receipt：

```yaml
context_read_receipt:
  aa_entry: "§0.1–§0.5 + §9 Group 0–10 + §12.3 Group 0"
  leaf_ids: []
  documents:
    - ref: "@docs/agent-native-unified-atomic-review-2026-07-14.md §0.5 + §9"
      role: "authority"
      decision_consumed: "本仓 @docs 是可执行施工输入，必须在 clean checkout 可读取，不能依赖开发者本机残留"
    - ref: "@docs/README.md §AA 修复上下文包（Git truth）"
      role: "design"
      decision_consumed: "README 只索引 AA，不复制逐 Group 路由形成第二事实源；历史文档入 Git 不恢复旧完成声明"
  source_baselines:
    hive_head: "58558131918a5b706c7438f52ea76ec9d8f560c7"
    freecode_head: "not-applicable: documentation portability gate"
    codex_head: "not-applicable: documentation portability gate"
  conflicts_or_deltas:
    - "旧 validator 只检查工作树文件存在；59 份被 AA 引用的 Markdown 命中 docs/ ignore 且不在 Git，clean checkout 会失去上下文"
  evidence_sink: "EVID-G0-004"
```

- Red 1（独立快照）：从 P1-004 Git index 构造 detached worktree 后运行完整 backend，得到 `3 failed, 7011 passed, 2 skipped`；其中两项为 `docs/hive-sota-master-goal.md` 不存在及 AA 本地路由文件不全，证明正常工作树的 Green 被 ignored docs 掩盖。第三项 `ToolRuntimeService.execute > 60` 属 P1-004，并已在 `EVID-G1-004` 独立收口。
- Red 2（永久回归）：先在 `test_document_routes_are_portable_and_external_refs_are_snapshot_bound` 增加 `local_references <= git ls-files` 断言；`pytest -q ...::test_document_routes_are_portable_and_external_refs_are_snapshot_bound` → `1 failed`，机械列出 59 个 untracked local route。
- 实现：对 AA 当前引用的 59 份既有 Markdown 执行显式 `git add -f`；`docs/README.md` 新增单一 AA context-pack 入口；validator 同时要求相对路径存在且 Git-tracked。没有把 59 份历史报告升级成 L0/L1，也没有复制其完成状态。
- 内容卫生：纳管前只机械移除 5 个旧行尾空白与 2 个 EOF 多余空行；`git diff --cached --check` exit `0`。对 staged docs 扫描常见 `sk-*`、AWS key、带密码 PostgreSQL URL、private-key header 与 Slack token 形态，无真实 secret 命中。
- Green（路由/北极星）：`pytest -q tests/architecture/test_agent_native_repair_ledger.py tests/architecture/test_model_agency_no_semantic_truncation.py` → `46 passed in 0.43s`。
- Green（clean checkout 全仓）：以 `git write-tree + commit-tree + git worktree add --detach` 构造只含 Git truth 的临时 checkout，执行 `pytest tests -q` → `7014 passed, 2 skipped in 251.57s`，exit `0`。该快照没有携带并发工作树新增的 27 个未提交测试，因此 7014 是本提交可复现的基线。
- commit / deploy：context-pack 独立 commit=`19c6ddeb7`；纯文档/validator，无 schema、data、runtime 或 production deploy。
- rollback：回退 `19c6ddeb7` 会让 tracked-route 回归再次 Red，且后续 Session 无法从 clean checkout 沿 AA 取上下文；除非先用等价可移植 artifact registry 替代，否则不得单独删除这些 docs。
- 七原子：Input=AA 的 local `@docs` 集合；Authority=AA/L0/L1 层级；Execution=Git index + architecture validator；Evidence=`git ls-files`、staged snapshot、full pytest；Recovery=commit 历史与 route delta；Consumption=所有 Group 修复 Session/CI；Acceptance=59/59 tracked、46 focused、7014 full Green。
- 残余风险：跨仓 Hive Connect 仍按 §0.3 commit+SHA registry 取证，不被本地 59 文件替代；后续新增 `@docs` 若未入 Git会由同一测试立即失败。业务 leaf 仍按 owner Group 保持原状态。

#### EVID-G0-005：Group 0–10 上下文包总索引与可执行回流门

- `leaf_ids` / `missing_ids`：无；本记录只增强终极施工导航，不改变 103 个 breakpoint、5 个 Missing、severity、owner 或业务状态。
- owner Group / 依赖 Group：Group 0 / 无。
- 当前状态：`closed`；11/11 Group 都已有“主合同 → §9 完整 `@` 路由 → 源码/Red → §12.4 证据”的唯一入口，且由 architecture validator 守恒。
- 证据 owner / 更新时间：主 Agent / 2026-07-15。
- 冻结事实：开工 HEAD=`67a0bcdcb37c6cc7a23471717bc7dd50c2821741`；开工工作树有 55 个 tracked dirty、4 个 untracked path。本项 owned manifest 仅为本文的 Group 0/Group 1 导航与 `EVID-G0-005`/`EVID-G1-005` 证据 hunk，以及 `backend/tests/architecture/test_agent_native_repair_ledger.py` 的路由门；其它业务改动未 stage、未覆盖、未归属本项。
- Context Read Receipt：

```yaml
context_read_receipt:
  aa_entry: "§0.5–§0.6 + §9 Group 0–10 + §12.3/§12.4"
  leaf_ids: []
  documents:
    - ref: "@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md §0 本文要拍板的核心结论"
      role: "design"
      decision_consumed: "Context 主合同必须逐 Group 进入 AA owner/consumer 路由，任何容量、迁移和裁决变化都回填同一总账"
    - ref: "@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md §0 本文的裁决位置"
      role: "design"
      decision_consumed: "Session Event/Item/Reducer 与 S/SESSION-G 不能被 Group 摘要替代，实际 schema/reducer/UI/生产证据必须回流 AA"
    - ref: "@docs/agent-native-atomic-review-2026-07-14.md §13–§22"
      role: "original_evidence"
      decision_consumed: "保留原断点语义和施工反例，不继承旧完成状态"
    - ref: "@docs/agent-native-atomic-review-501db655.md §13–§22"
      role: "original_evidence"
      decision_consumed: "逐 leaf 恢复安全、principal、Recovery、Memory、UI 和迁移 fault 证据"
    - ref: "@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md §5–§12"
      role: "acceptance"
      decision_consumed: "把单 root 100 child、400 Skill/200 MCP、大 Memory 和跨渠道 fault 送到唯一 owner Group"
  source_baselines:
    hive_head: "67a0bcdcb37c6cc7a23471717bc7dd50c2821741"
    freecode_head: "not-applicable: docs-only route registry; each business leaf rechecks its source baseline"
    codex_head: "not-applicable: docs-only route registry; each business leaf rechecks its source baseline"
  conflicts_or_deltas:
    - "既有 §9 已有 11 个长上下文包，但 AA 顶部没有一个可机械校验的快速总索引，执行者仍需人工猜第一份主合同"
    - "Group 1 AA 入口只列到 EVID-G1-004，和已存在的 EVID-G1-005/canonical 状态漂移"
  evidence_sink: "EVID-G0-005"
```

- Red 1：先给 architecture validator 增加 `test_every_group_has_one_machine_readable_context_package_index`，再运行 `pytest -q tests/architecture/test_agent_native_repair_ledger.py::test_every_group_has_one_machine_readable_context_package_index` → `1 failed`；正确失败为 AA 缺少 `group-context-package-map` machine-readable region。旧门只验证每个 Group 分散存在若干标记，无法证明有 0–10 唯一总索引、主合同、读取目的和证据 sink。
- Red 2：首次运行整份 ledger validator → `1 failed, 9 passed`；新证据把本仓 Markdown 通配模式误写成反引号内的真实 `@` 路由，portable-route 校验正确将其判为不存在文件。修复为普通描述文字，不通过放宽 path validator 掩盖错误。
- 实现：新增 §0.6 的 11 行 Group 上下文包总索引；每行指名最先打开的 `primary` 文档、必须带回施工的 `purpose`、§9 完整 `@` 路由和 §12.4 `EVID-Gn-*` sink。§9 仍是完整清单的唯一事实源，索引不复制所有长文档列表；未登记的新权威必须先更新路由/receipt/validator，纯历史或被覆盖方案不得污染 `@必须先读`。同步把 Group 1 AA 入口补到 `EVID-G1-005`，并更新 Group 0 顶部状态、§9 汇总和 §12.3 索引。
- 本仓文档审计：AA 当前解析出 79 个唯一的本仓 Markdown `@` 路由，`is_file + git ls-files --error-unmatch` 结果 `missing_or_untracked=0`；两份关键 Context/Session 合同和三份原始 Review 的文档引用差集只出现示例/运行时文件名 `AGENTS.md`、`CLAUDE.md`、`SKILL.md`、`backend-session-audit.md`、`soul.md`、`source.md`，没有遗漏新的子系统施工合同。
- 跨仓审计：`https://github.com/rocky2431/hive-connect.git` 的本地/remote HEAD 均为固定快照 `6cf0b591c037c52ab6b0542c1756006023c7f218`；对 §0.3 registry 的 8 个文件逐一执行 `git show <commit>:<path> | shasum -a 256`，结果 `verified=8 failed=0`。Group 7 仍必须在实际开工时 fresh-check registry delta，不能继承本次结果冒充未来状态。
- Green 1：新增门单测 → `1 passed in 0.23s`。
- Green 2：`cd backend && source .venv/bin/activate && pytest -q tests/architecture/test_agent_native_repair_ledger.py` → `10 passed in 0.29s`。
- Green 3：`cd backend && source .venv/bin/activate && ruff check tests/architecture/test_agent_native_repair_ledger.py` → `All checks passed!`；owned paths `git diff --check` → exit `0`。
- Green 4（独立 Git truth）：在写回本条证据前，以备用 index 从 HEAD 只加入候选 AA 与 route validator，得到 tree=`2d06e60cefb6712a0f88183b2892d5f86b5c5f34`、临时 verification commit=`d1bf15cc2156d7be398d68613cc682158f8ea292`；detached checkout 执行 `pytest -q tests/architecture` → `174 passed in 9.04s`，同一 validator Ruff → `All checks passed!`。验证结束后临时 worktree 已清理；结果不依赖当前 55 个 tracked dirty/4 个 untracked 外部路径；最终证据文字另由 Green 2/3 与提交前 diff check 重验。
- commit / deploy：本文与 route validator 组成独立两文件 commit；最终 hash 由 Git history 记录，正文不预写自引用 commit。纯文档/CI 门禁不触发三服务部署。
- migration / rollback：纯 Markdown 导航与 architecture validator，无 schema/data migration、backfill 或 runtime。rollback 只能同时回退 §0.6 与对应 validator；单独删索引会让测试 Red，单独删 validator 会重新允许路由静默漂移。
- 七原子：Input=Group/leaf/Missing；Authority=L0/L1 + §9 完整路由；Execution=primary→detail→源码/Red；Evidence=Context Read Receipt + EVID；Recovery=稳定章节、Git-tracked docs、跨仓 pinned snapshot 与 route delta；Consumption=后续每个 Group 的开工流程；Acceptance=11/11 index、79/79 本仓路由、8/8 跨仓 hash、10 tests、ruff、diff check。
- 残余风险：文档路由只能保证施工者拿到正确上下文和回填位置，不能证明任何业务 leaf 已 Green；Group 1 仍是 5 个 local Green/production gate open，Group 2–10 状态不因本记录改变。

#### EVID-G1-001：P0-F1 governed public HTTP egress

- `leaf_ids`：`P0-F1`；同根范围包含 Agent `web_fetch`/advanced fetch、Personal KB URL import、`upload_image(url=...)` 的远端 URL 转交；未把固定 provider API、显式 Custom API connector 或受权内网连接器偷换成“任意公网 fetch”。
- owner Group / 依赖 Group：Group 1 / Group 0。
- 当前状态：`closed`；当前 checkout 的实现、fault matrix、仓级 suite、三服务同源部署和 production 网络边界 canary 均已绿。
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
- commit / deploy / production canary：独立 P0 commit=`10b74360a`，随 source=`1b822eb766` 完成三服务部署：backend=`d5a93a36-6bab-4ff9-82cb-55edf1403213`、backend-api=`57bdc882-a1d8-4746-8c34-8ba1e825c4a1`、frontend=`815630ed-01b5-4121-a945-f9daebf8757c`，均 `SUCCESS`。生产容器通过同一 governed transport 得到：`https://example.com`=`200/559 bytes`；同源 redirect 最终 `https://httpbin.org/get`=`200/304 bytes`；direct metadata 与 redirect-to-metadata 均 `network_target_denied`；HTTPS→HTTP downgrade=`network_redirect_denied`。本地 fault matrix 继续覆盖不可安全注入 production 的 peer mismatch/DNS rebinding/compression bomb 等故障。
- 七原子：Input=Agent/user URL；Authority=public-target L0 policy；Execution=pinned transport/remote target gate；Evidence=typed code + tool/job receipts；Recovery=retry/换源且无 partial semantic fallback；Consumption=Web/PKB/ImageKit live path；Acceptance=本地 consumer/fault suite + production actual-network canary + 三服务 freshness 已绿。因此 canonical 行为 `closed:EVID-G1-001`。
- 残余边界：显式 Custom API、MCP、HTTP Hook 和企业内网连接器拥有不同的管理员配置/approval/network scope，不能被本 P0 public-fetch policy 粗暴删除；它们在 Group 1 的 authority/B-01 与后续 governance recheck 中必须证明 allowlist/pinning/credential/receipt，而不是默认继承“public fetch 已安全”。

#### EVID-G1-002：P0-F2 migration 与 RLS catalog fail-closed

- `leaf_ids`：`P0-F2`；只负责 deployment schema truth、migration owner/runtime separation 与启动前 catalog readiness，不把业务数据语义、一般 runtime availability 或模型判断扩张成 schema hard gate。
- owner Group / 依赖 Group：Group 1 / Group 0；production 已执行但未入 Git 的 `memory_context_warning_0714` 及其 warning consumer 是本项不可跳过的部署前置，不改变 `SES-ITEM-001` 的 Group 2 owner 归属。
- 当前状态：`closed`；本地 Red→Green、隔离 PostgreSQL fault/rollback/re-upgrade、deployed-source reconciliation、三服务同源部署、production startup/readiness/runtime-role 与 cross-tenant RLS canary 均已完成。
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
- migration / backfill / rollback：本项不新增 schema revision；只恢复 production 已执行 revision 的 Git truth。隔离库 downgrade/re-upgrade 与 readiness fault 已验；production 部署没有新增 row backfill。代码 rollback 不得删掉已执行的历史 revision 文件；如 readiness 拒绝，typed issue 保留且容器可重启重验。刻意破坏 production RLS/Alembic 的演练会中断线上流量，因此由隔离真实 PostgreSQL 的 downgrade/NO FORCE/re-upgrade fault drill 承担验收，不把破坏生产当作“更真实”的完成门。
- Model Agency / 北极星：该 hard gate逐项命中 machine contracts、authority、execution isolation 与 evidence/recovery allowlist；不检查 Prompt、模型输出、任务意义或自然语言，不裁剪 context，也不把 catalog failure伪装成模型结论。API/runtime 只能在机械 schema 不可消费时 fail-closed，不能因此禁用无关模型能力。
- commit / deploy / production canary：deployed-source prerequisite=`f7902ab7b`；P0-F2=`5ad6ff3c6`；二者随 source=`1b822eb766` 完成上述三服务部署。backend startup 通过 fail-closed entrypoint 后 health=`200/status ok`；容器内 readiness 输出 `actual_heads=[memory_context_warning_0714]`、`expected_heads` 同值、`checked_table_count=127`、`issues=[]`、`ready=true`。health 同时证明 runtime role=`app_rls`、`superuser=false`、`bypassrls=false`。生产只读双 tenant canary 在两个 scope 分别可见 `5`/`1` 个本租户 Agent，显式查询另一 tenant 的可见数均为 `0`。
- 七原子：Input=container env + migration graph；Authority=schema owner vs runtime role；Execution=single entrypoint gate；Evidence=typed JSON/exit/catalog/Git blob；Recovery=restart/隔离 rollback/re-upgrade；Consumption=runtime/API 只在 ready 后启动；Acceptance=本地 fault/真实 PG、production startup/readiness/role/RLS 与三服务 freshness 全绿。因此 canonical 行为 `closed:EVID-G1-002`。

#### EVID-G1-003：E-1 durable background subagent requester authority

- `leaf_ids`：`E-1`；同根范围覆盖 background enqueue、RuntimeTask worker dispatch、restart requeue、child transcript replay、completion projection、daemon wake、Tool/T0/audit/HR Personal KB requester 传播。前台同步 Sub-agent 不依赖 durable RuntimeTask，未被偷换成另一套后台身份模型。
- owner Group / 依赖 Group：Group 1 / Group 0；后续 `P1-004` 必须复用本项收敛后的 execution principal，但不能把 A2A frame 债务并入本项冒充关闭。
- 当前状态：`in_progress`；本地实现、真实 PostgreSQL、故障回归、仓级 suite、FreeCode/Codex 对照、production legacy read-only preflight、独立 commit 与三服务部署已绿；跨用户 live canary 与 legacy held drift 的 operator disposition 尚未完成，故 canonical 为 `in_progress-production-deployed:EVID-G1-003`，不得标 `closed`。
- 证据 owner / 更新时间：主 Agent / 2026-07-15。
- 冻结事实：开工 HEAD `5ad6ff3c6f22e5100844514ac6004f0705c12d31`；最终归属复核时共享工作树仍有 62 个 tracked dirty 与 4 个 untracked path。本项只拥有 `backend/app/services/runtime_task_authority.py`、`backend/app/services/subagent_run_service.py`、`backend/app/services/subagent_wake_consumer.py`、`backend/app/tools/handlers/subagent.py`、5 个对应 test path 与本文证据 hunk；没有接管 dirty 的 `backend/app/agents/subagent.py`、`backend/app/agents/orchestrator.py` 或 `backend/app/services/agent_team_runtime_service.py`。
- 已完整读取的 `@必须先读` 快照：`runtime-model-agency-constraint-audit`=`366c8a5e4351e76083e6096a8cca09fe93a952ce831cf01e3cae34e2f8b91530`、`agent-permission-governance-spec`=`e60f2dcf8711999cf655ccae180fb52810ad2a73f265028c1c56226ba73099ac`、`session-permission-and-enterprise-hard-rules`=`db8d895b283ecb7ae747ef7fbbb76591f32aba599052d24d759dcb31c83f7cb0`、`governance-layer-architecture`=`593c54f399708d3c4d61bf1900b8d788ecc1a3127077a1ffd9ab3a938b3ad94e`、`tool-call-governance-closure`=`05db3f2d3747a083575fe92f20acd3635ff1f0e48b372b3a6fe201e72df93963`、`session-rls-preflight`=`057b7631c75c80ce394096ea5c53cd3afc2b41d0994dcb62860d2fdc8a4029dc`、`rls-enforcement-migration-plan`=`66864a7c18233d7bcfcc825344eccc93a604d13039c40616d7b2b0387348b466`、`personal-company-knowledge-boundary`=`644dd7f85c2a212d6e93101a4101607d3e58ab79a8d6f8048061c5f654305609`、`personal-kb-completion-contract`=`7dad2c59695109d06c38e4f24cc39648c53fa66b59761c3af99b70ae57328544`、`runtime-budget-conformance`=`5299826c1a4b561328739e7bfc2d2438eb98388cf235124989a59b624dd8c039`。直接裁决条款是 accountable principal 与 actor 分离、Sub-agent 继承父 session/delegation 且只能缩权、Personal KB 属于 requester/owner 而不是 Agent、跨 owner 默认 fail closed。
- 当前 live entry / authority source：`spawn_subagent_tool(run_in_background)` 只能从 authenticated `ToolExecutionContext.user_id` 入队；`start_subagent_run()` 把它写入 canonical `runtime_tasks.root_user_id`、child session 与 metadata；worker/restart/completion/wake 一律经 `runtime_task_requester_user_id()` 读取该列。metadata 与 execution principal 只做一致性证据，不能在 canonical 列缺失时 fallback；`agent.creator_id` 不再出现在 durable dispatch/wake authority path。
- Red 1：首次运行 `pytest tests/services/test_runtime_task_authority.py tests/services/test_subagent_run_service.py -q` 在 collection 以缺少 `runtime_task_requester_user_id` 正确失败；只补 helper 后得到 5 个行为失败，分别坐实 enqueue 可缺 requester、dispatch 用 creator、并发 requester 串线、缺 requester 未 hold、child completion 可按漂移 session user wake。
- Red 2：background tool 入口缺 requester 的回归先失败，因为旧实现仍进入 parent runtime resolver；新增 typed `subagent_requester_unavailable` 后 focused test Green。
- Red 3：`pytest ...test_production_parent_wake_invoker_holds_creator_drift... ...holds_when_runtime_task_identity_is_missing -q` → `2 failed`；旧 daemon wake 不读 RuntimeTask，仍使用 `parent_session.user_id or agent.creator_id`，且缺 task id 时可退到 signal id。
- Red 4：production 只读盘点暴露旧 child transcript 入口后，新增 pre-model 两条回归；首次执行 → `2 failed`，正确失败为 loader 不接 requester、dispatch 不捕获 child-session authority drift。
- 实现：新增 typed `RuntimeTaskRequesterUnavailable(reason_code,evidence)`；`runtime_task_requester_user_id()` 只认 canonical column，并拒绝 metadata/principal 冲突。background enqueue 在预算、child session 或 RuntimeTask 写入前拒绝 missing/invalid requester；worker 用 canonical requester 覆盖任何 resolver/legacy creator context，且 canonical recovery metadata 最后写入，不能被不可信 persisted metadata 反覆盖。
- pre-model 数据入口：只有 child T0 transcript 实际存在时，loader 才在返回任何 message 前查询实际 `ChatSession(id,agent_id)` 并校验 `session.user_id == RuntimeTask.root_user_id`；missing/invalid/mismatch 进入 `pre_model_input` typed hold，模型、ToolRuntime 和 parent resolver 均不启动。无 transcript 的新执行不为方便而伪造 history，也不读取其它 user bytes。
- completion / wake / restart：completion projection 在写 child/parent event 或 outbox 前复核 child session user；restart requeue 在通知 worker 前复核 root requester；daemon wake 必须携 `subagent_run_id|runtime_task_id`，再校验 task type、tenant、parent Agent、root session、signal requester、parent session user 与 requester User tenant。缺失或漂移抛 typed unavailable，drain 保留 signal；不再以 signal id 或 creator 猜 authority。
- Recovery / observability：deterministic identity 缺失或冲突进入 `needs_reconciliation`，记录 phase、reason、authority source、evidence、decision entry 与 required operator action；默认没有 `reconciliation_retry_allowed`，因此 generic admin retry fail closed，operator 仍可 inspect/archive/resolve。瞬时 DB/transport exception 不被伪装成身份事实，继续走 worker retry；原证据和 signal 保留。
- migration / dry-run / backfill / cleanup / rollback：`root_user_id`/session 列已存在，无 schema migration。生产 READ ONLY 查询得到 `8,967` 条 subagent RuntimeTask：`completed=3,725`、`killed=26`、`needs_reconciliation=5,216`，无 pending/running/resumable/suspended；active missing root=`0`、metadata/root conflict=`0`。held 中 `1,499` 条满足 `child.user_id != root_user_id`，交叉查询证明 `1,499/1,499 child.user_id=agent.creator_id` 且 `root_user_id!=creator_id`，同时 `1,499/1,499` parent session 已缺失；因此没有足够机械证据自动改写 session owner，保持 quarantine 才是正确 backfill 结论。rollback 只回退代码 commit，不删除/重写这些历史证据。
- FreeCode/CC 语义底线：当前 FreeCode `runAsyncAgentLifecycle()` 保留完整 child message/progress/final/task notification，`resumeAgentBackground()` 从 transcript 恢复 tool/context；本项没有删工具、裁上下文、降模型或改变 result 语义，只在 authorized input 前增加 Hive 多用户 authority frame。Codex `ThreadManager.spawn_subagent()` 先 materialize/flush parent rollout，且测试要求 child 持久化 parent originator、completion 通知 parent；Hive 对应地保留 lineage/notification，同时把 Codex 单用户没有的 requester 绑定做成 additive enterprise delta。
- Green（定向）：真实 PG creator≠requester enqueue + session allow/deny 与两条 pre-model 回归 → `3 passed in 6.29s`；authority/run/wake/tool/architecture/worker/HR PKB 合集最终 → `121 passed in 9.55s`；scoped `ruff check` → `All checks passed!`，`ruff format --check` → `9 files already formatted`。
- Green（仓级最终复跑）：`cd backend && source .venv/bin/activate && pytest tests -q` → `7016 passed, 2 skipped in 235.72s`，exit `0`；该结果包含最终 tenant-bound pre-model gate、wake authority、真实 PG 与文档 ledger validator 当前代码状态。
- fault / concurrency / security：覆盖 creator context 注入、malicious recovery metadata 覆盖 requester、两个 requester 并发隔离、missing/invalid/conflicting root、restart missing requester、child completion drift、pre-model transcript drift、wake task-id missing、signal/session/requester drift；架构墓碑断言 durable dispatch/wake 不得重新引入 creator 或 signal-id fallback。
- 真实消费：real spawn 的 `AgentInvocationRequest.user_id` 与 `SessionContext.metadata.requester_user_id` 均为 durable requester；T0/tool/audit 继续消费同一 runtime context；`test_system_hr_personal_kb_read_is_bound_to_current_requester` 证明 HR Personal KB 仍按当前 requester 查库。E-1 只恢复可信 principal，不替代 `KB-AUTH-001` 的 cross-principal grant/sensitivity ceiling 修复。
- commit / deploy / production canary：独立 E-1 commit=`3b3b281543bc` 已随 source=`1b822eb766` 完成三服务同源部署并通过基础 health；成员 B 触发 creator A 的共享 Agent 后 child/T0/audit/HR PKB 均归 B、wake signal 保留/恢复，以及 1,499 条 held legacy drift 的 operator archive/retain 决策仍 open。
- 七原子：Input=authenticated background tool context；Authority=`runtime_tasks.root_user_id` + exact session/tenant binding；Execution=single RuntimeTask worker/wake path；Evidence=typed hold/decision entry/T0/span/outbox/signal；Recovery=quarantine、no blind retry、operator inspect/archive/resolve；Consumption=AgentInvocationRequest/Tool/T0/audit/HR PKB/parent wake；Acceptance=本地、production read-only 与 deploy 已绿，live canary/legacy disposition 未验。因此 canonical 行为 `in_progress-production-deployed:EVID-G1-003`，不是 closed。
- 残余风险 / 下一动作：执行 creator≠requester 的受控 production canary。对 1,499 条历史 held drift 只允许 dry-run 后显式 operator retain/archive；因 parent session 已不存在，不得用 creator、metadata 或自然语言猜 requester 并批量回填。完成 live canary 与 disposition evidence 后才可关闭 E-1；代码施工继续进入其它 Group 1 leaf，但不得用其进度掩盖 E-1 的 production gate。
- 对应 §12.2 canonical 行已更新为 `in_progress-production-deployed:EVID-G1-003`；不改变 103 分母、severity 或 Group owner。

#### EVID-G1-004：P1-004 typed A2A authority frame 与 restart receipt

- `leaf_ids`：`P1-004`；同根范围覆盖 sync/async A2A、custom executor、ToolRuntime effect boundary、RuntimeTask persistence/worker dispatch/restart resume、confirmed Plan handoff 与 child failure outcome。它不替代 `P1-F4` 的通用 RecoveryManifest，也不宣称 Group 2 的最终 Session item/prose 已闭环。
- owner Group / 依赖 Group：Group 1 / Group 0、`E-1`。本项复用 `E-1` 的 authenticated requester/root principal，再把它原子化绑定到 child tool effect；没有重新引入 creator fallback。
- 当前状态：`in_progress`；本地实现、独立 staged snapshot、仓级 clean-checkout、FreeCode/Codex 对照、production read-only preflight 与三服务部署已绿；sync/async/nested A2A live canary 与安全 rollback drill 未执行，因此 canonical 为 `in_progress-production-deployed:EVID-G1-004`，不是 closed。
- 证据 owner / 更新时间：主 Agent / 2026-07-15。
- 冻结事实：开工 HEAD=`0a8bba2bd`；code commit=`58558131918a5b706c7438f52ea76ec9d8f560c7`。提交前共享工作树含 hook/DB/Session 等其它 Session 改动；本项逐 hunk 排除了 `db=None`、hook `evidence_mode`、hook metadata 与对应测试期待，只提交 `git show --name-only 585581319` 所列 20 个 authority-frame 实现/测试 path。
- Context Read Receipt：

```yaml
context_read_receipt:
  aa_entry: "§9 Group 1 + §12.1 P1-004 + §12.2 P1-004"
  leaf_ids: ["P1-004"]
  documents:
    - ref: "@docs/agent-native-atomic-review-501db655.md §13 [P1-004] + §20–§22"
      role: "original_evidence"
      decision_consumed: "外层 A2A identity/policy/sandbox/approval 丢失后，inner tool 不能凭 child 默认值执行"
    - ref: "@docs/runtime-model-agency-constraint-audit-2026-07-13.md §5–§11 + §13–§15"
      role: "authority"
      decision_consumed: "只在 principal/policy/receipt/effect 等机械事实上 fail closed；禁止自然语言扫描或删减模型能力"
    - ref: "@docs/agent-permission-governance-spec-2026-07-07.md"
      role: "authority"
      decision_consumed: "requester、source/target Agent、tenant 与 delegation chain 必须来自可信 frame 且逐 hop 只能缩权"
    - ref: "@docs/ccplus-tool-call-governance-closure-landing-plan-2026-06-28.md"
      role: "design"
      decision_consumed: "所有 child effect 进入同一 governed ToolRuntime throat，并留下 authorization receipt"
    - ref: "@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md §9–§14 + §16.2 + §21–§24"
      role: "design"
      decision_consumed: "authority/terminal/recovery 使用 typed state 和 durable refs，restart 不从 prose 重建权限"
    - ref: "@docs/a2a-session-substrate-design-2026-06-24.md"
      role: "design"
      decision_consumed: "A2A child 保留 root/parent/session/trace lineage，不能把 Agent creator 当 requester"
  source_baselines:
    hive_head: "0a8bba2bd"
    freecode_head: "7dc15d6c8fb0c40c7fcc02ce9b58204324252632"
    codex_head: "5c19155cbd93bfa099016e7487259f61669823ff"
  conflicts_or_deltas:
    - "旧 success fixtures 没有 authenticated parent principal；新 contract 将其改为正确失败，而不是给 legacy path 隐式豁免"
    - "并发 hook/DB diff 不属于本 leaf，已从 staged commit 排除"
  evidence_sink: "EVID-G1-004"
```

- FreeCode/CC 语义底线：`createSubagentContext()` 隔离 child mutable state但继承 permission state，`reconstructForSubagentResume()` 从持久 transcript 恢复，`hasPermissionsToUseTool()` 在 effect time 使用 typed allow/ask/deny；没有用 prompt 关键词替代权限。Hive 保留这组能力并增加多租户 principal/receipt/restart reconciliation，不删 tool、不裁上下文、不降模型。
- Codex additive delta：`handle_spawn_agent`/`ThreadManager.spawn_subagent` 在 fork 前 materialize rollout，并把 approval policy/provider 作为 typed `TurnContext` 传播；Hive 采用同类 typed frame 与 durable receipt，但仍以 CC capability surface 为底线。
- Red A（完整性）：首轮 focused suite `3 failed`，分别证明缺 principal 仍进入模型、inner tool frame 没有完整 snapshot 字段、persisted snapshot drift 未校验。
- Red B（effect deny）：父 profile 精确 `denied_actions=[tool_name]` 时旧路径仍返回 `EFFECT_RAN`。
- Red C（identity/trace）：sync delegation 返回了生成 trace，但 request/snapshot 中 trace 仍为 `None`，receipt 无法绑定同一 effect。
- Red D（frame 约束）：target tenant drift、缺 explicit parent Agent、persisted `required=false` 三条回归均失败并继续执行或进入模型。
- Red E/F（旁路）：不接受 `authority_frame` 的 custom executor 返回 `UNFRAMED_EFFECT_RAN`；只带 legacy `interaction_type=delegation|agent_message` 的 Session 没有构造 required-invalid frame，可绕过校验。
- Red G（失败感知）：async `authority_unavailable:*` handle 被包装为 success；sync child runtime authority failure 也被当作普通 assistant reply。
- Red H（仓级兼容）：首次全仓运行 `9 failed, 7032 passed, 2 skipped`；9 项全部是既有成功 fixture 未提供 authenticated parent frame。fixture 改为真实 tenant/requester/parent/root 绑定后定向 `12 passed`，没有在 production code 添加兼容豁免。
- 实现（单原子 frame）：新增 `A2AToolAuthorityFrame(schema, principal, capability/policy hash+snapshot, execution identity, delegation token, child/parent/root session/task/budget/trace, sandbox, approval)`；Invoker、agent-message custom executor、`agent_tools` facade、ToolRuntime service 与 execution pipeline 只传一个 typed atom，避免 loose kwargs 独立丢失。
- 实现（可信 principal）：`_child_execution_principal()` 要求 explicit UUID parent，验证 parent source、requester owner、request tenant、target tenant 与 target Agent，再保留 root session/task 和 delegation chain；confirmed Plan handoff 使用确认用户而不是 source Agent creator。
- 实现（持久 receipt/recovery）：async enqueue 在排队前生成完整 authority snapshot、canonical policy hash、request hash 与 child principal；worker dispatch/restart rehydrate 必须重算并比对。missing/invalid fresh authority 返回 typed `unavailable`，persisted request/snapshot/policy/principal drift 进入 `needs_reconciliation`、禁用盲重试并保留原 evidence。
- 实现（effect boundary）：在 runtime resolution 和任何 handler 前验证 schema、`required is True`、SHA-256、snapshot/profile/principal、tenant/requester/target/session/task/root/budget/trace、execution identity、sandbox/approval 与 delegation token parent/child/id。custom executor 或 governed runtime 不支持 atomic frame 时返回 typed `authority_context_unavailable`，effect 不启动。
- Model Agency：唯一 capability hard deny 是 structured parent `denied_actions` 对 exact tool name 的匹配；benign 文本包含 security/tool/permission 词不会触发 gate。平台不扫描 prompt/final、不改写 model-authored final、不移除无关 tools；失败只产生 typed unavailable/reconciliation evidence，由模型在后续可恢复 turn 解释。
- Evidence/consumer：final tool decision receipt 记录 frame schema、snapshot/policy hash、principal、trace、session/task/root/budget、delegation token、sandbox 与 approval；AgentInvocation Session metadata、ToolExecutionContext、RuntimeTask restart 和 A2A outcome 消费同一事实。child runtime failure 不再伪装成成功 reply。
- migration / dry-run / backfill：无 schema revision；authority snapshot/receipt 写入既有 `runtime_tasks.metadata_json`。production read-only schema probe确认 `runtime_tasks`、`metadata_json`、`task_type`、`status` 均存在；`SELECT status,count(*) ... WHERE task_type='delegation'` 返回空集合，因此上线前没有 legacy in-flight delegation 需要自动回填或 quarantine。禁止据此推断部署后永远无任务。
- rollback：当前已部署。回退前必须停止新 delegation admission，并确认无 pending/running task；若已有新 receipt，先 drain 或 hold，不能让旧 runtime 忽略 frame 后继续 effect。receipt/evidence 字段可保留，不做破坏性删除。
- Green（focused staged snapshot）：从 Git index 构造独立 worktree，执行 `pytest -q tests/agents/test_orchestrator.py tests/agents/test_orchestrator_authority_frame.py tests/runtime/test_invoker.py tests/services/test_agent_message_runtime.py tests/services/test_plan_mode_delegation_handoff.py tests/services/test_runtime_task_authority.py tests/tools/test_service.py tests/architecture/test_ux04_orchestration_boundaries.py` → `201 passed in 2.37s`。
- Green（静态/架构）：scoped `ruff check` → `All checks passed!`；`ruff format --check` → `20 files already formatted`；当前工作树 `pytest tests/architecture -q` → `172 passed`。新增 frame 使 high-risk root 临时超过 60 行的 Red，通过把 execution-pipeline import 提到模块级恢复薄入口，不删除 contract。
- Green（仓级）：并发工作树最终 `pytest tests -q` → `7041 passed, 2 skipped in 235.98s`；更严格的 Git-only clean checkout 在 `19c6ddeb7` 纳管上下文包后执行同命令 → `7014 passed, 2 skipped in 251.57s`。后者不含其它 Session 的 27 个未提交测试，是可复现提交基线。
- fault / security：覆盖 missing/drift receipt、nested chain、cross-tenant target、requester/source mismatch、missing parent、required=false、snapshot/profile/hash/identity/sandbox/approval/token drift、exact parent deny、custom executor 不兼容、legacy marker、benign security words、restart hold 与 async/sync typed failure。
- commit / deploy / production canary：独立 code commit=`585581319` 已随 source=`1b822eb766` 完成三服务同源部署；production preflight 只读且上线前 delegation row=`0`。sync/async/nested creator≠requester A2A、effect deny/no-effect receipt、restart reconciliation 与安全 rollback drill仍 open。
- 七原子：Input=authenticated parent principal + child request；Authority=tenant/requester/source/target + policy/sandbox/approval frame；Execution=single ToolRuntime pre-effect validator；Evidence=receipt/hash/principal/span/typed outcome；Recovery=unavailable/needs_reconciliation、evidence-preserving hold；Consumption=Invoker/custom executor/ToolRuntime/RuntimeTask/A2A outcome；Acceptance=Red→201 + 172 + 7014、production read-only与deploy Green，live authority/effect canary仍 open。
- 残余风险 / 下一动作：A2A failure 的最终 Session item/prose 统一属于 Group 2 typed truth，不能在本项冒充关闭；通用 resume authorization 进入 `P1-F4`。P1-004 已完成 deploy，仍必须完成跨用户 live A2A effect/no-effect 与 restart/rollback canary 后才能从 `in_progress` 改 `closed`。
- 对应 §12.2 canonical 行已更新为 `in_progress-production-deployed:EVID-G1-004`；Group 1 仍有 14 个 leaf 未闭环（其中本项与 E-1/P1-F4 是已部署未关闭），103 分母、severity 与 owner 不变。

#### EVID-G1-005：P1-F4 RecoveryManifest authority、immutable resource 与 legacy quarantine

- `leaf_ids`：`P1-F4`；同根范围覆盖 turn-start load/hydration、tool checkpoint、pre/post-compaction prompt restoration、recovered tool-frame replay、Web Runtime root/task/T0 sequence 传播、完整 manifest 渐进读取、raw workspace/API/code-exec 旁路、legacy fleet cutover 与 observability。它不替代 Group 2 的 canonical Session event/item，也不关闭 Group 6 的通用 Context Resource Plane。
- owner Group / 依赖 Group：Group 1 / Group 0、`E-1`、`P1-004`。本项消费前两项建立的 authenticated requester 与 A2A principal/delegation frame；缺失可信 authority 时只让恢复能力进入 typed unavailable，不阻塞当前模型用本轮授权输入继续推理。
- 当前状态：`in_progress`；本地实现、对抗 Red→Green、独立 Git-index snapshot、宽回归、FreeCode/Codex 当前源码对照、production read-only inventory、三服务部署与部署后 fleet dry-run 已绿。受控 legacy quarantine/apply、direct/A2A/restart live canary、metrics/rollback drill 未执行，因此 canonical 为 `in_progress-production-dry-run:EVID-G1-005`，不是 closed。
- 证据 owner / 更新时间：主 Agent / 2026-07-15。
- 冻结事实：开工 HEAD=`611e58e8cd8dce3211f1b7884594ad012a720202`；code commit=`67a0bcdcb`；staged verification tree=`d7e5789a78e020832506b97a599eff65df77e78a`、temporary commit=`339cddfaaa036401598c8e5aec9cdf77f8c521b3`。共享工作树同时存在 Hook、DB、Session terminal 等其它 session 改动；本项对 `backend/app/kernel/engine.py`、`backend/app/services/web_chat_run_orchestrator.py`、`backend/app/services/web_chat_runtime.py` 逐 hunk staging，排除了 `evidence_mode`、DB transaction、terminal classifier/reconciliation 等外部 hunks。`git show --name-status 67a0bcdcb` 是本项 29 个 path 的精确 ownership manifest。
- Context Read Receipt：

```yaml
context_read_receipt:
  aa_entry: "§9 Group 1 + §12.1 P1-F4 + §12.2 P1-F4"
  leaf_ids: ["P1-F4"]
  documents:
    - ref: "@docs/agent-native-atomic-review-2026-07-14.md §13 [P1-F4] + §20–§22"
      role: "original_evidence"
      decision_consumed: "legacy singleton 无 session_id 时 fail-open、post-compaction 旁路校验、agent-writable manifest 不可作为恢复权威"
    - ref: "@docs/agent-native-atomic-review-501db655.md §13 [P1-005] + §20–§22"
      role: "original_evidence"
      decision_consumed: "恢复状态必须绑定 tenant/Agent/requester/session/root task/policy/config/delegation，并为 legacy 保留可审计隔离路径"
    - ref: "@docs/runtime-model-agency-constraint-audit-2026-07-13.md §5–§11 + §13–§15"
      role: "authority"
      decision_consumed: "hard outcome 只读取身份、权限、完整性、生命周期等机械事实；失败只能 hold/quarantine/unavailable，不改写模型语义"
    - ref: "@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md §1 + §7.3 + §7.6 + §8–§15"
      role: "design"
      decision_consumed: "超预算恢复证据外置为 hash-pinned、分页、可恢复 resource；禁止 raw path、静默切片或 dangling pointer"
    - ref: "@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md §9–§14 + §16.2 + §19 + §21–§24"
      role: "design"
      decision_consumed: "resume/fork/compact 使用稳定 session/root identity 和 typed recovery state；projection 不能成为第二事实源"
    - ref: "@docs/agent-permission-governance-spec-2026-07-07.md"
      role: "authority"
      decision_consumed: "requester、principal、delegation 与 permission profile 必须来自 authenticated runtime frame，逐 hop 只能缩权"
    - ref: "@docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md"
      role: "authority"
      decision_consumed: "Session 恢复不能扩大旧权限，denied/unavailable/held 必须可区分并可恢复"
    - ref: "@docs/ccplus-governance-layer-architecture-2026-06-28.md + @docs/ccplus-tool-call-governance-closure-landing-plan-2026-06-28.md"
      role: "design"
      decision_consumed: "恢复 tool frame 仍须经过当前 governed tool throat，旧 pending metadata 不能直接重放 effect"
  source_baselines:
    hive_head: "611e58e8cd8dce3211f1b7884594ad012a720202"
    freecode_head: "7dc15d6c8fb0c40c7fcc02ce9b58204324252632"
    codex_head: "5c19155cbd93bfa099016e7487259f61669823ff"
  conflicts_or_deltas:
    - "历史方案建议直接恢复被 revert 的大实现；当前代码与 Session/Context contracts 已变化，本项重建同一 fail-closed 语义而未盲目 cherry-pick 旧实现"
    - "首版 hash pointer 指向 mutable raw filesystem path，经过对抗 review 证明可绕过 verified loader；最终改为 authority+content hash 的 opaque immutable resource"
    - "production 54 个 legacy 文件虽都有 session_id，但均无 HMAC、root task、principal/policy/config/delegation binding，不能因 session_id 存在而自动信任或签名"
    - "FreeCode/Codex 提供 session/rollout resume 与 fork 工程底线，不提供 Hive 多租户 requester authority；signed frame 是 additive enterprise delta"
  evidence_sink: "EVID-G1-005"
```

- 已完整读取的 `@必须先读` 快照：`AGENTS.md`=`647ae3f2e101a2da9955c071dbdd6ecff80781cb9f11f9ff812cbb3836e254ff`、`hive-sota-master-goal`=`ba2e820d019f74eeaf6b90e6d72eef4da2f28a4c09fc48b24887a0f3daebe6a8`、`ccplus-north-star-contract`=`9b2bda91cc42a4464ec9b91c483b78fb83965fe0ff6909836ea1fecc18299e5e`、`runtime-model-agency`=`366c8a5e4351e76083e6096a8cca09fe93a952ce831cf01e3cae34e2f8b91530`、`unified-context`=`c83a1f94b206af7de8bc44f7f4de35746c65d255574a347cbfd80ce0cc3075b7`、`session-v2`=`52a13072ef51ec1ad8f22be5f484b274880c4b7aea801104bd4ca5cdc27c0ac4`、`agent-permission-governance`=`e60f2dcf8711999cf655ccae180fb52810ad2a73f265028c1c56226ba73099ac`、`session-enterprise-hard-rules`=`db8d895b283ecb7ae747ef7fbbb76591f32aba599052d24d759dcb31c83f7cb0`、`governance-layer-architecture`=`593c54f399708d3c4d61bf1900b8d788ecc1a3127077a1ffd9ab3a938b3ad94e`、`tool-call-governance-closure`=`05db3f2d3747a083575fe92f20acd3635ff1f0e48b372b3a6fe201e72df93963`、`session-rls-preflight`=`057b7631c75c80ce394096ea5c53cd3afc2b41d0994dcb62860d2fdc8a4029dc`、`rls-enforcement-migration`=`66864a7c18233d7bcfcc825344eccc93a604d13039c40616d7b2b0387348b466`、`personal-company-knowledge-boundary`=`644dd7f85c2a212d6e93101a4101607d3e58ab79a8d6f8048061c5f654305609`、`personal-kb-completion`=`7dad2c59695109d06c38e4f24cc39648c53fa66b59761c3af99b70ae57328544`、`runtime-budget-conformance`=`5299826c1a4b561328739e7bfc2d2438eb98388cf235124989a59b624dd8c039`。Knowledge/RLS 文档只用于证明 requester/tenant/authority 不可猜测；本 leaf 未修改 PKB sensitivity 或数据库 RLS。
- FreeCode/CC current-source 对照：FreeCode HEAD=`7dc15d6c...`；`src/utils/sessionRestore.ts` SHA-256=`9dfe9c2203aac5c3cc6d49e45bf2f8fd381582781b5cec3abbc4ceb6d219e177`、`src/utils/sessionStorage.ts`=`8a123ebce1ee72b9081d34b8f3697e5fcc9c7576df5b98e4206bb28414134412`、`src/commands/resume/resume.tsx`=`5ba5d9bde131f8bed73fe3687bd625d5d908ed31cf78846b878e2f6c4ca36a9e`。其语义底线是 session ID 与 project/transcript path 原子切换、从 transcript 恢复 file/todo/context-collapse 状态、fork 使用新 session 且不污染原 transcript；Hive 保留完整 resume/fork 能力，没有用 authority gate 删除工具、证据或历史。
- Codex current-source 对照：Codex HEAD=`5c19155c...`；`rollout_reconstruction.rs`=`47f1e2923255480036e065ba41f4068248201586639b43e274843e94f7faa410`、`thread_manager.rs`=`9d6603a353352e51f1b9b4a8d58be9af8e4851fd79792576d0281859ae806c8e`、`session/tests.rs`=`4995c78d8c431bbf16c8bdd9d741f7a8a4fac3e066c35c7026d1097488f248f9`、`thread_resume.rs`=`7f69d60547b3662e1cf3376a749f48ff7aaf54239376e84714229d191fe314d7`、`thread_fork.rs`=`c5940e0b8fd9177dcf6668f24f7ce8d77e046b3ea6c323d6e71304211b27d8e7`。Codex 从 durable rollout/thread store 重建 history/window/settings/world state，root resume 保持 thread/session identity，subagent 恢复 persisted parent session，fork 生成新 thread 且原 rollout bytes 不变；Hive 采用 typed reconstruction/immutable evidence 思路并增加 tenant/requester/delegation 权威。
- 当前 live entry / authority source / unique writer / consumer：`TurnOrchestrator.run_agent_turn()` 在 `RuntimeConfig` 建立后调用 `resolve_recovery_authority()`，以 tenant、Agent、requester、session、root session/root RuntimeTask、execution principal/hash、permission policy hash、runtime/model config hash、base transcript sequence 与 A2A delegation hash生成唯一 `RecoveryAuthorityFrame`。`persist_recovery_manifest()` 是唯一 head writer；`load_recovery_manifest()` 是唯一 head loader。turn-start hydration、runtime prompt attachment、post-compaction restoration 与 recovered tool replay只消费同一个 `RecoveryManifestLoadResult`，不再自行重开文件或从普通 `pending_tool_frames` fallback。
- 实现（存储与完整性）：current head 路径为 `<agent>/runtime_artifacts/recovery_manifests/<sha256(session)>/<authority_digest>.json`；envelope 使用 purpose-derived HMAC-SHA256、0600、temp+fsync+atomic replace。policy/config/base-sequence/root/principal 任一漂移返回 typed `held`；损坏、无签名或无法证明 authority 的 legacy bytes 原样移入 content-hash quarantine。agent/session path traversal、agent/runtime/snapshot symlink swap 与 immutable collision 均 fail closed。
- 实现（lossless progressive recovery）：模型可见 pointer 不再暴露 mutable storage path，而是 `recovery-manifest://<authority_digest>/<envelope_sha256>`。render 前把已验证 envelope exact bytes materialize 到 immutable snapshot；`read_context_resource` 逐页重验 tenant/Agent/requester/session/root task/policy/direct identity 或 execution-principal/delegation hash、HMAC 与 content hash。pointer 同时记录 chars/bytes/hash/reader tool；后续 checkpoint 覆盖 mutable head 不改变旧 pointer bytes。
- 实现（旁路封口）：`read_file`、list/glob/grep、WorkspaceAuthority、raw Files API、artifact delivery path 与 isolated code-exec workspace 都隐藏/拒绝 `runtime_artifacts/recovery_manifests/**`、`runtime_artifacts/recovery_manifest.json`、`workspace/recovery_manifest.json`。opaque ref 不是 bearer token；另一个 requester/session/root 即使拿到 ref 也只能得到 typed `authority_denied`，tamper 只能得到 `integrity_mismatch`。
- 实现（failure awareness / stale revoke）：snapshot materialization 失败时，首次 prompt 与 post-compaction 都注入无内部 path/ref 的 `Recovery State{status=unavailable,reason=resource_snapshot_unavailable}`，不再静默省略。checkpoint persist 若 `held` 或抛异常，立即撤销 turn-start 旧 `loaded` result，转为 typed held/unavailable，防止旧 policy/transcript state 在下一次 compaction 继续消费。authority/root task 缺失只关闭恢复车道并记录状态，不饿死当前模型。
- Model Agency：没有检查 prompt/final 的关键词、相似度或语义；没有替换、追加或压制模型终答；没有按恢复失败删除无关 tools、context 或 output budget。唯一 hard outcomes 来自 authenticated identity、exact policy/config hashes、HMAC、filesystem isolation、session/root lifecycle 与 schema。fallback 仅为 absent/held/quarantined/unavailable/retry/review，并保留 exact evidence 或 opaque recovery ref。
- Red 证据：① cross-session/cross-agent/policy/config/base-sequence drift、无 session legacy 与 agent-forged manifest 原实现可 hydrate 或无 provenance；② model-visible held payload 泄露 `manifest_ref`；③完整 pointer 无 immutable `resource_path`，并可通过 raw workspace listing/read 绕过 verified loader；④`read_context_resource` schema 没有 dynamic recovery ref；⑤ base transcript sequence `0` 被 `or` 错换为 fallback `99`；⑥ unavailable metric 没有记录；⑦ symlinked quarantine destination 与 snapshot parent swap 未拒绝；⑧ snapshot materialization failure 在首次/post-compaction prompt 静默消失；⑨ checkpoint persist `held` 后仍保留旧 `loaded` result。最后一项 Red 命令 `pytest -q tests/kernel/test_engine.py -k 'recovery_checkpoint_hold_revokes or recovery_checkpoint_exception_revokes' -x` 正确得到 `1 failed`，失败断言为旧 result identity 未撤销。
- migration / dry-run / backfill / cleanup：无数据库 migration。`python -m app.scripts.repair_recovery_manifest_authority` 默认只读 dry-run；`--apply` 还必须显式 `--confirm`，只把 exact legacy bytes 移到可逆 quarantine，不猜 requester/root/policy/config，也不把 unsigned bytes 自动签名。部署前 inventory：`legacy=54`、`valid_json=54`、`with_session_id=54`、`signed=0`、`workspace_legacy=0`、`scoped_heads=0`、`quarantine=0`。source=`1b822eb766` 部署后重新执行默认 dry-run：`scanned=54`、`by_reason.legacy_authority_unverifiable=54`、`would_quarantine=54`、`quarantined=0`。这只证明处置集合，不等于 operator 已批准 apply。
- rollback：当前已部署，不能直接退回旧 unsigned singleton reader，否则会重新打开 P1。允许的恢复路径是 forward-fix 到“保留 signed/quarantine/raw-path guards，但将 recovery lane typed unavailable”的安全版本，或先停止相关 runtime、验证无 active recovery consumer 后恢复同一 authority contract。quarantine bytes 不删除，可按 operator 证据审查，但不得放回 live legacy path 让旧代码消费。
- Green（focused current worktree）：最终命令覆盖 runtime/persistence/metrics/legacy/architecture/kernel/e2e/web context/API/workspace/context-resource/tool registry，结果 `238 passed in 6.37s`；scoped `ruff check` → `All checks passed!`。
- Green（宽回归 current worktree）：`pytest -q tests/kernel tests/runtime tests/api/test_prometheus_metrics.py tests/api/test_files_channel_download_token.py tests/api/test_files_write_boundaries.py tests/tools/test_workspace.py tests/tools/test_context_resource_tool.py tests/tools/test_workspace_resource_tool_authority.py tests/tools/test_filesystem_unified_facades.py tests/e2e/test_tool_call_recovery_closure.py tests/services/test_web_chat_run_orchestrator.py tests/services/test_web_chat_runtime.py tests/services/test_recovery_authority_web_context.py` → `1260 passed in 24.75s`，`git diff --check` exit `0`。
- Green（独立 staged snapshot）：以 `git write-tree + commit-tree + git worktree add --detach` 构造只含 Git index 的 `339cddfaaa036401598c8e5aec9cdf77f8c521b3`，同一 focused suite → `238 passed in 6.78s`；同一 scoped Ruff → `All checks passed!`。因此结果不依赖共享工作树未提交 Hook/DB/Session hunks。
- fault / security / observability：覆盖 concurrent sessions、fork isolation、different root task、tenant/requester/agent/principal/policy/config/transcript/delegation drift、unsigned/corrupt/tampered envelope、atomic replace failure、path traversal/symlink swap、immutable pointer、foreign ref、snapshot tamper、raw API/workspace/code-exec denial、missing authority、checkpoint stale revoke 与 recovered-effect replay gate。`recovery_manifest_events_total{operation,status,reason}` 使用 bounded labels 暴露 resolve/load/persist/resource 的 bound/loaded/held/quarantined/unavailable 等状态。
- 真实消费：Web worker 把 root RuntimeTask/root session 和 accepted user T0 sequence 写入 `SessionContext`；Kernel turn 只 load/hydrate 一次；prompt/post-compaction 使用同一 verified result；tool checkpoint 每次更新该 result；recovered mutating tool frame 还要求 session metadata 中 authority digest 与 result 一致；模型通过既有 core `read_context_resource` 恢复完整 signed envelope。raw 文件不进入 Workspace/Artifact consumer，operator 使用 metrics、quarantine inventory 与受控 repair script。
- commit / deploy / production canary：独立 code commit=`67a0bcdcb` 已随 source=`1b822eb766` 完成三服务同源部署；backend/API/frontend deployment ID 与 `EVID-G1-001` 相同且均 `SUCCESS`，health 证明 RLS/sandbox/daemons 正常。direct owner、creator≠requester、nested A2A、restart/compact、legacy quarantine、foreign ref/no-content、metrics 与安全 rollback canary 仍 open。
- 七原子：Input=authenticated InvocationRequest + Session/T0/root metadata；Authority=signed `RecoveryAuthorityFrame` + current tool context；Execution=single store/loader + governed context-resource/replay gate；Evidence=HMAC envelope、immutable hash ref、typed status、metrics、exact quarantine bytes；Recovery=absent/held/quarantine/unavailable、atomic persistence、dry-run/confirm、safe forward rollback；Consumption=turn hydration、prompt、compaction、tool replay、context resource、operator metrics；Acceptance=Red→238 + 1260 + staged 238、source baseline、production deploy/read-only/dry-run 已绿，live canary 与 migration apply 未验。因此只允许 `in_progress-production-dry-run:EVID-G1-005`。
- 残余风险 / 下一动作：先对 `54` 条处置集合做 operator 审查，再以 `--apply --confirm` 执行可逆 quarantine；选择受控 Agent 验证一个 unsigned legacy exact quarantine、一个新 signed scoped head、一次 restart+compaction、一次 foreign requester deny、一次 nested A2A principal/delegation bind，并检查 metrics/health。只有 apply/disposition、live no-leak/no-duplicate-effect 与安全 rollback evidence 全部写回后，P1-F4 才可 `closed`。本地施工可继续下一 Group 1 leaf，但不得用后续进度掩盖这些 production gate。
- 对应 §12.2 canonical 行已更新为 `in_progress-production-dry-run:EVID-G1-005`；Group 1 仍有 14 个 leaf 未闭环，103 分母、severity、Group owner 与 5 个 Missing 均不变。

#### EVID-G1-006：KB-EXTRACT-001 sensitivity 单一枚举与持久抽取硬闸

- `leaf_ids`：`KB-EXTRACT-001`；owner Group / 依赖 Group：Group 1 / Group 0。本证据只关闭 sensitivity canonicalization 与 durable graph extraction seam；不关闭 `KB-AUTH-001` 的跨 principal read/grant、`KB-PROP-001` 的 provenance/外传，也不关闭 `KB-CONTRACT-001` 的 tool schema/描述一致性。
- 当前状态：`closed`。实现、可逆 migration/backfill、真实 PostgreSQL round-trip、clean-checkout 全量 backend、production schema/data/RLS、三服务 source freshness 与 recovery 都已 Green；对应 canonical 为 `closed:EVID-G1-006`。
- 冻结事实：开工 HEAD=`29794cd39ba2af563985da03cae427cc1c46006a`；共享工作树已有 50+ 个与本 leaf 无关的 tracked/untracked 改动，均未 stage、覆盖或归属本项。owned code manifest 为 commit `53287e7166fb4af7020188f466b90b0d10e9a06c` 的 15 个 migration/model/service/test path；`git show 53287e716 --name-only` 与 staged manifest 一致。

```yaml
context_read_receipt:
  aa_entry: "§9 Group 1 + §12.1/§12.2 KB-EXTRACT-001"
  leaf_ids: ["KB-EXTRACT-001"]
  documents:
    - ref: "@docs/personal-knowledge-base-completion-contract-2026-07-08.md §A4. LLM 知识抽取 / §2026-07-08 A4/A5 / §2026-07-14 Tool-first 重基线"
      role: "design + acceptance"
      decision_consumed: "完整 segment 交给 LLM；PL3/PL4 在外部抽取前按显式 sensitivity fail closed；失败写 degraded/warning，不能伪 ready"
      sha256: "7dad2c59695109d06c38e4f24cc39648c53fa66b59761c3af99b70ae57328544"
    - ref: "@docs/personal-knowledge-base-spec.md §3 Authority / Content / Index / §9 Permission / §12 七原子验收"
      role: "authority"
      decision_consumed: "owner truth、content truth 与可重建 index 分离；sensitivity/source policy 在返回或持久投影前由 authority plane 生效"
      sha256: "9ddf849312667e254143b8c73e0a365ba67cc631ab24f48c51a258d37872db2b"
    - ref: "@docs/personal-company-knowledge-tool-boundary-2026-07-10.md §0 最终决策 / §6 三视图 / §8 Permission 与 unavailable semantics"
      role: "runtime boundary"
      decision_consumed: "不把 Knowledge 内容预注入；当前 turn/read authority 与持久 evidence 分层；本 leaf 不借 extraction gate 改写 owner-direct read 语义"
      sha256: "644dd7f85c2a212d6e93101a4101607d3e58ab79a8d6f8048061c5f654305609"
    - ref: "@docs/runtime-model-agency-constraint-audit-2026-07-13.md §5 Model Agency Boundary / §C-20 / §7 物理边界"
      role: "north-star guard"
      decision_consumed: "硬闸只绑定显式 data-ingress/secret/durable-write 事实；不以关键词猜语义，不改写模型输出；unknown 只能 hold/fail closed"
      sha256: "366c8a5e4351e76083e6096a8cca09fe93a952ce831cf01e3cae34e2f8b91530"
    - ref: "@docs/ccplus-north-star-contract-2026-06-24.md §0.1 Decision Order / §2 Source Priority / §6 Codex Delta / §8 Atomic Capability Boundary"
      role: "baseline arbitration"
      decision_consumed: "保留 CC capability floor；Personal KB sensitivity 是 Hive-native governance，不用平台确定性缩减模型能力"
      sha256: "9b2bda91cc42a4464ec9b91c483b78fb83965fe0ff6909836ea1fecc18299e5e"
  source_baselines:
    hive_head: "29794cd39ba2af563985da03cae427cc1c46006a"
    freecode_head: "7dc15d6c8fb0c40c7fcc02ce9b58204324252632"
    codex_head: "5c19155cbd93bfa099016e7487259f61669823ff"
  conflicts_or_deltas:
    - "FreeCode/Codex 当前源码未发现 PL1/PL2/PL3/PL4 或 Hive Personal KB sensitivity/extraction 同名合同；本项是 Hive-native additive governance，不是 parity 收缩"
    - "旧代码/文档混用 internal/private/confidential/pl3/pl4；实现统一为四个 persisted enum，同时保留 legacy alias 与精确可逆来源"
  evidence_sink: "EVID-G1-006"
```

- Red（开工快照）：新 contract test 首先因 `canonicalize_sensitivity` 不存在而 ImportError；alias 对抗抽取为 `3 failed, 4 passed, 8 deselected`；service 回归证明 `confidential` 仍调用 extractor、patch 后仍保存 `private`；migration test 首先因 revision 不存在失败，真实 PG 数据仍保留非 canonical label。失败分别命中“枚举事实源分裂、抽取 blocklist 漏洞、写边界漂移、无 backfill/constraint”，不是环境噪声。
- 实现：`privacy_layer.py` 成为唯一 sensitivity owner，persisted enum 固定为 `PL1_public/PL2_pii/PL3_sensitive/PL4_credential`；所有 ingest/queue/media/failure/patch/proposal write boundary 先 canonicalize，未知新写入直接拒绝；proposal 的 inferred/declared sensitivity 只可取更高等级。`personal_knowledge_extractor.py` 与 service 共享同一 `is_sensitive_extraction_blocked()`，所有 PL3/PL4 legacy alias 与 unknown 在 external LLM/graph projection 前 fail closed，并写 `knowledge_extraction_skipped_sensitive` degraded evidence；未新增自然语言主题词 hard gate。
- migration/backfill：revision `personal_kb_sensitivity_canonical_0715` 以 `memory_context_warning_0714` 为唯一 parent；legacy alias 映射至四枚举，未知历史值保守映射 PL3；原值分别进入 document metadata/proposal reason-code recovery marker；default 改为 `PL1_public`，两表新增 validated CHECK。downgrade 精确恢复原 label/metadata，并在 upgrade/downgrade 后重新 `ENABLE + FORCE RLS`。
- Green（独立 clean worktree `/tmp/hive-kb-extract-53287e716`，HEAD=`53287e716`）：focused service contract `86 passed in 0.42s`；Personal KB 全家族 `114 passed in 7.23s`；migration/schema/startup family `27 passed in 20.52s`；此前同一 clean snapshot 全量 backend 为 `7098 passed, 2 skipped in 244.50s`。可复现命令：

```bash
cd backend
source .venv/bin/activate
pytest -q tests/services/test_personal_knowledge_sensitivity.py tests/services/test_personal_knowledge_extractor.py tests/services/test_personal_knowledge_proposal_policy.py tests/services/test_personal_knowledge_service.py
pytest -q tests/services/test_personal_knowledge_sensitivity.py tests/services/test_personal_knowledge_proposal_policy.py tests/services/test_personal_knowledge_service.py tests/services/test_personal_knowledge_extractor.py tests/api/test_agent_personal_knowledge_api.py tests/integration/test_personal_knowledge_cross_owner.py tests/integration/test_personal_knowledge_proposals.py tests/migrations/test_personal_knowledge_core_migration.py tests/tools/test_personal_knowledge_tool.py
pytest -q tests/migrations/test_personal_kb_sensitivity_migration.py tests/migrations/test_workflow_migration.py tests/deploy/test_schema_startup_gate.py
pytest -q
```
- production preflight/backfill：升级前 head=`memory_context_warning_0714`，schema-owner 视图为 `knowledge_documents internal=17`、proposal=0、两项 CHECK 不存在。升级后 head=`personal_kb_sensitivity_canonical_0715`；schema-owner 视图为 `PL1_public=17`、legacy recovery `internal=17`、proposal=0、noncanonical=0；两个 CHECK 均 `convalidated=true`，两表 `relrowsecurity=true/relforcerowsecurity=true`，document default=`'PL1_public'::character varying`。runtime `app_rls` 无 tenant context 的同一查询返回 0 行，证明未用 schema-owner 可见性冒充 runtime authority。
- commit / deploy / recovery：独立 code commit=`53287e716`。同一 Git archive 部署后 backend=`f8e6fb64-4fdd-4897-b7c6-d3b5cdcf7311`、frontend=`db6041bf-799e-412e-80d8-d12016018e58` 均 `SUCCESS`；backend-api 首次 deployment=`c5efe31c-0afb-4459-b078-1670909848a0` 在 backend migration 尚未完成时耗尽 restart 并被移除，证明 startup schema gate fail closed；待 schema ready 后从同一 archive 重试 `1e1bddbf-5d21-433b-87ff-3f6674ec4653` 为 `SUCCESS`。2026-07-15 当前 Railway latest 仍是上述三项，backend `/api/health`=200、RLS runtime role=`app_rls` strict、三个 daemon healthy、frontend=200。
- 七原子：Input=upload/import/media/patch/proposal 的显式 sensitivity；Authority=server-side canonical enum + DB CHECK，非自然语言自报权限；Execution=单 canonicalizer + write boundary + extraction gate；Evidence=canonical column、legacy recovery marker、degraded warning、migration/catalog/deploy log；Recovery=unknown fail closed、reversible downgrade、startup hold、同源重试；Consumption=PL1/PL2 可继续进入 LLM graph extraction，PL3/PL4 保留 owner 文档但不进入持久图谱，proposal 消费同一 rank；Acceptance=alias/unknown/real-PG/full-suite/production 数据、RLS、三服务与失败恢复全部 Green。
- 北极星裁决：本项限制的是“未获准敏感 bytes 进入 external extractor 与 durable graph projection”这一 data-ingress/durable-write 物理效果，不限制 owner 的模型当场读取、推理或表达；没有扫描自然语言决定真伪、重要性或正确性，也没有替换模型 final。因而符合 capability-preserving determinism，不违背 CC floor、Codex additive delta 或 Hive-native Memory/Knowledge 优势。
- 残余边界：下一 leaf 必须是 `KB-AUTH-001`，按 owner-direct、autonomous owner-agent、requester≠owner/shared/A2A/subagent、PL4 reference-only 四路径建立 fresh read decision；`KB-PROP-001` 继续负责 sensitivity/provenance 在 transcript/T0/T2/outbound 的传播；`KB-CONTRACT-001` 负责 tool description/schema/runtime 三者诚实一致。三者未因本项关闭而降级。Group 1 现为 3/16 closed、3/16 deployed-but-open、10/16 pending；103 分母、severity、owner 与 5 个 Missing 不变。

#### EVID-G1-007：KB-AUTH-001 Personal KB requester-bound authority

- `leaf_ids`：`KB-AUTH-001`；owner Group / 依赖 Group：Group 1 / Group 0，并消费 E-1/P1-004 已建立的 authenticated requester/execution-principal frame。本证据只关闭 Personal KB 当场 search/read/grant authority；`KB-PROP-001` 仍拥有 transcript/T0/T2/outbound provenance，`KB-CONTRACT-001` 仍拥有全部 Knowledge spec/schema/description 总同步，不能借本 leaf 合并清零。
- 当前状态：`in_progress-local-green`。typed authority、migration/legacy quarantine、real-PG round-trip、owner/shared/A2A/subagent/PL4/revoke 回归、grant API/UI、frontend build、独立 staged snapshot 全量 backend 与 production read-only inventory 已 Green；commit、production migration、三服务 deployment、live authority canary 与安全 rollback 尚未完成，因此 canonical 只允许 `in_progress-local-green:EVID-G1-007`。
- 冻结事实：开工 HEAD=`e912408c8b8bc64455a9bbbfd2478d87781c1f9c`；工作树进入本 leaf 前已有其它 Session 的 runtime/Hook/Session/DB 等未提交改动。本 leaf 当前 owned manifest 是 8 个 backend model/service/tool/API path、9 个既有 backend test path、2 个新增 migration/test path、4 个 frontend API/page/test path与本文证据 hunk；其它 dirty path 未 reset、覆盖、stage 或归属本项。

```yaml
context_read_receipt:
  aa_entry: "§9 Group 1 + §12.1/§12.2 KB-AUTH-001 + §12.3 Group 1"
  leaf_ids: ["KB-AUTH-001"]
  documents:
    - ref: "@docs/agent-native-atomic-review-2026-07-14.md §11 Personal KB tool-only 与 Knowledge authority 结论 / §D-KB1"
      role: "original_evidence"
      decision_consumed: "保留 owner 交互态 PL1–PL3=owned Agent + agent_searchable；autonomous/shared/cross-user/A2A/subagent 强制 explicit grant+requester/session/purpose/delegation/ceiling/expiry；PL4 只返 opaque credential reference"
      sha256: "4f1e8893fe03251c02ce9805300b94d6db00c34e032c978f3805c2b1f061e919"
    - ref: "@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md §9 Group 1 / §10 依赖顺序"
      role: "original_evidence"
      decision_consumed: "KB authority 必须消费唯一 requester/principal/delegation frame，same owner 或 creator 身份不能替代当前 requester"
      sha256: "f11ba2fcae90731d1d2a53e667b71dbe7c191006326523ac24c3231d7f1ab881"
    - ref: "@docs/agent-permission-governance-spec-2026-07-07.md §2 Principal 模型 / §4 Action 模型 / §5 Personal Knowledge Authority / §8 ToolRuntime、A2A 与 Workflow / §10 Typed failure 与恢复"
      role: "authority"
      decision_consumed: "accountable user、actor Agent 与 context principal 分离；search 不隐含 read；A2A/Workflow/visibility 不放大 Personal grant；每次 effect fresh-check 并返回 typed decision"
      sha256: "e60f2dcf8711999cf655ccae180fb52810ad2a73f265028c1c56226ba73099ac"
    - ref: "@docs/personal-company-knowledge-tool-boundary-2026-07-10.md §0 最终决策 / §3 Personal KB 读取闭环 / §6 三个视图 / §8 Permission 与 unavailable semantics / §10 回归与验收"
      role: "runtime boundary + acceptance"
      decision_consumed: "Knowledge 仍为 tool-first；search/read 在 effect boundary 重新判权；current-turn 返回 authorized result，denied/unavailable/empty 分离，filesystem 不得旁路"
      sha256: "644dd7f85c2a212d6e93101a4101607d3e58ab79a8d6f8048061c5f654305609"
    - ref: "@docs/personal-knowledge-base-spec.md §3 Authority / Content / Index / §5 读取 / §9 Permission / §12 七原子验收 / §13 不变量"
      role: "design + acceptance"
      decision_consumed: "owner/grant/sensitivity/source/session/purpose/action 取交集；SQL/content/tool/UI 共享 Personal authority service，未授权 bytes 不进入 model-visible result 或 graph/source side channel"
      sha256: "9ddf849312667e254143b8c73e0a365ba67cc631ab24f48c51a258d37872db2b"
    - ref: "@docs/personal-knowledge-base-completion-contract-2026-07-08.md §A7/A8 Runtime Tool-first / §A10 授权管理 API / §A12 授权 UI / §2026-07-14 Tool-first 重基线"
      role: "implementation history + consumer"
      decision_consumed: "复用既有 search/read、owner grant API 与 Personal Knowledge UI，但撤销 session-as-grantee、自动永久 grant 和 DELETE 硬删等旧语义"
      sha256: "7dad2c59695109d06c38e4f24cc39648c53fa66b59761c3af99b70ae57328544"
    - ref: "@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md §7.6 Personal KB / Enterprise Knowledge / §18.3 无限资源检索 / §18.4 Tool 四态"
      role: "context design"
      decision_consumed: "Personal KB 不进入 resident context；descriptor/ref 不等于 executable authority；read result 必须携带可行动 typed state，不用 prompt 容量策略代替资源判权"
      sha256: "c83a1f94b206af7de8bc44f7f4de35746c65d255574a347cbfd80ce0cc3075b7"
    - ref: "@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md §16.2 A2A 保留 authority 与 receipt / §19 Visibility 与隐私"
      role: "session evidence"
      decision_consumed: "delegator/requester/scope/delegation/session/receipt 不得在 child 或 UI 投影中丢失；denied 不伪装 empty，精确隐藏内容不删除机械 identity"
      sha256: "52a13072ef51ec1ad8f22be5f484b274880c4b7aea801104bd4ca5cdc27c0ac4"
    - ref: "@docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md §Layering Contract / §Session Permission Behavior"
      role: "CCPlus permission boundary"
      decision_consumed: "session permission mode只能决定是否询问，不能扩大 tenant/resource/credential authority；resource hard rule 在 session allow/bypass 之外持续生效"
      sha256: "db8d895b283ecb7ae747ef7fbbb76591f32aba599052d24d759dcb31c83f7cb0"
    - ref: "@docs/runtime-model-agency-constraint-audit-2026-07-13.md §5 Model Agency Boundary / §9–§11 修复与验收"
      role: "north-star guard"
      decision_consumed: "硬结果只来自 authenticated identity、resource ACL、sensitivity、expiry 与 exact schema；平台不扫描自然语言授权、不替换模型 final，failure 只返回 typed facts"
      sha256: "366c8a5e4351e76083e6096a8cca09fe93a952ce831cf01e3cae34e2f8b91530"
  source_baselines:
    hive_head: "e912408c8b8bc64455a9bbbfd2478d87781c1f9c"
    freecode_head: "7dc15d6c8fb0c40c7fcc02ce9b58204324252632"
    codex_head: "5c19155cbd93bfa099016e7487259f61669823ff"
  conflicts_or_deltas:
    - "FreeCode 的 Tool.call/canUseTool 与 resolveHookPermissionDecision 保留逐 tool allow/deny/ask，且 hook allow 不越过 deny/ask；它没有 Hive Personal KB resource authority，同名 capability 不存在，不能复制为 owner/grant 方案"
    - "Codex 的 AskForApproval/SandboxPolicy 是可取的 typed effect-control 工程增量，但不是 Personal owner/resource ACL；Hive-native KnowledgeGrant/decision 在其外层补齐 accountable principal 与 data visibility"
    - "旧 completion evidence 允许 session grantee、自动 owner-agent grant 与硬 DELETE；当前 L0/L1/authority 合同要求 session 只是 Agent grant binding、无可证意图的历史 grant quarantine、revoke 保留审计"
  evidence_sink: "EVID-G1-007"
```

- Red：① migration 文件不存在、`AgentRuntimePrincipal` 无 purpose/autonomous evidence、identity lifecycle 自动创建永久 owner grant；② typed search/read result class 缺失，tool 无法表达 denied/empty/unavailable/partial；③ grant service/API 不接受 requester/session/purpose/delegation/ceiling，frontend `GrantsPanel` 无这些 binding；④ migration 缺 resource-binding CHECK；⑤ revoked grant 仍可能参与 proposal auto-approve；⑥新增 autonomous session 契约测试按 `pytest -q tests/services/test_personal_knowledge_service.py::test_agent_grant_rejects_unbounded_or_unbound_authority -x` 正确 Red，实际错误为 `agent grantee does not belong to the tenant` 而非 service 层拒绝 `autonomous_agent grants cannot carry session_id`；⑦第一份 staged snapshot `6eeaa8853c0471418d0da85ab8ff58f5c9cad713` 全量得到 `1 failed, 7115 passed, 2 skipped`，唯一失败是 migration closure test 仍把旧 head `personal_kb_sensitivity_canonical_0715` 写死。这些失败分别命中 authority、typed evidence、DB contract、consumer、machine-contract repair 与仓级验收漂移，不是环境噪声。
- 实现（principal/decision）：`AgentRuntimePrincipal` 只从 trusted `ToolExecutionContext`、`ExecutionPrincipal`、runtime identity、session/root task 和 delegation token 组装；model arguments 不能自报 owner/requester/autonomous。`PersonalKnowledgePermissionDecision` 记录 action、owner、authority source、grant/ceiling/expiry、deny code、retryability 与 principal evidence；interactive owner 只在 requester=owner、owned Agent、非 autonomous、exact session 时走 direct，autonomous/shared/cross-user/A2A/subagent 必须匹配 owner-created unexpired Agent grant，delegated lane 还必须 exact delegation。
- 实现（逐 effect/PL4/旁路）：search permission 不能执行 read；search 与 document read 每次 fresh-check，SQL 在 title/segment bytes 离开 PostgreSQL 前同时约束 tenant、owner、agent_searchable、resource、action、requester/session/purpose/delegation、ceiling、expiry、revoke。PL4/unknown sensitivity 不返回 title/snippet/heading/source path/body，只接受 `secret://`、`credential://`、`vault://` opaque reference；缺 reference 为 typed unavailable。legacy detail/source-preview 同样 fail closed，tool handler 还有最终 byte-shape failsafe。
- 实现（grant lifecycle/API/UI）：删除 Agent identity lifecycle 的自动 grant；active Agent grant 必须 purpose+requester+expiry，interactive/delegated 必须 session，A2A/subagent 必须 delegation，autonomous 只允许 owner requester 且无 session/delegation。grant 使用 deterministic binding key，DELETE 改为 auditable soft revoke；API schema 先返回可修复 4xx，UI 暴露 grantee、requester、purpose、session、delegation、ceiling、expiry 与 active/revoked 状态，不再把 session 当 grantee。
- migration/backfill/rollback：revision `personal_kb_authority_0715` 以 `personal_kb_sensitivity_canonical_0715` 为唯一 parent；新增 requester/session/purpose/delegation/ceiling/binding/revoke 字段、索引、FK、unique 与三类 CHECK。所有无法机械证明意图的 legacy grant 保留原 metadata recovery copy，但设 `legacy_quarantined`、PL1 ceiling、stable legacy binding 与 `revoked_at`，不猜授权。downgrade 恢复旧列形状前把全部 edge 过期并写 `downgrade_quarantined`，所以 rollback 不会重开旧漏洞；upgrade/downgrade 都恢复 ENABLE+FORCE RLS。
- Green（当前工作树）：backend authority/migration/tool/service/API/model/proposal adjacent family：`125 passed in 14.32s`；同一命令内 real PostgreSQL upgrade→constraint/quarantine→downgrade→re-upgrade round-trip Green；Ruff=`All checks passed!`，20 files format-check；`alembic heads`=`personal_kb_authority_0715 (head)`。frontend API/page：`2 files / 10 tests passed`；`npm run build` exit 0，7356 modules，AgentDetail 与 shared vendor bundle budgets 均 Green。`git diff --check` exit 0。
- Green（独立 staged snapshot）：Git index 精确 24 个 owned path；`git write-tree + commit-tree + git worktree add --detach` 生成 tree=`cf5e0eac800d2a7f08fcea558df35f20ee42986d`、snapshot=`e803a2461441afbe6fcb767d249f7f01cd0320e7`，复用主仓 `.venv` 在 detached backend 执行 `pytest -q` → `7116 passed, 2 skipped in 247.77s`。第一 snapshot 暴露的旧 closure-head 测试已进入同一 Red→Green，而不是被排除；因此全量结果不依赖其它 Session 的 unstaged 工作树。
- production read-only preflight：通过 Railway Postgres public TCP proxy、schema owner 的 read-only transaction 查询；未执行 DDL/DML。当前 head=`personal_kb_sensitivity_canonical_0715`；legacy grant=`4`，覆盖 `1 tenant / 3 owners`，全部为 `agent + scope + search`、全部未过期，user/session grant=`0`；Personal documents=`17`，全部 `PL1_public + agent_searchable=true`。因此 migration 将精确 quarantine 4 条旧自动 Agent grant；owner interactive direct 不依赖这些 edge，旧 autonomous read 会按设计转为 typed deny，直到 owner 创建有 ceiling/purpose/expiry 的新 grant。
- fault/security matrix：已覆盖 owner interactive PL1–PL3、autonomous no-grant、cross-owner/no-grant、wrong requester/session/purpose/delegation、search-vs-read、PL1/PL3 ceiling、expired/revoked grant、human explicit grant、HR requester scope、nested A2A carried principal mismatch、PL4 secret bytes/reference missing、legacy detail/preview bypass、proposal auto-approve after revoke、invalid resource/grantee tenant 与 real-PG legacy round-trip。尚待 production actual-data/live-session canary 才能关闭。
- Model Agency / CCPlus 裁决：所有 hard outcome 都指向 tenant/principal/resource/action/sensitivity/expiry/delegation/credential scheme/DB constraint；未按关键词、相似度或模型正文决定权限，未删除无关工具、压缩 authorized PL1–PL3 input 或替换模型 final。owner-direct 能力保留，跨 principal 只在 bytes ingress/effect boundary 收紧；符合 CC tool agency 底线、Codex typed policy 工程增量与 Hive-native enterprise authority。
- 七原子当前状态：Input=runtime principal + resource/action；Authority=owner relation或 bounded grant + DB/RLS；Execution=tool→typed service→SQL fresh-check；Evidence=decision/tool payload/grant row/migration recovery metadata；Recovery=deny/unavailable/expiry/revoke/quarantine/re-authorize/safe downgrade；Consumption=search/read tools、owner grant API/UI、proposal policy；Acceptance=Red→125+real-PG+10 frontend+build+ruff/alembic+detached 7116。因 commit、production migration/canary 尚 open，七原子仍不得标 closed。
- production/commit 待补：记录独立 code/evidence commit；同一 Git archive 部署 backend/backend-api/frontend；验证 head/columns/CHECK/RLS、4/4 legacy 全 quarantined、active unbound=0、owner PL3 direct、shared requester deny→bounded allow、wrong session/delegation/ceiling/revoke deny、PL4 only ref、health 与 safe forward rollback。完成后原位补 commit/deployment IDs 与 canary bytes，并把 canonical/Group 计数改为 closed；任何缺项继续保持本状态。

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
- 当前 dirty worktree 的全量 frontend vitest 与三服务生产验收；backend clean-checkout 全量 suite 已有 `EVID-G0-004/EVID-G1-004`，frontend production build 已有 `EVID-G1-001/002` 当前证据；
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

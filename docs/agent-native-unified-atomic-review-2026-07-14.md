# CCPlus 统一原子化复审：Fleet、单根 Session、Context 与 Session Truth

> 状态：当前工作账本、Group 0–10 施工入口与后续修复证据总报告；不是实现完成声明
>
> 原始审查冻结快照：`main@501db6555dae374e5fcf43a6fdcfe8a3dd89343e` + 2026-07-14 当时未提交工作树；后续施工不改写这个审查锚点，每条 `EVID-*` 另记自己的开工 HEAD、工作树与生产快照
>
> 修复账本滚动更新：2026-07-17；Group 0 与 Group 4 的既有证据门保持关闭。对当前 checkout 和线上 A2A 故障截图做 wiring/path 复核后，旧的 43/103 完成声明被主动撤销：`P1-004`、`SES-CONSUMER-001`、`A2A-TERMINAL-001`、`TEAM-FANOUT-001`、`ROOT-TREE-001` 五个 leaf 因 server-side read-only 未执行、Peer A2A 仍被并入 Sub-agent、terminal outcome 未关闭 root item、Team member model 未进入 worker、Team member implementation Session 暴露到普通列表而重开。五个回归的代码、迁移、typed consumer 与测试已由 commit `b9852f37f` 进入 production；backend=`a64092a1-395b-48c2-9853-83ff9b45c2ae`、backend-api=`ab14d317-3c29-4b74-9d31-341e778f92b7`、frontend=`3ff852aa-e078-464c-80c7-7568b1272a2a` 均 `SUCCESS`。production migration actual/expected head 均为 `collaboration_runtime_closure_0717`，148-table/4-trigger readiness `issues=[]/ready=true`，backend health=`ok`、RLS=`app_rls/strict/non-superuser/non-BYPASSRLS`、三个 daemon 与 sandbox healthy，frontend HTTP 200。首次 backend-api deployment `3c50f41e-aa94-4ff2-96d6-1f518d3b4919` 在 writer migration 前按设计 fail-closed；schema ready 后从同一 commit archive 重提成功，没有放宽 readiness 或改写业务数据。由于 authenticated deny/read、真实 Team model route、terminal/root reconciliation 与三类协作 browser canary 尚未执行，五个 leaf 当前统一记为 `in_progress-deployed-pending-canary`，业务分母仍为 **38/103 closed**，不是 43/103；本轮未新增 canonical ID，也未把“部署成功”冒充行为验收完成。Group 4 exact-source commit=`4e385d423` 的既有 return-storm/recovery 证据仍记录在 `EVID-G4-*`。2026-07-15T13:37Z 因 production `backend-volume` 在重启批次中从约 24.8 GB 急升到 28.65 GB，Group 1 曾显式暂停；`EVID-G8-PRE-001/002/003` 已分别关闭继续写放大、transaction lifecycle 与当次容量事故处置，核心数据停止门成立。B-01 发布后同挂载点 `df -B1 /data/agents` 为 used=`11,360,583,680` bytes（24%），未执行新的清理或核心数据删除。三个 Group 8 前置证据仍不关闭任何 Group 8 leaf 或 `MISS-RETENTION-001`；Object Storage、snapshot CAS、sealed T0 archive、T2 authority/replay 与跨资产 retention 仍未完成。
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

<!-- group-context-route-inventory-start -->
- root=1
- local=81
- external=8
- total=90
<!-- group-context-route-inventory-end -->

当前 `§9` 的完整 Group 路由共有 **90 个去重后的可执行文档入口**：仓库根 `@AGENTS.md` 1 份、本仓 `@docs` 81 份、固定 Hive Connect snapshot 8 份。这个数字是路由完整性快照，不是要求每个 leaf 一次加载 90 份文档：执行者只读取本 Group 的 `@必须先读`，再按实际触及的子域展开 `@按需读取`；实际消费必须写进 `Context Read Receipt`。本仓入口必须全部 Git-tracked，跨仓入口必须通过 §0.3 的 commit + SHA-256 registry；`backend/tests/architecture/test_agent_native_repair_ledger.py` 负责验证 11/11 Group、路径可读性、唯一 owner 与证据往返。

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
| §7.1 Memory | 6 | 8 | CC 式 bounded index 常驻、LLM 每轮最多选 5 条有界 excerpt、全文按需 load、source refs、selector unavailable typed degrade |
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
- CTX-A | Group 6 | T2/T3 Memory 不全量常驻；允许 CC 式 bounded index + LLM 每轮最多 5 条有界 excerpt，完整 body 继续按需 load
- CTX-B | Group 6 | 8% 仅为 256K resident review center，不是硬配额或填充目标
- CTX-C | Group 6 | 暂不新增统一 public context_search/context_load；统一内部合同，保留领域工具
- CTX-D | Group 6 | 后台只生成 descriptor/排序观察；正文必须由 LLM 从 authorized manifest 选择，并受 5 条、4KiB/200 行、20KiB/turn、60KiB/Session 自动披露上限约束
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
| §23 G1–G30 | 0 | 2–9 | 全部黄金轨迹必须变成自动化验收，不得挑选 happy path；同一场景可有前置 substrate，但最终只有一个验收 owner |
| §24–§25 | 0 | 2–9 | unit/contract/integration/browser/byte snapshot/production gate 与精确文件边界 |
| §26–§29 | 2 | 0、9、10 | S-01–S-30 ADR、最终体验、当前状态和源码参考必须随修复证据更新 |

#### Session S-01–S-30 owner map

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
- S-13 | Group 2 | Run、Turn、Transport、Projection Sync 四状态正交，连接状态不改写执行结果
- S-14 | Group 2 | 人类输入使用显式 intent、command acceptance、Hook admission 与 exactly-once receipt
- S-15 | Group 2 | 运行中输入进入 durable mailbox 并有唯一 terminal settlement
- S-16 | Group 9 | Evaluation Feedback 独立于 Conversation/Control，并由完整 API/UI 消费
- S-17 | Group 2 | ModelResultSeal 只结束 Round，RunOutcomeSeal 与 obligations 决定 terminal
- S-18 | Group 2 | observability/projection 是隔离 derived sink，不得冒充模型 failure
- S-19 | Group 2 | `session.ready` 与 highest-contiguous cursor 构成恢复协议
- S-20 | Group 2 | Stop 使用 accepted/cancelling/terminal typed receipt
- S-21 | Group 9 | terminal history 只做 event merge，禁止整数组替换
- S-22 | Group 9 | migration/backfill/V1 cleanup 与 production cutover 由一次 release graph 终验
- S-23 | Group 2 | Event Kind Matrix 是唯一协议词表
- S-24 | Group 2 | command registry 是外部 mutation 的唯一幂等权威
- S-25 | Group 2 | Turn 聚合 immutable Run attempts，retry 不改写旧 attempt
- S-26 | Group 2 | stop-and-replace 使用 durable saga、deterministic child cancel 与 execution fence
- S-27 | Group 9 | Feedback 使用 immutable mutation 与 CAS aggregate，完整产品面不借 Session prose 实现
- S-28 | Group 2 | result、obligation/assembly 与 outcome aggregate 分离恢复 Round/Run
- S-29 | Group 9 | connection generation 以 client/view 为边界，支持多标签页并存
- S-30 | Group 9 | per-Run writer generation 与 DB epoch 完成最终 V1 writer 退出和回滚演练
<!-- session-decision-map-end -->

#### Session G1–G30 黄金轨迹 owner map

<!-- session-golden-map-start -->
- SESSION-G1 | Group 2 | 基础模型—工具循环
- SESSION-G2 | Group 6 | 多次工具与动态压缩
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
- SESSION-G14 | Group 9 | 首次连接不是重连；Group 2 提供 ready/generation substrate
- SESSION-G15 | Group 2 | 模型已输出后 trace sidecar 失败不污染 semantic outcome
- SESSION-G16 | Group 2 | steer、queue-next 与 stop/replace 竞态
- SESSION-G17 | Group 2 | terminal transaction 超时与进程重启
- SESSION-G18 | Group 2 | sequence 1/3/2、gap 补齐与重复投递
- SESSION-G19 | Group 2 | socket accepted 但 bootstrap 失败仍保持 typed transport state
- SESSION-G20 | Group 2 | Stop accepted/rejected/ACK 丢失按 receipt 收敛
- SESSION-G21 | Group 9 | Evaluation Feedback 与继续对话分平面并完整产品消费
- SESSION-G22 | Group 2 | Provider request 发出、stream-start receipt 前崩溃进入可恢复待核对
- SESSION-G23 | Group 2 | cancel fence 后、settlement 前崩溃幂等恢复
- SESSION-G24 | Group 2 | replacement cancel 与新 Turn admission 间崩溃不丢输入
- SESSION-G25 | Group 9 | feedback revision race 由 immutable mutation/CAS aggregate 收敛
- SESSION-G26 | Group 9 | 多标签页与 React StrictMode 各自持有 connection generation
- SESSION-G27 | Group 2 | 并发 sequence allocation 与 outbox 事务守恒
- SESSION-G28 | Group 2 | idempotency key 绑定 namespace/payload/kind/target
- SESSION-G29 | Group 2 | durable stream DB failure 与 backpressure exhaustion 保留已提交 bytes
- SESSION-G30 | Group 9 | runtime artifact 权限污染 repair/rollback 与最终 writer cleanup artifact
<!-- session-golden-map-end -->

## 9. 最终一次性修复顺序

下面是依赖顺序，不是把 103 个 leaf 绑成一个发布列车。每个开工 leaf/同根家族必须一次完成 Red→Green、migration/backfill、fault injection、observability、recovery/rollback、真实消费与发布验收。P0/P1 自身闭环后立即独立发布。

> **本轮执行序（2026-07-17 重排）**：下表是**正确性依赖序**（谁不能先于谁闭环），**不是本轮施工先后**。Group 1–4（后端事实底座）已闭环并上生产，但用户可见的痛点全部押在未完成的 Group 6/7/9——写路径已 v2 化而读路径/前端收口未跟上，两平面断裂（首屏 REST 仍走 v1 → 白屏即此因）。本轮改为**用户痛点垂直切片序**，优先交付三个 P0 止血切片，唯一执行入口见 `@docs/p0-session-memory-a2a-repair-sequence-2026-07-17.md`：
>
> - **P0-1 Memory 不爆** → Group 6 子集：已实装 CC 式“bounded index 常驻 + LLM 每轮最多 5 条有界 excerpt + 全文 `search/load` + 60KiB Session ledger”，当前为 `in_progress-local-green:EVID-G6-001`；不再等到 `_MAX_SYSTEM_PROMPT_BUDGET` 才整轮失败，但完整 token-native admission 与 production canary 仍属 Group 6。
> - **P0-2 Session 能看见** → Group 9 前端收口子集：首屏 REST 走 `schema_version=2` canonical、前端只经 `SessionEventStore` 一次归约、删 load-earlier 可见性边界、固定 Codex 呈现序（Thinking→Text→Tool→Final）。
> - **P0-3 A2A 三层各自跑通** → 当前回归分别回填 Group 1/2/3：① server-side read-only authority，② Sub-agent / Agent Team / Peer A2A typed consumer 三分，③ terminal root、Team model 与 hidden member Session；Group 7 只继续拥有跨渠道 route/result/delivery Missing，不得吞掉当前单渠道 Peer A2A 故障。
>
> 三切片各挂 native 回归门（改前能跑/能看/能用的改后必须仍能）；证据仍回填到对应 Group 的 `EVID-*`，不另造账本。Group 5/10 等无对应痛点的纯工程深化本轮暂缓。

| Group | owner canonical leaf | owner Missing | 当前状态 |
|---:|---:|---:|---|
| 0 | 0（全局门） | 0 | closed：`EVID-G0-002/003/004/005/006`，Git truth、机器账本、11 个上下文包/90 个 `@` 文档入口、跨仓快照与 clean-checkout harness 已闭环 |
| 1 | 16 | 0 | in progress：15/16 保持 closed；`P1-004` 已同源发布，尚待 authenticated production deny/read canary |
| 2 | 14 | 0 | in progress：13/14 保持 closed；`SES-CONSUMER-001` 已同源发布且 full/build 完成，尚待真实 browser collaboration canary |
| 3 | 7 | 0 | in progress：4/7 保持 closed；`A2A-TERMINAL-001`、`TEAM-FANOUT-001`、`ROOT-TREE-001` 的 migration/apply/deploy 已完成，尚待 production model-route/reconciliation canary |
| 4 | 6 | 0 | closed：6/6 owner leaf 已由 `EVID-G4-001`–`EVID-G4-006` 独立关闭；immutable result object、ref-only outbox、mailbox sequence/CAS、lease/claim、integration epoch/page、governed reader 与 100-way return-storm recovery 已部署 |
| 5 | 2 | 0 | open |
| 6 | 10 | 0 | in progress：`XCB-MEM-001` 已本地 Green（`EVID-G6-001`），仍待 production canary；其余 9 leaf open |
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

**退出门**：§12 owner map 证明 103/103 唯一归属；5/5 Missing 唯一归属；CTX-A–F、S-01–S-30、SESSION-G1–G30 无遗漏；文档路径存在；CI 可复算；任何 Group 的证据能按 §0.2 回填。证据写入 `EVID-G0-*`。

### Group 1：真实安全、principal、authority 与 fail-open

**Owner leaf（16）**：`P0-F1`、`P0-F2`、`E-1`、`P1-004`、`P1-F4`、`KB-AUTH-001`、`KB-EXTRACT-001`、`KB-PROP-001`、`AUDIT-IMM-001`、`AUDIT-TENANT-001`、`F-PLAINTEXT`、`P2-F8`、`P2-F6`、`KB-CONTRACT-001`、`B-01`、`BUD-ROOT-001`。

**当前回归状态（2026-07-17）**：15/16 保持 closed；`P1-004` 因 `delegation_run` 只在 DTO/UI 声明 read-only、server mutation 仍可执行而重开。`EVID-G1-017` 已完成 exact `session_kind` mutation gate、manager 不可绕过、live API wiring、三服务同源部署与 schema/health 验收；authenticated production deny/read canary 前仍是 `in_progress-deployed-pending-canary`。

**依赖 Group**：Group 0。P0/P1 家族自身闭环后立即发布，不等待 Group 2–10。

**AA 开工入口**：本文 `§12.1` 的 16 个 Group 1 owner 行、`§12.2` 对应 canonical 行、`§12.3 EVID-G1-*` 和 `§12.4` 下由该索引列出的全部当前证据记录；每次只开一个 leaf/同根安全家族，不把 Group 1 当成单个巨型改动，也不在本入口硬编码易漂移的证据编号清单。

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

**@按需读取**：`@docs/personal-knowledge-base-spec.md`、`@docs/personal-knowledge-base-capability-rebaseline-2026-07-09.md`、`@docs/ccplus-governance-code-repair-plan-2026-06-28.md`、`@docs/ccplus-governance-truth-search-repair-plan-2026-06-28.md`、`@docs/runtime-budget-control-plane-plan-2026-07-03.md`。

**源码入口**：先用 graph 查 egress/web fetch、database startup/migration/RLS、principal/delegation frame、tool governance、runtime budget、Personal KB access/proposal/extraction；再读 exact live path。

**首个 Red**：分别复现 SSRF/redirect/DNS rebinding、缺失迁移仍启动、creator/requester 置换、cross-principal PKB 无 grant、audit 可改/静默丢弃、credential 明文与 budget authority fail-open；禁止用一个大测试掩盖多个独立安全 seam。

**证据回填**：为当前 leaf 创建/更新 `§12.4 EVID-G1-*`，在 `§12.2` 只更新被该证据覆盖的 canonical 行，并同步 `§12.3` 的 local/commit/deploy/canary 状态；P0/P1 独立发布证据不能等到整组完成后补写。

**退出门**：SSRF/redirect/DNS rebinding 与 sandbox egress 为零泄漏；schema/RLS fail-closed；唯一 requester/principal/delegation贯穿 inner effect、RecoveryManifest、PKB、audit 和 receipt；credential 不明文；budget service failure 只能缩小 work-amplification，不能伪造授权或冻结无关 direct answer。证据写入 `EVID-G1-*`。

### Group 2：Session 机械事实语言

**Owner leaf（14）**：`G-01A`、`A-01`、`A-04`、`B-02`、`B-03`、`G-01B`、`B-04`、`D-KB4`、`SES-ACCEPT-001`、`SES-ITEM-001`、`SES-PROJECTION-001`、`SES-PROSE-001`、`SES-TRANSPORT-001`、`SES-CONSUMER-001`。

**当前回归状态（2026-07-17）**：13/14 保持 closed；`SES-CONSUMER-001` 因 backend 把 Peer A2A `delegation` 投到 `subagents`、frontend `timelineModel` 也没有 `peer_a2a` typed section 而重开。`EVID-G2-015` 已完成 backend section/envelope、三类 canonical ThreadItem、frontend reducer/timeline/right rail、frontend full/build 与三服务同源部署；真实 browser collaboration canary 完成前仍为 `in_progress-deployed-pending-canary`，不得标 closed。

**依赖 Group**：Group 0、Group 1。Session envelope 必须携带 Group 1 收敛后的 principal/authority，不得先建一个无可信身份的第二事实语言。

**AA 开工入口**：本文 `§3.5` Session truth 降级链、`§8.1` Session S/G owner map、`§12.1` 的 14 个 Group 2 owner 行、`§12.2` canonical 行与 `§12.3 EVID-G2-*`。

**@原始断点证据**：

- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md` §8：当前 Session 六个 seam 与旧完成声明失效原因，是本 Group 的直接问题定义。
- `@docs/agent-native-atomic-review-2026-07-14.md` §10、§13、§15–§18、§22：平台 prose、typed outcome、consumer 与历史 UI/Knowledge 断点。
- `@docs/agent-native-atomic-review-501db655.md` §13 [P1-007]/[P2-013]、§15–§18、§22：failure prose 与字符串反推 machine outcome 的原始证据。
- `@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md` §5.2、§6–§7、§10：pressure/terminal 与输出失败感知证据。

**@必须先读**：

- `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`（全文，尤其 §9–§14、§18–§24、S-01–S-30、G1–G30）
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

**退出门**：accepted input 同事务成为 canonical event；stable item/lifecycle/ordinal；typed denied/unavailable/approval/retryable；persist-before-publish；live/history/reconnect/reload/resume 同 reducer；平台不以 assistant prose 冒充模型；final 除 exact secret redaction 外 byte-faithful。SESSION-G1/G3/G4/G6/G7 以及 G5 的 Group 2 event/reducer/transport fixture 必须通过；G5 的真实浏览器/长时重连终验仍由 Group 9 唯一拥有。Group 2 同时验收其新增机械场景 G15–G20、G22–G24、G27–G29；这不越权关闭 G2/G5/G14/G21/G25/G26/G30 的最终 owner。证据写入 `EVID-G2-*`。

### Group 3：Root admission、预算与终态

**Owner leaf（7）**：`A2A-ADMISSION-001`、`SUBAGENT-ADMISSION-001`、`A2A-CYCLE-001`、`A2A-TERMINAL-001`、`TEAM-FANOUT-001`、`SUBAGENT-APPROVAL-001`、`ROOT-TREE-001`。

**当前状态（2026-07-17）**：4/7 保持 closed；`A2A-TERMINAL-001`、`TEAM-FANOUT-001`、`ROOT-TREE-001` 因 terminal outcome/root item 漂移、Team model 配置未被 worker 消费、Team implementation Session 暴露到普通列表而重开。`EVID-G3-008` 已完成同事务 root terminal、Team model authority、hidden surface、历史 backfill、production migration apply 与三服务同源部署；真实 Team model route、terminal/root reconciliation 与 Session surface canary 前仍为 `in_progress-deployed-pending-canary`。Group 4 仍唯一拥有 durable result payload、mailbox、integration epoch 与 100-way return storm，不能由本 Group越权关闭。

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

**当前状态（2026-07-17）**：`closed`，6/6 owner leaf 已由 `EVID-G4-001`–`EVID-G4-006` 关闭。完整结果只写 immutable `runtime_result_objects`；`runtime_notification_outbox`、integration manifest 与 parent wake 只携 hash-pinned ref/size/sequence；`runtime_result_mailbox_cursors` 和 `runtime_result_integration_pages` 提供 CAS、claim token、lease、epoch/page、coverage 与 retry/recovery。100×1 MiB synthetic return storm 形成 4 个 25-ref page，不把 child bytes 线性回灌 parent Prompt。

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

**滚动状态（2026-07-17）**：`XCB-MEM-001` 的 P0 自动披露切片已接入 live `invoker -> memory_service -> retriever/assembler -> provider suffix` 并通过定向回归，记为 `in_progress-local-green:EVID-G6-001`。该状态不代表 Group 6 关闭，也不代表生产已验收；其余 9 leaf、三服务 exact-source deploy、真实长 Session/provider token 曲线仍须独立补齐。

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

**A2A 三层验收对象（2026-07-17 修正）**：A2A 不是"两条路"，是三层，必须拆成三个独立验收对象，禁止 ② 和 ③ 一锅端（依据 `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md` §16）：**①** sub-agent（`spawn_subagent`，`source="subagent"`）；**②** agent team（同一 lead Agent 的具名 teammate，`spawn_agent_team_member_runtime`）；**③** Peer A2A（跨 `agent_id`，`orchestrator.delegate_async`，`source="agent"`，principal/depth/cycle/budget + read-only `delegation_run`）。② 与 ③ 只在内核执行层汇合，编排、身份、Session 和产品消费必须分开。本轮重新坐实的 server read-only、typed consumer、terminal root、Team model 与 hidden Session 五个 seam 分别由 Group 1/2/3 的 `EVID-G1-017`、`EVID-G2-015`、`EVID-G3-008` 修复；它们不是 Group 7 的 owner。Group 7 只继续建设同一 root 跨钉钉/飞书/Slack/Web 的 route/result/destination delivery、逐 hop authority 与 channel fairness；不得用跨渠道 Missing 掩盖当前 Peer A2A，也不得用当前单渠道修复冒充跨渠道已完成。

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
- `@docs/backend-volume-storage-lifecycle-design-2026-07-15.md`（production inventory、冷热分层、transaction/T2/snapshot/T0 lifecycle、dry-run/quarantine/restore/sweep、核心数据停止边界与 `EVID-G8-PRE-001/002/003` 证据）
- `@docs/self-evolution-sota-plan.md`
- `@docs/t0-append-only-session-ledger-redesign-2026-06-18.md`
- `@docs/company-knowledge-base-spec-2026-07-07.md`
- `@docs/knowledge-pyramid-agent-person-org-2026-07-03.md`
- `@docs/personal-company-knowledge-tool-boundary-2026-07-10.md`
- `@docs/knowledge-substrate-plugin-architecture-2026-07-09.md`

**@按需读取**：`@docs/personal-knowledge-base-spec.md`、`@docs/personal-knowledge-base-implementation-plan-2026-07-07.md`、`@docs/personal-knowledge-base-completion-contract-2026-07-08.md`、`@docs/subagent-evolution-loop.md`、`@docs/eval-system-spec.md`。

**源码入口**：terminal hook/T2 job/outbox、T0 projection/hash verifier、T2/T3 write authority/locks、capability factor consumers、Memory availability gates、Knowledge ACL/index/retention/audit。

**首个 Red**：在 terminal commit 后注入 T2 provider outage、worker crash/restart、dead-letter/requeue、T0 hash tamper、并发 T3 write、rolling deploy 期间旧实例长期持有 Agent asset lock、Knowledge ACL revoke 与 retention/legal hold；证明 terminal 被阻塞、证据不可验、锁外写、无 timeout 的新实例 startup 等待、永久 held 或跨资产删除不守恒。`EVID-G1-010/011/012/013/014` 已连续五次记录 production backend 在 `startup: push default skills to every existing agent across tenants` 后长停顿；第三、四、五次都从该日志到 daemon ready 等待约 203 秒，对应 health 的 `event_loop.max_lag_ms` 依次为 `203831.58`、`197663.07`、`198063.26`。第五次还出现 backend 启动容器后约 126 秒才进入 entrypoint、backend-api 在 migration ready 前耗尽 10 次 readiness 重试且必须从同一 exact archive 重提的恢复缺口。`EVID-G8-PRE-001` 已坐实并关闭 exact-match write-amplification；`EVID-G8-PRE-002` 完成 transaction lifecycle/backfill；`EVID-G8-PRE-003` 完成事故期 exact restore/sweep 和机械可重建副本收敛，并把 owner 的“核心数据不删、可优化才优化”固化为停止门。这些只关闭继续增长、历史 transaction 无生命周期与当次容量事故处置三个子 seam。旧 journal O(n) recovery scan、blocking `flock`、schema-wait recovery、T2 authority/replay、snapshot CAS、sealed T0 archive、常态 trace/cache、Object Storage 和跨资产 retention/legal hold 仍必须由 Group 8 fault injection 建立 bounded typed recovery。`EVID-G1-011` 捕获的 `MemoryEnhancementSyncResult` 缺少 `skipped` warning 与 health `last_error=null` 仍并入 `F-OBS1`，不新增第 104 个 leaf，也不得被三个前置子闭环掩盖。

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
- P1 | E-1 | closed:EVID-G1-003 | durable requester authority commit `3b3b281543bc` 已部署；production actual-data + exact-code spawn/wake canary、creator drift hold、1,499 条 legacy `retain_needs_reconciliation` disposition 与零副作用 rollback drill 已绿
- P1 | P1-004 | in_progress-deployed-pending-canary:EVID-G1-017 | 旧 typed A2A authority frame 保留；`delegation_run` server-side read-only gate 已随 `b9852f37f` 部署，待 authenticated production deny/read canary
- P1 | P1-F4 | closed:EVID-G1-005 | signed authority frame commit `67a0bcdcb` 已部署；54/54 legacy exact-byte quarantine、direct/restart/compact/nested A2A/foreign authority、metrics、replay no-duplicate-effect 与 tamper/rollback canary 全绿
- P1 | C-BP1 | inherited-current-evidence | terminal hook 同步 T2 LLM 阻塞完成
- P1 | P1-008 | inherited-current-evidence | Memory dependency failure 冻结无关 effect
- P1 | P1-017 | inherited-dirty-fix-unaccepted | transcript commit 与 T0 wake 可见性
- P1 | G-01A | closed:EVID-G2-001 | canonical assistant/final 只引用模型 source blocks；平台 failure/denial 保持 typed item，不再冒充模型 author
- P1 | KB-AUTH-001 | closed:EVID-G1-007 | commit `637a56395` 的 requester/session/purpose/delegation-bound authority、PL4 reference-only、legacy quarantine、三服务部署与 production transaction canary 已全绿
- P1 | KB-EXTRACT-001 | closed:EVID-G1-006 | canonical sensitivity enum、全部写边界、PL3/PL4 extraction gate、可逆 backfill、DB constraint 与 production canary 已绿
- P1 | KB-PROP-001 | closed:EVID-G1-008 | commit `62e262c11` 将 trusted Knowledge sensitivity/provenance 贯穿 transcript、T0、T2 eligibility、direct/outbox channel effect 与 nested subagent return；append-only legacy dry-run、三服务部署和 production canary 已绿
- P2 | A-01 | closed:EVID-G2-002 | terminal/failure 由 result/outcome aggregate 与 exact protocol state 决定，不扫描模型正文前缀
- P2 | A-03 | inherited-current-evidence | compaction active projection/replay 边界漂移
- P2 | A-04 | closed:EVID-G2-003 | transport/cancel 使用 durable control receipt、ready/cursor 与 typed retry/reconciliation，不从 Redis/phase exception 猜终态
- P2 | C-BP2 | inherited-current-evidence | CORE_DAEMON 默认关闭隐藏自进化车道
- P2 | C-BP3 | inherited-current-evidence | T2 retry 耗尽后永久 held
- P2 | C-BP4 | inherited-current-evidence | T3 profile 锁外直写
- P2 | C-BP5 | inherited-current-evidence | T0 hash chain 只写不验
- P2 | C-BP6 | inherited-current-evidence | capability 三表无真实回读消费者
- P2 | F-OBS1 | inherited-current-evidence | T0 health 保留陈旧 last_error
- P2 | B-02 | closed:EVID-G2-004 | denied/unavailable/approval-required/retryable 是独立 typed outcome 并保留 matching invocation/result identity
- P2 | B-03 | closed:EVID-G2-005 | governance outcome 来自 permission/control/tool receipt 与 authority snapshot，不再从平台 prose 反推
- P2 | E-2 | closed:EVID-G4-001 | A2A/Hive Connect completion 先提交 immutable result + durable outbox，再由 typed continuation 唤醒原 parent Session；artifact/result refs 与原 authority identity 可恢复
- P2 | AUDIT-IMM-001 | closed:EVID-G1-010 | commit `94e3ecf58/c0e1108a6` 在数据库层禁止两张 canonical audit 表 UPDATE/DELETE/TRUNCATE，外部 principal provenance 改为 RESTRICT；真实 PG、clean-checkout 全量、三服务部署与 production 零残留事务 canary 已绿
- P2 | AUDIT-TENANT-001 | closed:EVID-G1-011 | commit `09c3823a0` 将 tenantless security event 路由到不可变 operator audit plane，返回 typed receipt；真实 PG/app_rls、clean-checkout 全量、成功认证 audit-failure fail-closed、三服务部署与 production append-only canary 已绿
- P2 | F-PLAINTEXT | closed:EVID-G1-012 | commit `8570efdad` 对全部非空 `AgentTool.config` 做版本化认证信封加密、透明运行时解密、API 结构化遮罩与 secure migration；真实 PG、clean-checkout 全量、三服务 exact-source 部署和 production 706/706 encrypted、0 plaintext inventory 已绿
- P2 | P2-F8 | closed:EVID-G1-013 | commit `6776c3d12` 在 model-authored search pattern 前加入 `rg --` machine-contract boundary；CC/Codex 源码对照、TDD、623 tools tests、clean-checkout 全量、三服务部署、live hash 与 production `--files/--pre` literal canary 已绿
- P2 | P2-F6 | closed:EVID-G1-014 | 原报告“Agent API 缺校验”经 current-source 复核被纠偏；commit `32778e239/4bae5e0e3` 把 Agent、AI Asset rollback、Role Template 与 DB composite FK 收敛到 tenant-owned enabled model authority，真实 PG、clean-checkout 全量、三服务 exact-source 部署、live hash 与 production constraint/inventory canary 已绿
- P2 | KB-CONTRACT-001 | closed:EVID-G1-015 | 三份 canonical Knowledge spec 共享同一 read-authority matrix，architecture gate 与 current tool metadata/runtime 对齐；owner-direct PL1–PL3、grant-required lanes 与 PL4 语义不再冲突
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
- P2 | G-01B | closed:EVID-G2-006 | canonical reducer 只消费 typed kind/lifecycle/status；字符串 `includes(...)` 不再决定 hard state
- P3 | B-01 | closed:EVID-G1-016 | commit `b805dd67e` 将 HR provisioning 绑定到 authenticated confirmation、immutable blueprint digest 与 RuntimeTask authority snapshot；全量回归、迁移、三服务 exact-source、production trigger/RLS/inventory/tamper canary 全绿
- P3 | A-05 | inherited-recheck | 旧报告单 Agent leaf A-05
- P3 | A-06 | inherited-recheck | 旧报告单 Agent leaf A-06
- P3 | A-07 | inherited-recheck | 旧报告单 Agent leaf A-07
- P3 | A-08 | inherited-recheck | 旧报告单 Agent leaf A-08
- P3 | B-04 | closed:EVID-G2-007 | result/finality 由 mechanical seal/outcome 决定；自然语言 failure 词不改变 execution state 或模型 bytes
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
- P3 | D-KB4 | closed:EVID-G2-008 | Knowledge/tool failure 通过通用 typed denied/unavailable/not-found result envelope 进入 Session，不合并成自由文本 warning
- P1 | XCB-CTX-001 | inherited-current-evidence | pre-model 20% Prompt hard cap
- P1 | XCB-CAP-001 | inherited-current-evidence | capability catalog 无 progressive wave/cursor
- P1 | XCB-MEM-001 | in_progress-local-green:EVID-G6-001 | CC 式 bounded index + LLM 最多 5 条有界 excerpt + 20KiB/turn + 60KiB/Session 已接 live path；production canary 与通用 Context Resource Plane 仍待完成
- P1 | XCB-OUT-001 | inherited-current-evidence | output continuation 固定三次后假 final
- P1 | XCB-LIM-001 | inherited-current-evidence | tool-round cliff/平台终答/预算假接线
- P1 | XCB-RESULT-001 | closed:EVID-G4-002 | 完整 result bytes 仅驻留 immutable `runtime_result_objects`；outbox/page/prompt 只保留 hash-pinned ref、size 与机械路由事实，旧 ref 可继续读取
- P2 | XCB-MCP-001 | inherited-current-evidence | MCP execution-time schema/auth fresh-check 缺失
- P1 | XCB-OBS-001 | inherited-current-evidence | stream/parts 无界且 pressure observation 缺失
- P1 | CONC-FANIN-001 | closed:EVID-G4-003 | 100×1 MiB child result 以 4 个 25-ref integration page 汇入 parent；完整 bytes 可 governed read，prompt resident context 不随 raw payload 线性增长
- P1 | CONC-WAKE-002 | closed:EVID-G4-004 | per-child intent 按 parent mailbox sequence 聚合为 durable integration epoch/page；前序 page fence、claim token 与 lease 阻止乱序/重复 wake
- P1 | A2A-ADMISSION-001 | closed:EVID-G3-001 | A2A 在 coordination signal/wakeup 前持久化 budget admission、RuntimeTask、root item 与 exact recovery identity；lease expiry 复用原 signal，不再产生 ghost delegation
- P1 | SUBAGENT-ADMISSION-001 | closed:EVID-G3-002 | Subagent 先落 durable task/root admission 再投影 child session；approval wait 不唤醒 worker，projection crash 可从原 task 恢复
- P1 | A2A-CYCLE-001 | closed:EVID-G3-003 | delegation path/cycle 成为 durable root item 事实；restart 后仍按同一路径拒绝 cycle，拒绝态不产生 child effect
- P1 | A2A-TERMINAL-001 | in_progress-deployed-pending-canary:EVID-G3-008 | `SessionRunOutcome` terminal transaction 同事务关闭 root item；additive backfill 已生产 apply，待 production reconciliation canary
- P1 | CHANNEL-FAIRNESS-001 | reclassified-plane-current | channel ingress/delivery 全局 FIFO
- P1 | TEAM-FANOUT-001 | in_progress-deployed-pending-canary:EVID-G3-008 | requested/admission 守恒保持；tenant-bound member model、worker override 与 hidden Session 已部署，待真实 model-route/历史 surface canary
- P1 | WF-HARDLIMIT-001 | inherited-current-evidence | Workflow 固定方便性上限 hard fail
- P1 | WF-PARTIAL-001 | closed:EVID-G4-005 | Workflow/Team/Subagent/A2A 共用 typed result status/ref 与 partial/late/duplicate/revision contract；terminal coverage 可重算且 late result 不伪造 root terminal
- P1 | BUD-BREAKER-001 | inherited-current-evidence | runtime breaker 机械终止/cancel
- P1 | BUD-ROOT-001 | closed:EVID-G1-009 | commit `3c1998607` 建立 typed budget-root unavailable contract：交互回合保留 direct reasoning/answer、work-amplification 双层禁用，后台/自治入口 fail-closed；三服务部署、production exact-code canary、指标、零 legacy active task inventory 与 health 已绿
- P1 | SUBAGENT-APPROVAL-001 | closed:EVID-G3-006 | foreground approval 绑定 exact RuntimeTask/root item/approval ref；approve/reject 幂等推进同一 intent，等待期不执行
- P1 | CONC-MAILBOX-001 | closed:EVID-G4-006 | parent mailbox 从 RuntimeTask JSON 改为独立 cursor/outbox/page rows；唯一 sequence、CAS version、claim token、lease 与 stale-ack fence 关闭 lost update
- P1 | ROOT-TREE-001 | in_progress-deployed-pending-canary:EVID-G3-008 | mixed-runtime root identity 保持；terminal/root transaction 与历史 backfill 已部署，待 production coverage 重算与幂等 canary
- P1 | FLEET-SCHED-001 | added-current-confirmed | RuntimeTask 全局 priority/FIFO 无 tenant/root fairness
- P2 | FLEET-TRIGGER-001 | added-current-confirmed | trigger daemon 全量 O(N) scan 无 page/shard/cursor
- P2 | SES-ACCEPT-001 | closed:EVID-G2-009 | accepted input、command、canonical event 与 outbox 在 admission transaction 中成立，worker 只 claim/apply
- P1 | SES-ITEM-001 | closed:EVID-G2-010 | stable item/block/result identity、lifecycle、ordinal、render owner 与 source refs 贯穿 stream/replay
- P1 | SES-PROJECTION-001 | closed:EVID-G2-011 | user/live projection 精确 redaction content 但保留 item、parent、invocation、result 与 sequence identity
- P2 | SES-PROSE-001 | closed:EVID-G2-012 | unknown/summary/private/final phase 保持 typed；平台不生成固定 reasoning/final prose
- P2 | SES-TRANSPORT-001 | closed:EVID-G2-013 | persist-before-publish、transactional outbox、ready/highest-contiguous cursor 与 gap recovery 共用 canonical envelope
- P1 | SES-CONSUMER-001 | in_progress-deployed-pending-canary:EVID-G2-015 | backend envelope、canonical ThreadItem、frontend reducer/timeline/right rail 已将 Sub-agent、Agent Team、Peer A2A 三分并同源部署；仍待 browser collaboration canary
<!-- canonical-ledger-end -->

### 12.3 Group 修复证据索引

本节是后续施工证据的唯一目录，不是测试结果占位符。每次修复必须先创建稳定的 `EVID-G<group>-<序号>` 记录，再把同一证据 ID 回填到对应 canonical leaf 或 Missing；一个证据可以覆盖同根家族的多个 leaf，但不能因此合并它们的独立状态。Group 标绿前，索引、leaf 状态、测试结果、迁移状态、部署状态与实际 consumer 必须一致。

<!-- group-evidence-index-start -->
| Group | 证据前缀 | Owner 范围 | 当前证据状态 | 下一次写入要求 |
|---:|---|---|---|---|
| 0 | `EVID-G0-*` | 0 leaf / 0 Missing | `closed`：`EVID-G0-001/002/003/004/005/006`；文档 Git truth、owner/path/decision/scenario CI、11 个 Group 上下文包、90 个去重 `@` 文档入口、跨仓快照与 fake-provider/PG/Redis harness 基座成立 | 后续任何新增本地 `@docs` 必须先进入 Git 并同步上下文包索引；业务场景 Green 仍由 owner Group 负责 |
| 1 | `EVID-G1-*` | 16 leaf / 0 Missing | `in_progress`：15/16 closed；`P1-004` 已由 `b9852f37f` 同源部署，状态为 `deployed-pending-canary` | 验证 `delegation_run` 的 start/steer/rename/delete/Team/Workflow/Plan 等 mutation 均返回 typed 409，owner 与 manager 都不能接管只读 peer Session；read transcript/workbench/export 必须保持可用 |
| 2 | `EVID-G2-*` | 14 leaf / 0 Missing | `in_progress`：13/14 closed；`SES-CONSUMER-001` 的 backend/frontend typed consumer、full/build 与三服务部署已完成 | 执行真实 browser 路径，验证 Sub-agent/Team/Peer A2A 三分、read-only A2A window 与 failure terminal；不得用 deployment success 冒充行为 canary |
| 3 | `EVID-G3-*` | 7 leaf / 0 Missing | `in_progress`：4/7 closed；三项 regression 的 migration/apply/deploy 已完成，状态为 `deployed-pending-canary` | 执行 Team model route、旧 Team Session hidden、terminal task/root coverage 重算和 restart/idempotency canary；Group 4 只消费 admitted item/result refs，不另造 root ledger |
| 4 | `EVID-G4-*` | 6 leaf / 0 Missing | `closed`：`EVID-G4-001`–`006`；code/migration、real-PG sequence/CAS/lease/epoch、100×1 MiB ref-only fan-in、partial/late/duplicate/revision/crash recovery、backend/frontend 全量回归、三服务 exact-source deployment 与 production backfill/schema/RLS/hash/health 证据齐全 | 后续 Group 若恢复 inline result bytes、另造 parent mailbox、破坏 ref reader authority、epoch order 或 result immutability，必须重开对应 leaf；当前下一施工入口为 Group 5 |
| 5 | `EVID-G5-*` | 2 leaf / 0 Missing | `open` | 写 fleet scheduler/trigger benchmark、公平性、分页续扫与 control-plane reserve |
| 6 | `EVID-G6-*` | 10 leaf / 0 Missing | `in_progress`：`XCB-MEM-001` 已 `in_progress-local-green:EVID-G6-001`；0/10 production closed | 先完成 Memory 三服务 exact-source deploy/长 Session canary，再继续 CTX-A–F 剩余 leaf、capacity ledger、compaction/output recovery 与尾部证据覆盖 |
| 7 | `EVID-G7-*` | 1 leaf / 1 Missing | `open` | 写跨渠道 execution/delivery ledger、逐 hop authority、fault matrix 与真实/沙箱 channel 分层证据 |
| 8 | `EVID-G8-*` | 9 leaf / 2 Missing | `open；已有 EVID-G8-PRE-001/002/003 三个前置子闭环，事故清理已停止` | 写 T0→T2→T3→soul、durable intelligence job、Enterprise Knowledge、retention/legal hold 与恢复证据 |
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
- 实现：四份真相文档与 `docs/README.md` 强制纳入 Git；新增 103 leaf/5 Missing/11 Group/6 CTX/30 Session/30 golden/40 extreme/10 LB 的机器守恒测试；Hive Connect 8 份文档改为 logical ref，并绑定 remote、commit 与逐文件 SHA-256；新增 §0.4 commit ownership 规则。
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
      decision_consumed: "所有 Group 明确消费其 Session Event/Item/Reducer、S-01–S-30 与 G1–G30 章节"
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
- 七原子：Input=Group/leaf/Missing；Authority=L0/L1 + §9 完整路由；Execution=primary→detail→源码/Red；Evidence=Context Read Receipt + EVID；Recovery=稳定章节、Git-tracked docs、跨仓 pinned snapshot 与 route delta；Consumption=后续每个 Group 的开工流程；Acceptance=当次 11/11 index、79/79 本仓路由、8/8 跨仓 hash、10 tests、ruff、diff check；当前新增的第 80 份本仓路由由 `EVID-G0-006` 的 89/89 守恒门承接。
- 残余风险：文档路由只能保证施工者拿到正确上下文和回填位置，不能证明任何业务 leaf 已 Green；Group 1 仍是 5 个 local Green/production gate open，Group 2–10 状态不因本记录改变。

#### EVID-G0-006：90 个 `@` 文档入口与防漂移开工索引终校

- `leaf_ids` / `missing_ids`：无；本记录只收紧 AA 的施工导航和证据回流，不改变 103 个 breakpoint、5 个 Missing、severity、owner 或业务状态。
- owner Group / 依赖 Group：Group 0 / 无。
- 当前状态：`closed`；11/11 Group 的完整上下文包继续以 §9 为唯一事实源，当前 90 个去重入口已有显式 inventory 与机器守恒。
- 证据 owner / 更新时间：主 Agent / 2026-07-15。
- 冻结事实：开工 HEAD=`bba729daf790645cd8bdc96e565da04cc6b56956`；共享工作树有其它 session 的 tracked/untracked 改动。本项 owned paths 仅为本文与 `backend/tests/architecture/test_agent_native_repair_ledger.py`，没有接管、覆盖或 stage 其它路径。
- Context Read Receipt：

```yaml
context_read_receipt:
  aa_entry: "§0.5–§0.6 + §9 Group 0–10 + §12.3/§12.4"
  leaf_ids: []
  documents:
    - ref: "@docs/agent-native-unified-atomic-review-2026-07-14.md §0.5–§0.6"
      role: "authority"
      decision_consumed: "AA 只保存导航、owner、裁决和证据；长设计留在逐 Group 分类的 @文档，施工后必须把证据收回 AA"
    - ref: "@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md §施工消费合同"
      role: "design"
      decision_consumed: "Context 全文设计由对应 Group 消费，不能复制成 AA 的第二份设计真相"
    - ref: "@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md §施工消费合同"
      role: "design"
      decision_consumed: "Session Event/Item/Reducer 的实施和验收证据回填统一 canonical ledger"
  source_baselines:
    hive_head: "bba729daf790645cd8bdc96e565da04cc6b56956"
    freecode_head: "not-applicable: docs-only navigation guard; each business leaf still performs fresh source comparison"
    codex_head: "not-applicable: docs-only navigation guard; each business leaf still performs fresh source comparison"
  conflicts_or_deltas:
    - "既有 Group 1 快速状态漏列已关闭的 KB-AUTH-001，和 §12.3 的 4/16 truth 漂移"
    - "Group 1 AA 开工入口硬编码到 EVID-G1-006，新增证据后会继续漂移"
    - "既有路由校验确认路径存在，但没有把 1 root + 79 local + 8 external 的当前总量变成显式守恒量"
  evidence_sink: "EVID-G0-006"
```

- Red：先新增 `test_group_context_route_inventory_is_explicit_and_current`，执行 `pytest -q tests/architecture/test_agent_native_repair_ledger.py::test_group_context_route_inventory_is_explicit_and_current` → `1 failed`；正确失败为 AA 缺少 `group-context-route-inventory` machine-readable region，证明“全部 `@` 出来”只有 prose/路径存在性，没有总量防漂移门。
- 实现：§0.6 当前显式声明 11 个 Group 共有 root 1、本仓 81、跨仓 8、总计 90 个去重入口；新增的第 90 个本仓入口是本轮三个用户 P0 垂直切片的 `@docs/p0-session-memory-a2a-repair-sequence-2026-07-17.md`。保留 `@必须先读` 与 `@按需读取` 的软披露边界，不要求单 leaf 全量加载。
- Green：targeted inventory gate → `1 passed`；写回最终证据后的完整 ledger validator → `11 passed`；`ruff check tests/architecture/test_agent_native_repair_ledger.py` → `All checks passed!`。
- migration / deploy / rollback：纯 Markdown 导航与 architecture validator，无 schema/data/runtime migration 或三服务部署。回退必须同时回退 inventory 文字、machine region 与 validator；不得只删一侧制造假 Green。
- 七原子：Input=Group/leaf；Authority=L0/L1 + §9 唯一路由；Execution=AA→primary→完整 Group 包→源码/Red；Evidence=inventory test + Context Read Receipt + EVID；Recovery=Git history、stable section、route delta；Consumption=所有后续 Group 开工；Acceptance=11/11 Group、89/89 路由、11 tests、Ruff。业务 leaf 状态不因本记录改变。

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
- 当前状态：`closed`；本地实现、真实 PostgreSQL、故障回归、仓级 suite、FreeCode/Codex 对照、独立 commit、三服务部署、production actual-data/exact-code canary、legacy operator disposition 与安全 rollback/漂移 hold 均已绿；canonical 为 `closed:EVID-G1-003`。
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
- migration / dry-run / backfill / cleanup / rollback：`root_user_id`/session 列已存在，无 schema migration。生产 READ ONLY 查询得到 `8,967` 条 subagent RuntimeTask：`completed=3,725`、`killed=26`、`needs_reconciliation=5,216`，无 pending/running/resumable/suspended；active missing root=`0`、metadata/root conflict=`0`。held 中 `1,499` 条满足 `child.user_id != root_user_id`，交叉查询证明 `1,499/1,499 child.user_id=agent.creator_id` 且 `root_user_id!=creator_id`，同时 `1,499/1,499` parent session 已缺失。二次只读 inventory 再证 `legacy_retry_enabled=0`、`automatic_retry=false`、`automatic_rewrite=false`；operator disposition 明确为 `retain_needs_reconciliation`：不改写、不自动重试、不归档，保留原证据供逐案 review。代码 rollback 只回退实现，不删除或重写历史记录；注入 stale creator identity 时必须继续 hold，不能恢复旧 creator fallback。
- FreeCode/CC 语义底线：当前 FreeCode `runAsyncAgentLifecycle()` 保留完整 child message/progress/final/task notification，`resumeAgentBackground()` 从 transcript 恢复 tool/context；本项没有删工具、裁上下文、降模型或改变 result 语义，只在 authorized input 前增加 Hive 多用户 authority frame。Codex `ThreadManager.spawn_subagent()` 先 materialize/flush parent rollout，且测试要求 child 持久化 parent originator、completion 通知 parent；Hive 对应地保留 lineage/notification，同时把 Codex 单用户没有的 requester 绑定做成 additive enterprise delta。
- Green（定向）：真实 PG creator≠requester enqueue + session allow/deny 与两条 pre-model 回归 → `3 passed in 6.29s`；authority/run/wake/tool/architecture/worker/HR PKB 合集最终 → `121 passed in 9.55s`；scoped `ruff check` → `All checks passed!`，`ruff format --check` → `9 files already formatted`。
- Green（仓级最终复跑）：`cd backend && source .venv/bin/activate && pytest tests -q` → `7016 passed, 2 skipped in 235.72s`，exit `0`；该结果包含最终 tenant-bound pre-model gate、wake authority、真实 PG 与文档 ledger validator 当前代码状态。
- fault / concurrency / security：覆盖 creator context 注入、malicious recovery metadata 覆盖 requester、两个 requester 并发隔离、missing/invalid/conflicting root、restart missing requester、child completion drift、pre-model transcript drift、wake task-id missing、signal/session/requester drift；架构墓碑断言 durable dispatch/wake 不得重新引入 creator 或 signal-id fallback。
- 真实消费：real spawn 的 `AgentInvocationRequest.user_id` 与 `SessionContext.metadata.requester_user_id` 均为 durable requester；T0/tool/audit 继续消费同一 runtime context；`test_system_hr_personal_kb_read_is_bound_to_current_requester` 证明 HR Personal KB 仍按当前 requester 查库。E-1 只恢复可信 principal，不替代 `KB-AUTH-001` 的 cross-principal grant/sensitivity ceiling 修复。
- commit / deploy / production canary：独立 E-1 commit=`3b3b281543bc` 已随 source=`1b822eb766` 完成三服务同源部署并通过 health。第一组 production canary 从真实生产数据选取 `creator!=requester` 的现存共享 Agent session，实际调用已部署的 child-session validator 与 `spawn_subagent` 构造路径：exact requester 放行、creator 被 `child_session_user_mismatch` 拒绝、canonical root 成立、metadata principal drift 以 `root_user_id_mismatch` 拒绝；受控 invoker 只捕获请求，外部副作用为 `0`，`AgentInvocationRequest.user_id` 与 `SessionContext.metadata.requester_user_id` 均绑定 requester。第二组 canary 运行已部署的 `build_production_parent_wake_invoker()`：requester/session 绑定的 continuation 产生 `1` 个仅内存捕获的 outbox intent；restart creator drift 在真正 enqueue 前以 `parent_session_user_mismatch` hold；`real_db_writes=0`、`external_effects=0`、`model_calls=0`。两组均未写生产数据。
- operator disposition / rollback drill：生产二次只读盘点确认 legacy active/resumable=`0`、1,499 条 creator drift 的 parent session 全部缺失，无法从机械事实安全恢复 requester；因此显式选择 `retain_needs_reconciliation`，禁止 automatic rewrite/retry/archive。安全 rollback/fault drill 不是把旧漏洞重新部署，而是向已部署代码注入 creator 漂移并验证 pre-enqueue hold、零 effect、原证据保留；恢复只能重新进入 requester-authoritative 路径。
- 七原子：Input=authenticated background tool context；Authority=`runtime_tasks.root_user_id` + exact session/tenant binding；Execution=single RuntimeTask worker/wake path；Evidence=typed hold/decision entry/T0/span/outbox/signal；Recovery=`retain_needs_reconciliation`、no blind retry、operator inspect/archive/resolve；Consumption=AgentInvocationRequest/Tool/T0/audit/HR PKB/parent wake；Acceptance=本地 real-PG/故障/全仓回归、production actual-data + exact deployed-code canary、三服务部署、legacy disposition 与零副作用 rollback drill 全绿。因此 canonical 行为 `closed:EVID-G1-003`。
- 残余边界：E-1 已关闭，但不替代 `P1-004` 的跨 hop A2A authority receipt，也不替代 `P1-F4` 的通用 RecoveryManifest。1,499 条 retained legacy records 是显式保留的历史隔离证据，不是可自动重试的活动任务；未来逐案 resolve/archive 必须产生独立 operator receipt，禁止用 creator、metadata 或自然语言猜 requester。
- 对应 §12.2 canonical 行已更新为 `closed:EVID-G1-003`；本项关闭时 Group 1 为 5/16 closed、2/16 deployed-but-open、9/16 pending，当前滚动状态只以 §9 与 §12.3 为准；不改变 103 分母、severity 或 Group owner。

#### EVID-G1-004：P1-004 typed A2A authority frame 与 restart receipt

- `leaf_ids`：`P1-004`；同根范围覆盖 sync/async A2A、custom executor、ToolRuntime effect boundary、RuntimeTask persistence/worker dispatch/restart resume、confirmed Plan handoff 与 child failure outcome。它不替代 `P1-F4` 的通用 RecoveryManifest，也不宣称 Group 2 的最终 Session item/prose 已闭环。
- owner Group / 依赖 Group：Group 1 / Group 0、`E-1`。本项复用 `E-1` 的 authenticated requester/root principal，再把它原子化绑定到 child tool effect；没有重新引入 creator fallback。
- 当前状态：`closed`；本地实现、独立 staged snapshot、仓级 clean-checkout、FreeCode/Codex 对照、production read-only preflight、三服务部署、sync/persisted-async/nested exact-code canary、effect/no-effect 与安全 rollback containment drill 均已绿；canonical 为 `closed:EVID-G1-004`。
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
- commit / deploy / production canary：独立 code commit=`585581319` 已随 source=`1b822eb766` 完成三服务同源部署；production preflight 只读且上线前 delegation row=`0`。受控 canary 在当前 production backend 容器直接运行已部署代码：A→B→C 两跳 principal 保留 requester/root session/root task 并将 chain 扩展为三个 Agent；同步 `_delegate`、custom executor typed frame 与 persisted async dispatch 均通过，benign message 中的 `security/approval/tool/secret` 不改变 authority；receipt 的 policy/snapshot hash 与 effect frame 一致。
- effect / capability / recovery canary：同一 immutable frame、RuntimeTask、budget、trace 与 delegation token 贯穿 inner ToolRuntime；受控 `read_file` fake registry 被调用 `1` 次并返回 `READ_ONLY_EFFECT_OK`，证明 parent deny 不会删除无关 read-only 能力；同一 profile 明确 deny `write_file` 时 registry 调用 `0`，返回 typed `a2a_parent_effect_denied`。不接受 `authority_frame` 的 legacy custom executor 调用 `0`。persisted async 正常派发 `1` 次；restart snapshot drift 不 spawn，写入内存捕获的 `needs_reconciliation`，reason=`a2a_authority_snapshot_drift`、`automatic_retry_disabled=true`。
- rollback / fault drill：未把旧漏洞重新部署。canary 依次注入 budget binding drift、RuntimeTask binding drift、delegation token missing、incompatible executor 与 restart receipt drift；每次都在 registry/effect 前以 typed unavailable/hold 停止，handler 调用 `0`、production DB 写入 `0`、模型调用 `0`、外部副作用 `0`。这就是可安全执行的 forward rollback containment：停止新 admission，保留 immutable receipt，drain 或 hold 已存在 run；不得回到忽略 frame 的旧 effect path。
- 七原子：Input=authenticated parent principal + child request；Authority=tenant/requester/source/target + policy/sandbox/approval/token frame；Execution=single ToolRuntime pre-effect validator；Evidence=receipt/hash/principal/span/typed outcome；Recovery=unavailable/needs_reconciliation、evidence-preserving hold、no blind retry；Consumption=Invoker/custom executor/ToolRuntime/RuntimeTask/A2A outcome；Acceptance=Red→201 + 172 + 7014、production deploy/read-only preflight、sync/async/nested exact-code canary、capability preservation、effect deny 与 drift/rollback fault matrix 全绿。因此 canonical 行为 `closed:EVID-G1-004`。
- 残余边界：A2A failure 的最终 Session item/prose 统一属于 Group 2 typed truth；通用 resume authorization 属于 `P1-F4`。二者没有被本项合并关闭。P1-004 只关闭逐 hop authority frame、effect boundary 与 persisted A2A restart receipt。
- 对应 §12.2 canonical 行已更新为 `closed:EVID-G1-004`；本项关闭时 Group 1 为 6/16 closed、1/16 deployed-but-open、9/16 pending，当前滚动状态只以 §9 与 §12.3 为准；103 分母、severity 与 owner 不变。

#### EVID-G1-005：P1-F4 RecoveryManifest authority、immutable resource 与 legacy quarantine

- `leaf_ids`：`P1-F4`；同根范围覆盖 turn-start load/hydration、tool checkpoint、pre/post-compaction prompt restoration、recovered tool-frame replay、Web Runtime root/task/T0 sequence 传播、完整 manifest 渐进读取、raw workspace/API/code-exec 旁路、legacy fleet cutover 与 observability。它不替代 Group 2 的 canonical Session event/item，也不关闭 Group 6 的通用 Context Resource Plane。
- owner Group / 依赖 Group：Group 1 / Group 0、`E-1`、`P1-004`。本项消费前两项建立的 authenticated requester 与 A2A principal/delegation frame；缺失可信 authority 时只让恢复能力进入 typed unavailable，不阻塞当前模型用本轮授权输入继续推理。
- 当前状态：`closed`；本地实现、对抗 Red→Green、独立 Git-index snapshot、宽回归、FreeCode/Codex 当前源码对照、production inventory、三服务部署、legacy quarantine apply、direct/restart/compact/nested A2A/foreign authority、metrics、no-duplicate-effect 与安全 rollback/tamper drill 全部已绿；canonical 为 `closed:EVID-G1-005`。
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
- migration / dry-run / backfill / cleanup：无数据库 migration。`python -m app.scripts.repair_recovery_manifest_authority` 默认只读 dry-run；`--apply` 还必须显式 `--confirm`，只把 exact legacy bytes 移到可逆 quarantine，不猜 requester/root/policy/config，也不把 unsigned bytes 自动签名。apply 前 inventory：`legacy=54`、`valid_json=54`、`with_session_id=54`、`signed=0`、总 bytes=`2,406,111`、digest-set SHA-256=`700aea462604c4249090eb5c540a05c6f40fc3fe18278f70bb0a7cbd52c2b41e`。对该 54 项统一处置集合采用显式 quarantine disposition 后执行 apply：`scanned=54`、`quarantined=54`；apply 后 live legacy=`0`、quarantine=`54`、mode `0600=54`、总 bytes 与 digest-set hash 完全相同，重复 dry-run=`scanned=0`。处置是 exact-byte 可逆隔离，不是删除、自动改写或自动签名。
- rollback：当前已部署，不能直接退回旧 unsigned singleton reader，否则会重新打开 P1。允许的恢复路径是 forward-fix 到“保留 signed/quarantine/raw-path guards，但将 recovery lane typed unavailable”的安全版本，或先停止相关 runtime、验证无 active recovery consumer 后恢复同一 authority contract。quarantine bytes 不删除，可按 operator 证据审查，但不得放回 live legacy path 让旧代码消费。
- Green（focused current worktree）：最终命令覆盖 runtime/persistence/metrics/legacy/architecture/kernel/e2e/web context/API/workspace/context-resource/tool registry，结果 `238 passed in 6.37s`；scoped `ruff check` → `All checks passed!`。
- Green（宽回归 current worktree）：`pytest -q tests/kernel tests/runtime tests/api/test_prometheus_metrics.py tests/api/test_files_channel_download_token.py tests/api/test_files_write_boundaries.py tests/tools/test_workspace.py tests/tools/test_context_resource_tool.py tests/tools/test_workspace_resource_tool_authority.py tests/tools/test_filesystem_unified_facades.py tests/e2e/test_tool_call_recovery_closure.py tests/services/test_web_chat_run_orchestrator.py tests/services/test_web_chat_runtime.py tests/services/test_recovery_authority_web_context.py` → `1260 passed in 24.75s`，`git diff --check` exit `0`。
- Green（独立 staged snapshot）：以 `git write-tree + commit-tree + git worktree add --detach` 构造只含 Git index 的 `339cddfaaa036401598c8e5aec9cdf77f8c521b3`，同一 focused suite → `238 passed in 6.78s`；同一 scoped Ruff → `All checks passed!`。因此结果不依赖共享工作树未提交 Hook/DB/Session hunks。
- fault / security / observability：覆盖 concurrent sessions、fork isolation、different root task、tenant/requester/agent/principal/policy/config/transcript/delegation drift、unsigned/corrupt/tampered envelope、atomic replace failure、path traversal/symlink swap、immutable pointer、foreign ref、snapshot tamper、raw API/workspace/code-exec denial、missing authority、checkpoint stale revoke 与 recovered-effect replay gate。`recovery_manifest_events_total{operation,status,reason}` 使用 bounded labels 暴露 resolve/load/persist/resource 的 bound/loaded/held/quarantined/unavailable 等状态。
- 真实消费：Web worker 把 root RuntimeTask/root session 和 accepted user T0 sequence 写入 `SessionContext`；Kernel turn 只 load/hydrate 一次；prompt/post-compaction 使用同一 verified result；tool checkpoint 每次更新该 result；recovered mutating tool frame 还要求 session metadata 中 authority digest 与 result 一致；模型通过既有 core `read_context_resource` 恢复完整 signed envelope。raw 文件不进入 Workspace/Artifact consumer，operator 使用 metrics、quarantine inventory 与受控 repair script。
- commit / deploy / production canary：独立 code commit=`67a0bcdcb` 已部署，当前仍包含该代码的最新三服务 deployment 为 backend=`075c1a80-221f-4abd-8a5e-783e6bbd7051`、backend-api=`2ed6c667-2a0f-494b-a160-ff6e55e2d197`、frontend=`ad045cd9-4e68-4b8a-b774-d5566236b0ed`，均 `SUCCESS`；health 证明 RLS/sandbox/daemons 正常。第一组 production temp-workspace exact-code canary：direct signed persist=`written`/load=`loaded`，pre-compaction immutable ref 在后续 checkpoint 后仍读取原 bytes，restart hydrate=true；pending mutating frame只作为 evidence 恢复；foreign requester=`authority_denied`；nested A2A resource read 成功，delegation hash drift=`authority_denied`。metrics 为 persist written=`3`、load loaded=`3`、resource loaded=`2`、held requester mismatch=`1`、held delegation hash mismatch=`1`；临时 workspace 已删除。
- replay / no-duplicate-effect canary：第二组 production temp-workspace 使用真实 signed envelope、真实 loader/hydrator 与 `_execute_recovered_pending_tool_frames()`。`write_file` recovered frame 首次和第二次执行调用均为 `0`，进入 `needs_reconciliation` 且消费 frame；`read_file` 首次调用 `1` 次并保留完整 `READ_RESULT_CANARY`，第二次总调用仍为 `1`，证明不重复 effect。foreign requester load=`held/requester_user_id_mismatch` 且 hydrate=false；tampered signed envelope=`quarantined`、hydrate=false、quarantine exact bytes 与 tampered input 相同；metrics、hook 与 typed event 均非空。临时 workspace 已删除，production DB 写入、模型调用和外部副作用均为 `0`。
- rollback / fault drill：54 个 unsigned legacy live path 已清零且 exact bytes 只在 0600 quarantine；tampered/current authority drift 只会 held/quarantine，不 hydrate、不 replay。允许的 rollback 仍是 forward-safe：停止 recovery admission，把 lane 置为 typed unavailable，同时保留 signed heads、immutable refs、quarantine 与 raw-path guards；禁止恢复旧 unsigned singleton reader或把 quarantine bytes 放回 live path。上述 tamper、foreign requester、delegation drift 与 mutating double-run 共同证明 rollback/故障状态不会产生重复 effect。
- 七原子：Input=authenticated InvocationRequest + Session/T0/root metadata；Authority=signed `RecoveryAuthorityFrame` + current tool context；Execution=single store/loader + governed context-resource/replay gate；Evidence=HMAC envelope、immutable hash ref、typed status、metrics、exact quarantine bytes；Recovery=absent/held/quarantine/unavailable、atomic persistence、confirmed exact-byte cutover、safe forward rollback；Consumption=turn hydration、prompt、compaction、tool replay、context resource、operator metrics；Acceptance=Red→238 + 1260 + staged 238、source baseline、production deploy/apply/direct+nested+restart+compact/no-duplicate/tamper canary 与 health 全绿。因此 canonical 行为 `closed:EVID-G1-005`。
- 残余边界：Group 2 仍拥有 canonical Session event/item/projection，Group 6 仍拥有通用 Context Resource Plane；二者没有被 P1-F4 合并关闭。未来如逐案恢复 54 个 legacy quarantine bytes，必须先从独立机械证据重建完整 authority 并产生 operator receipt，禁止原样放回 legacy live path。
- 对应 §12.2 canonical 行已更新为 `closed:EVID-G1-005`；本项关闭后 Group 1 为 7/16 closed、0 deployed-but-open、9/16 pending；当前滚动状态只以 §9 与 §12.3 为准，103 分母、severity、Group owner 与 5 个 Missing 均不变。

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
- 历史后继边界：本证据关闭时的下一 leaf `KB-AUTH-001` 已由 `EVID-G1-007` 闭环；`KB-PROP-001` 继续负责 sensitivity/provenance 在 transcript/T0/T2/outbound 的传播，`KB-CONTRACT-001` 继续负责 tool description/schema/runtime 三者诚实一致。当前 Group 计数只以 §9 与 §12.3 为准；103 分母、severity、owner 与 5 个 Missing 不变。

#### EVID-G1-007：KB-AUTH-001 Personal KB requester-bound authority

- `leaf_ids`：`KB-AUTH-001`；owner Group / 依赖 Group：Group 1 / Group 0，并消费 E-1/P1-004 已建立的 authenticated requester/execution-principal frame。本证据只关闭 Personal KB 当场 search/read/grant authority；`KB-PROP-001` 仍拥有 transcript/T0/T2/outbound provenance，`KB-CONTRACT-001` 仍拥有全部 Knowledge spec/schema/description 总同步，不能借本 leaf 合并清零。
- 当前状态：`closed`。typed authority、migration/legacy quarantine、real-PG round-trip、owner/shared/A2A/subagent/PL4/revoke 回归、grant API/UI、frontend build、detached clean-snapshot 全量 backend、独立 commit、production migration、三服务 deployment、actual-data transaction canary、health 与安全 rollback 证据均 Green；canonical 为 `closed:EVID-G1-007`。
- 冻结事实：开工 HEAD=`e912408c8b8bc64455a9bbbfd2478d87781c1f9c`，实现/evidence commit=`637a56395`；工作树进入本 leaf 前已有其它 Session 的 runtime/Hook/Session/DB 等未提交改动。commit 精确包含 24 个 owned path：8 个 backend model/service/tool/API path、9 个既有 backend test path、2 个新增 migration/test path、4 个 frontend API/page/test path与本文证据 hunk；`git show --stat 637a56395`=`24 files changed, 2938 insertions(+), 268 deletions(-)`，其它 dirty path 未 reset、覆盖、stage 或归属本项。

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
- migration/backfill/rollback：revision `personal_kb_authority_0715` 以 `personal_kb_sensitivity_canonical_0715` 为唯一 parent；新增 requester/session/purpose/delegation/ceiling/binding/revoke 字段、索引、FK、bound unique 与 4 个 CHECK。所有无法机械证明意图的 legacy grant 保留原 metadata recovery copy，但设 `legacy_quarantined`、PL1 ceiling、stable legacy binding 与 `revoked_at`，不猜授权。downgrade 恢复旧列形状前把全部 edge 过期并写 `downgrade_quarantined`，所以 rollback 不会重开旧漏洞；upgrade/downgrade 都恢复 ENABLE+FORCE RLS。旧镜像面对未知新 head/readiness mismatch 必须 fail closed；授权回退顺序固定为“先用新镜像执行已验证 downgrade 并确认 edge 全过期，再回退代码”，禁止 code-only rollback。
- Green（当前工作树）：backend authority/migration/tool/service/API/model/proposal adjacent family：`125 passed in 14.32s`；同一命令内 real PostgreSQL upgrade→constraint/quarantine→downgrade→re-upgrade round-trip Green；Ruff=`All checks passed!`，20 files format-check；`alembic heads`=`personal_kb_authority_0715 (head)`。frontend API/page：`2 files / 10 tests passed`；`npm run build` exit 0，7356 modules，AgentDetail 与 shared vendor bundle budgets 均 Green。`git diff --check` exit 0。
- Green（独立 staged snapshot）：Git index 精确 24 个 owned path；`git write-tree + commit-tree + git worktree add --detach` 生成 tree=`cf5e0eac800d2a7f08fcea558df35f20ee42986d`、snapshot=`e803a2461441afbe6fcb767d249f7f01cd0320e7`，复用主仓 `.venv` 在 detached backend 执行 `pytest -q` → `7116 passed, 2 skipped in 247.77s`。第一 snapshot 暴露的旧 closure-head 测试已进入同一 Red→Green，而不是被排除；因此全量结果不依赖其它 Session 的 unstaged 工作树。
- production read-only preflight（升级前）：通过 Railway Postgres public TCP proxy、schema owner 的 read-only transaction 查询；未执行 DDL/DML。head=`personal_kb_sensitivity_canonical_0715`；legacy grant=`4`，覆盖 `1 tenant / 3 owners`，全部为 `agent + scope + search`、全部未过期，user/session grant=`0`；Personal documents=`17`，全部 `PL1_public + agent_searchable=true`。因此 migration 精确处置 4 条旧自动 Agent grant；owner interactive direct 不依赖这些 edge，旧 autonomous read 按设计转为 typed deny，直到 owner 创建有 ceiling/purpose/expiry 的新 grant。
- production migration/catalog（升级后）：head=`personal_kb_authority_0715`；8/8 新列、4/4 validated CHECK、包含 `binding_key` 的 bound unique 均存在；`knowledge_grants` 为 `ENABLE + FORCE RLS`。grant total=`4`，其中 `4/4` 均为 `purpose=legacy_quarantined + PL1_public + binding_key=legacy:<id> + revoked_at + original metadata recovery`，active unbound Agent grant=`0`。同连接 `SET LOCAL ROLE app_rls` 后确认 `superuser=false`、`bypassrls=false`、无 tenant context 可见 grant=`0`。
- production actual-data transaction canary：从 detached `637a56395` 加载与生产相同的 service code，在生产 PostgreSQL 的单一未提交事务内选取现有 owner/owned-Agent/document 与另一 requester，只输出 typed state，不输出任何 tenant/user/document/content。结果为 `owner_interactive=ok`、`cross_without_grant=denied`、`agent_searchable_disabled=denied`、`exact_bound_grant=ok`、`wrong_session=denied`、`wrong_purpose=denied`、`pl1_cannot_read_pl3=denied`、`pl3_with_ceiling=ok`、`pl4_reference_only=ok` 且 document bytes 为 null、`pl4_missing_reference=unavailable`、`revoked_grant=denied`。最终显式 `ROLLBACK`，二次查询确认原 sensitivity/searchability/metadata 精确恢复且 canary grant residual=`0`（`rollback_verified=true`）。第一次 canary runner 因未调用 `import_all_models()` 在首个 ORM flush 前触发 `NoReferencedTableError: tenants`；连接关闭自动回滚，独立残留查询为 `0`，补齐完整 model registry 后同矩阵 Green，未把 harness 错误伪装成产品失败。
- fault/security matrix：本地/real-PG 已覆盖 owner interactive PL1–PL3、autonomous no-grant、cross-owner/no-grant、wrong requester/session/purpose/delegation、search-vs-read、PL1/PL3 ceiling、expired/revoked grant、human explicit grant、HR requester scope、nested A2A carried principal mismatch、PL4 secret bytes/reference missing、legacy detail/preview bypass、proposal auto-approve after revoke、invalid resource/grantee tenant 与 migration round-trip；production actual-data canary 再覆盖 owner/cross requester/exact binding/wrong session+purpose/ceiling/PL4/revoke/rollback 的主风险链。
- Model Agency / CCPlus 裁决：所有 hard outcome 都指向 tenant/principal/resource/action/sensitivity/expiry/delegation/credential scheme/DB constraint；未按关键词、相似度或模型正文决定权限，未删除无关工具、压缩 authorized PL1–PL3 input 或替换模型 final。owner-direct 能力保留，跨 principal 只在 bytes ingress/effect boundary 收紧；符合 CC tool agency 底线、Codex typed policy 工程增量与 Hive-native enterprise authority。
- 七原子关闭：Input=runtime principal + resource/action；Authority=owner relation或 bounded grant + DB/RLS；Execution=tool→typed service→SQL fresh-check；Evidence=decision/tool payload/grant row/migration recovery metadata；Recovery=deny/unavailable/expiry/revoke/quarantine/re-authorize、transaction rollback、fail-closed startup 与 safe downgrade；Consumption=search/read tools、owner grant API/UI、proposal policy；Acceptance=Red→125+real-PG+10 frontend+build+ruff/alembic+detached 7116+production catalog/actual-data/health。七原子均有当前真实消费与 production 证据，因此本 leaf 可独立 `closed`。
- commit/deploy/health：code/evidence commit=`637a56395`。同一 commit 的 Git archive 部署 backend=`075c1a80-221f-4abd-8a5e-783e6bbd7051`、frontend=`ad045cd9-4e68-4b8a-b774-d5566236b0ed` 均 `SUCCESS`；backend-api 首次 deployment=`7e513468-5987-4a95-9239-39eeb824c3da` 在主 migration 前按 schema readiness fail closed 并耗尽 10 次 restart，schema ready 后从同一 commit 重提 `2ed6c667-2a0f-494b-a160-ff6e55e2d197` 为 `SUCCESS`。最终 backend `/api/health`=`status=ok`，runtime role=`app_rls / strict / non-superuser / non-bypassrls`，evolution/trigger/workflow daemon 均 running+healthy；frontend=`HTTP/2 200`。该证据关闭时 Group 1 为 4/16 closed、3/16 deployed-but-open、9/16 pending；当前滚动状态只以 §9 与 §12.3 为准。103 分母、severity、owner 与 5 个 Missing 均不变，下一 Knowledge leaf 为 `KB-PROP-001`。

#### EVID-G1-008：KB-PROP-001 Knowledge sensitivity/provenance 端到端传播

- `leaf_ids`：`KB-PROP-001`；owner Group / 依赖 Group：Group 1 / Group 0、`E-1`、`P1-004`、`KB-EXTRACT-001`、`KB-AUTH-001`。本项只关闭已经获准进入模型的 Knowledge bytes 在 transcript、T0、T2 eligibility、nested subagent return 与 channel effect 之间丢失 sensitivity/provenance 的 seam；它不重新定义读权限，也不把 sensitivity 变成 owner-direct search/read 的通用闸。
- 当前状态：`closed`。trusted machine result projection、实时与 terminal delivery consumer、append-only legacy disposition、TDD/ruff/SQL compile、独立 Git-index snapshot、三服务部署、生产 dry-run、容器 exact-code canary 与 health 均已 Green；canonical 为 `closed:EVID-G1-008`。
- 冻结事实：开工 HEAD=`fb8c92b9f33429aed4e7851398a22d3687c178ff`；code commit=`62e262c117c4bbb32cb8276df37ab25c20de492d`、tree=`29401943a5463266fc1e37da808856c0b16d6a4e`，精确 ownership manifest 为 19 个 backend code/test path、`1848 insertions(+), 28 deletions(-)`。共享工作树同时存在 DB/Hook/Session/terminal 等其它 session 改动；本项对 `chat_transcript.py`、`subagent.py` 和 `test_chat_transcript.py` 逐 hunk staging，未纳入 after-commit、`evidence_mode` 或其它外部改动。`git show --name-status 62e262c11` 是唯一代码 ownership 事实源。

```yaml
context_read_receipt:
  aa_entry: "§9 Group 1 + §12.1/§12.2 KB-PROP-001 + §12.3 Group 1"
  leaf_ids: ["KB-PROP-001"]
  documents:
    - ref: "@docs/agent-native-atomic-review-2026-07-14.md §11 D-KB1 / §20–§22"
      role: "original_evidence"
      decision_consumed: "sensitivity 不替代 requester/grant 读权限；但必须约束持久化与跨边界外传，并保持工具合同诚实"
      sha256: "4f1e8893fe03251c02ce9805300b94d6db00c34e032c978f3805c2b1f061e919"
    - ref: "@docs/personal-company-knowledge-tool-boundary-2026-07-10.md"
      role: "knowledge boundary contract"
      decision_consumed: "Personal/Company Knowledge 都通过 governed tool result 暴露；下游只能消费可信 authority/sensitivity envelope，不扫描正文推断权限"
      sha256: "644dd7f85c2a212d6e93101a4101607d3e58ab79a8d6f8048061c5f654305609"
    - ref: "@docs/agent-permission-governance-spec-2026-07-07.md"
      role: "authority and effect contract"
      decision_consumed: "在 bytes ingress 与外部 effect 边界执行 machine-authoritative policy；denied/unavailable/approval-required 保持 typed"
      sha256: "e60f2dcf8711999cf655ccae180fb52810ad2a73f265028c1c56226ba73099ac"
    - ref: "@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md"
      role: "context resource compatibility"
      decision_consumed: "保留原始 authorized evidence 或 lossless ref；metadata projection 不能取代正文、引用或可恢复资源"
      sha256: "c83a1f94b206af7de8bc44f7f4de35746c65d255574a347cbfd80ce0cc3075b7"
    - ref: "@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md"
      role: "Session compatibility"
      decision_consumed: "provenance 先作为现有 transcript/T0 的 typed metadata 贯穿，canonical Session item/reducer 仍由 Group 2 建立，禁止本 leaf 私造第二事件语言"
      sha256: "52a13072ef51ec1ad8f22be5f484b274880c4b7aea801104bd4ca5cdc27c0ac4"
  evidence_sink: "EVID-G1-008"
```

- CC/Codex 当前源码对照：FreeCode `7dc15d6c8` 的 `src/utils/toolResultStorage.ts:1-3,137-183` 对超大 tool result 保留完整 session 文件与可恢复 preview，`src/remote/sdkMessageAdapter.ts:176-194` 按结构化 `tool_result` block 恢复 remote result；Hive 没有用 sensitivity 丢弃 raw Knowledge evidence。Codex `5c19155cb` 的 `codex-rs/protocol/src/models.rs:1029-1045` 集中定义 typed `FunctionCallOutputPayload`，`rollout-trace/.../normalize.rs:445-457` 在无法内联时保留 raw payload ref，`rollout-trace/.../agents.rs:309-327` 把 canonical tool-result raw payload ref 带到 multi-agent edge；Hive 的 typed provenance + source/hash ref 是 additive control-plane delta，没有缩小 CC tool/result capability。
- Red：首轮新回归在 collection 阶段因 `app.services.knowledge_provenance` 不存在而失败；建立最小 projector 后，五个回归分别坐实旧 `send_channel_message` adapter 不接收 sensitivity、nested subagent result 不携 provenance、assistant/T0/T2/outbox 消费链缺口；legacy repair 首轮因 repair module/entrypoint 缺失失败。失败都来自缺少 typed machine seam，不是自然语言扫描器的期望变化。
- Green 实现：
  - `knowledge_provenance.py` 只读取 `search/read_personal_kb`、`search/read_company_kb` 的可信 result envelope，按 canonical enum 求 `max_sensitivity`，记录 source manifest/result SHA-256、authority、coverage 与 warning；缺失/非法机器标签 fail closed 为 `PL4_credential`，但普通正文即使包含 `private/secret/restricted/credential` 也不会改变 PL1。
  - `append_session_event()` 把 tool result projection 写入同一 transcript metadata，并让同 run/turn 的 assistant message 继承聚合 provenance；T0 保留原始可审计 evidence 和敏感等级，PL3/PL4 只把 `semantic_memory_eligible=false` 传给 T2，绝不删除 raw transcript/T0。
  - T2 source-bundle 同时排除实时 `semantic_memory_eligible=false` 事件和 append-only repair 指向的 legacy transcript event，`excluded_refs` 保留可恢复引用；没有机械摘要、正文截断或平台生成语义。
  - terminal `channel_delivery_outbox` 在 intent commit 前按 tenant/agent/session/run 加载 provenance；即时 `send_channel_message` 在实际 external effect 前按同一 run/turn 加载并传入 `content_sensitivity`。两个路径都复用 `ChannelDeliveryService` 的 typed policy，不从 message body 反推敏感度。
  - foreground/background subagent、`spawn_subagent`、`check_subagent` 与 nested forwarding 都携 `hive.knowledge_provenance_aggregate.v1`；merge 保留 child source refs、tool names、coverage 与最大 sensitivity，避免 100-way/嵌套返回时 child label 消失。
  - `repair_knowledge_provenance.py` 默认 dry-run，只有 `--apply --confirm` 才 append `knowledge_provenance_repair` event；不 update 原 transcript，不复制正文，只保存 target event id、typed projection、hash/ref 与版本。metadata JSON 已是现有 durable schema，因此本 leaf 不需要伪造空 migration。
- 本地验收：TDD 最小 Green 为 `13 passed`，append-only repair Green 为 `4 passed`，nested source-ref 回归 Green；owned-area 宽回归为 `195 passed, 26 skipped`。从 staged Git index 解出的独立快照 `/tmp/hive-kbprop-index-20260715-a/backend` 初跑得到 `89 passed, 9 skipped in 3.26s`，文档回填时用下方同一解释器/同一快照复验为 `89 passed, 9 skipped in 2.83s`；同一快照对 19 个 owned code/test path 执行 `ruff check` 为 `All checks passed!`，`git diff --cached --check` 无输出。SQLAlchemy PostgreSQL compile 证明 JSON metadata filter 生成 `metadata_json ->> 'tool_name' IN (...)`，不是 SQLite-only 假绿。

```bash
cd /tmp/hive-kbprop-index-20260715-a/backend
/Users/rocky243/vc-saas/hiveclaw-main/backend/.venv/bin/pytest tests/services/test_knowledge_provenance.py tests/services/test_knowledge_provenance_repair.py tests/services/test_agent_tools_channel_delivery.py tests/services/test_chat_transcript.py tests/services/test_channel_delivery_outbox.py tests/agents/test_subagent.py tests/agents/test_subagent_spawn_tool.py -q
```

- production deploy/health：同一 commit archive 部署 backend=`dcb72fa7-964f-47a7-bc79-0c415891045b`、backend-api=`02101bee-5473-437f-a19f-a760929f1264`、frontend=`c56ae319-0b43-4dc4-aa41-4233695fa17c`，三项最终均 `SUCCESS`。backend `/api/health`=`status=ok/version=1.7.0`，runtime role=`app_rls`、`strict`、non-superuser、non-BYPASSRLS，evolution/trigger/workflow daemon 均 healthy；frontend=`HTTP/2 200`。
- production legacy disposition：backend 容器内运行 `python -m app.scripts.repair_knowledge_provenance` 默认 dry-run，结果 `tool_results_scanned=0`、`knowledge_results=0`、`sensitive_results=0`、`affected_sessions=0`、`repair_events_appended=0`。因此本次没有历史 Knowledge result 或既存 T2/T3 派生物需要 apply/quarantine/rebuild；没有把“脚本存在”冒充已经执行的迁移。
- production exact-code canary：容器内直接调用已部署 `build_knowledge_provenance()`：显式 PL3 得到 `max_sensitivity=PL3_sensitive / semantic_memory_eligible=false`；正文包含 `private_secret_restricted_credential` 但显式标签为 PL1 时仍为 `PL1_public / eligible=true`；缺失标签得到 `held_invalid_sensitivity / PL4_credential / eligible=false / coverage.complete=false`。三项均输出稳定 result/source-manifest SHA-256，证明 hard outcome 只依赖机器标签与 schema，不依赖自然语言关键词。
- rollback/fault：invalid/missing/forwarded-drift label 只会 fail closed、hold semantic promotion 或让外传进入 typed policy，不会删除 raw evidence或替模型写结论。若新链路故障，forward-safe containment 是暂停新 channel delivery、保留 transcript/T0/outbox/repair ref 并重试 projector；repair 是 append-only，原 bytes 无需数据 rollback。禁止回退到“丢 provenance 仍外传/蒸馏”的旧路径。生产历史扫描为零，所以本项不存在待回滚 data mutation。
- 七原子：Input=governed Knowledge tool result + exact run/turn/child envelope；Authority=KB-AUTH requester/grant frame + canonical sensitivity label；Execution=single projector/aggregator + T2/effect consumer；Evidence=transcript/T0 metadata、source/result hashes、outbox detail、subagent aggregate 与 append-only repair ref；Recovery=PL4 fail-closed、hold/retry、raw evidence preservation、dry-run/confirm；Consumption=T2 distillation eligibility、direct/terminal channel delivery、foreground/background/nested subagent；Acceptance=Red→Green、independent index snapshot、ruff/PG compile、three-service deploy、production dry-run/canary/health。七原子均有当前真实路径，因此本 leaf 可独立关闭。
- 残余边界：`KB-CONTRACT-001` 仍负责 Knowledge tool description/schema/runtime 三者诚实一致；Group 2 仍负责 canonical Session event/item/reducer 与 persist-before-publish；Group 6 仍负责通用 Context Resource Plane 和 100-way context pressure；Group 8 仍负责完整 T0→T2→T3→soul durable intelligence loop。对应 canonical 行已更新为 `closed:EVID-G1-008`；本证据关闭当时 Group 1 为 8/16 closed、0 deployed-but-open、8/16 pending，当前滚动状态只以 §9 与 §12.3 为准。103 分母、severity、owner 与 5 个 Missing 均不变。

#### EVID-G1-009：BUD-ROOT-001 budget-root unavailable failover 与 work-amplification containment

- `leaf_ids`：`BUD-ROOT-001`；owner Group / 依赖 Group：Group 1 / Group 0。本项只关闭“创建 authoritative budget root 失败时，执行链仍继续放大工作或把单 Session 锁死”这一 seam；不关闭 `BUD-BREAKER-001` 的运行中 breaker/terminal 语义、`ROOT-TREE-001` 的统一 root coverage/result ledger、Group 3 的 reserve/commit/release admission，也不代替 Group 4/5/6 的 100-way fan-in、fleet fairness 和 context-pressure 修复。
- 当前状态：`closed`。资源事实源、interactive/background 分流、capability assembly 与 pre-effect 双闸、typed runtime/UI/metrics、legacy containment、Red→Green、完整 staged snapshot、CC/Codex 源码对照、三服务部署、production exact-code canary、active-task inventory 与 health 均已 Green；canonical 为 `closed:EVID-G1-009`。
- 冻结事实与 ownership：开工 HEAD=`7fd2835de2c072cb6147537dc290b901cb6ec285`。完整 backend suite 在 BUD 施工期间坐实上一 Knowledge leaf 新增的 `repair_knowledge_provenance.py` RLS bypass 未登记 manifest；该独立安全回归先以 commit=`b744d4b9966451eb6f0248edad5883a7b98a0cce` 修复并通过 6/6 allowlist 测试，未冒充 BUD 语义。BUD code commit=`3c1998607220f0021c4f6a00af308ef5371f5622`、tree=`23725f0c0f1826009b8a6c6a478711206c5e0bf5`，精确 ownership 为 35 个 backend/frontend code/test path、`1358 insertions(+), 89 deletions(-)`；生产 archive 来自该 HEAD，因而同时包含其安全父提交。共享工作树的 DB/Hook/Session/terminal 等其它 session 改动仍未 stage、未覆盖，`git show --name-status 3c1998607` 是 BUD ownership 事实源。

```yaml
context_read_receipt:
  aa_entry: "§9 Group 1 + §12.1/§12.2 BUD-ROOT-001 + §12.3 Group 1"
  leaf_ids: ["BUD-ROOT-001"]
  documents:
    - ref: "@docs/runtime-budget-control-plane-plan-2026-07-03.md"
      role: "budget authority and root lifecycle"
      decision_consumed: "预算必须绑定唯一 root run；平台拥有可验证资源账本，不能用预算故障替模型生成语义终答"
      sha256: "7bfa0a469859eb149fb45012e32bfe5dc8daa09668bbadb33e906a14db69445c"
    - ref: "@docs/runtime-budget-conformance-audit-2026-07-09.md"
      role: "current runtime budget contract"
      decision_consumed: "根预算、child 继承、reserve/commit/release 与 typed terminal 是机械事实；缺失 authority 不得继续后台执行"
      sha256: "5299826c1a4b561328739e7bfc2d2438eb98388cf235124989a59b624dd8c039"
    - ref: "@docs/agent-native-extreme-boundary-atomic-review-2026-07-14.md"
      role: "original extreme-boundary evidence"
      decision_consumed: "单 root Session 的 100 child、巨型 capability surface 与跨渠道压力下，budget failure 必须限制新增工作而不是冻结 direct answer"
      sha256: "f11ba2fcae90731d1d2a53e667b71dbe7c191006326523ac24c3231d7f1ab881"
    - ref: "@docs/runtime-model-agency-constraint-audit-2026-07-13.md"
      role: "Model Agency hard-gate law"
      decision_consumed: "hard outcome 指向 resource/lifecycle 事实；禁止自然语言扫描、平台终答和对 authorized reasoning 的机械饥饿"
      sha256: "366c8a5e4351e76083e6096a8cca09fe93a952ce831cf01e3cae34e2f8b91530"
    - ref: "@docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md"
      role: "permission and recoverability floor"
      decision_consumed: "一次 effect deny 不冻结无关能力；unavailable/denied/approval-required/retryable 必须可区分并可恢复"
      sha256: "db8d895b283ecb7ae747ef7fbbb76591f32aba599052d24d759dcb31c83f7cb0"
    - ref: "@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md"
      role: "Session typed state compatibility"
      decision_consumed: "budget 状态进入既有 RuntimeTask/Session transport projection，不私造第二事件语言；UI 只消费 safe typed projection"
      sha256: "52a13072ef51ec1ad8f22be5f484b274880c4b7aea801104bd4ca5cdc27c0ac4"
    - ref: "@docs/ccplus-north-star-contract-2026-06-24.md"
      role: "CCPlus synthesis boundary"
      decision_consumed: "CC lifecycle/capability surface是语义底座，Codex typed control 是 additive delta，Hive 只在权威和资源边界收紧副作用"
      sha256: "9b2bda91cc42a4464ec9b91c483b78fb83965fe0ff6909836ea1fecc18299e5e"
  evidence_sink: "EVID-G1-009"
```

- CC/FreeCode 当前源码对照：FreeCode HEAD=`7dc15d6c8fb0c40c7fcc02ce9b58204324252632`；`src/query.ts` SHA-256=`74e0ce0d86cfd453add8dc1d15ccb6311b02964b8321e3721b8e71fbd87252ce`，其 `1506-1515` 在工具被 max-turn 中止时保留 typed attachment，`1704-1711` 产出 typed `max_turns` terminal 而不让平台伪造模型结论；`src/constants/prompts.ts` SHA-256=`7dac778e089a7f002403df2a2efb6f0b9e4a450af21766680ab8948596c10f25`，`186-193` 明确单个 permission deny 不冻结无关 reasoning，并允许 context compression。Hive 因而保留当前交互回合的模型推理/直接回答，只禁用会新增 child/workflow/continuation/external work 的能力。
- Codex 当前源码对照：Codex HEAD=`5c19155cbd93bfa099016e7487259f61669823ff`；`ext/goal/src/extension.rs` SHA-256=`456a9a6401899d26c543e2f6b08c1eab584984ae9bcc31c03e8b1bfab2f43fbe` 的 `271-320` 记录 turn abort/error 与 typed usage-limit reason；`runtime.rs` SHA-256=`68f7c4275958820c35c4d9410f8cb1e86d1eb805ecae02867be4b5ede078d5c6` 的 `238-305` 把 usage limit 绑定当前 turn、记账 progress 并以 `UsageLimited/Blocked` 终止；`spec.rs` SHA-256=`fdfc7aa6eeb6ce62f904e5e76246f7d7debf3ce93fbce75eaa9eba9dff6b4990` 的 `35-88` 只接受显式 token budget，由系统拥有 budget/usage 状态。Hive 采用 typed lifecycle/accounting，并增加更窄的 work-amplification containment；没有借预算故障缩小 authorized input、偷偷换模型或改写模型 final。
- 首个 Red 与逐 seam Red：最小 backend contract/guard suite 首轮为 `8 failed, 1 passed`；conditional `schedule_wakeup` 三个断言失败，证明“创建 continuation”和“精确 stop”不能被同一工具名粗暴处理；旧 `SessionContext` budget id 泄漏、缺失 typed user message、`goal_start/update_goal` 分类、Goal API `start_immediately` 非交互标记分别有独立失败；frontend 首轮缺少 runtime-budget state module；architecture gate 坐实 `AgentChatSection` 一度为 2405 行、超过 2400；metrics Red 坐实 Slack 被错误归入未知 source。所有失败都指向 resource/lifecycle machine seam，没有靠关键词或模型正文构造 hard outcome。
- Green 实现：
  - 新增 `hive.runtime_budget_binding.v1`：`bound/inherited/not_applicable/unavailable` 均是 typed binding。budget service exception 是 authoritative unavailable 事实；interactive 只对“本轮模型继续推理并直接回答” fail-open，同时强制 `work_amplifying_tools_disabled=true`；noninteractive/background 在创建 RuntimeTask、调用 LLM 或执行 effect 前 fail-closed。缺少 typed binding 的 legacy queued task 在 ORM load fence 被转换为 `legacy_budget_unbound` containment，不能被 `None` 当安全。
  - work-amplifying 集合来自 `ToolMeta` decorator/registry，而不是第二份易漂移硬编码表。`spawn_subagent/delegate_to_agent/send_message_to_agent/send_agent_session_message/start_workflow/set_trigger/update_trigger/goal_start` 可整体从 capability assembly 排除；`schedule_wakeup` 与 `update_goal` 保留精确安全子动作：`stop=true` 以及 pause/complete/blocked/progress-only 可用，创建 wakeup、`status=active` 或新 `objective` 才在 pre-effect 被拒绝。这样故障态仍能取消、停止、收口，不形成“安全闸导致无法退出”的死循环。
  - Invoker 在 handler effect 前再次调用参数级 classifier；即使旧 prompt、缓存工具列表或代码旁路仍暴露工具，也只返回 `status=unavailable / retryable=true / effect_started=false` 的 typed envelope。平台 notice 只陈述基础设施事实和可用恢复动作，不伪装成 assistant conclusion；模型 final 在 exact secret redaction 之外保持 byte-faithful。
  - `goal_continuation`（含 summary）、agent-session continuation、team member、`/loop` self-pace、trigger same-session 和 Goal API `start_immediately` 明确 `budget_interactive=false`；human web/channel、Plan handoff 和 Advanced Plan 仍为 interactive。旧 `SessionContext` 的 budget metadata 在新 turn 组装前清除，避免上一 turn 的 root id/status 漂移到下一 turn。
  - `/metrics` 以低基数 `source × decision` 导出 `runtime_budget_root_failures_total`；web、Feishu、WeCom、WeChat Personal、Slack、Teams、Discord、DingTalk、Telegram、Local Bridge 与 autonomous source 分组稳定。RuntimeTask API 与 Session UI 只暴露 safe projection；`RuntimeBudgetNotice` 呈现“可直接回答、放大能力暂不可用、可重试”，不泄露 exception/内部 policy bytes。
- 本地完整验收：最终 Git-index 精确快照=`/tmp/hive-budroot-release.wnrfuF`，加入只读 `.git` symlink 让 Git-truth architecture tests 读取同一 index，并复用 `node_modules` symlink；不是从脏工作树直接宣称 Green。backend 全量命令 `pytest tests -q --deselect tests/services/test_command_tooling.py::test_run_command_executes_inside_workspace` → `6744 passed, 405 skipped, 1 deselected in 210.94s`；唯一 outer-sandbox 不可嵌套用例在其正常边界单独运行 → `1 passed in 2.04s`，合计 `6745 passed, 405 skipped`。frontend `npm run test` → `116 files / 672 passed`；`npm run build` exit `0`，`AgentDetail=291245/380000 bytes, gzip=82634/115000`、`vendor=591449/620000, gzip=186474/200000`；owned paths `ruff check`=`All checks passed!`，`git diff --cached --check` 无输出。
- 全量 suite 的失败处置保持可追溯：较早 exact snapshot 暴露 5 项失败，其中 3 项是 detached snapshot 无 `.git` 导致 Git-truth 测试无法取证，补只读 Git metadata 后复验；1 项是上一 Knowledge leaf 的 RLS manifest 真回归，独立 commit `b744d4b99` 修复并通过 6/6；1 项是 Codex outer sandbox 内不能再启动 OS sandbox，已在正常单层 sandbox 边界单独 Green。没有删除测试、降低阈值或把 harness failure 冒充产品通过。
- migration/backfill 裁决：本 leaf 没有新增数据库列或第二 durable schema，binding 复用 `RuntimeTask.budget_run_id`、现有 `metadata_json` 和 Session safe projection，因此不伪造空 migration。生产只读 inventory 在全部 executable task type 的 `pending/running` 范围得到 `active_executable_total=0`、`legacy_unbound_total=0`；没有需要就地改写的 live legacy task。以后若读到历史 unbound queued task，load fence 只 hold/fail-closed 并保留证据，不自动补造 budget authority。
- production deploy/freshness：同一 `3c1998607` archive 部署 backend=`8dd55589-0324-400f-990b-67945e46a601`、backend-api=`783a654e-d3f4-4ac9-953d-24dae3457e70`、frontend=`5f078955-38a7-4862-9080-e306fe06ade7`，三项最终均 `SUCCESS`。backend 首次启动完成 migration/schema readiness、RLS/tool audit 与既有任务恢复后通过 `/api/health`：`status=ok/version=1.7.0`，runtime role=`app_rls / strict / non-superuser / non-BYPASSRLS`，evolution/trigger/workflow daemon、RuntimeTask worker、runtime control bus 与 Vercel Sandbox deny-all probe 均健康；frontend=`HTTP/2 200`。`/metrics` 已存在 `runtime_budget_root_failures_total` 的 HELP/TYPE，即使零事件也可被监控发现。
- production exact-code 证明：容器内 `runtime_budget_failover.py` SHA-256=`8a66d5f8495747d96c57f02cfba709b007e98b14581891ef22fd186c9d48623a`、`registry.py`=`3c80da874e0e5cb17a1a72d817c1fa24c311e9d52d508442e526dee169f6896d`，与本地 commit bytes 完全一致。canary 直接调用已部署代码并带 assertions：interactive=`unavailable/fail_open=true/fail_closed=false/disable=true`；background=`unavailable/fail_open=false/fail_closed=true/disable=true`；整体排除包含 `goal_start/spawn_subagent`，不包含 conditional `schedule_wakeup/update_goal`；参数级结果为 `wakeup_stop=false/wakeup_create=true/goal_active=true/goal_objective=true/goal_paused=false/goal_complete=false`。assertion 全部 exit `0`。
- fault/recovery/rollback：budget root unavailable 时，交互回合只降级放大能力，authorized reasoning/direct answer 仍可完成；后台入口在持久任务/LLM/effect 前终止，Invoker 双闸防 capability-cache/参数旁路。exact stop/pause/complete/blocked 继续可用，故障不会把 Session 锁进不可退出状态。恢复只在“下一个独立 turn”重新做 authoritative admission，不在旧 turn 内静默重开工具。安全回退是保留 typed evidence、hold/retry 和 forward-fix；禁止回滚到旧 fail-open 放大路径。该变更无生产数据 mutation，production inventory 为零，因而 rollback 不需要破坏性 data reversal。
- 七原子：Input=authenticated turn/continuation source + budget admission exception；Authority=RuntimeBudgetService/DB root binding 与 ToolMeta effect classification；Execution=entry admission + capability assembly + Invoker pre-effect guard；Evidence=typed binding、RuntimeTask metadata、tool envelope、Session projection、metric/deployment/hash；Recovery=direct-answer degradation、hold/fail-closed、exact stop/complete、next-turn retry、legacy quarantine；Consumption=web/channel/plan、Goal/Team/Trigger/Loop workers、tool runtime、API/UI/metrics；Acceptance=逐 seam Red→Green、exact-index 全量 backend/frontend/build/ruff、CC/Codex current source、三服务 production、exact-code canary、零 legacy active inventory 与 health。七原子均有当前真实路径，因此本 leaf 可独立关闭。
- 北极星裁决：hard gate 唯一依据是可验证的资源/生命周期事实和 effect 分类；没有检查自然语言来判任务重要性、没有静默裁剪 authorized context、没有降低模型输出预算、没有替换模型 final。CC 的 full-capability interactive loop 是底座，Codex 的 typed usage/control 是工程增量，Hive 在其上增加企业级 root authority、双闸、恢复与可观测性；这是 capability-preserving determinism，不是把 Agent 机械化。
- 残余边界：本证据关闭当时 `ROOT-TREE-001` 与 root admission/reservation/coverage ledger 尚属 Group 3，现已由 `EVID-G3-007` 独立关闭；当时尚缺的 100-child result manifest/integration epoch 也已由 `EVID-G4-001`–`006` 独立关闭。`BUD-BREAKER-001` 仍由 Group 6 修正运行中 breaker/cancel/terminal，Group 5 仍需 fleet fairness，Group 6 仍需 capability/context/output progressive disclosure。对应 canonical 行保持 `closed:EVID-G1-009`；本证据关闭当时 Group 1 为 9/16 closed、0 deployed-but-open、7/16 pending，当前滚动状态只以 §9 与 §12.3 为准。103 分母、severity、owner 与 5 个 Missing 均不变。

#### EVID-G1-010：AUDIT-IMM-001 数据库审计证据不可变与 provenance 守恒

- `leaf_ids`：`AUDIT-IMM-001`；owner Group / 依赖 Group：Group 1 / Group 0。本项只关闭 canonical `audit_logs` 与 `security_audit_events` 可被数据库 owner/runtime 直接 UPDATE、DELETE、TRUNCATE，以及 `external_principals ON DELETE SET NULL` 会反向抹掉历史 actor provenance 的 seam；不关闭 `AUDIT-TENANT-001` 的 `tenant_id=None` 静默丢弃，也不冒充 `MISS-RETENTION-001` 的 retention/export/legal-hold 方案。
- 当前状态：`closed`。release migration、fresh bootstrap、schema readiness、数据库 trigger、外部 principal RESTRICT、真实 PostgreSQL、clean-checkout 全量 backend、CC/Codex 源码对照、三服务部署、production catalog 与零残留事务 canary 均已 Green；canonical 为 `closed:EVID-G1-010`。
- 冻结事实与 ownership：开工 HEAD=`5282aaecc982933a2b8e2cec7a4e4faafeff0da0`。共享工作树同时存在其它 session 的 Runtime/Hook/DB 等改动，本项逐 commit 只 stage 自有 path：核心 guard commit=`94e3ecf5804830e0f19995089608c9059829f660`（tree=`44d1db51b862045fd6a23efc793d98b9dc9279df`，9 files，`670 insertions(+), 20 deletions(-)`）；provenance follow-up=`c0e1108a6d4fefaa0af493404c17a4fe3681c679`（tree=`7d941bcebea9e4df2e63b76b96f411a9aa160a0f`，5 files，`98 insertions(+), 16 deletions(-)`）。完整 release code HEAD=`b4d4446a49b0bfdab980eb0b80c6ef9d0fc4bb85` 另含独立 privacy evidence-preservation 修复；PostgreSQL 跨版本测试契约 follow-up=`4af79c39e2649ea590af8a70e73bc84ebef67861`。`git show --name-status <commit>` 是 ownership 事实源，其它 58 个 dirty/untracked path 未 stage、未覆盖、未归属本项。

```yaml
context_read_receipt:
  aa_entry: "§9 Group 1 + §12.1/§12.2 AUDIT-IMM-001 + §12.3 Group 1"
  leaf_ids: ["AUDIT-IMM-001"]
  documents:
    - ref: "@docs/agent-native-atomic-review-2026-07-14.md"
      role: "original atomic breakpoint and construction gates"
      decision_consumed: "audit evidence needs a mechanical source of truth, typed failure and recovery proof; application convention alone is not immutability"
      sha256: "4f1e8893fe03251c02ce9805300b94d6db00c34e032c978f3805c2b1f061e919"
    - ref: "@docs/agent-native-atomic-review-501db655.md"
      role: "parallel security/RLS evidence"
      decision_consumed: "database and startup truth must be verified on the release-upgrade path, not inferred from ORM declarations"
      sha256: "014734a43994bd1b4a906f89eea21d4686b08c88ec167d8c5046c0f0cdc7f0bb"
    - ref: "@docs/runtime-model-agency-constraint-audit-2026-07-13.md"
      role: "Model Agency hard-constraint law"
      decision_consumed: "evidence durability and exact database contracts are allowed hard invariants; natural-language content and model final remain outside this gate"
      sha256: "366c8a5e4351e76083e6096a8cca09fe93a952ce831cf01e3cae34e2f8b91530"
    - ref: "@docs/agent-permission-governance-spec-2026-07-07.md"
      role: "enterprise governance authority"
      decision_consumed: "audit actor/principal provenance must survive later account lifecycle changes and remain mechanically attributable"
      sha256: "e60f2dcf8711999cf655ccae180fb52810ad2a73f265028c1c56226ba73099ac"
    - ref: "@docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md"
      role: "CCPlus permission and recoverability floor"
      decision_consumed: "governance wraps effects/evidence without starving reasoning or replacing model-authored semantics"
      sha256: "db8d895b283ecb7ae747ef7fbbb76591f32aba599052d24d759dcb31c83f7cb0"
    - ref: "@docs/session-rls-preflight-review-2026-07-09.md"
      role: "runtime/schema role preflight"
      decision_consumed: "schema-owner migration and non-owner runtime role are separate facts; both deployment readiness and live catalog must be proved"
      sha256: "057b7631c75c80ce394096ea5c53cd3afc2b41d0994dcb62860d2fdc8a4029dc"
    - ref: "@docs/rls-enforcement-migration-plan.md"
      role: "migration, rollback and fail-closed discipline"
      decision_consumed: "fresh bootstrap, existing-release upgrade, downgrade and startup readiness must converge on one catalog contract"
      sha256: "66864a7c18233d7bcfcc825344eccc93a604d13039c40616d7b2b0387348b466"
  evidence_sink: "EVID-G1-010"
```

- CC/FreeCode 当前源码对照：FreeCode HEAD=`7dc15d6c8fb0c40c7fcc02ce9b58204324252632`；`src/utils/sessionStorage.ts` SHA-256=`8a123ebce1ee72b9081d34b8f3697e5fcc9c7576df5b98e4206bb28414134412`，`appendEntry` 最终由 `appendEntryToFile`/`appendFileSync` 写 JSONL；`src/types/logs.ts` SHA-256=`ccc8d6e57ba25f277a1ab2cff457a0486a93658d91509b779cacc4fcdd69190e` 明确区分 append-only/replay-all event 与 last-wins snapshot。Hive 保留这一 append-only evidence floor，并在企业数据库层增加无法被普通 ORM/SQL 绕过的约束；没有照抄供应商实现细节。
- Codex 当前源码对照：Codex HEAD=`5c19155cbd93bfa099016e7487259f61669823ff`；`codex-rs/rollout/src/recorder.rs` SHA-256=`ad9c29f5ee1d38d2fab224bdab0c758342a82d3769e8b3441da91cfc12bd029a` 使用 append-open rollout、ordered `pending_items`、flush ack、reopen/retry 与 writer failure observability。Hive 采用其 typed durability/recovery 工程增量，但数据库审计 trigger/FK 是 Hive enterprise authority delta，不改变 CC 的模型循环与表达主权。
- Red：迁移静态/真实 PG 用例首先证明新 revision、四个 trigger 与 readiness catalog 尚不存在，旧表允许 direct mutation；follow-up Red 证明 `external_principals ON DELETE SET NULL` 会隐式 UPDATE 不可变审计行并被新 trigger 以 `55000` 拒绝，说明旧 FK lifecycle 与 provenance 不可变互相冲突；privacy 独立 Red 为 `2 failed, 6 passed`，稳定复现 UUID 数字尾部被误判为 Phone。首次 `94e3ecf58` clean-checkout 全量为 `7156 passed, 2 skipped, 7 failed`：6 项是旧测试全表硬删 principal 触发上述真实 FK 冲突，1 项是随机 UUID 被错误 redaction 的既有 raw-evidence 回归；这些失败均被修复而非跳过。
- Green 实现：migration `audit_evidence_immutability_0715` 与 `db_bootstrap` 共用同一机械合同：两表分别安装 row-level `BEFORE UPDATE OR DELETE` 与 statement-level `BEFORE TRUNCATE` trigger，统一调用 `reject_audit_evidence_mutation()` 并返回 SQLSTATE `55000`；schema readiness 逐项核对 table/function、row/statement、BEFORE、event bits 与 enabled state，缺失/禁用/形状漂移都成为 typed retryable issue，`checked_trigger_count=4`。`AuditLog` 的 external-principal FK 改为具名 `fk_audit_logs_external_principal_id ON DELETE RESTRICT`；产品生命周期使用 revoke/status，不再通过硬删抹除 actor。app source 负向搜索未发现两张 canonical audit 表的 runtime UPDATE/DELETE/TRUNCATE consumer。
- provenance 兼容修复：`test_external_principal_service.py` 不再靠全表 DELETE 清理共享证据，而用唯一 tenant/provider/install/subject 和产品 revoke 路径隔离；release migration downgrade 恢复旧 FK/trigger 形状，upgrade 再收紧。生产 PostgreSQL 18.3 对显式 RESTRICT 返回 `23001 restrict_violation`，PG16 Testcontainers 对该 FK 路径返回 `23503 foreign_key_violation`；测试只接受这两个 class-23 码，任何其它结果仍失败。PG16 exact migration test真跑 `1 passed in 6.21s`，不是 Docker sandbox skip。
- raw-evidence 纠偏：commit=`b4d4446a4` 将 Phone candidate 限定为独立 token，并按 E.164 10–15 位校验；UUID 尾部与长数字 opaque identifier 保持 byte-identical，真实 `13812345678` 与 `+86 138 1234 5678` 仍被遮罩。隐私/Knowledge 定向套件为 `18 passed`。它维护已经关闭的 Knowledge/T0 证据保真，不计作第二个 Group 1 leaf，也不把 regex 升格为语义裁判。
- 本地验收：迁移/bootstrap/readiness/external-principal 扩展矩阵最终 `200 passed in 37.32s`；owned paths `ruff check` 与 format 均 clean。精确 detached worktree=`/tmp/hive-audit-imm-b4d4446a4`，HEAD=`b4d4446a49b0bfdab980eb0b80c6ef9d0fc4bb85`，仅 `.venv` 为复用依赖 symlink；完整 backend `pytest tests -q -rs` → `7165 passed, 2 skipped in 253.96s`，exit `0`。两个显式 skip 分别是本机无 OfficeCLI binary、DingTalk dynamic/pure-guide skill 无 declared tools，与 audit path 无关。
- migration/backfill：production preflight 为 head=`personal_kb_authority_0715`、`audit_logs=92777`、`security_audit_events=2166`、trigger=0、FK=`ON DELETE SET NULL`，与唯一 upgrade 前置精确一致。迁移是 transactional DDL，只安装 function/trigger/FK metadata，不扫描、不改写既有 94,943 条 evidence，因此无需 data backfill；已有行在 migration commit 后立即受保护。fresh bootstrap、release upgrade、downgrade/re-upgrade 与 dropped-trigger readiness fault 均有真实 PG 覆盖。
- production deploy/freshness：精确 `b4d4446a4` Git archive（关键 migration/privacy 文件与本地 SHA-256 相同）部署 backend=`0aa2be07-3a66-41e1-8de5-c51a07631907`、frontend=`97bae2dd-d7c4-4257-a5e0-bab146a19983`，均 `SUCCESS`。backend-api 首次 deployment=`7d789853-4852-452a-83a8-e0f919074c53` 在 runtime migration 前按 readiness 拒绝旧 head，不计完成；schema ready 后同一 archive 重提 `9fe37c2b-bf6c-4542-9715-cbc3cf73c132` 为 `SUCCESS`。最终 backend `/api/health`=`status=ok`，runtime role=`app_rls / strict / non-superuser / non-BYPASSRLS`，三 daemon 与 sandbox deny-all probe 健康；frontend=`HTTP/2 200`。
- production catalog/canary：live head=`audit_evidence_immutability_0715`，readiness=`issues=[] / ready=true / checked_trigger_count=4`；四个 trigger 均 enabled 且精确指向 `reject_audit_evidence_mutation`，FK 精确为 `ON DELETE RESTRICT`。schema-owner 零残留 canary 在一个 repeatable-read 外层事务和逐 attack savepoint 中尝试两表 UPDATE/DELETE/TRUNCATE，六项均以 `55000` rejected，snapshot counts unchanged，outer transaction rolled back。第二个 canary 在未提交事务内插入 principal+audit proof 后尝试删除 principal，PG18.3 返回 `23001`，audit provenance 仍 intact，最终 rollback 后 `residue_rows=0`；命令 exit `0`。
- 部署故障证据不丢弃：backend migration/readiness 在 startup 初段成功，随后日志最后停在 `push_default_skills_to_existing_agents()`；deployment 从提交到 `SUCCESS` 约 7 分钟，uvicorn `Waiting for application startup` 到 daemon start 约 3 分 20 秒，公共 health 在此期间一度 502。当前源码在该 seam 为每个默认 Skill 调用 `AgentAssetTransaction`，其 `_lock()` 使用无 timeout 的 blocking `flock(LOCK_EX)`；这是与症状一致的最强根因推断，但本次没有新容器 stack trace，不能把推断冒充 confirmed root。它不是 audit trigger failure，也不阻止本 leaf 的证据不可变闭环；该真实 availability 输入已回流 §9 Group 8，后续必须用旧实例持锁 fault injection 坐实/推翻并建立 bounded typed recovery，不能以最终成功掩盖。
- fault/recovery/rollback：任一 trigger 缺失/禁用/形状漂移或 migration head 不符时，backend-api 按 readiness fail closed；本次首次 API deployment 真实验证了这一点，并由 schema-ready 后同源重提恢复。正常 rollback 优先 forward-fix；若确需回旧镜像，必须先用新镜像执行 `alembic downgrade personal_kb_authority_0715`、核对 FK 恢复与 trigger 移除，再启动只认识旧 head 的镜像。该 downgrade 会撤销安全保护，只能走显式变更窗，禁止直接部署旧镜像制造永久 readiness loop。
- retention 边界：不可变 trigger 不等于“永不允许受治理的 retention”。`MISS-RETENTION-001` 必须另建 legal-hold/partition/archive/export/deletion ledger 与 schema-owner maintenance 路径；它不得重新开放 runtime UPDATE/DELETE，也不能让 account deletion 通过 SET NULL 抹 provenance。在该 Missing 关闭前，本项选择 evidence-preserving fail closed。
- 七原子：Input=append audit insert 与 schema-owner/runtime mutation attempt；Authority=schema catalog、migration head、principal FK 与 runtime role；Execution=single DB trigger/FK boundary + startup readiness；Evidence=append rows、trigger catalog、typed SQLSTATE、deployment/readiness/canary receipts；Recovery=transaction rollback、fail-closed restart、same-source retry、forward-fix/downgrade runbook；Consumption=AuditLog/SecurityAuditEvent writer、principal lifecycle、operator readiness/health；Acceptance=TDD Red→Green、fresh/release/downgrade real PG、clean-checkout 7165、CC/Codex source comparison、three-service production、six-attack+FK zero-residue canaries。七原子均有当前真实路径，因此本 leaf 可独立关闭。
- 北极星裁决：hard outcome 只依据 Evidence/Recovery/Machine Contract 的数据库事实；不读取自然语言、不判断模型结论、不裁剪 authorized input、不限制模型 output，也不替换 model final。CC append-only lifecycle 是语义底座，Codex ordered/recoverable recorder 是工程增量，Hive 增加 enterprise DB immutability、principal provenance 与 live readiness，属于 capability-preserving determinism。
- 后继边界：`AUDIT-TENANT-001` 已由 `EVID-G1-011` 独立关闭；`MISS-RETENTION-001` 仍负责合规生命周期，Group 8 继续接收 rolling-deploy asset-lock wait 场景。对应 canonical 行保持 `closed:EVID-G1-010`；本证据关闭当时 Group 1 为 10/16 closed、0 deployed-but-open、6/16 pending，当前滚动状态只以 §9 与 §12.3 为准。103 分母、severity、owner 与 5 个 Missing 均不变。

#### EVID-G1-011：AUDIT-TENANT-001 tenantless 安全审计 typed disposition

- `leaf_ids`：`AUDIT-TENANT-001`；owner Group / 依赖 Group：Group 1 / Group 0。本项只关闭 `write_audit_event()` 在 `tenant_id=None/zero UUID` 时 warning 后直接 return、没有 durable row/typed receipt，以及成功密码/OIDC 登录在 audit failure 后仍签发 token 的 fail-open seam；不重开 `AUDIT-IMM-001`，不冒充 `MISS-RETENTION-001`，也不把 rolling-deploy startup lag 或 Memory health 漂移算成新 leaf。
- 当前状态：`closed`。typed tenant/platform receipt、operator-only immutable sink、成功认证 fail-closed、真实 PostgreSQL `app_rls`、RLS bypass allowlist、clean-checkout 全量 backend、三服务 exact-source 部署、production append-only canary 与 health 均已 Green；canonical 为 `closed:EVID-G1-011`。
- 冻结事实与 ownership：开工 HEAD=`d8e7d296b202a687775297e2aff11e5fc463afce`；code commit=`09c3823a06ac399fd7e7da43a89c4d59dbc419c7`，tree=`b82744a04e3cababe156aa3935ba3ce852c0da81`，10 files，`432 insertions(+), 34 deletions(-)`。提交后共享工作树仍有 53 个 tracked dirty 与 5 个 untracked path，均属于其它 session；本项只 stage `auth.py`、`oidc.py`、`policy.py`、`rls_bypass_manifest.py`、`audit_logger.py` 及五个对应测试文件，`git show --name-status 09c3823a0` 是 ownership 事实源。

```yaml
context_read_receipt:
  aa_entry: "§9 Group 1 + §12.1/§12.2 AUDIT-TENANT-001 + §12.3 Group 1 + EVID-G1-010 residual boundary"
  leaf_ids: ["AUDIT-TENANT-001"]
  documents:
    - ref: "@docs/agent-native-atomic-review-2026-07-14.md"
      role: "original atomic breakpoint and construction gates"
      decision_consumed: "security evidence needs a durable typed disposition; warning-and-return is not a completion state"
      sha256: "4f1e8893fe03251c02ce9805300b94d6db00c34e032c978f3805c2b1f061e919"
    - ref: "@docs/agent-native-atomic-review-501db655.md"
      role: "parallel RLS and evidence-path review"
      decision_consumed: "runtime-role truth, explicit bypass ownership and real consumer behavior must be proved together"
      sha256: "014734a43994bd1b4a906f89eea21d4686b08c88ec167d8c5046c0f0cdc7f0bb"
    - ref: "@docs/runtime-model-agency-constraint-audit-2026-07-13.md"
      role: "Model Agency hard-constraint law"
      decision_consumed: "evidence durability and authenticated effect issuance are allowed hard invariants; model semantics remain untouched"
      sha256: "366c8a5e4351e76083e6096a8cca09fe93a952ce831cf01e3cae34e2f8b91530"
    - ref: "@docs/agent-permission-governance-spec-2026-07-07.md"
      role: "enterprise principal and audit authority"
      decision_consumed: "tenantless/public principals need attributable evidence without inventing a tenant or depending on uncommitted FK rows"
      sha256: "e60f2dcf8711999cf655ccae180fb52810ad2a73f265028c1c56226ba73099ac"
    - ref: "@docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md"
      role: "CCPlus capability-preserving governance floor"
      decision_consumed: "deny unauditable authentication effects at the narrow effect boundary without restricting unrelated reasoning"
      sha256: "db8d895b283ecb7ae747ef7fbbb76591f32aba599052d24d759dcb31c83f7cb0"
    - ref: "@docs/session-rls-preflight-review-2026-07-09.md"
      role: "non-owner runtime/RLS preflight"
      decision_consumed: "operator rows require an explicit audited scope and must be exercised under the production app_rls role"
      sha256: "057b7631c75c80ce394096ea5c53cd3afc2b41d0994dcb62860d2fdc8a4029dc"
    - ref: "@docs/rls-enforcement-migration-plan.md"
      role: "RLS recovery and deployment discipline"
      decision_consumed: "reuse the existing operator audit plane and static bypass manifest instead of adding a shadow table or implicit tenant"
      sha256: "66864a7c18233d7bcfcc825344eccc93a604d13039c40616d7b2b0387348b466"
  evidence_sink: "EVID-G1-011"
```

- CC/FreeCode 当前源码对照：FreeCode HEAD=`7dc15d6c8fb0c40c7fcc02ce9b58204324252632`；`src/utils/sessionStorage.ts` SHA-256=`8a123ebce1ee72b9081d34b8f3697e5fcc9c7576df5b98e4206bb28414134412` 仍以 append 写入 session evidence，`src/types/logs.ts` SHA-256=`ccc8d6e57ba25f277a1ab2cff457a0486a93658d91509b779cacc4fcdd69190e` 区分 replay-all event 与 last-wins snapshot。CC 没有 Hive 的 enterprise tenant/operator plane，因此本项保留其 append-only evidence floor，不伪造 vendor parity。
- Codex 当前源码对照：Codex HEAD=`5c19155cbd93bfa099016e7487259f61669823ff`；`codex-rs/rollout/src/recorder.rs` SHA-256=`ad9c29f5ee1d38d2fab224bdab0c758342a82d3769e8b3441da91cfc12bd029a` 仍提供 ordered append、flush acknowledgement、reopen/retry 与 writer-error 可见性。Hive 采用其 typed receipt/failure 工程增量，并用 operator RLS plane 承担无 tenant 的 enterprise attribution；这不是平台代写模型语义。
- Red：原 `test_write_audit_event_skips_zero_uuid_tenant/test_write_audit_event_skips_missing_tenant` 明确把丢弃当正确行为。替换后的 writer/sink Red 为 `5 failed, 5 passed`：两条 receipt 为 `None`、sink function 不存在、sink failure 没有传播；成功认证 Red 分别稳定证明 password login 在 audit exception 后仍返回 `200`，OIDC 在同一异常后 `DID NOT RAISE HTTPException`。这些旧测试属于 fail-open regression debt，已反转而非保留兼容。
- Green 实现：`app/core/policy.py::write_audit_event` 统一先捕获 execution identity，再按权威事实分流：真实 tenant 继续写 `security_audit_events` hash chain；`None/zero UUID` 调用 `write_platform_security_audit_event`，返回 frozen `SecurityAuditWriteReceipt(event_id, scope, tenant_id)`。新 sink 复用已由 `AUDIT-IMM-001` 锁死 UPDATE/DELETE/TRUNCATE 的 `audit_logs`，在独立 `async_session` 与静态 manifest 约束的 `operator platform security audit insert` scope 内 commit；actor/resource/request/execution identity 写入 `hive.platform_security_audit.v1` envelope，tenant/user/agent FK 均保持 NULL，避免 public registration/OIDC 未提交行的 FK/事务耦合。没有新表、shadow log 或猜造 tenant。
- effect/failure 语义：platform sink 不 catch-and-null，insert/serialization/RLS failure 原样向上；成功 password login 与 OIDC login 在 audit exception 时 rollback 并返回 `503 Security audit unavailable; authentication was not completed`，不发 token、不 commit auto-provision。已解析到 user 的错误凭据仍返回 401，因为没有授予效果；对应 audit 成功时同样落入 tenant hash chain 或 platform plane。该 hard outcome 只依据 durable evidence 是否 committed，不读取用户名/自然语言来做语义判断。
- 本地验收：writer/sink Green=`10 passed`；core/auth/OIDC/governance/approval/RLS allowlist 聚合=`52 passed`；全部 auth/OIDC 邻接=`56 passed`；真实 PG strict-RLS bootstrap 整文件=`6 passed`。owned 10 paths `ruff check`、format 与 `git diff --check` 均 clean。exact detached worktree=`/tmp/hive-audit-tenant-09c3823a0`、HEAD=`09c3823a0`：首次在 Codex outer sandbox 内为 `6762 passed, 409 skipped, 1 failed`，唯一失败是嵌套 `sandbox-exec: Operation not permitted`；同一 exact commit 在已批准的单层 OS sandbox 中该用例=`1 passed`，再跑完整 backend 得到 `7170 passed, 2 skipped in 242.24s`、exit `0`。没有删除测试、降低阈值或把 409 个环境 skip 当全量 Green。
- migration/backfill：本 leaf 不改变 schema；operator `audit_logs`、RLS policy、四个 immutability trigger 与 readiness 已由 `EVID-G1-010` 上线，新增 shadow schema 反而制造双事实源，所以没有空 migration。过去被 warning-and-return 丢掉的 event 没有可验证 bytes，无法安全回填；明确记为历史不可恢复 evidence gap，禁止根据应用日志猜造审计事实。cutover 后每次成功写都有 event UUID 与 scope receipt。
- production deploy/freshness：exact `09c3823a0` Git archive 的 `policy.py` SHA-256=`a8300d3160a9ad3840c605df527795770548f467031e281e18511575b55b45a9`、`audit_logger.py`=`b602262d8614956bd30667c970b2a4e2f7f4aac36fcaacab312e7a190d1c3e0e` 与 live backend 容器一致。backend=`68e93420-a5e1-45cc-ab2f-5b0d509a5f67`、backend-api=`8ff72414-8f1e-4f7e-9324-161dbdd7873b`、frontend=`13c0e03a-15b8-45d7-a1c6-a26400d828ec` 均 `SUCCESS`；backend health=`status=ok`、schema head=`audit_evidence_immutability_0715`、runtime role=`app_rls/non-superuser/non-BYPASSRLS/strict`、三 daemon/RuntimeTask worker/sandbox probe 均健康，frontend=`HTTP/2 200`。
- production canary：第一次多行 `python -c` 被 Railway SSH 参数重组，在任何 import/DB call 前以 `Argument expected for -c`、exit `2` 结束，不计证据且没有写 row。随后通过 stdin 执行同一已部署代码，append-only event=`b941ae45-9298-4af1-8d62-e2edbcbdb9d9`、marker=`AUDIT-TENANT-001-production-canary-1f791d93533940c489d725ec96c8f35c`；回读断言 scope=`platform_operator`、action=`platform_security.auth.login`、schema=`hive.platform_security_audit.v1`、tenant/user/agent 均 NULL、actor/request_id byte-preserved，exit `0`。该一条无 PII canary 是不可变验收证据，按设计不删除。
- fault/recovery/rollback：sink unavailable 时成功认证停在 effect 前并以 typed 503 允许客户端重试；恢复后下一次独立请求重新执行完整 authority/audit gate，不在旧请求里静默补发 token。实现没有 data migration，安全回退优先 forward-fix；若回旧镜像，新的 append-only rows仍可读但旧代码会重新丢 tenantless event，因此不能把代码 rollback 当安全恢复。rollback 前必须保留当前镜像或立即 forward-fix，不能撤掉 `AUDIT-IMM-001` trigger。
- 部署期间额外证据：backend 从 09:32:02 创建到 09:38:49 `SUCCESS`，uvicorn 在 09:34:59 `Waiting for application startup`，09:35:06 进入 default-skill push 后长停顿，health 最终报 `event_loop.max_lag_ms=190949.93`；该重复输入已回流 §9 Group 8 的 lock/recovery Red。启动后日志还显示 `MemoryEnhancementSyncResult` 缺少 `skipped`，但 evolution daemon health 为 healthy 且 `last_error=null`；该漂移进入 Group 8 `F-OBS1` 验收。两者不影响本 leaf 的 audit writer/consumer 七原子，也不被本 leaf 关闭。
- 七原子：Input=`tenant_id=None/zero` security event 与 authenticated login outcome；Authority=tenant UUID、operator RLS scope、static bypass manifest 与 immutable DB trigger；Execution=`write_audit_event` 单入口分流 + `write_platform_security_audit_event` 独立 commit + auth effect gate；Evidence=typed receipt、immutable row、versioned envelope、deployment/hash/canary；Recovery=exception propagation、rollback/503、next-request retry、historical-gap disclosure；Consumption=password login、OIDC、governance callers与 operator audit read；Acceptance=TDD Red→Green、allowlist、真实 PG/app_rls、clean-checkout 7170、三服务 exact archive、production append/read canary 与 health。七原子均有当前真实路径，因此本 leaf 可独立关闭。
- 北极星裁决：新增 hard gate 只依据 Authority、Evidence、Recovery 与 Machine Contract 的机械事实；不读取自然语言判真假、不裁剪 authorized evidence、不降低 output/context budget、不改写模型 final。CC 的 append-only lifecycle 是语义底座，Codex 的 typed recorder/failure 是工程增量，Hive 增加 tenant/operator 双审计面与企业认证 effect gate，属于 capability-preserving determinism。
- 残余边界：`MISS-RETENTION-001` 继续负责 audit retention/export/legal hold；Group 8 负责 startup asset lock 与 `F-OBS1` health 真相；本项不声明这些已修。对应 canonical 行保持 `closed:EVID-G1-011`；本证据关闭当时 Group 1 为 11/16 closed、0 deployed-but-open、5/16 pending，当前滚动状态只以 §9 与 §12.3 为准。103 分母、severity、owner 与 5 个 Missing 均不变；当时下一 Group 1 leaf 为 `F-PLAINTEXT`。

#### EVID-G1-012：F-PLAINTEXT AgentTool 配置全信封加密

- `leaf_ids`：`F-PLAINTEXT`；owner Group / 依赖 Group：Group 1 / Group 0。本项只关闭每个 Agent 的 `AgentTool.config` 会把任意 MCP/API/email credential 以可读 JSON 落库并经配置 API 回传的 seam；不把 Personal KB sensitivity、MCP OAuth token store、全局 `Tool.config` 或 tenant config 合并进本 leaf，也不以删减 MCP 配置字段来换安全。
- 当前状态：`closed`。完整非空配置的 authenticated envelope、透明运行时解密、API 双层遮罩与 sentinel merge、key rotation、真实 migration/backfill、secure downgrade、真实 PostgreSQL、exact-checkout 全量 backend、三服务 exact-source 部署、线上零明文 inventory 与 health 均已 Green；canonical 为 `closed:EVID-G1-012`。
- 冻结事实与 ownership：开工 HEAD=`786a36ae6d8083b03846e6734a5593192785679f`；code commit=`8570efdad20dc638a3afde608c57aac80a788432`，tree=`206b59c77498bc428af9e7509d4071ac67b2c7e4`，parent=`786a36ae6d8083b03846e6734a5593192785679f`，10 files，`1034 insertions(+), 16 deletions(-)`。共享工作树中的其它 session 改动从未 stage、reset 或归属本项；`git show --name-status 8570efdad` 是 owned manifest 事实源。

```yaml
context_read_receipt:
  aa_entry: "§9 Group 1 + §12.1/§12.2 F-PLAINTEXT + §12.3 Group 1"
  leaf_ids: ["F-PLAINTEXT"]
  documents:
    - ref: "@docs/agent-native-atomic-review-2026-07-14.md"
      role: "original atomic review and credential-storage refutation boundary"
      decision_consumed: "most platform secret paths being encrypted does not refute plaintext bytes in the per-agent AgentTool override path"
      sha256: "4f1e8893fe03251c02ce9805300b94d6db00c34e032c978f3805c2b1f061e919"
    - ref: "@docs/agent-native-atomic-review-501db655.md"
      role: "parallel authority and narrow-effect-boundary review"
      decision_consumed: "credential visibility is a machine authority invariant; denial must remain typed and must not remove unrelated Agent capability"
      sha256: "014734a43994bd1b4a906f89eea21d4686b08c88ec167d8c5046c0f0cdc7f0bb"
    - ref: "@docs/runtime-model-agency-constraint-audit-2026-07-13.md"
      role: "Model Agency hard-constraint law"
      decision_consumed: "protect stored credential bytes at ingress/read/effect boundaries without inspecting natural language or changing model output"
      sha256: "366c8a5e4351e76083e6096a8cca09fe93a952ce831cf01e3cae34e2f8b91530"
    - ref: "@docs/agent-permission-governance-spec-2026-07-07.md"
      role: "enterprise credential and agent authority"
      decision_consumed: "manage access authorizes configuration but never authorizes returning reusable secret bytes to the UI"
      sha256: "e60f2dcf8711999cf655ccae180fb52810ad2a73f265028c1c56226ba73099ac"
    - ref: "@docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md"
      role: "CCPlus capability-preserving security floor"
      decision_consumed: "retain the full MCP capability surface while enforcing credential visibility and durable storage at the narrow authoritative boundary"
      sha256: "db8d895b283ecb7ae747ef7fbbb76591f32aba599052d24d759dcb31c83f7cb0"
    - ref: "@docs/ccplus-tool-call-governance-closure-landing-plan-2026-06-28.md"
      role: "tool configuration and call-time governance closure"
      decision_consumed: "configuration persistence, runtime consumption and effect governance need one traceable authority path instead of display/runtime copies"
      sha256: "05db3f2d3747a083575fe92f20acd3635ff1f0e48b372b3a6fe201e72df93963"
    - ref: "@docs/rls-enforcement-migration-plan.md"
      role: "schema-owner migration, app_rls cutover and rollback discipline"
      decision_consumed: "backfill under schema authority, verify under runtime role and never make downgrade reintroduce plaintext"
      sha256: "66864a7c18233d7bcfcc825344eccc93a604d13039c40616d7b2b0387348b466"
  evidence_sink: "EVID-G1-012"
```

- 原断点证据：开工版本的 `models/tool.py:82` 将 `AgentTool.config` 直接映射为 JSON；`resource_discovery.py:395-422` 把 `smithery_api_key` 直接写入该列；开工 API 的 `_serialize_agent_tool_row/get_category_config/update_category_config/update_tool_config` 合并、返回或覆盖 raw config。其结果不是“可能命名为 api_key 的少数字段”而是任意第三方 provider 自定义 key 都可能以明文落库，且 schema-less/nested credential 可越过只依赖 `config_schema.sensitive` 的展示遮罩。
- CC/FreeCode 当前源码对照：FreeCode HEAD=`7dc15d6c8fb0c40c7fcc02ce9b58204324252632`。`src/services/mcp/config.ts` SHA-256=`fd78fd3d5b9c18245e8ad14b152463ac7bc21e27ab0c8263a42a47d94ee9a151` 保留完整 MCP config，并以 temp + datasync + rename 做原子更新；`src/services/mcp/auth.ts` SHA-256=`fce615a24470f433b43976917a83db8ff388caeeb75a50ac543c48a70ea4a2e8` 将 OAuth client secret 交给 `getSecureStorage`。但 `src/utils/secureStorage/index.ts` SHA-256=`e73e784ce18ba8f5e2b1d8c8fc6877662257b9d8df0ee3f89c9118670c79ab51` 在 macOS keychain 不可用时 fallback 到 plaintext、Linux 直接 plaintext；Hive 保留“完整配置可用、原子持久化”的 CC 语义底座，不复制其本地单用户明文 fallback 到企业云数据库。
- Codex 当前源码对照：Codex HEAD=`5c19155cbd93bfa099016e7487259f61669823ff`。`codex-rs/core/src/config/mod.rs` SHA-256=`a762c13f9d5a77a79421be1cd163db0f645e6c5f2764be0cbeb0297ef12cb095` 的 `ensure_no_inline_bearer_tokens` 拒绝 inline bearer token 并要求 env reference；`codex-rs/rmcp-client/src/oauth/resolved_store.rs` SHA-256=`2d112b9c6faf13c84f938420f9a289da735a634a47b45a42812452e36546e371` 把 concrete credential store 固定在一次 client lifecycle，读写删失败时拒绝热切换到可能陈旧的 store。Hive 采用其“凭据 authority 明确、失败可见、不隐式热切换”的工程增量，但由于多 tenant 云端 Agent 配置必须可迁移/轮换，使用数据库 authenticated envelope 而不是要求所有 provider 改名或删字段。
- Red：首轮 storage/API 回归稳定为 `8 failed`，分别证明完整 envelope helper 缺失、raw JSON bind/result、wrong-key/tamper/noop provider 未 fail-closed，以及 schema-less API credential 泄漏；补上 runtime/display 分离后，email connection Red=`1 failed`，证明测试端拿到的是遮罩 sentinel 而非真实 `auth_code`；再加 structural-key case 后，ModelScope Red=`1 failed`，证明 `modelscope_api_token` 未被 schema-only 策略遮罩。三类 Red 都先于对应实现，不以改测试期望保留明文行为。
- Green 实现：`agent_tool_config_storage.py` 用版本化 `hive:agent-tool-config:v1:<key_id>:<fernet-token>` 把每个非空 JSON document 整体封进 authenticated envelope；`AgentToolConfigType` 在 SQLAlchemy bind/result boundary 自动加解密，现有 MCP/email/search/runtime consumer 继续拿到同形 dict。空 `{}` 保持空；无 `SECRETS_MASTER_KEY` 的 noop provider 对非空写入明确失败；wrong key、畸形或被篡改 envelope fail-closed；previous-key keyring 可读并由迁移重包到 current key。该设计不猜第三方 credential 字段，也不把配置能力缩成 allowlist。
- API/消费闭环：展示侧 `mask_agent_tool_config_secrets` 同时使用 schema `sensitive` 标记与递归 structural credential key，覆盖 nested `githubPersonalAccessToken`、`clientSecret`、`api_key`、`modelscope_api_token`，不误遮罩 `token_budget`；更新侧 `merge_agent_tool_config_secrets` 将 sentinel 精确还原为已有值，避免普通 UI 保存清空 secret。`test_category_config` 从 ORM runtime config 读取真实解密值，Email/AgentBay 连接测试不消费 display sentinel；因此“API 不回传 credential”和“运行时仍完整可用”同时成立。
- migration/backfill/rollback：Alembic `agent_tool_config_encryption_0715` 在 schema-owner 事务中先 count-only inventory，再将全部 legacy non-empty row 加密/轮换，最后强制验证 `plaintext=0/non_current=0/malformed=0`；缺 master key 或发现 malformed envelope 直接阻断 migration。operator 脚本默认 dry-run，`--apply` 还必须显式 `--confirm`。downgrade 故意不解密，旧镜像若不认识 envelope 就 fail closed，安全恢复路径是保留 current code 或 forward-fix，而不是把 credential 重新落成明文。
- 本地验收：相关 storage/API/migration/邻接套件=`162 passed`；owned Python files 的 `ruff check`、`ruff format --check` 与 `git diff --check` 均 clean。真实 PostgreSQL migration test=`1 passed`，覆盖 legacy raw plaintext→raw ciphertext、ORM transparent read、新 ORM write raw ciphertext 与 secure downgrade。exact detached worktree=`/tmp/hive-f-plaintext-8570efdad`、HEAD=`8570efdad` 的完整 backend 为 `7182 passed, 2 skipped in 255.69s`、exit `0`；没有把环境 skip 当通过，也没有借共享 dirty tree 形成全量声明。
- production deploy/freshness：exact `8570efdad` Git archive 部署 backend=`3d423b2a-3ffe-429d-8857-58fcbc42be82`、backend-api=`3e7e0aa7-70fc-415b-8ed3-db955a500f6d`、frontend=`22b8da1a-da7e-4552-9855-84df8271846e`，2026-07-15 中断后复核仍全部 `SUCCESS`。live `agent_tool_config_storage.py` SHA-256=`0593b749cffce9762bd73e19ec5e850de6a6cdf22e620b4bc6c4ca232e182265`、migration SHA-256=`cdaaea5dafc4b88e733108a3c04879a96d521a58d89b64551a4b7a5f508440ef` 与 Git source 一致。backend health=`status=ok`、runtime=`app_rls/strict/non-superuser/non-BYPASSRLS`、三 daemon/RuntimeTask worker/sandbox probe 均健康；frontend=`HTTP/2 200`。
- production inventory/canary：线上 count-only 报告=`rows 13810 / non_empty 706 / encrypted 706 / plaintext 0 / non_current 0 / malformed 0`，只输出计数不输出任何配置或 secret bytes。schema 从 `audit_evidence_immutability_0715` 升到 `agent_tool_config_encryption_0715`；backend-api 首次 deployment=`a3b7c6a1-273d-4229-9516-0ad57aeafb6c` 在 schema owner 尚未完成 migration 时按 readiness 预期 fail-closed，schema ready 后以同一 exact archive 重提为最终成功 deployment，证明恢复没有切换代码或绕过 head gate。
- fault/recovery：单元/真实 PG 覆盖 wrong key、tampered/malformed envelope、缺 master key、previous-key rotation、重复 migration、empty config 与 secure downgrade；失败不会返回伪空配置、不会覆盖原 ciphertext，也不会让 UI sentinel 污染 runtime credential。生产 schema race 证明 read-only API role 在 stale head 时 hold，schema owner ready 后同源 retry 恢复。配置 envelope 不进入模型 prompt，运行时仅在已通过 Agent/tenant access 的 tool config consumer 内解密。
- 部署期间额外证据：backend 在 `startup: push default skills to every existing agent across tenants` 后再次等待约 203 秒，daemon 于 12:02:07 UTC 才 ready，health 记录 `event_loop.max_lag_ms=203831.58`。这是 `EVID-G1-010/011` 后第三次同位置复现，已回流 §9 Group 8 的 startup asset-lock/fault-injection Red；没有进程 stack 前仍不得把 blocking `flock` 当已证根因，也不新增第 104 个 leaf。
- 七原子：Input=manage-authorized per-agent arbitrary tool config 与 legacy rows；Authority=Agent/tenant manage access、schema-owner migration role、SecretsProvider current/previous keyring；Execution=ORM bind/result 单入口 + API masked display/runtime split + Alembic transactional backfill；Evidence=versioned authenticated envelope、count-only inventory、test/deploy/source hash；Recovery=fail-closed read/write、key rotation、idempotent rewrap、schema readiness retry、secure downgrade；Consumption=Smithery/MCP/search/email/AgentBay 等原调用方透明拿到完整 dict，UI 只拿 sentinel；Acceptance=TDD 三轮 Red→Green、162 targeted、真实 PG、exact-checkout 7182、三服务同源、production 706/706 与 health。七原子均有当前真实路径，因此本 leaf 可独立关闭。
- 北极星裁决：hard gate 的事实源只来自 Credential Visibility、Authority、Machine Contract、Evidence/Recovery allowlist；实现不扫描自然语言、不裁剪 authorized evidence、不降低 context/output budget、不改写模型 final，也不删除 Skill/MCP/subagent/workflow 能力。CC 的完整 MCP config/lifecycle 是语义底座，Codex 的 explicit credential authority/fail-visible storage 是工程增量，Hive 增加 multi-tenant DB envelope、RLS migration、API sentinel 与 fleet key rotation，属于 capability-preserving determinism 和 Hive-native enterprise delta。
- 残余边界：MCP OAuth token store 继续由独立 OAuth authority 管理；全局 `Tool.config` 与 `TenantToolConfig` 的 schema-driven secret 路径没有被本 leaf 偷换为“全部配置已统一”，后续治理重验仍以各自 consumer 为准。Group 8 继续负责 startup asset lock；这些边界不影响 `AgentTool.config` 的零明文闭环。对应 canonical 行保持 `closed:EVID-G1-012`；本证据关闭当时 Group 1 为 12/16 closed、0 deployed-but-open、4/16 pending，当前滚动状态只以 §9 与 §12.3 为准。103 分母、severity、owner 与 5 个 Missing 均不变；当时下一 Group 1 leaf 为 `P2-F8`。

#### EVID-G1-013：P2-F8 ripgrep option terminator

- `leaf_ids`：`P2-F8`；owner Group / 依赖 Group：Group 1 / Group 0。本项只关闭 `grep_search` 把 model-authored regex 直接放进 `rg` argv、导致以 `-` 开头的合法 pattern 被解释成 `--files`、`--pre` 等 CLI option 的 machine-contract seam；不把通用 Bash command、code-execution sandbox、Workspace scan budget/timeout 或 Group 6 context pressure 合并进本 leaf。
- 当前状态：`closed`。argv option terminator、leading-dash pattern byte preservation、路径 authority 邻接、真实 ripgrep、clean-checkout 全量 backend、三服务 exact-source 部署、live source hash、production `--files/--pre` literal canary 与 health 均已 Green；canonical 为 `closed:EVID-G1-013`。
- 冻结事实与 ownership：开工 HEAD=`52accd18d6f5b46e64558ce2f232ebfc0b6e8d0c`；code commit=`6776c3d12c9c3bb9844245aaa80fbd0017f87fea`，tree=`c60854775bce2000a292739cbf2e9987a9e81b23`，parent=`52accd18d6f5b46e64558ce2f232ebfc0b6e8d0c`，2 files，`40 insertions(+), 1 deletion(-)`。`workspace.py` 与 `test_workspace_search.py` 开工时均无 diff；共享工作树其它 session 改动未 stage、reset 或归属本项，`git show --name-status 6776c3d12` 是 owned manifest 事实源。

```yaml
context_read_receipt:
  aa_entry: "§9 Group 1 + §12.1/§12.2 P2-F8 + §12.3 Group 1"
  leaf_ids: ["P2-F8"]
  documents:
    - ref: "@docs/agent-native-atomic-review-2026-07-14.md"
      role: "original P2-F8 code-path evidence"
      decision_consumed: "grep_search passes an untrusted pattern to rg without an option terminator; this is argv flag injection, not shell interpolation"
      sha256: "4f1e8893fe03251c02ce9805300b94d6db00c34e032c978f3805c2b1f061e919"
    - ref: "@docs/agent-native-atomic-review-501db655.md"
      role: "parallel authority/effect-boundary review"
      decision_consumed: "the parallel report adds no duplicate P2-F8 leaf; its narrow-effect rule prevents merging this fix with unrelated tool-governance work"
      sha256: "014734a43994bd1b4a906f89eea21d4686b08c88ec167d8c5046c0f0cdc7f0bb"
    - ref: "@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md"
      role: "CCPlus tool execution envelope"
      decision_consumed: "model chooses the regex; platform owns exact argv/protocol validity and effect isolation without reducing the capability surface"
      sha256: "c83a1f94b206af7de8bc44f7f4de35746c65d255574a347cbfd80ce0cc3075b7"
    - ref: "@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md"
      role: "session tool-call and typed recovery contract"
      decision_consumed: "tool input bytes remain model-authored while the executor provides deterministic machine framing"
      sha256: "52a13072ef51ec1ad8f22be5f484b274880c4b7aea801104bd4ca5cdc27c0ac4"
    - ref: "@docs/runtime-model-agency-constraint-audit-2026-07-13.md"
      role: "Model Agency hard-constraint law"
      decision_consumed: "machine syntax/protocol safety is an allowed hard invariant; natural-language pattern blacklists are not"
      sha256: "366c8a5e4351e76083e6096a8cca09fe93a952ce831cf01e3cae34e2f8b91530"
    - ref: "@docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md"
      role: "CCPlus workspace/sandbox boundary"
      decision_consumed: "session permission cannot bypass tool-specific safety preflight, but a deny must not delete safe read capability"
      sha256: "db8d895b283ecb7ae747ef7fbbb76591f32aba599052d24d759dcb31c83f7cb0"
    - ref: "@docs/ccplus-governance-layer-architecture-2026-06-28.md"
      role: "tool-specific preflight ordering"
      decision_consumed: "validate final tool input at the narrow executor boundary and preserve all earlier authority decisions"
      sha256: "593c54f399708d3c4d61bf1900b8d788ecc1a3127077a1ffd9ab3a938b3ad94e"
    - ref: "@docs/ccplus-tool-call-governance-closure-landing-plan-2026-06-28.md"
      role: "workspace core and call-time governance"
      decision_consumed: "grep remains a core read capability; path boundary and exact machine invocation are platform-owned"
      sha256: "05db3f2d3747a083575fe92f20acd3635ff1f0e48b372b3a6fe201e72df93963"
  evidence_sink: "EVID-G1-013"
```

- 原断点证据：开工版本 `workspace.py::_grep_search` 构造 `['rg', '--line-number', '--color', 'never', pattern, search_root]`；虽然 `subprocess.run` 使用 argv list 且没有 shell interpolation，`rg` 自己仍会把 `pattern='--files'` 当控制参数，把 `--pre=<command>` 当预处理器配置。攻击面是 argument/parser differential：模型请求搜索某个字符串，外部程序却执行另一种模式；不能用“没有 shell=True”推翻。
- CC/FreeCode 当前源码对照：FreeCode HEAD=`7dc15d6c8fb0c40c7fcc02ce9b58204324252632`。`src/tools/GrepTool/GrepTool.ts` SHA-256=`80300c3119836315b802de705a5732c8377f36b05dee31ddcbb38a9abf0bfc07` 在 pattern 以 `-` 开头时显式使用 `-e pattern`，保留搜索能力同时阻止 option interpretation；`src/tools/BashTool/readOnlyValidation.ts` SHA-256=`07a99b7cc73ce9ee45dc0a5cb47755ef5cf2170e6b0d95cd273ff9ece5656c18` 还把 `rg --pre` 记录为可导致外部命令执行的真实危险面。Hive 必须达到这条 CC 语义/安全下限，不能靠禁止 leading-dash 搜索词降级。
- Codex 当前源码对照：Codex HEAD=`5c19155cbd93bfa099016e7487259f61669823ff`。`codex-rs/rollout/src/search.rs` SHA-256=`21e203405393a0634e018fb15b586026f5ca1ce97929ef76fcbdad22667c7758` 在 search term 前始终 `.arg('--')`；`codex-rs/shell-command/src/command_safety/is_safe_command.rs` SHA-256=`e10962e362cb35ee1dbe83554df572d00803535c69a1476700f6a9fb275c9318` 对通用 shell `rg` 额外拒绝 `--pre/--hostname-bin/--search-zip`。Hive 的 fixed `grep_search` 采用更窄、更确定的 Codex option terminator；通用 command surface 仍由既有 sandbox/governance 独立负责。
- Red：先新增 `test_grep_search_terminates_rg_options_before_untrusted_pattern`，mock 只捕获 argv 并返回无副作用结果；初跑稳定为 `1 failed`，差异精确显示 index 4 为用户 `--files`，预期为 `--`，没有因文件系统、ripgrep 缺失或随机输出失败。该测试同时断言 subprocess 仍是 `capture_output=True/text=True/check=False`，修复不能偷偷换成 shell string。
- Green/Refactor：唯一生产变化把末尾 argv 改成 `('--', pattern, absolute_search_root)`，并注释 model-authored pattern 永远是 data；不增加关键词表、不拒绝以 `-` 开头的 regex、不重写 pattern，也不改变 existing `max_results`、path authority、fallback regex 或 typed error path。单测=`1 passed`；Workspace search/path-authority 邻接=`42 passed`；完整 `tests/tools`=`623 passed`；owned files 的 `ruff check`、`ruff format --check`、`git diff --check` 均 clean。
- 本机真实 ripgrep：在非 mock 路径调用 `_grep_search(Path.cwd(), '--files', root='tests/tools', max_results=3)`，返回 `test_workspace_search.py` 中 3 条 `--files` 字面匹配，而不是文件清单或 option error；证明 argv contract 和真实 binary 语义一致。
- clean-checkout 全量：detached worktree=`/tmp/hive-p2-f8-6776c3d12`、HEAD=`6776c3d12`，执行完整 backend 得到 `7183 passed, 2 skipped in 257.77s`、exit `0`。共享主工作树的其它 session 改动不在该 checkout，不把它们的测试结果归给本项。
- migration/backfill/rollback：本 leaf 不改变 schema、持久数据或 UI，所以没有空 migration/backfill。历史 flag-like 查询没有 durable effect 可安全推断或回填；invocation evidence 按原 bytes 保留。rollback 到旧代码会重新打开 parser differential，安全恢复是保留当前 commit 或 forward-fix；不需要数据 rollback，也不得用 pattern blacklist 作为临时降级。
- production deploy/freshness：exact `6776c3d12` Git archive 部署 backend=`4591c65f-4457-4240-91e7-3702abcf6625`、backend-api=`7c180839-6601-45b5-8b39-ea3044fe7468`、frontend=`fcc0eeb9-7797-4425-be14-5e5b16c1a0c6`，三者均 `SUCCESS`。live `workspace.py` SHA-256=`914ae383254a593e007b9dd11760943d95d9ac073269606345d9b128b4e02b59` 与 Git source 一致；backend schema readiness=`agent_tool_config_encryption_0715`、issues=`[]`，health=`status=ok`、runtime=`app_rls/strict/non-superuser/non-BYPASSRLS`、三 daemon/RuntimeTask worker/sandbox probe 均健康；frontend=`HTTP/2 200`。
- production canary：第一次 `python -c` 因 Railway SSH 丢失远端引号，在 import/函数调用前 shell syntax error、exit `2`，不计行为证据且没有副作用。保留远端引号后，同一 live function 对 `--files` 与 `--pre` 各返回 `workspace.py:1228` 的字面匹配，证明两者都没有进入 rg option plane；canary 只读代码目录，不创建或修改生产数据。
- fault/recovery：leading-dash bytes 由 `--` 机械隔离；ordinary no-match 仍是正常空结果，invalid regex/rg unavailable 仍走既有 typed error/fallback，不伪造匹配。path escape/authority tests 保持 Green。若 binary invocation 失败，原 pattern 和 stderr 可用于重试；修复不把一次搜索拒绝扩散为 Session/Agent hard stop。Workspace scan duration/output/resource ceiling 属于既有 Group 6 `SESSION-G13`/capacity ledger 验收，不在本 leaf 造第二套 limiter。
- 部署期间额外证据：第四次 backend startup 于 12:20:07 UTC 进入 `startup: push default skills to every existing agent across tenants`，daemon 到 12:23:29 才 ready，约 203 秒；health `event_loop.max_lag_ms=197663.07`。它与 `EVID-G1-010/011/012` 同位置同量级，已回流 §9 Group 8；没有进程 stack 前仍不得把 blocking `flock` 当已证根因，也不新增第 104 个 leaf。
- 七原子：Input=model-authored regex/root/max_results；Authority=resolved Workspace boundary 与 `authority_scope` path predicate；Execution=`_grep_search` 唯一 argv constructor + `rg -- pattern absolute_root`；Evidence=captured argv、真实 binary result、live source hash、production literal canary；Recovery=no-match/error 保持 typed、原 input 可重试、无持久数据回滚；Consumption=原 `grep_search` tool/Skill/subagent/workspace caller 继续消费同形文本结果；Acceptance=TDD Red→Green、42/623 targeted、真实 rg、clean-checkout 7183、三服务同源、production `--files/--pre` 与 health。七原子均有当前真实路径，因此本 leaf 可独立关闭。
- 北极星裁决：`--` 只执行 Machine Contract / Execution Isolation 的可验证硬不变量，平台不判断 regex 语义、不扫描自然语言关键词、不删工具、不裁剪结果、不降低 context/output budget、不改写模型 final。CC 的完整 leading-dash pattern 能力是语义底座，Codex 的 typed argv terminator 是工程增量，Hive 保留 tenant/path authority 与企业 evidence，属于 capability-preserving determinism。
- 残余边界：通用 Bash/command 中用户显式选择的 `rg` flags 继续由 sandbox/approval/command safety 管理，不能用本 leaf 冒充整个 command plane 已关闭；Group 6 继续负责大目录搜索的时间/输出/context pressure。对应 canonical 行保持 `closed:EVID-G1-013`；本证据关闭当时 Group 1 为 13/16 closed、0 deployed-but-open、3/16 pending，当时下一 leaf `P2-F6` 已由后继 `EVID-G1-014` 独立关闭。当前滚动状态只以 §9 与 §12.3 为准；103 分母、severity、owner 与 5 个 Missing 均不变。

#### EVID-G1-014：P2-F6 tenant-bound Agent model authority

- `leaf_ids`：`P2-F6`；owner Group / 依赖 Group：Group 1 / Group 0。本项关闭 tenant-owned Agent primary/fallback model 与 Role Template model 在 governed write、AI Asset rollback、legacy migration 和数据库持久化之间缺少统一 cross-tenant authority 的 seam；不替模型选择“哪个模型更好”，不修改 prompt/context/output/tool surface，也不把 Agent Team、通用 model routing 或 budget 断点并入本 leaf。
- 当前状态：`closed`。原报告的过宽结论已纠偏，剩余真实 seam 已由 typed preflight + composite FK + audited migration 收敛；TDD、真实 PostgreSQL、两次 clean-checkout 全量、exact-source 三服务部署、live hash、production revision/constraint/inventory/health canary 均已 Green；canonical 为 `closed:EVID-G1-014`。
- 冻结事实与 ownership：开工 HEAD=`054a19808766979ca8e6be3d2cc4acbfbde20e3d`。主 code commit=`32778e239311851477dbdc4178255e63d937e6dc`，tree=`b9c010ce375acabd33fedace0c0cec0eabc69826`，parent=`054a19808766979ca8e6be3d2cc4acbfbde20e3d`，10 files，`796 insertions(+), 3 deletions(-)`；全量 gate 跟进 commit=`4bae5e0e3c9dacb141da609968dde3b8704912de`，tree=`8ca5cecc64bcae1088575de343fdcf4e1d523aa2`，parent=`32778e239311851477dbdc4178255e63d937e6dc`，只把 migration closure-head 测试从旧 head 更新到新 head，`1 insertion(+), 1 deletion(-)`。共享工作树其它 session 的 hook/runtime/API/test 改动未 stage、reset、覆盖或归属本项；两个 `git show --name-status` 是 owned manifest 事实源。

```yaml
context_read_receipt:
  aa_entry: "§9 Group 1 + §12.1/§12.2 P2-F6 + §12.3 Group 1"
  leaf_ids: ["P2-F6"]
  documents:
    - {ref: "@docs/agent-native-atomic-review-2026-07-14.md", sha256: "4f1e8893fe03251c02ce9805300b94d6db00c34e032c978f3805c2b1f061e919", decision: "original P2-F6 claim is evidence to revalidate, not authority to preserve a false missing-API conclusion"}
    - {ref: "@docs/agent-native-atomic-review-501db655.md", sha256: "014734a43994bd1b4a906f89eea21d4686b08c88ec167d8c5046c0f0cdc7f0bb", decision: "narrow authoritative boundaries and migration/RLS evidence remain mandatory; no duplicate leaf was added"}
    - {ref: "@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md", sha256: "c83a1f94b206af7de8bc44f7f4de35746c65d255574a347cbfd80ce0cc3075b7", decision: "authority is fixed before model input/effect and must not be implemented by starving model capability"}
    - {ref: "@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md", sha256: "52a13072ef51ec1ad8f22be5f484b274880c4b7aea801104bd4ca5cdc27c0ac4", decision: "configuration and rollback failures remain typed, recoverable Session evidence rather than silent model substitution"}
    - {ref: "@docs/runtime-model-agency-constraint-audit-2026-07-13.md", sha256: "366c8a5e4351e76083e6096a8cca09fe93a952ce831cf01e3cae34e2f8b91530", decision: "tenant reference equality is an authority/data-ingress invariant; model semantics and final output remain untouched"}
    - {ref: "@docs/agent-permission-governance-spec-2026-07-07.md", sha256: "e60f2dcf8711999cf655ccae180fb52810ad2a73f265028c1c56226ba73099ac", decision: "tenant and Agent authority must be derived server-side at the final write boundary"}
    - {ref: "@docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md", sha256: "db8d895b283ecb7ae747ef7fbbb76591f32aba599052d24d759dcb31c83f7cb0", decision: "session permission cannot authorize a foreign-tenant model reference"}
    - {ref: "@docs/ccplus-governance-layer-architecture-2026-06-28.md", sha256: "593c54f399708d3c4d61bf1900b8d788ecc1a3127077a1ffd9ab3a938b3ad94e", decision: "call-time preflight and durable persistence authority must agree"}
    - {ref: "@docs/ccplus-tool-call-governance-closure-landing-plan-2026-06-28.md", sha256: "05db3f2d3747a083575fe92f20acd3635ff1f0e48b372b3a6fe201e72df93963", decision: "a denial is scoped to the invalid effect and returns a recovery path"}
    - {ref: "@docs/session-rls-preflight-review-2026-07-09.md", sha256: "057b7631c75c80ce394096ea5c53cd3afc2b41d0994dcb62860d2fdc8a4029dc", decision: "application runtime stays app_rls; schema-owner canary is separately labeled"}
    - {ref: "@docs/rls-enforcement-migration-plan.md", sha256: "66864a7c18233d7bcfcc825344eccc93a604d13039c40616d7b2b0387348b466", decision: "release migration owns fleet inventory, audited legacy disposition and validated constraints"}
    - {ref: "@docs/personal-company-knowledge-tool-boundary-2026-07-10.md", sha256: "644dd7f85c2a212d6e93101a4101607d3e58ab79a8d6f8048061c5f654305609", decision: "Knowledge principal boundaries remain separate and are not widened by model configuration"}
    - {ref: "@docs/personal-knowledge-base-completion-contract-2026-07-08.md", sha256: "7dad2c59695109d06c38e4f24cc39648c53fa66b59761c3af99b70ae57328544", decision: "P2-F6 cannot be used to claim Knowledge contract closure"}
    - {ref: "@docs/runtime-budget-conformance-audit-2026-07-09.md", sha256: "5299826c1a4b561328739e7bfc2d2438eb98388cf235124989a59b624dd8c039", decision: "model authority validation does not introduce a hidden routing or token-budget downgrade"}
  evidence_sink: "EVID-G1-014"
```

- refute-first 重验：原报告把 `P2-F6` 概括成“model config 写入缺 cross-tenant reference 校验”，但 current-source `git blame` 证明 `backend/app/api/agents.py::_validate_model_refs` 自 commit `f24ed890fb`（2026-06-13）起已在 Agent create/update 前校验 primary/fallback model 的 tenant 与 enabled 状态。因此“主 Agent API 完全未校验”被推翻；真实残余是该规则只存在于一个 API，数据库仍是单列 FK，AI Asset rollback 与 Role Template 写路径可绕过，权威没有成为所有持久写的共同事实源。
- 实现：新增 `agent_model_authority.py::validate_tenant_model_references/validate_agent_model_references`，只接受同 tenant 且 enabled 的 model，并在 mutation 前返回 `ModelReferenceAuthorityError`。AI Asset Agent rollback 先解析 effective primary/fallback 再校验，失败时不调用 `apply_agent_content`；Role Template create/update 在赋值前校验并映射为 HTTP 400。既有 Agent create/update 校验保持不变，避免为了 DRY 触碰共享工作树中不属于本 leaf 的 `agents.py` 改动。
- 数据库权威：`llm_models(tenant_id,id)` 增加 `uq_llm_models_tenant_id_id`；tenant-owned `agents(tenant_id,primary_model_id/fallback_model_id)` 与 `agent_templates(tenant_id,model_id)` 分别增加 `fk_agents_primary_model_tenant`、`fk_agents_fallback_model_tenant`、`fk_agent_templates_model_tenant`。composite FK 是绕过 API 时仍成立的 cross-tenant hard boundary；“enabled”仍由 governed mutation preflight 判断，没有伪装成数据库可表达的静态 FK 事实。
- migration/backfill/recovery：revision=`agent_model_tenant_authority_0715`。升级先把 legacy missing/cross-tenant Agent 与 Role Template ref 写入 canonical `audit_logs`（`migration.agent_model_reference_quarantined` / `migration.agent_template_model_reference_quarantined`，含原 model、原因和 recovery），再只将无效 ref 置空；随后以 `NOT VALID` 建约束并 `VALIDATE CONSTRAINT`。secure downgrade 故意保留约束与不可变 quarantine evidence，旧应用与约束兼容，避免 rollback 重开越权写。production preflight 证明实际 backfill 为 0：102 个 Agent 的 primary/fallback missing/cross-tenant 全为 0，Role Template 当前 0 条。
- Red：metadata test 先因 `fk_agents_primary_model_tenant` 不存在而 `KeyError`；AI Asset rollback 对 foreign model 未抛错；migration contract 文件不存在。扩展到全部同根写路径后，Role Template foreign model create 仍返回 201、update 进入未类型化 500，且 template composite FK 缺失。每个失败都发生在预期 authority seam，不是 fixture 或网络噪声。
- Green/Refactor：`cd backend && .venv/bin/pytest tests/migrations/test_agent_model_tenant_authority_migration.py tests/integration/test_ai_asset_control_plane.py tests/integration/test_schema_readiness.py -q` → 真实 PostgreSQL 组合 `7 passed in 15.06s`；补强 secure-downgrade audit preservation 后，`.venv/bin/pytest tests/migrations/test_agent_model_tenant_authority_migration.py::test_real_migration_quarantines_legacy_refs_and_enforces_tenant_pair -q` → `1 passed in 5.67s`。所有 owned Python files 的 `ruff check`、`ruff format --check` 与 `git diff --check` 均 clean。部署后在 detached checkout `/tmp/hive-p2-f6-4bae5e0e3/backend` 执行 `/Users/rocky243/vc-saas/hiveclaw-main/backend/.venv/bin/pytest tests/models/test_agent_model_tenant_constraints.py tests/services/test_agent_model_reference_authority.py tests/api/test_role_templates.py tests/integration/test_ai_asset_control_plane.py -q` → `14 passed, 4 skipped in 0.42s`；4 个 skip 是该无 production DB 环境下既有 integration fixture 条件，不替代前述真实 PG 结果。
- clean-checkout 全量与 gate 修复：第一次 detached checkout HEAD=`32778e239` 执行完整 backend 得到 `7190 passed, 2 skipped, 1 failed in 262.52s`；唯一失败是 `test_alembic_single_head_is_current_closure_head` 仍硬编码旧 revision，不能被隐藏。`4bae5e0e3` 只更新该 closure-head gate 后，在 `/tmp/hive-p2-f6-4bae5e0e3` 复跑同一 `pytest tests -q` 得到 `7191 passed, 2 skipped in 269.34s`、exit `0`。
- CC/FreeCode current-source 对照：FreeCode HEAD=`7dc15d6c8fb0c40c7fcc02ce9b58204324252632`；`src/utils/model/agent.ts` SHA-256=`f5f1551da0cdcc261b28b3df8fecc81af05c1213e4bd99f2a5d7fb7541bad89b` 保留显式 model、`inherit`、parent tier 与 Bedrock region 的模型选择语义，不存在多租户 DB model UUID。Hive 没有把 tenant authority 变成 alias/region/model-quality 裁判，也没有降低 CC 的可选模型能力。
- Codex current-source 对照：Codex HEAD=`5c19155cbd93bfa099016e7487259f61669823ff`；`codex-rs/config/src/config_toml.rs` SHA-256=`fd3b7b600552e0567eb78ae0a45842e9a32011ebdf581ca18b6e5c3ed0ad7537` 以 `model`/`model_provider` 字符串表达显式选择，`config_requirements.rs` SHA-256=`c404fe3723779fa9f8d50963cab01f8a343a512d3760e951cacd232c57dba5a1` 提供 managed new-thread defaults，同样没有 Hive 的 tenant row authority。composite FK 与 typed preflight 因此是 Hive enterprise control-plane 增量，不是对 CC/Codex 模型语义的改写。
- production preflight：部署前 schema owner 只读查询为 revision=`agent_tool_config_encryption_0715`；Agent count=`102`，primary/fallback missing=`0/0`、cross-tenant=`0/0`；Role Template count=`0`、missing/cross-tenant=`0/0`。查询角色为 `postgres/superuser/BYPASSRLS`，只用于 fleet schema/inventory 取证，不能冒充应用 runtime 身份。
- exact-source deploy/recovery：发布 archive 来自 `4bae5e0e3`；migration SHA-256=`10a1406670a8a01a3f5aab46f3f68fa1280aa248f4c079f8642b904999263f77`，authority service=`9a7fa9c3b2cc3f96b3ccc0b833b8793dd1a2768b0f48856ec5fbdbad47c7667e`，Agent model metadata=`9a05fb166069a0f709232336e0a77f3b45dfe9e11970cc882cec97961d5aef8a`。backend=`faf6f902-c33a-4c54-801a-90092041c07f`、frontend=`5df91a9e-d44a-4431-b1ea-7f4376553558` 均 `SUCCESS`。首次 backend-api=`aa3ef7db-1c45-43c1-8fb6-b84b7ccb01ee` 在 backend migration 前耗尽 10 次 retryable readiness 重试且容器退出；schema ready 后从哈希相同的 archive 重提 `fe7a02a3-bf87-4a8a-b3c6-fbb9cc29a8f2` 为 `SUCCESS`。最终三服务 latest 均为 `SUCCESS`，没有用 working-tree dirty bytes 或不同 commit 恢复。
- production freshness/constraint canary：live backend 三个文件 SHA-256 与 archive 上述三值逐字一致；revision=`agent_model_tenant_authority_0715`。`uq_llm_models_tenant_id_id`、`fk_agents_primary_model_tenant`、`fk_agents_fallback_model_tenant`、`fk_agent_templates_model_tenant` 四项均存在且 `validated=true`；102 个 Agent 和 0 个 Role Template 的 invalid inventory 仍全为 0。没有为 canary 构造生产 tenant/model 或做合成 UPDATE；直接越权写的拒绝、same-tenant 写的成功、legacy quarantine 与 secure downgrade 已由隔离真实 PostgreSQL 测试证明，生产只读 catalog/inventory 避免留下业务残渣。
- production health：backend `/api/health` 返回 `status=ok`，应用 runtime role=`app_rls/strict/non-superuser/non-BYPASSRLS`，三 daemon、RuntimeTask worker 与 sandbox probe 健康；frontend=`HTTP/2 200`。这与 schema-owner canary 的 `postgres` 角色分层一致，未拿 owner connection 冒充 RLS enforcement。
- fault/recovery：governed write 的 missing/disabled/foreign model 在 mutation 前得到 typed 400/`ModelReferenceAuthorityError`，原对象不变且调用者可选择同 tenant enabled model 重试；绕过服务层的 cross-tenant write 由 DB `IntegrityError` 拒绝；legacy row 被审计并置空，不会静默 fallback 到 foreign model。失败只封锁该配置/rollback effect，不冻结 Agent 的其它推理、工具或 Session；回滚不删除安全约束。
- Agent Team 边界：`AgentTeamMember.model_id` 当前会被写入 member metadata/Team index，但 current-source 搜索未发现它被 runtime model resolver 消费；Team spawn 仍以 lead Agent/runtime task 路径执行。本 leaf 不把一个尚未进入 model-call effect 的 Team metadata 字段伪装成已修；它随既有 Group 3–4 的 Team admission、authority、execution、result-ledger 验收一起收敛，不新增第 104 个 leaf，也不得在进入真实执行前继续保持单列 FK。
- 部署期间额外证据：第五次 backend 于 12:59:30 UTC 进入 `startup: push default skills to every existing agent across tenants`，daemon 到约 13:02:53 才 ready，约 203 秒；health `event_loop.max_lag_ms=198063.26`。同次部署还在 container start 后约 126 秒才出现 entrypoint 日志，backend-api 因固定 10 次快速重试先退出。它与 `EVID-G1-010/011/012/013` 的同位置证据及 Group 8 rolling-deploy fault 场景合并，根因仍待进程 stack/fault injection；不新增 leaf，也不被 P2-F6 关闭掩盖。
- 七原子：Input=Agent create/update、Role Template create/update、AI Asset rollback 与 legacy migration model refs；Authority=server-side tenant ID + tenant-owned enabled `LLMModel` + composite FK；Execution=governed preflight 与 DB constraint 两层唯一写边界；Evidence=typed errors、canonical audit quarantine、real-PG `IntegrityError`、constraint catalog、revision/deploy/live hashes；Recovery=mutation 前拒绝、选择同 tenant model 重试、legacy audited null、secure downgrade 保留边界；Consumption=Agent runtime、Role Template provisioning 与 AI Asset rollback 只拿到可归属的 ref，Team 非消费边界显式路由；Acceptance=Red→Green、ruff、real PG、两次全量、三服务、production inventory/catalog/health。七原子均有当前真实路径，因此该 leaf 可独立关闭。
- 北极星裁决：硬约束指向可验证的 Authority/Data Ingress 事实源 `tenant_id` 与 FK，不读取自然语言、不判断模型质量、不偷偷路由到别的模型、不删 capability、不裁剪 context/output、不改写 model-authored final。CC 保留显式模型选择语义，Codex 的 typed config/managed defaults 作为工程参照，Hive 仅增加 enterprise tenant authority、审计与恢复，符合 capability-preserving determinism。
- 残余边界：Group 8 继续负责 startup stall、event-loop lag 与 schema-wait recovery；Group 3–4 负责 Agent Team model metadata 在进入真实 execution 前的 authority；Group 1 尚余 `KB-CONTRACT-001`、`B-01`。对应 canonical 行已更新为 `closed:EVID-G1-014`；Group 1 当前为 14/16 closed、0 deployed-but-open、2/16 pending。103 分母、severity、owner 与 5 个 Missing 均不变；下一 Group 1 leaf 为 `KB-CONTRACT-001`。

#### EVID-G1-015：KB-CONTRACT-001 Personal KB 读权限诚实契约

- `leaf_ids`：`KB-CONTRACT-001`；owner Group / 依赖 Group：Group 1 / Group 0。本证据只关闭 Personal KB 的 canonical spec、tool metadata 与已存在 runtime read-authority 之间的诚实性断点；不改变 runtime bytes、读取能力、sensitivity 语义、数据库或 production data，也不把 `B-01` 或 Group 8 Knowledge/Memory durable loop 并入本 leaf。
- 当前状态：`closed`。三份 canonical Knowledge 文档共享逐字相同的 read-authority matrix，architecture gate 同时导入真实 tool handler 并锁定 metadata 语义；owner-direct PL1–PL3、grant-required lanes、sensitivity ceiling 与 PL4 opaque credential reference 不再互相矛盾。canonical 为 `closed:EVID-G1-015`。

```yaml
context_read_receipt:
  aa_entry: "§9 Group 1 + §12.1/§12.2 KB-CONTRACT-001 + §12.3 Group 1"
  leaf_ids: ["KB-CONTRACT-001"]
  documents:
    - {ref: "@docs/agent-native-atomic-review-2026-07-14.md", sha256: "4f1e8893fe03251c02ce9805300b94d6db00c34e032c978f3805c2b1f061e919", decision: "original tool-contract finding is revalidated against current handler metadata and the three canonical specs"}
    - {ref: "@docs/personal-knowledge-base-spec.md", sha256: "f239a20c4c71c8bf303252614b8dc4661b3f63cc00d0355d458cc962df008135", decision: "Personal KB product semantics now use the shared runtime-lane authority matrix"}
    - {ref: "@docs/personal-company-knowledge-tool-boundary-2026-07-10.md", sha256: "c3cc6ead427ae6aff1fbdb1bdf6af5039fffc8c194d2891adb551596109d98a6", decision: "personal/company tool boundary no longer applies a blanket owner or sensitivity read rule"}
    - {ref: "@docs/agent-permission-governance-spec-2026-07-07.md", sha256: "d346ad45591d3d01d6499735d116db37b9dda00284a9cf99103a8a0ad8080649", decision: "permission governance names the same owner-direct and explicit-grant runtime lanes"}
  source:
    ref: "backend/app/tools/handlers/knowledge.py"
    sha256: "a4481832b9dbdf82d42662e1e2e2073a39b17d9f0b6a7e3b5f1f8d7cd7615527"
    decision: "runtime metadata already states the truthful authority contract and is not changed by this leaf"
  evidence_sink: "EVID-G1-015"
```

- refute-first 结论：原报告把“读侧不用 sensitivity 过滤”整体当成 bug 过重。current-source 显示 sensitivity 主管 durable extraction/promotion 与跨 principal ceiling；interactive owner-direct 读 PL1–PL3 依据 authenticated owner policy + `agent_searchable`，而 autonomous owner Agent、shared/cross-user、A2A 与 subagent 必须有未过期 explicit grant，并绑定 requester、session/task purpose、delegation 和 sensitivity ceiling。真实缺口是三份规范仍保留 blanket 描述，模型、开发者与 reviewer 可以得到与 tool metadata/runtime 相反的结论。
- Red：新增 `backend/tests/architecture/test_personal_knowledge_contract_alignment.py`后，`cd backend && .venv/bin/pytest tests/architecture/test_personal_knowledge_contract_alignment.py -q` 首跑为 `1 failed, 1 passed`；失败精确命中三份 spec 缺少 canonical Personal KB read matrix，tool metadata 测试已经通过，证明断点在文档契约而不是已有 runtime 描述。
- Green/实现：三份 spec 都以 `personal-kb-read-authority-matrix-start/end` 包裹同一矩阵，并删除周边 blanket owner/grant/sensitivity 表述。测试逐字比对三份 matrix，同时导入 `search_personal_kb` / `read_personal_kb` 的真实 `meta.description`，明确拒绝旧的虚假文案 `Results are tenant-, owner-, sensitivity-, and grant-filtered`。复跑同一命令为 `2 passed in 2.15s`。
- 回归验收：`cd backend && .venv/bin/pytest tests/tools/test_personal_knowledge_tool.py tests/integration/test_personal_knowledge_cross_owner.py -q` 为 `22 passed in 8.32s`；覆盖 owner-direct、cross-owner、grant、sensitivity ceiling 与 PL4 boundary。本 leaf 没有 schema/migration/backfill/deploy，因为 source runtime 与 production data 零改动；强行重发不会产生新验收事实。
- 七原子：Input=authenticated runtime lane 与 Knowledge tool call；Authority=owner policy/`agent_searchable` 或 explicit grant + requester/purpose/delegation/sensitivity ceiling；Execution=已有 `search_personal_kb` / `read_personal_kb` handler；Evidence=canonical matrix、tool metadata 与 architecture/regression tests；Recovery=denied/unavailable/grant-required 状态保持 typed 且可重试；Consumption=模型与开发者从 tool schema/spec 获得相同契约；Acceptance=Red→Green + 22 个读权限回归。本 leaf 的七原子成立。
- 北极星裁决：硬结果只来自 authenticated principal、runtime lane、explicit grant、sensitivity ceiling 与 PL4 credential boundary 这些 Authority/Data Ingress 事实源；不读取自然语言决定权限、不裁剪 model context/output、不删工具、不改写 final。owner-direct 保留 CC 能力面，跨 principal 使用 Hive enterprise grant/ceiling，符合 capability-preserving determinism。
- 历史边界：本证据关闭当时 `B-01` 尚待独立 current-source 重验，Group 1 为 15/16 closed；后继 `EVID-G1-016` 已完成该 leaf 并把当前滚动状态收敛为 16/16 closed。Group 8 的 Knowledge/Memory durable extraction、T2/T3/soul、retention 与 replay 不由本证据关闭。103 分母、severity、owner 与 5 个 Missing 均不变。

#### EVID-G1-016：B-01 immutable HR RuntimeTask authority 与受信业务体边界

- `leaf_ids`：`B-01`；owner Group / 依赖 Group：Group 1 / Group 0。本证据只关闭 HR canonical blueprint 从 authenticated confirmation 到 durable RuntimeTask、受信 domain runner、revision/retry/reconciliation 的 authority 与 TOCTOU 断点；不要求后台 worker 伪装成一次模型 tool call，也不把 Group 2 Session truth、Group 3 root ledger、Group 8 schema-wait/retention 或全部 tool governance 并入本 leaf。
- 当前状态：`closed`。原报告“worker 直调固定业务体绕统一 tool throat”经 refute-first 复核后被校正：受信、参数封闭、无模型自由输入的 domain worker 直接调用唯一业务 lifecycle owner 本身不是治理缺陷；真实缺口是 confirmed blueprint、requester confirmation、RuntimeTask principal/session/delegation/idempotency 与执行前 live row 之间没有不可变、可重验的同一 authority frame。commit `b805dd67e` 已把这些事实绑定，并为 legacy、并发和未知副作用建立 fail-closed recovery。canonical 为 `closed:EVID-G1-016`。

```yaml
context_read_receipt:
  aa_entry: "§9 Group 1 + §12.1/§12.2 B-01 + §12.3 Group 1"
  leaf_ids: ["B-01"]
  documents:
    - {ref: "@docs/agent-native-atomic-review-2026-07-14.md", sha256: "4f1e8893fe03251c02ce9805300b94d6db00c34e032c978f3805c2b1f061e919", decision: "revalidate the original P3 trusted-business-body/tool-throat finding instead of accepting its wording"}
    - {ref: "@docs/agent-native-atomic-source-audit-2026-07-12.md", sha256: "c8598067672d6cffc6bbbb16ce9bd3862ba6e84fd32e5386536cf588446d28cd", decision: "preserve the durable confirmation-to-RuntimeTask and retry/cancel lifecycle already closed by R-003"}
    - {ref: "@docs/session-workspace-hr-atomic-closure-2026-07-10.md", sha256: "3548d87486ee994e7dc626be7849e48359dd49e502b96bcee1e26f625b95b519", decision: "preserve requester-only authenticated confirmation, exact version/hash, canonical DB payload and idempotent recovery"}
    - {ref: "@docs/agent-permission-governance-spec-2026-07-07.md", sha256: "d346ad45591d3d01d6499735d116db37b9dda00284a9cf99103a8a0ad8080649", decision: "actor capability cannot exceed the accountable authenticated principal and effect authority"}
    - {ref: "@docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md", sha256: "db8d895b283ecb7ae747ef7fbbb76591f32aba599052d24d759dcb31c83f7cb0", decision: "session permission never overrides tenant, resource, confirmation, sandbox or enterprise effect boundaries"}
  source_commit: "b805dd67e"
  source_paths:
    - "backend/app/api/hr_creation.py"
    - "backend/app/services/hr_creation_service.py"
    - "backend/app/services/hr_creation_reconciliation.py"
    - "backend/app/services/hr_provisioning_runtime.py"
    - "backend/app/services/hr_provisioning_runner.py"
    - "backend/app/tools/handlers/hr.py"
    - "backend/app/db_bootstrap.py"
    - "backend/app/core/rls_bypass_manifest.py"
    - "backend/alembic/versions/hr_runtime_authority_0715.py"
  evidence_sink: "EVID-G1-016"
```

- TDD Red：先后以独立回归证明未确认 draft 仍可建 task、worker 接受篡改 authority、claim 前 principal/session/delegation 漂移、blueprint payload TOCTOU、failed revision 原地覆写、已有/未知副作用仍生成 successor、exact retry 双 successor、revision/retry 锁顺序竞争、runner 把缺确认误判成普通失败、fresh bootstrap 未装 trigger、migration 会降级 terminal evidence、superseded row 可被复活，以及 reconciler query shape 未进入 RLS bypass manifest。每一项都在实现前得到确定失败，不以已有 happy-path 测试替代。
- Authority/执行实现：canonical JSON、payload hash 与 blueprint hash 只有一套 helper；`build_hr_provisioning_runtime_task()` 必须看到 requester 本人 authenticated confirmation，并把 tenant、requester、HR Agent、parent/root session、delegation chain、idempotency key、完整 payload digest、config/policy snapshot 写入任务。worker 只接收 typed `_runtime_authority`，claim 时按 `RuntimeTask -> HrCreationDraft` 的统一锁序重新读取 live rows 和全部 snapshot；任何 drift 都进入 typed `needs_reconciliation`、提升 claim fence、清 lease、写审计，且不会调用 domain side effect。
- 不可变与并发恢复：数据库 trigger 禁止 `confirmed/creating/provisioning/failed/completed/superseded` blueprint payload 改写，并禁止 superseded status 复活。failed revision 只有在 task 明确 terminal、`side_effect_risk=not_started`、无 task/draft claim、无 `created_agent_id`、全部 step 未尝试且无 receipt/timestamp 时才创建新 UUID/version successor；原 draft/task/step 证据保留。相同 payload retry 幂等返回唯一 successor，不同 payload 拒绝；双 revision 与 revision-vs-retry 真实 PostgreSQL 并发测试证明只有一个序列化结果。其它状态一律 `reconciliation_required`，不自动重放未知副作用。
- Migration/rollback：`hr_runtime_authority_0715` 以 `storage_blob_lifecycle_0715` 为唯一 parent，schema-owner transaction 内对所有 HR RuntimeTask `FOR UPDATE`；可证明有效的 runnable legacy task 回填 snapshot，active/claimed legacy worker 提升 task+draft fence 并隔离，authority 不完整者进入 append-only quarantine audit，`completed/killed/skipped/needs_reconciliation` 原终态保留。fresh `create_all` 同样安装 trigger；secure downgrade 故意保留 trigger、hash 与审计，不删除数据或重开旧漏洞。migration tests 覆盖 backfill、quarantine、terminal preservation、RLS 恢复、trigger 和 downgrade。
- Green/本地验收：最终定向命令 `pytest tests/security/test_rls_bypass_allowlist.py tests/services/test_hr_provisioning_runtime.py tests/tools/test_hr_handler.py tests/architecture/test_tool_runtime_single_entry.py tests/migrations/test_hr_runtime_authority_migration.py -q` 为 `76 passed in 27.90s`；Group 1 唯一收口全量 `cd backend && source .venv/bin/activate && pytest tests -q` 为 `7259 passed, 2 skipped in 274.04s`。scoped Ruff=`All checks passed!`，`alembic heads`=`hr_runtime_authority_0715 (head)`，`git diff --check` 与 staged diff check 均 exit 0；B-01 code-reviewer 最终 verdict=`APPROVE`，无剩余 P0/P1/P2 finding。
- Production preflight：部署前 schema readiness actual/expected=`storage_blob_lifecycle_0715`、130 tables、`issues=[]`；只读 schema-owner inventory 为 HR task=0、draft=0、active claim=0、linked-without-confirmation=0。因此 migration 没有修改、隔离或删除任何现存 HR 业务行。
- Exact-source deployment：从 `b805dd67e` Git archive 部署 backend=`2e09bebd-ac62-4166-99ef-e9a6a5a77a51`、frontend=`edfe7211-43b1-4941-bbb4-b59d738a413d`，均 `SUCCESS`。backend-api 初次 deployment=`83fedb46-7d85-4a87-b726-3c2596694e0d` 在 schema owner 完成 migration 前按 readiness fail closed，10 次重试耗尽后为 `REMOVED`；schema ready 后从同一 archive 重提 `4fc3d15f-8383-4ebc-9f87-5054159c39be` 为 `SUCCESS`。该恢复缺口继续归 Group 8 schema-wait fault matrix，不伪装成 B-01 未关闭。
- Live source/DB/health：本地 commit 与 backend、backend-api 的 `hr_provisioning_runtime.py`=`5c8e634f…03e11`、`hr_creation_service.py`=`6e4eb918…3e768`、`handlers/hr.py`=`e3647cd5…c0cef`、migration=`cbef5ae9…9a19` SHA-256 逐字一致。production schema head=`hr_runtime_authority_0715`；immutability trigger count=1、enabled=`O`；`runtime_tasks/hr_creation_drafts/audit_logs` 均 `ENABLE+FORCE RLS`；HR task/draft/active claim/quarantine audit/unprocessed task 全为 0。exact-code canary 的合法 frame `baseline_issues=[]`；篡改 payload 得到 `blueprint_payload_integrity_mismatch/immutable_blueprint_mismatch/config_snapshot_mismatch`；缺 requester confirmation 被拒绝，`side_effects=0`。backend health=`ok`、runtime role=`app_rls/strict/non-superuser/non-BYPASSRLS`、三 daemon 与 sandbox healthy，frontend HTTP=200。
- Storage 停止门：本次不执行 storage GC、quarantine、move、hardlink、CAS 或删除。发布后同挂载点 `df -B1 /data/agents` 为 total=`48,891,670,528`、used=`11,360,583,680`、available=`37,514,309,632`、usage=`24%`；相对 `EVID-G8-PRE-003` 仅是正常运行增量，核心数据与既有 manifests/receipts 全部保留。
- 七原子：Input=authenticated requester 对 exact draft version/hash 的结构化确认；Authority=tenant/requester/HR Agent/session/delegation/idempotency + immutable DB digest；Execution=durable RuntimeTask worker 调唯一受信 HR domain lifecycle owner，effect 前 live revalidation；Evidence=task snapshots、draft/step/claim state、trigger、audit、migration receipts、tests/deploy/live hashes；Recovery=task→draft 锁序、claim fence、safe successor、typed reconciliation、secure downgrade、same-archive retry；Consumption=confirm API、worker、reconciler、retry/cancel/revision API 与 HR UI 继续消费同一 canonical draft/task projection；Acceptance=Red→Green、PG concurrency/migration、full suite、review、三服务、production source/DB/fault/health。七原子均有当前真实消费路径。
- 北极星裁决：本 leaf 的 hard gate 只读取 Authority/Data Ingress、Side Effect、Evidence/Recovery 和 Machine Contract 的外部可验证事实；不扫描自然语言判断意图、不限制模型推理/输出、不删工具、不把平台 prose 当模型结论。模型仍负责设计 HR blueprint；平台只确保用户确认的那一份 blueprint 以正确 principal 执行一次。它保留 CC 的模型能力面，采用 Codex typed task/snapshot/recovery 工程增量，并保留 Hive-native HR/control-plane lifecycle，符合 capability-preserving determinism。
- 残余边界：本记录形成时 Group 1 为 16/16 closed，随后 Group 2 已建立 Session event/item/reducer 与 persist-before-publish，Group 3 已建立 root admission/coverage，Group 4 已建立 durable result/mailbox/fan-in，并分别以 `EVID-G2-001`–`014`、`EVID-G3-001`–`007`、`EVID-G4-001`–`006` 独立关闭。Group 8 仍负责固定 schema readiness 重试耗尽、durable Memory/Knowledge 与跨资产 retention。103 分母、severity、owner 与 5 个 Missing 均不变；当前下一施工入口为 Group 5。

### Group 2 共同验收边界（适用于 `EVID-G2-001`–`EVID-G2-014`）

- Context Read Receipt：施工前完整读取 `@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`，并对照 `@AGENTS.md`、`@docs/ccplus-north-star-contract-2026-06-24.md`、`@docs/runtime-model-agency-constraint-audit-2026-07-13.md` 与 `@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`。实际裁决是 CC/FreeCode lifecycle/capability floor 优先，Codex typed Item/Turn/approval/recovery 只作 additive engineering delta，Hive Memory/Skill/A2A/Workflow/Knowledge 保持一等能力；设计文档若与该原始诉求冲突，必须修正文档而不是削弱模型能力。
- source/commit：`c50fea9da` 建立 Session command/event/outbox、input admission/mailbox/dispatch、permission/control/tool、Round result/obligation/outcome、replacement saga、ready/cursor/writer epoch 与 frontend canonical store；`578e773ba` 修复 rolling cutover 时 legacy-open Run 的证据投影；`5ffdb464f` 删除 canonical event 到 legacy message reducer 的二次解释链。六个 additive migration 以 `session_v2_projection_epoch_0716` 为当前 head，secure downgrade 保留 canonical evidence/constraint，不重开 V1 写权威。
- TDD/Green：原始 focused failure set=`219 passed`；Group 2 backend focused=`700 passed`；`cd backend && source .venv/bin/activate && pytest tests -q`=`7466 passed, 2 skipped in 336.72s`。前端先后复现 projector 缺失、`assistant_text` 被误判 final、tool result 重复渲染；修复后 targeted=`4 files / 47 tests`、full=`119 files / 687 tests`，`tsc && vite build && bundle budget` 通过，7362 modules，AgentDetail/vendor budgets 均未越界。architecture ledger 另把 Session ADR/scenario 守恒从 12/13 扩到 30/30。
- production/fault/recovery：首轮 exact-source 部署捕获 `writer_epoch_rejected legacy run authority`；没有删事件或放宽所有 writer，而是以 additive projection epoch 区分“新写权威”与“既有 Run evidence projection”。同一 event 后续 `attempts=1`、`error=null`、成功 projected，open projection=`0`。最终 `5ffdb464f` 三服务 deployment：backend=`e59dd282-97e5-42cb-b67a-84836bed0e09`、backend-api=`77967ddf-77d8-4b70-84f4-f3b2d8299895`、frontend=`3eb6c453-90dc-422d-990e-96ee2ee0131b`，全部 `SUCCESS`；backend health=`ok`，runtime=`app_rls/strict/non-superuser/non-BYPASSRLS`，sandbox passed，三个 daemon healthy，frontend HTTP 200，最终部署日志无 `writer_epoch_rejected`/Traceback/IntegrityError/Exception/ERROR 命中。
- 完成边界：本共同证据只支撑下列 14 个 Group 2 owner leaf，不把完整 Session V2、103 总账或 G1–G30 全部标绿。其关闭时 Group 3/4/6/7/8/9/10 分别拥有 root admission、100-way result/fan-in、极端 context/compaction、跨渠道、durable Memory/Knowledge evidence、feedback/产品投影/历史 backfill/V1 cleanup 与最终重认证；其中 Group 3 现已由独立 `EVID-G3-*` 关闭，其余 owner 不变。Group 2 的 writer epoch 仍为 observe/legacy-open compatible；`v2_only` 最终切换只由 Group 9 在分母、观察窗、rollback artifact 与 browser E2E 齐备后执行。

#### EVID-G2-001：G-01A 模型与平台作者边界

- `leaf_ids`：`G-01A`；owner/依赖：Group 2 / Group 0–1；当前状态：`closed`。
- Red/实现：原路径能把固定 failure prose 填入 assistant/final。现在平台只提交 `error/tool_result/control_input` 等 typed mechanical item；assistant final 只能由 `SessionModelResultV2` 的 ordered source blocks 和 render owner 引用产生，zero-copy final 不复制或改写模型 bytes。
- 消费/验收：backend result/outcome tests 与 frontend source-block/finality tests 覆盖 live/replay；七原子共享上方共同证据。exact secret redaction 仍是唯一允许的 byte-level delivery guard。

#### EVID-G2-002：A-01 正文前缀不再决定失败

- `leaf_ids`：`A-01`；owner/依赖：Group 2 / Group 0–1；当前状态：`closed`。
- Red/实现：benign model text 含 error/failure/security 词时旧 heuristic 可改变 hard state。现在 terminal、failed、retryable 与 reconciliation 只由 sealed result、unresolved obligations、Run outcome 和 exact protocol status 决定；自然语言完全不进入 hard-outcome predicate。
- 恢复/验收：decisive content 位于尾部和 multi-block 样本仍 byte-faithful；失败只 hold/retry/reconcile，不生成平台终答。

#### EVID-G2-003：A-04 transport 与取消可观测恢复

- `leaf_ids`：`A-04`；owner/依赖：Group 2 / Group 0–1；当前状态：`closed`。
- Red/实现：Redis/ACK 抖动可让客户端把 running 猜成 cancelled 或 phase 漂移。现在 `session.ready`、connection generation、highest-contiguous cursor 与 durable `ControlInput` receipt 分别表达 transport/run；accepted 后才 cancelling，ACK 丢失以相同 idempotency key 查询原 receipt。
- 故障/验收：gap/out-of-order/duplicate、bootstrap failure、worker 暂不可达和 cancel settlement crash 均保持 typed、幂等、可恢复；真实浏览器多标签页终验仍属 Group 9。

#### EVID-G2-004：B-02 denied/unavailable/retryable 分态

- `leaf_ids`：`B-02`；owner/依赖：Group 2 / Group 1 authority；当前状态：`closed`。
- Red/实现：旧证据层会把 denied 与 unavailable 合并为一条自由文本。`SessionPermissionDecision`、tool/control receipt 与 result envelope 现在分别保留 decision、authority source、retryability、approval state、invocation/result identity。
- 消费/验收：模型与 UI 得到相同 typed fact，可以继续无关能力；deny 不冻结 reasoning，unavailable 不伪造无权限，effect-uncertain 进入 reconciliation 而非盲重试。

#### EVID-G2-005：B-03 governance outcome 不从 prose 反推

- `leaf_ids`：`B-03`；owner/依赖：Group 2 / Group 1 authority；当前状态：`closed`。
- Red/实现：旧消费者可能根据平台文本猜 allow/deny/approval。现在 permission request/version、authenticated authority snapshot、decision receipt、tool fence 和 control command 是唯一事实；模型正文与提示文字不能授予权限。
- 验收：permission version race、重复 decision、expired approval、denied/unavailable 与 resume tests 证明原 request/item 原位结算且不复制卡片。

#### EVID-G2-006：G-01B typed reducer 取代字符串 hard state

- `leaf_ids`：`G-01B`；owner/依赖：Group 2 / Group 0–1；当前状态：`closed`。
- Red/实现：UI 曾以 `includes('expired')` 等自然语言匹配决定 hard state。canonical reducer 只读取 event kind、item kind、lifecycle、typed status、sequence 与 receipt；compatibility event 留在明确 V1 quarantine path。
- 消费/验收：frontend reducer/timeline tests 包含 benign keyword 文本；显示内容不再能改变权限、终态、retryability 或 Header 状态。

#### EVID-G2-007：B-04 自然语言 failure 不改写 execution state

- `leaf_ids`：`B-04`；owner/依赖：Group 2 / Group 0–1；当前状态：`closed`。
- Red/实现：结果中出现 failure/error 词曾可驱动 warn/counter 甚至 terminal。现在 `SessionModelResultV2` 只 seal Round content，`NextRoundAssemblyPlanV2` 结算 tool/input/hook/compact obligations，`SessionRunOutcomeV2` 才提交 Run terminal。
- 恢复/验收：terminal commit timeout、worker restart、sidecar exception 与 multi-obligation crash 都从同一 outcome/obligation aggregate 恢复，不再次调用 Provider 或改写文本。

#### EVID-G2-008：D-KB4 Knowledge/tool failure 进入统一 typed result

- `leaf_ids`：`D-KB4`；owner/依赖：Group 2 / Group 1 Knowledge authority；当前状态：`closed`。
- Red/实现：Knowledge not-found/denied/unavailable 可被合成自由文本 warning。通用 tool decision/result contract 现在保存 exact authority outcome、invocation pair、retry/recovery action 与 safe projection，正文由模型自行解释。
- 边界：本 leaf 只关闭 Session 表达与消费，不宣称 Group 8 的 Enterprise Knowledge、抽取、retention 或 T0→T3 durable loop 完成。

#### EVID-G2-009：SES-ACCEPT-001 accepted input 同事务成为事实

- `leaf_ids`：`SES-ACCEPT-001`；owner/依赖：Group 2 / Group 0–1；当前状态：`closed`。
- Red/实现：旧路径到 worker 才产生 canonical accepted evidence，ACK/crash 会造 ghost/duplicate。command registry、HumanInput、InputAdmission、首个 event 与 outbox 现在在 admission transaction 中 read-or-create；worker 只 claim、bind、dispatch、settle。
- 故障/验收：same key/same payload 返回原 receipt；不同 namespace/payload/kind/target 返回 conflict 且零新 row/effect；Hook blocked/prevented 不预建 Turn。

#### EVID-G2-010：SES-ITEM-001 stable item/block/result lifecycle

- `leaf_ids`：`SES-ITEM-001`；owner/依赖：Group 2 / Group 0–1；当前状态：`closed`。
- Red/实现：stream delta 缺 stable identity，thinking 可被聚入 final 附件。event/item contract 现在保留 item/block/result/invocation/parent/render-owner identity、scope、ordinal、visibility 与 lifecycle；private reasoning 只留授权元数据，safe summary 与 final 分离。
- 消费/验收：live/history/reload/reconnect/resume 归约同一 item；tool call 与恰好一个 matching result 配对，unknown text 保持 unknown，final 只显示一次。

#### EVID-G2-011：SES-PROJECTION-001 redaction 不删除关联 identity

- `leaf_ids`：`SES-PROJECTION-001`；owner/依赖：Group 2 / Group 1 visibility；当前状态：`closed`。
- Red/实现：旧 user/live projection 会丢 parent、invocation、result 或 source identity。现在 projection 可精确移除未授权 content bytes，但保留 event/item ID、sequence、kind、lifecycle、parent/result/invocation refs 与 redaction marker。
- 验收：不同 visibility snapshot 的结构 identity 同构；无权视图不能侧漏正文，operator/replay 仍能关联机械证据和恢复链。

#### EVID-G2-012：SES-PROSE-001 平台不冒充 reasoning/final

- `leaf_ids`：`SES-PROSE-001`；owner/依赖：Group 2 / Group 0–1；当前状态：`closed`。
- Red/实现：固定“正在分析/LLM Error/权限失败”等平台 prose 可落为 assistant。现在 phase 只接受 provider 显式值；无 phase 写 `assistant_text(unknown)`，sidecar/permission/runtime failure 写 typed infrastructure item，平台不能追加、替换或压制模型 final。
- 验收：frontend test 明确证明 `assistant_text` 非 terminal；private reasoning 不泄漏，safe summary 和 zero-copy final 各按 source refs 显示一次。

#### EVID-G2-013：SES-TRANSPORT-001 persist-before-publish 与连续 cursor

- `leaf_ids`：`SES-TRANSPORT-001`；owner/依赖：Group 2 / Group 0–1；当前状态：`closed`。
- Red/实现：transcript commit 与 live publish 曾分离，DB fail 后仍可能 publish。canonical event、sequence range 与 outbox 同事务；publisher 校验 envelope hash/event/session/sequence，至少一次重发以 event ID 去重；未 commit bytes 永不 publish。
- 故障/验收：32-way sequence allocation、重复 claim、publish/ACK crash、gap 1/3/2、backpressure exhaustion 与 reconnect history catch-up 均有定向测试；生产 open projection 为 0，最终日志无 writer epoch/DB 异常。

#### EVID-G2-014：SES-CONSUMER-001 canonical store 单一 reducer

- `leaf_ids`：`SES-CONSUMER-001`；owner/依赖：Group 2 / Group 0–1；当前状态：`closed`。
- 最后一次 refute-first 发现：backend V2 已正确，但 `AgentDetail.applyTranscriptToSession` 又把相同 raw event 送入 legacy `applyTranscriptEvent`，`timelineModel` 还会把任意非空 assistant text 当 final；这证明只看 producer 会产生虚假闭环。新增 Red 分别得到 projector function missing、`assistant_text`→final 和 tool pair 两条消息的确定失败。
- 实现/消费：canonical V2 event 现在只进入 `SessionEventStore` 一次；页面消息由 store snapshot 投影，`SessionItemV2` 保留 payload/actor/visibility/display/result/invocation/parent/render-owner/occurred-at；tool result child 合并到对应 call，final 从 ordered source blocks zero-copy 解析，legacy/V1 仍在显式 compatibility path。
- Green/边界：targeted `47 passed`、frontend full `687 passed`、build/budget 全绿。Group 9 可在此 store 上完成 Workbench/right rail/多标签页/browser/backfill/V1 cleanup，但不得重新引入第二 reducer 或 heuristic finality。

### Group 3 共同验收边界（适用于 `EVID-G3-001`–`EVID-G3-007`）

- Context Read Receipt：施工前重读本文 Group 3 路由、`@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md`、`@docs/ccplus-subagent-team-skill-mcp-hooks-parity-audit-2026-06-27.md`、`@docs/dynamic-workflow-harness-semantics-2026-06-24.md` 与 `@docs/runtime-budget-control-plane-plan-2026-07-03.md`，并逐条对照当前 direct/Subagent/A2A/Team/Workflow、RuntimeTask、budget、coordination 与 Session event 实现。裁决保持 CC/FreeCode delegation/workflow 能力面，采用 Codex-style typed item/lease/recovery 作为 additive engineering delta，同时保留 Hive-native Team、Workflow 与企业治理；平台只约束 authenticated authority、资源 admission、lifecycle、evidence 与 exact machine contract，不判断模型该委派什么任务。
- source/commit/migration：实现 commit=`01e979bb3`，41 个 owned backend/migration/test 文件，`4245 insertions(+), 291 deletions(-)`；其它 session 的 `.env.example`、`.ultra/**`、`.artifacts/**` 未 stage、reset 或部署。新增 `runtime_root_items` 作为 durable mechanical coverage ledger，包含 root/parent/task/session/agent、intent/work type/target/path、admission/state、budget/approval、child session/result refs 与 recovery lease/version；migration=`runtime_root_ledger_0716` 为 additive head，生产启用并强制 RLS，重装 paired parent/child session authority contract。
- 实现闭环：A2A 与 Subagent 都先完成 budget decision 和 durable RuntimeTask/root item，再发布 coordination 或建立 child-session projection；Team 在逐成员 admission 前写入完整 requested set 和 exact recovery intent，并由有租约、有限重试、最终 hold 的 worker 恢复 producer crash；direct web/channel、A2A/Subagent、Team、Workflow 共用 `root_runtime_task_id`、root coverage 与单调 state transition。Workflow gate/budget wait/resume 更新同一 root item；RuntimeTask approve/reject/cancel/terminal 也推进同一事实，不从 assistant prose 猜状态。
- TDD/Green：Red 覆盖 malformed root ID、ghost child/session、lease expiry 后重复 signal、durable cycle、terminal late completion、approval wait 误唤醒、Team producer crash/不完整 intent/无限 retry、real-PG 100-way/RLS/authority 与 workflow suspended mapping。Group 3 focused=`480 passed`；`cd backend && source .venv/bin/activate && pytest tests -q`=`7508 passed, 2 skipped in 332.99s`；frontend SESSION-G9 `timelineModel` targeted=`31 passed`，full=`119 files / 688 tests`，`tsc + vite build + bundle budget` 通过；41 个 Python 文件 Ruff check/format、`git diff --check` 与 `alembic heads` 全绿。
- capacity/fault/recovery：pure coverage 与真实 Team fanout 都对 `1/10/25/50/100` 参数化，断言 `requested = admitted + deferred + not_admitted` 且 `expected = admitted`；100-way real PG fixture 同时验证 tenant isolation。producer lease expiry 可 reclaim；缺 exact message/operation/ordinal 的 recovery intent 只能 hold，不由平台编造工作；retry exhaustion 进入 `needs_reconciliation`/hold 而非自旋。A2A coordination recovery 以 `runtime_task_id` 复用原 signal，cycle/terminal 在进程重启后仍是 durable 事实。
- production/fault/recovery：从 `01e979bb3` Git archive 发布。backend=`b67055e5-9dbc-4e4d-903e-14fe8322b728`、frontend=`20ca32aa-7682-4f6a-b6a5-ceebcca0fdad` 均 `SUCCESS`。backend-api 首次 deployment=`ebd727ba-dc7b-4044-a843-ba45922e6bec` 在主 backend 完成 migration 前按 schema readiness fail closed，10 次重启后为 `REMOVED`；schema ready 后从同一 archive 重提 `dd748dd4-ea68-4d94-a5bb-4fda7ecd7b90` 为 `SUCCESS`。该 rolling 顺序证据不放宽 readiness，也不伪装首轮成功；后续发布先等 schema owner 成功再启动只读 API。
- production truth：backend 与 backend-api 均报告 actual/expected head=`runtime_root_ledger_0716`、checked tables=`145`、checked triggers=`4`、issues=`[]`、ready=`true`；backend-api 日志有 `RLS runtime role verified: role=app_rls` 与 `Application startup complete`。public backend health=`ok`，runtime role=`app_rls/strict/non-superuser/non-BYPASSRLS`，evolution/trigger/workflow daemon healthy，RuntimeTask worker running 且暴露 `team_fanout_claimed/recovered/retried/needs_reconciliation` 指标；frontend HTTP/2 200。本次生产验收不插入、删除或改写客户 root item。
- 七原子：Input=模型/Workflow 决定的 exact requested intents；Authority=tenant/principal/root user/session/agent/budget frame；Execution=RuntimeTask worker、coordination gateway、Team/Workflow runtime 的唯一 governed paths；Evidence=RuntimeTask、root item、coordination signal、Session event 与 health metrics；Recovery=lease reclaim、idempotent signal、task/root replay、bounded retry/hold 与 terminal CAS；Consumption=direct/Subagent/A2A/Team/Workflow、budget approval、worker 与 Session projection 真实读写同一 ledger；Acceptance=Red→Green、real PG/RLS、capacity/fault、全量回归、三服务 exact-source 与 production readiness/health。七原子在 Group 3 范围内闭环。
- 后继状态：Group 3 自身只关闭 root admission/coverage/path/approval/terminal；其后 Group 4 已用 `EVID-G4-001`–`006` 独立关闭 result manifest、mailbox lease/CAS、integration epoch、partial/late/duplicate 与 synthetic return storm。Group 5 仍拥有 fleet fairness，Group 6 仍拥有 context/compaction/output 与运行中 breaker，Group 7 仍拥有跨渠道交付，Group 9 仍拥有最终 UI/browser/backfill。当前总账为 43/103 closed，5 个 Missing 不变。

#### EVID-G3-001：A2A-ADMISSION-001 durable enqueue 先于 coordination

- `leaf_ids`：`A2A-ADMISSION-001`；owner/依赖：Group 3 / Group 0–2；当前状态：`closed`。
- Red/实现：旧路径可先发 delegation signal、后丢 RuntimeTask，形成 queued ghost。现在 budget admission 后先持久化 `RuntimeTask + RuntimeRootItem`；coordination publish 失败进入 `needs_reconciliation`，等价工作持有 lease 时进入 `not_admitted` 并释放 reservation。worker/recovery 只对这条 durable task 发布 signal。
- 恢复/验收：`test_coordination_recovery_reuses_existing_signal_after_lease_expiry` 证明 lease 过期后按 `runtime_task_id` 找回原 signal，不重复通知；signal metadata、coordination key、lease 与 root ID 回写原 task。

#### EVID-G3-002：SUBAGENT-ADMISSION-001 无 ghost child session

- `leaf_ids`：`SUBAGENT-ADMISSION-001`；owner/依赖：Group 3 / Group 0–2；当前状态：`closed`。
- Red/实现：旧 Subagent 可在 durable enqueue 失败时留下 child session。现在 `start_subagent_run` 先完成 reservation、RuntimeTask 与 root item，再建立 child-session projection；projection 中断保留 `recovery_action=rebuild_child_session_projection`，由原 task 恢复而不是新建第二 child。
- Authority/验收：frozen Session contract 同时接受合法 parent endpoint 和与 RuntimeTask 绑定的 child endpoint，并拒绝不成对 agent/session；real-PG migration/authority fixture 已通过。

#### EVID-G3-003：A2A-CYCLE-001 durable path 与 restart cycle guard

- `leaf_ids`：`A2A-CYCLE-001`；owner/依赖：Group 3 / Group 0–2；当前状态：`closed`。
- Red/实现：进程内集合无法在重启后阻止 A→B→A。现在 ancestor/target path 写入 root item 与 task delegation chain；重复 target 产生 `runtime_root_cycle_detected`、`not_admitted`，不发布 signal、不启动 child effect。
- 验收：nested/shared-trace、restart payload 与 root path 单元/真实持久化测试证明 cycle 是 durable mechanical fact，不依赖自然语言、模型名称或进程内缓存。

#### EVID-G3-004：A2A-TERMINAL-001 单调终态

- `leaf_ids`：`A2A-TERMINAL-001`；owner/依赖：Group 3 / Group 2 typed outcome；当前状态：`closed`。
- Red/实现：late child completion 曾可能覆盖 kill/cancel。root transition 在锁定当前行后只允许合法前向边；`completed/failed/killed/skipped/cancelled/not_admitted` 全部 sealed，RuntimeTask 与 web/channel finalization 也保留既有 terminal。
- 验收：所有 terminal state 的参数化 late-completion、terminal cancel race、completed 后 late kill 与 worker reconciliation tests 均通过；无平台文本参与状态判断。

#### EVID-G3-005：TEAM-FANOUT-001 完整 requested set 与 crash recovery

- `leaf_ids`：`TEAM-FANOUT-001`；owner/依赖：Group 3 / Group 0–2；当前状态：`closed`。
- Red/实现：旧 Team 逐成员 create，进程在第 N 个 member 崩溃时没有总分母。现在先用稳定 operation/ordinal/intent key 提交完整 requested set，再逐项 budget admission/durable enqueue；producer lease 未完成的 item 由 cross-tenant audited claim、tenant-scoped delivery 的 recovery worker 接管。
- 验收：`1/10/25/50/100` mixed admission 曲线守恒；producer lease expiry reclaim、exact intent 校验、重试后 hold、worker health counters 与 fake/real DB fixtures 全绿。recovery 不能补写缺失 message 或 semantic defaults。

#### EVID-G3-006：SUBAGENT-APPROVAL-001 exact durable approval intent

- `leaf_ids`：`SUBAGENT-APPROVAL-001`；owner/依赖：Group 3 / Group 1 authority + Group 2 control item；当前状态：`closed`。
- Red/实现：foreground over-budget 过去可能只返回提示，重启后不知道批准哪次工作。现在 waiting approval 绑定 exact budget run/reservation、RuntimeTask、root item 与 `approval_ref`，不 wake worker；approve/reject 幂等推进原 task/root state，root intent 不能重新绑定另一 task。
- 验收：Subagent、Team 与 Workflow waiting-approval fixtures、budget exact pending-task resume/reject、malformed root ID 和 immutable root-task binding tests 通过。

#### EVID-G3-007：ROOT-TREE-001 统一 mixed-runtime root coverage

- `leaf_ids`：`ROOT-TREE-001`；owner/依赖：Group 3 / Group 0–2；当前状态：`closed`。
- 实现/消费：direct web/channel、Subagent、A2A、Team 与 Workflow 都写同一 `root_runtime_task_id` 下的 `requested/admitted/deferred/not_admitted/expected/terminal/waiting_approval` 事实；RuntimeTask create/update、budget decision、Workflow gate/resume 与 terminal settlement 推进同一 item。frontend `timelineModel` 的 Workflow segment、gate waiter、dedupe/count 继续消费 canonical projection，SESSION-G9 的 gate/wait/restart/resume 因此不再产生 ghost running。
- 验收/边界：coverage conservation、100-way tenant isolation、Workflow suspend/resume/kill、invalid root identity、frontend `timelineModel` targeted=`31 passed` 与 production worker/readiness 均通过。`result_refs_json` 在本证据中只保留稳定消费槽位；随后 Group 4 以独立 result object/outbox/mailbox/page 合同关闭 return storm，不能把该后继完成倒填成本证据自身的范围。

#### EVID-G4-001：E-2 A2A completion 可恢复唤醒原 parent

- `leaf_ids`：`E-2`；owner/依赖：Group 4 / Group 0–3；当前状态：`closed`；code commit=`4e385d423`。
- Context Read Receipt：FreeCode `7dc15d6c8` 的 `src/tasks/LocalAgentTask/LocalAgentTask.tsx`、`src/tools/TaskOutputTool/TaskOutputTool.tsx` 与 `src/utils/task/diskOutput.ts` 证明 CC 底线是 result 可恢复、通知只发生一次且完整输出仍可按 ref/file 读取；Codex `5c19155cb` 的 `codex-rs/core/src/tools/handlers/multi_agents/wait.rs` 与 `multi_agents_common.rs` 提供 typed wait/status 工程增量。Hive 保留模型读取完整结果和解释结果的语义主权，只把 authority、result bytes、通知顺序与 recovery 机械化。
- Red/实现/消费：A2A completion 过去虽能产生 child terminal，却没有统一 durable parent integration。`app.agents.orchestrator` 的 `a2a_delegation` completion 现在进入同一 immutable result/outbox；`agent_session_continuation` 把 ref-only page 作为 system runtime context 投影到原 parent Session，active parent 进入下一合法 Round，inactive open parent 启动 continuation，artifact/result refs 原样保留，不伪装成新的 user turn 或平台 final。
- 验收：A2A typed notification、active/inactive parent、artifact ref、runtime action projection、terminal-session rejection 与 outbox retry/ACK tests 均进入 Group 4 focused/full 分母；共同 migration、生产部署、source hash 与全量结果见 `EVID-G4-006`。该 leaf 只关闭 completion→parent wake，不外推为 Group 7 多渠道 A2A 产品合同完成。

#### EVID-G4-002：XCB-RESULT-001 immutable result truth 与 governed reader

- `leaf_ids`：`XCB-RESULT-001`；owner/依赖：Group 4 / Group 0–3；当前状态：`closed`；code commit=`4e385d423`。
- 实现：新增 `runtime_result_objects`，以 `tenant/source_kind/source_run_id/sha256` 唯一保存完整 canonical bytes、schema、media type、encoding 与 size；`runtime_notification_outbox` 删除 `summary/artifacts_json`，只留 `result_object_id/result_ref/result_sha256/result_size_bytes/artifact_count` 和机械路由元数据。更高 authority rank 产生新 immutable revision，不覆盖旧对象；低/同 rank 被拒绝时不产生 orphan object。
- 消费/authority：governed `read_runtime_result` 只接受当前 principal 可见且由 outbox/page manifest 授权的 hash ref，逐次校验 expected hash/size；旧 revision 即使新 epoch 已交付仍可按原 principal-bound page manifest 完整读取。模型拿到的是无损 payload，不是平台摘要；unauthorized/not-found/stale/mismatch 返回 typed failure。
- migration/production：`runtime_result_fanin_0717` 将 147 条历史 inline outbox 无损回填为 147 个 result object；production read-only 对账为 `bad_result_sizes=0`、`bad_sha256=0`、`orphan_outbox_results=0`、`missing_ref_facts=0`，forbidden inline columns=`0`、required ref columns=`8`。真实 PG upgrade→downgrade test 证明 decisive tail、artifacts、private metadata 均可还原，三张新表全为 RLS ENABLE+FORCE；共同 acceptance 见 `EVID-G4-006`。

#### EVID-G4-003：CONC-FANIN-001 100-way ref-only bounded fan-in

- `leaf_ids`：`CONC-FANIN-001`；owner/依赖：Group 4 / Group 0–3；当前状态：`closed`；code commit=`4e385d423`。
- Red/实现：真实 PostgreSQL fixture 并发提交 100 个各约 1 MiB 的 child result，旧形态会把 raw summary/artifact 重复带入 parent。新实现先验证并持久化完整 bytes，再按 parent mailbox sequence 生成 4 个各 25 refs 的 immutable integration page；page manifest 只含 result ref/hash/size/status/source/coverage，parent runtime context 不含 `summary/model_context/artifacts` body。
- capacity/acceptance：`test_100_one_mib_results_are_lossless_and_coalesced_into_four_ref_only_wakes` 验证 100 个对象逐一 SHA-256/size 完整、决定性尾部可恢复、sequence=`1..100`、epoch=`1..4`、event/page=`4`；每页 runtime context `<16,000` chars、四页合计 `<64,000` chars，而 raw result 约 100 MiB。5 个关键 real-PG tests=`5 passed in 10.95s`，Group 4 focused matrix=`81 passed in 28.24s`；这证明 bounded ref projection，不冒充 100 个付费模型同秒 completion 的 provider/成本曲线，后者继续列在 §13.2。

#### EVID-G4-004：CONC-WAKE-002 durable integration epoch 与 wake fence

- `leaf_ids`：`CONC-WAKE-002`；owner/依赖：Group 4 / Group 0–3；当前状态：`closed`；code commit=`4e385d423`。
- 实现：新增 `runtime_result_integration_pages`；同一 parent 的 page 使用唯一 integration epoch、manifest hash、mailbox range、item count、coverage、claim token、claimed_by、lease、attempt、receipt 与 terminal status。worker 只能 claim 最早可交付 page；存在更早 `prepared/processing` page 时后续 epoch typed defer，stale claim/page/row ACK 全部拒绝。
- fault：interleaved root scopes 按全局 parent mailbox contiguous run 保序，不把 A1/B2/A3 重排成 A1/A3/B2；两个 page worker 不能先交付 epoch 2；delivery commit 后 ACK 丢失由 event dedupe + lease reclaim 恢复，不重复 parent wake。`runtime_result_integration_pages_total/items_total` 按 delivery mode/outcome 暴露 bounded metrics，production `/metrics` 已出现完整 HELP/TYPE contract。

#### EVID-G4-005：WF-PARTIAL-001 typed partial/late/duplicate/revision

- `leaf_ids`：`WF-PARTIAL-001`；owner/依赖：Group 4 / Group 0–3；当前状态：`closed`；code commit=`4e385d423`。
- 实现/消费：Workflow、Team、Subagent、A2A、Trigger、Approval 与 direct RuntimeTask terminal 共用 `CompletionNotification`→result object→outbox→page 合同。每个 item 保留 terminal status、task/source kind、root scope、mailbox sequence、result ref 与 artifact count；partial success/failure/cancel/late result 可从 durable rows 重算，不由自然语言或全量 barrier 猜测。
- fault/恢复：duplicate enqueue 命中 deterministic delivery identity；更高 payload rank 到达已交付旧版本后产生独立 result revision 与新 integration epoch，旧 ref 不失效；低/同 rank 不覆盖 authority，也不创建 orphan；late completion 不回退 Group 3 已 sealed 的 root terminal。real-PG partial/duplicate/late、authoritative revision、old-ref readability、final-before-crash tests 均为 Green；共同 full/production acceptance 见 `EVID-G4-006`。

#### EVID-G4-006：CONC-MAILBOX-001 独立 mailbox CAS/lease 与 Group 4 总验收

- `leaf_ids`：`CONC-MAILBOX-001`；owner/依赖：Group 4 / Group 0–3；当前状态：`closed`；code commit=`4e385d423`，37 owned files，`4110 insertions(+), 134 deletions(-)`；其它 session 的 `.env.example`、`.ultra/**`、`.artifacts/**` 未 stage、reset 或进入 archive。
- Red→Green：新增 cursor-first-create race、interleaved roots、parallel page order、post-delivery revision/old-ref 与 final-before-crash regressions；四个新增边界测试首跑=`FFFF`，第一次 full suite 真实暴露 deterministic cursor PK race 为 `1 failed, 7521 passed, 2 skipped`。修复改为 no-target `ON CONFLICT DO NOTHING`、独立 mailbox row/unique sequence/version、prior-page fence、revision page identity 与 rank-rejection early return；critical real-PG=`5 passed`，focused=`81 passed`，最终 backend `pytest tests -q`=`7525 passed, 2 skipped in 375.96s`，Ruff all passed、`git diff --check` clean、Alembic single head=`runtime_result_fanin_0717`。
- frontend/构建：full Vitest=`119 files / 688 tests`；`npm run build`/bundle budget 通过，AgentDetail=`321910/380000` bytes、gzip=`89060/115000`，vendor=`591449/620000`、gzip=`186474/200000`。Group 4 没有新增第二个 UI reducer；parent 继续消费 Group 2 canonical Session projection。
- migration/rollback：real-PG migration 证明旧 `summary/artifacts_json/metadata` lossless upgrade 为 immutable bytes + ref-only routing，并可 downgrade 恢复旧列/内容；三张新表和 outbox 约束、FK、RLS/FORCE RLS 均通过。production schema-owner 首发迁移被旧已停止 backend PID `20758` 的未提交 outbox reader transaction 阻塞；只读锁图确认它是 migration PID `24309` 的直接 blocker 后，执行带 `20758 = ANY(pg_blocking_pids(24309))` 前置条件的单 PID terminate，PostgreSQL 只回滚该旧事务，未删除或改写客户结果。随后 head/readiness 正常前进，未放宽 schema gate。
- exact-source production：backend=`b16d1c5b-c28a-480e-896b-a8dd2ffd153a`、backend-api=`da84f7ae-0157-4551-95d0-4f93dbe0f029`、frontend=`96090a47-4267-488a-b0f5-94a5c18e6667`，均 `SUCCESS` 且 deployment message 指向 `4e385d423`。backend/backend-api 的 `runtime_notification_outbox.py`、`runtime_result.py`、migration SHA-256 与本地逐文件完全一致；production actual/expected head=`runtime_result_fanin_0717`、148 tables/4 triggers、issues=`[]`，四张相关表 RLS ENABLE+FORCE、四条 tenant policy、29 个关键 FK/unique/check 约束在位。public backend health=`ok`、runtime role=`app_rls/strict/non-superuser/non-BYPASSRLS`、三 daemon/sandbox/worker healthy，frontend HTTP/2 200；backend-api 日志为 `Application startup complete`。
- 七原子：Input=admitted child/runtime terminal；Authority=Group 1 principal + Group 3 root/item + tenant RLS；Execution=immutable result commit→ref-only outbox→ordered page→parent continuation；Evidence=result hash/size、sequence/epoch、manifest、receipt、metrics、canonical Session event；Recovery=claim/lease/CAS、stale fence、retry/dead-letter、revision、old-ref reader、migration downgrade；Consumption=A2A/Subagent/Team/Workflow/Trigger/Approval/RuntimeTask parent Session + governed tool read；Acceptance=Red→Green、real-PG、100×1 MiB、full backend/frontend、migration/backfill/rollback、三服务 exact-source 与 production canary。Group 4 因此为 6/6 closed；Group 5 fleet fairness、Group 6 全 Context Resource Plane、Group 7 跨渠道与 Group 9 最终 UI/browser 仍独立 open。

#### EVID-G1-017：Peer A2A `delegation_run` server-side read-only authority

```yaml
context_read_receipt:
  aa_entry: "§9 Group 1 + §12.1/§12.2 P1-004"
  leaf_ids: ["P1-004"]
  documents:
    - ref: "@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md §7.4 / §16.3"
      role: "authority"
      decision_consumed: "Peer Digital Employee A2A 有 owner 可见但 read-only 的 task-scoped delegation_run；它不能接管目标员工输入权"
    - ref: "@docs/runtime-model-agency-constraint-audit-2026-07-13.md Model Agency Boundary"
      role: "authority"
      decision_consumed: "硬闸只读取可信 session_kind 与 authenticated action，不扫描自然语言、不缩小模型推理能力"
  source_baselines:
    hive_head: "backend Red start=2689695c0528185339414d3b8f2d50a8e6d2aa2a; stable Session frontend base=92500e4c0"
    freecode_head: "not-applicable: Hive enterprise peer-employee authority delta"
    codex_head: "not-applicable: Codex typed thread ergonomics does not define Hive peer ownership"
  conflicts_or_deltas:
    - "DTO/UI 声明 read_only=true，但 server mutation authority 未消费 session_kind；旧 closed 证据因此失效"
  evidence_sink: "EVID-G1-017"
```

- Red：owner 可对 `delegation_run` 调 start/rename/delete/Team/Workflow/Plan 等 mutation，manager override 也能越过 UI；工具栏只读不是 authority。
- Green：`app/core/permissions.py::require_writable_session` 以 canonical `session_kind=delegation_run` 返回 typed `409 session_read_only`；`authorize_session_action(require_writable=True)`、Session V2 mutation authority与全部 legacy mutation API 在 effect 前调用同一硬闸。read transcript/workbench/export/feedback sidecar 仍可用。
- path proof：新增 REST `start_session_run` 回归证明 live route 在 `submit_live_human_input` 前拒绝；owner 与 manager override 双向测试证明 governance 不能把 read-only 变 writable。
- tests：初始家族 Red 属本轮 `9 failed`；authority leak 的独立 Red 为 outsider 在 read-only gate 提前执行时错误得到 409 而不是 403。修复后协作族 focused backend=`382 passed in 11.84s`，其中 owner/manager mutation typed 409、outsider 403、read transcript/workbench allow 均有 exact route 回归；backend full=`7567 passed, 2 skipped in 371.77s`。
- commit / deploy：原子实现 commit=`b9852f37f`；backend=`a64092a1-395b-48c2-9853-83ff9b45c2ae`、backend-api=`ab14d317-3c29-4b74-9d31-341e778f92b7`、frontend=`3ff852aa-e078-464c-80c7-7568b1272a2a` 均 `SUCCESS`。首次 backend-api=`3c50f41e-aa94-4ff2-96d6-1f518d3b4919` 在 writer migration 前按设计 fail-closed，schema ready 后同 commit archive 重提成功；production deny/read canary 未执行。
- status：`in_progress-deployed-pending-canary`。

#### EVID-G2-015：Sub-agent、Agent Team 与 Peer A2A typed runtime section 分流

```yaml
context_read_receipt:
  aa_entry: "§9 Group 2 + §12.1/§12.2 SES-CONSUMER-001"
  leaf_ids: ["SES-CONSUMER-001"]
  documents:
    - ref: "@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md §7.4 / §16"
      role: "design"
      decision_consumed: "轻量 Sub-agent、同 lead Agent Team、跨 agent_id Peer A2A 是三种产品身份，不能共用 generic delegation 视图"
    - ref: "@docs/session-timeline-projection-contract-2026-07-04.md"
      role: "acceptance"
      decision_consumed: "backend typed section 与 frontend reducer 必须一一对应，raw 只作 operator evidence"
  source_baselines:
    hive_head: "backend Red start=2689695c0528185339414d3b8f2d50a8e6d2aa2a; stable frontend integration base=92500e4c0"
    freecode_head: "7dc15d6c8fb0c40c7fcc02ce9b58204324252632"
    codex_head: "5c19155cbd93bfa099016e7487259f61669823ff"
  conflicts_or_deltas:
    - "backend 旧 _SUBAGENT_TASK_TYPES 同时包含 delegation；frontend RuntimeSectionsModel 原先没有 peer_a2a"
    - "backend runtime section 是 {schema,key,count,items} envelope，frontend 原先只接受 raw array，导致 live section 即使存在也会被读成空"
    - "并行 Session disclosure 修改已由独立 commit 92500e4c0 落定；本提交只包含 A2A typed consumer 的剩余 diff，不复制或重交其改动"
  evidence_sink: "EVID-G2-015"
```

- backend Green：`runtime_sections` 新增 `peer_a2a`；只有 `subagent` 进入 `subagents`，`delegation/a2a_delegation` 进入 `peer_a2a`，Team/Workflow/background 保持独立，`runs` 不再重复展示已分类 task，`raw` 保留完整 operator truth。section DTO 保持 `{schema,key,count,items}`，不另造第二协议。
- canonical item Green：ThreadItem union 新增 `agent_team_activity` 与 `peer_a2a_activity`，保留 `subagent_activity`；exact event map 与 metadata marker 可把 legacy `child_session/agent_task_notification` 正确分流。A2A item 明示 `read_only=true`，平台 failure 仍是 typed status，不作为 assistant prose。
- frontend path proof：`timelineModel` 同时消费 envelope `.items` 与兼容 raw array，分别建立 `agentTeams/peerA2A/subagents`；Runtime Console 具有独立 A2A segment、typed waiter/failure、可进入 read-only `delegation_run` window。`threadItemReducer` 与 `ThreadItemRenderer` 消费同一 generated discriminated union，不从标题或 summary 猜类型。
- migration/backfill：`collaboration_runtime_closure_0717` 仅对命中 exact event/metadata markers 的历史 collaboration rows 重分类，不全表把 `child_session` 粗暴改写；Peer A2A 历史 child Session 通过 tenant + normalized UUID + RuntimeTask task_type 精确绑定。
- tests：backend 协作族=`382 passed in 11.84s`；backend full=`7567 passed, 2 skipped in 371.77s`；frontend ThreadItem/reducer/timeline=`3 files / 51 passed`，A2A runtime-panel exact test=`1 passed / 108 skipped`，frontend full=`120 files / 709 tests`。`npm run build` 通过，7363 modules，AgentDetail=`335499/380000` bytes、gzip=`92583/115000`，vendor=`591449/620000`、gzip=`186474/200000`。
- commit / deploy：backend、frontend A2A typed consumer、migration、测试与证据进入原子 commit `b9852f37f`；Session disclosure 独立 commit `92500e4c0` 是其父级稳定 frontend 基线。三服务 deployment IDs 与 `EVID-G1-017` 相同且均 `SUCCESS`；browser collaboration canary 未执行。
- status：`in_progress-deployed-pending-canary`。

#### EVID-G3-008：terminal root、Team model 与 hidden member Session 回归闭环

```yaml
context_read_receipt:
  aa_entry: "§9 Group 3 + §12.1/§12.2 A2A-TERMINAL-001/TEAM-FANOUT-001/ROOT-TREE-001"
  leaf_ids: ["A2A-TERMINAL-001", "TEAM-FANOUT-001", "ROOT-TREE-001"]
  documents:
    - ref: "@docs/session-v2-cc-codex-alignment-contract-2026-07-14.md §12 / §16 / §18"
      role: "design"
      decision_consumed: "RunOutcome、RuntimeTask 与 RuntimeRootItem terminal 必须同事务单调；Team member 是 lead 内部具名 worker，不是普通用户 Session"
    - ref: "@docs/subagent-agent-team-cc-parity-audit-2026-07-03.md"
      role: "design"
      decision_consumed: "Agent Team member model/tool policy 是真实 runtime override，不是只持久化不消费的装饰字段"
  source_baselines:
    hive_head: "backend Red start=2689695c0528185339414d3b8f2d50a8e6d2aa2a; stable frontend base=92500e4c0"
    freecode_head: "7dc15d6c8fb0c40c7fcc02ce9b58204324252632"
    codex_head: "5c19155cbd93bfa099016e7487259f61669823ff"
  conflicts_or_deltas:
    - "Session terminal writer 只 completed RuntimeTask，未推进已 admitted root item"
    - "AgentTeamMember.model_id 已保存但 continuation/worker 仍使用 lead primary model"
    - "Team member ChatSession listed_surface=chat，与 Session V2 产品身份合同冲突"
  evidence_sink: "EVID-G3-008"
```

- terminal Green：`commit_terminal_outcome` 在同一 DB transaction 内用 `runtime_task_id` 锁定并 transition root item，写 outcome/model-result/event refs；missing/conflicting root 进入 typed reconciliation；idempotent replay 同时修复未关闭 root。
- Team model Green：spawn 前按 tenant + enabled + exact UUID/label/model 校验 member selector；不可用/歧义/跨 tenant 在 durable admission 前拒绝；resolved model ID 进入 `RuntimeTask.metadata.runtime_model_id`，worker loader重新校验 tenant/enabled 后实际使用，fallback/default pair 逻辑保留。
- surface Green：新 Team member Session 使用 `listed_surface=parent`；additive migration 将历史 `session_kind/runtime_source/source_channel` Team variants 回填为 parent-hidden，并以 exact RuntimeTask binding 修复旧 Peer A2A `delegation_run` surface。migration 同时把 terminal RuntimeTask 对应的 nonterminal root item修复为 completed/failed/killed/cancelled/not_admitted，保留 evidence metadata，downgrade 不重新暴露/重开真相。
- tests：初始家族 Red 属本轮 `9 failed`；migration Red 为 missing revision `1 failed`，A2A legacy-surface backfill 另以 missing builder 先红。Green：协作族 backend=`382 passed in 11.84s`；完整 real-PG migration suite=`214 passed in 160.31s`，其中新增真实 downgrade→seed legacy rows→upgrade→verify backfill；backend full=`7567 passed, 2 skipped in 371.77s`；Alembic single head=`collaboration_runtime_closure_0717`。
- commit / deploy：实现、migration、回填测试与本证据位于原子 commit `b9852f37f`。production 已执行 `peer_a2a_session_authority_0717 -> collaboration_runtime_closure_0717`；actual/expected head 一致，148-table/4-trigger readiness `issues=[]/ready=true`，三服务 IDs 与 `EVID-G1-017` 相同并均 `SUCCESS`。真实 Team model route、root reconciliation 与历史 Session surface canary 未执行。
- status：`in_progress-deployed-pending-canary`。

#### EVID-G6-001：XCB-MEM-001 CC 式 Memory 自动披露与 Session 软预算

- `leaf_ids`：`XCB-MEM-001`；owner/依赖：Group 6 / Group 0–2、4；当前状态：`in_progress-local-green`，不关闭 Group 6 其余 9 leaf，也不冒充 production closed。
- Context Read Receipt：以 `@docs/p0-session-memory-a2a-repair-sequence-2026-07-17.md` §3 P0-1 为本轮垂直切片入口，全文消费 `@docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md`，消费 `@docs/runtime-model-agency-constraint-audit-2026-07-13.md` C-13/Model Agency 边界，并以 FreeCode/CC current source 裁决自动召回语义。Session V2 只提供 durable Session/turn identity 与 typed pressure 消费，不成为 Memory truth。
- 冻结基线：Hive `7b67989336c5` + 本轮 owned worktree；FreeCode `7dc15d6c8fb0c40c7fcc02ce9b58204324252632`。FreeCode `memdir.ts` / `findRelevantMemories.ts` / `attachments.ts` SHA-256 分别为 `244cd4a01a4c82660dbeffe6f60808a45a12482d6342601f2b640542604f3e7c`、`360c291993881d94eb3b427ed60f25e61abfcce020292b4fd528c97908bf52ed`、`fa103be6cc512b9210fe50695c8de5554d7438d5f0cba831d42b08d134b3401f`。
- 根因：修复前 live `invoker._resolve_memory_context -> build_memory_context -> MemoryRetriever -> MemoryAssembler` 同时存在 resident explicit-overlay full body、selector 输入 full candidate body、selector unavailable 返回全部正文、assembler `del budget_chars` 四个线性放大点；最后的 prompt hard gate 只能把整个 turn 判失败。这不是“Memory 太多”的数据问题，而是把资源库误当每轮 Prompt 附件的组装边界问题。
- 实装与 live wiring：`profile_plane.py` 保持完整 identity profile，将 explicit overlay 变为 200 行/25,000-byte bounded index；`retriever.py` 只给 LLM name/description/load-ref descriptor manifest、最多选 5 条；`assembler.py` 执行单条 4,096 UTF-8 bytes/200 行与每轮 20KiB 含 ref 总上限；新 `session_surfacing.py` 与 `memory_service.py` 以 durable turn identity 记录 60KiB/Session 计数账本、跨进程锁与 typed exhaustion，不复制正文；`invoker.py` 将 selected count/bytes/remaining/receipt 进入真实 provider suffix metadata。完整授权 bytes 仍由 `search_memory/load_memory` 读取。
- Model Agency / failure：4KiB、20KiB、60KiB 只是“自动披露表示”的资源上限，不删除、重写、降级 Memory truth；平台排序不直接选 body，语义选择归 LLM。selector missing/failure、ledger/assembler failure 和 Session budget exhaustion 只产生 typed degraded/pressure，conversation 与正常 authority 下的无关 effect 继续；不允许“失败则塞回全量 body”。
- Red→Green：新合同首跑为 `ModuleNotFoundError: app.memory.session_surfacing`；排除新模块后为 `9 failed, 45 passed`，wiring 阶段为 `3 failed`。容量 Red 实测 5 条聚合为 `20,497 bytes > 20,480`；turn identity Red 证明不同 turn 曾共用 `turn-session-shared`；selector Red 证明 full-body marker 曾进入 `_select_with_model()` prompt。Green 后五条含 section/ref 总大小 `<=20,480`，只有 durable `turn_id/request_id/runtime_task_id` 可幂等复用，selector prompt 不含 candidate body。
- Green：定向 Memory/runtime suite → `104 passed in 1.13s`；完整 architecture suite → `198 passed in 13.03s`；backend 全量 `pytest tests -q` → `7543 passed, 2 skipped in 361.73s`；frontend 当前 checkout 全量 → `120 files / 693 tests passed`；`npm run build` 通过，AgentDetail=`322860/380000` bytes、gzip=`89493/115000`，vendor=`591449/620000`、gzip=`186474/200000`。scoped Ruff check/format、ledger/doc route 机器门与 diff check 均进入本 commit 验收。
- migration / backfill / rollback：无 DB schema migration，无历史 Memory 正文 backfill，旧 T0/T2/T3/soul truth 不重写。sidecar 位于既有 `memory/control/session_surfacing/`、按 Session 首次访问惰性创建；rollback 后旧版不消费该无正文计数文件，可保留或由受治清理器删除。
- 七原子：Input=当前 authenticated Agent/Session/query + authorized Memory index；Authority=既有 activation/principal/sensitivity 在 descriptor/body ingress 前生效；Execution=唯一 live `build_memory_context()` + invoker dynamic suffix；Evidence=selection/coverage receipt + Session byte ledger + runtime metadata；Recovery=turn 幂等、file lock、typed degrade、full-ref search/load；Consumption=真实 provider prompt suffix 与 Memory tools；Acceptance=定向 Green + 待回填的仓级验收与 production canary。
- 残余门：尚未部署三服务，尚未执行真实长 Session、provider actual-token/prompt-pressure 曲线、百万 descriptor 尾页到达、通用 cursor/hash/T0 traversal。因此 canonical 行保持 `in_progress-local-green:EVID-G6-001`，0/10 production closed；完整 Group 6 出口门仍不变。

#### EVID-G8-PRE-001：backend-volume default Skill startup 写放大止血

- 证据性质：Group 8 / `MISS-RETENTION-001` 的前置 P0 安全闭环，不关闭九个 Group 8 breakpoint，也不把 `MISS-RETENTION-001` 从 `missing` 改为 `closed`，因此 103 个 breakpoint、5 个 Missing 及其 owner 均不变。Group 1 在 `EVID-G1-014` 后显式暂停，仍为 14/16 closed、2/16 pending；`KB-CONTRACT-001` 未开工且没有文件改动。
- canonical 详细设计与 production inventory：`@docs/backend-volume-storage-lifecycle-design-2026-07-15.md`。该文档是后续 Agent transaction finalization、T2 authority/replay、snapshot CAS、sealed T0 cold archive、Blob/Ref/Resolver、dry-run/quarantine/sweep 的施工入口；本记录只同步总账事实，不复制完整方案。
- production 事故事实：Railway 30 日曲线从约 3 GB 阶梯式升到接近 29 GB。容器 `df -B1 /data/agents` 在止血部署前为 total=`48,891,670,528`、used=`28,648,972,288`、available=`20,225,921,024`、usage=`59%`。只读 journal inventory 为 23,186 个 transaction、约 11,921,293,967 bytes；其中 `active_skill_package_install` 为 21,163 个 committed、约 11,510,468,012 bytes。相较同日早期文档快照 14,733 个/约 7.80 GB，单类已新增 6,430 个/约 3.71 GB。
- 根因直证：修复前 startup 对每个 Agent × 每个 default Skill 无条件调用 `install_active_skill_package(overwrite=True)`；每次创建 `active_skill_package_install`，stage 全文件，lifecycle 读取并重写完整 `skill_review.md`，prepare 再复制完整 backup，commit 后没有 payload finalization。逐小时 journal 显示多个重启小时各新增约 918 个/0.51–0.53 GB，`2026-07-15T12` 单小时 1,622 个/约 0.95 GB，和 deployment/restart 阶梯一致。
- TDD Red：`test_asset_transaction_without_staged_changes_leaves_no_journal`、`test_install_active_skill_package_exact_overwrite_is_zero_write`、`test_default_skill_startup_batches_one_recovery_scan_per_agent` 首跑为 `3 failed`，分别命中 no-op 仍写 aborted journal、exact overwrite 仍返回 installed 并写入、startup batch helper 不存在。
- Green/实现：commit=`b2fbb530e`，仅含 `agent_asset_transaction.py`、`skill_installation.py`、`skill_seeder.py` 及两个对应测试文件。`AgentAssetTransaction` 延迟到首个真实 mutation 才建 journal；exact Skill bytes 在 lock 内返回 `unchanged` 且不写 lifecycle/revision/mtime；startup 收敛为每 Agent 一次 recovery scan 和至多一个 batch transaction。共享工作树其它 session 的 60+ tracked/untracked 改动未 stage、reset、覆盖或进入 deployment archive。
- 验收：事故 Red 集合 Green=`3 passed in 0.15s`；transaction/installer/lifecycle 聚焦=`21 passed in 1.20s`；scoped Ruff=`All checks passed!`；`git diff --check` exit 0；当前共享 checkout 完整 backend `pytest tests -q`=`7221 passed, 2 skipped in 261.22s`。
- 三服务 exact-source deployment：backend=`33b02f96-7b3f-4b7f-95a5-2ed1788ca215`、backend-api=`26e0972a-bc04-41bf-bb77-6544654f4c7e`、frontend=`f2c85d24-73ce-4733-ade1-621392a55335`，均 `SUCCESS`。首次并发 `railway up` 在 GraphQL TLS handshake EOF 前失败且未创建 deployment；随后按服务串行重试成功，未触碰 production data。
- production canary：修复重启前后 `active_skill_package_install` 均为 21,163；deployment cutover 后没有新 transaction journal。post-deploy `df` used=`28,650,721,280`、available=`20,224,172,032`，仅增加 1,748,992 bytes（约 1.67 MiB），未再出现约 0.5 GB 阶梯。backend health=`status=ok`、runtime role=`app_rls/strict/non-superuser/non-BYPASSRLS`、daemon/sandbox healthy；`event_loop.max_lag_ms=33468.71`，较修复前 `198063.26` 下降但仍非完成态。
- 七原子：Input=backend volume-bound startup + default registry packages；Authority=server-side registry/Agent workspace lock；Execution=每 Agent 单 batch + exact-byte no-op；Evidence=journal count/bytes、df、test/deploy/health receipts；Recovery=旧 journal 保留、no-change 零 mutation、deployment TLS 失败可同源重试；Consumption=startup 真实调用新 installer/batch；Acceptance=Red→Green、full suite、三服务、production restart count/bytes 对账。该七原子只关闭“继续制造 exact-match startup payload”的独立事故 scope。
- 该证据产生时的未关闭边界：历史 11.92 GB transaction payload 尚无 lifecycle，且没有创建 Bucket、修改 mount/backup/env、批量重放 held T2、迁移 T0/snapshot 或执行 GC；随后新增的 transaction lifecycle/backfill/quarantine 证据以 `EVID-G8-PRE-002` 为准。旧 journal O(n) recovery scan、blocking `flock`、backend-api schema-wait、T2 authority、snapshot duplication 与跨资产 retention/legal hold 仍由 Group 8 完整施工；任何历史 physical sweep 必须先有 inventory manifest、ref/pin/lease/legal-hold 校验、dry-run、quarantine/grace 和用户对该 manifest 的明确确认。

#### EVID-G8-PRE-002：transaction lifecycle、历史 backfill 与可恢复 quarantine

- 证据性质：Group 8 / `MISS-RETENTION-001` 的第二个前置子闭环。它建立 transaction payload 的生命周期权威并完成 production backfill/quarantine，但不关闭九个 Group 8 breakpoint，也不把跨 Memory/Knowledge/Artifact/Audit 的 `MISS-RETENTION-001` 从 `missing` 改成 `closed`。103 breakpoint、5 Missing、owner/severity 均不变；Group 1 继续暂停在 14/16 closed、2/16 pending。
- canonical 设计与逐对象生产证据：`@docs/backend-volume-storage-lifecycle-design-2026-07-15.md` §2.3.2、§10.6、§15–§16。本文只保存总账和完成边界，不复制 21,163 行 manifest。
- Red→Green：首轮 transaction/storage=`7 failed`，model/migration/blob=`6 failed`；实现后对应 Green=`8 passed`、`11 passed`，扩大聚焦=`72 passed in 1.73s`。首次 full suite=`5 failed, 7230 passed, 2 skipped`，真实暴露 migration head、RLS bypass/migration coverage 与 Skill Distiller compensation 漂移；修复 targeted=`14 passed in 10.72s`，最终 `pytest tests -q`=`7235 passed, 2 skipped in 271.69s`。scoped Ruff 与 `git diff --check` 均通过。
- 实现：commit=`df4a815c5`。append transaction 改为只 stage delta，并以 size/suffix/append hash 保证 crash-idempotent apply/compensation；新增 `committed_recoverable/finalized/compensated`、rollback deadline、payload GC/pin/projection metadata。普通 file-only transaction 自动 finalize，cross-store consumer 必须显式 projection/finalize；Skill Distiller 已接入“file commit -> DB projection -> finalize/compensate”。
- storage authority substrate：新增 `storage_blobs/storage_blob_refs/storage_gc_runs`、tenant non-null、RLS/FORCE RLS migration `storage_blob_lifecycle_0715`、verified immutable `FilesystemBlobStore`、manifest-bound lifecycle CLI。该 substrate 尚无 production S3 adapter/resolver consumer，不能把三张表或 Filesystem adapter 冒充冷热分层完成。
- production exact-source deployment：backend=`b47ea815-d41f-42d1-b011-6bdf1f006deb`、backend-api=`372ab45d-8c03-47f5-a252-7e08ea773015`、frontend=`cf930cde-b88c-4e6f-bc14-bb78f449d977`，latest 均 `SUCCESS`。schema readiness 的 expected/actual head 均为 `storage_blob_lifecycle_0715`、130 tables、`issues=[]`。首次 backend-api 在 migration 前按设计拒绝旧 schema，schema ready 后用同一 archive 重提成功；该“固定重试耗尽后需重提”恢复缺口继续留在 Group 8 schema-wait fault matrix。
- production inventory/backfill：inventory=`23,224` transactions，logical=`11,927,841,204` bytes、payload=`11,836,695,357` bytes。首份 backfill manifest run=`backfill-a6e367767e8f4bb9b6ea6b887adf1f24`、SHA-256=`a0dba48ef5affb211e6f187fe9331348c167ba5eef31215d69ee8eaec38d439a`，只选 allowlisted 21,163 个 default-Skill candidates、`11,422,977,781` bytes，并 hold 2,061 个其它 transaction。SSH 输出中断后远端 apply 按对象继续，第二份 remaining manifest/receipt 重验；终态 backfill dry-run=`candidate_count=0`、`hold_count=2061`。
- GC/quarantine：dry-run run=`gc-9acf3eafae5c413098e9f786140f3d2b`、SHA-256=`9c0adf4f497750effaa14e4b5ffd5f957260e681f2b75a517e8b15c10784ccd4`，给出 11,977 candidates、`6,211,996,397` bytes、2,071 hard holds；另有 9,186 个 finalized payload 仍在 commit-based retention 窗口。quarantine receipt processed=`11,977` / `6,211,996,397` bytes、`skipped=[]`；post-quarantine GC dry-run=`candidate_count=0`，sweep dry-run=`candidate_count=0`，24 小时 grace 正在生效。
- 七原子子闭环：Input=typed transaction/backfill/GC manifest；Authority=Agent DB tenant + journal/lock + retention/pin/legal-hold；Execution=hash-bound backfill/quarantine/restore/sweep service；Evidence=journal state、immutable manifests、SHA、durable receipts、deployment/schema/health；Recovery=crash-idempotent append、per-object recheck、quarantine restore、grace；Consumption=startup transaction 与 Skill Distiller 真实 finalize contract；Acceptance=Red→Green、full suite、migration、三服务、production backfill/quarantine/post-dry-run。该七原子只成立于 transaction payload 子域。
- 后继边界：本证据冻结时 physical sweep 尚未执行；随后 exact restore/sweep、cache/trace/T2 rebuildable staging 收敛和最终停止门统一记录在 `EVID-G8-PRE-003`。不得回写本条当时的 `currentSizeMB` 冒充当前值，也不得将 PRE-003 的事故期 operator disposition 泛化为常态 grace bypass。T2 authority/replay、snapshot CAS、sealed T0 cold archive、常态 trace/cache、Object Storage/resolver、跨资产 ref/pin/lease/legal hold/export/deletion ledger、backup/restore drill、metrics/alert 和旧 journal bounded recovery 仍待 Group 8 完整施工。

#### EVID-G8-PRE-003：production exact sweep、派生副本收敛与核心数据停止门

- 证据性质：Group 8 / `MISS-RETENTION-001` 的第三个前置事故子闭环，只证明 2026-07-15 当次 `backend-volume` 异常已按可验证 authority 安全收敛。它不关闭九个 Group 8 breakpoint、不把 `MISS-RETENTION-001` 改为 closed、不改变 103/5 分母，也不改变 Group 1 的 14/16 closed、2/16 pending。
- canonical 逐对象设计、receipts 与保护边界：`@docs/backend-volume-storage-lifecycle-design-2026-07-15.md` §2.1–§2.7、§7.6–§7.7、§15–§16。本文只保留总账；所有 manifest/receipt 位于 production `/data/agents/.storage_lifecycle/{manifests,runs}`，不得把本文抄录的汇总替代原 artifact。
- restore-first transaction 处置：原 run=`gc-9acf3eafae5c413098e9f786140f3d2b` 的 11,977 个 quarantine 对象先完整 restore，receipt processed=`11,977`、`skipped=[]`。随后用原 candidate key 构造 exact emergency manifest=`gc-emergency-gc-912ffd7a4a1148aa94c0225c0a92bb08.json`、SHA-256=`ed827ee0554f14215ad532e06a437bd0665f6e361ba98b345b27203684af805f`，明确排除新近跨 retention 的 1 个对象/876,181 bytes；重隔离与独立 sweep run=`sweep-e5065cf4fcb94203a7963ab7bc0d40c3` 精确处理 11,977 / `6,211,996,397` bytes、`skipped=[]`。
- superseded revision 处置：只选 `next_revision < current_revision`、allowlisted operation、finalized/hot、tenant 已归属、无 pin/legal hold 的旧 revision。dry-run run=`superseded-tx-463a31ab067e4ddba1c0cfd9cd3c1230`、SHA-256=`bad71215de264df92ba13d01856dc3c861827cb2a2f22a5d28a1fe2256836895` 为 9,118 / `5,176,697,828` bytes，68 holds；独立 sweep run=`sweep-734cc757559540d1b3b50e1a243d452a`、SHA-256=`bcfdc738df0ae687a8cb3720eee1dc6bc7abab45c59a13a70870248f3931c10c` 精确同数释放、`skipped=[]`。终态 transaction quarantine=0，fresh sweep=0 candidates，fresh GC=0 candidates/2,091 holds，transaction tree allocated=`777,310,208` bytes。
- 可重建/ACK 副本：web-fetch manifest=`web-fetch-cache-76dd7f64e078404da5c7a90b951939e8.json`、SHA-256=`5c17bd38ab58a587b70ce6096be46b1a49500f6681424cea2633dd1fce72d77e`，清理 10,967 files / `1,620,996,096` allocated bytes；trace dry-run=`invocation-spool-ack-dry-run-20260715.json`、SHA-256=`e37164e5f5a984bc33db1daafaaaebced2dd23b96473288562bc5eed02cdb18d`，只逐出 PostgreSQL ACK 的 637,844 lines / `2,081,917,321` logical bytes，未 ACK 3,305 lines / `10,634,149` bytes 原样保留，invalid=0。两条 receipt 均 `skipped=[]`。
- T2 恢复状态与临时 payload 分离：manifest=`t2-exhausted-staging-f96a016b0ee4479094d738f7877bdd27.json`、SHA-256=`dd265d42a4a6f0cc4f1edd4f3445d239d24d3788fe21c09c5460d530485dda6b` 只选择 status=held、retry exhausted 且文件名全在 allowlist 的 7,095 jobs；15,803 个可由 T0/DB 重建的文件释放 allocated=`1,533,579,264` bytes。7,095 个 `job_manifest.json`、issues、tenant/session/segment、retry/replay identity 全部保留，10 个非 eligible/running job 连 payload 一起保留；两段 receipt `skipped=[]`。
- production 终态：`df -B1 /data/agents` total=`48,891,670,528`、used=`11,316,330,496`、available=`37,558,562,816`、usage=`24%`；backend health=`ok`，三 daemon healthy，runtime RLS=`app_rls/strict/non-superuser/non-BYPASSRLS`，sandbox probe passed，DB pool saturation=`1.7%`。Railway UI `30.27 GB` 是清理前/滞后采样，不覆盖同挂载点内核事实；后续必须并列时间戳。
- owner 停止门：T0 allocated=`6,365,040,640`、workspace snapshots=`2,546,212,864`、workspace=`623,652,864`、transaction current/holds 和剩余 T2 manifests/job payload 均视为核心或未完成状态，本次零删除。snapshot 重复率 dry-run 已在 owner 指令后终止，没有 hardlink、CAS、移动或删除。后续只允许完整 Group 8 的 sealed archive、tenant-scoped CAS、bounded spool/cache 和 authority/replay 施工；禁止为了追求更低容量数字复用本次一次性 manifests 或扩大 scope。
- 七原子子闭环：Input=Railway 曲线异常、容器 df 和 immutable inventories；Authority=tenant/journal/revision/ref/ACK/job state + operator exact disposition；Execution=restore→exact quarantine→independent sweep 与 allowlisted rebuildable eviction；Evidence=hash-bound manifests、durable receipts、allocated bytes、fresh zero-candidate scans、health；Recovery=首批真实 restore drill、未 ACK/T2 job manifest/core data 保留；Consumption=current transaction、PostgreSQL span、active cache root、T2 replay identity 仍由生产 reader/worker 消费；Acceptance=count/byte/hash parity、0 skip、fresh scans、df、health/RLS/sandbox。该七原子只关闭当次事故 scope，常态 lifecycle 仍 open。

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
- Group 4 exact-source commit `4e385d423` 的 backend/frontend 全量回归与三服务生产验收已经证实；共享工作树中未进入该 commit/archive 的 `.env.example`、`.ultra/**`、`.artifacts/**` 仍不属于任何 Group 4 完成证据；
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

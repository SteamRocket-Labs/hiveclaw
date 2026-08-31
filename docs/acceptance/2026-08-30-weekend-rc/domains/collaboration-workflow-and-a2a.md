---
document_id: weekend-rc-domain-collaboration-workflow-a2a
owner: Example Owner / Codex
status: active
authority: canonical-domain-acceptance
last_reviewed: 2026-08-30
source_commit: 45340a3a
verification_status: acceptance-spec-not-execution-result
---

# 协作、Workflow 与 A2A 验收标准

[返回索引](../README.md) · [Journey Ledger](../04-journey-ledger.md) · [Single Agent](single-agent-and-session.md)

## 五种语义必须分开

| 能力 | 身份/寿命 | 适用任务 | 产品证据 | 不能退化成 |
|---|---|---|---|---|
| Sub-agent | 临时、隔离 worker | 主 Agent 拆出短期研究/执行/验证 | parent、narrow context/tools、child status/result、parent integration | 持久 Agent 或 Workflow step 换名 |
| Agent Team | 当前任务中的具名成员 | 多角色 fanout/review/aggregation | lead、role、member output、partial failure、final synthesis | 匿名 Sub-agent 列表 |
| Dynamic Workflow | 模型提出的版本化 execution graph | 确定性顺序/并行/gate/wait/resume | draft/preview/version/budget/confirm/step journal/result | 平台预写语义或聊天气泡串 |
| Fixed Workflow | 企业发布的可复用编排资产 | 重复业务流程与固定 A2A path | owner/version/publish/input/permission/run/audit | Dynamic draft 原地升级 |
| A2A | 独立持久数字员工之间 | 跨员工 request/delegation/continuation/nested | requester/peer/scope/child Session/result/artifact/return/recovery | Sub-agent/Team 或一条“已发送” |

单 Agent 先通过；不得为多 Agent 展示牺牲 Agent 的工具、context、output budget 或最终质量。

## Sub-agent

- [ ] 主 Agent 明确 why/role/task、narrow context、tools、budget、timeout、output contract。
- [ ] child Session/workspace/identity 与 parent 隔离；权限为 parent scope 的子集。
- [ ] spawn、running、progress、completed/failed/cancelled/timeout 有 typed evidence。
- [ ] 每个 child tool call 有 result；late completion、duplicate delivery、restart 幂等。
- [ ] result distillation 回到 parent；父最终结果实际消费 child evidence，不只显示完成 badge。
- [ ] child failure 不永久挂 parent；支持 retry/cancel/continue-partial。
- [ ] UI 清楚标临时 worker，不把它加入永久 Agent rail。

## Agent Team

- [ ] lead、成员、角色、共享目标、dependency、fanout/review/integration 明确。
- [ ] 每个成员为具名协作单位，有独立 output 和状态；不是多个匿名 spawn。
- [ ] partial failure、member timeout、lead crash、late result 有 repair/retry/cancel/abandon。
- [ ] lead synthesis 消费所有适用结果，说明 rejected/unresolved/coverage gap。
- [ ] Team 关闭、reload、resume 后状态和 final artifact 一致。
- [ ] 普通员工看到任务分工与最终结果；operator 才看 raw event/span。

## Dynamic Workflow

- [ ] Agent 用真实任务提出 workflow draft，语义内容由模型拥有。
- [ ] preview 显示 objective、steps、parallelism、gate/wait、budgets、permissions、effects 和 version。
- [ ] 用户可 revise/reject/cancel；approve 绑定 exact preview/version。
- [ ] confirmed run 进入 `RuntimeTask(task_type="workflow")` 和 canonical step/leaf journal。
- [ ] wait/signal/resume、step retry、worker restart、duplicate signal、cancel 都可恢复且幂等。
- [ ] validator 只返回 schema/protocol/authority observations，不改写 workflow semantics。
- [ ] final integration 回到 source Session、artifact/notification 和 workflow panel。

## Fixed Workflow

- [ ] draft、review、publish、retire、version replacement 和 rollback 分开。
- [ ] 发布者、tenant、input schema、allowed Agents/tools/effects、approval/budget 有权威记录。
- [ ] run instance 固定 exact version；发布更新不静默改变 active run。
- [ ] A→并行 B/C→review gate→join→final 的标准 DAG 完整通过。
- [ ] audit 能从 input→step→A2A→artifact→final 返回源引用。

固定路径的 UI 动画/图形可在功能闭环后优化，但当前必须先诚实显示 owner、current node、waiting/failed、required action、return result 和 recovery；不能只画成功线。

## A2A 路径矩阵

- [ ] sync consult：A 调用 B、B 回复、A 消费并 final。
- [ ] async delegation：A 派发 B、父可离开、B completion 经 outbox/notification 返回、父 continuation 恰一。
- [ ] same-child continuation：A 向同一 active child 续发，authority/replay/terminal rollover 正确。
- [ ] nested A→B→C：immediate parent route 与 chain-root authority 分开；C 结果逐层返回。
- [ ] long result：超过 inline limit 后使用受权 artifact/result ref，父不获得整个 child workspace。
- [ ] fixed workflow A2A：每条 edge 绑定 workflow version、step、principal、budget 和 receipt。
- [ ] peer unavailable/denied/timeout/ambiguous send：typed state、无自动 replay、可 retry/cancel/reconcile。
- [ ] reload/restart/duplicate delivery 不产生第二 child、第二 continuation 或第二 external effect。

## UI 消费

- Agent rail 只列持久数字员工；Sub-agent/Team/Workflow instance 在当前 Session 右栏或 timeline 呈现。
- 每条 collaboration row 显示 who→who、purpose、status、required action、result/artifact；内部 UUID 默认隐藏。
- A2A child Session 可按权限打开；parent lineage 和 return path 可理解。
- terminal 后不保留 stale running；typed read model 优先，transcript fallback 不能重复加总。

## Acceptance

五种能力各用不同真实任务连续两遍；不能用一个底层 RuntimeTask 测试外推。每条都覆盖 Input、Authority、Execution、Evidence、Recovery、Consumption、Acceptance，以及 member/worker failure、cancel、restart、duplicate、permission revoke 和 UI result integration。

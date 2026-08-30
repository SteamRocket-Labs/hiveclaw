---
document_id: weekend-rc-domain-automation-hooks-capabilities
owner: Rocky / Codex
status: active
authority: canonical-domain-acceptance
last_reviewed: 2026-08-30
source_commit: 45340a3a
verification_status: acceptance-spec-not-execution-result
---

# Automation、Hook 与外部能力验收标准

[返回索引](../README.md) · [Journey Ledger](../04-journey-ledger.md) · [HR/Permissions](hr-identity-and-permissions.md)

## Once / Schedule / Bounded Loop / Event Trigger

- [ ] 从真实 product surface 创建；owner、target Agent/Workflow、timezone、next run、permission、budget、enabled state 明确。
- [ ] once 有 exact time/idempotency/cancel/result；schedule 覆盖 timezone/DST、pause/resume/update。
- [ ] loop 有 objective、done、budget、stop、checkpoint 和 recovery；不实现隐式无限 polling/heartbeat。
- [ ] event trigger 绑定 verified event/source/principal，不用自然语言猜 trigger authority。
- [ ] fire→durable claim→Agent/Workflow→tool/effect→artifact/result→notification/channel→terminal audit 全链发生。
- [ ] daemon health、tick/outcome counter、queued row 不能计任务成功。
- [ ] duplicate fire、stale claim、worker restart、provider unavailable、permission revoke、delivery failure 幂等且无重复 effect。

## Notification / Approval Inbox

- [ ] 用户离开后，completed、failed、approval、clarification、permission change 进入唯一 inbox。
- [ ] deep-link 回到 exact Agent/Session/run/item，不能跳默认页面或旧 run。
- [ ] unread/read、dedupe、expiry、resolved、dismissed、cross-Agent aggregation 正确。
- [ ] approval preview 显示 action、scope、effect、requester、evidence、expiry；model 不能自批。
- [ ] late/duplicate approval、revocation、reload 和 multi-tab 不产生第二 effect。

## Channel

- [ ] ingress 绑定 verified external principal、tenant、Agent、Session/channel thread。
- [ ] message、attachment、reply、edit/retry 保留 provenance 和 canonical Session identity。
- [ ] outbound external send 在 effect boundary approval；provider receipt 和 delivery state durable。
- [ ] configured、unconfigured、auth-expired、revoked、rate-limited、ambiguous-send 分开。
- [ ] dead-letter/reconcile/retry 不重复 external delivery；cancel/abandon 可达。
- [ ] 普通员工看到可理解的 channel state；raw provider payload operator-only。

## Local Agent / Hive Connect

- [ ] install/activate、pairing approval、token exchange、capability snapshot 和 agent binding 顺序正确。
- [ ] browser/daemon channel 与 actual local runtime health 分开；offline 不伪装成 Agent 离职。
- [ ] workspace/artifact transfer 有 authority、hash、receipt 和 size/path safety。
- [ ] risky local action 先 approval，再进入 bridge poll/execute/report；拒绝后零 result/effect。
- [ ] disconnect→offline delivery→new ticket/reconnect→completion 可恢复。
- [ ] report replay 幂等；credential revoke、unpair、uninstall 后 active/retry 不再执行。
- [ ] 不把 client-echoed tenant/user 或 bridge metadata 当 authority。

## Hooks

每个 Hook 必须登记 event、trigger point、mode、consumer、input/output、timeout、failure policy、evidence 和 recovery。

- [ ] blocking Hook 只保护 exact authority/effect/protocol/resource invariant；denial 有 repair/retry/cancel/abandon。
- [ ] observe-only failure 不改变模型输出或任务完成语义。
- [ ] Hook timeout/crash/restart 不留下无出口 nonterminal state。
- [ ] SessionStart/Stop/PreTool/PostTool/compaction 等声明的 lifecycle 真实接线，不以注册函数存在作为证明。
- [ ] payload 中外部内容是 untrusted data，不能成为系统指令。

## Skill

- [ ] source/publisher/version/license/hash/risk/tenant scope 进入 Trust Review。
- [ ] capsule progressive disclosure；只有相关任务才 `tool_search`/`load_skill`。
- [ ] load 只增加 instructions/references/templates，不授予 tool/effect permission。
- [ ] activation eval、should-trigger、near-miss、output quality、failure handling 均验证。
- [ ] packaged workflow/subagent/script 仍通过各自治理 runtime 执行。
- [ ] update/version diff、deactivate/revoke/rollback、active Session cache invalidation 可用。
- [ ] Skill candidate 由 memory evidence 提名，但必须独立 eval/review/publish。

## MCP / Connector

- [ ] server/source、publisher、version、tool/resource/prompt、risk class、scope 和 credential owner 清楚。
- [ ] authentication 与 authorization 分离；per-user/tenant least privilege。
- [ ] token/secret 不进入 prompt、DOM、log、transcript 或 tool result。
- [ ] external description/prompt/result 视为 untrusted data，不能直接改变 policy 或选择工具。
- [ ] tool schema strict、local validation、timeout、result size、retry、audit 和 typed error 完整。
- [ ] auth expiry、reauth、disable/revoke、schema/version diff、server unavailable 均有 UI 和恢复。
- [ ] install/activate/use/update/deactivate/revoke/rollback 走同一 Trust Gate；撤销后 active/retry 不继续调用。

## `/schedule`、`/once`、`/loop`、`/workflow`、`/skill`、`/mcp`

这些命令的 Session 输入合同由 [single-agent-and-session.md](single-agent-and-session.md) 拥有；本文件拥有它们打开后的产品能力生命周期。命令成功但目标 surface 不工作，仍是断点。

## Acceptance

每类从 product entry 到 terminal delivery 连续两遍，并覆盖 duplicate/restart/revoke/unavailable/ambiguous/expiry。真实 external provider 或 bridge 不可用时诚实 `BLOCKED_PRECONDITION`；受控 fake 只能算 CI floor，不能计 production NPTCR。

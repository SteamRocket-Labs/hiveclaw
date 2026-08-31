---
document_id: weekend-rc-2026-08-30-p01-main-supporting-platform-admin-falcon-682
owner: Codex
status: completed
authority: immutable-production-supporting-evidence-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: signed-in-function-smoke-and-reload-pass
journey_id: P01-MAIN
pass: supporting-function-smoke
environment: production
source_commit: b2fb8b28ec00b24eca1235340a1ecc7ee4383fd4
deployed_commit: b2fb8b28ec00b24eca1235340a1ecc7ee4383fd4
manifest_sha256: e4c602f58e4c1736a147b82a62687e7ada361327bb13cd99c5a956d8998c78fb
deployment_ids: backend=c93dafab-e80b-4bef-9e9d-d1342fdff712; backend-api=f4750d4b-ba00-4a2a-86e5-3aabea996e70; frontend=8110c8be-5252-4fe7-a918-9b189b52ca13
persona_principal: authenticated lab platform-admin using EventPilot in the selected Example Owner experimental tenant
data_version: session-v2-production
target_session_id: 65b98e1a-8723-4288-b611-4ba8f3d1861a
marker: P01-STAGE1-FRESH-FALCON-682
started_at: 2026-08-31T14:32:00+08:00
ended_at: 2026-08-31T14:37:00+08:00
result: PASS
fault_recovery_result: PASS
negative_authority_result: NOT_RUN
cleanup_result: NOT_RUN
supersedes: none
---

# P01-MAIN platform-admin supporting function smoke

这是 current manifest 下的一次 signed-in production 功能 truth test。它证明当前 production 的真实 Agent/Session/GLM/Work Ledger/tool/artifact/reload 主路径可用，但 principal 是 `platform_admin`，因此不是 employee persona pass，也不进入 NPTCR。

## Input

- 从 EventPilot 的 fresh product Session 只发送一次 marker `P01-STAGE1-FRESH-FALCON-682`。
- 任务要求公开三步计划、至少三个 Work Ledger todo、`write_file` 恰好一次、成功后 `read_file` 恰好一次，以及一份满足七项外部硬判据的 Markdown runbook。
- 唯一允许写入为 `workspace/WEEKEND-RC-P01-STAGE1-FRESH-FALCON-682.md`；禁止外部消息、外网、其他 Agent、workflow、trigger、delegation、credential 与其他路径写入。

## Authority

- signed-in principal 为 Example Owner 实验 tenant 的现有 `platform_admin`；没有 forged token、DB role mutation、RLS bypass 或 credential 读取。
- owner 已在浏览器 action-time 明确确认发送该合成探针。写入位于已登记、可回收的 Agent synthetic workspace。
- 该 principal 只能证明功能路径；它不能证明 employee、org-admin 或 scoped-operator 的 UI/API authority。

## Execution

- 点击发送后页面切换到 fresh Session `65b98e1a-8723-4288-b611-4ba8f3d1861a`，只产生一个 run。
- GLM-5.3 先公开三步计划，再建立三个 todo；随后一次 `write_file` 成功写入 2229 字符，一次 `read_file` 读回同一路径。
- 最终三个 todo 全部 completed；Team/A2A/Workers/Workflow 均为 0，没有外部或跨 Agent effect。

## Evidence

- UI final 明确给出目标 path、marker、`TOTAL_MINUTES=90`、`RISK_ROWS=2`、一次写、一次读与 3/3 todos。
- 主 Codex 没有只采信模型自述：通过产品 artifact preview 独立读取保存快照，机械确认首行标题、独立 marker、三行议程、`TOTAL_MINUTES=90`、两行风险、`RISK_ROWS=2` 与 Owner/Timing/Fallback/Final handoff 清单。
- artifact card 为 `WEEKEND-RC-P01-STAGE1-FRESH-FALCON-682.md`，作者 EventPilot，当前 Session artifact count 为 1。

## Recovery

- terminal settle 后页面为 `完成 · GLM-5.3`、0 running、0 waiting、run `已完成`。
- hard reload 的短暂 loading state 收敛后，同一 Session 恢复精确 prompt/final、3/3 todos、一个 artifact、0 running、0 waiting；artifact preview 内容保持一致。
- 本证据只覆盖 reload，不覆盖 disconnect、process restart、duplicate delivery、cancel 或 retry fault injection。

## Consumption

- 普通产品 Session UI 消费模型计划、进度、todo、final、artifact preview/download 与 runtime state；没有借助 DB、console 或管理员补状态完成交付。
- artifact 是可直接交给现场团队使用的 Markdown runbook，而不是仅有文件名或模型声明。

## Acceptance

- 功能 smoke 的七项内容硬判据、selected-model surface、Work Ledger、一次写/一次读、artifact 预览与 reload 均 PASS。
- 由于 principal 为 `platform_admin`，本文件不写 P01-MAIN pass-1/pass-2，不推进 Journey verdict，NPTCR 保持 `0/96`。

## Cleanup

- Session、run 与 workspace artifact 作为共享 synthetic supporting evidence 保留；final RC cleanup 时只经 supported path 精确删除登记目标并 read back。

## Not proven

- employee persona、org-admin/scoped-operator 行为、权限负向、RLS、approval-required 分支、fault/restart/cancel/duplicate-delivery。
- P01-MAIN 双遍、其他 95 条 journey、五条 guardrail、Evidence Coverage、Zero Known Defects、final D/E 或 Weekend RC 完成。

---
document_id: weekend-rc-2026-08-30-p29-padmin-fault-active-runtime-guard-presentation
owner: Codex
status: immutable
authority: production-failure-evidence-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: reproduced
finding_id: RUNTIME-GUARD-PRESENTATION-001
journey_id: P29-PADMIN
pass: positive-runtime-health-pre-fix
environment: production
source_commit: bbf6d2340afe593b44f740fabfa178d126b5beca
deployed_commit: bbf6d2340afe593b44f740fabfa178d126b5beca
deployment_ids: backend=4ad99e93-d3be-48c9-be8d-0107dff44f82; backend-api=8aa5ccbc-fe9d-4da2-bb39-f16497de044f; frontend=638da152-1ef6-444c-bcd8-4dd00fa0296d
persona_principal: authenticated lab platform_admin
result: FAIL
recovery_result: NOT_RUN
cleanup_result: NOT_APPLICABLE_READ_ONLY
supersedes: none
---

# P29-PADMIN active runtime runs presented as safeguard interventions

本文件只固化修复前 production 失败事实，不进入 NPTCR。

## Input

- signed-in `platform_admin` 只读打开 `/enterprise/runtime-budgets`。
- 没有点击“只观察”“强制保护”“保存公司策略”或任何“暂停”按钮，也没有修改 tenant/model/role/grant。

## Authority

- 当前页面为既有 lab tenant 的 company control plane；run list 由 tenant-bound `/runtime-budgets/runs` 消费。
- 未读取员工消息正文、credential 或 raw provider payload；没有 operator reason-bound view。

## Execution

- frontend `protectedRuns` 只包含 waiting/resuming/exhausted/hard-stopped/stopped/expired/cancelled，当前 production 结果为 0。
- 同一组件在 0 时回退渲染 `runs.slice(0, 5)`，但 heading、description 与 badge 仍宣称 protected runs。
- backend `_user_reason()` 对 `active` 没有 explicit branch，落入“系统保护机制已介入”默认值。

## Evidence

- DOM 同时出现 heading“被保护的任务”、badge `0`、5 条“正在运行”、5 个“暂停”按钮。
- 五条 active row 的原因均为“系统保护机制已介入”，下一动作均为“等待当前运行完成”。
- 这不是保护终态：组件自身 `protectedRuns.length=0`，active status 也不在 protected allowlist。

## Recovery

- 本探针只读，未通过暂停任务或制造 protected run 改变生产状态。
- 修复必须保留管理员对 active run 的暂停能力，同时把 recent/protected 事实分开呈现。

## Consumption

- platform admin 无法判断“正常运行”与“保护机制已介入”的区别，且 heading/count/list 互相矛盾。
- 该错误影响 runtime/compliance health 的可信消费，因此 P29-PADMIN 不能 PASS。

## Acceptance

- finding `RUNTIME-GUARD-PRESENTATION-001` 为 P2 `Reproduced`；必须经过 failing regression、共享根因修复、全量门、三服务同提交部署与 signed-in production reload 后才能到 `Verified`。
- 本文件不是 P29-PADMIN pass-1/pass-2；NPTCR 保持 `0/96`。

---
document_id: weekend-rc-2026-08-30-p29-platform-admin-pass-1
owner: Codex
status: immutable
authority: production-journey-pass-not-closed-loop
last_reviewed: 2026-08-31
verification_status: pass-1-pass-pass-2-blocked-precondition
journey_id: P29-PADMIN
pass: 1
environment: production
source_commit: bf94b76a1706510daf2d11c4e98fd5051f23f28f
deployed_commit: bf94b76a1706510daf2d11c4e98fd5051f23f28f
manifest_sha256: d320edceeb26cf68fa724e77502d811e5476fa04ee3c9128075cc8c79eb38117
deployment_ids: backend=07059ce5-12ca-4b57-ac92-bc05d58dfb49; backend-api=c70ff972-b039-4cf5-831d-4526962c8d9d; frontend=308e7789-0b9c-46aa-93e9-7d1ddc110433
persona_principal: authenticated lab platform_admin
data_version: live-production-read-only-2026-08-31
started_at: 2026-08-31T00:30:43Z
ended_at: 2026-08-31T00:32:49Z
result: PASS
fault_recovery_result: BLOCKED_PRECONDITION
negative_authority_result: PASS
cleanup_result: PASS
supersedes: none
---

# P29-PADMIN pass 1

## Input

- 从 signed-in production platform workspace 依次 hard-load dashboard、info、LLM、memory、extensions、runtime budgets、quotas 与 audit。
- 同一遍打开九个 company direct URL，执行 14 个 authenticated status-only API negative，并 hard reload `/agents` 验证 ownership/exact-user 正向面。
- 全程只读；未点击 save/test/sync/create/delete/approve/publish，未读取 credential、header、token、cookie、browser storage 或 response body。

## Authority

- server-derived principal 为 `platform_admin`；deployment、auth `/me` 与 platform routes 使用同一已登录会话。
- platform nav 精确为 dashboard/info/LLM/memory/extensions/runtime-budgets/quotas/audit；dashboard cards 为除 dashboard 外的 7 项。sidebar 显示“超级管理员”，不显示“公司后台”。
- company direct routes 不得因 URL/query/DOM 获得更强 audience；Agent 只保留 ownership 或 exact user-scoped authority，不继承 company/department scope。

## Execution

- 八个允许页面都从真实 frontend route 进入 live API：auth、tenant config、LLM provider/model、Memory config、AI assets、runtime budget、quota 与 audit summary 均返回 200。
- 九个 company direct URL 全部由 product guard 收敛到 `/enterprise/dashboard`，没有挂载 company component。
- 14 个 negative 通过页面原生 API client 的既有 authenticated request 执行；探针只替换目标 URL、读取 status，并向原 Agent-list consumer 返回独立安全空数组，目标 response body 从未被消费。

## Evidence

- exact application `bf94b76a1706510daf2d11c4e98fd5051f23f28f` 的三服务 deployment IDs 均 `SUCCESS` 且 deployment message 为 `deploy bf94b76a platform-company admin boundary final`。backend health `status=ok`、RLS `strict`、runtime control bus no error；frontend HTTP 200。
- 允许面：8/8 route 保持目标 pathname，均无 runtime error 或 alert；各自 live API 为 200。
- dashboard：8 个 nav path、7 个 card path；User/数字员工/待审批 business metrics 与 Plaza 均为 0。
- URL negative：digital employees、knowledge、users、org、invitations、HR、approvals、guardrails 与 Plaza 9/9 回 dashboard；每次 0 table row、0 email-like、0 UUID-like business DOM、0 runtime error。
- API negative：stats、approvals、org departments/members、legacy-file status、invitation codes、Feishu org setting、Users、external principals、guard policies、company source contracts、system HR、Plaza posts/stats 共 14/14 为 403。
- Agent positive：hard reload 后 EventPilot 可见，首屏 12 张卡全部显示“我拥有的”，无 empty/runtime error；Agent summary 为 200，system HR 为 403。
- 当前 Codex task 保存了一张 platform-admin dashboard full-page screenshot；PNG 25,461 B，SHA-256 `a5686a853be472f71124f918b18068ddc66471fdd6bab0d6c63b01954eadaf36`。截图只证明本角色当前 UI state，不外推其他角色。

## Recovery

- 本遍每个允许页面均为 fresh navigation；dashboard 与 Agent list另做 hard reload，route/API/label 无 stale authority 或 runtime alert。
- denied URL 连续切换后仍统一回到 dashboard；随后 Agent、dashboard 正向面恢复，无业务 DOM 残留。
- expired-session 与 role-change recovery 留给 pass 2；当前没有可安全消耗的另一登录身份，也未获角色/grant mutation authority。

## Consumption

- platform admin 实际看到 platform/provider/runtime/config/compliance cards、tenant presentation、safe audit 与其拥有的 Agent。
- company User/Org/HR/Knowledge/Approval/Guard/Invitation/Plaza content 和 Feishu secret setting不进入默认 DOM 或 API body。

## Acceptance

- clean-path Input、Authority、Execution、Evidence、Consumption 与 URL/API negative 均通过；本证据语义 verdict 为 `PASS`。
- exact deployed finding regressions此前已通过 backend **8484 passed, 2 skipped, 1 warning**、真实 PG **13 passed**、platform-admin contract **423 passed**、frontend **156 files / 1161 tests**、build/budgets、Weekend **18 passed**、manifest、Ruff 与 diff check。
- pass 1 单独不关闭 Journey；mechanical scorer 精确报告缺少 `P29-PADMIN-pass-2.md`、`closed=0`、NPTCR `0%`，并仍要求 release gates；semantic verdict 固定未计算。

## Cleanup

- 本遍没有创建 Session、文件、Agent、grant、role、setting 或其他 synthetic asset；cleanup 为 `PASS`，无需 effect。

## Not proven

- pass 2 的 expired-session、role-change、fault recovery；member/company-admin/operator 三个独立 principal；完整四角色 screenshot matrix 与 reason-bound operator audit。
- 因此 P29-PADMIN 仍非 `Closed loop`，NPTCR 保持 `0/96`。

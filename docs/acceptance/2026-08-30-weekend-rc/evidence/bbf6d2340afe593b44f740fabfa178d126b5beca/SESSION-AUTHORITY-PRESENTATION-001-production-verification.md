---
document_id: weekend-rc-2026-08-30-session-authority-presentation-production-verification
owner: Codex
status: immutable
authority: production-finding-verification-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: verified
finding_id: SESSION-AUTHORITY-PRESENTATION-001
journey_id: P29-PADMIN
pass: bounded-authority-presentation-verification
environment: production
source_commit: bbf6d2340afe593b44f740fabfa178d126b5beca
deployed_commit: bbf6d2340afe593b44f740fabfa178d126b5beca
deployment_ids: backend=4ad99e93-d3be-48c9-be8d-0107dff44f82; backend-api=8aa5ccbc-fe9d-4da2-bb39-f16497de044f; frontend=638da152-1ef6-444c-bcd8-4dd00fa0296d
persona_principal: authenticated lab platform_admin
result: VERIFIED
recovery_result: PASS
cleanup_result: NOT_APPLICABLE_READ_ONLY
supersedes: evidence/3482b57a383d3c5bd33a5bcf813b87c6fab23339/P29-PADMIN-fault-denied-session-shell.md
---

# SESSION-AUTHORITY-PRESENTATION-001 production verification

本文件只关闭已复现的 Session authority presentation 根因，不进入 NPTCR。P29-PADMIN 的 platform health/compliance 正向面、双遍、role-change recovery、四角色 screenshot matrix、API/audit 全证据仍未完成。

## Input

- signed-in `platform_admin` 直接打开 production route `/agents/5d99fe45-7ea9-4f7e-979c-c57bcb2cd4ea/sessions/d5b47bd0-27d1-46e7-b417-4e9da362b553`。
- 该 route 未携带 `operator_view` 或 `operator_reason`，因此只允许普通 not-found/denied presentation，不允许读取更强 audience 数据。
- 负向验证后，从产品按钮返回数字员工列表；再 hard navigation 到合法 MAPLE Session `/agents/03d43a5c-0d5c-4c30-bab9-2734c5691434/sessions/52ddde7f-63bf-44a6-973f-ffb1da06d14a` 做正常路径回归。

## Authority

- 当前 principal 为既有 lab `platform_admin`；未登录、未创建账号、未修改 role/grant、未读取 credential，也未启用 operator view。
- 服务端对跨用户 Session 继续 fail closed；前端只消费 exact HTTP 403/404 machine status，不扫描错误自然语言，不制造权限结论。
- 整个验证为只读 navigation/DOM consumption；没有数据库写入、业务 effect、provider call 或外部消息。

## Execution

- application commits：`d4ae15fd5e4eed82c2ca68f7d6769a8f2575d1b7` 建立 resolving/403/404 truthful surface；`57823bcf98e97fcc8640aa8101a8924d7c4bb709` 排除 denied-route 离开时的 stale Session re-selection；`bbf6d2340afe593b44f740fabfa178d126b5beca` 将安全恢复入口收敛到既有 `/agents` 列表。
- 最终实现保留网络/5xx durable retry 与合法 Session read-only resolution；只在 authoritative 403/404 终态清除目标 Session 的 timeline/replay/event/runtime cache。
- `bbf6d234` 已 push；backend、backend-api、frontend 的 deployment message 均绑定该 exact commit并为 `SUCCESS`。

## Evidence

- deployment IDs：backend `4ad99e93-d3be-48c9-be8d-0107dff44f82`、backend-api `8aa5ccbc-fe9d-4da2-bb39-f16497de044f`、frontend `638da152-1ef6-444c-bcd8-4dd00fa0296d`。
- public readback：backend `/api/health` 返回 `status=ok` 且 `runtime_control_bus.last_error=null`；frontend `/` 返回 HTTP 200。
- 负向 route resolving 后，DOM 有且仅有 truthful surface：“找不到此会话”“此会话不存在，或当前账号无法访问。”“返回数字员工”。
- 同一 DOM 不含 `Read-only · User`、通用完成终局、运行错误、会话交付物或目标 Session 业务正文。
- 点击“返回数字员工”后当前 URL 精确为 `/agents`，数字员工列表可读，DOM 不再含 denied/not-found alert。

## Recovery

- 旧的 Agent chat 返回目标会自动选中该共享 HR Agent 下另一条不可访问 Session；production hard reload 已证伪这是 stale SPA ref，并定位为 Agent 默认 chat auto-selection。
- 最终恢复边界直接返回 `/agents`，因此不携带 denied Session context，也不依赖清理全局 Agent selection。
- 合法 MAPLE Session hard navigation 后没有 authority alert；页面显示 marker `P01-MAIN-PASS1-3482B-MAPLE-581`、完成终局、3/3 todos、一个 artifact，运行面板精确为 `0 个运行中 / 0 个等待中`，无 Stop 按钮。

## Consumption

- 无权用户消费到 server verdict 对应的明确 not-found 状态，而不是平台合成的成功/失败 workbench shell。
- 用户有明确、可操作且不重新进入不可访问 Agent context 的恢复入口。
- 合法 Session 的 prompt/final/todos/artifact/runtime consumption 保持完整，证明修复没有用隐藏整个 AgentDetail 的方式掩盖负向缺陷。

## Acceptance

- failing-first mounted regression 在实现前仍渲染假 shell；最终 frontend **154 files / 1148 tests passed**，i18n 双语各 3993 keys、production build 与 bundle budgets 通过，AgentDetail 恰为 2900 行。
- Weekend/atomic architecture **24 passed**；production manifest `valid=true`、denominator `96`、hash `d320edce…`。
- `SESSION-AUTHORITY-PRESENTATION-001` 推进为 production `Verified`。
- 本文件不是 P29-PADMIN pass-1/pass-2，不证明 platform provider/runtime/compliance 正向面、role-change recovery、operator audit、四角色 screenshot matrix 或 RC aggregate；NPTCR 保持 `0/96`。

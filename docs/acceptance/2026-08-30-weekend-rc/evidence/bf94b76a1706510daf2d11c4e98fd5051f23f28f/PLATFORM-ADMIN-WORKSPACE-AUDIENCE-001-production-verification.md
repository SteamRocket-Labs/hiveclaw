---
document_id: weekend-rc-2026-08-30-platform-admin-workspace-audience-production-verification
owner: Codex
status: immutable
authority: production-finding-verification-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: verified
finding_id: PLATFORM-ADMIN-WORKSPACE-AUDIENCE-001
journey_id: P29-PADMIN
pass: bounded-platform-admin-workspace-audience-verification
environment: production
source_commit: bf94b76a1706510daf2d11c4e98fd5051f23f28f
deployed_commit: bf94b76a1706510daf2d11c4e98fd5051f23f28f
deployment_ids: backend=07059ce5-12ca-4b57-ac92-bc05d58dfb49; backend-api=c70ff972-b039-4cf5-831d-4526962c8d9d; frontend=308e7789-0b9c-46aa-93e9-7d1ddc110433
persona_principal: authenticated lab platform_admin
result: VERIFIED
recovery_result: PASS_HARD_RELOAD_AND_DIRECT_ROUTE_RECOVERY
cleanup_result: NOT_APPLICABLE_READ_ONLY
---

# PLATFORM-ADMIN-WORKSPACE-AUDIENCE-001 production verification

本文件只关闭 platform admin 被提升为 company admin、默认读取公司生命周期 surface 的根因，不进入 NPTCR，也不把单一 platform-admin 身份外推为 P29 四角色矩阵或双遍 PASS。

## Root reproduction

- exact deployed `8f6a726375452042cf1252977394c647dd2aba80` 的 platform admin dashboard 默认展示全部 company-admin cards；直接打开 digital employees、knowledge、users、org、invitations、HR、approvals 与 guardrails 会挂载业务 DOM，并消费 200 company API。
- backend company lifecycle route 与 Agent authority 同时把 platform role 当作 tenant admin 或 blanket Agent manager，因此旧行为不是仅有链接的 presentation fault。
- immutable FAIL evidence：`evidence/8f6a726375452042cf1252977394c647dd2aba80/P29-PADMIN-fault-company-admin-workspace-audience.md`。

## Input

- 复用现有 signed-in `platform_admin`，只读 hard-load dashboard、允许页面、`/agents` 与九个 company direct URL。
- 负向 API 探针复用应用自身已登录 Agent-list 请求的既有鉴权头，只替换目标 URL、记录 HTTP status，并给页面返回安全空数组；没有读取或输出 header、token、cookie、storage、响应正文或业务标识。
- 没有点击 create/save/delete/approve/sync/test/publish 等 effect control，没有角色、grant、tenant、credential、billing 或生产数据变更。

## Authority

- frontend 的共享 role section registry 只给 platform admin 暴露 `dashboard`、`info`、`llm`、`memory`、`extensions`、`runtime-budgets`、`quotas`、`audit`；company routes 使用 exact `OrgAdminGuard`，Plaza 使用 company-member guard。
- backend company lifecycle routes 以 authenticated role 在 DB 前返回 403；stats、User/Org/HR/Approval/Knowledge/Guard/Plaza 不再继承宽泛 admin authority。
- Agent authority 不再因 platform role 自动升级为 manage。该身份仍可通过 ownership 或 exact user-scoped permission 消费 Agent；company 与 department scope 不对 platform admin 生效。

## Execution

- `24f012ba058181c02483b343e9d73f6add970d70` 在既有 route、permission helper、workspace registry 和 guard 上完成共享修复；没有 schema、migration、dependency、feature flag 或生产数据变更。
- D1 production hard reload 捕获残余：sidebar 仍使用 `nav.enterprise`，显示“公司后台”。该 presentation defect 没有被写成已知债务。
- `bf94b76a1706510daf2d11c4e98fd5051f23f28f` 复用既有 `nav.superAdmin`，以一行实现和 mounted regression 修正标题；backend authority 与 D1 相同。

## Evidence

- `HEAD = origin/main = bf94b76a1706510daf2d11c4e98fd5051f23f28f` 后部署 application archive；backend `07059ce5-12ca-4b57-ac92-bc05d58dfb49`、backend-api `c70ff972-b039-4cf5-831d-4526962c8d9d`、frontend `308e7789-0b9c-46aa-93e9-7d1ddc110433` 均为 `SUCCESS`，deployment message 均为 `deploy bf94b76a platform-company admin boundary final`。
- backend health 为 `status=ok`、RLS `strict`、`runtime_control_bus.last_error=null`；frontend `/` 为 HTTP 200。
- production dashboard hard load：sidebar 显示“超级管理员”而非“公司后台”；nav 精确 8 项，dashboard cards 精确 7 项；只显示“后台页面”指标，不显示 User、数字员工或待审批业务指标，也不显示 Plaza；页面只请求 auth/public notification API。
- direct URL 矩阵：`/enterprise/digital-employees`、`/enterprise/knowledge`、`/enterprise/users`、`/enterprise/org`、`/enterprise/invitations`、`/enterprise/hr`、`/enterprise/approvals`、`/enterprise/action-guardrails` 与 `/plaza` 全部收敛到 `/enterprise/dashboard`；各页面均为 0 table row、0 email-like、0 UUID-like business DOM。
- authenticated status-only API 矩阵全部为 403：enterprise stats/approvals/org departments/org members/legacy-file status/invitation codes、Users、external principals、guard policies、company knowledge source contracts、system HR、Plaza posts 与 Plaza stats。
- 正向 Agent surface 保持：`/agents` hard reload 的 Agent summary API 为 200、system HR 为 403；EventPilot 可见且显示 owner/manage 入口，首屏 12 张可见卡全部为“我拥有的”，无 runtime error。该结果与 deployed query contract 一起证明修复没有删除 ownership/exact-user path，也没有恢复 company/department blanket scope。
- role-appropriate pages 保持：`/enterprise/info` 只请求 tenant config，company intro/legacy export/broadcast/runtime error 均为 0；`/enterprise/audit` 两个 summary API 均为 200，默认 DOM 的 `session_id/job_id/issues/agent_name/raw provider error` 均为 0。

## Recovery

- dashboard、Agent list、info 与 audit 均经过 hard reload；nav/card、允许 API、Agent owner surface 和零业务正文结果保持，无 stale company DOM 或 runtime alert。
- 九个 direct URL 在 fresh navigation 后均回到同一 dashboard；没有通过 browser history、query 或旧 React state 恢复 company-admin section。

## Consumption

- platform admin 可继续消费 provider/runtime/config/compliance、tenant presentation、safe audit 和其拥有或 exact-user 授权的 Agent。
- company lifecycle cards、routes、API body、Plaza 与 blanket Agent/company/department authority 不再进入该角色的默认产品路径。

## Acceptance

- production-shaped RED 固定 dashboard/route/API/Agent authority 的旧错误；GREEN 后 platform-admin contract 集 **423 passed**，真实 PostgreSQL 权限/RLS 定向 **13 passed**。
- full gates：backend **8484 passed, 2 skipped, 1 warning**；frontend **156 files / 1161 tests**；production build 与 AgentDetail/vendor budgets、Weekend architecture **18 passed**、96-entry manifest `valid=true`、Ruff 全量和 `git diff --check` 全绿。
- seven-atom finding result：Input、Authority、Execution、Evidence、Recovery、Consumption 与 finding-level Acceptance 在 exact deployed commit 上成立，因此 finding 为 `Verified`。
- 未证明：P29 member/org-admin/operator 三个独立 signed-in principals、四角色 screenshot/API matrix、role-change/expired-session、完整 pass-1/pass-2 与 cleanup。P29 不写 PASS，NPTCR 保持 `0/96`。

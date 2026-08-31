---
document_id: weekend-rc-2026-08-30-p29-platform-admin-company-workspace-audience-fault
owner: Codex
status: immutable
authority: production-finding-reproduction-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: reproduced
finding_id: PLATFORM-ADMIN-WORKSPACE-AUDIENCE-001
journey_id: P29-PADMIN
environment: production
source_commit: 8f6a726375452042cf1252977394c647dd2aba80
deployed_commit: 8f6a726375452042cf1252977394c647dd2aba80
persona_principal: authenticated lab platform_admin
result: FAIL
cleanup_result: NOT_APPLICABLE_READ_ONLY
---

# P29-PADMIN platform admin received company-admin workspace surfaces

本文件只记录 exact deployed application 上的只读最早错误状态，不进入 NPTCR，不把单一 platform-admin 身份外推为四角色矩阵或 P29 pass-1。

## Frozen contract

- P29-PADMIN 要求 platform admin 默认消费 tenant/provider/runtime/config/compliance health，不默认读取公司业务正文。
- changing URL、query 或 DOM 不得提升为更强 audience；frontend 与 server verdict 必须一致。

## Production reproduction

- signed-in `platform_admin` 打开 `/enterprise/dashboard`，默认看到全部 16 个 company workspace 入口，包括 Digital Employees、Company Knowledge、Members & Roles、Organization Structure、Approval Center、Action Guardrails、Invitation Codes 与 HR Agent；另有 Local Agent Channel company card。
- 直接只读导航证明这不是仅有链接的 presentation fault：
  - `/enterprise/digital-employees` 请求 Agent inventory、User inventory、System HR 与 stats API，均返回 200；页面渲染 14 行 Agent 数据和 email-like 字段。
  - `/enterprise/knowledge` 的 promotion intake、proposal、source contract、legacy candidate 与 import-job API 均返回 200。
  - `/enterprise/users` 的 tenant User 与 external-principal API 均返回 200；页面渲染成员/角色控制。
  - `/enterprise/org` 的 Feishu org setting、department、member 与 runtime-status API 均返回 200。
  - `/enterprise/invitations`、`/enterprise/hr`、`/enterprise/approvals`、`/enterprise/action-guardrails` 的业务 API 均返回 200。
- `/enterprise/info`、`/enterprise/llm`、`/enterprise/memory`、`/enterprise/extensions`、`/enterprise/runtime-budgets`、`/enterprise/quotas`、`/enterprise/audit` 与 `/admin/platform-settings` 是 role-appropriate platform/config/health surfaces，不能因本 finding 被整体删除。

## Root wiring

- frontend `WorkspaceGuard` 只验证 `org_admin | platform_admin`，`WorkspaceLayout` 和 `ControlPlane` 对两者渲染同一全量 section/card 集；直接 `/enterprise/knowledge` 与 forced-tab routes 没有更窄的 org-admin guard。
- backend company lifecycle endpoints 同样复用 `get_current_admin`、`platform_admin | org_admin` 判断，或 `check_agent_access()` 的 platform-admin blanket `manage`，因此 URL/API 绕过前端仍会得到更强业务 surface。
- 这是 authenticated role、route 和 permission scope 的机械 authority defect；修复不得扫描自然语言，也不得把 platform health/config surface 一并隐藏。

## Safety boundary

- 本次只读取 DOM 结构、字段数量、请求路径和 HTTP status；未记录 User、Agent、部门、外部身份、审批或知识正文。
- 没有读取浏览器 token、cookie、localStorage/session store 或响应 body；没有点击 save/create/delete/resolve/sync/test 等 effect controls。
- 没有角色、grant、tenant、credential、billing 或生产数据变更。

## Verdict

- finding `PLATFORM-ADMIN-WORKSPACE-AUDIENCE-001` 为 P1 `Reproduced`。
- 必须以 exact role allowlist 同时修复 frontend consumption 与 backend authority，保留显式 user-scoped Agent grant，完成 RED/GREEN、全量门、三服务同提交部署和 signed-in hard-reload/direct-URL/API negative 后才能到 `Verified`。
- P29 仍不写 PASS，NPTCR 保持 `0/96`。

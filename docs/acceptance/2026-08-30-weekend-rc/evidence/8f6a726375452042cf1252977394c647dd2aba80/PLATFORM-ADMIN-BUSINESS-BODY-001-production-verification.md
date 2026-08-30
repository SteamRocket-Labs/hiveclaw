---
document_id: weekend-rc-2026-08-30-platform-admin-business-body-production-verification
owner: Codex
status: immutable
authority: production-finding-verification-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: verified
finding_id: PLATFORM-ADMIN-BUSINESS-BODY-001
journey_id: P29-PADMIN
pass: bounded-platform-admin-business-content-boundary-verification
environment: production
source_commit: 8f6a726375452042cf1252977394c647dd2aba80
deployed_commit: 8f6a726375452042cf1252977394c647dd2aba80
deployment_ids: backend=35e6d6e5-4dc4-4167-b120-d9a42113f4a0; backend-api=86615c7d-ffc2-4e9d-8ad6-c31c9313dc70; frontend=cfa5f254-61f4-4666-b554-5b1fb9fc4d22
persona_principal: authenticated lab platform_admin
result: VERIFIED
recovery_result: PASS_HARD_RELOAD_AND_DENIED_ROUTE
cleanup_result: NOT_APPLICABLE_READ_ONLY
---

# PLATFORM-ADMIN-BUSINESS-BODY-001 production verification

本文件只关闭 platform admin 默认公司正文与业务动作披露根因，不进入 NPTCR，也不把单一 platform-admin 身份外推为 P29 四角色矩阵或双遍 PASS。

## Root reproduction

- exact deployed `b23e94210e7e9523bafc3b591b35db8fc2762224`、signed-in `platform_admin` 只读打开 `/enterprise/info`。默认 DOM 直接显示公司介绍正文标记 `AI agents for teams`，同时展示 legacy-file surface 与 broadcast controls；无需 company-admin 身份或额外 reason。
- live source trace 证明 `EnterpriseSettings` 对所有 admin 身份挂载公司介绍、legacy export 与 broadcast section；backend `/enterprise/info` 以及通用 `/enterprise/system-settings/company_intro*` 也允许 platform admin 读取或改写 raw body。
- 页面标题说明还把 company profile、legacy export 与 notification broadcast 宣告为当前页面能力；因此即使隐藏某个 section，平台管理员 audience 仍会被错误描述为业务内容管理者。
- 该行为违反 P29 platform-admin 合同：默认只消费 tenant/provider/runtime/config/compliance health；同一 URL、query 或 DOM 不得把公司业务正文或 company-admin 写动作提升给 platform admin。

## Input

- 只读打开 selected lab tenant 的 `/enterprise/info`；没有保存 tenant 配置、公司介绍、broadcast、legacy export 或其他业务数据。
- post-fix 对同一 signed-in tab hard reload；同时复查 `/enterprise/audit` 与既有跨用户 denied Session route，没有触发 provider、审批、角色或数据写入。

## Authority

- platform admin 保留 tenant identity、timezone、presentation 与删除/配置等 control-plane surface；页面显示明确的“仅显示租户配置”角色边界。
- company intro、legacy-file 和 broadcast 只对 `org_admin` 挂载、请求与保存。frontend 订阅派生的 role boolean，不为 platform admin 发起 raw business-body 请求。
- backend 在 raw `/enterprise/info` list/update 和 `company_intro*` system-setting get/update 路由入口对 platform admin 返回 403；org admin 的既有行为保持。
- 判定只使用 authenticated role 与 exact setting key prefix，属于 authority/machine contract；不扫描自然语言内容，也不改写已有正文。

## Execution

- backend 在现有 enterprise route 上复用一个 platform-admin boundary guard；没有新增 service、schema、migration、dependency、feature flag 或生产数据变更。
- frontend 在现有 `EnterpriseSettings` / `WorkspaceInfoSection` 消费点按 role 派生 `canManageCompanyContent`，不复制权限系统或建立第二事实源。
- application `170c30e80d00709a314c2838c6c4842e906800a3` 首次部署后主体 section 已消失，但 production hard reload 仍显示错误的页面能力说明；该残余未被忽略。
- exact `8f6a726375452042cf1252977394c647dd2aba80` 随后把标题说明收敛为 role-appropriate company actions，并以 mounted regression 固定。

## Evidence

- exact `8f6a726375452042cf1252977394c647dd2aba80` 已 push；backend `35e6d6e5-4dc4-4167-b120-d9a42113f4a0`、backend-api `86615c7d-ffc2-4e9d-8ad6-c31c9313dc70`、frontend `cfa5f254-61f4-4666-b554-5b1fb9fc4d22` 均 `SUCCESS` 且 deployment message 绑定 exact full SHA。
- backend health 为 `status=ok`、RLS `strict`、`runtime_control_bus.last_error=null`；frontend `/` 为 HTTP 200。
- `/enterprise/info` hard reload 后：新 role-appropriate description 1、旧 description 0、边界标题 1、边界正文 1、company-intro heading 0、pre-fix body marker 0、legacy export surface 0、broadcast surface 0、tenant name 1、timezone 1、runtime error 0。
- 同一 commit 的 `/enterprise/audit` hard reload 仍显示 400 条 summary；`session_id/job_id/issues/reason/agent_name/raw Insufficient Balance` 均为 0，既有 GLM probe correlation/provider/model/success 保留。

## Recovery

- `/enterprise/info` hard reload 后 role boundary 与零 business surface 保持，没有 stale company body、重复请求错误或 runtime alert。
- 跨用户 Session `/agents/5d99fe45-7ea9-4f7e-979c-c57bcb2cd4ea/sessions/d5b47bd0-27d1-46e7-b417-4e9da362b553` 继续只显示 not-found、denied copy 与返回入口；read-only shell、runtime error、artifact 和合法 MAPLE marker 均为 0。

## Consumption

- platform admin 仍可从同一页面消费 tenant name、timezone、theme/presentation 与 role-appropriate control-plane actions；修复不是清空页面或整体禁止访问。
- company-admin business content 与 write controls 不再进入 platform-admin 默认读取流；页面描述也不再宣称这些越权能力。

## Acceptance

- production-shaped RED：backend route tests **4 failed / 11 passed**；frontend mounted/component tests **2 failed / 1 passed**。
- GREEN：backend target **16 passed**、adjacent **52 passed**；frontend target **3 passed**、adjacent **37 passed**。
- full gates：backend **8453 passed, 2 skipped, 1 warning**；frontend **155 files / 1151 tests**；i18n 9 node tests、en=zh=3997、anomaly 0；production build、AgentDetail/vendor bundle budgets、31 permission/RLS/RC architecture tests、96-entry manifest validate、Ruff/format 与 `git diff --check` 全绿。
- FastAPI route-entry regressions exercise real GET routing and prove `/enterprise/info` plus `company_intro*` platform-admin guards execute before DB access；mounted `EnterpriseSettings` regression proves the signed-in product consumer does not request or render raw business content。
- seven-atom finding result：Input、Authority、Execution、Evidence、Recovery、Consumption 与 finding-level Acceptance 在 exact deployed commit 上成立，因此 finding 为 `Verified`。
- 未证明：直接 production authenticated API 403 receipt。当前 Browser 验证不读取 token/localStorage，产品 UI 也不会再调用该 raw API，因此没有为补一条回执绕过登录边界。
- 未证明：P29 employee/company-admin/operator 三个独立 signed-in principals、四角色 screenshot/API matrix、role change/expired-session、完整 pass-1/pass-2、operator reason-bound inspector、negative authority 全集与 cleanup。P29 不写 PASS，NPTCR 保持 `0/96`。

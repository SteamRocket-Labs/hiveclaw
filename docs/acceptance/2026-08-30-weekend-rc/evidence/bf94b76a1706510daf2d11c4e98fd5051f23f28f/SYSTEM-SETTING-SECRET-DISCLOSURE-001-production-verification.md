---
document_id: weekend-rc-2026-08-30-system-setting-secret-disclosure-production-verification
owner: Codex
status: immutable
authority: production-finding-verification-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: verified
finding_id: SYSTEM-SETTING-SECRET-DISCLOSURE-001
journey_id: P29-PADMIN
pass: bounded-system-setting-secret-response-verification
environment: production
source_commit: bf94b76a1706510daf2d11c4e98fd5051f23f28f
deployed_commit: bf94b76a1706510daf2d11c4e98fd5051f23f28f
deployment_ids: backend=07059ce5-12ca-4b57-ac92-bc05d58dfb49; backend-api=c70ff972-b039-4cf5-831d-4526962c8d9d; frontend=308e7789-0b9c-46aa-93e9-7d1ddc110433
persona_principal: authenticated lab platform_admin
result: VERIFIED
recovery_result: PASS_STATUS_ONLY_RELOAD
cleanup_result: NOT_APPLICABLE_READ_ONLY
---

# SYSTEM-SETTING-SECRET-DISCLOSURE-001 production verification

本文件关闭 generic system-setting role/key 与 `feishu_org_sync` response projection 根因；不包含、推断或确认任何实际 production credential，不进入 NPTCR。

## Root reproduction

- exact deployed `8f6a726375452042cf1252977394c647dd2aba80` 的 `/api/enterprise/system-settings/feishu_org_sync` 对 signed-in platform admin 为 200。
- exact source 允许 generic route 读取任意未列入 tenant-key set 的 global key；GET/PUT 直接回传 stored `value`，而 Feishu runtime consumer 从 `value.app_secret` 读取 credential。
- reproduction 只以 route status 与 source contract 证明 disclosure path，没有读取 response body、secret length/value、数据库 row 或 process environment。immutable evidence：`evidence/8f6a726375452042cf1252977394c647dd2aba80/SYSTEM-SETTING-SECRET-DISCLOSURE-001-reproduction.md`。

## Input and authority

- platform admin 只允许 generic route 的 exact platform keys `notification_bar` 与 `platform`；org admin 只允许 tenant keys `agent_permission_default`、`feishu_org_sync` 与 exact `company_intro*` family。
- unknown 或 role-disallowed key 在 selected-tenant resolution 和 DB query 前返回 403。
- `feishu_org_sync` GET/PUT response 删除 `app_secret`，仅添加 `app_secret_configured: boolean`；stored value 与合法 org-admin update effect 不被改写。

## Execution

- 修复复用 enterprise API 现有 route，增加 exact role/key allowlist 与一个 response projection helper；没有新 service、schema、migration、dependency、feature flag 或 production data mutation。
- 平台只机械执行 authenticated role、exact key 与 response schema 合同，不扫描或判断 natural-language secret 内容。

## Evidence

- exact backend code 随 application `bf94b76a1706510daf2d11c4e98fd5051f23f28f` 部署；三服务 deployment IDs 与同目录 workspace-audience evidence 一致且均 `SUCCESS`。backend health `status=ok`、RLS `strict`、runtime bus no error。
- signed-in platform admin 对 `/api/enterprise/system-settings/feishu_org_sync` 的 production status-only probe 为 403。探针复用应用自身请求的鉴权 header，但不读取或输出 header/token/storage；仅访问 `response.status`，不调用 `json()`、`text()`、`blob()` 或其他 body reader，并给产品 consumer 返回独立安全空数组。
- local route-entry regression 证明 platform admin 的 Feishu/unknown key 与 org admin 的 platform keys 均在 DB 前 403；synthetic GET、missing setting 与 PUT 回归证明 response 只含非敏感字段和 `app_secret_configured`，永不回显 synthetic secret，同时合法 PUT 仍持久化原值。
- full gates 与部署证据同 workspace-audience finding：backend **8484 passed, 2 skipped, 1 warning**；真实 PostgreSQL 定向 **13 passed**；frontend **1161 tests**；build/budgets、Weekend architecture **18 passed**、manifest、Ruff 与 diff check 全绿。

## Recovery and consumption

- status-only matrix 连续执行并在每项后恢复原生 request function；最终 hard reload 的 auth、Agent、tenant-config 与 audit 正向路径保持 200，无登录状态丢失或 runtime error。
- platform admin 不再消费 Feishu tenant setting；org-admin consumer 的 deployed response contract 只能看到 secret 是否已配置，不接收 secret value。

## Acceptance

- finding 的 Input、Authority、Execution、Evidence、Recovery、Consumption 与 Acceptance 已在 exact deployed code、authenticated platform negative 和完整本地 route/schema gates 上成立，因此状态为 `Verified`。
- 未读取、写入、清空或轮换任何 production credential；未发送 PUT 或触发 org sync。
- 当前没有独立 signed-in org-admin identity，所以没有生产 org-admin 200 response projection screenshot。该缺口阻止 P29 四角色矩阵与 Journey PASS，但不回退 exact deployed route/schema 根因的 `Verified`；NPTCR 保持 `0/96`。

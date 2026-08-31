---
document_id: weekend-rc-2026-08-30-system-setting-secret-disclosure-reproduction
owner: Codex
status: immutable
authority: production-finding-reproduction-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: reproduced
finding_id: SYSTEM-SETTING-SECRET-DISCLOSURE-001
journey_id: P29-PADMIN
environment: production
source_commit: 8f6a726375452042cf1252977394c647dd2aba80
deployed_commit: 8f6a726375452042cf1252977394c647dd2aba80
persona_principal: authenticated lab platform_admin
result: FAIL
cleanup_result: NOT_APPLICABLE_READ_ONLY
---

# Generic system-setting response can disclose a stored tenant secret

本文件不包含、推断或确认任何实际 production credential。它只记录 exact source contract 与 authenticated route status，不读取响应 body，不进入 NPTCR。

## Earliest error state

- deployed `/api/enterprise/system-settings/feishu_org_sync` 对 signed-in `platform_admin` 返回 HTTP 200。
- exact deployed source 把 `feishu_org_sync` 定义为 tenant setting；其 runtime consumers从 stored `value.app_secret` 取得 credential。
- generic GET 与 PUT 都直接返回完整 `setting.value`，没有 response projection；同一路由还接受任意未列入 tenant-key set 的 key，并直接读取或写入 global `SystemSetting`。
- 因此只要目标 tenant 已配置 secret，authenticated response contract 就会把它作为普通 JSON 字段返回。是否存在实际 production secret 未被读取，也不需要读取才能证明代码级 disclosure path。

## Authority and required correction

- platform admin 只应通过 generic route 读取/更新 platform keys `notification_bar` 与 `platform`。
- org admin 只应读取/更新 tenant business keys `agent_permission_default`、`feishu_org_sync` 与 exact `company_intro*` family。
- unknown or role-disallowed keys must fail with 403 before any DB query.
- `feishu_org_sync` GET/PUT response must omit `app_secret` and expose only a boolean configured indicator; stored value and existing update effect remain unchanged.

## Safety boundary

- 未检查 response body、secret length/value、浏览器 auth storage、database row 或 process environment。
- 未发送 PUT、未保存设置、未同步组织、未修改 credential 或 tenant data。
- 该 finding 使用 exact key/role/schema machine facts，不使用关键词扫描来判断业务语义。

## Verdict

- finding `SYSTEM-SETTING-SECRET-DISCLOSURE-001` 为 P1 `Reproduced`。
- 必须先有 fail-before-DB role/key 回归与 response-projection 回归，再做最小共享 route 修复、全量验证、同提交部署和 signed-in status/DOM negative；不得为 production 回执读取真实 secret。
- P29 不写 PASS，NPTCR 保持 `0/96`。

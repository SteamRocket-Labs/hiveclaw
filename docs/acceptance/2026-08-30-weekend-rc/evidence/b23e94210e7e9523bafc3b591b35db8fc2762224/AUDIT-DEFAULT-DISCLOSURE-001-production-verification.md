---
document_id: weekend-rc-2026-08-30-audit-default-disclosure-production-verification
owner: Codex
status: immutable
authority: production-finding-verification-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: verified
finding_id: AUDIT-DEFAULT-DISCLOSURE-001
journey_id: P29-PADMIN
pass: bounded-admin-audit-disclosure-verification
environment: production
source_commit: b23e94210e7e9523bafc3b591b35db8fc2762224
deployed_commit: b23e94210e7e9523bafc3b591b35db8fc2762224
deployment_ids: backend=03d0919e-c86f-4e8a-9697-ead67479774c; backend-api=b0bb7ca3-5749-4af2-b101-7f68bcc9fa39; frontend=0dd299d8-8dce-4052-9d7c-da2a1e2e50f4
persona_principal: authenticated lab platform_admin
result: VERIFIED
recovery_result: PASS_HARD_RELOAD_AND_DENIED_ROUTE
cleanup_result: NOT_APPLICABLE_READ_ONLY
supersedes: evidence/cc6e726218bd491120f942edfa91e51d2d167ff4/LLM-PROBE-AUDIT-001-production-verification.md#not-proven
---

# AUDIT-DEFAULT-DISCLOSURE-001 production verification

本文件只关闭 admin audit 默认披露根因，不进入 NPTCR，也不把单一 platform-admin 身份外推为 P29 四角色矩阵或双遍 PASS。

## Root reproduction

- exact deployed `cc6e726218bd491120f942edfa91e51d2d167ff4`、signed-in `platform_admin` 打开 `/enterprise/audit`。页面默认一次合并 200 条 legacy audit 与 200 条 canonical security audit，共显示 400 条记录。
- default DOM 量化：`session_id=110`、`job_id=94`、`issues=94`、`reason=41`、`agent_name=77`、raw `Insufficient Balance=90`。其中 recovery reason 是用户提交的核验说明，issues 含 raw provider payload；无需 operator reason 或逐项展开即可读取。
- live source trace：legacy `/enterprise/audit-logs` 返回完整 `AuditLogOut.details/user_id/ip_address`；canonical `/enterprise/audit` 返回完整 `details/ip_address/user_agent/hash/execution identity`；frontend 对两者 `Object.entries(details)` 默认展开。
- `/enterprise/audit` 的 `search` 还把 raw JSON details 纳入 `ILIKE`，即使 UI 隐藏，也可通过查询命中推断业务 payload；CSV export 输出 raw details 与 IP。export/chain 另有 selected-tenant scope 断点：两者直接使用 `current_user.tenant_id`，没有复用 platform-admin selected tenant resolution/pinning。
- 该行为违反 P29 的 platform-admin 合同“tenant/runtime/provider/config/compliance health，不默认读取业务正文”，也违反同 URL/query/DOM 不得提升 audience 的负向标准。

## Input

- 只读打开当前 selected lab tenant 的 `/enterprise/audit`，没有点击审批、修复、暂停、配置、导出或其他写控制。
- post-fix 使用同一 signed-in tab hard reload；没有再次调用任何模型 provider。

## Authority

- admin list 与 CSV 现在只投影 action、time、actor/resource identity、hash/compliance identity，以及明确 allowlist 的 control-plane fields：`capability`、`changed_fields`、`force`、`latency_ms`、`max_tokens`、`model`、`outcome`、`phase`、`probe_id`、`provider`、`retry_count`、`status`、`success`、`tool`。
- `session_id`、`job_id`、`issues`、`reason`、`agent_name`、credential-like keys、IP、user-agent 与 free-form execution label 不进入 admin response/CSV/DOM。raw canonical rows和 hash input 没有被改写或删除，仍保留在持久证据源供受治理 operator path使用。
- raw details 不再参与 admin summary search；查询只匹配 machine action/event type，防止通过 search 命中泄漏隐含内容。
- audit list/export/chain 都使用 `resolve_and_pin_tenant_scope()`；platform admin 可以显式选择 tenant，org admin 不能越出自身 tenant，RLS session 同时固定。

## Execution

- backend 新增 `AuditLogSummaryOut`，并将 `AuditEventOut` 收敛为 admin summary；actor/resource/hash 等合规事实保持，网络指纹和业务 payload 删除。
- `project_admin_audit_details()` 是 exact key allowlist，不扫描自然语言、不推断业务含义，也不改变 canonical audit material。
- CSV 复用同一 projection 并移除 IP column；chain verification 继续以 canonical raw event 重算 hash，但 selected tenant authority 与 list/export 一致。
- frontend 在唯一 audit consumer 上重复同一 allowlist，防止旧 backend、mock 或 accidental adapter regression 把 raw details重新塞进 DOM。
- 没有 schema、migration、backfill、dependency、feature flag、persistent config 或 production data mutation。

## Evidence

- production hard reload 后仍显示 400 条记录；probe `a0f1be98-27bd-4d69-9bde-247b57c6b16c` 恰出现两次，`provider=zhipu`、`model=glm-5.3`、`success=true` 继续可消费。
- 同一 DOM 的 `session_id/job_id/issues/reason/agent_name/Insufficient Balance` counts 全部为 0；不是 CSS 隐藏或折叠，server response projection 与 frontend consumer 双重约束均已执行。
- exact `b23e94210e7e9523bafc3b591b35db8fc2762224` 已 push；backend `03d0919e-c86f-4e8a-9697-ead67479774c`、backend-api `b0bb7ca3-5749-4af2-b101-7f68bcc9fa39`、frontend `0dd299d8-8dce-4052-9d7c-da2a1e2e50f4` 均 `SUCCESS` 且 message 绑定 exact full SHA。
- backend health `status=ok`、RLS `strict`、`runtime_control_bus.last_error=null`；frontend HTTP 200。

## Recovery

- hard reload 后记录数、安全 probe 关联与六类零披露保持稳定；没有 stale raw details、duplicate query 或重新触发 provider effect。
- 同一 application commit 下重新 hard navigation 到跨用户 Session `/agents/5d99fe45-7ea9-4f7e-979c-c57bcb2cd4ea/sessions/d5b47bd0-27d1-46e7-b417-4e9da362b553`，终态只显示“找不到此会话 / 此会话不存在，或当前账号无法访问 / 返回数字员工”。DOM 不含 `Read-only · User`、运行错误、会话交付物、合法 MAPLE marker 或目标 Session body。

## Consumption

- platform admin 仍可读取 tenant/provider/runtime/compliance health：action、event type、severity、actor/resource、hash identity、model/provider、probe correlation、status/outcome/latency。
- business payload 与 operator forensic detail 不再占默认读取流；修复没有通过清空整个 audit 页面、删除事件或隐藏模型健康证据达成。

## Acceptance

- production-shaped RED：backend schema/export/chain/search 共 **4 failed**；frontend DOM disclosure **1 failed / 2 passed**。
- GREEN：backend selected-tenant + audit service **30 passed**；frontend workspace module **3 passed**；Ruff check/format 通过。
- full gates：backend **8448 passed, 2 skipped, 1 warning**；frontend **154 files / 1149 tests**；i18n 9 node tests、en=zh=3995、全部 anomaly 0；production build、AgentDetail/vendor bundle budgets、35 architecture tests、96-entry manifest validate 与 `git diff --check` 全绿。
- seven-atom result：Input、Authority、Execution、Evidence、Recovery、Consumption 与 finding-level Acceptance 均在 exact production commit 上闭环，因此 finding 为 `Verified`。
- 未证明：P29 employee/company-admin/operator 三个独立 signed-in principals、四角色 screenshot/API matrix、role change/expired-session、完整 pass-1/pass-2、operator reason-bound raw inspector、negative authority 全集与 cleanup。P29 不写 PASS，NPTCR 保持 `0/96`。

---
document_id: weekend-rc-2026-08-30-llm-probe-audit-production-verification
owner: Codex
status: immutable
authority: production-finding-verification-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: verified
finding_id: LLM-PROBE-AUDIT-001
journey_id: P29-PADMIN / P33-GLM
pass: bounded-provider-probe-audit-verification
environment: production
source_commit: cc6e726218bd491120f942edfa91e51d2d167ff4
deployed_commit: cc6e726218bd491120f942edfa91e51d2d167ff4
deployment_ids: backend=f619e4a9-5ff3-4389-8b32-3e13de2efc2e; backend-api=7edd592d-9e8e-4c73-b074-1a8f5f818497; frontend=beb9cd36-f28c-4cba-a5f5-4614c88b88b0
persona_principal: authenticated lab platform_admin
result: VERIFIED
recovery_result: PASS_RELOAD_NO_REPLAY
cleanup_result: NOT_APPLICABLE_CONTROL_PLANE_PROBE
---

# LLM-PROBE-AUDIT-001 production verification

本文件只关闭已复现的 provider health Test 审计断点，不进入 NPTCR，也不把 bounded health probe 外推为 P33 compatibility task。

## Root reproduction

- deployed `6a6695e8` 上，signed-in `platform_admin` 从 `/enterprise/llm` 分别触发受支持的 bounded health Test：MiniMax 成功 `7623ms`，DeepSeek 一次返回 `HTTP 402 Insufficient Balance`，GLM 成功 `7575ms`。
- 每次 Test 都是真实外部 provider 调用，可能产生 token、成本或供应商侧 effect；但 `/enterprise/audit` 没有对应 started/completed 事件。
- source trace 证明 `test_llm_model()` 直接返回 provider result，没有 canonical audit writer；前端 audit 页面只消费 legacy `/enterprise/audit-logs`，该 agent-bound surface 不能表达 agentless control-plane action。
- canonical `/enterprise/audit` 还只使用登录 principal 的 tenant，platform admin 不能把已选择 tenant 固定到读取边界。由此形成 Input/Execution 与 Evidence/Consumption 之间的断点。
- 三次 pre-fix verdict 均保留原事实：MiniMax 与 GLM 当次连通，DeepSeek 被供应商以余额不足拒绝。没有重试 DeepSeek、充值、更换 credential 或修改模型配置；这些 pre-fix 调用因没有审计事件，不能 retroactively 宣称已审计。

## Input

- post-fix 只在 signed-in `/enterprise/llm` 对当前 enabled GLM-5.3 entry 点击一次 exact “测试”。
- 请求于 `2026-08-30T21:21:32.516Z`（Asia/Shanghai `2026-08-31 05:21:32`）开始，使用既有 safe probe contract `max_tokens=16`。
- 没有调用 MiniMax 或 DeepSeek 第二次，没有修改 model/provider/base URL/API key/fallback/billing。

## Authority

- backend 在真实 provider effect 前解析并固定 selected tenant；`/enterprise/audit?tenant_id=...` 对 platform admin 使用同一 server-side tenant resolution。
- provider call 前必须先提交 `SecurityAuditEvent`。若 started audit 不可用，事务 rollback 并返回 HTTP 503，provider effect 不发生。
- canonical audit detail 只保存 phase、provider、model、max_tokens、probe/request ID、terminal success 与 latency；failure 仅保存 exception type。API key、base URL、provider reply、raw error 与业务正文不进入 audit payload。

## Execution

- started event：`event_type=llm_model.test_started`、`action=test_llm_model_started`。
- terminal event：`event_type=llm_model.test_completed`、`action=test_llm_model_completed`，与 started 共用生成的 `probe_id/request_id`。
- provider 成功或失败都会进入 terminal audit；若 provider effect 已发生但 terminal audit persistence 失败，API 返回 `success=false`、`provider_success`、`audit_status=result_persistence_failed`、`retryable=false`，明确禁止自动重试，同时保留 durable started event。
- frontend 并行读取 legacy operational audit 与 canonical security audit，归一化后合并排序；没有新增 schema、migration、dependency、feature flag 或持久配置。

## Evidence

- post-fix production probe ID：`a0f1be98-27bd-4d69-9bde-247b57c6b16c`。
- provider/model：`zhipu` / `glm-5.3`；completed 为 `success=true`、`latency_ms=3411`。
- audit UI 显示 completed `2026/8/31 05:21:36` 与 started `2026/8/31 05:21:32`；两条事件具有同一 probe ID、provider、model 与 `max_tokens=16`。
- hard reload 后 exact counts 为 `startedActions=1`、`completedActions=1`、`probeOccurrences=2`、`hasSuccess=true`、`hasRawApiKey=false`。没有重复 provider call 或重复 audit pair。
- exact application commit `cc6e726218bd491120f942edfa91e51d2d167ff4` 已 push；backend `f619e4a9-5ff3-4389-8b32-3e13de2efc2e`、backend-api `7edd592d-9e8e-4c73-b074-1a8f5f818497`、frontend `beb9cd36-f28c-4cba-a5f5-4614c88b88b0` 均 `SUCCESS` 且 deployment message 绑定该 full SHA。
- backend health `status=ok`、RLS strict、`runtime_control_bus.last_error=null`；frontend HTTP 200。

## Recovery and deployment incident

- 首次打包时错误地把 short SHA `cc6e7262` 手工扩展为不存在的 full SHA；`git archive` 失败，而原 zsh 脚本没有 fail-fast，导致 Railway 收到空上传。backend `446bb56e-f541-4baf-9fb9-de57ff59b715`、backend-api `771d44b3-e14d-4016-80c6-a5f543378848`、frontend `7f139625-77f3-4a97-949f-f0deeaae8e5c` 均立即 `FAILED`，没有替换当时运行实例。
- recovery 重新读取 `git rev-parse HEAD`，使用 `set -euo pipefail`，并在上传前机械确认 backend/frontend archive 含目标 Dockerfile 与 `railway.json`；随后三服务按 exact commit 成功部署。
- audit 页面 hard reload 后仍只有同一 started/completed pair；恢复过程没有盲目重发 provider probe。

## Consumption

- platform admin 在 tenant audit 页面可直接看到真实模型 Test 的开始与终态、模型、供应商、延迟和安全 resource identity。
- agentless control-plane action 不再依赖 legacy agent-bound audit；默认 DOM 不暴露 API key、base URL、raw provider payload、员工 Session 正文或其他 tenant 内容。

## Acceptance

- production-shaped RED：backend focused `5 failed`；frontend `2 failed`。系统 Python 3.13 触发的第三方 `pkg_resources` warning 属于环境污染，已丢弃；正确 Python 3.12 venv 才作为验收环境。
- GREEN：backend focused `6 passed`，完整 selected-tenant API file `22 passed`；frontend adjacent `34 passed`；Ruff check/format 通过。
- full gates：backend **8443 passed, 2 skipped, 1 warning**；frontend **154 files / 1149 tests**；i18n en=zh=3995、全部 anomaly 0、9 个 node tests、production build/bundle budgets、24 architecture tests、96-entry manifest validate 与 `git diff --check` 全绿。
- seven-atom result：Input、Authority、Execution、Evidence、Recovery、Consumption 与 finding-level Acceptance 均已在 exact production commit 上闭环，因此 finding 推进为 `Verified`。
- 未证明：P29 四角色 matrix、完整 platform-admin pass-1/pass-2、role-change/expired-session/fault recovery、P33 三个模型各自的 frozen compatibility task、P33 token/cache/cost/operator evidence 全矩阵、negative authority、synthetic cleanup。NPTCR 保持 `0/96`。

---
document_id: weekend-rc-2026-08-30-p29-padmin-fault-denied-session-shell
owner: Codex
status: immutable
authority: production-failure-evidence-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: reproduced
finding_id: SESSION-AUTHORITY-PRESENTATION-001
journey_id: P29-PADMIN
pass: negative-authority-pre-fix
environment: production
source_commit: 3482b57a383d3c5bd33a5bcf813b87c6fab23339
deployed_commit: 3482b57a383d3c5bd33a5bcf813b87c6fab23339
deployment_ids: backend=7c196980-34c6-4846-bf25-0397b7b55c0e; backend-api=8e7545b8-9b6c-4b32-a77d-48883191728a; frontend=6f6bd18c-1681-4049-ac20-6660a3f84fc3
persona_principal: authenticated lab platform_admin
result: FAIL
recovery_result: NOT_RUN
cleanup_result: NOT_APPLICABLE_READ_ONLY
supersedes: none
---

# P29-PADMIN denied Session route rendered a false workbench shell

本文件只固化修复前 production 失败事实，不进入 NPTCR。服务端拒绝正确、跨用户正文没有泄露；失败位于前端消费层：URL 指向不可访问 Session 时，页面没有收敛到 denied/not-found，而是用占位数据渲染了一个看似真实的 Session workbench。

## Input

- signed-in `platform_admin` 从 production 前端直接打开 `/agents/5d99fe45-7ea9-4f7e-979c-c57bcb2cd4ea/sessions/d5b47bd0-27d1-46e7-b417-4e9da362b553`。
- 目标 Session metadata 只通过 Railway backend 内受控只读查询解析：Session `d5b47bd0-27d1-46e7-b417-4e9da362b553`、user `a9408982-237c-40c4-8af2-ec094179829f`、Agent `5d99fe45-7ea9-4f7e-979c-c57bcb2cd4ea`。没有读取 title、message、transcript、artifact 或业务正文。
- URL 未携带 `operator_view` 或 `operator_reason`，因此这是普通 denied/not-found 负向探针，不是运营视图读取。

## Authority

- 当前浏览器 principal 经服务端核验为 user `42778d4b-fa70-47c1-ad3a-15f7fcf5e8aa`、role `platform_admin`、tenant `aac728fb-fe1c-45df-a2ff-a56e024a37a0`。
- message 与 lineage API 均返回 `Session not found`；服务端没有返回目标 Session 正文或更强 audience 数据。
- 未登录、未创建账号、未修改 role/grant、未读取 credential，也未启用 operator view。

## Execution

- `AgentDetail` 未在 URL Session 占位对象解析完成前保持 resolving 状态，而是立即把 `is_pending_session_lookup` 占位对象交给 `AgentChatSection`。
- transcript/message 读取的 terminal 404 被 `selectSession()` catch 转成 `runtime_action_failed/session_load_failed` 普通时间线事件；active Session 和 runtime workbench 仍保持可渲染。

## Evidence

- 后端失败事实为 `Session not found`，且没有目标 Session title、message、transcript 或 artifact 出现在 DOM。
- 同一页面却显示：标题“会话”、通用“完成”、`Read-only · User`、`已处理 1 个步骤`、`运行错误: 失败`、会话交付物 `0`、runtime `空闲 / 会话 · 就绪`、`0 running / 0 waiting`，以及一条合成 `runtime_action_failed` 活动。
- 页面错误文案为“对话加载失败。请重试当前会话或刷新页面。”，没有呈现服务端 denied/not-found verdict。
- 当前 tenant 侧栏仍只显示当前 tenant Agent；未观察到跨用户业务正文泄露。

## Recovery

- 修复前没有执行角色变更或重新授权；`recovery_result=NOT_RUN`。
- 该失败本身证明 URL/query negative route 没有在 terminal 404 后收敛，不能用“正文未泄露”替代 truthful recovery presentation。

## Consumption

- 用户消费到的是平台合成的 Session 成功/失败壳，而不是权威的 not-found 终局。
- 因 server verdict 与 frontend presentation 不一致，P29-PADMIN 的 audience/server-verdict/negative/recovery acceptance 均未成立。

## Acceptance

- finding `SESSION-AUTHORITY-PRESENTATION-001` 状态从 `Reproduced` 进入本地 `Fix Candidate` 后，仍必须完成三服务同提交部署并对同一负向 URL 做 signed-in production 复验。
- 本文件不是 P29-PADMIN pass-1/pass-2，也不证明四角色 screenshot matrix、provider/runtime/compliance 正向面、role-change recovery、operator audit 或 cleanup。
- NPTCR 保持 `0/96`。

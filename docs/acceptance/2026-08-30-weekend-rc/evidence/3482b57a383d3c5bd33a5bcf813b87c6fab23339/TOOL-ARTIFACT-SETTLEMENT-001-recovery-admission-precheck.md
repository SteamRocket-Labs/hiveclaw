---
document_id: weekend-rc-2026-08-30-tool-artifact-settlement-recovery-admission-precheck
owner: Codex
status: active
authority: immutable-production-pre-action-checkpoint-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: deployed-read-only-admission-pass-operator-action-pending
journey_id: P01-MAIN
pass: finding-recovery-admission-precheck
environment: production
source_commit: 3482b57a383d3c5bd33a5bcf813b87c6fab23339
deployed_commit: 3482b57a383d3c5bd33a5bcf813b87c6fab23339
manifest_sha256: d320edceeb26cf68fa724e77502d811e5476fa04ee3c9128075cc8c79eb38117
deployment_ids: backend=7c196980-34c6-4846-bf25-0397b7b55c0e; backend-api=8e7545b8-9b6c-4b32-a77d-48883191728a; frontend=6f6bd18c-1681-4049-ac20-6660a3f84fc3
persona_principal: authenticated lab platform-admin using EventPilot in the selected experimental tenant
target_session_id: b3962147-07cd-4223-8f23-f00193d7735c
target_runtime_task_id: 76a32f8e-f5d8-5a63-b02a-e591598321e9
started_at: 2026-08-31T02:52:26+08:00
ended_at: 2026-08-31T02:56:27+08:00
result: PASS
fault_recovery_result: BLOCKED_PRECONDITION
negative_authority_result: BLOCKED_PRECONDITION
cleanup_result: BLOCKED_PRECONDITION
supersedes: none
---

# TOOL-ARTIFACT-SETTLEMENT-001 recovery admission precheck

这是 `3482b57a` 的 finding 级只读部署/消费预检，不是 supported recovery PASS，不进入 NPTCR，也不授权填写证据说明、点击恢复、重试旧 run 或清理合成资产。

## Input

- 目标是既有 D2 failure：EventPilot Session `b3962147-07cd-4223-8f23-f00193d7735c` / RuntimeTask `76a32f8e-f5d8-5a63-b02a-e591598321e9`。
- 本次没有发送任何新 prompt，没有创建 Session、RuntimeTask、command、input、artifact 或 workspace effect。
- 只执行 exact application commit 的三服务部署、公共健康读取、旧 Session hard reload 和管理员恢复队列只读检查。

## Authority

- 三服务上传来自 `git archive` 的同一 committed application SHA `3482b57a383d3c5bd33a5bcf813b87c6fab23339`；验收文档、证据和 owner 既有 dirty/untracked 路径均不在 archive commit 中。
- 产品读取复用已登录的实验账号与选中 tenant `rocky的实验室`；管理员页显示当前 principal `rocky243`，没有读取 cookie、local storage、credential 或密码。
- 没有 DDL、计费、凭据、权限、账号、业务数据、恢复状态或外部消息写入。

## Execution

- backend deployment `7c196980-34c6-4846-bf25-0397b7b55c0e`、backend-api `8e7545b8-9b6c-4b32-a77d-48883191728a`、frontend `6f6bd18c-1681-4049-ac20-6660a3f84fc3` 均为 `SUCCESS`，deployment message 均为 `deploy 3482b57a runtime tool-effect recovery`。
- 公共 backend `/api/health` 返回 `status=ok`、`runtime_control_bus.last_error=null`；frontend `/` 返回 HTTP 200。backend-api freshness 只由其 exact deployment status 证明。
- Session hard reload 后才读取 DOM，排除旧 frontend bundle/cache 造成的假阴性。

## Evidence

- 旧 Session hard reload 后显示 alert：`工具可能已经产生效果。管理员核对证据前，当前会话不会继续。`
- 原 generic `重试本轮` 按钮消失；`输入消息...`、输入区动作和发送均 disabled。run 仍如实显示 `失败`，0 running、0 waiting，没有自动 replay、没有新 prompt 或新 effect。
- 管理员 `运行时对账` 队列显示 50 pending，但旧 run `76a32f8e` 被提升为第一项：Agent `EventPilot`、type `web_chat_turn`、reason `tool_effect_outcome_unknown`、risk `effect_outcome_unknown`、status `failed`。
- 该行只暴露 `必填的效果证据说明` 与 `确认效果并停止旧任务`；证据为空时按钮 disabled。该行没有 `已处理`、`归档` 或 `重试`，证明未知 effect 不能走 generic terminal/retry action。
- 更早的 EventPilot run `ff9536bd` 同样作为第二个 unknown-effect hold 出现在普通 reconciliation rows 之前；old hold 没有被 50-row limit 饿死。

## Recovery

- 本次停在 action boundary 前，没有填写 evidence reason，也没有点击 `确认效果并停止旧任务`。
- 因而 invocation 的 unknown-effect hold、旧 RuntimeTask 状态和 Session admission blocker 均保持原状；没有伪造 `tool_result`、没有清除 recovery owner、没有创建新 run。
- 下一步必须取得新的 browser action-time confirmation，才可对指定 run 执行一次 operator acknowledgement，并在 reload/readback 中验证旧 run 不 replay、Session 才恢复 fresh-turn admission。

## Acceptance

- `3482b57a` 的 exact three-service deployment、普通 Session fail-closed consumption、管理员队列优先级、evidence-required action 和 generic retry suppression 在只读范围内 PASS。
- supported recovery effect 尚未执行，故 `fault_recovery_result=BLOCKED_PRECONDITION`；`TOOL-ARTIFACT-SETTLEMENT-001` 仍为 `Fix Candidate`。
- 本文件不是 P01-MAIN/PJ-02/PJ-04 pass，不生成 `*-pass-1.md` 或 `*-pass-2.md`；NPTCR 保持 `0/96`。

## Not proven

- Operator acknowledgement 的 canonical events、recovery-owner release、旧 run no-replay readback 与 fresh-turn release。
- Authority-negative、cleanup、完整 P01-MAIN/P02-STREAM 双遍、任何 frozen Journey PASS、Evidence Coverage、Zero Known Defects 或 Weekend RC verdict。

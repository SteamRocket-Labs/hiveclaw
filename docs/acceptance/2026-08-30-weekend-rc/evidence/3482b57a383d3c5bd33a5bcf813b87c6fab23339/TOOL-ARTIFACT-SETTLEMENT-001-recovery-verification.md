---
document_id: weekend-rc-2026-08-30-tool-artifact-settlement-recovery-verification
owner: Codex
status: completed
authority: immutable-production-finding-verification-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: deployed-supported-recovery-no-replay-pass
journey_id: P01-MAIN
pass: finding-recovery-verification
environment: production
source_commit: 3482b57a383d3c5bd33a5bcf813b87c6fab23339
deployed_commit: 3482b57a383d3c5bd33a5bcf813b87c6fab23339
manifest_sha256: d320edceeb26cf68fa724e77502d811e5476fa04ee3c9128075cc8c79eb38117
deployment_ids: backend=7c196980-34c6-4846-bf25-0397b7b55c0e; backend-api=8e7545b8-9b6c-4b32-a77d-48883191728a; frontend=6f6bd18c-1681-4049-ac20-6660a3f84fc3
persona_principal: authenticated lab platform-admin using EventPilot in the selected experimental tenant
target_session_id: b3962147-07cd-4223-8f23-f00193d7735c
target_runtime_task_id: 76a32f8e-f5d8-5a63-b02a-e591598321e9
fresh_recovery_runtime_task_id: f8cdd9ac-91bf-58d5-a7c2-34176ca87b74
started_at: 2026-08-31T02:52:26+08:00
ended_at: 2026-08-31T03:21:18+08:00
result: PASS
fault_recovery_result: PASS
negative_authority_result: NOT_RUN
cleanup_result: NOT_RUN
extends: TOOL-ARTIFACT-SETTLEMENT-001-recovery-admission-precheck.md
---

# TOOL-ARTIFACT-SETTLEMENT-001 supported recovery verification

这是 application commit `3482b57a` 对既有 unknown-effect `write_file` 的 production supported-recovery 证据。它把 finding 从 `Fix Candidate` 推进为 `Verified`，但不是完整 P01-MAIN/PJ-02/PJ-04 双遍，不进入 NPTCR。

## Input

- 恢复目标固定为 EventPilot Session `b3962147-07cd-4223-8f23-f00193d7735c` / RuntimeTask `76a32f8e-f5d8-5a63-b02a-e591598321e9` / invocation `1dcbdf47-50b8-5598-8980-60ea7ac6c35e`，没有操作队列中的 `ff9536bd…` 或其他任务。
- 管理员提交的 evidence reason 为：`已从 EventPilot 工作区只读核验现有合成文件 workspace/WEEKEND-RC-P01-MAIN-PASS-1.md：Marker=P01-MAIN-P1-CEDAR-734、TOTAL_MINUTES=90、RISK_ROWS=2；保留现有 effect，禁止重放旧 provider round。`
- acknowledgement 完成后，只发送一次 fresh-turn probe：`D4-RECOVERY-ADMISSION-3482-K9M7：只回复“D4_RECOVERY_OK”。禁止调用任何工具、禁止写文件、禁止创建产物、禁止重放或继续先前轮次。`

## Authority

- 恢复动作来自 signed-in platform-admin 的产品 `运行时对账` 入口，tenant、Agent、Session 与 RuntimeTask 均由服务端重新绑定；目标行只支持 `acknowledge_tool_effect`。
- acknowledgement reason 是既有实验 workspace 文件的只读核验证据；没有 credential、账号、角色、计费、DDL、跨 tenant、外部消息或直接 DB mutation。
- 后续机械取证在 Railway backend 内使用 `asyncpg` readonly transaction，先精确 `set_config` 当前 tenant，只执行显式 tenant/session SELECT，最终 rollback；没有 RLS 绕过。

## Execution

- pre-action reload 先证明 unknown-effect alert、composer disabled、generic retry suppressed；目标 `76a32f8e…` 位于管理员队列首项，evidence 为空时 action disabled。
- 填入上述 evidence reason 后，目标按钮 enabled；Codex 只点击一次 `确认效果并停止旧任务`。2.5 秒后目标行计数从 1 变为 0，没有点击或修改其他 reconciliation row。
- 服务端只追加 sequence `312 tool_call.reconciled` 与 `313 recovery_action.reconciled`。目标 invocation 保持 `effect_state=needs_reconciliation`、`result_event_id=null`，只把 `recovery_owner` 清为 null，并把 receipt 指向 sequence 312 对应 event。
- 原 RuntimeTask 保持原有 `failed` 终态、attempt 1 / claim version 1；metadata 变为 `reconciliation_status=tool_effect_acknowledged`、`needs_reconciliation=false`。系统没有恢复、retry 或改写旧 provider round。
- fresh-turn probe 建立新的 RuntimeTask `f8cdd9ac-91bf-58d5-a7c2-34176ca87b74`，不是旧 run replay；唯一 round 绑定唯一新 input `ad602cdc-5008-4c62-a059-1fcf02ea1963` 并正常 `completed`。

## Evidence

- reconciliation event IDs 为 `ea6a2c3d-3541-53cc-bd1c-3b04bc81c9b0` 与 `225b71bf-e6c0-5333-a037-545d021fe519`；对应 outbox 均为 `published`、attempts 1、`last_error=null`。
- 目标 invocation 的 `receipt_ref=session-event://ea6a2c3d-3541-53cc-bd1c-3b04bc81c9b0`、version 3；它仍没有任何 `tool_result.completed`。旧 Session 中既有其他成功 todo/progress results 不属于该 invocation，未被误计为恢复结果。
- acknowledgement 后、fresh input 前，该 Session 仍只有原 `failed` RuntimeTask，目标 path 的 ChatArtifact 仍为 0；证明恢复动作没有制造 artifact、compatibility owner 或隐藏 run。
- fresh input 是 canonical sequence `314 human_input.accepted`；model result 为 `round_committed` 且 `bound_input_ids=[ad602cdc…]`。sequence `375 assistant_text.snapshot` 逐字为 `D4_RECOVERY_OK`，sequence `385 assistant_final.completed`，sequence `386 run.completed`。
- 新 run 的 `SessionToolInvocation` 数为 0；Session 全部 tool counts 仍只属于旧 run：`write_file=1`、`track_todo=6`、`report_progress=1`。没有第二次 write、read、artifact 或旧轮工具调用。
- Workspace reload 后目标文件列表恰一项；打开后 heading `WEEKEND-RC P01 MAIN PASS 1`、`P01-MAIN-P1-CEDAR-734`、`TOTAL_MINUTES=90`、`RISK_ROWS=2` 在 DOM 中各恰一，证明 effect 被保留且未重复。

## Recovery

- acknowledgement 后旧 Session alert 消失，composer 恢复 enabled；没有自动新 run。fresh probe 由新的明确 user input 启动，并不使用旧 provider request、旧 run ID 或旧 invocation。
- signed-in hard reload 后同一 Session 显示唯一 prompt 与唯一 `D4_RECOVERY_OK`，unknown-effect blocker 为 0、running 0、waiting 0、Stop 0，composer enabled。
- 管理员页重新导航后目标 `76a32f8e` 行为 0、`.admin-reconcile-error` 为 0；旧 unknown effect 已退出 operational hold，但事实状态仍未被伪造为成功或失败 tool result。

## Consumption

- 普通 AgentDetail 用户可以继续在同一 Session 发起 fresh turn，并在 reload 后消费准确终答；恢复过程没有把 operator evidence、内部 receipt 或 raw payload注入普通终答。
- 普通 Workspace 仍能只读消费原文件，文件内容和唯一性在 acknowledgement、新 turn 与 reload 后保持稳定。

## Acceptance

- exact `3482b57a` 三服务部署、fail-closed admission、evidence-required operator action、canonical no-result reconciliation、no-replay、fresh-turn release、reload 与 no-duplicate effect 均在 production PASS。
- `TOOL-ARTIFACT-SETTLEMENT-001` 的已复现根因因此推进为 `Verified`。完整 P01-MAIN/PJ-02/PJ-04 双遍、authority-negative 与 cleanup 尚未执行，所以这些 Journey 仍不写 PASS，NPTCR 保持 `0/96`。

## Not proven

- 非 platform-admin 或错误 tenant 对该恢复动作的权限负向。
- D3 与其他 Weekend synthetic assets 的物理 cleanup。
- clean P01-MAIN pass-1/pass-2、P02-STREAM pass-1/pass-2、Evidence Coverage、五条护栏、Zero Known Defects 或 Weekend RC verdict。

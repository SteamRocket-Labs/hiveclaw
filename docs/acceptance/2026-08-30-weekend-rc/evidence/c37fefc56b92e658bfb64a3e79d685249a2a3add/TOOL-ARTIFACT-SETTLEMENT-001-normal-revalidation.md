---
document_id: weekend-rc-2026-08-30-tool-artifact-settlement-normal-revalidation
owner: Codex
status: active
authority: immutable-production-finding-revalidation-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: normal-settlement-reload-pass-recovery-pending
journey_id: P01-MAIN
pass: finding-normal-settlement-revalidation
environment: production
source_commit: c37fefc56b92e658bfb64a3e79d685249a2a3add
deployed_commit: c37fefc56b92e658bfb64a3e79d685249a2a3add
manifest_sha256: d320edceeb26cf68fa724e77502d811e5476fa04ee3c9128075cc8c79eb38117
deployment_ids: backend=62e4ef56-7e6b-456e-a505-fea90fd286a0; backend-api=307f0df7-6ae0-4c57-817e-f9ca07fd59fc; frontend=db6b605d-7b8b-40ea-8da8-247259db29f8
persona_principal: authenticated lab platform-admin using EventPilot in the selected experimental tenant
data_version: D3-SETTLEMENT-C37-8K4P
started_at: 2026-08-31T01:10:29.465284+08:00
ended_at: 2026-08-31T01:11:37.521668+08:00
result: PASS
fault_recovery_result: BLOCKED_PRECONDITION
negative_authority_result: BLOCKED_PRECONDITION
cleanup_result: BLOCKED_PRECONDITION
supersedes: none
---

# TOOL-ARTIFACT-SETTLEMENT-001 normal production revalidation

这是一份 finding 级别的 bounded normal-path PASS，不是 `P01-MAIN-pass-1.md`，不进入 NPTCR，也不把 `TOOL-ARTIFACT-SETTLEMENT-001` 提前升级为 `Verified`。

## Input

- owner 在 action time 明确确认发送后，Codex 从已认证的普通 AgentDetail 产品入口创建 fresh EventPilot Session，并只点击一次「发送」。
- Session `0731ec15-c662-4552-9500-3f68f1094f11`；RuntimeTask `c124e51f-c09e-5b0d-9265-38b48ae0db27`；selected provider/model 为 `zhipu/glm-5.3`。
- 输入把唯一允许的 effect 固定为一次 `write_file` 创建 `workspace/WEEKEND-RC-TOOL-SETTLEMENT-C37-8K4P.md`，成功后一次 `read_file` 原路径读回；禁止其他工具、workflow、trigger、delegation、外部消息、credential 读取、其他路径和删除，write/settlement 失败时禁止重试 write。
- canonical `human_input.accepted` 为 sequence `1`，`human_input.bound` 为 sequence `13`；round 1 的 `bound_input_count=1`，后两轮均为 0，证明当前 durable input 只绑定一次。

## Authority

- 目标仅为选中实验 tenant 内 EventPilot Agent-owned `workspace/` 路径。artifact row 的 tenant、Agent、root Session、conversation Session 和 owner user 绑定均为 true，`authority_state=owned`。
- canonical invocation 与 span 只有一个 `write_file` 和一个 `read_file`；没有其他 tool span、approval、delegation、workflow 或外部消息。
- 没有 credential、账号、计费、DDL、权限或生产业务数据修改。只读机械取证在 Railway backend 内使用 `asyncpg` readonly transaction，并以当前 tenant `set_config` 后只执行显式 tenant/session SELECT；事务末尾 rollback，无 RLS 绕过。
- authority-negative 场景本次未执行，保持 `BLOCKED_PRECONDITION`。

## Execution

- RuntimeTask 为 `web_chat_turn/completed`，`attempt_count=1`、`claim_version=2`；三个 `llm.stream` span 均 `status=ok`。
- write invocation `dee92555-4588-5486-90d3-9310f5377b68`：sequence `121 tool_call.started` → `122 tool_call.progress(effect_started)` → `123 tool_call.completed(success)` → `124 tool_result.completed(success)`。
- write 的下一模型轮直到 sequence `127 result_commit.prepared` 才建立，因此 effect terminal pair 在后续 provider round 之前完成。
- read invocation `c509a552-d7ba-5cac-a48b-0b836f1134b4`：sequence `167 started` → `168 effect_started` → `169 tool_call.completed(success)` → `170 tool_result.completed(success)`；下一轮直到 sequence `173 result_commit.prepared` 才建立。
- 两个 invocation 均为 `effect_state=effect_committed`、`permission_state=not_required`、`recovery_owner=null`，没有自动 retry。
- 最终 sequence `205 assistant_final.completed`、`206 run.completed`、`207 turn.completed`、`208 run_outcome.terminal_committed` 全部 `projection_status=projected`。

## Evidence

- write 的 canonical `tool_call.completed` 与 `tool_result.completed` 共用非空 message ID `07afe8cd-ff96-5c03-b0f1-e54ca9c12462`。
- deterministic ChatMessage owner 恰一行；目标 ChatArtifact `be17c252-8a97-4782-ae3e-17e05d2f3519` 恰一行，并绑定同一 message、run、Session 与 path。
- write terminal outbox：sequence `123` outbox `6c37bb02-3fa8-4003-96f6-a143d3a3c8c9`，sequence `124` outbox `a1244c48-4573-4400-bd45-22df5c573659`；均为 `published`、`attempts=1`、`last_error=null`。tool-result envelope 与 event 的 message ID 一致，parts 恰为 1。
- read terminal outbox sequence `169/170` 同样为 `published`、`attempts=1`、`last_error=null`，无 artifact part，符合 read-only tool 预期。
- canonical read tool-result event `24dabf4f-8d17-4129-bc13-23de8da8d7ec` 的 provider-visible wrapper 为 529 B；只读查询机械比较得到 `contains_expected=true`，完整包含期望的 77 B 三行正文。wrapper 还含工具回执字段，因此不要求 wrapper 本身与原文件逐字相等。
- 计数：RuntimeTask 1；invocation 2；write 1；read 1；`tool_call.completed` 2；`tool_result.completed` 2；目标 artifact 1；目标 message owner 1；invocation/event/RuntimeTask reconciliation 均 0。
- 保存快照为 77 B，正文是三行且无尾随换行：

  ```text
  # D3 Tool Settlement Probe
  MARKER=D3-SETTLEMENT-C37-8K4P
  EXPECTED_ARTIFACTS=1
  ```

  snapshot `content_hash` 与独立计算的期望 SHA-256 都是 `2c3f309736338d6185614a50e56875de7fc1092cd239c765b7df1661f7ec07e6`；artifact revision/snapshot hash 为 `ecdbddee2d6deda2cf1e8b7b33300779caad56294052de6a114fb0835a632ce1`。
- 探针窗口 `2026-08-30T17:10:00Z–17:12:30Z` 的 exact backend/backend-api deployment logs 对 `ForeignKeyViolationError`、`ToolLifecyclePersistenceError`、`needs_reconciliation`、`tool_lifecycle_persistence_failed`、`chat_artifacts_message_id_fkey` 过滤均为 0。日志只作反证辅助，不替代 DB/event/UI path proof。

## Recovery

- 运行终态后执行一次 hard reload。持久会话历史加载完成后，仍是同一 Session、同一 RuntimeTask、同一 final、一个 write invocation、一个 read invocation、一个 artifact；0 running、0 waiting，没有自动 replay 或第二次 write。
- reload 后 canonical sequence、message ID、artifact ID 和 outbox identity 未变化；普通 UI 没有新增 duplicate tool card、artifact 或 final。
- 旧 D2 failure 在 exact c37 部署下也保持 typed `失败`、0 running、0 waiting、0 artifacts，不会因 reload 自动重放不确定 effect。
- 本证据没有执行受支持的 retry/recovery 动作或 production fault injection。该动作可能重新触发受治理 effect，必须另取 owner action-time confirmation；因此 `fault_recovery_result=BLOCKED_PRECONDITION`。

## Consumption

- no-reload 终态显示 `完成`、精确 final `D3_TOOL_SETTLEMENT_OK marker=D3-SETTLEMENT-C37-8K4P path=workspace/WEEKEND-RC-TOOL-SETTLEMENT-C37-8K4P.md readback=exact`、一个 77 B Markdown 文件、一个 Session artifact、0 running、0 waiting。
- 展开过程只出现一个 write tool row 和一个 read tool row；同一 canonical artifact 作为消息附件和右栏 Session delivery surface 被消费，DB 仍只有一个 artifact，不是 duplicate effect。
- 普通用户点击「打开」后看到“正在预览会话保存快照”，heading `D3 Tool Settlement Probe` 与两个 marker 字段均正确；没有使用 admin console 或 DB mutation 补状态。

## Acceptance

- bounded verdict：Input、Authority、Execution、Evidence、normal reload Recovery、Consumption 与 exact-deployment Acceptance 在本 probe 范围内 PASS。
- `TOOL-ARTIFACT-SETTLEMENT-001` 的生产 normal path 已证明 c37 修复真实接线；但 finding 关闭合同还要求 supported failure recovery、无重复 effect、authority negative 与 cleanup，因此 finding 仍为 `Fix Candidate`。
- 本文件不是完整 P01-MAIN/PJ-02/PJ-04 pass，未创建任何 `*-pass-1.md` 或 `*-pass-2.md`；NPTCR 保持 `0/96`。

## Cleanup

- synthetic Session、RuntimeTask、artifact 与 `workspace/WEEKEND-RC-TOOL-SETTLEMENT-C37-8K4P.md` 当前作为 finding evidence 保留。
- 当前受支持的 `delete_file` / 文件删除入口会物理 unlink，且由 `workspace.command.destructive_delete` / `destructive_once` 治理；没有已验证的 move-to-trash/rename 恢复入口。
- 本次发送确认不授权删除。cleanup 必须在 evidence/recovery 完成后另取 owner action-time confirmation；当前 `cleanup_result=BLOCKED_PRECONDITION`。

## Not proven

- Production supported failure retry/recovery、重启或中断后的无重复 effect、authority-negative、cleanup。
- 完整 P01-MAIN/P02-STREAM 双遍、任何 frozen Journey PASS、Evidence Coverage、Zero Known Defects 或 Weekend RC release verdict。
- 与本探针无关的 Memory availability、MiniMax/DeepSeek provider 路径、Hive Connect 或其他产品域。

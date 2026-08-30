---
document_id: weekend-rc-2026-08-30-p01-main-blocked-persona-platform-admin
owner: Codex
status: active
authority: immutable-production-precondition-evidence-not-nptcr-pass
last_reviewed: 2026-08-31
verification_status: functional-path-pass-required-employee-principal-not-proven
journey_id: P01-MAIN
pass: invalid-persona-probe
environment: production
source_commit: 3482b57a383d3c5bd33a5bcf813b87c6fab23339
deployed_commit: 3482b57a383d3c5bd33a5bcf813b87c6fab23339
manifest_sha256: d320edceeb26cf68fa724e77502d811e5476fa04ee3c9128075cc8c79eb38117
deployment_ids: backend=7c196980-34c6-4846-bf25-0397b7b55c0e; backend-api=8e7545b8-9b6c-4b32-a77d-48883191728a; frontend=6f6bd18c-1681-4049-ac20-6660a3f84fc3
persona_principal: authenticated lab platform_admin using the employee AgentDetail surface; frozen employee principal not proven
data_version: P01-MAIN-PASS1-3482B-MAPLE-581
started_at: 2026-08-31T03:34:29.012246+08:00
ended_at: 2026-08-31T03:54:24+08:00
result: BLOCKED_PRECONDITION
fault_recovery_result: BLOCKED_PRECONDITION
negative_authority_result: BLOCKED_PRECONDITION
cleanup_result: BLOCKED_PRECONDITION
supersedes: none
---

# P01-MAIN production path check blocked by principal mismatch

这次运行证明了当前 exact commit 上的开放任务、模型、工具结算、artifact、reload 与普通 UI 消费路径，但不能写成 `P01-MAIN-pass-1.md`：冻结 manifest 要求 `persona=employee`，当前唯一复用的 signed-in 浏览器主体经 server-side DB 绑定核验为 `platform_admin`。本文件不进入 NPTCR，正式 pass-1 仍为空。

## Input

- Codex 从 EventPilot 侧栏的 `New conversation with EventPilot` 按钮进入真正的无 `session_id` draft；发送前 URL 为 `/agents/03d43a5c-0d5c-4c30-bab9-2734c5691434#chat`，旧 prompt、running、waiting 均为 0。
- 唯一发送创建 fresh Session `52ddde7f-63bf-44a6-973f-ffb1da06d14a` 与 RuntimeTask `38381d84-779d-59fe-954d-dd75b2c07079`；输入 marker 为 `P01-MAIN-PASS1-3482B-MAPLE-581`，唯一允许的业务 artifact path 为 `workspace/WEEKEND-RC-P01-MAIN-PASS-1-3482B-MAPLE.md`。
- 输入要求模型先公开三步计划，建立并更新至少三个 Work Ledger todos，只写一次目标文件、结算成功后只读回一次，并以七项外部硬标准核验内容；write 失败、unknown 或 unresolved 时停止且禁止重试。外部消息、其他 Agent、web、workflow、trigger、其他路径与删除均禁止。
- SessionTurnInput `417244b6-45b1-4b9e-9f8f-f7eb4990d3ce` 为 `start_turn/applied`，只绑定 run `38381d84…` 的 round 1；后续五轮没有新 input。运行及两次 hard reload 后 DB 仍是一个 input、一个 run。

## Authority

- server-side principal `42778d4b…` 为 active `platform_admin`，tenant 精确绑定 `aac728fb-fe1c-45df-a2ff-a56e024a37a0`；EventPilot 具有同一 user scope 的 `manage` grant。
- ChatSession 的 tenant、Agent、user、`source_channel=web`、`session_kind=human_chat`、`actor_type=user`、`runtime_source=web_chat`、`visibility_scope=direct_user` 和 `listed_surface=chat` 均由服务端持久化。
- 冻结 P01-MAIN 要求 `profile=employee_session`、`persona=employee`。`platform_admin` 即使使用同一 employee-facing route，也不能证明普通 employee 的 DOM、能力或授权边界；更不能代替后续 denied-effect negative authority。
- 没有读取或更改 credential，没有登录、创建账号、切换身份、变更 grant 或提升权限。当前无第二个已登录 browser identity，因此正式 employee pass 停在 login/principal precondition。
- 生产机械取证均在 Railway backend 内使用 `asyncpg` readonly transaction，先设置当前 tenant，再执行显式 tenant/session SELECT，最后 rollback；无 DDL、DB 写或 RLS 绕过。

## Execution

- model route sequence `11` 选择 `zhipu/glm-5.3`（label `GLM-5.3`，reason `primary_model`）；Agent primary model enabled，input window `1,000,000`、output ceiling `32,768`，runtime `max_tool_rounds=200`。
- round 1 sealed snapshot 含唯一 bound input、3 条 provider messages 与完整 73-tool surface；其中包含 `track_todo`、`write_file`、`read_file` 以及 Memory、Skill、Workflow、sub-agent、A2A、shell/web 等受治理工具，没有基于 prompt 文本缩减工具。
- sequence `399 assistant_text.snapshot` 已公开三步计划，sequence `405` 才提交 round 1，首个工具直到 sequence `407` 才开始；计划先于工具效果。
- canonical invocation 恰为 `track_todo=7`、`write_file=1`、`read_file=1`。write invocation `164a096b…` 为 sequence `501 started → 502 progress → 503 completed → 504 tool_result.completed`；read invocation `6aee0fbf…` 为 `521 → 522 → 523 → 524`。二者均 `effect_committed`、`permission_state=not_required`、`result_event_id` 非空、`recovery_owner=null`。
- sequence `648` 是最终 assistant snapshot，`657 assistant_final.completed`、`658 run.completed`、`659 turn.completed`；RuntimeTask `completed`、`attempt_count=1`，没有 retry、second run 或 active task。
- `memory.context.resolve` 单独记录 typed `degraded/memory_semantic_selection_unavailable`，明确 no recalled bodies prefetched、conversation 可继续且 `search_memory/load_memory` 仍可用。P01 输入不依赖 Memory，当前开放任务功能路径不因该状态失败；Memory 专项 journeys 仍需独立验证，不能由本证据外推。

## Evidence

- 三个 model-authored todo 均持久化为 `completed`；ledger view 为 `todos_total=3`、`todos_complete=3`、`todos_open=0`、`verification_pending=0`、`failures_open=0`。底层 ledger container 仍显示 `status=running/current_phase=planning`，本证据如实保留该观察，不将它解释为 P04-LGR 已通过。
- 只有一个 owned ChatArtifact：`a8a036af-5268-4a79-b5ae-822f66544d00`，同一 Session/run/path，size `1257` B、MIME `text/markdown`、preview `markdown`。content hash `82fa30a498812a96340803bc93ae3605695b73df28788bf4cff3d6f7ecfc982c`，revision/snapshot hash `be2ab61874b309383146ca2af6960e8c59429d133e92cac90d18244b1946a442`，delivery preview 未截断。
- 保存快照逐项满足硬标准：首行恰为 `# WEEKEND-RC P01 MAIN PASS 1`；marker、`TOTAL_MINUTES=90`、`RISK_ROWS=2` 各一条独立行；agenda 恰三条指定 data row；risk 恰两条指定 data row；checklist 明确包含 Owner、Timing、Fallback、Final handoff。
- 9 个 invocation 均有 matching `tool_result.completed`。660 个 Session outbox 全为 `published`、attempts 恰 1、`last_error` 计数 0；unresolved/reconciliation tool 计数 0。
- canonical spans：`agent_kernel.handle=ok` 一次，`llm.stream=ok` 六次，`track_todo=ok` 七次，`write_file=ok` 一次，`read_file=ok` 一次；除已披露的 typed Memory degradation 外零 tool/generation error。
- accepted `2026-08-30T19:34:29.012246Z`；首 text delta `129007 ms`，首完整 text snapshot `131343 ms`，首工具 `131942 ms`，terminal `201987 ms`。UI 显示 `已处理 3m 22s / 13 个步骤`。一次单独计时的 hard reload 在 `2920 ms` 内收敛。

## Recovery

- 未刷新终态先显示同一 final、一个文件、3/3 todos、0 running/waiting/Stop。
- 两次 hard reload 后仍为同一 Session URL；第二次计时 reload 在 2.920 s 内恢复 `完成`、精确 final、一个 file card、3 个任务已完成，0 running、0 waiting、0 Stop。
- 第二次 reload 后 canonical 计数仍为 input 1、run 1、tool 9、artifact 1、max sequence `660`、active run 0；没有 replay、duplicate effect、duplicate final 或新 event。
- 本次没有执行 disconnect/worker restart fault，也没有执行 denied effect；两者留给正式 employee pass-2/fault evidence。

## Consumption

- employee AgentDetail route 在 no-reload 与 hard reload 后均显示 `完成 · GLM-5.3`、模型 final、`待办 3 个任务已完成`、一个 Markdown 文件卡和 `打开/下载`。
- 点击一次 `打开` 后出现“正在预览会话保存快照”，用户可读到 heading、两张精确表格、两个计数字段和完整 execution checklist；不是通过 admin console 或 DB mutation 补状态。
- 当前截图只存在于本次受控 Browser tool capture，没有生成可验证的仓库 screenshot artifact；本证据以 DOM/UI observation 与 canonical DB/event/artifact snapshot 交叉，不虚构 screenshot path。

## Acceptance

- 功能路径本身满足 P01 的开放任务、模型/工具 loop、外部硬标准、artifact、reload 与 user consumption；完整 73-tool bundle 与实际 `zhipu/glm-5.3` route 也已证明。
- 正式 verdict 仍为 `BLOCKED_PRECONDITION`，唯一原因是冻结 persona 与实际 server principal 不一致。它不是应用代码 finding，也不授权创建 member、改角色、读取密码或重新登录。
- 不创建 `P01-MAIN-pass-1.md`，Journey Ledger 的 Pass 1 保持 `—`，NPTCR 保持 `0/96`。

## Cleanup

- marker Session、run、artifact、Work Ledger 与 `workspace/WEEKEND-RC-P01-MAIN-PASS-1-3482B-MAPLE.md` 作为 precondition evidence 保留；另一个 LARCH 错入口资产和 D3 资产也仍在登记表中。
- 本次未授权 destructive file unlink 或 Session delete。最终 cleanup 必须走受支持产品路径并独立记录；当前 `cleanup_result=BLOCKED_PRECONDITION`。

## Not proven

- 真实 `member/employee` principal 的 P01 pass-1/pass-2、fault/recovery、negative authority 与 cleanup。
- P02 streaming 旅程、Memory 可用性、Work Ledger container terminal semantics、MiniMax/DeepSeek、其他 95 条 frozen journeys、Evidence Coverage、Zero Known Defects 或 Weekend RC release verdict。

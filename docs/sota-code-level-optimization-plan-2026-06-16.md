# Hive SOTA 代码级优化闭环计划（2026-06-16）

> 基线：接受 `docs/sota-atomic-system-audit-2026-06-16.md` 作为本轮审计结论基础。本文不重新裁决 Hive 是否整体达成 SOTA，而是把审计发现转化为当前代码层面可以直接推进的优化清单。
>
> 口径：只列可以通过代码、测试、配置模板、CI、观测面闭环的事项。纯生产操作、真实 secrets 配置、人工 benchmark 执行不单独算代码优化，但可以作为某项代码改造的验收后置条件。

## 0. 总体判断

当前最值得做的不是继续扩展新能力，而是把已经暴露的 dark/provisional 面收成可运行、可观测、可回归的主路径：

1. 先修会造成错误行为或安全假象的项：mutating subagent restart replay、Teammate Mailbox 写读裂脑、RLS runtime role assertion、artifact execution gate dark。
2. 再补让 SOTA 判定从 provisional 变成可量化的项：真实 behavior eval runner/baseline updater、sandbox probe scheduler、promotion regression report。
3. 最后补纵深和观测：connector ACL 权威化、memory lifecycle writers、needs_reconciliation 消费面、cache/Plan/interop 观测。

## 1. P0 必须优先修的代码问题

### P0-1. Mutating subagent restart replay 回退到 fail-closed，或做成真正 idempotent

**审计基础**

- `start_subagent_run()` 对非 replay-safe 的 mutating worker 也写入 `spawn_intent_recorded` journal。
- `resume_persisted_subagent_runs()` 只要看到 replay contract + `spawn_intent_recorded` 就会重跑 mutating worker。
- 结果是正常 crash/resume 路径不会进入 `needs_reconciliation`，有重复副作用风险。

**建议改造**

- 短期正确修法：mutating subagent 默认不可自动 replay，restart 后进入 `needs_reconciliation`。
- `has_mutating_restart_replay_journal()` 不应把 `spawn_intent_recorded` 视为足够恢复凭证；只有未来具备 per-effect idempotency/completion journal 时才能自动 resume。
- `start_subagent_run()` 对 `worker` 等 mutating 类型设置 `resume_after_restart=false` 或保留 `resume_after_restart=true` 但 resume 分支强制 reconciliation。
- 保留 `explorer` / `critic` 等 read-only 类型自动 resume。

**主要文件**

- `backend/app/services/subagent_run_service.py`
- `backend/app/services/runtime_task_service.py`
- `backend/tests/services/test_subagent_run_service.py`
- `backend/tests/services/test_runtime_task_service.py`

**TDD Red**

新增或改写测试：

```bash
cd backend
source .venv/bin/activate
pytest tests/services/test_subagent_run_service.py::test_resume_persisted_subagent_runs_rehydrates_mutating_worker_with_replay_contract -q
```

期望先失败并改为新的断言：mutating worker 即使有 `spawn_intent_recorded`，也不得调用 `spawn_subagent()`，必须写 `status="needs_reconciliation"`。

**验收**

```bash
cd backend
source .venv/bin/activate
pytest tests/services/test_subagent_run_service.py tests/services/test_runtime_task_service.py tests/agents/test_orchestrator.py -q
```

通过后，G4/G7 的正确表述应从“换皮恶化”恢复到“mutating lane fail-closed live, exactly-once still pending”。

### P0-2. Teammate Mailbox 消除默认写内存、读 Postgres 的裂脑

**审计基础**

- `COORDINATION_BACKEND` 默认是 `memory`。
- `gateway_scope()` 默认写 `InProcessCoordinationGateway`。
- `build_prompt_facing_team_context()` 固定从 `CoordinationSignal` / `RuntimeTask` Postgres 表读取。
- 默认部署下 signal 写读不同源，Mailbox 静默恒空。

**建议改造**

- 默认 `COORDINATION_BACKEND` 改为 `postgres`，`memory` 只作为显式 dev/test override。
- 当配置为 `postgres` 但缺 tenant_id 时，不再静默降级为 memory；返回空上下文并记录 warning/audit span，或在需要 durable mailbox 的路径 fail-closed。
- `ToolRuntimeService`、`orchestrator`、`subagent`、`workflow_runtime_service` 的 signal 写入路径必须都带 tenant_id。
- 增加端到端测试：发送 signal 后，下一轮 `build_prompt_facing_team_context()` 能读到同一条 mailbox signal。

**主要文件**

- `backend/app/config.py`
- `backend/app/agents/coordination_wiring.py`
- `backend/app/services/agent_team_context.py`
- `backend/app/services/workflow_runtime_service.py`
- `backend/app/agents/orchestrator.py`
- `backend/app/agents/subagent.py`
- `backend/tests/agents/test_coordination_wiring.py`
- `backend/tests/services/test_agent_team_context.py`（若不存在则新增）

**TDD Red**

新增测试：

```bash
cd backend
source .venv/bin/activate
pytest tests/services/test_agent_team_context.py tests/agents/test_coordination_wiring.py -q
```

Red 断言：默认配置下，经 `gateway_scope(tenant_id=...)` 写入的 `CoordinationSignal` 必须被 `build_prompt_facing_team_context()` 读出。

**验收**

G13 的 Teammate Mailbox 从 dark 改为 code-level live；仍需后续 live multi-agent eval 证明质量。

### P0-3. RLS runtime role assertion，避免生产 superuser/bypassrls 连接假安全

**审计基础**

- stage-3 scaffolding 存在：`SCHEMA_DATABASE_URL`、`grant_rls_app_role.py`、entrypoint Step 2.6。
- 默认 `SCHEMA_DATABASE_URL=None`，pre-cutover 复用 `DATABASE_URL`。
- 当前没有启动时断言 runtime DB role 不是 superuser / bypassrls。
- `FORCE ROW LEVEL SECURITY` 对 superuser 不生效。

**建议改造**

- 新增 startup preflight：查询 `current_user`、`pg_roles.rolsuper`、`pg_roles.rolbypassrls`。
- 增加配置：
  - `RLS_RUNTIME_ROLE_ENFORCEMENT=off|warn|strict`
  - production 默认 `strict`，dev/test 默认 `warn` 或 `off`。
- strict 模式下，如果 app engine 当前 role 是 superuser 或 bypassrls，启动失败。
- `/api/health` 增加非敏感 RLS role health component：`runtime_role_checked=true`、`superuser=false`、`bypassrls=false`。

**主要文件**

- `backend/app/config.py`
- `backend/app/main.py`
- `backend/app/database.py`
- `backend/app/db_bootstrap.py` 或新增 `backend/app/services/rls_runtime_guard.py`
- `backend/tests/api/test_rls_bypass_audit.py`
- `backend/tests/services/test_rls_runtime_guard.py`（新增）

**TDD Red**

```bash
cd backend
source .venv/bin/activate
pytest tests/services/test_rls_runtime_guard.py tests/api/test_rls_bypass_audit.py -q
```

Red 断言：mock 查询返回 `rolsuper=true` 或 `rolbypassrls=true` 时，strict 模式必须 raise；warn 模式必须记录 warning 并继续。

**验收**

G9 从“stage-3 scaffolding exists but default unproven”推进到“runtime fail-closed guard exists”；真实生产 cutover 仍需单独运维执行。

### P0-4. 将 artifact execution gate 接入 promotion / CI 主路径

**审计基础**

- `run_artifact_execution_gate()` 存在且有测试。
- `run_adversarial_suite()` 也存在。
- 但当前非测试主路径没有在 skill promotion 或 CI gate 中真正调用 artifact execution gate。

**建议改造**

- 对会产生代码、脚本、工作流定义、skill executable artifact 的 candidate，promotion 前必须运行 artifact gate。
- `decide_behavior_gated_promotion()` 输入增加 `artifact_gate_report`。
- gate 状态分三类：
  - `passed`：允许进入 behavior gate。
  - `not_applicable`：候选无 executable artifact，但必须记录原因。
  - `failed` / `missing_required`：promotion hold。
- CI 增加 adversarial suite entry，避免只在文档/测试里存在。

**主要文件**

- `backend/app/services/skill_distiller.py`
- `backend/app/evals/artifact_gate.py`
- `backend/app/evals/adversarial_suite.py`
- `backend/app/evals/ci_gate.py`
- `.github/workflows/*`（若现有 CI workflow 可接入）
- `backend/tests/services/test_skill_distiller.py`
- `backend/tests/evals/test_ci_gate.py`
- `backend/tests/evals/test_adversarial_suite.py`

**TDD Red**

```bash
cd backend
source .venv/bin/activate
pytest tests/services/test_skill_distiller.py::test_distiller_cannot_promote_without_external_behavior_eval tests/evals/test_ci_gate.py tests/evals/test_adversarial_suite.py -q
```

新增 Red：代码型 candidate 在 artifact gate 缺失或 failed 时必须 hold，即使 behavior report passing。

**验收**

G2 的 “artifact_gate dark” 可降级为 “promotion/CI live, production score pending”。

## 2. P1 直接提升 SOTA 可信度的代码优化

### P1-1. 真实 behavior eval runner 与 baseline updater

**问题**

当前最大阻断不是缺 gate，而是没有真实分数进入 `core_behavior_v1.json`。

**建议改造**

- 新增可重复 CLI：
  - `python -m app.evals.run_sota_behavior_eval --target hive --output ...`
  - `python -m app.evals.run_sota_behavior_eval --target hermes --output ...`
  - `python -m app.evals.update_behavior_baseline --report ... --commit-sha ...`
- CLI 必须支持 fixture/fake runner 测试模式和 live 模式。
- live 模式不允许 fallback，不允许写 provisional baseline。
- baseline updater 必须校验 `behavior_eval_passed(report)`，否则拒绝写入。

**主要文件**

- `backend/app/evals/hive_live_runner.py`
- `backend/app/evals/baseline.py`
- `backend/app/evals/hermes_baseline.py`
- 新增 `backend/app/evals/run_sota_behavior_eval.py`
- 新增 `backend/app/evals/update_behavior_baseline.py`
- `backend/tests/evals/test_hive_live_runner.py`
- 新增 `backend/tests/evals/test_behavior_baseline_update.py`

**验收**

```bash
cd backend
source .venv/bin/activate
pytest tests/evals/test_hive_live_runner.py tests/evals/test_behavior_baseline_update.py tests/evals/test_ci_gate.py -q
```

这项完成后仍不等于 SOTA 达成，但会把“怎么产生真实分数”从人工流程变成代码路径。

### P1-2. promotion regression report 不得跳过

**问题**

审计指出 distiller 路径可能没有把 regression report 明确传入 promotion decision，导致 E1 回归门在部分路径弱化。

**建议改造**

- `ensure_skill_distiller_behavior_report()` 返回的 report 必须被包装成明确 regression report。
- `decide_behavior_gated_promotion()` 对 regression report 缺失 fail-closed，除非 candidate 明确标记为非行为变更且 artifact gate 为 `not_applicable`。
- promotion ledger 记录 behavior report id、scenario scores、artifact gate report id、regression decision。

**主要文件**

- `backend/app/services/skill_distiller.py`
- `backend/app/services/tenant_behavior_eval_publisher.py`
- `backend/app/services/evolution_verification.py`
- `backend/tests/services/test_skill_distiller.py`
- `backend/tests/services/test_tenant_behavior_eval_publisher.py`

**验收**

promotion 不再只依赖 passing report 注入 runtime config，而是有完整 candidate -> behavior eval -> regression decision -> promotion/hold 审计链。

### P1-3. Memory lifecycle 三个 hold writer 接入生产写侧，或删除恒 0 报告字段

**问题**

`create_sketch()`、`record_conflict()`、`mark_reference_revalidation_required()` 有 store/test，但生产 writer 不足，maintenance 报告可能恒 0。

**建议改造**

- memory extraction 对低置信、待验证条目写 `create_sketch()`。
- dream / consolidation 检测到互斥事实时写 `record_conflict()`。
- source-backed memory 的外部引用过期、拉取失败、权限失效时写 `mark_reference_revalidation_required()`。
- 如果某类 writer 暂时不接，maintenance 报告不要把该 count 表述为 live signal。

**主要文件**

- `backend/app/memory/lifecycle_store.py`
- `backend/app/memory/lifecycle_maintenance.py`
- `backend/app/memory/t2_store.py`
- `backend/app/memory/retriever.py`
- `backend/app/services/extract_agent.py`
- `backend/app/services/auto_dream.py`
- `backend/tests/memory/test_lifecycle_maintenance.py`
- `backend/tests/memory/test_retrieval_pipeline.py`

**验收**

新增生产路径测试覆盖三类 writer，并证明 heartbeat maintenance 可以从真实 writer 输出非零 hold/discard report。

### P1-4. Connector ACL 从自指 agent ACL 升级为权威 document ACL

**问题**

Feishu/Drive 当前 source item 主要写 `agent_id`，Office 主要是 tenant scope；这不是 Glean 式 per-document ACL。

**建议改造**

- Feishu doc/drive read 成功后，调用 Feishu collaborator/sharing API 拉取真实可访问主体。
- ACL metadata 写入 `user_ids`、`department_ids`、`group_ids` 或 `tenant_ids + scope`，不能只写当前 `agent_id`。
- 未能拉取权威 ACL 时，source item 标记 `deny_by_default` 或 `acl_authority="connector_unverified"`，禁止作为 passing 权限证据。
- generated-output check 不只匹配 source URI，还记录 content digest/snippet signature，至少阻断明显转述泄漏。

**主要文件**

- `backend/app/services/connector_acl.py`
- `backend/app/services/agent_tool_domains/feishu_docs.py`
- `backend/app/services/agent_tool_domains/feishu_drive.py`
- `backend/app/tools/handlers/office.py`
- `backend/tests/services/test_connector_acl.py`
- `backend/tests/kernel/test_generated_source_acl.py`

**验收**

构造 A 用户可读、B 用户不可读的同一 connector source：A 进 prompt，B 被 filter；B final draft 引用 source URI 或受保护 snippet 均被 block。

### P1-5. `needs_reconciliation` 增加可消费的 admin/API/UI 面

**问题**

当前多个 runtime path 会写 `needs_reconciliation`，但缺少统一列表、处理、关闭、重试入口。

**建议改造**

- 增加 admin/runtime API：
  - list reconciliation tasks
  - get detail
  - mark resolved
  - retry when safe
  - kill/archive
- Frontend admin/agent detail 暴露 reconciliation 队列。
- 每次写入 `needs_reconciliation` 记录 audit span。

**主要文件**

- `backend/app/api/admin.py` 或新增 `backend/app/api/runtime_tasks.py`
- `backend/app/services/runtime_task_service.py`
- `frontend/src/pages/agent-detail/*`
- `frontend/src/api/domains/*`
- `backend/tests/api/test_runtime_task_reconciliation.py`
- `frontend/src/pages/agent-detail/*.test.tsx`

**验收**

有 UI/API 可看到 mutating restart-orphan；不会再出现 write-only dead-letter。

### P1-6. Sandbox probe scheduler 与趋势持久化

**问题**

`code_execution/probe.py` 提供 probe，但没有周期调度和生产趋势闭环。

**建议改造**

- heartbeat 或独立 daemon 周期运行 sandbox probe。
- 持久化最近 N 次 probe evidence，写入 system setting 或 dedicated table。
- `/api/health` 暴露最近 probe status、provider、age、network_denied、workspace_round_trip。
- Railway production 使用 `HIVE_CODE_EXEC_PROVIDER=vercel_sandbox` 时，probe 必须验证 microVM provider，不允许 fallback raw subprocess。

**主要文件**

- `backend/app/services/code_execution/probe.py`
- `backend/app/scripts/probe_code_execution_sandbox.py`
- `backend/app/services/heartbeat.py`
- `backend/app/api/system.py` 或 health path
- `backend/tests/services/test_code_execution_probe.py`

**验收**

G11 从 “probe exists but not scheduled” 推进到 “scheduled evidence path exists”。

## 3. P2 重要但不应阻塞 P0/P1 的优化

### P2-1. 清理 `policy_replay` 文档漂移，或恢复 replay guard

选择二选一：

- 如果 replay guard 仍是设计目标：恢复 `memory/policy_replay.py` / replay corpus，并接入 activation policy change path。
- 如果已退役：从 `CLAUDE.md` 和相关 docs 删除“activation policy changes must pass replay guard”的 live 不变量。

最低成本先做文档修正；更强方案是恢复 replay guard。

### P2-2. CODEOWNERS / evaluator trust root

新增 `.github/CODEOWNERS`，至少覆盖：

- `backend/app/evals/**`
- `backend/app/services/evolution_verification.py`
- `backend/app/evals/baselines/**`
- `.github/workflows/**`

再让 `evaluator_integrity` / CI 对 CODEOWNERS 缺失 fail。

### P2-3. Plan Mode decision_trace

Plan Mode 当前机制接近完成，但缺少语义阶段 trace。建议把以下事件写入 `decision_trace` / invocation spans：

- enter plan mode
- clarification requested
- plan proposed
- approval hash generated
- execute approved
- rejected / expired / hash mismatch

### P2-4. Cache metrics 持久化

当前 cache hit 主要是 runtime snapshot/in-memory 视角。建议把 prompt-cache anchor、provider cache read/write、compaction trigger、CJK estimate error 写入 PG metrics 或 invocation spans，支持按 agent/model/provider 聚合。

### P2-5. MCP OAuth resource-server 诚实升级

当前 `not_exposed` 是诚实状态。下一步代码优化是补齐 RFC 9728 / RFC 8707 resource metadata、WWW-Authenticate challenge、resource indicators，并加真实 MCP OAuth server conformance test。

### P2-6. Workflow behavior eval

Workflow 机制层很强，但缺 behavior eval。建议新增 workflow-native benchmark：

- gate/wait/resume
- external side-effect step graceful drain
- trigger pinned hash mismatch
- promote human approval
- restart after partial completion

结果进入 G5 单独 score，不再只用 repo evidence。

## 4. 建议实施顺序

1. P0-1 mutating replay fail-closed。
2. P0-2 Teammate Mailbox durable backend。
3. P0-3 RLS runtime role assertion。
4. P0-4 artifact gate 接入 promotion/CI。
5. P1-1 behavior eval runner/baseline updater。
6. P1-2 promotion regression report。
7. P1-3 memory lifecycle writer。
8. P1-4 connector ACL authority。
9. P1-5 reconciliation admin/API/UI。
10. P1-6 sandbox probe scheduler。
11. P2 项按团队风险偏好并行推进。

这个顺序优先修“现在可能产生错误安全感或重复副作用”的问题，再修“无法证明 SOTA”的问题，最后补观测和纵深。

## 5. 每一项的完成定义

任何一项不得只写代码或只写文档，完成时必须同时具备：

1. Red test：先证明当前缺陷或缺口存在。
2. Green implementation：主路径代码接通，不是测试专用分支。
3. Regression suite：相关 backend/frontend tests 通过。
4. Evidence update：更新 `docs/hive-sota-master-goal.md` 或审计文档时明确写成 code-level closed / live-pending / production-pending。
5. Production verifier：如果涉及 production truth surface，提供可重复命令，例如 health endpoint、Railway env readback、DB role check、baseline report check。

## 6. 最小推荐验证命令

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_subagent_run_service.py tests/services/test_runtime_task_service.py tests/agents/test_orchestrator.py -q
pytest tests/agents/test_coordination_wiring.py tests/services/test_agent_team_context.py -q
pytest tests/services/test_rls_runtime_guard.py tests/api/test_rls_bypass_audit.py -q
pytest tests/evals/test_artifact_gate.py tests/evals/test_adversarial_suite.py tests/evals/test_ci_gate.py tests/services/test_skill_distiller.py -q
pytest tests/evals/test_hive_live_runner.py tests/evals/test_behavior_baseline_update.py -q
pytest tests/memory/test_lifecycle_maintenance.py tests/memory/test_retrieval_pipeline.py -q
pytest tests/services/test_connector_acl.py tests/kernel/test_generated_source_acl.py -q
pytest tests/services/test_code_execution_probe.py -q
```

Frontend/API reconciliation 面完成后追加：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run test -- AgentDetailSections.test.tsx
npm run build
```

## 7. 不应现在做的事

- 不应继续新增展示型 SOTA 文案。没有 live behavior baseline 前，文案只会扩大证据债。
- 不应把 production env flip 和 code guard 混为一谈。RLS cutover 必须先有 runtime role assertion，再做 Railway 配置切换。
- 不应把 `provisional=true` 的 baseline 改成非 provisional，除非 report 来自 trusted live transport 且 `behavior_eval_passed()` 为 true。
- 不应为了让 promotion “动起来”而放松 behavior gate。Hive 当前落后 Hermes 的是实证效果，不是 gate 太严格本身。

## 8. Phase 完成证据

| Phase | 状态 | 代码变化 | 证据 | 剩余边界 |
|---|---|---|---|---|
| Phase 0: 审计基线与优化计划 | completed | 提交 `docs/sota-atomic-system-audit-2026-06-16.md`、本计划文档，并在 `docs/hive-sota-master-goal.md` 记录第三轮审计 | commit `c376821c` (`Document SOTA remediation phases`) | 仅文档基线，无代码修复 |
| Phase 1: mutating restart replay fail-closed | completed | `subagent_run_service` 与 `orchestrator` 不再自动重放 mutating subagent/delegation；Red tests 覆盖 `spawn_intent_recorded` journal 不能作为 replay-safe 凭证 | `cd backend && source .venv/bin/activate && pytest tests/services/test_subagent_run_service.py tests/agents/test_orchestrator.py tests/services/test_runtime_task_service.py -q` -> `57 passed, 4 warnings` | mutating lane 现在正确 fail-closed；尚未实现 Temporal 式 exactly-once side-effect journal，也未提供 reconciliation UI/API |
| Phase 2: Teammate Mailbox durable 默认后端 | completed | `COORDINATION_BACKEND` 默认切到 `postgres`；新增默认 `gateway_scope(tenant_id=...)` 写 signal 后由 prompt-facing Team Context 读同一 Postgres session 的测试；旧 in-process workflow signal 测试显式指定 `memory` | `cd backend && source .venv/bin/activate && pytest tests/agents/test_coordination_wiring.py tests/services/test_agent_team_context.py tests/agents/test_coordination_repository.py tests/services/test_workflow_completion_signal_gateway.py tests/runtime/test_workflow_completion_signal.py tests/agents/test_subagent_async.py tests/services/test_workflow_checkpoint_integration.py -q` -> `38 passed, 4 warnings` | 默认裂脑已关闭；仍需 production mailbox trace、completion wake chain 样本和 multi-agent behavior eval |
| Phase 3: RLS runtime role assertion | completed | 新增 `backend/app/services/rls_runtime_guard.py`；startup 在 schema bootstrap 后检查 runtime PostgreSQL role；默认 `RLS_RUNTIME_ROLE_ENFORCEMENT=strict` 拒绝 `rolsuper` / `rolbypassrls`；`/api/health` 暴露 `rls_runtime_role` component | Red: `pytest tests/services/test_rls_runtime_guard.py tests/api/test_health_liveness.py -q` -> `7 failed, 2 passed`（service 缺失）；Green: `cd backend && source .venv/bin/activate && pytest tests/services/test_rls_runtime_guard.py tests/api/test_health_liveness.py -q` -> `9 passed, 3 warnings` | 运行时 superuser/BYPASSRLS 假安全已 fail-fast；仍需 Railway production role readback、stage-3 app role cutover、pre-auth bare session 审计和真实 access-denial trace |
| Phase 4: artifact execution gate 接入 promotion / CI | completed | `decide_behavior_gated_promotion()` 对 `skill` / `skill_patch` / code / workflow artifacts 要求 artifact gate report；`skill_distiller` 运行 sandbox-backed skill artifact verifier 并把 `artifact_gate_report` 写入 promotion ledger；`ci_gate` 消费 artifact/adversarial report；`.github/workflows/harness-ci.yml` 生成并上传 adversarial suite JSON | Red: `pytest tests/services/test_promotion_hard_gate.py tests/services/test_skill_distiller.py::test_distiller_cannot_promote_when_artifact_gate_fails tests/evals/test_ci_gate.py tests/evals/test_adversarial_suite.py tests/evals/test_harness_ci_workflow.py -q` -> collect errors（缺常量/CLI/helper）；Green: `cd backend && source .venv/bin/activate && pytest tests/services/test_skill_distiller.py tests/services/test_promotion_hard_gate.py tests/evals/test_artifact_gate.py tests/evals/test_adversarial_suite.py tests/evals/test_ci_gate.py tests/evals/test_harness_ci_workflow.py -q` -> `71 passed, 4 warnings` | artifact gate 不再 dark；仍需真实 CI run artifact、生产 microVM provider 证据、behavior eval live PASS 和 score trend |
| Phase 5: behavior eval runner / baseline updater | completed | 新增 `backend/app/evals/run_sota_behavior_eval.py` 统一 SOTA report 入口；新增 `backend/app/evals/update_behavior_baseline.py` 从 trusted live report 生成非 provisional baseline；fixture/fallback Red tests 覆盖 hive/hermes 两侧 | Red: `pytest tests/evals/test_behavior_baseline_update.py -q` -> `6 failed`（module 缺失）；Green: `cd backend && source .venv/bin/activate && pytest tests/evals/test_hive_live_runner.py tests/evals/test_behavior_baseline_update.py tests/evals/test_ci_gate.py -q` -> `37 passed, 3 warnings`; `ruff check app/evals/run_sota_behavior_eval.py app/evals/update_behavior_baseline.py tests/evals/test_behavior_baseline_update.py` -> `All checks passed` | “如何产生并写入真实 baseline”已有代码路径；尚未执行真实目标环境 live report，因此 `core_behavior_v1.json` 仍保持当前 provisional truth |
| Phase 6: promotion regression report 不得跳过 | completed | `decide_behavior_gated_promotion()` 对行为变更候选缺 `regression_report` hold；`skill_distiller` 基于 `core_behavior_v1` baseline 构造 regression report，并把 behavior report id、artifact gate report id、scenario scores、baseline version/provisional 状态写入 promotion ledger | Red: `pytest tests/services/test_promotion_hard_gate.py tests/services/test_skill_distiller.py::test_run_skill_distillation_cycle_promotes_high_confidence_candidate -q` -> `2 failed`（缺 regression enforcement / ledger metadata）；Green: `cd backend && source .venv/bin/activate && pytest tests/services/test_skill_distiller.py tests/services/test_promotion_hard_gate.py -q` -> `43 passed, 4 warnings`; `ruff check app/services/evolution_verification.py app/services/skill_distiller.py tests/services/test_promotion_hard_gate.py tests/services/test_skill_distiller.py` -> `All checks passed` | 回归门不再可被隐式跳过；但当前 baseline 仍是 provisional seed，真实 live baseline 更新仍待运行 Phase 5 CLI |

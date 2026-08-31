# Hive SOTA 原子化系统审计（2026-06-15）

> 结论先行：以当前 `docs/hive-sota-master-goal.md` 为唯一目标口径、以当前代码/测试/本地对标项目/外部官方资料重新取证后，**95% 置信度判定：Hive 尚未整体达成 SOTA 总目标**。
>
> 更准确的状态是：**核心 harness、Plan Mode、Workflow、Work Ledger、代码执行隔离、MCP/interop、invocation trace、skill runtime telemetry 等机制已经接主链且有测试；但整体 SOTA claim 仍被 live behavior baseline、Hermes live delta、Zep/Letta 级记忆证明、Glean 式全 source ACL、mutating subagent replay、生产 microVM trend 阻断**。

## 0. 取证口径

- Hive 当前 checkout：`ff5160fa`，分支 `main`；本轮审计基于该 checkout 的当前代码、测试和对标源取证。
- 本轮未修改业务逻辑；仅刷新本审计文档。`.ultra/debug/subagent-log.jsonl` 是本地运行日志，不属于审计结论或业务改动。
- 目标口径只取 `docs/hive-sota-master-goal.md`：该文明确要求“已达成”必须有当前代码路径、测试、部署或生产证据，且做完外部行为 eval 前不能宣称整体超越 SOTA。
- 本地对标项目：
  - Claude Code / CC：`/Users/example-owner/vc-saas/free-code-main`，commit `7dc15d6`。
  - Hermes：`/Users/example-owner/vc-saas/hermes-agent`，commit `75643a615`。
  - Codex：`/Users/example-owner/Context Engineering/codex`，commit `9f4fac8ec4`。
- 外部对标重新核对 GitHub/官方资料：Voyager、Reflexion、DGM、ADAS、Letta/MemGPT、Graphiti/Zep、LangGraph、Magentic-One/AutoGen、Temporal、Vercel Sandbox、E2B、Glean、SEAL、AlphaEvolve、AZR、R-Zero。

## 1. 95% 置信答案

**没有整体达成。**

置信度能到 95% 的原因不是“某个局部实现很弱”，而是目标文件自身把以下项列为整体 SOTA 的硬门槛，而当前仓库仍能直接取证到未满足：

1. `backend/app/evals/baselines/core_behavior_v1.json:4-16` 仍是 `baseline_version="0.1.0-provisional"`、`commit_sha="pending-e2-live-run"`、`provisional=true`，六个核心场景分数仍是 `0.0` / `transport="pending"`。
2. `backend/app/evals/hive_live_runner.py:100-112` 的 `behavior_eval_passed()` 只接受完整 trusted live run；`backend/app/evals/ci_gate.py:55-63` 会对非 live report fail-closed。这证明门存在，不证明已有真实分数。
3. `.github/workflows/harness-ci.yml:100-151` 的 nightly behavior eval 缺 secrets 时写 evidence 并非零退出；它仍不是一次成功的 Railway eval live report。
4. skill runtime telemetry 已从旧缺口升级为“机制接入”：`backend/app/runtime/invoker.py:1071-1104` 捕获完成的 tool events，`backend/app/services/skill_runtime_telemetry.py:86-125` 只在 `load_skill` 真实出现时写 runtime usage，相关测试通过。但仓库仍缺真实生产 `skill_runtime_usage.jsonl` / promotion ledger 证明它驱动了 live patch/promote。
5. `backend/app/services/subagent_run_service.py:36-60,167-177` 与 `backend/app/agents/orchestrator.py:169,502-513,1396-1407` 明确只对 replay-safe lane 自动 resume；mutating subagent/delegation 会进入 reconciliation，不是 Temporal 式全副作用 replay。
6. connector ACL 本地 mirror、prompt 前过滤和 post-generation check 存在（`backend/app/services/connector_acl.py`），但 Feishu/Drive/Office 等 source connector 还没有全部达到 Glean 式 authoritative source ACL ingest + prefilter + generation recheck。
7. 代码执行 provider 和探针存在，但目标文档要求的持续 Railway/Vercel microVM trend 仍缺真实生产样本。
8. 长期记忆治理存在，但尚缺 Zep/Graphiti 级双时态 KG、multi-hop/PPR、TTL/引用重校验、conflict ledger 的行为级证明。

因此，“机制接近 / 局部达成 / 某些维度已接主链”可以说；“整体 SOTA / 超越 Hermes / 超越公开 SOTA”当前不能说。

## 2. 总目标原子矩阵

| 目标 | 本轮判定 | 置信度 | 当前证据 | 阻断点 |
|---|---:|---:|---|---|
| G1 治理化运行时自进化 | partial+ | 78% | skill candidate loop 默认开；distiller 有 patch-first；`invoke_agent()` 已接 skill runtime telemetry；promotion 需 behavior gate | 没有真实 live behavior report；没有生产 ledger 证明 organic telemetry 已驱动 patch/promote |
| G2 外部硬验证门 | near | 85% | `skill_guard`、`agent_behavior_check`、LLM rubric non-gating、`decide_behavior_gated_promotion()` fail-closed；相关测试 31 passed | 硬门可用，但运营上无真实 trusted report |
| G3 长期记忆 SOTA | partial | 60% | write gate、activation、principal stripping、memory hygiene 有实现和测试入口 | 缺双时态 KG、conflict ledger、TTL/引用重校验、live recall eval |
| G4 Durable execution | near | 85% | RuntimeTask、web chat resume、workflow journal/restart tests 57 passed | mutating delegation/subagent 不是全量 durable replay |
| G5 Workflow 确定性编排 | near | 88% | `WorkflowEngine` zero-DB interpreter、step/leaf journal、fanout resume、gate/wait/quota；workflow tests passed | 缺 live/product eval 与更长期跨 worker 运营证据 |
| G6 Plan Mode / confirmed autonomy | near | 88% | tool-intercept Plan Mode、read-only reminder、clarification、agent-authored `plan_markdown`；Plan Mode tests passed | 仍缺生产 UX 质量样本和所有 autonomous entry trace |
| G7 Subagent / Delegation | partial+ | 75% | background subagent RuntimeTask、memory isolation、replay-safe resume tests passed | mutating lane fail-closed reconciliation，缺 live multi-agent eval |
| G8 Work Ledger / Progress Ledger | near | 90% | `track_todo` cognitive-only；session/runtime/plan scoped artifact；reboot restore；tests passed | 仍需更多真实复杂任务 completion/replan UX evidence |
| G9 企业身份与控制面 | near | 88% | tenant-scoped session、RLS bypass explicit audit、RLS/ACL tests passed | 还缺 external directory / access package 级闭环 |
| G10 权限感知数据面 | partial+ | 78% | connector ACL mirror、prompt prefilter、post-generation permission check、knowledge inject filter；tests passed | 全 connector authoritative ACL ingest 不完整 |
| G11 安全执行隔离 | partial+ | 80% | provider selector、local OS sandbox、Vercel Sandbox provider、env allowlist、probe script、MCP token passthrough reject；tests passed | 缺持续生产 microVM probe artifact / trend |
| G12 Context/cache/tool economy | near | 82% | cache anchor、runtime reminders、tool expansion、CJK/token budget 在主链路 | tool surface 大规模模块树仍未证明 |
| G13 多 agent 编排 | partial | 70% | coordination signals、subagent/workflow state、Progress Ledger reminder | 缺 live multi-agent eval 证明收益 |
| G14 Eval/观测闭环 | partial+ | 76% | invocation spans、CI/eval gate、nightly workflow、Prometheus/trace 面 | 核心 baseline provisional，缺 score trend |
| G15 互操作诚实 | near | 90% | MCP authz reject passthrough；A2A/interop `not_exposed` tests passed | 完整 OAuth delegation / MCP resource-server flow 未实现 |

## 3. 与本地对标项目的差距

### CC / Claude Code local

CC 的强项仍是 agent 体验闭环：Plan Mode 是 read-only exploration 后由模型提交计划；TodoWrite 是 session cognitive list；TaskCreate 是任务记录而非自动执行；compaction、compact boundary、memory mechanics 和 retry/reconnect 处理成熟。Hive 在企业治理、RLS、Workflow、provider sandbox 上超过 CC 的本地表面，但在“live 行为质量是否超过 CC/Hermes”上没有分数。

### Hermes local

Hermes 的强项是 lean agent 质量：todo 会话恢复、external memory provider、skill/usage lifecycle、delegate_task 子 agent 隔离、默认禁止子 agent memory/execute_code/递归委托、文件修改落地 verifier、LSP diagnostics、release notes 里有自动行为 benchmark 与 patch-first 经验。Hive 的机制更企业级，但目标文件要求“单 agent 智能至少达到并超过 Hermes”；当前缺少 Hermes-vs-Hive live delta，因此不能 claim。

### Codex local

Codex 的强项是 sandbox/approval/rollout/memory consolidation：exec policy、OS sandbox、approval、rollout JSONL、resume/fork、MCP routing、两阶段 memory pipeline。Hive 在公司级控制面更完整，但 Codex 的本地执行/trace/memory pipeline 仍是重要对标线。

## 4. 外部 SOTA 对标结论

- Voyager / AlphaEvolve / DGM / ADAS / SEAL / AZR / R-Zero 的共同硬点是：自改/自进化必须被外部 evaluator 或可验证任务回报裁决。Hive 的 gate 方向正确，但缺真实 live 分数时序。
- Letta/MemGPT 与 Zep/Graphiti 的硬点是长期 memory runtime 和时间感知 memory graph。Hive 有 governed Markdown memory + activation，但不是完整 temporal KG / conflict reconciliation。
- LangGraph / Temporal 的硬点是 durable stateful workflow。Hive Workflow 有 deterministic interpreter + journal/resume，但 mutating subagent/delegation 未全覆盖。
- Magentic-One / Anthropic multi-agent research 的硬点是 orchestrator progress ledger、specialized workers、benchmark results。Hive 机制具备，但缺 live multi-agent eval。
- Vercel Sandbox / E2B 的硬点是真实隔离运行环境。Hive provider 接线正确，但仍要持续生产 probe。
- Glean 的硬点是权限感知 enterprise retrieval。Hive 有 mirror/filter/check，但 source connector 全覆盖未完。

## 5. 本轮验证命令

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/evals/test_hive_live_runner.py \
  tests/services/test_skill_distiller.py::test_distiller_cannot_promote_without_external_behavior_eval \
  tests/services/test_skill_distiller.py::test_run_skill_distillation_cycle_applies_verified_patch \
  tests/services/test_evolution_verification.py -q
# 31 passed, 3 warnings
```

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_invoker.py::test_invoke_agent_records_skill_runtime_usage_and_preserves_tool_callback \
  tests/runtime/test_invoker.py::test_invoke_agent_records_failed_skill_runtime_usage_when_kernel_raises \
  tests/services/test_skill_runtime_telemetry.py \
  tests/services/test_skill_lifecycle.py -q
# 10 passed, 4 warnings
```

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_plan_mode_core.py::test_explicit_plan_mode_does_not_seed_retired_manage_tasks_tool \
  tests/services/test_plan_mode_core.py::test_retired_manage_tasks_is_not_a_plan_mode_display_label \
  tests/services/test_plan_mode_core.py::test_render_plan_markdown_uses_agent_authored_body_when_present \
  tests/kernel/test_plan_mode_reminder.py \
  tests/tools/handlers/test_work_ledger_handler.py \
  tests/services/test_agent_work_ledger.py \
  tests/kernel/test_work_ledger_scaffold.py \
  tests/services/test_extract_agent.py::TestBuildConversationTextCaps::test_retired_db_task_tool_transcripts_are_not_silently_filtered -q
# 41 passed, 4 warnings
```

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_vercel_code_execution.py \
  tests/services/test_code_execution_probe.py \
  tests/services/test_mcp_authz.py \
  tests/services/test_interoperability.py \
  tests/api/test_interoperability_api.py -q
# 21 passed, 3 warnings
```

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_workflow_restart_resume.py \
  tests/runtime/test_workflow_worker_lease.py \
  tests/runtime/test_workflow_gate_step.py \
  tests/runtime/test_workflow_wait_signal.py \
  tests/runtime/test_workflow_wait_until.py \
  tests/services/test_workflow_runtime_service.py \
  tests/services/test_subagent_run_service.py \
  tests/kernel/test_subagent_memory_isolation.py \
  tests/agents/test_subagent_memory.py -q
# 57 passed, 4 warnings
```

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_connector_acl.py \
  tests/services/test_knowledge_inject.py \
  tests/services/test_audit_rls_coverage.py \
  tests/api/test_rls_bypass_audit.py -q
# 22 passed
```

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm run test -- AgentDetailSections.test.tsx
# 1 test file passed, 36 tests passed
```

## 6. 外部来源

- Voyager: https://github.com/MineDojo/Voyager
- Reflexion: https://github.com/noahshinn/reflexion
- DGM: https://github.com/jennyzzt/dgm
- ADAS: https://github.com/ShengranHu/ADAS
- Letta: https://github.com/letta-ai/letta
- Graphiti/Zep: https://github.com/getzep/graphiti
- LangGraph: https://github.com/langchain-ai/langgraph
- Magentic-One / AutoGen: https://github.com/microsoft/autogen
- Temporal: https://github.com/temporalio/temporal
- Vercel Sandbox: https://github.com/vercel/sandbox
- E2B: https://github.com/e2b-dev/e2b
- Glean permissions-aware AI: https://www.glean.com/perspectives/security-permissions-aware-ai
- SEAL: https://github.com/Continual-Intelligence/SEAL
- AlphaEvolve results: https://github.com/google-deepmind/alphaevolve_results
- Absolute Zero Reasoner: https://github.com/LeapLabTHU/Absolute-Zero-Reasoner
- R-Zero: https://github.com/Chengsong-Huang/R-Zero

## 7. 下一步解锁整体 SOTA claim

1. 跑一次真实 Railway eval backend 的 `hive_live_runner`，覆盖 provisional baseline，保留 behavior report artifact。
2. 同场景跑 Hermes live baseline，产出 Hive-vs-Hermes delta。
3. 保留真实 agent workspace 的 `skill_runtime_usage.jsonl`、`evolution/skill_candidates/<candidate_id>/` Candidate Package、promotion report，证明 organic skill telemetry 能驱动 patch/promote。
4. 给 mutating subagent/delegation 增加 step-level journal/idempotency/reconciliation contract，或明确把该 lane 排除在 durable SOTA claim 外。
5. 补全 Feishu/Drive/Office 等 source connector 的 authoritative ACL ingest + prompt prefilter + generation recheck fixture。
6. 生产持续运行 `python -m app.scripts.probe_code_execution_sandbox --persist --confirm`，保存 Vercel microVM probe trend。
7. 为 memory 增加 conflict/TTL/reference-revalidation/temporal multi-hop eval，再谈 Zep/Letta 级别。

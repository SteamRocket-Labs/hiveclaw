# CCPlus Dynamic Workflow Implementation Plan (2026-06-27)

> 状态：已闭环的上线前 Dynamic Workflow 统领实现文档。本文合并官方 CC/Anthropic 资料、本地源码核验、Hive 现有 Workflow runtime 事实、前端呈现、失败补救、prompt 策略和落地证据。
>
> 裁决关系：如本文与 `dynamic-workflow-cc-alignment-redesign-2026-06-23.md`、`dynamic-workflow-harness-semantics-2026-06-24.md` 或 `workflow-source-capability.md` 冲突，以本文为准；三篇旧文保留为调研证据和底层语义补充。

## 0. 最终裁决

Dynamic Workflow 先做，而且可以开始做。它和 A2A Workflow 必须分层：

| 轨道 | 本轮是否做 | 唯一路径 | 边界 |
| --- | --- | --- | --- |
| Dynamic Workflow | 是 | 当前 Agent 在当前 session 内设计 harness，降级为 Hive `WorkflowDefinition`，经预览、批准、运行、补救、固化 | 不跨完整 Agent principal，不读他人 memory/workspace/tool context |
| Registered template / Dynamic 固化物 | 是，作为 Dynamic 的固化结果和已有 runtime asset | approved registered workflow version/hash | 不是第三种 workflow，不重新发明一套 runtime |
| A2A Workflow | 后做 | 完整 Agent principal 之间的 process graph、artifact_ref、node session、edge gate | 不塞进 Dynamic Workflow，不复用 `WorkflowDefinition` 变成万能 DSL |

唯一主线：

```text
用户意图 / /workflow / 固定 workflow 命令 / selector 提示
  -> Agent 判断需要 Dynamic Workflow
  -> propose_dynamic_workflow 生成候选 harness proposal
  -> 候选降低到受治理 WorkflowDefinition
  -> preview_workflow 编译、准入、预算、风险、hash
  -> 用户批准 exact proposal_id + candidate_id + preview_id/hash/args
  -> start_workflow 启动唯一 WorkflowEngine
  -> RuntimeTask + workflow_steps + workflow_leaf_calls 监控、暂停、恢复
  -> 失败叶子定向补救，不整链重跑
  -> outcome evidence 决定 fork/mutate/promote
```

不得保留第二条产品路径。高级手动 JSON 只能作为同一 `preview_workflow/start_workflow` 的 raw artifact 调试入口，不能作为用户默认创作入口。

## 1. 核验来源

### 1.1 官方 CC / Anthropic 资料

已在 2026-06-27 重新核验：

- Claude Code Docs: [Orchestrate subagents at scale with dynamic workflows](https://code.claude.com/docs/en/workflows)
- Claude Blog: [Introducing dynamic workflows in Claude Code](https://claude.com/blog/introducing-dynamic-workflows-in-claude-code)
- Claude Blog: [A harness for every task](https://claude.com/blog/a-harness-for-every-task-dynamic-workflows-in-claude-code)
- Anthropic Engineering: [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)

核心事实：

- CC Dynamic Workflow 是 Claude 写出的 JavaScript orchestration script，由 runtime 后台执行。
- script 负责 loop、branch、fanout、barrier、intermediate state；subagent 只负责叶子任务。
- 中间结果保存在 workflow runtime state，不全部塞回主会话 context。
- 入口包括显式自然语言、触发词、`/deep-research` 等 saved/bundled workflow、以及 ultracode effort 下的模型自动判断。
- 启动前有 planned phases / raw script / token caution / allow-deny approval。
- 运行中通过 `/workflows` 或 background task pane 监控，能看 phase、agent、token、耗时、agent prompt/tool/result，并支持 pause/resume/stop/restart agent/save。
- resume 只在同一 session 内保证；已完成 agent 结果缓存，未完成部分继续跑。
- 约束包括无 mid-run user input、workflow script 不直接访问 filesystem/shell、agent 并发和总 agent 数上限。
- Anthropic 的通用 agent engineering 文章把 workflow 定义为预定 code path，把 agent 定义为模型动态决定过程；推荐简单可组合 pattern，例如 prompt chaining、routing、parallelization、orchestrator-workers、evaluator-optimizer。

### 1.2 本地源码核验

本地核验结论：

- Hive 已有下层 runtime：`backend/app/runtime/workflow_definition.py`、`workflow_compiler.py`、`workflow_admission.py`、`workflow_engine.py`、`backend/app/services/workflow_runtime_service.py`、`workflow_definitions.py`、`workflow_trigger.py`、`workflow_promote_suggestions.py`、`backend/app/tools/handlers/workflow.py`。
- Hive 已有 `preview_workflow/start_workflow`，并且 AgentTool `start_workflow` 已绑定 `preview_id` 或 `definition_hash + args_hash`。
- REST/UI 手动启动不再是第二条 raw path：`/workflows/runs` 必须携带 fresh `preview_id`，该 `preview_id` 来自同一个 `app.runtime.workflow_preview` binding store。
- Hive 已有 `propose_dynamic_workflow`、Dynamic proposal/candidate validation/lowering、proposal-aware chat card、run metadata、journal outcome evidence、repair endpoint、promotion evidence，以及 Workflows tab 的 dynamic evidence/repair 操作。
- FreeCode / claude-code-org 本地 checkout 能看到 workflow feature gate 和 `local_workflow` task type，但没有完整 WorkflowTool 实现目录，不能作为实现细节真相源。
- Codex Rust 没有等价 Dynamic Workflow runtime；Codex 可吸收的是 Plan Mode、policy gate、thread/workbench/progress/verification 等工程控制优势。

## 2. 产品触发和呈现

### 2.1 触发方式

Hive 需要四个入口，但都收敛到同一路径：

| 入口 | 用户形态 | 后端动作 | 是否可直接运行 |
| --- | --- | --- | --- |
| 自然语言 | “用 workflow 做一次全仓审计”“fanout 多个 agent 交叉验证” | Agent 调 `propose_dynamic_workflow` | 否，必须 preview + approval |
| `/workflow` / command | 打开 workflow proposal composer 和 Workflows tab | `commands.py` 只做 UI intent，不启动 runtime | 否 |
| 固定 workflow 命令 | 运行已批准的 registered workflow | 走 existing registered definition -> preview/start | 视定义风险要求确认 |
| selector 自动建议 | 任务明显大规模、并行、对抗验证、可复用、长运行 | Agent 先提出 Dynamic proposal | 否 |

不引入 Hive 版 `ultracode` 作为第一期产品名。第一期只需要 “workflow requested / workflow recommended” 两种语义，避免多一个 effort 模式。

### 2.2 前端呈现

触发后前端应呈现三层，而不是把 JSON 编辑器放在中心：

1. **Chat proposal card**
   - 显示 proposal name、目标、pattern mix、候选数、推荐候选、预计 step/agent/token、风险、需要用户批准的原因。
   - 支持展开 raw lowered `WorkflowDefinition`。
   - CTA：Preview selected、Ask agent to revise、Deny。

2. **Workflows tab**
   - 默认视图：Dynamic proposals、active runs、recent runs、promote suggestions、registered workflows。
   - 高级 raw JSON 折叠到 debug 区，仍走同一 API。

3. **Run monitor**
   - phase/step 列表、agent/leaf 状态、token、耗时、错误、result_ref。
   - 支持 pause/resume/cancel、retry failed leaf、inspect leaf prompt/tool/result、save/promote。

### 2.3 动态监控

后端监控继续复用 `RuntimeTask(task_type="workflow") + WorkflowStep + WorkflowLeafCall`，补齐 proposal metadata：

```json
{
  "dynamic_workflow": {
    "proposal_id": "dwf_prop_...",
    "candidate_id": "cand_...",
    "pattern_mix": ["fanout_synthesize", "adversarial_verify"],
    "approval_contract": {
      "preview_id": "...",
      "definition_hash": "...",
      "args_hash": "...",
      "budget": {"max_leaf_calls": 40, "max_tokens": 120000}
    }
  }
}
```

run monitor 的事实源仍是 workflow journal，不从聊天文本推断。

## 3. 核心机制

### 3.1 Selector

Selector 是 prompt + schema，不是第二个 runtime。它只回答三选一：

```text
no_workflow
fixed_workflow
dynamic_workflow
```

推荐 Dynamic Workflow 的条件：

- 工作需要多个独立视角、目录、文件、数据源或候选方案。
- 错误成本高，需要对抗验证或独立复核。
- 过程很长，主会话 context 不应承载全部中间结果。
- 需要 repeatable harness，未来可能保存成 workflow。
- 用户显式要求 workflow、fanout、多 agent orchestration、交叉验证、批量迁移、深度研究。

拒绝 Dynamic Workflow 的条件：

- 单文件、单问题、低风险、短上下文。
- 只需要普通工具调用或一个 subagent。
- 需要完整 Agent principal 之间交接，这应进入 A2A Workflow。
- 需要用户在中途频繁交互，第一期不支持 mid-run user input。

### 3.2 Proposal / Candidate

`propose_dynamic_workflow` 接收模型写出的候选，而不是平台机械生成“语义计划”。平台职责是解析、校验、降低、打分和留证。

建议 schema：

```json
{
  "goal": "string",
  "why_workflow": "string",
  "success_criteria": ["string"],
  "candidates": [
    {
      "candidate_id": "string",
      "name": "string",
      "pattern_mix": ["fanout_synthesize", "adversarial_verify"],
      "risk_level": "low|medium|high",
      "budget": {
        "max_steps": 8,
        "max_leaf_calls": 64,
        "max_concurrency": 8,
        "max_tokens": 120000,
        "max_wall_clock_seconds": 7200
      },
      "failure_policy": {
        "leaf_failure": "record_and_continue",
        "barrier_threshold": {"min_success_ratio": 0.75},
        "repair_rounds": 1,
        "no_full_chain_rerun": true
      },
      "lowered_definition": {}
    }
  ],
  "recommended_candidate_id": "string"
}
```

`lowered_definition` 必须是现有或扩展后的 Hive `WorkflowDefinition`，不能是 JS/Python/Jinja/eval。

### 3.3 Prompt strategy

提示词要直接告诉模型什么时候用 workflow，而不是只列工具名：

```text
Use Dynamic Workflow when the task needs many isolated workers, repeatable orchestration, adversarial review, or long-running state outside the main context.
First propose a workflow. Do not start it. Provide 2-3 candidates when design tradeoffs matter.
Each candidate must include pattern mix, budget, failure policy, success criteria, and a lowered governed WorkflowDefinition.
Start only after preview_workflow returns an exact preview artifact and the user approves that exact artifact.
```

Leaf agent prompt 必须窄化：

```text
You are one leaf in a workflow.
Objective: ...
Input slice: ...
Allowed tools/capabilities: ...
Output schema: ...
Evidence required: cite file/source/result refs.
Stop condition: return when the slice is complete; do not broaden scope.
Failure behavior: return structured failure; do not retry unrelated work.
```

Critic / verifier prompt：

```text
Try to falsify the candidate result using independent evidence.
Do not restate the worker's answer.
Return pass/fail, blocking issues, weak evidence, and targeted repair tasks.
```

Rescue prompt：

```text
Use only failed, thin, or contradicted leaves.
Reuse completed leaf results from the journal.
Do not rerun successful leaves unless the verifier identifies a concrete contamination reason.
```

### 3.4 3 到 4 轮迭代模式

Dynamic Workflow 的正常轨迹不是无限自治，而是有界重复：

| 轮次 | 目标 | 典型 pattern | 产物 |
| --- | --- | --- | --- |
| R0 Proposal | 设计 harness 候选 | selector + candidate critic | proposal/candidates |
| R1 Explore / Execute | 分片执行或多视角探索 | fanout, routing, orchestrator-workers | leaf results |
| R2 Verify | 交叉验证、反驳、投票 | adversarial verification, evaluator-optimizer | verifier reports |
| R3 Repair / Mutate | 定向补救失败或证据薄弱部分 | targeted retry, fork candidate | repair results |
| R4 Synthesize | 汇总最终答案和固化建议 | synthesis + outcome scorer | final artifact + promote evidence |

默认最多一次 repair round；更高轮次必须由 budget 或用户明确要求打开。

## 4. 失败处理和补救机制

原则：一个 Agent/leaf 失败，不能导致整个 workflow 从头重跑。

| 失败类型 | runtime 行为 | Agent 行为 | 用户可见 |
| --- | --- | --- | --- |
| 单 leaf timeout/error | 记录 `WorkflowLeafCall.status=failed`，fanout 继续 | 后续 verifier 判断是否需要补救 | phase 有 failed count |
| barrier 成功率不足 | step 进入 `needs_repair` 或 `suspended` | 生成 targeted repair leaves | 显示 repair request |
| verifier 不通过 | 保留原结果，新增 repair/fork step | 只补证据薄弱或被推翻部分 | 显示 blocking findings |
| 外部动作待确认 | workflow suspended at gate | 等用户确认或取消 | gate card |
| definition/hash mismatch | 拒绝 start/resume | 要求重新 preview | hard error |

补救路径：

```text
completed leaves -> cached
failed leaves -> targeted retry if reversible and budget remains
thin leaves -> verifier creates repair task
contradicted leaves -> fork/mutate candidate
all results -> final synthesis includes known failures and confidence
```

不允许静默整链重跑。整链重跑只能是用户显式 relaunch，且作为新 run 留证。

## 5. 与现有 Sub-agent / Send Agent / Agent Team / A2A 的关系

| 能力 | Dynamic Workflow 中的角色 | 不做什么 |
| --- | --- | --- |
| `spawn_subagent` | session-local leaf worker / critic / verifier | 不代表完整数字员工 |
| Agent Team | 长运行 peer sessions，可作为未来 worker pool 参考 | 第一版不作为 Dynamic Workflow 控制器 |
| `send_message_to_agent` / `delegate_to_agent` | To Employee/A2A 路径 | 不用于 Dynamic Workflow 默认叶子 |
| A2A Workflow | 完整 Agent principal process graph | 不和 Dynamic Workflow 合并 |
| MCP Skill / Hooks | 可作为 leaf capability 或 trigger/wait source | 不绕过 workflow admission 和 Platform Gate |

Dynamic Workflow 是 “To Session Worker”。A2A 是 “To Employee”。这两个必须在 prompt、tool description、API、UI 和 docs 里分开。

## 6. 实现顺序

### D1. Dynamic proposal schema 和 lowerer

目标：

- 新增 `backend/app/runtime/dynamic_workflow.py`。
- 定义 `DynamicWorkflowProposal`、`DynamicWorkflowCandidate`、`DynamicWorkflowFailurePolicy`、`DynamicWorkflowApprovalContract`。
- `lowered_definition` 用现有 `parse_workflow_definition -> compile -> admission` 校验。
- 不引入第二套 workflow runtime。

测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/runtime/test_dynamic_workflow_proposal.py
```

### D2. `propose_dynamic_workflow` tool

目标：

- 在 `backend/app/tools/handlers/workflow.py` 新增 `propose_dynamic_workflow`。
- 该工具只生成/验证 proposal，不启动 run。
- tool description 写清 “propose first, preview selected, start only after approval”。
- 加入 core/source capability surface。

测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_dynamic_workflow_tool.py
```

### D3. Plan Mode / approval contract

目标：

- Plan Mode 内允许 `propose_dynamic_workflow` 和 `preview_workflow`。
- Plan Mode 内禁止 `start_workflow`。
- `exit_plan_mode` 或等价用户确认必须绑定 `proposal_id + candidate_id + preview_id/hash + args_hash + budget`。
- `start_workflow` 继续只接受 exact preview artifact，不接受口头确认。

测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_plan_mode_dynamic_workflow.py tests/tools/test_workflow_preview_start_binding.py
```

### D4. Selector / prompt 常驻引导

目标：

- 在 runtime guidance 中加入 Dynamic Workflow selector。
- 明确 Sub-agent、Agent Team、Dynamic Workflow、A2A Workflow 的触发边界。
- 给 few-shot：大规模审计、交叉研究、批量迁移、对抗验证、候选 tournament。
- 避免 “默认自己做” 抑制 workflow；同时避免小任务滥用。

测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_runtime_guidance_catalog.py tests/services/test_agent_tools.py
```

### D5. Runtime metadata、repair 和 outcome evidence

目标：

- `RuntimeTask.metadata_json` 记录 proposal/candidate/approval contract。
- `WorkflowLeafCall` 失败不导致整链重跑；barrier 支持 threshold 和 repair request。
- outcome scorer 产出 quality evidence：success criteria、critic pass rate、failed leaf count、repair count、user feedback、promotion eligibility。
- promote suggestion 从 repeated hash 升级到 outcome evidence。

测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_workflow_runtime_service.py tests/runtime/test_workflow_engine.py tests/services/test_workflow_promote_suggestions.py
```

### D6. Frontend proposal + monitor

目标：

- `frontend/src/api/domains/workflows.ts` 增加 proposal types/API。
- `AgentWorkflowsSection.tsx` 增加 Dynamic proposals 区。
- Chat timeline 支持 `dynamic_workflow_proposal`、`workflow_run`、`workflow_step`、`workflow_leaf` 分层渲染。
- 高级 JSON 编辑器降级为 raw artifact/debug 区。

测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run build
npm run test -- AgentWorkflowsSection workflows
```

### D7. Saved / registered workflow integration

目标：

- 成功 run 可保存为 draft registered workflow。
- 激活仍需 human / owner / admin 审批。
- fixed workflow command 和 trigger 只引用 immutable version/hash。
- Dynamic Workflow 可以 fork fixed workflow，但仍回到 proposal -> preview -> approval。

测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/api/test_workflow_definitions.py tests/services/test_workflow_trigger.py
```

## 7. 验收标准

本轮完成后必须满足：

1. Agent 能在同一 session 内主动提出 Dynamic Workflow，而不是只会直接做或手写 JSON。
2. `propose_dynamic_workflow`、`preview_workflow`、`start_workflow` 是 AgentTool 唯一启动链路；REST/UI 手动入口也必须先 preview，并用 fresh `preview_id` 启动，不存在裸 `definition + args` 第二路径。
3. 用户能在 chat 或 Workflows tab 看见 proposal、phase、agent/leaf、token、耗时、错误、结果和 promote 状态。
4. 一个 leaf 失败不会导致整链重跑；完成叶子可缓存，失败叶子可定向补救。
5. prompt 明确区分 Sub-agent、Agent Team、Dynamic Workflow、A2A Workflow。
6. A2A Workflow 不被塞进 Dynamic Workflow 实现。
7. Workflow 不执行任意 JS/Python；Hive 只执行结构化 `WorkflowDefinition`。
8. registered workflow 和 ephemeral dynamic workflow 共用一套 engine、journal、budget、gate、audit。
9. 高级手动 JSON 不再是默认产品入口。
10. 所有实现都有 Red/Green 测试和证据文档。

## 8. 下一步从哪里开始

从 D1 开始，不先做 A2A Workflow，也不先做拖拽图编辑器。

第一组实现文件：

```text
backend/app/runtime/dynamic_workflow.py
backend/app/tools/handlers/workflow.py
backend/app/services/agent_tools.py
backend/app/kernel/runtime_guidance_catalog.py
backend/tests/runtime/test_dynamic_workflow_proposal.py
backend/tests/tools/test_dynamic_workflow_tool.py
```

第一组完成后必须提交 commit，并更新本文 §9 evidence。

## 9. Evidence Log

- 2026-06-27: 文档轮完成。核验官方 Claude Code Dynamic Workflows docs/blog、Anthropic agent patterns、本地 Hive workflow runtime、FreeCode/claude-code-org/Codex 源码表面。结论：Hive 下层 runtime 正确，缺 Dynamic proposal/evaluation/selector/UI 层；A2A Workflow 后置。
- 2026-06-27: D2/D3/D4 first implementation closed. `propose_dynamic_workflow` 已进入 core/source capability surface；candidate 会降低到现有 `WorkflowDefinition` 并经过 compile/admission/confirmation inspect；`preview_workflow` 绑定 `proposal_id`/`candidate_id`，`start_workflow` 仍要求 exact preview artifact/hash，并把 dynamic binding 写入 workflow run metadata；front-end chat tool card 已展示 proposal/candidates/next action。证据：
  - `cd backend && source .venv/bin/activate && pytest tests/tools/test_workflow_tool.py tests/services/test_agent_tools_core_surface.py tests/services/test_capability_gate_strict_mapping.py::test_t1_core_promoted_tools_have_capability_mappings tests/tools/test_service.py::test_interactive_plan_mode_allows_only_narrow_readonly_subagent_lane tests/runtime/test_t2_guidance_surface.py tests/services/test_tool_registry.py tests/tools/test_core_pack_disjoint.py -q` -> `49 passed, 4 warnings`
  - `cd backend && source .venv/bin/activate && ruff check app/tools/handlers/workflow.py app/services/workflow_launch.py app/services/workflow_runtime_service.py app/services/agent_tools.py app/services/capability_gate.py app/tools/plan_mode_policy.py app/agents/tool_policies.py app/runtime/prompt_sections/executing_actions.py app/runtime/prompt_sections/system.py app/runtime/prompt_sections/tools.py tests/tools/test_workflow_tool.py tests/services/test_agent_tools_core_surface.py tests/services/test_capability_gate_strict_mapping.py tests/tools/test_service.py tests/runtime/test_t2_guidance_surface.py tests/services/test_tool_registry.py tests/tools/test_core_pack_disjoint.py` -> `All checks passed!`
  - `cd frontend && npm test -- --run src/pages/agent-detail/toolResultEnvelope.test.ts src/pages/agent-detail/AgentDetailSections.test.tsx` -> `2 files passed, 84 tests passed`
- 2026-06-27: D1/D5/D6/D7 closure completed. Dynamic Workflow proposal helpers 已集中到 `backend/app/runtime/dynamic_workflow.py`；`preview_workflow` 会校验 `proposal_id/candidate_id` 的 lowered definition 与 args hash，`start_workflow` 禁止把普通 preview 临时伪装成 dynamic run；runtime completion 会把 `outcome_evidence` 和 `repair_plan` 写回 `RuntimeTask.metadata_json.dynamic_workflow`；API run detail/list 返回 dynamic metadata、outcome evidence、repair plan、leaf calls；`POST /agents/{agent_id}/workflows/runs/{run_id}/repair` 通过同一 journal `resume_run` 定向补救，执行前强制检查 `repair_plan.repairable`，并持久化 `repair_attempts`；promotion suggestion 同时接受 `ephemeral` 和 `dynamic_workflow` archive，并携带 `quality_evidence`；前端 Workflows tab 展示 dynamic badge、proposal/candidate、leaf evidence 和 repair action。证据：
  - `cd backend && source .venv/bin/activate && pytest tests/runtime/test_dynamic_workflow_proposal.py tests/api/test_workflows.py tests/tools/test_workflow_tool.py tests/services/test_workflow_promote_suggestions.py -q` -> `45 passed, 4 warnings`
  - `cd backend && source .venv/bin/activate && pytest tests/services/test_agent_tools_core_surface.py tests/services/test_tool_registry.py tests/services/test_capability_gate_strict_mapping.py tests/runtime/test_t2_guidance_surface.py -q` -> `37 passed, 4 warnings`
  - `cd backend && source .venv/bin/activate && pytest tests/runtime/test_dynamic_workflow_proposal.py tests/api/test_workflows.py tests/tools/test_workflow_tool.py tests/services/test_workflow_promote_suggestions.py tests/services/test_llm_error_policy.py tests/services/test_prompt_contracts.py::test_execution_playbook_keeps_skill_capsule_runtime_boundary tests/runtime/test_t2_guidance_surface.py -q` -> `60 passed, 4 warnings`
  - `cd backend && source .venv/bin/activate && ruff check app/kernel/engine.py app/runtime/dynamic_workflow.py app/tools/handlers/workflow.py app/services/workflow_runtime_service.py app/services/workflow_promote_suggestions.py app/api/workflows.py app/runtime/prompt_sections/executing_actions.py tests/runtime/test_dynamic_workflow_proposal.py tests/api/test_workflows.py tests/services/test_workflow_promote_suggestions.py tests/tools/test_workflow_tool.py tests/services/test_llm_error_policy.py` -> `All checks passed!`
  - `cd frontend && npm test -- --run src/api/domains/workflows.test.ts src/pages/agent-detail/AgentWorkflowsSection.test.tsx src/pages/agent-detail/toolResultEnvelope.test.ts src/pages/agent-detail/AgentDetailSections.test.tsx` -> `4 files passed, 110 tests passed`
  - `cd frontend && npm run build` -> `tsc && vite build` completed successfully

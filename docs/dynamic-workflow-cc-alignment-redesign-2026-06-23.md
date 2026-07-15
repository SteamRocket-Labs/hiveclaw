# Dynamic Workflow 对齐与重构设计（2026-06-23）

> 状态：讨论稿 / docs-only。本文只记录调研结论、代码事实、差距判断和目标设计，不包含实现改动。
>
> 目标：在保留 Hive 多租户治理、安全审计和确定性 runtime 的前提下，吸收 Claude Code Dynamic Workflows 的核心能力：模型现场生成 workflow、并行 subagent、对抗验证、运行中保存状态、失败可恢复、成功后可固化。
>
> **2026-06-27 裁决更新**：本文保留为早期调研证据和差距分析；当前 Dynamic Workflow 的上线前统领计划、唯一启动链路、触发/呈现/监控/失败补救/实施顺序以 [`dynamic-workflow-ccplus-implementation-plan-2026-06-27.md`](./dynamic-workflow-ccplus-implementation-plan-2026-06-27.md) 为准。

## 1. 结论先行

Hive 当前 Workflow 底座方向是对的：它是可序列化 JSON definition + 确定性解释器 + RuntimeTask/journal/quota/resume 的安全执行层。它比 Claude Code 的“模型写 JavaScript 并执行”更适合企业多租户平台。

但 Hive 现在缺少 Claude Code Dynamic Workflows 的核心上层能力：**动态 workflow 设计器**。

当前能力更像：

```text
Agent 写一份结构化 workflow definition
  -> preview_workflow 编译和准入
  -> 用户确认
  -> start_workflow 执行
```

目标能力应该是：

```text
Plan Mode 明确目标、约束、预算、验收标准
  -> DynamicWorkflowDesigner 生成多套候选 workflow
    -> Critic / Verifier 对候选做对抗评审
      -> 选择最优候选并 preview
        -> 用户确认 exact definition/hash/args
          -> Workflow runtime 执行
            -> 运行结果评分、失败 fork/mutate、成功建议 promote
```

所以本轮重构不应该推翻现有 WorkflowEngine。正确方向是新增一个 **Dynamic Workflow proposal/evaluation layer**，让现有安全 runtime 具备 CC dynamic workflow 的“流动性”。

## 2. 公开资料结论：CC Dynamic Workflow 到底是什么

Claude 官方文档和博客给出的定义很明确：

- Dynamic workflow 是 Claude 根据任务现场生成的 orchestration script。
- 这个 script 在隔离 runtime 里后台执行。
- script 可以 spawn 多个 subagents，并发运行、保存中间结果、合并输出。
- 中间结果保存在 script/runtime state 中，而不是全部塞进主会话 context。
- workflow 可查看 raw script，可保存为项目级或用户级 reusable workflow。
- runtime 有并发、总 agent 数、预算、确认和 resume 边界。

资料来源：

- Claude Docs: Dynamic Workflows: https://code.claude.com/docs/en/workflows
- Claude Blog: Introducing Dynamic Workflows: https://claude.com/blog/introducing-dynamic-workflows-in-claude-code
- Claude Blog: A Harness for Every Task: https://claude.com/blog/a-harness-for-every-task-dynamic-workflows-in-claude-code
- Anthropic: Building Effective Agents: https://www.anthropic.com/engineering/building-effective-agents

关键判断：

```text
CC Dynamic Workflow 不是“Plan Mode 的一个卡片”
也不是“多开几个 subagent”
而是“把控制流从模型上下文移到一个可执行、可查看、可恢复的 orchestration artifact”
```

这点和 Hive 现有 Workflow 的设计主旨一致，只是 CC 的 artifact 是 script，Hive 的 artifact 是受限 JSON definition。

## 3. 本地 CC / FreeCode / Codex 源码事实

### 3.1 CC / FreeCode

本地源码能看到 feature-gated workflow 入口，但没有完整 WorkflowTool 实现体。

代码事实：

- `claude-code-org/src/tools.ts` 里 `feature('WORKFLOW_SCRIPTS')` 才加载 `./tools/WorkflowTool/bundled/index.js` 和 `./tools/WorkflowTool/WorkflowTool.js`。
- 当前 checkout 没有 `src/tools/WorkflowTool/` 实现目录。
- `free-code-main/src/tools/WorkflowTool/` 只有 `constants.ts`。

因此当前本地 baseline 不能作为完整 CC dynamic workflow source。我们只能用官方文档、博客、公开复刻项目和本地 feature gate 入口来交叉判断。

### 3.2 Ultraplan 不是 Dynamic Workflow

本地 `ultraplan` 是远程高级 Plan Mode bridge：

- 启动 Claude Code on the web remote session。
- 设置 `permissionMode: 'plan'` 和 `ultraplan: true`。
- 轮询远程 `ExitPlanMode` 工具结果。
- 用户可选择 remote execute 或 teleport plan 回本地。

它解决的是“高级计划阶段在云端跑”，不是“本地 dynamic workflow runtime”。所以不能把 Ultraplan 当作 workflow 对标实现。

### 3.3 Codex

Codex 侧没有等价 dynamic workflow runtime。Codex 的强项在 Plan Mode collaboration contract、plan streaming、`update_plan` checklist 和严格模式边界。

对 Hive 的启发：

- Plan Mode 必须是 runtime/collaboration mode，不只是 prompt。
- 用户可见 plan 和机器执行 contract 要分层。
- `update_plan`/Work Ledger 是认知脚手架，不是 workflow execution。
- workflow 的执行必须在 Plan Mode 确认后，绑定 exact artifact/hash。

## 4. 开源实现与可借鉴项目

### 4.1 最接近 CC 复刻：hermes-dynamic-workflows

公开项目 `lingjiuu/hermes-dynamic-workflows` 是目前最接近 CC Dynamic Workflows 的复刻：

- 模型现场写 sandboxed Python script。
- runtime 注入 `agent()`、`parallel()`、`pipeline()`、`workflow()`、`args`、`budget` 等受控全局。
- 支持 persisted script、journal、subagent transcripts、dashboard、launch approval。
- 支持 1000 subagent 上限和并发上限。
- child agents 可配置 toolsets、model、worktree isolation。

参考：

- GitHub: https://github.com/lingjiuu/hermes-dynamic-workflows
- Technical doc: https://github.com/lingjiuu/hermes-dynamic-workflows/blob/HEAD/TECHNICAL.md

判断：

```text
值得参考 API 和运行模型
不建议直接照搬“模型写 Python/JS script 并 exec”的安全模型
```

Hive 多租户、RLS、审计、权限、企业治理要求更高，应继续坚持 structured definition + compiled interpreter。

### 4.2 LangGraph

LangGraph 的 `Send` API 和 orchestrator-worker 模式证明了一点：动态 fanout 不需要任意脚本才能表达。LLM 可以先生成 structured plan，runtime 再根据计划动态创建 worker nodes。

可借鉴：

- structured output 生成 worker sections。
- `Send` 动态创建 worker。
- shared state 收集所有 worker output。
- synthesizer 做 deterministic barrier merge。

参考：https://docs.langchain.com/oss/python/langgraph/workflows-agents

### 4.3 Magentic-One

Magentic-One 的核心是 Orchestrator agent + Task Ledger + Progress Ledger：

- outer loop 创建/修订 plan。
- inner loop 分派 agent、检查 progress。
- 如果多步停滞，更新 Task Ledger 并 replan。

可借鉴：

- dynamic workflow 不只需要 step graph，还需要 progress ledger。
- replan/fork 必须有明确 stall condition。
- lead orchestrator 的事实、猜测、计划、进度应该 durable。

参考：

- https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/magentic-one.html
- https://www.microsoft.com/en-us/research/articles/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/

### 4.4 CrewAI Flows

CrewAI Flows 更偏 event-driven application workflow：

- Flow state。
- start/listen decorators。
- 条件、分支、循环。
- persistence、resume/fork。

可借鉴：

- Flow state 和 typed state。
- persistence/fork semantics。
- event-driven step activation。

参考：https://docs.crewai.com/en/concepts/flows

### 4.5 OpenAI Agents SDK / Swarm

OpenAI 侧更强调 multi-agent orchestration primitives：

- handoffs：专家接管后续对话。
- agents as tools：manager 保持最终答案所有权。
- code orchestration：structured output、chain、parallel、evaluator loop。

对 Hive 的启发：

- Workflow leaf 更像 “agents as tools”，由 Workflow runtime 保持最终控制流所有权。
- Subagent/delegation/handoff 不应和 Workflow 混成一层。

参考：

- https://developers.openai.com/api/docs/guides/agents/orchestration
- https://github.com/openai/swarm

## 5. Hive 当前 Workflow 代码事实

### 5.1 Definition schema

`backend/app/runtime/workflow_definition.py` 已经把 workflow 定义为 serializable structured data：

- 无 eval。
- 无 Jinja。
- 无 Python/JS expression。
- 模板只允许 `{{args.x}}`、`{{steps.<id>.output}}`、`{{item}}` 这种 key lookup。
- step type 包括 `agent_step`、`fanout_step`、`gate_step`、`wait_until_step`、`wait_signal_step`。
- leaf 是 `LeafRef`，类型可为 `explorer`、`worker`、`critic`。

这是 Hive 的核心优势：多租户平台里，workflow artifact 可以由 Agent 生成，但执行仍由平台解释。

### 5.2 Compiler / admission

`backend/app/runtime/workflow_compiler.py` 已有结构校验：

- step type allowlist。
- prior step reference check。
- args reference check。
- leaf catalog binding。
- external/irreversible effects 必须有前置 gate。
- retry 只允许 reversible steps。

`backend/app/runtime/workflow_admission.py` 已有运行准入：

- args schema 校验。
- budget limit。
- fanout item limit。
- max concurrency。
- max leaf calls。
- max wall clock。
- allowed leaves。

这说明 Hive 已具备 dynamic workflow 的安全准入底座。

### 5.3 Runtime

`backend/app/services/workflow_runtime_service.py` 的 `start_run()` 已经：

- 创建 `RuntimeTask(task_type="workflow")`。
- 写 `definition_source`、`definition_hash`、`args_hash`、`confirmed_plan_id`、`definition_json`、`args`。
- 建立 `WorkflowQuota`。
- 审计 `workflow_run_started`。
- 执行同一套 engine。

`resume_run()` 已经从 archived `definition_json` 恢复，并校验 hash integrity。

这说明 Hive 的 runtime 已经比很多开源实现更接近企业级要求。

### 5.4 Tool surface

`backend/app/tools/handlers/workflow.py` 已有：

- `preview_workflow`：编译、admission、confirmation needs、planned leaf calls、budget。
- `start_workflow`：必须绑定 `preview_id` 或 `definition_hash + args_hash`。

但当前 tool description 也暴露一个设计问题：

```text
preview_workflow 的 confirmation notes 是 informational
start_workflow 不会自动进入 Plan Mode
```

这在普通 workflow 场景可接受，但 dynamic workflow 需要更强的 Plan Mode handoff：

```text
dynamic candidate approval 必须绑定 exact definition/hash/args/budget/scope
```

### 5.5 Registered lifecycle

`backend/app/services/workflow_definitions.py` 已有：

- immutable versions。
- `draft -> active -> deprecated | revoked`。
- visibility 和 executability 分离。
- agents 只能 create draft。
- activation 需要 human approver。
- registered version + patch 可以 fork 成 ephemeral definition。

`backend/app/services/workflow_promote_suggestions.py` 已有：

- 按 completed ephemeral runs 的 `definition_hash` 聚合。
- 超过阈值后建议 promote。
- 只 observe/propose，不自动注册。

这是 dynamic workflow “成功后固定下来”的雏形。



```text
plan
  -> explore fanout
    -> critic
      -> synthesize
```

这不是 dynamic workflow。它只是在 generic workflow runtime 上注册了一个固定 product template。


```text
它没有动态设计候选 workflow
没有根据任务生成最优研究 harness
没有多候选对抗评审
没有失败后的 workflow mutation/fork
没有从成功 run 质量证据中自动建议固化
```

## 6. Gap Matrix

| 能力 | CC Dynamic Workflow | Hive 当前状态 | 差距 |
| --- | --- | --- | --- |
| 现场生成 workflow | Claude 写 script | Agent 可传 JSON definition | 缺少 first-class designer/candidate protocol |
| 执行 artifact | JavaScript script | JSON definition | Hive 更安全，但表达力需要补 dynamic primitives |
| 多候选方案 | 可通过 workflow/tournament 组织 | 无固定协议 | 缺 candidate set、scoring、selection |
| 对抗验证 | 官方核心 pattern | critic leaf 可表达 | 缺 workflow-level verifier/score 规范 |
| Loop until done | script 可以 loop | JSON DSL 目前不开放任意 loop | 需要受限 loop/replan 机制，不能开放任意代码 |
| 状态保存 | script variables + runtime state | RuntimeTask + journal + step/leaf records | 底座已有，需让 dynamic layer 写入设计/评分状态 |
| Resume | same session resumable | archived definition + hash resume | 底座较强 |
| Budget | workflow budget/cost | quota + admission | 底座已有，但 dynamic planning 也要预算 |
| Promotion | save workflow | promote suggestions by repeated hash | 需要从 count 升级为 quality evidence |
| UX | raw script、workflows view | frontend workflow view 不完整 | 需要 candidate preview、progress、failure/replan UI |
| Plan Mode | approval before run | Plan Mode + preview/start 分散 | 需要 exact artifact approval contract |

## 7. 目标架构

### 7.1 新增层：DynamicWorkflowDesigner

新增服务边界：

```text
DynamicWorkflowDesigner
  input:
    objective
    plan_mode_context
    constraints
    source inventory
    allowed leaves/tools
    budget envelope
    success criteria
    risk policy

  output:
    DynamicWorkflowProposal
      candidates[]
      critiques[]
      selected_candidate
      selection_rationale
      preview_binding
      approval_contract
```

它不执行 workflow，只产出可编译、可预览、可批准的 workflow artifacts。

### 7.2 Candidate schema

建议新增内部 schema，不直接等同 `WorkflowDefinition`：

```yaml
proposal_id: uuid
objective: string
scope:
  included: []
  excluded: []
constraints:
  max_tokens: int
  max_wall_clock_seconds: int
  max_leaf_calls: int
  allowed_effects: []
success_criteria: []
candidates:
  - candidate_id: string
    strategy: "fanout_synthesize | adversarial_review | tournament | loop_until_done | staged_migration | hybrid"
    definition: WorkflowDefinition
    expected_strengths: []
    expected_risks: []
    estimated_cost:
      leaf_calls: int
      tokens: int
      wall_clock_seconds: int
    required_approvals: []
critiques:
  - candidate_id: string
    reviewer: "coverage | security | cost | evidence | adversarial"
    pass: boolean
    findings: []
    required_changes: []
selected_candidate_id: string
selection_rationale: string
```

### 7.3 Approval contract

Dynamic workflow 的批准必须绑定 exact artifact：

```yaml
approval_contract:
  proposal_id: uuid
  selected_candidate_id: string
  definition_hash: sha256
  args_hash: sha256
  budget_tokens: int
  max_leaf_calls: int
  effects_summary: []
  allowed_leaves: []
  expires_at: timestamp
```

如果 definition、args、budget、scope、allowed leaves 任一变化，必须重新 preview 和重新确认。

### 7.4 Execution path

目标执行路径：

```text
Plan Mode
  -> propose_dynamic_workflow
    -> generate candidates
    -> critique candidates
    -> select candidate
    -> preview_workflow
    -> exit_plan_mode with approval_contract
      -> user approves
        -> start_workflow from exact preview/hash
          -> WorkflowRuntimeService.start_run
```

注意：

- `propose_dynamic_workflow` 和 `preview_workflow` 可在 Plan Mode 使用。
- `start_workflow` 在 Plan Mode 内继续禁止，必须在确认后执行。

## 8. DSL 表达力改造建议

Hive 不建议开放任意 JS/Python script。应通过受限 DSL 补足动态工作流需要的表达力。

### 8.1 output schema per step

当前 leaf output 约束不够强。应给 `agent_step` / `fanout_step` 加可选 output schema：

```yaml
output_schema:
  type: object
  required: [...]
  properties: {...}
```

用途：

- verifier 可稳定读取。
- synthesizer 可少吃自由文本噪音。
- promotion/eval 可评分。

### 8.2 verifier step type

当前 critic 只是 leaf type，不是 workflow-level contract。建议新增：

```yaml
type: "verify_step"
target_step: "explore"
verification_strategy: "adversarial | evidence_check | schema_check | test_check | vote"
pass_threshold: ...
```

或者 v1 先不新增 step type，而是在 candidate schema 里要求 `critic` leaf 输出统一 schema。

### 8.3 bounded loop / replan

CC script 的 `loop until done` 很强，但 Hive 不能开放任意 while。建议引入受限 loop：

```yaml
type: "repeat_step"
body: [...]
max_iterations: 3
stop_condition:
  field: "steps.verify.output.remaining_findings"
  op: "eq"
  value: 0
```

硬约束：

- 必须有 `max_iterations`。
- 每轮都计入 max leaf calls 和 token budget。
- stop_condition 只能引用 args/prior outputs 的 structured fields。
- external/irreversible effects 不允许在 repeat body 中自动重试。

### 8.4 tournament pattern

支持生成多个候选答案/方案并 pairwise judge：

```yaml
type: "tournament_step"
leaf: planner
attempts: 5
judge_leaf: critic
rubric: ...
winner_schema: ...
```

也可以先用现有 fanout + critic + synthesize 表达，不急着新增 step type。

### 8.5 worktree isolation

CC dynamic workflow 支持 subagents 自己 worktree。Hive 需要把这个变成 leaf policy：

```yaml
leaf:
  name: migration_worker
  type: worker
  isolation: "read_only | workspace | worktree | sandbox"
```

执行仍由 subagent runtime 决定，不由 workflow DSL 直接操作 git。



### 9.1 Built-in registered template

保留一个稳定模板：

```text
  -> scope/classify
  -> plan source angles
  -> search/fetch fanout
  -> claim extraction
  -> adversarial verification
  -> synthesis
  -> citation audit
```

适合普通研究任务。

### 9.2 Dynamic research workflow

对于复杂研究任务，走 dynamic generation：

```text
question
  -> classify research type
  -> generate candidate research harnesses
    A: source-angle fanout
    B: hypothesis/refutation
    C: stakeholder-perspective tournament
  -> critic chooses harness
  -> preview and approve
  -> run
```


## 10. 产品与 UX 要求

### 10.1 用户看到的不是内部工具名

用户可见层：

```text
目标
范围
工作流策略
预计 worker 数
预计成本/时间
风险和确认点
输出物
```

机器层：

```text
definition_json
definition_hash
args_hash
leaf names
runtime_task_id
workflow_ref
artifact paths
```


### 10.2 Workflow preview UI

需要展示：

- candidate list。
- selected strategy。
- phase list。
- expected leaf calls。
- budget。
- required approvals。
- verifier plan。
- raw JSON definition 展开查看。

### 10.3 Workflow run UI

需要展示：

- phase progress。
- per-step status。
- per-leaf status。
- failed/skipped/timed out packets。
- token usage。
- verifier findings。
- final artifact。
- resume/retry/fork actions。

### 10.4 Promotion UI

当同类 ephemeral workflow 多次成功：

- 不只显示 “run_count >= threshold”。
- 还要显示 success rate、avg cost、last failures、manual edits、critic pass rate。
- 用户可以保存为 template、改名、限定 agent/org scope、设置 call policy。

## 11. 实施路线

### P0 文档和 contract 固化

- 本文档作为目标设计入口。
- 更新 `docs/workflow-source-capability.md`，加入 dynamic workflow 章节和本设计链接。

### P1 Proposal schema + service skeleton

- 新增 `DynamicWorkflowProposal` schema。
- 新增 `DynamicWorkflowDesigner` service。
- 只生成 candidates，不执行。
- 测试：模型输出非法 definition 时必须被 compile/admission 拦截。

### P2 Plan Mode 集成

- Plan Mode 允许 `propose_dynamic_workflow` 和 `preview_workflow`。
- Plan Mode 禁止 `start_workflow`。
- `exit_plan_mode` 绑定 approval contract。
- 测试：definition/args/hash 变更后旧 approval 不可用。

### P3 Candidate critique

- 加 workflow candidate critic preset。
- 至少覆盖 cost/security/coverage/evidence 四类 critique。
- 测试：critic 要求修改时不得直接 start。

### P4 Runtime metadata

- `RuntimeTask.metadata_json` 增加 proposal/candidate linkage。
- 记录 selected rationale、critique summary、approval contract。
- promote suggestion 读取 proposal metadata。

### P5 DSL 表达力增量

优先顺序：

1. step output schema。
2. verifier output schema。
3. bounded repeat。
4. tournament shorthand。
5. worktree isolation policy。

每一项都必须先有 compile/admission/runtime tests。


- 保留 fixed registered template。
- 复杂任务走 dynamic workflow designer。
- 用户可见 plan 不出现 internal tool/file/path。

### P7 Frontend

- candidate preview。
- workflow run progress。
- failure/replan/fork。
- promotion proposal。

## 12. 不做什么

第一轮不做：

- 不开放任意 JavaScript/Python script 执行。
- 不让 workflow 自己直接访问 filesystem/shell/network。
- 不让 child agent 继承全部父工具。
- 不让 Work Ledger 驱动 Workflow 控制流。
- 不把 promotion 自动变 active，必须 human approval。
- 不为了“像 CC”牺牲 Hive 的 RLS、审计、租户、权限和可恢复性。

## 13. 需要立刻修正的产品心智

当前用户描述的“Plan Mode + Workflow + Sub-agent 组合拳”是正确的，但还要补一层：

```text
Plan Mode: 决定要不要做、做什么、批准什么
Dynamic Workflow Designer: 设计怎么编排，生成和评审候选 workflow
Workflow Runtime: 按批准的 artifact 确定性执行
Sub-agent: 在隔离上下文里完成 leaf work
Promotion: 把成功的 ephemeral workflow 固化成 reusable template
```

一句话：

```text
Dynamic Workflow 不是替代 Workflow。
Dynamic Workflow 是 Workflow 上方的“动态设计、评审、试跑、固化”闭环。
```

## 14. 下一步建议


推荐下一步代码切口：

```text
P1: DynamicWorkflowProposal schema + DynamicWorkflowDesigner skeleton
P2: Plan Mode approval contract exact hash binding
P3: candidate critic preset
```

每个切口都必须 TDD：

- schema validation tests。
- compiler/admission rejection tests。
- Plan Mode approval hash mismatch tests。
- no internal tool leak visible plan tests。
- workflow runtime proposal metadata tests。

# Deep Research 原生组合重构与 Plan Mode 对齐审计（2026-06-23）

> 状态：讨论稿 / docs-only。本文只记录当前判断、代码证据和目标设计，不包含实现改动。
> 术语说明：用户口头提到的 “play mode” 本文按 **Plan Mode** 处理。

## 1. 结论先行

Deep Research 不应该继续表现为一组模型可直接调用的专用工具包。它应该是一个由三类原生能力组合出来的产品级模板：

```text
Plan Mode 负责制定和确认计划
  -> Workflow 负责确定性编排和恢复
    -> Sub-agent 负责隔离上下文、并行调研、批评和综合
```

当前代码已经有这三块能力，但 Deep Research 还没有真正收敛成这个清晰模型。它现在处在“半 workflow-native、半专用工具、半 Plan Mode handoff”的混合状态，所以会出现用户看到的现象：计划卡片显示失败，但普通对话又继续说“计划已生成”；用户可见计划里泄漏了 `deep_research_*` 这类内部工具细节，随后被 Plan Mode validator 拦截。

Plan Mode 本身也不能说已经完全对齐 CC 和 Codex。它已经接近 CC 的“进入计划、只读探索、提交计划、用户确认后执行”模型；但距离 Codex 的“严格协作模式、用户命令不改变模式、计划输出和普通 assistant message 分流渲染”还有明显差距。要先把 Plan Mode 的语义和 UX 收干净，再谈 Deep Research。

## 2. 当前代码事实

### 2.1 Hive Plan Mode

当前 Hive Plan Mode 的核心事实：

- `backend/app/services/web_chat_runtime.py:1559` 会创建 `PlanModeState`，并把状态写入 session context 与 metadata mirror。
- `backend/app/services/web_chat_runtime.py:1571` 会把 Deep Research 请求识别成 `handoff_target="deep_research"`，这是产品专用分支。
- `backend/app/services/web_chat_runtime.py:1714` 的入口逻辑明确：只有显式 Plan Mode 才 materialise awaiting plan，普通 schedule/monitor intent 只推荐。
- `backend/app/services/web_chat_runtime.py:1872` 会恢复已确认但排队的 Plan Mode handoff。
- `backend/app/services/web_chat_runtime.py:2314` 在调用主 Agent loop 前设置 interactive Plan Mode context，`plan_mode_submitted` 后清除。

Plan Mode service 的核心事实：

- `backend/app/services/plan_mode_service.py:208` 说明当前 plan 是 caller-owned structured fill。
- `backend/app/services/plan_mode_service.py:210` 明确 substantive plan 应由 agent 在 Plan Mode 中编写，并通过 `exit_plan_mode` 提交。
- `backend/app/services/plan_mode_service.py:338` 明确旧的 isolated RPC “workflow planner” 已移除，provenance 是 agent-authored。
- `backend/app/services/plan_mode_service.py:449` 有用户可见计划泄漏检查，会拒绝 `deep_research_start/check/cancel/export`、`runtime_artifacts/`、`work_ledger.json`、内部 json/jsonl 文件名等。

这个方向是对的：Plan Mode 已经从“平台替 Agent 填表”转向“Agent 在主循环里写计划，平台只做 envelope、hash、确认和 handoff”。

### 2.2 Hive Workflow

当前 Workflow 已经是原生能力，不是 Deep Research 的私有执行器：

- `backend/app/tools/handlers/workflow.py:159` 暴露 `start_workflow`。
- tool description 明确：只有当顺序、fanout、gate、预算本身是需求时才使用 workflow；先 `preview_workflow`，再显式确认后 `start_workflow`。
- `backend/app/services/workflow_launch.py:183` 的 `start_ephemeral_workflow_for_agent()` 是通用启动路径。
- `backend/app/services/workflow_launch.py:206` 会构造 `SubagentSpawnContext`。
- `backend/app/services/workflow_launch.py:215` 用 `build_subagent_leaf_executor()` 生成 leaf executor。
- `backend/app/services/workflow_launch.py:224` 最终调用 `WorkflowRuntimeService.start_run()`。

这说明 Workflow 和 Sub-agent 的关系已经是正确方向：Workflow 做 deterministic orchestration，leaf 执行仍走真实 subagent，从而继承治理、租户、审计和预算边界。

### 2.3 Deep Research 当前形态

Deep Research 已经部分 workflow-native：

- `backend/app/services/deep_research/workflow_definition.py:28` 构造 `deep_research.v1` workflow definition。
- definition 是 `plan -> explore fanout -> critic -> synthesize`。
- `backend/app/services/deep_research/workflow_definition.py:152` 会 ensure tenant-scoped workflow definition。
- `backend/app/services/deep_research/workflow_definition.py:201` 会启动 workflow run。
- `backend/app/services/deep_research/workflow_definition.py:248` 会把 definition source pin 到 `registered:{name}:v{version}:{hash}`。

Deep Research leaf 也已经通过 leaf preset 接入真实 subagent：

- `backend/app/services/workflow_leaf_presets.py:3` 定义 leaf preset 是系统侧 capability injection。
- `backend/app/services/workflow_leaf_presets.py:10` 明确 preset 包装真实 `spawn_subagent`，不替代它。
- `backend/app/services/deep_research/leaf_presets.py:34` 限制 explorer worker 只允许 web research tools。
- `backend/app/services/deep_research/leaf_presets.py:40` 排除 `deep_research_*`、delegation、write/edit/delete 等递归和副作用工具。
- `backend/app/services/deep_research/leaf_presets.py:683` 注册 planner/explorer/critic/synthesizer 四类 leaf preset。

但是 Deep Research 仍保留产品专用 Plan Mode handoff：

- `backend/app/services/deep_research/plan_mode.py:18` 定义 `DEEP_RESEARCH_HANDOFF_TARGET = "deep_research"`。
- `backend/app/services/deep_research/plan_mode.py:57` 直接构造 Deep Research 专用 plan fill。
- `backend/app/services/deep_research/plan_mode.py:167` 在 plan_json 里写入 `handoff.target = deep_research`。
- `backend/app/services/deep_research/plan_mode.py:173` 还写入专用 `deep_research` contract。
- `backend/app/services/deep_research/plan_mode.py:201` 的 handoff handler 最终又调用 `start_deep_research_background_run()`。

所以它不是纯粹的 “confirmed plan -> generic workflow handoff”。它仍然是 “confirmed plan -> deep_research 专用 handler -> deep_research background run -> workflow path / fallback path”。

## 3. 为什么会出现“计划失败又成功”

从截图和当前代码可以推导出最可能的链路：

1. 用户发起 Deep Research。
2. Web chat 识别为 Deep Research intent，进入 interactive Plan Mode，并设置 `handoff_target=deep_research`。
3. Agent/handler 生成 structured fill。
4. fill 的用户可见字段中出现 `deep_research_run` / `deep_research_start` 等内部工具细节。
5. `PlanModeService._visible_plan_leak_errors()` 拒绝该 plan，于是 plan row 进入 `planning_failed`。
6. 同一轮对话里，Agent 普通文本仍然可以继续流式输出“计划已生成，等待确认”。
7. 前端同时渲染 plan card 和普通 assistant message，于是用户看到“计划失败又成功”。

这不是一个单点 UI 文案问题。它暴露的是 Plan Mode 里缺少明确的双层 contract：

- **用户可见计划**：只讲目标、范围、步骤、风险、成本、确认点，不出现内部 tool 名、artifact path、runtime file 名。
- **机器执行契约**：可以包含 `workflow_ref`、definition hash、args hash、leaf plan、预算、artifact contract，但不直接出现在用户可见计划正文中。

当前 Deep Research 把这两层揉在一个 `plan_json` 里，再靠 visible leak validator 做事后拦截。这个结构天然容易制造“模型说成功，平台判失败”的分裂。

## 4. CC / FreeCode 基线

本地 FreeCode / CC 基线体现的是 permission-mode 风格 Plan Mode：

- `EnterPlanModePermissionRequest.tsx` 中的 UI 文案是：进入 Plan Mode 后 Claude 会探索代码库、识别现有模式、设计实现策略，并提交计划等待批准。
- 同一入口明确告诉用户：批准计划前不会做代码修改。
- `ExitPlanModePermissionRequest.tsx` 在用户批准退出 Plan Mode 后，会把 `currentPlan` 作为 “Implement the following plan” 注入新的实现上下文。
- 退出时还保留 transcript hint，提示实现阶段可以回读 Plan Mode 之前的完整 transcript。
- `AskUserQuestionPermissionRequest/QuestionView.tsx` 在 Plan Mode 内显示 planning file，并允许用户回答问题、继续聊、或跳过 interview 直接计划。

因此 CC 的 Plan Mode 不是“某个工具的确认卡”，而是一段受权限约束的协作阶段：

```text
进入 Plan Mode
  -> 只读探索 / 提问 / 写 plan
  -> ExitPlanMode 提交计划
  -> 用户批准
  -> 清上下文或保留上下文，进入实现阶段
```

Hive 对齐点：

- 有显式进入。
- 有 Plan Mode runtime state。
- 有 `ask_user_question` / `exit_plan_mode`。
- 有 plan file path。
- 有 agent-authored plan。
- 有 confirmed plan ledger/hash/version/handoff。

Hive 缺口：

- Deep Research 目前不是普通 “计划 -> 实现” handoff，而是产品专用 `deep_research` handoff。
- 用户可见 plan 和机器执行 contract 没完全分层。
- plan card 状态和 assistant 普通文本状态没有统一成单一 truth。
- CC 的 transcript handoff 是实现阶段核心路径；Hive 当前更多依赖 plan_json/handoff_payload，对完整 transcript 回读的产品路径还不够明确。

## 5. Codex 基线

Codex 的 Plan Mode 更强调“协作模式”本身，而不是某个确认工具：

- `codex-rs/collaboration-mode-templates/templates/plan.md` 明确：Plan Mode 会持续到 developer message 显式结束。
- 用户的语气、命令式语言或“现在执行吧”不能改变 Plan Mode；仍要当成“规划执行”处理，而不是真的执行。
- `update_plan` 是 checklist/progress/TODO 工具，和 Plan Mode 进入/退出无关。
- Plan Mode 允许只读探索和非 mutating 检查，但不能做会改变 repo tracked state 或执行计划的动作。
- 最终计划用 `<proposed_plan>` block 输出，客户端可特殊渲染。

Codex runtime 还有一层 UI/streaming 处理：

- `codex-rs/core/src/session/turn.rs:1145` 定义 plan-mode streaming state。
- `codex-rs/core/src/session/turn.rs:1275` 在 Plan Mode 中延迟 agent message start，避免 plan-only 输出变成空 assistant message。
- `codex-rs/core/src/session/turn.rs:1547` 对 Plan Mode 中的 agent message 做专门完成逻辑。
- `codex-rs/core/src/session/turn.rs:1749` 从 `turn_context.collaboration_mode.mode == ModeKind::Plan` 判断 Plan Mode。

这说明 Codex 不只是在 prompt 上说“不要执行”，它还有渲染和 stream protocol 层的 Plan Mode 分流。

Hive 对齐点：

- Plan Mode 和 Work Ledger / TODO 已经分层；`track_todo` 不应等同进入 Plan Mode。
- Plan Mode 已经允许只读探索和非常窄的 helper lane。
- `start_workflow` 在 Plan Mode 内应继续被禁止，`preview_workflow` 才是计划期允许动作。

Hive 缺口：

- Hive 的 Plan Mode 退出/进入主要是产品 runtime state，不是 Codex 那种更硬的 collaboration-mode contract。
- 用户可见 `<proposed_plan>` 等计划渲染协议没有成为统一输出格式；当前是 plan card + markdown path +普通 assistant message 混合。
- 没有 Codex 那种 plan-mode stream parser 来阻止 plan-only 内容同时落成普通 assistant message。
- Deep Research 会把 Plan Mode 切到产品专用 target，这和 Codex 的通用协作模式不一致。

## 6. Plan Mode 当前对齐判断

结论：**不能宣称已完全对齐 CC 和 Codex。**

更准确的状态是：

| 维度 | 当前判断 | 说明 |
| --- | --- | --- |
| CC 进入/退出计划语义 | 部分对齐 | 有显式进入、只读规划、`exit_plan_mode`、计划确认；但 Deep Research 有专用 handoff 分支。 |
| CC 计划后实现 handoff | 部分对齐 | 有 confirmed plan ledger；但还没有统一成 “plan markdown + transcript hint + implementation turn” 的核心路径。 |
| CC 只读 helper | 基本对齐但需收窄 | 当前允许 explorer/critic、preview/check 是合理 Hive delta；必须继续禁止 worker/background/memory writeback/delegation/start_workflow。 |
| Codex collaboration mode | 未完全对齐 | Codex 是 developer-controlled mode；Hive 是 web runtime state + tool/context metadata。 |
| Codex Plan Mode vs TODO | 基本对齐 | Hive 已把 Work Ledger/Progress Ledger 和 Plan Mode 分开。 |
| Codex plan rendering/stream isolation | 明显缺口 | Hive 仍会让普通 assistant 文本和 plan card 同时表达不一致状态。 |
| 用户可见计划与执行契约分层 | 明显缺口 | Deep Research 的 visible leak validator 正在替架构分层兜底。 |

所以 Plan Mode 的下一步不是继续给 Deep Research 补 prompt，而是先把 Plan Mode contract 固化：

```text
Plan Mode visible output = human plan only
Plan Mode execution contract = hidden, typed, hash-pinned handoff
Plan Mode stream/UI = one status source, no duplicate success/failure narrative
Plan Mode allowed actions = non-mutating exploration + preview only
```

## 7. Deep Research 目标设计

### 7.1 Deep Research 的定位

Deep Research 应该是一个产品级 workflow template，而不是底层原生能力：

```text
Base capability:
  - Plan Mode
  - Workflow
  - Sub-agent
  - Work Ledger
  - Web research tools
  - Artifact delivery

Product template:
  - deep_research.v1
  - default lanes
  - source policy
  - evidence ledger shape
  - synthesis/critic quality gates
  - report artifact contract
```

模型默认工具表面不应该继续暴露 `deep_research_run/start/check/cancel/export` 作为主路径。保留它们可以作为兼容/admin/internal surface，但默认产品体验应该是：

```text
用户提出深度研究需求
  -> Agent 进入或请求 Plan Mode
  -> Agent 写用户可见研究计划
  -> 平台抽取/保存隐藏 execution_contract
  -> 用户确认计划
  -> PlanModeService handoff 到 generic workflow launcher
  -> workflow runtime 按 pinned definition/hash/args 执行
  -> leaves 调真实 spawn_subagent
  -> final artifact 走统一 chat artifact / workspace packet
```

### 7.2 Plan Mode 产物结构

计划应该拆成两层：

```json
{
  "visible_plan": {
    "title": "...",
    "objective": "...",
    "steps": [],
    "success_criteria": [],
    "risk_assessment": {},
    "estimated_cost": {},
    "stop_conditions": []
  },
  "execution_contract": {
    "type": "workflow",
    "workflow_ref": "deep_research.v1",
    "workflow_definition_version": 1,
    "workflow_definition_hash": "...",
    "args_hash": "...",
    "args": {},
    "artifact_contract": {
      "canonical": "report.md",
      "derived_formats": []
    },
    "budget": {},
    "leaf_policy": {}
  }
}
```

用户只看到 `visible_plan`。平台和 runtime 使用 `execution_contract`。validator 不应该再靠扫描用户可见文本来猜测是否泄漏；schema 本身应该让泄漏不可能发生。

### 7.3 Workflow 运行职责

Workflow 应该负责这些事情：

- 绑定 workflow definition version/hash。
- 校验 args/hash 与确认时一致。
- 控制 step 顺序、fanout concurrency、retry、gate、quota。
- 记录 workflow run、step journal、leaf journal。
- 恢复 crash/restart 后未完成 run。
- 给每个 leaf 调用真实 `spawn_subagent`。
- 收敛 artifacts 到统一 delivery contract。

Workflow 不应该负责这些事情：

- 决定用户到底想研究什么。
- 写用户可见计划。
- 在 leaf definition 里塞任意 prompt/tool injection。
- 绕过 Plan Mode 或 capability gate。
- 把 Deep Research 的产品状态伪装成通用 Workflow 状态。

### 7.4 Sub-agent 运行职责

Sub-agent 应该负责：

- planner：把确认后的研究目标拆成可执行 lanes。
- explorer：只用 web/source 工具，产出 source-bound digest。
- critic：对来源覆盖、矛盾、过度推断、缺口做对抗审查。
- synthesizer：基于 ledger 和 critique 写最终 report。

Sub-agent 不应该：

- 再调用 `deep_research_*`。
- 自己启动 workflow。
- peer delegate 给数字员工。
- 写 durable memory/skill/soul。
- 直接写最终 workspace artifact，除非通过 workflow leaf postprocess 受控落盘。

## 8. 推荐改造顺序

### A. 先固化 Plan Mode contract

不先改 Deep Research 主逻辑，先定义并测试：

- Plan Mode visible plan schema。
- hidden execution contract schema。
- `exit_plan_mode` 输出如何拆层。
- planning_failed 时 assistant 普通文本不能继续说“计划已生成”。
- Plan Mode 中 `start_workflow`、background subagent、delegation、memory writeback 继续禁止。

验收标准：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/tools/test_plan_mode_policy.py tests/services/test_plan_mode_service.py -q
```

### B. 把 Deep Research 降级为 workflow template

目标：

- `deep_research.v1` 是 registered workflow template。
- Plan Mode handoff target 改为 generic `workflow`。
- `execution_contract.workflow_ref = deep_research.v1`。
- `definition_version/hash` 和 `args_hash` 被 confirmed plan pin 住。
- Deep Research 专用 handler 只保留兼容入口，不作为默认模型工具。

### C. 收敛 UI 状态源

目标：

- plan card status 是 Plan Mode 的唯一真相源。
- assistant 普通 message 不再重复宣布 plan success/failure。
- 如果 plan validation 失败，普通文本只解释“计划未通过校验，需要修改”，不能同时展示“等待确认”。
- Deep Research workflow run 的进度挂在 confirmed plan / workflow run 下，不再和 planning card 混为一层。

### D. 做 live trace 验收

必须用真实 prod/eval session 看：

- 是否进入 Plan Mode。
- Plan Mode 内是否只发生只读探索/preview。
- `exit_plan_mode` 是否生成 visible plan + hidden contract。
- 用户确认后是否启动 workflow。
- workflow leaf 是否真实调用 subagent。
- final artifact 是否回到同一 chat session artifact surface。

## 9. 需要补的测试清单

后续实现前，建议先写这些 red tests：

1. `planning_failed` 不得伴随 “plan generated / waiting confirmation” assistant status。
2. 用户可见 plan 中出现 `deep_research_start` 时应失败；但 hidden execution contract 中引用 `workflow_ref=deep_research.v1` 应通过。
3. Deep Research Plan Mode handoff 应产生 generic workflow contract，而不是 `handoff.target=deep_research`。
4. confirmed plan 启动 workflow 时必须校验 `definition_hash` 和 `args_hash`。
5. Plan Mode 内 `start_workflow` 必须被拒绝，`preview_workflow` 可以执行。
6. Plan Mode 内 `spawn_subagent` 只能允许同步 read-only explorer/critic，禁止 worker/background/memory writeback。
7. Deep Research leaf preset 必须排除 `deep_research_*`、delegation 和 write/edit/delete 工具。
8. Workflow leaf executor 必须走真实 `spawn_subagent`，不能回退到 Deep Research 私有 worker loop。
9. final report artifact 必须绑定 workflow_run_id、plan_id、chat_session_id。

## 10. 最终判定

Deep Research 当前“不能用”的根因不是某个研究 Agent 的能力差，而是组合边界混乱：

- Plan Mode 在承担确认边界，同时又被 Deep Research 专用 handoff 污染。
- Workflow 已经具备原生能力，但 Deep Research 还没有完全把它当唯一执行控制面。
- Sub-agent 已经是正确 leaf 执行内核，但 Deep Research 的产品工具表面还在绕出一条专用叙事。
- 前端同时展示 plan card 和普通 assistant message，导致失败/成功叙事冲突。

要重做 Deep Research，优先顺序应该是：

1. 先把 Plan Mode 对齐 CC/Codex 的 contract 收稳。
2. 再让 Plan Mode 产出隐藏 workflow execution contract。
3. 再让 Workflow 统一驱动 Sub-agent。
4. 最后把 Deep Research 作为 workflow template / product preset 重新接回用户体验。

在完成这些之前，不应宣称 Deep Research 是一个可靠的一等能力，也不应宣称 Plan Mode 已完整对齐 CC/Codex。

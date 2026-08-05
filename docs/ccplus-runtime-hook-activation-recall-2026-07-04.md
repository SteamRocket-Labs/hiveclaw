# CCPlus Runtime Hook Activation 召回设计

日期：2026-07-04  
状态：历史讨论稿；Knowledge hint 与统一 Q/K/V Router 方向已退役  
范围：在不改写 CC 核心 runtime 语义的前提下，把 Hive 的记忆召回、Skill 排序、Sub-agent / Workflow / Tool 动态激活，挂到现有 runtime 与 tool-use 生命周期上。

> **2026-07-14 覆盖说明：** 本文是旧方案讨论，不是当前 runtime contract。Personal / Company Knowledge 不生成 KB hint、不 prefetch、不自动进入 dynamic suffix；模型通过 governed search/read tools 自主发现知识。旧统一 Activation Router 也已退役。当前机制见 `docs/hive-native-external-attention-runtime-2026-07-06.md` 与 `docs/personal-company-knowledge-tool-boundary-2026-07-10.md`。

## 0. 一句话结论

这条路是对的：Hive 不应该为了 memory recall 重写 CC runtime，而应该在现有 runtime 生命周期上增加一层 **Activation Hook / Attention Router**。

它的职责不是替模型执行工具，也不是绕过 Memory Gate / Platform Gate，而是在合适的 runtime 节点向模型提供一段小而明确的动态提醒：

```text
当前任务可能需要召回哪些记忆？
哪些 Skill / Sub-agent / Workflow / Tool 候选更相关？
哪些内容因为 ACL / sensitivity / tool permission 不应进入候选集？
```

最终执行仍然走原来的模型循环、tool loop、ToolRuntimeService、hook governance、权限门和审计链。

## 1. 为什么这是 CCPlus，而不是破坏 CC

Hive 的底座仍然是 CC / FreeCode 风格的 agent lifecycle：

```text
接受用户输入
→ 持久化 transcript / T0
→ 组装上下文
→ 模型循环
→ 工具循环
→ hook / permission / compaction / stop
→ turn 完成
```

CC 的核心价值是稳定的 runtime 语义：模型自己判断、工具通过 runtime 暴露、hook 在生命周期边界介入、上下文由 runtime 组装。

Hive 的增强点是：

```text
CC runtime
+ Hive Memory / Personal Knowledge / Company Knowledge
+ 权限硬门
+ 动态召回与资产排序
= CCPlus Activation Layer
```

所以这里的设计原则是：

- 不改写模型循环。
- 不把 memory 变成隐藏的自动执行器。
- 不让 hook 直接绕过 tool governance。
- 不把 KB 正文粗暴塞进 frozen prefix。
- 用 hook 给模型动态提示，让模型在正常 tool-use 中选择是否展开。

## 2. 当前 runtime 已经具备的落点

当前代码里已经有一套可用的 hook substrate：

- `USER_PROMPT_SUBMIT`：用户输入已经 durable append，模型循环开始前触发。
- `SESSION_START`：invocation 开始、prompt context 组装时触发。
- `PRE_TOOL_USE` / `POST_TOOL_USE` / `POST_TOOL_FAILURE`：工具执行前后触发。
- `STOP` / `TURN_STOP` / `TURN_ABORT` / `SESSION_END`：turn 或 session 结束边界。
- `SUBAGENT_START` / `SUBAGENT_STOP`：子 agent 生命周期边界。
- `PRE_COMPACTION` / `POST_COMPACTION`：上下文压缩边界。

最关键的是，当前 `USER_PROMPT_SUBMIT` hook 的 `additional_contexts` 已经会进入 `system_prompt_suffix`，而 `system_prompt_suffix` 又进入动态后缀，不进入 frozen prefix。因此它天然适合做轻量召回提醒：

```text
UserPromptSubmit Hook
→ 产出 additional_contexts
→ system_prompt_suffix
→ dynamic prompt suffix
→ 模型本轮可见
```

这点非常重要，因为它满足三个条件：

1. 在模型决定是否召回/调用工具之前发生。
2. 不破坏 prompt cache 的 frozen prefix。
3. 不绕过 tool loop，只是给模型提醒。

基于当前代码的精确约束：

- `USER_PROMPT_SUBMIT` 的 `additional_contexts` 当前已经被消费并进入动态后缀，是第一版最稳入口。
- `SESSION_START` 当前会触发，但返回的 `additional_contexts` 还没有进入 prompt；如果未来要用它做 session 级 activation，需要补这条消费链。
- `PRE_TOOL_USE` 当前适合 block / rewrite / audit；它发生在模型已经选择工具之后，不适合作为首轮工具推荐入口。
- Skill frontmatter hook 当前是 session-scoped 的提醒机制；它不会执行脚本，也不会绕过工具治理。

## 3. Hook 适合做什么，不适合做什么

### 3.1 适合做

Hook 适合做 **Activation Hint**：

```text
这轮任务可能相关的记忆：top-k id + 短理由
这轮任务可能相关的 Personal KB：top-k title/id + 短理由
这轮任务可能相关的 Company KB：已过 ACL 的 top-k title/id + 短理由
这轮任务可能相关的 Skill：top-k skill slug + 触发原因
这轮任务可能适合的 Sub-agent：top-k worker type + 触发原因
这轮任务可能适合的 Workflow：top-k workflow slug + 触发原因
这轮任务可能需要的 Tool：tool_search / search_personal_kb / load_skill / preview_workflow 等候选
```

这些提醒的正确形态应该是“小提示”，不是正文注入：

```text
## Runtime Activation Hints
- Memory: 命中 `self:failure-pattern-ui-overlap`，因为用户要求检查 UI overlap；如相关，先查看对应记忆证据。
- Skill: `frontend-skill` 分数高；如果要改 UI，可先 `load_skill`.
- Personal KB: `kb:doc-123` 可能相关；如果需要正文，用 `search_personal_kb` 展开。
- Sub-agent: 当前任务暂不建议 spawn，原因：单线程上下文足够。
```

### 3.2 不适合做

Hook 不应该做这些事：

- 直接调用工具。
- 直接读取并注入大量 KB 正文。
- 绕过 `ToolRuntimeService.execute()`。
- 绕过 `Memory Gate` / `Platform Gate` 写入 durable memory。
- 把没有 ACL 权限的 Company KB 放进候选提示。
- 替模型做最终决策。
- 把易变的排序结果放进 frozen prefix。

因此，hook 的正确定位是：

```text
提醒模型注意某些候选，而不是代替模型行动。
```

## 4. 生命周期位置：哪里触发召回最合理

### 4.1 `USER_PROMPT_SUBMIT`：主入口

这是最适合做召回提醒的位置。

原因：

- 用户输入已经被接受并持久化。
- 模型还没有开始本轮推理。
- 可以根据 prompt、agent、tenant、session、execution mode 做候选召回。
- `additional_contexts` 已经能进入动态后缀。

建议把第一版 Activation Router 挂在这里。

### 4.2 `PRE_TOOL_USE`：治理和纠偏，不是首轮工具推荐

`PRE_TOOL_USE` 在模型已经选择了某个工具之后触发。因此它更适合：

- block 高风险工具调用。
- rewrite 参数。
- 给工具调用做权限/策略判断。
- 记录“模型选了什么工具”的反馈信号。

它不适合作为“提醒模型第一次该用什么工具”的主入口，因为那个决策已经发生了。

### 4.3 `POST_TOOL_USE` / `POST_TOOL_FAILURE`：反馈回流

工具成功或失败后，hook 可以记录：

- 哪个工具被使用。
- 是否成功。
- 是否产出有用结果。
- 是否造成返工。
- 是否应该提高或降低相关 Skill / Tool / Workflow 的权重。

这部分是动态权重的学习信号，不是本轮最初的召回入口。

### 4.4 `STOP` / `TURN_STOP`：学习与归档

turn 结束时可以沉淀：

- 本轮 activation candidates。
- 实际使用了哪些候选。
- 哪些候选被模型忽略。
- 哪些候选帮助完成任务。
- 哪些候选导致错误或浪费。

这为后续权重、遗忘、归档、Skill 晋升提供反馈。

### 4.5 `SUBAGENT_START`：子 agent 独立上下文

Sub-agent 要保留 CC 风格的 clean specialist 语义。父 agent 的全部 memory 不能泄漏给子 agent。

因此 `SUBAGENT_START` 可以做的是：

- 给子 agent 注入与子任务相关的最小上下文。
- 注入被授权的 artifact refs。
- 注入与子任务相关的 skill hint。

不能做的是：

- 把 host agent 的完整 memory/soul/profile 复制给子 agent。
- 绕过 child session 的权限边界。

## 5. Activation Router 的候选对象

统一候选对象可以覆盖 memory、knowledge、skill、sub-agent、workflow、tool：

```text
ActivationCandidate:
  kind: agent_memory | personal_kb | company_kb | skill | subagent | workflow | tool
  scope: agent | personal | company
  value_ref: memory id / kb doc id / skill slug / subagent id / workflow slug / tool name
  keys: labels / entities / domain / task type / examples / source_refs / trigger phrases
  hard_masks: ACL / sensitivity / allowed_tools / execution_mode / availability
  score_components:
    semantic_relevance
    lexical_relevance
    graph_relevance
    profile_match
    historical_success
    owner_feedback
    recency
    authority
    cost_penalty
    risk_penalty
    decay_penalty
  render_hint: 给模型看的短提示
```

排序过程应该是：

```text
收集候选
→ 权限与安全 hard mask
→ 计算动态分数
→ 按 token / tool budget 截断
→ 渲染成 Hook Additional Context
→ 模型决定是否展开或调用工具
```

这里最重要的规则：

```text
ACL / sensitivity / allowed_tools 是 hard mask，不是 soft weight。
```

没有权限的知识、工具、Sub-agent、Workflow，不能进入候选，更不能通过提示泄露存在性。

## 6. 和 Sparse Attention / MoE 的对应关系

这个设计可以类比为：

```text
Query = 当前用户请求 + session state + agent identity + owner/company context
Key = labels / entities / refs / skill manifest / workflow trigger / subagent profile / ACL scope
Value = 可注入提示、可展开文档、可加载 Skill、可调用工具、可 spawn 的 worker
Mask = ACL / sensitivity / permission / execution mode / safety
Score = 相关性 + 权重 + 成功历史 + 反馈 + 新鲜度 - 成本 - 风险
Output = 动态后缀里的 Activation Hints，或正常 tool-use 里的展开结果
```

Company Knowledge Base 里的权限判断，就是 Sparse Attention 的 mask，也是 MoE Router 的专家选择边界：

```text
只有通过权限与场景条件的专家/知识/工具，才有资格参与排序。
```

## 7. 与 Skill / Tool / Sub-agent / Workflow 的关系

### 7.1 Skill

Skill 不应该平铺加载。随着 Skill 数量增多，必须动态排序。

Hook 可以提示：

```text
本轮 top skill candidates：
1. `frontend-skill`：命中 UI / responsive / overlap。
2. `audit`：命中 review / regression / verification。
3. `pdf`：命中文档解析。
```

模型仍然通过正常的 `load_skill` 加载 Skill，Skill frontmatter hook 也仍然是 session-scoped，不绕过治理。

### 7.2 Tool

Tool ranking 不是替模型调用工具，而是改善工具候选排序。

Hook 可以提示：

```text
如需展开个人知识库正文，使用 `search_personal_kb`。
如需发现 Skill / MCP / deferred tools，使用 `tool_search`。
如需启动确定性流程，先用 `preview_workflow`。
```

真正的工具调用仍由模型发起，进入原工具循环。

### 7.3 Sub-agent

Sub-agent 当前不常触发，本质问题可能不是模型“不想用”，而是候选暴露和触发理由不够明确。

Activation Hook 可以让模型看到：

```text
可选 specialist：
- `code-reviewer`：适合独立审查 diff。
- `researcher`：适合并行查资料。
- `frontend-ux`：适合独立 UI 体验检查。
```

但是否 spawn，仍由模型结合任务复杂度、上下文成本、权限、用户意图决定。

### 7.4 Workflow

Workflow 是确定性 orchestration，不是普通自由工具调用。

Hook 可以在任务明显匹配既有流程时提示：

```text
该任务匹配 `release-checklist` workflow；如用户目标是完整发版，建议先 `preview_workflow`。
```

## 8. 最小版本建议

第一版不要全系统大改，只做一个最小闭环：

```text
USER_PROMPT_SUBMIT
→ Activation Router 生成 top-k hints
→ 进入 dynamic suffix
→ 模型按正常 tool-use 展开
→ POST_TOOL_USE / TURN_STOP 记录使用反馈
```

第一版候选范围建议：

1. Agent Memory recall hint。
2. Skill ranking hint。
3. Tool prior hint（包含可发现的 Knowledge tool schema，但不包含知识正文或自动查询结果）。

Sub-agent 和 Workflow 可以先进入候选格式，但先轻量提示，不强行自动触发。

## 9. 验收标准

这套机制是否有效，不能只看“有没有注入提示”，要看这些指标：

- 模型是否更常在正确场景召回 memory / KB。
- `load_skill` 是否更常加载正确 Skill，而不是依赖平铺 catalog。
- Sub-agent 是否在合理任务上更精确触发。
- 错误工具调用是否下降。
- irrelevant recall 是否下降。
- prompt cache 是否不被破坏。
- ACL leakage 是否为 0。
- 每次 activation 是否可解释：为什么候选出现、为什么被过滤、为什么被排序在前。

## 10. 当前结论

我们现在讨论到的核心点是：

```text
Hive 的记忆和知识系统，不应该作为 CC runtime 的替代品存在；
它应该作为 CC runtime 上的一层动态注意力系统存在。
```

Hook 是最合适的接入方式之一，尤其是 `USER_PROMPT_SUBMIT`：

```text
它在模型推理前发生，
它已经能把 additional_contexts 放入动态后缀，
它不会破坏 frozen prefix，
它不绕过工具治理，
它可以自然承载 memory / skill / sub-agent / workflow / tool 的动态候选排序。
```

这就是一个真正有价值的 CCPlus 点：  
**CC 负责稳定 runtime，Hive 在 runtime 之上提供 governed sparse activation。**

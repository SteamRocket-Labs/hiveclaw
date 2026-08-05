# CCPlus Runtime Activation / 权重层设计

日期：2026-07-04  
状态：讨论稿  
范围：在不改写现有 CCPlus runtime、上下文组装、ToolRuntimeService、Memory Gate / Platform Gate 的前提下，为 Agent Memory、Skill、Tool、Sub-agent、Workflow、Personal Knowledge、Company Knowledge 增加统一的动态激活与权重排序层。

## 0. 一句话结论

这件事不应该做成“新的执行器”，也不应该把权重塞进 ToolRuntimeService 里面。

正确形态是：在现有 runtime 前面加一层 **Runtime Activation Layer**。

它负责做三件事：

1. 把当前用户输入、session 状态、agent 身份、owner/company 权限、task profile、历史反馈，转换成一组可排序的候选对象。
2. 对候选对象先做硬门过滤，再做权重评分。
3. 把排序后的结果分别送到现有的动态上下文、Skill catalog、deferred tool disclosure、active tool groups、hook additional context、prompt manifest 和反馈回流中。

它不负责做这些事：

1. 不替模型调用工具。
2. 不绕过 ToolRuntimeService。
3. 不绕过 Memory Gate / Platform Gate。
4. 不把没有 ACL 的 Company Knowledge 泄漏给模型。
5. 不把动态排序结果写进 frozen prefix。
6. 不把 profile / persona / taste 做成第 4 个产品。

更准确地说：

```text
Attention is all you need for recall, but not for truth.
```

Attention / 权重 / 激活只决定“先看什么、优先提示什么、优先披露什么”；真相仍然由 T0/T2/source_refs、权限、审计、Memory Gate、Platform Gate、ToolRuntimeService 和 Company Knowledge 的 authority surface 保证。

## 1. 产品边界：仍然只有三层

本设计必须服从现有三层产品路径：

```text
1. Agent Memory
2. Personal Knowledge / Knowledge LM
3. Company Knowledge Base
```

人格侧写、Owner profile、认知风格、taste、领域偏好，不是第四个产品。它们属于 Personal Knowledge / Knowledge LM 内部的 profile/context plane。

这点会影响权重层的设计：

| 层级 | 权重的主要用途 | 不应该做什么 |
| --- | --- | --- |
| Agent Memory | 帮单个 agent 在当前任务中召回自己学到的经验、失败模式、工作偏好、open loops | 不把 agent 的片面 profile 当成 owner 的完整画像 |
| Personal Knowledge / Knowledge LM | 组织 owner 的个人事实、偏好、认知风格、领域 taste、跨 agent 共享的个人语境 | 不把某个 agent 的观察直接提升成 owner 真相 |
| Company Knowledge Base | 组织企业事实、政策、流程、对象、权限化文档、团队知识 | 不允许无 ACL 的知识进入候选提示 |

所以权重层要同时支持三种不同 authority：

```text
Agent Memory        = agent-behavior learning layer
Personal Knowledge  = owner/principal-scoped knowledge and profile layer
Company Knowledge   = tenant/company authoritative layer
```

## 2. 当前 runtime 的真实边界

### 2.1 Frozen Prefix：不要动

当前 frozen prefix 由 `backend/app/runtime/prompt_builder.py` 的 `build_frozen_prompt_prefix()` 构建。

它的语义是 session-stable：

```text
agent identity / soul / role
§ System
§ Doing Tasks
§ Using Your Tools
```

代码注释里已经明确：Skill catalog 已经从 frozen prefix 移到 dynamic suffix，原因是 Skill 会变化，放进 frozen prefix 会破坏 prompt-cache boundary。

因此权重层不能把动态记忆、Skill 排名、Tool 排名、KB 命中结果写进 frozen prefix。

### 2.2 Dynamic Suffix：这是主承载面

当前 dynamic suffix 由 `backend/app/runtime/prompt_builder.py` 的 `build_dynamic_prompt_suffix()` 构建。

它已经包含这些动态内容：

```text
Memory snapshot
Session learning projection
Session continuity
Runtime metadata
Permissions context
Scenario section
Active tool groups
Available deferred tools
Skill catalog
Retrieval context / Company Knowledge
Likely deferred tool groups
Environment
System prompt suffix / hook additional context / runtime attachments
```

这说明权重层最自然的落点不是“新 prompt 系统”，而是现有 dynamic suffix 的上游 resolver。

### 2.3 Kernel 装配点：已经有 prompt manifest

当前 `backend/app/kernel/engine.py` 会在模型调用前解析：

```text
resolve_memory_context
resolve_retrieval_context
resolve_runtime_metadata_context
permissions_context
available_deferred_tools
skill_catalog
system_prompt_suffix
active_tool_groups
```

然后构建 dynamic suffix，并通过 `build_runtime_prompt_assembly_manifest()` 记录本轮 prompt assembly。

这对权重层很关键：Activation Layer 的结果必须进入 manifest，否则后续无法解释“为什么这个 Skill 排在前面”“为什么这个 tool 被披露”“为什么这个记忆被注入”。

### 2.4 Tool disclosure：现有工具层已经是多层结构

当前 tool-use 不是单层，而是至少五层：

```text
1. Prompt rules
2. Schema exposure / get_agent_tools_for_llm
3. Deferred discovery / tool_search
4. Kernel tool expansion
5. ToolRuntimeService.execute
```

其中最关键的是：

- `discoverable_tool_names_for_query()` 是 query -> deferred tool names 的单一来源之一。
- `available_deferred_tool_names_for_agent()` 会给 turn-1 dynamic suffix 提供可发现工具列表。
- `tool_search` 会通过 `_resolve_tool_expansion()` 把选中的 deferred tools 扩展成真实 schemas。
- `get_agent_tools_for_llm()` 会继续执行 tenant、AgentTool、MCP reachability、Feishu access、pack policy、requested names 等过滤。
- `ToolRuntimeService.execute()` 是实际执行入口，包含 plan gate、runtime context、hook、L2 policy、governance、ActionPreflight、timeout、registry/fallback、trace。

所以权重不能放在最后的 executor 里。放到 executor 里已经太晚了，模型已经选完工具。

权重应该影响的是：

```text
哪些工具先出现在 Available Deferred Tools 里
tool_search 对某个 query 返回哪些候选
哪些 active tool groups 被提示
哪些 Skill 排在 catalog 前面
哪些候选通过 hook additional context 被提醒给模型
```

## 3. 本质模型：不是训练 Loss，而是 runtime Attention

可以借用 Transformer / NLP 的类比，但不能机械套用。

在 LLM 训练里，loss function 用来更新参数；在 Hive runtime 里，我们不训练基础模型参数，而是在每轮推理前做候选激活和排序。

更准确的映射是：

| Transformer 概念 | Hive runtime 对应物 |
| --- | --- |
| Query | 当前用户输入 + session 状态 + agent role + task profile |
| Key | labels、entities、source_refs、权限、领域、Skill metadata、Tool metadata、历史反馈、profile facets |
| Value | memory item、KB section、Skill、Tool schema、Sub-agent、Workflow、evidence ref |
| Attention score | Activation score / 权重分 |
| Sparse Attention | ACL / sensitivity / allowed_tools / pack policy / tenant scope 后的候选子集 |
| MoE routing | 根据任务类型和权限，激活特定 Tool pack、Skill、Sub-agent、Workflow、KB lane |
| Residual connection | frozen prefix / soul / core runtime contract 保持稳定，不被动态权重覆盖 |

因此，权重层不是“训练层”，而是“推理时路由层”：

```text
当前上下文
→ 生成候选集
→ hard mask
→ score
→ top-k
→ 注入 dynamic suffix / tool disclosure / skill catalog / hook hint
→ 模型决定下一步
→ 工具与结果反馈回流
```

### 3.1 触发机制：每轮轻触发，条件重召回，后台写侧学习

Memory 的 Attention 不应该理解为“只有用户说记忆时才启动”。更准确地说，它分三档：

| 档位 | 什么时候触发 | 做什么 | 进入哪里 |
| --- | --- | --- | --- |
| 轻触发 | 每次 `USER_PROMPT_SUBMIT` / 模型调用前 | 生成本轮 activation query，做 hard mask + 粗排序，只渲染很短的 hint / 排序结果 | dynamic suffix / prompt manifest |
| 重召回 | query 命中显式记忆、实体、历史任务、open loop、owner 偏好、领域 taste、技能/工具选择、高风险决策、低置信或冲突 | 加载更深的 Agent Memory / Personal KB / Company KB 候选，做多跳扩散和 top-k packing | memory snapshot / retrieval context / tool result |
| 后台学习 | `POST_TOOL_USE` / `POST_TOOL_FAILURE` / `TURN_STOP` / session feedback / heartbeat / dream | 更新 heat、success、failure、lifecycle、retention、archival，不直接改变当轮事实 | activation sidecar / T2→T3 / retention |

第一版应该做到：

```text
每轮都做轻触发；
只有满足触发条件时才做重召回；
召回结果只影响“看什么、先看什么、提示什么”；
写侧学习仍然走 T2/T3、Memory Gate、Platform Gate。
```

重召回的触发条件可以先用明确规则，不需要训练模型：

1. 用户显式引用历史：`之前`、`上次`、`记得`、`按我的偏好`、`我们讨论过`、`那个文档`。
2. 当前 query 可抽出实体 / 概念 / 人 / 组织 / 项目 / 文件名，并能命中 Agent Memory 或 KB key。
3. 当前任务需要选择 Skill、Tool、Sub-agent、Workflow，且候选数量大于阈值。
4. 当前上下文存在低置信、冲突、返工、失败、owner negative feedback。
5. 当前任务影响范围高：会改 soul、skill、权限、公司知识、外部动作、不可逆工具。
6. session resume / compaction 后，当前轮需要恢复 open loop 或长期任务状态。

不触发重召回的情况也要明确：普通闲聊、一次性小问答、无实体命中、无历史依赖、无外部动作风险时，只做轻触发和已有 P0 侧写常驻，不扩展检索范围。

### 3.2 QKV 机制：对象级 Attention，不是 token 级 Attention

Transformer 的 Q/K/V 在模型内部是 token 级的；Hive Memory 里应该是对象级的：

| QKV | Hive Memory 对应 | 说明 |
| --- | --- | --- |
| Query | 当前用户输入 + session 状态 + agent role + task profile + owner/principal context + 风险/权限要求 | 本轮“我现在要解决什么问题” |
| Key | labels、entities、relations、source_refs、scenario、profile facets、lifecycle、权限、历史反馈、Skill/Tool metadata | 候选对象“在什么情况下应该被想起” |
| Value | memory 条目、KB 段落、概念页、milestone、profile slice、Skill、Tool schema、Workflow、evidence ref | 真正被披露、加载、引用或提示的内容 |

关键是 **Key 和 Value 必须分离**：

```text
Key 小、稳定、可索引，用于召回和排序；
Value 大、可变、有权限和预算成本，只在被选中后按 surface 披露。
```

落到当前三层记忆结构上：

| 层 | Key 怎么来 | Value 是什么 | 默认行为 |
| --- | --- | --- | --- |
| `soul.md` | 不作为动态 key；它是 residual / frozen identity | 宪法核 | 常驻，不参与权重排序 |
| `self/self.md` | 能力、失败模式、生命周期状态、证据 refs、场景条件 | 相关 self slice | P0 常驻小段 + active 失败模式前置 |
| `profiles/*.md` | owner/collaborator/domain facets、场景条件、反馈强度 | 相关 profile slice | 常驻基础画像；重召回时按 domain/agent-local 切片 |
| `knowledge/<concept>.md` | title、aliases、Relations、Contradictions、Evidence、反向引用 | 概念页或摘要 slice | query top-k + PPR 多跳 |
| `milestones/<id>.md` | 时间、任务类型、owner feedback、被 self/knowledge 引用次数 | 叙事锚点 | 只在相关任务/追溯/复盘时召回 |
| `T2 segment package` | summary、labels、importance、source_refs、stitch id | 证据摘要 + 不可变 refs | 默认不进 prompt；用于验证、追溯、重消化 |
| `T0` | 不作为常规召回 key | 原始事件真相 | 只在审计/回放/证据核查时下钻 |
| `explicit/` overlay | explicit 类型、目标区、时效、吸收状态 | 用户刚要求记住的内容 | 高优先短期常驻，吸收后退役 |

所以 Agent Memory 的 QKV 不是把 T0/T2/T3 都混在一个向量库里，而是：

```text
Q = 本轮任务语义 + 场景 + 权限 + 风险
K = T3/KB/Skill/Tool 的可索引特征 + T2 派生标签
V = 被允许披露的最小内容片段 + evidence refs
```

Personal Knowledge / Company Knowledge 复用同一模型，但 Key 的来源不同：

| 产品层 | Key | Value |
| --- | --- | --- |
| Agent Memory | T3 条目、T2 labels、Relations、profile facets | agent 自己的经验、概念、失败模式、owner 局部画像 |
| Personal Knowledge | documents/segments/entities/assertions/links、owner global/domain profile | 个人文档段落、实体、断言、跨 agent profile slice |
| Company Knowledge | typed ontology、policy、doc segments、ACL、authority surface | 企业知识、流程、制度、对象、受权限控制的段落 |

### 3.3 联想机制：Attention = 多头召回 + 图扩散 + 抑制

Memory 的联想不应该是 LLM 自由发挥，而应该是可解释路径：

```text
hard mask
→ seed retrieval
→ multi-head scoring
→ graph / relation expansion
→ fusion + inhibition
→ top-k packing
→ render surface
```

可以先定义六个“头”：

| Head | 负责什么 | 典型路径 |
| --- | --- | --- |
| semantic head | 当前 query 与 memory/KB/Skill/Tool 的语义相关 | query → concept / doc segment / skill |
| entity head | 人、公司、项目、文件、概念的显式命中 | entity → aliases → assertions / concept page |
| episodic head | 最近任务、open loop、milestone、session continuity | current task → milestone → supporting T2 refs |
| profile head | owner 偏好、认知风格、taste、domain regime | query domain → owner domain slice → agent-local slice |
| procedural head | 该不该提示 Skill、Tool、Sub-agent、Workflow | task profile → skill/tool/workflow candidate |
| authority head | 证据强度、来源权威、冲突、ACL、sensitivity | candidate → evidence/source_refs/authority mask |

联想路径的核心是“共振”和“抑制”：

```text
共振：同一个候选被多个 head 命中，分数上升。
抑制：候选过期、冲突未解、证据弱、风险高、成本高、无 ACL，分数下降或 hard mask。
```

例子：

```text
用户问：“按我们之前讨论的 Personal Knowledge 方案，下一步怎么做？”

semantic head 命中：Personal Knowledge / Knowledge LM
entity head 命中：personal-knowledge-base-spec.md、knowledge-pyramid...
episodic head 命中：2026-07-03 拍板记录、当前 open loop
profile head 命中：owner 明确不接受第四个产品
procedural head 命中：需要文档设计，不需要 tool 自动执行
authority head 命中：memory-system-spec / pyramid / personal spec 是权威文档

结果：
优先召回三级产品边界、个人层 M1/M2/M3、当前 spec；
抑制“第四个产品”“全量 owner profile 注入所有 agent”等错误路径。
```

这就是 Memory 里的 Attention：不是“模型内部注意到哪个 token”，而是 runtime 在 prompt 进入模型之前，先把最可能有用、最有证据、最有权限、最不该遗忘的对象排到前面。

### 3.4 最小落地算法

第一版可以按这个算法实现，不需要引入训练：

```text
on USER_PROMPT_SUBMIT:
  q = build_activation_query(
        user_prompt,
        session_state,
        agent_role,
        task_profile,
        owner_context,
        permission_context,
        risk_context,
      )

  candidates = gather_candidates(
        agent_memory,
        personal_knowledge_hint,
        company_knowledge_if_allowed,
        skill_catalog,
        deferred_tools,
        tool_groups,
        subagents,
        workflows,
      )

  visible = hard_mask(candidates, permission_context, runtime_policy)

  seed_scores = score_heads(q, visible)

  expanded = expand_relations(
        top_seed_candidates,
        relations,
        backlinks,
        entities,
        source_refs,
        ppr_budget,
      )

  ranked = fuse_scores(seed_scores, expanded, lifecycle, feedback, cost, risk)

  render(
        ranked,
        surfaces=[
          memory_snapshot,
          retrieval_context,
          skill_catalog,
          available_deferred_tools,
          active_tool_groups,
          hook_additional_context,
          prompt_manifest,
        ],
      )
```

Memory-specific 的分数可以先用可解释公式：

```text
memory_activation_score =
  relevance(query, key)
+ relation_boost
+ evidence_strength
+ owner_feedback_boost
+ usage_heat
+ lifecycle_boost
- staleness_penalty
- contradiction_penalty
- token_cost_penalty
- risk_penalty
```

这不是 Loss，也不是梯度回归。它更像 Transformer 的 inference-time attention score：只决定本轮上下文的披露顺序；真正的学习发生在 `TURN_STOP` 之后，通过 feedback、引用计数、retention、T2→T3 消化、收敛环和归档生命周期慢慢改变后续的 Key / lifecycle / heat。

## 4. 统一候选对象

建议把所有可激活内容统一抽象成 `ActivationCandidate`。它不是新的持久真相源，而是运行时可重建的候选对象。

```python
ActivationCandidate:
    kind: Literal[
        "agent_memory",
        "personal_knowledge",
        "company_knowledge",
        "skill",
        "tool",
        "tool_group",
        "subagent",
        "workflow",
    ]
    scope: Literal["agent", "personal", "company"]
    value_ref: str
    title: str
    summary: str
    keys: list[str]
    source_refs: list[str]
    hard_masks: list[ActivationHardMask]
    score_components: ActivationScoreComponents
    final_score: float
    render_surfaces: list[
        Literal[
            "memory_snapshot",
            "retrieval_context",
            "skill_catalog",
            "available_deferred_tools",
            "active_tool_groups",
            "hook_additional_context",
            "prompt_manifest",
        ]
    ]
    action_semantics: Literal[
        "hint_only",
        "rank_existing_context",
        "expose_schema",
        "load_on_model_request",
        "execute_only_after_model_tool_call",
    ]
```

关键点：

1. `ActivationCandidate` 是统一排序对象，不是统一执行对象。
2. `kind=tool` 的 candidate 可以影响 tool disclosure，但不能直接执行。
3. `kind=company_knowledge` 必须先过 ACL，再参与评分；没有权限时不能以“被过滤”形式泄漏存在性。
4. `kind=skill` 可以影响 catalog 顺序和 `load_skill` 建议，但不能改变 `load_skill` 的语义。
5. `kind=subagent` / `kind=workflow` 只能提示或排序，真正 spawn/start 仍然必须由模型选择工具并经过治理。

## 5. Hard Mask 必须先于 Score

权重层第一步不是打分，而是 hard mask。

原因很简单：权限、可用性、治理边界不是“低分项”，而是“不可见项”。

必须先过滤：

```text
tenant scope
agent ownership
owner/principal context
company ACL
sensitivity level
allowed_tools / excluded_tools
execution mode
pack policy
MCP reachability
tool availability
subagent child permission profile
workflow permission
Memory lifecycle suppression
reference revalidation requirement
conflict unresolved
```

特别注意：

```text
Denied candidate must be absent, not shown as denied.
```

也就是说，如果某个 Company Knowledge 文档当前用户没有权限，它不能出现在 top-k、候选理由、debug hint 或 prompt 中。最多只能在服务端 trace 里以安全摘要记录“某类候选被 hard mask 过滤”，不能暴露具体 title/id。

## 6. Score Components：权重要反映什么

评分不应该只有 embedding similarity。Hive 的权重至少要混合以下信号：

| 维度 | 含义 | 适用对象 |
| --- | --- | --- |
| semantic_relevance | 当前 query 与候选语义相关度 | memory / KB / skill / tool / workflow |
| lexical_relevance | 关键词、实体、slug、title 直接命中 | 全部 |
| task_profile_match | 与当前 task profile / scenario 的匹配 | skill / tool_group / workflow / subagent |
| authority | 候选是否来自更权威层，例如 source_refs 完整、company source、reviewed memory | memory / personal / company |
| evidence_strength | T0/T2/source_refs 支持强度 | memory / KB |
| recency | 最近出现、最近更新、最近使用 | 全部 |
| retention | 明确被保留、open_loop、长期偏好 | memory / profile |
| lifecycle_state | active / stale / archived / expired / unresolved conflict | memory / skill / workflow |
| historical_success | 过去在相似任务中是否有帮助 | skill / tool / subagent / workflow |
| owner_feedback | owner/session feedback 的正负反馈 | memory / skill / tool |
| usage_heat | 使用频率与最近热度 | memory / skill / tool |
| cost_penalty | token、latency、tool cost、上下文预算压力 | KB / tool / workflow |
| risk_penalty | 外部可见、不可逆、敏感动作风险 | tool / workflow |
| diversity_bonus | 避免 top-k 全部来自同一类候选 | memory / KB / skill |

建议第一版使用可解释的线性加权，而不是黑盒模型：

```text
final_score =
  semantic_relevance      * w_semantic
+ lexical_relevance       * w_lexical
+ task_profile_match      * w_task
+ authority               * w_authority
+ evidence_strength       * w_evidence
+ recency                 * w_recency
+ retention               * w_retention
+ historical_success      * w_success
+ owner_feedback          * w_feedback
+ usage_heat              * w_heat
+ diversity_bonus         * w_diversity
- cost_penalty            * w_cost
- risk_penalty            * w_risk
```

每个 candidate 最终必须带 `reasons`，例如：

```text
score=0.83
reasons=[
  "query_entity_match: memory-system",
  "task_profile_match: architecture_design",
  "source_refs_complete",
  "recent_success_with_skill: frontend-skill",
  "low_cost_hint_only"
]
```

这会让系统可调试，不会变成“玄学排序”。

## 7. 权重最终体现在哪里

权重不应该只存在于一个表里。它应该体现在 runtime 的多个 disclosure surface 中。

### 7.1 Memory Activation

当前 `backend/app/memory/activation.py` 已经有 `ActivationScorer`，包含：

```text
goal_weight
owner_weight
company_weight
open_loop_weight
retention_weight
confidence_weight
usage_heat_weight
```

这说明 Agent Memory 的权重基础已经存在。下一步不是推翻它，而是把它纳入统一 Activation Layer，让 memory 的 score 可以和 skill/tool/subagent/workflow 的 score 一起被解释和记录。

Memory 的正文注入仍然走现有 memory resolver，不应该由 hook 直接粗暴注入。

### 7.2 Skill Catalog 排序

当前 `backend/app/services/skill_catalog_ranker.py` 已经按 scenario overlap、lifecycle state、use_count 对 Skill catalog 排序。

这就是 Skill 权重的正确落点。

需要增强的是：

```text
scenario overlap
+ task_profile
+ historical success
+ owner/session feedback
+ skill state
+ declared tool packs
+ token cost
+ recent failure penalty
```

不要把权重放进 `load_skill`。`load_skill` 应该保持“模型已经选择某个 Skill 后，加载该 Skill 内容”的确定性语义。

### 7.3 Deferred Tool Disclosure

当前 dynamic suffix 会渲染 `Available Deferred Tools`，并提示模型用：

```text
tool_search query="select:<tool_name>"
```

这就是 tool 权重最关键的地方。

未来应该让 `available_deferred_tool_names_for_agent()` 和 `discoverable_tool_names_for_query()` 返回经过 hard mask 和 score 排序的结果。

权重影响：

```text
哪些 tool 进入前 40 个 deferred tools
哪些 tool 在 tool_search 结果里排前面
哪些 tool group 被提示为 likely useful
```

权重不影响：

```text
工具是否绕过 pack policy
工具是否绕过 allowed_tools
工具是否绕过 ActionPreflight
工具是否自动执行
```

### 7.4 Active Tool Groups / Likely Deferred Tool Groups

当前 dynamic suffix 已经有：

```text
Active Runtime Tool Groups
Likely Deferred Tool Groups
```

这里适合放 pack-level / group-level 的权重提示。

例如：

```text
## Likely Deferred Tool Groups
- web_pack: 当前任务需要外部检索，且用户询问 latest/current
- memory_pack: 当前任务涉及 owner 偏好与历史决策
- workflow_pack: 当前任务是多步骤可恢复流程
```

这比一次性披露大量 tool schema 更符合 sparse attention。

### 7.5 Hook Additional Context

`USER_PROMPT_SUBMIT` 当前已经能通过 `additional_contexts` 进入 `system_prompt_suffix`，再进入 dynamic suffix。

这适合放非常短的 activation hints：

```text
## Runtime Activation Hints
- Memory: 可能相关 `memory:t3:skill-loading-regression`，原因：用户询问 Tool use 和 Skill 排序。
- Skill: `code-review-expert` 相关度高；如进入代码审查，可先 load_skill。
- Tool Group: `web_pack` 暂不优先；原因：当前问题基于本地 runtime 文件，不需要外部搜索。
- Sub-agent: 暂不建议 spawn；原因：任务是单一设计文档整理。
```

Hook 在这里不是唯一架构，只是最合适的提示注入面。

### 7.6 Prompt Manifest / Trace

每轮必须可解释：

```text
candidate_count_before_mask
candidate_count_after_mask
top_candidates
rendered_surfaces
suppressed_counts_by_reason
selected_tools
loaded_skills
subagents_spawned
workflow_started
feedback_events
```

这些信息应该进入 prompt assembly manifest 或 runtime trace，而不是只出现在日志里。

## 8. Hook 应该怎么用

Hook 是 activation 的一个 surface，不是全部。

建议分工：

| Hook / runtime point | 用途 |
| --- | --- |
| `USER_PROMPT_SUBMIT` | 生成本轮 activation hints，进入 dynamic suffix |
| `SESSION_START` | 未来可做 session 级 warmup；但当前 additional_contexts 未进入 prompt，需要补消费链后再用 |
| `PRE_TOOL_USE` | 治理、rewrite、audit、风险提醒；不做首轮工具推荐 |
| `POST_TOOL_USE` | 记录工具实际成功/失败/有用性，作为权重反馈 |
| `POST_TOOL_FAILURE` | 记录失败 penalty、fallback 机会、是否需要降权 |
| `TURN_STOP` / `STOP` | 汇总本轮候选、实际选择、用户反馈、是否应调整 heat/success |
| `SUBAGENT_START` | 为 child agent 注入最小授权上下文，不复制父 agent 完整 memory |
| `PRE_COMPACTION` / `POST_COMPACTION` | 保留 activation trace，避免压缩后丢掉为什么这么召回 |

第一版最应该先用：

```text
USER_PROMPT_SUBMIT → activation hints
POST_TOOL_USE / POST_TOOL_FAILURE / TURN_STOP → feedback signals
```

## 9. Sub-agent / Workflow / Skill 的动态化

### 9.1 Skill

Skill 是 progressive-disclosure capability capsule。权重的作用是：

```text
把最相关的 Skill 排到 catalog 前面
在 Runtime Activation Hints 里提醒模型可 load_skill
记录 load_skill 后是否真的帮助完成任务
```

它不应该：

```text
自动加载大量 Skill 正文
因为 Skill 分数高就自动执行脚本
因为 Skill 相关就绕过工具权限
```

### 9.2 Tool

Tool 权重的作用是 schema disclosure 和 discovery ranking。

它可以影响：

```text
turn-1 Available Deferred Tools 的顺序
tool_search(query) 的候选顺序
Likely Deferred Tool Groups 的提示
```

它不能影响：

```text
ToolRuntimeService 是否执行治理
ActionPreflight 是否被绕过
disabled pack 是否可用
allowed_tools / excluded_tools 是否生效
```

### 9.3 Sub-agent

Sub-agent 权重的作用是“建议是否分工”，不是自动分工。

候选评分要考虑：

```text
任务是否可拆分
是否需要隔离上下文
是否需要并行
是否存在 specialist subagent
child allowed_tools 是否足够
当前任务是否单线程更快
spawn 成本是否值得
```

如果分数高，应该提示模型：

```text
当前任务适合考虑 spawn_subagent: reason...
```

但真正 spawn 仍然由模型调用 `spawn_subagent` 或相关工具，并经过 runtime 边界。

### 9.4 Workflow

Workflow 权重的作用是识别“这个任务是否适合可恢复、可审计、多步骤 orchestration”。

候选评分要考虑：

```text
任务是否重复
是否有明确步骤
是否有等待/审批/恢复点
是否需要跨工具稳定执行
是否比普通 agent loop 更合适
```

如果分数高，提示模型可以：

```text
preview_workflow
start_workflow
```

但不能让 hook 直接 start workflow。

## 10. Personal Knowledge 与 Owner Profile 的处理

多个 Agent 共享同一个 Owner 时，每个 Agent 的 memory 都只是 owner 的一个局部观察。

因此上下文注入不能简单地做：

```text
把所有 owner profile 全量注入所有 agent
```

更合理的是三层切片：

```text
1. Global Owner Slice
   owner 的稳定身份、长期偏好、明确授权偏好、跨领域硬约束。

2. Domain Slice
   当前任务领域相关的品味、认知风格、决策偏好。

3. Agent-local Slice
   这个 agent 自己和 owner 互动中学到的局部经验。
```

权重层的职责是根据当前 query 和 agent role，选择合适 slice：

```text
owner_global: low token, high authority, always small
owner_domain: medium token, query/task matched
agent_local: high relevance only, evidence required
```

这样可以避免一个 agent 的片面观察污染所有场景，也能避免用户提到的“改 Skill 1 / Skill 2 导致 Skill 3 崩溃”的连带问题。

## 11. 遗忘与归档

Hive 的“遗忘”不应该是删除，而应该是召回排序与生命周期管理。

建议分为：

```text
active     当前容易被召回
warm       相关时可召回
cold       默认不召回，但可搜索
archived   不进入 prompt，只保留证据和可审计搜索
blocked    冲突、过期、缺证据、需复核，不进入模型上下文
```

这对应到 runtime：

| 状态 | 行为 |
| --- | --- |
| active | 可进入 top-k memory / skill / tool hints |
| warm | query 明确相关时进入候选 |
| cold | 仅 search / explicit query 时出现 |
| archived | 不自动召回，只可审计检索 |
| blocked | hard mask，不进入 prompt |

这能解决 Wiki 扩大后的核心问题：

```text
内容越来越多，但默认召回范围不会无限扩大。
```

## 12. 实现落点建议

后续如果进入实现，建议按这些文件落地。

### 12.1 新增统一 runtime activation 模块

建议新增：

```text
backend/app/runtime/activation_candidates.py
backend/app/runtime/activation_router.py
backend/app/runtime/prompt_sections/activation_hints.py
```

职责：

```text
activation_candidates.py:
  定义 ActivationCandidate / ActivationScore / hard mask reason / render surface。

activation_router.py:
  输入 request/session/task profile/principal context，输出 ranked candidates。

activation_hints.py:
  把 top-k candidates 渲染成极短 prompt hint。
```

### 12.2 连接现有 Memory Activation

文件：

```text
backend/app/memory/activation.py
backend/app/memory/retriever.py
```

做法：

```text
保留现有 ActivationScorer。
让 retriever 输出带 reasons 的 memory candidate trace。
把 memory score 纳入统一 manifest。
不要绕过现有 memory resolver。
```

### 12.3 增强 Skill catalog ranker

文件：

```text
backend/app/services/skill_catalog_ranker.py
```

做法：

```text
保留现有 scenario overlap + lifecycle + use_count。
增加 task_profile、recent_success、recent_failure、declared_pack、feedback、token cost。
输出排序理由，供 manifest 记录。
```

### 12.4 增强 Tool discovery ranking

文件：

```text
backend/app/services/agent_tools.py
backend/app/runtime/invoker.py
```

做法：

```text
先维持 discoverable_tool_names_for_query() 是 schema path / text path 的单一来源。
在它内部或上游增加 score 排序。
hard mask 仍然走 tenant、pack policy、MCP reachability、allowed/excluded tools。
tool_search 只加载模型选择的 schema，不自动执行。
```

### 12.5 USER_PROMPT_SUBMIT hint 注入

文件：

```text
backend/app/runtime/invoker.py
backend/app/runtime/hooks.py
backend/app/runtime/prompt_builder.py
```

做法：

```text
在 USER_PROMPT_SUBMIT 后生成 activation hint。
通过 additional_contexts / system_prompt_suffix 进入 dynamic suffix。
严格限制字符预算，例如 800-1500 chars。
只放 top-k refs + reason，不放大段正文。
```

### 12.6 Feedback ledger

建议新增可重建 sidecar 或 DB trace，不作为真相源：

```text
activation_events:
  turn_id
  candidate_ref
  kind
  score
  reasons
  rendered_surface
  selected_by_model
  tool_success
  user_feedback
  latency_cost
  token_cost
```

这不是 durable memory truth，而是控制面 / ranking sidecar。丢了可以从 traces、T0、tool lifecycle 部分重建一部分。

## 13. 不要动的核心

这些边界不应该因为权重层而改：

1. `build_frozen_prompt_prefix()` 的 frozen prefix 语义。
2. ToolRuntimeService 的治理、ActionPreflight、L2 gate、hook、trace。
3. `tool_search` 作为 deferred schema discovery 的模型选择入口。
4. `load_skill` 作为 progressive disclosure 的确定性加载入口。
5. Memory T0/T2/T3/soul 的 truth-source 与 source_refs 规则。
6. Company Knowledge ACL / sensitivity / tenant scope。
7. Sub-agent 的 clean specialist context 和 child allowed_tools 边界。
8. Workflow 作为确定性 orchestration substrate，不被 memory/hook 自动启动。

## 14. 验收标准

这层上线后，应该能用以下标准验收：

### 14.1 正确性

```text
无权限 Company Knowledge 不进入候选、prompt、manifest 的可见字段。
disabled pack 的 tool 不会因为高分出现在 tool_search 可加载结果里。
allowed_tools / excluded_tools 对 subagent 仍然生效。
frozen prefix cache 不因每轮权重变化失效。
```

### 14.2 召回质量

```text
同样上下文下，最相关 Skill / Tool / Memory 排名前移。
不相关 memory 的 prompt 注入减少。
Sub-agent 不再长期“很少触发”，但也不会滥触发。
Workflow 只在多步骤、可恢复、需审计任务中被建议。
```

### 14.3 可解释性

```text
每个进入 prompt 的 hint 都有 candidate_ref + reasons。
每个被 hard mask 的类型都有聚合统计。
每次 tool_search 的结果排序可追踪。
每次 Skill catalog 排序可解释。
```

### 14.4 反馈闭环

```text
POST_TOOL_USE / POST_TOOL_FAILURE 能影响后续 tool weight。
load_skill 后是否产出有效结果能影响 skill weight。
用户 session feedback 能影响 memory / profile / skill 排名。
失败候选能降权，但不会破坏 evidence truth。
```

## 15. 建议的落地顺序

这不是“半成品 MVP”顺序，而是后续实现时的依赖顺序。每一轮实际交付都必须包含测试、trace、回滚/兼容和无权限泄漏验证。

### V1：只做观察与提示，不改变执行语义

目标：

```text
ActivationCandidate 数据结构
Activation Router 初版
Runtime Activation Hints 渲染
prompt manifest 记录 top candidates
USER_PROMPT_SUBMIT 注入短提示
```

不做：

```text
不自动调用工具
不自动 load_skill
不自动 spawn_subagent
不改变 ToolRuntimeService
```

### V2：Skill 与 Tool disclosure 排序

目标：

```text
增强 skill_catalog_ranker
增强 available_deferred_tool_names_for_agent 排序
增强 discoverable_tool_names_for_query 排序
记录 tool_search candidate trace
```

这一步会真正改善“Skill/Tool 太多时谁排前面”的问题。

### V3：Sub-agent / Workflow 候选化

目标：

```text
把 subagent 和 workflow 纳入 ActivationCandidate
只做 hint / ranking
不自动启动
记录模型是否采纳建议
```

这一步解决 Multi-agent 系统里 subagent 触发率低、workflow 选择不稳定的问题。

### V4：Personal Knowledge / Owner Profile 切片

目标：

```text
Global Owner Slice
Domain Slice
Agent-local Slice
三者通过 hard mask + score 进入 dynamic suffix
```

这一步解决“多个 Agent 共享 owner，但每个 Agent 只是片面观察”的问题。

### V5：反馈学习与归档

目标：

```text
把 POST_TOOL_USE / POST_TOOL_FAILURE / TURN_STOP / session feedback 接入权重 sidecar
形成 active / warm / cold / archived / blocked lifecycle
支持记忆强度、Skill 强度、Tool 强度、Workflow 强度动态变化
```

## 16. 最终形态

最终系统应该像这样工作：

```text
用户输入
→ T0 / transcript append
→ USER_PROMPT_SUBMIT
→ Runtime Activation Layer
   → gather candidates
   → hard mask
   → score
   → top-k
→ dynamic suffix
   → memory snapshot
   → skill catalog order
   → deferred tools order
   → activation hints
   → retrieval context
→ 模型循环
→ tool_search / load_skill / spawn_subagent / preview_workflow / normal tools
→ ToolRuntimeService / governance / ActionPreflight
→ tool result
→ POST_TOOL_USE / POST_TOOL_FAILURE
→ TURN_STOP feedback
→ weight sidecar / lifecycle update
```

用一句话说：

```text
权重层不是替代 CC runtime，而是让 CC runtime 的上下文组装和工具披露具备 sparse attention。
```

这应该成为 CCPlus 的一个核心增强点：不改变模型循环，不绕过治理，但让模型每一轮看到的 memory、Skill、Tool、Sub-agent、Workflow、Personal Knowledge、Company Knowledge 都更有主次。

# T3 到 Soul / Skill 改造讨论稿

日期：2026-06-19

状态：整改设计文档。本文只定义 T3 以上的原则、责任边界、候选包形态和流程关系，不表示代码已经完成。

范围：

- 覆盖 `T3 -> soul.md`。
- 覆盖 `T3 -> Skill candidate -> skills/<name>/SKILL.md`。
- 不重新定义 T0 -> T2；以 `docs/t0-to-t2-segment-package-redesign-2026-06-18.md` 为准。
- 不重新定义 T2 -> T3；以 `docs/t2-to-t3-curation-redesign-2026-06-18.md` 为准。
- 不把 Workflow 纳入本流程。Workflow 是独立执行控制体系，memory 只提供 evidence handoff / reference hint。

最高原则仍然是：

```text
LLM 负责判断、提炼、反思、归纳、候选生成；
平台负责证据引用、权限、去重、回滚、审计、最终落盘。
```

## 1. 核心结论

T3 到 Soul 和 T3 到 Skill 是两条不同流程：

```text
T3 -> Soul:
  accepted T3 memory
  -> Dream / Soul Writer Agent
  -> soul_pitch.md + soul_patch.md
  -> Soul Memory Gate Agent fresh review
  -> Platform Soul Gate atomic commit
  -> soul.md

T3 -> Skill:
  accepted T3 capability / skill_seed evidence
  -> Skill Distiller / Skill Writer Agent
  -> Skill Candidate Package
  -> eval + Skill Review
  -> Platform Skill Gate atomic promotion
  -> skills/<name>/SKILL.md
```

二者共享同一条治理模式，但目标不同：

| 维度 | T3 -> Soul | T3 -> Skill |
|---|---|---|
| 目标 | always-on identity / constitution | progressive capability capsule |
| 主要输入 | `worker.md`、少量 `user.md`、极少 `episodes.md` / `capabilities.md` 的原则性信号 | `capabilities.md` skill_seed，辅以 `episodes.md` examples、`worker.md` failure modes |
| 输出 | `soul_patch.md`，最终进入 `soul.md` | LLM-authored `SKILL.md.draft` + eval / review，最终进入 `skills/<name>/SKILL.md`；机械 fast-reflection/lifecycle 信号只能写 `candidate_signal.md` |
| 是否常驻 prompt | 是，`soul.md` 进入 frozen identity 区 | 否。Skill 只在 catalog / relevant load 时进入上下文 |
| 验证强度 | 很强，因污染每一次未来 prompt | 很强，因会固化成可复用能力和工具操作引导 |
| 常见失败 | 把 T3 细节塞进 Soul，导致 identity 噪音 | 把未验证方法固化成 Skill，导致错误 SOP |

一句话边界：

```text
如果它应该影响每一次 prompt，才有资格进 soul.md。
如果它只是相关时有用，留在 T3。
如果它是可执行方法，走 Skill。
```

### 1.1 “落盘”语义澄清

本文里的 `Platform Gate atomic commit` 不表示平台决定语义插入位置、重排内容、拼接段落或重写文本。

正确语义是：

```text
Agent 负责生成最终可提交内容；
Platform Gate 负责校验和事务提交这份 Agent-authored content。
```

也就是说：

| 层级 | Agent 生成什么 | Platform Gate 做什么 |
|---|---|---|
| T2 | `summary.md`、`labels.md`、`review.md` candidate | 校验 refs / XML / 权限，然后把完整 package 原子提交 |
| T3 | `revised_patch.md`，其中包含 target file、base revision、精确 append/replace/retire/reinforce 操作和 XML block 原文 | 校验 base revision、refs、路径、review，然后按 Agent-authored patch 原子提交 |
| Soul | 默认生成完整 `soul.md.next`；必要时也可生成精确 block patch | 校验 frozen sections、base revision、refs、权限、rollback，然后 atomic replace 或 exact patch apply |
| Skill | 完整 Skill Candidate Package；`SKILL.md.draft` 必须由 Skill Writer / Distiller LLM 生成，机械信号只能作为 `candidate_signal.md` evidence | 校验 eval、路径、权限、rollback，然后 atomic promote 整个 package |

因此，Soul 不是平台“把一段内容插到某个地方”。Soul 的上下文顺序、块归属、旧内容如何保留、哪些内容被改写，都必须由 Dream / Soul Writer Agent 在候选文件里明确完成。

如果平台发现 base file 已变化、frozen section 不一致、patch 无法精确应用，默认进入 `needs_agent_rebase` / held state，退回 Agent 重写；平台不能自己猜插入点。

## 2. T3、Soul、Skill 的关系

### 2.1 T3 是动态长期记忆，不是最高层宪法

设计目标里的 accepted T3 只包含四个 Markdown 文件：

```text
memory/t3/
  episodes.md
  user.md
  worker.md
  capabilities.md
```

这四个文件是长期语义层，但仍是 dynamic memory。它们进入 prompt 时应该经过 relevance、owner/company scope、sensitivity、prompt priority 和 budget 选择。

现有兼容视图、read model、relation graph、contradiction view、index view 可以存在，但不能作为 T3 以上流程的语义 source of truth。T3 以上流程读取它们时，只能当导航和辅助，不当证据。

### 2.2 Soul 是 always-on identity / constitution

`soul.md` 不是滚动全书摘要，也不是 T3 的压缩版。它只保存少数经过长期验证、会影响 agent 身份、使命、权限、协作方式、质量标准和长期行为原则的内容。

可以类比为：

```text
T3     ~= Claude Code 的 relevant memories / rule snippets / project knowledge
soul.md ~= CLAUDE.md 里最上层的身份契约、北极星、不可轻易改变的行为宪法
```

但这个类比有边界：`soul.md` 不应变成一个巨大的 `CLAUDE.md`。Claude Code 的 `CLAUDE.md` 可能同时包含项目规则、命令、偏好、工具说明；Hive 的 `soul.md` 应该更窄，只保存最高层 identity / constitution。项目级动态规则继续留在 T3 或 Skill。

### 2.3 Skill 是程序性能力，不是 T3 页面

Skill 是 progressive capability capsule。它可以由 T3 `capabilities.md` 中的 skill_seed 生长出来，但最终 source of truth 是：

```text
skills/<name>/SKILL.md
skills/<name>/references/
skills/<name>/templates/
skills/<name>/scripts/
skills/<name>/evals/
```

Skill 不是 T3 子页，也不是 `soul.md` 的一节。T3 只保存它的证据、使用场景、方法雏形和 promotion hint。

## 3. 什么内容能从 T3 进入 Soul

### 3.1 可进入 Soul 的内容

只考虑以下内容：

| T3 来源 | 可进入 Soul 的内容 | 示例 |
|---|---|---|
| `worker.md` | 已多次验证、应 always-on 的工作原则、红线、质量标准 | “涉及生产部署必须先取证当前环境，再做变更。” |
| `user.md` | 跨任务稳定、影响所有协作方式的 owner / principal 偏好 | “用户要求架构问题先对齐边界和术语，不能直接补丁化。” |
| `episodes.md` | 极少数反复出现、足以改变 agent 服务方式的场景模式 | “该 agent 长期服务投研场景，默认输出要带证据链和可追溯来源。” |
| `capabilities.md` | 非具体方法，而是能力使用哲学或质量原则 | “可复用方法必须先经验证，再晋升为 Skill。” |
| `memory/explicit/**` | 用户显式要求记住且 identity-grade 的指令 | “以后任何改造都必须一次性闭环，不接受 MVP。” |

### 3.2 不应进入 Soul 的内容

这些内容必须留在 T3、explicit overlay、Skill 或 audit：

- 单次项目事实、一次性需求、短期偏好。
- 具体 SOP 步骤、调研方法、命令序列、脚本片段。
- `RuntimeTask` id、Attempt id、trigger id、artifact path、临时文件路径。
- 普通场景记忆和 episodic cue。
- 还没有多次验证的行为建议。
- 只在某个用户、某个公司、某个 channel、某个任务中成立的窄规则。
- 任何会扩大权限、改变 owner/company 边界、绕过审批的内容。

## 4. `soul.md` 建议格式

当前默认模板的 `Learned Behaviors`、`Core Strategies`、`Blocked Patterns`、`User Profile` 边界太粗，容易把 T3 内容直接倒入 Soul。建议升级为 `hive.soul.v2`：

```md
---
schema: hive.soul.v2
agent_id: "<agent_id>"
owner_id: "<owner_id>"
protected_sections:
  - frozen_identity_contract
  - authority_boundaries
updated_at: "..."
---

# Soul — <agent_name>

## Frozen Identity Contract
<soul_identity id="identity" status="frozen">
  <role>...</role>
  <mission>...</mission>
  <primary_users>...</primary_users>
  <core_outputs>...</core_outputs>
</soul_identity>

## Authority Boundaries
<soul_boundary id="boundary-..." status="frozen">
  <rule>...</rule>
  <requires_owner_approval_when>...</requires_owner_approval_when>
  <source_refs>...</source_refs>
</soul_boundary>

## Operating Constitution
<soul_principle id="principle-..." status="active" stability="stable">
  <rule>...</rule>
  <applies_when>...</applies_when>
  <does_not_apply_when>...</does_not_apply_when>
  <source_refs>...</source_refs>
</soul_principle>

## Collaboration Contract
<soul_user_model id="collab-..." status="active" stability="stable">
  <summary>...</summary>
  <applies_when>...</applies_when>
  <does_not_apply_when>...</does_not_apply_when>
  <source_refs>...</source_refs>
</soul_user_model>

## Quality Bar
<soul_quality_bar id="quality-..." status="active" stability="stable">
  <standard>...</standard>
  <verification>...</verification>
  <source_refs>...</source_refs>
</soul_quality_bar>

## Red Lines
<soul_redline id="redline-..." status="active" stability="stable">
  <rule>...</rule>
  <why>...</why>
  <source_refs>...</source_refs>
</soul_redline>
```

格式规则：

- Markdown 是容器，XML block 是可审查、可回滚、可引用的语义单元。
- Frozen sections 默认不可由 Dream 修改；需要 owner/company 级确认。
- 非 frozen blocks 也必须有 source refs、stability、applies_when、does_not_apply_when。
- Soul 不保存导航索引；运行时导航来自 `build_t3_entry_manifest()` / Memory Navigation，唯一持久 Memory Wiki 地图是 generated/read-model `memory/wiki_map.md`，不进入 frozen identity。旧 `memory/INDEX.md` / `memory/index.md` / `memory/.derived/t3_index.md` 已退役。
- Soul 不保存完整方法步骤；方法步骤走 `capabilities.md` / Skill。
- 不存在 `source.md` 作为最高层 identity 文件；T3 -> Soul 只允许更新 `soul.md`。T0 segment 里的 `source.md` 是原始事件账本，和最高层 Soul 没有命名关系。

### 4.1 Soul 模板迁移策略

实现时采用 `hive.soul.v2` 作为唯一目标格式：

1. 新建 agent 直接使用 `hive.soul.v2` 模板。
2. 现有旧四段式 `soul.md` 不由机械脚本直接重写语义；第一次进入 Soul Candidate Batch 时，由 Dream / Soul Writer Agent 读取旧 `soul.md`，生成完整 `soul.md.next`，再由 Soul Memory Gate + Platform Soul Gate 迁移。
3. 旧 `Learned Behaviors`、`Core Strategies`、`Blocked Patterns`、`User Profile` 只作为 migration input，不再作为新增写入目标。
4. 迁移必须保留 frozen identity / authority boundary，不得把旧 T3 细节机械倒入 Soul。

## 5. T3 -> Soul 写入路径

### 5.1 谁负责写

语义写入者：

```text
Dream / Soul Writer Agent
```

它负责生成：

```text
evolution/soul_candidates/<candidate_id>/
  soul_pitch.md
  soul_patch.md
  soul.md.next
  manifest.json
```

提交执行者：

```text
Platform Soul Gate
```

Platform Soul Gate 只做 hard check、文件锁、rollback ref、audit 和 atomic commit。它不能机械生成或改写 `soul_patch.md` / `soul.md.next` 的语义内容，也不能决定“插到哪里”。如果 review 要求改写、base revision 冲突或 protected section 校验失败，必须退回 Dream / Soul Writer Agent 生成新 patch / next file。

### 5.2 流程

```text
Dream / Soul Candidate Batch
  -> read accepted T3: episodes/user/worker/capabilities
  -> read current soul.md
  -> read explicit memory overlay if identity-grade
  -> read candidate/evolution ledger
  -> Dream / Soul Writer Agent produces soul_pitch.md
  -> Dream / Soul Writer Agent produces soul_patch.md and soul.md.next
  -> Soul Memory Gate Agent fresh-reviews latest soul_patch.md
  -> Platform Soul Gate hard-checks:
       source_refs
       XML schema
       protected sections
       base revision
       owner/company authority boundary
       rollback ref
       no runtime ids / transient artifacts
       no direct T3 mutation
  -> atomic replace soul.md with soul.md.next, or exact apply an Agent-authored block patch
  -> audit + rollback snapshot
```

### 5.3 和 T2 -> T3 的相似点

相同：

- 都是 LLM Writer 先生成完整语义候选。
- 都需要独立 Memory Gate / Referee 对最新 patch 做 fresh review。
- 都由 Platform Gate 做最终事务提交。
- 都要求 source refs、rollback refs、audit。
- Platform Gate 都不能替 LLM 改写语义内容。

不同：

| 维度 | T2 -> T3 | T3 -> Soul |
|---|---|---|
| 输入 | reviewed Segment Packages + targeted T0 refs | accepted T3 + current soul + identity-grade explicit saves |
| 输出 | dynamic long-term memory blocks | always-on identity / constitution blocks |
| 验证强度 | 中高 | 极高 |
| Review 关注 | 语义去重、合并、稳定性、target file selection | identity 污染、权限漂移、frozen charter、prompt blast radius |
| 失败默认 | hold T3 candidate | hold Soul candidate |
| 可修改目标 | `memory/t3/{episodes,user,worker,capabilities}.md` | `soul.md` only |

## 6. Soul Memory Gate Rubric 草案

只要涉及打分，必须有明确标准。Soul review 建议使用 0-4 分制，每个分数必须带 rationale 和 source refs。

| 分项 | 0 分 | 2 分 | 4 分 |
|---|---|---|---|
| `evidence_strength` | 无 accepted T3 / explicit source | 有证据但来源少或存在不确定 | 多个 accepted T3 / explicit refs 支撑 |
| `stability` | 任务局部 / 一次性 | 可能长期有效但仍需观察 | 跨 session 稳定成立 |
| `identity_fit` | 只是普通记忆或方法 | 影响协作方式但不是 always-on | 明确影响身份、使命、权限、长期行为原则 |
| `conflict_safety` | 与 frozen/charter/权限冲突 | 有潜在冲突需 owner 澄清 | 不冲突且边界清楚 |
| `prompt_blast_radius` | 太宽，会污染未来 prompt | 范围偏宽但可加 applies_when | 精准、紧凑、可条件化 |

默认规则：

```text
只有 evidence_strength >= 3
且 stability >= 3
且 identity_fit >= 3
且 conflict_safety >= 3
且 prompt_blast_radius >= 3
才允许进入 active soul block。
```

任何涉及 frozen identity、authority expansion、company boundary 的 patch，即使分数达标，也需要 owner/company gate。

### 6.1 Soul 自动晋升 / 人工确认分级

默认只允许低风险 active Soul block 自动晋升：

```text
auto-commit 条件：
  - 不修改 frozen identity / authority boundary
  - 不扩大权限、工具能力、外部可见动作范围
  - 不改变 owner/company/principal 边界
  - 不包含敏感个人信息、凭证、商业机密原文
  - latest Soul Memory Gate review 全部分项 >= 3
  - patch 只写 active soul_principle / soul_user_model / soul_quality_bar / soul_redline
```

以下情况一律 `needs_owner_or_company_approval`：

- 修改 frozen identity、mission、charter、authority boundary。
- 扩大 agent 权限、改变审批要求、改变公司/owner 归属边界。
- 写入跨租户、跨公司、跨用户可见的长期行为规则。
- Soul Memory Gate 认为语义上成立但 blast radius 过大。
- explicit memory overlay 中用户指令与现有 Soul / charter 存在冲突。

### 6.2 Soul manifest / rollback

不为每个 Soul block 建独立 `manifest.json`。证据和回滚统一放在：

```text
evolution/soul_candidates/<candidate_id>/manifest.json
evolution_ledger.jsonl
soul_patch.md 内部 source_refs
Platform Soul Gate rollback snapshot
```

XML block 内必须保留 `source_refs`、`stability`、`applies_when`、`does_not_apply_when`；Platform Soul Gate 只校验这些字段存在且可解析，不替 Agent 补语义字段。

## 7. T3 -> Skill 写入路径

### 7.1 谁负责写

语义写入者：

```text
Skill Distiller / Skill Writer Agent
```

它负责生成：

```text
evolution/skill_candidates/<candidate_id>/
  skill_pitch.md
  SKILL.md.draft        # only when generated by Skill Writer / Distiller LLM
  candidate_signal.md   # optional mechanical evidence; never an active draft
  eval_plan.md
  failure_cases.md
  manifest.json
```

评估 / 审核者：

```text
Skill Eval Runner
Skill Review Agent
```

提交执行者：

```text
Platform Skill Gate
```

Platform Skill Gate 只做权限、文件路径、命名、eval pass、rollback、audit 和 atomic promotion。它不能替 Skill Writer 改写 `SKILL.md.draft` 的语义内容，也不能把 `candidate_signal.md` 当成可激活 Skill 草稿，不能拆开候选包自行拼装 active skill。

### 7.2 流程

```text
Skill Candidate Batch
  -> read memory/t3/capabilities.md skill_seed blocks
  -> read supporting Segment Packages / T0 refs
  -> read existing skills catalog
  -> Skill Distiller writes skill_pitch.md
  -> Skill Writer writes SKILL.md.draft + references/templates/scripts/evals if needed
  -> mechanical candidate_signal.md may be used only as evidence
  -> Skill Eval Runner executes eval_plan / smoke tests when applicable
  -> Skill Review Agent reviews:
       evidence
       scope
       overlap with existing skills
       safety / tool governance
       eval result
  -> Platform Skill Gate hard-checks:
       no secrets
       allowed paths
       eval pass or explicit hold
       skill naming/version
       rollback ref
       install metadata
  -> atomic promotion to skills/<name>/
  -> skill catalog refresh
```

### 7.3 和 T2 -> T3 的相似点

相同：

- 都从 accepted / reviewed evidence 出发。
- 都由 LLM 写完整候选。
- 都由独立 reviewer 做 fresh review。
- 都由 Platform Gate 原子提交，不代写语义。

不同：

| 维度 | T2 -> T3 | T3 -> Skill |
|---|---|---|
| 输入 | Segment Packages | `capabilities.md` skill_seed + supporting evidence |
| 输出 | T3 memory XML blocks | Skill package |
| 是否需要 eval | 通常不需要外部 eval | 必须有 eval_plan；可执行能力必须有 eval / smoke test |
| Prompt 进入方式 | 动态 memory activation | progressive disclosure：catalog 先出现，full body 通过 `load_skill` 加载 |
| 风险 | 记忆污染 | 错误能力被复用、工具边界被误导 |

### 7.4 Skill eval 分级标准

所有 Skill Candidate 都必须有 `eval_plan.md` 和 `failure_cases.md`。最低门槛按能力类型分级：

| Skill 类型 | 最低 eval 门槛 | 必须失败的情况 |
|---|---|---|
| Prompt-only skill | LLM-authored `SKILL.md.draft` parse 成功；Skill Review rubric 全部分项 >= 3；至少 2 个 usage example / failure case；`candidate_signal.md` 不能替代 draft | 指令过宽、无触发条件、和现有 skill 高重叠 |
| Reference skill | Prompt-only 门槛 + references 可解析、路径合法、无敏感原文泄漏 | 引用不存在、引用不可追溯、引用包含 secrets |
| Template skill | Reference 门槛 + 模板变量 smoke render | 模板缺变量、渲染后破坏格式、输出越权 |
| Script skill | Template 门槛 + sandbox smoke test + artifact gate pass | 脚本访问未授权路径、网络/凭证越权、不可重复执行 |
| Tool-governance skill | Script 门槛 + ActionPreflight / policy simulation pass | 绕过审批、扩大工具权限、暗示直接执行敏感动作 |

Platform Skill Gate 只读取 eval 结果并 fail-closed；它不能补写 eval，也不能把未通过 eval 的候选“部分安装”。

## 8. T3 -> Soul 与 T3 -> Skill 的分流规则

| T3 block 类型 | 默认去向 | 说明 |
|---|---|---|
| `t3_episode` | 留在 T3 | 作为场景召回锚点，除非反复证明会改变身份/服务对象 |
| `t3_user_memory` | 大多留 T3，少数进 Soul | 只有跨任务、长期稳定、会影响所有协作方式的 user model 才进 Soul |
| `t3_worker_rule` | Soul 候选主来源 | 若应 always-on，进入 Soul；若只在特定任务相关，留 T3 |
| `t3_capability` | Skill 候选主来源 | 方法、SOP、验证步骤进 Skill；能力哲学才可能进 Soul |
| explicit memory overlay | 视内容分流 | 用户显式 identity 指令可进 Soul；普通显式记忆先 overlay，后续吸收进 T3 |

判断口诀：

```text
身份 / 权限 / 长期行为原则 -> Soul
方法 / SOP / 可复用流程 -> Skill
场景 / 事实 / 细粒度偏好 -> T3
显式但未验证 -> explicit overlay
```

## 9. 上下文组装影响

### 9.1 Soul

`soul.md` 进入 frozen identity 区。它必须短、稳、低噪音。每新增一条 Soul block，都等于永久增加未来 prompt 的默认行为压力。

### 9.2 T3

T3 仍然动态选择。`episodes.md` 负责先找到场景，`user.md` / `worker.md` / `capabilities.md` 按链接和相关性补充。

### 9.3 Skill

Skill 不常驻。默认只进入 skill catalog / tool-search surface。完整 `SKILL.md` 只有在相关、被加载或被 tool/agent 显式调用时进入上下文。

## 10. 初步红线测试清单

后续实现时至少需要这些红线测试：

- Dream / Soul Writer 不能直接写 `memory/t3/**`。
- Dream / Soul Writer 不能直接写 `soul.md`，只能写 soul candidate。
- Soul Memory Gate review 必须发生在最新 `soul_patch.md` 之后；旧 review 不能授权新 patch。
- Platform Soul Gate 不能机械改写 Soul patch 语义正文。
- `soul.md` 不允许出现 RuntimeTask id、Attempt id、trigger id、临时 artifact path。
- Frozen identity / authority boundary 默认 fail-closed。
- T3 `capabilities.md` 的 skill_seed 不能直接创建 active Skill。
- Skill active promotion 必须有 candidate package、eval_plan、review、Platform Skill Gate decision。
- Skill eval 门槛必须按 prompt/reference/template/script/tool-governance 类型分级；缺 eval 或 eval 不通过 fail-closed。
- Skill 文件不能由 Dream / T3 Consolidator 直接写。
- Skill catalog 可以动态刷新，但不能进入 frozen identity prefix。

## 11. 已定策略点

1. 不引入最高层 `source.md`。T3 -> Soul 只写 `soul.md`。
2. `soul.md` 目标格式升级为 `hive.soul.v2`；新 agent 直接使用 v2，旧 agent 由 Soul Candidate Batch 通过 Agent-authored `soul.md.next` 迁移。
3. Soul block 不做独立 manifest；使用 Soul Candidate Package manifest、evolution ledger、XML `source_refs` 和 Platform Soul Gate rollback snapshot。
4. Soul promotion 分为 auto-commit 和 owner/company approval；涉及 frozen identity、authority expansion、company boundary、敏感长期规则，一律确认。
5. Skill eval 按 prompt/reference/template/script/tool-governance 分级；所有候选必须有 `eval_plan.md` 和 `failure_cases.md`。
6. Dream 当前 JSON-only runtime contract 退役为 compatibility wrapper。canonical contract 是显式提交 `soul_pitch.md`、`soul_patch.md`、`soul.md.next`、`manifest.json` artifacts；wrapper 如果仍需要 JSON，只能承载 artifact path 和 status，不能承载最终语义插入逻辑。

## 12. 本文当前结论

T3 以上不是简单的“继续压缩一层”。它分成两条高影响 lane：

```text
T3 -> Soul = 把少数长期身份 / 权限 / 行为宪法变成 always-on。
T3 -> Skill = 把可复用方法 / SOP / failure shield 变成可验证能力胶囊。
```

它们和 T2 -> T3 一样，都遵守：

```text
LLM Writer -> independent review -> Platform Gate atomic commit
```

但因为 Soul 和 Skill 的 blast radius 更大，所以它们必须比 T2 -> T3 更严格：Soul 需要 identity / authority / prompt blast-radius gate；Skill 需要 eval / failure-case / capability-governance gate。

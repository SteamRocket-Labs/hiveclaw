# Hive 记忆系统干净循环整改方案（2026-06-17）

> 状态：下一轮代码整改前的设计文档。本文只定义责任边界、流程、去留判断和红线测试，不等于代码已经完成。
>
> 目标：回到“**四层记忆架构 + 三层蒸馏核心**”的原点，保留模型智能，去掉重复写入、重复判断、外挂增强和职责冲突。
>
> 最高设计原则：
>
> ```text
> LLM 负责判断、提炼、反思、归纳、候选生成；
> 平台负责证据引用、权限、去重、回滚、审计、最终落盘。
> ```
>
> 这里的“最终落盘”只表示 Platform Gate 对 LLM-authored Markdown patch 做 hard check、文件锁、审计、rollback ref 和原子提交；平台不是内容作者，不能机械生成或改写语义内容。
>
> T0 -> T2 的具体改造契约以 `docs/t0-to-t2-segment-package-redesign-2026-06-18.md` 为准；本文只保留全局循环和职责边界。

## 0. 结论

当前问题不是“四层记忆架构”本身错了，而是后来叠加的外围循环太多，导致职责重复、写入路径分裂、语义判断被机械逻辑稀释。

需要守住的核心是：

1. **四层记忆架构不动**：`T0 -> T2 -> T3 -> soul.md`。
2. **核心蒸馏链路不动**：T0 -> T2 由 Summary Agent + Learning Brain 完成，T2 -> T3 由 T3 Curator / Heartbeat Curator 完成，T3 -> `soul.md` 由 Dream Reconsolidator 完成；旧 `Extractor` 只能作为实现适配名，不再代表独立晋升权。
3. **T0 是原始证据层**：不需要 LLM 总结才存在，也不能直接晋升 T3。
4. **T2 是挂在稳定 ChatSession 下的 Segment Package**：`ChatSession.id` 是外部会话锚点，不能被人为切碎；T2 的人为/模型切分单位叫 `segment`，一个 segment 只对应一个 canonical Segment Package。外部 UI chat session / Feishu thread 可以包含多个 segments。
5. **Agent Markdown Wiki / Learning Vault 包含 T0 到 T3 的全量文件系统**：T0 是证据区，T2 是总结区，T3 是整理后的语义收敛区；不能把 Wiki 等同于 T3。
6. **T3 是长期 semantic layer**：只能由 T2 推导，并通过 `source_refs` 回溯 T0 做残差式证据验证。
7. **Learning Brain 重新定位为 T2 标签 / 结构化智能**：它参与 T2 层活动，读取 Summary Agent 产出的摘要和 T0 原始证据引用，专注生成 `labels.md`；不把标签塞回 `summary.md`，也不做最终晋升裁决。
8. **Memory Gate Agent 和 Platform Gate 分离**：Memory Gate Agent 是裁判层 LLM，负责独立上下文下的复查、打分、晋升建议；Platform Gate 是硬闸门，负责权限、证据引用、去重、回滚、审计、原子提交。Platform Gate 不是内容作者，不能机械生成或改写语义内容。旧称 `Memory Control Plane` 不再作为主命名，也不缩写成 `MCP`，避免和 MCP Servers 混淆。
9. **T3 保持原生 Markdown 记忆**：不接 OpenViking、Hindsight、Handside、Honcho 或其他外部记忆系统作为 T3 增强层。

本轮整改不是再加一层，而是把已经散开的组件重新归位：

- 能证明事实的，归到证据层。
- 能做事实总结的，归到 Summary Agent（旧 Extractor 只作为实现 adapter）。
- 能打标签和结构化的，归到 Learning Brain。
- 能复查打分和提出晋升建议的，归到 Memory Gate Agent。
- 能决定能不能写、怎么审计、怎么回滚的，归到 Platform Gate。
- 只能加速读取的，必须可重建、不可成为真相源。
- 只是企业知识库或团队共享知识的，必须留在 agent 记忆系统之外。

## 1. 最终原型：Agent Markdown Wiki / Learning Vault

最终原型不是再新增一个记忆系统，而是把现有四层记忆收敛成一个 **Agent Markdown Wiki / Learning Vault**：

```text
Agent Markdown Wiki / Learning Vault =
  Segment Packages = ChatSession-anchored T0 raw evidence + T2 segment summary/labels/review
  + T3 accepted views = cross-session accepted memory surfaces
  + source_refs-backed residual evidence verification
  + soul.md / source.md identity layer
  + Skill capability candidates
  + Memory Gate Agent review
  + Platform Gate write/read governance
```

核心取舍：

- **整个 vault 才是 Wiki**：Agent Markdown Wiki / Learning Vault 包含 T0 evidence stream、T2 segment packages、T3 semantic layer、`soul.md`、skills、evolution/audit sidecars。T3 只是其中的语义收敛层，不是 Wiki 的全部。
- **Markdown 是语义真相源**：T2、T3、soul、Skill 都必须能以 Markdown 形式被人和 LLM 直接读取。T0 raw evidence 也属于 Wiki 的 evidence area，但 T0 的 raw body 不承载语义结论。Graph、vector、search index、UI read model、关系图谱都只能是派生物，必须可从 Markdown + runtime artifacts 重建。
- **统一候选包 / patch envelope 模式**：T0 -> T2、T2 -> T3、T3 -> soul/source、T3 -> Skill 都遵守同一条权责原则：LLM Writer 生成完整语义候选，独立 LLM Referee 做最新候选复查，Platform Gate 只做硬阻拦、审计、rollback 和原子提交。不同层级的文件形态可以不同：T2 是 Segment Package，T3 是 Consolidation Patch Envelope，soul 是 Soul Patch Candidate，Skill 是 eval-backed Skill Candidate Package。
- **主梯度不可旁路**：`T0 -> T2 -> T3 -> soul.md` 是唯一主链路。T0 不能直接进入 T3，T3 也不能直接从 runtime logs 生成长期结论。
- **残差不是一层记忆**：残差只是一种 curation 读法。T3 curator 先读 T2，再沿 T2 的 `source_refs` 回到 T0 或原始 artifact 复核证据，避免错误总结被继续放大。
- **T3 的长期形态是跨 T2 的长期综述层**：T3 不再理解成几个孤立平铺文件，也不是整个 Wiki，而是对多个 T2 session summaries 的长期聚合、总领和稳定结论。T3 最终只收敛为四个 accepted memory files：`episodes.md`、`user.md`、`worker.md`、`capabilities.md`。导航、关系图、冲突视图只能是 T3 外部派生索引，不能放进 `memory/t3/`。
- **Skill 是从记忆中长出的能力胶囊**：重复成功的方法、失败 shield、可验证 SOP 先在 `capabilities.md` 中成为 capability / skill_seed，再形成 eval-backed `skill_candidate`，经 Skill Review、评估和 Platform Skill Gate 晋升为 `skills/<name>/SKILL.md`。Skill 不是 T3 子页，也不是语义记忆本体。
- **Workflow 不属于 GE / memory 的落盘产物**：Workflow 是独立执行控制体系，定义通常是人为设计的 JSON/YAML/DSL，或 follow Claude Code dynamic workflow 的运行时机制。Memory/Wiki 可以引用 workflow 相关证据和结果，但不生成 workflow definition，也不把 workflow candidate 作为记忆目标。
- **Memory Gate Agent 是语义裁判，Platform Gate 是硬闸门**：主观评分和晋升建议由独立上下文的 LLM 裁判完成；权限、source_refs 存在性、去重、审计、rollback 和原子提交由平台硬闸门完成。平台不能用 counters、regex、截断摘要替代 LLM 的判断、提炼、反思、归纳和候选生成，也不能机械生成或改写 Markdown 语义内容。

建议的 vault 形态：

```text
memory/
  index.md                          # OKF-style progressive disclosure index
  log.md                            # OKF-style chronological update log
  _meta/
    schema.md                       # vault schema, tag taxonomy, wiki rules
    tags.md                         # controlled tag taxonomy
    relation-types.md               # typed relation conventions
  sessions/                         # semantic packages grouped by stable ChatSession.id
    <chat_session_id>/
      index.md                      # session-level map; not raw truth
      segments/
        <segment_id>/
          summary.md                # T2 XML-style structured summary only
          labels.md                 # T2 thin engineering labels + event/fact labels
          review.md                 # Memory Gate Evidence Check + Promotion Check
          manifest.json             # Platform sidecar: source_refs, hashes, audit, rollback
  t3/                               # only final accepted memory files
    episodes.md                     # cue-rich episodic anchors; scenario-first recall entry
    user.md                         # stable user/principal preferences, constraints, working model
    worker.md                       # agent operating principles, conditional rules, redlines
    capabilities.md                 # reusable methods, SOPs, procedural memory, skill seeds
  lifecycle.json                    # sidecar, not semantic truth
  distillation_audit.jsonl          # audit, not semantic truth
soul.md                             # highest identity / behavioral constitution
# source.md                         # optional future naming alias; not decided here
memory/
  t0/
    sessions/
      <chat_session_id>/
        index.json                  # sequence/segment sidecar, not semantic truth
        segments/
          <segment_id>/
            source.md               # current T0 append-only MD/XML session ledger
logs/
  YYYY-MM-DD/                       # legacy/import compatibility only
    behavior/
      chat-*.md                     # legacy importable behavior evidence
    system/
      heartbeat-*.md                # legacy audit only, not semantic T0
      dream-*.md                    # legacy audit only, not semantic T0
runtime_artifacts/
  session_memory.md                 # runtime continuity, not durable memory
  session_learning_projection.jsonl # short-lived working context, not a memory layer
skills/
  <skill>/SKILL.md
  <skill>/references/
  <skill>/templates/
  <skill>/scripts/
  <skill>/evals/
evolution/
  evolution_ledger.jsonl            # candidate/eval/promotion/rollback truth
```

OKF 对我们的启发不是“再接一个外部系统”，而是定义 Markdown Wiki 的最小互操作规则：

- **Knowledge Bundle**：整个 `memory/` 是一个可被人、LLM、UI、search、graph viewer 共同消费的 bundle。
- **Concept Document**：T2 summary package 和 T3 semantic section 都要能用受控 frontmatter / metadata 被消费；T3 不靠大量目录表达语义。
- **Progressive Disclosure**：每个目录的 `index.md` 只列目录内容和摘要，帮助人和 agent 逐层打开，不做全文镜像。
- **Update Log**：每个需要审计的层级可以有 `log.md`，记录 creation/update/deprecation。
- **Graph-shaped links**：目录只表达梯度层级，bundle-relative Markdown links 和受控 tags 表达关系；consumer 应容忍未知 type、未知 frontmatter 字段和 broken links。
- **Citations / source_refs**：T3 正文里的稳定 claim 必须能回到 T2 summary package 和 T0 source packet；外部引用可进入 `references/`。

现有 `memory/learnings/*.md` 和兼容 T3 平铺文件可以先作为 compatibility views 存在；目标结构应向 OKF-style bundle 收敛，但收敛方向是层级更少、metadata 更强，而不是预设大量 topic folders。

与 CC / Codex / Hermes 的取舍关系：

- 借鉴 Claude Code 的 `CLAUDE.md` / rules / relevant memories 动态注入边界：稳定身份和规则进入 identity/context，具体记忆按相关性动态注入。
- 借鉴 Codex 的两阶段后台记忆流水线：单个 rollout 先抽取，随后由 consolidation agent 全局合并到文件系统。
- 借鉴 Hermes Agent 的两个点：`llm-wiki` 的 Markdown vault 形态，以及 Skill 作为 procedural memory / capability capsule。
- 不引入 Hermes/Hindsight/Honcho/OpenViking 等外部记忆程序作为 T3 增强层。外部系统最多作为企业 KB、source importer 或派生 read accelerator。

### 1.1 Markdown + XML Block 统一格式法则

Hive 的 Agent Markdown Wiki 采用 OKF 的最小互操作思想：Markdown 文件是知识容器，YAML frontmatter 是文件级 metadata，正文可被人和 LLM 直接读取。Hive 在 OKF 之上增加一条更强的内部约定：

```text
Markdown 文件 = 容器 / concept document
YAML frontmatter = 文件级 metadata、routing、schema、owner、version
XML block = 文件内部唯一合法的可切分语义块
每个 XML block = 独立可引用、可审查、可回滚、可检索的记忆单元
```

红线：

- 所有 Markdown 文件中需要分块的内容，必须统一使用 XML block；不能混用 Markdown heading、bullet、ad hoc delimiter 当作 block 边界。
- Markdown heading 只能用于人类阅读和章节说明，不能作为机器切块边界。
- YAML frontmatter 只描述文件级信息，不承载 block 级语义结论。
- block 内必须显式携带 `id`、`status`、`source_refs`、`confidence/stability`、`created_at/updated_at` 或等价字段。
- 平台可以校验 XML well-formed、schema、source_refs、权限、去重和回滚信息；不能替 LLM 改写 XML block 的语义内容。

统一 block 形态：

```xml
<memory_block id="..." schema="..." status="accepted|candidate|deprecated" stability="tentative|stable|contested">
  <summary>一句话说明这个 block 记住了什么。</summary>
  <applies_when>这个记忆在什么条件下适用。</applies_when>
  <does_not_apply_when>这个记忆在什么条件下不适用。</does_not_apply_when>
  <evidence>
    <source_ref path="memory/sessions/.../summary.md" block_id="..."/>
    <source_ref path="memory/t0/sessions/<chat_session_id>/segments/<segment_id>/source.md" range="seq-12..seq-18"/>
  </evidence>
  <links>
    <link type="related|derives|updates|contradicts" ref="..."/>
  </links>
</memory_block>
```

T2、T3、soul candidate、skill candidate 只是在 XML block 的 schema 和字段上不同；切块机制必须相同。

## 2. 四层记忆架构

### 2.0 书本类比

当前架构可以用一本书来理解：

```text
T0      = 原始引用资料 / 会议逐字稿 / 运行证据
T2      = 单个 ChatSession 内 segment 的结构化 summary、独立 labels、review 和短期 carryover
T3      = 跨多个 T2 后形成的长期综述、总领和稳定结论
Index   = 目录 / 导航 / 检索入口
soul.md = 纲领 / 宪法 / 世界观 / 长期人格原则
source.md = 若后续改名，可作为最高层文件别名；当前文档仍以 soul.md 为准
```

这个类比的边界是：

- `T0` 是证据，不做语义结论。
- `T2` 是对一个 ChatSession 内 segment 的第一次稳定加工；它不是最终长期记忆。长对话不能切碎 `ChatSession.id`，只能在同一个 session 下形成多个 segments。
- `T3` 是跨多个 T2 之后形成的长期综合记忆。
- `index.md` 只负责导航，不代表知识本体。
- `soul.md` 不是滚动全书摘要，而是少数经过长期验证、会影响 agent 身份和行为原则的最高层沉淀。

| 层级 | 作用 | 文件/存储 | 关键边界 |
|---|---|---|---|
| T0 | 原始行为证据、系统审计、可回放上下文 | `memory/t0/sessions/<chat_session_id>/segments/<segment_id>/source.md`、DB `ChatMessage`、`invocation_spans`、runtime artifacts | 不做语义结论；当前实现是 append-only session ledger，chat/one-off task/trigger/delegation/heartbeat/dream 都写 ledger；idle/close 只 seal segment；`logs/...` 是 legacy/import compatibility，不是 runtime session truth；不能直接写 T3 |
| T2 | 一个 ChatSession 内 segment 对应一个 Segment Package | `memory/sessions/<chat_session_id>/segments/<segment_id>/summary.md`、`labels.md`、`review.md` | `summary.md` 只放 XML-style structured summary；`labels.md` 单独放轻量标签；`review.md` 放裁判结论；Platform Gate 只负责格式、权限、证据、去重和原子提交 |
| T3 | 稳定长期语义记忆 / converged semantic layer | `memory/t3/episodes.md`、`user.md`、`worker.md`、`capabilities.md` | `memory/t3/` 只保存这四个最终 accepted memory files；不保存 index、chapter、关系索引、冲突索引、curation package；T3 patch 只能由 T2 Segment Packages 推导，并用 `source_refs` 回溯 T0 验证；必须经 Memory Gate Agent 复查，再由 Platform Gate 原子提交 |
| Soul / Source | 身份、使命、长期人格与不可轻易变更的行为原则 | `soul.md`；`source.md` 仅作为待定命名别名 | 必须由 Dream / Soul Writer Agent 产出语义 patch，经 Memory Gate Agent 复查，再由 Platform Gate 原子提交；平台不能机械生成或改写内容 |

### 2.1 ChatSession、T0 Raw Stream 与 T2 Segment Package

当前代码里的 `ChatSession.id` 是稳定会话锚点，不等同于 T2 切分单位。T0 原始证据目前来自 append-only session ledger：`memory/t0/sessions/<chat_session_id>/segments/<segment_id>/source.md`，同时 DB `ChatMessage` / `invocation_spans` 作为可交叉校验的运行时读模型。`SESSION_IDLE`、`SESSION_CLOSE` 不再写旧 chat logs，而是 seal 当前 segment。one-off task、trigger、delegation、heartbeat、dream 这类非聊天运行事件也必须写入同一套 session ledger。旧 `logs/YYYY-MM-DD/**` 只作为 legacy/import compatibility，不应该被当成当前 session truth。

因此文档里的 `segment` 应统一改名为 **segment**：

```text
ChatSession.id = 外部会话锚点，稳定存在
T0 Raw Stream = 该 session 的原始证据流，按 append-only session ledger 分布在多个 sealed/open segments
T2 Segment = 对同一个 ChatSession 内某段证据区间的语义加工
```

T2 的 canonical body 仍然是 Markdown，不是数据库、不是 JSON、不是向量库，也不是外部记忆系统。但一个 Segment Package 必须拆开三类内容：

- `summary.md`：LLM-authored structured summary，给人和模型读。
- `labels.md`：Learning Brain 生成的受控标签，给检索、聚合和治理读。
- `review.md`：Memory Gate Agent 生成的证据复查和晋升建议。

一对一红线：

```text
一个 ChatSession 内的一个 segment_id
  -> 只能对应一个 canonical Segment Package
```

禁止：

- 多个 T2 对应同一个 `segment_id`。
- 用 T2 segment 反向切碎或改写 ChatSession / T0 raw stream。
- T2 只保存零散事件 bullet，没有结构化 summary。
- T3 从 T0 直接猜长期结论，绕过 T2。

一个 Segment Package 必须同时具备：

1. Summary Agent 写出的结构化 XML-style summary。
2. Learning Brain 写出的独立 `labels.md`。
3. Memory Gate Agent 写出的 Evidence Check 和 Promotion Check。
4. `source_refs`、`chat_session_id`、`segment_id` 和 source range，保证可以回到唯一 T0 证据区间。

目标文件形态：

```text
memory/sessions/<chat_session_id>/
  index.md
  segments/
    <segment_id>/
      summary.md
      labels.md
      review.md
      manifest.json
```

当前 `memory/t2/**`、`memory/learnings/insights.md`、`errors.md`、`requests.md` 可以作为 compatibility views，但最终要迁移成 Segment Package 的派生视图，而不是继续靠文件名表达语义分类。

推荐 package 形态：

构建期 refs 不落成 canonical Markdown 文件。`T0ToT2PackageBuilder` 在 staging 中生成：

````json
{
  "schema_version": "t2.source_bundle.v1",
  "agent_id": "agent-xxx",
  "tenant_id": "tenant-xxx",
  "session_id": "session-xxx",
  "segment_id": "seg-0001",
  "source_range": "seq=12..18",
  "distillation_scope": "semantic_candidate",
  "source_refs": ["t0://session/session-xxx/segment/seg-0001#seq=12..18"],
  "message_refs": ["message://..."],
  "span_refs": ["span://..."],
  "artifact_refs": []
}
````

落盘后 refs 进入 Segment Package 的 `manifest.json` 和各语义文件内部的 `source_refs`，不再单独维护 `raw_refs.md`。

`manifest.json`：

````json
{
  "schema_version": "t2.segment-package.v1",
  "package_id": "t2pkg-xxx",
  "agent_id": "agent-xxx",
  "tenant_id": "tenant-xxx",
  "session_id": "session-xxx",
  "segment_id": "seg-0001",
  "source_refs": ["t0://session/session-xxx/segment/seg-0001#seq=12..18"],
  "source_hashes": {
    "memory/t0/sessions/session-xxx/segments/seg-0001/source.md": "<sha256>"
  },
  "content_hashes": {
    "summary.md": "<sha256>",
    "labels.md": "<sha256>",
    "review.md": "<sha256>"
  },
  "status": "reviewed",
  "rollback_ref": "..."
}
````

T0 真相源仍是 `memory/t0/sessions/<chat_session_id>/segments/<segment_id>/source.md`；DB `ChatMessage`、`invocation_spans` 只用于交叉校验和运行时读模型。

`summary.md`：

````markdown
---
type: T2 Segment Summary
schema_version: t2.segment_summary.v1
session_id: session-xxx
segment_id: seg-0001
source_range:
  source_ref: t0://session/session-xxx/segment/seg-0001#seq=12..18
segment_hash: "<sha256>"
status: active
created_at: 2026-06-17T00:00:00Z
---

# Session Summary

Summary Agent 对一个 T0 segment source range 的事实摘要。这里必须覆盖用户目标、场景线索、事件边界、方法轨迹、工具结果、纠正、决策、未决问题和产物。

T2 的主体不是一堆标签，而是一个结构化 summary。推荐使用 Markdown 文件承载 XML-style semantic body：Markdown 继续作为人和 LLM 的真相源，XML-style body 让平台可以做 schema、source_refs 和完整性校验。

```xml
<t2_summary schema="t2.summary.v1" completeness="closed">
  <overview>
    本 segment 中发生了什么，用户真正想完成什么，最后停在什么状态。
  </overview>

  <event_segments>
    <event id="E1" status="closed" salience="high">
      <boundary>
        <start source_ref="logs/2026-06-17/behavior/chat-1126-abcd.md#turn-12"/>
        <end source_ref="logs/2026-06-17/behavior/chat-1126-abcd.md#turn-18"/>
      </boundary>
      <user_intent>用户想重新定义 Hive 记忆系统中 T2/T3 的职责边界。</user_intent>
      <scene_context>围绕 memory、Learning Brain、T3 Wiki、Dream 写入边界的连续架构讨论。</scene_context>
      <retrieval_cues>
        <cue>之前讨论 T2/T3 记忆系统怎么切分</cue>
        <cue>记忆系统不要机械化、要保持模型智能</cue>
      </retrieval_cues>
      <method_trace>先区分 Summary Agent、Learning Brain、Memory Gate Agent、Platform Gate，再定义 T2/T3 package。</method_trace>
      <decisions>
        <decision>Learning Brain 在 T2 负责结构化标签，不做最终晋升裁决。</decision>
      </decisions>
      <open_questions>
        <question>T3 的 episode anchor、capability memory、worker rule、user memory 是否需要进一步拆分 schema。</question>
      </open_questions>
      <outcome_artifacts>
        <artifact path="docs/memory-clean-loop-refactor-plan-2026-06-17.md"/>
      </outcome_artifacts>
      <source_refs>
        <source_ref>logs/2026-06-17/behavior/chat-1126-abcd.md#turn-12</source_ref>
        <source_ref>logs/2026-06-17/behavior/chat-1126-abcd.md#turn-18</source_ref>
      </source_refs>
    </event>
  </event_segments>

  <short_term_carryover>
    <item status="open" source_ref="logs/2026-06-17/behavior/chat-1126-abcd.md#turn-18">
      下次继续时需要细化 T3 各 accepted view 的 block schema 和检索策略。
    </item>
  </short_term_carryover>

  <promotion_hints>
    <hint target="t3_candidate" reason="跨多轮形成稳定架构原则，需要进入 T3 进一步综合。"/>
  </promotion_hints>
</t2_summary>
```
````

`labels.md`：

````markdown
---
type: T2 Segment Labels
schema_version: t2.segment_labels.v1
session_id: session-xxx
segment_id: seg-0001
summary_ref: memory/sessions/session-xxx/segments/seg-0001/summary.md
---

# Engineering Labels

Engineering Labels 是系统/治理标签，不是语义记忆本体。它们必须尽量薄，只保留平台无法从正文可靠推导、但治理必须读取的字段。

```yaml
source_integrity: complete
sensitivity: PL1
systems: [memory, workflow, prompt_context]
risk_flags: []
```

# Event Labels

Event / Fact Labels 是轻量索引，不重复 XML summary 正文。用户身份、agent 归属、tenant、公开/私有边界属于 ChatSession、`manifest.json` 和 platform metadata，不由 LLM 在这里推断。

```yaml
events:
  - id: E1
    event_type: correction
    memory_domain: semantic_memory
    subjects:
      projects: [hive_memory_system]
    outcome: accepted
    actionability: t3_candidate
    stability: evolving
    salience: high
    source_refs:
      - logs/2026-06-17/behavior/chat-1126-abcd.md#turn-17
  - id: E2
    event_type: decision
    memory_domain: policy_memory
    subjects:
      projects: [hive_memory_system]
    outcome: accepted
    actionability: t3_candidate
    stability: stable
    salience: high
    source_refs:
      - logs/2026-06-17/behavior/chat-1126-abcd.md#turn-18
```
````

`review.md`：

````markdown
---
type: T2 Review
schema_version: t2.review.v1
session_id: session-xxx
segment_id: seg-0001
summary_ref: memory/sessions/session-xxx/segments/seg-0001/summary.md
labels_ref: memory/sessions/session-xxx/segments/seg-0001/labels.md
---

# Evidence Check

裁判检查 `summary.md` 和 `labels.md` 是否忠实于 `manifest.json` / `source_refs` 指向的 T0 证据区间。

```yaml
evidence_score: 0.92
missing_refs: []
hallucination_risk: low
contested_points: []
```

# Promotion Check

Promotion Check 不是“已经晋升”，也不是单独文件。它只是裁判对 `summary.md` 中哪些内容值得进入下一层的建议。

```yaml
semantic_importance: 0.88
stability_score: 0.81
promotion_decision: promote_candidate
target: memory/t3/episodes.md
rationale: "该 session 明确修正了记忆系统的职责边界。"
```
````

`memory/lifecycle.json`、access telemetry、conflict/revalidation 这类文件是 **Platform Gate 的 sidecar / control metadata**，不是 T2 主体。它们可以解释、治理、隐藏、归档 Markdown package，但不能替代 Markdown package 成为语义记忆本体。

#### 2.1.1 不完整 / 无限 Session 的处理

`Segment Package` 的一对一关系不能简单绑定 UI chat session 或 Feishu thread。飞书、微信、公开 agent 对话都可能无限延长，也可能聊到一半中断。因此 T2 的真实输入单位是同一个 ChatSession 下的 **segment**，而不是新的 ChatSession。

这里必须区分三个概念：

```text
ChatSession.id
  = 外部会话锚点，由 channel/session routing 决定；可以很长，不承担语义闭合职责。

Segment Window
  = 在一个 ChatSession 内由语义边界、时间边界、上下文压力或 runtime task 边界切出的处理窗口。

Segment Package
  = 对一个 Segment Window 的 T2 加工产物：summary.md + labels.md + review.md。
```

```text
一个 ChatSession.id
  -> 一个 T0 raw stream
  -> 多个 T2 segments
  -> 每个 segment 一个 canonical Segment Package
```

segment 可以由这些边界切出：

- 明确的 runtime task / web chat turn / workflow step 完成。
- 用户明显换题、目标改变、开始新任务。
- 关键事件闭合：问题被解决、产物被交付、决策被确认、纠错被接受。
- `SESSION_IDLE`：当前 websocket 默认 `WS_IDLE_DREAM_SECONDS=180` 秒后触发 idle hook，写增量 T0 和 DB session summary。
- `SESSION_CLOSE`：当前 websocket 默认 `WS_IDLE_TIMEOUT_SECONDS=3600` 秒后触发 close hook。
- token/context pressure 触发 rolling checkpoint。
- channel 长对话达到消息数、工具调用数或时间上限。

对外 UI 里的一个“会话”对应稳定 ChatSession，可以包含多个 segments；一个 segment 仍然只产生一个 Segment Package。这样既保留 `session_id`，又不会让无限 session 阻塞 T2 总结。

断连规则必须对齐当前代码事实：

- 浏览器 WebSocket 断开只会 `manager.disconnect`，不会自动创建新 ChatSession。
- 如果前端重连时继续带同一个 `session_id`，后端校验后复用该 ChatSession。
- 如果重连没有带 `session_id`，后端会找该 user + agent 最近的 ChatSession；没有最近 session 才新建默认 session。
- 用户点击“新建对话”或外部 channel 产生新的 `external_conv_id`，才应形成新的 ChatSession。
- 因此“30 分钟内回来是否新会话”不能写成记忆层规则；它是 channel/session routing 规则。记忆层只看实际 `chat_session_id`，再在该 session 内做 segment continuation。

不完整 segment 不丢弃，而是写成 `completeness="open"` 或 `completeness="rolling_checkpoint"` 的 `summary.md`：

```xml
<t2_summary schema="t2.summary.v1" completeness="rolling_checkpoint">
  <overview>用户讨论了 Web3 调研方法，但尚未确认最终 SOP。</overview>
  <event_segments>
    <event id="E1" status="open" salience="medium">
      <user_intent>想复用之前某次 Web3 调研方法。</user_intent>
      <scene_context>用户只提到“之前那次调研”，尚未给出明确目标对象。</scene_context>
      <retrieval_cues>
        <cue>之前那次 Web3 调研</cue>
        <cue>用那个方法再调研一遍</cue>
      </retrieval_cues>
      <open_questions>
        <question>具体要调研哪个项目或领域？</question>
      </open_questions>
      <source_refs>
        <source_ref>t0://session/chat-session-id/segment/seg-20260617#seq=4..6</source_ref>
      </source_refs>
    </event>
  </event_segments>
  <short_term_carryover>
    <item status="open">下次用户回来时，优先恢复“Web3 调研方法复用”这个未闭合意图。</item>
  </short_term_carryover>
  <promotion_hints>
    <hint target="recall_only" reason="事件未闭合，不能晋升 T3。"/>
  </promotion_hints>
</t2_summary>
```

处理规则：

- open / rolling checkpoint 可以进入 prompt 的短期连续性区域，但不能直接进入 T3。
- 如果用户后续在同一个或新的 ChatSession 继续同一件事，新 segment 的 `summary.md` 用 `continues_from` 引用上一份 open segment，而不是改写历史事实。
- 如果后来事件闭合，由新的 closed Segment Package 汇总 open segment chain，再进入 T3 intake。
- 如果长期没有继续，open segment 归档为 recall-only evidence；它仍可被检索，但不作为长期结论。
- Platform Gate 可以校验 XML 是否 well-formed、`source_refs` 是否存在、`completeness` 是否允许 promotion；不能替 Summary Agent 补写语义内容。

状态机：

```text
open
  -> rolling_checkpoint      # 长上下文压缩、idle、token pressure、用户暂离
  -> closed                  # 事件闭合、决策完成、产物交付、纠错确认

rolling_checkpoint
  -> open                    # 用户回来继续同一件事
  -> closed                  # 后续 segment 把前序 open chain 收束
  -> archived_recall_only    # 长期无继续，只保留召回证据

closed
  -> reviewed                # Memory Gate 复查完成
  -> absorbed                # 被 T3 Patch Envelope 引用并落入 T3 accepted memory file
```

晋升规则：

- `open` 和 `rolling_checkpoint` 永远不能直接生成 T3 Patch Envelope。
- `closed + reviewed` 才能被 T3 Curator 读取。
- 如果一个长期事件跨多个 open/rolling segments，必须由最后的 closed Segment Package 显式引用 `continues_from` chain，再作为 T3 intake 的入口。
- T3 只看成熟 Segment Packages；残差回看 T0 必须通过 T2 的 `source_refs`。

#### 2.1.2 短期记忆与压缩

短期记忆不是新的长期层级，而是 open / rolling Segment Package 的 prompt 激活策略：

```text
open T2 / rolling checkpoint
  -> active short-term carryover
  -> prompt dynamic memory area
  -> 后续 segment 继续、闭合、归档或晋升
```

压缩触发时不能只生成普通聊天摘要。它必须生成或更新 rolling Segment Package，并保留：

- 用户当前目标和未闭合意图。
- 用户使用过的自然语言召回线索。
- 已经确认的事实、纠错和决策。
- 方法轨迹和待复用步骤。
- 产物路径和外部系统引用。
- `source_refs`、range、hash、artifact path。
- 下一步应恢复的 open thread。

这解决两个问题：

- **无限 session**：通过 segment + rolling checkpoint 切成可治理片段。
- **碎片化 session**：即使事件未闭合，也会留下短期 carryover，不会因为“不够 T3”而从 agent 视野里消失。

### 2.2 T2 Summary 与 Labels 的分工

T2 不应该把所有维度都塞进标签，也不应该把标签塞进 summary。正确分工是：

```text
summary.md = 可读的结构化叙事，保留场景、事件边界、方法轨迹、未决问题和短期 carryover
labels.md  = 可控的索引和治理标签，服务检索、T3 聚合、权限和 review
review.md  = 独立裁判复查和晋升建议
```

这样可以避免两个问题：

- `summary.md` 太长、太像机器表格，导致人和 LLM 都难读。
- `labels.md` 太重、重复叙事正文，导致标签系统变成第二份 summary。

T2 标签体系仍分两块。工程向标签用于权限、风险、系统路由；事件向标签用于表达“这个 segment 里到底发生了什么”。T3 晋升时主要依赖 `summary.md` 的 XML body + `labels.md` 的轻量索引做语义聚合，工程向标签只做治理辅助。

Learning Brain 的主要职责就是在 T2 层完成这两类标签：

```text
T0 raw stream segment
  + Summary Agent 的摘要
  -> Learning Brain
  -> labels.md
```

#### 2.2.1 工程向标签 / Control Metadata

这部分给系统和平台治理使用，不代表记忆内容本体。工程标签要尽量少，避免把 T2 变成系统分类垃圾桶。

| 维度 | 允许值 | 作用 |
|---|---|---|
| `source_integrity` | `complete`、`partial`、`replayed`、`missing_refs` | T0 source 是否完整 |
| `sensitivity` | `PL0`、`PL1`、`PL2`、`PL3`、`PL4` | 可见性和写入闸门 |
| `systems` | `_meta/tags.md` 维护，例如 `memory`、`workflow`、`auth`、`railway`、`prompt_context` | 涉及的工程系统；只做粗粒度 |
| `risk_flags` | `privacy_sensitive`、`cross_tenant`、`security_relevant`、`production_impact`、`policy_conflict`、`evidence_gap` | 风险和治理提示 |
| `confidence` | `0.0-1.0`，按 T0->T2 prompt rubric 公式计算并 round 到 `0.05` | 工程置信分，不是模型自报概率；由 evidence coverage、source integrity、label specificity、internal consistency、closure score 和 penalties 组成 |
| `package_status` | `open`、`rolling_checkpoint`、`closed`、`reviewed`、`absorbed`、`archived` | Segment Package 生命周期 |

工程标签必须有可量化边界：`confidence`、`source_integrity`、`risk_flags`、`package_status` 都必须在 prompt 中给出判定公式或枚举标准。事件 / 事实标签可以依赖 LLM 的语义能力，但工程标签不能靠“感觉”选择；证据不足时必须显式标 `missing_refs`、`unknown` 或 `evidence_gap`。

#### 2.2.2 事件 / 事实向标签

这部分才是 T2 到 T3 的主要索引基准。它贴近事实、事件、项目和结果，而不是贴近代码模块。注意：完整叙述写在 XML summary 里，Event Labels 只保留受控枚举和轻量索引。`actor_user_id`、`tenant_id`、`agent_id`、公开/私有边界属于 ChatSession、`manifest.json` 和 platform metadata，不由 LLM 在 Event Labels 里重新推断。

| 维度 | 允许值 | 作用 |
|---|---|---|
| `event_type` | `instruction`、`correction`、`decision`、`preference`、`constraint`、`observation`、`problem`、`resolution`、`open_question`、`reference`、`relationship`、`artifact` | 发生了什么 |
| `memory_domain` | `working_memory`、`episodic_memory`、`semantic_memory`、`procedural_memory`、`preference_memory`、`policy_memory`、`relationship_memory`、`project_memory` | 属于哪类记忆 |
| `subjects.projects` | project key / repo / product area | 事件涉及的项目或产品区域 |
| `cue_terms` | 受控短词数组 | 从 XML `retrieval_cues` 抽出的检索短词 |
| `outcome` | `accepted`、`rejected`、`pending`、`blocked`、`fixed`、`deployed`、`contested`、`superseded` | 事件结果 |
| `actionability` | `recall_only`、`t3_candidate`、`soul_candidate`、`skill_candidate`、`workflow_reference_hint`、`archive` | 后续可能去向 |
| `stability` | `ephemeral`、`short_lived`、`evolving`、`stable` | 是否适合长期聚合 |
| `completeness` | `open`、`rolling_checkpoint`、`closed` | 事件是否闭合 |
| `salience` | `low`、`medium`、`high`、`critical` | 未来召回和 review 优先级 |

原则：

- 文件夹表达 package 边界：`memory/sessions/<chat_session_id>/segments/<segment_id>/` 表达单 T2 Segment Package；`memory/t3/` 不再表达 package 边界，只表达最终 accepted views。
- 工程向标签表达治理位置；事件向标签表达事实内容。
- T3 主要按事件向标签聚合，而不是按工程模块分类。
- 不要再用大量 T3 topic folders 表达语义。
- 关系用 `[[wikilinks]]`、`related`、`source_refs`、`relation-types.md` 表达。
- tags 是检索和治理辅助，不是模型智能的替代品；LLM 仍负责判断、提炼、反思、归纳和候选生成。

### 2.3 T3 Accepted Views：跨 Session 的长期收敛

T3 和 T2 复用同一套模式，但标签语义不同：

```text
T2 标签 = session 内发生了什么
T3 标签 = 多个 session 共同证明了什么
```

T3 不能只是把多个 T2 摘要拼在一起，也不能再在 `memory/t3/` 下制造一层 `chapters/`。T3 的本质是收敛，不是扩散：越往上，文件数量和结构应该越少，语义密度应该越高。

**红线：`memory/t3/` 只保存四个最终 accepted memory files：`episodes.md`、`user.md`、`worker.md`、`capabilities.md`。它不保存 `index.md`、`relations.md`、`contradictions.md`、`chapters/`、curation package 或 audit。**

T3 Curator / Writer Agent 接手一个或多个已经完成 review 的 Segment Packages 后，产出的是待审的 **T3 Patch Envelope**，不是 `memory/t3/chapters/<id>/sources.md/synthesis.md/review.md`。Patch Envelope 可以进入 `evolution/evolution_ledger.jsonl`、ActivityLog 或专门的 audit/candidate 表；它是候选和审计材料，不是语义真相源。只有 Memory Gate Agent 审核通过、Platform Gate 完成硬检查后，最终 patch 才能原子写入 `episodes.md`、`user.md`、`worker.md`、`capabilities.md`。

T3 的主设计目标不是“把知识按工程 topic 分类”，而是让 Agent 能像人一样从 cue 进入记忆：

```text
用户自然提问：
  你还记不记得我之前让你用某个方式调研过一个领域？

T3 recall path：
  episodes.md 通过 scene_context / cue_terms 找到那次场景锚点
  -> 跟随 links_to 找到 capabilities.md 中的可复用方法
  -> 同时读取 user.md / worker.md 中相关偏好、约束和红线
  -> 必要时沿 source_refs 回到 T2/T0 复核原始证据
```

因此，T3 accepted views 不再以 `canon.md / relations.md / contradictions.md` 作为主结构。新的 T3 收敛面只有四个稳定 Markdown 文件：

| Accepted view | 记忆类型 | 解决的问题 | 不能做什么 |
|---|---|---|---|
| `episodes.md` | episodic / scene memory | 保存跨 session 后仍值得召回的“事件锚点、场景线索、用户当时怎么描述、后来如何回忆” | 不保存 T0 原文，不替代 Segment Package |
| `user.md` | stable user / principal memory | 保存稳定用户偏好、工作方式、边界、长期上下文、关系模型 | 不保存单次临时偏好 |
| `worker.md` | semantic policy / agent operating memory | 保存 agent 自身应该遵守的条件化工作原则、红线、applies_when / does_not_apply_when | 不写最高身份原则；最高原则仍进 `soul.md` |
| `capabilities.md` | procedural memory | 保存可复用方法、SOP、渐进式能力胶囊、Skill seed | 不直接创建 `skills/<name>/SKILL.md` |

导航、关系图、冲突视图只能由读取层或数据库 cache 从这四个文件实时生成 / 异步重建；它们不进入 `memory/` 文件树，不是 T3 文件，不是语义真相源，也不能承载没有四个 accepted memory files 支撑的独立结论。旧 `canon.md`、`relations.md`、`contradictions.md` 都应退役为 compatibility read-only view 或迁移输入，不再作为新的 T3 accepted view。原因是这些文件会把偏好、事实、原则、方法、冲突治理重新拆成横向桶，和 T3 收敛原则冲突。

T2 完成后的 handoff 条件：

```text
Segment Package ready for T3 intake =
  summary.md exists
  labels.md exists
  review.md exists
  manifest.json exists
  review.md / Evidence Check is not failed
  review.md / Promotion Check is not archive-only
```

T3 Curator 接手时必须读取：

1. 当前待处理 Segment Package 的 `summary.md`、`labels.md`、`review.md` 和 `manifest.json`。
2. 与同一主题相关的其他成熟 Segment Packages。
3. 当前 T3 accepted memory files：`memory/t3/episodes.md`、`user.md`、`worker.md`、`capabilities.md`。
4. 必要时读取运行时生成的 navigation / relation / conflict read model；这些 read model 不是 Markdown 记忆文件。
5. `_meta/tags.md` 和 `relation-types.md`。

第二轮 Agent 的第一步不是直接写 T3，而是给出 intake decision：

| Decision | 含义 | 后续 |
|---|---|---|
| `archive_only` | 该 Session 只适合保留为历史证据，不形成长期结论 | 标记已处理，不写 T3 |
| `hold_for_more_evidence` | 信息有价值但证据不足，需要更多 session 支持 | 留在 review queue |
| `patch_episodes` | 应写入或更新场景式记忆锚点 | 生成 `episodes.md` patch envelope |
| `patch_user` | 应写入或更新稳定用户模型 | 生成 `user.md` patch envelope |
| `patch_worker` | 应写入或更新 agent 条件化工作原则 | 生成 `worker.md` patch envelope |
| `patch_capabilities` | 应写入或更新 procedural memory / skill seed | 生成 `capabilities.md` patch envelope |
| `read_model_refresh` | 只刷新关系、导航或冲突 read model | 生成 read-model rebuild request，不写 Markdown memory |
| `soul_candidate_after_t3` | 可能影响最高层身份/原则 | 先进入 T3，再交给 Dream，不直写 soul |

推荐 T3 Patch Envelope 结构：

```yaml
schema: t3_patch_envelope.v1
candidate_id: t3-candidate-2026-06-17-001
target_view: episodes
consolidation_mode: create_anchor
source_segment_refs:
  - memory/sessions/<chat_session_id>/segments/<segment_id>/summary.md#E1
targeted_t0_refs:
  - logs/2026-06-17/behavior/chat-1126-abcd.md#turn-12
proposed_patch: |
  <!-- LLM-authored XML block to insert/update in the accepted view -->
labels:
  source_coverage: [...]
  cue_strength: strong
  stability: stable
  behavior_impact: behavior_guidance
conflict_notes: []
review_status: pending
```

这个 envelope 可以被审计和回滚系统引用，但不能作为长期记忆被 prompt 默认加载。Prompt 只加载已通过审核并落入 accepted views 的内容。

T3 标签采用“继承 + 升维”，不是完全复用 T2，也不是随机新建体系：

| 维度 | 允许值 | 作用 |
|---|---|---|
| `target_view` | `episodes`、`user`、`worker`、`capabilities`、`soul_candidate`、`read_model_refresh` | 该综合结果应该进入哪个 T3 accepted memory file；read model refresh 不属于 T3 写入 |
| `consolidation_mode` | `create_anchor`、`merge_anchor`、`extract_capability`、`update_rule`、`update_user_model`、`resolve_conflict`、`hold` | 这次收敛是在新建、合并、抽取方法、更新规则还是暂缓 |
| `source_coverage` | Segment Package id list / count / time range | 说明该 T3 patch 覆盖哪些 session |
| `cue_strength` | `weak`、`medium`、`strong` | 未来能否被用户自然语言线索召回 |
| `stability` | `tentative`、`stable`、`contested`、`deprecated` | T3 patch 稳定程度 |
| `behavior_impact` | `recall_only`、`behavior_guidance`、`identity_candidate`、`skill_candidate` | 是否影响行为或身份 |
| `affected_entities` | users / agents / projects / systems / files | 跨 session 受影响对象 |
| `contradiction_state` | `none`、`resolved`、`open`、`requires_owner_review` | 是否存在冲突 |
| `prompt_priority` | `low`、`normal`、`high`、`pinned_identity_candidate` | 是否应该进入动态 prompt |
| `freshness` | source date range / last verified at | 结论的新鲜度 |
| `rollback_sensitivity` | `low`、`medium`、`high` | 错误写入后的回滚敏感度 |

T3 审核通过只代表对应 patch 可以进入 T3 accepted views。它不能自动改 `soul.md` / `source.md`。如果 T3 结论具有身份或长期行为原则价值，它只能在 accepted view 中标记 `soul_candidate`，再交给 Dream / Soul Writer Agent 进入最高层流程。

#### 2.3.1 T3 文件级格式

四个 T3 文件都必须采用同一个文件级格式：

```markdown
---
type: hive_t3_memory
schema: hive.t3.<episodes|user|worker|capabilities>.v1
title: <Episodes|User|Worker|Capabilities>
owner_agent_id: <agent-id>
okf_version: "0.1-compatible"
updated_at: <iso8601>
---

<!-- XML blocks only below. Markdown headings are allowed for humans, but are not machine block boundaries. -->
```

T3 文件不是 T2 细节汇总。T2 保存 session/segment 事实细节；T3 只保存跨多个 T2 后稳定、可复用、未来可召回的记忆块。每个 T3 block 必须满足：

- 可单独检索：block 内有 `id`、`summary`、`cue_terms` 或 `applies_when`。
- 可证据回溯：block 内有 `source_refs`，指向 T2 Segment Package，必要时再由 T2 回到 T0。
- 可避免误用：block 内有 `applies_when` / `does_not_apply_when`。
- 可治理：block 内有 `status`、`stability`、`confidence`、`last_verified_at`。
- 可连接：block 内可用 `links` 指向其他 T3 block 或 skill candidate，但不能依赖外部 graph 文件才能理解。

`episodes.md` block：

```xml
<episode_memory id="ep_2026_06_memory_system_refactor" status="accepted" stability="stable" confidence="0.86">
  <summary>用户多轮讨论 Hive 记忆系统，核心诉求是让 T0/T2/T3/soul 回到干净的渐进式收敛链路。</summary>
  <scene_context>围绕 T3 文件收敛、Learning Brain、Memory Gate、XML block、OKF 形式展开的架构讨论。</scene_context>
  <recall_cues>
    <cue>之前讨论记忆系统怎么切 T3</cue>
    <cue>不要机械化破坏模型智能</cue>
    <cue>Markdown 里面统一用 XML block</cue>
  </recall_cues>
  <meaning>用户未来可能先用场景线索召回这次讨论，再要求复用其中的方法或原则。</meaning>
  <applies_when>用户问“之前那次讨论/调研/方案怎么做的”且没有直接说出方法名。</applies_when>
  <does_not_apply_when>用户只是询问当前代码事实或要求重新实时审计。</does_not_apply_when>
  <links>
    <link type="method" ref="cap_memory_architecture_review"/>
    <link type="worker_rule" ref="rule_discuss_before_architecture_edits"/>
  </links>
  <source_refs>
    <source_ref path="memory/sessions/<chat_session_id>/segments/<segment_id>/summary.md" block_id="event_memory_t3_design"/>
  </source_refs>
  <last_verified_at>2026-06-17T00:00:00Z</last_verified_at>
</episode_memory>
```

`user.md` block：

```xml
<user_memory id="user_pref_discuss_before_architecture_changes" status="accepted" stability="stable" confidence="0.9">
  <summary>用户在架构方案未收敛时明确要求先讨论，不要直接改实现或文档。</summary>
  <preference>架构、记忆系统、边界设计等高影响问题，先讨论清楚，再落文档，再进入代码。</preference>
  <applies_when>任务涉及记忆架构、Learning Brain、Memory Gate、T3/soul、系统边界或长期设计。</applies_when>
  <does_not_apply_when>用户明确说“开始改”“写进文档”“全部修复”或要求直接实现。</does_not_apply_when>
  <source_refs>
    <source_ref path="memory/sessions/<chat_session_id>/segments/<segment_id>/summary.md" block_id="decision_discuss_first"/>
  </source_refs>
  <last_verified_at>2026-06-17T00:00:00Z</last_verified_at>
</user_memory>
```

`worker.md` block：

```xml
<worker_rule id="rule_no_mechanical_memory_substitution" status="accepted" stability="stable" confidence="0.88">
  <summary>凡是需要判断、提炼、反思、归纳、候选生成的记忆步骤，必须由 LLM 完成；平台只负责治理和落盘。</summary>
  <rule>LLM 负责判断、提炼、反思、归纳、候选生成；平台负责证据引用、权限、去重、回滚、审计、最终落盘。</rule>
  <applies_when>Summary、Learning Brain、T3 Curator、Dream、Skill candidate 等语义处理路径。</applies_when>
  <does_not_apply_when>权限检查、source_refs 存在性校验、XML well-formed 校验、审计和原子写入。</does_not_apply_when>
  <source_refs>
    <source_ref path="memory/sessions/<chat_session_id>/segments/<segment_id>/summary.md" block_id="principle_llm_platform_boundary"/>
  </source_refs>
</worker_rule>
```

`capabilities.md` block：

```xml
<capability_memory id="cap_memory_architecture_review" status="accepted" stability="evolving" confidence="0.82" skill_candidate="true">
  <summary>针对复杂记忆系统改造，先画清层级和文件流，再定义 block schema、审查链路和红线测试。</summary>
  <when_to_use>用户要求重新设计记忆系统、蒸馏链路、T2/T3/soul 转化或类似长期知识架构。</when_to_use>
  <method>
    <step>先固定 canonical chain：T0 -> T2 -> T3 -> soul。</step>
    <step>区分 semantic truth、candidate、audit、read model。</step>
    <step>为每个 Markdown 文件定义 YAML frontmatter 和 XML block schema。</step>
    <step>最后写红线测试，禁止旧路径回流。</step>
  </method>
  <verification>必须引用当前代码路径、现有文档和 T2 source_refs；不能只靠抽象架构描述。</verification>
  <source_refs>
    <source_ref path="memory/sessions/<chat_session_id>/segments/<segment_id>/summary.md" block_id="method_memory_architecture_review"/>
  </source_refs>
</capability_memory>
```

Memory Gate Agent 对 T3 Patch Envelope 的 review 默认结构：

```markdown
# T3 Evidence Check

- 是否忠实于引用的 Segment Packages？
- 是否沿 source_refs 回看必要的 T0 raw evidence？
- 是否遗漏反证或冲突？

# T3 Promotion / Commit Check

- 是否足够稳定？
- 应该 create / merge / hold / archive？
- 目标是 episodes、user、worker、capabilities、read model refresh、skill candidate 还是 soul candidate？
- 是否允许 Platform Gate 原子提交？
```

### 2.4 上下文组装与 `save_memory` 归属

当前 prompt 不应该把整个 Wiki 全量塞进去。Wiki 是可检索的文件系统，prompt 只组装当前任务需要的 memory packet。

默认 prompt memory sections：

1. **Frozen Identity**：`soul.md` 总是进入固定身份区。
2. **Active Long-Term Memory**：按目标相关性、owner/company scope、敏感级、lifecycle 选取 T3 的 `episodes.md` / `user.md` / `worker.md` / `capabilities.md` 片段。
3. **Active Summary Memory**：选取尚未被 T3 吸收、刚被用户纠正、或比 T3 更新鲜的 T2 summary packages / package segments。
4. **Session Working Memory**：当前会话、session projection、runtime recovery context；只做短期上下文，不是 durable truth。
5. **Navigation Map**：由读取层从四个 T3 文件生成的 compact navigation snapshot，只在需要探索记忆空间、检索失败、或 agent 需要决定加载哪个文件时进入 prompt；不默认全文进入。
6. **Relation / Conflict Read Models**：由读取层从四个 T3 文件和 source refs 生成，只在 disambiguation、冲突复核、关系跳转时注入，不默认作为长期记忆正文进入 prompt。
7. **Residual Evidence**：T0 原文只在 T3 curation、debug、replay、争议复核时按 `source_refs` targeted load；普通 prompt 不加载 T0 raw body。

结论：

- `soul.md` 是 identity 必选项。
- navigation snapshot 是读取层 read model，不是 identity；它不应默认常驻 prompt，只能作为 compact map 动态注入。
- 长期记忆默认来自 T3 四个主 accepted views 的选中片段：先用 `episodes.md` 做场景召回，再按链接补 `capabilities.md`、`user.md`、`worker.md`；必要时补充高价值 T2 blocks。
- 短期记忆来自 session projection / current run state，并带 TTL。

用户显式调用 `save_memory` 时：

```text
save_memory
  -> Summary Agent / Learning Brain path when needed
  -> Segment Package T2 summary/labels or existing package update:
       evidence=user_stated
       actionability=t3_candidate | soul_candidate | skill_candidate | recall_only
       source=save_memory
  -> Memory Gate Agent review if it implies promotion
  -> Platform Gate final write
```

也就是说，`save_memory` 是显式高优先级信号，不是手动直写 T3 的路径。它默认进入 explicit memory overlay，并按权限即时激活，避免用户明确要求“记住”的内容被一天级别的 T2/T3 冷却拖住；后续如果内容稳定、低风险、source_refs 完整，再由 T3 Consolidation Batch 吸收为 T3 patch candidate，并仍必须经过 Memory Gate Agent 复查和 Platform Gate 原子提交。

### 2.5 Memory UI Tabs

为了避免把 file browser、audit、candidate、prompt recall 混在一起，Agent 记忆 UI 应收敛成这些 tab：

| Tab | 内容 | 不能做什么 |
|---|---|---|
| Overview | 健康度、最近变更、主要 T3 摘要、held candidates | 不展示 raw file dump 作为默认入口 |
| Evidence / T0 | source packets、行为证据、artifact refs、trace refs | 不做语义结论 |
| Summary / T2 | per-segment summary packages、XML-style structured summary、工程标签、事件/事实标签、review、source_refs | 不绕过 T3 直接改 soul/skill |
| Wiki / T3 | `episodes.md`、`user.md`、`worker.md`、`capabilities.md` 的 Markdown 渲染；关系图和冲突视图由读取层现场生成 | 不退回 `canon.md` 大杂烩，不在 `memory/` 下生成 derived Markdown 文件，不在 `memory/t3/` 下生成 index、relations、contradictions 或大量 topic folders |
| Prompt Context | 当前进入 prompt 的 `soul.md`、T3 snippets、T2 blocks、session projection 和 activation reasons | 不写记忆 |
| Held / Candidate Review | held / contested / duplicate / explicit-memory absorption / promotion candidates | 不替代治理层落盘 |
| Raw / Audit | advanced raw Markdown、lifecycle、distillation audit、invocation refs | 不作为普通用户默认知识视图 |

### 2.6 残差证据回溯规则

T2 -> T3 的蒸馏不能只看已经压缩过的 T2，否则会像没有 residual connection 的深层网络一样丢细节、丢语境、丢错误来源。但这个残差不是新增层，也不是 T0 直达 T3。

```text
T3 curation input =
  selected Segment Packages / T2 summaries
  + source_refs carried by those Segment Packages
  + targeted T0 evidence loaded through source_refs
  + current T3 semantic pages / index
  + optional short-lived session projection as context only
```

红线：

- 残差证据回溯不是一层新记忆。
- T0 不能绕过 T2 直接生成 T3。
- T3 candidate 必须引用至少一个 Segment Package 或 T2-derived candidate。
- T3 curator 可以沿 `source_refs` 回看 T0，以质疑、修正或拒绝 T2 总结。
- session projection 只能作为当前上下文提示，不是 durable semantic truth，也不能直接进入 T3/soul。

## 3. 蒸馏核心与裁判链路

原始“三层蒸馏”仍然保留，但责任拆得更清楚：

```text
T0 -> T2:
  Summary Agent
  + Learning Brain
  + Memory Gate Agents
  + Platform Gate

T2 -> T3:
  T3 Curator / Heartbeat Curator
  + Memory Gate Agents
  + Platform Gate

T3 -> soul.md:
  Dream / Soul Writer Agent
  + Soul Memory Gate Agent
  + Platform Soul Gate

T3 -> Skill:
  Skill Distiller / Skill Writer Agent
  + Skill Review / Eval Gate
  + Platform Skill Gate
```

| 层级 | 当前应承担的智能职责 | 输入 | 输出 | 不能做什么 |
|---|---|---|---|---|
| Summary Agent | 从一个 ChatSession-scoped T0 source range 生成 XML-style structured summary | T0 source range、消息、工具结果、Work Ledger evidence | `summary.md` candidate | 不能打最终晋升分；不能直接写 T3/soul/skill/workflow |
| Learning Brain | 生成轻量工程标签和事件/事实标签 | T0 source range、Summary Agent 摘要、source refs | `labels.md` candidate | 不能做最终晋升裁决；不能写 durable files |
| Memory Gate Agents | 独立上下文复查 T2/T3/soul/skill candidates，打分并给晋升建议 | T0 refs、Segment Package / T3 Patch Envelope、existing T3 accepted files、soul patch、skill candidate、promotion rules | `# Memory Gate Review`、promotion decision、review feedback | 不能最终提交；不能绕过 Platform Gate；不能用旧 review 授权新 patch |
| T3 Curator / Heartbeat Curator | 把多个成熟 Segment Packages 聚合成跨 session 的 T3 semantic candidate | Segment Packages、T2 source_refs 指向的 T0 evidence、当前 T3 accepted memory files、runtime/db read models | T3 Patch Envelope candidate | 不能让 T0 直达 T3；不能写 `memory/t3/`；不能顺手跑 skill/workflow/dream |
| Dream / Soul Writer Agent | 慢速全局重组、identity/soul 候选、最高层行为原则 patch | 稳定 T3、soul、候选 ledger、历史证据 | `soul_pitch.md` / `soul_patch.md` candidate；必要时输出 held T3 concern | 不能绕过 Soul Memory Gate Agent 和 Platform Soul Gate 直接改 soul；不能借 Dream 路径直接改 T3 |
| Skill Distiller / Skill Writer Agent | 从 T3 `capabilities.md` 的 capability / skill_seed 和 evidence 中形成可验证能力胶囊 | `capabilities.md`、Segment Packages、eval reports、failure cases | skill candidate package / eval report / promotion proposal | 不能把 Skill 写成 T3 页面；不能无 eval 晋升 active skill |
| Platform Gate | 权限、source_refs 存在性、去重、审计、rollback、原子提交 | reviewed candidates、source refs、principal context | Session/T3/soul/source/lifecycle/archive/evolution decision record | 不能替 LLM 做主观评分、语义晋升判断或内容改写 |

### 3.1 Review 问题

总结 Agent、Learning Brain、T3 Curator、Dream 都会出错，所以裁判层必须独立：

- **T2 Evidence Review**：检查 Segment Package 是否可追溯到唯一 T0，是否遗漏关键否定证据，是否把一次性事实误当长期记忆。
- **T2 Semantic Review**：检查事件标签是否准确，summary 是否忠实，是否足够支持 T3 聚合。
- **T3 Promotion Review**：检查 T3 candidate 是否真的从 T2 推导，是否沿 `source_refs` 回看证据，是否正确处理 contradiction / contested claim。
- **Dream Review**：检查 soul/lifecycle patch 是否有重复证据、owner/company 边界、frozen charter 冲突、rollback ref。

这些 review 由 Memory Gate Agents 执行；Platform Gate 只记录 decision、source refs、rollback refs、audit，并原子提交已通过的 LLM-authored patch。

### 3.2 渐进验证：验证强度跟影响范围走

验证不能只压在“打标签 / 写 frontmatter”这一步。T0 -> T2 -> T3 -> soul 的每一层都要有验证，但验证强度必须按影响范围递增，不能把所有普通记忆都做成重审批。

| 阶段 | 写入/变更对象 | 主要风险 | 默认验证强度 | 触发升级条件 |
|---|---|---|---|---|
| T0 capture | raw evidence / source packet | 数据污染、权限串租户、凭据泄漏 | 机械 hard gate：source 存在、tenant/agent/session 匹配、PL4 拒绝、审计 id | 跨租户、外部来源、疑似 prompt injection、敏感内容 |
| T0 -> T2 | per-segment XML-style structured Markdown summary package | 模型过度总结、误把临时信息写成长期记忆 | Summary Agent + Learning Brain + Memory Gate Agents；Platform Gate 做硬校验 | 低置信、与旧 T2/T3 冲突、用户明确纠错、权限/隐私敏感 |
| T2 -> T3 | semantic page / stable claim | 错误结论进入长期语义层并反复召回 | source_refs 回溯 T0、duplicate/contradiction 检查、可回滚 patch | 高频召回、会影响行为策略、跨用户/company、冲突未解 |
| T3 -> soul | identity / charter / long-term behavior | 身份和权限边界漂移 | Dream / Soul Writer proposal + fresh Soul Memory Gate review + Platform Soul Gate；frozen charter hard check；rollback ref | frozen charter、authority expansion、公司利益冲突 |
| Skill | capability source of truth | 错误 SOP 固化成能力 | T3 `capabilities.md` seed + Skill candidate package + eval + Skill Review + Platform Skill Gate | 外部动作、资金/客户/生产系统、高风险自动化 |
| Workflow reference | external execution system reference | 把记忆系统误当 workflow generator | 只允许 source_refs / evidence handoff；workflow definition 由 Workflow 系统治理 | 需要 JSON/YAML/DSL、durable state、gate/replay、人工设计或 dynamic workflow runtime |

核心原则：

```text
验证强度跟影响范围走，不跟“是不是标签”走。
```

因此：

- 给 T3 页面补一个低风险 tag，可以轻验证。
- 把 T2 总结晋升为 T3 稳定原则，需要中等验证和 source_refs 回看。
- 改 `soul.md`、晋升 Skill，需要强验证、最新 patch/candidate 的 fresh review、外部 eval 或 owner approval；Workflow 相关输出只能是证据引用或移交给 Workflow 系统的建议，不属于 memory lane / Platform Gate 的写入目标。
- 涉及权限、隐私、公司边界、身份原则，必须 hard gate fail-closed。

### 3.3 机械部分的保真规则

机械结构没有语义能力，所以它的职责不是“删掉不懂的东西”，而是 **保留证据、限制权限、标记不确定性、让 LLM 能继续判断**。

机械规则必须遵守：

- **不做语义裁剪**：不能因为关键词、长度、regex、计数器就把候选压成“无意义摘要”或直接丢弃。
- **不凭空补语义**：缺证据时写 `held` / `incomplete_context` / `missing_source_ref`，不能 synthesize memory。
- **不破坏原始证据**：T0 raw body append-only 或 immutable；需要标签时写 frontmatter/sidecar/index，不重写原始事实。
- **预算不足时保留指针**：大内容不能塞进 prompt 时，保留 source_refs、range、hash、artifact path，而不是截断后当完整事实。
- **失败时 hold 而不是硬删**：LLM 失败、schema 失败、冲突未解时保留 candidate/audit，等待重试或 Dream，不直接生成 durable memory。
- **secret/privacy 是例外**：PL4 credential 必须拒绝或占位，不能因为“保真”而落盘裸凭据。
- **read model 可重建**：navigation map、topic-map、graph、vector cache 只能由 Markdown truth + sidecars 重建；索引损坏不能反向改写语义。

这也是从 CC/Codex 借鉴过来的取舍：它们不是每条语义都找另一个模型重判，而是用 schema、secret scanning、pollution marker、sandbox、workspace diff、job lock、baseline 等机械约束防污染；Hive 在此基础上按影响范围增加渐进验证。

## 4. Agent 协作：运动员、裁判、平台硬闸门

记忆系统必须坚持“运动员”和“裁判”分离。

```text
运动员层：Summary Agent + Learning Brain
裁判层：Memory Gate Agents
平台层：Platform Gate
```

### 4.1 运动员层：Summary Agent + Learning Brain

Summary Agent 和 Learning Brain 不是新的记忆层，也不是每轮对话后的总分流器。它们在当前方案里的位置是：

```text
Summary Agent =
  ChatSession-scoped T0 source range -> XML-style structured summary

Learning Brain =
  labels.md with thin engineering labels + lightweight event/fact labels
```

Learning Brain 负责：

- 读取 Summary Agent 的摘要和 `manifest.json` / `source_refs` 指向的 T0 原始证据区间。
- 写 `labels.md` 中的工程向标签。
- 写 `labels.md` 中的事件 / 事实向标签。
- 维护 `_meta/tags.md` 中的受控标签体系和 alias。
- 为后续 T3 聚合生成可比对的结构化材料。

它不负责：

- 最终晋升裁决。
- 权限、去重、回滚或审计。
- 自己写 `memory/**`、`evolution/**`、`soul.md`、`skills/**`。
- 用 counters、regex、截断摘要替代 LLM 判断。

### 4.2 裁判层：Memory Gate Agents

Memory Gate Agent 是独立上下文的 LLM 裁判层。它不复用运动员的 prompt，也不参与原始总结。默认不是多个裁判文件，也不是必须多个裁判 Agent；它是一个 `review.md` 流程，内部有多个判断维度。它只读取：

```text
T0 source refs
Segment Package summary.md
Segment Package labels.md
XML-style structured summary
existing T3 context
promotion rules
```

`review.md` 默认分成两个主 section：

| Section | 负责判断 | 输出 |
|---|---|---|
| Evidence Check | `summary.md` / `labels.md` 是否忠实于 `manifest.json` / `source_refs` 指向的 T0 证据区间，source_refs 是否完整，是否有幻觉或遗漏反证 | `evidence_score`、`missing_refs`、`contested_points` |
| Promotion Check | 是否重要、稳定、可复用，是否值得进入 T3 / skill / soul_candidate / archive | `semantic_importance`、`stability_score`、`promotion_decision`、`target` |

Memory Gate Agent 可以做主观评分和晋升建议：

```text
review:
  evidence_score: 0.92
  semantic_importance: 0.88
  stability_score: 0.81
  promotion_decision: promote_candidate
  target: memory/t3/episodes.md
```

但它不能直接写 T3、soul、skill 或 workflow definition。

### 4.3 平台层：Platform Gate

Platform Gate 是**最终提交入口**，不是内容作者。它不是语义裁判，不负责判断“这件事值不值得记住”，也不能机械生成或改写 Markdown 语义内容；它只负责硬约束和原子提交：

```text
Memory Gate Agents
  做：复查、评分、解释、晋升建议
  不做：最终提交、回滚管理、权限放行

Platform Gate
  做：权限、source_refs 存在性、去重、审计、rollback ref、文件锁、原子提交
  不做：替模型做主观打分、语义晋升判断或内容改写
```

## 5. 标准运行流程

### 5.1 普通对话回合

```text
用户输入
  -> invoke_agent
  -> DB ChatMessage / invocation_spans / AgentActivityLog
  -> T0 raw stream: memory/t0/sessions/<session_id>/segments/<segment_id>/source.md + DB messages + invocation_spans
  -> segment boundary detector 判断是否 close / continue / rolling checkpoint
  -> source_bundle.json staging 输入包
  -> Summary Agent 读取 ChatSession-scoped T0 source range
  -> Summary Agent 生成 XML-style structured summary
  -> Learning Brain 读取 Summary + source_bundle/source_refs，生成 labels.md
  -> 形成唯一 Segment Package 的 T2 部分
  -> Memory Gate Agents 独立上下文复查、打分、提出晋升建议
  -> Platform Gate 执行权限、证据、去重、审计、rollback ref
  -> 原子提交 memory/sessions/<chat_session_id>/segments/<segment_id>/{summary.md,labels.md,review.md}
  -> 后续 T3 Curator / Dream 读取成熟 Segment Packages
  -> 沿 T2 source_refs 回看 T0
  -> T3 Curator 生成 T3 Patch Envelope
  -> envelope 内写 target-view labels 和 proposed accepted-memory-file patch
  -> Memory Gate Agents 复查 T3 candidate
  -> Platform Gate 原子提交到 memory/t3/{episodes.md,user.md,worker.md,capabilities.md}
  -> Retriever / Activation / Prompt Builder 读取最新状态
```

### 5.2 T0 -> T2

```text
ChatSession-scoped T0 source range
  -> Summary Agent:
       XML-style structured summary
       completeness=open | rolling_checkpoint | closed
  -> Learning Brain:
       labels.md
  -> Segment Package T2 section:
       Session Summary XML body
       labels.md
       short_term_carryover when open
       source_refs
       segment_hash
  -> Memory Gate Agents:
       evidence_score
       semantic_importance
       stability_score
       governance_decision
       promotion_decision
  -> Platform Gate:
       permission
       source_refs validation
       dedupe
       audit
       rollback ref
  -> memory/sessions/<chat_session_id>/segments/<segment_id>/
       summary.md
       labels.md
       review.md
```

T2 只总结一个 ChatSession 内的 segment source range。Summary Agent 和 Learning Brain 是运动员；Memory Gate Agents 是裁判；Platform Gate 是硬闸门。`completeness=open` 或 `rolling_checkpoint` 的 package 可以进入短期 prompt carryover，但不能进入 T3 promotion，除非后续 closed package 汇总并通过 review。

### 5.3 T2 -> T3

T2 完成后，由第二轮 Agent 接手：

```text
接手者：T3 Curator / Heartbeat Curator
输入：一个或多个 mature Segment Packages
输出：T3 intake decision；必要时生成 T3 Patch Envelope
审核：Memory Gate Agent review T3 Patch Envelope
提交：Platform Gate 原子提交到四个 T3 accepted memory files
```

T3 Curator 必须看：

- 每个候选 Segment Package 的 `summary.md`。
- 每个候选 Segment Package 的 `labels.md`。
- 每个候选 Segment Package 的 `review.md`，尤其是 Promotion Check。
- 每个候选 Segment Package 的 `manifest.json`。
- 当前 `memory/t3/episodes.md`、`user.md`、`worker.md`、`capabilities.md`。
- 必要时读取运行时生成的 navigation / relation / conflict read model；这些不是 Markdown memory 文件。
- `_meta/tags.md`、`relation-types.md`。

T3 Curator 可以给出的结论：

| 结论 | 含义 |
|---|---|
| `archive_only` | 不进入 T3，只保留 Segment Package |
| `hold_for_more_evidence` | 暂缓，等待更多 session |
| `patch_episodes` | 写入或更新场景式记忆锚点 |
| `patch_user` | 写入或更新稳定用户模型 |
| `patch_worker` | 写入或更新 agent 条件化工作原则 |
| `patch_capabilities` | 写入或更新 procedural memory / skill seed |
| `read_model_refresh` | 刷新 T3 外部 read model，不写 Markdown memory |
| `soul_candidate_after_t3` | 先进入 T3，再由 Dream 判断是否上升到 soul/source |

```text
T3 curation tick
  -> 选择成熟 Segment Packages / T2 summaries
  -> 读取每个 Segment Package 的 source_refs
  -> targeted load T0 evidence / runtime artifacts
  -> 读取当前 T3 episodes / user / worker / capabilities
  -> 必要时读取 runtime navigation / relation / conflict read model
  -> T3 Curator LLM 生成 consolidation_pitch.md
  -> 如 Memory Gate Agent 给出 editorial feedback / merge directives，T3 Curator 先吸收反馈
  -> T3 Curator / Writer Agent 生成 revised_patch.md
  -> revised_patch.md 内包含 target-view labels 和 proposed accepted-memory-file patch
  -> Memory Gate Agents 对最新 revised_patch.md 做 fresh final review：
       是否忠实于 T2/T0
       是否足够稳定
       是否应该进入 episodes / user / worker / capabilities / read model refresh / skill / soul / archive
  -> 如需修改，退回 T3 Curator / Writer Agent 重写
  -> Platform Gate 做 hard check、审计、rollback ref、原子提交
  -> 写 memory/t3/{episodes.md,user.md,worker.md,capabilities.md} 中对应文件
  -> 如有需要，异步重建 runtime/db read models
```

关键点：

- T3 的素材主入口是 T2。
- T0 只通过 T2 的 `source_refs` 被回看。
- Learning Brain 在 T2 阶段做 session/segment 标签；T3 阶段的 target-view labels 写在 T3 Patch Envelope 内，不需要单独标签文件。
- T3 的收敛结果只允许进入 `episodes.md` / `user.md` / `worker.md` / `capabilities.md`；关系和冲突只能在 T3 外部派生。
- Memory Gate Agent 做主观复查和晋升建议；最终提交前必须复查最新 patch，旧 review 不能授权新 patch；Platform Gate 做最终写入硬约束。

### 5.4 Dream

```text
Dream scheduled job
  -> 读取稳定 T3 semantic pages + soul + candidate/evolution ledger + preservation flags
  -> Dream Reconsolidator 做重组、冲突判断、identity/soul 提案
  -> Soul Writer Agent / Dream 生成 soul_pitch.md / soul_patch.md candidate
  -> Soul Memory Gate Agent 对最新 soul_patch.md 做 fresh review：
       证据是否足够
       是否和 T3 / soul 冲突
       是否影响 owner/company 边界
       是否需要回滚或重写
  -> 如需修改，退回 Dream / Soul Writer Agent 重新生成 patch
  -> Platform Gate 校验：
       source refs
       evidence kind
       volatility
       frozen mission
       rollback ref
       owner/company boundary
  -> 接受：原子提交 soul.md / optional source.md patch
  -> 如发现需要修改 T3：写 held T3 concern，回流到下一轮 T3 Consolidation Batch；不能借 Dream 路径直接改 T3
  -> 拒绝/搁置：保留在 ledger 中，不能偷偷落盘
```

### 5.5 读取与 Prompt 组装

```text
Retriever / Activation
  -> 按 PrincipalStack、owner/company、敏感等级、目标相关性选记忆
  -> session projection 只作为动态上下文
  -> T3 semantic pages / high-priority T2 / soul 按各自区域进入 prompt
  -> Prompt Builder 只读不写
```

Prompt 组装层不能产生新记忆状态。

## 6. 候选协议

候选协议可以保留，但它不是一条绕过四层主链的写入高速路。

```python
class MemoryCandidateEnvelope(TypedDict):
    schema: Literal["memory_candidate.v1"]
    agent_id: str
    tenant_id: str
    source: Literal[
        "summary_agent",
        "learning_brain",
        "heartbeat_curator",
        "t3_curator",
        "dream_reconsolidator",
        "soul_writer_agent",
        "session_feedback",
        "work_ledger",
        "manual_owner_instruction",
    ]
    target: Literal[
        "session_package_t2",
        "t3_chapter_candidate",
        "soul_candidate",
        "source_candidate",
        "skill_candidate",
        "workflow_reference_hint",
        "lifecycle_patch",
        "artifact_only",
    ]
    content: str
    source_refs: list[str]
    evidence_refs: list[str]
    confidence: float
    volatility: Literal["ephemeral", "session", "project", "stable"]
    evidence_kind: Literal["user_stated", "tool_verified", "system_observed", "inferred"]
    rationale: str
    boundary_checks: dict[str, bool | str]
    created_at: str
```

规则：

- `target="session_package_t2"` 只能来自 Summary Agent / Learning Brain / 明确 owner instruction。
- `target="t3_chapter_candidate"` 必须引用 Segment Package 的 T2 summary/labels 或 T2-derived candidate。
- `soul_candidate` / `source_candidate` 必须来自 Dream / Soul Writer Agent 或明确 owner instruction。
- `skill_candidate` 是 capability lane，不是 T3 写入；`workflow_reference_hint` 只能移交 Workflow 系统，不属于 memory write target。
- 机械 fallback 只能产出 `artifact_only` 或 held candidate，不能直接 durable write。

## 7. 责任矩阵

| 组件 | 应该保留的职责 | 可以读 | 可以写 | 禁止事项 |
|---|---|---|---|---|
| Runtime hooks | 事件路由、非阻塞 fanout | 消息、metadata | hook event / source packet | 不能做语义分类，不能写 durable memory |
| T0 session ledger | 原始 session 证据、可回放上下文、resume boundary | accepted user/assistant/tool events、runtime metadata | `memory/t0/sessions/<chat_session_id>/segments/<segment_id>/source.md`；idle/close 只 seal segment | 不能做语义分类，不能写 durable memory，不能回写历史事件 |
| T0 legacy logger | 旧文件格式 import / manual compatibility | legacy file payload | `logs/YYYY-MM-DD/{behavior,system}/` | runtime hooks 不能调用；legacy chat logs 只能 import；`system/` 日志默认不能喂进语义记忆 |
| Source Bundle Builder | 为 T2 job 生成 staging 输入包 | T0 session ledger、DB ChatMessage、invocation_spans、artifact refs、legacy import records | `memory/.staging/t2_jobs/<job_id>/source_bundle.json` | 不能生成语义总结，不能落 canonical Markdown |
| Summary Agent | T0 -> T2 XML-style structured summary | ChatSession-scoped source range、messages、tool result、Work Ledger evidence | Segment Package 的 `summary.md` candidate | 不能写 T3/soul/skill/workflow；不能做最终晋升裁决 |
| Learning Brain | thin T2 engineering labels + lightweight event/fact labels | T0 source range、Summary、source refs | Segment Package 的 `labels.md` candidate | 不能直接落盘；不能做最终晋升裁决 |
| Memory Gate Agents | T2/T3/soul/skill candidate review, scoring, promotion advice | Segment Package、T0 refs、existing T3 accepted files、soul patch、skill candidate、promotion rules | `review.md` / promotion decision / feedback directives | 不能最终提交；不能绕过 Platform Gate；不能用旧 review 授权新 patch |
| T3 Curator / Heartbeat Curator | T2 -> T3 semantic layer LLM curation；在 T3 Patch Envelope 内生成 target-view labels 和 source coverage | Segment Packages、T2 source_refs 指向的 T0 evidence、T3 accepted memory files、runtime/db read models | T3 Patch Envelope candidate、heartbeat audit | 不能做总调度器；不能让 T0 直达 T3；不能拆出单独的 T3 标签写入路径；不能直接写 `memory/t3/` |
| Dream Reconsolidator / Soul Writer Agent | T3 重组、矛盾处理、soul/source patch 提案 | T3、soul/source、候选 ledger | `soul_pitch.md` / `soul_patch.md` candidate；held T3 concern | 不能绕过 Soul Memory Gate Agent 和 Platform Soul Gate 直接写；不能借 Dream 路径直接改 T3 |
| Skill Distiller / Skill Writer / Skill Review | skill 候选验证、eval、晋升建议 | `capabilities.md` skill_seed、Segment Package refs、eval reports | skill candidate package / eval report / promotion ledger | 不能和 memory lane 抢同一个信号；不能不经 eval 和 Platform Skill Gate 写 active skill |
| Platform Gate | 权限、证据、去重、回滚、审计、原子提交 | reviewed candidate requests、source refs、principal context | Session/T3/soul/source/skill/lifecycle/archive/evolution decision record | 不能替模型做语义学习、主观评分或内容改写 |
| Retriever / Activation | principal-aware recall | T3、高优先级 T2、session projection、runtime/db read models | access telemetry | 不能修改语义记忆 |
| Prompt Builder | 渲染当前 prompt context | identity、memory snapshot、session learning | final system prompt | 不能创建新记忆状态 |

## 8. Canonical Sources 与唯一 T0 证据源

这里必须区分两个概念，避免把 T0 的“唯一原始真相源”和各层的 canonical accepted files 混在一起：

1. **唯一 T0 原始 / 回放真相源**：只指 `memory/t0/sessions/<chat_session_id>/segments/<segment_id>/source.md`。这是可 replay、可残差回看的原始事件账本。
2. **各层 canonical accepted files**：T2 的 Segment Package、T3 的 accepted memory files、`soul.md` 都是各自层级的 accepted source，但不是 T0 原始证据的替代物。

因此，T3 Curator 的入口不是把 T3 真相源替换成 `summary.md` / `labels.md` / `review.md`。正确关系是：T3 Curator 读取成熟 T2 Segment Packages 作为主入口，并沿其中的 `source_refs` 回看 T0 原始证据，同时读取当前四个 T3 accepted memory files 作为目标层上下文；最终只生成 T3 Patch Envelope，不能直接覆盖 T3 accepted files。

| 关注点 | Canonical Source | Derived / Audit Only |
|---|---|---|
| 原始可回放证据 | `memory/t0/sessions/<chat_session_id>/segments/<segment_id>/source.md` | DB `ChatMessage`、`invocation_spans` 是交叉校验/运行时读模型；`source_bundle.json` 是 staging 输入包；`logs/...` 是 legacy/import compatibility |
| 短期 session projection | runtime session memory、`runtime_artifacts/session_learning_projection.jsonl` | 只允许 TTL / session scoped；不是 durable semantic truth |
| T2 Segment Package | `memory/sessions/<chat_session_id>/segments/<segment_id>/summary.md`、`labels.md`、`review.md`、`manifest.json` | `memory/t2/**`、`memory/learnings/*.md` compatibility views |
| T3 accepted memory files | `memory/t3/episodes.md`、`user.md`、`worker.md`、`capabilities.md` | Compatibility `memory/wiki/**/*.md`、`memory/*.md` T3 files；旧 `canon.md`、`relations.md`、`contradictions.md` 只作为迁移输入或 read-only compatibility view |
| T3 派生读模型 | 无 canonical source；只能从四个 T3 accepted memory files 和 source refs 重建 | runtime/db cache、graph/vector/index、UI read model；不写入 `memory/` 文件树 |
| 身份记忆 | `soul.md`；`source.md` 仅作为待定别名 | dream reasoning、promotion candidates、rollback snapshots |
| 候选/评估/晋升/回滚 | `evolution/evolution_ledger.jsonl` | ActivityLog UI timeline |
| heartbeat 计数和历史 | `evolution/scorecard.md`、`lineage.md`、`blocklist.md` | 不是语义记忆 |
| 读取可见性 | `ActivationContext` + `PrincipalStack` + lifecycle metadata | prompt preview |
| 搜索加速 | 无外部增强作为真相源 | 未来 adapter 必须可重建 |

## 9. 允许写入路径

| 目标 | 唯一允许写入者 |
|---|---|
| `memory/t0/sessions/<chat_session_id>/segments/<segment_id>/source.md` | T0 session ledger writer (`web_chat_runtime` append points; `task_executor` one-off task events; runtime hooks for trigger/delegation/heartbeat/dream; idle/close seal chat segments) |
| `logs/.../{behavior,system}/*.md` | Legacy T0 import/manual compatibility only；runtime hooks must not write here |
| `memory/.staging/t2_jobs/<job_id>/source_bundle.json` | T0ToT2PackageBuilder staging writer；构建期输入包，不是 canonical memory |
| `runtime_artifacts/session_learning_projection.jsonl` | runtime continuity / session projection writer；TTL/session scoped |
| `memory/sessions/<chat_session_id>/segments/<segment_id>/summary.md` / `labels.md` / `review.md` / `manifest.json` | Platform Gate atomically commits LLM-authored Summary Agent + Learning Brain + Memory Gate outputs plus mechanical manifest |
| `memory/t2/index.md` / `memory/t2/summary.md` / 兼容 `memory/learnings/*.md` | Derived/rebuildable views from Segment Packages |
| `memory/t3/episodes.md` / `user.md` / `worker.md` / `capabilities.md` | Platform Gate atomically commits LLM-authored T3 patch after fresh Memory Gate review of the latest patch |
| T3 navigation / relation / conflict read models | Derived index builder writes runtime/db cache only; rebuildable from T3 accepted memory files and source refs |
| `memory/archive.md` | Platform Gate lifecycle patch |
| `memory/lifecycle.json` | Platform Gate |
| `soul.md` / optional `source.md` learned behavior sections | Platform Soul Gate atomically commits Dream / Soul Writer authored patch after fresh Soul Memory Gate review of the latest patch；frozen charter / identity-protected sections fail closed |
| `evolution/skill_candidates/<candidate_id>/**` | Skill Candidate Builder writes LLM-authored candidate package, eval inputs, failure cases, and review artifacts through Platform Skill Gate staging |
| `skills/<name>/SKILL.md` and skill package files | Platform Skill Gate atomically promotes eval-backed Skill Writer authored package after Skill Review; active skill files are not written by T3 Curator or Dream |
| `evolution/evolution_ledger.jsonl` | Candidate/eval/promotion services through ledger API |
| `evolution/lineage.md`、`scorecard.md`、`blocklist.md` | platform audit writer only |
| Memory enhancement adapter | 保留 no-op hook；不能配置具体外部记忆程序 |

禁止跨界：

- Summary Agent / Learning Brain 不能晋升到 T3/soul/skill/workflow。
- T3 Curator / Heartbeat 不能直接跑 skill/workflow/dream/scene/wiki 等无关任务。
- Learning Brain 不能写文件，也不能替代 Memory Gate Agents。
- Dream 不能在没有 candidate/decision/rollback record 的情况下改身份；Dream 路径不能直接提交 T3 patch。
- Skill Writer 不能在没有 eval、failure case、review 和 promotion record 的情况下写 active skill。
- `lineage.md` 不能作为 T2/T3/soul 的语义来源。
- 外部增强程序不能成为 T3 真相源。
- Prompt assembly 不能修改记忆状态。

## 10. 散落组件去留表

这张表回答“除了四层记忆、核心蒸馏链路、Learning Brain、Memory Gate Agents、Platform Gate 之外，剩下的东西到底要不要”。

| 散落部分 | 当前主要路径 | 去留 | 最终边界 |
|---|---|---|---|
| Session Memory / runtime continuity | `backend/app/services/session_memory.py`、`runtime_artifacts/session_memory.md`、legacy `workspace/session_memory.md` | **保留** | 只是运行时续接和恢复上下文，不是 T2/T3/soul；Prompt 可读，不能 durable 写入语义记忆 |
| Recovery Manifest | `backend/app/runtime/recovery_manifest.py` | **保留** | 从 session memory 组装恢复线索；属于 runtime recovery，不属于 memory distillation |
| Session Learning Projection | `backend/app/services/session_learning.py`、`evolution/session_learning_projections.jsonl` | **保留但限权** | 只服务下一轮/当前 session/T3 curation 的上下文辅助；有 TTL/状态；不直接进入 T3 |
| Fast Reflection Learning Brain | `backend/app/services/fast_reflection_learning_brain.py` | **迁移/收窄** | 不再作为 durable memory 分流主路；可降级为 Summary Agent / Learning Brain / Memory Gate Agents 的辅助候选生成或审计输入，不能独立写长期记忆 |
| Fast Reflection Service | `backend/app/services/fast_reflection_service.py` | **保留为 adapter** | 只持久化短期 projection/candidate；机械 fallback 只能 audit/held candidate |
| Reportable Reflection | `backend/app/services/reflection_service.py`、`memory/reflections/*.jsonl` | **已降级为 artifact-only** | 保留为失败审计和 source packet；不再直接 `append_t2_entries`，也不写 `memory/learnings/*.md`。如需进入长期记忆，只能被后续 T0/T2 Segment Package 或 T3 Consolidation 作为 evidence 引用 |
| Session Feedback | `backend/app/services/session_feedback.py` | **保留** | owner useful/misleading 是高价值信号；默认写入 Explicit Memory Overlay 并即时可激活，后续是否吸收到 accepted T3 必须经 T3 Consolidator、Memory Gate Agents 和 Platform Gate；T3 candidate 必须引用 T2/source refs |
| Work Ledger / Todo Ledger | `backend/app/tools/handlers/work_ledger.py`、`backend/app/services/agent_work_ledger.py` | **保留** | 它是当前任务工作台和证据板，不是长期记忆；可作为 Summary Agent 的 source evidence |
| Evolution Ledger / Manifest / View / Validation | `backend/app/services/evolution_ledger.py`、`evolution_manifest.py`、`evolution_view.py`、`evolution_validation.py`、`evolution_verification.py` | **保留** | 候选、评估、晋升、回滚真相源；不是语义记忆本体 |
| `lineage.md` / `scorecard.md` / `blocklist.md` | heartbeat/evolution audit files | **保留但降级** | 只做人类可读审计和计数；不能作为模型学习的语义来源 |
| Scene / Wiki / Understanding | `backend/app/memory/scene_curator.py`、`wiki_curator.py`、`understanding_store.py`、`relation_graph.py`、`wiki_retrieval.py`、`backend/app/services/memory_curation.py` | **保留但收敛到 Agent Markdown Wiki 派生面** | 目标 T3 语义层只允许 `episodes.md`、`user.md`、`worker.md`、`capabilities.md`；当前 `memory/wiki/**/*.md` 是兼容面；relation graph、retrieval index、understanding store 是派生索引，必须可重建 |
| Retriever / Activation / Visibility | `backend/app/memory/retriever.py`、`activation.py`、`visibility.py`、`assembler.py`、`access_log.py` | **保留** | 读取面和可见性面；只读 T2/T3/session projection，最多写 access telemetry |
| Write Gate / T2/T3 Store | `backend/app/memory/write_gate.py`、`t2_store.py`、`t3_store.py` | **保留但包进 Platform Gate facade** | 作为 Platform Gate 的内部实现；外部 caller 不应直接散点调用 |
| Lifecycle / Hygiene / Legacy Migration | `backend/app/memory/lifecycle_store.py`、`lifecycle_maintenance.py`、`hygiene.py`、`legacy_migration.py` | **保留** | 平台维护、迁移、归档、修复；不能生成新的语义判断 |
| Memory Metrics / Retrieval Eval | `backend/app/memory/metrics.py`、`backend/app/memory/retrieval_eval.py`、`backend/app/evals/run.py`、`backend/app/evals/self_evolution_bakeoff.py` | **保留** | 只做评估和质量度量；不能反向变成写入路径 |
| Memory Backend protocol | `backend/app/memory/backend.py` | **保留兼容接口，收窄写权** | 只能作为 native Markdown read adapter/兼容层；`store()` 这类直接写 T3 的能力应退役或改走 Platform Gate |
| Memory Enhancement hook | `backend/app/memory/enhancement.py` | **保留 no-op** | 只保留未来可重建 read accelerator 的空接口；当前不接任何具体外部记忆 program |
| OpenViking / Enterprise KB injection | `backend/app/services/knowledge_inject.py`、`viking_client.py`、`backend/app/api/files.py` | **保留但不属于 agent memory** | 它是企业知识库/检索注入，不是 T3 增强，不写 agent soul/T3；必须 ACL/principal scoped |
| Team Memory | `backend/app/services/team_memory.py` | **保留但单独命名空间** | 公司/团队共享知识，不是单个 agent 的 T3/soul；注入 prompt 时必须经过 principal/ACL |
| Subagent Memory | `backend/app/agents/subagent_memory.py` | **保留但受同一规则约束** | subagent 可贡献证据或候选；长期写入仍必须经过 Memory Gate Agents + Platform Gate |
| Knowledge Read Model / Session Recall / Memory Service helpers | `backend/app/services/knowledge_read_model.py`、`backend/app/services/memory_service.py`、`backend/app/services/session_recall.py` | **保留为 helper/read model** | 只能辅助读取、汇总、展示；不能拥有独立写入权 |
| Conversation Summarizer | `backend/app/services/conversation_summarizer.py` | **保留为工具型能力** | 可以做摘要辅助，但不能替代 Summary Agent、Learning Brain、T3 Curator、Dream 的语义判断和候选生成 |
| Skill candidate lane | `backend/app/services/skill_distiller.py`、`backend/app/services/skill_flywheel.py`、`backend/app/services/skill_curator.py` | **保留但分 lane** | Skill 能力进化可以消费 memory evidence，但不能直接把 skill 结果写成 T3 |
| Workflow system handoff | `backend/app/services/workflow_promote_suggestions.py`、`backend/app/services/workflow_signal_consumer.py`、workflow runtime/API/model/test 路径 | **移出 memory 体系** | Workflow 是独立 JSON/YAML/DSL 执行控制体系；memory 只提供 evidence refs / reference hints，不拥有 workflow candidate 落盘权 |
| Hindsight / HandSide / OpenViking-as-T3 wording | 旧文档、旧提示词或残留概念 | **退役** | T3 不挂任何外部增强系统；OpenViking 只保留企业 KB 角色 |

## 11. 需要移除或降级的路径

### R1. 降级 Fast Reflection classifier

当前问题：Fast Reflection 仍有 marker/规则 fallback，可能独立产出 durable memory candidate。

目标：

- 保留 `fast_reflection_service.py` 作为 adapter / persistence layer。
- 语义判断回归 Summary Agent、Learning Brain、T3 Curator、Dream 这些 LLM 主路。
- 机械 fallback 只能生成 held/audit candidate，不能直接 durable write，也不能替 Memory Gate Agents 打晋升分。

### R2. 收敛 T0 -> T2 Segment Package 路径

当前问题：旧 Extractor 同时做 atomization、container routing、候选升级，容易让 T0 直接冲到 T3。

目标：

- `build_t2_segment_package(chat_session_id, source_range)`：一个 ChatSession 内的一个 segment source range 只生成一个 canonical Segment Package。
- Segment Package 内部只保留 `summary.md`、`labels.md`、`review.md` 三个语义文件和机械 `manifest.json`；T0 原始证据通过 `manifest.json` / `source_refs` 回溯。
- `summary.md` 内部只包含 XML-style structured summary，不写标签。
- `labels.md` 内部包含 thin Engineering Labels、lightweight Event / Fact Labels。
- open / rolling checkpoint package 必须写入 `short_term_carryover`，并禁止 T3 promotion。
- continuation chain 用 `continues_from` 引用上一份 open package，不改写历史 package。
- `review.md` 内部包含 Evidence Check 和 Promotion Check；Promotion Check 是晋升建议，不是已经晋升。
- replay/backfill 只能重建同一个 Segment Package 或修复 `manifest.json` / staging `source_bundle.json`，不允许生成第二份互相竞争的 T2。
- 旧 `Extractor` 只能作为实现 adapter：可以降级薄弱证据，不能把内容升级成 T3/soul/skill/workflow。

### R3. 瘦身 T3 Curator / Heartbeat

当前问题：Heartbeat 现在同时承担 curator、reflection router、skill distiller runner、独立 scene/wiki sweep、dream scheduler、audit writer。

目标：

- T3 Curator / Heartbeat 只保留：
  - 读取成熟 Segment Packages；
  - 沿 T2 `source_refs` targeted load T0 evidence；
  - 读取当前四个 T3 accepted memory files；
  - 必要时读取 runtime/db read models；
  - 调 T3 curation LLM；
  - 生成 T3 Patch Envelope；
  - 交给 Memory Gate Agents 做 T3 Patch Review；
  - 交给 Platform Gate 做硬校验、审计、rollback ref、原子提交到四个 T3 accepted memory files；
  - 已记录结果后再 mark T2 absorbed；
  - 写 heartbeat audit。
- Skill distillation、derived graph/index rebuild、独立 scene/wiki maintenance sweep、Dream scheduling 迁到 `EvolutionScheduler` 或独立 daemon tick；derived graph/index rebuild 不能写 `memory/` 文件树。
- T2 -> T3 semantic layer 的主 curation 属于 T3 Curator / Heartbeat Curator，但它不拥有最终写权。

### R4. Dream 只返回 soul candidate + held T3 concern

当前问题：Dream 虽然有 ledger，但仍含有直接写 soul/T3 lifecycle 的逻辑；同时不能被削弱成永远不能更新 `soul.md` 的审计工具。

目标：

- Dream / Soul Writer Agent 返回 LLM-authored `DreamDecision`、`soul_pitch.md` 和 `soul.md` / optional `source.md` patch candidate。
- Soul Memory Gate Agent 对最新 soul/source patch 做 Dream Review；如果 review 要求改写，必须退回 Dream / Soul Writer Agent 生成新 patch，旧 review 立即失效。
- Platform Soul Gate 只能原子提交：
  - `soul_patch_candidate`
  - optional `source_patch_candidate`
  - preservation / rollback / audit metadata
- 所有 patch 都有 candidate id、decision id、source refs、rollback ref。
- 如果 Dream 发现 T3 中存在重复、冲突、过期或需要合并的内容，只能写 held T3 concern / next T3 consolidation input；`t3_merge_patch`、`t3_contradiction_patch`、T3 lifecycle patch 必须回到 T3 Consolidator + Memory Gate + Platform Gate 路径，不能由 Dream 路径直接提交。
- Platform Gate 不能机械生成或改写 patch 内容；如 review 要求修改，必须退回 Dream / Soul Writer Agent 重写。

### R5. 审计面按角色收敛

当前问题：UI 和 operator 会看到太多相似表面，容易把 audit 当 memory。

目标：

- `evolution_ledger.jsonl`：candidate/eval/promotion/rollback truth。
- `lineage.md` / `scorecard.md`：heartbeat audit only。
- `AgentActivityLog`：UI event stream only。
- `invocation_spans`：trace/debug truth。
- legacy `logs/.../system/*.md`：distiller reasoning audit only；当前 runtime truth 写入 T0 session ledger。

所有 UI label 必须标注类别：`semantic`、`candidate`、`audit`、`trace`、`derived`、`review`。

### R6. 建立 Platform Gate facade，并显式记录 Memory Gate review

目标模块可以沿用现有代码名迁移，但概念命名必须是 Platform Gate；不要把它再叫 `MCP`：

```text
backend/app/memory/platform_gate.py
backend/app/memory/memory_gate_review.py
```

建议公开 API：

```python
class PlatformGate:
    async def append_session_package_t2(package: SessionPackage) -> T2WriteResult
    async def record_memory_gate_review(review: MemoryGateReview) -> ReviewRecord
    async def stage_t3_consolidation_job(job: T3ConsolidationJob) -> CandidateDecision
    async def apply_t3_consolidation_patch(patch: T3Patch, review_id: str) -> PatchResult
    async def apply_lifecycle_patch(patch: LifecyclePatch, review_id: str) -> PatchResult
    async def stage_soul_patch_candidate(candidate: SoulPatchCandidate) -> CandidateDecision
    async def apply_soul_or_source_patch(patch: SoulPatch, review_id: str) -> SoulPatchResult
    async def stage_skill_candidate(candidate: SkillCandidatePackage) -> CandidateDecision
    async def promote_skill_candidate(candidate_id: str, review_id: str, eval_id: str) -> SkillPromotionResult
    async def retire_memory(entry_id: str, reason: str, source_refs: list[str]) -> RetireResult
    async def retrieve_context(query: str, principal: PrincipalStack) -> MemoryContext
```

现有 `write_gate.py`、`t2_store.py`、`t3_store.py`、`activation.py`、`visibility.py` 可以继续作为内部实现，但外部 caller 不能再散点调用；语义 review 结果来自 Memory Gate Agents，Platform Gate 只做硬规则、审计和原子提交。

## 12. 实施顺序

这不是 MVP 分期，而是一次完整整改的执行顺序。代码 pass 只有全部完成才算闭环。

1. 添加 architecture guard tests，覆盖写入边界和外围组件去留。
2. 建立 ChatSession -> source_bundle -> Segment Package schema、one-segment-one-Segment-Package invariant、受控 tag taxonomy、`source_refs` / residual evidence backreference contract。
3. 建立 Agent Markdown Wiki schema + T3 accepted memory file schema：`_meta/schema.md`、`tags.md`、`index.md`、`log.md`、frontmatter、wikilinks、source refs、contradiction markers；`memory/t3/` 只允许四个文件。
4. 建立 Summary Agent output schema：只写 `# Session Summary` 内的 XML-style semantic body，不做晋升裁决。
5. 建立 Learning Brain T2 labeling schema：独立 `labels.md` 内的 thin `# Engineering Labels`、lightweight `# Event / Fact Labels`。
6. 建立 open / rolling checkpoint contract：`completeness`、`short_term_carryover`、`continues_from`、禁止未闭合 package 晋升 T3。
7. 建立 Memory Gate Review schema：`review.md` 内的 `# Evidence Check`、`# Promotion Check`。
8. Fast Reflection 改为候选/审计 adapter，不再独立 durable classify。
9. 旧 Extractor 收敛成 T0-to-Session-Package adapter 和 replay/backfill adapter。
10. `reflection_service.py`、`session_feedback.py`、`save_memory` 改为 T2/T3 candidate + Memory Gate review + Platform Gate 路径。
11. 新增 Platform Gate facade，并迁移新 caller；现有 `write_gate.py`、`t2_store.py`、`t3_store.py` 只能作为内部实现。
12. 瘦身 Heartbeat，把 skill、derived graph/index rebuild、dream trigger 迁出；保留 T2 -> T3 semantic layer curation。
13. T3 Curator / Heartbeat curation 接入 mature Segment Packages、T2 source_refs-backed T0 evidence、当前四个 T3 accepted memory files，并产出 `consolidation_pitch.md`、`revised_patch.md`；Platform Gate 只允许在 fresh Memory Gate final review 后原子提交到 `episodes.md`、`user.md`、`worker.md`、`capabilities.md`。
14. Dream apply path 改为 Dream/Soul Writer authored `soul_pitch.md` / `soul_patch.md` + fresh Soul Memory Gate review + Platform Soul Gate atomic apply；Dream 发现的 T3 concern 回流到下一轮 T3 Consolidation Batch。
15. Skill lane 改为从 `capabilities.md` skill_seed / evidence 生成 Skill Candidate Package，经 eval、Skill Review、Platform Skill Gate 后晋升 `skills/<name>/SKILL.md`。
16. `backend/app/memory/backend.py` 的直接 store 写入能力退役或改走 Platform Gate。
17. T3 外部增强相关旧文案、旧配置、旧 prompt 全部清理，只保留 no-op enhancement hook。
18. ActivityLog、evolution view、memory UI 统一 observability label。
19. 跑完整 backend tests，并补 production/eval live trace。

## 13. 红线测试清单

### 13.1 架构/所有权

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/architecture/test_memory_clean_loop_ownership.py -q
```

必须断言：

- Learning Brain 没有 filesystem writes。
- Learning Brain 只产出 `labels.md` candidate，不直接生成 T3/soul durable write。
- Summary Agent 没有 T3/soul/skill/workflow write calls。
- 旧 Extractor adapter 没有 T3/soul/skill/workflow write calls。
- T3 Curator / Heartbeat 在 scheduler split 后不直接调用 skill distiller、derived graph/index rebuild、dream。
- T3 Curator / Heartbeat 的 T3 semantic layer curation 输入包含 mature Segment Packages、T2 source_refs 指向的 T0 evidence、current T3 semantic page context。
- T3 candidate 必须引用 Segment Package 或 T2-derived candidate；T0 不允许直达 T3。
- Dream writeback 进入 Memory Gate review + Platform Gate facade。
- `lineage.md` 不被作为 semantic memory input。
- 没有具体外部 enhancement program 被接入 T3 recall/writeback。
- `knowledge_inject.py` / OpenViking 只作为 enterprise KB，不作为 memory backend。
- `team_memory.py` 不写入单 agent T3/soul。

### 13.2 T0 -> T2 Segment Package

```bash
pytest tests/memory/test_segment_package.py tests/memory/test_t2_xml_summary_contract.py tests/memory/test_t2_labels_contract.py tests/memory/test_open_segment_rolling_checkpoint.py tests/services/test_extract_agent.py -q
```

必须断言：

- 一个 ChatSession 内的一个 `segment_id` 只能对应一个 canonical Segment Package；外部 chat session / Feishu thread 可以包含多个 segments。
- Summary Agent 只生成 XML-style structured summary，不打最终晋升分。
- `summary.md` 的 XML body 必须 well-formed，包含 `overview`、`event_segments`、`retrieval_cues`、`source_refs`，open segment 必须包含 `short_term_carryover`。
- `summary.md` 不允许包含 `# Engineering Labels` / `# Event Labels`。
- Learning Brain 在同一个 Segment Package 的 `labels.md` 中写 thin engineering labels、lightweight event/fact labels，不把正文维度重复塞进标签。
- Memory Gate Agents 在同一个 Segment Package 的 `review.md` 中写 Evidence Check 和 Promotion Check。
- `completeness=open` / `rolling_checkpoint` 的 package 不允许产生 T3 promotion decision，只能进入 active short-term carryover 或 recall-only。
- continuation chain 必须通过 `continues_from` 引用上一份 open package，不能改写历史 `summary.md`。
- Replay/backfill path 是 LLM-primary，并且 fallback 有明确标记。
- 旧 Extractor adapter 可以降级薄弱证据，但不能升级到 T3/soul/skill/workflow。
- Segment Package 输出保留 source refs，供 T3 curation 回溯证据。

### 13.3 T2 -> T3 Curator / Heartbeat

```bash
pytest tests/services/test_heartbeat.py tests/services/test_heartbeat_reflection_learning.py -q
```

必须断言：

- T3 Curator / Heartbeat 只做 T2 -> T3 curation 和 audit。
- T3 Curator / Heartbeat curation prompt 包含 Segment Packages、T2 source_refs、targeted T0 evidence、current four T3 accepted memory files。
- T3 Curator / Heartbeat 不允许把 T0 evidence 作为直接 T3 输入；必须通过 Segment Package 起步。
- Segment Packages 只有在 T3 Patch Envelope、Memory Gate review、Platform Gate accepted-file commit 被记录后才能 mark absorbed。
- Noop heartbeat 只保留 audit。

### 13.4 Learning Brain / Agent Markdown Wiki

```bash
pytest tests/memory/test_learning_brain_wiki_organization.py tests/memory/test_t3_markdown_wiki_contract.py -q
```

必须断言：

- Learning Brain 输出 T2 engineering labels 和 event/fact labels。
- T3 target-view labels 写入 T3 Patch Envelope，包括 `target_view`、`consolidation_mode`、`source_coverage`、`cue_strength`、`stability`、`behavior_impact`、`prompt_priority` 等 patch labels。
- Learning Brain 不能写 `memory/**`、`evolution/**`、`soul.md`、`skills/**`。
- `memory/t3/` 下只允许 `episodes.md`、`user.md`、`worker.md`、`capabilities.md`；任何 `index.md`、`relations.md`、`contradictions.md`、`chapters/**` 都必须被测试拒绝。
- 四个 T3 accepted memory files 和兼容 `memory/wiki/**/*.md` 写入只能通过 Platform Gate 原子提交 LLM-authored patch。
- 每个 T3 主 accepted view block 带 frontmatter、source refs、confidence 或明确 evidence status；`episodes.md` 必须包含 cue_terms / scene_context，`capabilities.md` 必须包含 when_to_use / method / verification，`user.md` 和 `worker.md` 必须包含 applies_when / does_not_apply_when。
- `[[wikilinks]]` / relation graph / vector index 都是派生索引，可从 Markdown 重建。
- contradiction / contested 标记不能被 Dream 或 Heartbeat 静默覆盖；如果需要索引，只能在 runtime/db read model 中重建，不能写回 `memory/`。
- 旧 `canon.md`、`relations.md`、`contradictions.md` 不能作为新的 canonical T3 accepted view。
- Skill 候选不能被写入 T3 semantic page 作为最终能力 source of truth；Workflow definition 不属于 memory target。

### 13.5 Dream

```bash
pytest tests/services/test_auto_dream.py tests/services/test_evolution_ledger.py -q
```

必须断言：

- Dream decisions 写成 candidates/patches 后才能 writeback。
- Soul promotion 必须有 source refs、verified evidence、frozen mission gate、rollback ref。
- repeated-feedback mechanical fallback 只能提出 candidate，不能直接改 identity。
- Dream / Soul Writer Agent 必须可以产出 `soul.md` / optional `source.md` patch，否则 Dream 无法完成最高层收敛。
- Dream patch 必须先有 Memory Gate review，再由 Platform Gate atomic apply。
- Platform Gate 不允许机械生成或改写 Dream patch 内容；需要改动时必须退回 Dream / Soul Writer Agent。

### 13.6 外围组件

```bash
pytest tests/architecture/test_memory_peripheral_boundaries.py -q
```

必须断言：

- `session_memory.py`、`recovery_manifest.py` 只服务 runtime continuity。
- `session_learning.py` 只写短期 projection，不写 T2/T3。
- `reflection_service.py` 不再直接 `append_t2_entries`，也不得创建 `memory/learnings/*.md` 投影。
- `session_feedback.py` 和 `save_memory` 不再直接 `append_t3_memory_candidate`；它们应进入 Segment Package / candidate path，再由 Memory Gate Agents + Platform Gate 决定是否晋升 T3。
- `memory/backend.py` 不允许绕过 Platform Gate 写 T3。
- `memory/enhancement.py` 保持 no-op；没有外部 program 配置。
- scene/wiki/understanding 是 derived/rebuildable，不是 canonical memory。

### 13.7 渐进验证 / 机械保真

```bash
pytest tests/memory/test_memory_governance_validation_ladder.py tests/memory/test_mechanical_fidelity.py -q
```

必须断言：

- 验证强度按影响范围递增：metadata/index patch 不触发完整语义判卷，T3 稳定结论必须有 T2 ref 和 T0 source_ref，soul/skill promotion 必须走强验证和 gate；workflow promotion 不在 memory lane 内执行。
- 机械 fallback 不能把截断摘要、regex 命中、计数器变化直接写成 T2/T3/soul 语义事实，只能生成 held candidate 或 audit event。
- 预算不足时，机械层优先保留 `source_refs`、artifact path、range、hash、candidate id，而不是保留一段失真的短摘要。
- 去重、权限、frontmatter/schema 校验可以是硬规则，但不能改写 LLM 产出的语义结论。
- 缺证据、证据冲突、validator 失败时，候选保持 held/rejected/audit 状态，不允许静默删除或静默降级为“已吸收”。

### 13.8 Read / Prompt

```bash
pytest tests/runtime/test_prompt_sections.py tests/runtime/test_system_prompt_budget.py tests/memory/test_activation.py -q
```

必须断言：

- `soul.md` 只进入 frozen identity 区域。
- T3/T2/session projection 只进入 dynamic memory 区域；compact navigation map 只能由读取层动态生成，不默认常驻。
- Principal/PL visibility 会剥离无权读取的记忆。
- 没有外部 enhancement 时 canonical T3 recall 不受影响。
- Prompt Builder 没有写副作用。
- 显式 `save_memory` 进入 explicit memory overlay，并可按权限即时激活；后续是否吸收到 accepted T3 必须走 T3 Consolidator + Memory Gate Agents + Platform Gate，不能绕过治理层直接进入 T3。

## 14. 验收标准

整改被接受的条件：

1. 主梯度只有 `T0 -> T2 -> T3 -> soul.md`。
2. T0 不直接进入 T3；T3 只通过 T2 source_refs 回溯 T0。
3. 一个 ChatSession 内的一个 `segment_id` 只能有一个 canonical Segment Package。
4. Summary Agent 只是 T0 -> T2 事实总结者。
5. Learning Brain 只是 T2 标签和结构化智能，不再是 durable write router。
6. Memory Gate Agents 是独立裁判层，负责 review、score、promotion recommendation。
7. Platform Gate 只有 hard check + atomic commit 权限，没有内容作者权。
8. T3 Curator / Heartbeat 只是 T2 -> T3 curator，不再是 evolution orchestrator。
9. Dream / Soul Writer Agent 是 `soul.md` / optional `source.md` 的语义 patch 作者；它不能自由直写，但必须能在 fresh Soul Memory Gate review 和 Platform Soul Gate 通过后更新最高层文件。
10. 所有候选和晋升决策都有 source refs，必要时有 rollback refs；T3/soul/skill 的 review 必须覆盖最新 patch/candidate。
11. audit / candidate / semantic / trace / derived / review 在 UI 和日志上可区分。
12. Prompt assembly 没有写副作用。
13. T3 没有外部增强系统，OpenViking 只作为 enterprise KB。
14. Team Memory、Work Ledger、Session Memory 都有独立边界，不混入 agent T3/soul。
15. 验证强度按影响范围递增，机械层不因“方便治理”而替代 LLM 语义判断。
16. 机械层在预算/错误/降级场景下保留 source refs、range、hash、artifact path 和 held candidate，不写失真的 durable memory。
17. 显式 save_memory 的 immediate overlay、T3 absorption、conflict/dup/withdraw 路径都有可审计记录。
18. Skill lane 只能从 T3 capability evidence / skill_seed 到 candidate/eval/review/gate，再到 active skill；不能由 T3 Curator 或 Dream 直接写 active skill。
19. Full backend tests pass。
20. Railway production/eval 能看到一条 live trace：

```text
turn
  -> T0 source packet
  -> Summary Agent
  -> Learning Brain T2 labeling
  -> one canonical Segment Package with source_refs
  -> Memory Gate Agents T2 review
  -> Platform Gate atomic commit for Segment Package
  -> T3 Curator / Heartbeat curation with source_refs-backed T0 evidence
  -> T3 Patch Envelope with target-view labels and proposed accepted-memory-file patch
  -> Memory Gate Agents T3 promotion review
  -> Platform Gate atomic commit to memory/t3/{episodes.md,user.md,worker.md,capabilities.md}
  -> Dream / Soul Writer Agent soul-source patch candidate
  -> Soul Referee / Memory Gate review
  -> Platform Gate atomic commit for soul.md / source.md
  -> prompt recall
```

## 15. 当前决策

下一步代码整改不是继续打补丁，而是做一次责任重排：

- **保留**四层记忆架构；
- **保留**核心 LLM 蒸馏链路，但把 T0 -> T2 明确拆成 Summary Agent + Learning Brain；
- **重新定位**Learning Brain 为 T2 标签和结构化智能；T3 target-view labels 由 T3 Curator 写入 Patch Envelope，不新增 T3 标签文件或章节结构；
- **新增明确概念**Memory Gate Agents，负责独立复查、打分、晋升建议；
- **保留并改名**治理和提交层为 Platform Gate；它只做 hard check、审计、rollback 和原子提交，不做内容作者；
- **明确**Dream / Soul Writer Agent 是 `soul.md` / optional `source.md` 的语义 patch 作者，review 通过后可以更新最高层文件；
- **保留但隔离**Session Memory、Team Memory、Work Ledger、OpenViking KB、scene/wiki、eval/metrics；
- **迁移**feedback/reflection/direct-store 这类绕过路径；
- **退役**T3 外部增强系统、机械 durable fallback、重复语义判断者；
- **不再新增记忆系统**，所有外部东西只能是 evidence、read accelerator、team/enterprise KB 或 capability candidate lane。

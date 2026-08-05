# Memory Dream CC/Codex Alignment Plan（2026-06-22）

> Purpose: 把 Hive 自己的 Memory / Dream 系统重新放到 CC / FreeCode / Codex 的基底上审视，明确哪些机制已经对齐、哪些职责需要重命名、哪些 Agent/阶段属于过度设计，以及下一轮实现应如何收敛。
>
> Scope: 单 Agent / Giant Agent 的 memory 生命周期。A2A、Team、Workflow、Skill promotion 只在它们和 memory 边界相交时讨论。
>
> Status: Implemented substrate. The 2026-06-22 implementation added the session hot-memory lane, Memory Dream workspace staging, mandatory LLM-authored T2 Memory Gate review, and the Dream compatibility entrypoint that runs Memory Dream before Soul Dream. Prompt wording/eval tuning remains a later prompt-layer pass.

## 0. Bottom Line

Hive 的 Memory 大方向是对的：

```text
T0 JSONL = mechanical truth
Markdown = deterministic projection and semantic working surface
T0 -> T2 -> T3 -> soul.md = durable learning gradient
```

但当前实现有一个核心错位：

```text
CC / FreeCode / Codex 的 Dream 等价物主要是 project memory consolidation。
Hive 当前的 auto_dream 更像 T3 -> soul.md 的 IdentityPromoter。
```

因此下一轮不应该推翻 T0/T2/T3/soul，而应该做三件事：

1. 把 Dream 拆成 `Memory Dream` 和 `Soul Dream`。
2. 为当前 session 增加 CC-style `session_memory.md` 热记忆 lane。
3. 把过多的 memory "agents" 收敛为少数隔离 worker + mandatory Memory Gate review，复杂度放回 Platform Gate、权限、审计、回滚和 soul/skill 高风险晋升上。

Implementation note 2026-06-22:

- `backend/app/services/session_memory.py` now writes CC-style hot session memory under `memory/session_state/<session_id>/session_memory.md`. The writer is LLM-primary (`session_memory.writer.v1`), with deterministic projection only as an observable no-model/failure fallback; old `memory/sessions/<session_id>/session_memory.md`, `runtime_artifacts/session_memory.md`, and `workspace/session_memory.md` are retained as read-only migration fallbacks.
- `backend/app/memory/t2/segment_package.py` now requires Memory Gate LLM review for every T2 Segment Package. There is no low-risk platform-authored self-review path; if the Memory Gate is unavailable, the package is held rather than committed.
- `backend/app/services/memory_dream.py` implements the Memory Dream lane: reviewed T2/T2.5 package selection, `.staging/dream_workspace` sync, rollout summaries, `phase2_workspace_diff.md`, baseline finalization, and T3 consolidation batch staging.
- `backend/app/services/auto_dream.py` remains the compatibility scheduler entrypoint, but now runs Memory Dream first and then the existing Soul Dream / identity-promotion lane.

## 1. North Star

Hive 的基底仍然是：

```text
CC / FreeCode baseline
  + Codex memory/write/read optimizations
  + Hive MD-first Memory / self-evolution system
  + enterprise governance and control plane
```

Memory 不是凌驾于 CC/Codex 基底之上的主系统。Memory 是在 agent substrate 完整对齐之后的增强层，可以内置，也可以未来 plugin 化。

设计原则：

- CC 负责给我们 baseline semantics: session transcript、resume、hooks、session memory、extract memory、dream consolidation、tool/use lifecycle。
- Codex 负责给我们优化点: rollout extraction、two-phase memory write、workspace diff、read-path progressive disclosure、citation discipline、prompt style。
- Hive 负责新增价值: principal-aware governance、tenant/org visibility、MD-first Learning Vault、soul identity layer、skill/evolution promotion、rollback/audit。

## 2. CC / FreeCode Memory And Dream Mechanism

FreeCode 不是只有一个 Dream。它有三层：

| Layer | FreeCode source | Semantics | Hive mapping |
| --- | --- | --- | --- |
| Session Memory | `/Users/rocky243/vc-saas/free-code-main/src/services/SessionMemory/sessionMemory.ts` | forked subagent 异步维护当前 conversation 的 markdown notes，用于当前会话连续性、resume、compact | 新增 `session_memory.md` hot lane |
| extractMemories | `/Users/rocky243/vc-saas/free-code-main/src/services/extractMemories/extractMemories.ts` | 每轮完整 query loop 结束后由 Stop Hook 触发，后台 forked agent 抽取 durable memories 到 project memory dir | Hive T0 segment seal / response complete -> T2 package extraction |
| autoDream | `/Users/rocky243/vc-saas/free-code-main/src/services/autoDream/autoDream.ts` | 时间 + session 数 + lock 满足后，forked agent 读取 memory root 和 transcript dir，整理 memory topic files / `MEMORY.md` | Hive `Memory Dream`: T2/T3 global consolidation |

关键结论：

- FreeCode 的 `SessionMemory` 是当前会话热记忆，不是长期语义记忆。
- FreeCode 的 `extractMemories` 是 stop-hook 后台抽取，不阻塞主 loop。
- FreeCode 的 `autoDream` 是 project memory 整理器，不是 persona/soul promoter。
- FreeCode 的 memory prompt 明确区分 memory、plan、tasks，避免把当前任务状态误写成长记忆。

## 3. Codex Memory Mechanism

Codex 的 memory write path 是更工程化的两阶段 pipeline：

| Phase | Codex source | Semantics | Hive mapping |
| --- | --- | --- | --- |
| Phase 1: Rollout Extraction | `/Users/rocky243/Context Engineering/codex/codex-rs/memories/README.md` | 从 state DB claim eligible rollouts，过滤 memory-relevant response items，并行抽取 `raw_memory` / `rollout_summary` / `rollout_slug`，写回 DB | Hive T2 Segment Package / rollout summary |
| Phase 2: Global Consolidation | same | claim 全局 lock，同步 `raw_memories.md` / `rollout_summaries/`，生成 git-style workspace diff，启动受限 consolidation sub-agent 写 filesystem memory | Hive T3 Markdown Wiki consolidation |
| Read path | `/Users/rocky243/Context Engineering/codex/codex-rs/memories/read` | `memory_summary.md` 常驻，`MEMORY.md` / skills / rollout summaries progressive disclosure，要求 memory citation | Hive `wiki_map.md` / T3 retriever / T2 source refs |

关键结论：

- Codex 用 DB 做 mechanical claims、leases、retry/backoff、stage output，不把 DB 当最终 memory。
- 最终长期 memory 仍是 filesystem Markdown。
- Phase 2 用 workspace diff 让 consolidation agent 看到"上次成功到这次输入"的变化，而不是每次盲目全量重读。
- Read path 很克制：先读短 summary，再搜索 handbook，再按需打开 rollout summaries 或 skill files。

## 4. Hive Current State

Hive 已经对齐的部分：

| Area | Current source | Judgment |
| --- | --- | --- |
| T0 mechanical truth | `/Users/rocky243/vc-saas/hiveclaw-main/backend/app/memory/t0/ledger.py` | 已对齐，甚至强于 CC: `events.jsonl` truth + `source.md` projection + `index.json` + hash chain |
| T2 source package | `/Users/rocky243/vc-saas/hiveclaw-main/backend/app/memory/t2/segment_package.py` | 已有 canonical `source_bundle.json -> summary/labels/review -> Platform Gate` |
| T3 staging | `/Users/rocky243/vc-saas/hiveclaw-main/backend/app/memory/t3_consolidation.py` | 已遵守 LLM staging, Platform Gate commit |
| T3 Platform Gate | `/Users/rocky243/vc-saas/hiveclaw-main/backend/app/memory/t3_platform_gate.py` | 已有 target file、source refs、base revision、atomic commit checks |
| Flow map | `/Users/rocky243/vc-saas/hiveclaw-main/docs/memory-system-flow-map-2026-06-17.md` | 已定义 `T0 -> T2 -> T3 -> soul.md` 梯度 |

Hive 当前错位的部分：

| Current component | Current behavior | Problem |
| --- | --- | --- |
| `auto_dream.py` | 读取 T3 + soul，提出 soul promotion candidate；T3 concern 多数只是 held signal | 更像 `Soul Dream`，不是 CC/Codex 的 project memory dream |
| T2 multi-agent shape | Summary Agent、Learning Brain、Memory Gate、Episode Stitcher、Episode Gate 都可以成为单独 cognitive stage | 普通 memory extraction 路径过重 |
| T3 consolidation | 已有 staging/gate，但缺 Codex-style workspace diff/baseline read model | 长期会倾向全量重读或重复整理 |
| session hot memory | 文档中有 short-lived session projection，但没有明确 CC-style `session_memory.md` contract | resume/compact/current continuation 缺一个独立、低延迟、可读的热记忆面 |

## 5. External SOTA Reference

这些外部系统不能替代 Hive 的 MD truth surface，但能帮助校准分层：

| System | Useful idea | Hive decision |
| --- | --- | --- |
| LangMem | long-term memory 分 semantic / episodic / procedural，memory update 是 conversation + current memory -> LLM consolidate -> updated memory | T3 = semantic wiki, T2 = episodic/rollout summary, soul.md = procedural/identity rules |
| Letta | memory blocks 是 always-visible prompt blocks；sleep-time agent 异步把 conversation history 学成 learned context | `soul.md` 必须极小、稳定、强治理；普通 memory 不应进入 frozen prefix |
| Mem0 | conversation / session / user / org 分层；extraction and retrieval 分离；ADD-only extraction 避免 premature overwrite | T0/T2 保持 append-only，T3 做受控收敛，org memory 要 principal-aware |
| Zep / Graphiti | temporal graph、fact invalidation、hybrid retrieval | graph/vector/search 只做 derived index，不做 source of truth |

References:

- LangMem conceptual guide: https://langchain-ai.github.io/langmem/concepts/conceptual_guide/
- Letta memory blocks: https://docs.letta.com/guides/core-concepts/memory/memory-blocks/
- Letta sleep-time agents: https://docs.letta.com/guides/agents/architectures/sleeptime/
- Mem0 memory types: https://docs.mem0.ai/core-concepts/memory-types
- Mem0 memory evaluation: https://docs.mem0.ai/core-concepts/memory-evaluation
- Graphiti overview: https://help.getzep.com/graphiti/getting-started/overview

## 6. Target Architecture

### 6.1 Layer Contract

```text
T0 events.jsonl
  Mechanical truth. Append-only. Replayable. Hash/index/segment sealed.

T0 source.md
  Deterministic readable projection of T0 truth.

session_memory.md
  Hot session memory. Current task/session continuity only.
  Used by resume, compact, interruption recovery, current-window continuity.
  Not durable long-term memory by itself.

T2 Segment Package / Rollout Summary
  Codex Phase 1 equivalent.
  Structured raw_memory / summary / labels / source_refs package.
  Append-only by default; no premature overwrite.

T3 Markdown Wiki
  Codex Phase 2 equivalent.
  Accepted semantic layer: episodes.md, user.md, worker.md, capabilities.md.
  Uses source_refs back to T2/T0.

wiki_map.md or memory_summary.md
  Prompt-loaded compact navigation map.
  It is an index/read model, not the full memory.

soul.md
  Stable identity, principles, quality bars, redlines.
  Loaded as frozen prefix.
  Only updated by Soul Dream + Soul Gate + Platform Gate.

skills/
  Verified capability capsules grown from T3 evidence and evals.
  Skill is not a T3 page.

graph/vector/search/relations
  Derived and rebuildable indexes.
  Never source of truth.
```

### 6.2 Dream Split

Current `Dream` should be split conceptually:

| Name | Input | Output | Frequency | Gate |
| --- | --- | --- | --- | --- |
| Memory Dream | T2 packages, current T3 wiki, explicit overlay, workspace diff | T3 patch candidate | session count / time / T3 pressure | T3 Memory Gate + Platform Gate |
| Soul Dream | stable accepted T3, current soul, candidate/evolution ledger | soul patch candidate | slower cadence, high-confidence only | Soul Gate + Platform Soul Gate |

Rules:

- `Memory Dream` is the CC/Codex equivalent.
- `Soul Dream` is Hive-specific.
- `Soul Dream` cannot directly rewrite T3.
- `Memory Dream` cannot directly rewrite soul.md.
- If a Dream only produces held concerns, those concerns should become inputs to the next relevant consolidation batch, not disappear into audit text.

## 7. Agent Necessity Review

### 7.1 Agents We Actually Need

| Runtime role | Keep as separate worker? | Reason |
| --- | --- | --- |
| Main Agent | Yes | User-facing reasoning and tool use |
| Session Memory Writer | Yes, restricted fork | Isolation, low latency, exact write permission to hot session memory |
| T2 Extractor | Yes, batch/parallel worker | Can process sealed segments/rollouts without blocking main loop |
| Memory Dream Consolidator | Yes, serialized worker | Needs lock, workspace diff, T3 write proposal |
| Soul Dream / Soul Reviewer | Yes, but rare | soul.md blast radius is high |
| Skill Evaluator | Yes, gated by skill candidate | Needs eval and promotion ledger |
| Platform Gate | Not an LLM agent | Mechanical validation and atomic commit |

### 7.2 Roles To Collapse

These do not need to be always-separate runtime agents on normal paths:

- Summary Agent
- Learning Brain
- Memory Gate
- Episode Stitcher
- Episode Gate

Recommended shape:

```text
Normal low-risk T2 extraction:
  one Extractor prompt produces summary + labels + source_refs + self-check
  Platform Gate validates hard constraints

Escalated T2 extraction:
  independent Memory Gate reviewer only when:
    - source conflict exists
    - cross-principal / PL3 sensitivity exists
    - identity / capability / behavior rule is being proposed
    - low confidence or missing refs
    - user correction contradicts existing T3/soul

T3 consolidation:
  one Memory Dream Consolidator writes patch
  Memory Gate reviewer only on risky patch classes
```

This preserves rigor without paying multi-agent cost for every small memory write.

## 8. Governance Boundary

The boundary stays unchanged:

```text
LLM:
  judge, extract, summarize, reflect, consolidate, propose candidates

Platform:
  source refs, permissions, sensitivity, dedupe, rollback, audit, atomic commit
```

Forbidden:

- Regex/counters/truncated summaries becoming semantic memory.
- Main Agent directly writing durable T2/T3/soul files.
- T0 raw events going directly into accepted T3 without T2 package.
- `soul.md` being used as ordinary project memory.
- graph/vector/provider memory becoming T3 source of truth.

Allowed:

- Mechanical fallback can hold a candidate with refs and reason.
- Platform can reject, hold, rebase, rollback, quarantine.
- Derived indexes can accelerate retrieval if rebuildable from MD/T0.

## 9. Implementation Plan

### P1. Documentation and naming cleanup

Status: implemented as substrate.

- Add this document as the current Memory Dream alignment target.
- Add a supersession note from `docs/memory-system-flow-map-2026-06-17.md` to this document.
- Rename conceptually:
  - `auto_dream.py` current behavior -> `Soul Dream`
  - new CC/Codex-equivalent consolidation -> `Memory Dream`

### P2. Session Memory lane

Status: implemented.

Add a CC-style session memory surface:

```text
<AGENT_DATA_DIR>/<agent_id>/memory/session_state/<session_id>/session_memory.md
```

Contract:

- Written by the Session Memory Writer LLM when a memory model is available; deterministic projection is only the fallback when the writer cannot run.
- Triggered from the runtime hooks that refresh current continuity: response complete, pre-compaction, turn stop, and session close.
- Used by resume, compact, interruption recovery, and current session continuation.
- Contains current state, task spec, files/artifacts touched, open questions, errors/corrections, worklog.
- Not absorbed into T3 unless later T2/T3 pipeline finds durable signal.

### P3. T2 extraction simplification

Status: implemented.

Change normal T2 from multi-agent pipeline to single structured extraction package:

```text
source_bundle.json
summary.md
labels.md
review.md
manifest.json
```

Every Segment Package review is now Memory Gate LLM authored. Platform Gate can
hold a package when the Memory Gate is unavailable, but it no longer manufactures
low-risk `self_review.md` or `extractor_self_check` approvals.

2026-06-22 branch/rollback-aware completion:

- `source_bundle.json` now carries `lineage`, `visible_source_view`, `context_refs`, and `excluded_refs`.
- `projection_only` and `semantic_memory_eligible=false` T0 events stay in JSONL truth but are excluded from semantic T2 input.
- Branch / rollback / regenerate / edit / compaction-replacement lineage remains visible in the source bundle and review context; Memory Gate review is mandatory for all packages, not only risky packages.
- T2 package manifests persist the same lineage and visible-source view so downstream T2.5/T3 jobs can replay the exact branch view.
- T2.5 episode stitching is lineage-aware: default adjacent-package selection only picks compatible branch lineage; incompatible adjacent packages become `lineage_warnings` and cannot be approved for direct `t3_intake`.

### P4. Memory Dream / T3 consolidation

Status: implemented as staging/workspace substrate. Semantic T3 patch writing still stays with the existing T3 Consolidator prompt and Platform Gate.

Add Codex-style consolidation workspace behavior:

```text
memory/.staging/dream_workspace/
  raw_t2_inputs.md
  rollout_summaries/
  phase2_workspace_diff.md
  baseline metadata
```

Rules:

- claim global per-agent dream lock
- select bounded reviewed T2 packages
- sync selected inputs to workspace
- produce diff from last successful baseline
- if no diff, exit
- if diff exists, run Memory Dream Consolidator
- commit only through T3 Platform Gate

### P5. Soul Dream isolation

Status: implemented by preserving `auto_dream.py` as the Soul Dream path and moving project-memory consolidation into `memory_dream.py`.

Keep `soul.md` promotion strict:

- input only stable accepted T3 + current soul + promotion candidates
- no direct T3 writes
- no single-episode promotion
- independent Soul Gate for all accepted soul patch candidates
- rollback metadata required

### P6. Read path alignment

Status: partially implemented. `soul.md`, T3 retrieval, `wiki_map.md`, and session memory restoration exist. Remaining work is prompt-layer tuning of exactly how the compact map and dynamic memory suffix are worded.

Target read path:

```text
Always prompt-loaded:
  soul.md frozen prefix
  short wiki_map.md / memory_summary.md

Dynamically retrieved:
  selected T3 snippets
  relevant T2 summaries
  current session_memory.md

On demand:
  T0 source.md / events.jsonl by source_refs
  rollout summaries
  derived graph/vector/search results
```

The prompt should preserve static cacheability where possible: static system/soul first, dynamic memory as suffix/context message.

### P7. Tests and verification

Status: implemented for the substrate.

Minimum tests:

- T0 append still writes JSONL truth and MD projection.
- session memory writer can only edit exact session memory path.
- compact/resume can load session memory without treating it as T3.
- T2 low-risk path completes without independent reviewer.
- T2 high-risk path escalates to Memory Gate.
- Memory Dream writes T3 patch candidate but cannot commit without Platform Gate.
- Soul Dream cannot write T3 and cannot edit frozen soul sections.
- derived index rebuild does not mutate truth files.

## 10. Success Criteria

This work is complete only when:

- Hive has a CC-style current session memory lane.
- `Dream` terminology no longer hides two different responsibilities.
- Memory Dream is the T2/T3 global consolidation mechanism.
- Soul Dream is a slower identity promotion mechanism.
- T2 extraction can run cheaply on normal paths and escalates only when needed.
- Read path has a compact always-loaded index plus progressive disclosure.
- T0 JSONL remains the mechanical truth.
- Markdown remains the human/agent-readable durable surface.
- graph/vector/search remain derived and rebuildable.
- enterprise governance still controls permissions, evidence refs, rollback, audit, and final commit.

## 11. Implementation Decisions

The 2026-06-22 substrate implementation resolves the pre-implementation questions this way:

1. The Hive-native compact map is `memory/indexes/wiki_map.md`; `memory_summary.md` can be added later as a Codex-compatible alias if prompt-layer work needs it.
2. `session_memory.md` lives under `memory/session_state/<session_id>/session_memory.md`, not inside the T0 segment and not in `runtime_artifacts` as the primary path.
3. `auto_dream.py` remains the compatibility scheduler entrypoint; project-memory consolidation moved into `memory_dream.py`, while the existing identity-promotion path is treated as Soul Dream.
4. Memory Dream uses a manifest/hash baseline and `phase2_workspace_diff.md` first. A real git baseline is optional future optimization, not required for the substrate.
5. Independent Memory Gate review is triggered by concrete risk signals: PL3/PL4 sensitivity, non-owner principal scope, explicit risk flags, confidence below 0.85, non-closed/non-standalone continuity, branch/rollback/regenerate/edit lineage, compaction-replacement lineage, or high-risk terms such as soul/identity/permission/credential/secret/capability/skill_seed.

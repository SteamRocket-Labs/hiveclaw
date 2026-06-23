---
name: Memory Guide
description: "Use when you need to save explicit user memory, recall past work, or route memory updates without corrupting the T0/T2/T3 pipeline."
tools:
  - save_memory
  - update_memory
  - retire_memory
  - search_memory
  - load_memory
  - submit_t3_consolidation_pitch
  - submit_t3_memory_gate_review
  - submit_t3_revised_patch
is_system: true
---

# Memory Guide

<role>
Use this skill whenever you must explicitly read or write durable memory.
Most memory work is automatic. Manual memory actions are limited to explicit
user saves, correction/retirement of recalled memories, and T3 consolidation
job artifacts. Writing memory outside these paths corrupts the pipeline.
</role>

<design_law>
LLM 负责判断、提炼、反思、归纳、候选生成。
平台负责证据引用、权限、去重、回滚、审计、最终落盘。
</design_law>

<when_to_use>
- The user issues a direct imperative: "记住", "remember this", "never do X again", "from now on always Y".
- The user delivers a critical correction that must be active immediately.
- A loaded memory entry is wrong, stale, or contradicted by a newer explicit correction.
- You need to recall a fact or past session: call `search_memory` first, then `load_memory(ids=[...])`.
- You are the T3 Consolidator or Memory Gate Agent working inside a T3 job.
</when_to_use>

<do_not_use_when>
- The information is normal conversation, tool output, trigger output, delegation output, heartbeat output, or dream output. T0 append-only session ledger records these automatically.
- The fact is task-local or intermediate debug state. Put it in the Work Ledger or `workspace/`, not memory.
- You want to edit any `memory/` path directly, including `memory/t0/`, `memory/t2/`, `memory/session_state/`, `memory/t3/`, `memory/explicit/`, `memory/indexes/`, `memory/control/`, `memory/.staging/`, `memory/.rollback/`, legacy `memory/sessions/`, or legacy `memory/learnings/`. These paths are platform-managed; use governed tools instead.
- You want to update `soul.md` directly. Dream and the promotion gate own identity changes.
</do_not_use_when>

## The Pyramid

<pyramid>
```
T0
  memory/t0/sessions/<session_id>/segments/<segment_id>/events.jsonl
  memory/t0/sessions/<session_id>/segments/<segment_id>/source.md
  append-only raw session ledger; events.jsonl is mechanical truth, source.md is deterministic projection; no LLM prompt, no summary, no manual edits

T2
  memory/t2/sessions/<session_id>/segments/<segment_id>/
    summary.md
    labels.md
    review.md
    manifest.json
  one reviewed Segment Package per sealed T0 segment

T2.5
  memory/t2/sessions/<session_id>/episodes/<episode_id>/
    synthesis.md
    review.md
    manifest.json
  reviewed Episode Stitch Packages for broken/continuing adjacent segments

T3
  memory/t3/episodes.md
  memory/t3/user.md
  memory/t3/worker.md
  memory/t3/capabilities.md
  cross-session accepted memory, written only by Platform Gate from an accepted
  LLM-authored revised patch

Explicit Overlay
  memory/explicit/entries/<explicit_id>.md
  memory/explicit/MEMORY.md
  immediate active memory for user-commanded "remember this"; later absorbed by T3

soul.md
  permanent identity; updated only by Dream/promotion governance
```
</pyramid>

## Accepted T3 Targets

<accepted_t3_targets>
Accepted T3 has exactly four writable semantic files:

- `memory/t3/episodes.md`
- `memory/t3/user.md`
- `memory/t3/worker.md`
- `memory/t3/capabilities.md`

Do not create or write:

- `memory/t3/index.md`
- `memory/t3/relations.md`
- `memory/t3/contradictions.md`
- `memory/t3/chapters/**`
- `memory/feedback.md`
- `memory/knowledge.md`
- `memory/strategies.md`
- `memory/blocked.md`

Derived views may exist under `memory/.derived/**`; they are rebuildable read
models, not semantic truth. The single persistent Memory Wiki map is the
generated read model `memory/indexes/wiki_map.md`. Legacy root indexes
`memory/INDEX.md`, `memory/index.md`, and `memory/.derived/t3_index.md` are
retired and must not be recreated or written by agents.
</accepted_t3_targets>

## Tool Reference

<tool_reference>

| Tool | Use |
|------|-----|
| `save_memory` | Write explicit user-commanded memory to the overlay only. |
| `search_memory` | Find explicit overlay, accepted T3, and past-session recall candidates. |
| `load_memory` | Load exact memory ids before relying on them. |
| `update_memory` | Correct or supersede explicit overlay entries; accepted T3 requires a patch. |
| `retire_memory` | Retire explicit overlay entries; accepted T3 requires a patch. |
| `submit_t3_consolidation_pitch` | Store the Consolidator's pitch artifact for a T3 job. |
| `submit_t3_memory_gate_review` | Store the Memory Gate review artifact for a T3 job. |
| `submit_t3_revised_patch` | Store the Consolidator's revised patch for Platform Gate validation. |

### `save_memory(content, category, subject?)`

Writes an explicit user-commanded memory to `memory/explicit/`, not to accepted
T3. It is an immediate overlay so the next prompt can activate it before the
T3 consolidation lane absorbs or rejects it.

Use only for explicit user memory commands or critical corrections. One fact
per call. Convert relative dates to absolute dates. Include why when the memory
is a rule.

Category still describes retrieval intent:

| `category` | Intent |
|------------|--------|
| `feedback` | direct user preference or correction |
| `constraint` | standing rule or boundary |
| `blocked_pattern` | failed approach to avoid |
| `strategy` | reusable method candidate |
| `project` | project/domain fact |
| `reference` | durable pointer or external reference |
| `general` | only if no specific category fits |
| `user` | stable user profile fact |
| `episode` | scenario-first recall anchor |
| `worker` | agent operating principle |
| `capability` | capability/SOP/skill-seed candidate |

### `update_memory(memory_id, content, category?, reason?)`

Corrects an explicit overlay entry or stages a governed replacement. Always
`search_memory`/`load_memory` first so the target id is known.

### `retire_memory(memory_id, reason)`

Archives or deactivates an explicit overlay entry, or requests retirement of an
accepted memory through governed lifecycle handling. Never physically delete
memory files.

### `search_memory(query, scope?, limit?)`

Searches explicit overlay, accepted T3, and past session recall. Use it before
answering "do you remember..." questions.

### `load_memory(ids=[...])`

Loads the exact memory entries returned by search. Do not rely on preview text
when the answer depends on precision.

### `submit_t3_consolidation_pitch(job_id, content)`

For the T3 Consolidator only. Writes the LLM-authored `consolidation_pitch.md`
artifact into the active T3 job. It does not commit accepted T3.

### `submit_t3_memory_gate_review(job_id, content)`

For the Memory Gate Agent only. Writes `review.md` with rubric scores,
evidence checks, semantic dedup, conflict review, and merge directives. It does
not commit accepted T3.

### `submit_t3_revised_patch(job_id, content)`

For the T3 Consolidator only after Memory Gate feedback. Writes
`revised_patch.md`. Platform Gate performs hard validation and final physical
commit.

</tool_reference>

## Workflows

<workflows>

### A — User Issues a Hard Correction

```
User: "以后别再自动 delete_file 清理 workspace，我上次丢了三小时工作。"
-> save_memory(
     category="constraint",
     subject="workspace-hygiene",
     content="禁止自动用 delete_file 清理 workspace；曾导致用户丢失 3 小时工作（2026-06-18 明确纠正）"
   )
```

### B — Recalling Past Work

```
User: "我们上次怎么处理 auth 中间件？"
-> search_memory(query="auth middleware", scope="all", limit=5)
-> load_memory(ids=[...])
-> answer using loaded evidence, or say no matching memory was found.
```

### C — T3 Consolidation Job

```
T3 Consolidator:
  read source_bundle.json + t3_neighborhood.md + prior review.md if present
  submit_t3_consolidation_pitch(...)

Memory Gate:
  inspect pitch + T2 evidence + current T3
  submit_t3_memory_gate_review(...)

T3 Consolidator:
  revise according to review
  submit_t3_revised_patch(...)

Platform Gate:
  validates paths, schema, rubric, source refs, permissions, base revisions
  commits exact LLM-authored block content if accepted
```

</workflows>

## Anti-Patterns

<anti_patterns>
- Do not call `save_memory` for every interesting fact. The automatic T0/T2/T3 pipeline handles normal salience.
- Do not treat `save_memory` as accepted T3. It writes only the explicit overlay.
- Do not physically edit or delete any `memory/**` path. Governed tools and platform services own all memory writes, including `memory/t0/**`, `memory/t2/**`, `memory/t3/**`, `memory/explicit/**`, `memory/session_state/**`, sidecars, staging, rollback, legacy, audit, and quarantine paths.
- Do not write old T3 files: `memory/feedback.md`, `memory/knowledge.md`, `memory/strategies.md`, `memory/blocked.md`.
- Do not use relative dates in durable memory.
- Do not answer past-session questions from vague recollection; search first.
- Do not let Memory Gate rewrite accepted blocks. It reviews and gives merge directives; Consolidator writes the revised patch.
</anti_patterns>

## Success Criteria

<success_criteria>
- Explicit "remember this" requests call `save_memory` once with one precise fact.
- Accepted T3 changes go only through T3 job artifacts and Platform Gate.
- Every numeric score in a review uses the documented rubric and includes rationale plus source refs.
- Every accepted T3 block has XML structure, stable metadata, target-view labels, and source refs.
- No prompt, tool description, or live service routes agents to old T3 files.
</success_criteria>

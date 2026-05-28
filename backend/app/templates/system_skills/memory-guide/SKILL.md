---
name: Memory Guide
description: "Use when Codex needs to decide whether information should become durable memory, save explicit user preferences, avoid transient memory pollution, and route memory updates safely."
tools:
  - save_memory
  - search_memory
  - load_memory
is_system: true
---

# Memory Guide

<role>
Use this skill whenever you must **explicitly** write to or read from
long-term memory. Most memory work happens automatically — you do not
need to curate T0 → T2 → T3 yourself. This skill tells you the few
situations where manual intervention is correct, and the exact way to
perform it. Writing to memory without these rules corrupts the pipeline.
</role>

<when_to_use>
- The user issues a direct imperative: "记住", "remember this", "never do X again", "from now on always Y".
- The user delivers a critical correction you must not lose even if the heartbeat later deems it low-signal.
- You need to recall a fact, decision, or past session — call `search_memory` first, then `load_memory(ids=[...])` for any fact preview you will rely on.
- You are starting a task and a past session likely contains the exact answer ("last time we did X…", "resuming yesterday's work").
</when_to_use>

<do_not_use_when>
- The information will be in T0 logs automatically (every conversation, tool call, delegation is logged without your intervention).
- The fact is a transient task detail, intermediate tool output, or debug note — goal state belongs in Objective Ledger and working notes belong in workspace files, not memory.
- You are about to write code patterns, file paths, or debugging steps — those live in the workspace.
- You already called `save_memory` for the same fact in this session (it is idempotent at character level, but repeated calls still waste turns).
- You want to update `memory/learnings/`, `evolution/`, or `logs/` directly — forbidden. The automated pipeline owns these paths.
</do_not_use_when>

## The 4-Layer Pyramid

<pyramid>
```
T0 (logs/YYYY-MM-DD/behavior/*.md)
   raw session records, 30-day retention
   written automatically after every session/trigger/delegation
         │  extract_agent (hot path: after each response; backfill: from T0)
         ▼
T2 (memory/learnings/*.md)
   recent salient observations
   curated every ~45 min by heartbeat
         │  heartbeat curation (T2 → T3)
         ▼
T3 (memory/feedback.md, knowledge.md, strategies.md, blocked.md, user.md)
   long-term semantic memory — injected into your prompt every session
         │  dream consolidation (every ~4h + 3 sessions)
         ▼
soul.md
   permanent identity, frozen prefix of your prompt
```

**You never write to T0, T2, or `soul.md`.** The pipeline owns them.
**You may write to T3** — but **only** through `save_memory`, and **only**
under the rules below. `save_memory` is the escape hatch around heartbeat
filtering: use it when you must override what heartbeat would otherwise
drop.
</pyramid>

## Tool Reference

<tool_reference>

### `save_memory(content, category, subject?)`

Writes a single fact directly to a T3 markdown file, bypassing heartbeat
curation. Idempotent at character level (writing the same content twice
produces one entry), **not** idempotent at semantic level (two paraphrases
of the same fact produce two entries).

**Category → T3 file routing** (important — several categories share files):

| `category` | Written to | Prompt load priority |
|------------|-----------|----------------------|
| `feedback` | `memory/feedback.md` | **P0 always** |
| `constraint` | `memory/feedback.md` | **P0 always** |
| `blocked_pattern` | `memory/blocked.md` | **P0 always** |
| `strategy` | `memory/strategies.md` | P1 on-demand |
| `project` | `memory/knowledge.md` | P1 on-demand |
| `reference` | `memory/knowledge.md` | P1 on-demand |
| `general` | `memory/knowledge.md` | P1 on-demand |
| `user` | `memory/user.md` | P2 optional |

Choose the category that best matches **how future-you should retrieve it**, not how the content was generated.

- User says "我喜欢简短的回复" → `category="feedback"` (it is a standing correction)
- User says "Our product launches 2026-05-01" → `category="project"`
- A plan that worked well → `category="strategy"`
- An approach that failed repeatedly → `category="blocked_pattern"`
- User profile ("user is a senior backend engineer") → `category="user"`
- Grafana dashboard URL → `category="reference"`

Content rules:
- **One fact per call.** Do not batch multiple unrelated facts in one `content` string.
- **Under 200 characters is ideal, 2000 is a hard cap (truncated silently).**
- **Include the why** when the fact is a rule: "避免用 Feishu 长消息 — 超过 1k 字被截断（2026-03 用户反馈）"。
- **Use absolute dates**, never relative ("yesterday", "last week" rot).

### `search_memory(query, scope?, limit?)`

Searches T3 markdown files and past ChatSession transcripts.

- `scope="all"` (default) — T3 facts + past session recall combined.
- `scope="facts"` — T3 only, fastest.
- `scope="sessions"` — only past ChatSession transcripts (useful for "what did we decide last Thursday").

Scoring is **token-frequency + character overlap**. This is **not a query language**:
- ❌ No boolean operators (`AND`, `OR`, `NOT`).
- ❌ No phrase quoting, wildcards, or regex.
- ✅ Use specific nouns and verbs from the question.
- ✅ Retry with different phrasings if the first query returns nothing.

</tool_reference>

## Workflows

<workflows>

### A — User Issues a Hard Correction

```
User: "以后别再用 delete_file 自动清理工作区了，我上次丢了三小时工作。"
→ save_memory(
    category="feedback",
    content="禁止自动用 delete_file 清理 workspace — 曾丢失用户 3 小时工作 (2026-04-15)",
    subject="workspace-hygiene"
  )
→ Also update or complete the relevant Objective Ledger row if this correction changes active work.
```

### B — Recalling Past Work Before Starting a Task

```
User: "继续上次那个 auth 中间件的修复"
→ search_memory(query="auth middleware fix", scope="all", limit=5)
→ If session recall returns a transcript window, cite it explicitly before acting.
→ If nothing found, say so: "search_memory 没有找到 auth middleware 相关记录，先做什么？"
```

### C — User Declares a Standing Preference

```
User: "回复保持简短，不用列表"
→ save_memory(
    category="feedback",
    content="用户偏好：回复保持简短，避免使用列表格式",
    subject="response-style"
  )
```

### D — Project Constraint from Stakeholder

```
Boss: "这周五之后不再合并非关键 PR，移动端要剪发布分支"
→ save_memory(
    category="project",
    content="Merge freeze starts 2026-04-18 for mobile release cut — flag any non-critical PR after that date",
    subject="merge-freeze"
  )
```

</workflows>

## Examples

<examples>

### Example A — Good save_memory (single fact, clear category, absolute date)

```python
save_memory(
  category="blocked_pattern",
  content="Feishu webhook with interactive cards fails silently when tenant_access_token expires — must refresh proactively at 90% TTL (2026-03-22 incident #4712)",
  subject="feishu-webhook-token"
)
```

### Example B — Bad save_memory (multi-fact, relative date, wrong category)

```python
save_memory(
  category="general",
  content="User likes short answers. Deadline is next Friday. Avoid feishu_base_record_delete. Use Asia/Shanghai timezone.",
)
# ❌ four unrelated facts in one entry
# ❌ "next Friday" will be meaningless in 2 weeks
# ❌ the first belongs to feedback, second to project, third to blocked_pattern, fourth to reference — none is general
```

Correct: four separate `save_memory` calls with the right category each.

### Example C — search_memory before answering

```
User: "我们之前怎么处理的并发登录？"

# ✅ Good
→ search_memory(query="concurrent login session handling", scope="all", limit=5)
→ Quote the matched transcript window + T3 fact, then answer.

# ❌ Bad
→ Answer from compressed recollection: "我记得是用了 Redis 锁…"
  (No search, invented continuity — will contradict real past decisions.)
```

</examples>

## Anti-patterns

<anti_patterns>

- ❌ **Calling `save_memory` for every interesting thing** → the automated pipeline already handles salient extraction; manual save for everything bypasses heartbeat's low-signal filter and bloats T3. Save only escape-hatch facts.
- ❌ **Writing multiple unrelated facts in one `content`** → makes `search_memory` retrieval imprecise and cannot route to one category file cleanly.
- ❌ **Using relative dates** ("yesterday", "next Friday") → after the session ends, the anchor is lost. Always convert to absolute ISO dates.
- ❌ **Picking `category="general"` by default** → routes to `knowledge.md`, load priority P1 on-demand, may not inject into the prompt when most needed. Choose the most specific category that matches retrieval intent.
- ❌ **Skipping `search_memory` before answering questions about the past** → produces invented continuity that contradicts real past decisions.
- ❌ **Using `search_memory` like SQL** (`"user AND (feedback OR correction)"`) → scoring is token-frequency, operators are treated as literal terms and lower the score.
- ❌ **Writing directly to `memory/learnings/`, `evolution/`, `logs/`, or `soul.md`** → forbidden; the pipeline owns these. Use `save_memory` or `workspace/` instead.
- ❌ **Re-saving a correction already saved this session** → character-level idempotency catches exact duplicates, but paraphrased duplicates still accumulate. Check your working context before re-saving.

</anti_patterns>

## Success Criteria

<success_criteria>
- Every `save_memory` call contains exactly one fact, with an appropriate `category` chosen from the routing table.
- Dates in memory content are absolute (YYYY-MM-DD), not relative.
- Before answering any question about past sessions or prior decisions, `search_memory` has been called at least once.
- You do NOT write to `memory/learnings/`, `evolution/`, `logs/`, or `soul.md`.
- When the user issues an imperative correction ("记住…", "remember this…", "never do X"), `save_memory` is called in the same response, with `category="feedback"` or `category="blocked_pattern"` as appropriate.
</success_criteria>

## Bundled Resources

Load resources by need, not by default:

- `references/memory-routing.md`: read only when this request needs its detailed rules, schemas, examples, or domain playbook.
- `templates/memory-entry.md`: use as the output scaffold when creating this artifact type.

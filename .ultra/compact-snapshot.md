# Compact Snapshot
*Generated: 2026-05-29 06:49:58 UTC*
*Working dir: /Users/rocky243/vc-saas/hiveclaw-main/backend (pyproject.toml)*

## Git State
Branch: `main`

Recent commits:
  e0f6de7 fix(deep-research): cap claims in synthesis payload (live bug RC10/F7)
  042ad79 fix(deep-research): coerce worker_topics from JSON string (live bug RC9)
  346fd33 fix(deep-research): resolve 8 production root causes (F1-F6)
  e56050c Harden Deep Research v2: plan gate, integration synthesis, evidence grading, devil's advocate
  f831ce7 Refine agent memory and runtime workflows

Modified files:
  M ../.ultra/compact-snapshot.md
   M ../.ultra/debug/subagent-log.jsonl
   M ../.ultra/memory/chroma/a6ff9575-dcd6-4ca6-a872-9a01d6acbb57/data_level0.bin
   M ../.ultra/memory/chroma/chroma.sqlite3
   M ../.ultra/memory/sessions.jsonl
   M ../.ultra/sessions/orphan-trail.md
  ?? ../.claude/scheduled_tasks.lock
  ?? ../docs/CHAT_UX_SOTA_PLAN.md

## Session Memory (this branch)
Recent session summaries for context continuity:
- [2026-05-29] ## Accomplished


## Unfinished
Implement F1: increase max_sources with fair worker allocation | Implement F2: PDF text extraction via pdfplumber, drop binary content | Implement F3: per-page trunc...
- [2026-05-28] ## Accomplished


## Decisions
V2 commit aac7149 only half-landed - worker layer added but synthesis still eats 70K+ chars with 4-fold redundancy vs tight digest (orchestrator.py:521-567, reasoner....
- [2026-05-28] Diagnosed root cause: backend streams all events correctly, frontend hides via tool_call default hidden + broken isWaiting + isolated deep_research SSE | Wrote `docs/CHAT_UX_SOTA_PLAN.md` implement...
- [2026-05-28] Analyzed onyx deep research architecture (orchestrator-worker two-layer, ~1500 lines) vs hive pipeline (3407 lines) | Discovered root causes of stitching feeling: lack of digestion layer, context i...
- [2026-05-28] 跨仓对比分析：claude-mem hooks→worker压缩→SQLite+Chroma vs Hive MD-first 4层金字塔 | docs/memory-claude-mem-borrow-plan.md | 识别4个可借鉴点：①索引+load_memory取详情②进程内语义召回③幂等持久队列④成本记账+双轴标签 | 澄清对比框架：开发态插件(SessionStart rece...

## Recovery Instructions
After compact, read this file to restore context:
`Read /Users/rocky243/vc-saas/hiveclaw-main/.ultra/compact-snapshot.md`

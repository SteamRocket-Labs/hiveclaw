# Compact Snapshot
*Generated: 2026-04-06 14:23:41 UTC*
*Working dir: /Users/rocky243/vc-saas/Clawith*

## Git State
Branch: `main`

Recent commits:
  d13ac2d Expand all 16 rules with full MUST/NEVER and actionable context
  c14174e Refactor operating contract: principles only, procedures → skills
  dfe0f60 Add 4 high-impact rules: deliverables, look-before-leap, external state, soul boundaries
  b065030 Harden all prompt rules to MUST/NEVER — no soft language
  e47a25a Add required skill routing table to executing_actions prompt

Modified files:
  M .ultra/compact-snapshot.md
   M .ultra/debug/subagent-log.jsonl
   M .ultra/memory/chroma/a6ff9575-dcd6-4ca6-a872-9a01d6acbb57/data_level0.bin
   M .ultra/memory/chroma/chroma.sqlite3
   M .ultra/memory/daemon-errors.log
   M .ultra/memory/sessions.jsonl
  ?? .ultra/specs/

## Active Subagents
These subagents were running at compact time:
- code-reviewer (id: a5c73a1d6384...)
- general-purpose (id: afdc257ea3e8...)
- Explore (id: a7f6e34240de...)

## Session Memory (this branch)
Recent session summaries for context continuity:
- [2026-04-06] Add cross-phase integration tests for memory pipeline (Phase 7) + Add dream MD→MD consolidation + DREAM.md template (Phase 6) + Redesign heartbeat as KAIROS persistent session with T2→T3 curation (...
- [2026-04-05] web_mcp.py:156 max_results 类型错误已定位 (LLM 传字符串导致 min() 崩溃) | kernel 中 MiniMax M2.7 system role 不兼容已识别 (multi-turn 拒绝) | AutoDream consolidation 丢失 67→0 facts 已定位 (聚合 bug，备份保留)
- [2026-04-05] `web_mcp.py` — 添加 `_safe_int()` 处理 LLM 非标准数字格式 (lines 156,334,927) | `llm_client.py` — OpenAI system message list content 保护 + Anthropic 连续 user 消息合并 (lines 247-260, 1398-1407) | `activity_logger.p...
- [2026-04-04] 深入研究 machine-dream_AG 的 Dreaming Pipeline、ImportanceCalculator、FastClusterV2 聚类、LearningUnitManager 版本化机制 | 对比分析 Machine Dream vs Hive 进化体系差异（场景、触发、存储、模式提取、追踪、失败学习） | 识别 6 个高中价值借鉴机制：重要性评分（P0）、聚类后再综...
- [2026-04-04] 8-dimension design review completed (AI Slop detection, visual hierarchy, cognitive load, emotional journey, discoverability, composition, typography, color) | Identified 7 priority issues includin...

## Recovery Instructions
After compact, read this file to restore context:
`Read /Users/rocky243/vc-saas/Clawith/.ultra/compact-snapshot.md`

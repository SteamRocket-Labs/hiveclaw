# Hive Docs Current Map

> 状态：2026-06-15 当前文档入口。`docs/` 根目录只放近期仍可作为工程真相面或当前设计入口的文档；历史计划、旧诊断和讨论稿归档到 `docs/archive/legacy-docs/`。

## Current Truth Surface

- `hive-sota-master-goal.md` — Hive SOTA 总目标、总矩阵和以后每轮循环对照的 canonical 第一入口。
- `harness-engineering-audit-2026-06-11.md` — 第一轮 harness 工程审计、整改记录和验收证据。
- `round2-sota-benchmark-2026.md` — 第二轮 SOTA benchmark、详细竞品对标、当前能力差距和已完成 milestone 证据库。
- `self-evolution-sota-plan.md` — 自我进化 foundation canonical plan。
- `agent-memory-purity-spec.md` — memory purity、lifecycle、hygiene contract。
- `external-behavior-eval-ci.md` — 外部行为 eval CI canonical design。
- `remote-workstation-runtime.md` — agent 远程工作站 runtime canonical design。

## Active Design Areas

`hive-sota-master-goal.md` §3 is the canonical atomic capability map. Use it to resolve overlap between design docs.

- Plan Mode: `plan-mode-design.md` is the entry point; `plan-mode-agent-authored-planning.md`、`plan-mode-runtime-paradigm.md`、`plan-mode-path-unification.md`、`plan-mode-agent-work-ledger.md` are detail tracks.
- Workflow: `workflow-source-capability.md` is the source capability entry; `workflow-ops-runbook.md` is the production runbook; `execution-mode-spectrum.md` explains when to use workflow vs other modes.
- Subagents / delegation: `subagent-source-capability.md` is the source capability entry; `subagent-evolution-loop.md` covers subagent memory/definition promotion.
- Agent TodoList / Work Ledger / Progress Ledger: `agent-task-cognitive-scaffold.md` is the CC Task/Todo alignment entry; `plan-mode-agent-work-ledger.md` covers the Plan Mode boundary.
- Memory / self-evolution: `agent-evolution-memory-redesign-2026-06-20.md`、`self-evolution-sota-plan.md`、`agent-memory-md-first-spec.md`、`agent-memory-purity-spec.md`、`agent-memory-research.md`、`owner-steward-agent-memory-design.md`。
- Skills / MCP / extension surface: `agent-extension-surface-skill-mcp.md`、`SKILLS_AND_PACKS_V2.md`、`capability-pack-consolidation.md`、`cc-tooling-alignment-and-plugin-system.md`。
- Trigger / automation: `trigger-cc-alignment.md`、`execution-mode-spectrum.md`。
- Frontend / agent workbench: `frontend-agent-workbench-redesign-2026-06-20.md`；`frontend-claude-design-migration-plan.md` remains the broader prototype-to-frontend migration baseline.
- Office / document / multimodal: `document-conversion-multimodal-design.md`。
- Web / data source ingestion: `web-data-source-layer-plan-2026-06-20.md`；known URL / OCR / document conversion boundary resolves through `document-conversion-multimodal-design.md`。
- Remote workstation / code execution: `remote-workstation-runtime.md`。
- Knowledge / connector ACL / control plane: `knowledge-container-boundaries.md`、`org-agent-asset-rights-model.md`。
- RLS: `rls-stage0-findings.md`、`rls-enforcement-migration-plan.md`、`rls-stage3-cutover.md`。
- Eval / observability: `external-behavior-eval-ci.md`、`agent-framework-cc-sota-atomic-audit-2026-06-15.md`。

## Archive Policy

Archived docs are not deleted. They are kept for evidence, historical reasoning, and old implementation context, but they should not be treated as current source of truth without re-verifying code and production state.

Archive path:

```text
docs/archive/legacy-docs/
```

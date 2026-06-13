# Hive Docs Current Map

> 状态：2026-06-13 当前文档入口。`docs/` 根目录只放近期仍可作为工程真相面或当前设计入口的文档；历史计划、旧诊断和讨论稿归档到 `docs/archive/legacy-docs/`。

## Current Truth Surface

- `harness-engineering-audit-2026-06-11.md` — 第一轮 harness 工程审计、整改记录和验收证据。
- `round2-sota-benchmark-2026.md` — 第二轮 SOTA benchmark、当前能力差距和已完成 milestone。
- `self-evolution-sota-plan.md` — 自我进化 foundation canonical plan。
- `agent-memory-purity-spec.md` — memory purity、lifecycle、hygiene contract。
- `external-behavior-eval-ci.md` — 外部行为 eval CI canonical design。
- `remote-workstation-runtime.md` — agent 远程工作站 runtime canonical design。

## Active Design Areas

- Plan Mode: `plan-mode-design.md`、`plan-mode-agent-authored-planning.md`、`plan-mode-runtime-paradigm.md`、`plan-mode-path-unification.md`、`plan-mode-agent-work-ledger.md`。
- Workflow / runtime: `workflow-source-capability.md`、`workflow-ops-runbook.md`、`execution-mode-spectrum.md`。
- Memory / knowledge: `agent-memory-md-first-spec.md`、`knowledge-container-boundaries.md`。
- Agent assets / control plane: `org-agent-asset-rights-model.md`、`SKILLS_AND_PACKS_V2.md`、`agent-extension-surface-skill-mcp.md`。
- Subagents: `subagent-source-capability.md`、`subagent-evolution-loop.md`。
- RLS: `rls-stage0-findings.md`、`rls-enforcement-migration-plan.md`、`rls-stage3-cutover.md`。

## Archive Policy

Archived docs are not deleted. They are kept for evidence, historical reasoning, and old implementation context, but they should not be treated as current source of truth without re-verifying code and production state.

Archive path:

```text
docs/archive/legacy-docs/
```


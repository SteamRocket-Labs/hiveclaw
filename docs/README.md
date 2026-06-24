# Hive 文档当前地图

> 状态：2026-06-15 当前文档入口。`docs/` 根目录只放近期仍可作为工程真相面或当前设计入口的文档；历史计划、旧诊断和讨论稿归档到 `docs/archive/legacy-docs/`。

## 当前真相入口

- `hive-sota-master-goal.md` — Hive SOTA 总目标、总矩阵和以后每轮循环对照的 canonical 第一入口。
- `cc-python-evolution-north-star-2026-06-22.md` — Hive 作为 CC Python evolution + Memory/Iter + Codex delta 的北极星，以及单 Session 全面排查总纲。
- `ccplus-north-star-contract-2026-06-24.md` — CCPlus 边界契约：CC/FreeCode 是语义基底，Codex 只做工程控制增强；本地 CLI 语义在 scope 内，供应商远程独占能力不作为 CC parity 要求。
- `ccplus-freecode-00-08-terminal-audit-2026-06-24.md` — 对照 FreeCode `docs/00` 到 `docs/08` 的 CCPlus 终极排查入口：先达成 CC/FreeCode 生命周期，再吸收 Codex 工程优势，并明确 Hive-native Memory 与 provider-hosted / proprietary remote 能力排除边界。
- `ccplus-company-control-plane-a2a-permission-design-2026-06-24.md` — CCPlus Phase 2 叠加设计：在 00-08 基底之上定义公司级权限中台、RelationshipGraph、A2A Session Evidence、Project/Agent Link 与 Hive Connect 映射。
- `agent-lifecycle-full-cc-parity-review-2026-06-22.md` — Agent 全生命周期 CC 对标 review，覆盖 context composition、Skill、Sub-agent、Workflow、Hooks 和 session-middle 改造顺序。
- `ccplus-session-middle-parity-audit-2026-06-24.md` — Plan Mode / Subagent / Hooks / Agent Team / Schedule / Goal 是否已形成 CCPlus 的最新代码级审计结论；当前判定为底座强，但还不是完整 CCPlus。
- `harness-engineering-audit-2026-06-11.md` — 第一轮 harness 工程审计、整改记录和验收证据。
- `round2-sota-benchmark-2026.md` — 第二轮 SOTA benchmark、详细竞品对标、当前能力差距和已完成 milestone 证据库。
- `self-evolution-sota-plan.md` — 自我进化 foundation canonical plan。
- `agent-memory-purity-spec.md` — memory purity、lifecycle、hygiene contract。
- `external-behavior-eval-ci.md` — 外部行为 eval CI canonical design。
- `remote-workstation-runtime.md` — agent 远程工作站 runtime 探索稿；它记录安全边界和可能切片，不是当前 canonical 主线。

## 活跃设计领域

`hive-sota-master-goal.md` 第 3 节是 canonical 原子能力地图。设计文档之间出现重叠时，优先用它裁决边界。

- Plan Mode：`plan-mode-design.md` 是入口；`plan-mode-agent-authored-planning.md`、`plan-mode-runtime-paradigm.md`、`plan-mode-path-unification.md`、`plan-mode-agent-work-ledger.md` 是细节轨道。
- Workflow：`workflow-source-capability.md` 是源能力入口；`workflow-ops-runbook.md` 是生产 runbook；`execution-mode-spectrum.md` 解释 workflow 和其他模式的使用边界。
- Subagents / delegation：`a2a-session-substrate-design-2026-06-24.md` 是 Session-first A2A/delegation 设计入口；`subagent-source-capability.md` 是源能力入口；`subagent-evolution-loop.md` 覆盖 subagent memory/definition promotion。
- Company Control / Relationship / A2A：`ccplus-company-control-plane-a2a-permission-design-2026-06-24.md` 是 Phase 2 总入口；它把 Permission Control Plane、RelationshipGraph、Project/Agent Link、A2A Session Evidence 与 Hive Connect 统一为 00-08 基底上的 Hive-native control-plane overlay。
- Local Agent / Hive Bridge：`hive-bridge-cc-connect-fork-plan-2026-06-24.md` 是当前 Local Agent runner 决策：fork cc-connect 成为 Hive-owned local runner，Hive Cloud 保持 IM/control-plane 真相源，并使用 cc-connect core/session/agent adapters 作为 local runtime substrate。`local-agent-bridge-first-pass-2026-06-22.md` 只保留历史 first-pass context，不能覆盖这个 fork plan。
- Agent TodoList / Work Ledger / Progress Ledger：`agent-task-cognitive-scaffold.md` 是 CC Task/Todo 对齐入口；`plan-mode-agent-work-ledger.md` 覆盖 Plan Mode 边界。
- Memory / self-evolution: `agent-evolution-memory-redesign-2026-06-20.md`、`self-evolution-sota-plan.md`、`agent-memory-md-first-spec.md`、`agent-memory-purity-spec.md`、`agent-memory-research.md`、`owner-steward-agent-memory-design.md`。
- Skills / MCP / extension surface: `agent-extension-surface-skill-mcp.md`、`SKILLS_AND_PACKS_V2.md`、`capability-pack-consolidation.md`、`cc-tooling-alignment-and-plugin-system.md`。
- Trigger / automation: `trigger-cc-alignment.md`、`execution-mode-spectrum.md`。
- CC/Codex session runtime alignment：`ccplus-north-star-contract-2026-06-24.md` 是边界契约；`ccplus-freecode-00-08-terminal-audit-2026-06-24.md` 是 00-08 终极排查入口；`cc-python-evolution-north-star-2026-06-22.md`、`agent-lifecycle-full-cc-parity-review-2026-06-22.md`、`ccplus-session-middle-parity-audit-2026-06-24.md` 是支撑审计入口；`session-loop-cc-alignment-plan.md`、`t0-append-only-session-ledger-redesign-2026-06-18.md`、`conversation-experience-cc-codex-parity-plan-2026-06-22.md`、`chat-runtime-disclosure-cc-codex-alignment-2026-06-22.md`、`frontend-session-workbench-cc-codex-parity-gap-2026-06-23.md` 是支撑轨道。
- Frontend / agent workbench：`frontend-session-workbench-cc-codex-parity-gap-2026-06-23.md` 是 Phase 1，也是当前 Session Workbench refactor truth surface：优先完成 in-session Codex Desktop / CC-grade timeline、active-run-cell、composer、inspector、session-native controls。`frontend-agent-workbench-redesign-2026-06-20.md` 是 Phase 2 和更大的 agent workbench redesign baseline；它必须复用 Phase 1 的 Session Workbench contract，不能重造 conversation UX。`frontend-claude-design-migration-plan.md` 保留为更大的 prototype-to-frontend migration baseline；`chat-runtime-disclosure-cc-codex-alignment-2026-06-22.md` 是 CC/Codex-aligned runtime disclosure 和 transcript replay 设计。
- Office / document / multimodal: `document-conversion-multimodal-design.md`。
- Web / data source ingestion: `web-data-source-layer-plan-2026-06-20.md`；known URL / OCR / document conversion boundary resolves through `document-conversion-multimodal-design.md`。
- Remote workstation / code execution: `remote-workstation-runtime.md`。
- Knowledge / connector ACL / control plane: `knowledge-container-boundaries.md`、`org-agent-asset-rights-model.md`。
- RLS: `rls-stage0-findings.md`、`rls-enforcement-migration-plan.md`、`rls-stage3-cutover.md`。
- Eval / observability: `external-behavior-eval-ci.md`、`agent-framework-cc-sota-atomic-audit-2026-06-15.md`。

## 归档策略

归档文档不会删除。它们保留为证据、历史推理和旧实现上下文，但在没有重新验证代码和生产状态前，不能当作当前真相源。

归档路径：

```text
docs/archive/legacy-docs/
```

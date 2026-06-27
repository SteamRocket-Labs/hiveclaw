# Hive 文档当前地图

> 状态：2026-06-15 当前文档入口。`docs/` 根目录只放近期仍可作为工程真相面或当前设计入口的文档；历史计划、旧诊断和讨论稿归档到 `docs/archive/legacy-docs/`。

## 当前真相入口

- `ccplus-unclosed-gap-register-2026-06-27.md` — 上线前未闭环阻断项登记表：记录已关闭证据（`/compact`/`/rewind` next-turn context、前端 command UI action、command registry active projection 文案）和剩余不能宣称闭环的部分（TurnEnvelope source-of-truth、Hooks/MCP/Skill/Dynamic Workflow 等）。
- `ccplus-session-checkpoint-branch-ui-upgrade-plan-2026-06-27.md` — Session checkpoint / rewind / branch 的后续 UI/UX 升级方案：在基础 `SessionCommandControlPanel` 已闭合后，规划 checkpoint timeline rail、active head、rewound tail、branch graph、compact marker、Session Inspector 等体验深化。
- `ccplus-final-prelaunch-convergence-master-plan-2026-06-27.md` — 上线前最后一轮 CCPlus 优化统领入口：把 Session Control Spine、AgentTool/Sub-agent/Completion Bus、Agent Team/A2A Session-first、TurnEnvelope/Workbench/Hooks/Skill/MCP 四条主线统一成一个执行顺序和验收口径；2026-06-27 复核后不再单独作为“全部闭环”证明，未闭环裁决以 gap register 为准。
- `hive-sota-master-goal.md` — Hive SOTA 总目标、总矩阵和以后每轮循环对照的 canonical 第一入口。
- `cc-python-evolution-north-star-2026-06-22.md` — Hive 作为 CC Python evolution + Memory/Iter + Codex delta 的北极星，以及单 Session 全面排查总纲。
- `ccplus-north-star-contract-2026-06-24.md` — CCPlus 边界契约：CC/FreeCode 是语义基底，Codex 只做工程控制增强；本地 CLI 语义在 scope 内，供应商远程独占能力不作为 CC parity 要求。
- `ccplus-freecode-00-08-terminal-audit-2026-06-24.md` — 对照 FreeCode `docs/00` 到 `docs/08` 的 CCPlus 终极排查入口：先达成 CC/FreeCode 生命周期，再吸收 Codex 工程优势，并明确 Hive-native Memory 与 provider-hosted / proprietary remote 能力排除边界。
- `ccplus-v1-deep-verification-reconciliation-2026-06-24.md` — CCPlus V1 主审计与 deep-verification 的合并裁决：统一“行为级 P0=0”与“工程阻断仍存在”的口径，并把 D-01 到 D-32 债务落入 Package A-G / A1。
- `ccplus-v1-implementation-evidence-2026-06-24.md` — CCPlus V1 当前实现证据账本：每个 Package 完成后追加 Red/Green 测试、lint、剩余边界与 commit 证据。
- `ccplus-freecode-00-08-deep-verification-2026-06-24.md` — CC 自检版 deep-verification 证据账本：267 条原子 verdict、00-08 file:line 复核、D-01 到 D-32 技术债总账；不替代 V1 主入口。
- `ccplus-round2-v2-hive-connect-master-plan-2026-06-24.md` — CCPlus Round 2 / V2 总入口：在 V1 / 00-08 基底之上定义 Hive Connect 七条主线，覆盖 A2A、Memory、企业知识库 / Ontology、Skill 进化、Workflow、权限控制和 Session 对话控制。
- `ccplus-round2-v2-company-control-plane-a2a-permission-design-2026-06-24.md` — CCPlus Round 2 / V2 专项设计：在 Master Plan 下展开公司级权限中台、RelationshipGraph、A2A Session Evidence、Project/Agent Link 与 Hive Connect local runtime governance。
- `ccplus-session-ux-contract-2026-06-26.md` — CCPlus Session UX 契约：把 CC 能力内核、Codex 式 session 信息分层、Hive 企业治理边界收成一个体验准绳；规定用户默认看到 Agent 工作判断而非工具流水账，Plan Mode 必须是“观察事实 + 关键判断 + 执行范围 + 验证方式 + 风险”的执行前提案，权限菜单只用请求批准 / 替我批准 / 完全访问，最终交付物必须作为 session 内 artifact 卡片出现并在右侧 inspector 预览；在上线前最后一轮中作为 Session Control / Workbench / artifact / raw JSON 折叠的 UX contract。
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
- Workflow：`workflow-source-capability.md` 是确定性 runtime 源能力入口；`dynamic-workflow-ccplus-implementation-plan-2026-06-27.md` 是上线前 Dynamic Workflow 统领实施入口，规定唯一链路 `propose_dynamic_workflow -> preview_workflow -> exact approval -> start_workflow -> journal/repair/promote`；`workflow-ops-runbook.md` 是生产 runbook；`execution-mode-spectrum.md` 解释 workflow 和其他模式的使用边界；`dynamic-workflow-harness-semantics-2026-06-24.md` 保留为 Dynamic Harness 语义补充，`a2a-workflow-orchestration-design-2026-06-24.md` 是完整 Agent 间 A2A Process Graph 专项。
- Subagents / delegation：`ccplus-final-prelaunch-convergence-master-plan-2026-06-27.md` 是上线前最后一轮统领入口；其中明确 To Session Worker 走 CC-compatible AgentTool / internal `spawn_subagent` / completion bus，To Employee 才走 A2A `delegate_to_agent` / `send_message_to_agent`。`a2a-session-substrate-design-2026-06-24.md` 是 Session-first A2A/delegation 设计入口；`subagent-source-capability.md` 是源能力入口；`subagent-evolution-loop.md` 覆盖 subagent memory/definition promotion。
- Company Control / A2A / Hive Connect：`ccplus-round2-v2-hive-connect-master-plan-2026-06-24.md` 是 CCPlus Round 2 / V2 总入口；它把 A2A、Memory、企业知识库 / Ontology、Skill 进化、Workflow、权限控制和 Session 对话控制统一为 V1 / 00-08 基底上的 Hive-native overlay。`a2a-integrated-implementation-plan-2026-06-27.md` 是 A2A 三层总计划；`a2a-relationship-retirement-plan-2026-06-27.md` 是 A2A Layer 1 的迁移裁决且已完成第一轮闭合：删除旧 Relationship Python 路径，不再用 `relationships.md` 作为 A2A 名单 / 权限 / prompt source，A2A collaborator read model 成为唯一 To Employee 主路径。后续 A2A Session-first 工作必须服从 `ccplus-final-prelaunch-convergence-master-plan-2026-06-27.md` 的顺序，排在 Session Control Spine、AgentTool/Completion Bus、Agent Team Runtime 之后。`ccplus-round2-v2-company-control-plane-a2a-permission-design-2026-06-24.md` 保留为权限、RelationshipGraph、A2A Evidence 与 Hive Connect local runtime 的上游背景。`ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md` 是当前 session 权限菜单、企业 Agent 删除硬规则、完整 CC Hook contract 优先、以及哪些 runtime / governance surface 必须放入 Session/T0 闭环的生产修复裁决；更复杂的 tenant-wide enterprise Hook governance 延后。
- Local Agent / Hive Bridge：`hive-bridge-cc-connect-fork-plan-2026-06-24.md` 是当前 Local Agent runner 决策：fork cc-connect 成为 Hive-owned local runner，Hive Cloud 保持 IM/control-plane 真相源，并使用 cc-connect core/session/agent adapters 作为 local runtime substrate。`local-agent-bridge-first-pass-2026-06-22.md` 只保留历史 first-pass context，不能覆盖这个 fork plan。
- Agent TodoList / Work Ledger / Progress Ledger：`agent-task-cognitive-scaffold.md` 是 CC Task/Todo 对齐入口；`plan-mode-agent-work-ledger.md` 覆盖 Plan Mode 边界。
- Memory / self-evolution: `agent-evolution-memory-redesign-2026-06-20.md`、`self-evolution-sota-plan.md`、`agent-memory-md-first-spec.md`、`agent-memory-purity-spec.md`、`agent-memory-research.md`、`owner-steward-agent-memory-design.md`。
- Skills / MCP / extension surface: `agent-extension-surface-skill-mcp.md`、`SKILLS_AND_PACKS_V2.md`、`capability-pack-consolidation.md`、`cc-tooling-alignment-and-plugin-system.md`。
- Trigger / automation: `trigger-cc-alignment.md`、`execution-mode-spectrum.md`。
- CC/Codex session runtime alignment：`ccplus-final-prelaunch-convergence-master-plan-2026-06-27.md` 是上线前最后一轮总入口；`ccplus-runtime-context-agenttool-codex-delta-gap-audit-2026-06-27.md` 是 Runtime / Context / AgentTool / Codex delta 审计主文档；`ccplus-subagent-team-skill-mcp-hooks-parity-audit-2026-06-27.md` 是 Subagent / Agent Team / Skill / MCP / Hooks 子系统附录；`ccplus-session-control-command-alignment-2026-06-27.md` 是 Session Control Spine 专项 contract；`ccplus-session-runtime-token-compaction-alignment-2026-06-27.md` 是 token / compaction / context window 底座证据；`ccplus-session-native-closure-gap-ledger-2026-06-25.md` 是 Session 全闭环差距总账；`ccplus-session-ux-contract-2026-06-26.md` 是当前 Session UX 契约，规定工作判断、Plan Mode、权限降维、工具折叠和最终交付物右侧预览；`ccplus-north-star-contract-2026-06-24.md` 是边界契约；`ccplus-freecode-00-08-terminal-audit-2026-06-24.md` 是 00-08 终极排查入口；`ccplus-v1-deep-verification-reconciliation-2026-06-24.md` 统一 terminal-audit 与 `ccplus-freecode-00-08-deep-verification-2026-06-24.md` 的执行口径；`ccplus-v1-implementation-evidence-2026-06-24.md` 是当前实现与测试证据账本；`cc-python-evolution-north-star-2026-06-22.md`、`agent-lifecycle-full-cc-parity-review-2026-06-22.md`、`ccplus-session-middle-parity-audit-2026-06-24.md` 是支撑审计入口；`session-loop-cc-alignment-plan.md`、`t0-append-only-session-ledger-redesign-2026-06-18.md`、`conversation-experience-cc-codex-parity-plan-2026-06-22.md`、`chat-runtime-disclosure-cc-codex-alignment-2026-06-22.md`、`frontend-session-workbench-cc-codex-parity-gap-2026-06-23.md` 是支撑轨道。
- Frontend / agent workbench：`ccplus-session-ux-contract-2026-06-26.md` 是下一轮 Session UI/UX 的产品契约，特别是工作判断优先、工具折叠、Plan Mode 提案、权限菜单简化、交付物 artifact 卡片和右侧 inspector 预览；`frontend-session-workbench-cc-codex-parity-gap-2026-06-23.md` 是 Phase 1，也是当前 Session Workbench refactor truth surface：优先完成 in-session Codex Desktop / CC-grade timeline、active-run-cell、composer、inspector、session-native controls。`frontend-agent-workbench-redesign-2026-06-20.md` 是 Phase 2 和更大的 agent workbench redesign baseline；它必须复用 Phase 1 的 Session Workbench contract，不能重造 conversation UX。`frontend-claude-design-migration-plan.md` 保留为更大的 prototype-to-frontend migration baseline；`chat-runtime-disclosure-cc-codex-alignment-2026-06-22.md` 是 CC/Codex-aligned runtime disclosure 和 transcript replay 设计。
- Office / document / multimodal: `document-conversion-multimodal-design.md`。
- Web / data source ingestion: `web-data-source-layer-plan-2026-06-20.md`；known URL / OCR / document conversion boundary resolves through `document-conversion-multimodal-design.md`。
- Remote workstation / code execution: `remote-workstation-runtime.md`。
- Knowledge / connector ACL / control plane: `company-knowledge-ontology-plane-plan-2026-06-20.md` 是 V2 企业知识库 / Ontology / SAG 专项入口；`knowledge-container-boundaries.md`、`org-agent-asset-rights-model.md` 是支撑边界文档。
- RLS: `rls-stage0-findings.md`、`rls-enforcement-migration-plan.md`、`rls-stage3-cutover.md`。
- Eval / observability: `external-behavior-eval-ci.md`、`agent-framework-cc-sota-atomic-audit-2026-06-15.md`。

## 归档策略

归档文档不会删除。它们保留为证据、历史推理和旧实现上下文，但在没有重新验证代码和生产状态前，不能当作当前真相源。

归档路径：

```text
docs/archive/legacy-docs/
```

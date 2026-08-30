---
document_id: weekend-rc-2026-08-30-journey-ledger
owner: Rocky / Codex
status: active
authority: canonical-human-journey-ledger
last_reviewed: 2026-08-31
source_commit: c18b181c
verification_status: frozen-production-denominator-96-no-pass-evidence-yet
---

# Journey Ledger

[返回索引](README.md) · [当前状态](03-current-status.md) · [Runbook](06-runbook-and-release-gates.md)

本文件拥有旅程分母候选、闭环状态和证据链接。Domain 文档拥有验收标准；Evidence 文件拥有实际结果；本文件不复制两者正文。

## 分母状态

- 当前：`Frozen`，共 **96** 条可独立计分的 production journeys。
- 机器权威：[`acceptance/weekend_production_journeys.v1.json`](../../../acceptance/weekend_production_journeys.v1.json)，freeze basis `c18b181c690fe3c4aa5366a8fd504023b0c41864`；记录 persona、entry、data version、allowed effects、acceptance、fault probes、evidence path 和 cleanup。
- 冻结后不得删除或合并失败项；owner 只能带理由标为 `Excluded`。
- `BLOCKED_PRECONDITION` 留在分母并按失败计，不得用受控 fake、历史 PASS 或未执行状态替代。
- production release 要求全部冻结旅程在同一 exact commit 连续两遍 clean pass；组级通过不能替代子旅程。

## 现有确定性 CI 基线

来源：[`acceptance/atomic_user_journeys.v1.json`](../../../acceptance/atomic_user_journeys.v1.json)。下表只是映射；是否当前通过必须以重新运行结果为准。

| ID | CI 旅程 | 对应领域 | 当前用途 |
|---|---|---|---|
| J-01 | message_to_terminal_answer | Single Agent / Session | deterministic CI floor |
| J-02 | upload_to_deliverable | Frontend / Artifact | deterministic CI floor |
| J-03 | plan_confirm_to_observation | Single Agent / Plan | deterministic CI floor |
| J-04 | goal_long_task | Single Agent / Goal | deterministic CI floor |
| J-05 | schedule_trigger_delivery | Automation | deterministic CI floor |
| J-06 | branch_fork_rewind | Session recovery | deterministic CI floor |
| J-07 | personal_knowledge_ingest_search | Knowledge | deterministic CI floor |
| J-08 | skill_discover_load_evolve | Growth / Capability | deterministic CI floor |
| J-09 | spawn_subagent | Collaboration | deterministic CI floor |
| J-10 | agent_team_aggregate | Collaboration | deterministic CI floor |
| J-11 | dynamic_workflow | Workflow | deterministic CI floor |
| J-12 | hr_confirm_provision | HR | deterministic CI floor |
| J-13 | channel_ingress_delivery | Channel | deterministic CI floor |
| J-14 | local_agent_bridge | Local Agent | deterministic CI floor |
| J-15 | operator_inspector_audience | Frontend / Audience | deterministic CI floor |

该 manifest 声明 `llm_provider`、`channel_provider`、`sandbox_provider`、`local_bridge_peer` 为 external fakes；因此 15/15 绿不能计为 production NPTCR。

## Production journey 候选组

| Candidate ID | 旅程组 | Domain 权威 | 分母状态 | 当前闭环判断 |
|---|---|---|---|---|
| PJ-01 | 单 Agent 真实开放任务与 CCPlus 生命周期 | [Single Agent](domains/single-agent-and-session.md) | Frozen ×1 | Partial loop |
| PJ-02 | Session streaming、terminal、failure、reload 同构 | [Single Agent](domains/single-agent-and-session.md) | Frozen ×1 | Partial loop；核心子集有历史 Closed 证据 |
| PJ-03 | 20 条斜杠命令逐条产品闭环 | [Single Agent](domains/single-agent-and-session.md) | Frozen ×20 | Breakpoint |
| PJ-04 | Plan / Goal / Task / Ledger | [Single Agent](domains/single-agent-and-session.md) | Frozen ×3 | Partial loop |
| PJ-05 | J1 candidate provisional trial | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×1 | Partial loop |
| PJ-06 | J2 longitudinal growth 与 owner feedback | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×1 | Partial loop |
| PJ-07 | J3 platform change non-regression | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×1 | Partial loop |
| PJ-08 | J4 FreeCode/Hermes real bakeoff | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×1 | BLOCKED_PRECONDITION until real run |
| PJ-09 | Agent Memory T0→T2→T3→Soul/Skill reuse | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×1 | Partial loop |
| PJ-10 | Personal KB multi-format ingest/search/read/cite | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×5 | Partial loop |
| PJ-11 | Company KB direct/background import→publish→read | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×2 | Partial loop |
| PJ-12 | Personal/Agent→Company promotion 与治理 | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×1 | Partial loop |
| PJ-13 | HR 创建、revise/reject/confirm/provision/首任务 | [HR/Identity](domains/hr-identity-and-permissions.md) | Frozen ×1 | Partial loop |
| PJ-14 | Agent→HR 受治理 handoff | [HR/Identity](domains/hr-identity-and-permissions.md) | Frozen ×1 | Partial loop |
| PJ-15 | 角色/权限正负向与 active revocation | [HR/Identity](domains/hr-identity-and-permissions.md) | Frozen ×4 | Breakpoint |
| PJ-16 | owner transfer、offboarding、retention/export/delete | [HR/Identity](domains/hr-identity-and-permissions.md) | Frozen ×3 | Partial loop / Missing policies |
| PJ-17 | Sub-agent 完成、失败、取消、父任务消费 | [Collaboration](domains/collaboration-workflow-and-a2a.md) | Frozen ×1 | Partial loop |
| PJ-18 | Agent Team fanout/review/partial failure/integration | [Collaboration](domains/collaboration-workflow-and-a2a.md) | Frozen ×1 | Partial loop |
| PJ-19 | Dynamic Workflow preview/confirm/run/wait/resume/result | [Collaboration](domains/collaboration-workflow-and-a2a.md) | Frozen ×1 | Partial loop |
| PJ-20 | Fixed A2A Workflow version/publish/run/audit | [Collaboration](domains/collaboration-workflow-and-a2a.md) | Frozen ×1 | Partial loop |
| PJ-21 | A2A sync/async/continuation/nested/artifact/fixed edge | [Collaboration](domains/collaboration-workflow-and-a2a.md) | Frozen ×6 | Partial loop |
| PJ-22 | once/schedule/bounded loop/event trigger | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×4 | Breakpoint aggregate |
| PJ-23 | Notification/Approval/Channel return loop | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×3 | Breakpoint aggregate |
| PJ-24 | Local Agent pair/online/offline/approval/reconnect/revoke | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×1 | BLOCKED_PRECONDITION until live bridge |
| PJ-25 | Hook blocking/observe-only/lifecycle/recovery | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×3 | Breakpoint aggregate |
| PJ-26 | Skill trust/load/use/update/revoke | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×1 | Breakpoint aggregate |
| PJ-27 | MCP/Connector auth/use/expiry/revoke/schema change | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×1 | Breakpoint aggregate |
| PJ-28 | Agent rail/AgentDetail employee scale and navigation | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×3 | Breakpoint |
| PJ-29 | Employee/admin/platform/operator audience split | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×4 | Breakpoint |
| PJ-30 | Artifact preview/download/version/ACL/reopen | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×4 | Breakpoint aggregate |
| PJ-31 | Async deep-link/inbox/unread/dedupe/expiry | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×1 | Breakpoint aggregate |
| PJ-32 | Theme/narrow screen/keyboard/a11y/state screenshots | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×4 | Breakpoint aggregate |
| PJ-33 | MiniMax/GLM/DeepSeek model fidelity 与资源观测 | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×3 | Breakpoint aggregate |
| PJ-34 | Prompt injection、cross-tenant、secret、replay、approval、delegation | [Release Gates](06-runbook-and-release-gates.md) | Frozen ×6 | Breakpoint aggregate |
| PJ-35 | three-service exact deploy、rollback 与 production double pass | [Release Gates](06-runbook-and-release-gates.md) | Frozen ×1 | Partial loop |

## 每条冻结记录必需字段

`journey_id`、persona/principal、真实入口、输入与数据版本、allowed tools/effects、成功硬判据、negative authority、fault/recovery probe、expected artifact、latency/cost measurement、evidence location、cleanup/retention。

## 最新有效证据索引

分母已冻结，但本轮尚未产生可计入 NPTCR 的 production pass。这里只登记关系，不复制证据正文：

最新 finding-level production verification 为 [`AUDIT-DEFAULT-DISCLOSURE-001`](evidence/b23e94210e7e9523bafc3b591b35db8fc2762224/AUDIT-DEFAULT-DISCLOSURE-001-production-verification.md)：它关闭 P29-PADMIN 的默认 audit business-payload disclosure，但没有完成四角色 signed-in 双遍、完整 fault、negative authority 或 cleanup，因此下表保持空值。

| Journey | Pass 1 | Pass 2 | Fault/Recovery | Negative Authority | Final Verdict |
|---|---|---|---|---|---|
| P01-MAIN～P35-RELEASE（96 条） | — | — | — | — | 0/96 Closed；NPTCR 0% |

## 状态变化规则

1. Domain 标准存在不等于旅程存在。
2. Manifest 冻结不等于执行通过。
3. 自动化绿不等于 production pass。
4. 单次 pass 不等于双遍 `Closed loop`。
5. `Closed loop` 必须链接 exact commit 下的 pass 1、pass 2、fault/recovery 和 authority-negative evidence。

---
document_id: weekend-rc-2026-08-30-journey-ledger
owner: Example Owner / Codex
status: active
authority: canonical-human-journey-ledger
last_reviewed: 2026-09-09
source_commit: 8f7762e
verification_status: frozen-96-functional-b4-in-progress
---

# Journey Ledger

[返回索引](README.md) · [当前状态](03-current-status.md) · [Runbook](06-runbook-and-release-gates.md)

本文件记录旅程分母、主 Codex/owner 已接受的闭环状态和证据链接。Domain 文档记录验收标准；Evidence 文件记录实际结果；本文件不复制两者正文，也不从机械字段自行推导语义 verdict。

## 当前功能批次索引（B4 进行中）

本轮按功能真实消费推进，不以最终发布分数概括用户已经可以使用的局部结果；未测、失败、外部阻塞分别保留。下列索引不修改96条冻结分母或完整D/E门槛。

| 批次 | 已接受结果 | 未关闭范围 / 证据 |
|---|---|---|
| B4（09-09 01:54起） | draft命令会话绑定、五格式个人知识实际Agent消费、公司发布下线/恢复v2、新member首任务归属、显式记忆更正/退役/fresh检索、临时子Agent父消费、知识重建queued→ready、HR精确待命创建、390px标题可达、固定Reviewer A2A父消费、member Team计算/复核/报告/正式关闭、clear、内置Skill消费、rewind续接、branch创建/历史读取、Plan继任卡恢复/精确确认、Useful反馈落盘、主题/键盘局部消费；MiniMax Workflow三叶真实计算/文件/父消费、Goal暂停继续及完成、DOCX原生校验与Office下载、固定定义生命周期 | 8f7762ec三服务同源已部署，90最终archive检查通过；6c6ea30f CI整体success。新增明确失败：Plan禁止schedule仍误建trigger（已正式暂停）、compact扣留已终态失败工具、XLSX schema校验、Workflow完成组误标中断与Local通知错误deep-link；Goal预算/提示一致性未验。反馈memory held、B3 once/Local result、完整角色/成长/外部能力与故障矩阵仍未关闭。等待owner决定新增根因的Codex实施例外，不把配置/入口可用或未测项算PASS。[第四批记录](evidence/87b845dba4ae397bd4205b21e657e6efeb9fac7f/functional-batch-2026-09-09-04.md) |
| B3 | Local默认会话刷新/离开重开能读回原审批和实际pwd/marker | 本地连接器只发text无result、正式升级源unavailable；once原live权限生命周期阻塞，候选未接受。[第三批记录](evidence/aeaaacb59704ac7631da553c97d699c2cc87bdb4/functional-batch-2026-09-09-03.md) |
| B2续接 | fresh和原两条文件/HR第二轮输入、显式终态恢复、文件预览/下载、HR修订拒绝与刷新、boundary自然delivered | 原底层重启恢复根因与独立trigger故障未闭环，不迁移成最终版本PASS。[第二批记录](evidence/33f6332f663f6e648f27eb704593876c4de17053/functional-batch-2026-09-08-02.md) |

## 2026-09-08 有限功能试批次 B1（历史）

PDEC-015 的两小时试批次收束，12:16:24 前交付，不自动续作；[单一证据记录](evidence/17f073bb4f07098e55d9ef1684781dc67cfa454e/functional-batch-2026-09-08-01.md)。前段测试绑定 `17f073bb`；11:54 后生产为三服务同源 `33f6332f`，新版本只取得下列 fresh KB/文件消费证据，不迁移旧业务 PASS。**NPTCR 仍为 0/96**。

本批次新增完整按冻结协议执行 **0/96**，完整功能要求通过 **0/96**，最终 D 严格闭环 **0/96**；另有八类跨域入口及局部操作实测，具体范围见表。这三个零不抹掉局部结果，也不把入口数或工具调用数换算成完整旅程数。历史 P01 正常双遍/负向/cleanup 仍只属于其原版本。

| 涉及旅程 | 本批次实际取得的证据 | 未通过或未覆盖 |
|---|---|---|
| P10 / P30 | 个人粘贴 MD、归档排除/恢复重现；新版本 GLM fresh Session 搜索/完整读取唯一文档、正确采用修正并写读报告，预览/下载/reload 成功；最终文档已归档 | 旧 KB Session 的 delivery unknown 保留；文件 V2 输入未执行；上传被浏览器扩展阻止，完整格式/版本/角色与故障合同未完成 |
| P13 | MiniMax 真实生成 HR 草案，正式拒绝且未 provision | 修订输入未执行；确认/创建/首任务本批次未做 |
| P03 | 已保存 Session 的 context/permissions/usage 面板可用 | fresh draft context 422；其余命令及面板信息完整性未完整验证 |
| P15 / P29 / P34 | 实际 member/org_admin API fixture 员工 Agent 200、平台公司后台 403、跨 tenant Agent/跨 person 文档 404 | 员工/公司管理员 UI、完整角色 matrix、撤销与故障恢复未完成 |
| P22 / P24 | Automation 表单检查后取消；既有 Local Agent 在线，新消息返回 approval_required | 未建 once、未收 Local Agent 实际回显，不计执行或恢复成功 |
| P29 / P32 / P33 | 管理后台返回 App、折叠设置；GLM 文件首轮、MiniMax HR 首轮实际执行 | 非完整前端/模型兼容性旅程；DeepSeek 不盲重试 |

两个第二轮输入均被旧 turn_stop boundary 的 dead letter / attempt 8 / `WebTerminalBoundaryPending` 阻挡；11:47 app_rls/read-only/tenant-scoped 对账确认 `waiting_for_terminal_boundary_ack`，合并为一个共同入口 finding，不重复创建修复包。Automation once 未创建、Local Agent 仅到 approval_required；外部模型未 ready 不盲重试。其余未触及旅程保留原状态。

## 分母状态

- 当前：`Frozen`，共 **96** 条可独立计分的 production journeys。
- 机器权威：[`acceptance/weekend_production_journeys.v1.json`](../../../acceptance/weekend_production_journeys.v1.json)，freeze basis `c18b181c690fe3c4aa5366a8fd504023b0c41864`；记录 persona、entry、data version、allowed effects、acceptance、fault probes、evidence path 和 cleanup。
- 冻结后不得删除或合并失败项；owner 只能带理由标为 `Excluded`。
- 只有 unresolved product-controlled requirement 可记录 blocking fact `BLOCKED_PRECONDITION`；underlying Journey 保持 `Breakpoint` 或 `Missing`，留在分母并按未闭环计。可恢复的合成 fixture、仓库 runtime/adapter 和已验证第三方 external readiness 不得冒充该 fact，也不得用 fake、历史 PASS 或未执行状态替代。
- production release 要求全部 in-scope 冻结旅程在同一 exact commit 连续两遍 clean pass；owner 带理由明确 `Excluded` 的旅程不进入 NPTCR 分母，组级通过不能替代子旅程。

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
| PJ-08 | J4 FreeCode/Hermes real bakeoff | [Memory/Growth](domains/memory-knowledge-and-growth.md) | Frozen ×1 | `Breakpoint / IMPLEMENTATION_QUEUED`：构建 Hive/FreeCode same-envelope adapter；旧 [preflight](evidence/bf94b76a1706510daf2d11c4e98fd5051f23f28f/P08-J4-blocked-runtime-contract.md) 只保留历史事实 |
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
| PJ-24 | Local Agent pair/online/offline/approval/reconnect/revoke | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×1 | `Breakpoint / RECOVERY_QUEUED`：PDEC-008 已授权 lab login/pair/revoke，真实 bridge/provider secret 仍不可读取或轮换 |
| PJ-25 | Hook blocking/observe-only/lifecycle/recovery | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×3 | Breakpoint aggregate |
| PJ-26 | Skill trust/load/use/update/revoke | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×1 | Breakpoint aggregate |
| PJ-27 | MCP/Connector auth/use/expiry/revoke/schema change | [Automation](domains/automation-hooks-and-capabilities.md) | Frozen ×1 | Breakpoint aggregate |
| PJ-28 | Agent rail/AgentDetail employee scale and navigation | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×3 | Breakpoint |
| PJ-29 | Employee/admin/platform/operator audience split | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×4 | Breakpoint |
| PJ-30 | Artifact preview/download/version/ACL/reopen | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×4 | Breakpoint aggregate |
| PJ-31 | Async deep-link/inbox/unread/dedupe/expiry | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×1 | Breakpoint aggregate |
| PJ-32 | Theme/narrow screen/keyboard/a11y/state screenshots | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×4 | Breakpoint aggregate |
| PJ-33 | MiniMax/GLM/DeepSeek model fidelity 与资源观测 | [Frontend](domains/frontend-and-product-consumption.md) | Frozen ×3 | Breakpoint aggregate；DeepSeek 当前 `EXTERNAL_UNAVAILABLE`，P33-DEEPSEEK 保持未闭环且不伪造 success |
| PJ-34 | Prompt injection、cross-tenant、secret、replay、approval、delegation | [Release Gates](06-runbook-and-release-gates.md) | Frozen ×6 | Breakpoint aggregate |
| PJ-35 | three-service exact deploy、rollback 与 production double pass | [Release Gates](06-runbook-and-release-gates.md) | Frozen ×1 | Partial loop |

## 每条冻结记录必需字段

`journey_id`、persona/principal、真实入口、输入与数据版本、allowed tools/effects、成功硬判据、negative authority、fault/recovery probe、expected artifact、latency/cost measurement、evidence location、cleanup/retention。

## 最新有效证据索引

分母已冻结，下表只保留历史 application `17f073bb` 的支持证据，不是当前 `33f6332f` 的通过数。该历史版本的 P01 双遍、negative、十 Session 和三文件 cleanup 均已核实；冻结的 disconnect/worker restart 恢复仍未完成，NPTCR 保持 0/96。这里只登记关系，不复制证据正文：

latest exact `bf94b76a` finding verification 为 [`PLATFORM-ADMIN-WORKSPACE-AUDIENCE-001`](evidence/bf94b76a1706510daf2d11c4e98fd5051f23f28f/PLATFORM-ADMIN-WORKSPACE-AUDIENCE-001-production-verification.md) 与 [`SYSTEM-SETTING-SECRET-DISCLOSURE-001`](evidence/bf94b76a1706510daf2d11c4e98fd5051f23f28f/SYSTEM-SETTING-SECRET-DISCLOSURE-001-production-verification.md)。旧 [`P29-PADMIN-pass-1`](evidence/bf94b76a1706510daf2d11c4e98fd5051f23f28f/P29-PADMIN-pass-1.md) 绑定 manifest `d320edce…`，不能迁移为 current-manifest PASS；旧 [`BLOCKED_PRECONDITION`](evidence/bf94b76a1706510daf2d11c4e98fd5051f23f28f/P29-PADMIN-fault-pass-2-role-session-precondition.md) 文件也只保存当时缺身份的历史事实。P29 current-manifest canonical pass 1/pass 2 均未运行。

| Journey | Pass 1 | Pass 2 | Fault/Recovery | Negative Authority | Final Verdict |
|---|---|---|---|---|---|
| P01-MAIN | exact `17f073bb` fresh [`pass 1`](evidence/17f073bb4f07098e55d9ef1684781dc67cfa454e/P01-MAIN-pass-1.md) clean | exact `17f073bb` fresh [`pass 2`](evidence/17f073bb4f07098e55d9ef1684781dc67cfa454e/P01-MAIN-pass-2.md) clean | 三条terminal均attempt1自然delivered且hard reload clean；仍缺冻结合同的disconnect + worker restart零重复证明 | exact `17f073bb` fresh [`negative`](evidence/17f073bb4f07098e55d9ef1684781dc67cfa454e/P01-MAIN-negative-authority.md) clean：唯一越界写typed deny/non-retryable/无fence零效果，同run合法写读成功 | `Partial loop / FAULT_RECOVERY_PENDING`：十Session与三文件[`cleanup`](evidence/17f073bb4f07098e55d9ef1684781dc67cfa454e/P01-MAIN-cleanup.md)已核实；补真实断线/worker重启消费后才能Closed |
| P29-PADMIN | 未运行；旧 `d320edce…` pass 1 仅历史 supporting evidence | 未运行；supported-path fixture setup pending | 旧 denied-route/reload evidence retained；current-manifest expired-session/role-change 待测 | 旧 9 URL + 14 API evidence retained；current-manifest 待测 | `Partial loop`，未 Closed |
| 其余 94 条 | — | — | — | — | 未执行或仅有 finding-level evidence |
| Aggregate（历史 `17f073bb`） | 历史1/96 条有 current-manifest pass 1 | 历史1/96 条完成current-manifest signed-in双遍 | 0/96完成整条fault/recovery合同 | 历史1/96完成fresh负向 | 0/96 Closed；NPTCR 0%，P01清理完成但fault/recovery尚缺 |

## 状态变化规则

1. Domain 标准存在不等于旅程存在。
2. Manifest 冻结不等于执行通过。
3. 自动化绿不等于 production pass。
4. 单次 pass 不等于双遍 `Closed loop`。
5. `Closed loop` 必须链接 exact commit 下的 pass 1、pass 2、fault/recovery 和 authority-negative evidence。

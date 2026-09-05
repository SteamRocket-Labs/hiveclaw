---
document_id: weekend-rc-2026-08-30-north-star
owner: Example Owner
status: active
authority: canonical-release-charter
last_reviewed: 2026-09-05
source_commit: 0ce51f049e03c689a440075a5de8a7a9d99c609c
verification_status: owner-approved-usability-simplicity-and-full-scope-renewal
---

# North Star、指标与边界

[返回索引](README.md) · [Owner 决策](02-owner-decisions.md) · [当前状态](03-current-status.md)

## North Star

> 一个不懂 Agent、模型、RuntimeTask、向量索引或内部权限术语的普通员工，能够在一个清晰、可恢复的 Session 中创建并使用数字员工完成真实任务；过程中知道 Agent 正在做什么、何时需要自己决定、最终交付了什么；Agent 能持续学习，但所有知识、权限和外部动作始终可控、可追溯、可恢复。

该目标同时要求：

1. 单 Agent 能力不弱于 CCPlus / FreeCode 的完整生命周期语义，智能与自我进化质量至少达到 `hermes-agent` 内部基准。
2. Agent Memory、Personal KB、Company KB 与 Governance 各有唯一权威和真实消费路径。
3. Sub-agent、Agent Team、Dynamic Workflow、Fixed Workflow 与 A2A 保持五种不同协作语义。
4. 普通员工体验以 Codex Desktop 的内容层级、流式状态、恢复和克制表达为主；Letta Code Desktop 只补充多 Agent 导航外壳。

产品判断为：

```text
真实任务闭环 × 单 Agent 质量 × 纵向成长 × 治理安全 × 小白可消费体验
```

任一项为零，整体不得宣称符合 North Star。

### 2026-09-04 再确认的长期目标

1. **功能畅通，尤其 Agent 真正好用。** 从发现入口、创建数字员工、完成开放任务，到工具、产物、记忆和恢复均走真实链路；不能把配置齐全、测试绿或单次演示当成全量完成。
2. **架构简单，治理有度。** 复用现有权威层和平台能力；控制只保护明确的身份、数据、效果或恢复边界，不以堆 gate、编排或测试脚手架代替功能交付。简化前说明受保护性质、真实误伤、替代边界和恢复方式，不能删掉必要安全能力。
3. **所有页面对小白友好。** 以 Codex Desktop 的 Session 交互、信息层级与渐进披露作参照；默认突出任务、进展、所需决定和交付物，隐藏无意义零值和工程细节，但不隐藏失败、授权请求或可恢复下一步。覆盖全部用户可见页面，不只美化聊天页。

上述要求细化既有范围，不减少冻结的 96 条旅程。旧品牌兼容/部署门、后台建公司、管理员与成员邀请、Back to App 修复继续纳入最终候选与复验。分工和顺序见 PDEC-012 与 Runbook；历史对照产品是比较证据，不是必须复制的厂商细节。

### 2026-09-05 产品角色更新（PDEC-013）

- 平台管理员管理平台及目标公司的全部业务；公司管理员管理本公司的全部业务，两者都能分配其管理范围内的公司管理员。
- 管理权限包括员工 Agent 的私有会话、文件和知识正文；不展示明文凭据。普通业务访问不以额外 operator grant/手填理由为前置，仍使用真实身份、精确公司/资源范围和审计。
- 员工只看到自己的和公司内公开的 Agent，可管理自己的 Agent但不能删除 Agent或自授管理员权限；公开 Agent 不等于公开其他用户的私有内容。
- `operator` 是技术能力/验收标签，不是第四种产品身份。旧的 admin/private-content 否定断言由本次裁决替代，96 条 ID/数量及发布门槛不变，旧证据不迁移为新 PASS。详见 [角色合同](domains/hr-identity-and-permissions.md)。
- 公司后台提供直接返回 App 的入口，保留登录和合法公司上下文。

## 主指标

正式主指标为 **Novice Production Task Closure Rate（NPTCR，普通员工生产任务闭环率）**：

```text
NPTCR = 普通员工无需管理员、数据库手改、开发者控制台或人工补状态，
        从真实产品入口完成并可恢复、可复核、可使用的冻结关键旅程数
        / 冻结关键旅程总数
```

一条旅程只有全部成立才计成功：

- 普通用户能发现真实入口；
- tenant / user / Agent / delegation 权威正确；
- 经过生产真实执行器，不依赖 mock、孤儿函数、legacy shell 或手写 DB 结果；
- UI 能解释状态、所需决定、结果和下一动作；
- hard reload、断线、重试与恢复不丢失、不重复、不假成功；
- 最终产物能直接使用，引用、权限和审计可追溯；
- Input、Authority、Execution、Evidence、Recovery、Consumption、Acceptance 七原子完整。

Production journey manifest 已按 owner 裁决冻结为 [`acceptance/weekend_production_journeys.v1.json`](../../../acceptance/weekend_production_journeys.v1.json)。执行中不得删除、合并或改写失败旅程来提高 NPTCR。只有未解决的 Hive/product-controlled requirement 才能记录 blocking fact `BLOCKED_PRECONDITION`；underlying Journey 保持 `Breakpoint` 或 `Missing`，留在分母并阻止发布。缺少可恢复的合成身份/fixture 或仓库 runtime/adapter 不符合该 blocking fact；经独立确认的第三方不可用按 PDEC-009 进入 external readiness。范围变化只能由 owner 明确标为 `Excluded` 并保留原因。

## 五条不可平均护栏

| 护栏 | 必须证明 | 一票否决 |
|---|---|---|
| 单 Agent 质量与模型主权 | 完整授权证据、正确规划和工具使用、可用结果；平台不以 keyword/regex/counter/fallback 代写语义 | 只能靠人工代做、平台补写或删能力才通过 |
| 纵向成长与自进化 | 候选试用、failure recurrence/avoidance、知识/Skill 复用、owner 负反馈与返工实际改善 | 只生成 Memory/Skill 文件，没有后续行为收益 |
| 基准不回退 | 平台改动不把 Agent 改笨；以真实 CLI、工作区和外部判据跑 FreeCode / Hermes bakeoff | 未真跑却宣称“不弱于/超过” |
| 治理安全 | 零跨 tenant 泄漏、零未授权效果、零 approval bypass、零 delegation 权限扩张 | 任一越权或不可恢复权限漂移 |
| 体验、资源与模型忠实度 | TTFT、总耗时、重连收敛、重复/闪烁、介入、token/cost/cache 可观测；不静默换模型或删能力 | 无测量、无限等待或高成本掩盖缺陷 |

Evidence Coverage Score 只衡量发布证据覆盖，不是统计置信度：源码/live wiring 15、自动化/真 PG 20、signed-in 生产双遍 30、故障恢复 20、权限负向 15。达到 95 仍不能覆盖七原子缺失、护栏失败、开放 P0/P1/用户可见 P2、三服务版本漂移、真实路径依赖 fake 或普通用户必须使用 raw ID/控制台。

## Model Agency 与 RLS 尺度

Hive 产品 turn 内，selected runtime LLM 在已认证权限框架中拥有任务推理、语义判断、综合与回答表达；RC 验收循环内，主 Codex 拥有验收分解、证据解释、优先级、Journey/Finding verdict 和最终交付表达；owner 拥有产品语义与风险授权裁决。zCode、Kimi 与 CC 可以独立分析、提出异议、发现遗漏和给出审查结论，但不能自授生产效果或最终验收权威。NPTCR、Evidence Coverage、CI、测试、receipt、timeout、attempt count、deployment/health 与 `mechanical_ready` 只校验 exact facts 或聚合已接受 verdict，不能机械裁决语义、改写/压制模型输出或删除无关能力。exact invariant 缺失时，机械门只 hold 对应 read/effect/release，并返回可恢复的 typed observation。

RLS/ACL 在 server-derived tenant、principal、Agent、Session、resource、delegation、action 与 approval 的 data-ingress/read/write/effect 边界 fail closed；未授权字节不得进入模型 context、API response 或 UI。已登记的 read-only cross-tenant existence/deny probe 仍在验收范围内，但只能得到不泄漏存在性的 `deny/not-found`，不得读取 protected row 或产生效果；若 probe 意外返回 protected bytes，立即停止该 lane，不继续读取、传播或把 raw bytes 写入 evidence，只保留最小脱敏 P0 事故证据。一个 denial 只阻断该次 read/effect，不能裁剪已授权证据、中断无关推理或移除获准工具。platform/operator 路径必须先有应用层 authority，再使用 exact tenant/reason/scope、审计和恢复明确的窄 bypass；数据库 bypass 不能创造业务授权，owner 指令也不能把未授权访问变成授权。

## 证明顺序

1. 先证明 Agent 智能：单 Agent 在真实 Session 中完成开放任务，语义质量与自进化不弱于 FreeCode/Hermes 基准；机械规则不得替代、改写或压低模型智能。
2. 再完成全部前后端功能主路径与功能性恢复：Session/20 commands、Memory/Growth、Personal/Company KB、Agent/HR 创建、Subagent、Team、Workflow、A2A、Automation、Hook/Skill/MCP/Local Agent、Artifact 与所有普通员工/后台 UI 消费。功能不存在、未接线、前后端不一致或小白不可用，先修到真实可用。
3. 所有功能性路径成立后，再集中验收并修复 employee/company-admin/platform-admin 三种角色及技术 inspector 权限、RLS/ACL、active revocation、secret/PII、prompt injection、replay、approval 与 delegation escalation；权限断言以 PDEC-013 为准。
4. 最后冻结 coherent `D`，完成三服务 exact deploy、同提交双遍、故障/权限负向、evidence 与 cleanup。

“功能优先、安全后验收”是排查与修复顺序，不是关闭现有安全控制：未授权数据/效果始终 fail closed，真实泄漏立即停该 lane；但不得用权限加固、额外 RLS 拒绝或安全评分提前阻断无关功能补全，也不得用“更安全”掩盖产品不能用。复杂控制面或更严格权限围绕一个更弱、不可用的 Agent，都不计产品成功。

## Included

- CCPlus 单 Agent 全生命周期：definition、context、model/tool loop、Hooks、Skill/MCP、Plan、Ledger、compaction、stop、resume、delivery。
- J1–J4：候选试用、纵向成长、平台改动不回退、FreeCode/Hermes 真实 bakeoff。
- HR Agent 创建数字员工、普通员工使用、Agent→HR 受治理 handoff、首个任务。
- Agent Memory、Personal KB、Company KB 的进入、解析、索引、检索、引用、权限、恢复与 UI 消费。
- Sub-agent、Agent Team、Dynamic/Fixed Workflow、A2A。
- once/schedule/bounded loop、事件 trigger、Channel、Local Agent、Notification/Approval。
- Hook、Skill、MCP、Connector 的 Trust Review、激活、使用、更新、撤销和凭据失效。
- owner/sponsor/manager/participant、转移、停用/offboarding、进行中权威撤销和数据保留边界。
- Session 流式、呈现、回退、分叉、恢复、权限和约定的 20 条斜杠命令。
- Artifact preview/download/version/citation/ACL/archive/reopen；UI 宣传格式逐一验证。
- 员工、公司管理员、平台管理员及技术 inspector 的前后端权限和信息边界；inspector 不增加第四种产品角色。
- 所有用户可见 surface 的空、加载、成功、失败、需决定、恢复、双主题、窄屏、键盘和小白表达审计。
- selected-model fidelity、provider typed states、token/cost/cache/latency、人工介入和安全对抗验收。
- 与上述旅程直接相关的 migration/backfill、tests、i18n、observability、cleanup 和 rollback。

## Excluded 或 action-time 单独授权

- Agent Sandbox provider 重构、Extension/plugin convergence、Knowledge Graph/Ontology、新 Office/多模态引擎、新 Connector 集成，以及没有真实用户旅程失败证据的全站视觉重写。
- 充值、替换/轮换/暴露真实模型或 bridge credential、读取组织 secret、邀请真实外部成员；已登记 lab Local Agent 的受支持 login/pair/revoke 生命周期属于 PDEC-008。
- 不可逆生产 DDL/迁移、删除真实生产数据或历史证据；additive/backward-compatible migration 仅在完整 migration test、backfill、rollout safety、幂等 retry 和 rollback/forward recovery 下属于已授权实现工作。
- Letta 的 Memory、Secrets、Working directory、Connect Models 信息架构；截图只授权多 Agent rail → Agent sidebar → Session 的布局参考。

最初 12 小时窗口只保留为计划背景，不是 Goal-wide terminal condition、语义 verdict 或降低 Release Gate 的理由；不得设置人工 Goal-wide timeout、step cap 或 attempt cap。每次 provider/tool/subagent 调用仍使用 task-sized timeout、cancel、quota 与 backoff 保护资源，但 expiry 只终止或恢复当前 attempt。仓库内 semantic runner/adapter 缺失是必须实现的 product-controlled gap；经独立确认且不由 Hive 造成的第三方不可用进入 external readiness，park 该 provider-success assertion 并继续其他安全路径。两者都不得用 deterministic green、mock、历史 Session 或平台补写语义代替。

## 完成状态

Journey completion state 只使用：`Closed loop`、`Partial loop`、`Breakpoint`、`Missing`、`Excluded`。`BLOCKED_PRECONDITION` 与 `EXTERNAL_UNAVAILABLE` 分别是独立的 blocking/readiness fact，不是 completion state；前者只绑定未解决的 product-controlled requirement 并阻止发布，后者按 PDEC-009 处理，二者都不产生 PASS/Closed。若冻结旅程要求真实 provider success，它在 provider 恢复或 owner 明确 `Excluded` 前保持未闭环。测试通过、部署成功、单次生产成功分别是证据层，不是额外完成状态。

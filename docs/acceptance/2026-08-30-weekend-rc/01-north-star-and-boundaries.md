---
document_id: weekend-rc-2026-08-30-north-star
owner: Rocky
status: active
authority: canonical-release-charter
last_reviewed: 2026-08-30
source_commit: 45340a3a
verification_status: owner-review-required-for-pending-decisions
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

## 主指标

主指标候选为 **Novice Production Task Closure Rate（NPTCR，普通员工生产任务闭环率）**：

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

进入执行窗前必须冻结 production journey manifest。执行中不得删除、合并或改写失败旅程来提高 NPTCR；范围变化只能由 owner 明确标为 `Excluded` 并保留原因。

## 五条不可平均护栏

| 护栏 | 必须证明 | 一票否决 |
|---|---|---|
| 单 Agent 质量与模型主权 | 完整授权证据、正确规划和工具使用、可用结果；平台不以 keyword/regex/counter/fallback 代写语义 | 只能靠人工代做、平台补写或删能力才通过 |
| 纵向成长与自进化 | 候选试用、failure recurrence/avoidance、知识/Skill 复用、owner 负反馈与返工实际改善 | 只生成 Memory/Skill 文件，没有后续行为收益 |
| 基准不回退 | 平台改动不把 Agent 改笨；以真实 CLI、工作区和外部判据跑 FreeCode / Hermes bakeoff | 未真跑却宣称“不弱于/超过” |
| 治理安全 | 零跨 tenant 泄漏、零未授权效果、零 approval bypass、零 delegation 权限扩张 | 任一越权或不可恢复权限漂移 |
| 体验、资源与模型忠实度 | TTFT、总耗时、重连收敛、重复/闪烁、介入、token/cost/cache 可观测；不静默换模型或删能力 | 无测量、无限等待或高成本掩盖缺陷 |

Evidence Coverage Score 只衡量发布证据覆盖，不是统计置信度：源码/live wiring 15、自动化/真 PG 20、signed-in 生产双遍 30、故障恢复 20、权限负向 15。达到 95 仍不能覆盖七原子缺失、护栏失败、开放 P0/P1/用户可见 P2、三服务版本漂移、真实路径依赖 fake 或普通用户必须使用 raw ID/控制台。

## 证明顺序

1. 先证明单 Agent 在真实 Session 中完成开放任务且不弱于基准。
2. 再证明 Memory / Skill / Soul 的进化在后续真实任务中产生收益。
3. 再证明 HR、知识、权限和员工生命周期。
4. 最后扩展 Sub-agent、Team、Workflow、A2A、Automation 与控制中台。

复杂控制面围绕一个更弱的 Agent，不计产品成功。

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
- 员工、公司管理员、平台管理员、operator 的前后端权限和信息边界。
- 所有用户可见 surface 的空、加载、成功、失败、需决定、恢复、双主题、窄屏、键盘和小白表达审计。
- selected-model fidelity、provider typed states、token/cost/cache/latency、人工介入和安全对抗验收。
- 与上述旅程直接相关的 migration/backfill、tests、i18n、observability、cleanup 和 rollback。

## Excluded 或 action-time 单独授权

- Agent Sandbox provider 重构、Extension/plugin convergence、Knowledge Graph/Ontology、新 Office/多模态引擎、新 Connector 集成，以及没有真实用户旅程失败证据的全站视觉重写。
- 充值、替换模型凭据、重新登录 Hive Connect、签发或替换 bridge credential、邀请外部成员。
- 生产 DDL、不可逆迁移、删除生产数据或历史证据。
- Letta 的 Memory、Secrets、Working directory、Connect Models 信息架构；截图只授权多 Agent rail → Agent sidebar → Session 的布局参考。

12 小时是执行预算，不是降低 Release Gate 的理由。真实 semantic runner 不可用时，相关旅程保持 `BLOCKED_PRECONDITION`，不得用 deterministic green、mock、历史 Session 或平台补写语义代替。

## 完成状态

只使用：`Closed loop`、`Partial loop`、`Breakpoint`、`Missing`、`Excluded`、`BLOCKED_PRECONDITION`。测试通过、部署成功、单次生产成功分别是证据层，不是额外完成状态。

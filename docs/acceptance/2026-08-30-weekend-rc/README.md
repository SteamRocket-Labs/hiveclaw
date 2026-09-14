---
document_id: weekend-rc-2026-08-30-index
owner: Example Owner / Codex
status: active
authority: canonical-index
last_reviewed: 2026-09-08
source_commit: 33f6332f663f6e648f27eb704593876c4de17053
verification_status: bounded-functional-pilot-closed-partial-results
---

# Weekend RC 验收文档入口

本目录按事实类型分工。当前完成标准由 [PDEC-016](02-owner-decisions.md#2026-09-09-最新裁决pdec-016) 定义；八步结果、最近记录的发布与剩余条件只在 [当前状态](03-current-status.md) 维护。本页不复制完成数、部署状态或待办。

公开文档中的运行标识和本机路径已脱敏，原始回执由维护者受限保存。重放需取得授权及原始定位信息，详见[公开文档与受限证据](../../README.md#公开文档与受限证据)。

## 阅读顺序

1. 从本页选择主题；继续执行前先读当前状态和对应裁决。
2. 查验收定义时读领域文档，查问题时读 Findings，查实际结果时读 Evidence。
3. 历史批次、旧分工与已被替代的门槛仅供追溯，不产生新的工作授权。

自动恢复入口固定为 `03-current-status.md`。更新进度只写该文件；定义、裁决、问题和运行证据分别写入下表对应文件。

## 权威地图

| 文件 | 唯一职责 | 更新节奏 |
|---|---|---|
| [01-north-star-and-boundaries.md](01-north-star-and-boundaries.md) | North Star、主指标、护栏、范围和产品边界 | 低频；产品裁决变化时 |
| [02-owner-decisions.md](02-owner-decisions.md) | owner 明确接受、拒绝或待决的决定 | 每次 owner 裁决后 |
| [03-current-status.md](03-current-status.md) | 当前提交、生产基线、阻塞、下一动作、Not Done | 每个证据批次或计划步骤后 |
| [04-journey-ledger.md](04-journey-ledger.md) | 验收分母、旅程状态、领域与最新证据关系 | 旅程冻结或状态变化时 |
| [05-findings.md](05-findings.md) | 当前有效 finding、严重度、最早错误状态和修复闭环 | 复现、修复、复验时 |
| [06-runbook-and-release-gates.md](06-runbook-and-release-gates.md) | 执行顺序、测试、部署、停止与发布门 | 执行合同变化时 |
| [domains/](domains/single-agent-and-session.md) | 各能力的验收标准；不记录运行结果 | 产品契约变化时 |
| [evidence/](evidence/README.md) | exact commit / persona / run 的不可变证据记录 | 每次实际运行后新增 |
| [archive/](archive/README.md) | 旧总账、历史推理和过时状态 | 只归档，不作为当前权威 |

## 领域清单

- [Single Agent 与 Session](domains/single-agent-and-session.md)
- [Memory、Knowledge 与 Growth](domains/memory-knowledge-and-growth.md)
- [HR、身份与权限](domains/hr-identity-and-permissions.md)
- [协作、Workflow 与 A2A](domains/collaboration-workflow-and-a2a.md)
- [Automation、Hook 与外部能力](domains/automation-hooks-and-capabilities.md)
- [前端与产品消费](domains/frontend-and-product-consumption.md)

相关审计：[CC+ 产品目标、系统差距与代码精简审计（2026-09-05）](../../ccplus-product-and-code-audit-2026-09-05.md)。该文是评估快照与建议，不替代本组目标、状态、Findings 或发布 verdict。

## 证据与执行边界

- 结构检查只验证文件、ID、链接、字段和引用完整性，不判断语义质量。
- 实际结果需要关联其环境、源码提交、输入、执行与消费证据；测试、部署和业务验收分别记录。
- GitHub Issue 只是执行信息的投影；label、comment、assignee、open/closed 都不是 Journey/Finding verdict。
- worker/reviewer 的独立判断是审查意见，不直接改写 Journey verdict。当前执行角色及授权以 PDEC-016 为准。
- 公共文档保存结论、限制和必要证据引用；私人对话、凭据定位及原始日志由维护者受限保留。
- 单一主题保留一个主定义；其他文件以链接引用。纠正证据时保留原结论的适用范围，不把旧通过迁移到新版本。

## 历史机器基线

以下内容描述旧 96 条合同下的记录，**不构成当前八步任务的完成门槛**。

[`acceptance/atomic_user_journeys.v1.json`](../../../acceptance/atomic_user_journeys.v1.json) 是 CI 的 15 条确定性旅程，允许已声明的受控外部 fake。历史生产清单 [`acceptance/weekend_production_journeys.v1.json`](../../../acceptance/weekend_production_journeys.v1.json) 由 35 个候选组展开为 96 条旅程，禁止 external fake；具体结果保留在旅程账本和原始证据中。

当前 manifest 的 `P01-MAIN` 已有两次clean signed-in pass，并在application `17f073bb`完成fresh authority-negative及十条Session/三个文件的正式cleanup；仍须补冻结合同要求的disconnect与worker restart零重复恢复证明，不能以hard reload或清理代替。因此NPTCR仍为0/96。此前application与旧manifest证据只作historical supporting。[`backend/scripts/weekend_rc_gate.py`](../../../backend/scripts/weekend_rc_gate.py)只计算机械分数，固定输出`semantic_verdict=not_computed_by_tool`。

历史分工变更：PDEC-015 替代 PDEC-012/PDEC-014 的日常协作门；随后 PDEC-016 确定单一 Codex 与八步出口。完整理由与权限范围见裁决文件，不在索引重复。

历史机器清单可用下列命令检查；机械评分不产生当前产品验收结论：

```bash
python3 backend/scripts/weekend_rc_gate.py validate
python3 backend/scripts/weekend_rc_gate.py score --deployed-commit <40-char-application-sha>
```

## 上位文档

本目录的产品目标与安全边界遵循仓库 `AGENTS.md`、产品总目标和 CCPlus 边界契约；设计与运行入口见[文档首页](../../README.md)。

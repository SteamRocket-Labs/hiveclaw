# HiveClaw 文档

初次了解项目，先读[项目首页](../README.zh-CN.md)。这里按你要做的事整理入口，不重复记录版本更新或验收进度。

## 使用、开发与部署

| 你要做的事 | 推荐入口 |
| --- | --- |
| 本地启动，创建第一位员工 | [快速开始](../README.zh-CN.md#开始使用) |
| 理解代码，运行开发检查 | [工程指南](../ENGINEERING.md) |
| 操作 Railway 生产环境 | [生产运行手册](railway-production-runbook.md) |
| 排查 Workflow 运行问题 | [Workflow 运维手册](workflow-ops-runbook.md) |
| 查看产品变化 | [更新日志](../CHANGELOG.md) |
| 核对验证结果、限制和待办 | [验收入口](acceptance/2026-08-30-weekend-rc/README.md) |
| 使用编码 Agent 修改仓库 | [Agent 工作约定](../AGENTS.md) |

## 产品与架构

下面是设计入口，不是功能完成清单。阅读某个模块时，从对应主文档进入，再按其中的链接查细节。

| 主题 | 文档 |
| --- | --- |
| 产品目标与能力边界 | [产品总目标](hive-sota-master-goal.md) · [CCPlus 边界契约](ccplus-north-star-contract-2026-06-24.md) |
| 模型能力与平台约束 | [Model Agency 边界](runtime-model-agency-constraint-audit-2026-07-13.md) · [Harness 审计](harness-engineering-audit-2026-06-11.md) |
| 会话、事件与上下文 | [Session V2 契约](session-v2-cc-codex-alignment-contract-2026-07-14.md) · [上下文与渐进披露](unified-context-assembly-and-progressive-disclosure-2026-07-14.md) |
| 会话界面与交付物 | [前端设计](frontend-design-refinement-2026-07-03.md) · [Session UX](ccplus-session-ux-contract-2026-06-26.md) · [Timeline 投影](session-timeline-projection-contract-2026-07-04.md) |
| 记忆与自我改进 | [记忆流程图](memory-system-flow-map-2026-06-17.md) · [记忆路径契约](memory-vault-path-contract-2026-06-23.md) · [自我进化方案](self-evolution-sota-plan.md) |
| 个人与公司知识 | [知识架构](knowledge-substrate-plugin-architecture-2026-07-09.md) · [个人知识库](personal-knowledge-base-spec.md) · [公司知识库](company-knowledge-base-spec-2026-07-07.md) |
| 知识访问与授权 | [Tool-first 边界](personal-company-knowledge-tool-boundary-2026-07-10.md) · [权限契约](agent-permission-governance-spec-2026-07-07.md) |
| Skill、MCP 与扩展 | [扩展能力](agent-extension-surface-skill-mcp.md) · [Skills 与 Packs](SKILLS_AND_PACKS_V2.md) |
| 计划与工作记录 | [Plan Mode](plan-mode-design.md) · [任务记录](agent-task-cognitive-scaffold.md) |
| 子任务与员工协作 | [Subagent](subagent-source-capability.md) · [A2A Session](a2a-session-substrate-design-2026-06-24.md) |
| Workflow | [执行能力](workflow-source-capability.md) · [Dynamic Workflow](dynamic-workflow-ccplus-implementation-plan-2026-06-27.md) · [A2A Process Graph](a2a-workflow-orchestration-design-2026-06-24.md) |
| 自动化与触发器 | [Trigger 设计](trigger-cc-alignment.md) · [执行模式选择](execution-mode-spectrum.md) |
| 本地 Agent 连接 | [Hive Bridge 方案](hive-bridge-cc-connect-fork-plan-2026-06-24.md) |
| 文档与网页处理 | [文档转换](document-conversion-multimodal-design.md) · [网页数据来源](web-data-source-layer-plan-2026-06-20.md) |
| 隔离与评估 | [RLS 迁移](rls-enforcement-migration-plan.md) · [外部行为评估](external-behavior-eval-ci.md) |

## 验收与历史依据

验收状态只沿[验收入口](acceptance/2026-08-30-weekend-rc/README.md)查询。它指向状态、用户旅程、问题记录和具体证据；本页不再抄写通过数、未完成数或部署结果。

- [统一原子审查报告](agent-native-unified-atomic-review-2026-07-14.md)：保留问题、责任分组和修复上下文。涉及对应分组时，按报告里的原始证据与参考文档继续追踪。
- [可复用审查提示词](reusable-agent-native-atomic-review-prompt.md)：用于按相同边界复核实现与证据。
- [历史文档归档](archive/legacy-docs/README.md)：保留旧方案和当时的判断，供追溯使用。

报告中引用的仓库内文档应随 Git 保存，确保新检出和 CI 可以读取。旧审计的结论只说明当时的情况，不能覆盖当前源码、配置或运行证据。

## 公开文档与受限证据

本仓库公开。生产项目、服务、部署及用户/租户/会话标识，真实姓名和联系方式，本机路径、账单与客户明细不写入公开文档。公开运行记录使用匿名标识和变量路径；`disclosure: public-redacted` 或“公开副本”说明表示已脱敏，不是可直接执行的生产定位信息，也不表示重新验收。

完整原始回执、标识对应关系和运维配置由维护者在受限存储中保留。公开副本保留源码提交、故障机制、验证结论及未证明范围；需要重放时先取得授权并读取受限原件，不能把匿名 ID 当作真实目标。隐私清理不改变通过、失败或未验证结论。

Agent 运行状态、会话日志、个人记忆、临时报告和私人附件留在本地，不纳入 Git。发布前检查正文、图片/附件及其引用目标；普通脱敏提交和删除分支不会自动清除旧历史或他人副本。

## 文档放在哪里？

- **项目首页**：项目是什么、适合做什么、如何上手；英文和中文保持一致。
- **工程指南与专题文档**：实现结构、稳定约定、操作方法和设计理由。
- **CHANGELOG**：面向使用者的更新摘要，只维护这一份，不另建中英文副本。
- **验收与运行记录**：测试范围、环境、提交和部署证据。必要的版本号或提交号属于证据定位，不是第二份更新日志。

修订一份指南时，直接更新相关说明，不在文末连续追加“本轮完成”“已推送”等进度播报。历史证据保留原路径，不为整理首页而删除或改写。文件名里的日期用于识别设计或审计背景，不代表它是最新实现状态。

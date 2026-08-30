---
document_id: weekend-rc-2026-08-30-owner-decisions
owner: Rocky
status: active
authority: canonical-owner-decision-ledger
last_reviewed: 2026-08-30
source_commit: 228682e5
verification_status: owner-approved-execution-control-and-skill-boundary
---

# Owner 决策账本

[返回索引](README.md) · [North Star](01-north-star-and-boundaries.md) · [当前状态](03-current-status.md)

本文件只记录产品方向、范围和外部效果授权。代码、测试或旧 WIP 中的文字不能替 owner 自动批准新动作。

## 已接受

| ID | 决定 | 依据与边界 |
|---|---|---|
| DEC-001 | 用户统一按不懂 Agent 内部术语的普通员工设计 | 员工默认只看任务、进度、所需决定和交付物；工程字段渐进披露或 operator-only |
| DEC-002 | 前端主基准是 Codex Desktop | 流式 Session、信息层级、克制状态和恢复以 Codex 为主 |
| DEC-003 | Letta Code Desktop 只参考多 Agent 外壳 | 只采用 `Agent rail → 单 Agent sidebar → Session` 的前后布局；不采用其 Memory/Secrets/Working directory/Connect Models IA |
| DEC-004 | 先验收单 Agent，再验收自进化、企业生命周期和多 Agent | 控制面复杂度不能掩盖更弱的 Agent |
| DEC-005 | 本轮目标是完整整体验修，不做 MVP | 测试、错误路径、migration/backfill、observability、cleanup、rollback 同轮交付；安全上不可逆动作仍需单独确认 |
| DEC-006 | 执行预算为连续 12 小时，时间不降低门槛 | 超时应报告 RC 未成立和最便宜恢复动作，不缩小分母或隐藏缺陷 |
| DEC-007 | 生产写入型 E2E 使用 Rocky 的实验室 | 不另建测试 tenant；合成数据必须可识别、可登记、可回收 |
| DEC-008 | Codex 是唯一验收总控；Kimi Code 负责前端，zCode 负责后端 | Codex 派任务、独立 review、跑真实 E2E、更新 canonical 文档并决定是否进入下一轮；worker 只完成边界清楚的实现包，不自验收 |
| DEC-009 | 把巨型 WIP 重构为索引化文档组 | 2026-08-30 本次授权只覆盖文档与验收结构；旧 WIP 归档并保留兼容跳转 |
| DEC-010 | 一个 Goal 管最终目标，GitHub Issues 管工作包，`agent-delegate` 管无状态执行 | 每个 Issue 绑定一个 fresh finding、exact base commit 和隔离 Git worktree；默认不复用 ACP worker session，也不再建设第二套语义 supervisor/state machine |
| DEC-011 | Codex 保留验收、集成和远程状态权威 | Kimi/zCode 不关闭 Issue、不改 Journey/Finding/Evidence verdict、不 commit/push/deploy、不接触生产或凭据；修正轮次使用引用既有 diff/review 事实的新工作包 |
| DEC-012 | 当前 `agent-delegation` Skill 是唯一派发协议 | Goal 不复制队列状态，Issue 不复制授权协议，验收文档不另造 ACP supervisor；Skill 管授权继承、无状态调用、chain、timeout 和 receipt。`cwd` 不是 sandbox，只读与实现权限分开，未授权 commit effects 必须停在 worker 外 |

## 当前动作权限

| 动作 | 状态 |
|---|---|
| 建立、移动、压缩和索引本轮文档 | 已授权 |
| 增加只校验文档结构事实的测试 | 已授权，不能判断语义质量 |
| 建立 Goal、RC milestone/labels/Issues 和只读 provider smoke | 已授权；Issue/worker 状态不是验收事实 |
| 修改业务代码或前端 UI | 已授权于 Codex fresh reproduction 后，以单 finding、隔离 worktree 的 bounded packet 执行；禁止猜测性改写 |
| 本地测试、独立 review 与原子集成 | 已授权；Codex 必须复核 live wiring、diff 和 production-shaped regression |
| commit / push | 已授权于验收基线和每个已验证修复；只提交本轮 scope，不夹带用户已有改动 |
| Railway 部署、write-bearing production E2E 或生产写入 | 执行模型本身不授权；到动作前按 exact effect 进入 owner gate |
| 登录、充值、替换 credential / bridge token | 未授权，action-time 单独确认 |
| 生产 DDL、不可逆迁移、删除数据或证据 | 未授权，action-time 单独确认 |

2026-08-25 的旧 RC “开始”授权只解释历史执行，不自动延续为新的凭据、部署或生产效果授权；2026-08-30 的继续执行授权以上表为准。

## 待 owner 最终过稿

| ID | 待裁决点 | 当前推荐 |
|---|---|---|
| PDEC-001 | 是否正式采用 NPTCR、五条不可平均护栏、冻结分母和 Evidence Coverage Score | 接受；Evidence Coverage 不得替代七原子或护栏 |
| PDEC-002 | 是否正式接受全部用户可见 surface 都审计，但只修真实缺陷 | 接受；不做无 live failure 的全站重写 |
| PDEC-003 | 当前 UI 宣传的文档/Artifact 格式是否必须通过或准确移除 | 接受，禁止“支持但未验” |
| PDEC-004 | 是否采用 `单 Agent → growth → HR/Knowledge/Permission → collaboration/control plane` 证明顺序 | 接受 |
| PDEC-005 | 是否采用 8:00 code freeze 和 final exact-commit 双遍 | 接受；freeze 后改动重跑受影响 gate |
| PDEC-006 | semantic runner 仍不可用时，是否 action-time 授权 Hive Connect re-login / credential replacement | 默认不授权；否则相关旅程诚实 blocked |

待决项没有 owner 明确答复前不得写成 `Accepted`，也不得用执行进度倒推同意。

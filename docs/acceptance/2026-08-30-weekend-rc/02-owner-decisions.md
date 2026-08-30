---
document_id: weekend-rc-2026-08-30-owner-decisions
owner: Rocky
status: active
authority: canonical-owner-decision-ledger
last_reviewed: 2026-08-30
source_commit: c18b181c
verification_status: owner-approved-complete-execution-and-delivery-contract
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

## 2026-08-30 已接受执行裁决

| ID | 已接受决定 | 精确边界 |
|---|---|---|
| PDEC-001 | 正式采用 NPTCR、冻结分母、五条不可平均护栏和 Evidence Coverage Score | `NPTCR = Closed / (Frozen - owner 明确 Excluded)`；`BLOCKED_PRECONDITION` 留在分母并阻止发布；Evidence Coverage 不能替代七原子或护栏 |
| PDEC-002 | 审计全部用户可见 surface，但只修改 fresh reproduction 的真实缺陷 | Session/整体表达以 Codex Desktop 为主；Letta 只参考 `Agent rail → Agent sidebar → Session`；没有 live failure 的页面以 PASS 证据收口，不做猜测性重写 |
| PDEC-003 | UI 宣传的每种 Knowledge/Artifact 格式必须通过或准确移除 | 默认修复完整 `upload → parse → index → search/cite → preview/download → authority → recovery`；只有现有产品合同明确排除时才由 Codex 记录并移除宣传 |
| PDEC-004 | 采用 `单 Agent → Growth → HR/Knowledge/Permission → Collaboration/Control Plane` 发布顺序 | 调查和互不重叠的实现可并行，但前一 Gate 未通过时后一层不能升级为 Closed 或用于掩盖基础能力失败 |
| PDEC-005 | 采用相对执行窗 `T+8:00` RC code freeze 和 final exact-commit 双遍 | 冻结候选记为应用提交 `D`；其后任何 runtime/code/config/schema 变化使 `D` 作废并生成新候选，重跑相关门、三服务部署和完整双遍；docs-only 证据提交 `E` 不使 `D` 作废 |
| PDEC-006 | 采用真实语义 Runner 与外部前置条件分流合同 | Runner 是 Hive 生产 Agent 经真实 selected model/provider 的执行链，不是 Kimi/zCode；健康 provider 上的 Hive failure 是 Finding，余额/auth/rate-limit/offline 是 `BLOCKED_PRECONDITION`；Hive Connect 只阻塞 Local Agent 旅程；re-login、credential replacement 或充值仍需 action-time owner 授权，恢复后从旅程起点重跑 |

## 当前动作权限

| 动作 | 状态 |
|---|---|
| 建立、移动、压缩和索引本轮文档 | 已授权 |
| 增加只校验文档结构事实的测试 | 已授权，不能判断语义质量 |
| 建立 Goal、RC milestone/labels/Issues 和只读 provider smoke | 已授权；Issue/worker 状态不是验收事实 |
| 修改业务代码或前端 UI | 已授权于 Codex fresh reproduction 后，以单 finding、隔离 worktree 的 bounded packet 执行；禁止猜测性改写 |
| 本地测试、独立 review 与原子集成 | 已授权；Codex 必须复核 live wiring、diff 和 production-shaped regression |
| commit / push | 已授权于验收基线和每个已验证修复；只提交本轮 scope，不夹带用户已有改动 |
| Railway 部署 | owner 已授权最终冻结应用提交 `D` 同时部署 `backend`、`backend-api`、`frontend`；本授权不覆盖凭据、计费、DDL、不可逆数据效果或非冻结提交 |
| write-bearing production E2E | 已授权仅在 Rocky 实验账号/tenant 内按冻结 manifest 创建可识别、可登记、可回收的合成资产；必须先登记 cleanup，禁止真实客户数据和未列外部发送 |
| 登录、充值、替换 credential / bridge token | 未授权，action-time 单独确认 |
| 生产 DDL、不可逆迁移、删除数据或证据 | 未授权，action-time 单独确认 |

2026-08-25 的旧 RC “开始”授权只解释历史执行，不自动延续为新的凭据或不可逆生产效果授权；2026-08-30 owner 的“按照你的建议来”正式接受 PDEC-001～PDEC-006 和上述最终部署边界。任何后续范围扩大仍需新的明确裁决。

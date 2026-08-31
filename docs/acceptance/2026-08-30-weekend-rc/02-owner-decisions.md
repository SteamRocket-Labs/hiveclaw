---
document_id: weekend-rc-2026-08-30-owner-decisions
owner: Example Owner
status: active
authority: canonical-owner-decision-ledger
last_reviewed: 2026-08-31
source_commit: bf94b76a1706510daf2d11c4e98fd5051f23f28f
verification_status: owner-approved-model-agency-rls-and-external-readiness-contract
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
| DEC-007 | 生产写入型 E2E 使用 Example Owner 的实验室 | 不另建测试 tenant；合成数据必须可识别、可登记、可回收 |
| DEC-008 | Codex 是唯一验收总控；Kimi Code 负责前端，zCode 负责后端 | Codex 派任务、独立 review、跑真实 E2E、更新 canonical 文档并决定是否进入下一轮；worker 只完成边界清楚的实现包，不自验收 |
| DEC-009 | 把巨型 WIP 重构为索引化文档组 | 2026-08-30 本次授权只覆盖文档与验收结构；旧 WIP 归档并保留兼容跳转 |
| DEC-010 | 一个 Goal 管最终目标，GitHub Issues 管工作包，`agent-delegate` 管无状态执行 | 每个 Issue 绑定一个 fresh finding、exact base commit 和隔离 Git worktree；默认不复用 ACP worker session，也不再建设第二套语义 supervisor/state machine |
| DEC-011 | Codex 保留验收、集成和远程状态权威 | Kimi/zCode 不关闭 Issue、不改 Journey/Finding/Evidence verdict、不 commit/push/deploy、不接触生产或凭据；修正轮次使用引用既有 diff/review 事实的新工作包 |
| DEC-012 | 当前 `agent-delegation` Skill 是唯一派发协议 | Goal 不复制队列状态，Issue 不复制授权协议，验收文档不另造 ACP supervisor；Skill 管授权继承、无状态调用、chain、timeout 和 receipt。`cwd` 不是 sandbox，只读与实现权限分开，未授权 commit effects 必须停在 worker 外 |

DEC-006、DEC-008、DEC-010～DEC-012 保留为历史记录，已分别由 PDEC-005/PDEC-007 覆盖，不再控制本轮执行。

## 2026-08-30 已接受执行裁决

| ID | 已接受决定 | 精确边界 |
|---|---|---|
| PDEC-001 | 正式采用 NPTCR、冻结分母、五条不可平均护栏和 Evidence Coverage Score | `NPTCR = Closed / (Frozen - owner 明确 Excluded)`；Journey completion state 只用 AGENTS.md 的五态。只有 unresolved product-controlled requirement 可记录 blocking fact `BLOCKED_PRECONDITION`；underlying Journey 保持 `Breakpoint`/`Missing`、留在分母并阻止发布。external readiness、缺 fixture/runtime 不得冒充该 fact；Evidence Coverage 不能替代七原子或护栏 |
| PDEC-002 | 审计全部用户可见 surface，但只修改 fresh reproduction 的真实缺陷 | Session/整体表达以 Codex Desktop 为主；Letta 只参考 `Agent rail → Agent sidebar → Session`；没有 live failure 的页面以 PASS 证据收口，不做猜测性重写 |
| PDEC-003 | UI 宣传的每种 Knowledge/Artifact 格式必须通过或准确移除 | 默认修复完整 `upload → parse → index → search/cite → preview/download → authority → recovery`；只有现有产品合同明确排除时才由 Codex 记录并移除宣传 |
| PDEC-004 | 采用 `Agent 智能 → 全部前后端功能可用 → 权限/RLS/安全 → Release` 证明顺序 | 先补全所有功能主路径、功能性恢复和小白 UI，再集中验收角色权限、RLS 与安全对抗；既有 authority/secret/effect 边界全程保留，真实泄漏立即停 lane，但权限加固不得抢在功能补全前、阻断无关功能或掩盖产品不可用。调查和互不重叠实现可并行，主 Codex仍基于 live evidence 选择当前阶段最高杠杆的安全路径 |
| PDEC-005 | 采用 condition-based application freeze 和 final exact-commit 双遍 | 不设人工 Goal-wide timeout/step/attempt cap；形成 coherent candidate 后冻结为应用提交 `D`，其后任何 runtime/code/config/schema 变化使 `D` 作废并生成 `D2`，重跑相关门、三服务部署和完整双遍；docs/evidence-only 直接子提交 `E` 不部署、不使 `D` 作废 |
| PDEC-006 | 采用真实语义 Runner 与 external readiness 分流合同 | Runner 是 Hive 生产 Agent 经真实 selected model/provider 的执行链，不是 Kimi/zCode。可用 provider 上的 Hive wiring/config/handling failure 是 Finding；经独立确认且不由 Hive 造成的 balance/credential/upstream/rate-limit/offline，在 Hive truthful typed、审计、保留无关能力并给出恢复指导后记 `EXTERNAL_UNAVAILABLE`，不写 `BLOCKED_PRECONDITION`、不写 PASS/Closed，也不停止其他 lane。真实 credential replacement、rotation 或充值仍需 owner action-time 授权；恢复后从旅程起点重跑 |
| PDEC-007 | 主 Codex 对全栈交付负责，允许 Codex 原生 Multi-Agent 与 subagent | 主 Codex 独占 Goal、最终验收、集成、生产 E2E/A2A、部署、evidence 和交付权威；原生 subagent 可承担有边界的调查、实现、测试和 review，但不能自验收。Kimi、zCode、Coze、ACP、`agent-delegation` 等外部 Harness、第二语义控制器和 shadow ledger 仍禁用；Issue 只作审计引用，不拥有执行或 verdict |
| PDEC-008 | 主 Codex 自行建立可复用的实验 tenant 合成 fixture | 可创建/登录/切换/撤销 employee、company-admin、platform-admin、scoped-operator 身份及临时 grant、Session、Agent、KB、Workflow、Local Agent binding；只走受支持且经过认证的 UI/API/control-plane path，禁止伪造 JWT/token、直接修改 tenant/role DB 字段、关闭/放宽 RLS 或用 broad bypass 创造业务授权。生产 fixture effect 由主 Codex 执行；原生 subagent 只做 repo 调查、实现、测试和 review。缺少预存会话或 fixture 不是 owner gate，支持路径失败形成 fresh Finding |
| PDEC-009 | 产品正确性与 external readiness 分账 | Hive 缺陷、缺实现或缺接线必须修复；独立确认的第三方不可用不是 Hive defect，只 park 对应 provider-success assertion 并继续无关工作，但不能制造 PASS/Closed。冻结旅程若要求真实 provider success，在 provider 恢复或 owner 明确 `Excluded` 前保持未闭环并继续计入 NPTCR 分母 |
| PDEC-010 | 缺少仓库内 runtime/build/adapter 是实现工作 | Codex 可按 lockfile 安装既有依赖并构建 FreeCode 或最小 benchmark adapter；不得因没有预编译 CLI 而写 `BLOCKED_PRECONDITION` |
| PDEC-011 | 本 Goal 创建并登记的合成资产 cleanup 已授权 | 通过受支持、经过认证的产品路径核对 exact target/readback 后可幂等清理本 Goal 创建并登记的合成身份、grant、Session、文件和 binding，并验证无 searchable/active ghost；pre-existing fixture、不可变 evidence、owner 基础账号、真实数据、真实密码/组织 secret、计费和实际读取/披露/修改其他 tenant 受保护数据永不作为 cleanup target。已登记的 read-only deny/not-found probe 仍在 scope，且不得获取受保护字节或产生效果；若意外返回 protected bytes，立即停止该 lane，不继续读取/传播/写 raw evidence，只保留最小脱敏 P0 事故证据 |

## 当前动作权限

| 动作 | 状态 |
|---|---|
| 建立、移动、压缩和索引本轮文档 | 已授权 |
| 增加只校验文档结构事实的测试 | 已授权，不能判断语义质量 |
| 建立 Goal、RC milestone/labels/Issues 和只读 provider smoke | 已授权；Issue/worker 状态不是验收事实 |
| 修改业务代码或前端 UI | 已授权于 Codex fresh reproduction 或当前源码证明缺失实现后直接完成共享根因修复；禁止猜测性改写 |
| 本地测试、独立 review 与原子集成 | 已授权；Codex 必须复核 live wiring、diff 和 production-shaped regression |
| commit / push | 已授权于验收基线和每个已验证修复；只提交本轮 scope，不夹带用户已有改动 |
| Railway 部署 | 主 Codex 已获授权把 coherent frozen `D` 同时部署 `backend`、`backend-api`、`frontend`；不逐 commit 自动部署，本授权不覆盖凭据、计费、不可逆数据效果或非冻结提交 |
| write-bearing production E2E | 主 Codex 已获授权仅在 Example Owner 实验账号/tenant 内按冻结 manifest 创建可识别、可登记、可回收的合成资产；必须先登记 cleanup，禁止真实客户数据和未列外部发送；原生 subagent 不执行生产 effect |
| 合成登录、角色/grant、lab Session / Local Agent binding | 已授权；只走受支持认证路径，必须登记、限定 Example Owner 实验 tenant、验证正向与负向边界并 cleanup；禁止 forged identity、直接 DB role/tenant mutation、RLS weakening 和 ambient bypass |
| 充值、真实 provider credential、真实用户密码、组织 secret | 未授权，action-time 单独确认 |
| additive/backward-compatible migration | 已授权于完整 migration test、backfill、rollout safety 与 rollback/forward recovery；不可逆生产迁移或真实数据删除仍需确认 |

2026-08-25 的旧 RC “开始”授权只解释历史执行；2026-08-30 接受 PDEC-001～PDEC-006，2026-08-31 的完整 Goal 进一步接受并修订 PDEC-005～PDEC-011。最新合同禁止其他 Harness，不禁止 Codex 原生 Multi-Agent/subagent；production manifest 的 96 条分母和旅程语义不变。绑定旧 manifest hash 的 immutable evidence 继续保留为历史事实，不自动迁移为当前 final-manifest PASS。PDEC-007～PDEC-011 覆盖旧 worker、预存会话、fixture、lab login/pair 和仓库 runtime action gate，但不授权真实凭据、计费、真实外部收件人、不可逆真实数据效果或实际读取/披露/修改其他 tenant 受保护数据。

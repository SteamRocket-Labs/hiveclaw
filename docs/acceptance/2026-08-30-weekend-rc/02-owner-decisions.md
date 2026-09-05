---
document_id: weekend-rc-2026-08-30-owner-decisions
owner: Example Owner
status: active
authority: canonical-owner-decision-ledger
last_reviewed: 2026-09-05
source_commit: 0ce51f049e03c689a440075a5de8a7a9d99c609c
verification_status: owner-approved-pdec-013-role-contract-implementation-pending
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

DEC-006、DEC-008、DEC-010～DEC-012 保留为历史记录，已分别由 PDEC-005/PDEC-007 覆盖；2026-09-04 的执行分工以 PDEC-012 为准，不自动恢复历史 Issue 队列或旧时间限制。

## 2026-08-30 已接受执行裁决

| ID | 已接受决定 | 精确边界 |
|---|---|---|
| PDEC-001 | 正式采用 NPTCR、冻结分母、五条不可平均护栏和 Evidence Coverage Score | `NPTCR = Closed / (Frozen - owner 明确 Excluded)`；Journey completion state 只用 AGENTS.md 的五态。只有 unresolved product-controlled requirement 可记录 blocking fact `BLOCKED_PRECONDITION`；underlying Journey 保持 `Breakpoint`/`Missing`、留在分母并阻止发布。external readiness、缺 fixture/runtime 不得冒充该 fact；Evidence Coverage 不能替代七原子或护栏 |
| PDEC-002 | 审计全部用户可见 surface，但只修改 fresh reproduction 的真实缺陷 | Session/整体表达以 Codex Desktop 为主；Letta 只参考 `Agent rail → Agent sidebar → Session`；没有 live failure 的页面以 PASS 证据收口，不做猜测性重写 |
| PDEC-003 | UI 宣传的每种 Knowledge/Artifact 格式必须通过或准确移除 | 默认修复完整 `upload → parse → index → search/cite → preview/download → authority → recovery`；只有现有产品合同明确排除时才由 Codex 记录并移除宣传 |
| PDEC-004 | 采用 `Agent 智能 → 全部前后端功能可用 → 权限/RLS/安全 → Release` 证明顺序 | 先补全所有功能主路径、功能性恢复和小白 UI，再集中验收角色权限、RLS 与安全对抗；既有 authority/secret/effect 边界全程保留，真实泄漏立即停 lane，但权限加固不得抢在功能补全前、阻断无关功能或掩盖产品不可用。调查和互不重叠实现可并行，主 Codex仍基于 live evidence 选择当前阶段最高杠杆的安全路径 |
| PDEC-005 | 采用 condition-based application freeze 和 final exact-commit 双遍 | 不设人工 Goal-wide timeout/step/attempt cap；形成 coherent candidate 后冻结为应用提交 `D`，其后任何 runtime/code/config/schema 变化使 `D` 作废并生成 `D2`，重跑相关门、三服务部署和完整双遍；docs/evidence-only 直接子提交 `E` 不部署、不使 `D` 作废 |
| PDEC-006 | 采用真实语义 Runner 与 external readiness 分流合同 | Runner 是 Hive 生产 Agent 经真实 selected model/provider 的执行链，不是 Kimi/zCode。可用 provider 上的 Hive wiring/config/handling failure 是 Finding；经独立确认且不由 Hive 造成的 balance/credential/upstream/rate-limit/offline，在 Hive truthful typed、审计、保留无关能力并给出恢复指导后记 `EXTERNAL_UNAVAILABLE`，不写 `BLOCKED_PRECONDITION`、不写 PASS/Closed，也不停止其他 lane。真实 credential replacement、rotation 或充值仍需 owner action-time 授权；恢复后从旅程起点重跑 |
| PDEC-007 | 历史：主 Codex 对全栈交付负责，允许 Codex 原生 Multi-Agent 与 subagent | **执行分工与外部 Harness 禁令已由 2026-09-04 PDEC-012 替代，不再生效。** 原合同仅允许原生 subagent；主 Codex 的 Goal、最终验收、集成、生产 E2E/A2A、部署、evidence 与交付责任继续保留 |
| PDEC-008 | 主 Codex 自行建立可复用的实验 tenant 合成 fixture | 可创建/登录/切换/撤销 employee、company-admin、platform-admin、scoped-operator 身份及临时 grant、Session、Agent、KB、Workflow、Local Agent binding；只走受支持且经过认证的 UI/API/control-plane path，禁止伪造 JWT/token、直接修改 tenant/role DB 字段、关闭/放宽 RLS 或用 broad bypass 创造业务授权。生产 fixture effect 由主 Codex 执行；zCode、Kimi、CC 只做各自获准的 repo 工作。缺少预存会话或 fixture 不是 owner gate，支持路径失败形成 fresh Finding |
| PDEC-009 | 产品正确性与 external readiness 分账 | Hive 缺陷、缺实现或缺接线必须修复；独立确认的第三方不可用不是 Hive defect，只 park 对应 provider-success assertion 并继续无关工作，但不能制造 PASS/Closed。冻结旅程若要求真实 provider success，在 provider 恢复或 owner 明确 `Excluded` 前保持未闭环并继续计入 NPTCR 分母 |
| PDEC-010 | 缺少仓库内 runtime/build/adapter 是实现工作 | 主 Codex 可派 zCode 按现有 lockfile 安装既有依赖并构建 FreeCode 或最小必要 benchmark adapter；缺少预编译 CLI 不等于 `BLOCKED_PRECONDITION`，也不授权新增供应链或真实凭据/计费效果 |
| PDEC-011 | 本 Goal 创建并登记的合成资产 cleanup 已授权 | 通过受支持、经过认证的产品路径核对 exact target/readback 后可幂等清理本 Goal 创建并登记的合成身份、grant、Session、文件和 binding，并验证无 searchable/active ghost；pre-existing fixture、不可变 evidence、owner 基础账号、真实数据、真实密码/组织 secret、计费和实际读取/披露/修改其他 tenant 受保护数据永不作为 cleanup target。已登记的 read-only deny/not-found probe 仍在 scope，且不得获取受保护字节或产生效果；若意外返回 protected bytes，立即停止该 lane，不继续读取/传播/写 raw evidence，只保留最小脱敏 P0 事故证据 |

## 2026-09-04 已接受的新一轮执行裁决

| ID | 已接受决定 | 精确边界 |
|---|---|---|
| PDEC-012 | 全量目标不变，文档先行，再建立新 Goal；zCode/Kimi 实现，CC/Codex 两层 review | zCode 负责后端及功能实现代码，Kimi Code 负责前端 UI/交互。日常 CC（Claude Code）先独立审查，Codex 随后独立检查代码和证据、核对结论并补充遗漏；**只有重大节点额外进行 Codex 与 CC 双向对抗性审查，挑战方案、对账证据，解决阻塞发现并收敛结论后推进**。不把对抗流程套到每次小改动，不增加第二 controller。主 Codex 保留派单、集成、生产 effects、验收与交付；用现有 `agent-delegation`，精确任务边界、隔离或不重叠写入，worker 不 commit/push/deploy、不自验收。此决定明确替代 PDEC-007 与旧 Goal 中 single-Codex/禁止外部代理的分工条款，不改变 96 条分母、七原子、五护栏、双遍、D/E、rollback 或 cleanup |

长期目标再次确认为：所有功能尤其 Agent 使用畅通、架构简单且治理有度、所有页面对小白友好并以 Codex Desktop Session 交互为参照。执行收敛顺序和重大节点只写在 Runbook。外部 reviewer 的工作不是替代 Codex；Codex 必须对同一候选独立复核。复验修复覆盖受影响路径，不为没有新事实的事项安排无限审查轮次。

主 Codex 自撰或自改的部分优先交非作者 reviewer 做只读 review；没有外部 reviewer 时，Codex 不把自审冒称独立意见，而以更严格的逐调用链、逆向红例和可执行证据补偿。不因此增加 reviewer/controller。stage 与 commit/push/deploy 一样只由主 Codex 操作。

## 2026-09-05 已接受的产品角色裁决

| ID | 已接受决定 | 精确边界 |
|---|---|---|
| PDEC-013 | 三种产品角色；管理员拥有管理范围内的全部业务权限，凭据不明文展示；公司后台补返回 App | 平台管理员拥有平台管理权限，可管理目标公司的全部 Agent/业务内容并分配公司管理员；公司管理员拥有本公司全部业务权限，可给本公司其他员工授予公司管理员。owner 明确确认包含员工 Agent 的私有会话、文件和知识内容。员工只看到自己的及公开的 Agent，管理自己的 Agent，但不能删除 Agent，也不能自行授予公司/平台管理权限。明文密码、密钥、token 等凭据不进入 API/页面/导出。公司后台必须有可发现、可键盘操作的返回 App 入口，不以退出登录代替 |

该决定替代旧的「公司管理员不能读 Personal KB/private Session」「平台管理员只看运维、不看业务正文」及管理员必须额外取得 `operator.inspect` 才能访问管理范围内业务内容的条款。管理员普通业务访问由真实角色与资源所属公司授权，不要求填写 operator 理由或额外申请 grant；审计继续记录真实操作者、目标、动作与公司，不冒用员工身份。公司管理员不得跨公司，员工不能因 Agent 公开而读取其他使用者的私有会话/个人知识或管理该 Agent。

「公开 Agent」沿用现有公司内公开入口，不把私有 Agent 或业务内容发布到互联网。既有 scoped operator 是技术能力/验收标签，不是第四种产品角色，也不能成为绕过员工「自己/公开 Agent」可见面的旁门。员工「等部分操作」未列明的细项不得由实现者猜测扩大禁令；以现有动作合同逐项核对，重大冲突交 owner 裁决。

这是产品权限语义的显式变更，不是为通过旧测试而放宽控制。保持 96 条 journey ID、数量、评分和 D/E 完成门槛；修订 P15/P29 及其关联角色断言、fixture 描述与冻结指纹，保留旧 evidence 为历史，不迁移为新合同 PASS。跨层实现与最终证据按重大节点完成非作者交叉审查、Codex 严格复核和证据对账。

本裁决只授权实现、验证产品能力，不扩大验收操作者的真实数据/生产效果权限；仍只使用已批准的合成 scope。GROVE 实际管理员分配的 browser action-time 确认仍单独等待，不把本次产品规则回答当作提交该表单的确认。Goal 继续使用已激活实例，并通过其 canonical 文档引用恢复本裁决，不为修改文字把未完成 Goal 标记完成或重建。

## 2026-09-05 已接受的审查可用性裁决

| ID | 已接受决定 | 精确边界 |
|---|---|---|
| PDEC-014 | CC 不再是进度前置门；使用可用的非作者交叉审查，并由 Codex 加严最终复核 | Claude Code 不可用、限额或等待时不得停住 Goal，也不必等其恢复。优先让未参与该候选实现的 zCode 或 Kimi 做只读交叉审查：zCode 可审 Kimi 的前端候选，Kimi 可审 zCode 的后端候选；实现者不能把自审算作独立 review。无 CC 时主 Codex 必须加严到完整 scoped diff、真实调用链、逆向旧逻辑红例、真实 PostgreSQL/RLS/浏览器证据及回归边界，并独立裁决 reviewer 结论。重大节点仍需对方案、反例和证据做对抗性对账，但可由可用的非作者 reviewer 与 Codex完成；不得因 reviewer 品牌缺失伪造意见、降低门槛或把本地绿冒充 production PASS |

PDEC-014 只修改 reviewer 可用性与顺序，不修改 PDEC-012 的实现分工、Codex 的唯一验收/生产权威、96 条分母或 PDEC-013 产品语义。实际报告必须标注真实 reviewer 和证据来源；CC 历史 review 仍只对其 exact snapshot 有效。

## 当前动作权限

| 动作 | 状态 |
|---|---|
| 建立、移动、压缩和索引本轮文档 | 已授权 |
| 增加只校验文档结构事实的测试 | 已授权，不能判断语义质量 |
| 建立 Goal、RC milestone/labels/Issues 和只读 provider smoke | 已授权；Issue/worker 状态不是验收事实 |
| 修改业务代码或前端 UI | 已授权于 fresh reproduction 或当前源码证明缺失实现后修复；按 PDEC-012 由 zCode/Kimi 实现、主 Codex 集成；PDEC-014 只允许替换 reviewer，不替换实现作者；禁止猜测性改写 |
| 本地测试、交叉 review 与原子集成 | 已授权；优先由非作者 zCode/Kimi 或可用 CC 先只读 review，Codex 再加严复核 live wiring、完整 diff、逆向红例、关键真实测试和遗漏；reviewer 不可用不再 hold，处理阻塞发现后集成 |
| commit / push | 已授权于验收基线和每个已验证修复；只提交本轮 scope，不夹带用户已有改动 |
| Railway 部署 | 主 Codex 已获授权把 coherent frozen `D` 同时部署 `backend`、`backend-api`、`frontend`；不逐 commit 自动部署，本授权不覆盖凭据、计费、不可逆数据效果或非冻结提交 |
| write-bearing production E2E | 主 Codex 已获授权仅在 Example Owner 实验账号/tenant 内按冻结 manifest 创建可识别、可登记、可回收的合成资产；必须先登记 cleanup，禁止真实客户数据和未列外部发送；zCode、Kimi、CC 不执行生产 effect |
| 合成登录、角色/grant、lab Session / Local Agent binding | 已授权；只走受支持认证路径，必须登记、限定 Example Owner 实验 tenant、验证正向与负向边界并 cleanup；禁止 forged identity、直接 DB role/tenant mutation、RLS weakening 和 ambient bypass |
| 充值、真实 provider credential、真实用户密码、组织 secret | 未授权，action-time 单独确认 |
| additive/backward-compatible migration | 已授权于完整 migration test、backfill、rollout safety 与 rollback/forward recovery；不可逆生产迁移或真实数据删除仍需确认 |

2026-08-25 的旧 RC “开始”授权只解释历史执行；2026-08-30/31 的裁决保留全量验收及效果边界，2026-09-04 PDEC-012 更新实施与 review 分工，2026-09-05 PDEC-013 更新产品角色语义，PDEC-014 取消等待 CC 的进度前置门。production manifest 的 96 条分母不变；PDEC-013 所涉及的角色断言必须显式修订并 review，其他旅程与评分不变。旧 hash 的 immutable evidence 保留但不迁移为当前 PASS。新分工和产品权限不授权验收操作者读取真实凭据、计费、真实外部收件人、不可逆真实数据效果或实际读取/披露/修改其他 tenant 受保护数据；旧品牌兼容和后台公司/邀请/返回 App 修复继续纳入最终 D。

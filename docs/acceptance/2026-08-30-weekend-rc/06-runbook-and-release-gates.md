---
document_id: weekend-rc-2026-08-30-runbook
owner: Codex
status: active
authority: canonical-execution-and-release-gate
last_reviewed: 2026-09-05
source_commit: 0ce51f049e03c689a440075a5de8a7a9d99c609c
verification_status: owner-approved-pdec-013-role-contract-implementation-pending
---

# Weekend RC 协作收敛 Runbook 与 Release Gates

[返回索引](README.md) · [Owner 决策](02-owner-decisions.md) · [Journey Ledger](04-journey-ledger.md) · [Evidence 合同](evidence/README.md)

本文件规定执行顺序和机械发布门，不拥有产品语义、旅程状态或实际结果。

本轮执行 PDEC-012/PDEC-014：zCode 负责后端及功能实现，Kimi Code 负责前端 UI；优先由未参与该候选实现的 zCode/Kimi 或可用 CC 做只读交叉 review，主 Codex 随后独立加严审查代码、调用链和真实证据。CC 不可用、限额或等待不再阻断进度；只有重大节点额外挑战方案、反例并对账证据。主 Codex 对派单、集成、Git、三服务部署、生产 E2E/A2A、证据与最终验收负责。旧 single-Codex 与禁用外部代理的分工已被替代；GitHub Issue 仍只是可选审计引用。

## 分工与 Review

| 角色 | 工作 | 边界 |
|---|---|---|
| zCode | 后端、API/业务合同、功能实现、相关回归；必要的前端功能接线；可只读交叉 review Kimi 候选 | 不承担 UI 设计；不能把自己实现的候选自审算作独立意见；跨前后端共享文件先由 Codex 指定单一 writer |
| Kimi Code | 前端 UI、交互、文案、信息层级、可访问性及 UI 回归；可只读交叉 review zCode 候选 | 消费已确认的功能合同，不另造后端语义或 fake 主路径；不能把自己实现的候选自审算作独立意见 |
| CC / Claude Code | 可用时只读检查源码、真实接线、需求覆盖、简洁性、回归与风险，给出 finding 和证据 | 是可选 reviewer，不再是进度前置门；默认不修改实现，不把实现者自测或状态标记当成审查通过 |
| 主 Codex | 独立检查同一候选，核对交叉审查结论、补充遗漏、验证关键证据；派修、集成、真实验收和交付 | 无 CC 时必须加严完整 diff、真实调用链、逆向红例及真实 PG/RLS/浏览器证据；不冒称缺失的独立意见，不静默接管指定实现分工 |

**日常 Review：非作者 zCode/Kimi 或可用 CC 先只读交叉 review → Codex 独立加严 review/补充 → 修复发现并复验。** 外部 reviewer 暂不可用时 Codex 直接执行加严复核，不等待；作者自审不能占据交叉审查席位。

主 Codex 自撰/自改部分优先由可用非作者 reviewer 提供只读 review；没有时 Codex 的严格自审不冒称独立意见。stage 只由主 Codex 操作。纯文档准备的审查结果及修正记入当前状态并引用现有 delegation receipt，不冒充 production journey evidence。

**只有重大节点额外启动对抗性证据对账：** Codex 对交叉 reviewer 的方案/结论提出反例、遗漏路径和证据不足，可用的非作者 reviewer 再挑战 Codex 的接受/拒绝依据；双方对齐 exact snapshot、争议事实与剩余风险。CC 缺席不阻断该节点，Codex 必须增加显式反命题、可执行反例和证据覆盖审计。不是每个小修复都往返一轮，也不设没有新事实的固定/无限辩论轮数。争议无法由证据解决且涉及产品/风险裁决时回到 owner。

Review 绑定 exact base SHA 与完整 scoped diff（含相关未跟踪文件）的内容指纹、要求、实际测试和未验证边界。代码变化后只把旧 review 保留为对应旧快照的证据；重新审查修正与受影响路径。两步审查的范围、发现、处理与残余分别记入既有 Finding/evidence，`03-current-status.md` 只记录当前结论及下一动作；审查记录不是 production PASS。

派单使用现有 `agent-delegation`，不新建编排框架或第二账本。任务包保留 worker 正常分析与工具能力，明确目标、已授权效果、禁止效果、exact base/diff 与 Done；`cwd` 不是 sandbox。实现采用隔离 worktree 或明确不重叠的文件写入，不能把只含 HEAD 的 worktree 冒充已包含当前 dirty candidate。zCode/Kimi/CC 不 commit/push/deploy、不持有生产凭据、不做生产 effects 或最终验收；review 默认只读，主 Codex 单写集成与状态。

代理暂不可用时记录真实失败并按 PDEC-014 使用非作者交叉 reviewer 或 Codex 加严复核继续推进；不得伪造 review PASS、把作者自审冒充独立意见或静默改变实现分工。

## 执行控制模型

| 层 | 唯一职责 | 不能证明 |
|---|---|---|
| Codex Goal | 保存最终 Done、停止条件和整个循环的存续 | 任何单项功能已通过 |
| canonical 文档 + frozen manifest | owner 接受的合同、分母及已接受 verdict 与证据关系 | worker 是否改对代码，或证据的语义含义 |
| GitHub Issue | 可选 Finding 审计引用 | 执行顺序、语义正确、已验收或可发布 |
| 当前 checkout + scoped diff/tests | 变更内容、失败回归和本地行为 | 已部署或生产通过 |
| 非作者交叉 review + Codex 加严复核 + 重大节点对抗 | 各自基于同一候选的发现、证据、遗漏；重大节点的方案挑战及证据对账结论 | 真实生产旅程已通过或最终发布可接受 |
| immutable production evidence | exact commit 下的真实旅程结果 | 未覆盖旅程的结果 |

统一执行循环：主 Codex 在 owner-approved proof order 内基于 live evidence 选择最高杠杆的安全未闭环路径，按上述分工派发不冲突的工作；Gate 编号、分数、Issue 顺序、attempt count 与 timeout 不决定语义 priority。缺少预存身份、fixture、Session 或仓库内 runtime/build/adapter 时，主 Codex 经受支持路径建立生产合成 fixture，或派 zCode 补齐最小必要 runtime；它们都不是 owner gate。

禁止用 Hive Dynamic Workflow 来验收 Hive 自己，也不增加第二套 semantic supervisor、shadow ledger 或 worker-owned truth。Goal 负责持续性；产品 turn 的 selected runtime LLM 负责任务语义，主 Codex负责验收语义，owner 负责产品/风险裁决；canonical docs 记录已接受合同/verdict；Git/测试负责代码事实，evidence 负责生产事实。worker/reviewer 可以独立推理与挑战假设，但其回执不能自动晋升验收结果。

Model Agency Boundary 全程有效：Hive 产品 turn 的 selected runtime LLM 在已认证 frame 内拥有任务 reasoning、semantic judgment、synthesis 与 answer language；RC 循环的主 Codex 拥有验收 decomposition、priority、evidence interpretation、acceptance judgment、Journey/Finding verdict 与 final handoff；owner 拥有产品语义和风险授权裁决。机械 checks 只能验证 exact authenticated authority/RLS、manifest、commit、deployment、evidence、arithmetic、resource、recovery 和 machine-contract facts，并只 hold 与缺失 invariant 对应的 effect/release，不得决定 semantic truth、quality、failure、`blocked`、priority、Journey/Finding verdict，或改写/压制模型输出。NPTCR、五护栏与 Evidence Coverage 只聚合主 Codex 基于真实证据已接受的 verdict。

Synthetic identity/fixture 只经受支持、经过认证的 product/control-plane path 建立；禁止 forged claim/JWT/token、直接修改 tenant/role DB 字段、关闭/放宽 RLS，或用 broad/ambient bypass 创造业务 authority。RLS/ACL 在 exact unauthorized ingress/read/write/effect 处 fail closed；denial 只阻断该操作，保留已授权证据、无关推理、获准工具、draft 与 recovery。已登记 read-only deny/not-found/existence probe 必须继续，但不得获取受保护字节或产生效果；若意外返回 protected bytes，立即停止该 lane，不继续读取/传播或写 raw evidence，只留最小脱敏 P0 事故证据。platform/operator 路径必须 server-derived、reason/scope-bound、audited 且不可由 client 自行升级；owner 指令不能把未授权访问变成授权。

## Gate 0：执行前冻结

PDEC-013 是当前产品角色合同：平台管理员/公司管理员可访问管理范围内的私有业务内容并任命公司管理员，凭据不明文展示；employee 仅自己的/公开 Agent、不能删除 Agent或自授管理员。operator 是技术视图/能力，不是第四种身份，不是管理员业务访问的前置 grant。角色实现属于重大跨层节点；旧 deny/observer-only evidence 不迁移。修改 manifest 角色断言和冻结指纹须显式绑定 PDEC-013，并证明全部 96 个 ID、数量、评分、非角色旅程及验收操作者的合成效果边界不变。

- 锁定 checkout、production exact commit、三个 Railway service deployment、账号/角色和数据版本。
- 确认 selected model/provider、semantic runner、Channel、Local Agent 当前可用性。
- 核对已冻结 production journey manifest 的 Git blob/hash；96 条旅程不得因执行结果变化而删除或合并。
- 登记合成资产、预期外部效果、成本/时延测量点和 cleanup。
- 读取 [02-owner-decisions.md](02-owner-decisions.md)；未授权的 credential、DDL、删除、邀请和生产效果不执行。
- 仓库内 semantic runner/build/adapter 缺失时直接构建；外部 provider/服务不可用时验证 typed unavailable、审计、恢复提示和无关能力保留，只暂停外部成功断言。
- 共享合成身份/fixture 保留到所有依赖旅程完成；lane-local transient effect 在 reconciliation 后清理；final `D` 双遍结束后清理全部 Goal-created synthetic assets。不得删除 owner 的 Example Owner 基础账号、immutable evidence 或无关数据。

## 单线优先顺序

| 顺序 | 主线 | Gate |
|---:|---|---|
| 1 | Gate 0 最小可用 fixture + 最小真实 Session 中的单 Agent 智能、Memory/Growth、自进化、bakeoff 与 selected-model fidelity | 先证明真实 Agent 会思考、会完成开放任务、会进化且不弱于基准；fixture 只用于进入功能路径，不在此阶段展开完整 Session/command 或权限加固 |
| 2 | 全部前后端功能：完整 Session streaming/20 commands、Agent/HR、Knowledge、Subagent/Team、Workflow/A2A、Automation、Hook/Skill/MCP/Local Agent、Artifact 与全部 UI | 每类真实入口、主路径、功能性故障恢复和最终消费完整；功能未完成时先修功能，不以安全工作掩盖 |
| 3 | 权限与安全：PDEC-013 三角色与技术 inspector UI/API、RLS/ACL、revocation/offboarding、secret/PII、injection、replay、approval、delegation escalation | 在完整功能面上做管理员业务正向、员工与跨公司负向及凭据 no-leak；修安全不能破坏同 tenant novice 主路径 |
| 4 | coherent `D` 全量门、三服务 exact deploy、完整双遍、evidence 与 cleanup | 任一代码/config/schema 修复生成新 `D` 并重跑受影响门 |

该顺序只决定验收与修复优先级，不关闭任何现有 authorization、secret 或 external-effect boundary。若功能检查遇到真实越权/泄漏，立即隔离该 lane；除此之外，权限加固、RLS 扩张和安全评分不得提前阻断无关功能补全。Gate 3 必须基于 Gate 1/2 已真实可用的功能面验证“很好用，然后很安全”。

不设人工 Goal-wide timeout、step cap 或 attempt cap。task-sized per-call/per-attempt timeout、cancel、quota 与 backoff 仍作为资源/生命周期控制；expiry 只结束或恢复当前 attempt，不参与产品语义判断。只要仍有安全的 in-scope 工作，Goal 继续，不缩小分母或降低门槛。

## 新一轮收敛节点

下表 M0–M4 是本轮重大节点；M1–M4 对应上表既有顺序 1–4，M0 仅收口已有 dirty candidate，不另设一套进度状态。各节点在非作者交叉 review 与 Codex 加严复核之外增加对抗/证据对账并收敛结论；CC 可参与但不可用时不等待。涉及跨层权威、核心 Agent 路径或发布条件的实质改变也按重大节点处理；把 owner 已裁定的分工如实写入文档，不等于另作一次产品/发布决策，故本次文档准备不是 M0。文档措辞、孤立小修复不自动升级。功能实现与相应 UI 可按不重叠文件并行，必要安全边界始终生效。

| 节点 | 可观察成果 | 首要工作与不得冒充 |
|---|---|---|
| M0 当前候选收口 | 现有 dirty candidate 边界清楚，已知失败修复，必要回归通过，适用的独立 review 及重大节点对抗/对账完成 | zCode 先收口 J4 断言/真实入口接线和 tenant retirement/Local Bridge；保留 admin/invite 与旧品牌兼容。J4 只补真实 bakeoff 必需部分，不继续扩张通用测试平台；绿测试不是 RC 完成 |
| M1 Agent 真正可用 | 普通员工从真实入口创建/选择 Agent，完成开放任务，消费工具与产物，能继续、reload、恢复；Memory/Growth/J1–J4 的真实消费和收益得到证明 | 先跑最小用户黄金路径，再补齐该域冻结要求；身份 fixture 只服务对应 persona。独立确认的 provider 不可用只 park 对应成功断言，不制造 PASS、不拖停无关工作 |
| M2 全功能与小白 UI | 既有 96 条范围里的全部功能入口、主路径、功能性恢复与前端消费真实可用 | zCode 修共享根因，Kimi 同步收敛 Session/导航/表单/产物/后台等全部页面，优先真实已见的工程噪声、零值面板和恢复困难；不做脱离旅程的全站重写 |
| M3 权限/故障与冻结 | PDEC-013 三角色及技术 inspector 正负向、no-leak、active revocation、安全对抗、故障恢复与全部本地/CI release gates 成立；形成 coherent `D` | 管理员业务访问正向与员工边界都须成立；不能把旧基线 CI 算成当前 dirty candidate 通过 |
| M4 发布及全量闭环 | 三服务 exact `D`，96 条同提交连续双遍、完整 rollback/cleanup、最终 review 与 evidence-only `E` | 先复验旧品牌/KDF 兼容、后台建公司/邀请/Back to App 和已知 operator 修复，再完成全量；任何应用修复产生新 `D` 并重跑完整双遍 |

耗时只在 M1 首次真实功能盘点后重估；此前讨论的 1–2 周只是低置信度计划量级，不是完成承诺或降门槛依据。当前状态、实际进度与未完成项只在 `03-current-status.md` 更新。

J4 本地恢复：per-envelope workspace/artifacts/scoring 叶子要求当前attempt的归属；共享immutable scorer可复用，但每个文件必须匹配冻结hash。遇到 `scorer_snapshot_mismatch` 时保留原证据，换新建的owned output目录重试，不覆盖或自动删除未知旧文件。目录检查只承诺发现已观察到的冲突，不宣称抵抗同UID敌对进程的全部路径竞态。

## Finding 处理链

```text
frozen journey + exact code/data/persona
  -> novice UI reproduction
  -> earliest wrong state + seven-atom breakpoint
  -> live-entry wiring/path proof
  -> production-shaped failing regression
  -> zCode/Kimi repair one authoritative contract/root cause
  -> focused + cross-domain + full + real PG + build/i18n/a11y
  -> non-author zCode/Kimi cross-review or CC review when available
  -> Codex strict independent review of code/evidence, reverse old logic, check conclusions and add omissions
  -> at major milestones only: adversarial challenge of plan, counterexamples and evidence; no CC wait
  -> resolve findings and revalidate affected paths
  -> Codex atomic integration commit
  -> continue remaining frozen journeys without per-commit production deploy
  -> freeze final application candidate D
  -> backend/backend-api/frontend exact same commit deploy
  -> signed-in clean pass 1 + hard reload
  -> signed-in clean pass 2 + fault/recovery + negative authority
  -> evidence file + ledger Closed; external readiness exception stays separate
```

## Commit、Issue 与交付账本

1. 复现后才创建 Finding/Issue；同一 dispatch wave 可用一个 docs checkpoint 提交多个互不相关的已复现 Finding。
2. 一个可独立回滚的共享根因对应一个 Codex integration commit，包含实现、failing-first regression 和适用 migration/backfill/rollback；不按页面、worker、测试运行或 receipt 机械拆 commit。
3. 非作者 zCode/Kimi 或可用 CC 先只读交叉 review，Codex 再严格复核 scoped diff、live wiring、逆向红例、关键真实测试和遗漏；外部 reviewer 不可用时 Codex 不等待但必须记录加严证据，处理阻塞发现后才提交。跨层合同先固定权威层，再消费前端，不允许半个合同进入候选；review 不代替真实产品验收。
4. 每个集成提交 push 后运行现有 CI，但不自动部署生产。Issue 在本地验证后只能标 `awaiting-production`，不能 Closed。
5. 最终应用候选记为 `D`；所有 runtime code、schema、migration、config、测试和当时状态文档都在 `D`。三个 Railway 服务只部署同一个 `D`。
6. `D` 的 signed-in 双遍完成后，新增纯 evidence/docs commit `E`，其父提交为 `D`。最终交付同时报告 Application SHA `D` 和 Evidence SHA `E`；`E` 不重新部署，否则会产生未被双遍验证的新应用身份。
7. 若 production 暴露缺陷，旧 `D` 和其 pass 立即失效；修复形成 `D2`，三个服务全部重新部署并重新运行完整双遍，最后才生成新的 `E2`。

机械入口：

```bash
python3 backend/scripts/weekend_rc_gate.py validate
python3 backend/scripts/weekend_rc_gate.py score --deployed-commit <40-char-application-sha>
python3 backend/scripts/legacy_brand_release_gate.py working-tree
python3 backend/scripts/legacy_brand_release_gate.py git-archive <40-char-application-sha>
```

该工具只拥有 manifest/evidence/deployment exact facts 和算术，不拥有 Journey、Finding、产品质量或最终语义 verdict。

## 本地与 CI Release Gate

- frontend tests 全绿；TypeScript、i18n、build、bundle budget、a11y 通过。
- backend full tests 全绿；Docker-on/真 PostgreSQL 类型没有整类 skip。
- 变更路径 Ruff check/format 与 `git diff --check` 通过。
- migration upgrade/downgrade/retry-safe、legacy backfill 与 rollback 在适用时通过。
- 旧品牌只保留批准的 input-only/package-mirror、历史 KDF 解密与一次性 theme-key 兼容；最终 committed archive 不含已登记的本机路径、个人账号或真实形态测试身份；working tree 与最终 committed archive 两个 gate 均通过。
- 每个 bug 有 failing-first 回归，且测试走 production live entry，不用 fake 掩盖 wiring。
- 无 raw internal payload/ID/provider private prose 泄漏到普通用户 DOM。
- 结构测试只能验证 exact fact；semantic quality 由真实任务、外部判据和 owner/独立 review 决定。

## 生产 Gate

- backend、backend-api、frontend 来自同一 exact committed archive，Railway 全部 `SUCCESS`。
- 三服务上传前对 application SHA 运行 `legacy_brand_release_gate.py git-archive`；不得以 working-tree PASS 代替 committed archive PASS。
- 公共 backend `/api/health` 与 frontend `/` 成功；backend-api 以 exact deployment status 证明 freshness。
- runtime worker、stream forwarder、相关 daemon、RLS、sandbox/connector 只按其真实合同检查；health 不能替产品旅程。
- 全部 in-scope 冻结旅程在同一 final commit 连续两遍 clean pass；owner 带理由明确 `Excluded` 的旅程不进入 NPTCR 分母。
- reload、disconnect、worker restart、duplicate delivery、retry、cancel 不造成假成功、永久挂起或重复 effect。
- permission denied、unavailable、empty、not-indexed、parse-failed 分开表达。
- Zero Known Defects：范围内无开放 P0/P1/用户可见 P2，其他 defect 也不得以优先级自动延期。
- `weekend_rc_gate.py score` 必须 `mechanical_ready=true`，但该结果只是 Codex 已接受证据的机械完整性确认，不能独立升级 Journey。

## 安全与对抗性 Gate

- PDF/DOCX/HTML、KB、邮件/Channel、MCP description/prompt、Skill、tool/sub-agent/A2A result 中的间接 prompt injection 只能作为数据。
- 覆盖 cross-tenant existence probing、越权 citation/read、secret/PII 泄漏、path/URL/token passthrough、approval bypass、模型自批、delegation 权限扩张和外部发送重放。
- benign 文本包含 security/tool 关键词仍能正常推理；决定性证据在长文末页、最后 chunk 或巨大 tool result 中不被静默丢弃。
- denied effect 后，无关推理与获准工具仍可继续。
- reviewer/provider unavailable 只能 hold/retry/report typed failure，不得机械接受、拒绝、晋升、删除或改写语义。
- RLS/权限修复必须同时通过 cross-tenant no-leak 负向与 same-tenant novice 正向路径；不能用“更严格”掩盖主路径被误伤。
- 每次生产事故进入 production-shaped regression，且控制启用时 novice primary path 仍可完成。

## Evidence Coverage Score

| 层 | 权重 | 事实源 |
|---|---:|---|
| 当前源码与 live wiring | 15 | UI/API/tool/daemon 到唯一执行器 |
| 自动化与真 PostgreSQL | 20 | failing-first、权限、恢复、幂等、Docker-on |
| signed-in 生产双遍 | 30 | 普通员工真实入口、同一 commit、无手工补状态 |
| 故障与恢复注入 | 20 | reload、disconnect、retry、cancel、restart、duplicate |
| 权限负向与跨租户 | 15 | denied/unavailable/empty、无存在性泄漏 |

达到 95 仍不能抵消任何七原子缺失、护栏失败、开放产品缺陷、版本漂移或 fake live boundary；外部 readiness 另表披露。

## 停止并回到 owner

1. exact 下一效果会尝试或要求超出已授权 synthetic scope 的读取、披露、修改或作用于真实非合成数据/其他 tenant 受保护数据，或向未登记的真实外部对象发送邀请、消息或文件；只会得到 non-leaking deny/not-found 且不取回 protected bytes/不产生效果的已登记负向 probe 不在此列。probe 若意外泄漏 protected bytes，立即停止该 lane 并按最小脱敏 P0 事故证据处理。
2. 需要购买/充值、轮换或暴露真实 provider credential、组织 secret 或真实用户密码。
3. 需要无安全恢复路径的不可逆生产数据删除/迁移，或发现跨租户/凭据泄漏与数据破坏风险。
4. 修复要求改变 owner 已接受且当前源码/North Star 无法裁决的产品语义，或违反法律/平台安全政策。

经受支持认证路径执行的合成登录、角色/grant、lab Session、Local Agent login/pair/revoke/binding、仓库 runtime build、registered synthetic cleanup 不属于停止条件；它们绝不授权伪造身份、直接 DB role/tenant mutation、RLS weakening 或读取/轮换真实 secret。同一路径重复失败触发更深根因调查，不机械终止整个 Goal。

## 最终交付

- Application SHA `D`：exact code/tests/migration/backfill/rollback；
- backend/backend-api/frontend 绑定 `D` 的 deployment identity；
- Evidence SHA `E`：只记录在 `D` 上已发生的生产事实；
- production journey evidence + screenshot matrix；
- employee/admin/operator audience verdict；
- J1/J2/J3/J4 growth and bakeoff；
- latency/token/cost/cache/intervention/model fidelity；
- NPTCR、五护栏、Evidence Coverage、七原子、ZKD、Excluded/Blocked、cleanup ledger；
- 当前状态中的唯一下一动作。

## 新 Goal 启动文本

以下是本轮批准的 model-facing objective；先完成文档一致性检查，再请求 Goal 工具创建。若旧 Goal 尚未完成且工具拒绝新建，如实保留旧状态并请 owner 结束/替换旧 Goal；不得为了腾位置把未完成 RC 标记 complete，也不得暗中新建 Codex task。

新 Goal 已按 2026-09-04 合同创建且保持 active。以下模板在 2026-09-05 补入 owner PDEC-013，不再声称与保存 prompt 仅品牌名不同；当前用户 steering 和 Goal 引用的 canonical 文档提供新角色合同。恢复时读回已有 Goal、再读 PDEC-013，不为改文字重复创建或假完成。仓库模板使用 `legacy-brand` 泛称以保持兼容 release hygiene。

```text
Complete the full Hive Weekend RC 2026-08-30 under the owner's 2026-09-04 renewal. This is full product delivery, not an MVP or documentation-only completion. Read docs/acceptance/2026-08-30-weekend-rc/README.md and its canonical north-star, owner-decisions, current-status, journey-ledger and runbook documents. Use 03-current-status.md as the single recovery state and reconcile it with the live checkout/runtime.

Deliver all functionality, especially useful and capable Agents; keep architecture simple and governance proportionate; make every user-visible page novice-friendly using Codex Desktop Session interaction and progressive disclosure as reference. Preserve all 96 frozen production journeys, including Agent intelligence, Memory/Growth J1-J4, Session commands, Knowledge, HR/creation, collaboration/A2A/workflows, automation/extensions/Local Agent, artifacts, models, role/security and release acceptance. Preserve legacy-brand and credential-compatibility gates plus admin company creation, administrator/member invitations and Back to App fixes in the final candidate and retest.

PDEC-012 supersedes the old single-Codex/external-agent prohibition. zCode implements backend and functional code; Kimi Code implements frontend UI and interaction. PDEC-014 says Claude Code availability is not a progress gate: prefer a read-only cross-review by a non-author zCode/Kimi worker or CC when available, then require primary Codex to strictly inspect the full scoped diff, live callers, reversed-old-logic failures and decisive runtime evidence. An author cannot count self-review as independent. At major milestones, adversarially reconcile the plan, counterexamples and evidence with an available non-author reviewer; do not wait for CC, lower the acceptance bar or add fixed debate rounds.

Use the existing agent-delegation skill with scoped missions and isolated or non-overlapping writes, not another controller or ledger. Workers/reviewers may reason independently but may not stage, commit, push, deploy, access production credentials, perform production effects or grant final acceptance. Primary Codex owns dispatch, integration, production effects, acceptance, deployment and delivery. Finish the current document review before implementation.

Apply the owner's 2026-09-05 PDEC-013 role amendment: platform administrators manage the platform and all business content in the explicitly targeted company; company administrators manage all business content within their own company; both may appoint company administrators within scope. This explicitly includes employees' private Agent conversations, files and knowledge. Never expose plaintext credentials. Employees see only their own and company-public Agents, manage their own Agents except deletion and administrator-only operations, and cannot grant themselves stronger roles. Operator inspection is a technical capability/view, not a fourth product role or an additional grant prerequisite for administrator business access. Retain authenticated actor identity, precise tenant/resource authority and audit. Add a discoverable Back to App entry in the company backend. Amend conflicting frozen role assertions with explicit review while retaining all 96 IDs, counts, scoring and unrelated journeys; historical evidence is not a new-contract PASS. Treat the cross-layer role change as a major reciprocal-review milestone. Product role permissions do not enlarge the acceptance executor's synthetic production-effect scope.

Follow runbook M0-M4: close the existing dirty candidate without expanding benchmark scaffolding; prove the ordinary employee Agent golden path and real Memory/Growth/bakeoff; complete all functions and functional recovery with novice UI; complete exhaustive role/RLS/security and fault checks; freeze and release coherent application D. Preserve existing authority, secret and effect boundaries throughout. Missing lab sessions/fixtures or repository runtimes require supported-path recovery or implementation. Verified external unavailability parks only the exact provider-success assertion, keeps the required journey unclosed and does not stop unrelated safe work.

Done requires every in-scope frozen journey Closed loop, NPTCR 100%, all five guardrails, Evidence Coverage >=95 without missing acceptance atoms, Zero Known Defects, three services backend/backend-api/frontend on the same exact D, two clean signed-in production passes, negative-authority and fault/recovery evidence, rollback verification and registered synthetic cleanup. After final review, create evidence-only direct child E without deploying E. Any application/code/config/schema fix requires a new exact three-service D and full double pass. Mocks, historical passes, worker receipts, CI, health or scores are not production acceptance.

Preserve unrelated owner changes. No artificial Goal-wide time/step/attempt cap decides completion. Pause before ungranted billing, real credentials/secrets, real external recipients, unauthorized protected data, irreversible production data effects or unresolved owner product/risk decisions; continue unrelated safe work. Use only the existing approved synthetic scope. Mark complete only when the actual full outcome is achieved.
```

---
document_id: weekend-rc-2026-08-30-runbook
owner: Codex
status: active
authority: canonical-execution-and-release-gate
last_reviewed: 2026-08-31
source_commit: bf94b76a1706510daf2d11c4e98fd5051f23f28f
verification_status: owner-corrected-model-agency-rls-and-native-multi-agent-contract
---

# Codex-Native Multi-Agent Runbook 与 Release Gates

[返回索引](README.md) · [Owner 决策](02-owner-decisions.md) · [Journey Ledger](04-journey-ledger.md) · [Evidence 合同](evidence/README.md)

本文件规定执行顺序和机械发布门，不拥有产品语义、旅程状态或实际结果。

本轮由主 Codex 对调查、实现、测试、Git、三服务部署、生产 E2E/A2A、证据与最终验收负责。允许使用 Codex 原生 Multi-Agent 与 subagent 完成有边界的调查、实现、测试和 review；Kimi Code、zCode、Coze、ACP、`agent-delegation` 等外部 Harness 仍禁用，GitHub Issue 只可作为可选审计引用。

## 执行控制模型

| 层 | 唯一职责 | 不能证明 |
|---|---|---|
| Codex Goal | 保存最终 Done、停止条件和整个循环的存续 | 任何单项功能已通过 |
| canonical 文档 + frozen manifest | owner 接受的合同、分母及已接受 verdict 与证据关系 | worker 是否改对代码，或证据的语义含义 |
| GitHub Issue | 可选 Finding 审计引用 | 执行顺序、语义正确、已验收或可发布 |
| 当前 checkout + scoped diff/tests | 变更内容、失败回归和本地行为 | 已部署或生产通过 |
| immutable production evidence | exact commit 下的真实旅程结果 | 未覆盖旅程的结果 |

统一执行循环：主 Codex 在 owner-approved proof order 内基于 live evidence 选择最高杠杆的安全未闭环路径，可把互不冲突的 bounded repo 调查、实现、测试、build 或 review 交给原生 subagent；Gate 编号、分数、Issue 顺序、attempt count 与 timeout 不决定语义 priority。主 Codex 负责整合并独立复核 live wiring、scoped diff 和关键结果，并独占生产 fixture mutation、生产 E2E/A2A、Journey verdict、部署和交付。缺少预存身份、fixture、Session 或仓库内 runtime/build/adapter 时，主 Codex 经受支持路径建立生产合成 fixture，或由主 Codex/subagent 构建 repo runtime；它们都不是 owner gate。

原生 subagent 的失败、超时或无产出是 operational observation；主 Codex 按语义价值选择 retry、reassign、缩小 packet 或接管，不因此终止整个 Goal 或产生 verdict。禁止用 Hive Dynamic Workflow 来验收 Hive 自己，也不增加第二套 semantic supervisor、shadow ledger 或 worker-owned truth。Goal 负责持续性；产品 turn 的 selected runtime LLM 负责任务语义，主 Codex负责验收语义，owner 负责产品/风险裁决；canonical docs 记录已接受合同/verdict；Git/测试负责代码事实，evidence 负责生产事实。

Model Agency Boundary 全程有效：Hive 产品 turn 的 selected runtime LLM 在已认证 frame 内拥有任务 reasoning、semantic judgment、synthesis 与 answer language；RC 循环的主 Codex 拥有验收 decomposition、priority、evidence interpretation、acceptance judgment、Journey/Finding verdict 与 final handoff；owner 拥有产品语义和风险授权裁决。subagent 与机械 checks 都不拥有这些语义权威；checks 只能验证 exact authenticated authority/RLS、manifest、commit、deployment、evidence、arithmetic、resource、recovery 和 machine-contract facts，并只 hold 与缺失 invariant 对应的 effect/release，不得决定 semantic truth、quality、failure、`blocked`、priority、Journey/Finding verdict，或改写/压制模型输出。NPTCR、五护栏与 Evidence Coverage 只聚合主 Codex 基于真实证据已接受的 verdict。

Synthetic identity/fixture 只经受支持、经过认证的 product/control-plane path 建立；禁止 forged claim/JWT/token、直接修改 tenant/role DB 字段、关闭/放宽 RLS，或用 broad/ambient bypass 创造业务 authority。RLS/ACL 在 exact unauthorized ingress/read/write/effect 处 fail closed；denial 只阻断该操作，保留已授权证据、无关推理、获准工具、draft 与 recovery。已登记 read-only deny/not-found/existence probe 必须继续，但不得获取受保护字节或产生效果；若意外返回 protected bytes，立即停止该 lane，不继续读取/传播或写 raw evidence，只留最小脱敏 P0 事故证据。platform/operator 路径必须 server-derived、reason/scope-bound、audited 且不可由 client 自行升级；owner 指令不能把未授权访问变成授权。

## Gate 0：执行前冻结

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
| 3 | 权限与安全：四角色 UI/API、RLS/ACL、revocation/offboarding、secret/PII、injection、replay、approval、delegation escalation | 在完整功能面上做正向可用性 + 负向 no-leak；修安全不能破坏同 tenant novice 主路径 |
| 4 | coherent `D` 全量门、三服务 exact deploy、完整双遍、evidence 与 cleanup | 任一代码/config/schema 修复生成新 `D` 并重跑受影响门 |

该顺序只决定验收与修复优先级，不关闭任何现有 authorization、secret 或 external-effect boundary。若功能检查遇到真实越权/泄漏，立即隔离该 lane；除此之外，权限加固、RLS 扩张和安全评分不得提前阻断无关功能补全。Gate 3 必须基于 Gate 1/2 已真实可用的功能面验证“很好用，然后很安全”。

不设人工 Goal-wide timeout、step cap 或 attempt cap。task-sized per-call/per-attempt timeout、cancel、quota 与 backoff 仍作为资源/生命周期控制；expiry 只结束或恢复当前 attempt，不参与产品语义判断。只要仍有安全的 in-scope 工作，Goal 继续，不缩小分母或降低门槛。

## Finding 处理链

```text
frozen journey + exact code/data/persona
  -> novice UI reproduction
  -> earliest wrong state + seven-atom breakpoint
  -> live-entry wiring/path proof
  -> production-shaped failing regression
  -> repair one authoritative contract/root cause
  -> focused + cross-domain + full + real PG + build/i18n/a11y
  -> atomic commit
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
3. Codex 复核 scoped diff、live wiring 和测试后才提交。跨层合同先固定权威层，再消费前端，不允许半个合同进入候选。
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

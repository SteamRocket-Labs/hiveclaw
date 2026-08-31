---
document_id: weekend-rc-2026-08-30-runbook
owner: Codex
status: active
authority: canonical-execution-and-release-gate
last_reviewed: 2026-08-31
source_commit: bf94b76a1706510daf2d11c4e98fd5051f23f28f
verification_status: owner-corrected-single-codex-execution-contract
---

# Single-Codex Runbook 与 Release Gates

[返回索引](README.md) · [Owner 决策](02-owner-decisions.md) · [Journey Ledger](04-journey-ledger.md) · [Evidence 合同](evidence/README.md)

本文件规定执行顺序和机械发布门，不拥有产品语义、旅程状态或实际结果。

本轮由一个 Codex 在当前 checkout 内完成调查、实现、测试、Git、三服务部署、生产 E2E、证据与验收。Kimi Code、zCode、Coze、ACP、agent-delegation、subagent 和并行 Codex 任务均禁用；GitHub Issue 只可作为可选审计引用。

## 执行控制模型

| 层 | 唯一职责 | 不能证明 |
|---|---|---|
| Codex Goal | 保存最终 Done、停止条件和整个循环的存续 | 任何单项功能已通过 |
| canonical 文档 + frozen manifest | North Star、分母、finding、verdict 与证据关系 | worker 是否真的改对代码 |
| GitHub Issue | 可选 Finding 审计引用 | 执行顺序、语义正确、已验收或可发布 |
| 当前 checkout + scoped diff/tests | 变更内容、失败回归和本地行为 | 已部署或生产通过 |
| immutable production evidence | exact commit 下的真实旅程结果 | 未覆盖旅程的结果 |

单一执行循环：Codex 从 frozen journey 选择最高价值的未闭环路径，沿真实入口复现最早错误，修共享根因，跑 failing-first 与相关全量门，review scoped diff，形成 coherent application batch `D`，push 后把同一 `D` 部署到三服务，再完成 signed-in pass 1、hard-reload/recovery pass 2、权限负向、evidence 与 cleanup。缺少预存身份、fixture、Session 或仓库内 runtime/build/adapter 时由 Codex 创建或构建，不是停止条件。

禁止用 Hive Dynamic Workflow 来验收 Hive 自己，也不增加第二套 semantic supervisor、shadow ledger 或 worker-owned truth。Goal 负责持续性，canonical docs 负责语义，Git/测试负责代码事实，evidence 负责生产事实。

## Gate 0：执行前冻结

- 锁定 checkout、production exact commit、三个 Railway service deployment、账号/角色和数据版本。
- 确认 selected model/provider、semantic runner、Channel、Local Agent 当前可用性。
- 核对已冻结 production journey manifest 的 Git blob/hash；96 条旅程不得因执行结果变化而删除或合并。
- 登记合成资产、预期外部效果、成本/时延测量点和 cleanup。
- 读取 [02-owner-decisions.md](02-owner-decisions.md)；未授权的 credential、DDL、删除、邀请和生产效果不执行。
- 仓库内 semantic runner/build/adapter 缺失时直接构建；外部 provider/服务不可用时验证 typed unavailable、审计、恢复提示和无关能力保留，只暂停外部成功断言。

## 单线优先顺序

| 顺序 | 主线 | Gate |
|---:|---|---|
| 1 | Gate 0、manifest bindings、单 Agent / Session / commands | 单 Agent 失败先修基础，不以控制面掩盖 |
| 2 | Memory/Growth、HR、Knowledge、权限与 offboarding | 真实消费、纵向收益、多格式/引用和四角色负向成立 |
| 3 | Subagent、Team、Workflow、A2A、Automation、Hook/Skill/MCP/Local Agent、安全 | 每类走独立真实入口、失败恢复与最终消费 |
| 4 | coherent `D` 全量门、三服务 exact deploy、完整双遍、evidence 与 cleanup | 任一代码/config/schema 修复生成新 `D` 并重跑受影响门 |

超时、尝试次数或缺 fixture 不参与产品语义判断；只要仍有安全的 in-scope 工作，Goal 继续，不缩小分母或降低门槛。

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
```

该工具只拥有 manifest/evidence/deployment exact facts 和算术，不拥有 Journey、Finding、产品质量或最终语义 verdict。

## 本地与 CI Release Gate

- frontend tests 全绿；TypeScript、i18n、build、bundle budget、a11y 通过。
- backend full tests 全绿；Docker-on/真 PostgreSQL 类型没有整类 skip。
- 变更路径 Ruff check/format 与 `git diff --check` 通过。
- migration upgrade/downgrade/retry-safe、legacy backfill 与 rollback 在适用时通过。
- 每个 bug 有 failing-first 回归，且测试走 production live entry，不用 fake 掩盖 wiring。
- 无 raw internal payload/ID/provider private prose 泄漏到普通用户 DOM。
- 结构测试只能验证 exact fact；semantic quality 由真实任务、外部判据和 owner/独立 review 决定。

## 生产 Gate

- backend、backend-api、frontend 来自同一 exact committed archive，Railway 全部 `SUCCESS`。
- 公共 backend `/api/health` 与 frontend `/` 成功；backend-api 以 exact deployment status 证明 freshness。
- runtime worker、stream forwarder、相关 daemon、RLS、sandbox/connector 只按其真实合同检查；health 不能替产品旅程。
- 全部冻结旅程在同一 final commit 连续两遍 clean pass。
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

1. exact 下一效果会操作真实非合成数据、其他 tenant，或向未登记的真实外部对象发送邀请/消息/文件。
2. 需要购买/充值、轮换或暴露真实 provider credential、组织 secret 或真实用户密码。
3. 需要无安全恢复路径的不可逆生产数据删除/迁移，或发现跨租户/凭据泄漏与数据破坏风险。
4. 修复要求改变 owner 已接受且当前源码/North Star 无法裁决的产品语义，或违反法律/平台安全政策。

合成登录、角色/grant、lab Session、Local Agent re-pair/token/binding、仓库 runtime build、registered synthetic cleanup 不属于停止条件。同一路径重复失败触发更深根因调查，不机械终止整个 Goal。

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

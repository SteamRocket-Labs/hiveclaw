---
document_id: weekend-rc-2026-08-30-runbook
owner: Codex
status: active
authority: canonical-execution-and-release-gate
last_reviewed: 2026-08-30
source_commit: 228682e5
verification_status: execution-control-and-skill-boundary-approved-manifest-review-pending
---

# 12 小时 Runbook 与 Release Gates

[返回索引](README.md) · [Owner 决策](02-owner-decisions.md) · [Journey Ledger](04-journey-ledger.md) · [Evidence 合同](evidence/README.md)

本文件规定执行顺序和机械发布门，不拥有产品语义、旅程状态或实际结果。

当前安装的 `agent-delegation` Skill 是 delegation mechanics 的唯一权威；本 Runbook 只增加 Hive 的角色分工、Finding 前置条件和验收门，不复制或改写 Skill 的授权继承、chain、permission、timeout、receipt 与恢复语义。两者冲突时，Skill 管派发协议，本目录管产品 acceptance。

## 执行控制模型

| 层 | 唯一职责 | 不能证明 |
|---|---|---|
| Codex Goal | 保存最终 Done、停止条件和整个循环的存续 | 任何单项功能已通过 |
| canonical 文档 + frozen manifest | North Star、分母、finding、verdict 与证据关系 | worker 是否真的改对代码 |
| GitHub Issue | 一个 fresh finding 的 bounded work packet 和队列状态 | 语义正确、已验收或可发布 |
| isolated Git worktree + diff/tests | 变更内容、失败回归和本地行为 | 已部署或生产通过 |
| `agent-delegate` receipt | 调用目标、cwd、timeout、权限、事件和 worker 自报结果 | live wiring、测试真实性或最终 acceptance |
| immutable production evidence | exact commit 下的真实旅程结果 | 未覆盖旅程的结果 |

角色边界：Codex 是唯一 controller、dispatcher、independent reviewer、integrator、canonical-doc writer 和 signed-in E2E verifier；Kimi Code 是 frontend worker；zCode 是 backend worker。worker 不关闭 Issue，不修改 verdict/evidence，不 commit/push/deploy，也不访问生产、凭据或不可逆效果。

每个 worker 调用必须满足：

1. Codex 先 fresh reproduce，并把 Finding ID、Journey ID、exact base commit、最早错误状态、目标、included/excluded scope、验收命令和失败探针写入 Issue。
2. 从 exact base 创建单 Issue 隔离 worktree，按当前 Skill 发起 fixed-cwd、finite-timeout 的无状态 packet，不复用 persistent ACP session。研究/只读 smoke 使用 `approve-reads`；owner 已授权的本地实现包才可用 `approve-all` 和必要的 `--terminal`，并必须用 `--authorization-note` 写明“仅限该 finding 的本地编辑与测试”。
3. `cwd` 只是上下文，不是 OS sandbox。Delegation envelope 必须把 commit/push/network publish/deploy/production/credential/billing/destructive/irreversible effects 列为未下放的 commit gates；worker 可完成无副作用准备，但必须停在这些效果前。需要更强隔离而当前 host 无法提供时，不派包。
4. 最多并行两个互不重叠的 packet：一个 Kimi frontend、一个 zCode backend。跨层 contract 变更按权威层先后串行，禁止并发修改同一协议或文件。
5. 完整 events/stderr/result 留在 receipt 目录；父上下文只消费目标、exit/timeout、changed paths、tests、risks 和 blocker 摘要。`exit=0` 只表示 transport 成功。
6. Codex 直接检查当前源码、Git diff、live-entry wiring、failing-first test 和完整相关 gate；失败则创建引用旧 diff/review 事实的新 correction packet，不把旧 worker 对话继续带入。
7. Codex 复核通过后才原子集成、更新 Finding/Status/Issue；生产 Gate 通过后才允许 `Closed`。下一轮从 canonical docs 重新派发。

禁止用 Hive Dynamic Workflow 来验收 Hive 自己，也不增加第二套 semantic supervisor、shadow ledger 或 worker-owned truth。Goal 负责持续性，Issue 负责排队，文档负责语义，Git/测试负责代码事实，evidence 负责生产事实。

## Gate 0：执行前冻结

- 锁定 checkout、production exact commit、三个 Railway service deployment、账号/角色和数据版本。
- 确认 selected model/provider、semantic runner、Channel、Local Agent 当前可用性。
- 冻结 production journey manifest；所有 Candidate 展开为可独立计分的旅程。
- 登记合成资产、预期外部效果、成本/时延测量点和 cleanup。
- 读取 [02-owner-decisions.md](02-owner-decisions.md)；未授权的 credential、DDL、删除、邀请和生产效果不执行。
- semantic runner 不可用时立即记录 `BLOCKED_PRECONDITION`，不跑伪语义验收。

## 12 小时受控排程

| 时间窗 | 主线 | Stop Gate |
|---:|---|---|
| 0:00–0:30 | Gate 0 与 semantic runner go/no-go | 无真实 runner 时记录 blocker 并停止相关语义分支 |
| 0:30–2:00 | 冻结 manifest + 单 Agent North Star | 单 Agent 失败先修基础，不进入多 Agent 装饰 |
| 2:00–4:00 | Session、20 commands、Agent rail/AgentDetail；独立前后端 finding 可双 worker 并行 | 量化 streaming/reload、target panel、角色与 50+ Agent scale |
| 4:00–6:00 | HR、Growth、三层知识、权限、offboarding | 创建/首任务、纵向收益、多格式/引用、权限负向、数据政策 |
| 6:00–8:00 | Collaboration、Automation、Hook/Skill/MCP、安全 | 五类协作分别跑；trigger/channel/local；Trust lifecycle；对抗与恢复；8:00 code freeze |
| 8:00–9:15 | 最终本地/CI gate | 任一修复使受影响 gate 和 code-freeze 重新计时 |
| 9:15–9:45 | 原子交付与三服务 exact deploy | 三服务未同提交 `SUCCESS` 不进入最终计分 |
| 9:45–11:45 | frozen manifest signed-in 双遍 | 同一 final commit；hard reload、断线、retry/cancel、权限负向、artifact、notification、console/log、latency/cost |
| 11:45–12:00 | 最终 Verdict | NPTCR、五护栏、Evidence Coverage、七原子、ZKD、Excluded/Blocked、cleanup、下一动作 |

并行只减少等待，不改变 finding、review、code freeze 或生产证明顺序。时间不够时输出“RC 未成立 + 精确剩余 + 最便宜恢复动作”，不得缩小分母或降低门槛。

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
  -> backend/backend-api/frontend exact same commit deploy
  -> signed-in clean pass 1 + hard reload
  -> signed-in clean pass 2 + fault/recovery + negative authority
  -> evidence file + ledger Closed, or truthful BLOCKED_PRECONDITION
```

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

达到 95 仍不能抵消任何七原子缺失、护栏失败、开放 blocker、版本漂移或 fake live boundary。

## 停止并回到 owner

1. 需要生产 DDL、不可逆迁移、删除数据、创建外部账号或发送外部邀请。
2. 发现跨租户泄漏、凭据泄漏或数据破坏风险。
3. 修复要求改变已接受产品语义，而不是恢复现有合同。
4. 同一路径连续三次修复暴露不同底层问题；停止补丁，提交根因报告。
5. 需要充值、重新登录、替换模型/bridge credential。

## 最终交付

- exact code/tests/migration/backfill/rollback；
- three-service deployment identity；
- production journey evidence + screenshot matrix；
- employee/admin/operator audience verdict；
- J1/J2/J3/J4 growth and bakeoff；
- latency/token/cost/cache/intervention/model fidelity；
- NPTCR、五护栏、Evidence Coverage、七原子、ZKD、Excluded/Blocked、cleanup ledger；
- 当前状态中的唯一下一动作。

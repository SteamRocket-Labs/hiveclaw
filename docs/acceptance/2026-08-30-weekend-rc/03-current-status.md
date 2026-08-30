---
document_id: weekend-rc-2026-08-30-current-status
owner: Codex
status: active
authority: canonical-working-state
last_reviewed: 2026-08-30
source_commit: 45340a3a
verification_status: execution-control-structurally-verified-remote-baseline-pending
---

# 当前状态与唯一下一动作

[返回索引](README.md) · [旅程账本](04-journey-ledger.md) · [Findings](05-findings.md) · [Runbook](06-runbook-and-release-gates.md)

> 文档重构已完成；owner 已批准执行控制模型并启动唯一 Goal。产品旅程尚未开始，production manifest 尚未冻结。

## 当前目标与可观察 Done

完成 Weekend RC 整体验收与修复闭环：Codex 按冻结旅程持续执行“真实 E2E → finding → bounded worker packet → 独立 review → 集成 → 重验 → 更新文档”，Kimi Code 只负责前端，zCode 只负责后端，直到全部 in-scope 旅程 `Closed loop`。

可观察 Done：NPTCR=100%；五条不可平均护栏全部通过；Evidence Coverage ≥95 且不抵消七原子缺口；Zero Known Defects；backend/backend-api/frontend 同一 exact commit `SUCCESS`；frozen manifest signed-in 双遍、故障恢复和权限负向全部通过；合成资产 cleanup 完成。

## 当前事实快照

| 事实 | 当前值 | 证据等级 |
|---|---|---|
| checkout | `HEAD/main/origin/main = 45340a3a` | 本轮本地已核验 |
| active Goal | Codex Goal `01a05189-c369-75f0-a720-ffe16136644f`，状态 `active` | 本轮 Goal API 已核验 |
| execution roles | Codex 总控/验收；Kimi Code 前端；zCode 后端 | owner 2026-08-30 已批准，见 DEC-008～DEC-011 |
| delegation readiness | `agent-delegate doctor = ok`；Kimi `1.49.0`，zCode ACP `0.1.0+ultra.zcode.0.16.5` | 本轮本地已核验；只证明 registry/version，不证明真实 task |
| 旧 WIP archive | 原 5,685 行、约 1.29 MB，完整迁移并增加 archive warning | 本轮本地已核验 |
| 当前 production 业务提交 | `eb61d468221aa22a4f22c1d96353baadef3b51e6` | 从旧账当前快照迁移；本次未重新查询 Railway |
| Session terminal/failure | 旧账记录 §7.77/§7.78 已部署并完成 signed-in 双遍 | 历史证据；不外推整体 RC |
| executable CI manifest | `acceptance/atomic_user_journeys.v1.json`，J-01～J-15，声明受控 external fakes | 本轮源码已核验 |
| production NPTCR manifest | 尚未创建或冻结 | 明确 Not Done |

## 当前产品总判断

| 验收域 | 当前判断 | 仍需证明 |
|---|---|---|
| Git / Production | 基线健康，不等于 RC 完成 | 后续代码变更后 exact same commit 三服务部署和全旅程重验 |
| Session 核心终局 | `Closed loop`（只限旧账已证明的 terminal/failure 与 reload 子集） | 20 commands、复杂协作、全局视觉/a11y |
| 整体前端 | `Partial loop` | 员工/公司后台/operator 的 state screenshot matrix、密度、双主题、窄屏、键盘、Agent rail 规模 |
| Rewind / Resume / Fork / Rollback | 旧账标 `Closed loop` | 只在 fresh journey 复现新错误时重开 |
| Personal / Company KB | `Partial loop` | 多格式、多入口、权限负向、失败恢复、Agent/Personal→Company/后台路径统一生产矩阵 |
| Agent Memory | `Partial loop` | coverage、T3 consumption、fresh Session recall |
| Growth / Eval | `Partial loop` | J1/J2 longitudinal reuse、owner feedback/rework、真实 J4 FreeCode/Hermes bakeoff |
| HR 创建 Agent | `Partial loop` | Agent→HR 与普通员工 fresh 双遍、被创建 Agent 首任务 |
| Plan Mode | `Partial loop` | model-authored plan、revise/reject/cancel、exact-version approve、execute/recover |
| Sub-agent / Team | `Partial loop` | 两类真实语义任务双遍、成员失败与父结果 UI 消费 |
| Dynamic / Fixed Workflow | `Partial loop` | 各自从真实产品入口到结果双遍 |
| A2A | `Partial loop` | async continuation、A→B→C nested、长结果/artifact、固定路径 UI |
| Commands / AgentDetail | `Breakpoint` 候选 | 20 commands 逐条 UI↔API↔runtime；四个候选断点先复现 |
| 权限与角色 UI | `Breakpoint` | employee/admin/platform/operator 正负向矩阵 |
| Offboarding | `Partial loop` | signed-in 停用/重放/并发、通知、数据保留/导出/删除政策 |
| Trigger / Channel / Local Agent | `Breakpoint` aggregate | create→fire→execute→deliver→notify→retry/cancel/audit；offline/reconnect/revoke |
| Hooks / Skill / MCP | `Breakpoint` aggregate | lifecycle、Trust Review、真实调用、update/revoke/auth expiry |
| Artifact / async return / model-cost | `Breakpoint` aggregate | preview/download/version/ACL、deep-link、selected-model fidelity、latency/token/cost/cache |

## 已知外部前置条件

- DeepSeek 曾返回 `HTTP 402 Insufficient Balance`。
- MiniMax fresh/既有调用曾长时间无成功终局或 rate-limit。
- Hive Connect Local Agent 曾显示 offline，CLI/daemon 曾返回 `HTTP 401 Invalid bridge token`。

这些是需要重新验证的历史 blocker，不是本次文档重构的修复对象。不得自动 re-login、充值、换 credential 或盲重试。

## 本次已完成

- owner 已授权文档组重构。
- 已加载并应用 `task-state-with-files` 与 `agents-best-practices`。
- 已确认现有 CI manifest 与未来 production manifest 必须分账。
- 已建立索引、North Star、owner decisions、current status、journey ledger、findings、runbook、六个 domain 和 evidence contract。
- 已把旧总账完整移入 `archive/legacy-ledger-2026-08-25.md` 并加入显著历史警告；旧路径缩为 21 行兼容跳转。
- 已把 `work/task-state.ref` 切换到本文件，恢复解析成功。
- 已更新 `docs/README.md`，并增加纯结构架构测试。
- owner 已批准一个 Goal + GitHub Issue work packet + stateless `agent-delegate` + isolated worktree 的执行模型。
- 已启动唯一 Goal；已明确 Codex/Kimi/zCode 权限边界、独立验收和上下文隔离合同。
- `agent-delegate doctor --json` 当前为 `ok`；Kimi/zCode 的真实 no-write provider smoke 仍待执行。
- 已新增 Weekend RC Issue form，并修正现有 Issue template 的旧仓库链接；尚未写入远程 GitHub。

## 最近验证

- Weekend document group + existing atomic manifest architecture tests：**13 passed**。
- 新 Python test：Ruff check passed；Ruff format check passed。
- required files、metadata、Markdown links、balanced fences、J-01～J-15、PJ-01～PJ-35、active line budgets 全部通过。
- task-state resolve：`docs/acceptance/2026-08-30-weekend-rc/03-current-status.md`。
- active 文档合计约 1,300 行，单文件最大 117 行；历史 5,000+ 行内容只存在 archive。
- `git diff --check`：通过。
- 当前 checkout 再核验：`HEAD = main = origin/main = 45340a3a`。
- Weekend document group + existing atomic manifest architecture tests：**15 passed**（包含 execution-control 与 Issue form 新合同）。
- 新 Python test：Ruff check/format passed；全部 Issue Form YAML 可解析；task-state resolve 与 `git diff --check` passed。

## 唯一下一动作

运行 execution-control 文档结构、链接、Ruff 和 diff 验证；通过后原子提交并 push 验收基线，建立 GitHub RC milestone/labels/umbrella Issue，再从同一 exact commit 分别执行 Kimi/zCode 的隔离 no-write smoke。随后 owner 过稿 PDEC-001～PDEC-006 和候选分母，冻结 production manifest，进入 Gate 0。

## Not Done / Do Not Redo

- 未冻结 production journey manifest，未计算 NPTCR 或 Evidence Coverage Score。
- 未执行真实 Kimi/zCode task、新业务代码/UI 修改、测试全量、commit、push、远程 GitHub 写入、Railway 部署或生产写入。
- 未重新验证 provider、Hive Connect 或生产三服务状态。
- 不触碰 pre-existing `.ultra/.runtime/compact-snapshot.md`、`bp-kingdee/`、`output/`、root `package*.json`、`tmp/pdfs/` 等用户工作树内容。
- 不把 archive 中某个历史 `PASS` 自动迁移成当前 aggregate `Closed loop`。

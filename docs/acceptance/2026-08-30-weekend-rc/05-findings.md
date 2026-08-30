---
document_id: weekend-rc-2026-08-30-findings
owner: Codex
status: active
authority: canonical-active-finding-ledger
last_reviewed: 2026-08-30
source_commit: 56ec5dd0
verification_status: one-p1-reproduced-and-ready-for-backend-work-packet
---

# 当前 Findings 与 Blockers

[返回索引](README.md) · [当前状态](03-current-status.md) · [Journey Ledger](04-journey-ledger.md)

历史包、旧 PASS 和已被取代的根因只保留在 [archive](archive/README.md)。本文件只接纳当前仍需处理的 finding；旧账内容若没有 fresh reproduction，不自动成为当前缺陷。

## Finding 状态

`Observed` → `Reproduced` → `Fix Candidate` → `Verified` → `Closed`。Review 失败使用 `Review Failed`；确认不属于范围且不是现有契约缺陷时才用 `Excluded`。

只有 `Reproduced` 且已记录最早错误状态的 finding 才能生成修复 Issue。Issue 必须回链本文件的 finding ID 和冻结 Journey ID；worker 回执、PR、CI 或 Issue closed 都不能自动推进 finding 状态。

## 当前候选 findings

### 已复现、允许派发

| ID | 状态 | Severity | Journey | 最早错误状态 | 当前根因边界 | 下一动作 |
|---|---|---:|---|---|---|---|
| SESSION-CONTEXT-001 | Reproduced | P1 | P01-MAIN / P02-STREAM | 同一 Session 第二轮 provider request 未获得上一轮 user/assistant 语义历史，模型明确称“这是本会话我收到的第一条消息” | canonical Session V2 transcript 保存了两轮完整输入与 `assistant_text.snapshot`；兼容 `/messages` 只有 10 条 system/debug，runtime `_load_runtime_context` 仍从 `ChatMessage` 组装 provider conversation。应恢复一个 canonical、可重放、可 compact/fork 的历史输入合同，而不是仅让 UI 看起来连续 | 建 backend Issue；zCode 在隔离 worktree 实现 production-shaped failing-first regression 与最小权威根因修复；Codex 独立 review/retest |

#### SESSION-CONTEXT-001 复现证据

- production application：`eb61d468221aa22a4f22c1d96353baadef3b51e6`；实验 Session：`59257e7a-960b-459a-9652-2ff39be117ee`。
- 第一轮 run `2fa2f887-b76e-556c-99c8-3a814c37f27b` 正确生成 No-Go 判断及可观察恢复证据；第二轮 run `58b222f2-b52b-5cb1-b5a1-f657ced4222a` 在相同 Session 审计上一轮时否认存在上一轮。
- `GET .../transcript?limit=1000&schema_version=2` 返回 sequence `1..635`，包含两组 `human_input.accepted`、`assistant_text.snapshot`、`run.completed`；第一轮 prompt 和完整 assistant snapshot 均可从 canonical payload 读取。
- `GET .../messages` 返回 10 行，全部为 `system`（model route、memory degradation、context window、provider ledger），user/assistant 为 0。
- current checkout wiring：`web_chat_runtime._load_runtime_context()` 查询 `ChatMessage`；`web_chat_run_orchestrator` 把该 history 转为 `state.conversation`；Session V2 当前输入另由 `bind_round_inputs()` 注入。因此第二轮只有本轮 input，旧轮语义历史未进入 provider request。
- 这不是“Memory degraded”本身：Memory 事件明确说明可继续，且同一 Session 原始对话历史属于 Session lifecycle，不应依赖 Personal/Agent Memory 检索。
- 本次证据只建立 P1 与最早断点，不是最终根因修复设计，也不构成 P01/P02 PASS。

### 尚待 fresh reproduction

| ID | 状态 | Severity | Journey | 观察/假设 | 下一证明动作 |
|---|---|---:|---|---|---|
| UI-CMD-001 | Observed | P2 candidate | PJ-03 | `/skill` 与 `/agent` 可能返回目标 subview，但 Agent extensions/selector 未消费目标，仍停在默认 catalog | signed-in UI 分别输入命令，记录 URL、selected tab、目标对象和 reload |
| UI-CMD-002 | Observed | P2 candidate | PJ-03 | `/workflow` 可能只切换 tab，没有打开指定 draft/preview | signed-in fresh draft 逐字段复现，追踪 `ui_action → route → consumer` |
| UI-CMD-003 | Observed | P2 candidate | PJ-03 | `/context`、`/usage`、`/permissions` 可能缺少目标 panel，最终只显示 “Command completed” 或内部 ID | 逐命令复现 success/failure/cancel/reload，确认可读 panel 是否存在 |
| KNOWLEDGE-UI-001 | Observed | P1/P2 candidate | PJ-09/PJ-10/PJ-11 | Agent Knowledge 消费 `entries + pages`，可能把 Agent Memory、Personal KB、Company KB 混成一个不诚实状态 | 从 employee Agent Detail 逐层核对来源、owner、authority 和空/拒绝/不可用状态 |

以上均为候选，未完成 fresh reproduction 前不得修改代码或宣称根因。

## 外部前置条件，不归类为产品 finding

| ID | 状态 | 历史事实 | 允许的当前动作 |
|---|---|---|---|
| BLOCKER-MODEL-001 | PARTIAL_PRECONDITION | GLM production 调用成功；MiniMax/DeepSeek exact binding 已确认但尚未 live probe | 各做一次 bounded semantic call；充值或 credential change 需 action-time 授权 |
| BLOCKER-BRIDGE-001 | BLOCKED_PRECONDITION | Hive Connect daemon running，但 `hive-connect status` fresh 返回 `401 Invalid bridge token`，UI linked `0` / offline | 保留 blocker；re-login/token replacement 需 action-time 授权 |

外部 blocker 必须在执行窗重新验证；恢复后回到真实语义路径，不以旧失败永久阻断。

## 严重度

| 级别 | 定义 |
|---|---|
| P0 | 越权、跨租户泄漏、数据破坏、不可逆错误、全局不可用 |
| P1 | 核心旅程阻断、永久非终态、假成功、证据丢失、不可恢复 |
| P2 | 外部测试者可见且显著破坏理解或信任 |
| P3 | 不阻断任务的小型一致性或美观问题；不能自动延期 |

## Finding 关闭合同

每个 finding 必须链接：冻结 journey、最早错误状态、live-entry wiring proof、production-shaped failing regression、最小共享根因修复、focused/cross-domain/full/真 PG/build gates、exact commit、三服务部署、signed-in pass 1/2、fault/recovery、authority negative 和 rollback。

禁止只隐藏 UI、字符串猜语义、放宽断言、用 fake pin 孤儿路径、在失败 provider 上盲重试，或把一个能力的证据外推给另一个能力。

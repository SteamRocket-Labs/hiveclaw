---
document_id: weekend-rc-domain-single-agent-session
owner: Rocky / Codex
status: active
authority: canonical-domain-acceptance
last_reviewed: 2026-08-30
source_commit: 45340a3a
verification_status: acceptance-spec-not-execution-result
---

# Single Agent 与 Session 验收标准

[返回索引](../README.md) · [Journey Ledger](../04-journey-ledger.md) · [Frontend](frontend-and-product-consumption.md)

## 产品目标

普通员工能在一个 Session 中让单个数字员工完成真实开放任务：理解输入、规划、使用获准能力、解释过程、请求必要决定、交付可用结果，并在刷新、断线、失败、压缩、回退和分叉后保持事实与权威一致。

单 Agent 是所有多 Agent 和控制面能力的前置 Gate。

## CCPlus 生命周期

- [ ] Agent definition、identity、`soul.md` 与当前组织/用户权威可追溯。
- [ ] context assembly 提供完整授权证据或无损可发现引用；Personal/Company KB 仍 tool-only。
- [ ] 用户 prompt 经唯一 accepted input 写入 transcript/T0；重复提交幂等。
- [ ] model loop 使用用户选择的 model/provider，不按自然语言静默降级或删工具。
- [ ] tool search/load/call/result 每次都有 typed lifecycle、权限、receipt 和失败出口。
- [ ] Hook blocking/observe-only 发生在真实边界，不用平台语义替代模型。
- [ ] Work Ledger/Todo/Progress 是 Agent authored cognitive bookkeeping，写 todo 不自动执行。
- [ ] Plan、approval、effect 彼此分离；批准绑定 exact version。
- [ ] compaction 是 model-led complete coverage；失败不静默裁剪。
- [ ] stop/cancel/terminal/failure/resume/close 有 durable evidence 和可达恢复。
- [ ] final answer 保持 model-authored；平台只做 exact unauthorized-secret protection。

## Session 流式输出

| 阶段 | 普通员工必须看到 | 不得出现 |
|---|---|---|
| accepted | 输入已接收、可取消；首个反馈及时 | 空白等待、重复 user message |
| queued/starting | 一条稳定过程卡和当前可解释状态 | 每个 event 一张卡、raw RuntimeTask ID |
| thinking/working | 有意义的步骤、工具类别、必要决定 | 私有 reasoning、provider jargon、schema/payload |
| tool/approval | 动作目的、影响、批准范围、成功/失败和恢复 | 模型自批、自然语言充当 grant |
| streaming answer | 增量正文稳定追加，Markdown 不闪回或重排 | live/canonical 两份 answer、光标跳动 |
| completed | 唯一 final、耗时、deliverable、引用、下一动作 | terminal 后第二张“仍在处理”卡 |
| failed/unknown | typed failure、是否可重试、是否需用户决定 | fixed platform prose 冒充模型结论、自动 replay ambiguous send |
| reload/resume | 与终局/过程同构，ID 和内容不重复 | 历史 run 覆盖当前 header 或 active state |

必须测量 platform acceptance feedback、provider TTFT、first visible output、total time、reload convergence、duplicate/flicker count 和人工介入。

## Session 恢复与分叉

- [ ] `/resume` 恢复同一权威状态，不重复 prompt/run/effect。
- [ ] `/rewind` 先预览影响，确认后建立可恢复新 head，保留原 evidence。
- [ ] `/branch` 从选定 durable event 建 child，parent 不变，lineage 可读。
- [ ] Retry 在 provider final 前失败时从 canonical user event 建 `edit` branch；不猜 assistant anchor。
- [ ] `/clear` 明确清理范围，不删除 durable audit。
- [ ] `/compact` 保留覆盖 ledger、active plan/approval/skills/changed artifacts。
- [ ] WebSocket disconnect、rolling deploy、transient read failure 不取消 run 或清空 durable timeline。
- [ ] stale/duplicate/out-of-order events 不能创建第二份语义记录。

## Plan / Goal / Task / Ledger

- [ ] Plan 内容由模型撰写，包含事实、判断、范围、验证、风险与回滚；平台不补写语义。
- [ ] clarification、revise、reject、cancel 是一等状态，每个非终态都有出口。
- [ ] approve 绑定 exact plan version/hash；未确认 autonomous/high-risk work 零副作用。
- [ ] confirmed handoff 进入真实 executor；execution、observation、recovery 回到同一 Session。
- [ ] Goal 有 objective、done condition、budget、stop、progress 和 terminal evidence；counter 不等于完成。
- [ ] Task/Todo 有 dependency 和 recovery；创建账本不启动执行。

## 20 条斜杠命令矩阵

每条都要验证 discover/help、arguments/autocomplete、keyboard submit、`ui_action`、API/runtime、authority/approval、success/failure/cancel、reload/resume、i18n/a11y 和 novice-safe expression。

| ID | 命令 | 用户态 Done |
|---|---|---|
| CMD-01 | `/plan` | 打开真实 Plan 面；model-authored、clarify/revise/reject/exact approve，批准前零 effect |
| CMD-02 | `/resume` | 列出并恢复同一权威对象，不重复 input/run/effect |
| CMD-03 | `/rewind` | 预览 checkpoint/影响；确认后新 head 可恢复，原证据保留 |
| CMD-04 | `/branch` | 从选定 turn 建 child；parent 不变，lineage 可见，标题无工程后缀 |
| CMD-05 | `/clear` | 明确清理范围、取消与恢复；不越权删除 durable audit |
| CMD-06 | `/compact` | model-led、覆盖可见、失败不静默截断 |
| CMD-07 | `/context` | 打开可读 context 来源、覆盖、权限面；不只显示 completed/raw ID |
| CMD-08 | `/permissions` | 打开有效权限、approval 和受限动作；denied/unavailable 有下一步 |
| CMD-09 | `/steer` | 只定向当前 active run，输入恰一；非 active typed reject/rollover |
| CMD-10 | `/usage` | 当前 Session/Agent 的 token/cost/budget 来源准确，角色可见性正确 |
| CMD-11 | `/agent` | 定位目标 Agent selector/detail，不只打开默认 catalog |
| CMD-12 | `/goal` | 打开 durable goal，显示 done/budget/stop/progress |
| CMD-13 | `/task` | 打开 Work Ledger/Todo；dependency/recovery 可见，不自动执行 |
| CMD-14 | `/team` | 打开当前 Team/创建入口；lead/member/status/result 与 Sub-agent/A2A 分开 |
| CMD-15 | `/schedule` | 创建/编辑 schedule；timezone/trigger/owner/permission/next run/pause-resume 明确 |
| CMD-16 | `/once` | 创建一次性 trigger；exact time/owner/idempotency/cancel/result 可见 |
| CMD-17 | `/loop` | 目标、预算、stop、recovery 的 bounded loop；不做隐式无限 polling |
| CMD-18 | `/workflow` | 打开对应 draft/preview；exact version confirm 后运行并显示 step tree |
| CMD-19 | `/skill` | 定位 Skill catalog/detail/load；load 只加 guidance，不授予 effect 权限 |
| CMD-20 | `/mcp` | 显示 connection/capabilities/auth scope/error/revoke；不泄露 token/secret |

## Authority 与故障探针

- [ ] branch/resume/steer/compact 不改变企业权限或 principal。
- [ ] denied effect 后，无关推理和获准工具继续。
- [ ] malformed schema 返回 repairable observation，不由平台代写 result。
- [ ] decisive evidence 在最后 chunk 仍被模型看到。
- [ ] duplicate input、late approval、stale revision、cancel/terminal race、worker restart 均幂等。
- [ ] selected model/provider、prompt/tool bundle version、context/cache/token/cost 有权威记录。

## Acceptance

真实开放任务必须有外部硬判据与可用 deliverable；同一 production commit 连续两遍，从 fresh Session 发送到 terminal/reload 全程无需管理员或控制台。任何平台 fake、人工补状态或删除失败旅程都不计通过。

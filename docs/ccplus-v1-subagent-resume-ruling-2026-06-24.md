# CCPlus V1 — D-16 Completed-Subagent-Resume 裁决（Hive-native non-parity）

日期：2026-06-24
状态：V1 显式裁决（消除"既不实现也不裁定"的静默缺口）
上位契约：`docs/ccplus-north-star-contract-2026-06-24.md`
执行入口：`docs/ccplus-v1-deep-verification-reconciliation-2026-06-24.md`（D-16，§4.3 / 行 102/177）

## 1. 债务

D-16：CC `resumeAgentBackground` 允许重开一个**已完成（terminal）**的 subagent 后台会话、过滤孤儿 tool_use、追加 follow-up 回合继续跑。Hive 的 `continue_agent_session_from_mailbox`（`backend/app/services/agent_session_continuation.py`）对 terminal 状态（`completed/failed/killed/closed/skipped/cancelled`）**直接拒绝**，无法原地重开已完成子会话。

reconciliation 要求：**必须显式支持，或写成有意 Hive-native non-parity 裁决 + 测试**——不允许"既不实现也不裁定"的静默缺口。

## 2. 裁决：Hive-native non-parity（new-spawn-only）

Hive **不**复刻 CC 的"原地重开 terminal 会话"语义，理由如下，均绑定北极星：

1. **密封审计真相不可变（治理优先，L2/L3）**：T0 会话段在 `SESSION_CLOSE` 被 seal、生成 T2 Segment Package 并进入 Memory/Platform Gate 的证据链。原地重开一个已 seal 的 terminal 会话会破坏"sealed = 不可变审计真相"这一企业治理不变量。CC 是单机 CLI，没有这层公司级审计；Hive 作为控制中台必须保持密封性。
2. **CC LAW 仍被满足**：CC 续问语义的**目标**（让一个已结束的子任务能被追加新指令继续推进）在 Hive 中通过**新建一个 durable 子会话**实现，并经 `parent_session_id` / `root_session_id` / RuntimeTask 链接回原谱系——continuation 不丢、谱系可追溯（`SessionGraphV1` 投影把这条链显式呈现）。能力等价，只是承载体是新会话而非复活旧会话。
3. **非阉割**：本地/云端都可用；这不是因为执行底座受限而砍能力，而是用更强的"密封 + 新谱系链接"机制达成同一目标（North Star §3 Hive-native delta）。

因此 D-16 归类为**deliberate Hive-native non-parity**，而非 CC parity 缺口，也不再是静默缺席。

## 3. 落地（已实现，非纯文档）

`continue_agent_session_from_mailbox` 对 terminal 会话的拒绝路径不再是死胡同，而是**显式 new-spawn 重定向**：

- 拒绝事件 `agent_session_message_rejected` 的 metadata 与返回值都带 `resumable=False` + `redirect="spawn_new_session"`，调用方据此新建子会话而非以为消息被静默吞掉。
- 代码锚点：`backend/app/services/agent_session_continuation.py`（terminal 分支）。

## 4. 测试（contract / ruling test，使 `pytest -k subagent_resume` 真命中）

`backend/tests/services/test_subagent_resume_ruling.py`：

- terminal 会话续问 → `ok=False, status=rejected, reason=terminal_agent_session, resumable=False, redirect=spawn_new_session`，并落 `agent_session_message_rejected` 事件（revert 敏感：若去掉裁决字段，断言失败）。
- non-terminal（open）会话续问 → 走正常 append + consume，不被拒绝（证明裁决只作用于 terminal）。

## 5. 边界

- 若未来要做"真正复活 terminal 子会话"，必须设计为 Hive-native 能力（重开时 fork 出新 seal 边界 + 过滤孤儿 tool_use + 审计新回合），不得破坏既有 seal 的不可变性；在那之前，new-spawn-only 是 V1 的正式裁决。
- 本裁决不改变 non-terminal A2A 会话的 resume（那条路径 live 且不受影响）。

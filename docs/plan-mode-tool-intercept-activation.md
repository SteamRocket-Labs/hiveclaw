# Plan Mode Phase 5 — Tool-Intercept → Interactive 激活机制（开放点 A 设计）

> 本文是 `docs/plan-mode-runtime-paradigm.md` 的 **Phase 5 开放点 A** 的聚焦设计稿。
>
> 前序范式文档解决了「规划在主循环、每轮 reminder、统一只读闸门、plan 文件可写」（Phase 1–4B，已 commit `2403086` / `1256bd0` / `5d07d04`）。本文只解决一个被前序明确标注为「机制未给」的开放点：
>
> **当 agent 在一次 live web chat 里调用自主行为工具（如 `set_trigger`）被 PlanModeGate 拦截时，如何不返回一段静态 `needs_plan` 文本了事，而是把它「无缝拽进」interactive Plan Mode，让它在同一个对话循环里继续规划、最后用 `exit_plan_mode` 出确认卡。**
>
> 治理层（`plan_hash` / `validate_confirmation` / `PlanModeGate` fail-closed）一行不动。本文只改「拦截之后怎么引导」，不改「拦截本身」。

---

## 0. TL;DR / 拍板结论

- **方向**：闸门只发信号，kernel 激活状态。✅ 已拍板。
- **关键不变量（本文存在的理由）**：reminder 与只读闸门读的是**两个独立状态源**——
  - 每轮 reminder 注入读 **typed state**：`request.session_context.plan_mode`（`engine.py:1634`）
  - 只读闸门读 **ContextVar**：`interactive_plan_mode_active()`（`tools/service.py:465`）

  kernel 激活时若只写 typed state，就**只有提醒、没有强制只读**——agent 仍能执行写/自主工具，破坏 Plan Mode 最高不变量。**完整闭环必须双写：typed state（喂 reminder）+ arm ContextVar（喂 gate）。** 这与 Phase 1 铁律②同源。
- **完整闭环**：`needs_plan` 信号 → kernel 写 typed state → kernel 为后续 tool calls arm ContextVar → 下一轮 full reminder + 只读 gate 同时生效 → agent 续规划 → `exit_plan_mode` 出卡。
- **边界**：`source == "web"`（或 `channel == "web"`）且有真实 `request.session_context` 才激活；heartbeat / trigger daemon / delegation / task executor 继续走 RPC fallback。
- **feature flag**：`plan_mode.tool_intercept_interactive`，**默认 off**。
- **seed**：分层都给（原始请求 + 被拦动作上下文），但 seed 只是规划上下文，**不是 approval**——确认仍必须走 `exit_plan_mode`。

---

## 1. 问题：双状态源 + 拦截点拿不到 typed state

### 1.1 核实的事实（file:line，2026-06-02 实测）

| # | 事实 | 位置 |
|---|---|---|
| 1 | `ToolRuntimeService.execute()` 只拿 `agent_id` / `user_id` / `session_id`，**不持有 `SessionContext`** | `backend/app/tools/service.py:158` |
| 2 | invoker 只把 `session_id` 字符串传给 `execute_tool`，**不传 typed session** | `backend/app/runtime/invoker.py:739` |
| 3 | 每轮 reminder 注入读 `request.session_context.plan_mode`（**typed state**） | `backend/app/kernel/engine.py:1634` |
| 4 | 只读闸门读 `interactive_plan_mode_active()`（**ContextVar**），不是 typed state | `backend/app/tools/service.py:465` |
| 5 | web chat 已有「写 typed state + metadata mirror」的激活逻辑（显式 Plan Mode 路径） | `backend/app/services/web_chat_runtime.py:408` |
| 6 | runtime session 实际 source/channel 是 **`"web"`**，不是 `"web_chat"` | `backend/app/services/web_chat_broker.py:74,81` |

### 1.2 为什么 ToolRuntimeService / web_chat_runtime 都不是激活点

- **ToolRuntimeService**（拦截发生地）：事实 1 —— 它没有 `SessionContext`，无法写 typed state。让它直接改 session state 等于给工具层一个跨层写权限，职责错位。
- **web_chat_runtime**（外层）：它持有 `session_context`，但激活需要发生在 `invoke_agent` 的**循环中途**（tool 执行之后、下一轮之前）。web_chat 在 `invoke_agent` 外层，拿不到中途的 tool result，无法在循环内切换状态。
- **kernel（engine）**：唯一同时持有 ① tool result（识别信号）② `request.session_context`（写 typed state）③ 下一轮 prompt 注入点 + 后续 tool calls 的执行点（arm ContextVar）的层。**所以激活逻辑属于 kernel。**

---

## 2. 方案：信号 → kernel 双写 + arm → reminder + gate → exit

```text
agent 在 live web chat 调 set_trigger
  └─ ToolRuntimeService.execute
       └─ _plan_mode_gate_block → PlanModeGate.check → needs_plan
            └─ 工具【不执行】(fail-closed 不变)
            └─ 返回的 needs_plan envelope 带 activate_interactive_plan + interactive_plan_seed
  └─ kernel 收到该 tool result，识别信号 (feature flag on 且边界匹配时)：
       ① request.session_context.plan_mode = PlanModeState(active=True, seed...)
       ② request.session_context.metadata["plan_mode"] = state.to_metadata()
       ③ arm ContextVar（set_interactive_plan_mode）供【后续 tool calls】只读 gate 生效
  └─ 下一轮循环：
       ④ engine 读 typed state → 注入 full reminder（首轮 full，含 plan 文件提示若有）
       ⑤ 后续 tool calls 经 _interactive_plan_mode_readonly_block → ContextVar armed → 只读
  └─ agent 续规划（只读勘察）→ exit_plan_mode → awaiting_confirmation → plan card
```

这与现有 `ToolExpansionResult → active_packs` 机制同构（`engine.py:2189`：kernel 已经会根据 tool 执行结果改 `session_context` 并重建 prompt）。激活 Plan Mode 是同一模式的新成员。

---

## 3. 信号契约（`needs_plan` envelope 扩展）

闸门在判定「live chat + flag on」时，在现有 `needs_plan` JSON 上追加：

```json
{
  "status": "needs_plan",
  "activate_interactive_plan": true,
  "interactive_plan_seed": {
    "source": "tool_intercept",
    "action_kind": "create_enabled_trigger",
    "tool_name": "set_trigger",
    "tool_args": { "...": "redacted" },
    "original_request": "<latest user message, truncated>",
    "plan_id": "...",
    "plan_version": 1,
    "plan_hash": "...",
    "plan_json": { "...": "if already materialised" }
  }
}
```

- `activate_interactive_plan` 缺省 / `false` → kernel 不激活（保持今天的静态 `needs_plan` 行为，即 flag off 或非 live chat 的路径）。
- envelope 其余字段（`plan_id` 等）与现有契约保持一致，前端/UI 不受影响。

---

## 4. kernel 激活逻辑（双写 + ContextVar 生命周期）

### 4.1 双写（核心，缺一不可）

```python
# 伪码：kernel 处理 tool result 时
signal = _parse_interactive_plan_signal(tool_result)   # 仅当 flag on 且边界匹配
if signal is not None and request.session_context is not None:
    state = PlanModeState(
        active=True,
        intent_type=signal.intent_type,
        action_kind=signal.action_kind,
        tool_name=signal.tool_name,
        original_request=signal.original_request,
        plan_id=signal.plan_id,
        source="tool_intercept",
        plan_file_path=_provision_plan_file(request, signal),   # 复用 Phase 4B 约定，可选
    )
    request.session_context.plan_mode = state                    # ① 喂 reminder（engine:1634）
    request.session_context.metadata["plan_mode"] = state.to_metadata()  # ② mirror（前端/兼容）
    _arm_plan_mode_context(state)                                # ③ 喂只读 gate（service:465）
```

### 4.2 ContextVar 生命周期（落地需仔细处理，见 §11）

只读 gate 读 ContextVar，而 ContextVar 必须在**后续 tool calls 执行时**是 armed 的。推荐做法：

> **让 typed state 成为单一事实源（SSOT），ContextVar 每轮从 typed state 派生。**

即 engine 在每轮循环顶部（已经在读 typed state 注入 reminder 的同一处）顺带同步 ContextVar：`plan_mode.active` 为真则 ensure armed、为假则 ensure reset。`invoke_agent` 退出时统一 reset。

好处：① tool-intercept 中途激活与显式 Plan Mode 走同一条同步路径；② 根治「reminder 读 typed / gate 读 ContextVar」双源漂移——ContextVar 永远是 typed 的每轮投影；③ 现有 web_chat 外层 `set_interactive_plan_mode` 的 try/finally（`web_chat_runtime.py:944`）可在收敛后移除，由 engine 接管（落地时协调，避免双重 arm）。

kernel 纯粹性：ContextVar 同步用的是 `plan_mode_runtime_context`（纯 ContextVar primitive，无 DB/IO）。建议通过 `KernelDependencies` 注入 `arm/reset` callback 以保持 kernel 对 service 层零直接依赖（备选：直接 import，轻微妥协，落地时定，见 §11）。

---

## 5. live chat 边界（按实测 source 修正）

```python
def _is_live_interactive_chat(session_context) -> bool:
    if session_context is None:
        return False
    src = getattr(session_context, "source", None)
    ch = getattr(session_context, "channel", None)
    return src in {"web", "web_chat"} or ch == "web"
```

- **激活**：`source == "web"`（实测值）或 `channel == "web"`，且有真实 `request.session_context`。
- **继续 RPC fallback**：heartbeat、trigger daemon、delegation、task executor、以及其他无「同等 plan card / confirmation UX」的渠道。
- **Feishu / Slack 等暂不混入**：除非它们具备等价的 plan card 确认 UX；否则把它们拽进 interactive loop 反而无处确认。

---

## 6. Feature flag

- **名**：`plan_mode.tool_intercept_interactive`
- **默认**：**off**（生产保持现有静态 `needs_plan` + RPC fallback 行为）。
- **语义**：只控制「tool intercept 之后是否进入 interactive loop」。**不影响**：显式 Plan Mode（路径 A，已上线）、plan recommendation、无人值守 RPC fallback。
- **灰度**：dev/staging 先开，验收后按环境开；保留一键回退（关 flag 即回到今天行为）。

---

## 7. seed 分层契约

只给 tool args 太窄（丢失用户原始意图），只给原始请求又缺被拦动作的精确上下文。**两者都给，分层**：

| 字段 | 来源 | 用途 |
|---|---|---|
| `original_request` | 最近一条 user message，截断到合理长度 | 规划的真实目标 |
| `action_kind` | 被拦动作的 ACTION_KIND | 精确意图（创建 trigger / 委派 / …） |
| `tool_name` | 被拦工具名 | 上下文 |
| `tool_args`（redacted） | 被拦 tool args，敏感值脱敏 | 规划起点 |
| `plan_id` / `version` / `hash` / `plan_json` | 若已 materialise | 衔接已有 awaiting plan |

**铁律**：seed 只是**规划上下文**，不是 approval。即使 seed 里带了 `plan_id`，确认仍必须经 `exit_plan_mode` 产出 plan card → 用户加密确认。kernel 激活不等于计划已确认。

---

## 8. 安全不变量（逐条保持）

1. **被拦工具始终不执行** —— fail-closed 与今天逐字节一致；激活只改「拦截后引导方式」。
2. **双写强制只读** —— typed + ContextVar 同时生效，杜绝「只有 reminder 无 gate」的写逃逸。
3. **seed ≠ approval** —— 确认链路 `exit_plan_mode` → `validate_confirmation`（真实 user_id）→ handoff 不变；禁止 agent 自我确认。
4. **flag 默认 off** —— 生产行为零变化直至显式开启。
5. **边界白名单** —— 仅 live web chat；无人值守路径继续 fail-closed RPC fallback，不进 interactive。

---

## 9. 测试面（落地时）

```bash
cd backend && source .venv/bin/activate
pytest \
  tests/tools/test_plan_mode_tool_gate.py \
  tests/kernel/test_plan_mode_tool_intercept_activation.py \
  tests/kernel/test_plan_mode_reminder.py \
  tests/tools/test_service.py
```

关键用例：
- 闸门在 live chat + flag on 时，`needs_plan` 带 `activate_interactive_plan` + seed；flag off / 非 web 时不带。
- kernel 收到信号后：typed state.active=True、metadata mirror 一致、ContextVar armed（后续 write/autonomous 工具被只读 gate 拦）。
- 下一轮 full reminder 注入（复用现有 `test_plan_mode_reminder` 断言）。
- 被拦工具未执行（fail-closed 回归）。
- `exit_plan_mode` 出卡、`plan_hash` / `validate_confirmation` 不变。
- 无人值守路径（source != web）不激活，走 RPC fallback。

---

## 10. 落地步骤（建议顺序，每步可独立验证）

1. **信号产出**：`_plan_mode_gate_block` / `_attach_intercepted_plan`（`tools/service.py`）在 flag on + 边界匹配时，在 `needs_plan` envelope 注入 `activate_interactive_plan` + seed。flag off 时零变化。
2. **边界判定**：实现 `_is_live_interactive_chat`（§5）。但闸门无 `session_context`（事实 1）——边界判定所需的 source/channel 需由 kernel 侧持有，**因此信号产出可以无条件带 seed，激活与否的边界判定放在 kernel**（kernel 有 session_context）。这是落地时的职责切分要点。
3. **kernel 激活**：engine 识别信号 → §4.1 双写 + §4.2 ContextVar 同步（typed→ContextVar 每轮派生）。
4. **flag 接线**：`plan_mode.tool_intercept_interactive`，默认 off。
5. **RPC 降级协同**：flag on 且 live chat 时 tool-intercept 不再跑 RPC planner（已降级的 RPC planner 退为纯无人值守 fallback，`5d07d04` 已放宽其预算）。
6. **测试 + 全量回归**：§9 + 后端全量（治理逐字节不变）。
7. **灰度**：dev 开 flag 验收 → 生产按环境开。

---

## 11. 开放实现选择（落地前定）

1. **ContextVar arm 的注入方式**：`KernelDependencies` callback（保持 kernel 纯粹）vs engine 直接 import `plan_mode_runtime_context`（更简单，轻微妥协）。推荐前者。
2. **ContextVar 每轮派生 vs 一次性 arm + finally reset**：推荐「每轮从 typed state 派生」（根治双源漂移），但需与现有 `web_chat_runtime.py:944` 外层 try/finally 协调（避免双重 arm / 收敛由 engine 接管）。
3. **plan 文件**：tool-intercept 激活是否立即 provision `plan_file_path`（复用 Phase 4B），还是先只 structured fill。建议先 structured fill，plan 文件作为后续。
4. **边界判定落点**：§10 步骤 2 —— 信号无条件带 seed、kernel 用 session_context 判边界 + flag 决定是否激活。确认这个切分。

---

> **状态**：设计稿，待 review 拍板。拍板后按 §10 落地，复用 §9 测试面。治理层不动。

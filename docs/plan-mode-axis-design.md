# Track 1 — Plan Mode 轴完整设计（A entry + E plan→执行交接 + G/H 模式边界）

> 设计原则：Plan Mode = 用户控制的权限模式开关；判断归模型(L1)、触发/批准归用户（零强加）、治理 L2 叠加复用 Plan 批准。**2026-06-18 口径**：risk grade / tool gate / REST gate 只能阻断执行并返回 `requires_confirmation`，不能强制切入 Plan Mode；`needs_plan` 只属于显式 Plan Mode 内的计划提交/确认流程。

## 关键事实地图

| 事实 | file:line |
|---|---|
| `classify_plan_mode_entry` 产 mode∈{none,recommend,auto,explicit,declined} | `plan_mode_core.py:164-224` |
| `_LONG_TASK_RE → mode=auto`（A 要删） | `plan_mode_core.py:122-126, 214-222` |
| web_chat 激活 `_maybe_handle_plan_mode_entry→_activate_interactive_plan_mode` | `web_chat_runtime.py:562-628`（call 621） |
| Feishu 激活 `mode in {"auto","explicit"}`（第二消费方） | `api/feishu.py:2166, 2225-2226` |
| `objective_trigger` handoff 丢弃 `plan_markdown` 只取 wake_policy | `plan_mode_handoff.py:98-110` |
| live chat handoff 已注入 plan_markdown（E 参照） | `plan_mode_session_handoff.py:69-86` |
| trigger 唤醒构造 trigger_context（不读 plan）= E 真实注入点 | `trigger_daemon.py:1039-1118` |
| `execution_mode` 误导命名（cache hint） | `invoker.py:136,292,324` |
| `long_task` 一词三义 | intent `core.py:36` / handoff alias `core.py:386`+`session_handoff.py:34` / artifact 子系统 `long_task_runtime.py`（不在范围） |
| `tool_action` handoff 无 handler 死路径 | `core.py:388-389`（registry 未注册） |
| in-loop 激活信号机制（A 新工具落点） | `kernel/engine.py:883-992` |

## A — Entry：进入路径收敛为「用户显式 + AI 请求许可」

**结论**：删 pre-LLM `auto` 自动激活；进入只剩两条——① 用户显式（`plan_mode_requested`/`_EXPLICIT_PLAN_MODE_RE`）；② AI 调 `request_plan_mode`（对标 CC `EnterPlanModeTool`，语义=请求许可，需用户批准）。工具结果本身永远不 flip Plan Mode；`recommend` 的"建议不触发"语义保留但判断来源从正则改为 prompt 引导下模型产出 `request_plan_mode`。

**硬骨头①**：CC 的 EnterPlanMode 在终端 `call()` 直接 flip mode + deferred permission gate；Hive 无同构同步终端批准。→ Hive 用「**进入低门槛（read-only 无害）+ 执行高门槛（PlanCard 用户确认）**」替代 CC 的 flip-时-批准，与 CC「plan mode 本身无害、ExitPlanMode 才需批准」语义一致。
**硬骨头②**：无人值守（trigger/heartbeat）无在场用户批准 → `request_plan_mode` 在无人值守 source **fail-closed**（不暴露/no-op）；无人值守路径只能产出 checkpoint / `requires_confirmation`，不能自行进入 Plan Mode。

**改动文件**：
- `plan_mode_core.py`：删 `classify_plan_mode_entry` 的 `has_long_task` 分支(214-222) + `_LONG_TASK_RE`(122-126)；`PlanModeEntryDecision.mode` 枚举去 `auto`。保留 schedule→recommend。
- `web_chat_runtime.py`：`_maybe_handle_plan_mode_entry` 收窄到 recommend+explicit（auto 删后自然）；更新 docstring。
- `api/feishu.py:2225-2226`：`in {"auto","explicit"}` → `=="explicit"`。
- **`tools/handlers/plan_mode.py`**：`@tool request_plan_mode`（产 `plan_mode_entry_requested` envelope；用户批准后由 web/chat entry path 进入 Plan Mode，工具结果不激活）。
- `tools/capability_gate.py CAPABILITY_MAP`：⚠️必加 `request_plan_mode`=safe（STRICT 默认 True，漏注册→真实 invocation 被拒）。
- `runtime/prompt_sections/`：「何时该先规划」判据移进 prompt（与 Track 3 协调，见 Track 3 plan_mode_guidance.py）。

**契约 `request_plan_mode`**：governance=safe, read_only=True, parallel_safe=False；params `{reason: string}`（reason 必填供用户判断和审计）。返回 `{status:"plan_mode_entry_requested", reason, next_action:"END turn, 等用户批准"}`。没有 `activate_interactive_plan` / `interactive_plan_seed`，也不写 PlanRequest；只有真实用户批准事件才能进入 Plan Mode。

**测试**：更新 `test_plan_mode_gate_core.py:372`（auto 断言→改为长任务文本不自动进）；新 `test_request_plan_mode_tool.py`（envelope 正确/seed entry=agent_requested/无参成功）、`test_request_plan_mode_no_op_in_unattended`（硬骨头②防线）、`test_long_task_text_no_longer_auto_activates`、`test_executing_actions_teaches_when_to_plan`。

**迁移**：无 schema。`auto` 删除是纯运行时分类变更，`auto` 从不持久化，零回填。release note 标注「纯长任务措辞不再自动进 Plan Mode」。

**验收**：`grep '_LONG_TASK_RE\|mode == "auto"'` 空；`request_plan_mode` 在 CAPABILITY_MAP；live chat 真 invocation agent 调→进 read-only Plan Mode；无人值守 fail-closed。

## E — Plan→执行交接：定时任务携带 plan_markdown

**结论 + 硬骨头④（关键）**：注入点**不是** `plan_mode_handoff.py`（handoff 创建 objective+trigger 时，trigger 还没 fire），而是 `trigger_daemon.py:_invoke_agent_for_triggers`（trigger **每次** fire 时）。trigger.config 只存 `plan_id`（已有 `handoff.py:193`），fire 时按 id 回读 plan row 取 `plan_markdown`（避免 config 膨胀 + 单一真相源）。

**改动文件**：
- `trigger_daemon.py:_invoke_agent_for_triggers`：新 helper `_load_confirmed_plan_for_trigger(db,trigger)`（校验 `config.plan_id` → plan status==confirmed + agent 匹配，fail-closed）；在 trigger_context 拼装(1039-1049)前注入「已确认的计划」段（plan_markdown，fallback objective/original_request）。
- 抽 `plan_mode_session_handoff._plan_execution_prompt`(69-86) → 可复用纯函数 `build_plan_execution_instruction(*,plan_id,plan_version,plan_markdown,objective,original_request,source)`，live+trigger 共用，**消除文案漂移**。
- `plan_mode_handoff.py`：plan_id 链已就绪，E 不需改这里。

**测试**（真 PG）：`test_trigger_daemon_plan_context.py`：confirmed plan→fire→trigger_context 含 plan_markdown；无 plan_id trigger 不变；unconfirmed/agent-mismatch fail-closed；多 trigger 同 plan 去重。`test_plan_execution_instruction.py` 纯函数单测。E2E 扩展。

**迁移**：无 schema。存量 plan-born trigger 的 config.plan_id 已在 → fire 时自动回读，**无需主动回填**。无 plan_id trigger 行为零变化。硬骨头⑤：plan_id 缺失 fail-closed 静默降级（按 reason/focus 唤醒）+ 可选 audit log。

**验收**：真 PG E2E：confirmed objective_trigger→fire→agent 首条 message 含 plan_markdown；两路文案出自同一 `build_plan_execution_instruction`（grep 无第二份手写）。

**范围纪律**：delegation handoff 是否也注入完整 plan_markdown = follow-up，E 先只统一 live+trigger 两路（prompt 明确要求的），避免蔓延。

## G/H — 模式边界：命名收敛 + 断链修复 + 去散落特判

### G/H.1 `execution_mode` 误导命名
**关键澄清**：存在**两个** execution_mode，不能一刀切——(a)`agent.execution_mode`（DB 列 `models/agent.py:64`，standard/coordinator，语义合理**不改**）；(b)`request.execution_mode`（`invoker.py:136`，conversation/task/heartbeat，**只**喂 apply_cache_hints + risk_clause + identity 模板）= 误导命名。**只改 (b)** → `invocation_scope`。
**硬骨头⑥（最大）**：`invoker.py:229` 把 agent.execution_mode(a) 赋给 request 同名字段(b)——命名混淆根源。改名时必须**显式解耦**（加 `_invocation_scope_for(agent,request)` 映射函数），否则只是换名字。
改动面广（prompt_cache.py/context_budget.py/executing_actions.py/identity.py/task_executor.py + 所有 call site）；机械改名 + 1 处解耦。

### G/H.2 `long_task` 一词三义收敛
①intent-type（合法**保留**）②handoff-target alias（废弃，已被 continue_current_session 取代）③artifact 子系统（**不动**）。
改：`_INTENT_HANDOFF_TARGET["long_task"]:"long_task"`→`"continue_current_session"`（`core.py:386`）；`LEGACY_LONG_TASK_TARGET` 注册**保留**（存量兼容）+ deprecation 注释。零数据迁移。

### G/H.3 handoff 断链
`tool_action`（external_action/state_change→无 handler 死路径，但实际无生产入口产这俩 intent）；`detached_runtime_task`（已 fail-closed stub，**已修好**）。
**推荐**：`_INTENT_HANDOFF_TARGET["external_action"]/["state_change"]` 从 `"tool_action"` 改 `"continue_current_session"`（直接复用 live 续跑，无 session 时 fail-closed），彻底删 `tool_action` target 词。
关键验收测试 `test_every_intent_handoff_target_has_registered_handler`（遍历 `_INTENT_HANDOFF_TARGET.values()` 全有 handler，钉死无 no_handler_registered 死路径）。

### G/H.4 Workflow 确认不再复用 Plan Mode 强制入口（**2026-06-18 修正**）
`start_workflow` 不再注册 `plan_gate_action_kind`，`workflow_launch.inspect_workflow_confirmation_needs` 只给 preview/start 返回 `confirmation_required` / `confirmation_reasons`。需要确认时由 UI/调用方收集 `user_confirmed` 或同等用户事件；risk/预算/fanout/等待阈值只能解释为什么要确认，不能决定是否进入 Plan Mode。

**G/H 验收**：`invocation_scope` 改名 + 两 execution_mode 语义文档化；skeleton long_task intent→continue_current_session；`test_every_intent_handoff_target_has_registered_handler` 绿；workflow 的确认面返回 `requires_confirmation` 或 `confirmation_required`，不会产出 `needs_plan`。

## 落地顺序（Track 1 内）
1. G/H.2+G/H.3（命名/断链收敛，纯重构+兼容别名，风险最低）。
2. E（trigger 注入 plan_markdown，独立可测，价值高）。
3. A（删 auto+新工具+prompt 引导，依赖 Track 3 措辞）。
4. G/H.1（execution_mode 改名，面最广，与 Track 2 冲突面大，单独窗口）。

**生产真接线检查**（防绿测试≠完成）：E 红测必须经 `trigger_daemon._invoke_agent_for_triggers` 真实 fire 路径；A 必须在 CAPABILITY_MAP+CORE 真实可达；G/H.3 遍历真实注册。

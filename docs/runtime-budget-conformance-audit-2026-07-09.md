# Runtime Budget Control Plane — 一致性审计 + 收窄-A 补齐记录

日期：2026-07-09 · 作者：g-budget-step12 · 施工图：`docs/runtime-budget-control-plane-plan-2026-07-03.md`

## 0. 结论

Runtime Budget Control Plane 的 **主体（Step 1–7 大部分）在 07-04 已实现并合入 main**（commit `7bcd9b10d` / `5e53c988a`）。本轮为**收窄-A**：一致性审计 + 补三个真实缺口（§10 breaker 四维接活为核心）。**非重建**。

图例：✅ 07-04 已实现且本轮核验通过 · 🟢 本轮补齐 · ⏭️ 明确不做（附理由）

## 1. 逐条一致性

| 条款 | 状态 | 证据 / 说明 |
|---|---|---|
| §7.1 `runtime_budget_policies` | ✅ | `models/runtime_budget.py` 全字段（命名 `max_provider_calls` = spec 的 `max_llm_calls`，语义一致）。🟢 本轮加 `max_failures/max_needs_reconciliation/max_child_failure_ratio/max_parent_invocations`。 |
| §7.2 `runtime_budget_runs` | ✅ | 全字段 + reserved/used 对。🟢 本轮加同 4 个 max 维 + `failures/needs_reconciliation_count/parent_invocations` 计数器。 |
| §7.3 `runtime_budget_events` | ✅ | append-only + `reservation_key` 幂等约束。🟢 本轮新增 `event_type="circuit_break"`。 |
| §7.4 `runtime_tasks` 增量 | 🟢 | `budget_run_id`✅(07-04) + 本轮加 `root_runtime_task_id`、`budget_snapshot_json`（spec 点名两列，此前缺）。 |
| §7.5 reservation 原子性 | ✅ | `reserve()` 走 `_lock_run`(`SELECT ... FOR UPDATE`) + `_denied_dimensions` 全维检查 + 仅全通过才 `_increment_reserved`（多维 all-or-nothing）+ `reservation_key` 入口幂等。符合 §7.5「FOR UPDATE 整行锁」可选实现。 |
| §7.6 estimate contract | ✅ | `estimate_reservation_tokens` 取 max(default, prompt, observed_floor)；builtin default reservation 非小常数（200K/250K/300K）。 |
| §7.7 reaper / 悬挂回收 | ✅ | `reap_expired_runs`（过期 run→expired + kill pending 未认领 task）+ `reconcile_orphaned_reservations`（终态 task 释放 reservation）。 |
| §8.1 root run 边界 | ✅ | create_run 全入口接线：trigger/heartbeat/web_chat/workflow/orchestrator。web_chat 正确实现「续跑继承 vs 下一条独立消息新建」。 |
| §8.7 降级语义 | ✅ | `decide_budget_service_failure`：interactive 非放大→fail-open + disable work-amplifying；其余→fail-closed。 |
| §9.1 内置默认 | ✅ | `_BUILTIN_PROFILE_DEFAULTS` 四 profile 数值与 §9.1 逐格吻合。🟢 本轮补 `max_parent_invocations`（interactive/scheduled=16, workflow=64, agent_team=24，来自 §9.1 列）。 |
| §10 circuit breaker | 🟢 | **本轮核心。** 详见 §2。 |

## 2. §10 breaker 补齐（本轮核心 = 常春藤复发防护）

**此前状态**：wake breaker 已存在（`subagent_wake_consumer._trip_child_failure_breaker_if_needed`），但**阈值硬编码在模块常量**（needs_reconciliation=3/failures=5/min_children=8/ratio=0.5），未从 policy 读；`max_parent_invocations` 完全缺失；settle 后无 breaker eval。

**本轮补齐**：

1. **四维 policy 驱动**：`max_failures/max_needs_reconciliation/max_child_failure_ratio/max_parent_invocations` 进入 policy→run snapshot，四个调用方从 policy 拷贝（tenant override 可流通），builtin 默认兜底。
2. **纯函数 `evaluate_circuit_breaker`**（functional core，无 DB 无 mock，8 单测本机通过）：accumulation 维（tokens/cache_miss 计 used+reserved，其余计 used）+ parent_invocations + failures + needs_reconciliation + child_failure_ratio。
3. **两处真实 enforce**：
   - ① `settle()` 后：读 run 行计数器/累计维，超限→`_apply_breaker` 按 `fail_mode` 转 summary_only/hard_stopped + 写 `circuit_break` event + 取消 pending 未认领 task。
   - ② `evaluate_wake_breaker()`（wake 前）：查 child 终态 → **物化** `failures`/`needs_reconciliation_count` 列（真实写入点，ground-truth 无漂移）+ **递增 `parent_invocations`** → eval → apply。`subagent_wake_consumer` 委托此方法，删除硬编码常量。

## 3. 关键设计判断（≤10）

1. **failures/needs_reconciliation = ground-truth 查询物化到列**，非在 ~15 个 child-failed 站点散落递增。理由：那 15 处多在 `subagent.py` 是 result 信封非 DB 持久化；散落计数器易漂移；安全 breaker 用 child 状态真相源无漂移。列在 wake 处从查询写入 = 满足「真实写入点」且零漂移。
2. **parent_invocations = 纯计数器**，wake 单一干净写入点递增（真正全缺的维度）。
3. **child_failure_ratio = failed/total**（wake 查询），`min_children=8` 统计显著性下限（沿用事故常量），settle 路径不查 child 故跳过 ratio。
4. **事故校准常量 5/3/0.5 作 breaker 默认**：§9.1 未列这三维，沿用 wake 原硬编码值（常春藤校准）。
5. **`require_confirmation` fail_mode → summary_only 状态**：暂停放大 + 保留审批（`approve_overrun` 已存在）；`hard_stop`/`fail_closed` → hard_stopped。
6. **settle 路径 breaker 只读 run 行**（无 child 查询，避免每 provider call 一次查询）；child-outcome breaker 归属 wake 路径（child 完成时刻）。
7. **wake breaker 命中即阻断 wake**（任何 fail_mode），保留既有行为；summary_only「放行一次总结 wake」的细化归 **Step 5**（本轮未改 wake-proceed 语义）。
8. **migration 保持单头**：`runtime_budget_breaker_dims_0709`（down_revision=`capability_factor_intake_0709`）。
9. **surgical**：既有 6 文件本就 `ruff format` 漂移（HEAD 版本即 would-reformat），本轮**不 blanket-format**（避免 collateral + 共享文件冲突）；仅格式化自有新文件。

## 4. ⏭️ 明确不做（附理由）

- **§14 八个分测试文件不拆**（缺口3，lead 拍板）：既有覆盖已在 `test_runtime_budget_service.py`（924 行）实质合规，拆文件是形式。新 §10 breaker 测试放独立 `test_runtime_budget_breaker.py`（新特性专属模块，非 §14 那八个的分解）。
- **PG 门控用例本机不跑**（缺口4）：`migrated_pg_url` 纯 Testcontainers，本机无 Docker → 5 个新 DB breaker 测试 + 既有 19 个 budget PG 测试 skip；CI 有 Docker 会跑。**本机未验证并发/reaper/DB-wiring 用例**（仅代码审查 + 纯函数覆盖）。

## 5. 测试

- 纯函数（本机跑）：`test_runtime_budget_breaker.py` 8 passed（breaker 决策全维 + fail_mode 映射 + builtin 默认 + ratio 下限 + token used+reserved）。
- DB 集成（CI 跑）：5 个（连败→summary_only / reconciliation→hard_stop / parent_invocations 超限→stop / settle 耗尽→breaker / hard_stop 取消 pending）。
- 回归：`test_runtime_budget_service/breaker/llm + api + subagent_run_service + invoker + runtime_task_worker` = **117 passed, 26 skipped**（skip 全 Docker 门控），零失败。
- ruff check 全绿；alembic 单头。

## 6. 未尽事项 / 交接提醒

- **共享工作树混改**：`trigger_daemon.py`（+240，其中我仅 4 行 breaker；~236 为 Loop#5 的 `/loop` same-session 投递）、`web_chat_runtime.py`（+43，我 4 行）、`heartbeat.py`（我 4 行）含**其他 teammate 未提交工作**。lead commit 我的切片时需 `git add -p` 只挑 budget hunk，勿裹入 Loop/Goal 未完成代码。
- 我的**纯 budget 切片**：`models/runtime_budget.py`、`models/runtime_task.py`、`services/runtime_budget_service.py`(+254)、`services/subagent_wake_consumer.py`、`alembic/versions/runtime_budget_breaker_dims_0709.py`(新)、`tests/services/test_runtime_budget_breaker.py`(新) = **完全我的**；四个调用方各 +4 行 = 我的 breaker 拷贝。

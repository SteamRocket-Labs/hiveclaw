# 生产修复与收尾统一方案

> 建档 2026-08-23
> 诊断证据：`docs/wip/memory-companykb-production-acceptance.md` §3.6
> 生产：`postgres-volume 13.5 GB / 48.8 GB`、`backend-volume 21.3 GB / 48.8 GB`、三服务 Online、commit `b3e0546f`（与本地 HEAD `0cac825b` 在 backend/frontend 零差异）
> 状态：方案待 owner 确认，未施工

---

## 0. 一页总览

按「用户是否正在受损」排序，不按技术难度排序。

| 优先级 | 项 | 性质 | 用户是否正在受损 | 首步是否需要写代码 |
|---|---|---|---|---|
| **P0-A** | LLM provider 额度耗尽（429/402） | **账单阻断** | **是。任何 agent 都无法成功运行** | **否** |
| **P0-B** | trigger 终态写入断裂（07-16 起零 completed） | **静默产品故障** | **是。自主执行链停摆 38 天** | 是 |
| **P0-C** | skip 不推进调度游标 → 每分钟空转 | 结构性缺陷 | 间接（容量泄漏源头） | 是 |
| **P0-D** | 51 个 agent 主模型为 NULL | 配置债 | 是，但前三项不修则修它无用 | **否** |
| P1 | runtime_tasks / budget_runs 容量泄漏 | 容量与可观测性债 | 否（P0 的排泄物） | 否（先量，后改） |
| P2 | B3 Company KB 首次真实数据验收 | 验收缺口 | 否（功能零采用） | 否 |
| P3 | Memory 前端术语净化（SA-08 硬要求） | 表达缺陷 | 轻度（误解） | 是 |
| P4 | SA-07 T0→T2 生产回填 | 验收缺口 | 否 | 否 |
| P5 | 图投影流转施工 | 新能力 | 否 | 是 |
| P6 | 已登记未建能力 + hermes 对标 | 北极星缺口 | 否 | 是 |

**P0-A 不需要改任何代码，且不做它其余全部无效。**

---

## P0：自主执行链故障（2026-08-23 15:5x 重写）

> **本节已推翻首版。** 首版把 P0 定性为「51 个 agent 从未配主模型，配上即止损」。
> 生产只读复核证伪了它的三个前提。证据：本文档 §P0 证据表，探针保留在 session scratchpad。

### 被证伪的三个前提

| 首版说法 | 实测 |
|---|---|
| 这 51 个数字员工「从未运行成功」 | **50/51 累计消耗 22.57 亿 token**（中位 1,530 万，最高 7.62 亿）。它们工作过，后来被清空主模型 |
| 空转是 `no_model` 特有的问题 | **与 skip reason 无关。** 另一个 agent 因 `plan_required` 空转，速率**恰好也是 1,440 次/天** |
| 配上模型即可止损 | **不会。** 自主执行链已整体停摆 38 天，且与主模型无关；provider 账户同时已无额度 |

### 三个独立故障，按「挡住恢复」的顺序

#### P0-A　LLM provider 额度耗尽（**卡住其余一切**）

`runtime_tasks` 中 30 天内 failed heartbeat 的真实原因：

| 原因 | 次数 | 首次 | 最近 | agent |
|---|---|---|---|---|
| `HTTP 429 已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量` | **1,119** | 2026-08-04 | **今天** | 5 |
| `HTTP 402 Insufficient Balance` | 314 | 2026-07-24 | 2026-08-06 | 2 |
| `ReadTimeout` / `JSONDecodeError` / 其它 | 280 | — | — | — |

**这不是工程问题，是账单问题。** heartbeat 的 completed 在 2026-08-13 归零。
在额度恢复之前，给任何 agent 配模型都不会产生一次成功运行。

另：`常春藤` 租户（49/51 个空转 agent 在此）只有 **1 个** enabled 模型；其余 5 个租户各有 2–3 个，且 NULL 主模型数均为 **0**。

#### P0-B　trigger 执行从未进入 agent（**真正的产品故障**）

##### 已确证的事实

```
task_type=trigger 最后一次 completed   : 2026-07-16 09:30:17Z
最后一条 trigger invocation span       : 2026-07-16
最后一个 trigger_run ChatSession        : 2026-07-16
最早一个永久卡死的 running task         : 2026-07-16 16:00:18Z
此后 38 天：running 2,107 个，completed 0，failed 0
```

**2,107 个卡死任务中，产生过 invocation span 的：0 个；建出 trigger_run ChatSession 的：0 个。**

`ChatSession(session_kind="trigger_run")` 在 `_invoke_agent_for_triggers` 中于 `invoke_agent` **之前**插入并 flush。它一次都没发生，说明执行连函数前 ~90 行都没走完，`invoke_agent` / `hook.setup` 根本没被触达。

##### 这不是异常

函数外层有 `except Exception`，它会写 `status="failed"` 并结算 budget（`trigger_daemon.py:2218` 起）。而这 2,107 个是 `running`，30 天内 `failed | trigger` 计数为 **0**。
**结论：从未有异常被抛出并捕获。** 协程要么挂在某个 await 上永不返回，要么被销毁/取消（`CancelledError` 属 `BaseException`，会穿过 `except Exception` 且不留终态）。

##### 已排除

| 假设 | 证据 |
|---|---|
| ImportError（函数级 import 失效） | 在**生产镜像内**逐个 import，10/10 全部 OK；`inspect.getsource` 确认部署源码与本地一致 |
| 部署陈旧 | VERSION=1.7.0，进程 `elapsed=29 天 12:36`，自 2026-07-24T19:51Z 未重启 |
| DB 连接池耗尽 | `pg_stat_activity` 28/500 连接，无长事务 |
| 锁竞争 | `pg_locks` 共 3 个，全部 granted |
| 信号量耗尽为**起因** | 07-24 冷启动后首批 15 个任务同样 0 span。全新信号量有 8 个空位，若只是排队，前 8 个应产出 `hook.setup`。（耗尽仍可能是**放大器**：`TRIGGER_MAX_CONCURRENT=8`，一旦 8 个挂住则后续永久排队） |
| 各类有守卫的失败分支 | `agent_not_found` / `agent_not_runnable` / `model_error` / admission 失败**全部写 `skipped`**，与 `running` 不符 |

##### 嫌疑窗口已缩到四个无守卫的 await

`_invoke_agent_for_triggers`（`trigger_daemon.py:1678-2249`）从入口到 ChatSession 插入之间，只有这四处失败后不写终态：

1. `async with tenant_scoped_session(tenant_id, require_tenant=True, source="trigger")` — 会话获取
2. `await _build_confirmed_plan_context(db, triggers, agent_id)`
3. `await db.execute(select(Participant)...)`
4. `db.add(session); await db.flush()`

前面的 `fire_workflow_for_trigger` 对普通 cron 是同步返回 `None`（无 `workflow_ref`），`_resolve_batch_same_session_target` 也是纯函数返回 `None`，两者都不会挂。

##### 生产复现：前段完全干净

在生产容器内、用生产配置与生产数据，对一个真实卡死的 agent（`40ba82c4` / `daily_ks_radar_scan`）逐步重跑前段，每步带超时，只读不写：

| 步骤 | 结果 |
|---|---|
| `resolve_tenant_for_agent` | ok 0.20s |
| `admit_agent_runtime_tenant` | ok 0.00s → `allowed` |
| `tenant_scoped_session(require_tenant=True)` 获取 | ok 0.02s |
| 载入 AgentTrigger | ok — `cron`、`enabled`、`last_fired_at=2026-07-16 01:09:12`（**冻结**）、config 含 `_fire_inflight` |
| `_resolve_batch_same_session_target` | `None`（不走 same_session 分支） |
| 载入 Agent | ok — `status=idle`，**有 primary_model_id** |
| `select_trigger_model` | ok 0.01s |
| `_build_confirmed_plan_context` | ok 0.00s → `""` |
| `select(Participant)` | ok 0.01s |
| **抵达 ChatSession 插入点** | **累计 0.10 秒** |

**四个无守卫 await 全部干净，整段 0.1 秒跑完。代码路径不是问题——协程根本没运行。**

##### 根因：派发方式保证了静默死亡

trigger 任务有两条派发路径，`_tick()` 走的是**没有任何完成核算**的那条：

| | `trigger_daemon._tick()`（实际走的） | `runtime_task_worker`（正确的那条） |
|---|---|---|
| 派发 | `asyncio.create_task(run_bounded(...))`（`trigger_daemon.py:2543`） | `_dispatch_async_runtime_task(...)`（`runtime_task_worker.py:635`） |
| 强引用 | **无** —— 事件循环只持弱引用 | `_DISPATCHED_TASKS` 持有 |
| done callback | **无** | 有（`runtime_task_worker.py:682`） |
| claim / lease | **无** —— 任务被直接建成 `running` | 有 |
| 死亡可被观测 | **不可能** | 可以 |

四条缺失叠加的后果：

1. `_tick()` 把 RuntimeTask **直接建成 `running`**（`started_at` 甚至早于 `created_at` 9ms），因此它从不进入 worker 的认领队列。
2. 裸 `create_task` 不留引用、不挂回调，协程若被 GC 回收或取消，**没有任何代码有机会写终态**。
3. `claimed_by` / `claim_expires_at` 全为 NULL —— **没有租约可过期**，运行期回收机制够不到它。
4. 唯一的回收器是**启动时**的 `startup orphaned runtime-task reconcile`（`runtime_task_service.py:807`），它把 `status='running'` 且 `task_type='trigger'` 的行改判 `needs_reconciliation`。

第 4 条有实物指纹：30 天内 `needs_reconciliation | trigger` 共 **32** 行，`latest_created = 2026-07-24 19:03Z` —— 正好在 19:48Z 那次部署重启之前。**那是这个回收器最后一次运行。** 此后进程连续运行 29.5 天，2,107 个孤儿无人清扫。

##### 仍未证实的一环（诚实标注）

**具体是什么杀死了协程**（GC 回收 / 取消 / 事件循环饥饿）尚无直接证据——需要抓到 `Task was destroyed but it is pending` 或 `Task exception was never retrieved` 日志。生产日志被 RLS BYPASS enter/exit 噪音淹没（40 秒 986 行），而 trigger 每小时仅烧约 3 次，短窗口抓不到。

**但这一环不阻塞修复。** 已证实的部分已经足够定义修法：无论谁杀死它，当前设计都保证死亡不可观测、不可恢复、不可告警。

##### 修复已实现（2026-08-23，未提交、未部署）

根因是「派发方式保证死亡不可观测」，因此修法是**删掉那条派发路径**，改走仓库里已经正确的那条（`runtime_task_worker`：强引用 + done callback + 租约 + fence + 认领恢复）。没有新建任何并行机制。

| # | 改动 | 文件 |
|---|---|---|
| 1 | trigger RuntimeTask 一律建成 `pending`（此前是 `running`，落在 `CLAIMABLE_RUNTIME_TASK_STATUSES` 之外，永不进认领队列） | `services/trigger_daemon.py` |
| 2 | 三个 `asyncio.create_task(run_bounded("trigger", ...))` 全部删除，改为 `_queue_trigger_run_for_worker()` → `notify_runtime_task_worker()` | `services/trigger_daemon.py`（tick / 立即触发 / 重启恢复） |
| 3 | `trigger` 加入 `LEASE_RECLAIMABLE_RUNTIME_TASK_TYPES` —— 租约死掉的 running 行现在运行期可回收，不再只能靠进程重启 | `services/runtime_task_claim_service.py` |
| 4 | **会话已绑定守卫**：认领到的 trigger 若已绑 `child_session_id` → `needs_reconciliation`，绝不盲目重放（工具可能已执行） | `services/trigger_daemon.py` |
| 5 | **陈旧意图守卫**：意图超过 `TRIGGER_MAX_INTENT_AGE_SECONDS`（默认 1h）→ `skipped(stale_trigger_intent)`。这既是产品语义（fire 绑定在时刻上，重放昨天的日报是噪音），也是**防止修复本身在首次部署时把 2,107 条历史 fire 一次性放出来** | `services/trigger_daemon.py` + `config.py` |
| 6 | `mark_daemon_tick` 不再写 `last_success_at`；新增 `mark_daemon_outcome`，只有真正达到终态才算成功；健康面新增 `last_outcome_at` / `outcome_count` | `services/daemon_liveness.py` |
| 7 | trigger 每次写入终态时上报 outcome（含 `needs_reconciliation`） | `services/trigger_daemon.py` |
| 8 | 存量 2,107 条孤儿的对账脚本：dry-run 默认，`--apply --confirm RECONCILE_ORPHANED_TRIGGER_RUNS`，按「是否绑过会话」分流 | `scripts/reconcile_orphaned_trigger_runs.py` |
| 9 | 启动期告警：trigger daemon 起来但本进程 worker 被禁用时明确警告（跨进程认领无法自证，故为 advisory 不是硬门） | `services/trigger_daemon.py` |
| 10 | RLS BYPASS 白名单登记新脚本的调用点 | `core/rls_bypass_manifest.py` |

##### 接线证明（live entry → 新代码）

```
main.py:702  lifespan → start_runtime_task_worker_loop()   [gate: runtime_task_worker_enabled(), 生产 role=runtime ✓]
   → claim_and_dispatch_once() → RuntimeTaskClaimService.claim_available()
      认领 pending，以及（改动 3 之后）租约已死的 running trigger
   → _dispatch_claimed_task() → runtime_task_worker.py:635
      _dispatch_async_runtime_task(..., task_type="trigger")
         ├ _DISPATCHED_TASKS[run_key] = task      ← 强引用，GC 不再能吞掉它
         ├ add_done_callback(...)                 ← 完成核算
         └ run_claimed_runtime_task(lease_seconds=180)  ← 租约续期
   → _execute_claimed_trigger_task()（包异常）→ execute_claimed_trigger_runtime_task()
      → 会话守卫 / 陈旧守卫 → _invoke_agent_for_triggers()

trigger_daemon._tick() → _queue_trigger_run_for_worker() → notify_runtime_task_worker()
   （本进程不再执行任何 agent 调用）
```

生产前置条件已核实：`/api/health` → `process_role.role = "runtime"`、`runtime_task_worker.running = true`、`RUNTIME_TASK_WORKER_TASK_TYPE_LIMITS` 已含 `trigger=8`、`RUNTIME_TASK_CLAIM_POLL_SECONDS = 1.0`。

##### 被推翻的旧测试契约（三条钉着 bug）

| 测试 | 原断言 | 现断言 |
|---|---|---|
| `test_tick_creates_trigger_runtime_task_before_invocation` | `status == "running"` | `status == "pending"` |
| `test_tick_does_not_apply_agent_level_dedup_window` | `scheduled == ["_invoke_agent_for_triggers", ...]` | 排队两次；`create_task` 被断言为绝不调用 |
| `test_fire_trigger_once_now_runs_full_fire_path` | 同上（立即触发路径） | 同上 |
| `test_resume_persisted_trigger_runs_requeues_unstarted_run` | `status == "running"` + in-process 重放 | `status == "pending"` + 排队 |
| `test_daemon_fanout_dispatch_sites_are_bounded` | trigger 需 ≥2 处 `run_bounded` | trigger 需 **0** 处，且必须 `_queue_trigger_run_for_worker` |
| `test_runtime_task_claim_statement_reclaims_only_expired_active_rows` | 可回收类型枚举不含 trigger | 含 trigger |

##### 验收证据

```
cd backend && source .venv/bin/activate && pytest tests -q
→ 7992 passed, 2 skipped, 1 warning in 479.29s
ruff check app/ tests/services/  → All checks passed!
ruff format --check <11 个改动文件>  → all formatted
```

新增红测 12 条（`tests/services/test_trigger_dispatch_accountability.py`），逐条对应上面的机械事实：可认领状态、不得 fire-and-forget（含 AST 结构性回归门）、租约可回收、会话绑定不重放、陈旧意图不重放、新鲜意图正常执行、tick 不算成功、outcome 已接线、脚本分流与确认门。

##### 仍未做（明确列出）

- **未提交、未部署。** 两者各需单独授权。
- **存量 2,107 条孤儿尚未清理。** 脚本已写好并有测试，但生产 `--apply` 属不可逆写入，需 owner 授权；先跑 dry-run 看分流数字。
- **P0-A 未解决前，修好 trigger 也只会立刻撞 429。** 部署顺序应为：充值 → 部署 → dry-run 对账 → apply。
- 「具体是 GC / 取消 / 事件循环饥饿哪一种杀死协程」仍未证实。修复不依赖该结论（四条缺失中任何一条被补上，死亡都不再静默），但若想彻底定性，需在生产抓 `Task was destroyed but it is pending`。

##### 已部署（2026-08-23，两次提交）

| 提交 | 内容 | 生产状态 |
|---|---|---|
| `fcd5e4b8` | trigger 改由 RuntimeTask worker 认领执行（强引用 + done callback + 租约 + fence）；会话绑定守卫；陈旧意图守卫；`mark_daemon_outcome` | 22:24Z 前已上线，三服务 SUCCESS |
| `aeb777e0` | **真正的根因修复**（见下）+ 全仓 13 处 loguru 地雷 | 22:29Z 上线，三服务 SUCCESS |

##### 真正的根因（生产 traceback 实证）

```
CheckViolationError: writer_epoch_rejected legacy run authority
  trigger_daemon.py _invoke_agent_for_triggers → append_session_event → db.flush()
```

`session_event_contract` 要求写 transcript 行时 `runtime_tasks` 里**已存在** run→session 绑定。
web chat 建 RuntimeTask 时就带 `parent_session_id`，天然满足；trigger 每次发火现建会话，却把 `child_session_id` 写在第一条 event **之后 23 行**。该约束随 Session V2 cutover（`c50fea9d`，2026-07-16 10:54Z）上线，正好落在最后一次成功（09:30Z）与第一条永久卡死（16:00Z）之间。

**修复**：绑定提到 `db.flush()` 之后、任何 transcript 写入之前。`update_runtime_task_record` 独立连接提交，READ COMMITTED 下约束可见。

##### 隐藏它 38 天的第二个 bug

```python
logger.error(f"Failed to invoke agent {agent_id} for triggers: {e}", exc_info=True)
```

loguru 无 `exc_info` 参数 → 当作格式参数 → 对已插值的 f-string 再跑 `.format()` → 异常文本含 `{"source": "trigger_daemon"` → `str.format` 把 `"source"` 当字段名、`:` 当格式串 → `KeyError: '"source"'`。**异常处理器在写终态前先炸了**，这就是 30 天 `failed|trigger`=0 的成因，也是 `exc_info=True` 从未产出 traceback 的成因。

全仓同形状共 **13 处**（feishu / oidc / teams / hr_provisioning_runner / wechat_personal_stream / trigger_daemon），全部改为 `logger.opt(exception=True).<lvl>(f"...")`，并加全仓 AST 门禁止复发。

**这也修正了本文档此前的判断**：不是「没抛异常」，是抛了、而处理器自己又抛了一次。

##### 生产验收状态（2026-08-23 22:43Z）

| 项 | 结果 |
|---|---|
| 卡死 `running` trigger | 2,107 → **15 → 1** |
| 这些行有租约 | **全部有**（此前全为 NULL） |
| 失败能否写终态 | **已证实修好**：部署后出现 `failed`，附真实原因。30 天来第一次 |
| `stale_trigger_intent` 生效 | 全天触发 20 次 |
| **2026-07-16 之后第一个 `completed`** | **尚未出现 —— 未达成** |

未达成的原因有两层：

1. **时间窗**：68 个调度型 trigger 的 `_fire_inflight` 停在 18:06Z，`_TRIGGER_FIRE_INFLIGHT_STALE_SECONDS=6h` → 最早 **00:06Z** 才会再次发火。在那之前无法验证 A 是否真的修好。
2. **浮现出第三个阻塞（原本被 B 隐藏）**：

```
QueryCanceledError: canceling statement due to statement timeout
[SQL: SELECT ... FROM runtime_tasks WHERE runtime_tasks.id = $1::UUID FOR UPDATE]
```

主键查询加 `FOR UPDATE` 撞 30s `statement_timeout` —— **不是慢扫描，是行锁等待**。当前时刻复查 `pg_locks` 15 个锁全 granted、`blocked_by=0`，说明是**间歇性争用**而非持续阻塞。

**这正是本文档 P1 一节预留的判据被触发**：「若 P0-B 的诊断指向查询超时，则 P1 不是 578 天以后的事，而是 P0-B 的共因，须并入 P0。」**现在成立，P1 应提级并入 P0。**

##### 下一步（按顺序）

1. **00:06Z 之后复查**是否出现 2026-07-16 之后第一个 `completed` —— 这是 A 的唯一验收
2. 定位 `runtime_tasks ... FOR UPDATE` 的行锁持有者（怀疑租约续期与任务自身更新在同一行上争用，或 transcript 路径的 `pg_advisory_xact_lock` 串联）
3. P0-A 充值（MiniMax 套餐 / DeepSeek 余额）—— 即使 A/C 全通，5 个 MiniMax agent 仍会撞 429
4. P0-D 51 个 agent 分类

##### 验收通过（2026-08-24 03:42:47Z）

```
★ trigger 全局最后一次 completed: 2026-08-24 03:42:47Z   （部署完成后 47 秒）
   上一次: 2026-07-16 09:30:32Z —— 中断 39 天
```

| 指标 | 修复前 | 部署后 12 分钟 |
|---|---|---|
| `completed` | 0（39 天） | **1**（`AIAnalyst`，28s，9 spans，落 durable result ref） |
| invocation span | 0 | **9** |
| `trigger_run` 会话 | 0 | **1** |
| `last_fired_at` 推进 | 0 | **2 个 trigger** |
| 卡死 `running` | 2,107 | **0** |
| `failed` | 128（前一版） | **0** |

`last_fired_at` 推进比 `completed` 更关键：它证明「skip 不推游标 → cron 永久判定为 due → 每分钟空转」这个自我强化循环从根上断了。

##### 三次提交

| 提交 | bug |
|---|---|
| `fcd5e4b8` | fire-and-forget 派发：无强引用、无租约、无完成核算 → 2,107 条无声堆积 |
| `aeb777e0` | `writer_epoch_rejected legacy run authority`（真根因）+ 全仓 13 处 loguru 地雷（隐藏它 38 天） |
| `de66ac4e` | FK 锁自死锁 —— **`aeb777e0` 引入的回归**，不是既有问题 |

`de66ac4e` 的机制：`ChatSession.runtime_task_id` 是 `ForeignKey("runtime_tasks.id")`，其 INSERT 在连接 A 上持 `FOR KEY SHARE` 直到提交；`aeb777e0` 把绑定夹在 INSERT 与 commit 之间，用连接 B 发 `FOR UPDATE` 同一行 → 两个连接在同一协程里互等 → 30s `statement_timeout`。修法是预生成 session id，让绑定发生在取锁的 INSERT 之前，同时满足 Session V2 约束与锁约束。

##### 本文档需撤回的三条判断

| 原判断 | 实际 |
|---|---|
| 「**不是异常**」——依据 30 天 `failed\|trigger`=0 推断外层 handler 从未执行 | handler 执行了，但 loguru 地雷让它在写终态前自爆。同一份证据符合两种解释，选错分支 |
| 「怀疑 GC / 取消 / 事件循环饥饿」 | 全都不是。自始至终是一个普通异常被吞掉 |
| 「`FOR UPDATE` 超时 = 容量泄漏打到 trigger 路径，**P1 应提级并入 P0**」 | 不是慢扫描、不是容量问题，是 `aeb777e0` 引入的锁冲突。**P1 提级的判据不成立，该结论撤回**；是否提级需在 trigger 稳定运行后重新评估 |

##### 仍未完成

- **69 个调度 trigger 的 `last_fired_at` 仍冻结在 2026-07-16 之前**，各自等 6h 在途守卫窗口到期后依次发火。需持续观察确认，尚未证实
- **P0-A 充值未做**：heartbeat 已出现 1 个 `failed`；5 个 MiniMax agent 必然撞 429，与本次修复无关
- `skipped/no_model` 仍在（P0-D 的 51 个 agent），符合预期
- `scripts/reconcile_orphaned_trigger_runs.py` 保留但已无用武之地——存量孤儿在 `fcd5e4b8` 部署重启时被启动回收器settle 完毕

#### P0-C　skip 路径不推进调度游标（**容量泄漏的真正机制**）

`trigger_daemon._evaluate_trigger`（`app/services/trigger_daemon.py:846`）：

```python
base = trigger.last_fired_at or trigger.created_at
cron = croniter(expr, local_base)
next_run = cron.get_next(datetime)
return local_now >= next_run          # last_fired_at 不动 → 永远为真
```

`last_fired_at` 只由 `_record_trigger_success_state` 推进，而 preflight skip 路径根本走不到那里。于是：

```
主模型为 NULL → preflight skip → last_fired_at 冻结 → cron 永远判定为 due
             → fire lease 按分钟切片（cron:{id}:{YYYYMMDDHHMM}）→ 每分钟一条 skipped RuntimeTask → 137 天
```

**速率模型（实测吻合）**：`空转/天 = 1440（有 ≥1 个卡住的 cron/interval/poll）+ 12（heartbeat 120 分钟节律）`

| 桶 | agent 数 | 速率 | 来源 | 占比 |
|---|---|---|---|---|
| 高频 | 23 | 1,452/天 | trigger daemon 每分钟 tick | **98.6%** |
| 低频 | 28 | 12/天 | heartbeat（0 个 enabled cron） | 1.0% |
| 另计 | 1 | 1,440/天 | 同一循环，reason=`plan_required` | — |

低频桶 28 个 agent **零** enabled cron/interval/poll，其 12 次/天全部来自 heartbeat；其中 6 个 `heartbeat_enabled=False` 仍在空转——印证 heartbeat 已是平台托管、该字段不再生效。

**因此 P0-C 的修复不是「退避」**：退避是给一个坏掉的时钟加延时。正确修复是 **skip 时也必须推进调度游标**（跳过错过的槽位），或直接挂起该 trigger。这样修才能同时覆盖 `plan_required` 等其它 skip reason。

### 修复顺序

```
P0-A 充值 / 升级 Token Plan        ← 无代码。不做这个，后面全部无效
        ↓
P0-B 修 trigger 终态写入 + 回收 2,108 个卡死 running  ← 有代码。这是产品故障本身
        ↓
P0-C skip 推进调度游标 + 堵源头 + 告警触达          ← 有代码。顺带砍掉 ~98.6% 写入
        ↓
P0-D 51 个 agent 分类与配模型                       ← 需 owner。放最后，因为前三步不做它无意义
```

### P0-D 分类表已生成

51 行，含 owner/邮箱、累计 token、建档日、最后人类交互、最后成功触发、会话数、卡住的定时任务名、空转速率，两种格式：

- `p0_agent_classification.csv`（可直接填「分类」列）
- `p0_agent_classification.md`（带勾选框）

两份文件**含 owner 邮箱，故未入库**，留在 session scratchpad：`/private/tmp/claude-501/-Users-example-owner-vc-saas-hiveclaw-main/a4df9afc-f0b1-460f-bfcc-7595730e6c18/scratchpad/`。

分类判据（实测）：**近 30 天有人类会话的只有 1/51**，最后一次成功触发全部早于 2026-07-16。
涉及 21 个 owner，前三名：SimonXu1212（10）、Leslie Lu（9）、Zhuocheng Shi（4）。
3 个 `.local` 为 Local Agent，零 enabled trigger，12 次/天全来自 heartbeat——建议从 heartbeat 调度排除。

### 验收

- P0-A：provider 额度恢复后，24h 内出现至少一次 `status=completed` 的 heartbeat
- P0-B：出现 2026-07-16 之后的第一个 `task_type=trigger AND status=completed`，且该次运行产出 invocation span 与 `trigger_run` ChatSession；2,107 个卡死 running 被判定为 terminal 或 needs_reconciliation；红测：进程重启/任务被销毁后在途 trigger 任务可被回收；红测：`/api/health` 在「tick 正常但零终态产出」时必须报 unhealthy
- P0-C：红测：preflight skip 后 `last_fired_at` 推进或 trigger 挂起，且同一 agent 24h 内不再产生 >2 条 skipped；对 `plan_required` 同样成立
- P0-D：51 个 agent 完成分类与处置，留执行记录；`no_model` 计数接近 0

### P0 证据表（生产只读，2026-08-23 08:03–08:14Z）

| 事实 | 值 |
|---|---|
| `no_model` skipped（24h） | 33,856（与 14:0x 首测同值 → 速率恒定） |
| 其中 `task_type=trigger` | 33,243 / 23 个 agent |
| 其中 `task_type=heartbeat` | 612 / 51 个 agent |
| `plan_required` + trigger | 1,440 / **1** 个 agent |
| `primary_model_id IS NULL`（未删除） | **51 / 92**（首版记 60/103，含已删除 agent） |
| 51 个 agent 累计 token | 2,257,173,741 |
| `tokens_used_total = 0` 的 | **1**（`MuhandeMacBook-Pro.local`） |
| 全库 `running` 任务 | 2,108，全部 unclaimed、无租约 |
| 其中 `task_type=trigger` | 2,107；**产出 span 的 0 个，建出 trigger_run 会话的 0 个** |
| 30 天内 `failed \| trigger` | **0** → 外层 `except Exception` 从未执行 → 不是异常 |
| 生产进程 uptime | 29 天 12:36（自 2026-07-24T19:51Z，未重启） |
| `pg_stat_activity` | 28 / max 500 连接，无长事务；`pg_locks` 3 个全 granted |
| 生产镜像内 import 自检 | 函数级 10 个 import 全部 OK；部署源码 == 本地源码 |
| `TICK_INTERVAL` | **15s**（cron event key 按分钟切片，故仍是 1 次/分钟） |
| `_TRIGGER_FIRE_INFLIGHT_STALE_SECONDS` | 21600（6h）→ 每个卡住的 trigger 每天重放 4 次 → 实测 72/天 |
| `/api/health` trigger_daemon | `healthy=true, tick_count=87928` —— **健康检查未覆盖终态产出** |
| FK `agents.primary_model_id` delete_rule | `NO ACTION` → **不是级联清空**，是显式置 NULL |
| `agents.updated_at` | 51 个全部聚在今天 → heartbeat 触碰导致，**不可用作取证** |

## P1：容量泄漏治理

### 事实

| 表 | 累计插入 | 累计删除 | 删除率 | 体积 |
|---|---|---|---|---|
| `runtime_tasks` | 2,322,010 | **0** | **0.0%** | **4,946 MB** |
| `runtime_budget_runs` | 1,627,782 | **0** | **0.0%** | 1,769 MB |
| `invocation_spans` | 665,589 | **0** | **0.0%** | 1,771 MB |
| `agent_activity_logs` | 610,198 | 1,282 | 0.2% | 1,159 MB |
| `chat_transcript_events` | 285,480 | 32,203 | 11.3% | 1,215 MB |

DB 12 GB / volume 13.5 GB，上限 48.8 GB → 剩 35.3 GB。三张零删除表约 61 MB/天 → **约 578 天触顶**。`runtime_budget_runs` 与 task 数 1:1（近 7 天各约 25 万），说明它随 P0 一同泄漏。

次生症状：`runtime_task_worker.last_error` = `QueryCanceledError`（`statement_timeout=30s`）；运维聚合查询在该表上不可用，EXPLAIN 显示 RLS filter `current_setting()` 不可下推、未设 tenant 上下文时逐行扫 232 万行；`agent_activity_logs` last_autovacuum = 2026-07-03（7 周前）、`chat_transcript_events` = 2026-07-17。

### 修复顺序（P0 完成后再评估紧迫性）

1. **retention 策略**：terminal 态记录按保留窗归档/删除。**`invocation_spans` 是 CLAUDE.md 明载的 canonical trace surface，只可归档不可删。**
2. **历史清理**：分批删除历史 `skipped` 行（小批次 + 间隔，避免长事务锁表），完成后 `VACUUM (ANALYZE)`。预计释放约 4 GB。
3. **autovacuum 调优**：对超大表下调 `autovacuum_vacuum_scale_factor`。
4. **运维可观测性**：为跨租户运维查询提供受审计的 BYPASS 通道或预聚合物化视图——否则表越大越查不动，问题越难发现。

**注意**：P0-C 落地后写入量降约 98.6%，日增从 61 MB 降到个位数 MB，578 天将大幅延长。**因此 P1 的第 1–3 项应在 P0 之后重新评估，不必与 P0 并行。**

**但 P1 有一条需要提前判断的线索**：`statement_timeout=30s` 已经在打运行时——`runtime_task_worker.last_error = QueryCanceledError`，`agent_activity_logs` 自 2026-07-03 未被 autovacuum 触碰。表越大越查不动是自我加强的。若 P0-B 的诊断指向查询超时，则 P1 不是「578 天以后的事」，而是 **P0-B 的共因**，须并入 P0。

---

## P2：B3 Company KB 首次真实数据验收

Company KB/Ontology 28 张表 `n_live_tup` 全为 0（已用不受 RLS 影响的统计复核）。代码、schema、RLS、双端前端均已在生产运行；`b3e0546f` 与本地 HEAD 在 backend/frontend 零差异；alembic head 生产=本地=`merge_incident_kimi_0725` 单头。

**唯一缺的是「从来没有人放进一份真实文档」。** 需 owner 提供测试租户 + 素材并授权写生产数据。

覆盖旅程：`source contract → ingest → proposal → review → publish → index → 授权 search/read/cite/explain → 同租户 deny 零侧信道 → Library deny → permission revoke 即时拒绝 → Agent submitted proposal（active publication 数不变）→ retire → new-version restore → event chain`；Ontology 侧 `install → activate（dry-run 真实执行 golden query）→ curate → review → publish → query/explain/simulate → retire/restore`。

**新增观察目标**：中文冲突对照集的**漏检率**。`FTS('simple')` 不分词，一份与现行制度矛盾的中文 PDF 可能因检索不到对照集而被判「无冲突」通过审核。B3 结果决定 dense 检索的优先级——这次会有真实数据，不是推断。

**与 P0 的协同**：两者都需要 owner 参与、都是「让产品真正开始被使用」。建议同一次 owner 时间窗内一并处理。

---

## P3：Memory 前端术语净化（SA-08 硬要求）

`kimi-review-report-2026-07-24.md` §11 第 14 项 SA-08 明载：**「整个员工 DOM 不得出现 T0/T2、heartbeat、Dream、runtime task/job id」**。当前**不满足**：

```
featureHub.memory.subtitle          = …T0 会话真相、T2/T3 蒸馏…
agent.evolution.memoryJobsHeading   = T3 记忆候选
agent.knowledge.distiller.t2_pipeline = 经历打包 (T0→T2)
personalKnowledge.profileEmpty      = 后端尚未产出 profile plane。
agent.knowledge.dreamCoverageHint   = Dream 提交前必须…
agent.knowledge.personalEmpty       = 当前 owner scope 的…
agent.knowledge.personalReadonlyDesc = Agent Detail 只负责检索和查看 Personal KB 证据；…个人知识库工作区。
```

三类问题：① 内部分层（T0/T2/T3/plane/Dream）暴露给业务用户；② 同一概念两种叫法同句混用（`Personal KB` 与「个人知识库」）；③ `AgentKnowledgeSection.tsx:62-65` 把 `self.entries + profiles.entries + knowledge.pages + milestones.pages` 相加成「长期记忆」单一数字——而 `memory-system-spec.md` §1.3 明确两平面蒸馏规律相反、不得混用。

另有一处耦合缺陷：`AgentKnowledgeSection.tsx:37-38` 用中文字符串作程序 key（`规避中:` / `已根除:`），后端改措辞即断。

**参照物是自家的 Company KB 文案**——它已经做对了（区分「加载失败/权限受限/无匹配」三种空、用「制度、手册和公司指引」这类业务语言）。方向不是新造一套，是把 Memory 拉到同一水准。原始术语保留在已有的 `raw` subview 里（`SubView` 已含 `raw`），那是它该待的地方。

**受众判断（需 owner 确认）**：Agent Detail 记忆页 owner 与管理者都会看。建议**业务语言为默认视图 + `raw` 子视图给运维**，而非在同一页混排两套语言。

---

## P4：SA-07 T0→T2 生产回填

与已完成的 two-plane migration（56/56 applied，2026-07-03）**是两件不同的事**。SA-07 是把历史会话回填为 T2 Segment Package，工具 `python -m app.scripts.backfill_t0_to_t2`（脚本存在），验收标准 `post_apply_inventory.coverage_complete=true`。

回填对象规模真实：`chat_transcript_events` n_live_tup **279,245**、`chat_sessions` **42,802**。

流程（review 原文）：逐 Agent 先 dry-run，保存 `sealed_segments`/`existing_t2_packages`/`invalid_t2_packages`/`candidate_segments`/`remaining_segments`/`batch_selection_complete`/warnings receipt；核对 T0 与 T2 实物守恒后，才可 `--apply --confirm APPLY_T0_TO_T2_BACKFILL` 分批重进 canonical LLM job。旧 `ChatSession.summary` 不得复制成 T2。

**成本提示**：回填走 canonical LLM job，27.9 万 transcript events 的 LLM 成本需先估算并由 owner 设上限。**建议在 P0/P1 之后、且明确成本预算后再启动。**

---

## P5：图投影流转施工

设计已定稿：`docs/company-knowledge-graph-projection-design-2026-08-23.md`（证据层抽取 + 图作为 publication 派生投影 + `begin_review` 守卫门 + override 留痕）。

**依赖 P2**，理由与补跑范围见「依赖与建议节奏」§B3 必须早于图投影。要点：B3 不只提供校准语料，它先回答「这套 review 流程用户是否愿意走」——而 P5 会让 review 更重。

设计文档 §10 有两项待 owner 定：R1 分析成本上限策略、R5 未定义 object type 时是否拒绝物化（建议拒绝，保持类型受控）。

---

## P6：已登记未建能力与北极星缺口

不在本轮承诺范围，仅登记：

- `proactive_employee_loop`（HN-01）、`memory/policy_replay`（HN-02）——明确登记未建设
- Memory 层双时间轴——`app/memory/` 内 `occurred_at|valid_from|mentioned_at` grep 零命中；**参考实现取自 Hive 自己的 ontology 层**（`company_knowledge_evidence` 已有 `occurred_at`/`effective_from`/`effective_until`/`observed_at`；`company_ontology_objects` 有 `valid_from`/`valid_until`/`observed_at`），不抄外部项目
- hermes-agent 同模型同任务行为级质量对标（§11 第 1 项）——Goal 1 的核心验收，未做
- 真 PG 故障注入 / 双进程恢复 / 真实浏览器 / Hive Connect 真机 / secret rotation（§11 第 2–7 项）
- `unified exec`、`execpolicy`、跨会话 `session_search`、`verify-on-stop`——§10.3 的「Codex 增量吸收建议」，**非已承诺漏做项**

---

## 依赖与建议节奏

```
① P0-A 充值/升级 Token Plan   ← owner，无代码。不做则以下全部无效
                    ↓
② P0-B 修 trigger 终态写入     ← 代码。产品故障本身；先做 span 诊断定因
                    ↓
③ P0-C skip 推进调度游标 + 堵源头 + 告警触达（代码）
                    ↓
④ P0-D 51 个 agent 分类        ← owner。分类表已生成
                    ↓
⑤ P1 重新评估紧迫性            若 P0-B 指向查询超时，P1 提前并入 P0
                    ↓
⑥ P5 = 图投影施工              用 B3 的语料与结论校准

P2 = B3       ── 需 owner 素材；可与 P0-B/P0-C 并行，但**须在 P0-A 之后**（ingest/抽取要调 LLM）
P3 术语净化    ── 与上列全部无依赖，全程可并行
P4 T0→T2 回填  ── 需先定 LLM 成本预算，且额度恢复后才可能跑
P6 登记未建    ── 不排期
```

**第一个 owner 时间窗只有一件事：P0-A 充值。** B3（P2）随后，因为它的 ingest / 抽取 / 冲突检测全都要调 LLM——在 429 状态下跑 B3 只会得到假阴性。

### B3（P2）必须早于图投影（P5）——三个理由

**① B3 是图投影的需求验证，不只是它的语料来源。**
Company KB 已 137 天零采用。B3 会回答一个比「能不能跑通」更根本的问题：**这套 review 流程，真实用户愿意走吗？** 若结论是「审核环节太重，用户宁愿把文档丢到群里」，则图投影须重新设计——**因为它会让 review 更重**（审核者要额外看冲突台账与事实变更清单）。先建图投影再发现这一点，等于在未验证的假设上盖楼。

**② B3 不写代码，图投影工程量不小。**
B3 = owner 提供租户与素材 + 跑旅程取证。
P5 = 两张新表 + alembic 单 revision + 异步作业 processor + `begin_review` 守卫 + `override_analysis_gate` 权限 + publish 物化 + retire/restore 联动 + 审核界面 + 双语 i18n。先做零代码的那个。

**③ 图投影设计的两个待定参数需要真实文档才能定。**
设计文档 §10 的 R1（LLM 成本上限策略）与 R5（未定义 object type 时是否拒绝物化），在零数据环境下无判断依据。3 份真实制度文档跑完即有依据。

### 代价：B3 有一段需要在 P5 上线后补跑

不是整条旅程失效，是审核环节一段：

| B3 验收项 | P5 上线后 |
|---|---|
| source contract → ingest → publish → 授权 search/read/cite/explain | **不失效**，授权链不变 |
| 同租户 deny 零侧信道 / Library deny / permission revoke 即时拒绝 | **不失效** |
| retire → new-version restore → event chain | **不失效** |
| Agent submitted proposal（active publication 数不变） | **不失效** |
| **proposal → review** | **需补跑**：新增冲突台账、事实变更清单、`begin_review` 守卫门、override 路径 |

补跑范围限于 review 环节。该代价换取的是「动工前已知流程是否可被接受」，判断为值得。

### 会翻转此顺序的条件

**若 B3 素材只有 1–2 份文档**，其作为「真实语料」的价值很低——没有可冲突的对象，冲突检测无法校准。此时 P2 与 P5 可并行：B3 只验授权链，P5 按设计施工，最后合并验收一次。

**素材建议：至少 3 份真实制度文档，其中一份刻意与另一份矛盾**（例如新旧两版报销标准）。这样 B3 同时完成两件事：验授权链，并测出中文 `FTS('simple')` 在冲突对照集检索上的**漏检率**——该数字直接决定 dense 检索是否要提前排期（见 P2「新增观察目标」）。

---

## 下个 session 从这里开始

### 立即可执行（无需 owner 决策，无代码）

1. **P0-B 定因**：取一个卡死 `running` trigger 任务的 `invocation_spans`，看执行停在哪个 span。这是把「07-16 起零 completed」从现象变成根因的最短路径，只读。
2. **P3 术语净化**：与其他所有项无依赖，可直接开工。清单见 §P3，改动面是 `frontend/src/i18n/{en,zh}.json` 与 `AgentKnowledgeSection.tsx:37-38 / 62-65`。

基线已取（2026-08-23 08:03Z）：`no_model` 24h = 33,856；`primary_model_id IS NULL` = 51/92；全库 `running` = 2,108。

### 卡在 owner 的唯一一件事

**LLM provider 充值 / 升级 Token Plan（P0-A）。** 在此之前，任何 agent 都不可能产生一次成功运行，B3 也跑不了。

51 个 agent 的分类（P0-D）**降级为第四优先**：分类表已生成，但前三步不修，配上模型也只会让它们加入那 2,108 个卡死的 running 任务。

### 同时需要 owner 准备的

**B3 素材**：≥3 份真实制度文档，其中一份刻意与另一份矛盾（用于测漏检率）。**在 P0-A 完成后**再跑。

### 本轮产出的文档（均未提交）

| 文件 | 职责 | 状态 |
|---|---|---|
| `docs/wip/production-remediation-plan-2026-08-23.md` | **行动方案（本文件）**— 优先级 / 顺序 / 依赖 / 待决 6 项 | 新增，未提交 |
| `docs/wip/memory-companykb-production-acceptance.md` | 诊断证据 — 生产实测、Codex 交叉核查、两处自我纠错 | 新增，未提交 |
| `docs/company-knowledge-graph-projection-design-2026-08-23.md` | P5 施工设计 — 证据层抽取 / 守卫门 / override 契约 | 新增，未提交 |
| `docs/memory-ontology-external-baseline-evaluation-2026-08-17.md` | Hindsight / Semantica 评估 v2 + 撤回记录 | 新增，未提交 |
| `CLAUDE.md` | Memory 章节 8 处校正 + 新增「状态权威顺序」 | 已修改，未提交 |

工作树另有 `docs/agent-environment-extension-convergence-architecture-2026-08-23.md`（**非本轮产出**）与 `.ultra/` 运行时文件，均未触碰。

**提交需单独授权，本轮未提交。**

### 复现要点（只读）

生产访问：`railway ssh --project dd959a13-19f9-497a-9704-42c310eae230 --environment production --service backend 'python3 -' < <脚本>`（stdin 管道，脚本不落生产磁盘）。本轮所用探针保留在 session scratchpad。

关键指标三条：

- `SELECT count(*) FROM runtime_tasks WHERE status='skipped' AND metadata_json->>'skip_reason'='no_model' AND created_at > now() - interval '24 hours'` — 止血前基线 33,856
- `SELECT count(*) FROM agents WHERE primary_model_id IS NULL` — 当前 60 / 103
- `SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC` — 容量趋势

**盘点纪律（重要）**：这是 RLS-FORCE 库。只读盘点必须用 `pg_stat_user_tables.n_live_tup` / `pg_class.reltuples`，或先 `SET app.current_tenant_id = 'BYPASS'`。直接 `SELECT count(*)` 在 `app_rls` 角色下会**静默返回 0 而不报错**——本轮已因此误报过一次（诊断文档 §3.6.5）。另注：`statement_timeout = 30s`，RLS 的 `current_setting()` filter 不可下推，对 232 万行表做聚合必超时，务必带 tenant 条件或用统计表。

---

## 待 owner 决定

| # | 决定项 | 我的建议 |
|---|---|---|
| 0 | **P0-A：是否立即充值 / 升级 Token Plan** | **立即做。** 唯一挡住全部恢复路径的一步，且不需要写代码 |
| 1 | 51 个 agent 的分类处置（P0-D） | 分类表已生成。**但建议等 P0-A/B/C 完成后再花你的时间**；3 个 `.local` 建议直接从 heartbeat 调度排除 |
| 2 | P0-C 修法：指数退避 vs 推进调度游标 | **推进调度游标或直接挂起**，不要退避。退避是给坏掉的时钟加延时，且不覆盖 `plan_required` 等其它 skip reason |
| 3 | B3 测试租户与素材 | 建议用真实制度文档，且刻意包含一份与现有内容矛盾的，以测漏检率 |
| 4 | P3 受众定位 | 业务语言默认 + `raw` 子视图给运维 |
| 5 | P4 LLM 成本上限 | 先对单个 agent dry-run 估算，再定全量预算 |
| 6 | P5 的 R1/R5 | R5 建议拒绝物化未定义 type |

---

## 修订记录

| 日期 | 变更 |
|---|---|
| 2026-08-23 | 首版。基于 §3.6 诊断建立统一优先级；确认 P0 为唯一「用户正在受损」项且首步无需代码；确认 no_model 为历史遗留而非当前创建流程缺陷 |
| 2026-08-23 | 补齐 P2→P5 依赖论证：B3 早于图投影的三个理由（需求验证 / 零代码优先 / 定 R1-R5 参数）、补跑范围表（仅 review 环节失效，授权链不失效）、翻转条件（素材少于 3 份则可并行）与素材建议（≥3 份且含一份刻意矛盾，用于测漏检率） |
| 2026-08-23 | 加入「下个 session 从这里开始」：立即可执行项、owner 唯一阻塞项（51 个 agent 分类）、本轮未提交产出清单、只读复现要点与 RLS 盘点纪律 |
| 2026-08-24 | **P0-B 验收通过。** `de66ac4e` 修复 `aeb777e0` 引入的 FK 锁自死锁（预生成 session id，绑定先于取锁 INSERT）。部署后 47 秒出现 2026-07-16 以来第一个 `completed`（`AIAnalyst`，28s，9 spans），`last_fired_at` 开始推进，卡死 running=0、failed=0。**撤回本文档三条判断**：非「不是异常」（是 loguru 吞了异常）、非 GC/饥饿、`FOR UPDATE` 超时非容量泄漏故 P1 提级判据不成立 |
| 2026-08-23 | **两次提交已部署生产（`fcd5e4b8` + `aeb777e0`），三服务均 SUCCESS。** 生产 traceback 定位真根因：`writer_epoch_rejected legacy run authority` —— trigger 把 `child_session_id` 绑定写在第一条 transcript event 之后，违反 Session V2 约束（随 `c50fea9d` 于 2026-07-16 10:54Z 上线）；隐藏它的是全仓 13 处 `logger.error(f"...", exc_info=True)` loguru 地雷（`KeyError: '"source"'` 使异常处理器在写终态前自爆），据此**修正本文档此前「不是异常」的判断**。已验收：卡死行 2,107→1、全部带租约、失败能写终态（30 天来第一次）。**未验收**：2026-07-16 后第一个 `completed` 尚未出现，最早 00:06Z 才可验（在途守卫 6h）。**新增阻塞**：`runtime_tasks ... FOR UPDATE` 行锁间歇超时 → P1 提级并入 P0 |
| 2026-08-23 | **P0-B 修复实现完成（未提交未部署）。** 删除 trigger daemon 的三处 fire-and-forget 派发，改由既有 `runtime_task_worker` 认领执行（强引用 + done callback + 租约 + fence）；trigger 任务建成 `pending`；`trigger` 加入租约可回收类型；新增会话绑定守卫与陈旧意图守卫（后者同时防止修复本身首次部署时放出 2,107 条历史 fire）；`mark_daemon_tick` 不再冒充成功，新增 `mark_daemon_outcome`；新增存量孤儿对账脚本（dry-run 默认 + 确认短语）。新增 12 条红测，推翻 6 条钉着 bug 的旧断言。验收：`pytest tests -q` → 7992 passed, 2 skipped |
| 2026-08-23 | **P0-B 定因闭合。** 在生产容器内复现前段：四个可疑 await 全部干净，整段 0.10 秒抵达 ChatSession 插入点 → 代码路径无问题，协程根本没运行。根因是派发方式：`_tick()` 把任务直接建成 `running` 绕过 worker 认领队列，裸 `asyncio.create_task` 不留强引用、不挂 done callback、不设租约，四条缺失叠加使协程死亡不可观测、不可恢复；唯一回收器是启动时的 orphan reconcile（指纹：30 天内 `needs_reconciliation\|trigger` 32 行，latest_created 2026-07-24 19:03Z 即最后一次部署重启前），而进程已连续运行 29.5 天。残留未证实项：具体是 GC/取消/饥饿哪一种杀死协程——不阻塞修复 |
| 2026-08-23 | **P0-B 定因推进。** invocation_spans 诊断：2,107 个卡死 trigger 任务**零 span、零 trigger_run 会话**，执行从未进入 `invoke_agent`；30 天 `failed|trigger`=0 证明外层 `except Exception` 从未执行，**故非异常而是挂起或任务被销毁**；在生产镜像内逐项排除 ImportError、陈旧部署、连接池耗尽、锁竞争、信号量为起因；嫌疑窗口缩至四个无守卫 await；标记 `asyncio.create_task` 未保强引用为结构性可疑点（待日志证实）；附带发现 `/api/health` 只测 tick 循环不测终态产出，是 38 天无人发现的直接原因 |
| 2026-08-23 | **P0 整节重写。** 生产只读复核证伪首版三个前提：① 51 个 agent 并非「从未运行」，累计消耗 22.57 亿 token；② 空转与 `no_model` 无关，`plan_required` 同样 1,440 次/天，机制是 skip 路径不推进 `last_fired_at` 导致 cron 永久判定为 due；③ 配模型不足以恢复服务——`task_type=trigger` 自 2026-07-16 09:30Z 起零 completed，2,108 个 running 任务无租约无法回收，且 provider 已返回 1,119 次 429 额度耗尽。P0 拆为 P0-A 充值 / P0-B 修终态 / P0-C 修调度游标 / P0-D 分类，owner 阻塞项从「51 个 agent 分类」改为「充值」 |

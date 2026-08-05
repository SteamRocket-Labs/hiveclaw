# Hive Runtime Pool Isolation Plan（2026-07-02）

状态：方案稿 v3。用于接续 `docs/performance-slimming-plan-2026-07-02.md` 的 F 组，把生产卡顿治理从“单进程瘦身”推进到“运行面隔离”。  
结论：目标不是减少业务执行，也不是杀掉 running sub-agent；目标是在业务需要大量 sub-agent / web research / workflow 的前提下，把低延迟 API、长任务执行、调度扫描、重读接口分成真实资源隔离域。**任何卸载都必须先有接手方；不能以停掉 trigger、heartbeat、自进化、workflow resume 或 IM channel 为代价换 API 速度。** Railway volume 审计后，迁移形态反转：现有挂 volume 的 `backend` 留守 runtime 重活，新建无 volume 的 `backend-api` 承接控制面。

## 0. Railway Volume 审计结论

官方文档事实（2026-07-02 复核，Railway Volumes reference：https://docs.railway.com/volumes/reference）：

- volume 是挂载到 service 的持久数据能力。
- caveats 明确写着：`Each service can only have a single volume`。
- caveats 明确写着：`Replicas cannot be used with volumes`。
- Railway 还会阻止同一挂 volume service 有多个 active deployments，以避免数据损坏。

对 Hive 的直接后果：

- 当前 `/data/agents` / `AGENT_DATA_DIR` 是 T0/T2/T3/session ledger/workspace 的 truth surface。
- `web_chat_turn`、channel run、memory/evolution、subagent/delegation child session、workspace/file tools 几乎都要读写这份 truth。
- 因此不能第一刀新建一个无 volume 的 `backend-worker`，再把这些核心任务迁过去；它拿不到 T0/workspace truth。
- 也不能指望挂 volume 的现有 `backend` 通过 replicas 横向扩，因为 Railway volumes 与 replicas 不兼容。

迁移方向因此倒置：

- **现有 `backend` 保持挂 volume，转为 runtime plane**：承接 Worker + Daemon/Scheduler + 初始 Read-Model，继续执行所有触碰 T0/workspace/memory 的重活，并重新打开 daemon/channel 接手方。
- **新建无 volume 的 `backend-api`，转为 control plane**：只承接 auth、轻 CRUD、start/cancel/steer、WS 订阅、轻量状态。它只写 DB/Redis 控制记录，不写 T0/workspace 文件。
- **Read-Model 独立 service 仍是读数驱动可选项**：第一刀可以留在 volume-backed `backend` 或 API 内独立 read engine；只有当读数证明重读仍拖垮控制面，且对应路径不需要 volume truth 时，再拆 service。
- **Worker -> API WS 事件总线仍必须做**：执行在 volume-backed runtime，WS 长连接在 `backend-api`，streaming token/tool/done 仍要跨进程转发。

这不是降低隔离目标，而是把 volume truth 留在重活一侧，把可水平扩的能力留给无 volume API。只有未来把 T0/workspace truth 迁到对象存储或 DB-backed workspace 后，才重新打开“新建独立 worker service 承接 volume-touching task”的路径。

## 1. 一句话总判

Hive 现在的问题不是“业务任务太多所以只能慢”，而是**所有业务面抢同一个 backend 进程、同一个事件循环、同一个 DB pool**。

一步到位的逻辑目标是四个 pool：

1. **API Pool**：只做低延迟控制面。
2. **Worker Pool**：执行所有 `RuntimeTask` 和大模型/工具 fanout。
3. **Daemon/Scheduler Pool**：只做扫描、投递、恢复，不直接执行重活。
4. **Read-Model Pool**：承载 transcript/workbench/activity/export 等重读接口。

这里的 pool 指**资源隔离域**：进程/服务隔离、DB engine/pool 隔离、并发预算隔离、队列 claim 隔离、观测指标隔离。不是只在代码里加四个 class 名字。

执行优先级不是四个 service 同时硬拆。Railway volume 审计后，强制顺序是：

0. **先承认 volume 约束**：触碰 T0/workspace/memory 的重活必须先留在现有 volume-backed `backend`。
1. **先建无 volume API Pool**：把低延迟控制面从现有 `backend` 拆出来，让它可以 replicas / `uvicorn --workers N` 横向扩。
2. **现有 backend runtime 必须接回 daemon/channel**：它是当前唯一能安全访问 volume truth 的执行面；不能关闭 trigger、heartbeat、workflow resume、IM channel。
3. **Worker Pool 先是 volume-backed backend 内的 role/budget/claim 隔离**：不是第一刀新 service；未来是否拆新 worker service 取决于 workspace truth 是否迁出 Railway volume。
4. **Read-Model 先用独立 engine/pool、statement timeout、限流拿 80% 收益**；只有读数证明重读仍拖垮 API Pool，且路径不依赖 volume truth 时，再拆独立 service。

## 2. 当前事实

源码和生产症状指向同一个结构性问题：

- `backend/app/database.py` 当前只有一个主 `engine` 和一个全局 `async_session`，池大小由 `DB_POOL_SIZE / DB_MAX_OVERFLOW / DB_POOL_TIMEOUT` 控制。
- `backend/app/config.py` 已写明单进程部署会让 HTTP、WebSocket、daemon、agent run 共用一个池；当前只是把参数环境变量化，没有真正分 role。
- `backend/app/main.py` 当前通过 `CORE_DAEMON_STARTUP_ENABLED` 和 `CHANNEL_STREAM_STARTUP_ENABLED` 控制部分 daemon/channel 是否启动，但 API service 仍然是唯一 backend 运行面。
- `backend/app/services/web_chat_runtime.py` 里 start run 后直接 `asyncio.create_task(execute_web_chat_run(...))`，也就是说 web chat run 仍在 API 进程内执行。
- `backend/app/agents/orchestrator.py` 的 async delegation/subagent 也是进程内 `asyncio.create_task(...)`。
- `backend/app/api/chat_sessions.py` 的 `workbench`、`export`、`transcript` 都走普通 API dependency `get_db`，即走同一 DB pool。
- Railway 在 2026-07-02 的 production service 快照是 `backend`、`frontend`、`Postgres`、`Redis`、`searxng`、`onlyoffice-documentserver`，没有独立 worker/read-model/daemon service。2026-07-14 起在线 Office 编辑已从源码拓扑退役；`onlyoffice-documentserver` 仅等待新预览链生产验收后的显式删除门，不再属于目标拓扑。

因此，A-E 瘦身能降低压力，但不能从架构上保证“runtime 抽干时 API 仍可用”。四池隔离是下一层根治。**如果 API 侧关闭 daemon/channel startup，却没有 daemon/worker 接手，这是功能停摆，不是可接受的最终状态。**

## 3. 设计原则

1. **业务优先**：不能通过杀 sub-agent、禁 web research、禁止 workflow fanout 来换响应速度。
2. **控制面保底**：用户必须始终能登录、看列表、开始/取消/steer run、订阅 WS、读轻量状态。
3. **执行面有预算**：Worker 可以高并发，但必须受 per-tenant、per-agent、per-task-type、per-tool-domain、全局 DB checkout 上限约束。
4. **调度不执行**：Daemon/Scheduler 只负责发现该做什么，并投递成 `RuntimeTask` 或 signal；不直接跑 LLM、不直接跑大量工具。
5. **重读不拖写入**：Read model 能慢、能限流、能缓存，但不能拖垮 API 控制面和 worker 持久化。
6. **兼容现有 running work**：切换期间不能把线上已 running 的业务 sub-agent 当作垃圾清掉；必须支持 drain、resume、orphan reconcile。
7. **不复活旧 Deep Research runtime**：历史 `task_type='deep_research'` 只作为兼容任务类型由 Worker 接管；未来 Deep Research V3 仍是 Skill + Workflow + Sub-agent，不新增专用 `deep_research_*` runtime/API/tool。
8. **先接手，再卸载**：任何从 API Pool 移走的能力，都必须先有 Worker/Daemon/Read-Model 接收路径、测试、监控和回滚开关。禁止出现“API 不跑、别处也不跑”的中间态。

## 4. 目标拓扑

```text
frontend / browser
  |
  |-- auth/list/start/cancel/steer/ws/light status --> backend-api (no volume)
  |
  |-- heavy read / legacy runtime / volume-backed paths ----> backend runtime (volume)

backend-api / API Pool (no volume)
  |-- creates RuntimeTask(status='pending') and DB-only control records
  |-- holds WebSocket subscribers
  |-- publishes run_queued / control events
  |-- subscribes to worker stream events and forwards to WS
  |-- can use replicas / uvicorn workers after daemon/runtime are absent
  |-- never executes model/tool fanout
  |-- never writes T0/workspace/memory files

backend runtime / Worker + Daemon + initial Read-Model Pool (volume-backed)
  |-- scans trigger/heartbeat/workflow waits/channel streams
  |-- creates RuntimeTask or workflow signal
  |-- claims pending/resumable RuntimeTask
  |-- executes model/tool/subagent/workflow/channel/memory/evolution work
  |-- persists transcript, T0 ledger, memory, workspace artifacts, spans, status
  |-- publishes stream events to API Pool event bus
  |-- cannot use Railway replicas while volume is attached

Postgres / Redis / agent volume
  |-- Postgres/Redis shared by both services with separate pool budgets
  |-- agent volume mounted only on existing backend runtime
  |-- Redis Pub/Sub or Streams carries Worker -> API WS events
```

同一份 backend image 可以用不同 `HIVE_PROCESS_ROLE` 启动：

- `HIVE_PROCESS_ROLE=api`
- `HIVE_PROCESS_ROLE=worker`
- `HIVE_PROCESS_ROLE=daemon`
- `HIVE_PROCESS_ROLE=read_model`

Railway 第一刀不是四个 service 同时落地。推荐初始 service 形态：

- `backend-api`：新建，无 volume，只跑 API Pool，允许 replicas 和多 Uvicorn worker。
- `backend`：沿用现有 service 和 volume，跑 Worker + Daemon/Scheduler + 初始 Read-Model。这里先通过 role-aware DB pool、内部 semaphore、claim/budget 隔离来治理重活。

未来可选 service 形态：

- `backend-daemon`：只有在 daemon 路径不需要直接写 volume，或仍能安全访问 workspace truth 时才拆。
- `backend-read-model`：只有当读数证明重读仍拖垮控制面，且路径能以 DB/cache 读模型完成时才拆。
- `backend-worker`：只有当 T0/workspace/memory truth 迁出 Railway 单 service volume 约束后，才可承接 volume-touching RuntimeTask。

存储前置条件已经有结论：任何会读写 `AGENT_DATA_DIR` / `/data/agents` 的 role（heartbeat、dream/evolution、memory consolidation、workspace/file tools、T0/T2/T3 写入等）必须暂留在现有 volume-backed `backend`，或先完成共享对象存储/DB-backed workspace 迁移。

## 5. 四个 Pool 的职责边界

### 5.1 API Pool

允许：

- auth / SSO / session refresh
- agent、tenant、user、skill、tool、trigger、workflow definition 的轻量 CRUD/list
- start run：创建 `RuntimeTask(status='pending')`
- cancel run：写取消 intent，必要时通知 Worker
- steer run：把用户追加消息写入 run mailbox / DB transcript record，不写 T0/workspace 文件
- WS 订阅：订阅 session/run 事件，不执行 run
- active run / run summary / permission resolve 的轻量状态面

禁止：

- 直接调用 `invoke_agent`
- 直接执行 `ToolRuntimeService.execute`
- 直接跑 `execute_web_chat_run`
- 直接展开 subagent/delegation/workflow leaf
- 启动 trigger/workflow/evolution daemon
- 启动长连接 channel stream manager
- 承载默认全量 transcript/workbench/export

API Pool 的成功标准不是吞吐最高，而是**尾延迟稳定**。当 Worker Pool 满载时，API 仍应能快速返回“已排队 / 正在运行 / 可取消”。

迁移清单不能只看 web chat。当前所有同步或 in-process agent execution 路径都要分类：

- `backend/app/api/websocket.py` 的兼容 `invoke_agent` 路径；
- `backend/app/api/tasks.py` / `backend/app/services/task_executor.py` 的 business `Task` execution；
- `backend/app/services/plan_mode_system_run.py`；
- HR / onboarding / agent creation 中可能同步唤起模型的路径；
- heartbeat/evolution/hook maintenance 中的模型调用。

最终 API role 内不得有未分类的 `invoke_agent()` / `ToolRuntimeService.execute()` / execution `create_task()`。如果某路径确实要保留同步执行，必须在迁移清单里明确解释为什么它不是 RuntimeTask，并单独给 DB/timeout/budget 约束。

### 5.2 Worker Pool

执行所有 RuntimeTask，包括：

| task_type / 来源 | 目标归属 | 是否触碰 volume | 说明 |
| --- | --- | --- | --- |
| `web_chat_turn` | 现有 volume-backed `backend` 的 Worker role | 是：T0/session ledger、workspace artifacts | web chat durable turn。`backend-api` 只写 DB pending/control，不写 T0；Worker claim 后先落 T0 user turn 再进模型循环。 |
| `goal_continuation` / `advanced_plan` / `team_member` | 现有 volume-backed `backend` 的 Worker role | 通常是 | 属于持续 agent run，可能写 transcript/T0/workspace。 |
| `subagent` / `delegation` / `a2a_delegation` | 现有 volume-backed `backend` 的 Worker role | 通常是 | 当前 in-process fanout 必须迁移为 claim 后执行；child session/T0/workspace 留在 volume-backed runtime。 |
| `workflow` | DB-only engine 可迁；含 leaf 执行留 volume-backed backend | 视 leaf 而定 | workflow 控制面偏 DB；leaf 若调用 subagent/tool/workspace，则按 leaf 的 volume 需求留在 runtime。 |
| channel run | 现有 volume-backed `backend` 的 Worker/Daemon role | 是 | Feishu/Slack/Discord/WeCom/DingTalk/Telegram/email inbound turn 的模型执行，通常写 session transcript/T0。 |
| memory/evolution/background jobs | 现有 volume-backed `backend` 的 Worker/Daemon role | 是 | Memory Gate、T2/T3 提炼、dream/evolution 等重活，必须访问同一 memory/workspace truth。 |
| legacy `deep_research` | 现有 volume-backed `backend` 的 Worker role | 通常是 | 仅兼容历史任务，不恢复旧专用 runtime。若历史任务实际不触碰 volume，可读数后再迁。 |
| business `Task` execution | DB-only 可迁；workspace 型留 volume-backed backend | 视 task 而定 | `backend/app/api/tasks.py` 当前 create 后 in-process execute，也应迁移；纯 DB 型可先迁到 API->RuntimeTask->backend claim，workspace 型不迁到无 volume service。 |

第 0 步 audit 结论已经回填：Railway volume 不能作为多 service 共享 workspace truth 使用。第一刀的 Worker Pool 是现有 `backend` 内的 runtime role/budget/claim 隔离，不是新建无 volume worker service。新 `backend-api` 只能写 DB/Redis 控制面，并通过 RuntimeTask + event bus 把重活交给现有 volume-backed backend。

Worker Pool 必须具备：

- claim loop：从 DB/Redis claim pending work。
- lease：防止多个 Worker 重复执行同一 run。
- heartbeat：运行中定期续租。
- graceful cancel：读取 cancel intent，停止后写 `killed` / `cancelled` 状态。
- orphan reconcile：Worker 重启后把超时 lease 的 `running` 任务恢复为 `pending` 或 `resumable`。
- per-tenant budget：避免一个租户抽干全局 worker。
- per-agent budget：避免一个 agent 的 fanout 抽干租户。
- per-task-type budget：例如 workflow/subagent/web_chat_turn 分开限额。
- per-tool-domain budget：web search/firecrawl/MCP/code execution 等外部 IO 需要独立 semaphore。
- DB checkout budget：worker 内部并发不能超过它自己的 DB pool 能承受的上限。

### 5.3 Daemon/Scheduler Pool

允许：

- trigger scan
- heartbeat scan
- workflow wait/resume scan
- channel stream / webhook polling / subscription receive
- schedule due check
- orphan task reconcile
- enqueue `RuntimeTask`
- enqueue workflow signal

禁止：

- 直接调用模型
- 直接执行工具
- 直接展开 subagent
- 在扫描事务里持连接等待外部 IO
- 每个 eligible agent 直接 `create_task` 执行重活

Daemon/Scheduler 的设计目标是“薄扫描器”。它发现工作后快速 commit，再由 Worker claim。

部署形态按读数决定：

- 第一刀：和 Worker 同在现有 volume-backed `backend`，以独立 role loop/独立 DB session budget 运行，恢复 trigger/heartbeat/workflow resume/channel streams。
- 可选后续：独立 `backend-daemon` service。只有当 daemon 路径不需要直接读写 `/data/agents`，或 workspace truth 已迁到可共享存储时，才拆。

无论最终是否独立 service，都不能把 daemon/channel 关掉后无人接手。`backend-api` 上 `CORE_DAEMON_STARTUP_ENABLED=false` / `CHANNEL_STREAM_STARTUP_ENABLED=false` 是正确方向；但现有 volume-backed `backend` 的 runtime role 必须打开对应接手方并验证 liveness。

### 5.4 Read-Model Pool

承载：

- transcript
- workbench
- activity
- export
- file/content preview 中的重读路径
- session/workflow/admin inspection 的大 payload 路径

必须具备：

- 默认分页，不允许默认全量。
- 最大 `limit` 和最大响应体积。
- 压缩和裁剪大 metadata/tool result。
- cache / ETag / sequence cursor。
- statement timeout。
- per-user/per-session 限流。
- 与 API 控制面分离的 DB pool。

Read-Model Pool 的慢不能阻塞 API Pool 的 start/cancel/steer，也不能阻塞 Worker 的 transcript/status 写入。

部署形态按读数决定：

- 默认先做**同 API service 内的独立 read-model engine/pool**：单独 pool size、statement timeout、response size limit、rate limit。这样不引入 nginx path 分流和第四个部署单元，也能先拿到大部分隔离收益。
- 只有当 Worker/Daemon 拆完后，生产读数仍显示 API Pool 被 transcript/workbench/activity/export 拖累，才升级为独立 `backend-read-model` service。
- 独立 service 不是验收条件；读数隔离才是验收条件。

## 6. DB Pool 预算

原则：先按 Postgres `max_connections` 设总预算，再分给四个 role。禁止每个 service 都默认 `20+10`，否则服务一多直接把 PG 连接上限打爆。

建议初始预算（需按生产 `max_connections` 和 Railway plan 复核）：

| Role | pool_size | max_overflow | pool_timeout | 说明 |
| --- | ---: | ---: | ---: | --- |
| API | 8 | 4 | 2s | 控制面快速失败，保尾延迟。 |
| Worker | 20 | 10 | 5s | 执行面主要吞吐，靠内部 semaphore 控制。 |
| Daemon | 4 | 2 | 3s | 扫描/投递，不应该需要大池。 |
| Read-Model | 6 | 4 | 5s | 可先作为 API service 内第二 engine/pool；重读面独立限流，避免抢 API。 |
| Schema/Admin | 2 | 2 | 30s | 迁移/DDL/repair 专用，不参与常态请求。 |

落地方式：

- 新增 role-aware DB settings，例如：
  - `API_DB_POOL_SIZE`
  - `WORKER_DB_POOL_SIZE`
  - `DAEMON_DB_POOL_SIZE`
  - `READ_MODEL_DB_POOL_SIZE`
  - 以及对应 `MAX_OVERFLOW` / `POOL_TIMEOUT`
- `app.database` 提供按 role 构造 engine/session factory 的能力。
- health 输出当前 role 的 pool 快照，并带 `process_role`。
- 部署检查必须验证：所有 service 的 `pool_size + max_overflow` 总和低于 PG 连接预算。
- 如果同一 service 内有多个 Uvicorn worker，预算按进程乘法计算：`process_count × (pool_size + max_overflow)`。

## 7. RuntimeTask Claim 契约

`RuntimeTask` 需要从“记录执行状态”升级为“跨进程可 claim 的执行队列”。

需要补齐字段或等价 metadata：

- `status`: `pending | running | suspended | resumable | completed | failed | killed`
- `claimed_by`: worker instance id
- `claim_expires_at`: lease 过期时间
- `attempt_count`
- `last_heartbeat_at`
- `priority`
- `scheduled_at`
- `cancel_requested_at`
- `queue_partition`: tenant/agent/type 分区键
- `idempotency_key`: start run 防重复

`status` 新值必须走 Alembic 迁移和消费方兼容审计。所有读取 `RuntimeTask.status` 的 API、admin/workflow ops、frontend 状态映射、metrics、清理脚本都要先接受新值，才能上线写入新值。

claim 语义：

```sql
select *
from runtime_tasks
where status in ('pending', 'resumable')
  and scheduled_at <= now()
order by priority desc, created_at asc
for update skip locked
limit :batch_size;
```

claim 后同一事务内写：

- `status='running'`
- `claimed_by=:worker_id`
- `claim_expires_at=now()+:lease_seconds`
- `attempt_count=attempt_count+1`

Worker 执行期间续租。Worker 崩溃后，Daemon/Scheduler 的 reconcile 把 lease 过期任务恢复为 `pending/resumable`，但必须尊重 task 类型的幂等和 resume 能力。

claim 延迟是用户体感指标，不是后台细节：

- `web_chat_turn` 从 API 创建 `pending` 到 Worker claim 的 P95 必须 < 200ms。
- 不能只靠低频 polling。API 创建任务后必须用 Redis Pub/Sub、Redis Streams、PG `LISTEN/NOTIFY` 或等价机制唤醒 Worker 立即 claim。
- 允许 polling 作为兜底，兜底间隔建议 <= 1s；web chat 不允许只靠纯 polling。
- health/metrics 必须暴露 `claim_lag_ms`、`pending_age_p95_ms`、`queue_wakeup_failures`。

## 8. Worker -> API WS 流式事件总线

这是拆 Worker 的硬前置。当前 `web_chat_runtime.broadcast_web_chat_event()` 直接调用同进程 `web_chat_broker`，API 进程持有 WebSocket 连接，执行代码也在同一进程内。拆分后 Worker 执行 run，API 持有 WS，必须设计 Worker -> API 的正向事件通路。

目标：

- Worker 产生 `chunk`、`thinking`、`tool_call`、`tool_result`、`permission`、`done`、`error`、`run_started`、`run_cancelled` 等事件。
- Worker 持久化关键 transcript/T0 事件。
- Worker 把流式事件发布到跨进程 bus。
- API Pool 订阅 bus，把事件转发给当前 WS subscribers。
- 浏览器断线或 API 重启后，通过 transcript/T0 replay + current run state 补齐可重放事件。

推荐底层：

- 首选 Redis Streams：保序、可 consumer group、可按 session/run 回放短窗口，适合断线重连补最近流式事件。
- Redis Pub/Sub 可作为低延迟广播层，但不能是唯一 truth；Pub/Sub 丢消息必须由 transcript/T0 replay 补。
- PG `LISTEN/NOTIFY` 可用于 Worker claim wakeup，不适合作为高频 token stream 主通道。

实施起点建议：按 `run_id` 建 per-run Redis Stream，设置分钟级 retention TTL；API 订阅当前 run/session 的 live stream，重连时先读 transcript tail，再从 last stream sequence 后追 live。per-session stream 可以后续按读数再评估，避免一开始把 stream 分区和清理做复杂。

事件 envelope：

```json
{
  "schema": "hive.web_chat.stream.v1",
  "tenant_id": "uuid",
  "agent_id": "uuid",
  "session_id": "uuid",
  "run_id": "uuid",
  "sequence": 1234,
  "event_type": "chunk",
  "payload": {},
  "created_at": "2026-07-02T12:00:00Z"
}
```

顺序保证：

- 每个 `run_id` 内必须单调 `sequence`。
- API 转发前按 `run_id + sequence` 去重。
- 同一 run 的事件乱序到达时，API 可以短暂 buffer；超过小窗口仍缺口时，前端依赖 transcript replay 修复。

背压：

- API WS subscriber 慢时，不能阻塞 Worker。
- API 对每个 WS connection 设置 outbound queue 上限；超过上限时合并 chunk 或断开并要求客户端重连 replay。
- Worker 发布 bus 失败时，关键事件仍先落 DB/T0；API 可通过 replay 看见最终状态。

重放：

- `chunk/thinking` 这类高频流式事件可以只保短期 Redis Stream retention，用于秒级断线恢复。
- `tool_call/tool_result/permission/done/error` 必须进入 durable transcript/T0。
- 客户端重连时先拉 transcript tail，再订阅 live bus，从 `last_sequence` 后继续。

验收：

- Worker 和 API 不同进程时，web chat 首 token、chunk、tool_call、done 能实时显示。
- API 进程重启不取消 Worker run。
- 浏览器断线重连后，已 durable 的事件不丢。
- Redis bus 短暂不可用时，run 不失败；最多退化为无 live token、靠 transcript tail 追上。

## 9. 并发预算

Worker Pool 初始推荐预算：

| 维度 | 默认值 | 说明 |
| --- | ---: | --- |
| global running RuntimeTask | 24 | 单 volume-backed runtime service 内初始总执行上限；按每 run 平均 DB 并发 0.3-0.5 与 Worker 20+10 池预算匹配，读数稳定后再升。 |
| per tenant | 12 | 单租户不能抽干全局。 |
| per agent | 6 | 单 agent fanout 有边界，但不禁止业务 fanout。 |
| `web_chat_turn` | 8 | 用户交互优先。 |
| `subagent` / `delegation` | 16 | 允许高并发，但受 tenant/agent 双限。 |
| `workflow` | 8 | workflow leaf 还要受 subagent/tool 预算。 |
| channel run | 8 | 渠道消息避免饿死 web chat。 |
| memory/evolution | 4 | 后台学习不能抢交互路径。 |
| web research tool domain | 8 | search/fetch/firecrawl/anysearch 类统一限额。 |
| MCP tool domain | 6 | 避免 governance/MCP lookup 放大。 |
| code execution | 4 | sandbox/provider 独立限额。 |

预算命中时行为：

- 不丢任务。
- 保持 `pending` 或 `queued`。
- API 返回明确 queued 状态和当前位置/原因。
- UI 展示“排队中”，而不是让请求一直挂住。

## 10. 路由与部署

### 10.1 Backend Process Role

新增：

```text
HIVE_PROCESS_ROLE=api|worker|daemon|read_model
```

不同 role 的启动行为：

| Role | FastAPI | WS | claim loop | daemon scan | channel stream | heavy read endpoints |
| --- | --- | --- | --- | --- | --- | --- |
| api | yes | yes | no | no | no | no/default disabled |
| worker | optional admin only | no | yes | no | no | no |
| daemon | optional health only | no | no | yes | yes | no |
| read_model | yes | no | no | no | no | yes |

`runtime plane` 是 Railway 部署 profile，不要求新增一个独立 enum。第一刀的现有 `backend` 可以用同一镜像同时启用 `worker` claim loop、daemon scan、channel stream 和 read-model engine，但它们必须有独立 DB session budget、内部 semaphore 和 health 指标。若后续实现发现单 env enum 不够表达组合，可以新增 `HIVE_ENABLE_WORKER_LOOP`、`HIVE_ENABLE_DAEMON_LOOP`、`HIVE_ENABLE_CHANNEL_STREAMS` 这类布尔开关；原则是 API role 不启 runtime loop，volume-backed backend 启 runtime loop。

API role 拆干净后（无 daemon、无 in-process run），才真正解锁 `uvicorn --workers N` 横向扩展。启用多 worker 前必须按进程数重算 API DB pool 总预算，且确认 daemon/channel 不会在每个 Uvicorn worker 里重复启动。

### 10.2 Railway Services

Railway volume 审计后的推荐第一刀：

- `backend-api`：新建 service，无 volume，`HIVE_PROCESS_ROLE=api`。只承接控制面和 WS 订阅；可以配置 replicas / `uvicorn --workers N`，但 DB pool 预算必须按进程数乘法重算。
- `backend`：沿用现有 service 和 volume，使用 `worker+daemon+channel+read_model` 的 runtime deployment profile。承接所有 RuntimeTask 执行、daemon/channel、T0/workspace/memory 写入、初始重读接口。

可选后续形态：

- `backend-daemon`：读数证明 daemon 需要独立，且 daemon 不直接依赖 volume truth 后再拆。
- `backend-read-model`：读数证明重读仍拖垮控制面，且对应读模型可以不直接读 volume 后再拆。
- `backend-worker`：T0/workspace/memory truth 迁出 Railway volume 单 service 约束后再拆。迁出前禁止让无 volume worker 承接 volume-touching task。

production 切换完成前，不能让 `backend-api` 和现有 `backend` 同时执行同一类 run，也不能让 `backend-api` 停掉某类后台能力后无人接手。现有 `backend` 关不掉；它要从“混合 API + runtime”收敛为“volume-backed runtime plane”。

### 10.3 Frontend / Nginx Routing

路径建议：

| 路径 | 目标 |
| --- | --- |
| `/api/auth/**` | `backend-api` |
| `/api/agents`、`/api/users`、`/api/enterprise` 轻量 CRUD | `backend-api` |
| `/api/agents/{id}/sessions/{sid}/runs/**` | `backend-api`，只写 DB pending/control |
| `/api/ws/**` / WebSocket | `backend-api` |
| `/api/agents/{id}/sessions/{sid}/transcript` | Read-Model Pool；第一刀可仍在 volume-backed `backend`，或 API 内独立 read engine，按读数切 |
| `/api/agents/{id}/sessions/{sid}/workbench` | Read-Model Pool；第一刀可仍在 volume-backed `backend`，或 API 内独立 read engine，按读数切 |
| `/api/agents/{id}/sessions/{sid}/export` | Read-Model Pool；超阈值走 async export task |
| `/api/agents/{id}/activity/**` | Read-Model Pool；按读数决定是否留 backend |
| 未分类重接口 / workspace 文件 / volume-backed legacy path | 现有 `backend`，直到完成分类和替代 |

实现可以通过 frontend nginx path routing、Railway internal domain，或 API gateway。禁止在 `backend-api` 内部同步 proxy 大响应，否则隔离失效。也禁止把未分类重接口误路由到 `backend-api`，否则无 volume API 会重新承担 runtime 压力或直接碰到文件 truth 缺失。

## 11. 代码改造范围

### 11.1 Database

- `app.database` 增加 role-aware engine/session factory。
- `get_db` 默认绑定当前 role session factory。
- `tenant_scoped_session` 支持显式 role/session factory。
- health 暴露 role、pool 快照、claim loop 状态。

### 11.2 RuntimeTask Queue

新增服务：

- `RuntimeTaskQueueService`
- `RuntimeTaskClaimService`
- `RuntimeTaskLeaseService`
- `RuntimeTaskBudgetService`

所有 start/run 入口改成：

1. API/Daemon 写 `RuntimeTask(status='pending')`。
2. Worker claim。
3. Worker 执行现有 execution 函数。
4. Worker 写完成状态和 transcript/span。

### 11.3 Web Chat

当前：

- `start_web_chat_run_from_saved_turn` 创建 `RuntimeTask(status='running')` 后直接 `create_task(execute_web_chat_run(...))`。
- saved-turn 路径当前会在 API 侧预写用户消息/T0，然后同进程启动执行。迁到无 volume `backend-api` 后，这个写点不能留在 API。

目标：

- `backend-api` 创建 `RuntimeTask(status='pending')`，并只写 DB 控制记录：`ChatMessage` / run mailbox / pending user payload / idempotency key。它不得调用 T0 append，也不得访问 `/data/agents`。
- `backend-api` broadcast `run_queued`，不要伪造已经执行的 `run_started`。
- volume-backed `backend` Worker claim 后，先把本轮 user turn materialize 到 T0/session ledger，确认 sequence/order 后再调用 `execute_web_chat_run`。
- Worker claim 后将状态改 `running` 并执行 `execute_web_chat_run`。
- Worker 通过 §8 事件总线发布 chunk/tool_call/done 等流式事件，API 转发到 WS。
- cancel/steer 通过 DB mailbox + broker 通知 Worker。

专门测试必须钉住：

- 无 volume API start run 不调用任何 T0/workspace 文件写接口。
- Worker claim 后，在第一次模型调用前写入 user turn T0 event。
- T0 append-only sequence 与原 saved-turn 语义一致：user turn 在 assistant/tool events 之前，重放 transcript 不丢首条用户消息。
- API 重启或 WS 断开不取消已经 pending/running 的 Worker run。

### 11.4 Subagent / Delegation

当前：

- orchestrator 内部 `create_task(_run())`。

目标：

- spawn/delegate 写 `RuntimeTask(status='pending', task_type='subagent'/'delegation')`。
- Worker claim 后执行 `_run` 等价逻辑。
- parent 等待场景通过 runtime task future/poll/broker 收结果；async 场景直接返回 task handle。

### 11.5 Workflow

目标：

- `preview_workflow` 仍是 API/agent tool 的轻量预检。
- `start_workflow` 只创建 `RuntimeTask(task_type='workflow', status='pending')`。
- Worker 执行 workflow engine 和 leaf calls。
- Daemon/Scheduler 只恢复 suspended/waiting workflow。

### 11.6 Channel Runtime

目标：

- webhook/stream receive 在 Daemon/Scheduler 或 API 写入 inbound message 和 `RuntimeTask`。
- Worker 执行模型回复和 channel delivery。
- channel stream manager 不再和 API process 共用启动面。

### 11.7 Read Model

目标：

- transcript/workbench/export/activity 先迁入独立 read-model engine/pool。
- 默认分页/压缩/裁剪继续保留。
- 大 export 可以返回 async export task，超阈值不在 HTTP 请求里同步生成。
- 只有生产读数证明 API 控制面仍被重读拖累，才迁入独立 Read-Model service。

## 12. 实施顺序

这是实现顺序，不是允许上线半成品。Production cutover 必须在完整兼容测试通过后进行。

0. **Volume/Storage audit 已完成并回填**：Railway volume 是 service-bound；挂 volume service 不能 replicas；触碰 `/data/agents` 的任务第一刀留在现有 `backend`。
1. **文档与契约**：本文件，明确倒置后的 role/pool/task 分类：现有 `backend` = volume-backed runtime，新增 `backend-api` = no-volume control plane。
2. **Red tests**：写失败测试，证明 no-volume API start run 只写 DB、不写 T0、不执行；volume-backed Worker claim 后先写 user-turn T0 再执行；Worker -> API WS bus 可流式转发；Daemon 在 runtime plane 有接手方；Read-Model 独立限流。
3. **Role-aware settings/database**：不同 role 拥有独立 pool 配置和 health surface；`backend-api` 多进程预算按 replicas/workers 乘法计算。
4. **RuntimeTask claim/lease/budget + wakeup**：跨进程 claim、即时唤醒、续租、取消、orphan reconcile。第一执行方是现有 volume-backed `backend`。
5. **Worker -> API WS event bus**：先把正向流式事件跑通，否则不能把 WS 长连接迁到 `backend-api`。
6. **Web chat vertical slice**：`backend-api` 创建 pending run，现有 `backend` claim 并写 T0/执行；这是用户体感主路径。
7. **Subagent/delegation migration**：把 fanout 从 in-process `create_task` 迁到 volume-backed Worker claim，不迁到无 volume service。
8. **Workflow/channel/background migration**：统一纳入 Worker/Daemon 边界，且保证 daemon/channel 在现有 `backend` runtime plane 中恢复，不停摆。
9. **Read-Model engine split**：transcript/workbench/activity/export 先走独立 read engine/pool；是否路由到独立 service 看读数和 volume 依赖。
10. **Railway service split**：新增 `backend-api`，配置无 volume、控制面 env、API DB pool；现有 `backend` 配置 runtime env、volume、Worker/Daemon DB pool 和并发预算。
11. **Production drain/cutover**：先启 `backend-api` shadow/小流量，再按路径反向路由控制面；现有 `backend` 保留 runtime，观测 running/orphan/queue/backlog。

## 13. 测试门

### 13.1 Unit / service tests

- API start run 创建 `RuntimeTask(status='pending')`，不调用 `execute_web_chat_run`。
- no-volume `backend-api` start run 不调用 T0/workspace 文件写接口，只写 DB 控制记录。
- volume-backed Worker claim 后，在第一次模型调用前写入 user-turn T0 event。
- T0 user-turn sequence 保持在 assistant/tool events 之前，transcript replay 能恢复完整首屏。
- Worker claim 使用 `FOR UPDATE SKIP LOCKED`，并发 claim 不重复。
- API 创建 web chat task 后通过 wakeup 触发 Worker claim，claim P95 < 200ms。
- lease 过期任务可 reconcile。
- cancel intent 能被 running Worker 读取并停止。
- budget 命中时任务保持 queued，不丢失。
- per-tenant/per-agent/per-task-type 限额生效。
- `deep_research` legacy task type 只走兼容 worker，不出现新专用 runtime/tool/API。
- Worker stream event envelope 单调 sequence、API 去重、慢 WS subscriber 背压策略生效。

### 13.2 Integration tests

- web chat：`backend-api` start -> queued -> volume-backed `backend` worker claim -> user turn T0 write -> API WS receives chunk/tool_call/done -> transcript replay -> completed。
- steer：running turn 接收追加用户消息。
- cancel：API cancel 后 Worker 停止，状态变 killed/cancelled。
- subagent fanout：父任务可收到子任务结果，且预算生效。
- workflow：start_workflow 创建 workflow task，Worker 执行 leaf，Daemon 恢复 suspended workflow。
- channel inbound：stream/webhook 投递后由 Worker 回复。
- read model：transcript/workbench 走独立 read engine/pool；独立 service 为读数驱动可选验收。
- Railway role smoke：`backend-api` 没有 volume mount 也能 start/cancel/steer/WS；现有 `backend` 有 volume mount 并能写 T0/workspace/memory。

### 13.3 Production smoke

- Worker 满载时：
  - `/api/health` p95 < 200ms
  - login/list/start/cancel/steer p95 < 500ms
  - API DB pool saturation < 50%
- Worker Pool：
  - queue backlog 可观测
  - no duplicate claim
  - no stuck lease beyond reconcile SLA
- Stream bus：
  - web chat live token/tool/done 跨 Worker/API 进程可见
  - reconnect 后可由 transcript tail 追上
  - API 重启不取消 Worker run
- Read-Model Pool：
  - transcript 首屏 < 100KB
  - workbench 默认 < 100KB
  - 499 不再成簇
- Daemon/Scheduler：
  - trigger/heartbeat 不直接执行模型
  - scan transaction 短持连接

## 14. 观测面

每个 role health 输出：

```json
{
  "process_role": "api",
  "db_pool": {
    "size": 8,
    "checked_out": 2,
    "capacity": 12,
    "saturation_pct": 16.7
  },
  "runtime_queue": {
    "pending": 42,
    "running": 18,
    "stale_leases": 0,
    "claim_lag_p95_ms": 120
  },
  "budgets": {
    "tenant_saturated": 1,
    "agent_saturated": 3,
    "tool_domain_saturated": 2
  }
}
```

必需日志：

- `runtime_task_claimed`
- `runtime_task_lease_extended`
- `runtime_task_budget_deferred`
- `runtime_task_cancel_requested`
- `runtime_task_cancelled`
- `runtime_task_orphan_requeued`
- `runtime_task_wakeup_sent`
- `runtime_task_wakeup_missed`
- `web_chat_stream_published`
- `web_chat_stream_forwarded`
- `web_chat_stream_replayed`
- `web_chat_stream_backpressure`
- `read_model_limited`
- `api_heavy_endpoint_rejected`

## 15. Cutover 与回滚

### Cutover

1. 部署代码但保持现有 `backend` 兼容旧路径；`backend-api` 先不上主流量。任何尚无接手方的 daemon/channel/in-process execution 不关闭。
2. 把现有 volume-backed `backend` 配成 runtime plane：打开 Worker claim dry-run、Daemon/Scheduler liveness、channel stream liveness，只记录会 claim 的任务，不执行或不切主路径。
3. 新建 `backend-api`，无 volume，打开 health/auth/light CRUD shadow smoke；验证它不会访问 `/data/agents`。
4. 打开 RuntimeTask claim/wakeup 的小流量真实执行，执行方仍是现有 volume-backed `backend`。
5. 打开 Worker -> API WS event bus shadow mode：runtime 发布，`backend-api` 订阅但不影响旧 WS 路径。
6. 启用 `web_chat_turn` 垂直切片：`backend-api` start run 只写 DB pending，现有 `backend` claim 后先写 user-turn T0 再执行，WS 事件由 bus 转发。
7. 按路径把控制面流量路由到 `backend-api`：auth、轻 CRUD、start/cancel/steer、WS。未分类重接口、workspace 文件、volume-backed legacy path 继续走现有 `backend`。
8. 启用 subagent/delegation/workflow/channel/background 的 claim/budget 隔离，但执行仍留现有 volume-backed `backend`，直到 workspace truth 迁出。
9. 启用 read-model engine split；只有读数需要且 volume 依赖已消除时，再做 read-model service route split。

### 回滚

回滚原则：不删除任务、不清 running 状态。

- 把控制面路由切回现有 `backend`。
- 关闭 `backend-api` start/cancel/steer/WS 主流量。
- 关闭新 claim 路径时，现有 `backend` 可临时恢复旧 in-process execution 开关。
- 如果 daemon 接手方不可用，现有 `backend` 必须恢复 core daemon/channel startup 或保留原接手方；不能让 trigger/heartbeat/channel 长时间停摆。
- Reconcile 把未执行的 `pending` 保留。
- 已 running 且 lease 未过期的任务等待 Worker drain；lease 过期后按 task resume 能力恢复。

## 16. 明确不做

- 不通过杀 running sub-agent 止血。
- 不通过减少业务 fanout 作为架构方案。
- 不通过停掉 trigger、heartbeat、dream/evolution、workflow resume、channel streams 来换 API 速度。
- 不恢复旧 Deep Research 专用 runtime/API/tool。
- 不直接给 API service 加 uvicorn 多 workers 后仍让 daemon 在每个 worker 里跑。
- 不让 API Pool 同步 proxy Read-Model 大响应。
- 不让所有 Railway service 继续使用同一组 `DB_POOL_SIZE=20 / DB_MAX_OVERFLOW=10`。

## 17. 最终验收

这件事完成的标准不是“多了几个 service”，而是：

- Worker 满载时，API Pool 的 start/cancel/steer/WS/light read 仍稳定。
- RuntimeTask 全部经 claim/lease/budget 执行，没有 in-process 无预算 fanout。
- Worker -> API 的 WS 流式事件总线可用，live token/tool/done 不因进程拆分丢失。
- Daemon/Scheduler 有接手方，只投递不执行，trigger/heartbeat/workflow resume/channel streams 不停摆。
- Read-Model 的大响应不会拖垮 API/Worker；是否独立 service 由读数决定。
- 每个 role 的 DB pool 占用、queue backlog、budget defer 都可观测。
- 线上不再出现“业务任务一多，前端整体卡死/API health 也慢”的模式。

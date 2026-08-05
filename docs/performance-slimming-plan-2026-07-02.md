# Hive 生产性能瘦身方案（2026-07-02）

状态：诊断已完成（三路源码审计 + 生产日志读数），措辞按 owner 复核修订（v2）。方案待拍板。

## 0. 一句话总判

生产卡顿不是单一故障，是四层叠加：**同步 IO 阻塞事件循环（强证据，待 app-side timing 终证）→ 重接口（workbench + transcript）→ 连接池饱和（结构性风险已实锤，占用源贡献度待 C2 读数）→ 前端双轨轮询持续供压**。四层各修各的根，任何一层单独修都只是缓解。"瘦身"的正确对象是运行时（响应大小、查询量、阻塞点、连接占用），不是代码行数。

## 1. 症状与生产读数（2026-07-02）

- `/api/health` TTFB 2.6–4.9s——该端点**不查 DB**（`main.py:656`，只读内存快照）。**强烈指向**事件循环/进程级阻塞而非仅连接池排队；但公网采样经过 Railway edge/网络链路（静态资源同窗口也偶发极慢），最终确认需容器内或 app-side timing（已纳入 C2 观测项）。
- QueuePool 错误持续且成簇：`QueuePool limit of size 20 overflow 10 reached, connection timed out, timeout 30.00` 从 01:40 到 04:52（采样时刻）持续出现，且多次**同一秒 3 条**（01:47:44-46、04:22:07、04:45:36）——池饱和瞬间所有排队者一起超时的典型形态。
- 重接口读数（frontend nginx 日志 300 行窗口）：
  - `transcript`：**33 次请求，11 次 499**（用户等不及、客户端主动断开，放弃率 1/3），成功响应平均 **465KB**；
  - `workbench`：19 次请求，成功响应平均 **1.88MB**（峰值 2.77MB）；nginx 报 `upstream response is buffered to a temporary file`；
  - transcript 请求频率约为 workbench 的 **2 倍**——transcript 与 workbench 同为必拆的重接口。
- RLS 次生报错 `Failed to restore tenant scope after BYPASS: Can't reconnect until invalid transaction is rolled back`：owner 早前在日志中观察到；本次最近 300 行窗口内未复现（疑被 QueuePool 错误刷出窗口）。列为待复核症状，修复项 C3 照做。
- 静态资源偶发极慢（vendor 427KB / 37.8s），但 assets 缓存头正确（`public, immutable`）→ 判为 Railway edge/链路抖动，非本轮目标。

## 2. 根因链（全部已源码核验）

### L1 — 事件循环被同步 IO/CPU 阻塞（为什么"全局都慢"；强证据，待 app-side timing 终证）

阻塞机制本身是源码实锤；"health 慢完全由它解释"是强推断（公网采样含 edge 链路），终证靠 C2 的 app-side timing。

- `memory/t0/ledger.py:399` `replay_t0_session_events` 是**纯同步函数**：遍历该 session 全部 segment，同步读取并解析**全部** `events.jsonl`。
- `session_command_runtime.py:376` `_load_events` 在 async 路径直接调用它（无 `to_thread`），全量读完才 `[:limit]` 截断；全仓约 10 个调用点（workbench / export / branch / rewind 等 session 命令）。
- `api/chat_sessions.py:1392` workbench 返回裸 dict，FastAPI 默认 JSON 编码器在事件循环上序列化 MB 级响应。
- `entrypoint.sh:202` uvicorn 无 `--workers` → **单进程单事件循环**，API + 全部 daemon + 全部 agent run + channel 长连接同一个 loop。
- 附带疑似功能 bug：`[:limit]` 取的是**最早** 1000 条（`_load_events` 返回按 sequence 升序）。超过 1000 events 的 session，workbench 的 `latest_event` / active turn 推导可能一直停在旧数据上。修 A1 时一并核验并写测试钉死。

### L2 — 重接口：workbench 与 transcript（为什么单次请求那么贵）

- `session_control_plane.py:1327` `build_session_workbench`：`timeline_limit=1000`，events 之外**串行** await：active_run、runtime_tasks、goals、teams（每个 team 再单独查 members，N+1，`:1274`）、workflow_journals、session_index、approvals、branches。
- 同一响应里 `timeline` / `tool_calls` / `hooks` / `compactions` / `context_window` 全部由同一份 events 派生，重复膨胀。
- `api/chat_sessions.py:1812` transcript 端点**已有分页参数**（`after_sequence` + `limit`），但默认 `limit=500` 一次就是 465KB 平均响应，且前端 `getSessionTranscript` 每次切 session 从 0 全量拉、未用增量参数。生产读数：请求频率 2× workbench、499 放弃率 1/3。
- `database.py:118` `get_db` 请求级持锁：慢端点整个生命周期占住 1 条连接。

### L3 — 连接池饱和（为什么 QueuePool timeout 30s）

- `database.py:29-34` `pool_size=20` / `max_overflow=10` **硬编码**（违反自家 forbidden_patterns "Hardcoded config"），`pool_timeout` 未设 = SQLAlchemy 默认 **30s**，与日志逐字吻合。单进程 → 全应用共享这一个 30 连接池。
- **daemon 无上限 per-agent 扇出**（**结构性高风险占用源**，源码实锤；对池饱和的实际贡献度需 C2 pool 指标确认）：`heartbeat.py:2118`、`trigger_daemon.py:301,1888` 每个 eligible/fired agent 一个 `create_task`，无任何 semaphore；`trigger_daemon.py:1689` 还会派发无上限的 auto_dream。heartbeat 60s tick、trigger 15s tick。一批 agent 同时到期（如重启后）→ 同时 checkout 数可突破 30。
- 工具路径高频短 session churn：每次 tool call 顺序开多个短 session（`tools/service.py:473,505`、`governance_resolver.py:75,82`、每 tool call/chunk 的 T0 + event 持久化），单个都短，但并发下"同时被 checkout 的总数"超限。注意：线上报错栈多落在 `tools/service.py` 的 policy/session 读取路径，但**报错点 ≠ 占用源**——池被抽干后所有 checkout 点都会报错，谁占着池要靠 C2 读数回答。
- **已排除**：invoke_agent 全链是短借短还（`web_chat_runtime.py:3162` 外层无 session 包裹，持久化回调各自短开短还）——**不存在**"持连接等 LLM"的经典泄漏。
- `database.py:230-241` `enter_rls_bypass` 的 finally 在 invalid transaction 上直接 `execute`，restore 二次报错——这是池雪崩的**次生噪音**，不是根源，但要修。

### L4 — 前端持续供压（为什么池一直回不了血）

- `AgentDetail.tsx:2213` `chat-active-run` **3s 轮询、无条件常开**（session 完全 idle 也一样）：idle chat tab 稳态 26 req/min，live 时 46 req/min（含 `:2206` runtime-summary 10s、`ChatWorkLedgerDock.tsx:90,109` live 双 3s）。
- `AgentDetail.tsx:1585` 每个 WS `tool_call` 事件触发 invalidate → 再放大 2 个 HTTP 重拉（runtime-summary + work-ledger）。
- `AgentChatSection.tsx:3250,3262` workbench 查询 staleTime 仅 10s：每次切 session、每次窗口 refocus 重拉 2.77MB；分支 session 拉**双份**（gitline-axis 对 root session 再拉一整份 workbench）。
- 聊天流本身已走 WS（`AgentChatSection.tsx:1496`）——这层 HTTP 轮询是**双轨冗余**。
- `nginx.conf:62-64` `proxy_next_upstream` 含 `timeout`：池饱和时 GET 超时被 nginx 自动重放一次，流量放大器。

## 3. 修复方案（五组，组内一次改完零债）

### A 组 — 事件循环解阻塞（最高优先）

- **A1** T0 读路径：`replay_t0_session_events` 增加尾部读取模式（按 segment 倒序读，凑满 limit 即停），`_load_events` 语义改为"最新 N 条"；全部文件 IO 用 `asyncio.to_thread` 移出事件循环。同步核验并修复 `[:limit]` 取最早 N 条的疑似 bug，测试钉死"取最新"。
- **A2** 响应序列化：FastAPI `default_response_class=ORJSONResponse`；`pyproject.toml` 当前**无显式 orjson 依赖**，必须显式加入（不赌 `fastapi[standard]` extra 隐式携带）。MB 级序列化从百 ms 级降到十 ms 级。
- 验收：20 并发 workbench 压测下 `/api/health` TTFB p95 < 200ms。

### B 组 — 重接口拆轻（workbench + transcript）

- **B1** workbench 默认瘦身：timeline 默认最新 50 条；`tool_calls` / `hooks` / `compactions` 改为 `?include=` 显式请求；全量回放走 transcript 端点。字段不删、默认值变化，**前端适配同批交付**。
- **B2** teams 的 N+1 members 查询合并为单 IN 查询。
- **B3** export 保持全量（导出语义），但同走 A1 线程池路径。
- **B4** transcript 瘦身：端点已有 `after_sequence`/`limit` 分页（`chat_sessions.py:1816-1817`）——前端 `getSessionTranscript` 改为增量拉取（首屏最新 N 条 + 向上翻页），后端默认 `limit` 从 500 收紧；核验单事件序列化体积（465KB/500条 ≈ 平均 1KB/条，注意大 payload 事件裁剪）。**实现约束（owner 拍板 2026-07-02）**：不得破坏现有 `after_sequence` 增量语义——要么新增 `before_sequence`/`direction=backward`，要么首屏 tail 查询内部按尾部读取但返回仍为升序，前端按 sequence 正常合并。
- 验收：默认 workbench 响应 < 100KB、服务端耗时 < 300ms；transcript 首屏 < 100KB；**同等操作路径下异常 499 不再成簇/不再由大响应等待触发**（用户主动切页、关闭窗口产生的合法 499 不计入）；nginx temp file 警告消失。

### C 组 — 连接池与 daemon 治理

- **C1** daemon 扇出上限：heartbeat / trigger / dream 派发各加 `asyncio.Semaphore`（env 可配，默认 heartbeat 4 / trigger 8 / dream 2），超限排队不丢。
- **C2** 观测 + 池参数环境变量化：`DB_POOL_SIZE` / `DB_MAX_OVERFLOW` / `DB_POOL_TIMEOUT`；`/api/health` components 增加 pool 占用快照（checkedout / size / overflow）——修复是否有效、L3 谁在占池，看这个读数。同时加 **app-side timing**（请求耗时中间件或容器内 health 采样打日志），用于终证/排除 L1 的"事件循环阻塞 vs edge 链路"归因。
- **C3** `enter_rls_bypass` finally 修复：restore 前检测事务态，invalid 则先 rollback（或跳过 restore 交外层回滚）；回归测试钉住"bypass 内抛 DB 异常不再产生二次报错"。
- 验收：QueuePool timeout 日志清零；health pool checkedout 峰值 < 50%。

### D 组 — 前端流量收敛

- **D1** `chat-active-run`：idle session 停止轮询（run 状态由已有 WS 事件驱动），live 时保留 3s 兜底。
- **D2** WS `tool_call` → invalidate 节流（2s 窗口合并）。
- **D3** workbench 查询：staleTime → 60s、`refetchOnWindowFocus: false`；分支场景 checkpoints 优先用 index（已有逻辑），仅 index 缺失才拉 workbench。
- **D4** work-ledger live 轮询 3s → 5s。
- 验收：idle chat tab 稳态 ≤ 4 req/min；live ≤ 20 req/min（不含 WS）。

### E 组 — 边缘修正（小）

- **E1** `nginx.conf` `proxy_next_upstream` 去掉 `timeout`（保留 `error http_502 http_503 http_504` 覆盖 redeploy 场景），消除超时重放放大。
- **E2** 静态资源 / edge 抖动：不动，持续观察。

### F — 进程拆分（独立一仗，本轮不动手，需拍板）

API 与 daemon 分进程（同镜像，`HIVE_ROLE=api|daemon` 或 daemon 开关；Railway 两个 service）：API 进程才能上 `--workers` 横向扩展，daemon 独立池不再与用户请求抢连接。约束：worker 数 × pool_size 必须 < Postgres max_connections。**注意：在拆分之前直接加 `--workers` 是错的**——daemon 会跑 N 份重复派发。A–E 落地后若压力仍在，这是下一仗；现在拆反而掩盖 A–E 的效果验证。

## 4. 交付顺序

1. **C2 观测先行**（pool 快照进 health + app-side timing）——没有读数，L1/L3 的归因无法终证，后续修复无法验收。
2. **A 组**（全局解阻塞）。
3. **B 组**（接口瘦身，含前端适配）。
4. **C1 / C3 + D 组**（供压收敛）。
5. **E1**。

每组独立 commit，附修复前后读数（health TTFB、workbench 大小/耗时、pool 峰值、req/min）。

## 5. 实施台账（2026-07-02 拍板当日完成）

| 组 | Commit | 内容 | 证据 |
|----|--------|------|------|
| C2 观测 | `41c5bada` | DB_POOL_SIZE/MAX_OVERFLOW/TIMEOUT env 化；health 加 db_pool 快照（100% 饱和降级）+ event_loop lag monitor；Server-Timing 头 + slow_request WARN | 12 红→18 绿；tests/api 636 passed |
| A 解阻塞 | `e4c4fa65` | replay_t0_session_events_tail（segment 倒序尾读提前停）；_load_events 走 to_thread + 语义改"最新 N 条"（修 [:limit] 取最早的 bug）；全局 ORJSONResponse + orjson 显式依赖 | 5 红→37 绿；api+services+runtime+memory 4010 passed |
| B 拆轻 | `ad51ba6b` | workbench 默认 50 条窗口、heavy sections 走 ?include=（前端零消费确认）、export 保持全量；teams N+1 合并；transcript direction=backward/before_sequence（after_sequence 契约不变）、默认 limit 200；前端首屏 tail 100 条 + 加载更早翻页 | 后端 8 红→57 绿、3294 passed；前端 typecheck 清、416 passed |
| C1/C3 池治理 | `b02a451c` | heartbeat/trigger/dream 扇出 semaphore（4/8/2，env 可配，排队不丢）；enter_rls_bypass finally 失败事务跳过 restore、restore 失败只 log 不遮蔽原始异常、非法 tenant fail-closed | 6 红→18 绿；daemon+RLS 167 passed；api+services 3074 passed |
| D 前端收敛 | `adacaccf` | chat-active-run 撤销无条件 3s（idle 靠 WS 事件驱动、live 才 3s 兜底）；tool_call invalidate 2s 节流；workbench/index staleTime 60s + 关 focus refetch + 分支轴 workbench 仅 index 缺 checkpoints 才拉；work-ledger 3s→5s | 2 红→101 绿；全量 70 文件/417 passed |
| E1 nginx | 本次 | proxy_next_upstream 去掉 timeout（保留 error/502/503/504 覆盖 redeploy） | nginx 配置测试 2 passed |

收官全量：backend `pytest tests` → **5314 passed, 1 skipped, 0 failed**；frontend 全量 417 passed + `tsc --noEmit` 干净。

**部署后验收动作（生产读数，待部署执行）**：
1. `/api/health` 看 `db_pool.checked_out` 峰值（目标 <50%）与 `event_loop.max_lag_ms`（终证 L1 归因）。
2. 公网 curl 对比 `Server-Timing: app;dur=` 与 total（差值=edge 链路，区分 L1 vs 网络抖动）。
3. Railway 日志确认 QueuePool timeout 清零、`slow_request` WARN 定位残余慢端点。
4. frontend 日志确认 workbench 响应 <100KB、transcript 首屏 <100KB、同等操作路径 499 不再成簇。

## 6. 明确不做

- **重启止血**：可临时释放池但不根治，owner 手动决定，不占方案位。
- **代码量瘦身 / 删功能**：本轮病灶是运行时肥胖，不是代码肥胖。
- **直接加 uvicorn `--workers`**：daemon 会跑 N 份，必须先做 F 的进程拆分。

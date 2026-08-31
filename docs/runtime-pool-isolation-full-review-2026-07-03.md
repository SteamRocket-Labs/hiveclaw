# Runtime Pool Isolation 全面审查报告(2026-07-03)

**基准**: HEAD `820e312a` + 工作区修复快照(2026-07-03 01:40 GMT+8)。本版已把上一版报告里的双进程阻断点 B1-B5 和两条实质 P1 运行风险落地修复。

**总判**: Runtime pool isolation 的主线现在可以进入统一部署验证。API plane 不再直接碰 T0/volume/本地 cancel event；runtime plane 接管执行、cancel、idle/close hook、business task claim；compose 单机路径也已恢复为可登录可代理形态。

---

## 一、已修复的阻断点

| 项 | 原问题 | 当前修复 |
|---|---|---|
| B1 cancel 跨进程失效 | API 只把 DB 状态改 killed，runtime 本地 `_CANCEL_EVENTS` 收不到信号，agent 可能继续跑 | 新增 `runtime_control_bus`，API 发布 `web_chat_cancel`/`delegation_cancel` 到 Redis channel，runtime listener set 本地 cancel event / cancel delegation task；DB killed 仍是 fallback truth |
| B2 API T0 split-brain | `append_session_event(... bridge_to_t0=True)` 在无 volume API 容器写临时盘 T0 | `chat_transcript._bridge_to_t0_enabled()` 按 `HIVE_PROCESS_ROLE=api` 禁止 T0 bridge；API 只写 DB transcript，T0 文件写入留给 runtime |
| B3 enterprise KB 路由错位 | nginx 把 `/api/enterprise/...` 送到无 volume `backend-api`，上传/列表读临时盘或空目录 | nginx group2 移除 `enterprise`；API role 白名单移除 enterprise prefix/exact，enterprise KB/info 回 runtime |
| B4 WS idle/close hook 在 API 触发 memory pipeline | `/ws/chat` 由 API 持连接，idle/close inline `emit_hook` 会触碰 T0/T2 volume | 新增 `_emit_ws_session_lifecycle_hook()`；API role 只投递 runtime control event，runtime role 执行 `SESSION_IDLE/SESSION_CLOSE` hook |
| B5 compose 登录 502 | nginx 硬编码 `backend-api:8000`，compose 没有 backend-api service，端口也和 nginx listen 不一致 | compose frontend 注入 `BACKEND_HOST=backend:8000`，端口为 `3008 -> 80`，本地 compose 塌缩回单 backend |

---

## 二、额外收口

1. `trigger_task` 旧路径已收编：不再 `asyncio.create_task(execute_task(...))` 进程内直跑，改为复用 `_enqueue_business_task_execution()` 创建 `RuntimeTask(task_type="business_task")`，由 worker claim/预算执行。
2. `business_task` 执行异常已收敛：`_execute_claimed_business_task()` 捕获 executor 异常后把 RuntimeTask 标 `failed`，避免卡在 `running`。
3. startup resume 竞态已由门闩修复：worker 首次 claim 等待 resume/reconcile 完成，避免重启时误杀/双 spawn。
4. 迁移链、head 常量、plan gate、agent_message 越权语义这些上一轮红项已经纳入测试并转绿。

---

## 三、关键实现点

- `backend/app/services/runtime_control_bus.py`: Redis control channel，承载 `web_chat_cancel`、`delegation_cancel`、`session_lifecycle_hook`。
- `backend/app/main.py`: runtime role 启动 `runtime_control_listener`；API role 继续只启动 stream forwarder，不启动 worker/daemon/runtime hook 执行面。
- `backend/app/services/web_chat_runtime.py`: cancel API 本地 set event 后同时发布跨进程 cancel。
- `backend/app/agents/orchestrator.py`: delegation cancel 支持 runtime control 投递，并保留 owner/fallback registry 的 forbidden 语义。
- `backend/app/services/chat_transcript.py`: API role 禁止 T0 bridge。
- `backend/app/api/websocket.py`: WS idle/close hook 在 API role 转投 runtime control bus。
- `frontend/nginx.conf`: enterprise 从 API-plane regex 移除，落回 runtime。
- `docker-compose.yml`: frontend 本地代理恢复为 `BACKEND_HOST=backend:8000` 和 `3008:80`。

---

## 四、验证证据

**后端全量**

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests -q
# 5359 passed, 1 skipped, 6 warnings in 93.13s
```

**B1-B4 / P1 目标与相关回归**

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_runtime_control_bus.py \
  tests/services/test_web_chat_runtime.py::test_cancel_web_chat_run_sets_cancel_event_and_marks_runtime_task_killed \
  tests/services/test_chat_transcript.py::test_append_session_event_api_role_does_not_bridge_to_t0 \
  tests/test_startup_background_config.py::test_api_role_path_boundary_allows_control_plane_and_rejects_volume_paths \
  tests/api/test_websocket_call_llm.py::test_emit_ws_session_lifecycle_hook_api_role_publishes_runtime_control \
  tests/api/test_plan_mode_rest_gate.py \
  tests/services/test_runtime_task_worker.py \
  tests/agents/test_orchestrator.py -q
# 86 passed, 3 warnings
```

**lint / frontend / compose**

```bash
cd /Users/example-owner/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
ruff check app/services/runtime_control_bus.py app/services/web_chat_runtime.py app/agents/orchestrator.py app/services/chat_transcript.py app/api/websocket.py app/main.py app/api/tasks.py app/services/runtime_task_worker.py tests/services/test_runtime_control_bus.py tests/services/test_web_chat_runtime.py tests/services/test_chat_transcript.py tests/test_startup_background_config.py tests/api/test_websocket_call_llm.py tests/api/test_plan_mode_rest_gate.py tests/services/test_runtime_task_worker.py
# All checks passed!

cd /Users/example-owner/vc-saas/hiveclaw-main/frontend
npm run build
# tsc && vite build succeeded

cd /Users/example-owner/vc-saas/hiveclaw-main
docker compose config | sed -n '68,105p'
# frontend: BACKEND_HOST=backend:8000, published 3008 -> target 80
```

---

## 五、剩余非阻断债

1. `claim_expires_at` 仍不是完整 lease 机制：现在不能简单按过期重抢，否则长任务可能重复执行。正确修法需要 running task lease renewal + orphan reclaim 契约，建议单独设计和红测。
2. Redis Stream 的 replay 仍主要靠 DB transcript 兜底，Stream 当前更像 live fanout/短缓冲，不是完整 replay source。若要把 §8 的短窗口重放做实，需要补 xread/xrange consumer 和 sequence replay 测试。
3. worker 满载饥饿仍需调度策略：父 run 等子 delegation 时可能占槽，后续需要为 child/delegation 预留 capacity 或改父等待方式。
4. `entrypoint` 里的 alembic non-fatal 策略仍有风险：迁移失败继续启动会把缺列延迟到运行时爆。生产部署建议继续坚持 runtime 先、api 后，并把迁移失败改 hard-fail 另开一刀。

---

## 六、部署前核对

1. Railway runtime/backend service: `HIVE_PROCESS_ROLE` 不是 `api`，worker/daemon env 按生产需求开启。
2. Railway `backend-api` service: `HIVE_PROCESS_ROLE=api`，不要开启 daemon/worker。
3. frontend env: `BACKEND_API_HOST` 指向 backend-api，`BACKEND_RUNTIME_HOST` 指向 backend；enterprise 路由现在应落 runtime。
4. 部署顺序: runtime/backend 先，backend-api 后，frontend 最后。

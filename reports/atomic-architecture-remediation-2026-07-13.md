# 原子化架构断点整改记录

## 1. 记录边界

- 审查基线：`reports/atomic-architecture-audit-596dab1.md`。
- 执行规则：每个稳定编号独立完成 Red → Green → 回归 → 证据登记 → commit；不得把多个断点打包成一个无法审计的大改。
- 状态规则：只有七个原子和生产验收均成立才标记“闭环”。本地源码已修、但 production cutover 尚未执行的条目只能标记“局部闭环”。
- 高风险边界：production migration、backfill、rebind、delete、deploy 必须先 dry-run、备份并取得明确生产确认；本记录中的本地代码提交不代表已执行生产变更。

## 2. 整改总表

| 编号 | 当前整改状态 | 本轮结论 | 独立提交主题 |
|---|---|---|---|
| SEC-001 | 局部闭环 | current checkout 的 migration/startup gate 已 fail-closed；production strict RLS cutover 待确认执行 | `fix(security): fail closed on tenant schema drift` |

## 3. [SEC-001] tenant NULL / RLS migration 启动门禁

### 3.1 本轮边界与根因

本轮只关闭 `SEC-001` 的 current-checkout 启动旁路，不混入 `DATA-001` 的 operator restore/rebind UI：

1. `backend/entrypoint.sh` 曾把 `alembic upgrade head` 的非零退出吞成 warning，随后仍启动 `uvicorn`。
2. migration 后没有强制运行现有的 `audit_tenant_null_semantics --fail-on-legacy-null`，因此 schema 已到 head 也不能证明 strict tenant-owned 表已经无 NULL。
3. `RLS_BACKFILL_ON_DEPLOY=1` 仍可在后台启动旧 Stage-2b backfill；该旁路不阻塞健康检查，也不受 post-migration acceptance gate 约束。
4. `HIVE_PROCESS_ROLE=api` 会跳过所有 schema mutation，但原实现也跳过只读 readiness audit，rolling deploy 时可能先用旧 RLS 接受流量。

### 3.2 变更

- `backend/entrypoint.sh:34-41,176-194`
  - runtime 与 API role 共用一个 strict tenant NULL schema audit gate。
  - Alembic 失败立即以非零状态退出，禁止启动应用。
  - data migration 后以 schema owner 连接执行 strict tenant NULL 只读审计；审计非零立即退出。
  - API role 不执行 mutation，但必须通过同一个 owner read-only audit 才可启动，避免 rolling version 窗口。
- `backend/entrypoint.sh:190-195`
  - 保留既有 RLS role bootstrap 顺序。
  - 删除后台 `backfill_stage2b_tenant_id` convenience path；tenant 归属迁移只由 Alembic migration + quarantine contract 负责。
- `backend/tests/deploy/test_backend_dockerfile.py:18-153`
  - 对真实 `entrypoint.sh` 做 process-boundary 行为测试；仅用可控命令替身隔离 Python/DB/uvicorn 外部进程。
  - 覆盖 Alembic 失败、runtime/API audit 失败、成功启动和旧 background backfill 不再执行。
- 2026-07-13 production 只读变量键名检查：`backend-api` 同时配置 `DATABASE_URL`、`SCHEMA_DATABASE_URL`、`HIVE_PROCESS_ROLE`；检查未读取或记录任何 secret value，因此 API role 可使用既有 schema-owner connection 做 readiness audit。

### 3.3 七原子复核

| 原子 | current checkout 证据 | 状态 |
|---|---|---|
| 输入 | 容器启动输入为 `SCHEMA_DATABASE_URL`/`DATABASE_URL`、当前 Alembic graph 和历史 tenant rows | 已连接 |
| 权威 | schema owner 执行 migration；tenant-owned authority 由 non-null `tenant_id` 和现有 strict migration 决定 | 已连接 |
| 执行 | runtime 只有 `alembic upgrade head` → data migration → strict audit → app start；API 只有 read-only strict audit → app start | 已连接 |
| 证据 | Alembic exit status、只读 NULL audit exit status、quarantine receipts 和测试 call log | 已连接 |
| 恢复 | migration/audit 失败时容器非零退出且不接受流量；无法推导的历史行仍由既有可逆 quarantine 保留 | 已连接 |
| 消费 | runtime/API 的 `uvicorn` 都只在 strict audit 成功后消费 schema；旧 background writer 已移除 | 已连接 |
| 验收 | shell 行为测试、真实 PostgreSQL migration/RLS tests、audit contract、Alembic single head 均通过；production 验收待执行 | 局部连接 |

### 3.4 TDD 与验证证据

Red：

```text
cd backend
source .venv/bin/activate
pytest tests/deploy/test_backend_dockerfile.py -q

3 failed, 2 passed
- Alembic exit 23 后 entrypoint 仍 return 0 并启动 uvicorn
- tenant audit exit 29 未被调用，entrypoint 仍 return 0
- success path 仍调用 background backfill，且没有 strict audit
```

Green（启动门禁）：

```text
pytest tests/deploy/test_backend_dockerfile.py -q
5 passed in 6.21s
```

第二轮 Red（API rolling-start gate）：

```text
pytest tests/deploy/test_backend_dockerfile.py -q
2 failed, 5 passed
- API role 的 tenant audit exit 29 未被调用，仍 return 0 并启动 uvicorn
- API success path 没有执行 read-only strict audit
```

第二轮 Green：

```text
pytest tests/deploy/test_backend_dockerfile.py -q
7 passed in 11.86s
```

真实 PostgreSQL migration、policy 与 audit 回归：

```text
pytest tests/deploy/test_backend_dockerfile.py \
  tests/architecture/test_tenant_null_semantics.py \
  tests/scripts/test_audit_tenant_null_semantics.py \
  tests/migrations/test_tenant_null_semantics_migration.py -q
15 passed in 15.76s
```

相邻启动/Alembic contract：

```text
pytest tests/test_alembic_bootstrap.py tests/architecture/test_entrypoint_model_imports.py -q
16 passed in 0.51s

ruff check tests/deploy/test_backend_dockerfile.py
All checks passed!

bash -n backend/entrypoint.sh
# exit 0

alembic heads
hr_draft_recovery_0712 (head)
```

### 3.5 尚未完成的生产验收

因此 `SEC-001` 当前只能标记“局部闭环”，不能虚报“闭环”。生产关闭仍要求：

1. owner-role preflight dry-run 与可恢复备份。
2. 明确确认后执行 strict migration；不得猜测 residual tenant 归属。
3. production 所有 strict tenant tables 的 NULL count 为 0，policy 不含 tenant-owned NULL bypass。
4. `app_rls` 跨 tenant 读写矩阵全部拒绝。
5. `backend`、`backend-api`、`frontend` 部署同一 current commit 且均为 `SUCCESS`。
6. quarantine operator lifecycle 由 `DATA-001` 独立关闭，不在本提交伪装完成。

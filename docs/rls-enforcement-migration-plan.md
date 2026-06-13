# RLS 真正生效迁移方案（Goal-2 地基的下一仗）

> 状态：**设计稿，待 owner 拍板**。不动代码——这是 high-risk 权限层迁移（不可逆、影响所有查询），按交付纪律先文档定稿再实现。
> 前置：`docs/archive/legacy-docs/system-audit-2026-06-09.md` P0-2（RLS 是摆设）+ `app/scripts/audit_rls_coverage.py`（量化脚本，已上线）。
> 现状证据来自 2026-06-09 全量调查（184 bare session 清单 + 表覆盖矩阵 + GUC 机制）。

## 1. 目标与非目标

**目标**：让 PostgreSQL RLS 成为租户隔离的**真实 DB 级防线**——任何漏带 `tenant_id` 过滤的查询在 DB 层 fail-closed，而不是靠应用层 WHERE 自觉。

**非目标**：不重写业务逻辑，不改数据模型语义，不引入新的隔离范式。只做"让已存在的 RLS 机制真正强制"。

## 2. 现状：地基已就位，只差翻开关 + 堵绕过

**关键发现（核实）**：GUC 注入基础设施前人已全部建好，这不是从零搭建：

| 组件 | 位置 | 状态 |
|------|------|------|
| 请求级 tenant ContextVar | `database.py:27` `_current_tenant_id` + `set_current_tenant` | ✅ 已用 |
| Middleware 从 JWT 设 ContextVar | `core/tenant_middleware.py` 读 `tid` → `set_current_tenant` | ✅ 已用 |
| `get_db()` 依赖自动设 GUC | `database.py:43-68` 从 ContextVar `SET LOCAL app.current_tenant_id` | ✅ 已用 |
| `tenant_scoped_session(tid)` | `database.py:81-116` 显式设 GUC 的 session | ⚠️ 存在但生产几乎未用 |
| `enter_rls_bypass(reason=)` | `database.py:131-176` 审计化跨租户 | ⚠️ 存在但生产未用 |
| RLS policy（16 表） | `db_bootstrap.py` + 3 个 alembic migration；policy 含 `BYPASS`/`tenant_id NULL` 逃逸 | ✅ 已建 |
| 覆盖审计脚本 | `app/scripts/audit_rls_coverage.py` | ✅ 已上线（本轮 Phase 1D） |

**结论**：标准 request 路径（`Depends(get_db)`）**已经 GUC-aware**——RLS 一旦强制，它们就正确工作。这把这场仗从"搭基础设施"降级为"翻开关 + 堵三个缺口"。

## 3. 三个缺口（证据）

### 缺口 A — app 以表 owner 连接（翻开关的那步）
`db_bootstrap.py:39-41` 自认："the production connection IS the table owner … so ENABLE alone is inert there"。owner 绕过非 FORCE 的 RLS。只有 workflow/coordination 7 表加了 FORCE，9 张核心表只 ENABLE（= 摆设）。

### 缺口 B — 184 处 bare `async_session()` 绕过 get_db（= 绕过 GUC）
全量调查（`audit_rls_coverage` 同源方法）：
| 类别 | 数量 | 性质 | 迁移目标 |
|------|------|------|----------|
| REQUEST-scoped | ~139 | 直接 `async_session()` 而非 `Depends(get_db)`，但 tenant_id 在 scope 内（current_user / 参数 / 已加载实体） | → ContextVar 已设时直接复用，或 `tenant_scoped_session(tid)` |
| DAEMON/后台 | ~38 | trigger/heartbeat/evolution/stream，无 request context，ContextVar 为空 | → 循环内 `set_current_tenant` + GUC session，或 `enter_rls_bypass` |
| SCRIPT | ~4 | 运维脚本（含本轮的 scrub/audit），合法跨租户 | → `enter_rls_bypass(reason=)` |

> ⚠️ 184 是**上界**：其中部分可能查全局表（无 tenant_id，RLS 不管）或实际经 ContextVar 已 GUC-aware。**阶段 0 影子验证负责把它精确化**——真正需要改的只是"FORCE 后返回空"的那些。

### 缺口 C — agent-scoped 表无 policy（~20 表）
chat_sessions / tasks / runtime_tasks / triggers / channel_configs / notifications / chat_messages 等有 `agent_id` 但无 `tenant_id` 列（Phase 1 已确认）。FORCE 后它们要么不可访问，要么需要 join policy。

## 4. 关键设计决策（待 owner 拍板）

### D1 — 翻开关方式：非 owner 角色 vs 全表 FORCE
- **方案 A（推荐）非 owner 连接角色**：建一个 `app_rls`（NOLOGIN BYPASSRLS=false）角色，app 用它连接。**一处切换**即对所有表生效，回滚 = 切回 owner 角色。
- 方案 B 全表 FORCE：每张租户表加 `FORCE ROW LEVEL SECURITY`。需逐表 migration，回滚要逐表撤。
- **推荐 A**：单点开关 = 单点回滚，最干净。`DATABASE_URL` 一个值切换。

### D2 — agent-scoped 表：加 tenant_id 列 vs join policy
- **方案 A（推荐）加 `tenant_id` 列 + 标准 policy**：schema migration + 从 agent 回填 + 复合索引。性能好（无子查询）、policy 统一、未来查询可直接按 tenant 过滤。
- 方案 B join policy：`agent_id IN (SELECT id FROM agents WHERE tenant_id = current_setting(...))`。零 schema 改动，但每次查询带子查询（性能）+ policy 不统一。
- **推荐 A**：一次性 schema 成本换长期性能与一致性；和现有 tenant_id-列表同构。

### D3 — 降工作量的核心手段：ContextVar-driven GUC（避免 184 处手改）
不要 184 处手动传 tenant_id。而是：daemon/后台在拿到 tenant_id 后 `set_current_tenant(tid)`，所有 session 经一个**统一 GUC-aware 工厂**（让 bare `async_session()` 也自动从 ContextVar 设 GUC，与 get_db 同源）。这样 184 处的多数变成"确保 ContextVar 设好"，而非逐处改签名。**这是把工作量从线性 184 降到"少数注入点 + daemon 循环"的关键。** 需在阶段 1 先建这个工厂。

### D4 — daemon：per-tenant session vs bypass
按是否**真跨租户**分：trigger/heartbeat/evolution/stream 的 agent 级工作 → per-tenant（`set_current_tenant(agent.tenant_id)`）；真正的跨租户清理（evolution_daemon 的 cleanup、全局扫描）→ `enter_rls_bypass(reason=)`（审计化）。判据：这段逻辑该不该看到别的租户？

## 5. 分阶段方案（每阶段可独立验证；最后一步是回滚开关）

### 阶段 0 — 影子验证（把 184 精确化，不改生产）
- staging 建非 owner 角色 + 全表 FORCE（或临时 FORCE）。
- 跑全量测试 + 回放代表性流量。
- 收集所有"返回空 / 报错"的查询 = **精确的待迁移 accessor 清单**（远少于 184）。
- 产出：真实工作清单 + 优先级。**风险零**（staging）。

### 阶段 1 — accessor 迁移（主要工作量，可增量）
1. 先建 D3 的 ContextVar-driven GUC 工厂 + 测试。
2. daemon 循环逐个接 `set_current_tenant` / `enter_rls_bypass`（trigger/heartbeat/evolution/stream，~8-10 个循环）。
3. 阶段 0 清单里的 request-scoped 直接 session 逐个迁（优先高频 + 高敏感表）。
4. 每迁一处加测试证明 GUC 设置（钉死，防回归）。
5. 持续用 `audit_rls_coverage` + 影子环境验证收敛。

### 阶段 2 — agent-scoped 表补 policy（D2 方案 A）
- 加 `tenant_id` 列 migration + 从 agent 回填（dry-run + 确认门，不可逆数据步）。
- 复合索引 `(tenant_id, ...)`。
- 加 ENABLE policy（与核心表同构）。

### 阶段 3 — 翻开关（D1 方案 A）+ 回滚预案
- 前两阶段在影子环境 100% 绿后，生产切 `DATABASE_URL` 到非 owner 角色。
- **回滚 = 切回 owner 角色**（GUC 仍设但不强制，无害）——发现遗漏 accessor 立即恢复。
- 灰度：先 staging 全量 + 影子流量，再生产低峰切，盯 error/空结果指标。

## 6. 风险 / 回滚 / 验收

| 风险 | 缓解 |
|------|------|
| 遗漏 accessor → FORCE 后返回空 → 生产崩 | 阶段 0 影子验证穷举 + 阶段 3 角色开关秒级回滚 |
| 跨租户 daemon 误锁 | D4 显式 `enter_rls_bypass` + 审计日志 |
| agent-scoped 回填错误 | dry-run + 确认门（同 Phase 1 scrub 模式） |
| join policy 性能（若选 D2-B） | 推荐 D2-A 加列避免 |

**验收**：① 影子环境全量测试 + 流量零空结果；② `audit_rls_coverage` 报告 UNPROTECTED=[] 且 INERT=[]（全部 ENFORCED）；③ 一个红队测试：构造跨租户查询，DB 层拒绝；④ 角色回滚演练通过。

## 7. 工作量（粗估，阶段 0 后精确化）
- 阶段 0：1-2 天（建影子环境 + 回放）。
- 阶段 1：~2-3 周（ContextVar 工厂 + daemon 循环 + 清单内 accessor，取决于阶段 0 精确数）。
- 阶段 2：~3-5 天（加列 + 回填 + policy）。
- 阶段 3：~2-3 天（角色 + 灰度 + 回滚演练）。

**总计 ~3-4 周**，但**可增量上线**：阶段 0-2 不改变生产行为（只加 GUC + 不翻开关），随时可停；只有阶段 3 翻开关是"那一刻"，且有秒级回滚。
